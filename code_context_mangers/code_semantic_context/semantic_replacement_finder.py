import os
from typing import List, Optional, Tuple

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_semantic_context.candidate_sorter import CandidateSorter, Candidate
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag


class ReplacementFinder:

    def __init__(self,
                 querier: UnifiedQuerier,
                 task: dict,
                 path: str,
                 lsp_available: bool = True,):
        self.querier = querier
        self.base_uri_prefix = f"file://{self.querier.project_abs_path}/"
        self.lsp_available = lsp_available
        self.task = task
        self.path = path  # Path of the reviewed file.
        self.primary_lang_id = querier.main_lang_id

    def find_suitable_replacement(self, cmd) -> Tuple[Optional[str], Optional[int], Optional[int]]:
        """
        Fallback strategy:
        for Python/Ruby, use repo search -> sorting -> document_symbol validation;
        for other languages, use workspace_symbol -> sorting.
        Returns (new_path, new_line_0based, new_char), or (None, None, None) if no replacement is found.
        """
        query = (cmd.target_symbol or "").strip()
        if not query:
            return (None, None, None)

        # --- Python / Ruby: use the search path ---
        if (self.primary_lang_id or "").lower() in {"python", "ruby"}:
            new_path, new_line, new_char = self.find_suitable_replacement_with_search(cmd,query)
        else:
            new_path, new_line, new_char = self.find_suitable_replacement_with_symbol(cmd,query)
        return new_path, new_line, new_char

    # ===== Python/Ruby: search -> sorting -> document_symbol validation =====
    def find_suitable_replacement_with_search(self,cmd,query):
        # 1) Search the symbol across the repository with Git plain-text search.
        search_resp = self.querier.search_text_in_commit(
            query=query
        )

        # 2) Normalize candidates into (path, line_0based, char).
        cands = self._normalize_candidates_from_search(search_resp, query)
        if not cands:
            return (None, None, None)

        # 3) Sort candidates: same file first, then nearest location; across files,
        #    prefer closer basename edit distance and then later locations.
        ordered = self._order_candidates(
            cands,
            current_path=self.path,
            cur_line_0based=cmd.line_num,
            cur_char=cmd.cursor
        )

        # 4) Validate each candidate with document_symbol: start line matches and symbol name is exact.
        _root, source_extension = os.path.splitext(self.path)
        for path, line0, ch in ordered:
            if os.path.splitext(path)[1]!=source_extension:
                continue;
            # Force cache-only mode when requested by the caller or when LSP is unavailable.
            force_cache_only = is_always_query_with_cache or not self.lsp_available
            is_validate = self.querier.validate_symbol_at_line(
                file_path = path,
                target_name = query,
                start_line0=line0,
                is_always_query_with_cache = force_cache_only,
            )
            if is_validate:
                return (path, line0, ch)

        # No candidate passed validation.
        return (None, None, None)

    def find_suitable_replacement_with_symbol(self, cmd, query):
        # Force cache-only mode when requested by the caller or when LSP is unavailable.
        force_cache_only = is_always_query_with_cache or not self.lsp_available
        ws_resp = self.querier.query_by_cache_flag(
            file_path = self.path,
            command = "get_workspace_symbol",
            params = {"query": query},
            is_always_query_with_cache = force_cache_only,
        )

        if self.querier.is_null_or_empty_result(ws_resp):
            return (None, None, None)

        # 1) Normalize into (path, line_0based, char).
        cands = self._normalize_candidates_from_workspace_symbol(ws_resp, query=query)
        if not cands:
            return (None, None, None)

        # 2) Use generic sorting: same file and closer distance first; across files,
        #    use basename edit distance and later locations.
        ordered = self._order_candidates(
            cands,
            current_path=self.path,
            cur_line_0based=cmd.line_num,
            cur_char=cmd.cursor
        )

        # 3) Take the first sorted candidate; workspace_symbol is already a symbol-level hit.
        path, line0, ch = ordered[0]
        return (path, line0, ch)

    def _order_candidates(
            self,
            candidates: List[Tuple[str, int, int]],
            current_path: str,
            cur_line_0based: int,
            cur_char: int,
    ) -> List[Tuple[str, int, int]]:
        """
        Use CandidateSorter consistently:
        - Same file: |delta_line| * 100 + |delta_char| ascending.
        - Different files: basename edit distance ascending, then relative-position distance ascending.
        """

        # Compute the relative position in the current file.
        cur_total = len(self.querier.file_provider.get_lines(current_path))
        cur_rel = float(cur_line_0based+1) / cur_total

        # Compute each candidate's relative position and build Candidate objects.
        cand_objs = []
        for (p, l0, ch) in candidates:
            tot = len(self.querier.file_provider.get_lines(p))
            rel = float(l0+1) / tot
            cand_objs.append(Candidate(path=p, line0=l0, char=ch, rel=rel))

        sorter = CandidateSorter(
            current_path=current_path,
            cur_line0=cur_line_0based,
            cur_char=cur_char,
            cur_rel=cur_rel,
        )
        ordered_objs = sorter.sort(cand_objs, top_k=None)
        return [(c.path, c.line0, c.char) for c in ordered_objs]

    # --------- Normalize search results into a list of (path, line_0based, char) ---------
    def _normalize_candidates_from_search(
            self,
            search_resp,
            query: Optional[str] = None
    ) -> List[Tuple[str, int, int]]:
        """
        Normalize search_text_in_commit results into a list of (path, line_0based, char).
        Logic:
          - use file_path
          - convert 1-based line_number to 0-based
          - if query is provided, use its first occurrence in line_content as char; otherwise use 0
        """

        items = ((search_resp or {}).get("result") or [])
        out: List[Tuple[str, int, int]] = []

        for it in items:
            path = it.get("file_path")
            if not path:
                continue

            line1 = it.get("line_number")
            try:
                line0 = int(line1) - 1
            except Exception:
                continue

            ch = 0
            if query and isinstance(it.get("line_content"), str):
                idx = it["line_content"].find(query)
                if idx >= 0:
                    ch = idx

            out.append((path, line0, ch))

        return out

    def _normalize_candidates_from_workspace_symbol(
            self,
            ws_resp: dict,
            query: Optional[str] = None
    ) -> List[Tuple[str, int, int]]:
        """
        Normalize get_workspace_symbol results into a list of (path, line_0based, char).
        """
        items = (ws_resp or {}).get("result") or []
        out: List[Tuple[str, int, int]] = []

        for it in items:

            if not isinstance(it, dict):
                continue

            name = it.get("name") or ""
            if query and not (name == query or name.endswith("." + query)):
                continue

            loc = it.get("location")
            if not isinstance(loc, dict):
                # WorkspaceSymbol location is optional; without symbol/resolve, skip entries that lack it.
                continue

            uri = loc.get("uri")
            rng = loc.get("range")
            if not (isinstance(uri, str) and isinstance(rng, dict)):
                continue

            # Only process files inside the project.
            if self.base_uri_prefix not in uri:
                continue

            start = rng.get("start") or {}
            line0 = start.get("line")
            char = start.get("character")
            if not (isinstance(line0, int) and isinstance(char, int)):
                continue

            path = self._uri_to_path(uri)
            out.append((path, line0, char))

        return out

    # --------- Validate whether a candidate really is the target symbol with document_symbol ----------
    def _validate_candidate_with_document_symbol(self, file_path: str, target_name: str, start_line0: int) -> bool:
        """
        Call LSP get_document_symbol without a cursor.
        After flattening, look for name == target_name and range.start.line == start_line0.
        """

        # Force cache-only mode when requested by the caller or when LSP is unavailable.
        force_cache_only = is_always_query_with_cache or not self.lsp_available
        resp = self.querier.query_by_cache_flag(
            file_path = file_path,
            command = "get_document_symbol",
            params={},
            is_always_query_with_cache = force_cache_only,
        )

        if self.querier.is_null_or_empty_result(resp):
            return False

        symbols = resp.get("result") or []

        target_line1 = int(start_line0 or 0) + 1  # Convert to 1-based for comparison.
        for s in symbols:
            name = s.get("name")
            start_line1 = s.get("start_line")
            if name == target_name and isinstance(start_line1, int) and start_line1 == target_line1:
                return True

        return False

    @staticmethod
    def _uri_to_path(uri: Optional[str]) -> str:
        if isinstance(uri, str) and uri.startswith("file://"):
            return uri[7:]
        return uri or ""
