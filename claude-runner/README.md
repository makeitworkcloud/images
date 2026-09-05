# Claude Code self-hosted runner image

Runs Anthropic's Claude Code **self-hosted environments** runner (`claude
self-hosted-runner`) — the Claude-native execution fleet for
claude.makeitwork.cloud cloud sessions.

Anthropic does not publish a pre-built runner image. Per their instructions,
every organization builds its own image around the `claude` binary; this is
ours, kept as close to upstream as possible.

## Upstream instructions — authoritative

Operational truth for this fleet lives in Anthropic's documentation. This
image and any deployment of it defer to these pages:

- [Self-hosted environments](https://code.claude.com/docs/en/self-hosted-environments) —
  how environments, runners, and sessions work; plan availability (public
  beta, Team and Enterprise) and limitations
- [Quickstart](https://code.claude.com/docs/en/self-hosted-environments-quickstart) —
  create the environment in the claude.ai admin UI, copy the environment key,
  start a runner, route a session to it
- [Deploy to production](https://code.claude.com/docs/en/self-hosted-environments-deploy) —
  **the page this Containerfile mirrors** ([Build the runner image](https://code.claude.com/docs/en/self-hosted-environments-deploy#build-the-runner-image)):
  hardening, network requirements, git credentials, and the official
  Kubernetes and Compose recipes
- [Customize sessions](https://code.claude.com/docs/en/self-hosted-environments-configuration) —
  lifecycle hooks, wrapper scripts, on-demand runners, MCP servers
- [Test end to end](https://code.claude.com/docs/en/self-hosted-environments-testing) —
  CI smoke test to run before promoting a new image
- [Reference](https://code.claude.com/docs/en/self-hosted-environments-reference) —
  every runner CLI flag, env var, metric, and the health endpoint
- [Verify session identity](https://code.claude.com/docs/en/self-hosted-environments-identity) —
  validating session tokens from adjacent services
- [Binary integrity and code signing](https://code.claude.com/docs/en/setup#binary-integrity-and-code-signing) —
  verifying the downloaded binary against the release's signed manifest

## What the image contains — and deliberately omits

Contains (upstream's minimal set):

- `debian:bookworm-slim` with `git`, `curl`, `ca-certificates`, `openssh-client`
- The `claude` binary at `/usr/local/bin/claude`, pinned via
  `CLAUDE_CODE_VERSION` from the standard release location
  (`downloads.claude.ai/claude-code-releases`)
- System-wide git identity and `safe.directory '*'`, per upstream

Omits, per upstream's hardening guidance (["No broad credentials in the
image"](https://code.claude.com/docs/en/self-hosted-environments-deploy#harden-your-deployment)):

- **No environment secret** — mounted at runtime as a Kubernetes Secret and
  passed via `--environment-secret-file` (their recipe)
- **No SSH keys, tokens, or git credentials** — configured per their
  [Configure git](https://code.claude.com/docs/en/self-hosted-environments-deploy#configure-git)
  options at deploy time

## Version pinning

Each session's child process runs the runner's own binary, and the runner
disables auto-update inside the sessions it spawns. Upgrade the fleet by
bumping `CLAUDE_CODE_VERSION`, rebuilding, and restarting the runners — see
[Pin the version](https://code.claude.com/docs/en/self-hosted-environments-deploy#pin-the-version).
`2.1.224` is the minimum release that recognizes the `self-hosted-runner`
subcommand.

## Deviations from upstream

Recorded here so drift stays visible:

1. Upstream guards `ARG CLAUDE_CODE_VERSION` with `:?` and passes it via
   `--build-arg`; this Containerfile carries a pinned default so the repo's
   buildah workflow builds without extra arguments.
2. House `LABEL` lines added per repo convention.

Everything else mirrors the docs' Dockerfile verbatim. Do not add further
drift without recording it in this section.

## Requirements from Anthropic's docs

- Plan: public beta on Team and Enterprise; an Owner must enable **Allow
  self-hosted environments** on the Cloud environments admin page
- Claude Code ≥ 2.1.224 and git ≥ 2.24; `linux-x64` build (arm64/musl
  variants documented upstream)
- Outbound HTTPS to `api.anthropic.com` and the hosts in their
  [network requirements table](https://code.claude.com/docs/en/self-hosted-environments-deploy#network-requirements);
  Anthropic makes no inbound connections
- NTP-synced clock — authentication fails when the clock is more than five
  minutes off
- Billing: sessions consume the organization's Claude Code usage, the same as
  Anthropic-hosted environments (subscription path, not per-token API)

## Ownership and delivery

`makeitworkcloud/images` owns this source image; its `main` workflow publishes
`ghcr.io/makeitworkcloud/claude-runner` after merge. Deployment wiring —
chart/Deployment, environment Secret, egress policy for the fleet — is not
yet authored; `charts` and `kustomize-cluster` ownership follows separately.
Authored, published, selected, and healthy remain distinct delivery stages.
