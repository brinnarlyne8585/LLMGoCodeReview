# config.py
# --------------------------------------------------
# Description:
#   Centralized configuration file.
# --------------------------------------------------

import os

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- 1. Communication and workspace path settings ---

# Root directory that stores repository source code.
REPOS_BASE_DIR = ""

# LSP query cache database.
LSP_CACHE_DB_PATH = os.path.join(MODULE_DIR, "lsp_query_cache.db")

# ZMQ endpoint.
ZMQ_ENDPOINT = "ipc:///tmp/lsp_manager_frontend.ipc"

# Root directory for generated per-project caches and workspaces.
WORKSPACE_ROOT = ""

# --- 2. Language server configuration ---
GOPLS_ROOT_PATH = ""
PYLSP_ROOT_PATH = ""
NODE_BIN_PATH = ""
TSLS_BIN_PATH = ""
RBENV_ROOT_PATH = ""
DOTNET_ROOT_PATH = ""
JDTLS_BIN_PATH = ""
INTELEPHENSE_BIN_PATH = ""
OMNISHARP_DLL_PATH = ""

JAVA_HOME_DEFAULT_PATH = ""
JAVA_HOME_PATH = JAVA_HOME_DEFAULT_PATH
ANDROID_HOME_PATH = ""
GRADLE_GLOBAL_CACHE_PATH = ""
GRADLE_MIRRORS_INIT_PATH = ""


def _path_with_base(base_path, *parts):
    return os.path.join(base_path, *parts) if base_path else ""


def _prepend_paths(base_path, *relative_parts):
    paths = [_path_with_base(base_path, part) for part in relative_parts]
    paths = [path for path in paths if path]
    current_path = os.environ.get("PATH", "")
    if current_path:
        paths.append(current_path)
    return ":".join(paths)

LANGUAGE_SERVERS = {

    "go": {
        "extensions": [".go"],
        "server_cmd": [GOPLS_ROOT_PATH],
        "workspace_strategy": "NONE",
        "workspace_base_path": None,
        "env": None,
    },

    "python": {
        "extensions": [".py"],
        "server_cmd": [PYLSP_ROOT_PATH],
        "workspace_strategy": "NONE",
        "workspace_base_path": None,
        "env": None,
    },

    "javascript": {
        "extensions": [".js", ".jsx", ".ts", ".tsx"],
        "server_cmd": [NODE_BIN_PATH, TSLS_BIN_PATH, "--stdio"],
        "workspace_strategy": "NONE",
        "workspace_base_path": None,
        "env": None,
    },

    "ruby": {
        "extensions": [".rb"],
        # Use the absolute rbenv shim path.
        "server_cmd": [_path_with_base(RBENV_ROOT_PATH, "shims", "solargraph"), "stdio"],
        "workspace_strategy": "NONE",
        "workspace_base_path": None,
        "env": {
            # 1. Set the rbenv root directory.
            "RBENV_ROOT": RBENV_ROOT_PATH,
            # 2. Put rbenv shims and bin directories at the front of PATH.
            "PATH": _prepend_paths(RBENV_ROOT_PATH, "shims", "bin"),
            # 3. Remove conflicting environment variables by setting them to None.
            "GEM_HOME": None,
            "GEM_PATH": None,
            # 4. Force rbenv to use Ruby 3.3.4 for solargraph.
            "RBENV_VERSION": "3.3.4",
        },
    },

    "java": {
        "extensions": [".java"],
        "server_cmd": [
            JDTLS_BIN_PATH,
            "--jvm-arg=-Xms4g",  # Initial heap size: 4GB.
            "--jvm-arg=-Xmx16g",  # Maximum heap size: 16GB.
            "--jvm-arg=-XX:+UseParallelGC",  # Better for high-throughput indexing work.
            "-data",  # Placeholder; replaced dynamically.
        ],
        "workspace_strategy": "PER_PROJECT",
        "workspace_base_path": os.path.join(WORKSPACE_ROOT, "java"),
        "env": None,
    },

    "php": {
        "extensions": [".php"],
        "server_cmd": [
            INTELEPHENSE_BIN_PATH,
            "--stdio",
        ],
        "workspace_strategy": "PER_PROJECT",
        "workspace_base_path": os.path.join(WORKSPACE_ROOT, "php"),
        "env": None,
    },

    "csharp": {
        "extensions": [".cs"],
        "server_cmd": [
            # Use stdbuf to create a PTY-like buffered environment.
            "stdbuf", "-oL",
            _path_with_base(DOTNET_ROOT_PATH, "dotnet"),
            OMNISHARP_DLL_PATH,
            "--languageserver",
            "--stdio",
            "--loglevel", "debug",
        ],
        # For C#, the workspace is the project directory itself. The dotnet
        # toolchain creates caches such as obj/bin inside the project directory,
        # so the server must start from the project root.
        "workspace_strategy": "PER_PROJECT",
        "workspace_base_path": None,
        "env": {
            "DOTNET_ROOT": DOTNET_ROOT_PATH,
            "PATH": _prepend_paths(DOTNET_ROOT_PATH)
        },
    },
}
