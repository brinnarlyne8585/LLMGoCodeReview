# code_content_manager.py
from __future__ import annotations

import csv
import json
import os
import pickle
import re
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor
from dataclasses import dataclass, fields
from typing import Dict, Iterable, List, Optional

import pandas as pd

from code_analysis_lsp.lsp_querier.unified_querier import UnifiedQuerier
from code_analysis_lsp.utils.helpers import get_lang_from_path, run_command
from code_context_mangers.code_context_orchestrator import ContextBlock, ContextOrchestrator
from code_context_mangers.code_semantic_context.code_semantic_context_fetcher import SemanticContextFetcher
from code_context_mangers.code_surrounding_context.code_surrounding_context_fetcher import SurroundingContextFetcher
from code_context_mangers.code_consistency_context.code_consistency_context_fetcher import ConsistencyContextFetcher
from code_context_mangers.code_random_context.code_random_context_fetcher import RandomContextFetcher
from code_context_mangers.run_for_code_context import *
from code_context_mangers.run_for_code_context import is_always_query_with_cache as cache_flag

is_always_query_with_cache = cache_flag

import traceback
def _run_guarded(func, *args, **kwargs):
    """
    Run func in a subprocess, catch exceptions, and return the full traceback.
    Return shape:
      {"ok": True,  "value": <func return value>}
      {"ok": False, "exc_type": "...", "exc_msg": "...", "traceback": "..."}
    """
    try:
        value = func(*args, **kwargs)
        return {"ok": True, "value": value}
    except Exception as e:
        return {
            "ok": False,
            "exc_type": e.__class__.__name__,
            "exc_msg": str(e),
            "traceback": traceback.format_exc(),
        }

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable: Iterable, **kwargs):
        return iterable

GITHUB_URL_PATTERN = re.compile(r"https://api\.github\.com/repos/([^/]+)/([^/]+)/")
@dataclass
class ProjectInfo:
    owner: str
    repo: str
    @property
    def project_name(self) -> str:
        return f"{self.owner}_{self.repo}"
    @property
    def project_name_full(self) -> str:
        return f"{self.owner}/{self.repo}"

class CodeContentManager:

    def __init__(self,
                 tasks_file_path,
                 config: ContextConfig = None,
                 llm_plan_file: Optional[str] = None,
                 neighborhood_cache_path: Optional[str] = None,
                 version_override: Optional[str] = None,
                 debug_semantic_override: Optional[bool] = None,
                 debug_similar_override: Optional[bool] = None,
                 debug_random_override: Optional[bool] = None,
                 ):
        self.tasks_file_path = tasks_file_path
        self.config = config or ContextConfig()
        self.version_override = version_override
        self.debug_semantic = is_debug_for_semantic if debug_semantic_override is None else debug_semantic_override
        self.debug_similar = is_debug_for_similar if debug_similar_override is None else debug_similar_override
        self.debug_random = is_debug_for_random if debug_random_override is None else debug_random_override

        self.augmented_context_path: Optional[str] = None
        self._context_map: Dict[str, Dict[str, str]] = {}

        self.llm_plan_file = llm_plan_file
        self._llm_plans_map: Dict[str, List[Dict]] = {}
        if self.config.use_semantic_context and self.llm_plan_file:
            self._load_llm_plans()

        self.neighborhood_cache_path = neighborhood_cache_path
        self._neighborhood_cache: Dict[str, ContextBlock] = {}
        # Load the cache if a cache path is provided.
        if self.neighborhood_cache_path:
            self._load_neighborhood_cache()

    @staticmethod
    def _extract_project_details(comment_url: str) -> Optional[ProjectInfo]:
        if not comment_url or not isinstance(comment_url, str): return None
        m = GITHUB_URL_PATTERN.match(comment_url)
        if not m: return None
        return ProjectInfo(owner=m.group(1), repo=m.group(2))

    def _load_neighborhood_cache(self):
        if not self.neighborhood_cache_path or not os.path.exists(self.neighborhood_cache_path):
            print(f"Warning: Neighborhood object cache file does not exist: {self.neighborhood_cache_path}")
            return

        print(f"Loading neighborhood object cache from {self.neighborhood_cache_path}...")
        try:
            with open(self.neighborhood_cache_path, 'rb') as f:
                self._neighborhood_cache = pickle.load(f)
            print(f"Loaded {len(self._neighborhood_cache)} neighborhood object records.")
        except Exception as e:
            print(f"Error loading neighborhood object cache: {e}")

    def _load_llm_plans(self):
        """Load and parse the LLM-generated plan CSV file."""
        if not self.llm_plan_file or not os.path.exists(self.llm_plan_file):
            print(f"Warning: LLM plan file does not exist: {self.llm_plan_file}")
            return

        print(f"Loading LLM semantic plans from {self.llm_plan_file}...")
        try:
            df = pd.read_csv(self.llm_plan_file)
            for _, row in df.iterrows():
                comment_url = row.get('comment_url')
                plan_json_str = row.get('parsed_plan_list_json')
                if not comment_url or not plan_json_str or pd.isna(plan_json_str):
                    continue

                try:
                    # Parse the JSON string and extract the 'commands' list.
                    plan_data = json.loads(plan_json_str)
                    commands = plan_data.get("commands", [])

                    self._llm_plans_map[comment_url] = commands
                except json.JSONDecodeError:
                    print(f"Warning: Failed to parse plan JSON for comment_url '{comment_url}': {plan_json_str[:100]}...")

            print(f"Loaded {len(self._llm_plans_map)} valid LLM semantic plans.")
        except Exception as e:
            print(f"Error loading LLM plan file: {e}")

    def _build_hidden_hunk_mask_block(self, task: dict) -> Optional[ContextBlock]:
        """
        Treat `hunk_change` as a hidden neighborhood block so semantic and similar
        contexts do not duplicate the reviewed hunk.
        """
        hunk_change = task.get('hunk_change', '')
        path = task.get('path', '')
        hunk_range = SurroundingContextFetcher._parse_hunk_header_for_new_file(hunk_change)
        if not hunk_range or not path:
            return None

        hunk_start_line, hunk_count = hunk_range
        hunk_end_line = hunk_start_line + hunk_count - 1 if hunk_count > 0 else hunk_start_line

        return ContextBlock(
            start_line=hunk_start_line,
            end_line=hunk_end_line,
            source='neighborhood',
            path=path,
            description_above_path='[hidden pseudo-neighborhood mask for semantic overlap trimming]',
            content_lines=['[hidden reviewed hunk mask: overlap-trimming only]'],
        )

    # ====================================================================
    # Part 2: Context construction logic (offline generation)
    # ====================================================================

    def _group_tasks_by_project_and_commit(self, tasks):
        """
        Group tasks by project and commit, then sort commit bundles by commit timestamp in descending order.
        """
        grouped = {}
        ts_cache = {}  # (project_name, sha) -> ts

        for t in tasks:
            proj = self._extract_project_details(t.get('comment_url'))
            project_name = proj.project_name
            project_abs = f"{REPOS_BASE_DIR}/{project_name}"
            commit_sha = t.get('original_commit_id')

            if (project_name, commit_sha) not in ts_cache:
                out = run_command(project_abs, ["git", "show", "-s", "--format=%ct", commit_sha], check=False)
                ts_cache[(project_name, commit_sha)] = int(str(out).strip())

            if project_name not in grouped:
                grouped[project_name] = {
                    "project_abs": project_abs,
                    "project_name_full": proj.project_name_full,
                    "commits_map": defaultdict(list)
                }
            grouped[project_name]["commits_map"][commit_sha].append(t)

        for project_name, info in grouped.items():
            items = []
            for sha, lst in info["commits_map"].items():
                ts = ts_cache.get((project_name, sha), 0)
                items.append({"sha": sha, "ts": ts, "tasks": lst})
            items.sort(key=lambda x: x["ts"], reverse=True)
            info["commits"] = items
            del info["commits_map"]

        return grouped

    def _process_one_project_sequentially(self, project_name: str, info: dict, is_stage_1: bool):
        """
        Process all commits for one project sequentially in descending timestamp order.
        Reuse one UnifiedQuerier per commit to reduce checkout and LSP rebuild overhead.
        Return this project's result rows as list[dict].
        """
        results = []
        project_abs = info["project_abs"]
        project_name_full = info["project_name_full"]

        for bundle in info["commits"]:
            commit_sha = bundle["sha"]
            commit_tasks = bundle["tasks"]

            # 1) Collect languages involved in this commit.
            langs_in_commit = set()
            for t in commit_tasks:
                p = t.get('path')
                if not p:
                    continue
                lang = get_lang_from_path(p)
                if lang:
                    langs_in_commit.add(lang)

            # 2) Build one querier per language and reuse it within the commit.
            queriers = {}
            for lang in langs_in_commit:
                q = UnifiedQuerier(
                    project_name_full=project_name_full,
                    commit_sha=commit_sha,
                    main_lang_id=lang,
                    project_abs_path=project_abs
                )
                queriers[lang] = q

            # Prepare a default querier for files whose language cannot be detected.
            default_q = UnifiedQuerier(
                project_name_full=project_name_full,
                commit_sha=commit_sha,
                main_lang_id=None,
                project_abs_path=project_abs
            )

            # 3) Process tasks in original order and reuse queriers by language.
            try:
                for task in commit_tasks:
                    p = task.get('path')
                    lang = get_lang_from_path(p)
                    q = queriers.get(lang, default_q)

                    # Reuse the querier.
                    res = self._process_single_task_for_generation(
                                task,
                                is_stage_1=is_stage_1,
                                querier_override=q)
                    order_idx = task.get("_order_idx", -1)
                    results.append((order_idx, res))
            finally:
                # 4) Close queriers created for this commit.
                for q in queriers.values():
                    q.close()
                default_q.close()

        return results

    def _process_single_task_for_generation(self,
                                            task: dict,
                                            is_stage_1: bool = False,
                                            querier_override: Optional[UnifiedQuerier] = None) -> dict:
        """
        Fetch structured data, orchestrate it, and store the result.
        """
        querier = None
        try:
            # --- Step 0: basic setup ---
            result_row = {'comment_url': task.get('comment_url', ''),
                          'msg': task.get('msg', ''),
                          'hunk_change': task.get('hunk_change', '')
                          }
            proj = self._extract_project_details(task.get('comment_url'))

            task['project_name_full'] = proj.project_name_full
            project_abs = f"{REPOS_BASE_DIR}/{proj.project_name}"
            file_path = task.get('path')
            commit_sha = task.get('original_commit_id')
            lang_id = get_lang_from_path(file_path)

            # Initialize the querier.
            if querier_override is not None:
                querier = querier_override
            else:
                querier = UnifiedQuerier(
                    project_name_full=proj.project_name_full,
                    commit_sha=commit_sha,
                    project_abs_path=project_abs,
                    main_lang_id=lang_id
                )

            # Fetch the original file content.
            main_reviewing_file_content = querier.file_provider.get_file_content(file_path)
            if not main_reviewing_file_content:
                result_row['neighborhood_context'] = f"Error: failed to fetch original file content {file_path}"
                return result_row

            lsp_available = not is_always_query_with_cache

            # Instantiate the orchestrator.
            orchestrator = ContextOrchestrator(task = task, querier = querier)

            # Cache fetched blocks.
            existing_blocks = []

            # Fetch neighborhood block.
            neighborhood_block: Optional[ContextBlock] = None
            if self.config.use_neighborhood_context:
                if self._neighborhood_cache:
                    # Stage 2: read from cache.
                    comment_url = task.get('comment_url', '')
                    neighborhood_block = self._neighborhood_cache[comment_url]
                else:
                    # Stage 1, or Stage 2 cache miss: compute it.
                    neighborhood_fetcher = SurroundingContextFetcher(task = task,
                                                                     querier = querier,
                                                                     lsp_available = lsp_available)
                    neighborhood_block = neighborhood_fetcher.fetch_block()
                    hunk_change_with_sign = neighborhood_fetcher.fetch_formatted_hunk_with_signature()
                    result_row['_raw_neighborhood_block'] = neighborhood_block
                    result_row['hunk_change_with_sign'] = hunk_change_with_sign

                existing_blocks.append(neighborhood_block)
            elif self.config.use_semantic_context or self.config.use_similar_context:
                # Treat the reviewed hunk as a hidden neighborhood block to avoid
                # duplicate semantic/similar context when neighborhood context is disabled.
                hidden_hunk_mask_block = self._build_hidden_hunk_mask_block(task)
                if hidden_hunk_mask_block is not None:
                    existing_blocks.append(hidden_hunk_mask_block)

            semantic_blocks: List[ContextBlock] = []
            similar_blocks: List[ContextBlock] = []
            semantic_debug_log = ""
            similar_debug_log = {}

            # Fetch semantic blocks.
            if self.config.use_semantic_context:
                comment_url = task.get('comment_url', '')
                # Get the current task's plan from the loaded map.
                plan_for_task = self._llm_plans_map.get(comment_url, [])
                semantic_fetcher = SemanticContextFetcher(
                    task = task,
                    querier = querier,
                    llm_plan=plan_for_task,
                    lsp_available=lsp_available,
                    existing_blocks=existing_blocks,
                )
                semantic_blocks, semantic_debug_log = semantic_fetcher.fetch_blocks()

            # Fetch similar blocks.
            if self.config.use_similar_context:
                s_fetcher = ConsistencyContextFetcher(
                    task = task,
                    querier = querier,
                    lsp_available = lsp_available,
                    existing_blocks = existing_blocks,
                )
                similar_blocks, similar_debug_log = s_fetcher.fetch_blocks()

            # Fetch random blocks.
            random_blocks: List[ContextBlock] = []
            random_debug_log = {}
            if self.config.use_random_context:
                r_fetcher = RandomContextFetcher(
                    task=task,
                    querier=querier,
                )
                random_blocks, random_debug_log = r_fetcher.fetch_blocks()

            # --- Step 2: consolidate and render ---
            final_contexts = orchestrator.orchestrate(
                neighborhood_block,
                semantic_blocks,
                similar_blocks,
                random_blocks,
                render_only_target_hunk = is_stage_1,
            )

            # --- Store final contexts in result_row ---
            result_row.update(final_contexts)

            if self.config.use_semantic_context and self.debug_semantic and semantic_debug_log:
                result_row['semantic_debug_log'] = json.dumps(semantic_debug_log, ensure_ascii=False)

            if self.config.use_similar_context and self.debug_similar and similar_debug_log:
                result_row['similar_debug_log'] = json.dumps(similar_debug_log, ensure_ascii=False)

            if self.config.use_random_context and self.debug_random and random_debug_log:
                result_row['random_debug_log'] = json.dumps(random_debug_log, ensure_ascii=False)

            return result_row

        finally:
            # Close only queriers created here; do not close a reused querier_override.
            if querier_override is None and querier is not None:
                querier.close()

    # <-- Stage 1: generate Neighborhood cache and CSV file ---
    def generate_neighborhood_cache_and_file(self, max_workers: int = DEFAULT_MAX_WORKERS):
        """
        Stage 1: run only Neighborhood extraction, then write object cache (.pkl) and rendered CSV.
        """
        tasks_file_path = self.tasks_file_path
        print(f"--- Stage 1: generating Neighborhood cache from {tasks_file_path} ---")
        print(self.config.__str__())

        # Set cache file names.
        base_name = os.path.splitext(os.path.basename(tasks_file_path))[0]
        output_csv_path = f"{CACHE_OUTPUT_DIR}/{base_name}_neighborhood_cache({version}).csv"
        output_pkl_path = f"{CACHE_OUTPUT_DIR}/{base_name}_neighborhood_blocks({version}).pkl" # Object cache used by Stage 2.

        print(f"Rendered CSV cache will be saved to: {output_csv_path}")
        print(f"Reusable object Pkl will be saved to: {output_pkl_path}")

        if not os.path.exists(tasks_file_path):
            print(f"Error: task file does not exist at {tasks_file_path}")
            return

        with open(tasks_file_path, 'r', encoding='utf-8') as f:
            tasks = [json.loads(line) for line in f]

        # Attach original order index to each task.
        for idx, t in enumerate(tasks):
            t["_order_idx"] = idx

        grouped = self._group_tasks_by_project_and_commit(tasks)

        # Run projects in parallel and commits within each project sequentially; restore original order later.
        results_buffer: Dict[int, dict] = {}

        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_guarded,
                            self._process_one_project_sequentially,
                            proj, info, True): proj
                for proj, info in grouped.items()
            }
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Stage 1: generate Neighborhood per project"):
                proj = futures[fut]
                try:
                    res = fut.result()  # list[(idx, res)]
                    if res.get("ok"):
                        res_list = res["value"]
                        for idx, item in res_list:
                            if idx >= 0:
                                results_buffer[idx] = item
                    else:
                        tqdm.write(f"\n--- Exception details (project: {proj}) ---")
                        tqdm.write(f"{res['exc_type']}: {res['exc_msg']}")
                        tqdm.write(res["traceback"])
                        tqdm.write("----------------------------------------\n")
                except Exception as exc:
                    # Fallback for process-level failures, such as worker crashes.
                    tqdm.write(f"\n--- Process-level exception (project: {proj}) ---")
                    tqdm.write(repr(exc))
                    tb_str = traceback.format_exc()
                    tqdm.write(tb_str)
                    tqdm.write("----------------------------------------\n")

        # Restore input order.
        ordered_results = [results_buffer[i] for i in range(len(tasks)) if i in results_buffer]

        # Write the Stage 1 CSV columns.
        print(f"Writing {len(ordered_results)} results to rendered CSV cache...")
        os.makedirs(os.path.dirname(output_csv_path), exist_ok=True)
        # Write only columns relevant to this stage.
        fieldnames = ['comment_url', 'msg', 'hunk_change', 'hunk_change_with_sign', 'neighborhood_context']
        try:
            with open(output_csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(ordered_results)
            print(f"Rendered CSV cache saved: {output_csv_path}")
        except IOError as e:
            print(f"Error writing CSV cache: {e}")

        # 3. Extract ContextBlock objects and write the Pickle cache.
        print("Extracting ContextBlock objects for Pkl cache...")
        object_cache: Dict[str, ContextBlock] = {}
        for res in ordered_results:
            if '_raw_neighborhood_block' in res:
                comment_url = res.get('comment_url')
                block = res.get('_raw_neighborhood_block')
                if comment_url and block:
                    object_cache[comment_url] = block

        try:
            with open(output_pkl_path, 'wb') as f:
                pickle.dump(object_cache, f)
            print(f"Object Pkl cache saved: {output_pkl_path} ({len(object_cache)} records)")
        except IOError as e:
            print(f"Error writing Pkl cache: {e}")

        return output_pkl_path  # Return the pkl path for Stage 2.

    def generate_augmented_context_file(self, max_workers: int = DEFAULT_MAX_WORKERS):
        tasks_file_path = self.tasks_file_path
        print(f"Stage 2: augmenting context from {tasks_file_path} with {max_workers} workers...")
        print(self.config.__str__())

        effective_version = self.version_override or version
        self.augmented_context_path = self.config._get_dynamic_filepath(input_tasks_path=tasks_file_path,
                                                                        OUTPUT_DIR=OUTPUT_DIR,
                                                                        version=effective_version)
        print(f"--- Generating context file with config: {self.augmented_context_path} ---")
        print(f"--- Config: {self.config} ---")
        if self.neighborhood_cache_path:
            print(f"--- Reusing Neighborhood object cache: {self.neighborhood_cache_path} ---")

        if not os.path.exists(tasks_file_path):
            print(f"Error: task file does not exist at {tasks_file_path}")
            return

        with open(tasks_file_path, 'r', encoding='utf-8') as f:
            tasks = [json.loads(line) for line in f]

        # Attach original order index to each task.
        for idx, t in enumerate(tasks):
            t["_order_idx"] = idx

        grouped = self._group_tasks_by_project_and_commit(tasks)

        results_buffer: Dict[int, dict] = {}

        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_guarded, self._process_one_project_sequentially, proj, info, is_stage_1=False): proj
                for proj, info in grouped.items()
            }
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Stage 2: generate augmented context per project"):
                proj = futures[fut]
                try:
                    res = fut.result()  # list[(idx, res)]
                    if res.get("ok"):
                        res_list = res["value"]  # original list[(idx, res)]
                        for idx, item in res_list:
                            if idx >= 0:
                                results_buffer[idx] = item
                    else:
                        # Includes the full traceback from the subprocess.
                        tqdm.write(f"\n--- Exception details (project: {proj}) ---")
                        tqdm.write(f"{res['exc_type']}: {res['exc_msg']}")
                        tqdm.write(res["traceback"])
                        tqdm.write("----------------------------------------\n")
                except Exception as exc:
                    # Fallback for process-level failures, such as worker crashes.
                    tqdm.write(f"\n--- Process-level exception (project: {proj}) ---")
                    tqdm.write(repr(exc))
                    tb_str = traceback.format_exc()
                    tqdm.write(tb_str)
                    tqdm.write("----------------------------------------\n")

        # Restore input order.
        ordered_results = [results_buffer[i] for i in range(len(tasks)) if i in results_buffer]

        # Write CSV.
        print(f"Writing {len(ordered_results)} results to CSV...")
        os.makedirs(os.path.dirname(self.augmented_context_path), exist_ok=True)
        base_fieldnames = ['comment_url', 'msg', 'hunk_change']
        context_fieldnames = ContextOrchestrator.all_column_names()
        fieldnames = base_fieldnames + context_fieldnames + ['flat_context']
        if self.debug_semantic and self.config.use_semantic_context:
            debug_fieldnames = ['semantic_debug_log']
            fieldnames.extend(debug_fieldnames)
        if self.debug_similar and self.config.use_similar_context:
            debug_fieldnames = ['similar_debug_log']
            fieldnames.extend(debug_fieldnames)
        if self.debug_random and self.config.use_random_context:
            debug_fieldnames = ['random_debug_log']
            fieldnames.extend(debug_fieldnames)
        try:
            with open(self.augmented_context_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(ordered_results)
            print(f"Results saved: {self.augmented_context_path}")
        except IOError as e:
            print(f"Error writing CSV: {e}")
