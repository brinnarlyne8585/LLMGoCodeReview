# lsp_transport.py
# Description:
#   Minimal viable LSP transport layer responsible for JSON-RPC communication
#   with the language server:
#   - open / close sessions (initialize / shutdown)
#   - send requests / notifications and read responses / notifications
#   - handle a small subset of server→client requests so the server does not hang
#   - expose ``ready_evt`` as a "server is usable" signal
#
#   ``lsp_client.py`` builds feature-oriented helpers on top of this layer.
#
# Example:
#   t = LSPTransport(["gopls"])
#   t.initialize("/path/to/workspace")
#   t.request("textDocument/definition", {...})
#   t.shutdown()

import subprocess, json, os, sys, threading, time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse, unquote


class LSPTransport:
    """LSP transport layer: initialization, I/O, and minimal request handling."""

    def __init__(self, cmd, env=None, cwd=None):
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
            cwd=cwd,
        )
        self.req_id = 1
        self.pending = {}
        self.running = True
        self.root_path = None
        self.root_uri = None
        self.ready_evt = threading.Event()
        self.diagnostics = defaultdict(list)

        # --- Use a standard dict plus an explicit lock instead of defaultdict. ---
        self.diagnostics_events = {}
        self.diag_lock = threading.Lock()
        # ---

        self.progress_tokens = {}
        self.indexing_done_evt = threading.Event()

        self._t_out = threading.Thread(target=self._reader, name="lsp-stdout", daemon=True)
        self._t_err = threading.Thread(target=self._stderr, name="lsp-stderr", daemon=True)
        self._t_out.start()
        self._t_err.start()

        self._wlock = threading.Lock()  # Writes also need serialization.
        self._settings = {}

        self.process_exit_code = None
        self.stderr_buffer = []  # Keep the last stderr lines for debugging.
        self.stderr_buffer_lock = threading.Lock()

    def set_settings(self, settings: dict):
        self._settings = settings or {}

    def _write(self, payload: dict):
        if self.process_exit_code is not None:
            raise IOError(f"LSP process has exited with code {self.process_exit_code}. Cannot write.")
        try:
            body_bytes = json.dumps(
                payload,
                ensure_ascii=True,  # Keep ASCII escaping explicit.
                separators=(",", ":")  # Strip insignificant whitespace.
            ).encode("utf-8")
            header = f"Content-Length: {len(body_bytes)}\r\n\r\n".encode("ascii")

            # Serialize writes to avoid interleaving from multiple threads.
            with self._wlock:
                self.proc.stdin.write(header)
                self.proc.stdin.write(body_bytes)
                self.proc.stdin.flush()
        except Exception as e:
            print(f"[LSPTransport _write error] {e}", file=sys.stderr)
            # If the pipe broke, fetch the exit code immediately.
            if isinstance(e, (IOError, BrokenPipeError)):
                self.proc.wait(timeout=1)
                self.process_exit_code = self.proc.returncode
            pass  # Let the caller handle the failure.

    def check_process_health(self):
        """Check whether the child process is still running."""
        if self.process_exit_code is not None:
            with self.stderr_buffer_lock:
                stderr_log = list(self.stderr_buffer)
            return (False, self.process_exit_code, stderr_log)

        # poll() is non-blocking.
        exit_code = self.proc.poll()
        if exit_code is not None:
            # The process just died; record the state.
            self.process_exit_code = exit_code
            time.sleep(0.1)  # Give the stderr thread time to capture final output.
            with self.stderr_buffer_lock:
                stderr_log = list(self.stderr_buffer)
            return (False, exit_code, stderr_log)

        return (True, None, [])  # Still running.

    def request(self, method, params=None, timeout=60):
        """Send a request and block for the response, with a timeout."""
        rid = self.req_id
        self.req_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

        # Wait until the response lands in ``self.pending``.
        start = time.time()
        while time.time() - start < timeout:
            if rid in self.pending:
                return self.pending.pop(rid)
            time.sleep(0.05)
        return None  # Timeout returns None.

    def notify(self, method, params=None):
        """Send a notification that does not expect a response."""
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    # ---------- Read loop ----------
    def _read_headers(self):
        """Read JSON-RPC headers until a blank line and return Content-Length."""
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            s = line.decode("utf-8", errors="ignore")
            if s in ("\r\n", "\n", ""):
                break
            k, _, v = s.partition(":")
            if k.lower().strip() == "content-length":
                try:
                    headers["len"] = int(v.strip())
                except:
                    pass
        return headers.get("len")

    def _reader(self):
        """Read stdout in the background and dispatch responses, notifications, and requests."""
        try:
            while self.running and not self.proc.stdout.closed:
                try:
                    ln = self._read_headers()
                    if ln is None:
                        # STDOUT reached EOF; the process is gone.
                        break

                    # raw = self.proc.stdout.read(ln).decode("utf-8", errors="ignore")

                    buf = b""
                    remain = ln
                    while remain > 0:
                        chunk = self.proc.stdout.read(remain)
                        if not chunk:  # EOF / broken pipe from the subprocess.
                            break
                        buf += chunk
                        remain -= len(chunk)
                    raw = buf.decode("utf-8", errors="ignore")

                    if not raw:
                        continue
                    msg = json.loads(raw)

                    # server→client request: must send a reply.
                    if "id" in msg and "method" in msg and "result" not in msg and "error" not in msg:
                        self._handle_server_request(msg)
                        continue

                    # Response: stash it for the waiting caller.
                    if "id" in msg and ("result" in msg or "error" in msg):
                        self.pending[msg["id"]] = msg
                        continue

                    # Notification: logging, progress, diagnostics, etc.
                    self._handle_notification(msg)
                except Exception as e:
                    if self.running:
                        print(f"[LSPTransport] read error: {e}", file=sys.stderr)
                    break
        finally:
            if self.running:
                print("[LSPTransport] Reader thread exiting. Marking process as terminated.", file=sys.stderr)
                self.running = False
                self.proc.wait(timeout=1)  # Wait for the process to exit fully.
                self.process_exit_code = self.proc.returncode

    def _stderr(self):
        """Read stderr in the background, print it, and keep the last N lines."""
        try:
            while self.running and not self.proc.stderr.closed:
                line = self.proc.stderr.readline()
                if not line:
                    break
                s = line.decode("utf-8", errors="ignore").rstrip()
                if s:
                    with self.stderr_buffer_lock:
                        self.stderr_buffer.append(s)
                        if len(self.stderr_buffer) > 20:  # Keep only the last 20 lines.
                            self.stderr_buffer.pop(0)
                    print(f"[LSP STDERR] {s}", file=sys.stderr)
        finally:
            if self.running:
                # stderr closed; the process is almost certainly dead.
                self.proc.wait(timeout=1)  # Wait for the process to exit fully.
                self.process_exit_code = self.proc.returncode

    # ---------- Minimal server→client request handling to avoid stalls ----------
    def _handle_server_request(self, msg):
        method, mid = msg.get("method"), msg.get("id")
        params = msg.get("params", {})
        result = None

        if method == "window/workDoneProgress/create":
            result = None
        elif method == "workspace/configuration":
            items = params.get("items", [])
            result = []
            for it in items:
                section = it.get("section")
                if section and section in self._settings:
                    result.append(self._settings[section])
                else:
                    # jdtls often requests the "java" section explicitly.
                    if section == "java" and "java" in self._settings:
                        result.append(self._settings["java"])
                    else:
                        result.append({})
            self._write({"jsonrpc": "2.0", "id": mid, "result": result})
            return

        elif method == "client/registerCapability":
            result = None
        elif method == "workspace/workspaceFolders":
            if self.root_uri:
                result = [{"uri": self.root_uri, "name": os.path.basename(self.root_path)}]
            else:
                result = []
        else:
            result = None

        self._write({"jsonrpc": "2.0", "id": mid, "result": result})

    def _handle_notification(self, msg):
        """Handle notifications such as logs, progress, and diagnostics."""

        method = msg.get("method")
        params = msg.get("params", {})

        if method == "$/progress":
            token = params.get("token")
            value = params.get("value", {})
            kind = value.get("kind")
            if kind == "begin":
                title = value.get("title")
                self.progress_tokens[token] = title
                keywords = ["diagnos", "analyz", "index", "load", "packages", "workspace"]
                if any(k in title.lower() for k in keywords):
                    self.indexing_done_evt.clear()
                print(f"[PROGRESS START] {title}: {value.get('message', '')}")
            elif kind == "report":
                title = self.progress_tokens.get(token, "Unknown Task")
                message = value.get('message', '')
                percentage = value.get('percentage')
                progress_str = f"{percentage}% " if percentage is not None else ""
                print(f"[PROGRESS REPORT] {title}: {progress_str}{message}")
            elif kind == "end":
                title = self.progress_tokens.pop(token, "Unknown Task")
                print(f"[PROGRESS END] {title}: {value.get('message', 'Done.')}")
                keywords = ["diagnos", "analyz", "index", "load", "packages", "workspace"]
                if any(k in title.lower() for k in keywords):
                    self.indexing_done_evt.set()
                    print(f"[LSP SIGNAL] Main indexing task '{title}' detected as complete. Proceeding.")

        elif method == "textDocument/publishDiagnostics":
            raw = params.get("uri")
            if raw:
                uri = self._normalize_uri(raw)
                self.diagnostics[uri] = params.get("diagnostics", [])
                # --- Use a lock to access and set the event safely. ---
                with self.diag_lock:
                    evt = self.diagnostics_events.setdefault(uri, threading.Event())
                    evt.set()
                    # print(f"[DEBUG] Setting event for {os.path.basename(uri)}, Event object: {id(evt)}")
                # ---
                # print(f"[LSP DIAGNOSTICS] set for {uri}")

    def shutdown(self):
        """Close the LSP session."""
        try:
            self.request("shutdown", None, timeout=5)
            self.notify("exit", None)
        except Exception:
            pass
        finally:
            self.running = False
            try:
                if self.proc.stdin and not self.proc.stdin.closed:
                    self.proc.stdin.close()
            except:
                pass
            try:
                self.proc.terminate()
            except:
                pass
            print("[LSP] shut down")


    def wait_for_diagnostics(self, uri, timeout=20):
        """
        Wait for diagnostics of a specific file.
        - Default maximum wait: 30 seconds.
        - Return True immediately once a real diagnostic arrives (``evt.set()``).
        - Return False only if the full timeout is exhausted without diagnostics.
        """
        uri = self._normalize_uri(uri)
        with self.diag_lock:
            evt = self.diagnostics_events.setdefault(uri, threading.Event())

        # Normalize None: never wait forever.
        if timeout is None:
            timeout = 30

        # If diagnostics already exist, return immediately.
        if evt.is_set():
            return True

        # Non-positive timeout: do not wait, just return the current state.
        if timeout <= 0:
            return evt.is_set()

        deadline = time.monotonic() + timeout

        while True:
            # Check once at the top of the loop to avoid one extra wait.
            if evt.is_set():
                return True

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Time is up; check the flag one last time.
                return evt.is_set()

            # Wait for the remaining time; return True if set during the wait.
            triggered = evt.wait(timeout=remaining)
            if triggered or evt.is_set():
                return True
            # Otherwise continue and reevaluate the remaining budget.

    def clear_diagnostics_event(self, uri):
        """Clear the diagnostics wait signal for one file before the next wait."""
        uri = self._normalize_uri(uri)
        with self.diag_lock:
            self.diagnostics_events.setdefault(uri, threading.Event()).clear()

    def remove_diagnostics_event(self, uri):
        uri = self._normalize_uri(uri)
        with self.diag_lock:
            self.diagnostics_events.pop(uri, None)

    def request_begin(self, method, params=None):
        """Send a request without blocking and return the request id."""
        rid = self.req_id
        self.req_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)
        return rid

    def await_response(self, rid, timeout=30):
        """Wait for the response of a specific request id; return None on timeout."""
        start = time.time()
        while time.time() - start < timeout:
            if rid in self.pending:
                return self.pending.pop(rid)
            time.sleep(0.05)
        return None

    def get_stderr_tail(self, max_lines: int = 20):
        with self.stderr_buffer_lock:
            return list(self.stderr_buffer[-max_lines:])

    def get_stderr_head(self, max_lines: int = 20):
        with self.stderr_buffer_lock:
            return list(self.stderr_buffer[0:max_lines+1])

    def _normalize_uri(self, u: str) -> str:
        try:
            up = urlparse(u)
            if up.scheme == "file":
                # Drop netloc, decode %xx, normalize to a local absolute path, then call as_uri().
                return Path(unquote(up.path)).resolve().as_uri()
            return u or ""
        except Exception:
            return u or ""
