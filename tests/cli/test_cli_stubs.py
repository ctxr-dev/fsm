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
# ``mcp`` — MCP server (W4, now implemented)
# ---------------------------------------------------------------------------
#
# These tests used to assert on the W3 stub's deferral message. W4
# replaced the stub with a real boot path that takes over the process
# until the transport returns, so we can no longer ``invoke(app, ["mcp"])``
# from a unit test — that would actually start the FastMCP stdio loop
# and block. Instead we exercise the surface via ``--help`` (which the
# Typer runner can render without entering any tool body) and assert
# that every documented flag (``--transport``, ``--host``, ``--port``)
# is listed. Real end-to-end coverage of the boot sequence is a W4
# integration test that drives the server in a subprocess with a
# wire-level MCP client; that lives outside this stub-shaped file.


def test_mcp_help_lists_transport_host_and_port_options() -> None:
    """``ctxr-fsm mcp --help`` documents the W4 flag surface."""
    result = runner.invoke(app, ["mcp", "--help"])

    # ``--help`` always exits 0 — anything else means a registration
    # regression (e.g. the import in cli/__init__.py was dropped).
    assert result.exit_code == 0, (
        f"`mcp --help` must exit 0 (got {result.exit_code}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Each flag must be visible by name so operator scripts and the
    # docs site can reliably parse the help text. We assert on the
    # long-form name with the leading double-dash because Typer's
    # rich help renderer can wrap descriptions but always prints the
    # flag token verbatim on its own line.
    assert "--transport" in result.stdout
    assert "--host" in result.stdout
    assert "--port" in result.stdout

    # ``--db`` is shared across every subcommand — also documented here
    # so callers see the full surface in one screen. Asserting on it
    # too pins the wiring against an accidental drop of ``DB_OPTION``.
    assert "--db" in result.stdout


def test_mcp_rejects_invalid_transport_flag() -> None:
    """An out-of-set ``--transport`` value fails Typer validation.

    The transport allowlist (``stdio``, ``http``) is enforced up-front
    in the CLI shim, before the server's heavyweight boot sequence
    runs, so this test can safely invoke the command without ever
    entering the FastMCP loop.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["mcp", "--transport", "websocket"])

    # ``typer.BadParameter`` renders through Click's usage-error path,
    # which exits with code 2. We assert "non-zero, not 1" so the
    # distinction between validation failure (2) and any future stub-
    # style deferral (1) stays explicit.
    assert result.exit_code != 0
    assert result.exit_code != 1


def test_mcp_rejects_out_of_range_port() -> None:
    """``--port`` outside 0-65535 fails Typer's bound check.

    We allow ``port=0`` (let the OS pick a free ephemeral port) so the
    lower bound is ``0``, not ``1``; the upper bound is the canonical
    TCP maximum. Going past either trips Typer's range validation
    before the server boots.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["mcp", "--port", "70000"])

    assert result.exit_code != 0
    assert result.exit_code != 1


# ---------------------------------------------------------------------------
# ``api`` — FastAPI server (W5, now implemented)
# ---------------------------------------------------------------------------
#
# These tests used to assert on the W3 stub's deferral message. W5
# replaced the stub with a real boot path that takes over the process
# until uvicorn returns, so we can no longer ``invoke(app, ["api"])``
# from a unit test — that would actually start the FastAPI / uvicorn
# loop and block. Instead we exercise the surface via ``--help`` (which
# the Typer runner can render without entering the command body) and
# assert that every documented flag (``--host``, ``--port``,
# ``--reload``, ``--db``) is listed. Real end-to-end coverage of the
# boot sequence is a W5 integration test that drives the server in a
# subprocess with an HTTP client; that lives outside this stub-shaped
# file.


def test_api_help_lists_host_port_reload_and_db_options() -> None:
    """``ctxr-fsm api --help`` documents the W5 flag surface."""
    result = runner.invoke(app, ["api", "--help"])

    # ``--help`` always exits 0 — anything else means a registration
    # regression (e.g. the import in cli/__init__.py was dropped).
    assert result.exit_code == 0, (
        f"`api --help` must exit 0 (got {result.exit_code}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Each flag must be visible by name so operator scripts and the
    # docs site can reliably parse the help text. We assert on the
    # long-form name with the leading double-dash because Typer's
    # rich help renderer can wrap descriptions but always prints the
    # flag token verbatim on its own line.
    assert "--host" in result.stdout
    assert "--port" in result.stdout
    assert "--reload" in result.stdout

    # ``--db`` is shared across every subcommand — also documented here
    # so callers see the full surface in one screen. Asserting on it
    # too pins the wiring against an accidental drop of ``DB_OPTION``.
    assert "--db" in result.stdout


def test_api_rejects_out_of_range_port() -> None:
    """``--port`` outside 0-65535 fails Typer's bound check.

    We allow ``port=0`` (let the OS pick a free ephemeral port) so the
    lower bound is ``0``, not ``1``; the upper bound is the canonical
    TCP maximum. Going past either trips Typer's range validation
    before the server boots.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["api", "--port", "70000"])

    # ``typer.BadParameter`` renders through Click's usage-error path,
    # which exits with code 2. We assert "non-zero, not 1" so the
    # distinction between validation failure (2) and any future stub-
    # style deferral (1) stays explicit.
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
        # Stub commands carry an explicit "ships in W{N}" marker in
        # their registered help line. ``mcp`` (W4) and ``api`` (W5)
        # are now real implementations, so their top-level help
        # strings no longer advertise a workstream — see the
        # dedicated cases below.
        ("serve", "W7"),
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


def test_mcp_registered_and_visible_in_help() -> None:
    """The real ``mcp`` command must appear on the top-level help screen.

    Mirrors :func:`test_all_stubs_registered_and_visible_in_help` for
    the now-implemented W4 command — the parametrised case above can't
    cover it because ``mcp``'s help line no longer carries a workstream
    marker. Asserting on the subcommand name plus the word "Protocol"
    pins both the registration and the documented purpose.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "mcp" in result.stdout
    # "Protocol" appears in the registered help text and is a stable,
    # distinctive token — far less likely to false-match than "MCP"
    # which could collide with another command's description.
    assert "Protocol" in result.stdout


def test_api_registered_and_visible_in_help() -> None:
    """The real ``api`` command must appear on the top-level help screen.

    Mirrors :func:`test_mcp_registered_and_visible_in_help` for the
    now-implemented W5 command — the parametrised case above can't
    cover it because ``api``'s help line no longer carries a
    workstream marker. Asserting on the subcommand name plus a stable
    token from the registered help text pins both the registration
    and the documented purpose.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "api" in result.stdout
    # "FastAPI" is the stable, distinctive token in the registered
    # help line — unlikely to false-match against any other command's
    # description.
    assert "FastAPI" in result.stdout
