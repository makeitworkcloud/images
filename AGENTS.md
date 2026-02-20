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

## CI/CD Workflows

### buildah.yml (Build)

Builds container images and pushes to `ghcr.io/makeitworkcloud/<image>:latest`.

- Runs on `ubuntu-latest` (not self-hosted runners)
- Triggered on push to `main` or manual dispatch
- Manual dispatch accepts optional `image` input to build a specific image

**Known issues:**
- Transient network failures can occur when downloading tools (e.g., OpenTofu installer hitting GitHub API). Re-run the workflow if you see SSL/connection errors.

### pull.yml (Pull)

Imports images from GHCR to the internal OpenShift registry after successful Build.

- Runs on `arc` self-hosted runners (requires OpenShift connectivity)
- Imports to `image-registry.openshift-image-registry.svc:5000/public-registry/<image>:latest`

**Known issues:**
- The `|| true` at the end of the import command masks failures. Always verify import succeeded by checking logs for actual image metadata output (not "Unable to connect" errors).
- If import fails, re-run the Pull workflow or manually run: `oc import-image <image>:latest --from=ghcr.io/makeitworkcloud/<image>:latest -n public-registry --confirm --reference-policy=local`

## Renaming Images

When renaming an image (e.g., `runner` → `tfroot-runner`):

1. Rename the directory in this repo
2. Update all references in `shared-workflows` and consumer repos
3. After successful build, verify Pull workflow imports to OpenShift
4. Manually delete the old package from GHCR (requires `delete:packages` scope): https://github.com/orgs/makeitworkcloud/packages

## Related Repositories

- `shared-workflows` - Contains reusable GitHub Actions workflows that reference the tfroot-runner image
- `tfroot-cloudflare`, `tfroot-libvirt`, `tfroot-github`, `tfroot-aws` - Terraform root module repos that use the tfroot-runner image via shared-workflows
