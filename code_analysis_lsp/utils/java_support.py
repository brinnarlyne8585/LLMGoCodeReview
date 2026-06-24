# utils/java_support.py
import os
from collections import defaultdict

from code_analysis_lsp.lsp_config import GRADLE_MIRRORS_INIT_PATH

BUILD_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle")

JAVA_STD_SEGMENTS = (
    "/src/main/java/",
    "/src/test/java/",
    "/src/it/java/",
    "/app/src/main/java/",
    "/app/src/test/java/",
    "/app/src/androidTest/java/",
)

def resolve_workspace_and_path(project_root: str, relative_path: str, lang_id: str):
    """
    Decide which root (`preferred_root`) should be used for this request
    and which file path (`target_path`) should be passed to LSP.

    Rules:
      - Non-Java: no special handling; keep the original root + relative_path.
      - Java with a file under a standard source path: still use the repo root
        (`project_root`) + the original relative_path.
      - Java outside the standard source layout: enter "simple project" mode,
          preferred_root = directory containing the file
          target_path    = absolute file path
    """
    if not (lang_id == "java" and relative_path):
        return project_root, relative_path  # Return unchanged.

    abs_file = os.path.abspath(os.path.join(project_root, relative_path))
    norm = abs_file.replace("\\", "/")

    is_std = any(seg in norm for seg in JAVA_STD_SEGMENTS)
    if is_std:
        # Still use the repo root; the original relative path is enough for target.
        return project_root, relative_path

    # simple-project: use the containing directory as root to avoid double path joining.
    preferred_root = os.path.dirname(abs_file)
    target_path = abs_file  # Passing an absolute path is the safest choice.
    return preferred_root, target_path

def detect_module_root(abs_file: str, repo_root: str):
    repo_root = os.path.abspath(repo_root)
    p = os.path.abspath(os.path.dirname(abs_file))
    while True:
        if any(os.path.exists(os.path.join(p, m)) for m in BUILD_MARKERS):
            return p
        if p == repo_root:
            return None
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent

class JavaWorkspace:
    """Manage state and actions for added module roots and simple-project configuration."""
    def __init__(self):
        self._added_workspace_folders = defaultdict(set)   # key: client_key, val: set(uris)
        self._configured_simple_projects = set()           # set of absolute paths

    def add_workspace_folder_if_needed(self, client, client_key, folder_path: str):
        folder_path = os.path.abspath(folder_path)
        uri = f"file://{folder_path}"
        if uri in self._added_workspace_folders[client_key]:
            return
        client.t.notify("workspace/didChangeWorkspaceFolders", {
            "event": {
                "added":   [{"uri": uri, "name": os.path.basename(folder_path)}],
                "removed": []
            }
        })
        try:
            client.t.request("workspace/executeCommand",
                             {"command": "java.project.refresh", "arguments": []},
                             timeout=30)
            client.wait_for_indexing_complete(timeout=60*3)
            self._added_workspace_folders[client_key].add(uri)
            print(f"[JDT] Added workspace folder: {folder_path}")
        except Exception as e:
            import sys
            print(f"[JDT] add_workspace_folder failed: {e}", file=sys.stderr)

    def ensure_simple_project_config(self, client, simple_root: str, repo_root: str):
        simple_root = os.path.abspath(simple_root)
        if simple_root in self._configured_simple_projects:
            return
        srcs = ["."]
        main_src = os.path.join(repo_root, "java", "src", "main", "java")
        if os.path.isdir(main_src):
            srcs.append(os.path.relpath(main_src, simple_root))

        try:
            client.t.notify("workspace/didChangeConfiguration", {
                "settings": {"java": {"project": {"sourcePaths": srcs}}}
            })
            client.t.request("workspace/executeCommand",
                             {"command": "java.project.refresh", "arguments": []},
                             timeout=30)
            client.wait_for_indexing_complete(timeout=60*3)
            self._configured_simple_projects.add(simple_root)
            print(f"[JDT] configured simple-project sourcePaths at {simple_root}: {srcs}")
        except Exception as e:
            import sys
            print(f"[JDT] configure simple-project failed: {e}", file=sys.stderr)

def build_initial_java_settings(project_java_home: str,
                                http_proxy_host: str, http_proxy_port: int,
                                socks_host: str, socks_port: int):
    """
    Build generic JDTLS settings for the initialization stage only
    (runtimes and Gradle import arguments).
    Note: this does not finalize sourcePaths; simple-project mode may override them at runtime.
    """
    return {
        "java": {
            "project": {"sourcePaths": ["."]},  # Safe default; simple-project mode can override this later.
            "configuration": {
                "runtimes": [
                    {"name": "ProjectJDK", "path": project_java_home, "default": True},
                ]
            },
            "import": {
                "gradle": {
                    "java": {"home": project_java_home},
                    "wrapper": {"enabled": True},
                    "offline": {"enabled": False},
                    "arguments": [
                        "-I", GRADLE_MIRRORS_INIT_PATH,
                        f"-DsocksProxyHost={socks_host}",
                        f"-DsocksProxyPort={socks_port}",
                        "-Dhttp.nonProxyHosts=",
                        f"-Dhttp.proxyHost={http_proxy_host}", f"-Dhttp.proxyPort={http_proxy_port}",
                        f"-Dhttps.proxyHost={http_proxy_host}", f"-Dhttps.proxyPort={http_proxy_port}",
                    ],
                }
            }
        }
    }
