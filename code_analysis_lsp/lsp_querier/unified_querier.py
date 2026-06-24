# unified_querier.py
# --------------------------------------------------
# Description:
#   Provides a top-level unified querier API.
#   This is the single entry point for code analysis queries.
#   - It composes GitQuerier for read-only Git operations.
#   - It composes CachingQuerier for cached LSP operations.
#   - It contains business logic, post-processing, and validation helpers.
# --------------------------------------------------
from datetime import datetime
import os
import sys
from typing import List, Dict, Union, Optional, Any

from code_analysis_lsp.lsp_querier.file_content_provider import FileContentProvider
from code_analysis_lsp.lsp_querier.git_querier import GitQuerier
from code_analysis_lsp.lsp_querier.query_cacher import CachingQuerier

# Import helper functions.
from code_analysis_lsp.lsp_querier.lsp_utils import flatten_symbol_tree, format_type_definition_resp
from code_analysis_lsp.utils.helpers import get_lang_from_path

title = "UnifiedQuerier"

class UnifiedQuerier:

    def __init__(self,
                 project_name_full: Optional[str] = None,
                 commit_sha: Optional[str] = None,
                 main_lang_id: Optional[str] = None,
                 project_abs_path: Optional[str] = None,
                 ):
        """
        Initialize the unified querier.
        - Create a GitQuerier instance for read-only operations.
        - Create a CachingQuerier instance for LSP operations.
        """
        self.project_name_full = project_name_full
        self.commit_sha = commit_sha
        self.main_lang_id = main_lang_id
        self.project_abs_path = project_abs_path

        self.git_querier = GitQuerier()
        self.caching_querier = CachingQuerier()

        self.file_provider = FileContentProvider(self.git_querier,
                                                 self.project_abs_path,
                                                 self.commit_sha)

    # ---- Unified result inspection helpers ----
    @staticmethod
    def has_error_or_warnings(resp: dict) -> bool:
        """
        Treat the response as failed when:
        - the top level contains a non-empty error / errors field
        - the top level contains non-empty warnings (string/list/dict)
        """
        if resp is None:
            return False
        if not isinstance(resp, dict):
            return False

        err = resp.get("error") or resp.get("errors")
        if err:
            return True

        warns = resp.get("warnings")
        if isinstance(warns, str) and warns.strip():
            return True
        if isinstance(warns, (list, dict)) and len(warns) > 0:
            return True

        return False

    @staticmethod
    def is_symbol_analysis_failed(resp: dict) -> bool:
        if resp is None:
            return False
        if not isinstance(resp, dict):
            return False

        err = (resp.get("error") or resp.get("errors") or "")
        return "Symbol Analysis Failed" in str(err)

    @staticmethod
    def is_null_or_empty_result(resp: dict) -> bool:
        """
        Determine whether the response is null or empty:
        - resp is None
        - resp does not contain a result field
        - result is None, [] or {}
        """
        if resp is None:
            return True

        if not isinstance(resp, dict):
            return False

        if "result" not in resp:
            return True

        r = resp.get("result")
        return r is None or r == [] or r == {}

    # ---- 1. Read-only Git queries (delegated to GitQuerier) ----
    # def get_file_content(self,
    #                      file_path: str,
    #                      project_path: str=None,
    #                      commit_sha: str=None,
    #                      ):
    #     """Get file content in read-only mode."""
    #     _project_path = project_path if project_path is not None else self.project_abs_path
    #     _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
    #     return self.git_querier.get_file_content(_project_path, _commit_sha, file_path)

    def list_files_in_directory(self,
                                directory_path: str,
                                project_path: str = None,
                                commit_sha: str = None,
                                ) -> dict:
        """List directory contents in read-only mode."""
        _project_path = project_path if project_path is not None else self.project_abs_path
        _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
        return self.git_querier.list_files_in_directory(_project_path, _commit_sha, directory_path)

    def search_text_in_commit(self,
                              query: str,
                              project_path: str = None,
                              commit_sha: str = None,
                              ) -> dict:
        """Search text inside a commit in read-only mode."""
        _project_path = project_path if project_path is not None else self.project_abs_path
        _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
        return self.git_querier.search_text_in_commit(_project_path, _commit_sha, query)

    # ---- 2. Cached LSP queries (delegated to CachingQuerier) ----
    def query_by_cache_flag(self,
                            file_path,
                            command,
                            params,
                            is_always_query_with_cache: bool,
                            project_name_full = None,
                            commit_sha = None,
                            lang_id = None,
                            post_process_params: Optional[Dict[str, Any]] = None):
        """
        Use `is_always_query_with_cache` to decide whether to
        query cache only or run the smart read/write cached query path.
        """
        _project_name_full = project_name_full if project_name_full is not None else self.project_name_full
        _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
        _lang_id = lang_id if lang_id is not None else self.main_lang_id
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if is_always_query_with_cache:
            # Cache-only query.
            raw_response = self.caching_querier.query_cache_only(
                project_name_full = _project_name_full,
                commit_sha = _commit_sha,
                lang_id=_lang_id,
                file_path = file_path,
                command = command,
                params = params,
            )
        else:
            # Smart query with cache read/write.
            raw_response = self.caching_querier.query(
                project_name_full=_project_name_full,
                commit_sha=_commit_sha,
                lang_id=_lang_id,
                file_path=file_path,
                command=command,
                params=params,
                # Enable cache read/write by default.
                use_cache_read=True,
                use_cache_write=True,
            )

        # Apply unified post-processing here.
        post_processed_result = self._post_process_result(command, raw_response, post_process_params)
        return post_processed_result

    def query_cache_only(self,
                         file_path,
                         command,
                         params,
                         project_name_full=None,
                         commit_sha=None,
                         lang_id=None,
                         post_process_params: Optional[Dict[str, Any]] = None):
        _project_name_full = project_name_full if project_name_full is not None else self.project_name_full
        _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
        _lang_id = lang_id if lang_id is not None else self.main_lang_id
        raw_response = self.caching_querier.query_cache_only(
            project_name_full = _project_name_full,
            commit_sha = _commit_sha,
            lang_id = _lang_id,
            file_path = file_path,
            command = command,
            params = params,
        )
        # Apply unified post-processing here.
        return self._post_process_result(command, raw_response, post_process_params)

    def query(self,
              file_path,
              command,
              params,
              project_name_full=None,
              commit_sha=None,
              lang_id=None,
              post_process_params: Optional[Dict[str, Any]] = None,
              use_cache_read=True,
              use_cache_write=True,
              is_probe = False,
              ):
        _project_name_full = project_name_full if project_name_full is not None else self.project_name_full
        _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
        _lang_id = lang_id if lang_id is not None else self.main_lang_id
        raw_response = self.caching_querier.query(
                project_name_full = _project_name_full,
                commit_sha = _commit_sha,
                lang_id=_lang_id,
                file_path = file_path,
                command=command,
                params=params,
                # Enable cache read/write by default.
                use_cache_read=use_cache_read,
                use_cache_write=use_cache_write,
                is_probe=is_probe,
            )
        # Apply unified post-processing here.
        return self._post_process_result(command, raw_response, post_process_params)


    def _post_process_result(self,
                             command: str,
                             response: dict,
                             post_process_params: Optional[Dict[str, Any]] = None) -> dict:
        """
        Post-process successful results for specific commands.
        """
        if self.is_null_or_empty_result(response):
            return response

        # Get command-specific parameters, or an empty dict if absent.
        cmd_params = (post_process_params or {}).get(command, {})

        # Post-processing for get_document_symbol.
        if command == "get_document_symbol":
            ori_results = response.get('result') or []
            prefer_selection_range = bool(cmd_params.get("prefer_selection_range", False))
            formatted_symbols = flatten_symbol_tree(
                ori_results,
                prefer_selection_range=prefer_selection_range)
            response['result'] = formatted_symbols

        # Post-processing for get_type_definition.
        if command == "get_type_definition":
            ori_results = response.get('result') or []
            formatted_symbols = format_type_definition_resp(ori_results)
            response['result'] = formatted_symbols

        return response

    def validate_symbol_at_line(
            self,
            file_path: str,
            target_name: str,
            start_line0: int,
            is_always_query_with_cache: bool = False,
            project_name_full=None,
            commit_sha=None,
            lang_id=None,
    ) -> bool:
        """
        Strictly validate that the flattened get_document_symbol result contains
        an item whose name equals `target_name` and whose start_line equals `start_line0 + 1`.
        """
        if not target_name or start_line0 is None:
            return False

        _project_name_full = project_name_full if project_name_full is not None else self.project_name_full
        _commit_sha = commit_sha if commit_sha is not None else self.commit_sha
        _lang_id = lang_id if lang_id is not None else self.main_lang_id
        resp = self.query_by_cache_flag(
            project_name_full = _project_name_full,
            commit_sha = _commit_sha,
            file_path = file_path,
            lang_id = _lang_id,
            command="get_document_symbol",
            params={},
            is_always_query_with_cache=is_always_query_with_cache,
        )

        if resp is None or self.has_error_or_warnings(resp) or self.is_null_or_empty_result(resp):
            return False

        items = resp.get("result") or []  # Already flattened in _post_process_result.
        want_line1 = int(start_line0) + 1  # Target line number is 1-based.

        for s in items:
            if isinstance(s, dict) and s.get("name") == target_name and s.get("start_line") == want_line1:
                return True
        return False

    @staticmethod
    def _validate_language_match(file_path: str, expected_lang: str) -> bool:
        """
        Validate whether the language inferred from the file suffix
        matches the expected primary language.

        :param file_path: File path to check, for example "src/main.c"
        :param expected_lang: Expected language.
        :return: bool
        """
        # If no expected language is provided, treat it as matched.
        if not expected_lang:
            return True

        # Use the helper from helpers.py.
        actual_lang = get_lang_from_path(file_path)

        if actual_lang is None:
            # Could not infer the language from the path, for example .txt, .md,
            # or files without an extension. Treat this as a mismatch.
            return False

        return actual_lang == expected_lang

    def close(self):
        """Close all underlying connections and clear in-memory cache."""
        self.caching_querier.close()
        # print(f"[{title}] All connections closed.")
