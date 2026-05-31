"""Root pytest conftest: register the ``--run-e2e`` opt-in and the
matching auto-skip for ``@pytest.mark.e2e`` tests.

Why this lives at the root rather than under ``tests/integration/e2e/``:
``pytest_addoption`` is only honoured when it appears in a plugin or a
root-level conftest. Nested conftests register their CLI flags too late
for ``pytest --help`` and (sometimes) for upstream option parsing,
which means a user running ``uv run pytest --run-e2e`` from the
project root would hit an "unrecognized arguments" error if the hook
lived inside the e2e subpackage.

Why E2E is opt-in: pytest-playwright's sync Playwright loop persists
its greenlet event loop across the session, which collides with the
``anyio.run(...)`` calls inside the unit + integration suites and
provokes ``RuntimeError: Already running asyncio in this thread``.
Running E2E in a separate pytest invocation isolates the two loops.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _wide_plain_terminal_for_rich_assertions(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin a wide, colour-less terminal so Rich emits stable plain text.

    Several CLI tests assert ``'--mode' in result.stdout`` against Typer
    help output. Typer renders help through Rich, which on CI:

      1. Defaults to ~80 cols (no ``COLUMNS``), wrapping long flag tokens.
      2. Emits ANSI escape codes (``\\x1b[1m--\\x1b[0m\\x1b[36mmode\\x1b[0m``)
         that split flag tokens across colour spans, so a literal
         ``'--mode'`` substring match against the captured stdout fails
         even though the rendered text contains the flag.

    Both behaviours are environment-dependent (a developer's iTerm has
    wide columns + still emits ANSI under CliRunner, so locally the
    assertions pass by luck). The dashboard's CLI surface is identical
    in both environments; the tests' substring checks are what's
    brittle. The env knobs:

      - ``COLUMNS=200`` widens the panel so flag tokens fit on one line.
      - ``NO_COLOR=1`` (https://no-color.org) tells Rich to skip ANSI
        styling, so the captured stdout is plain ASCII.
      - ``TERM=dumb`` belt-and-braces for tools that ignore NO_COLOR.

    Opt-out: tests that DELIBERATELY exercise Rich's colour SGR output
    (e.g. ``tests/unit/cli/test_render.py``) can opt out with
    ``@pytest.mark.keeps_rich_color``, after which Rich's own
    ``force_terminal=True`` arg fully drives the colour decision.
    """
    if request.node.get_closest_marker("keeps_rich_color") is not None:
        return
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-e2e`` opt-in flag for the E2E suite."""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help=(
            "Run E2E tests under tests/integration/e2e/ "
            "(otherwise they are skipped to avoid pytest-playwright + "
            "pytest-asyncio event-loop interference). Equivalent to "
            "setting RUN_E2E=1 in the environment."
        ),
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Auto-skip ``@pytest.mark.e2e`` tests unless the opt-in is on.

    Opt-in is satisfied when ``--run-e2e`` is on the CLI OR when the
    ``RUN_E2E=1`` env var is set. Either way, Playwright must also be
    importable (it ships in the ``e2e`` dependency group).
    """
    try:
        import playwright  # type: ignore[import-not-found,unused-ignore]  # noqa: F401
    except ImportError:
        playwright_available = False
    else:
        playwright_available = True

    explicitly_enabled = (
        config.getoption("--run-e2e", default=False)
        or os.environ.get("RUN_E2E") == "1"
    )
    if explicitly_enabled and playwright_available:
        return

    if not playwright_available:
        reason = (
            "Playwright not installed. To run E2E tests: "
            "`uv sync --all-extras --group e2e && uv run playwright install chromium && "
            "uv run pytest tests/integration/e2e/ --run-e2e`."
        )
    else:
        reason = (
            "E2E tests skipped by default. Pass --run-e2e or set RUN_E2E=1 to enable. "
            "Run them in a separate `uv run pytest tests/integration/e2e/ --run-e2e` "
            "invocation to avoid event-loop interference with the unit + integration "
            "suites."
        )
    skip_mark = pytest.mark.skip(reason=reason)
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip_mark)
