# lsp_querier.py
# --------------------------------------------------
# Description:
#   Provides the ``LSPQuerier`` class for programmatic Python usage.
#   Wraps Git operations and communication with the background manager.
# --------------------------------------------------
from datetime import datetime
import itertools
import threading
import time
import random

import zmq
import os

from filelock import FileLock

from code_analysis_lsp.lsp_config import ZMQ_ENDPOINT
from code_analysis_lsp.utils.helpers import get_lang_from_path, run_command, find_cs_solution_file

title = "LSPQuerier"

# Project-level concurrency control.
# key: project_path (str), value: threading.Lock
_project_locks = {}
# Protects the ``_project_locks`` dictionary itself.
_locks_lock = threading.Lock()

# Per-process instance counter.
_id_lock = threading.Lock()
_id_counter = itertools.count(1)

def get_project_lock(project_path):
    """Get or create the lock that guards state changes for one project path."""
    normalized_path = os.path.abspath(project_path)
    with _locks_lock:
        if normalized_path not in _project_locks:
            lock_file = os.path.join(normalized_path, ".lsp_git_checkout.lock")
            _project_locks[normalized_path] = FileLock(lock_file)
        return _project_locks[normalized_path]


def _resolve_ref(project_path: str, commit_sha: str) -> str:
    """Resolve a commit SHA to its canonical reference name when possible."""
    if not commit_sha:
        return ""

    # Prefer a keep/<sha> branch when it exists.
    potential_branch_name = f"keep/{commit_sha[:8]}"
    if run_command(project_path, ["git", "rev-parse", "--verify", potential_branch_name], check=False):
        return potential_branch_name

    # Otherwise fall back to the full commit SHA.
    return commit_sha

def clean_worktree(project_path: str):
    """Clean the worktree before switching commits."""
    # Drop local changes to tracked files.
    run_command(project_path, ["git", "reset", "--hard", "HEAD"], check=False)
    # Remove untracked files and directories.
    run_command(project_path, ["git", "clean", "-fd"], check=False)

class LSPQuerier:

    def __init__(self):

        # Ensure each querier instance gets a unique client id.
        with _id_lock:
            instance_id = next(_id_counter)

        # Initialize the ZMQ context and connect to the background manager.
        self.context = zmq.Context()

        # DEALER is asynchronous and more robust than REQ for retry scenarios.
        self.socket = self.context.socket(zmq.DEALER)

        # Use an identity that includes the instance id.
        client_id = f"querier-{os.getpid()}-{instance_id}".encode("utf-8")

        self.socket.setsockopt(zmq.IDENTITY, client_id)
        self.socket.connect(ZMQ_ENDPOINT)
        # Documentation removed in publish cleanup.

        # Documentation removed in publish cleanup.
        # Retry configuration.
        self.POLL_TIMEOUT_S = 30.0
        # Exponential backoff parameters.
        self.RETRY_SLEEP_INITIAL_S = 1.0
        self.RETRY_SLEEP_MAX_S = 10.0
        self.RETRY_SLEEP_MULTIPLIER = 1.5
        self.RETRY_JITTER = 0.2

    @staticmethod
    def _get_changed_files(project_path, old_ref, new_ref):
        """Return changed files between two refs using ``git diff-tree``."""
        old_ref = old_ref.strip()
        new_ref = new_ref.strip()
        # ``-r`` ensures recursion into subdirectories.
        diff_output = run_command(project_path, ["git", "diff-tree", "-r", "--name-status", old_ref, new_ref])

        changed_files = {'created': [], 'changed': [], 'deleted': []}
        if not diff_output:
            return changed_files

        for line in diff_output.strip().split('\n'):
            if not line: continue
            try:
                status, file_rel_path = line.split('\t')
                abs_path = os.path.join(project_path, file_rel_path)
                uri = f"file://{os.path.abspath(abs_path)}"

                if status.startswith('A'):
                    changed_files['created'].append(uri)
                elif status.startswith('M'):
                    changed_files['changed'].append(uri)
                elif status.startswith('D'):
                    changed_files['deleted'].append(uri)
            except ValueError:
                print(f"[{title}] Git: warning: failed to parse diff line: '{line}'")

        return changed_files

    def query(self,
              project_path,
              commit_sha,
              file_path,
              command,
              params,
              trace_id,
              timeouts=None,
              lang_id=None,):
        """Run a full query transaction against the background LSP manager."""
        if not os.path.isdir(project_path):
            print(f"[{title}] Error: project path not found: {project_path}")
            return {"error": f"project path not found: {project_path}"}

        expected_tid = trace_id

        timeouts = timeouts or {}
        # Waiting-budget configuration.
        overall_init = timeouts.get("overall_init", 20 * 60)
        overall_incr = timeouts.get("overall_incremental", 5 * 60)
        exec_budget = timeouts.get("exec", 3 * 60)
        # Keep diagnostics under two minutes when possible.
        diag_budget = min(timeouts.get("diagnostics", 120), max(30, exec_budget - 30))
        timeouts["diagnostics"] = diag_budget

        # Serialize project-directory state changes such as git checkout.
        project_lock = get_project_lock(project_path)
        with project_lock:
            result = None
            try:
                # Resolve the target ref and inspect current Git state.
                target_ref = _resolve_ref(project_path, commit_sha)
                current_ref = run_command(project_path, ["git", "branch", "--show-current"])
                # Detached HEAD: fall back to the current SHA.
                if not current_ref:
                    current_sha = run_command(project_path, ["git", "rev-parse", "HEAD"])
                    current_ref = _resolve_ref(project_path, current_sha)

                did_switch = False
                changed_files = {}
                target_ref = target_ref.strip()
                current_ref = current_ref.strip()
                # Switch only when the current ref differs from the target ref.
                if current_ref != target_ref:
                    did_switch = True
                    # Compute changed files before checkout.
                    changed_files = self._get_changed_files(project_path, current_ref, target_ref)
                    # Clean the worktree before switching commits.
                    clean_worktree(project_path)
                    if run_command(project_path, ["git", "checkout", target_ref, "--quiet"]) is None:
                        print(f"[{title}] Error: failed to switch {project_path} to {target_ref}")
                        raise RuntimeError(f"failed to switch {project_path} to {target_ref}")
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    print(
                        f"[{title}] {now}, Git: switched {project_path} "
                        f"from {current_ref[:40]} to {target_ref[:40]}."
                    )
                else:
                    pass

                # Build the request payload sent to the manager.
                if lang_id is None:
                    lang_id = get_lang_from_path(file_path)
                request = {
                    "project_path": project_path,
                    "commit_sha": commit_sha,
                    "file_path": file_path,
                    "lang_id": lang_id,
                    "command": command,
                    "params": params,
                    "trace_id": trace_id,
                    "did_switch": did_switch,
                    "changed_files": changed_files,
                    "timeouts": timeouts or {},
                }

                if lang_id == 'csharp':
                    solution_file = find_cs_solution_file(os.path.join(project_path, file_path), project_path)
                    if solution_file:
                        request["solution_file"] = solution_file

                # Polling loop: wait for preparation, then execution.
                prep_mode = None  # "initial" | "incremental"
                prep_started_at = time.time()
                prep_deadline = None
                backoff = self.RETRY_SLEEP_INITIAL_S
                poll_count = 0

                need_send = True
                while True:

                    if need_send:
                        self.socket.send_json(request)

                    # Compute the next poll timeout.
                    if prep_deadline is not None:
                        remain = max(0, prep_deadline - time.time())
                        poll_s = min(self.POLL_TIMEOUT_S, remain, 10.0)
                    else:
                        # Before PREPARING arrives, use a conservative interval.
                        poll_s = min(self.POLL_TIMEOUT_S, 10.0)

                    poller = zmq.Poller()
                    poller.register(self.socket, zmq.POLLIN)
                    socks = dict(poller.poll(timeout=int(poll_s * 1000)))
                    have_msg = (self.socket in socks) and (socks[self.socket] == zmq.POLLIN)

                    if have_msg:
                        resp = self.socket.recv_json()

                        resp_tid = resp.get("trace_id")
                        # Ignore responses that belong to a different trace id.
                        if resp_tid and resp_tid != expected_tid:
                            need_send = False
                            continue

                        poll_count += 1
                        status = resp.get("status_code")

                        if not status:
                            # A real result from the execution phase.
                            return resp
                        if status == "INIT_FAILED":
                            return resp
                        if status == "PREPARING":
                            stage = resp.get("stage")  # "initializing" | "indexing"
                            kind = resp.get("kind")  # "initial" | "incremental"
                            ra = float(resp.get("retry_after", 5))
                            # First PREPARING response establishes the prep window.
                            if prep_deadline is None:
                                if kind == "initial" or stage == "initializing":
                                    prep_mode = "initial"
                                    prep_window_s = overall_init
                                else:
                                    prep_mode = "incremental"
                                    prep_window_s = overall_incr
                                prep_deadline = time.time() + prep_window_s

                            # Abort once the preparation window is exhausted.
                            if prep_deadline is not None and time.time() >= prep_deadline:
                                elapsed = time.time() - prep_started_at
                                elapsed_second = round(elapsed, 2)
                                return {"error": f"Preparation timed out ({prep_mode}), "
                                                 f"wait_seconds={elapsed_second}",
                                        "stage": stage,
                                        "kind": kind}

                            # Wait using backoff, but do not resend immediately.
                            sleep_s = min(ra, backoff)
                            jitter = (1 - self.RETRY_JITTER) + (random.random() * 2 * self.RETRY_JITTER)
                            time.sleep(max(0.1, sleep_s * jitter))
                            backoff = min(self.RETRY_SLEEP_MAX_S, backoff * self.RETRY_SLEEP_MULTIPLIER)

                            # Only send did_switch/changed_files on the first request.
                            request["did_switch"] = False
                            request["changed_files"] = {}

                            need_send = True
                            continue

                        # Unknown state: treat it as still preparing.
                        time.sleep(1.0)
                        need_send = True
                        continue

                    # Poll timeout: continue until the preparation deadline.
                    if prep_deadline is not None and time.time() >= prep_deadline:
                        elapsed = time.time() - prep_started_at
                        elapsed_second = round(elapsed, 2)
                        return {"error": f"Preparation timed out ({prep_mode}), "
                                         f"wait_seconds={elapsed_second}"}

                    need_send = True
                    continue

            finally:
                pass


    def close(self):
        """Close the socket and terminate the ZMQ context."""
        self.socket.close()
        self.context.term()
        # print(f"[{title}] Connection closed.")
