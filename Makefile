.PHONY: list-images list-images-json changed-images help

SHELL := /bin/bash

# Find all directories containing a Containerfile
IMAGES := $(shell find . -maxdepth 2 -name Containerfile -printf '%h\n' | cut -d'/' -f2 | sort -u)

# Registry configuration
REGISTRY ?= ghcr.io/makeitworkcloud

help:
	@echo "Available targets:"
	@echo "  list-images       - List all image directories (one per line)"
	@echo "  list-images-json  - List all image directories as JSON array"
	@echo "  changed-images    - List images with changes since HEAD~1 as JSON array"
	@echo "  build IMAGE=name  - Build a specific image"
	@echo "  build-all         - Build all images"

list-images:
	@for img in $(IMAGES); do echo "$$img"; done

list-images-json:
	@echo '$(IMAGES)' | tr ' ' '\n' | jq -R -s -c 'split("\n") | map(select(length > 0))'

changed-images:
	@changed=$$(git diff --name-only HEAD~1 HEAD 2>/dev/null | cut -d'/' -f1 | sort -u); \
	for img in $(IMAGES); do \
		echo "$$changed" | grep -qx "$$img" && echo "$$img"; \
	done | jq -R -s -c 'split("\n") | map(select(length > 0))'

build:
ifndef IMAGE
	$(error IMAGE is required. Usage: make build IMAGE=runner)
endif
	buildah build --squash -t $(REGISTRY)/$(IMAGE):latest $(IMAGE)

build-all:
	@for img in $(IMAGES); do \
		echo "Building $$img..."; \
		$(MAKE) build IMAGE=$$img; \
	done
