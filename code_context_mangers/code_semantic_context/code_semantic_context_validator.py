"""
code_semantic_plan_validator.py
--------------------------
Purpose:
  Provides the `SemanticPlanValidator` class, which parses, normalizes,
  and validates raw LLM command plans. This is step 1 in `SemanticContextFetcher`.
"""
import re
from typing import List, Dict, Optional, Set, Tuple, Iterable

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_utils import parse_hunk_and_find_added_lines
from code_context_mangers.code_semantic_context.code_semantic_context_model import SemanticCommand
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag

class SemanticPlanValidator:
    """
    Parse and validate the raw LLM plan.
    """

    _junk_quote_chars = set("'\"`")

    def __init__(self,
                 querier: UnifiedQuerier,
                 task: dict,
                 main_file_lines: List[str],
                 lsp_available: True,
                 search_window_0based: Optional[Tuple[int, int]] = None,):

        self.querier = querier
        self.lsp_available = lsp_available
        self.task = task
        self.main_file_lines = main_file_lines
        self.hunk = self.task.get('hunk_change', '')
        self._cached_modified_lines: Optional[Set[int]] = None

        # Search window [low, high], 0-based and inclusive. Defaults to the full file.
        self._win_low = 0
        self._win_high = len(self.main_file_lines) - 1
        if search_window_0based is not None and self.main_file_lines:
            lo, hi = search_window_0based
            if lo > hi:
                lo, hi = hi, lo
            self._win_low = max(0, lo)
            self._win_high = min(len(self.main_file_lines) - 1, hi)

        self.path = self.task.get("path") # Path of the reviewed file.

    def _get_modified_line_numbers(self) -> Set[int]:
        """Lazily load and cache the set of modified line numbers."""
        if self._cached_modified_lines is None:
            lines = parse_hunk_and_find_added_lines(self.hunk)
            self._cached_modified_lines = set(lines) if lines else set()
        return self._cached_modified_lines

    def process_and_validate_plan(self, llm_plan: List[Dict]) -> List[SemanticCommand]:
        """
            Parse, normalize, and validate the raw command list from the LLM.
              1) Run steps 1-4 for all items and record source rules.
              2) Deduplicate items with no early errors by (command_type, line_num, target_symbol):
                 - keep the first occurrence
                 - mark later duplicates as already executed elsewhere
                 - merge duplicate source rules into the first kept item
              3) Apply rule/modified-line filtering, skipping items with existing errors.
              4) Mark final valid commands with is_valid=True.
            """
        # ---------- Pass 1: normalize steps 1-4 and source rules ----------
        cmds: List[SemanticCommand] = []
        for raw_cmd_dict in llm_plan:

            cmd = SemanticCommand(
                raw_command = raw_cmd_dict.get("command", ""),
                raw_line = str(raw_cmd_dict.get("line", "")),
                raw_target_symbol = raw_cmd_dict.get("target_symbol", ""),
            )

            # 4.5: Fill source rules.
            source_rule_id = raw_cmd_dict.get("source_rule_id")
            if source_rule_id:
                if isinstance(source_rule_id, str):
                    cmd.source_rules.add(source_rule_id)
                elif isinstance(source_rule_id, (list, set)):
                    cmd.source_rules.update(source_rule_id)

            # Step 1: normalize and validate command type.
            self._format_and_validate_command_type(cmd)
            # Step 2: normalize target_symbol.
            if not cmd.error_message:
                self._preprocess_target_symbol(cmd)
            # Step 3: correct the line number.
            if not cmd.error_message:
                self._correct_line_and_symbol(cmd)
            # Step 4: compute cursor.
            if not cmd.error_message:
                self._format_and_validate_cursor(cmd)
            if not cmd.error_message:
                self. _validate_get_search_required(cmd)

            cmds.append(cmd)

        # ---------- Pass 2: deduplicate by tuple, only for items without early errors ----------
        first_index: Dict[Tuple[Optional[str], Optional[int], Optional[str]], int] = {}

        # Record the first occurrence.
        for i, cmd in enumerate(cmds):
            if cmd.error_message:
                continue  # Items with early errors do not participate in deduplication.
            key = cmd.identity_key()
            if key not in first_index:
                first_index[key] = i

        # Mark duplicates.
        for i, cmd in enumerate(cmds):
            if cmd.error_message:
                continue
            key = cmd.identity_key()
            # Skip items not present in first_index, e.g. when the tuple contains None.
            if key not in first_index:
                continue
            win_cmd_index = first_index[key]
            # Non-first occurrence: mark as duplicate.
            if i != win_cmd_index:
                win_cmd = cmds[win_cmd_index]
                win_cmd.source_rules |= cmd.source_rules  # Merge duplicate source rules.
                win_cmd_str = win_cmd.get_format_cmd()
                cmd.error_message = f"Already executed elsewhere; equivalent to {win_cmd_str}"
                continue

        # ---------- Pass 3: rule / modified-line filtering, skipping existing errors ----------
        for cmd in cmds:
            if cmd.error_message:
                continue
            if cmd.requires_modified_line:
                self._filter_by_modified_line(cmd)
                if cmd.error_message:
                    continue
            cmd.is_valid = True

        return cmds
    #-------------------- Step 1: normalize and validate command type --------------------#
    def _format_and_validate_command_type(self, cmd: SemanticCommand) -> None:
        """
        Step 1: normalize and validate command type.
        """
        cmd_lower = cmd.raw_command.lower()
        if "reference" in cmd_lower:
            cmd.command_type = "get_references"
        elif "definition" in cmd_lower:
            cmd.command_type = "get_definition"
        elif "search" in cmd_lower:
            cmd.command_type = "get_search"
        else:
            cmd.error_message = f"Unrecognized command type: '{cmd.raw_command}'"

    # -------------------- Step 2: normalize target_symbol --------------------#
    def _preprocess_target_symbol(self, cmd: SemanticCommand) -> None:
        """
        Step 2: normalize target_symbol.
        - get_references / get_definition: strip any surrounding quote characters.
        - get_search: keep raw_target_symbol if it exists in the hunk; otherwise strip surrounding quotes.
        """
        raw_symbol = (cmd.raw_target_symbol or "").strip()
        if not raw_symbol:
            cmd.error_message = "raw_target_symbol must not be empty"
            return

        if cmd.command_type in ("get_references", "get_definition"):
            fixed = self._strip_surrounding_quotes(raw_symbol)
        elif cmd.command_type == "get_search":
            # Keep the raw symbol when it appears in the hunk; otherwise strip quotes.
            if raw_symbol and (raw_symbol in (self.hunk or "")):
                fixed = raw_symbol
            else:
                fixed = self._strip_surrounding_quotes(raw_symbol)
        else:
            # Should not be reached because command type validation already guards this.
            fixed = raw_symbol

        if not fixed:
            cmd.error_message = "Normalized target_symbol is empty; cannot continue"
            return

        cmd.target_symbol = fixed

    def _strip_surrounding_quotes(self, s: str) -> str:
        """
        Strip surrounding quote characters (' " `), including multiple outer layers.
        Only handles strings whose first and last characters are quote characters,
        and also trims surrounding whitespace.
        """
        t = s.strip()
        while len(t) >= 2 and (t[0] in self._junk_quote_chars) and (t[-1] in self._junk_quote_chars):
            t = t[1:-1].strip()
        return t

    # -------------------- Word-boundary matching helpers --------------------#
    # Compile once and cache the pattern; key = symbol string.
    _word_boundary_cache: Dict[str, re.Pattern] = {}

    @classmethod
    def _word_boundary_pattern(cls, symbol: str) -> re.Pattern:
        """Build and cache a word-boundary regex for symbol."""
        if symbol not in cls._word_boundary_cache:
            cls._word_boundary_cache[symbol] = re.compile(
                r'(?<!\w)' + re.escape(symbol) + r'(?!\w)'
            )
        return cls._word_boundary_cache[symbol]

    def _find_symbol_in_line(self, symbol: str, line_content: str,
                              use_word_boundary: bool) -> int:
        """
        Find symbol in line_content and return its start position, or -1 if not found.
        When use_word_boundary=True, use word-boundary matching for get_definition/get_references.
        When use_word_boundary=False, use plain substring matching for get_search.
        """
        if use_word_boundary:
            m = self._word_boundary_pattern(symbol).search(line_content)
            return m.start() if m else -1
        else:
            return line_content.find(symbol)

    def _symbol_in_line(self, symbol: str, line_content: str,
                         use_word_boundary: bool) -> bool:
        """Return whether symbol exists in line_content."""
        return self._find_symbol_in_line(symbol, line_content, use_word_boundary) >= 0

    # -------------------- Step 3: line-number correction --------------------#
    def _correct_line_and_symbol(self, cmd: SemanticCommand) -> None:
        """
        Step 3: line-number correction.
        """
        # Parse only the raw line string and use it as the search anchor.
        line_match = re.search(r'\d+', cmd.raw_line)
        if not line_match:
            cmd.error_message = f"Invalid line format; no number found in: '{cmd.raw_line}'"
            return
        # Store the 0-based anchor line number.
        cmd.line_num = int(line_match.group(0)) - 1

        if cmd.line_num is None:
            cmd.error_message = "Internal error: anchor line is missing before symbol correction"
            return

        # get_search uses substring matching; get_definition/get_references use word-boundary matching.
        use_wb = cmd.command_type in ("get_definition", "get_references")

        # 1. Build the search list sorted by distance.
        symbol = cmd.target_symbol
        anchor_line_0based = cmd.line_num
        sorted_lines_to_search: List[Tuple[int, int]] = self._build_search_lines(anchor_line_0based)

        # 2. Search in order, preferring nearby lines.
        for line_0based, distance in sorted_lines_to_search:
            content = self.main_file_lines[line_0based]
            if self._symbol_in_line(symbol, content, use_wb):
                cmd.line_num = line_0based
                return

        # No candidate line contained the symbol.
        cmd.error_message = f"Could not find '{symbol}' in candidate lines"

    def _build_search_lines(self, anchor_line_0based: int) -> List[Tuple[int, int]]:
        """
        Build the ordered search list:
        1) anchor line
        2) nearby modified lines, preferring lower lines before upper lines at the same distance
        3) nearby unmodified lines, with the same distance ordering
        Returns items as (line_0based, distance).
        """
        n = len(self.main_file_lines)
        seen: Set[int] = set()
        ordered: List[Tuple[int, int]] = []

        # Normalize modified lines to 0-based and drop out-of-range values.
        modified_0based = {
            i - 1 for i in self._get_modified_line_numbers()
            if 0 <= (i - 1) < n
        }

        # 1) Add the anchor line first if it is in range.
        if 0 <= anchor_line_0based < n:
            ordered.append((anchor_line_0based, 0))
            seen.add(anchor_line_0based)

        # 2) Expand to neighbors, collecting modified neighbors before unmodified neighbors.
        modified_neighbors: List[Tuple[int, int]] = []
        unmodified_neighbors: List[Tuple[int, int]] = []

        for idx, dist in self._iter_neighbor_indices_in_window(anchor_line_0based):
            if idx in seen:
                continue
            if idx in modified_0based:
                modified_neighbors.append((idx, dist))
            else:
                unmodified_neighbors.append((idx, dist))
            seen.add(idx)

        return ordered + modified_neighbors + unmodified_neighbors

    def _iter_neighbor_indices_in_window(self, anchor: int) -> Iterable[Tuple[int, int]]:
        """
        Expand from anchor within [self._win_low, self._win_high].
        For the same distance, yield the lower side first (larger line number),
        then the upper side (smaller line number). Only yields indices inside the window.
        """
        if not self.main_file_lines:
            return
        low, high = self._win_low, self._win_high
        # Anchor may be outside the window; still expand around it and yield only in-window lines.
        max_d = max(abs(anchor - low), abs(high - anchor))
        for d in range(1, max_d + 1):
            down = anchor + d
            if low <= down <= high:
                yield down, d
            up = anchor - d
            if low <= up <= high:
                yield up, d

    # -------------------- Step 4: cursor computation --------------------#
    def _format_and_validate_cursor(self, cmd: SemanticCommand) -> None:
        """
        Step 4: cursor computation.
        """
        if cmd.line_num is None or cmd.target_symbol is None:
            cmd.error_message = "Internal error: line number or symbol is empty when computing cursor"
            return

        # 1. Get line content.
        line_content = self.main_file_lines[cmd.line_num]

        # 2. Find the symbol start position in the line.
        use_wb = cmd.command_type in ("get_definition", "get_references")
        symbol_start_index = self._find_symbol_in_line(cmd.target_symbol, line_content, use_wb)

        if symbol_start_index == -1:
            # This should not happen because _correct_line_and_symbol already confirmed it.
            cmd.error_message = f"Internal error: symbol '{cmd.target_symbol}' still not found on corrected line {cmd.line_num + 1}"
            return

        # 3. --- Compute cursor position ---
        cursor_index_in_symbol = -1
        for i in range(len(cmd.target_symbol) - 1, -1, -1):
            if cmd.target_symbol[i].isalnum():
                cursor_index_in_symbol = i
                break  # Found it; stop scanning.

        if cursor_index_in_symbol == -1:
            cmd.error_message = f"Symbol '{cmd.target_symbol}' contains no letter or digit"
            cmd.cursor = symbol_start_index
        else:
            cmd.cursor = symbol_start_index + cursor_index_in_symbol

    # -------------------- Step 5: filter get_search() for overly simple symbols --------------------#
    def _validate_get_search_required(self, cmd: SemanticCommand) -> None:
        if cmd.command_type != "get_search":
            return
        if len(cmd.target_symbol)<3:
            cmd.error_message = f"Symbol '{cmd.target_symbol}' is too short; get_search() is unnecessary"


    # -------------------- Step 5 (2): filter by modified line and rule source --------------------#
    def _filter_by_modified_line(self, cmd: SemanticCommand) -> None:
        """
            Step 5: filter based on whether the corrected line is modified.
            - If the line is not modified but contains an unmatched '(',
              search downward for the matching ')'.
              If a modified line exists between them, keep the command.
            """

        # ---- The line is modified ----
        is_mod = self._is_line_modified(cmd.line_num)
        if is_mod:
            return

        # ---- Document-symbol allow rule: function/class block covers a modified line
        #      and the cursor is on its start line. ----
        file_path = self.task.get('path', '')
        # Force cache-only mode when requested by the caller or when LSP is unavailable.
        force_cache_only = is_always_query_with_cache or not self.lsp_available
        sym_resp = self.querier.query_by_cache_flag(
            file_path=file_path,
            command="get_document_symbol",
            params={},
            is_always_query_with_cache=force_cache_only,
        )
        if sym_resp and not sym_resp.get('error'):
            symbols = sym_resp.get('result') or []

            # 1-based anchor line and modified-line set.
            anchor_start_line_1based = cmd.line_num + 1
            modified_1based_set = set(self._get_modified_line_numbers())

            for symbol in symbols:
                start_line_1b = symbol.get('start_line')
                end_line_1b = symbol.get('end_line')

                # Require the cursor to be on the symbol start line.
                if anchor_start_line_1based != start_line_1b:
                    continue

                # Check whether the symbol block covers any modified line.
                # It is enough to have m in modified_1based_set and start <= m <= end.
                if any(start_line_1b <= m <= end_line_1b for m in modified_1based_set):
                    # Matched the symbol allow rule.
                    return

        # Filter commands that did not match any allow rule.
        cmd.error_message = f"Corrected line {cmd.line_num + 1} is not a modified line; filtered"

    def _is_line_modified(self, line_0based: int) -> bool:
        """Return whether the 0-based line number is modified."""
        return (line_0based + 1) in self._get_modified_line_numbers()

    def _unmatched_left_paren_in_line(self, line_idx: int) -> int:
        """
        Return the number of left parentheses not canceled by right parentheses
        on the same line. Only () is considered.
        A value > 0 means this line may start a multiline call.
        """
        if not (0 <= line_idx < len(self.main_file_lines)):
            return 0
        line = self.main_file_lines[line_idx]
        # Simplified handling: ignore strings/comments and count characters only.
        opens = line.count('(')
        closes = line.count(')')
        return max(0, opens - closes)

    def _find_closing_paren_line(self, start_idx: int, need_closes: int) -> Optional[int]:
        """
        Search downward from the line after start_idx until enough right parentheses
        are found. Return the 0-based line number containing the matching right
        parenthesis, or None if not found.
        Honors the neighborhood window upper bound when available.
        """
        if need_closes <= 0:
            return None
        # Prefer the window upper bound when available; otherwise use the full file.
        high = getattr(self, "_win_high", len(self.main_file_lines) - 1)

        remaining = need_closes
        i = start_idx + 1
        while i <= high and i < len(self.main_file_lines):
            line = self.main_file_lines[i]
            # This is a count-based approximation; it does not model ordering precisely.
            left = line.count('(')
            right = line.count(')')
            # Consume right parentheses first.
            if right >= remaining:
                return i
            remaining -= right
            # Extra left parentheses require more right parentheses.
            remaining += max(0, left - 0)
            i += 1
        return None


