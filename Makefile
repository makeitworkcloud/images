.PHONY: help changed-images list-images list-images-json

SHELL := /bin/bash

# Find all directories containing a Containerfile
IMAGES := $(shell find . -maxdepth 2 -name Containerfile -printf '%h\n' | cut -d'/' -f2 | sort -u)

help:
	@echo "Available targets:"
	@echo "  list-images       - List all image directories (one per line)"
	@echo "  list-images-json  - List all image directories as JSON array"
	@echo "  changed-images    - List images with changes since HEAD~1 as JSON array"

# List images with changes since HEAD~1 as JSON array
changed-images:
	@changed=$$(git diff --name-only HEAD~1 HEAD 2>/dev/null | cut -d'/' -f1 | sort -u); \
	for img in $(IMAGES); do \
		echo "$$changed" | grep -qx "$$img" && echo "$$img"; \
	done | jq -R -s -c 'split("\n") | map(select(length > 0))'

# List all image directories (one per line)
list-images:
	@for img in $(IMAGES); do echo "$$img"; done

# List all image directories as JSON array
list-images-json:
	@echo '$(IMAGES)' | tr ' ' '\n' | jq -R -s -c 'split("\n") | map(select(length > 0))'
