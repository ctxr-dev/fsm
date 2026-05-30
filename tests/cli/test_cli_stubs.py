"""Tests for the W3 ``ctxr-fsm`` CLI stub commands.

The ``serve`` / ``mcp`` / ``api`` / ``ui`` subcommands are placeholders
for later workstreams (W7 / W4 / W5 / W6). Each one must:

1. Exit with a non-zero status so wrapping shell scripts that pipe
   through ``set -e`` fail loudly today rather than silently no-op'ing
   once the real implementation lands.
2. Print the documented "lands in W{N}" deferral message on stderr so
   stdout stays clean for callers that pipe into ``jq`` / similar.
3. Honour the documented per-command option validation today (``--mode``
   for ``serve``, ``--transport`` for ``mcp``, ``--port`` bounds for
   ``api`` / ``ui``) — anything accepted by the stub must also be
   accepted by the eventual real implementation, so a future change can
   never break operator scripts pinned against today's surface.

The tests below lock those guarantees in. They run through Typer's
``CliRunner`` to exercise the actual argv-parsing path, give each
invocation an isolated tempdir (the stubs do not actually open the
project DB — they fail before any IO — but the tempdir scaffolding
matches the convention used by sibling CLI tests and shields these
cases from any future change that starts touching the FS), and assert
on both the exit code and the deferral message wording so any silent
drift trips a test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ctxr.fsm.cli import app

# A single shared runner is fine — ``CliRunner.invoke`` is stateless
# between calls, so reusing one instance keeps the test bodies short
# without sharing any mutable state across cases.
runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_db(tmpdir: str) -> Path:
    """Resolve a project-DB path inside the test's tempdir.

    The W3 stubs do not actually open the project DB — they fail before
    any IO — but constructing this path keeps the test shape aligned
    with the convention in the brief (``tempfile.TemporaryDirectory()``
    for the project DB) so the next workstream can extend these tests
    without restructuring them.
    """
    return Path(tmpdir) / "fsm.db"


# ---------------------------------------------------------------------------
# ``serve`` — supervisor stub (W7)
# ---------------------------------------------------------------------------


def test_serve_stub_exits_nonzero_with_deferral_message() -> None:
    """``ctxr-fsm serve`` must defer to W7 and exit non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)  # reserve a path; stub does not consume it
        result = runner.invoke(app, ["serve"])

    # Non-zero exit is the loud-failure contract — see module docstring.
    assert result.exit_code != 0, (
        f"serve stub must exit non-zero (got {result.exit_code}); "
        f"stderr={result.stderr!r}"
    )
    # Wording match — runbooks may quote this string verbatim, so any
    # drift here is a docs-breaking change we want to catch in CI.
    assert "lands in W7" in result.stderr
    assert "supervisor" in result.stderr.lower()


def test_serve_stub_accepts_valid_mode_flag() -> None:
    """A valid ``--mode`` value reaches the stub body (still exits 1)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["serve", "--mode", "prod"])

    # Valid mode -> stub body runs -> deferral + exit 1.
    assert result.exit_code == 1
    assert "lands in W7" in result.stderr


def test_serve_stub_rejects_invalid_mode_flag() -> None:
    """An out-of-set ``--mode`` value fails Typer validation (exit 2)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["serve", "--mode", "staging"])

    # ``typer.BadParameter`` renders through Click's usage error path,
    # which exits with code 2. We assert on "non-zero, not 1" to pin
    # the distinction between validation failure (2) and stub deferral
    # (1) so a future refactor cannot collapse them.
    assert result.exit_code != 0
    assert result.exit_code != 1


# ---------------------------------------------------------------------------
# ``mcp`` — MCP server stub (W4)
# ---------------------------------------------------------------------------


def test_mcp_stub_exits_nonzero_with_deferral_message() -> None:
    """``ctxr-fsm mcp`` must defer to W4 and exit non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["mcp"])

    assert result.exit_code != 0
    assert "MCP server lands in W4" in result.stderr


def test_mcp_stub_accepts_valid_transport_flag() -> None:
    """``--transport http`` reaches the stub body."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["mcp", "--transport", "http"])

    assert result.exit_code == 1
    assert "lands in W4" in result.stderr


def test_mcp_stub_rejects_invalid_transport_flag() -> None:
    """An out-of-set ``--transport`` value fails Typer validation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["mcp", "--transport", "websocket"])

    assert result.exit_code != 0
    assert result.exit_code != 1


# ---------------------------------------------------------------------------
# ``api`` — FastAPI stub (W5)
# ---------------------------------------------------------------------------


def test_api_stub_exits_nonzero_with_deferral_message() -> None:
    """``ctxr-fsm api`` must defer to W5 and exit non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["api"])

    assert result.exit_code != 0
    assert "FastAPI server lands in W5" in result.stderr


def test_api_stub_accepts_valid_host_and_port() -> None:
    """Custom ``--host`` / ``--port`` values reach the stub body."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(
            app,
            ["api", "--host", "0.0.0.0", "--port", "9090"],
        )

    assert result.exit_code == 1
    assert "lands in W5" in result.stderr


def test_api_stub_rejects_out_of_range_port() -> None:
    """``--port`` outside 1-65535 fails Typer's bound check."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["api", "--port", "808080"])

    assert result.exit_code != 0
    assert result.exit_code != 1


# ---------------------------------------------------------------------------
# ``ui`` — Vite UI launcher stub (W6)
# ---------------------------------------------------------------------------


def test_ui_stub_exits_nonzero_with_deferral_message() -> None:
    """``ctxr-fsm ui`` must defer to W6 and exit non-zero."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["ui"])

    assert result.exit_code != 0
    assert "Vite UI dev server lands in W6" in result.stderr


def test_ui_stub_accepts_valid_port_flag() -> None:
    """A custom in-range ``--port`` reaches the stub body."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["ui", "--port", "5174"])

    assert result.exit_code == 1
    assert "lands in W6" in result.stderr


def test_ui_stub_rejects_out_of_range_port() -> None:
    """``--port`` outside 1-65535 fails Typer's bound check."""
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["ui", "--port", "70000"])

    assert result.exit_code != 0
    assert result.exit_code != 1


# ---------------------------------------------------------------------------
# Surface-level smoke — all four stubs registered on the top-level app
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcommand", "marker"),
    [
        ("serve", "W7"),
        ("mcp", "W4"),
        ("api", "W5"),
        ("ui", "W6"),
    ],
)
def test_all_stubs_registered_and_visible_in_help(
    subcommand: str, marker: str
) -> None:
    """``--help`` must mention each stub and its target workstream.

    This is the single test that catches "someone removed the stub
    registration in ``ctxr/fsm/cli/__init__.py``" — the per-command
    invocation tests above would still pass via the module-level
    function, but the top-level help screen would silently lose its
    advertised surface.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # ``--help`` lists each subcommand by name; the workstream marker
    # ships in the per-command help line registered in the app factory.
    assert subcommand in result.stdout
    assert marker in result.stdout
