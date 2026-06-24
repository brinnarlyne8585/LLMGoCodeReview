"""
code_context_utils.py
---------------------
Purpose:
  Shared constants and helper functions used by code_content_manager.py and
  consistency_context_fetcher.py.
"""
import os
import re
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple, Union

import Levenshtein

# =========================
# Constants
# =========================

# LSP Symbol Kinds
FUNCTION_LEVEL_KINDS = {6, 9, 12}  # Method, Constructor, Function
STRUCT_LEVEL_KINDS = {5, 11, 23}   # Class, Interface, Struct
ALL_RECOGNIZED_KINDS = FUNCTION_LEVEL_KINDS | STRUCT_LEVEL_KINDS

# Hunk header regex.
HUNK_HEADER_RE = re.compile(
    r"^@@\s*-(?P<a>\d+)(?:,(?P<ac>\d+))?\s+\+(?P<c>\d+)(?:,(?P<dc>\d+))?\s*@@(?P<extra>.*)$",
    re.MULTILINE,
)


# =========================
# Dataclasses
# =========================

@dataclass(frozen=True)
class HunkHeader:
    a: int      # old-file start
    ac: int     # old-file count
    c: int      # new-file start
    dc: int     # new-file count
    extra: str = ""

    @property
    def short(self) -> str:
        return f"@@ -{self.a},{self.ac} +{self.c},{self.dc} @@"

# =========================
# Helper Functions
# =========================

def parse_hunk_header(line: str, anchored: bool = True) -> Optional[HunkHeader]:
    """
    anchored=True  -> require the line to start with @@.
    anchored=False -> allow @@ to appear later in the line.
    """
    m = HUNK_HEADER_RE.match(line) if anchored else HUNK_HEADER_RE.search(line)
    if not m:
        return None
    return HunkHeader(
        a=int(m.group("a")),
        ac=int(m.group("ac") or 1),
        c=int(m.group("c")),
        dc=int(m.group("dc") or 1),
        extra=m.group("extra") or "",
    )

def parse_hunk_and_find_anchors(hunk_str: str) -> List[int]:
    """Parse a hunk and return changed 1-based line numbers in the new file."""
    lines = hunk_str.splitlines()
    if not lines:
        return []
    hdr = parse_hunk_header(lines[0], anchored=True)
    if not hdr:
        return []
    new_start_line = hdr.c
    anchor_lines: set[int] = set()
    current_new = new_start_line
    for line in lines[1:]:
        if line.startswith('+'):
            anchor_lines.add(current_new)
            current_new += 1
        elif line.startswith('-'):
            # Deleted lines anchor to the next non-deleted line position.
            anchor_lines.add(current_new)
        else:  # space or other
            if line.startswith(' '):
                current_new += 1
    return sorted(anchor_lines)

def parse_hunk_and_find_added_lines(hunk_str: str) -> List[int]:
    """
    Parse a hunk and return only added-line 1-based line numbers in the new file.

    Args:
        hunk_str: Full hunk string, including the "@@ ... @@" header.

    Returns:
        A list of added-line numbers, e.g., [55, 56].
    """
    lines = hunk_str.splitlines()
    if not lines:
        return []

    # Reuse parse_hunk_header.
    hdr = parse_hunk_header(lines[0], anchored=True)
    if not hdr:
        return []

    new_start_line = hdr.c
    added_lines: set[int] = set()
    current_new = new_start_line

    for line in lines[1:]:
        if line.startswith('+'):
            # Added line.
            added_lines.add(current_new)
            current_new += 1
        elif line.startswith('-'):
            # Deleted lines do not consume new-file line numbers.
            pass
        elif line.startswith(' '):
            # Context line.
            current_new += 1
        # Ignore special lines such as "\ No newline at end of file".

    return sorted(list(added_lines))

