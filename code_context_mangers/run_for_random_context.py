# run_for_random_context.py

from __future__ import annotations

import faulthandler
faulthandler.enable()

from code_context_mangers.run_for_code_context import (
    cleanup_git_locks,
    config_run,
    is_always_query_with_cache,
    tasks_file_path,
)
from code_context_mangers.code_context_config import ContextConfig

version = "26A-random-8x20"

# Override the default configuration for the random baseline.
config_run = ContextConfig(
    use_neighborhood_context=False,
    use_semantic_context=False,
    use_similar_context=False,
    use_random_context=True,
)


if __name__ == "__main__":
    if not is_always_query_with_cache:
        cleanup_git_locks()

    from code_context_mangers.code_context_manager import CodeContentManager
    print("\n" + "=" * 50)
    print("--- Running Random Context Baseline ---")
    print("=" * 50)
    print(f"version = {version}")
    print(config_run)

    manager = CodeContentManager(
        tasks_file_path=tasks_file_path,
        config=config_run,
        version_override=version,
    )
    manager.generate_augmented_context_file()

    print("\n--- Random context generation completed ---")
