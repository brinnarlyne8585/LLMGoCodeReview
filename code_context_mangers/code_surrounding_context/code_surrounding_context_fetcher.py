"""
code_consistency_context_fetcher.py
"""

import re
import sys
from typing import Dict, List, Optional, Tuple

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_orchestrator import ContextBlock
from code_context_mangers.code_context_utils import (
    FUNCTION_LEVEL_KINDS,
    STRUCT_LEVEL_KINDS,
    parse_hunk_and_find_anchors,
    parse_hunk_header,
)
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag

class SurroundingContextFetcher:
    """
        Generate surrounding code context for a given diff hunk.

        The process includes:
        1. Try to use LSP, through document symbols, to expand the hunk to the full enclosing function or code block.
        2. If LSP fails or produces no valid expansion, fall back to a line-window expansion strategy.
        3. Get other hunks in the same file that overlap with the expanded window, i.e. co-occurring changes.
        4. Merge those related hunks inline into the file-content window and highlight the target hunk under review.
        """

    def __init__(self,
                 task: dict,
                 querier: UnifiedQuerier,
                 lsp_available: bool=True):
        self.task = task
        self.querier = querier
        self.lsp_available = lsp_available
        self.path = task.get('path', '')
        self.main_reviewing_file_content = self.querier.file_provider.get_file_content(self.path)

    def fetch_block(self) -> Dict[str, str]:
        """
        Core enrichment logic: generate a temporary, unmerged context fragment.
        """
        original_hunk = self.task.get('hunk_change', '')
        path = self.task.get('path', '')

        anchor_lines = parse_hunk_and_find_anchors(original_hunk)
        if not anchor_lines:
            msg = f"{path}: Lines N/A to N/A.\nCould not parse hunk."
            return {
                'new_file_context': msg,
                'related_changes': f"{path}:\n(No patch found)",
            }

        lsp_expanded = False
        min_line, max_line = 0, 0

        # Decide whether to force cache-only queries:
        #    - when requested by the caller (is_always_query_with_cache = True)
        #    - or when LSP is unavailable (self.lsp_available = False), leaving cache as the only option.
        force_cache_only = is_always_query_with_cache or not self.lsp_available
        resp = self.querier.query_by_cache_flag(
            file_path=path,
            command="get_document_symbol",
            params={},
            is_always_query_with_cache=force_cache_only,
        )

        # LSP query successfully returned content.
        if not self.querier.is_null_or_empty_result(resp):
            symbols = resp.get('result') or []
            function_blocks = self._find_function_containing_blocks(symbols, anchor_lines)

            if function_blocks:
                min_line = min(int(b['start_line']) for b in function_blocks)
                max_line = max(int(b['end_line']) for b in function_blocks)
                hunk_range = self._parse_hunk_header_for_new_file(original_hunk)
                if hunk_range:
                    hs, hc = hunk_range
                    he = hs + hc - 1
                    # Treat this as a successful expansion only when the LSP range is larger than the original hunk range.
                    if not (hs <= min_line and max_line <= he):
                        lsp_expanded = True
                        # Take the union of hunk_range and function_blocks.
                        min_line = min(min_line, hs)
                        max_line = max(max_line, he)
                else:
                    lsp_expanded = True

        # If LSP fails or does not expand the range, use the line-count-based fallback.
        if not lsp_expanded:
            new_body, min_line, max_line = self._extract_segment_by_lines(self.main_reviewing_file_content,
                                                                          original_hunk,
                                                                          max_lines=50)
        if min_line > 0 and max_line > 0:
            return ContextBlock(
                start_line=min_line,
                end_line=max_line,
                source='neighborhood',
                path=self.path
            )
        return None

    @staticmethod
    def _find_function_containing_blocks(flat_symbols: List[Dict], anchor_lines: List[int]) -> List[Dict]:
        function_symbols = [s for s in flat_symbols if s.get("kind") in (FUNCTION_LEVEL_KINDS | STRUCT_LEVEL_KINDS)]
        containing: List[Dict] = []
        processed_symbols = set()
        for ln in anchor_lines:
            for s in function_symbols:
                s_tuple = (s['name'], s['start_line'], s['end_line'])
                if s_tuple in processed_symbols:
                    continue
                if int(s["start_line"]) <= ln <= int(s["end_line"]):
                    containing.append(s)
                    processed_symbols.add(s_tuple)
        return containing

    @staticmethod
    def _parse_hunk_header_for_new_file(hunk_str: str) -> Optional[Tuple[int, int]]:
        lines = hunk_str.splitlines()
        if not lines:
            return None
        hdr = parse_hunk_header(lines[0], anchored=True)
        return (hdr.c, hdr.dc) if hdr else None

    @staticmethod
    def _extract_segment_by_lines(
            file_content: str,
            patch: str,
            max_lines: Optional[int] = None,
            max_tokens: Optional[int] = None,
            avg_chars_per_token: int = 4,
    ) -> Tuple[str, int, int]:
        """Extract a line segment centered near the patch midpoint, then trim by token budget if needed.

        Returns: (segment_text, start_line, end_line), where line numbers are 1-based and inclusive.
        """
        max_lines = max_lines if max_lines is not None else sys.maxsize
        file_content = file_content.strip()
        lines = file_content.splitlines(keepends=True)
        N = len(lines)
        if N <= max_lines and max_tokens is None:
            return file_content, 1, N

        m = re.search(r"\+\s*(?P<start>\d+),(?P<count>\d+)\s*@@", patch)
        if m:
            try:
                start_line = int(m.group("start"))
                count = int(m.group("count"))
                mid_idx = start_line + count // 2 - 1
            except Exception:
                mid_idx = N // 2
        else:
            mid_idx = N // 2

        # If the hunk range already exceeds max_lines.
        if count >= max_lines:
            left_1based = max(1, start_line)
            right_1based = min(N, start_line + count - 1)

            left = left_1based - 1
            right = right_1based
        else:
            half = max_lines // 2
            left = max(0, mid_idx - half)
            right = min(N, left + max_lines)
            if right - left < max_lines:
                left = max(0, right - max_lines)

        if max_tokens is not None:
            max_chars = max_tokens * avg_chars_per_token
            window_chars = sum(len(lines[i]) for i in range(left, right))
            while window_chars > max_chars and (right - left) > 1:
                dist_left = mid_idx - left
                dist_right = (right - 1) - mid_idx
                if dist_left > dist_right:
                    window_chars -= len(lines[left])
                    left += 1
                else:
                    window_chars -= len(lines[right - 1])
                    right -= 1

        start_num = left + 1
        end_num = right
        segment = "".join(lines[left:right])
        return segment, start_num, end_num

    def fetch_formatted_hunk_with_signature(self) -> str:
        """
        Get a formatted hunk string with function-signature context.

        1. Parse the hunk to get its range in the new file.
        2. Try to use LSP to find the function containing this hunk.
        3. Determine the real function start line, skipping comments.
        4. If the function starts before the hunk, prepend the function signature and an ellipsis.
        5. Otherwise, format only the hunk itself.
        6. Return the final formatted string for the LLM prompt.
        """
        original_hunk = self.task.get('hunk_change', '')
        path = self.task.get('path', '')

        hunk_range = self._parse_hunk_header_for_new_file(original_hunk)

        if not hunk_range:
            return f"File: {path}\n(Could not parse hunk header)"

        hunk_start_line, hunk_count = hunk_range
        # hunk_start_line is still valid even when hunk_count is 0, such as deletion-only hunks.
        hunk_end_line = hunk_start_line + hunk_count - 1 if hunk_count > 0 else hunk_start_line

        # 1. Try to find a function signature that should be prepended.
        prepending_sig = self._find_prepending_function_signature(original_hunk, hunk_start_line)

        # 2. Determine the final start and end lines.
        final_start_line: int
        final_end_line: int

        if prepending_sig:
            sig_line_num, sig_content = prepending_sig
            final_start_line = sig_line_num
            final_end_line = hunk_end_line
        else:
            final_start_line = hunk_start_line
            final_end_line = hunk_end_line

        if final_start_line < 0 or final_end_line < 0:
            return f"File: {path}\n(Invalid line range calculation)"

        # 3. Start formatting.
        padding_width = len(str(final_end_line))
        formatted_lines = []

        # 3.1 Add the file header.
        header = f"File: {path} Lines: {final_start_line}-{final_end_line}"
        formatted_lines.append(header)

        # 3.2 Add the prepended signature if present.
        if prepending_sig:
            sig_line_num, sig_content = prepending_sig
            tag = str(sig_line_num).zfill(padding_width)
            formatted_lines.append(f"[{tag}] {sig_content.rstrip()}")

            # Add an ellipsis.
            if sig_line_num + 1 < hunk_start_line:
                formatted_lines.append(f"(Lines {sig_line_num + 1}-{hunk_start_line - 1} omitted ...)")

        # 3.3 Format the hunk body.
        hunk_lines = original_hunk.splitlines()
        current_line_num = hunk_start_line
        removal_tag = "-" * padding_width

        # Start from the second hunk line, skipping @@ ... @@.
        for line in hunk_lines[1:]:
            prefix = line[0] if line else ' '

            if not line:  # Empty line, which may appear in the middle of a hunk.
                tag = str(current_line_num).zfill(padding_width)
                formatted_lines.append(f"[{tag}] {line}")
                current_line_num += 1
                continue

            if prefix == '+':
                tag = str(current_line_num).zfill(padding_width)
                formatted_lines.append(f"[{tag}] {line}")
                current_line_num += 1
            elif prefix == '-':
                formatted_lines.append(f"[{removal_tag}] {line}")
            elif prefix == '\\':
                # "\ No newline at end of file"
                formatted_lines.append(f"[{removal_tag}] {line}")
            elif prefix == ' ':
                # Context line inside the hunk.
                tag = str(current_line_num).zfill(padding_width)
                formatted_lines.append(f"[{tag}] {line}")
                current_line_num += 1
            # Ignore @@ lines if the hunk contains multiple headers.
            elif prefix == '@' and line.startswith('@@'):
                continue

        return "\n".join(formatted_lines)

    def _find_prepending_function_signature(self, original_hunk: str, hunk_start_line: int) -> Optional[
        Tuple[int, str]]:
        """
        Find the function containing the hunk and determine whether its signature should be prepended.
        Return (true_start_line_1based, line_content) when prepending is needed; otherwise return None.
        """
        path = self.task.get('path', '')
        anchor_lines = parse_hunk_and_find_anchors(original_hunk)
        if not anchor_lines:
            return None

        # 1. Query LSP symbols.
        resp = self.querier.query_cache_only(
            file_path=path,
            command="get_document_symbol",
            params={},
            # Post-processing parameters scoped by command.
            post_process_params={
                "get_document_symbol": {
                    "prefer_selection_range": True  # Prefer selectionRange when available.
                }
            },
        )

        # Exit when the result is empty.
        if self.querier.is_null_or_empty_result(resp):
            return None

        # 2. Find the function block that contains the hunk.
        symbols = resp.get('result') or []
        function_blocks = self._find_function_containing_blocks(symbols, anchor_lines)
        if not function_blocks:
            return None

        # 3. Pick the earliest function block.
        function_blocks.sort(key=lambda b: int(b['start_line']))
        earliest_block = function_blocks[0]
        earliest_function_start_line_1based = earliest_block['start_line']

        file_lines = self.main_reviewing_file_content.splitlines()
        line_content = file_lines[earliest_function_start_line_1based - 1]
        return (earliest_function_start_line_1based, line_content)


