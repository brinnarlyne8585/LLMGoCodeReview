# Code Semantic Context

This module builds semantic code context for review-comment generation. It analyzes code changes, plans which semantic queries are needed, executes those queries through the LSP analysis layer, and converts the returned results into ranked context blocks that can be inserted into prompts.

## Structure

- `code_semantic_context_fetcher.py`: Main entry for fetching semantic context for a review task.
- `code_semantic_plan_executor.py`: Executes semantic query plans and collects LSP-backed results.
- `code_semantic_block_creator.py`: Converts query results into context blocks.
- `candidate_sorter.py`: Sorts and selects candidate context blocks.
- `semantic_replacement_finder.py`: Finds replacement candidates when semantic query results need fallback matching.
- `syntax_noncode_detector.py`: Filters non-code or syntactically irrelevant snippets.
- `code_semantic_context_model.py`: Data models used by the semantic context pipeline.
- `code_semantic_context_validator.py`: Validation helpers for generated semantic context.
- `tree_sitter_planner/`: Builds semantic query plans from Tree-sitter change analysis.
- `tree_sitter_planner/rule_matchers/`: Rule implementations for module-level, method-level, variable-level, and identifier-level semantic queries.
- `tree_sitter_planner/output/`: Prepared semantic plan files used by the published experiments.

## Dependencies

This module relies on the LSP analysis module for definition, reference, search, and symbol queries. Before using it, configure [code_analysis_lsp/lsp_config.py](/Volumes/Disk/LLMReviewer(Publish)/code_analysis_lsp/lsp_config.py) and make sure the repositories and commits to analyze are already available under `REPOS_BASE_DIR`.

The module does not clone repositories or install language-server tools. Repository preparation, language-server setup, and cache configuration should be completed before running semantic context generation.
