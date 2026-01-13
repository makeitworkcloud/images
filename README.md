# images

Container image monorepo. Each subdirectory with a `Containerfile` is built and pushed to `ghcr.io/makeitworkcloud/<dir>:latest`.

## Structure

```
<image-name>/
├── Containerfile
└── ...
```

## How It Works

1. Push to `main` triggers build for changed images only
2. Images are linted with hadolint, built with buildah, pushed to GHCR
3. After push, `pull.yml` imports images to OpenShift via Cloudflare WARP

## Adding an Image

1. Create `<name>/Containerfile`
2. Push to `main`
3. Image publishes to `ghcr.io/makeitworkcloud/<name>:latest`

## Images

| Directory | Description |
|-----------|-------------|
| `tfroot-runner/` | Alpine-based IaC runner with OpenTofu, Checkov, pre-commit, SOPS, tflint, terraform-docs. Used by all `tfroot-*` repos. |
| `gh-cli/` | GitHub CLI image |

## Pre-commit Configuration

The `tfroot-runner/pre-commit-config.yaml` file is the **canonical pre-commit configuration** for all `tfroot-*` repositories. This config is:

1. Bundled into the container image to pre-cache hook environments
2. Fetched at CI time by the shared OpenTofu workflow

To update pre-commit hooks for all tfroot repos, modify `tfroot-runner/pre-commit-config.yaml` and push to main.
