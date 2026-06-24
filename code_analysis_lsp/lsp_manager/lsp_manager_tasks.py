# lsp_manager_tasks.py
# --------------------------------------------------
# Description:
#   Core logic for the manager.
#   Holds global state (clients, futures) and task executors (fast/slow lanes).
#   Called by ``lsp_server_manager.py``.
# --------------------------------------------------

import json
import shutil
import time
from collections import defaultdict
from typing import Optional, Dict, Any

import zmq
import os
import threading
import sys
import signal
from concurrent.futures import ThreadPoolExecutor,Future
from code_analysis_lsp.lsp_client.lsp_client import LSPClient
from code_analysis_lsp.lsp_config import (
    ANDROID_HOME_PATH,
    GRADLE_GLOBAL_CACHE_PATH,
    JAVA_HOME_DEFAULT_PATH,
    LANGUAGE_SERVERS,
    ZMQ_ENDPOINT,
)
from code_analysis_lsp.lsp_client.lsp_errors import (
    LSPInitError, NON_RETRIABLE, classify_init_error_from_text
)
from code_analysis_lsp.lsp_manager.memory_guard import (
    MemoryGuard, bytes_from_gb, client_activity_scope
)
from code_analysis_lsp.utils.java_support import (
    resolve_workspace_and_path,
    detect_module_root,
    JavaWorkspace,
    JAVA_STD_SEGMENTS,
)


# --- Global state ---
initialized_clients = {}
client_lock = threading.Lock()
# Future objects that track background initialization tasks.
initialization_futures = {}
futures_lock = threading.Lock()

# --- Memory management ---
# Can be overridden by environment variables; defaults to 80GB.
MEMORY_GUARD_THRESHOLD_GB = float(os.environ.get("LSP_MEMORY_GUARD_THRESHOLD_GB", "80"))
MEMORY_GUARD_THRESHOLD_GB = MEMORY_GUARD_THRESHOLD_GB * 1.8
MEMORY_GUARD_GRACE_SECONDS = int(os.environ.get("LSP_MEMORY_GUARD_GRACE_SECONDS", "60"))
# Only evict the oldest idle client from these languages.
EVICT_LANGS = {"go", "python", "javascript", "ruby"}

# Client metadata used for activity and eviction tracking.
CLIENT_REGISTRY = {}  # { client_key: {"lang": str, "created_at": float, "last_used": float, "active": int} }

MEM_GUARD = MemoryGuard(
    threshold_bytes=bytes_from_gb(MEMORY_GUARD_THRESHOLD_GB),
    allowed_langs=EVICT_LANGS,
    grace_seconds=MEMORY_GUARD_GRACE_SECONDS,
)

def _on_evict_cleanup(victim_key, _victim_client):
    # Optionally remove the matching initialization future to avoid stale state.
    with futures_lock:
        initialization_futures.pop(victim_key, None)


# --- Timeout configuration ---
# 1. Handshake (initialize) timeout.
default_soft = 60 * 1  # 1 minute
default_hard = 60 * 3  # 3 minutes

# 2. Global indexing timeout.
SERVERS_WITH_INDEXING_PROGRESS = {"java", "csharp"}
DEFAULT_GLOBAL_INDEXING_TIMEOUT = 60 * 15  # 15 minutes
DEFAULT_INCREMENTAL_INDEXING_TIMEOUT = 60 * 5  # 5 minutes after did_change / switch.

# 3. File-diagnostics timeout.
SHORT_DIAG_SECOND = 60 * 1  # 1 minute
LONG_DIAG_SECOND = 60 * 2  # 2 minutes
DEFAULT_DIAG = {"go": SHORT_DIAG_SECOND,
                "python": SHORT_DIAG_SECOND,
                "javascript": SHORT_DIAG_SECOND,
                "ruby": LONG_DIAG_SECOND,  # Ruby is slightly slower.
                "java": LONG_DIAG_SECOND,
                "php": LONG_DIAG_SECOND,
                "csharp": LONG_DIAG_SECOND,
                }

# --- Failure circuit breaker ---
LAST_INIT_FAIL_REASON = {}  # { client_key: {"until": epoch_seconds, "reason": "INIT_HARD_TIMEOUT"/"INIT_ERROR", "message": str} }
fail_reason_lock = threading.Lock()

# --- Proxy and Java configuration ---
PROXY = {
    "SOCKS_HOST": "127.0.0.1",
    "SOCKS_PORT": "1080",   # Port used by autossh -D.
    "HTTP_HOST":  "127.0.0.1",
    "HTTP_PORT":  "8118",   # Privoxy port; optional if Privoxy is unused.
    "NO_PROXY":   "localhost,127.0.0.1,.local"
}
java_ws = JavaWorkspace()

# --- Semaphores ---
LANG_LIMITS = { 'java': 4, 'csharp': 2 }   # Tune this per machine if needed.
lang_semaphores = {k: threading.BoundedSemaphore(v) for k, v in LANG_LIMITS.items()}
repo_semaphores = defaultdict(lambda: threading.BoundedSemaphore(1))

# ---
# get_or_create_lsp_client (handshake only)
# ---
def get_or_create_lsp_client(project_root, lang_id, solution_file=None, force=False):
    """
    Runs in the slow lane.
    Creates the client instance and performs the LSP initialize handshake only.
    It does not wait for indexing.
    """

    err_msg = None

    # --- Compute the client key. ---
    if lang_id == 'csharp':
        if not solution_file:
            err_msg = "[Manager Worker] ERROR: C# request requires a solution_file."
            print(err_msg)
            return None, err_msg
        client_key = (project_root, lang_id, solution_file)
    else:
        client_key = (project_root, lang_id)

    # --- Fast-fail non-retriable errors unless force=True is set. ---
    with fail_reason_lock:
        last_reason = LAST_INIT_FAIL_REASON.get(client_key)
        if last_reason in NON_RETRIABLE and not force:
            err_msg = (
                f"Previous initialization failed with a non-retriable reason: {last_reason}. "
                f"Failing fast this time. Pass force=True to retry anyway."
            )
            print(f"[Manager Worker] {err_msg}")
            return None, err_msg

    # --- Reuse an existing healthy instance when available. ---
    with client_lock:
        v = initialized_clients.get(client_key)
        if isinstance(v, LSPClient):
            return v, err_msg

    # --- Keep expensive operations outside the lock. ---
    print(f"[Manager Worker] Creating new client for key: {client_key}...")
    config = LANGUAGE_SERVERS[lang_id]
    server_cmd = config["server_cmd"].copy()

    project_name = os.path.basename(project_root)
    # Build the server command.
    if config["workspace_strategy"] == "PER_PROJECT":
        if config['workspace_base_path']:
            workspace_dir = os.path.join(config['workspace_base_path'], project_name)
            os.makedirs(workspace_dir, exist_ok=True)
            if lang_id == "java":
                server_cmd.append(workspace_dir)
            elif lang_id == "php":
                server_cmd.append(f"--storage-path={workspace_dir}")

        if lang_id == "csharp":
            if solution_file and os.path.exists(solution_file):
                server_cmd.extend(["-s", solution_file])
            else:
                with client_lock:
                    initialized_clients[client_key] = None
                err_msg = "C# initialization failed: no valid solution_file (.sln) was provided."
                return None, err_msg

    # --- Build environment variables dynamically. ---
    env = os.environ.copy()

    # Inject proxy environment variables in a standard way.
    env["ALL_PROXY"] = f"socks5h://{PROXY['SOCKS_HOST']}:{PROXY['SOCKS_PORT']}"
    env["HTTP_PROXY"] = f"http://{PROXY['HTTP_HOST']}:{PROXY['HTTP_PORT']}"
    env["HTTPS_PROXY"] = f"http://{PROXY['HTTP_HOST']}:{PROXY['HTTP_PORT']}"
    env["NO_PROXY"] = PROXY["NO_PROXY"]
    # Ensure helper proxy readers observe the same values.
    os.environ.setdefault("ALL_PROXY", env["ALL_PROXY"])
    os.environ.setdefault("HTTP_PROXY", env["HTTP_PROXY"])
    os.environ.setdefault("HTTPS_PROXY", env["HTTPS_PROXY"])
    os.environ.setdefault("NO_PROXY", env["NO_PROXY"])
    # Optionally expose SOCKS_HOST / SOCKS_PORT for fallback proxy readers.
    os.environ.setdefault("SOCKS_HOST", PROXY["SOCKS_HOST"])
    os.environ.setdefault("SOCKS_PORT", str(PROXY["SOCKS_PORT"]))

    project_java_home_for_config = None  # Passed through to the LSPClient config.

    # Configure JAVA_HOME for Java projects.
    if lang_id == "java":
        # The JDTLS process itself always starts with the default JDK (21).
        runtime_java_home = JAVA_HOME_DEFAULT_PATH
        print(f"[Manager Worker] Starting JDTLS process with JAVA_HOME: {runtime_java_home}")
        env["JAVA_HOME"] = runtime_java_home
        env["PATH"] = f"{runtime_java_home}/bin:{env.get('PATH', '')}"

        # All Java projects use the same configured JDK path.
        project_java_home_for_config = JAVA_HOME_DEFAULT_PATH
        print(f"[Manager Worker] Will configure JDTLS to use this JDK for '{project_name}': {project_java_home_for_config}")

        # Set Android SDK paths.
        env["ANDROID_HOME"] = ANDROID_HOME_PATH
        env["ANDROID_SDK_ROOT"] = env["ANDROID_HOME"]

        # Point GRADLE_USER_HOME at the configured global cache path.
        print(f"[Manager Worker] Using global Gradle cache at: {GRADLE_GLOBAL_CACHE_PATH}")
        env["GRADLE_USER_HOME"] = GRADLE_GLOBAL_CACHE_PATH
        os.makedirs(env["GRADLE_USER_HOME"], exist_ok=True)

        env["GRADLE_OPTS"] = " ".join([f"-Dorg.gradle.java.home={project_java_home_for_config}",
                                       "-DsocksProxyHost=127.0.0.1",
                                       "-DsocksProxyPort=1080",
                                       "-Dhttp.nonProxyHosts=",
                                      ] + env.get("GRADLE_OPTS", "").split())

    else:  # Environment setup for other languages.
        config_env = config.get("env")
        if config_env:
            for key, value in config_env.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = str(value)

    try:
        client = LSPClient(server_cmd,
                           env=env,
                           project_java_home=project_java_home_for_config,
                           cwd=project_root
                           )

        # Handshake only (3-minute timeout).
        client.initialize(project_root,
                          wait_seconds=default_soft,
                          hard_cap_seconds=default_hard)

        # Initialization succeeded: replace the placeholder and clear failure state.
        with client_lock:
            initialized_clients[client_key] = client
        with fail_reason_lock:
            LAST_INIT_FAIL_REASON.pop(client_key, None)

        # Record registry metadata for eviction and activity tracking.
        with client_lock:
            CLIENT_REGISTRY[client_key] = {
                "lang": lang_id,
                "created_at": time.time(),
                "last_used": time.time(),
                "active": 0,
            }

        # After creating a client, perform one memory check and evict if needed.
        evicted_key, stats = MEM_GUARD.maybe_evict(
            initialized_clients=initialized_clients,
            registry=CLIENT_REGISTRY,
            lock=client_lock,
            exclude_keys={client_key},  # Never evict the newly created client.
            on_evict=_on_evict_cleanup
        )
        if evicted_key:
            print(
                f"[Manager Worker] MemoryGuard evicted {evicted_key}. "
                f"System memory {stats['used_gb']:.1f}GB/{stats['total_gb']:.1f}GB, "
                f"threshold {MEM_GUARD.threshold_gb:.1f}GB."
            )

        return client, err_msg

    except Exception as e:
        err_msg = str(e)
        print(f"[Manager Worker] FAILED to initialize client for key {client_key}:\n{err_msg}", file=sys.stderr)

        with client_lock:
            initialized_clients[client_key] = None

        # Normalize the error code: LSPInitError uses e.code; other exceptions use prefix classification.
        code = e.code if isinstance(e, LSPInitError) else classify_init_error_from_text(err_msg)

        with fail_reason_lock:
            if code in NON_RETRIABLE:
                LAST_INIT_FAIL_REASON[client_key] = code
            else:
                LAST_INIT_FAIL_REASON.pop(client_key, None)

        return None, err_msg

# ---
# Slow lane
# ---
def _run_global_init_and_index(client_key: tuple,
                               project_root: str,
                               lang_id: str,
                               solution_file: Optional[str],
                               timeouts: Dict[str, Any]):
    """
    Run inside the slow-lane init_executor.
    This handles the most expensive global preparation work:
    handshake plus global indexing.
    """
    print(f"[Manager SlowLane] {client_key} starting global preparation...")

    # 1. Handshake (3-minute timeout)
    #    (use semaphores to limit concurrency)
    sem_lang = lang_semaphores.get(lang_id)
    sem_repo = repo_semaphores[os.path.basename(project_root)]
    if sem_lang:
        sem_lang.acquire()
    sem_repo.acquire()
    try:
        client, err_msg = get_or_create_lsp_client(project_root, lang_id, solution_file)
    finally:
        sem_repo.release()
        if sem_lang:
            sem_lang.release()

    if not client:
        print(f"[Manager SlowLane] {client_key} handshake failed.")
        return (None, err_msg)  # Handshake failed.

    # 2. Global indexing (15-minute timeout)
    try:
        if lang_id in SERVERS_WITH_INDEXING_PROGRESS:
            indexing_timeout = timeouts.get("indexing", DEFAULT_GLOBAL_INDEXING_TIMEOUT)
            print(f"[Manager SlowLane] {client_key} handshake succeeded, waiting for global indexing (max {indexing_timeout}s)...")
            if not client.wait_for_indexing_complete(timeout=indexing_timeout):
                print(f"[Manager SlowLane] {client_key} global indexing timed out.")
                # An indexing timeout is not always fatal; still return the client.
            else:
                print(f"[Manager SlowLane] {client_key} global indexing completed.")
        else:
            print(f"[Manager SlowLane] {client_key} handshake succeeded (no global indexing needed).")

        return (client, None)  # Success.

    except Exception as e:
        print(f"[Manager SlowLane] {client_key} exception during indexing: {e}")
        # Indexing failed; the Client instance may already be corrupted.
        with client_lock:
            initialized_clients.pop(client_key, None)
            CLIENT_REGISTRY.pop(client_key, None)
        return (None, str(e))


def _run_re_indexing(client: LSPClient,
                     lang_id: str,
                     client_key: tuple,
                     timeouts: Dict[str, Any]):
    """
    Run inside the slow-lane init_executor.
    Trigger re-indexing on an existing Client after did_switch.
    Returns (client, err_msg).
    """
    print(f"[Manager SlowLane] {client_key} starting re-indexing...")
    try:
        if lang_id in SERVERS_WITH_INDEXING_PROGRESS:
            indexing_timeout = timeouts.get("indexing", DEFAULT_INCREMENTAL_INDEXING_TIMEOUT)
            print(f"[Manager SlowLane] {client_key} waiting for re-indexing (max {indexing_timeout}s)...")
            if not client.wait_for_indexing_complete(timeout=indexing_timeout):
                print(f"[Manager SlowLane] {client_key} re-indexing timed out.", file=sys.stderr)
            else:
                print(f"[Manager SlowLane] {client_key} re-indexing completed.")
        else:
            print(f"[Manager SlowLane] {client_key} no re-indexing needed.")

        return (client, None)  # Success.
    except Exception as e:
        print(f"[Manager SlowLane] {client_key} exception during re-indexing: {e}", file=sys.stderr)
        with client_lock:
            initialized_clients.pop(client_key, None)
            CLIENT_REGISTRY.pop(client_key, None)
        return (None, str(e))

# ---
# Core logic: medium/fast lane
# ---
def _run_medium_sync_and_fast_query(client: LSPClient,
                                    message: Dict[str, Any],
                                    client_key: tuple,
                                    target_path: str):
    # --- Extract parameters ---
    lang_id = message.get("lang_id")
    commit_sha = message.get("commit_sha")
    timeouts = message.get("timeouts", {})
    command = message.get("command")
    params = message.get("params", {})
    trace_id = message.get("trace_id")
    warnings = []  # Collect synchronization-timeout warnings.

    # --- Step 1: Java multi-module handling ---
    if lang_id == "java":
        project_root = message.get("project_path") # Original project_root.
        preferred_root = client_key[0]  # Resolved preferred_root.
        module_root = detect_module_root(os.path.abspath(target_path), project_root)
        if module_root:
            java_ws.add_workspace_folder_if_needed(client, client_key, module_root)
        elif os.path.abspath(preferred_root) != os.path.abspath(project_root):
            java_ws.ensure_simple_project_config(client, preferred_root, project_root)

    # --- Step 2: (medium lane) lightweight file synchronization (1-2 minute blocking) ---
    need_wait = False
    if not client.is_document_open(target_path):
        client.clear_diagnostics_event(target_path)
        client.did_open(target_path,
                        lang_id,
                        target_path,
                        command,
                        params,
                        idea_commit = commit_sha)
        # client.did_change(target_path)
        need_wait = True
    if need_wait:
        diagnostics_timeout = timeouts.get("diagnostics", DEFAULT_DIAG.get(lang_id, SHORT_DIAG_SECOND))
        t0 = time.monotonic()
        ok = client.wait_for_diagnostics(target_path, timeout=diagnostics_timeout)
        spent = time.monotonic() - t0
        if not ok:
            print(f"[Manager] trace_id={trace_id} "
                  f"wait_for_diagnostics spent={spent:.2f}s, "
                  f"timeout={diagnostics_timeout}s, ok={ok}")
            warn_msg = (
                f"[trace_id={trace_id}] "
                f"lightweight synchronization timed out (max {diagnostics_timeout}s, "
                f"actual_wait={spent:.2f}s). "
                f"The file may not have finished analysis yet, so query results may be empty or incomplete."
            )
            warnings.append(warn_msg)
    else:
        print("[Manager] No wait needed for lightweight probe sync; the target file is already open and no checkout occurred.")

    # --- Step 3: (fast lane) execute the query (millisecond-scale blocking) ---
    print(f"[Manager] Synchronization complete. Executing command '{command}'...")

    # --- Full command mapping ---
    if command == "get_definition":
        result = client.definition(target_path, params['line'], params['character'])
    elif command == "get_type_definition":
        result = client.type_definition(target_path, params['line'], params['character'])
    elif command == "get_references":
        result = client.references(target_path, params['line'], params['character'], include_declaration=False)
    elif command == "get_hover":
        result = client.hover(target_path, params['line'], params['character'])
    elif command == "get_document_symbol":
        result = client.document_symbol(target_path)
    elif command == "get_document_symbol_range":
        result = client.document_symbol_range(target_path, params['start_line'], params['end_line'])
    elif command == "get_workspace_symbol":
        result = client.workspace_symbol(params['query'])
    elif command == "get_document_highlight":
        result = client.document_highlight(target_path, params['line'], params['character'])
    elif command == "get_selection_ranges":
        params['positions'] = [(params['line'], params['character'])]
        result = client.selection_ranges(target_path, params['positions'])
    elif command == "get_folding_ranges":
        result = client.folding_ranges(target_path)
    elif command == "get_signature_help":
        result = client.signature_help(target_path, params['line'], params['character'])
    elif command == "prepare_call_hierarchy":
        result = client.prepare_call_hierarchy(target_path, params['line'], params['character'])
    elif command == "get_incoming_calls":
        result = client.incoming_calls(params['item'])
    elif command == "get_outgoing_calls":
        result = client.outgoing_calls(params['item'])
    elif command == "prepare_type_hierarchy":
        result = client.prepare_type_hierarchy(target_path, params['line'], params['character'])
    elif command == "get_supertypes":
        result = client.supertypes(params['item'])
    elif command == "get_subtypes":
        result = client.subtypes(params['item'])
    else:
        result = {"error": f"Unknown command: {command}"}

    def _wrap(r):
        return r if isinstance(r, dict) else {"result": r}

    def _is_empty_result(payload):
        if payload is None:
            return True
        if isinstance(payload, dict):
            if "error" in payload:
                return False
            if "result" in payload:
                v = payload["result"]
                return v in (None, [], {}) or (isinstance(v, dict) and not v)
        return False

    payload = _wrap(result if result is not None else {"result": None})

    if _is_empty_result(payload) and warnings:
        payload["warnings"] = warnings

    return payload
