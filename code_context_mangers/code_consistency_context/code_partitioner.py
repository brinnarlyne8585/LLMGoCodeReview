"""
code_partitioner.py
------------------------------
Description / Purpose:
  Provides CodePartitionerMixin, which contains the logic for splitting source files
  into code blocks according to LSP symbols. This includes LSP-based symbol partitioning,
  merging small OTHERS blocks, and DP-based optimal segmentation.

  Extracted from ConsistencyContextFetcher and used as a mixin.
"""
import os
from typing import List, Dict
from collections import defaultdict

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_utils import ALL_RECOGNIZED_KINDS, FUNCTION_LEVEL_KINDS
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag


"""
Partition parameter presets for one-step switching.
"""
PARTITION_PRESETS = {

    "large": {          # Coarse granularity: about 50 lines per block.
        "CHUNK_TARGET": 50,
        "MIN_CHUNK": 20,
        "MAX_CHUNK": 80,
        "MERGE_THRESHOLD": 20,
        "CUT_PENALTY": 200,
    },

    "medium": {         # Medium granularity: about 20 lines per block.
        "CHUNK_TARGET": 20,
        "MIN_CHUNK": 10,
        "MAX_CHUNK": 30,
        "MERGE_THRESHOLD": 10,
        "CUT_PENALTY": 50,
    },

    "small": {          # Fine granularity: about 10 lines per block.
        "CHUNK_TARGET": 10,
        "MIN_CHUNK": 5,
        "MAX_CHUNK": 15,
        "MERGE_THRESHOLD": 5,
        "CUT_PENALTY": 10,
    },
}

# Switch the active preset here.
ACTIVE_PARTITION_PRESET = "medium"


class CodePartitionerMixin:
    """File partitioning methods mixed into ConsistencyContextFetcher."""

    # --- Load partition parameters from the preset ---
    _preset = PARTITION_PRESETS[ACTIVE_PARTITION_PRESET]
    CHUNK_TARGET = _preset["CHUNK_TARGET"]       # Ideal block size in lines.
    MIN_CHUNK = _preset["MIN_CHUNK"]             # Minimum block lines, as a hard DP constraint.
    MAX_CHUNK = _preset["MAX_CHUNK"]             # Maximum block lines, as a hard DP constraint.
    MERGE_THRESHOLD = _preset["MERGE_THRESHOLD"] # Merge threshold for small OTHERS blocks, in total lines.
    CUT_PENALTY = _preset["CUT_PENALTY"]         # Extra cost for cutting at non-empty lines.

    def _create_block(self, path: str, lines: List[str], start: int, end: int) -> Dict:
        """Create a code block dictionary."""
        return {
            "path": path,
            "start_line": start,
            "end_line": end,
            "content_lines": lines[start - 1: end]
        }

    def _create_other_blocks(self, path: str, lines: List[str], start: int, end: int) -> List[Dict]:
        """Create an OTHERS block without splitting; DP handles splitting uniformly."""
        if end - start + 1 <= 0:
            return []
        block = self._create_block(path, lines, start, end)
        block['category'] = 'OTHERS'
        return [block]

    def _partition_file(self, file_path: str) -> List[Dict]:
        """
        Split file content into code blocks, returning all blocks without type-based filtering.
        """
        return self._get_or_partition_file(file_path)

    def _get_or_partition_file(self, file_path: str) -> List[Dict]:
        """
        Unified entry for retrieving file partitions, with caching.
        Return the cached partition when available; otherwise compute, cache, and return it.
        """
        if file_path in self._file_partition_cache:
            return self._file_partition_cache[file_path]

        # First try LSP analysis according to the current environment.
        content = self.querier.file_provider.get_file_content(file_path)
        if not content:
            print(
                f"File {file_path} in project {self.project_abs_path} "
                f"does not exist at commit {self.task.get('original_commit_id')}; "
                f"_get_or_partition_file() returns an empty partition.")
            return []

        # Decide whether to force cache-only queries:
        #    - when requested by the caller (is_always_query_with_cache = True)
        #    - or when LSP is unavailable (self.lsp_available = False), leaving cache as the only option.
        force_cache_only = is_always_query_with_cache or not self.lsp_available
        sym_resp = self.querier.query_by_cache_flag(
            file_path=file_path,
            command="get_document_symbol",
            params={},
            is_always_query_with_cache=force_cache_only,
        )

        # LSP analysis succeeded:
        if not self.querier.is_null_or_empty_result(sym_resp):
            symbols = sym_resp.get('result')
            # Call the core partitioning logic.
            partition = self._create_file_partition(file_path, content.splitlines(), symbols)
            self._file_partition_cache[file_path] = partition
            return partition

        # LSP analysis failed:
        else:
            print(f"LSP failed; falling back to degraded partitioning: project {self.project_abs_path}, file {file_path}, commit {self.task.get('original_commit_id')[8:]}")
            content = self.querier.file_provider.get_file_content(file_path)
            if not content:
                print(f"File {file_path} in project {self.project_abs_path} "
                      f"does not exist or is empty at commit {self.task.get('original_commit_id')[8:]}; "
                      f"_get_or_partition_file() returns an empty partition.")
                return []

            # Treat the whole file as an OTHERS block, then split it with DP.
            lines = content.splitlines()
            raw_blocks = self._create_other_blocks(file_path, lines, 1, len(lines))
            partition = []
            for b in raw_blocks:
                partition.extend(self._dp_split_block(b, lines))
            self._file_partition_cache[file_path] = partition
            return partition

    def _create_file_partition(self, file_path: str, lines: List[str], symbols: List[Dict]) -> List[Dict]:
        """
        Core partitioning logic: handle nested symbols and split the whole file into non-overlapping blocks.
        Rule: a code block's category is determined by its outermost container.
        """
        if not symbols:
            # If there is no symbol information, the whole file is an OTHERS block and is split by DP.
            raw_blocks = self._create_other_blocks(file_path, lines, 1, len(lines))
            result = []
            for b in raw_blocks:
                result.extend(self._dp_split_block(b, lines))
            return result

        # 1. Identify all top-level symbols.
        top_level_symbols = []
        recognized_symbols = [s for s in symbols if s['kind'] in ALL_RECOGNIZED_KINDS]

        for s1 in recognized_symbols:
            is_nested = False
            for s2 in recognized_symbols:
                if s1 is s2:
                    continue
                # If s1 is contained by s2 and s2 is not exactly s1, then s1 is nested.
                if s2['start_line'] <= s1['start_line'] and s1['end_line'] <= s2['end_line'] and s1 != s2:
                    is_nested = True
                    break
            if not is_nested:
                top_level_symbols.append(s1)

        # 2. Create FUNCTION and DATA blocks from top-level symbols.
        sorted_top_symbols = sorted(top_level_symbols, key=lambda x: x['start_line'])
        blocks = []
        last_line = 0

        for s in sorted_top_symbols:
            # 2a. Add the OTHERS block before the symbol, i.e. a gap.
            if s['start_line'] > last_line + 1:
                blocks.extend(self._create_other_blocks(file_path, lines, last_line + 1, s['start_line'] - 1))

            # 2b. Add the block for the symbol itself.
            category = "FUNCTION" if s['kind'] in FUNCTION_LEVEL_KINDS else "STRUCT"
            block = self._create_block(file_path, lines, s['start_line'], s['end_line'])
            block['category'] = category
            blocks.append(block)

            last_line = s['end_line']

        # 3. Add the trailing OTHERS block at the end of the file.
        if len(lines) > last_line:
            blocks.extend(self._create_other_blocks(file_path, lines, last_line + 1, len(lines)))

        # 4. Run post-processing to merge small OTHERS blocks.
        merged_blocks = self._merge_small_other_blocks(blocks, lines)

        # 5. Run DP-based optimal segmentation on large blocks.
        final_blocks = []
        for block in merged_blocks:
            final_blocks.extend(self._dp_split_block(block, lines))

        return final_blocks

    def _merge_small_other_blocks(self, blocks: List[Dict], file_lines: List[str]) -> List[Dict]:
        """
        Post-process partitions by merging small OTHERS blocks into nearby non-OTHERS semantic blocks by content proximity.
        """
        if len(blocks) < 2:
            return blocks

        # --- Pass 1: mark merge decisions ---
        for i, block in enumerate(blocks):
            # Use total line count to decide whether this is a small block.
            block_total_lines = block['end_line'] - block['start_line'] + 1
            if block.get('category') != 'OTHERS' or block_total_lines > self.MERGE_THRESHOLD:
                continue

            content_lines_with_indices = [
                (idx + block['start_line'], line)
                for idx, line in enumerate(block.get('content_lines', [])) if line.strip()
            ]

            # --- Find the nearest semantic block before and after this block, meaning any non-OTHERS type. ---
            prev_semantic_idx, next_semantic_idx = -1, -1
            for j in range(i - 1, -1, -1):
                if blocks[j].get('category') != 'OTHERS':  # No longer hard-code 'FUNCTION'.
                    prev_semantic_idx = j
                    break
            for j in range(i + 1, len(blocks)):
                if blocks[j].get('category') != 'OTHERS':  # No longer hard-code 'FUNCTION'.
                    next_semantic_idx = j
                    break

            if prev_semantic_idx == -1 and next_semantic_idx == -1:
                continue

            dist_up, dist_down = float('inf'), float('inf')
            if content_lines_with_indices:
                content_start_line = content_lines_with_indices[0][0]
                content_end_line = content_lines_with_indices[-1][0]
                if prev_semantic_idx != -1:
                    dist_up = content_start_line - blocks[prev_semantic_idx]['end_line']
                if next_semantic_idx != -1:
                    dist_down = blocks[next_semantic_idx]['start_line'] - content_end_line
            else:
                if prev_semantic_idx != -1:
                    dist_up = block['start_line'] - blocks[prev_semantic_idx]['end_line']
                if next_semantic_idx != -1:
                    dist_down = blocks[next_semantic_idx]['start_line'] - block['end_line']

            if dist_up < dist_down:
                if prev_semantic_idx != -1: block['merge_target'] = prev_semantic_idx
            elif next_semantic_idx != -1:
                block['merge_target'] = next_semantic_idx
            elif prev_semantic_idx != -1:
                block['merge_target'] = prev_semantic_idx

        # --- Pass 2: execute merges ---
        merges_into_target = defaultdict(list)
        indices_to_merge_away = set()
        for i, block in enumerate(blocks):
            if 'merge_target' in block:
                target_idx = block['merge_target']
                merges_into_target[target_idx].append(i)
                indices_to_merge_away.add(i)

        if not indices_to_merge_away:
            return blocks

        final_blocks = []
        for i, block in enumerate(blocks):
            if i in indices_to_merge_away:
                continue

            if i in merges_into_target:
                blocks_to_merge_indices = [i] + merges_into_target[i]
                new_start_line = min(blocks[j]['start_line'] for j in blocks_to_merge_indices)
                new_end_line = max(blocks[j]['end_line'] for j in blocks_to_merge_indices)
                new_block = self._create_block(block['path'], file_lines, new_start_line, new_end_line)

                # --- Inherit the parent block category ---
                new_block['category'] = block['category']
                final_blocks.append(new_block)
            else:
                final_blocks.append(block)

        return final_blocks

    def _dp_split_block(self, block: Dict, file_lines: List[str]) -> List[Dict]:
        """
        Split a large code block with DP-based optimal segmentation.
        If the block has <= CHUNK_TARGET lines, return [block] without splitting.
        Cost function = (block size - target size)^2 + non-empty-line cut penalty.
        """
        start = block['start_line']
        end = block['end_line']
        total = end - start + 1

        # No splitting needed.
        if total <= self.CHUNK_TARGET:
            return [block]

        # Collect blank line positions with 1-based indexing.
        blank_lines = set()
        for i in range(start, end + 1):
            if i - 1 < len(file_lines) and file_lines[i - 1].strip() == '':
                blank_lines.add(i)

        INF = float('inf')

        # dp[i] = minimum total cost from line i to end.
        # choice[i] = optimal next cut point j corresponding to dp[i].
        dp = {}
        choice = {}
        dp[end + 1] = 0  # Terminal state: cost is 0.

        # Dynamic programming from back to front.
        for i in range(end, start - 1, -1):
            best_cost = INF
            best_j = -1

            # Enumerate the next cut point j; the current block is [i, j-1] with size j - i.
            j_min = i + self.MIN_CHUNK
            j_max = min(i + self.MAX_CHUNK, end + 1)  # j can be at most end+1, meaning the final block.

            for j in range(j_min, j_max + 1):
                chunk_size = j - i

                # Size penalty: deviation from the ideal size.
                size_penalty = (chunk_size - self.CHUNK_TARGET) ** 2

                # Cut-position penalty: no penalty at the end or at blank lines.
                if j == end + 1 or j in blank_lines:
                    cut_penalty = 0
                else:
                    cut_penalty = self.CUT_PENALTY

                j_cost = dp.get(j, INF)
                if j_cost == INF:
                    continue

                total_cost = size_penalty + cut_penalty + j_cost
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_j = j

            dp[i] = best_cost
            choice[i] = best_j

        # If no legal split can be found in extreme cases, return the original block.
        if dp.get(start, INF) == INF:
            return [block]

        # Backtrack to produce split results.
        sub_blocks = []
        pos = start
        while pos <= end:
            nxt = choice.get(pos, end + 1)
            sub_end = min(nxt - 1, end)
            sub_block = self._create_block(block['path'], file_lines, pos, sub_end)
            sub_block['category'] = block['category']  # Inherit the original block category.
            sub_blocks.append(sub_block)
            pos = nxt

        return sub_blocks
