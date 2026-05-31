"""E2E tests: spawn the supervisor against a tmp-project fixture and
drive the real UI through Playwright.

Tests in this package require the ``e2e`` dependency group (Playwright
+ pytest-playwright) and ``playwright install chromium``. They are
auto-skipped when those preconditions are not met, so the default
``uv run pytest`` flow on a fresh checkout still goes green.

To run them locally:

    uv sync --all-extras --group e2e
    uv run playwright install chromium
    uv run pytest tests/integration/e2e/ -v
"""
