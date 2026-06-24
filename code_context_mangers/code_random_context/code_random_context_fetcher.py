"""
code_random_context_fetcher.py
------------------------------
Random context baseline:
1. Collect files from the same commit (consistent with existing logic in similar context).
2. Keep only non-removed Go files.
3. Partition each file into fixed 20-line windows.
4. Remove the overlap between blocks and the current review hunk range.
5. Randomly sample up to 8 remaining blocks with a deterministic per-case seed.
"""
from __future__ import annotations

import hashlib
import json
import random
from typing import Dict, List, Optional, Set, Tuple

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_orchestrator import ContextBlock
from code_context_mangers.code_context_utils import parse_hunk_header


class RandomContextFetcher:
    WINDOW_SIZE = 20
    TOP_K_BLOCKS = 8
    GLOBAL_SEED = 42

    def __init__(self,
                 task: dict,
                 querier: UnifiedQuerier):
        self.task = task
        self.querier = querier
        self.file_path = task.get("path", "")
        self.hunk_range = self._parse_target_hunk_range()

    def fetch_blocks(self) -> tuple[list[ContextBlock], dict]:
        candidate_files = self._get_same_commit_go_files()
        candidate_blocks: List[ContextBlock] = []

        for path in candidate_files:
            candidate_blocks.extend(self._build_blocks_for_file(path))

        none_count = sum(block is None for block in candidate_blocks)
        if none_count:
            raise ValueError(
                f"RandomContextFetcher produced {none_count} None blocks "
                f"for comment_url={self.task.get('comment_url', '')}"
            )

        if not candidate_blocks:
            return [], {
                "same_commit_go_files": len(candidate_files),
                "candidate_blocks_before_sampling": 0,
                "selected_blocks": [],
            }

        rng = random.Random(self._case_seed())
        sample_size = min(self.TOP_K_BLOCKS, len(candidate_blocks))
        selected_blocks = rng.sample(candidate_blocks, sample_size)

        debug_log = {
            "same_commit_go_files": len(candidate_files),
            "candidate_blocks_before_sampling": len(candidate_blocks),
            "selected_blocks": [
                {
                    "path": block.path,
                    "start_line": block.start_line,
                    "end_line": block.end_line,
                }
                for block in selected_blocks
            ],
            "seed": self._case_seed(),
        }
        return selected_blocks, debug_log

    def _case_seed(self) -> int:
        comment_url = self.task.get("comment_url", "")
        digest = hashlib.md5(comment_url.encode("utf-8")).hexdigest()
        return self.GLOBAL_SEED + int(digest[:8], 16)

    def _parse_target_hunk_range(self) -> Optional[Tuple[int, int]]:
        hunk = self.task.get("hunk_change", "")
        lines = hunk.splitlines()
        if not lines:
            return None
        header = parse_hunk_header(lines[0], anchored=True)
        if not header:
            return None
        start_line = header.c
        line_count = max(header.dc, 1)
        end_line = start_line + line_count - 1
        return start_line, end_line

    def _get_same_commit_go_files(self) -> Set[str]:
        try:
            commit_files_json = json.loads(self.task.get("files", "[]"))
            same_commit_files = {
                f.get("filename") for f in commit_files_json
                if (
                    f.get("filename")
                    and f.get("status") != "removed"
                    and not (f.get("status") == "added" and f.get("additions", 1) == 0)
                    and str(f.get("filename")).endswith(".go")
                )
            }
        except (json.JSONDecodeError, TypeError):
            same_commit_files = set()
        return {path for path in same_commit_files if path}

    def _build_blocks_for_file(self, path: str) -> List[ContextBlock]:
        file_lines = self.querier.file_provider.get_lines(path)
        if not file_lines:
            return []

        total_lines = len(file_lines)
        blocks: List[ContextBlock] = []
        for start_line in range(1, total_lines + 1, self.WINDOW_SIZE):
            end_line = min(start_line + self.WINDOW_SIZE - 1, total_lines)
            blocks.extend(self._subtract_hunk_overlap(path, start_line, end_line))
        return blocks

    def _subtract_hunk_overlap(self, path: str, start_line: int, end_line: int) -> List[ContextBlock]:
        if path != self.file_path or self.hunk_range is None:
            return [self._make_block(path, start_line, end_line)]

        hunk_start, hunk_end = self.hunk_range
        overlap_start = max(start_line, hunk_start)
        overlap_end = min(end_line, hunk_end)

        if overlap_start > overlap_end:
            return [self._make_block(path, start_line, end_line)]

        blocks: List[ContextBlock] = []
        if start_line < overlap_start:
            blocks.append(self._make_block(path, start_line, overlap_start - 1))
        if overlap_end < end_line:
            blocks.append(self._make_block(path, overlap_end + 1, end_line))
        return blocks

    @staticmethod
    def _make_block(path: str, start_line: int, end_line: int) -> ContextBlock:
        return ContextBlock(
            start_line=start_line,
            end_line=end_line,
            source="random",
            path=path,
        )
