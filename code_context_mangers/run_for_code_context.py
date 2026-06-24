# run_for_code_context.py

from __future__ import annotations

import faulthandler
faulthandler.enable()

import os

from config import BASE_DIR
from code_analysis_lsp.lsp_config import REPOS_BASE_DIR
from code_context_mangers.code_context_config import ContextConfig

# --- Core constants and switches from the original manager/shared config ---
CODE_CONTEXT_MANAGER_DIR = f"{BASE_DIR}/code_context_mangers"
CACHE_OUTPUT_DIR = f"{CODE_CONTEXT_MANAGER_DIR}/_context_cache"
OUTPUT_DIR = f"{CODE_CONTEXT_MANAGER_DIR}/_context"

DEFAULT_MAX_WORKERS = 24

# 2026-05-18:
# Purpose:
# Rule-based semantic context variant used by the published context-generation pipeline.
version = "26A-FULL"

is_debug_for_semantic = False
is_debug_for_similar = False
is_debug_for_random = False

is_always_query_with_cache = True

# Runtime configuration for pipeline.py.
# Default configuration, can be overridden by pipeline.py.
CONTEXT_MODE = "neighborhood_semantic_similar"


def build_context_config(context_mode: str) -> ContextConfig:
    config_map = {
        "neighborhood": ContextConfig(
            use_neighborhood_context=True,
            use_semantic_context=False,
            use_similar_context=False,
        ),
        "semantic": ContextConfig(
            use_neighborhood_context=False,
            use_semantic_context=True,
            use_similar_context=False,
        ),
        "similar": ContextConfig(
            use_neighborhood_context=False,
            use_semantic_context=False,
            use_similar_context=True,
        ),
        "semantic_similar": ContextConfig(
            use_neighborhood_context=False,
            use_semantic_context=True,
            use_similar_context=True,
        ),
        "neighborhood_semantic": ContextConfig(
            use_neighborhood_context=True,
            use_semantic_context=True,
            use_similar_context=False,
        ),
        "neighborhood_similar": ContextConfig(
            use_neighborhood_context=True,
            use_semantic_context=False,
            use_similar_context=True,
        ),
        "neighborhood_semantic_similar": ContextConfig(
            use_neighborhood_context=True,
            use_semantic_context=True,
            use_similar_context=True,
        ),
    }
    if context_mode not in config_map:
        raise ValueError(f"Unknown CONTEXT_MODE: {context_mode}")
    return config_map[context_mode]


config_run = build_context_config(CONTEXT_MODE)

# Input task file.
tasks_file_path = f"{BASE_DIR}/_extended_data/1438_go_from_ref-test.jsonl"

# Base name used for cache generation.
base_name = os.path.splitext(os.path.basename(tasks_file_path))[0]

def cleanup_git_locks():
    """Remove leftover Git index lock files after interrupted runs."""
    import subprocess
    print("Cleaning Git index lock files...")
    cmd = f"find {REPOS_BASE_DIR} -type f -path '*/.git/index.lock' -delete 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0:
        print("Git lock file cleanup completed.")
    else:
        print(f"Git lock file cleanup may have failed: {result.stderr}")


if __name__ == "__main__":
    # Remove stale Git lock files left by interrupted runs.
    if not is_always_query_with_cache:
        cleanup_git_locks()

    from code_context_mangers.code_context_manager import CodeContentManager

    print("\n" + "=" * 50)
    print(f"--- Running Code Context Generation: {CONTEXT_MODE} ---")
    print("=" * 50)
    print(f"version = {version}")
    print(config_run)

    pkl_cache_path = None
    if config_run.use_neighborhood_context:
        print("\n" + "=" * 50)
        print("--- Stage 1: Generate / reuse Neighborhood cache ---")
        print("=" * 50)

        manager_stage_1 = CodeContentManager(
            tasks_file_path=tasks_file_path,
        )
        # Return the object-cache .pkl path.
        # pkl_cache_path = manager_stage_1.generate_neighborhood_cache_and_file()
        pkl_cache_path = f"{CACHE_OUTPUT_DIR}/1438_go_from_ref-test_neighborhood_blocks(26A).pkl"

    print("\n" + "=" * 50)
    print("--- Stage 2: Generate target context ---")
    print("=" * 50)

    # Stage 2 configuration.
    TREE_SITTER_PLAN_MODULE_DIR = f"{CODE_CONTEXT_MANAGER_DIR}/code_semantic_context/tree_sitter_planner"
    TREE_SITTER_OUTPUT_DIR = f"{TREE_SITTER_PLAN_MODULE_DIR}/output"
    TREE_SITTER_DEF_PLAN_FILE = f"{TREE_SITTER_OUTPUT_DIR}/1438_go_from_ref-test_plan(definition).csv"
    TREE_SITTER_REF_PLAN_FILE = f"{TREE_SITTER_OUTPUT_DIR}/1438_go_from_ref-test_plan(reference).csv"
    TREE_SITTER_RULE_PLAN_FILE = f"{TREE_SITTER_OUTPUT_DIR}/1438_go_from_ref-test_plan(rule).csv"
    TREE_SITTER_BOTH_PLAN_FILE = f"{TREE_SITTER_OUTPUT_DIR}/1438_go_from_ref-test_plan(both).csv"

    PLAN_FILE = TREE_SITTER_BOTH_PLAN_FILE

    config_stage_2 = config_run
    manager_stage_2 = CodeContentManager(
        tasks_file_path=tasks_file_path,
        config=config_stage_2,
        neighborhood_cache_path=pkl_cache_path,  # Reuse the cache only when neighborhood context is enabled.
        llm_plan_file=PLAN_FILE,
    )
    manager_stage_2.generate_augmented_context_file()

    print("\n--- Context generation completed ---")
