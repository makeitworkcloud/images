# images

Container image monorepo. Each subdirectory containing a `Containerfile` is built and published to `ghcr.io/makeitworkcloud/<dir>:latest` (and a SHA-tagged sibling).

## Images

| Directory | Base | Purpose |
|---|---|---|
| `tfroot-runner/` | `ghcr.io/actions/actions-runner` (Ubuntu) | gha-runner-scale-set runner with the OpenTofu IaC toolchain (kubectl, kustomize, sops, ansible, pre-commit, tflint, terraform-docs, infracost, checkov) |
| `gh-cli/` | `alpine:3.21` | Minimal `gh` image for automation Jobs |

## How It Works

```
push to main ─▶ detect changed images ─▶ pre-commit + hadolint ─▶ buildah build ─▶ push to GHCR
```

`workflow_dispatch` accepts an optional `image` input to rebuild a single image; with no input it builds all images.

The detect step uses the `Makefile` (`make changed-images` / `make list-images-json`) to enumerate directories that contain a `Containerfile`.

## Adding an Image

1. Create `<name>/Containerfile`
2. Open a PR — the build runs in PR mode (no push)
3. Merge to `main` — the image publishes to `ghcr.io/makeitworkcloud/<name>:latest` and `:<sha>`

## Canonical Pre-commit Config

`tfroot-runner/pre-commit-config.yaml` is the **canonical pre-commit configuration** for every `tfroot-*` repository. It is:

1. Pre-cached into the runner image at build time so hooks don't re-fetch on every CI run
2. Fetched at CI time by the shared OpenTofu workflow in `shared-workflows`

To change pre-commit hooks across all `tfroot-*` repos, edit this file and merge to `main`.

## License

GPLv3
