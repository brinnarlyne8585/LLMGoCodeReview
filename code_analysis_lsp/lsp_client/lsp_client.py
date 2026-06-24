# lsp_client.py
# Description:
#   Feature-oriented wrapper on top of ``LSPTransport``.
#   It exposes the LSP operations that are most useful for collecting code-review
#   context for downstream models.

import os, json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from code_analysis_lsp.lsp_client.lsp_transport import LSPTransport
from code_analysis_lsp.utils.java_support import build_initial_java_settings
from code_analysis_lsp.lsp_client.lsp_errors import (
    LSPInitError, format_init_error,
    INIT_HARD_TIMEOUT, INIT_EXIT_IMMEDIATE, INIT_EXIT_DURING_WAIT, INIT_SERVER_ERROR
)


def _pick_http_proxy_from_env():
    # Accept either "http://host:port" or a bare "host:port" value.
    for k in ("HTTPS_PROXY", "HTTP_PROXY"):
        v = os.environ.get(k) or os.environ.get(k.lower())
        if not v:
            continue
        if "://" in v:
            u = urlparse(v)
            return (u.hostname or "127.0.0.1", u.port or 8118)
        if ":" in v:
            host, port = v.split(":", 1)
            return (host, int(port))
    return ("127.0.0.1", 8118)

def _pick_socks_from_env():
    v = os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    if v and "socks" in v:
        u = urlparse(v if "://" in v else "socks5h://" + v)
        return (u.hostname or "127.0.0.1", u.port or 1080)
    # Fallback to the JVM proxy values injected by the manager when present.
    return (os.environ.get("SOCKS_HOST", "127.0.0.1"),
            int(os.environ.get("SOCKS_PORT", 1080)))

def _to_uri(root_path, path):
    p = Path(path if os.path.isabs(path) else os.path.join(root_path or "", path)).resolve()
    return p.as_uri()   # Always yields a correct cross-platform file URI.

class LSPClient:
    """LSP client for code-review context collection."""
    METHOD_KINDS = {11, 12}  # LSP SymbolKind: 11=function, 12=method

    def __init__(self, cmd, env=None, project_java_home=None, cwd=None):
        self.t = LSPTransport(cmd, env=env, cwd=cwd)
        self.token_legend = None
        self.project_java_home = project_java_home

        self._open_docs = {}  # Tracks which documents are currently open.

    def initialize(self, root_path, wait_seconds=30, hard_cap_seconds=None):
        """
        wait_seconds: soft timeout; warn when exceeded but keep waiting
        hard_cap_seconds: hard timeout; fail and shut down the process when exceeded
        """

        # --- Startup health check ---
        time.sleep(0.3)
        is_running, exit_code, stderr_log = self.t.check_process_health()
        if not is_running:
            # The process exited immediately after startup.
            stderr_str = "\n".join(stderr_log)
            msg = format_init_error(
                INIT_EXIT_IMMEDIATE,
                exit_code=exit_code,
                stderr_tail=stderr_str
            )
            print(msg, file=sys.stderr)
            self.t.shutdown()
            raise LSPInitError(INIT_EXIT_IMMEDIATE, msg)

        # Advertise useful client capabilities so the server can return richer results.
        caps = {
            "window": {"workDoneProgress": True},
            "workspace": {"workspaceFolders": True},
            "textDocument": {
                "publishDiagnostics": {"relatedInformation": True},
                "hover": {"contentFormat": ["plaintext", "markdown"]},
                "foldingRange": {"lineFoldingOnly": True},
                "selectionRange": {},
                "signatureHelp": {"signatureInformation": {"parameterInformation": {"labelOffsetSupport": True}}},
                "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                "callHierarchy": {},
                "typeHierarchy": {},
                "semanticTokens": {
                    "requests": {
                        "full": True
                    },
                    "tokenTypes": [
                        "namespace", "type", "class", "enum", "interface", "struct", "typeParameter",
                        "parameter", "variable", "property", "enumMember", "event", "function",
                        "method", "macro", "keyword", "modifier", "comment", "string", "number",
                        "regexp", "operator"
                    ],
                    "tokenModifiers": [
                        "declaration", "definition", "readonly", "static", "deprecated", "abstract",
                        "async", "modification", "documentation", "defaultLibrary"
                    ],
                    "formats": ["relative"]
                }
            }
        }

        # --- Build initialize request parameters ---
        abs_root_path = os.path.abspath(root_path)
        root_uri = Path(abs_root_path).resolve().as_uri()

        # Manually propagate root_path and root_uri to the transport layer.
        self.t.root_path = abs_root_path
        self.t.root_uri = root_uri

        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": caps,
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(abs_root_path)}],
        }

        # For Java projects, prepare runtime settings first.
        if self.project_java_home:
            http_host, http_port = _pick_http_proxy_from_env()
            socks_host, socks_port = _pick_socks_from_env()
            settings_obj = build_initial_java_settings(
                project_java_home=self.project_java_home,
                http_proxy_host=http_host, http_proxy_port=http_port,
                socks_host=socks_host, socks_port=socks_port
            )
            self.t.set_settings(settings_obj)
            params["initializationOptions"] = {"settings": settings_obj}

        # Send initialize, then wait in small steps.
        rid = self.t.request_begin("initialize", params)

        start = time.time()
        soft_deadline = start + wait_seconds
        hard_deadline = start + hard_cap_seconds
        soft_warned = False

        resp = None
        while True:
            now = time.time()
            if now >= hard_deadline:
                # Hard cap reached: fail and shut down here.
                running, exit_code, tail = self.t.check_process_health()
                tail_str = "\n".join(tail)
                msg = format_init_error(
                    INIT_HARD_TIMEOUT,
                    seconds=hard_cap_seconds,
                    running=running,
                    exit_code=exit_code,
                    stderr_tail=tail_str
                )
                print(msg, file=sys.stderr)
                self.t.shutdown()
                raise LSPInitError(INIT_HARD_TIMEOUT, msg)

            # Wait in 0.5-second increments.
            remaining = min(0.5, hard_deadline - now)
            resp = self.t.await_response(rid, timeout=remaining)
            if resp is not None:
                break  # Successfully received the initialize response.

            # Warn once at the soft timeout and keep waiting.
            if not soft_warned and time.time() >= soft_deadline:
                print(f"[LSP Client] Initialize soft-timeout after {wait_seconds}s; "
                        f"continuing to wait (hard cap {hard_cap_seconds}s)...",
                        file=sys.stderr)
                soft_warned = True

            # Keep checking process health while waiting.
            running, exit_code, _ = self.t.check_process_health()
            if not running:
                msg = format_init_error(
                    INIT_EXIT_DURING_WAIT,
                    exit_code=exit_code
                )
                print(msg, file=sys.stderr)
                self.t.shutdown()
                raise LSPInitError(INIT_EXIT_DURING_WAIT, msg)

        # The server returned an error.
        if resp and "error" in resp:
            msg = format_init_error(
                INIT_SERVER_ERROR,
                server_error=json.dumps(resp["error"])
            )
            print(msg, file=sys.stderr)
            self.t.shutdown()
            raise LSPInitError(INIT_SERVER_ERROR, msg)

        # Initialization succeeded.
        if resp and "result" in resp:
            self.t.notify("initialized", {})
            if self.project_java_home:
                self.t.notify("workspace/didChangeConfiguration", {"settings": settings_obj})
            print("[LSP] initialized")

        # Cache the semantic-token legend if the server provides one.
        if resp and resp.get("result", {}).get("capabilities", {}).get("semanticTokensProvider"):
            self.token_legend = resp["result"]["capabilities"]["semanticTokensProvider"].get("legend")

    def shutdown(self):
        """Shut down the LSP session."""
        self.t.shutdown()

    def is_document_open(self, file_path):
        uri = _to_uri(self.t.root_path, file_path)
        return uri in self._open_docs

    def did_open(self,
                 file_path,
                 languageId="go",
                 target_path: str=None,
                 command: str=None,
                 params: str=None,
                 idea_commit: str=None
                 ):
        """Notify the server that a document has been opened."""
        # Read the file contents.
        if os.path.isabs(file_path):
            abs_path = file_path
        else:
            abs_path = os.path.join(self.t.root_path, file_path)
        if not os.path.exists(abs_path):
            try:
                import subprocess
                head = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=self.t.root_path
                ).decode().strip()
            except Exception as e:
                head = f"<HEAD-ERROR: {e}>"
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            context = f"{now} \n" \
                      f"PATH: {target_path}\n" \
                      f"COMMAND: {command}\n" \
                      f"PARAM: {params}\n" \
                      f"Expected commit: {idea_commit}\n" \
                      f"Actual commit: {head}"
            raise FileNotFoundError(f"[LSPClient] ⚠️⚠️⚠️\n"
                                    f"File not found: {abs_path} \n"
                                    f"when {context}")
        uri = _to_uri(self.t.root_path, file_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception:
            text = ""
        print(f"[LSPClient] didOpen {uri} (len={len(text)})")
        self.t.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": languageId, "version": 1, "text": text}
        })
        # Mark the document as open.
        self._open_docs[uri] = 1

    def did_change(self, file_path):
        uri = _to_uri(self.t.root_path, file_path)
        # Read the latest full document text.
        try:
            with open(os.path.join(self.t.root_path, file_path), "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        except Exception as e:
            print(f"[LSPClient] didChange read failed: {e}; sending empty text", file=sys.stderr)
            text = ""
        print(f"[LSPClient] didChange {uri} (len={len(text)})")
        self.t.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": text}]  # Replace the full document content.
        })

    def did_close(self, file_path):
        """Close an opened document and release server-side incremental state."""
        uri = _to_uri(self.t.root_path, file_path)
        print(f"[LSPClient] didClose {uri}")
        self.t.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
        # Local state cleanup.
        self._open_docs.pop(uri, None)
        # Also clear the diagnostics event so a stale event does not satisfy a later wait.
        try:
            # self.t.clear_diagnostics_event(uri)
            self.t.remove_diagnostics_event(uri)
        except Exception:
            pass

    def close_all_open_documents(self):
        """Close all currently opened documents and return the closed count."""
        uris = list(self._open_docs.keys())
        for uri in uris:
            print(f"[LSPClient] didClose {uri}")
            self.t.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            # Clear diagnostics wait events by URI.
            try:
                self.t.remove_diagnostics_event(uri)
            except Exception:
                pass
        count = len(uris)
        self._open_docs.clear()
        print(f"[LSPClient] closed {count} open document(s)")
        return count

    def notify_files_changed_on_disk(self, changed_files):
        """
        Notify the server about batched on-disk file changes through
        ``workspace/didChangeWatchedFiles``. This is the correct way to handle
        file modifications introduced by external commands such as ``git checkout``.

        :param changed_files: Dictionary containing ``created``, ``changed``,
            and ``deleted`` lists of absolute file URIs.
        """
        if not changed_files:
            return

        # LSP event types: 1=Created, 2=Changed, 3=Deleted.
        events = []
        for uri in changed_files.get('created', []):
            events.append({'uri': uri, 'type': 1})
        for uri in changed_files.get('changed', []):
            events.append({'uri': uri, 'type': 2})
        for uri in changed_files.get('deleted', []):
            events.append({'uri': uri, 'type': 3})

        if not events:
            return

        print(f"[LSP Client] Notifying the server about {len(events)} file changes caused by git checkout...")
        self.t.notify("workspace/didChangeWatchedFiles", {"changes": events})

    def wait_for_diagnostics(self, file_path, timeout=20):
        """Wait for the server to finish diagnostics for a specific file."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.wait_for_diagnostics(uri, timeout)

    def clear_diagnostics_event(self, file_path):
        uri = _to_uri(self.t.root_path, file_path)
        self.t.clear_diagnostics_event(uri)

    def wait_for_indexing_complete(self, timeout=180):
        """Wait for the server to finish its main background indexing work."""
        print("\nWaiting for server to finish indexing...")
        return self.t.indexing_done_evt.wait(timeout=timeout)

    # ---------- Navigation / symbols ----------
    def definition(self, file_path, line, ch):
        """Return the definition location of the symbol at the given cursor."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/definition", {
            "textDocument": {"uri": uri}, "position": {"line": line, "character": ch}
        })

    def type_definition(self, file_path, line, ch):
        uri = _to_uri(self.t.root_path, file_path)
        resp = self.t.request("textDocument/typeDefinition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": ch}
        })
        return resp

    def references(self, file_path, line, ch, include_declaration=True):
        """Return references of the symbol at the given cursor position."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": ch},
            "context": {"includeDeclaration": bool(include_declaration)}
        })

    # def document_symbol(self, file_path):
    #     """textDocument/documentSymbol returns symbols from the current file, either
    #     as a tree or a flat list, with range information.
    #
    #     Purpose: quickly retrieve the full symbol range (`range`) and the precise
    #     name range (`selectionRange`), which is commonly used to determine method
    #     boundaries.
    #
    #     Example (Go, 0-based line/column):
    #       Source:
    #         0: package main
    #         1:
    #         2: func add(a, b int) int { return a + b }  // function definition; "add" is at line 2, char 5..8
    #         3: func main(){ _ = add(1,2) }
    #
    #       Possible return format 1 (DocumentSymbol with hierarchy and children):
    #         result: [
    #           {
    #             "name": "add", "kind": 11,                // 11=function, 12=method
    #             "range": { "start": {"line":2,"character":0}, "end": {"line":2,"character":33} },          # full function body
    #             "selectionRange": { "start": {"line":2,"character":5}, "end": {"line":2,"character":8} }   # function name only
    #             // "children": [ ... child symbols such as params/locals, depending on server support ... ]
    #           },
    #           { "name": "main", "kind": 12, "range": {...}, "selectionRange": {...} }
    #         ]
    #
    #       Meaning:
    #         - range: full symbol coverage, usually including function body bounds.
    #         - selectionRange: precise bounds of the symbol name, useful for highlight/jump.
    #         - DocumentSymbol can be traversed recursively to build an in-file symbol tree;
    #           SymbolInformation is typically returned as a flat list.
    #     """
    #     limit = None
    #     uri = _to_uri(self.t.root_path, file_path)
    #     resp = self.t.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
    #     if limit is not None and resp and isinstance(resp.get("result"), list):
    #         resp = {"result": resp["result"][:int(limit)]}
    #     return resp

    def _symbols_error_from_stderr(self, *, timeout=False):
        """
        Return ``None`` when stderr shows no clear failure signal.
        Otherwise return ``{error, stderr_tail}``.
        """
        tail = "\n".join(self.t.get_stderr_tail())  # May be empty.
        if timeout:
            return {"error": f"Symbol Analysis Failed, with timeout {timeout}.\nstderr_tail: {tail}"}
        if "pylsp_document_symbols" in tail or "pylsp.plugins.symbols" in tail:
            return {"error": f"Symbol Analysis Failed, with stderr.\nstderr_tail: {tail}"}
        return None


    def document_symbol(self, file_path):
        """Safely query document symbols and surface explicit failures."""
        uri = _to_uri(self.t.root_path, file_path)
        resp = self.t.request("textDocument/documentSymbol",
                              {"textDocument": {"uri": uri}},
                              timeout=60)

        # Success: ``result`` exists and is a list. An empty list is still valid.
        if resp and isinstance(resp.get("result"), list):
            return resp

        # Timeout: ``resp`` is None.
        if resp is None:
            err = self._symbols_error_from_stderr(timeout=True) or {}
            return {"result": [], **err}

        # Explicit JSON-RPC error.
        if isinstance(resp, dict) and "error" in resp:
            return {"result": [], "error": f"Symbol Analysis Failed, with JSON-RPC error.\n"
                                           f"{json.dumps(resp['error'], ensure_ascii=False)}"}

        # Unexpected response shape: inspect stderr for pylsp plugin errors.
        err = self._symbols_error_from_stderr(timeout=False)
        if err:
            return {"result": [], **err}

        # No failure signal at all: treat it as a normal empty result.
        return {"result": []}


    def document_symbol_range(self, file_path, start_line, end_line):
        """
        Return document symbols that overlap the given line range.
        Filtering is performed client-side after fetching the full symbol list.
        """
        # 1. Fetch the full symbol list first.
        full_symbols_resp = self.document_symbol(file_path)

        if not full_symbols_resp or not full_symbols_resp.get("result"):
            return full_symbols_resp  # Return directly on failure or empty result.

        # 2. Filter recursively on the client side.
        symbols = full_symbols_resp.get("result", [])

        def _filter_symbols_in_range(symbol_list, s_line, e_line):
            """Recursively filter a symbol list by line-range overlap."""
            filtered = []
            for symbol in symbol_list:
                # DocumentSymbol and SymbolInformation use different shapes.
                s_range = symbol.get("range") or symbol.get("location", {}).get("range")
                if not s_range:
                    continue

                symbol_start_line = s_range["start"]["line"]
                symbol_end_line = s_range["end"]["line"]

                # Keep the symbol if its range overlaps the target range.
                if symbol_end_line >= s_line and symbol_start_line <= e_line:

                    # Filter child nodes recursively.
                    if "children" in symbol and symbol["children"]:
                        filtered_children = _filter_symbols_in_range(symbol["children"], s_line, e_line)
                        symbol["children"] = filtered_children

                    filtered.append(symbol)
            return filtered

        filtered_symbols = _filter_symbols_in_range(symbols, start_line, end_line)
        return {"result": filtered_symbols}

    def workspace_symbol(self, query):
        """Search the whole workspace for symbol definitions / declarations by name."""
        return self.t.request("workspace/symbol", {"query": query})

    # ---------- Semantics / explanations ----------
    def hover(self, file_path, line, ch):
        """Return hover information such as type, signature, and documentation."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/hover", {
            "textDocument": {"uri": uri}, "position": {"line": line, "character": ch}
        })

    def document_highlight(self, file_path, line, ch):
        """Highlight occurrences of the same symbol within the current file."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/documentHighlight", {
            "textDocument": {"uri": uri}, "position": {"line": line, "character": ch}
        })

    def selection_ranges(self, file_path, positions):
        """Return hierarchical selection ranges for one or more cursor positions."""
        uri = _to_uri(self.t.root_path, file_path)
        pos = [{"line": l, "character": c} for (l, c) in positions]
        return self.t.request("textDocument/selectionRange", {
            "textDocument": {"uri": uri}, "positions": pos
        })

    def folding_ranges(self, file_path):
        """Return folding ranges, often useful for function or region boundaries."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/foldingRange", {"textDocument": {"uri": uri}})

    def signature_help(self, file_path, line, ch):
        """Return signature / argument information at a call site."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/signatureHelp", {
            "textDocument": {"uri": uri}, "position": {"line": line, "character": ch}
        })

    def semantic_tokens(self, file_path):
        """Return decoded full-file semantic tokens."""
        uri = _to_uri(self.t.root_path, file_path)
        resp = self.t.request("textDocument/semanticTokens/full", {"textDocument": {"uri": uri}})

        if not resp or "result" not in resp or "data" not in resp["result"]:
            return resp

        if not self.token_legend or not self.token_legend.get("tokenTypes"):
            return {"error": "Token legend not available. Cannot decode semantic tokens."}

        # Decode the token stream.
        decoded_tokens = []
        data = resp["result"]["data"]
        current_line = 0
        current_char = 0

        token_types = self.token_legend.get("tokenTypes", [])
        token_modifiers = self.token_legend.get("tokenModifiers", [])

        i = 0
        while i < len(data):
            delta_line = data[i]
            delta_start = data[i + 1]
            length = data[i + 2]
            token_type_idx = data[i + 3]
            modifier_mask = data[i + 4]
            i += 5

            if delta_line > 0:
                current_line += delta_line
                current_char = delta_start
            else:
                current_char += delta_start

            # Decode the token type.
            token_type = token_types[token_type_idx] if token_type_idx < len(token_types) else "unknown"

            # Decode token modifiers from the bitmask.
            modifiers = []
            for j in range(len(token_modifiers)):
                if (modifier_mask >> j) & 1:
                    modifiers.append(token_modifiers[j])

            decoded_tokens.append({
                "line": current_line,
                "start": current_char,
                "length": length,
                "type": token_type,
                "modifiers": modifiers
            })

        return {"result": decoded_tokens}

    def semantic_tokens_range(self, file_path, start_line, end_line):
        """Return decoded semantic tokens for a selected line range."""
        uri = _to_uri(self.t.root_path, file_path)
        # Note the LSP method name: textDocument/semanticTokens/range.
        resp = self.t.request("textDocument/semanticTokens/range", {
            "textDocument": {"uri": uri},
            "range": {
                "start": {"line": start_line, "character": 0},
                "end": {"line": end_line, "character": 0}
            }
        })

        # The decoding logic is the same as in ``semantic_tokens``.
        if not resp or "result" not in resp or "data" not in resp["result"]:
            return resp

        if not self.token_legend or not self.token_legend.get("tokenTypes"):
            return {"error": "Token legend not available. Cannot decode semantic tokens."}

        decoded_tokens = []
        data = resp["result"]["data"]
        current_line = start_line  # Decoding starts from the requested start line.
        current_char = 0

        token_types = self.token_legend.get("tokenTypes", [])
        token_modifiers = self.token_legend.get("tokenModifiers", [])

        i = 0
        while i < len(data):
            delta_line = data[i]
            delta_start = data[i + 1]
            length = data[i + 2]
            token_type_idx = data[i + 3]
            modifier_mask = data[i + 4]
            i += 5

            if delta_line > 0:
                current_line += delta_line
                current_char = delta_start
            else:
                current_char += delta_start

            token_type = token_types[token_type_idx] if token_type_idx < len(token_types) else "unknown"

            modifiers = []
            for j in range(len(token_modifiers)):
                if (modifier_mask >> j) & 1:
                    modifiers.append(token_modifiers[j])

            decoded_tokens.append({
                "line": current_line,
                "start": current_char,
                "length": length,
                "type": token_type,
                "modifiers": modifiers
            })

        return {"result": decoded_tokens}

    # ---------- Call hierarchy ----------
    def prepare_call_hierarchy(self, file_path, line, ch):
        """Prepare the call-hierarchy anchor item at the given cursor."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri}, "position": {"line": line, "character": ch}
        })

    def incoming_calls(self, item):
        """Return incoming call edges for a prepared call-hierarchy item."""
        return self.t.request("callHierarchy/incomingCalls", {"item": item})

    def outgoing_calls(self, item):
        """Return outgoing call edges for a prepared call-hierarchy item."""
        return self.t.request("callHierarchy/outgoingCalls", {"item": item})

    # ---------- Type hierarchy ----------
    def prepare_type_hierarchy(self, file_path, line, ch):
        """Prepare the type-hierarchy anchor item at the given cursor."""
        uri = _to_uri(self.t.root_path, file_path)
        return self.t.request("textDocument/prepareTypeHierarchy", {
            "textDocument": {"uri": uri}, "position": {"line": line, "character": ch}
        })

    def supertypes(self, item):
        """Return direct supertypes of a prepared type-hierarchy item."""
        return self.t.request("typeHierarchy/supertypes", {"item": item})

    def subtypes(self, item):
        """Return direct subtypes / implementers of a prepared type-hierarchy item."""
        return self.t.request("typeHierarchy/subtypes", {"item": item})

    # ---------- Small helpers ----------
    def find_symbol_by_name_in_file(self, file_path, name):
        """Find the starting position of a function / method by name inside one file."""
        resp = self.document_symbol(file_path)
        if not resp or not resp.get("result"):
            return None

        def walk(arr):
            for s in arr:
                # DocumentSymbol shape.
                if "range" in s:
                    if s.get("kind") in self.METHOD_KINDS and s.get("name") == name:
                        sr = s.get("selectionRange") or s.get("range")
                        return (sr["start"]["line"], sr["start"]["character"])
                    child = s.get("children") or []
                    r = walk(child)
                    if r:
                        return r
                # SymbolInformation shape.
                elif "location" in s:
                    if s.get("kind") in self.METHOD_KINDS and s.get("name") == name:
                        r = s["location"]["range"]
                        return (r["start"]["line"], r["start"]["character"])
            return None

        return walk(resp["result"])
