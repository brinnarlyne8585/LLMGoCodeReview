import os
import subprocess
import sys
from code_analysis_lsp.lsp_config import LANGUAGE_SERVERS

def run_command(cwd, command, check=True):
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True,
            check=check, encoding='utf-8', errors='ignore'
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running command '{' '.join(command)}' in '{cwd}': {e.stderr.strip()}", file=sys.stderr)
        return None


def find_cs_solution_file(start_path, project_root):
    """
        Walk upward from ``start_path`` and locate a ``.sln`` or ``.csproj`` file.
        This version searches all parent directories for ``.sln`` first. Only if
        no ``.sln`` is found does it restart the walk for ``.csproj``.

        :param start_path: Absolute path of the starting file or directory.
        :param project_root: Absolute project-root path used as the upper bound.
        :return: Path to a ``.sln`` or ``.csproj`` file, or ``None`` if not found.
        """
    abs_project_root = os.path.abspath(project_root)

    # Determine the directory where the upward search starts.
    if os.path.isdir(start_path):
        current_path = os.path.abspath(start_path)
    else:
        current_path = os.path.dirname(os.path.abspath(start_path))

    # --- Pass 1: exhaustively search parent directories for a .sln file. ---
    search_path = current_path
    while abs_project_root in search_path or abs_project_root == search_path:
        try:
            for f in os.listdir(search_path):
                if f.endswith('.sln'):
                    # Return immediately once any solution file is found.
                    return os.path.join(search_path, f)
        except FileNotFoundError:
            pass  # Skip missing paths.

        if search_path == abs_project_root:
            break

        search_path = os.path.dirname(search_path)

    # --- Pass 2: if no .sln exists, restart and search for .csproj. ---
    search_path = current_path  # Reset the search path.
    while abs_project_root in search_path or abs_project_root == search_path:
        try:
            for f in os.listdir(search_path):
                if f.endswith('.csproj'):
                    return os.path.join(search_path, f)
        except FileNotFoundError:
            pass

        if search_path == abs_project_root:
            break

        search_path = os.path.dirname(search_path)

    return None


def get_lang_from_path(file_path):
    """Infer the lowercase language name from a file path."""
    extension = os.path.splitext(file_path)[1]
    extension = extension.strip()
    if not extension:
        return None
    for lang, config in LANGUAGE_SERVERS.items():
        if extension in config["extensions"]:
            return lang
    return None
