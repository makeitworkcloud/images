# Agent Instructions

## Repository Purpose

This is a container image monorepo. Each subdirectory with a `Containerfile` becomes a container image published to `ghcr.io/makeitworkcloud/<dir>:latest`.

## Push Access

Agents are authorized to push directly to `main` in this repository.

## Key Files

### tfroot-runner/pre-commit-config.yaml

This is the **canonical pre-commit configuration** for all `tfroot-*` repositories in the organization. When modifying pre-commit hooks:

1. Edit `tfroot-runner/pre-commit-config.yaml` in this repo
2. The shared-workflows OpenTofu workflow fetches this config at CI time
3. The tfroot-runner image pre-caches these hooks for faster CI runs

**Do not** modify `.pre-commit-config.yaml` files in individual `tfroot-*` repos - they source from here.

## Related Repositories

- `shared-workflows` - Contains reusable GitHub Actions workflows that reference the tfroot-runner image
- `tfroot-cloudflare`, `tfroot-libvirt`, `tfroot-github`, `tfroot-aws` - Terraform root module repos that use the tfroot-runner image via shared-workflows
