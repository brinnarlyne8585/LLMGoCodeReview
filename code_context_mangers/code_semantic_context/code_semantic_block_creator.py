"""
code_semantic_block_creator.py
--------------------------
Purpose:
  Provides the `SemanticBlockCreator` class, which converts raw LSP/Git
  query results into `ContextBlock` lists and extends context ranges.
  This is step 3 in `SemanticContextFetcher`.
"""
from typing import List, Dict
import Levenshtein
from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_orchestrator import ContextBlock
from code_context_mangers.code_context_utils import FUNCTION_LEVEL_KINDS, STRUCT_LEVEL_KINDS
from code_context_mangers.code_semantic_context.candidate_sorter import Candidate, CandidateSorter
from code_context_mangers.code_semantic_context.code_semantic_context_model import SemanticCommand, CommandBlockGroup, BlockProvenance
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag

class SemanticBlockCreator:
    """
    Convert raw LSP outputs into a list of `CommandBlockGroup` objects.
    """
    def __init__(self,
                 querier: UnifiedQuerier,
                 task: dict,
                 lsp_available: bool=True,):
        self.querier = querier  # Required for fetching document symbols.
        self.lsp_available = lsp_available
        self.base_uri_prefix = f"file://{self.querier.project_abs_path}/"
        self.task = task
        self.path = task.get('path', '') # Path of the reviewed file.


    def format_results_to_groups(self, commands: List[SemanticCommand]) -> List[CommandBlockGroup]:
        """
        Step 3 core: convert raw LSP outputs into `CommandBlockGroup` objects.
        """
        all_command_groups: List[CommandBlockGroup] = []

        for cmd in commands:

            if not cmd.lsp_result or cmd.lsp_result.get('error') or not cmd.command_type or not cmd.target_symbol:
                continue

            lsp_result = cmd.lsp_result.get('result', [])
            if lsp_result == None or len(lsp_result) == 0:
                continue

            if cmd.command_type == "get_definition":
                filtered_result = self._filter_definition_results(cmd, lsp_result)
                blocks = self._create_blocks_from_get_definition(cmd, filtered_result)
            if cmd.command_type == "get_type_definition":
                filtered_result = self._filter_definition_results(cmd, lsp_result)
                blocks = self._create_blocks_from_get_type_definition(cmd, filtered_result)
            elif cmd.command_type == "get_references":
                blocks = self._create_blocks_from_get_references(cmd, lsp_result, cmd.lsp_result)
            elif cmd.command_type == "get_search":
                blocks = self._create_blocks_from_get_search(cmd, lsp_result)

            if blocks:
                # Build provenance for each block.
                block_provenances = {}
                for block in blocks:
                    prov = BlockProvenance(
                        command_types={cmd.command_type},
                        target_symbols={cmd.target_symbol},
                        source_rules=set(cmd.source_rules),
                    )
                    block_provenances[id(block)] = prov

                all_command_groups.append(CommandBlockGroup(
                    source_command=cmd,
                    generated_blocks=blocks,
                    block_provenances=block_provenances,
                ))

        return all_command_groups

    def _filter_definition_results(self, cmd: SemanticCommand, lsp_result_list: List[Dict]) -> List[Dict]:
        """
        Reuse CandidateSorter: convert LSP definition results to Candidate objects
        and choose top-1 with same-file and nearest-location priority.
        """
        if not lsp_result_list:
            return []

        # Relative position in the current file; used to avoid zero-division issues.
        cur_total = len(self.querier.file_provider.get_lines(self.path))
        cur_rel = float(cmd.exec_line_num + 1) / cur_total

        # Collect candidates.
        cand2loc = {}
        for loc in lsp_result_list:
            try:
                uri = loc.get('uri', '')
                if self.base_uri_prefix not in uri:
                    continue
                file_path = uri[len(self.base_uri_prefix):]

                r = loc.get('range', {}) or {}
                start = r.get('start', {}) or {}
                line0 = int(start.get('line', 0) or 0)
                char = int(start.get('character', 0) or 0)

                tot = len(self.querier.file_provider.get_lines(file_path))
                rel = float(line0+1) / (tot)

                c = Candidate(path=file_path, line0=line0, char=char, rel=rel)
                cand2loc[c] = loc
            except Exception:
                continue

        if not cand2loc:
            return []

        sorter = CandidateSorter(
            current_path=self.path,
            cur_line0=cmd.exec_line_num,
            cur_char=cmd.exec_cursor,
            cur_rel=cur_rel,
        )
        best = sorter.sort(cand2loc.keys(), top_k=1)
        if not best:
            return []

        return [cand2loc[best[0]]]

    def _create_blocks_from_get_definition(self, cmd, lsp_result):
        raw_blocks = []
        for loc in lsp_result:
            if not isinstance(loc, dict) or 'uri' not in loc or 'range' not in loc:
                continue

            description_above_path = f"Find the definition of `{cmd.target_symbol}`:"

            uri = loc['uri']
            file_path = uri[len(self.base_uri_prefix):]
            # Skip files outside the project.
            if self.base_uri_prefix not in uri:
                continue;

            lsp_range = loc['range']
            # Raw LSP results are 0-based.
            anchor_start_line_0based = lsp_range['start']['line']
            anchor_end_line_0based = lsp_range['end']['line']

            new_start_line_0based, new_end_line_0based, content_lines = self._extend_context_considering_function(
                target_symbol=cmd.target_symbol,
                file_path=file_path,
                anchor_start_line_0based=anchor_start_line_0based,
                anchor_end_line_0based=anchor_end_line_0based)
            # Create ContextBlock and convert to 1-based lines.
            block = ContextBlock(
                start_line=new_start_line_0based + 1,
                end_line=new_end_line_0based + 1,
                source='semantic',
                path=file_path,
                description_above_path=description_above_path,
                content_lines=content_lines,
                anchor_line=anchor_start_line_0based + 1,
            )
            raw_blocks.append(block)
        merged_blocks = self._merge_blocks_for_cmd(raw_blocks)
        return merged_blocks

    def _create_blocks_from_get_type_definition(self, cmd, lsp_result):
        raw_blocks = []

        for loc in (lsp_result or []):
            if not isinstance(loc, dict):
                continue
            uri = loc.get("uri")
            lsp_range = loc.get("range")

            # Only process files inside the project.
            if self.base_uri_prefix not in uri:
                continue

            # 1) Normalize the relative path, e.g. network/wsPeer.go.
            file_path = uri[len(self.base_uri_prefix):]

            # 2) Parse 0-based line and character positions.
            s = lsp_range["start"]
            e = lsp_range["end"]
            anchor_start_line_0based = int(s["line"])
            anchor_end_line_0based = int(e["line"])
            start_char = int(s.get("character", 0))
            end_char = int(e.get("character", 0))

            # 3) Read file content and slice by range. LSP end positions are exclusive.
            file_lines = self.querier.file_provider.get_lines(file_path)

            if file_lines \
                    and 0 <= anchor_start_line_0based < len(file_lines) \
                    and 0 <= anchor_end_line_0based < len(file_lines) \
                    and anchor_start_line_0based==anchor_end_line_0based:
                line = file_lines[anchor_start_line_0based]
                type_definition_text = line[start_char:end_char].strip()
            else:
                continue;

            # 4) Build the description.
            description_above_path = f"Find the type definition of `{cmd.target_symbol}` (which is `{type_definition_text}`):"

            # 5) Extend context anchored at the LSP range. Use the sliced text as the target name;
            #    fall back to cmd.target_symbol if needed.
            new_start_line_0based, new_end_line_0based, content_lines = self._extend_context_considering_function(
                target_symbol = type_definition_text,
                file_path = file_path,
                anchor_start_line_0based = anchor_start_line_0based,
                anchor_end_line_0based = anchor_end_line_0based
            )

            # 6) Create ContextBlock and convert to 1-based lines.
            block = ContextBlock(
                start_line=new_start_line_0based + 1,
                end_line=new_end_line_0based + 1,
                source='semantic',
                path=file_path,
                description_above_path=description_above_path,
                content_lines=content_lines,
                anchor_line=anchor_start_line_0based + 1,
            )
            raw_blocks.append(block)

        merged_blocks = self._merge_blocks_for_cmd(raw_blocks)
        return merged_blocks

    def _create_blocks_from_get_references(self, cmd, lsp_result, lsp_msg):

        raw_blocks = []
        description_above_path = f"Find the reference to `{cmd.target_symbol}`:"

        for loc in lsp_result:
            if not isinstance(loc, dict) or 'uri' not in loc or 'range' not in loc:
                continue

            uri = loc['uri']
            file_path = uri[len(self.base_uri_prefix):]
            # Skip files outside the project.
            if self.base_uri_prefix not in uri:
                continue;

             # Skip files no longer tracked by Git.
            if not self.querier.file_provider.has_content_in_git(file_path):
                continue;

            lsp_range = loc['range']
            # Raw LSP results are 0-based.
            anchor_start_line_0based = lsp_range['start']['line']
            anchor_end_line_0based = lsp_range['end']['line']

            new_start_line_0based, new_end_line_0based, content_lines = self._extend_context_with_window(file_path,
                                                                                                         anchor_start_line_0based,
                                                                                                         anchor_end_line_0based)
            # Create ContextBlock and convert to 1-based lines.
            block = ContextBlock(
                start_line=new_start_line_0based + 1,
                end_line=new_end_line_0based + 1,
                source='semantic',
                path=file_path,
                description_above_path=description_above_path,
                content_lines=content_lines,
                anchor_line=anchor_start_line_0based + 1,
            )
            raw_blocks.append(block)

        merged_blocks = self._merge_blocks_for_cmd(raw_blocks)
        return merged_blocks

    def _create_blocks_from_get_search(self, cmd, lsp_result):
        raw_blocks = []

        max_candidator = 1000
        if len(lsp_result) > max_candidator:
            scored_results = []
            for occurance in lsp_result:
                file_path = occurance['file_path']
                distance = Levenshtein.distance(self.path, file_path)
                similarity_score = 1 / (1 + distance)
                scored_results.append((similarity_score, occurance))
            scored_results.sort(key=lambda x: x[0], reverse=True)
            lsp_result = [occurance for _, occurance in scored_results[:max_candidator]]

        for i, occurance in enumerate(lsp_result):
            description_above_path = f"Find the existing occurrence of `{cmd.target_symbol}`:"
            file_path = occurance['file_path']
            line_number = occurance['line_number']
            # Git results are 1-based.
            anchor_start_line_0based = line_number - 1
            anchor_end_line_0based = line_number - 1

            new_start_line_0based, new_end_line_0based, content_lines = self._extend_context_with_window(file_path,
                                                                                                         anchor_start_line_0based,
                                                                                                         anchor_end_line_0based)
            if new_start_line_0based is not None \
                    and new_end_line_0based is not None \
                    and content_lines is not None:
                # Create ContextBlock and convert to 1-based lines.
                block = ContextBlock(
                    start_line=new_start_line_0based + 1,
                    end_line=new_end_line_0based + 1,
                    source='semantic',
                    path=file_path,
                    description_above_path=description_above_path,
                    content_lines=content_lines,
                    anchor_line=anchor_start_line_0based + 1,
                )
                raw_blocks.append(block)
        return raw_blocks

    def _extend_context_considering_function(self, target_symbol, file_path, anchor_start_line_0based,
                                             anchor_end_line_0based):

        file_lines = self.querier.file_provider.get_lines(file_path)

        if file_lines is None:
            # Could not fetch content; return the original anchor and an empty content list.
            return anchor_start_line_0based, anchor_end_line_0based, []

        max_line_index = len(file_lines) - 1

        # 2. Try to match a function/class symbol.
        anchor_start_line_0based = max(0, min(anchor_start_line_0based, max_line_index))
        anchor_end_line_0based = max(0, min(anchor_end_line_0based, max_line_index))
        anchor_start_line_1based = anchor_start_line_0based + 1

        final_start_line_0based = anchor_start_line_0based
        final_end_line_0based = anchor_end_line_0based
        found_symbol_block = False

        try:
            # Force cache-only mode when requested by the caller or when LSP is unavailable.
            force_cache_only = is_always_query_with_cache or not self.lsp_available
            sym_resp = self.querier.query_by_cache_flag(
                file_path=file_path,
                command="get_document_symbol",
                params={},
                is_always_query_with_cache = force_cache_only,
            )

            if sym_resp and not sym_resp.get('error'):
                symbols = sym_resp.get('result') or []

                for symbol in symbols:
                    kind = symbol.get('kind')
                    is_function_or_class = (kind in FUNCTION_LEVEL_KINDS) or (kind in STRUCT_LEVEL_KINDS)
                    start_line_1based = symbol.get('start_line')
                    end_line_1based = symbol.get('end_line')
                    anchor_is_inside = (start_line_1based <= anchor_start_line_1based <= end_line_1based)
                    name_contains_target = target_symbol in symbol.get('name', '')
                    # Handle naming differences such as "route.methodName" vs "(ReceiverType).methodName".
                    if not name_contains_target and '.' in target_symbol:
                        bare_name = target_symbol.split('.')[-1]
                        name_contains_target = bare_name in symbol.get('name', '')

                    if is_function_or_class and anchor_is_inside and name_contains_target:
                        start_line_1based = symbol.get('start_line')
                        end_line_1based = symbol.get('end_line')
                        final_start_line_0based = max(0, start_line_1based - 1)
                        final_end_line_0based = min(max_line_index, end_line_1based - 1)
                        found_symbol_block = True
                        break

        except Exception as e:
            print(f"Warning: get_document_symbol failed; falling back to window expansion: {e}")

        # 3. Fall back to window expansion if no symbol block was found.
        if not found_symbol_block:
            final_start_line_0based, final_end_line_0based = self._expand_window(
                file_lines,
                anchor_start_line_0based,
                anchor_end_line_0based
            )

        # 4. Slice content by the final range.
        content_lines = file_lines[final_start_line_0based: final_end_line_0based + 1]

        return final_start_line_0based, final_end_line_0based, content_lines

    def _extend_context_with_window(self, file_path, anchor_start_line_0based, anchor_end_line_0based):
        file_lines = self.querier.file_provider.get_lines(file_path)
        if file_lines:
            # Use the helper to compute the range.
            new_start_line_0based, new_end_line_0based = self._expand_window(
                file_lines,
                anchor_start_line_0based,
                anchor_end_line_0based
            )
            content_lines = file_lines[new_start_line_0based:new_end_line_0based + 1]
            return new_start_line_0based, new_end_line_0based, content_lines
        else:
            print(f"Warning: could not fetch file content for '{file_path}'; context was not extended.")
            return None, None, None



    def _expand_window(self, file_lines: List[str], anchor_start_0based: int, anchor_end_0based: int) -> (int, int):
        """
        Starting from the anchor, extend upward and downward over non-empty lines
        with a maximum of 3 lines per direction. Inputs and outputs are all 0-based.
        """
        max_line_index = len(file_lines) - 1

        # Clamp anchors into the file.
        anchor_start_0based = max(0, min(anchor_start_0based, max_line_index))
        anchor_end_0based = max(0, min(anchor_end_0based, max_line_index))

        # Extend upward over at most 3 non-empty lines.
        new_start_0based = anchor_start_0based
        for _ in range(3):
            check_line = new_start_0based - 1
            if check_line < 0 or not file_lines[check_line].strip():
                break  # Reached file start or an empty line.
            new_start_0based = check_line

        # Extend downward over at most 3 non-empty lines.
        new_end_0based = anchor_end_0based
        for _ in range(3):
            check_line = new_end_0based + 1
            if check_line > max_line_index or not file_lines[check_line].strip():
                break  # Reached file end or an empty line.
            new_end_0based = check_line

        return new_start_0based, new_end_0based

    def _merge_blocks_for_cmd(self, blocks: List[ContextBlock]) -> List[ContextBlock]:
        """
        Merge overlapping ContextBlock objects.
        Assumes `blocks` is already sorted by `path` and then `start_line` ascending.
        """
        if not blocks:
            return []

        merged_blocks: List[ContextBlock] = []

        # 1. Merge ranges in a single pass.
        # Start with the first block.
        current_merged_block = blocks[0]
        # Traverse the remaining blocks.
        for next_block in blocks[1:]:
            if current_merged_block.overlaps(next_block):
                # If they overlap in the same file, extend the current block.
                current_merged_block.expand_to_include(next_block)
            else:
                # If they do not overlap, store the previous merged block.
                merged_blocks.append(current_merged_block)
                # Start a new merged block.
                current_merged_block = next_block
        # Add the final merged block.
        merged_blocks.append(current_merged_block)

        for block in merged_blocks:
            # Fetch file content from the provider.
            full_file_lines = self.querier.file_provider.get_lines(block.path)
            if full_file_lines:
                # Update content_lines according to the merged range.
                start_0based = max(0, block.start_line - 1)
                end_0based = min(len(full_file_lines) - 1, block.end_line - 1)
                block.content_lines = full_file_lines[start_0based: end_0based + 1]
            else:
                # Fallback: if fetching fails, content_lines remains from the first
                # pre-merge block and may be incomplete.
                print(
                    f"Warning: could not fetch content for {block.path}; content_lines for merged block "
                    f"{block.start_line}-{block.end_line} may be incomplete.")

        return merged_blocks
