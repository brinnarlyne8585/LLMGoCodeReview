# Code Analysis LSP

This module provides LSP-based code analysis utilities for retrieving code context from local repositories. It is mainly used to query definitions, references, document symbols, type definitions, hover information, and related source-code structure needed by the review-comment generation pipeline. If you want to analyze specific repositories and commits, those repositories must already be available under `REPOS_BASE_DIR`; this module can switch commits for analysis requests, but it does not clone, crawl, or download repositories.

## Structure

- `lsp_config.py`: Central configuration for repository paths, cache paths, ZMQ endpoint, workspace paths, and language-server commands.
- `lsp_client/`: Low-level LSP client and transport code. It starts language servers, sends LSP requests, and handles protocol-level communication.
- `lsp_manager/`: Long-running manager process for initializing LSP clients, switching commits, synchronizing files, and serving query requests through ZMQ.
- `lsp_querier/`: User-facing query layer. It wraps Git read-only queries, cached LSP queries, and post-processing helpers.
- `init/`: Optional workspace warmup scripts for pre-initializing repositories and language servers.
- `utils/`: Shared helpers for language detection, command execution, Java workspace handling, and related support code.

## Configuration

Before using this module, configure [lsp_config.py](lsp_config.py).

At minimum:

- Set `REPOS_BASE_DIR` to the directory that contains the repositories to analyze.
- Make sure each target repository already exists under `REPOS_BASE_DIR`.
- Make sure the commits you want to analyze exist in those local repositories. The manager can switch commits during analysis, but repository preparation is outside this module.
- Configure the language-server tools used by your target languages, such as `GOPLS_ROOT_PATH`, `PYLSP_ROOT_PATH`, `NODE_BIN_PATH`, `TSLS_BIN_PATH`, `JDTLS_BIN_PATH`, `INTELEPHENSE_BIN_PATH`, `DOTNET_ROOT_PATH`, and `OMNISHARP_DLL_PATH`.
- Configure `LSP_CACHE_DB_PATH`, `ZMQ_ENDPOINT`, and `WORKSPACE_ROOT` for the local runtime environment. By default, `LSP_CACHE_DB_PATH` points to the bundled `code_analysis_lsp/lsp_query_cache.db`.

The module assumes language-server binaries and runtime dependencies are already installed. It does not download or install LSP tools automatically.
