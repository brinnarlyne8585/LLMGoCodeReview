# lsp_server_manager.py
# --------------------------------------------------
# Description:
#   Shell for the background service.
#   Starts ZMQ, the proxy, and worker threads.
#   ``worker_routine`` calls ``handle_request`` as the fast-lane dispatcher.
# --------------------------------------------------
import json
import shutil
import time
from collections import defaultdict
from typing import Optional

import zmq
import os
import threading
import sys
import signal
from concurrent.futures import ThreadPoolExecutor,Future
from code_analysis_lsp.lsp_client.lsp_client import LSPClient
from code_analysis_lsp.lsp_config import LANGUAGE_SERVERS, ZMQ_ENDPOINT, JAVA_HOME_DEFAULT_PATH
from code_analysis_lsp.lsp_client.lsp_errors import (
    LSPInitError, NON_RETRIABLE, classify_init_error_from_text
)
from code_analysis_lsp.lsp_manager.lsp_manager_tasks import (
    initialized_clients, client_lock,
    initialization_futures, futures_lock,
    LAST_INIT_FAIL_REASON, fail_reason_lock,
    CLIENT_REGISTRY,
    NON_RETRIABLE, SERVERS_WITH_INDEXING_PROGRESS,
    _run_global_init_and_index,
    _run_medium_sync_and_fast_query,
    resolve_workspace_and_path, _run_re_indexing
)
from code_analysis_lsp.lsp_manager.memory_guard import client_activity_scope

# --- ZMQ endpoint definitions ---
ZMQ_BACKEND_ENDPOINT = "inproc://lsp_manager_workers"

# This determines how many projects can be indexed concurrently.
# Java and C# are resource-heavy, so keep it reasonably below total CPU cores.
MAX_MANAGER_WORKERS = 32

# --- Global shutdown event ---
SHUTDOWN = threading.Event()
def _handle_signal(signum, frame):
    print(f"\n[Manager] Caught signal {signum}, shutting down...")
    SHUTDOWN.set()
signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# Track what kind of preparation task each client_key is currently running.
FUTURE_KIND = {}  # { client_key: "initial"|"incremental" }
def _preparing(stage: str, kind: str, retry_after: int = 5):
    # Shared PREPARING response shape.
    return {"status_code": "PREPARING", "stage": stage, "kind": kind, "retry_after": retry_after}

def _attach_trace(payload: dict, message: dict):
    tid = (message or {}).get("trace_id")
    if tid:
        payload["trace_id"] = tid
    return payload

# ---------------------------------
# --- Core fast-lane dispatcher (Handle Request) ---
# ---------------------------------
def handle_request(message, init_executor):
    """
    Runs inside a fast-lane ZMQ worker.
    Handles one request and must return immediately for slow-lane work.
    It may block for medium-lane work such as file diagnostics.
    """
    try:
        warnings = []

        command = message.get("command")

        # --- Command 1: non-blocking trigger for a slow-lane task. ---
        if command == "initialize_project":
            project_root = message.get("project_path")
            lang_id = message.get("lang_id")
            solution_file = message.get("solution_file")
            timeouts = message.get("timeouts", {})
            client_key = (project_root, lang_id, solution_file) \
                           if lang_id == 'csharp' \
                           else (project_root, lang_id)

            with futures_lock:
                if client_key not in initialization_futures or initialization_futures[client_key].done():
                    print(f"[Manager] initialize_project received; submitted slow-lane task: {client_key}")
                    future = init_executor.submit(_run_global_init_and_index,
                                                  client_key,
                                                  project_root,
                                                  lang_id,
                                                  solution_file,
                                                  timeouts)
                    initialization_futures[client_key] = future
                    FUTURE_KIND[client_key] = "initial"
                else:
                    print(f"[Manager] initialize_project received, but task is already running: {client_key}")

            payload =  {"result": f"OK, task for {client_key} submitted or already running."}
            return _attach_trace(payload, message)

        # --- Command 2: non-blocking status query. ---
        elif command == "get_initialization_status":
            with futures_lock:
                futures = list(initialization_futures.values())
                total = len(futures)
                done = sum(1 for f in futures if f.done())
                active = sum(1 for f in futures if f.running())
                queued = max(total - active - done, 0)
                successful = 0
                failed = 0
                for f in futures:
                    if f.done():
                        if f.exception() is not None:
                            failed += 1
                        else:
                            try:
                                if f.result() is not None:
                                    successful += 1
                                else:
                                    failed += 1  # Treat None as a failure.
                            except Exception:
                                failed += 1

            status_report = {
                "total_tasks": total,
                "tasks_active": active,  # Actually running right now.
                "tasks_queued": queued,  # Waiting in the thread-pool queue.
                "tasks_done": done,  # Finished: success + failure.
                "tasks_successful": successful,
                "tasks_failed": failed
            }
            payload = {"result": status_report}
            return _attach_trace(payload, message)

        # --- Command 3: core LSP query routing across fast/medium/slow lanes. ---
        project_root = message.get("project_path")
        relative_path = message.get("file_path")
        ideal_commit = message.get("commit_sha")
        solution_file = message.get("solution_file")
        lang_id = message.get("lang_id")  # Supplied directly by the querier.
        did_switch = message.get("did_switch", False)
        changed_files = message.get("changed_files", {})
        timeouts = message.get("timeouts", {})

        if lang_id is None:
            print(f"[Manager] No matching programming language found for {relative_path}; skipping analysis.")
            payload = {"error": f"No matching programming language found for {relative_path}; skipping analysis."}
            return _attach_trace(payload, message)

        preferred_root, target_path = resolve_workspace_and_path(project_root, relative_path, lang_id)
        client_key = (preferred_root, lang_id, solution_file) if lang_id == 'csharp' else (preferred_root, lang_id)

        # --- Step 1: fetch the client instance non-blockingly. ---
        client = None
        with client_lock:
            if client_key in initialized_clients:
                client = initialized_clients[client_key]

        # --- Step 2: handle did_switch at highest priority. ---
        # Reuse the client, notify it, and trigger re-indexing.
        if did_switch and client:
            print(f"[Manager] {client_key} detected did_switch. Notifying client and triggering slow-lane re-indexing...")

            # Notify the client from the fast lane.
            client.notify_files_changed_on_disk(changed_files)
            client.close_all_open_documents()

            # Submit the re-index task to the slow lane.
            with futures_lock:
                # Replace the previous future tied to the old commit state.
                future = init_executor.submit(_run_re_indexing, client, lang_id, client_key)
                initialization_futures[client_key] = future
                FUTURE_KIND[client_key] = "incremental"

            # Return immediately from the fast lane.
            payload = _preparing(stage="indexing", kind="incremental", retry_after=5)
            return _attach_trace(payload, message)

        # --- Step 3: no client yet; route to the slow lane. ---
        if client is None:
            with futures_lock:
                fut = initialization_futures.get(client_key)

                if fut and not fut.done():
                    # State 3a: global preparation is still in progress.
                    payload = _preparing(stage="initializing", kind=FUTURE_KIND.get(client_key, "initial"), retry_after=2)
                    return _attach_trace(payload, message)

                if fut and fut.done():
                    try:
                        # State 3b: a previous attempt finished but failed.
                        client_result, err_msg = fut.result()
                        if client_result is None:
                            payload = {"error": f"Client {client_key} failed initialization. {err_msg}",
                                    "status_code": "INIT_FAILED"}
                            return _attach_trace(payload, message)
                        # Else: future finished but the client is missing; fall through and retry.
                    except Exception as e:
                        payload = {"error": f"Client {client_key} future failed. {e}",
                                "status_code": "INIT_FAILED"}
                        return _attach_trace(payload, message)

            # State 3c: first request for this client, trigger global preparation.
            print(f"[Manager] {client_key} first request; triggering slow-lane task...")
            future = init_executor.submit(_run_global_init_and_index,
                                          client_key,
                                          preferred_root,
                                          lang_id,
                                          solution_file,
                                          timeouts)
            with futures_lock:
                initialization_futures[client_key] = future
                FUTURE_KIND[client_key] = "initial"

            payload = _preparing(stage="initializing", kind="initial", retry_after=2)
            return _attach_trace(payload, message)

        # --- Step 4: client exists and no did_switch; use fast/medium lanes. ---

        # 4a: check whether global indexing is still running.
        with futures_lock:
            fut = initialization_futures.get(client_key)
            if fut and not fut.done():
                kind = FUTURE_KIND.get(client_key, "initial")
                stage = "initializing" if kind == "initial" else "indexing"
                payload = _preparing(stage=stage, kind=kind, retry_after=5)
                return _attach_trace(payload, message)

        # 4b: the client is ready and global indexing is done.
        # The ZMQ worker now runs medium-lane and fast-lane work synchronously.
        with client_activity_scope(CLIENT_REGISTRY, client_key, client_lock):
            # This can block for file diagnostics plus a short query execution.
            import subprocess
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"],
                cwd=client.t.root_path
            ).decode().strip()
            ideal_branch = f"keep/{ideal_commit[:8]}"
            if branch!=ideal_branch:
                payload = {
                    "error": "Commit changed before medium/fast-lane execution. "
                             "This is likely a shadow query and can be ignored."
                }
                return _attach_trace(payload, message)
            else:
                payload = _run_medium_sync_and_fast_query(client,
                                                          message,
                                                          client_key,
                                                          target_path,
                                                          )
                return _attach_trace(payload, message)

    except Exception as e:
        print(f"[Manager Worker] An error occurred while handling request: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        payload = {"error": str(e)}
        return _attach_trace(payload, message)

# Worker thread routine.
def worker_routine(context, init_executor):
    """
    Main loop for one worker thread.
    It connects to the backend DEALER socket and waits for tasks in a loop.
    """
    socket = context.socket(zmq.DEALER)
    socket.setsockopt(zmq.LINGER, 0)
    socket.connect(ZMQ_BACKEND_ENDPOINT)
    thread_name = threading.current_thread().name  # Current worker-thread name.
    print(f"[Worker {thread_name}] Started.")

    try:
        while True:
            # Wait for a multipart request: [client_identity, message_body]
            client_identity, request_body = socket.recv_multipart()
            message = json.loads(request_body)

            # Run the business-logic handler.
            result = handle_request(message, init_executor)

            # Send the result back to the ROUTER, preserving client identity.
            response_body = json.dumps(result).encode('utf-8')
            socket.send_multipart([client_identity, response_body])

    except zmq.ZMQError as e:
        # ``recv_multipart`` raises ETERM after ``context.term()``.
        if e.errno == zmq.ETERM:
            print(f"[Worker {thread_name}] Terminated by context shutdown.")
        else:
            print(f"[Worker {thread_name}] ZMQ Error: {e}")
    except Exception as e:
        # Catch unexpected errors so the worker thread does not crash silently.
        print(f"[Worker {thread_name}] Non-ZMQ error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()

    finally:
        # Always close the socket to help context termination complete.
        print(f"[Worker {thread_name}] Closing socket.")
        socket.close()
        print(f"[Worker {thread_name}] Exiting.")


def proxy_thread_func(frontend, backend):
    """
    Dedicated thread function for ``zmq.proxy``.
    ``proxy()`` exits with ETERM once ``context.term()`` is called.
    """
    print("[Manager] Proxy thread started.")
    try:
        zmq.proxy(frontend, backend)
    except zmq.ZMQError as e:
        if e.errno == zmq.ETERM:
            print("[Manager] Proxy thread terminated by context shutdown.")
        else:
            print(f"[Manager] Proxy thread encountered error: {e}")
    print("[Manager] Proxy thread exiting.")

def main():
    context = zmq.Context()

    # Clean up any stale IPC socket file.
    ipc_file = ZMQ_ENDPOINT.replace("ipc://", "")
    if os.path.exists(ipc_file):
        os.remove(ipc_file)

    # Frontend socket that receives requests from all clients.
    frontend = context.socket(zmq.ROUTER)
    frontend.setsockopt(zmq.LINGER, 0)  # Close immediately.
    frontend.bind(ZMQ_ENDPOINT)

    # Backend socket that distributes requests to worker threads.
    backend = context.socket(zmq.DEALER)
    backend.setsockopt(zmq.LINGER, 0)  # Close immediately.
    backend.bind(ZMQ_BACKEND_ENDPOINT)

    print(f"[Manager] LSP Server Manager started.")
    print(f"[Manager] Frontend listening on {ZMQ_ENDPOINT}")
    print(f"[Manager] Initializing {MAX_MANAGER_WORKERS} parallel workers...")

    # Two thread pools:
    # 1. init_executor: long-running or blocking LSP initialization work
    # 2. worker threads: receive and handle ZMQ messages
    # Keeping them separate prevents init work from starving request handling.
    init_executor = ThreadPoolExecutor(max_workers=MAX_MANAGER_WORKERS,
                                       thread_name_prefix='LSP-Init-')

    # Mark internal init threads as daemon threads to avoid blocking interpreter exit.
    def _daemonize_pool_threads(executor):
        try:
            for t in list(executor._threads):
                t.daemon = True
        except Exception:
            pass

    # Mark them shortly after creation.
    threading.Timer(0.1, _daemonize_pool_threads, args=(init_executor,)).start()

    workers = []
    for i in range(MAX_MANAGER_WORKERS):
        worker = threading.Thread(
            target=worker_routine,
            args=(context, init_executor),
            name=f"ZMQ-Worker-{i}",
            daemon = True,
        )
        worker.start()
        workers.append(worker)

    # Run the proxy in its own dedicated thread.
    proxy_thread = threading.Thread(
        target=proxy_thread_func,
        args=(frontend, backend),
        name="ZMQ-Proxy",
        daemon=False,  # Keep non-daemon; we join it with a timeout below.
    )
    proxy_thread.start()

    try:
        # Use the global event to break the wait loop.
        while proxy_thread.is_alive() and not SHUTDOWN.is_set():
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[Manager] Shutting down due to Ctrl+C...")
        SHUTDOWN.set()
    finally:
        print("[Manager] Terminating ZMQ context (will interrupt workers)...")
        context.term()

        print("[Manager] Waiting for proxy thread to exit...")
        proxy_thread.join(timeout=2)
        if proxy_thread.is_alive():
            print("[Manager] Warning: Proxy thread did not exit cleanly.")

        print("[Manager] Shutting down init_executor (canceling queued tasks)...")
        # Do not wait for running tasks.
        try:
            # Python 3.9+ supports cancel_futures.
            init_executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python 3.8 has no cancel_futures argument.
            init_executor.shutdown(wait=False)

        print("[Manager] Shutdown complete.")


if __name__ == "__main__":
    main()
