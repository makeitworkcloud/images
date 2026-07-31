# Agent Instructions

## Repository Purpose

Container image monorepo. Every direct child directory with a `Containerfile` becomes an image at `ghcr.io/makeitworkcloud/<dir>`.

## Change Workflow

Work on a branch and open a PR; do not push directly to `main` or push any
branch unless explicitly requested. A successful push to `main` publishes
affected images to GHCR with `latest` and commit SHA tags, so treat merges as
production publication.

## Images

- **`tfroot-runner/`** — gha-runner-scale-set runner image layered on `ghcr.io/actions/actions-runner`. Carries kubectl, kustomize, sops, ansible-core, openssh, pre-commit, OpenTofu, tflint, terraform-docs, infracost, checkov, hcledit, tfupdate, yq, jq.
- **`gh-cli/`** — Alpine + `gh` for short-lived automation Jobs (e.g., the ArgoCD postsync token sync).

## Canonical Pre-commit Config

`tfroot-runner/pre-commit-config.yaml` is the source of truth for pre-commit hooks across every `tfroot-*` repo. The runner image pre-caches its hook environments; the shared OpenTofu workflow in `shared-workflows` fetches it at CI time. Secret scanning uses Gitleaks alongside `detect-private-key`.

**Do not** edit `.pre-commit-config.yaml` files in individual `tfroot-*` repos — they pull from here.

## Build Workflow (`buildah.yml`)

Single workflow, three jobs, all on `ubuntu-latest`.

1. **checks** — runs pre-commit, including full-tree Gitleaks scanning,
   hadolint, and actionlint, for every `main` push, pull request, and manual
   dispatch.
2. **detect** — enumerates which images to build:
   - `workflow_dispatch` with `image` input → just that one
   - `workflow_dispatch` with no input → all images (`make list-images-json`)
   - push/PR → only directories changed since the previous commit (`make changed-images`)
3. **build** — after checks pass, fan out over the detected image matrix:
   - install buildah and podman
   - `redhat-actions/buildah-build@v2` with `--squash`
   - on `push` to `main`, or `workflow_dispatch` with `mode=build & push`, push to GHCR with tags `latest` and `${{ github.sha }}`

PRs and `workflow_dispatch` with `mode=build` build but do not push.

## Makefile

- `make list-images` — newline-separated list of directories with a `Containerfile`
- `make list-images-json` — same as a JSON array
- `make changed-images` — JSON array of directories that changed in the previous commit

These targets are the contract the `detect` job depends on.

## Adding an Image

1. `mkdir <name>` and add a `Containerfile`
2. Open a PR — confirm the build matrix picks up the new directory
3. Merge — image publishes at `ghcr.io/makeitworkcloud/<name>:latest`

## Image Pull Pattern

Workloads pull directly from `ghcr.io/makeitworkcloud/<image>:latest` (or `:<sha>` for pinned references). The k3s nodes have anonymous pull access to public GHCR packages; private packages need a pull secret in the consuming namespace.

## Known Issues

- Transient SSL/network failures while downloading toolchain binaries (OpenTofu, hadolint) can break the build. Re-run the workflow.
- `--squash` on the buildah build means each image is one big layer — fine for our scale, but expect full rebuilds when any layer-affecting input changes.

## Related Repositories

- `shared-workflows` — reusable GitHub Actions workflows that consume the `tfroot-runner` image
- `tfroot-aws`, `tfroot-cloudflare`, `tfroot-github`, `tfroot-libvirt` — IaC roots that run on the `tfroot-runner` image via `shared-workflows`
- `kustomize-cluster` — runs `gh-cli` in a postsync Job to sync the cluster SA token to GitHub Actions secrets
