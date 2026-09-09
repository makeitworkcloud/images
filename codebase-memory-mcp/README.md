# codebase-memory-mcp

Container image for [DeusData/codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) — a pure-C, self-contained stdio MCP server that indexes repositories into a SQLite knowledge graph: tree-sitter parsing across 162 languages, bundled local embeddings, structural/semantic/BM25 search, and read-only Cypher queries. No language runtime, database service, or API keys.

## Purpose

Backend for the `makeitwork-codebase-memory` ToolHive `MCPServer` in `kustomize-cluster/workloads/mcp-gateway`, succeeding the `makeitwork-repo-search` filesystem backend (owner decision 2026-09-09). It consumes the existing `mcp-repo-cache` read-only.

## Build notes

- Uses the fully static `linux-amd64-portable` release asset; the runtime base carries no library dependency on the binary.
- `CBM_VERSION` and `CBM_TARBALL_SHA256` are pinned ARGs; the hash is copied from the upstream release's official `checksums.txt`. Bump both together.
- `--ui=false` is baked into the ENTRYPOINT and must be preserved: upstream auto-enables the embedded graph-UI HTTP listener (loopback :9749) on first run when the cache directory has no UI config, which is every start on an emptyDir-backed `CBM_CACHE_DIR`.
- amd64 only (single-node k3s); an arm64 build would use `codebase-memory-mcp-linux-arm64-portable.tar.gz`.

## Runtime contract

Set by the consuming `MCPServer`, not baked into the image: `CBM_ALLOWED_ROOT`, `CBM_CACHE_DIR`, `CBM_RUNTIME_DIR`, `CBM_WORKERS`, `CBM_MEM_BUDGET_MB`, and a writable `HOME`. The binary makes no outbound network requests.

## Update procedure

1. Check [upstream releases](https://github.com/DeusData/codebase-memory-mcp/releases) for a new version.
2. Copy the new `codebase-memory-mcp-linux-amd64-portable.tar.gz` SHA-256 from that release's `checksums.txt`.
3. Bump both ARGs in the `Containerfile` and open a PR.

Merge publishes `ghcr.io/makeitworkcloud/codebase-memory-mcp:{latest,<sha>}`. Upstream moves quickly (~10 releases in three weeks as of 2026-09); expect index rebuilds after version changes.
