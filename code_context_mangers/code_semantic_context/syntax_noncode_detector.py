# file: syntax_noncode_detector.py
from dataclasses import dataclass
from typing import List, Tuple, Optional
import re

@dataclass
class LangSpec:
    single_line: List[str]              # e.g. ["#", "//"]
    block: List[Tuple[str, str]]        # e.g. [("/*","*/")]
    strings: List[Tuple[str, str, Optional[str], str]]
    # strings: list of (start, end, escape, mode)
    #   mode in {"normal","raw","verbatim"}; escape like "\\" or '""' (C# verbatim)
    ruby_begin_end: bool = False        # =begin ... =end

@dataclass
class ScanState:
    in_block: bool = False
    block_end: Optional[str] = None

    in_ruby_block: bool = False         # =begin ... =end

    in_string: bool = False
    str_end: Optional[str] = None
    str_mode: str = "normal"
    str_escape: Optional[str] = None
    escaped: bool = False               # for "\\" escaping

def is_cursor_in_noncode(
    primary_lang_id: str,
    file_lines: List[str],
    line_0based: int,
    col_0based: int,
) -> Tuple[bool, str, str]:
    """
    Returns: (is_noncode, kind, detail)
      - is_noncode: whether the cursor is in a non-code region
      - kind: "comment" | "string" | "none"
      - detail: "line-comment" | "block-comment" | "triple-quote" | "verbatim" | "string" | ""
    Scans only the text from the start of the file up to (line_0based, col_0based).
    """
    spec = _make_lang_spec(primary_lang_id)
    target_line = max(0, min(line_0based, len(file_lines) - 1))
    target_col = max(0, col_0based)

    st = ScanState()
    line_comment_start_at: Optional[int] = None

    for i in range(0, target_line + 1):
        line = file_lines[i] if i < len(file_lines) else ""
        limit = len(line) if i < target_line else min(target_col, len(line))
        j = 0

        # Ruby =begin/=end block comments are line-level and ignore indentation.
        if spec.ruby_begin_end:
            stripped = line.lstrip()
            if not st.in_string and not st.in_block and not st.in_ruby_block:
                if re.match(r"^=begin\b", stripped):
                    st.in_ruby_block = True
            elif st.in_ruby_block:
                if re.match(r"^=end\b", stripped):
                    if i == target_line and target_col >= 0:
                        return True, "comment", "ruby-begin-end"
                    st.in_ruby_block = False

        while j < limit:
            ch = line[j]

            # --- Inside a block comment ---
            if st.in_block:
                if _starts_with(line, j, st.block_end):
                    st.in_block, st.block_end = False, None
                    j += len(st.block_end or "")
                    continue
                j += 1
                continue

            # --- Inside a Ruby line-level block comment ---
            if st.in_ruby_block:
                if i == target_line:
                    return True, "comment", "ruby-begin-end"
                j = limit
                continue

            # --- Inside a string ---
            if st.in_string:
                j = _advance_in_string(line, j, st)
                continue

            # --- Code state: try starts ---

            # 1) Block-comment start.
            if _try_start_block_comment(line, j, spec, st):
                if i == target_line and j <= target_col:
                    return True, "comment", "block-comment"
                # Start matched; advance j.
                j += len(st.block_end) and 2 or 2
                # Simplified advancement; the next loop immediately enters the in_block branch.
                continue

            # 2) Single-line comment start.
            tok = _match_any(line, j, spec.single_line)
            if tok:
                if i == target_line and line_comment_start_at is None:
                    line_comment_start_at = j
                j = limit  # End of line.
                continue

            # 3) String start. Longer tokens are matched first, such as Python triple quotes and C# $@".
            started, consumed, kind = _try_start_string(line, j, spec, st)
            if started:
                if i == target_line and j <= target_col:
                    detail = "verbatim" if st.str_mode == "verbatim" else ("triple-quote" if len(st.str_end or "") > 1 else "string")
                    return True, "string", detail
                j += consumed
                continue

            j += 1

        # Single-line comment check, only when the comment token is to the left of the cursor.
        if i == target_line and line_comment_start_at is not None and line_comment_start_at <= target_col:
            return True, "comment", "line-comment"

    # If scanning ends while still inside an unclosed comment/string, treat it as non-code.
    if st.in_block or st.in_ruby_block:
        return True, "comment", "block-comment"
    if st.in_string:
        return True, "string", "string"
    return False, "none", ""

# ----------------
# Helper functions and language configuration
# ----------------

def _make_lang_spec(lang: str) -> LangSpec:
    l = (lang or "").lower().strip()
    if l in {"python"}:
        return LangSpec(
            single_line=["#"],
            block=[],
            strings=[
                ("'''", "'''", None, "raw"),
                ('"""', '"""', None, "raw"),
                ("'", "'", "\\", "normal"),
                ('"', '"', "\\", "normal"),
            ],
        )
    if l in {"javascript", "js", "typescript", "ts"}:
        return LangSpec(
            single_line=["//"],
            block=[("/*", "*/")],
            strings=[
                ("`", "`", "\\", "normal"),
                ("'", "'", "\\", "normal"),
                ('"', '"', "\\", "normal"),
            ],
        )
    if l in {"go", "golang"}:
        return LangSpec(
            single_line=["//"],
            block=[("/*", "*/")],
            strings=[
                ("`", "`", None, "raw"),
                ("'", "'", "\\", "normal"),
                ('"', '"', "\\", "normal"),
            ],
        )
    if l in {"java"}:
        return LangSpec(
            single_line=["//"],
            block=[("/*", "*/")],
            strings=[
                ("'", "'", "\\", "normal"),
                ('"', '"', "\\", "normal"),
            ],
        )
    if l in {"csharp", "cs"}:
        return LangSpec(
            single_line=["//"],
            block=[("/*", "*/")],
            strings=[
                ('$@"', '"', '""', "verbatim"),
                ('@"', '"', '""', "verbatim"),
                ('$"', '"', "\\", "normal"),
                ('"', '"', "\\", "normal"),
                ("'", "'", "\\", "normal"),
            ],
        )
    if l in {"php"}:
        return LangSpec(
            single_line=["//", "#"],
            block=[("/*", "*/")],
            strings=[
                ("'", "'", "\\", "normal"),
                ('"', '"', "\\", "normal"),
            ],
        )
    if l in {"ruby"}:
        return LangSpec(
            single_line=["#"],
            block=[],  # Typical Ruby does not use /* */ comments.
            strings=[
                ("'", "'", "\\", "normal"),
                ('"', '"', "\\", "normal"),
            ],
            ruby_begin_end=True,
        )
    # Default C/Java-style configuration.
    return LangSpec(
        single_line=["//", "#"],
        block=[("/*", "*/")],
        strings=[
            ("'", "'", "\\", "normal"),
            ('"', '"', "\\", "normal"),
        ],
    )

def _match_any(s: str, pos: int, tokens: List[str]) -> Optional[str]:
    for t in tokens:
        if s.startswith(t, pos):
            return t
    return None

def _starts_with(s: str, pos: int, tok: Optional[str]) -> bool:
    return bool(tok) and s.startswith(tok, pos)

def _try_start_block_comment(line: str, j: int, spec: LangSpec, st: ScanState) -> bool:
    for bstart, bend in spec.block:
        if line.startswith(bstart, j):
            st.in_block = True
            st.block_end = bend
            return True
    return False

def _try_start_string(line: str, j: int, spec: LangSpec, st: ScanState) -> Tuple[bool, int, str]:
    # Match longer start tokens first.
    for start, end, escape, mode in sorted(spec.strings, key=lambda x: -len(x[0])):
        if line.startswith(start, j):
            st.in_string = True
            st.str_end = end
            st.str_mode = mode
            st.str_escape = escape
            st.escaped = False
            return True, len(start), mode
    return False, 0, ""

def _advance_in_string(line: str, j: int, st: ScanState) -> int:
    """Advance the in-string state machine and return the new j."""
    # C# verbatim string: " closes it; "" escapes a quote.
    if st.str_mode == "verbatim":
        if j < len(line) and line[j] == '"':
            nxt = line[j+1:j+2]
            if nxt == '"':  # Escaped quote.
                return j + 2
            # End of string.
            st.in_string = False
            st.str_end = None
            st.str_mode = "normal"
            st.str_escape = None
            return j + 1
        return min(j + 1, len(line))

    # Normal/raw strings, including triple quotes and backticks.
    if st.str_escape == "\\":
        if st.escaped:
            st.escaped = False
            return min(j + 1, len(line))
        if j < len(line) and line[j] == "\\":
            st.escaped = True
            return min(j + 1, len(line))

    end_tok = st.str_end or ""
    if end_tok and line.startswith(end_tok, j):
        st.in_string = False
        st.str_end = None
        st.str_mode = "normal"
        st.str_escape = None
        return j + len(end_tok)

    return min(j + 1, len(line))
