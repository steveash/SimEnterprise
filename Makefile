# Thin wrappers over the canonical commands — scripts/gate.sh stays the single
# source of truth for the quality gate; this file only saves keystrokes.
.PHONY: help setup gate check test lint fmt golden smoke

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

setup: ## install dev dependencies (uv sync --extra dev)
	uv sync --extra dev

gate: ## the one rule: format + lint-fix + mypy + pytest (run before finishing)
	./scripts/gate.sh

check: ## CI mode: verify-only gate (no auto-fix)
	./scripts/gate.sh --check

test: ## full pytest suite (keyless, ~20s)
	uv run pytest

lint: ## ruff lint only (verify)
	uv run ruff check .

fmt: ## ruff format + lint --fix
	uv run ruff format . && uv run ruff check --fix .

golden: ## regenerate + eval the deterministic golden run
	uv run enterprise-sim run examples/golden.toml
	uv run enterprise-sim eval runs/golden/golden-slice-co-6c66fbef69f8

smoke: ## real-LLM runtime import smoke (bench extra, no key needed)
	uv sync --extra dev --extra bench
	uv run python scripts/import_smoke.py
