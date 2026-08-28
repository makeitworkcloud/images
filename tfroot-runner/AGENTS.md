# tfroot-runner guidance

This directory owns the canonical OpenTofu CI runtime and pre-commit configuration consumed by `tfroot-*`, `terraform-libvirt-domain`, and `shared-workflows`.

Treat tool, hook, and image changes as compatibility changes. Identify affected workflows and consumers before editing. Verify image publication through CI before a dependent workflow relies on a new runtime capability. Do not copy these pins into downstream roots.
