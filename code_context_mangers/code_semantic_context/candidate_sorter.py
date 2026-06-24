# candidate_sorter.py
from dataclasses import dataclass
from typing import Iterable, List, Optional, Callable, Tuple
import os
import Levenshtein

@dataclass(frozen=True)
class Candidate:
    path: str
    line0: int
    char: int = 0
    rel: float = 0

class CandidateSorter:
    """
    Generic candidate sorter:
      - Same file: sort by |delta_line| * 100 + |delta_char| ascending,
        then stabilize with char_abs and line0.
      - Different files: sort by relative-position distance |rel_cand - rel_cur|,
        then by basename Levenshtein distance, then prefer later locations
        with larger line and character values.
    Note:
      - This uses precomputed relative positions. If those are unavailable,
        callers should provide the legacy fallback values.
    """
    def __init__(
        self,
        current_path: str,
        cur_line0: int,
        cur_char: int,
        cur_rel: float,
    ):
        self.current_path = os.path.normpath(current_path or "")
        self.cur_line0 = max(0, int(cur_line0 or 0))
        self.cur_char = max(0, int(cur_char or 0))
        self.cur_rel = float(cur_rel)
        self.cur_base = os.path.basename(self.current_path)


    def sort(self, candidates: Iterable[Candidate], top_k: Optional[int] = None) -> List[Candidate]:
        same_file_keys: List[Tuple[Tuple, Candidate]] = []
        other_file_keys: List[Tuple[Tuple, Candidate]] = []

        for c in candidates:
            norm_path = os.path.normpath(c.path or "")
            if norm_path == self.current_path:
                # Same file: shorter distance is better.
                line_abs = abs((c.line0 or 0) - self.cur_line0)
                char_abs = abs((c.char or 0) - self.cur_char)
                score = line_abs * 100 + char_abs
                key = (0, score, char_abs, c.line0, c.char)
                same_file_keys.append((key, c))
            else:
                # Different file: basename edit distance first, then relative-position distance.
                name_lev = Levenshtein.distance(os.path.basename(norm_path), self.cur_base)
                rel_diff = abs(float(c.rel) - self.cur_rel)
                other_file_keys.append(((1, name_lev, rel_diff, c.line0, c.char), c))

        same_file_keys.sort(key=lambda x: x[0])
        other_file_keys.sort(key=lambda x: x[0])

        ordered = [c for _, c in same_file_keys] + [c for _, c in other_file_keys]
        return ordered[:top_k] if (top_k is not None) else ordered
