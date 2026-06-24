# initialize_workspaces.py
# --------------------------------------------------
# Description:
#   Initialize projects in parallel with a thread pool to speed up warmup.
#   Record per-project processing time and display overall progress.
# --------------------------------------------------
import csv
import os
import subprocess
import json
import re
import sys
import time
import threading
from datetime import datetime
from collections import defaultdict
import zmq
from concurrent.futures import ThreadPoolExecutor, as_completed
from code_analysis_lsp.utils.helpers import run_command,get_lang_from_path,find_cs_solution_file


# This assumes the configuration file lives in the parent code_analysis_lsp package.
# Adjust the import if your layout is different.
from code_analysis_lsp.lsp_config import ZMQ_ENDPOINT, LANGUAGE_SERVERS, REPOS_BASE_DIR
from config import BASE_DIR

# --- Configuration ---

COMMENT_HISTORY_JSONL = f"{BASE_DIR}/_crawled_data/comment_change_history_aligned.jsonl"
SUPPORTED_LANGS = {"go", "python", "javascript", "ruby", "java", "php", "csharp"}

# Number of parallel workers; adjust based on available CPU cores.
MAX_WORKERS = 2

# Output path for the log file.
CACHE_DIR = f"{BASE_DIR}/code_analysis_lsp/_lsp_cache"
OUTPUT_CSV_PATH = os.path.join(CACHE_DIR, "base_commits_log.csv")

# --- Thread-safe printing ---
print_lock = threading.Lock()
def safe_print(*args, **kwargs):
    """A thread-safe print helper."""
    with print_lock:
        print(*args, **kwargs, flush=True)


# --- Parallelization: wrap single-project processing into one function ---
def process_project(project_name, project_data):
    """
    Run the full initialization flow for a single project.
    This function executes in its own thread.
    """
    start_time = time.time()
    safe_print(f"-> Starting processing for project: {project_name}")
    project_path = os.path.join(REPOS_BASE_DIR, project_name)

    if not os.path.isdir(project_path):
        return {'project_name': project_name, 'status': 'error', 'message': 'Directory not found'}

    # 1. Find the latest commit.
    latest_commit_sha, latest_date = None, None
    for commit in project_data['commits']:
        log = run_command(project_path, ["git", "show", "-s", "--format=%H %cI", commit], check=False)
        if not log: continue
        sha, date_str = log.split(' ', 1)
        current_date = datetime.fromisoformat(date_str.strip())
        if latest_date is None or current_date > latest_date:
            latest_date, latest_commit_sha = current_date, sha

    if not latest_commit_sha:
        return {'project_name': project_name, 'status': 'error', 'message': 'Could not determine latest commit'}

    # 2. Git checkout.
    # Parallel checkout across different directories is safe.
    run_command(project_path, ["git", "checkout", latest_commit_sha], check=False)
    safe_print(f"[{project_name}] Switched to base version.")

    langs_to_init = project_data['langs']

    # 3. C# special handling.
    solution_file_for_init = None
    if 'csharp' in langs_to_init:
        cs_paths = [p for p in project_data['paths'] if p.endswith('.cs')]
        found_solutions = {find_cs_solution_file(os.path.join(project_path, p), project_path) for p in cs_paths}
        found_solutions.discard(None)
        if found_solutions:
            solution_file_for_init = min(found_solutions, key=len)

    # --- 4. Send initialization requests via DEALER + Poller ---
    context = zmq.Context.instance()
    # Use DEALER instead of REQ.
    socket = context.socket(zmq.DEALER)

    # Set an identity for debugging.
    client_id = f"init-{os.getpid()}-{threading.get_ident()}".encode('utf-8')
    socket.setsockopt(zmq.IDENTITY, client_id)

    socket.connect(ZMQ_ENDPOINT)

    # Create a Poller.
    poller = zmq.Poller()
    poller.register(socket, zmq.POLLIN)

    success_langs = []
    for lang_id in langs_to_init:
        request = {"command": "initialize_project", "project_path": project_path, "lang_id": lang_id}
        if lang_id == 'csharp':
            if not solution_file_for_init: continue
            request['solution_file'] = solution_file_for_init

        safe_print(f"[{project_name}] Sending initialization request for {lang_id.upper()}...")

        try:
            # Send the request.
            socket.send_json(request)

            # Wait for the acknowledgment response.
            timeout_ms = 30 * 1000
            socks = dict(poller.poll(timeout=timeout_ms))

            if socket in socks and socks[socket] == zmq.POLLIN:
                response = socket.recv_json()
                if "error" not in response:
                    success_langs.append(lang_id)
                    safe_print(
                        f"[{project_name}] SUCCESS: Initialization request for {lang_id.upper()} acknowledged by manager.")
                else:
                    safe_print(
                        f"[{project_name}] ERROR: Manager failed to acknowledge task for {lang_id.upper()}. Response: {response}",
                        file=sys.stderr)
            else:
                # Timeout.
                safe_print(
                    f"[{project_name}] ERROR: Timeout waiting for acknowledgment for {lang_id.upper()} from manager (waited {timeout_ms / 1000}s).",
                    file=sys.stderr)

        except Exception as e:
            safe_print(f"[{project_name}] EXCEPTION during ZMQ communication for {lang_id.upper()}: {e}",
                       file=sys.stderr)

    socket.close()

    duration = time.time() - start_time
    log_entry = {
        'project_name': project_name,
        'base_commit_sha': latest_commit_sha,
        'base_commit_date': latest_date.isoformat(),
        'initialized_languages': ', '.join(sorted(list(langs_to_init))),
        'status': 'submitted' if success_langs else 'failure',
        'submission_time_seconds': round(duration, 2)
    }
    return log_entry


# --- Main logic (rewritten to support parallel execution) ---
def main():
    try:
        from tqdm import tqdm
    except ImportError:
        def tqdm(iterable, **kwargs):
            return iterable

    start_total_time = time.time()
    print("--- Starting Workspace Initialization (Parallel Mode) ---")
    os.makedirs(CACHE_DIR, exist_ok=True)

    # 1 & 2. Aggregate and filter data (kept unchanged).
    print(f"Step 1 & 2: Reading, filtering, and aggregating data...")
    all_entries, filtered_entries = [], []
    github_url_pattern = re.compile(r"https://api\.github\.com/repos/([^/]+)/([^/]+)/")
    with open(COMMENT_HISTORY_JSONL, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                data = json.loads(line)
                match = github_url_pattern.match(data['comment_url'])
                if not match: continue
                owner, repo = match.groups()
                entry = {"project_name": f"{owner}_{repo}", "commit_sha": data['original_commit_id'],
                         "path": data['path']}
                all_entries.append(entry)
                lang = get_lang_from_path(entry['path'])
                if lang and lang in SUPPORTED_LANGS:
                    entry['lang'] = lang
                    filtered_entries.append(entry)
            except (json.JSONDecodeError, KeyError):
                continue

    projects_data = defaultdict(lambda: {'commits': set(), 'paths': set(), 'langs': set()})
    for entry in filtered_entries:
        name = entry['project_name']
        projects_data[name]['commits'].add(entry['commit_sha'])
        projects_data[name]['paths'].add(entry['path'])
        projects_data[name]['langs'].add(entry['lang'])

    safe_print(f"-> Found {len(all_entries)} total entries, {len(filtered_entries)} are supported.")
    safe_print(f"-> Aggregated into {len(projects_data)} unique projects to process.")

    # 3. Initialize in parallel with a thread pool.
    total_projects = len(projects_data)
    safe_print(
        f"\nStep 3: Submitting initialization tasks for {total_projects} projects using up to {MAX_WORKERS} parallel workers...")

    commit_log = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_project = {executor.submit(process_project, name, data): name for name, data in projects_data.items()}

        # Use tqdm to monitor task submission progress.
        for future in tqdm(as_completed(future_to_project), total=total_projects, desc="Submitting Tasks"):
            project_name = future_to_project[future]
            try:
                result = future.result()
                commit_log.append(result)
            except Exception as exc:
                safe_print(f'\n--- FATAL ERROR: Project {project_name} generated an exception: {exc} ---',
                           file=sys.stderr)
                commit_log.append({'project_name': project_name, 'status': 'exception', 'message': str(exc)})

    safe_print(f"\nAll {total_projects} initialization tasks have been submitted to the manager.")
    safe_print("The manager will now process them sequentially in the background.")

    # 4. Write the log file.
    safe_print(f"\nStep 4: Writing submission log to {OUTPUT_CSV_PATH}...")
    if commit_log:
        fieldnames = ['project_name', 'status', 'submission_time_seconds', 'base_commit_sha', 'base_commit_date',
                      'initialized_languages', 'message']
        with open(OUTPUT_CSV_PATH, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(commit_log)
        safe_print("-> Log file created successfully.")

    total_duration = time.time() - start_total_time
    safe_print("\n--- Client-side Initialization Script Finished ---")
    safe_print(f"Total time taken to SUBMIT all tasks: {total_duration:.2f} seconds.")


if __name__ == "__main__":
    main()
