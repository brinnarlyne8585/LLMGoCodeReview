"""
code_consistency_context_fetcher.py
------------------------------
Description / Purpose:
  Provides the core class `ConsistencyContextFetcher`, which finds similar code snippets
  in surrounding files, such as files from the same commit and directory, for a given
  review task. These snippets provide context for code-consistency review.

  1. Determine the target: analyze the review hunk, extract changed lines (+ or -),
     and identify their code-structure type (Function, Data, or Others).
  2. Determine the search scope: build the list of surrounding files to scan.
  3. Partition code blocks: split surrounding files into blocks by target type
     (Function/Data/Others). Large `Others` blocks are further split.
  4. Similarity ranking: use TF-IDF to score each code block against target changed lines.
  5. Format output: select the highest-scoring TOP-N blocks and return them as formatted context.

  The code is split into three modules:
  - code_partitioner.py (CodePartitionerMixin): file-partitioning logic.
  - block_ranker.py (BlockRankerMixin): scoring and ranking logic.
  - this file: main workflow orchestration.
"""
import json
import os
from typing import Set, List, Optional, Dict

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_orchestrator import ContextBlock
from code_context_mangers.code_context_utils import parse_hunk_header

from code_context_mangers.code_consistency_context.code_partitioner import CodePartitionerMixin
from code_context_mangers.code_consistency_context.block_ranker import BlockRankerMixin


class ConsistencyContextFetcher(CodePartitionerMixin, BlockRankerMixin):

    def __init__(self,
                 task: dict,
                 querier: UnifiedQuerier,
                 # Whether to filter candidates overlapping with selected Neighborhood/Semantic blocks before ranking.
                 skip_overlap_with_existing: bool = True,
                 # Already selected blocks, without distinguishing neighborhood and semantic sources.
                 existing_blocks: Optional[List[ContextBlock]] = None,
                 lsp_available: bool = True,
                 ):
        self.task = task
        self.querier = querier
        self.project_abs_path = querier.project_abs_path
        self.lsp_available = lsp_available
        self.file_path = self.task.get('path', '')
        self._file_partition_cache: Dict[str, List[Dict]] = {}  # File partition cache.
        self.tokenizer = self.subword_tokenizer

        self.skip_overlap_with_existing = skip_overlap_with_existing
        self.existing_blocks: List[ContextBlock] = list(existing_blocks or [])

    def fetch_blocks(self) -> tuple:
        """Main execution method. Fetch and format consistency context. Returns (blocks, debug_log)."""
        # 1. Analyze source-file changes and build a full information object.
        source_change_info = self._analyze_source_change()
        if not source_change_info:
            return [], {}

        # 2. Collect all candidate blocks.
        surrounding_files = self._get_surrounding_files()

        candidate_blocks = []
        for s_file in surrounding_files:
            # Partition surrounding files according to the source-change type.
            candidate_blocks.extend(self._partition_file(s_file))

        # 3. Clean all candidate block content.
        for block in candidate_blocks:
            block['meaningful_lines'] = [
                line for line in block['content_lines'] if self._is_meaningful_line(line)
            ]
        # Remove blocks that become empty after filtering.
        candidate_blocks = [block for block in candidate_blocks if block['meaningful_lines']]

        if not candidate_blocks:
            return [], {}

        # 4. Rank and filter using the full source-change information.
        top_ranked_blocks = self._rank_blocks(candidate_blocks, source_change_info)

        # 5. Build debug information.
        debug_log = {
            'selected_blocks': [
                {
                    'path': block['path'],
                    'start_line': block['start_line'],
                    'end_line': block['end_line'],
                    'score': block.get('score', 0),
                }
                for block in top_ranked_blocks
            ],
            'total_candidates': len(candidate_blocks),
            'config': {
                'partition_preset': getattr(self, 'ACTIVE_PARTITION_PRESET', 'unknown'),
                'selection_mode': getattr(self, 'SELECTION_LIMIT_MODE', 'unknown'),
                'top_n': getattr(self, 'TOP_N_BLOCKS', 0),
                'score_mode': getattr(self, 'SCORE_MODE', 'unknown'),
            }
        }

        # 6. Convert dictionaries into ContextBlock objects.
        final_blocks = []
        for block_dict in top_ranked_blocks:
            final_blocks.append(
                ContextBlock(
                    start_line=block_dict['start_line'],
                    end_line=block_dict['end_line'],
                    source='similar',
                    path=block_dict['path'],
                )
            )
        return final_blocks, debug_log

    def _analyze_source_change(self) -> Optional[Dict]:
        """
        Analyze the source-file change hunk and return a dictionary containing target lines and source block IDs.
        Target lines are all meaningful changed lines, no longer filtered by category.
        Source block IDs identify blocks that contain changed lines, so they can be excluded during ranking.
        """
        hunk = self.task.get('hunk_change', '')
        lines = hunk.splitlines()
        if not lines: return None

        # 1. Parse the hunk and get change details (line number <=> content).
        hdr = parse_hunk_header(lines[0])
        if not hdr: return None

        additions = []
        deletions = []
        current_new_line = hdr.c
        for line in lines[1:]:
            if line.startswith('+'):
                additions.append({'line_num': current_new_line,
                                  'content': line[1:],
                                  'original_line': line,
                                  })
                current_new_line += 1
            elif line.startswith('-'):
                deletions.append({'line_num': current_new_line,
                                  'content': line[1:],
                                  'original_line': line,
                                  })
            elif line.startswith(' '):
                current_new_line += 1

        change_details = additions if additions else deletions
        if not change_details: return None

        # 2. Get the source-file code block partition and identify source blocks that contain changed lines.
        source_file_blocks = self._get_or_partition_file(self.file_path)
        if not source_file_blocks:
            print(f"Warning: source file partition failed for {self.file_path}.")
            return None

        source_blocks = set()
        anchor_lines = {c['line_num'] for c in change_details}

        for block in source_file_blocks:
            for line_num in anchor_lines:
                if block['start_line'] <= line_num <= block['end_line']:
                    source_blocks.add(
                        (block['path'], block['start_line'], block['end_line'])
                    )
                    break

        # 3. Filter meaningful changed lines; no category-voting filter is used anymore.
        filtered_lines = [
            d for d in change_details if self._is_meaningful_line(d['content'])
        ]
        if not filtered_lines:
            return None

        # 4. Assemble and return.
        return {
            "lines": filtered_lines,
            "source_block_ids": source_blocks
        }

    def _get_surrounding_files(self) -> Set[str]:
        """Get the list of files from the same commit."""
        # 1. Files from the same commit
        try:
            commit_files_json = json.loads(self.task.get("files", "[]"))
            same_commit_files = {f.get("filename") for f in commit_files_json
                                 if (f.get("filename") and
                                     f.get("status") != "removed" and
                                     not (f.get("status") == "added" and f.get("additions", 1) == 0))}
        except (json.JSONDecodeError, TypeError):
            same_commit_files = set()

        # 2. Filter by the source file extension.
        try:
            # Get the source file extension, such as ".py" or ".java".
            _root, source_extension = os.path.splitext(self.file_path)
            # If the source file has no extension, such as "Makefile".
            if not source_extension:
                # Keep only files that also have no extension.
                filtered_files = {f for f in same_commit_files if not os.path.splitext(f)[1]}
            else:
                # Keep all files with matching extensions.
                filtered_files = {f for f in same_commit_files if os.path.splitext(f)[1] == source_extension}
        except Exception as e:
            # Safety fallback: if path handling fails, return the unfiltered file list.
            print(f"Warning: failed to filter surrounding files by extension: {e}. Returning the unfiltered file list.")
            return same_commit_files


        # 3. Return the final filtered set.
        return filtered_files
