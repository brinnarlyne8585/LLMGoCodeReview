# git_querier.py
# --------------------------------------------------
# Description:
#   Provides a GitQuerier class dedicated to stateless, read-only Git operations.
#   Examples include `git show`, `git ls-tree`, and `git grep`.
#   These operations do not mutate the working tree, require no lock, and can run efficiently in parallel.
# --------------------------------------------------
import os
import re
import sys
from code_analysis_lsp.utils.helpers import run_command

title = "GitQuerier"
class GitQuerier:
    """Wrap read-only Git query operations."""

    def get_file_content(self, project_path, commit_sha, file_path):
        """
        Read file content directly from Git at a specific commit.
        This is a pure read-only operation (`git show`) and does not mutate
        the working tree, so it requires no locking and can run in parallel.

        :param project_path: Absolute path to the project.
        :param commit_sha: Commit SHA to inspect.
        :param file_path: File path relative to the project root.
        :return: A dict containing file content or an error.
        """
        if not os.path.isdir(project_path):
            print(f"[{title}] Error: Project path not found: {project_path}")
            return {"error": f"Project path not found: {project_path}"}

        # print(f"[{title}] Getting content for {file_path} at commit {commit_sha[:7]} (no lock needed)...")

        # Use `git show` to read file content directly with the form `commit:path/to/file`.
        git_spec = f"{commit_sha}:{file_path}"
        content = run_command(project_path, ["git", "show", git_spec], check=False)

        if content is None:
            print(f"[{title}] Error: Failed to get content for '{file_path}' at commit '{commit_sha}'. It may not exist.")
            return {"error": f"Failed to get content for '{file_path}' at commit '{commit_sha}'. It may not exist."}

        return {"content": content}

    def list_files_in_directory(self, project_path: str, commit_sha: str, directory_path: str) -> dict:
        """
        Read the file list for a directory directly from Git at a specific commit.
        This is a pure read-only operation (`git ls-tree`) and does not mutate
        the working tree, so it requires no locking and can run in parallel.

        :param project_path: Absolute path to the project.
        :param commit_sha: Commit SHA to inspect.
        :param directory_path: Directory path relative to the project root. Use '' for the repository root.
        :return: A dict containing the file list or an error. e.g. {"files": ["path/a.py", "path/b.py"]}
        """
        if not os.path.isdir(project_path):
            print(f"[{title}] Error: Project path not found: {project_path}")
            return {"error": f"Project path not found: {project_path}"}

        if not commit_sha:
            print(f"[{title}] Error: Commit SHA must be provided.")
            return {"error": "Commit SHA must be provided."}

        # Use `git ls-tree` to list content precisely for a commit and path.
        # It also works when the path is an empty string, meaning the repo root.
        command = ["git", "ls-tree", "--name-only", commit_sha, directory_path.rstrip("/") + "/"]
        output = run_command(project_path, command, check=False)

        if output is None:
            print(f"[{title}] Error: Failed to list files for '{directory_path}' at commit '{commit_sha}'. Path may not exist.")
            # run_command returns None on execution failure.
            return {"error": f"Failed to list files for '{directory_path}' at commit '{commit_sha}'. "
                             f"Path may not exist."}

        # `ls-tree --name-only` returns full paths relative to the repo root.
        files = output.splitlines()

        return {"files": files}

    def search_text_in_commit(self, project_path: str, commit_sha: str, query: str) -> dict:
        """
        Run a plain-text search with `git grep` inside a specific commit.
        This is a pure read-only operation, so it does not mutate the working
        tree, requires no locking, and can run in parallel.

        :param project_path: Absolute path to the project.
        :param commit_sha: Commit SHA to search.
        :param query: Plain-text query string.
        :return: A dict containing the search results or an error.
                 e.g., {"results": [{"file_path": "a.py", "line_number": 10, "line_content": "..."}, ...]}
        """
        if not os.path.isdir(project_path):
            print(f"[{title}] Error: Project path not found: {project_path}")
            return {"error": f"Project path not found: {project_path}"}
        if not commit_sha or not query:
            print(f"[{title}] Error: Commit SHA and query must be provided.")
            return {"error": "Commit SHA and query must be provided."}

        # print(f"[{title}] Searching for '{query}' at commit {commit_sha[:7]} (no lock needed)...")

        try:
            # Build the git grep command:
            # -n: include line numbers
            # --heading: print the file name before matches in each file
            # -F: treat query as a fixed string instead of a regex
            # -w: require query to match as a standalone word
            # <commit_sha>: search inside this commit
            command = ["git", "grep", "-n", "--heading", "-F", "-w", query, commit_sha]
            output = run_command(project_path, command, check=False)

            if output is None:
                # Command execution failed.
                print(f"[{title}] Error: git grep command failed for query '{query}'.")
                return {"error": f"git grep command failed for query '{query}'."}

            if not output.strip():
                # No match found; return an empty list.
                return {"results": []}

            # Parse the git grep output.
            results = []
            current_file = ""
            # Match lines of the form "number:anything".
            content_pattern = re.compile(r'^(\d+):(.*)$')

            for line in output.strip().split('\n'):
                if not line:
                    continue

                # Remove possible leading/trailing whitespace added by git grep.
                clean_line = line.strip()

                match = content_pattern.match(clean_line)
                if match:
                    # Case 1: this is a content line, e.g. "25:CTU..."
                    if not current_file:
                        continue  # Content line found before a file name; skip the orphan line.

                    line_number = int(match.group(1))
                    line_content = match.group(2)
                    results.append({
                        "file_path": current_file,
                        "line_number": line_number,
                        "line_content": line_content
                    })
                else:
                    # Case 2: this is a file-name line, e.g. "SHA:path/to/file.py".
                    # Remove the SHA prefix.
                    parts = clean_line.split(':', 1)
                    if len(parts) == 2:
                        # parts[0] is the SHA, parts[1] is the file path.
                        current_file = parts[1]
                    else:
                        # Fallback in case the output does not include the SHA prefix.
                        current_file = clean_line

            return {"result": results}
        except Exception as e:
            print(f"[{title}] Error: An unexpected error occurred during search: {e}")
            return {"error": f"An unexpected error occurred during search: {e}"}
