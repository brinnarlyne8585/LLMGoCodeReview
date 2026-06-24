# Code Context Managers

This module builds code context used by the review-comment generation pipeline. It prepares neighborhood, semantic, consistency/similar, and random context blocks for each review task, deduplicates and renders them, and writes the generated context files used by the published experiments.

## Structure

- `run_for_code_context.py`: Main entry for generating neighborhood, semantic, similar, and combined context variants.
- `run_for_random_context.py`: Entry for generating the random-context baseline.
- `code_context_manager.py`: Coordinates task loading, project/commit grouping, cache reuse, context fetching, and CSV output.
- `code_context_orchestrator.py`: Consolidates context blocks from different sources, removes overlaps, and renders final context strings.
- `code_context_renderer.py`: Loads generated context CSV files and renders prompt-ready file-change context.
- `code_context_config.py`: Defines which context sources are enabled for a run.
- `code_context_utils.py`: Shared hunk-parsing helpers and data structures.
- `code_surrounding_context/`: Builds neighborhood context around the reviewed snippet.
- `code_semantic_context/`: Builds LSP-backed semantic context such as definitions, references, and related symbols.
- `code_consistency_context/`: Builds similar-code context by ranking related code blocks for consistency.
- `code_random_context/`: Builds random-code baseline context.
- `_context/`: Generated context CSV files used by experiments.
- `_context_cache/`: Reusable cache files, mainly for neighborhood context blocks.

## Dependencies

This module relies on the LSP analysis module for semantic and consistency context. Before running it, configure [code_analysis_lsp/lsp_config.py](../code_analysis_lsp/lsp_config.py) and make sure the repositories and commits to analyze are already available under `REPOS_BASE_DIR`.

To reproduce the published experiments, keep `is_always_query_with_cache = True` in `run_for_code_context.py` and use the bundled `code_analysis_lsp/lsp_query_cache.db`. In this mode, the pipeline reads cached LSP query results and does not require starting the live LSP manager.

If you need to run new data or perform live LSP queries, start the background manager first:

```bash
python3 -m code_analysis_lsp.lsp_manager.lsp_server_manager
```

The input review tasks are expected to be available under the configured project data paths. This module does not clone repositories or install language-server tools; repository preparation, LSP setup, and path configuration must be completed before context generation.
