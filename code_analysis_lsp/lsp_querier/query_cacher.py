# caching_querier.py
# --------------------------------------------------
# Description:
#   Provides a unified cached LSP querier.
#   This is intended to be the single entry point for future code analysis queries.
#   It owns an internal LSPQuerier instance and automatically handles cache logic.
# --------------------------------------------------
import sqlite3
import json
import os
import sys
import time
import uuid

# Make sure lsp_querier can be imported.
sys.path.append(os.path.abspath(os.path.dirname(__file__)))
from code_analysis_lsp.lsp_config import LSP_CACHE_DB_PATH, REPOS_BASE_DIR
from lsp_querier import LSPQuerier

title = "CachingQuerier"

class CachingQuerier:

    def __init__(self):
        """
        Initialize the smart querier.
        - Connect to the persistent cache database.
        - Create the underlying real LSPQuerier instance.
        """
        # --- Cache database setup ---
        db_dir = os.path.dirname(CACHE_DB_PATH)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self.conn = sqlite3.connect(CACHE_DB_PATH, check_same_thread=False, timeout=30)
        # WAL mode allows concurrent reads/writes and reduces "database is locked" errors.
        self.conn.execute("PRAGMA journal_mode=WAL")
        with self.conn:
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS lsp_cache (
                    project_name TEXT, commit_sha TEXT, file_path TEXT,
                    command TEXT, params_json TEXT, result_json TEXT,
                    PRIMARY KEY (project_name, commit_sha, file_path, command, params_json)
                )
            ''')

        # --- Hold the real querier instance ---
        self.real_querier = LSPQuerier()

    def _serialize_key(self, project_name_full, commit_sha, file_path, command, params):
        params_json = json.dumps(params, sort_keys=True)
        return project_name_full, commit_sha, file_path, command, params_json

    def _get_from_cache(self, key):
        # Create a dedicated cursor inside the method for thread safety.
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT result_json FROM lsp_cache WHERE project_name=? AND commit_sha=? AND file_path=? AND command=? AND params_json=?",
            key)
        row = cursor.fetchone()
        cursor.close()

        # Validate row and row[0] before parsing.
        if row and row[0]:
            return json.loads(row[0])
        return None

    def _set_to_cache(self, key, result):
        result_json = json.dumps(result)
        # Retry on database lock errors.
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with self.conn:
                    self.conn.execute('''
                        INSERT OR REPLACE INTO lsp_cache
                        (project_name, commit_sha, file_path, command, params_json, result_json)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (*key, result_json))
                return  # Return on success.
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < max_retries - 1:
                    # Database is locked; wait and retry.
                    wait_time = 0.1 * (2 ** attempt) + (time.time() % 0.1)  # Exponential backoff + jitter.
                    time.sleep(wait_time)
                else:
                    raise  # Final retry failed, or this is another error.

    def query_cache_only(self,
                         project_name_full,
                         commit_sha,
                         file_path,
                         command,
                         params,
                         lang_id = None):
        """
        Read results only from the cache database.
        If the cache hits, return the cached value even if it is an empty list or null.
        If the cache misses, return None directly without executing a real query.
        """
        # print(f"[{title}] Querying CACHE ONLY for: {file_path}...")
        key = self._serialize_key(project_name_full, commit_sha, file_path, command, params)
        response = self._get_from_cache(key)
        return response

    def _mk_trace_id(self, owner_repo, sha, path, cmd):
        return f"{owner_repo}|{sha[:8]}|{os.path.basename(path)}|{cmd}|{uuid.uuid4().hex[:8]}"

    def query(self,
              project_name_full,
              commit_sha,
              file_path,
              command,
              params,
              use_cache_read=True,
              use_cache_write=True):
        """
        Execute one smart query and handle cache interaction automatically.

        :param use_cache_read: Whether to attempt reading from cache (default True).
        :param use_cache_write: Whether to write the result to cache after a real query (default True).
        ... (other parameters match the previous query_project behavior)
        """

        # Convert "owner/repo" to "owner_repo" to match on-disk directory names.
        project_dir_name = project_name_full.replace('/', '_')
        project_abs_path = os.path.join(REPOS_BASE_DIR, project_dir_name)

        key = self._serialize_key(project_name_full, commit_sha, file_path, command, params)

        # 1. Check the cache.
        if use_cache_read:
            cached_result = self._get_from_cache(key)
            if cached_result is not None:
                # return cached_result
                result_value = cached_result.get("result")
                # Only treat it as a valid hit when the result field exists and is non-empty.
                if result_value is not None and result_value != []:
                    print(f"[CachingQuerier] Cache HIT for: {project_name_full} {commit_sha} {file_path} {command} {params}")
                    return cached_result
                else:
                    # The record exists in the database, but null / [] is treated as stale and forces a re-query.
                    print(f"[CachingQuerier] Stale cache found for {project_name_full} {commit_sha} {file_path} {command} {params}. Forcing re-query.")

        # 2. Cache miss or cache bypass; execute a real query.
        print(f"[{title}] Cache MISS for: {project_name_full} {commit_sha} {file_path} {command} {params}. Querying LSP server...")
        try:
            trace_id = self._mk_trace_id(project_name_full,
                                         commit_sha,
                                         file_path,
                                         command)

            response = self.real_querier.query(
                project_path=project_abs_path,
                commit_sha=commit_sha,
                file_path=file_path,
                command=command,
                params=params,
                trace_id=trace_id,
            )
        except Exception as e:
            # RuntimeError, such as a Git checkout failure, should be raised directly and not cached.
            if isinstance(e, RuntimeError):
                raise
            exc_info = f"{e.__class__.__name__}: {e}"
            response = {"error": f"EXCEPTION: {exc_info}"}

        # 3. Write the result to cache.
        if use_cache_write:
            self._set_to_cache(key, response)

        return response


    def close(self):
        """Close the cache database and the underlying querier connection."""
        self.conn.close()
        self.real_querier.close()
        # print(f"[{title}] All connections closed.")
