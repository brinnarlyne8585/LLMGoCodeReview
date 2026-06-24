# context_orchestrator.py
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from threading import RLock

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_utils import parse_hunk_header, parse_hunk_and_find_anchors


@dataclass
class ContextBlock:
    """Standard code-block data structure with metadata."""
    start_line: int = -1
    end_line: int = -1
    source: str = ""  # Source: 'neighborhood', 'semantic', 'similar'

    is_virtual: bool = False

    description_above_path: str = ""
    description_under_path: str = ""  #

    path: str = ""
    project_path: str = ""
    commit_sha: str = ""

    content_lines: List[str] = field(default_factory=list)

    anchor_line: Optional[int] = None

    def overlaps(self, other: 'ContextBlock') -> bool:
        """Check whether two blocks overlap in the same file."""
        if not self.path or not other.path:
            return False
        if self.path != other.path:
            return False
        return max(self.start_line, other.start_line) <= min(self.end_line, other.end_line)

    def expand_to_include(self, other: 'ContextBlock'):
        """Expand this block so it fully contains another block."""
        self.start_line = min(self.start_line, other.start_line)
        self.end_line = max(self.end_line, other.end_line)


class ContextOrchestrator:
    """
    Orchestrates structured blocks from fetchers, deduplicates them, and renders output.
    """

    CONTEXT_SOURCES = ("neighborhood", "semantic", "similar", "random")

    @classmethod
    def column_name(cls, source: str) -> str:
        """Map a source name to its standard column name, e.g. 'neighborhood_context'."""
        return f"{source}_context"

    @classmethod
    def all_column_names(cls) -> List[str]:
        """All standard column names in fixed order."""
        return [cls.column_name(s) for s in cls.CONTEXT_SOURCES]

    @classmethod
    def active_column_names(cls, config) -> List[str]:
        """
        Return enabled *_context column names from ContextConfig.
        Switch fields are expected to be named use_<source>_context.
        """
        names: List[str] = []
        for s in cls.CONTEXT_SOURCES:
            if getattr(config, f"use_{s}_context", False):
                names.append(cls.column_name(s))
        return names

    @classmethod
    def empty_contexts(cls) -> Dict[str, str]:
        """Build an empty dictionary with all context columns."""
        return {cls.column_name(s): "" for s in cls.CONTEXT_SOURCES}

    def __init__(self,
                 task: dict,
                 querier: UnifiedQuerier):
        self.task = task
        self.querier = querier
        self.file_path = task.get('path', '')
        self.hunks_by_path = self._build_hunks_index()
        self.target_hunk_coords = self._parse_target_hunk_from_patch(task.get('hunk_change', ''))

    def orchestrate(
            self,
            neighborhood_block: Optional[ContextBlock],
            semantic_blocks: List[ContextBlock],
            similar_blocks: List[ContextBlock],
            random_blocks: Optional[List[ContextBlock]] = None,
            render_only_target_hunk: bool = False
    ) -> Dict[str, str]:
        """
        Run the full orchestration flow: consolidate blocks, then render them.
        """
        # Step 1: consolidate and deduplicate blocks.
        consolidated_blocks = self._consolidate_blocks(
            neighborhood_block, semantic_blocks, similar_blocks, random_blocks or []
        )

        # Step 2: render the final block lists into strings.
        final_contexts = self._render_contexts(
            consolidated_blocks,
            render_only_target_hunk = render_only_target_hunk)
        final_contexts["flat_context"] = self._render_flat_context(
            consolidated_blocks,
            render_only_target_hunk=render_only_target_hunk,
        )

        return final_contexts

    def _consolidate_blocks(
            self,
            neighborhood_block: Optional[ContextBlock],
            semantic_blocks: List[ContextBlock],
            similar_blocks: List[ContextBlock],
            random_blocks: Optional[List[ContextBlock]] = None,
    ) -> Dict[str, List[ContextBlock]]:
        random_blocks = random_blocks or []
        if neighborhood_block is None:
            return {
                "neighborhood": [],
                "semantic": semantic_blocks,
                "similar": similar_blocks,
                "random": random_blocks,
            }
        # --- Step 1: expand the neighborhood block ---
        # The neighborhood block absorbs overlapping semantic and similar blocks first.
        for sem_block in semantic_blocks:
            if neighborhood_block.overlaps(sem_block):
                neighborhood_block.expand_to_include(sem_block)

        for s_block in similar_blocks:
            if neighborhood_block.overlaps(s_block):
                neighborhood_block.expand_to_include(s_block)

        # --- Step 2: filter semantic blocks ---
        # Keep only semantic blocks that do not overlap the final expanded neighborhood block.
        final_semantic_blocks = []
        for sem_block in semantic_blocks:
            if not neighborhood_block.overlaps(sem_block):
                final_semantic_blocks.append(sem_block)

        # --- Step 3: filter similar blocks ---
        # Keep only similar blocks that do not overlap neighborhood or retained semantic blocks.
        final_similar_blocks = []
        for s_block in similar_blocks:
            # Check overlap with neighborhood.
            is_overlapped_by_neighborhood = neighborhood_block.overlaps(s_block)
            if is_overlapped_by_neighborhood:
                continue # Skip blocks that overlap neighborhood.

            # Check overlap with any retained semantic block.
            final_similar_blocks.append(s_block)

        return {
            "neighborhood": [neighborhood_block],
            "semantic": final_semantic_blocks,
            "similar": final_similar_blocks,
            "random": random_blocks,
        }

    def _render_contexts(self,
                         consolidated_blocks: Dict[str, List[ContextBlock]],
                         render_only_target_hunk: bool = False) -> Dict[str, str]:
        """Shared rendering flow."""
        final_strings = self.empty_contexts()
        for source, blocks in consolidated_blocks.items():
            output_parts = []
            for block in blocks:
                # Render each block with the shared renderer.
                rendered_block = self._render_single_block(block,render_only_target_hunk=render_only_target_hunk)
                if len(rendered_block)>0:
                    output_parts.append(rendered_block)
            final_strings[f"{source}_context"] = "\n\n".join(output_parts)

        return final_strings

    def _render_flat_context(
            self,
            consolidated_blocks: Dict[str, List[ContextBlock]],
            render_only_target_hunk: bool = False) -> str:
        """Flatten blocks from all sources, sort them, and merge overlaps within each file."""
        flat_blocks: List[ContextBlock] = []
        for source in self.CONTEXT_SOURCES:
            flat_blocks.extend(consolidated_blocks.get(source, []))

        if not flat_blocks:
            return ""

        merged_blocks = self._merge_flat_blocks(flat_blocks)
        rendered_blocks = []
        for block in merged_blocks:
            rendered_block = self._render_single_block(
                block,
                render_only_target_hunk=render_only_target_hunk,
                include_descriptions=False,
            )
            if rendered_block:
                rendered_blocks.append(rendered_block)

        if not rendered_blocks:
            return ""
        return "\n\n".join(rendered_blocks)

    def _merge_flat_blocks(self, blocks: List[ContextBlock]) -> List[ContextBlock]:
        """Sort by path hierarchy and merge overlapping blocks within each file."""
        sorted_blocks = sorted(
            (deepcopy(block) for block in blocks if block is not None),
            key=lambda block: (
                self._tree_path_sort_key(block.path or ""),
                block.start_line if block.start_line is not None else -1,
                block.end_line if block.end_line is not None else -1,
            ),
        )

        merged_blocks: List[ContextBlock] = []
        for block in sorted_blocks:
            if not merged_blocks:
                merged_blocks.append(block)
                continue

            last_block = merged_blocks[-1]
            if self._can_merge_flat_blocks(last_block, block):
                last_block.expand_to_include(block)
                last_block.description_above_path = self._merge_descriptions(
                    last_block.description_above_path,
                    block.description_above_path,
                )
                last_block.description_under_path = self._merge_descriptions(
                    last_block.description_under_path,
                    block.description_under_path,
                )
                continue

            merged_blocks.append(block)

        return merged_blocks

    @staticmethod
    def _can_merge_flat_blocks(left: ContextBlock, right: ContextBlock) -> bool:
        if left.is_virtual or right.is_virtual:
            return False
        if not left.path or not right.path:
            return False
        return left.overlaps(right)

    @staticmethod
    def _merge_descriptions(left: str, right: str) -> str:
        parts: List[str] = []
        for text in (left, right):
            text = (text or "").strip()
            if text and text not in parts:
                parts.append(text)
        return "\n\n".join(parts)

    @staticmethod
    def _tree_path_sort_key(path: str):
        """
        Simulate file-tree ordering:
        - compare path segments level by level
        - directories come before files at the same level
        - sort lexicographically within the same type
        """
        if not path:
            return tuple()

        segments = [segment for segment in path.split("/") if segment]
        key_parts = []
        for index, segment in enumerate(segments):
            is_file = index == len(segments) - 1
            key_parts.append((1 if is_file else 0, segment))
        return tuple(key_parts)

    def _render_single_block(
            self,
            block: ContextBlock,
            render_only_target_hunk: bool = False,
            include_descriptions: bool = True) -> str:
        """
            Render a single block.
            Flow:
            1. Fetch the original block content.
            2. Trim leading/trailing blank lines and determine the display range.
            3. Inject related hunk fragments inside the trimmed range.
            4. Build the final output.
            """

        original_start, original_end = block.start_line, block.end_line
        path = block.path

        # Step 1: fetch file content.
        file_lines = self.querier.file_provider.get_lines(path)

        # Get the original block content.
        result_lines = file_lines[original_start - 1: original_end]

        # Step 2: trim leading and trailing blank lines.
        trim_start_index = 0
        while trim_start_index < len(result_lines) and not result_lines[trim_start_index].strip():
            trim_start_index += 1

        trim_end_index = len(result_lines) - 1
        while trim_end_index >= trim_start_index and not result_lines[trim_end_index].strip():
            trim_end_index -= 1

        # Return early if the trimmed content is empty.
        if trim_start_index > trim_end_index:
            return ""

        # Trimmed content and the new line-number range.
        trimmed_lines = result_lines[trim_start_index: trim_end_index + 1]
        trimmed_start_line = original_start + trim_start_index
        trimmed_end_line = original_start + trim_end_index

        # Step 3: find and inject hunk fragments inside the trimmed range.
        hunks = self.hunks_by_path.get(path, [])
        hunk_injections = []  # [(insert_position, header, body)]

        for h in hunks:

            # Check whether this is the target hunk.
            if render_only_target_hunk:
                is_target_hunk = (
                        self.file_path == path and
                        self.target_hunk_coords and
                        (h["a"], h["ac"], h["c"], h["dc"]) == self.target_hunk_coords
                )
                # Skip non-target hunks when target-only rendering is enabled.
                if not is_target_hunk:
                    continue

            anchor_lines = self._hunk_anchors(h)
            trimmed_range = set(range(trimmed_start_line, trimmed_end_line + 1))
            anchor_set = set(anchor_lines)

            if not anchor_set.intersection(trimmed_range):
                continue

            # Extract the portion of this hunk that falls inside the trimmed range.
            hunk_fragment = self._extract_hunk_fragment_in_range(
                h, trimmed_start_line, trimmed_end_line, path
            )

            if hunk_fragment:
                hunk_injections.append(hunk_fragment)

        # Step 4: inject hunk fragments from back to front.
        hunk_injections.sort(key=lambda x: x['position'], reverse=True)

        for injection in hunk_injections:
            pos = injection['position']
            header = injection['header']
            body = injection['body']
            replace_cnt = injection['replace_count']

            # Replace the corresponding new-file lines with "@@ header + diff lines".
            trimmed_lines[pos: pos + replace_cnt] = [header] + body

        # Step 5: build the final output.
        final_content = "\n".join(trimmed_lines)

        # --- Compose final output ---
        output_parts = []

        # Add the pre-path description before the header if present.
        if include_descriptions and block.description_above_path:
            output_parts.append(f"{block.description_above_path}")

        # Virtual blocks stop rendering here.
        if block.is_virtual:
            pass;
        else:
            header = f"File: {path}  Lines: {trimmed_start_line}-{trimmed_end_line}"
            output_parts.append(header)
            output_parts.append(final_content)

        return "\n".join(output_parts)

    # --- Helper functions ---
    def _get_related_hunks_for_file(self) -> str:
        try:
            files_json = json.loads(self.task.get("files", "[]"))
        except (json.JSONDecodeError, TypeError):
            files_json = []
        for ch in files_json:
            if ch.get("filename") == self.file_path: return ch.get("patch", "")
        return ""

    def _build_hunks_index(self) -> Dict[str, List[Dict]]:
        try:
            files_json = json.loads(self.task.get("files", "[]"))
        except (json.JSONDecodeError, TypeError):
            files_json = []
        hunks_by_path = {}
        for ch in files_json:
            filename = ch.get("filename")
            patch = ch.get("patch") or ""
            if filename and patch:
                hunks_by_path[filename] = self._split_hunks(patch)
            elif filename:  # Create an empty list for files without patches to avoid KeyError.
                hunks_by_path[filename] = []
        return hunks_by_path

    def _split_hunks(self, related_changes: str) -> List[Dict]:
        if not related_changes: return []
        hunks_text = re.split(r"(?=^@@ )", related_changes, flags=re.MULTILINE)
        parsed = []
        for hunk_str in hunks_text:
            if not hunk_str.strip() or not hunk_str.lstrip().startswith("@@"): continue
            lines = hunk_str.strip().splitlines()
            hdr = parse_hunk_header(lines[0], anchored=True)
            if not hdr: continue
            parsed.append({
                "raw_header": lines[0], "header_short": hdr.short,
                "a": hdr.a, "ac": hdr.ac, "c": hdr.c, "dc": hdr.dc, "body": lines[1:],
            })
        return parsed

    def _parse_target_hunk_from_patch(self, patch: str) -> Optional[Tuple[int, int, int, int]]:
        if not patch: return None
        for ln in patch.splitlines():
            if ln.lstrip().startswith("@@"):
                hdr = parse_hunk_header(ln.strip(), anchored=True)
                if hdr: return (hdr.a, hdr.ac, hdr.c, hdr.dc)
        return None

    def _get_related_hunks_for_file(self, path: str) -> str:
        try:
            files_json = json.loads(self.task.get("files", "[]"))
        except (json.JSONDecodeError, TypeError):
            files_json = []
        for ch in files_json:
            if ch.get("filename") == path:
                return ch.get("patch", "") or ""
        return ""

    def _extract_hunk_fragment_in_range(self, hunk_dict, range_start, range_end, path):
        """
        Extract the portion of a hunk that intersects [range_start, range_end],
        and recompute the hunk header for that fragment.

        Returns: {
            'position': int,  # insertion position relative to range_start
            'header': str,    # recomputed @@ header
            'body': List[str] # hunk body lines
        }
        """
        # Rebuild the hunk string.
        hunk_str = "\n".join([hunk_dict["raw_header"]] + hunk_dict["body"])
        lines = hunk_str.splitlines()

        if not lines:
            return None

        # Parse the original hunk header.
        hdr = parse_hunk_header(lines[0], anchored=True)
        if not hdr:
            return None

        # Traverse the hunk body and collect lines inside the range.
        old_line = hdr.a  # current old-file line number
        new_line = hdr.c  # current new-file line number

        fragment_lines = []
        fragment_old_start = None  # fragment start line in the old file
        fragment_new_start = None  # fragment start line in the new file
        fragment_old_count = 0
        fragment_new_count = 0

        in_range = False

        for line in lines[1:]:
            # Check whether the current new_line is inside the range.
            # Deleted lines do not consume new-file line numbers, so handle them explicitly.
            if line.startswith('-'):
                # Deleted line: check whether its anchor position is inside the range.
                current_in_range = range_start <= new_line <= range_end
            else:
                # Added or context line.
                current_in_range = range_start <= new_line <= range_end

            if current_in_range and not in_range:
                # First line inside the range: record the start position.
                fragment_old_start = old_line
                fragment_new_start = new_line
                in_range = True

            if in_range:
                fragment_lines.append(line)

                if line.startswith('-'):
                    fragment_old_count += 1
                    old_line += 1
                elif line.startswith('+'):
                    fragment_new_count += 1
                    new_line += 1
                else:  # context line, usually starting with a space
                    fragment_old_count += 1
                    fragment_new_count += 1
                    old_line += 1
                    new_line += 1

                # Stop collecting after leaving the range.
                if new_line > range_end:
                    break
            else:
                # Update line numbers without collecting this line.
                if line.startswith('-'):
                    old_line += 1
                elif line.startswith('+'):
                    new_line += 1
                else:
                    old_line += 1
                    new_line += 1

        if not fragment_lines or fragment_old_start is None:
            return None

        # Compute insertion position relative to range_start.
        insert_position = fragment_new_start - range_start

        # Rebuild the hunk header.
        new_header = f"@@ -{fragment_old_start},{fragment_old_count} +{fragment_new_start},{fragment_new_count} @@"

        # Mark the target hunk.
        if (self.file_path == path
                and self.target_hunk_coords
                and (hunk_dict["a"], hunk_dict["ac"], hunk_dict["c"], hunk_dict["dc"]) == self.target_hunk_coords):
            new_header += " <—— This hunk needs review"

        return {
            'position': insert_position,
            'header': new_header,
            'body': fragment_lines,
            'replace_count': fragment_new_count,
        }

    def _hunk_anchors(self, h: Dict) -> List[int]:
        """Return hunk anchor lines where changes occur."""
        hunk_str = "\n".join([h["raw_header"]] + h["body"])
        anchors = parse_hunk_and_find_anchors(hunk_str) or []
        return anchors
