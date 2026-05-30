# Repo Makefile — thin wrappers around the dev loop's most-common
# entry points so a contributor can ``make test``, ``make lint``,
# ``make audit-strings`` etc. without remembering the underlying
# tool incantation.
#
# Every target is uv-aware; ``uv run`` keeps the dev tools (pytest,
# ruff, mypy) pinned by the project's ``uv.lock``.

.PHONY: help test test-unit lint typecheck audit-strings format

help:  ## Print available targets.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-20s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

test:  ## Run the full test suite (unit + integration + cli).
	uv run pytest

test-unit:  ## Run only the fast unit tests.
	uv run pytest tests/unit/

lint:  ## Lint the source tree with ruff.
	uv run ruff check ctxr/fsm/ tests/

typecheck:  ## Type-check the package with mypy.
	uv run mypy ctxr/fsm/

format:  ## Auto-format with ruff.
	uv run ruff format ctxr/fsm/ tests/

audit-strings:  ## Guard against closed-vocabulary string literals (W14i).
	bash scripts/audit_strings.sh .
