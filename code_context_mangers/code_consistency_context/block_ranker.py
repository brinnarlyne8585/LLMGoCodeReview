"""
block_ranker.py
------------------------------
Description / Purpose:
  Provides BlockRankerMixin, which contains all logic for scoring and ranking
  candidate code blocks. This includes TF-IDF similarity calculation,
  target_avg block scoring, multi-level sorting, and deduplication.

  Extracted from ConsistencyContextFetcher and used as a mixin.
"""
import re
from typing import Set, List, Dict

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from code_context_mangers.code_context_orchestrator import ContextBlock


class BlockRankerMixin:
    """Methods for candidate-block scoring and ranking, mixed into ConsistencyContextFetcher."""

    # --- Scoring constants ---
    # Select the top blocks by count.
    TOP_N_BLOCKS = 5

    # Similarity threshold filter: keep blocks whose similarity score is >= this threshold.
    #   None   - no filtering; keep all blocks.
    #   0.001  - keep blocks with score >= 0.001.
    MIN_SIMILARITY_THRESHOLD = 0.001

    # Discount factor for comment-line weights in target_avg scoring.
    COMMENT_WEIGHT_DISCOUNT = 0.3

    # ========================= Utility methods =========================

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """Compute the Levenshtein distance, or edit distance, between two strings."""
        if len(s1) > len(s2):
            s1, s2 = s2, s1

        distances = range(len(s1) + 1)
        for i2, c2 in enumerate(s2):
            distances_ = [i2 + 1]
            for i1, c1 in enumerate(s1):
                if c1 == c2:
                    distances_.append(distances[i1])
                else:
                    distances_.append(1 + min((distances[i1], distances[i1 + 1], distances_[-1])))
            distances = distances_
        return distances[-1]

    @staticmethod
    def subword_tokenizer(text: str) -> List[str]:
        # 1. Extract continuous letter, digit, and underscore sequences.
        raw_tokens = re.findall(r'\w+', text)
        result = []
        for token in raw_tokens:
            # 2. Split by underscores.
            parts = token.split('_')
            for part in parts:
                if not part:
                    continue
                # 3. Split camel case at lowercase-to-uppercase boundaries, or before lowercase after consecutive uppercase letters.
                # Example: "SetDatabaseURL" -> ["Set", "Database", "URL"]
                #      "parseHTTPResponse" -> ["parse", "HTTP", "Response"]
                subwords = re.findall(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+', part)
                if subwords:
                    result.extend(w.lower() for w in subwords)
                else:
                    result.append(part.lower())
        return result

    def _get_comment_lines(self, file_path: str) -> Set[int]:
        """
        Scan the full file and return the set of comment line numbers, using 1-based indexing.
        Supports Go single-line // comments and /* */ block comments.
        """
        content = self.querier.file_provider.get_file_content(file_path)
        if not content:
            return set()

        comment_lines = set()
        in_block_comment = False

        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()

            if in_block_comment:
                # Currently inside a /* */ block comment.
                comment_lines.add(i)
                if '*/' in stripped:
                    in_block_comment = False
            else:
                if stripped.startswith('//'):
                    # Single-line comment.
                    comment_lines.add(i)
                elif stripped.startswith('/*'):
                    # Block comment starts.
                    comment_lines.add(i)
                    if '*/' not in stripped:
                        # Not closed on the same line; enter block-comment state.
                        in_block_comment = True

        return comment_lines

    def _is_meaningful_line(self, line: str) -> bool:
        """
        Determine whether a code line is meaningful for filtering.
        Rules: 1. non-empty and not whitespace-only. 2. at least two tokens after tokenization.
        """
        # Rule 1: filter empty or whitespace-only lines.
        if not line or not line.strip():
            return False

        # Rule 2: filter lines that contain only one token.
        tokens = self.tokenizer(line)
        if len(tokens) <= 1:
            return False

        return True

    def _build_tfidf_vectorizer(self, custom_stop_words):
        """Create a unified TfidfVectorizer instance so all modes use consistent parameters."""
        return TfidfVectorizer(
            analyzer='word',
            tokenizer=self.tokenizer,
            lowercase=False,
            min_df=2,
            ngram_range=(1, 3),
            stop_words=list(custom_stop_words)
        )

    # ========================= Scoring methods =========================

    def _compute_block_score(self, block_sims, tfidf_matrix=None, num_target=0,
                             comment_mask=None) -> float:
        """
        Compute the target_avg score for one candidate block.

        Args:
            block_sims: similarity submatrix with shape (num_target_lines, num_block_lines).
            tfidf_matrix: full TF-IDF matrix used to compute line weights.
            num_target: number of target lines; the first num_target rows in tfidf_matrix are target lines.
            comment_mask: boolean array of length num_target; True means the target line is a comment.

        Returns:
            Block score as a float.
        """
        if block_sims.size == 0:
            return 0.0

        # For each target line, find the most similar candidate line inside the block.
        target_best = block_sims.max(axis=1)  # shape: (num_target,)

        # Use the L2 norm of TF-IDF vectors as weights:
        # lines with more information, such as rare terms, have higher norms and larger weights.
        if tfidf_matrix is not None and num_target > 0:
            from scipy.sparse import issparse
            target_vectors = tfidf_matrix[:num_target]
            if issparse(target_vectors):
                # Sparse matrix: compute the L2 norm row by row.
                norms = np.array(target_vectors.power(2).sum(axis=1)).flatten()
                norms = np.sqrt(norms)
            else:
                norms = np.linalg.norm(target_vectors, axis=1)

            # Discount comment-line norms to reduce their impact in the weighted average.
            if comment_mask is not None:
                for i, is_comment in enumerate(comment_mask):
                    if is_comment:
                        norms[i] *= self.COMMENT_WEIGHT_DISCOUNT

            total = norms.sum()
            if total > 0:
                weights = norms / total
                return float(np.dot(target_best, weights))

        # Fallback: no weights, use a direct average.
        return float(target_best.mean())

    # ========================= Main score calculation =========================

    def _score_line_level(self, filtered_candidates, target_lines, target_lines_dicts,
                          custom_stop_words, comment_mask, num_candidate_blocks):
        """
        Build TF-IDF line by line and compute the target_avg main score for each block.
        Mutates the 'score' field of each block in filtered_candidates.
        Returns True on success, or False when scoring cannot be computed.
        """
        candidate_lines = [line for block in filtered_candidates for line in block['meaningful_lines']]
        if not candidate_lines:
            return False

        # Build the corpus.
        corpus = target_lines + candidate_lines
        try:
            vectorizer = self._build_tfidf_vectorizer(custom_stop_words)
            tfidf_matrix = vectorizer.fit_transform(corpus)
        except ValueError:
            return False

        # Compute the similarity matrix.
        num_target = len(target_lines)
        if tfidf_matrix.shape[1] == 0:
            return False
        sim_matrix = cosine_similarity(tfidf_matrix[:num_target], tfidf_matrix[num_target:])

        # Compute the main score for each block.
        line_offset = 0
        for block in filtered_candidates:
            num_block_lines = len(block['meaningful_lines'])
            if num_block_lines == 0:
                block['score'] = 0
            else:
                block_sims = sim_matrix[:, line_offset: line_offset + num_block_lines]
                block['score'] = self._compute_block_score(block_sims, tfidf_matrix, num_target, comment_mask)

            line_offset += num_block_lines

        return True

    # ========================= Shared: secondary metrics + sorting + deduplication =========================
    def _compute_secondary_metrics(self, filtered_candidates, source_block_ids):
        """Compute secondary sorting metrics for each candidate block: edit distance and code proximity."""
        source_reference_line = min(b[1] for b in source_block_ids) if source_block_ids else 0
        for block in filtered_candidates:
            block['edit_distance'] = self._levenshtein_distance(self.file_path, block['path'])
            block['proximity'] = abs(block['start_line'] - source_reference_line)

    def _sort_and_select_top_blocks(self, filtered_candidates) -> List[Dict]:
        """Apply multi-level sorting, content deduplication, and Top-N selection."""
        # Apply similarity threshold filtering.
        if self.MIN_SIMILARITY_THRESHOLD is not None:
            filtered_candidates = [b for b in filtered_candidates
                                  if b.get('score', 0) >= self.MIN_SIMILARITY_THRESHOLD]
            if not filtered_candidates:
                return []

        sorted_blocks = sorted(
            filtered_candidates,
            key=lambda b: (-b['score'], b['edit_distance'], b['proximity'], b['path'])
        )

        top_blocks = []
        seen_content = set()

        for block in sorted_blocks:
            if len(top_blocks) >= self.TOP_N_BLOCKS:
                break

            content_tuple = tuple(block['content_lines'])
            if content_tuple not in seen_content:
                top_blocks.append(block)
                seen_content.add(content_tuple)

        return top_blocks


    # ========================= Main entry: _rank_blocks =========================
    def _rank_blocks(self, candidate_blocks: List[Dict], source_change_info: Dict) -> List[Dict]:
        """
        Rank code blocks by TF-IDF similarity.

        Flow:
        1. Extract target-line information.
        2. Filter candidate blocks, excluding the source block and blocks overlapping with already selected blocks.
        3. Compute the target_avg main score.
        4. Compute secondary sorting metrics, including edit distance and proximity.
        5. Apply multi-level sorting, deduplication, and Top-N selection.
        """
        target_lines_dicts = source_change_info['lines']
        target_lines = [d['content'] for d in target_lines_dicts]

        # Compute the target-line comment mask for weight discounting.
        comment_line_set = self._get_comment_lines(self.file_path)
        comment_mask = [d['line_num'] in comment_line_set for d in target_lines_dicts]
        if not candidate_blocks or not target_lines:
            return []

        # --- Stop words. Currently empty, kept as an extension point. ---
        custom_stop_words = set()

        # --- Filter candidate blocks ---
        # 1. Filter blocks containing extremely long single lines (>1000 chars).
        # These are usually generated content or data blobs, not normal code.
        # This follows common heuristic filtering rules from CodeLLM data cleaning.
        MAX_LINE_LENGTH_THRESHOLD = 1000
        source_block_ids = source_change_info.get("source_block_ids", set())
        filtered_candidates = []
        for b in candidate_blocks:
            # Exclude the block that contains the original change.
            if (b['path'], b['start_line'], b['end_line']) in source_block_ids:
                continue
            # Reject abnormal blocks with an extremely long single line (>1000 chars).
            if any(len(line) > MAX_LINE_LENGTH_THRESHOLD for line in b['content_lines']):
                continue
            filtered_candidates.append(b)

        if not filtered_candidates:
            return []

        # Exclude candidates that overlap with already selected blocks in the same file and line range.
        if getattr(self, "skip_overlap_with_existing", False) and getattr(self, "existing_blocks", None):
            def _cand_overlaps_existing(cand: Dict) -> bool:
                cand_blk = ContextBlock(
                    start_line=cand['start_line'],
                    end_line=cand['end_line'],
                    source='candidate',
                    path=cand.get('path', '')
                )
                for blk in self.existing_blocks:
                    if blk and cand_blk.overlaps(blk):
                        return True
                return False
            filtered_candidates = [b for b in filtered_candidates if not _cand_overlaps_existing(b)]
            if not filtered_candidates:
                return []

        # --- Compute main score ---
        success = self._score_line_level(
            filtered_candidates, target_lines, target_lines_dicts,
            custom_stop_words, comment_mask, len(candidate_blocks)
        )

        if not success:
            return []

        # --- Compute secondary sorting metrics ---
        self._compute_secondary_metrics(filtered_candidates, source_block_ids)

        return self._sort_and_select_top_blocks(filtered_candidates)
