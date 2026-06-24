"""
semantic_query_executor.py
--------------------------
Purpose:
  Provides the `SemanticQueryExecutor` class, which executes validated
  `SemanticCommand` objects. This is step 2 in `SemanticContextFetcher`.
"""
import copy
import json

from typing import List, Optional

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_context_mangers.code_semantic_context.code_semantic_context_model import SemanticCommand
from code_context_mangers.code_semantic_context.semantic_replacement_finder import ReplacementFinder
from code_context_mangers.code_semantic_context.syntax_noncode_detector import is_cursor_in_noncode
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag

class SemanticQueryExecutor:
    """
    Dispatch and execute LSP or Git commands according to command type.
    """

    def __init__(self,
                 querier: UnifiedQuerier,
                 task: dict,
                 path: str,
                 main_file_lines: List[str],
                 lsp_available: bool = True,
                 ):
        self.task = task
        self.querier = querier
        self.lsp_available = lsp_available
        self.primary_lang_id = querier.main_lang_id
        self.path = path  # Path of the reviewed file.
        self.main_file_lines = main_file_lines

        # Used to find a replacement location when the cursor is in non-code.
        self._replacement_finder = ReplacementFinder(
            querier=querier,
            lsp_available=lsp_available,
            task=task,
            path=path,
        )

    def execute_commands(self, commands: List[SemanticCommand]) -> List[SemanticCommand]:
        """
        Step 2 core: dispatch and execute LSP or Git commands according to command type.
        """

        # Execute commands and collect LSP results.
        for cmd in commands:
            if cmd.command_type == "get_search":
                # --- Run Git plain-text search ---
                resp = self.querier.search_text_in_commit(
                    query=cmd.target_symbol
                )
                cmd.lsp_result = resp
                continue;


            # --- Run symbol-level LSP query ---

            # Force cache-only mode when requested by the caller or when LSP is unavailable.
            force_cache_only = is_always_query_with_cache or not self.lsp_available

            command_type = cmd.command_type
            lsp_params = {
                "line": cmd.line_num,
                "character": cmd.cursor
            }

            first_resp = self.querier.query_by_cache_flag(
                file_path=self.path,
                command=command_type,
                params=lsp_params,
                is_always_query_with_cache=force_cache_only,
            )

            # Keep the first response directly when it has a non-empty result.
            if not self.querier.is_null_or_empty_result(first_resp):
                cmd.lsp_result = first_resp
                continue

            # Empty result: check whether the cursor is in a non-code region.
            is_noncode, kind, detail = is_cursor_in_noncode(
                primary_lang_id=self.primary_lang_id,
                file_lines=self.main_file_lines,
                line_0based=cmd.line_num,
                col_0based=cmd.cursor,
            )

            if not is_noncode:
                # Not in non-code: keep the first empty response.
                cmd.lsp_result = first_resp
                continue

            # Cursor is in non-code: try to find the actual code location.
            new_path, new_line, new_char = self._replacement_finder.find_suitable_replacement(cmd)
            if (new_path is None) or (new_line is None) or (new_char is None):
                cmd.error_message = f"Cursor is inside a {'string' if kind == 'string' else 'comment'}, " \
                                    f"and no replacement location was found; this command is not useful in non-code."
            else:
                # Override line/column and record cross-file path in cmd.exec_path for execution.
                cmd.exec_path, cmd.exec_line_num, cmd.exec_cursor = new_path, new_line, new_char
                retry_lsp_params = {
                    "line": cmd.exec_line_num,
                    "character": cmd.exec_cursor,
                }
                retry_resp = self.querier.query_by_cache_flag(
                    file_path=cmd.exec_path,
                    command=command_type,
                    params=retry_lsp_params,
                    is_always_query_with_cache=force_cache_only,
                )
                cmd.lsp_result = retry_resp

        # Normalize exec_* and non-exec attributes.
        for cmd in commands:
            cmd.exec_path = cmd.exec_path if cmd.exec_path is not None else self.path
            cmd.exec_line_num = cmd.exec_line_num if cmd.exec_line_num is not None else cmd.line_num
            cmd.exec_cursor = cmd.exec_cursor if cmd.exec_cursor is not None else cmd.cursor

        # Add a paired get_type_definition command for successful get_definition commands.
        expanded: List[SemanticCommand] = []
        for cmd in commands:
            expanded.append(cmd)
            if cmd.error_message:
                continue
            if cmd.command_type == "get_definition" \
                    and (self.primary_lang_id not in ["python", "ruby", "php"]):
                twin = copy.deepcopy(cmd)
                twin.command_type = "get_type_definition"
                twin_lsp_params = {
                    "line": cmd.exec_line_num,
                    "character": cmd.exec_cursor,
                }
                twin_resp = self.querier.query_by_cache_flag(
                    file_path=twin.exec_path,
                    command=twin.command_type,
                    params=twin_lsp_params,
                    is_always_query_with_cache=force_cache_only,
                )
                twin.lsp_result = twin_resp
                expanded.append(twin)

        # Deduplicate commands with identical execution results.
        deduped_commands = self._dedup_commands_by_result(expanded)

        return deduped_commands

    def _serialize_result(self, resp) -> Optional[str]:
        try:
            # Use a stable serializable string as the key when possible.
            return json.dumps(resp, sort_keys=True, default=str)
        except Exception:
            return None

    # === Deduplicate executed commands and keep the earliest (line_num, cursor). ===
    def _dedup_commands_by_result(self, commands: List[SemanticCommand]) -> List[SemanticCommand]:
        """
        Deduplication rules:
        - Commands are duplicates only when serialized lsp_result['result'] is identical.
        - command_type, target_symbol, and other fields are ignored.
        - Keep only the command with the earliest (line_num, cursor).
        - Keep all commands with no result, errors, or unserializable responses.
        """
        buckets = {}  # key: norm_result -> (best_line, best_cur, best_idx)
        key_for_idx = {}  # key: idx -> norm_result, used for reverse lookup.

        for idx, cmd in enumerate(commands):

            if cmd.lsp_result==None:
                continue

            # Get the parsed LSP result.
            lsp_result = cmd.lsp_result.get('result', [])
            if lsp_result==None or len(lsp_result)==0:
                key_for_idx[idx] = None  # Skip deduplication and leave it to downstream handling.
                continue;

            # Try to serialize the result.
            norm_result = None
            if cmd.lsp_result:
                norm_result = self._serialize_result(lsp_result)

            # No result / error / cannot normalize: skip deduplication.
            if norm_result is None:
                key_for_idx[idx] = None  # Mark as not deduplicable.
                continue

            key_for_idx[idx] = norm_result

            line = cmd.line_num
            cur = cmd.cursor

            if norm_result not in buckets or (line, cur) < buckets[norm_result][:2]:
                # Found a new winner.
                buckets[norm_result] = (line, cur, idx)

        # Collect winner indices.
        keep_indices = {i for _, _, i in buckets.values()}

        final_commands = []
        for idx, cmd in enumerate(commands):
            norm_result = key_for_idx.get(idx)

            # 1. Keep commands that are unserializable or have no result.
            if norm_result is None:
                final_commands.append(cmd)
                continue

            # 2. Keep winner commands.
            if idx in keep_indices:
                final_commands.append(cmd)

            # 3. Mark loser commands with equivalent results but later positions as discarded.
            else:
                win_idx = buckets[norm_result][2]
                win_cmd = commands[win_idx]

                win_cmd_str = win_cmd.get_format_cmd()
                cmd.error_message = f"Execution result is equivalent to: {win_cmd_str}"
                cmd.is_valid = False  # Mark as invalid.
                cmd.lsp_result = None  # Clear the result.
        return final_commands
