# OpenCode Alpine Runtime Prototype

Owner and maintainer: makeitworkcloud. Architectural review: ADVANCE for a
bounded prototype only, not production rollout or publication approval.

## Image Contract

Thin derivative of the chart's official OpenCode image:
`ghcr.io/anomalyco/opencode:1.18.29@sha256:ecc3bf96ee55dad226d9cde50d79aaa8a1215c47860c0fcdc71570461bf438b8`.
Preserves the upstream compiled musl runtime and `ENTRYPOINT ["opencode"]`.
The chart can continue passing `web` arguments. No chart defaults change.

Only official apk runtime prerequisites are added: bash, CA certificates,
gcompat, git, libgcc, libstdc++, Node >=22.13, and ripgrep. Node's built-in
SQLite FTS5 is a build assertion. No Python/compiler toolchain, standalone
Bun, custom ONNX build, Debian fallback, foreign repositories, or unofficial
glibc graft. UID/GID 1000 and HOME `/home/opencode` are the runtime defaults.
`CONTEXT_MODE_DIR=/home/opencode/.local/share/context-mode` avoids the masked
config directory. Runtime rootfs can be read-only with writable HOME and /tmp.

Upstream's Dockerfile says `FROM alpine`, not a release number. The exact
Alpine release inside the pinned digest has NOT been inspected by executing
it during authoring. The CI report reads `/etc/alpine-release` and resolved
apk versions from the actual built image; review those before promotion.
The official Alpine 3.24 source branch currently defines Node 24.18.1 and
gcompat 1.1.0-r4, including the x86_64 `ld-linux-x86-64.so.2` loader. Those
source observations are NOT claims about the digest's installed packages or
proof that gcompat implements every ONNX-required glibc symbol. The build
uses only the pinned base's existing repositories and stops if unavailable.
No exact apk revisions are fabricated or pinned; rebuilding is not hermetic.

Standalone Bun is deliberately absent: `community/bun/APKBUILD` was not found
on the official 3.24 stable source branch. No complex alternate installer is
introduced. OpenCode's embedded Bun is not a standalone `bun` executable.
Node covers the context-mode subprocess runtime; standalone Bun/TypeScript
execution is an explicitly unvalidated gap.

## Plugin Lifecycle

No plugins, models, config, or tests are baked into the image. The image does
not activate anything. An externally managed config must explicitly opt in.
Only the disposable CI fixture enables `context-mode@1.0.169` and
`opencode-mem@2.26.0`, both as native OpenCode plugins, with no duplicate MCP.
OpenCode's existing npm/Arborist loader automatically resolves these exact
top-level versions into `$HOME/.cache/opencode/packages/<spec>/node_modules`.
Lifecycle scripts are disabled by that upstream loader. No startup apk,
external npm install, custom entrypoint, or manual dependency fix is added.

HOME is persistent; `~/.config/opencode` is separately mounted, writable,
and disposable, analogous to the chart's seeded config emptyDir. Replacing
that mount must not discard the HOME plugin/model caches. The fixture uses
`~/.opencode-mem/data` for memory storage and its `.cache` for public model
artifacts, as defined by the pinned plugin's local Transformers backend.

Top-level versions do NOT freeze transitive ranges. Context-mode includes
`better-sqlite3 ^12.6.2`, SDK `^1.26.0`, and Zod `^3.25.0`; opencode-mem includes
Transformers `^4.2.0`, libsql `^0.17.4`, OpenCode plugin/SDK `^1.18.15`, and
Zod `^4.4.3`. ONNX is directly pinned to 1.20.1; nested install/override
behavior remains an upstream risk. Sharp and other native transitive
packages must work without rebuilds. SQLite built-ins avoid context-mode's
better-sqlite3 runtime path but do not prove the memory stack is compatible.
The model `Xenova/nomic-embed-text-v1` (768 dimensions) downloads on the cold
run. Its revision is NOT pinned. Cold public npm/model access and latency
are approved pilot limitations, not a reproducible or offline install claim.

## CI Gate

`buildah.yml` runs `tests/runtime.py` immediately after Build image, only for
`matrix.image == 'opencode-server'`, before any push. It uses Podman against
the same local Buildah store, resolves the built image to a local immutable
image ID, asserts linux/amd64 and inherited entrypoint, and uses `--pull=never`.
No shared workflow dependency. Failures and cleanup failures exit nonzero;
no continue-on-error. Existing checks, triggers, permissions, publication
conditions and attestation flow remain in place.

The existing last-commit image detection remains for other images. A scoped
union additionally selects opencode-server when its directory or buildah.yml
changes across the PR base-to-checkout or push-before-to-checkout range.
This catches workflow-only and multi-commit followups. Consequently a later
main merge of a workflow-only change can rebuild/publish this image under
the existing publication rules; review that consequence before any merge.

The gate requires:

1. Empty HOME plugin/model caches, a fresh config volume, UID/GID 1000,
   read-only root, dropped capabilities, no-new-privileges, tmpfs /tmp,
   loopback listeners and no published host ports. A named root helper with
   only CHOWN prepares test-owned volume permissions; it is not the server.
2. Both exact plugin specs in `/config`, and each required native tool once
   in `/experimental/tool/ids`, including memory and context execution/search.
3. Real memory `POST /api/memories`, then `/api/search?q=...&tag=...`, matching
   the created id, byte-equal content, and finite similarity >=0.6. This runs
   local CPU embeddings under the actual compiled OpenCode host.
4. Context-mode built-in SQLite FTS5, native shell execution, indexed tool
   search, and synthetic read/compaction state. This complementary probe
   directly imports the installed plugin under Node; it does NOT prove
   context tool invocation through an LLM or OpenCode's embedded runtime.
5. Stop/remove the cold server, discard config and /tmp, then start the SAME
   image and HOME with freshly seeded config and `--network=none`. Recall
   must return the same memory id/content/similarity contract. Context state
   and FTS search must persist. Plugin manifest mtimes/lock hashes and model
   artifact count/bytes/content hash must match before/after replacement.
6. UUID-named helpers, containers, and volumes cleaned without pruning or
   deleting any unrelated object. A 25-minute internal deadline leaves
   cleanup headroom under the 30-minute step timeout.

Reports cold config/plugin-ready, first write, first recall, total cold-to-
write, warm config/plugin-ready, warm recall, and total warm-to-recall seconds.
Memory starts background warmup at plugin initialization: first-write timing
includes any remaining warmup and is NOT an isolated model-load benchmark.
The report contains only image/runtime identity, apk versions, timings,
cache fingerprints/counts, similarity and fixed failure classifications.
No config/auth files, raw subprocess errors or container logs are printed.
A native-load suspicion flag is informational, not a diagnosis; every failed
assertion blocks the gate regardless of classification.

No providers are enabled, permissions default to deny, extraction/capture,
profile learning/cleanup and memory chat/compaction injection are disabled.
Only synthetic API data and context events are used. The web API token is a
public fixture, never an existing credential. No real secrets are read or
forwarded. Cold egress remains allowed for public npm/model downloads; the
harness is not a network allowlist or a sandbox for malicious dependencies.
Warm no-network testing intentionally fails if fresh config dependencies
cannot be satisfied from HOME; do not mask that persistence gap.

## Provenance And Review

Memory API/id/content/similarity and replacement design adapted from
[charts PR 113's probe](https://github.com/makeitworkcloud/charts/blob/a34d8ef1646470c5d24275660c72d314f63b617b/.github/tests/opencode_memory_runtime.py).
That stock-image run reached the real memory write and failed at the missing
loader; search/replacement were not reached. It is not a passing baseline.
This repository owns the new image and adapted tests.

Source contracts reviewed: OpenCode v1.18.29 Dockerfile, core npm/global,
plugin loader/registry and experimental tool API; context-mode release
`589d8214d56740a28b5f7bf63167743d586b0b40` package manifest, native adapter,
SQLite adapter and plugin tests; opencode-mem release
`0c8ed7d54382d9225def8484d691182d46e8552d` manifest, config, embedding backend,
and plugin entry/index. Context probes do not configure an MCP server.

Authoring performed through GitHub MCP only. No local validation, CI run,
PR creation, dispatch/rerun, publication, merge, chart change or rollout was
performed. Parent reviews this branch and opens the PR. Review native
hadolint/actionlint/pre-commit and the runtime build matrix result in that PR.
Inspect the actual Alpine/apk identity, failure phase, cold/warm timings,
cache reuse, and same-id recall evidence. No performance thresholds are
asserted beyond bounded deadlines; acceptability requires human review.

Any native compatibility, missing package, installation or offline-reuse
failure stops advancement. Do not add an alternate base, custom ONNX,
unofficial glibc, startup compiler, or fallback embedding provider to turn
the gate green. A separate owner decision is required before publication or
any chart/GitOps adoption. Passing this synthetic amd64 probe would not prove
ARM64, Kubernetes PVC/Service wiring, concurrent sessions, disaster recovery,
real conversation extraction, provider behavior, or production safety.
