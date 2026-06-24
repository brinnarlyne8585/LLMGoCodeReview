"""
code_semantic_context_fetcher.py
------------------------------
Purpose:
  Provides the core `SemanticContextFetcher` coordinator:
  1. Validator: parse the LLM plan
  2. Executor: execute LSP queries
  3. Creator: format results into ContextBlock objects
  4. Self: organize, deduplicate, and generate logs
"""
import os
import time
from collections import defaultdict
from typing import List, Dict, Optional, Tuple

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_context_orchestrator import ContextBlock
from code_context_mangers.code_semantic_context.candidate_sorter import Candidate, CandidateSorter
from code_context_mangers.code_semantic_context.code_semantic_block_creator import SemanticBlockCreator
from code_context_mangers.code_semantic_context.code_semantic_context_model import SemanticCommand, CommandBlockGroup, BlockProvenance
from code_context_mangers.code_semantic_context.code_semantic_context_validator import SemanticPlanValidator
from code_context_mangers.code_semantic_context.code_semantic_plan_executor import SemanticQueryExecutor
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag
from code_context_mangers.run_for_code_context import is_debug_for_semantic

is_always_query_with_cache = cache_flag
top_k_for_references_and_search = 5
is_dedup_definition_blocks_by_anchor = True

class SemanticContextFetcher:
    """
    Fetch semantic code context according to the LLM plan.
    """

    def __init__(self,
                 task: dict,
                 querier: UnifiedQuerier,
                 llm_plan: List[Dict],  # LLM plan read from CSV for the current task.
                 lsp_available: bool=True,
                 existing_blocks: Optional[List[ContextBlock]] = None,
                 ):
        self.task = task
        self.querier = querier
        self.llm_plan = llm_plan
        self.lsp_available = lsp_available
        self.existing_blocks = existing_blocks
        self.path = task.get('path', '')
        self.main_reviewing_file_content = self.querier.file_provider.get_file_content(self.path)

        # Derive the neighborhood window first. The window is 0-based and inclusive.
        nb_window = self._derive_neighborhood_window_0based()

        # Initialize submodules.
        self.validator = SemanticPlanValidator(
            querier=querier,
            lsp_available=lsp_available,
            task=task,
            main_file_lines=self.main_reviewing_file_content.splitlines(),
            search_window_0based=nb_window,
        )
        self.executor = SemanticQueryExecutor(
            querier=querier,
            lsp_available=lsp_available,
            task=task,
            main_file_lines=self.main_reviewing_file_content.splitlines(),
            path=self.path,
        )
        self.block_creator = SemanticBlockCreator(
            querier=querier,
            lsp_available=lsp_available,
            task=task,
        )

    def _derive_neighborhood_window_0based(self) -> Optional[Tuple[int, int]]:
        """
        Find the single neighborhood block for the same file and return a 0-based
        inclusive window (start0, end0). Return None when not found, which falls
        back to the whole file.
        """
        if not self.existing_blocks:
            return None

        nb = next(
            (b for b in self.existing_blocks
            if isinstance(b, ContextBlock) and b.source == "neighborhood" and b.path == self.path),
            None
        )
        if nb is None:
            return None

        start0 = max(0, nb.start_line - 1)
        end0 = max(start0, nb.end_line - 1)
        return (start0, end0)

    def fetch_blocks(self) -> List[ContextBlock]:
        """
        Main execution flow: parse plan -> execute commands -> format results -> return ContextBlock list.
        """

        # Step 1: normalize and validate LLM commands.
        all_commands = self.validator.process_and_validate_plan(self.llm_plan)
        valid_commands = [cmd for cmd in all_commands if cmd.is_valid]

        if not valid_commands:
            print(f"Warning: no valid semantic command found for {self.task.get('comment_url')}.")
            debug_log = self._generate_debug_log(all_commands, [], {}, {}, []) if is_debug_for_semantic else []
            return [], debug_log

        # Step 2: execute validated commands.
        executed_commands = self.executor.execute_commands(valid_commands)

        # Step 3: organize and format command outputs.
        all_command_groups = self.block_creator.format_results_to_groups(executed_commands)

        # Step 4: sort and deduplicate within groups and against existing blocks.
        final_blocks, overlap_details, provenance_map, discarded_blocks = self._organize_blocks(all_command_groups)

        # Step 5: generate debug logs only when is_debug_for_semantic is enabled.
        debug_log = []
        if is_debug_for_semantic:
            debug_log = self._generate_debug_log(all_commands, all_command_groups, overlap_details, provenance_map, discarded_blocks)

        return final_blocks, debug_log


    # ---------------------- Steps 4 & 5: organize, filter, and debug ---------------------- #
    def _organize_blocks(self, command_groups: List[CommandBlockGroup]) -> Tuple[
        List[ContextBlock], Dict[str, str], Dict[int, BlockProvenance], List[ContextBlock]]:
        """
        Group, sort, and deduplicate all generated blocks.
        Returns (final_blocks, overlap_details, provenance_map, discarded_blocks).
        """
        # Collect all provenance records into a unified map.
        provenance_map: Dict[int, BlockProvenance] = {}
        for group in command_groups:
            for blk_id, prov in group.block_provenances.items():
                if blk_id in provenance_map:
                    provenance_map[blk_id].merge(prov)
                else:
                    provenance_map[blk_id] = prov

        discarded_blocks: List[ContextBlock] = []

        # Step 1: group command-block groups by target_symbol.
        groups_by_symbol: Dict[str, List[CommandBlockGroup]] = defaultdict(list)
        for group in command_groups:
            if group.source_command.target_symbol:
                groups_by_symbol[group.source_command.target_symbol].append(group)

        # Step 2: find the sort key for each group, using its earliest command cursor.
        sorted_group_list: List[tuple] = []
        for symbol, groups in groups_by_symbol.items():
            first_cmd = min(groups, key=lambda g: (
                g.source_command.line_num if g.source_command.line_num is not None else float('inf'),
                g.source_command.cursor if g.source_command.cursor is not None else float('inf')
            ))
            sort_key = (
                first_cmd.source_command.line_num if first_cmd.source_command.line_num is not None else float('inf'),
                first_cmd.source_command.cursor if first_cmd.source_command.cursor is not None else float('inf')
            )
            sorted_group_list.append((sort_key, symbol, groups))

        # Step 3: sort groups by earliest cursor.
        sorted_group_list.sort(key=lambda x: x[0])

        # Step 4: sort and deduplicate within groups, excluding existing blocks.
        overlap_details: Dict[str, str] = {}  # (unique block key, overlap reason)
        total_seen_blocks: List[ContextBlock] = []
        for sort_key, symbol, groups in sorted_group_list:
            # Step 4a: sort within the group, with definition before reference.
            PRIORITY_ORDER = ("get_definition", "get_type_definition", "get_references", "get_search")
            _priority = {name: i for i, name in enumerate(PRIORITY_ORDER)}
            groups.sort(
                key=lambda g: _priority.get(
                    g.source_command.command_type,
                    len(_priority)  # Unknown commands go last.
                )
            )

            # Step 4b: trim definition/type_definition blocks against existing blocks.
            for group in groups:
                new_group_blocks: List[ContextBlock] = []
                is_def_type = group.source_command.command_type in ('get_definition', 'get_type_definition')
                for block in group.generated_blocks:
                    block_key = (block.path, block.start_line, block.end_line)
                    block_key_str = str(block_key)
                    blk_id = id(block)

                    if is_def_type and block.anchor_line is not None:
                        original_range = (block.start_line, block.end_line)
                        trimmed = self._trim_block_against_existing(block, self.existing_blocks)
                        if trimmed is None:
                            # Anchor is inside an existing block; discard the whole block.
                            reason = f"Anchor L{block.anchor_line} contained by existing block"
                            overlap_details[block_key_str] = reason
                            if blk_id in provenance_map:
                                provenance_map[blk_id].status = "discarded"
                                provenance_map[blk_id].discard_reason = reason
                                provenance_map[blk_id].original_range = original_range
                            discarded_blocks.append(block)
                            continue
                        # Check whether the block was trimmed.
                        if (trimmed.start_line, trimmed.end_line) != original_range:
                            if blk_id in provenance_map:
                                provenance_map[blk_id].status = "trimmed"
                                provenance_map[blk_id].original_range = original_range
                        new_group_blocks.append(trimmed)
                    else:
                        # For ref/search or no-anchor blocks, use anchor containment.
                        is_contained, overlap_detail = self._is_anchor_contained_by_existing_blocks(block, self.existing_blocks)
                        if is_contained:
                            overlap_details[block_key_str] = overlap_detail
                            if blk_id in provenance_map:
                                provenance_map[blk_id].status = "discarded"
                                provenance_map[blk_id].discard_reason = overlap_detail
                            discarded_blocks.append(block)
                            continue
                        new_group_blocks.append(block)
                group.generated_blocks = new_group_blocks

            # Step 4c: limit get_reference/get_search count, deduplicate against seen blocks, then sort.
            for group in groups:
                if group.source_command.command_type in ['get_definition', 'get_type_definition']:
                    # get_definition/get_type_definition do not need top-k deduplication here.
                    for block in group.generated_blocks:
                        total_seen_blocks.append(block)
                else:
                    cur_line_based1 = group.source_command.line_num
                    cur_char = group.source_command.cursor
                    cur_total = len(self.querier.file_provider.get_lines(self.path))
                    cur_rel = float(cur_line_based1) / cur_total

                    cand2block = {}
                    for blk in group.generated_blocks:
                        blk_line_based1 = (blk.start_line + blk.end_line) / 2
                        blk_char = 0
                        blk_total = len(self.querier.file_provider.get_lines(blk.path))
                        blk_rel = float(blk_line_based1) / blk_total
                        c = Candidate(path=blk.path, line0=blk_line_based1 - 1, char=blk_char, rel=blk_rel)
                        cand2block[c] = blk

                    if not cand2block:
                        continue

                    sorter = CandidateSorter(
                        current_path=self.path,
                        cur_line0=cur_line_based1 - 1,
                        cur_char=cur_char,
                        cur_rel=cur_rel,
                    )
                    ordered_cands = sorter.sort(list(cand2block.keys()))
                    ordered_blocks = [cand2block[c] for c in ordered_cands]

                    # Apply top-k truncation.
                    final_top_k_blocks: List[ContextBlock] = []
                    for b in ordered_blocks:
                        is_overlap, _ = self._is_overlap_with_existing_blocks(b, total_seen_blocks)
                        if is_overlap:
                            reason = f"Top-k overlap with already seen block"
                            blk_id = id(b)
                            if blk_id in provenance_map:
                                provenance_map[blk_id].status = "discarded"
                                provenance_map[blk_id].discard_reason = reason
                            discarded_blocks.append(b)
                            continue
                        final_top_k_blocks.append(b)
                        # Truncate only when K > 0 and K has been reached.
                        if top_k_for_references_and_search \
                                and (len(final_top_k_blocks) == top_k_for_references_and_search):
                            # Mark overflow blocks.
                            remaining = ordered_blocks[ordered_blocks.index(b) + 1:]
                            for overflow_b in remaining:
                                overflow_id = id(overflow_b)
                                if overflow_id in provenance_map:
                                    provenance_map[overflow_id].status = "discarded"
                                    provenance_map[overflow_id].discard_reason = f"Top-k overflow (k={top_k_for_references_and_search})"
                                discarded_blocks.append(overflow_b)
                            break

                    group.generated_blocks = final_top_k_blocks

                    # Sort.
                    group.generated_blocks.sort(
                        key=lambda b: (
                            os.path.basename(b.path),
                            int(b.start_line),
                            int(b.end_line),
                        )
                    )

                    # Add to visible range.
                    for block in group.generated_blocks:
                        total_seen_blocks.append(block)


        # Step 4.5: deduplicate definition/type_definition blocks by anchor containment.
        if is_dedup_definition_blocks_by_anchor:
            self._dedup_definition_blocks_by_anchor(sorted_group_list, overlap_details, provenance_map, discarded_blocks)

        # Step 5: flatten the list.
        final_blocks: List[ContextBlock] = []
        for sort_key, symbol, groups in sorted_group_list:
            for group in groups:
                for block in group.generated_blocks:
                    final_blocks.append(block)

        return final_blocks, overlap_details, provenance_map, discarded_blocks

    def _dedup_definition_blocks_by_anchor(self,
                                              sorted_group_list: list,
                                              overlap_details: Dict[str, str],
                                              provenance_map: Dict[int, BlockProvenance] = None,
                                              discarded_blocks: List[ContextBlock] = None):
        """
        Deduplicate blocks generated by get_definition / get_type_definition by anchor containment.
        If block A's anchor_line falls inside block B's [start_line, end_line] range in the same file,
        remove block A and keep the larger block B.
        This directly mutates each group's generated_blocks list.
        """
        DEFINITION_TYPES = ("get_definition", "get_type_definition")
        PRIORITY = {t: i for i, t in enumerate(DEFINITION_TYPES)}

        # 1. Collect all definition/type_definition blocks with their source info.
        #    (priority, block, group_ref)
        all_def_entries = []
        for _, _, groups in sorted_group_list:
            for group in groups:
                cmd_type = group.source_command.command_type
                if cmd_type not in DEFINITION_TYPES:
                    continue
                pri = PRIORITY[cmd_type]
                for block in group.generated_blocks:
                    all_def_entries.append((pri, block, group))

        if len(all_def_entries) <= 1:
            return

        # 2. Sort by range size descending so larger blocks are kept first.
        all_def_entries.sort(key=lambda e: -(e[1].end_line - e[1].start_line))

        # 3. Mark a block for removal when its anchor is contained by an already kept block.
        kept_blocks: List[ContextBlock] = []
        remove_set = set()  # ids of removed blocks.

        for pri, block, group in all_def_entries:
            if block.anchor_line is None:
                kept_blocks.append(block)
                continue

            is_contained = False
            for kept in kept_blocks:
                if (block.path == kept.path
                        and kept.start_line <= block.anchor_line <= kept.end_line):
                    is_contained = True
                    block_key = (block.path, block.start_line, block.end_line)
                    overlap_details[str(block_key)] = (
                        f"Anchor L{block.anchor_line} contained by "
                        f"{kept.path} L{kept.start_line}-{kept.end_line}"
                    )
                    break

            if is_contained:
                remove_set.add(id(block))
                # Update provenance.
                if provenance_map and id(block) in provenance_map:
                    provenance_map[id(block)].status = "discarded"
                    provenance_map[id(block)].discard_reason = overlap_details.get(
                        str((block.path, block.start_line, block.end_line)), "Anchor dedup")
                if discarded_blocks is not None:
                    discarded_blocks.append(block)
            else:
                kept_blocks.append(block)

        # 4. Remove marked blocks from each group's generated_blocks.
        if remove_set:
            for _, _, groups in sorted_group_list:
                for group in groups:
                    group.generated_blocks = [
                        b for b in group.generated_blocks if id(b) not in remove_set
                    ]

    def _is_overlap_with_existing_blocks(self, cand_blk: ContextBlock, existing_blocks):
        is_overlap = False;
        overlap_detail = ""
        # Check whether it overlaps with any existing block.
        for blk in existing_blocks:
            if blk and cand_blk.overlaps(blk):
                is_overlap = True
                overlap_detail = f"Overlaps with existing {blk.source} block ({blk.path} L{blk.start_line}-{blk.end_line})"
                break
        return is_overlap, overlap_detail

    def _is_anchor_contained_by_existing_blocks(self, cand_blk: ContextBlock, existing_blocks):
        """
        Check whether the candidate block's anchor falls inside an existing block.
        Only anchor containment counts as duplicate; range overlap alone does not.
        If anchor_line is missing, fall back to overlap detection.
        """
        if cand_blk.anchor_line is None:
            return self._is_overlap_with_existing_blocks(cand_blk, existing_blocks)

        for blk in existing_blocks:
            if (blk
                    and blk.path == cand_blk.path
                    and blk.start_line <= cand_blk.anchor_line <= blk.end_line):
                detail = (f"Anchor L{cand_blk.anchor_line} contained by existing "
                          f"{blk.source} block ({blk.path} L{blk.start_line}-{blk.end_line})")
                return True, detail
        return False, ""

    def _trim_block_against_existing(self, block: ContextBlock, existing_blocks) -> Optional[ContextBlock]:
        """
        Trim a definition/type_definition block:
        - anchor inside existing block -> return None (discard)
        - overlapping range but anchor outside -> trim and keep the side containing the anchor
        - no overlap -> return unchanged

        Updates content_lines and start_line/end_line after trimming.
        """
        if block.anchor_line is None:
            return block

        for blk in existing_blocks:
            if not blk or blk.path != block.path:
                continue

            # No overlap; skip this existing block.
            if not block.overlaps(blk):
                continue

            # Anchor inside existing block: discard.
            if blk.start_line <= block.anchor_line <= blk.end_line:
                return None

            # Overlap but anchor outside: trim.
            # Anchor above existing block: keep block.start_line ~ blk.start_line - 1.
            if block.anchor_line < blk.start_line:
                new_end = blk.start_line - 1
                if new_end < block.start_line:
                    return None
                block.content_lines = block.content_lines[:new_end - block.start_line + 1]
                block.end_line = new_end
            # Anchor below existing block: keep blk.end_line + 1 ~ block.end_line.
            else:
                new_start = blk.end_line + 1
                if new_start > block.end_line:
                    return None
                offset = new_start - block.start_line
                block.content_lines = block.content_lines[offset:]
                block.start_line = new_start

        return block

    def _generate_debug_log(self,
                            all_commands: List[SemanticCommand],
                            all_command_groups: List[CommandBlockGroup],
                            overlap_details: Dict[str, str],
                            provenance_map: Dict[int, BlockProvenance] = None,
                            discarded_blocks: List[ContextBlock] = None) -> List[Dict]:
        """
        Step 6 core: generate final debug logs and use provenance to report block status.
        """
        if provenance_map is None:
            provenance_map = {}
        if discarded_blocks is None:
            discarded_blocks = []

        # 1. Create a map from command id to kept blocks.
        cmd_id_to_blocks_map: Dict[int, List[ContextBlock]] = defaultdict(list)
        for group in all_command_groups:
            cmd_id = id(group.source_command)
            cmd_id_to_blocks_map[cmd_id].extend(group.generated_blocks)

        discarded_block_ids = {id(b) for b in discarded_blocks}

        # 2.5 Build final_blocks_trace and block_id -> trace_order mapping first,
        #     so command entries can reference trace_ref.
        final_blocks_trace = []
        block_id_to_trace_order: Dict[int, int] = {}
        trace_order = 0
        for group in all_command_groups:
            cmd = group.source_command
            for block in group.generated_blocks:
                blk_id = id(block)
                prov = provenance_map.get(blk_id)
                trace_order += 1
                block_id_to_trace_order[blk_id] = trace_order
                trace_entry = {
                    "order": trace_order,
                    "block": f"{block.path} L{block.start_line}-{block.end_line}",
                    "description": block.description_above_path,
                    "source_command": f"{cmd.command_type}(L{cmd.line_num}, S='{cmd.target_symbol}')",
                    "source_rules": sorted(prov.source_rules) if prov else sorted(cmd.source_rules),
                    "status": prov.status if prov else "kept",
                }
                if prov and prov.original_range and prov.status == "trimmed":
                    trace_entry["original_range"] = f"L{prov.original_range[0]}-{prov.original_range[1]}"
                final_blocks_trace.append(trace_entry)

        debug_log: List[Dict] = []

        # 3. Iterate over all commands, including failed ones.
        for cmd in all_commands:
            cmd_id = id(cmd)

            # 3a. Fill basic information.
            entry = {
                "raw_command_info": f"{cmd.raw_command} (L:{cmd.raw_line}, S:'{cmd.raw_target_symbol}')",
                "is_valid": cmd.is_valid,
                "validation_error": cmd.error_message,
                "formatted_command": "N/A",
                "execution_summary": "Not Executed",
                "source_rules": sorted(cmd.source_rules),
                "generated_blocks_info": []
            }

            if not cmd.is_valid:
                debug_log.append(entry)
                continue

            # 3b. Fill validation-passed information.
            entry["formatted_command"] = f"{cmd.command_type}(L{cmd.line_num}, C{cmd.cursor}, S='{cmd.target_symbol}')"

            # 3c. Fill execution result.
            if not cmd.lsp_result:
                entry["execution_summary"] = "Execution Error (No Result Object)"
            elif cmd.lsp_result.get("error"):
                entry["execution_summary"] = f"Execution Error: {cmd.lsp_result.get('error')}"
            else:
                result_list = cmd.lsp_result.get("result", [])
                result_len = len(result_list) if result_list is not None else 0
                entry["execution_summary"] = f"Executed, Found {result_len} raw result(s)"

            # 3d. Fill generated blocks and their status as structured dicts.
            generated_blocks = cmd_id_to_blocks_map.get(cmd_id, [])
            has_discarded = any(
                id(b) in discarded_block_ids for b in discarded_blocks
                if provenance_map.get(id(b)) and cmd.command_type in provenance_map[id(b)].command_types
                and cmd.target_symbol in provenance_map[id(b)].target_symbols
            )
            if not generated_blocks and not has_discarded:
                entry["generated_blocks_info"].append({
                    "block": None,
                    "status": "no_result",
                    "detail": "No blocks generated from results.",
                })
            else:
                # Kept blocks.
                for block in generated_blocks:
                    blk_id = id(block)
                    prov = provenance_map.get(blk_id)
                    block_entry = {
                        "block": f"{block.path} L{block.start_line}-{block.end_line}",
                        "status": prov.status if prov else "kept",
                        "trace_ref": block_id_to_trace_order.get(blk_id),
                    }
                    if prov and prov.source_rules:
                        block_entry["rules"] = sorted(prov.source_rules)
                    if prov and prov.original_range and prov.status == "trimmed":
                        block_entry["original_range"] = f"L{prov.original_range[0]}-{prov.original_range[1]}"
                    entry["generated_blocks_info"].append(block_entry)

                # Discarded blocks matching the current command.
                for block in discarded_blocks:
                    blk_id = id(block)
                    prov = provenance_map.get(blk_id)
                    if not prov:
                        continue
                    if cmd.command_type not in prov.command_types:
                        continue
                    if cmd.target_symbol not in prov.target_symbols:
                        continue

                    block_entry = {
                        "block": f"{block.path} L{block.start_line}-{block.end_line}",
                        "status": "discarded",
                        "trace_ref": None,
                    }
                    if prov.discard_reason:
                        block_entry["reason"] = prov.discard_reason
                    if prov.source_rules:
                        block_entry["rules"] = sorted(prov.source_rules)
                    if prov.original_range:
                        block_entry["original_range"] = f"L{prov.original_range[0]}-{prov.original_range[1]}"
                    entry["generated_blocks_info"].append(block_entry)

            debug_log.append(entry)

        return {"commands": debug_log, "final_blocks_trace": final_blocks_trace}

    def _print_blocks(self, blocks: List[ContextBlock]):
        """
        Helper method: print a list of ContextBlock objects.
        """
        if not blocks:
            print("\n" + "="*30 + " Render Context Blocks " + "="*30)
            print("No Context Blocks Found")
            print("="*75 + "\n")
            return

        print(f"\n{'='*30} Rendering {len(blocks)} context blocks {'='*30}\n")

        for i, block in enumerate(blocks):
            # 1. --- Get data ---
            path = getattr(block, 'path', 'N/A')
            start_line = getattr(block, 'start_line', 1) # Defaults to 1.
            end_line = getattr(block, 'end_line', '?')
            content_lines = getattr(block, 'content_lines', None)
            
            # 2. --- Print description, preferring description_under_path ---
            description = getattr(block, 'description_under_path', None)
            if description:
                print(description)

            # 3. --- Print file header ---
            print(f"File: {path}  Lines: {start_line}-{end_line}")

            # 4. --- Print content with line numbers ---
            if content_lines:
                current_line_num = start_line
                for line in content_lines:
                    print(f"{current_line_num:<4} | {line}")
                    current_line_num += 1
            else:
                print("(No content lines)")

            # 5. --- Print separator ---
            print()
        
        print(f"{'='*30} Rendering complete {'='*30}\n")
