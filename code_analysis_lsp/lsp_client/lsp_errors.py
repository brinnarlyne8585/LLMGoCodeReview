# code_analysis_lsp/lsp_errors.py
from dataclasses import dataclass

# ---- Canonical error codes ----
INIT_HARD_TIMEOUT        = "INIT_HARD_TIMEOUT"
INIT_EXIT_IMMEDIATE      = "INIT_PROCESS_EXIT_IMMEDIATE"
INIT_EXIT_DURING_WAIT    = "INIT_PROCESS_EXIT_DURING_WAIT"
INIT_SERVER_ERROR        = "INIT_SERVER_ERROR"
INIT_ERROR               = "INIT_ERROR"

# ---- Non-retriable errors (tunable if needed) ----
NON_RETRIABLE = {
    INIT_HARD_TIMEOUT,
    INIT_EXIT_IMMEDIATE,
    INIT_SERVER_ERROR,
}

# ---- Error-message templates (single source of truth) ----
TEMPLATES = {
    INIT_HARD_TIMEOUT:
        "[LSP Client] Initialize failed: Hard timeout after {seconds}s. "
        "Process running={running}, exit_code={exit_code}. Last STDERR:\n{stderr_tail}",

    INIT_EXIT_IMMEDIATE:
        "[LSP Client] Initialize failed: Process exited immediately "
        "with code {exit_code}. Last STDERR:\n{stderr_tail}",

    INIT_EXIT_DURING_WAIT:
        "[LSP Client] Initialize failed: Process exited "
        "during 'initialize' wait (code={exit_code}).",

    INIT_SERVER_ERROR:
        "[LSP Client] Initialize failed: Server returned error: {server_error}",

    INIT_ERROR:
        "[LSP Client] Initialize failed: {message}",
}

@dataclass
class LSPInitError(RuntimeError):
    code: str
    message: str
    def __str__(self) -> str:
        return self.message

def format_init_error(code: str, **kwargs) -> str:
    tmpl = TEMPLATES.get(code, TEMPLATES[INIT_ERROR])
    try:
        return tmpl.format(**kwargs)
    except Exception:
        # Fall back to the raw template if some format arguments are missing.
        return tmpl

# ---- Prefix-based fallback classification for legacy logs ----
PREFIX_MAP = {
    "Hard timeout after ":     INIT_HARD_TIMEOUT,
    "Process exited immediately": INIT_EXIT_IMMEDIATE,
    "Process exited during 'initialize' wait": INIT_EXIT_DURING_WAIT,
    "Server returned error: ": INIT_SERVER_ERROR,
}

def classify_init_error_from_text(text: str) -> str:
    if not text:
        return INIT_ERROR
    # Match only the key signal and ignore variable details such as paths or durations.
    for key, code in PREFIX_MAP.items():
        if key in text:
            return code
    return INIT_ERROR
