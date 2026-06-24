# lsp_query_tool.py (updated as a command-line wrapper)
# --------------------------------------------------
# Description:
#   A lightweight command-line tool built on top of the LSPQuerier API.
# --------------------------------------------------
import argparse
import json
from lsp_querier import LSPQuerier


def main():
    parser = argparse.ArgumentParser(description="LSP Query CLI Tool")
    parser.add_argument("project_path", help="Absolute path to the project's git repository.")
    parser.add_argument("commit_sha", help="The git commit SHA to check out.")
    parser.add_argument("file_path", help="Relative path to the file within the project.")
    parser.add_argument("command", help="LSP command (e.g., 'definition', 'references').")

    # Collect all unknown arguments into a dict for params.
    # Example: --line 10 --character 5 --query "myFunc"
    # becomes {'line': '10', 'character': '5', 'query': 'myFunc'}.
    # Note: types still need manual conversion.
    args, unknown = parser.parse_known_args()

    params = {}
    for i in range(0, len(unknown), 2):
        key = unknown[i].lstrip('-')
        value = unknown[i + 1]
        # Try to convert the value to int or float.
        try:
            value = int(value)
        except ValueError:
            try:
                value = float(value)
            except ValueError:
                pass  # Keep it as a string.
        params[key] = value

    querier = LSPQuerier()
    try:
        result = querier.query(args.project_path, args.commit_sha, args.file_path, args.command, params)
        print(json.dumps(result.get("result", result), indent=2, ensure_ascii=False))
    finally:
        querier.close()


if __name__ == "__main__":
    main()
