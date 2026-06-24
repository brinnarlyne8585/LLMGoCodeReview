import re
from typing import Optional, List, Dict, Any, Tuple
from code_analysis_lsp.lsp_querier.git_querier import GitQuerier

class TreeSitterUtils:
    @staticmethod
    def get_file_content_at_commit(project_path: str, commit_sha: str, file_path: str) -> Optional[str]:
        """
        Use GitQuerier to get file content at a specific commit.
        """
        querier = GitQuerier()
        result = querier.get_file_content(project_path, commit_sha, file_path)
        if result and "content" in result:
            return result["content"]
        return None

    @staticmethod
    def parse_hunk_lines(hunk: str) -> Tuple[List[int], List[int]]:
        """
        Parse a diff hunk and identify changed line numbers in old and new files.
        Returns:
            (old_lines, new_lines): two lists of 0-based line numbers.
        """
        old_lines = []
        new_lines = []
        try:
             # Basic unified-diff header regex: @@ -old_start,old_count +new_start,new_count @@
            header_regex = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
            
            lines = hunk.splitlines()
            if not lines:
                return [], []

            header_match = header_regex.match(lines[0])
            if not header_match:
                return [], []

            old_start = int(header_match.group(1))
            new_start = int(header_match.group(3))
            
            current_old = old_start
            current_new = new_start
            
            for line in lines[1:]:
                if line.startswith("-"):
                    old_lines.append(current_old - 1) # 0-based
                    current_old += 1
                elif line.startswith("+"):
                    new_lines.append(current_new - 1) # 0-based
                    current_new += 1
                elif not line.startswith("\\"): # Ignore "No newline at end of file".
                    current_old += 1
                    current_new += 1
        except Exception as e:
            print(f"Error while parsing hunk: {e}")
            return [], []

        return old_lines, new_lines
