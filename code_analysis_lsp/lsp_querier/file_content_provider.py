# file_content_provider.py
from typing import List, Dict, Optional, Tuple
from threading import RLock

from code_analysis_lsp.lsp_querier.git_querier import GitQuerier


class FileContentProvider:
    """
    A second-level cache bound to (project_path, commit_sha), keyed by file_path.
    It stores the raw file content to avoid repeated git show calls and splitting.
    """
    def __init__(self,
                 querier: GitQuerier,
                 project_path: str,
                 commit_sha: str):
        self.querier = querier
        self.project_path = project_path
        self.commit_sha = commit_sha
        self._content_cache: Dict[str, str] = {}  # file_path -> content
        self._lock = RLock()

    def get_file_content(self, file_path: str)-> str:
        """
        Get file content as a string, using and populating the cache.
        This method is thread-safe and uses double-checked locking to avoid duplicate fetches.
        """

        file_path = file_path.replace(self.project_path+"/", "")

        # 1. First check without locking; this is the fastest path.
        content = self._content_cache.get(file_path)
        if content is not None:
            return content

        # 2. Second check with the lock if the first check misses.
        with self._lock:
            # Check again in case another thread loaded the content while we were waiting.
            content = self._content_cache.get(file_path)
            if content is not None:
                return content

            # 3. The cache is still empty, so perform lazy loading.
            #    Keep this inside the lock so only one thread performs the I/O.
            resp = self.querier.get_file_content(
                project_path=self.project_path,
                commit_sha=self.commit_sha,
                file_path=file_path
            )
            content = resp.get("content") or ""

            # 4. Store the loaded content in the cache.
            self._content_cache[file_path] = content
            return content

    def get_lines(self, file_path: str) -> List[str]:
        """
        Get file lines as a list of strings, reusing the get_file_content cache.
        """
        file_path = file_path.replace(self.project_path+"/", "")
        content = self.get_file_content(file_path)
        return content.splitlines()

    def has_content_in_git(self, file_path: str) -> bool:
        """
        Return True only if Git can read the file and the content remains non-empty after stripping whitespace.
        """
        file_path = file_path.replace(self.project_path+"/", "")
        content = self.get_file_content(file_path)  # Returns a string when found; it may still be empty.
        return bool(content and content.strip())
