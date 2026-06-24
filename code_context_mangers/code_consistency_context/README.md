# Code Consistency Context

This module builds code-consistency context for review-comment generation. It searches surrounding files from the same review task, partitions them into code blocks, ranks those blocks by similarity to the changed lines, and returns the most relevant blocks as prompt context.

## Structure

- `code_consistency_context_fetcher.py`: Main entry for fetching consistency context for a review task.
- `code_partitioner.py`: Splits source files into code blocks using LSP document symbols when available, with a fallback path that partitions the whole file.
- `block_ranker.py`: Scores candidate blocks with TF-IDF similarity, applies filtering and deduplication, and selects the top-ranked blocks.
- `__init__.py`: Package marker.

## Dependencies

This module relies on the LSP analysis module for document-symbol queries when available. Before using it, configure [code_analysis_lsp/lsp_config.py](/Volumes/Disk/LLMReviewer(Publish)/code_analysis_lsp/lsp_config.py) and make sure the repositories and commits to analyze are already available under `REPOS_BASE_DIR`.

When LSP results are unavailable, the module can fall back to whole-file partitioning, but repository preparation and file access still need to be configured beforehand.
