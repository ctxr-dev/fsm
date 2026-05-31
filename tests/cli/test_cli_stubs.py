"""Tests for the ``ctxr-fsm`` CLI subcommand surface.

The file is named ``test_cli_stubs`` for historical reasons — it was
born in W3 when ``serve`` / ``mcp`` / ``api`` / ``ui`` were all
"deferral" stubs and the only thing worth asserting was the exit code
and wording of the deferral message. As each workstream (W4 mcp,
W5 api, W6 ui, W7 serve) replaced its stub with a real implementation,
the corresponding test block was rewritten in the same shape: drive
``<subcommand> --help`` through Typer's :class:`CliRunner` to render
the surface without entering the command body, then assert the
documented flags are visible.

Why the ``--help`` shape and not the real body
----------------------------------------------

Every real implementation in this family takes over the process until
its server returns:

* ``mcp`` enters the FastMCP stdio (or HTTP) loop.
* ``api`` enters the uvicorn / FastAPI loop.
* ``ui`` shells out to ``npm install`` + ``npm run dev``.
* ``serve`` enters the W7 supervisor's task group (which itself
  spawns and supervises the three above).

Driving any of those from an in-process unit test would block the
runner; the right place for end-to-end coverage of each boot sequence
is the per-workstream integration tests (subprocess + wire-level
client) that live outside this file. What stays here is the
surface-level contract: registration, option visibility, and Typer's
own up-front validation (port bounds, mode allowlist, transport
allowlist), all of which the runner can exercise via ``--help`` or a
deliberately-invalid argument that trips Typer before the body runs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

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

    None of the commands exercised in this file actually consume the
    DB path — the assertions either run against ``--help`` (which never
    touches the FS) or against an invalid flag value that fails Typer
    validation before the body runs. Constructing this path anyway
    keeps the test shape aligned with the convention in the brief
    (``tempfile.TemporaryDirectory()`` for the project DB) so a future
    addition that does touch the FS can extend these tests without
    restructuring them.
    """
    return Path(tmpdir) / "fsm.db"


# ---------------------------------------------------------------------------
# ``serve`` — unified supervisor (W7, now implemented)
# ---------------------------------------------------------------------------
#
# These tests used to assert on the W3 stub's deferral message. W7
# replaced the stub with a real boot path that spawns the MCP + API
# (+ UI in dev mode) children inside an anyio task group and blocks
# until SIGINT/SIGTERM, so we can no longer ``invoke(app, ["serve"])``
# from a unit test — that would actually start the supervisor and hang
# the runner. Instead we exercise the surface via ``--help`` (which
# the Typer runner can render without entering the command body) and
# assert that every documented flag (``--mode``, ``--db``) is listed.
# Real end-to-end coverage of the boot sequence is a W7 integration
# test that drives the supervisor in a subprocess and confirms the
# three child URLs come up; that lives outside this stub-shaped file.


def test_serve_help_lists_mode_and_db_options() -> None:
    """``ctxr-fsm serve --help`` documents the W7 flag surface."""
    result = runner.invoke(app, ["serve", "--help"])

    # ``--help`` always exits 0 — anything else means a registration
    # regression (e.g. the import in cli/__init__.py was dropped).
    assert result.exit_code == 0, (
        f"`serve --help` must exit 0 (got {result.exit_code}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Each flag must be visible by name so operator scripts and the
    # docs site can reliably parse the help text. We assert on the
    # long-form name with the leading double-dash because Typer's
    # rich help renderer can wrap descriptions but always prints the
    # flag token verbatim on its own line.
    assert "--mode" in result.stdout
    # ``--db`` is shared across every subcommand — also documented here
    # so callers see the full surface in one screen. Asserting on it
    # too pins the wiring against an accidental drop of ``DB_OPTION``.
    assert "--db" in result.stdout


def test_serve_rejects_invalid_mode_flag() -> None:
    """An out-of-set ``--mode`` value fails the up-front allowlist check.

    The ``--mode`` allowlist (``dev``, ``prod``) is enforced inside the
    Typer shim *before* the supervisor's heavyweight task-group boot
    runs, so this test can safely invoke the command without ever
    spawning a child process. ``typer.BadParameter`` renders through
    Click's usage-error path, which exits with code 2.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["serve", "--mode", "staging"])

    # We assert "non-zero, not 1" to pin the distinction between
    # validation failure (2) and any future stub-style deferral (1)
    # so a future refactor cannot silently collapse them.
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
# ``ui`` — Vite UI launcher (W6, now implemented)
# ---------------------------------------------------------------------------
#
# These tests used to assert on the W3 stub's deferral message. W6
# replaced the stub with a real boot path that shells out to ``npm
# install`` and ``npm run dev``, both of which would actually spawn
# the Vite dev server and block the test runner. We therefore exercise
# the surface via ``--help`` (which Typer can render without entering
# the command body) and assert that every documented flag
# (``--port``, ``--api-port``, ``--no-install``) is listed. Real
# end-to-end coverage of the boot sequence is a manual smoke
# (``ctxr-fsm ui`` against a live API) plus the W6 UI tests that
# drive Vite directly; both live outside this stub-shaped file.


def test_ui_help_lists_port_api_port_and_no_install_options() -> None:
    """``ctxr-fsm ui --help`` documents the W6 flag surface."""
    result = runner.invoke(app, ["ui", "--help"])

    # ``--help`` always exits 0 — anything else means a registration
    # regression (e.g. the import in cli/__init__.py was dropped).
    assert result.exit_code == 0, (
        f"`ui --help` must exit 0 (got {result.exit_code}); "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Each flag must be visible by name so operator scripts and the
    # docs site can reliably parse the help text. We assert on the
    # long-form name with the leading double-dash because Typer's
    # rich help renderer can wrap descriptions but always prints the
    # flag token verbatim on its own line.
    assert "--port" in result.stdout
    assert "--api-port" in result.stdout
    assert "--no-install" in result.stdout


def test_ui_rejects_out_of_range_port() -> None:
    """``--port`` outside 1-65535 fails Typer's bound check.

    The bound check runs before the command body, so this test can
    safely invoke the command without ever spawning ``npm`` — the
    invalid value trips Typer's range validation first.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["ui", "--port", "70000"])

    # ``typer.BadParameter`` renders through Click's usage-error path,
    # which exits with code 2. We assert "non-zero, not 1" so the
    # distinction between validation failure (2) and any future stub-
    # style deferral (1) stays explicit.
    assert result.exit_code != 0
    assert result.exit_code != 1


def test_ui_rejects_out_of_range_api_port() -> None:
    """``--api-port`` outside 1-65535 fails Typer's bound check.

    Mirrors :func:`test_ui_rejects_out_of_range_port` for the
    sibling flag — same rationale (validation runs before the body,
    so no Vite spawn).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        _project_db(tmpdir)
        result = runner.invoke(app, ["ui", "--api-port", "70000"])

    assert result.exit_code != 0
    assert result.exit_code != 1


# ---------------------------------------------------------------------------
# Surface-level smoke — every long-running subcommand is registered
# ---------------------------------------------------------------------------
#
# The W3 parametrised "stubs registered" test is gone — the only stub
# it ever covered (``serve``) is now a real implementation, leaving an
# empty parametrize list that pytest would warn about. The per-command
# registration checks below take its place: each one asserts the
# subcommand name appears on the top-level help screen alongside a
# stable, distinctive token from its registered help line.


def test_serve_registered_and_visible_in_help() -> None:
    """The real ``serve`` command must appear on the top-level help screen.

    Mirrors the per-workstream registration checks below — asserting
    on the subcommand name plus a stable token from the registered
    help text pins both the registration and the documented purpose.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.stdout
    # "supervisor" is the stable, distinctive token in the registered
    # help line — unlikely to false-match against any other command's
    # description.
    assert "supervisor" in result.stdout.lower()


def test_mcp_registered_and_visible_in_help() -> None:
    """The real ``mcp`` command must appear on the top-level help screen.

    Asserting on the subcommand name plus the word "Protocol" pins
    both the registration and the documented purpose.
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

    Asserting on the subcommand name plus a stable token from the
    registered help text pins both the registration and the
    documented purpose.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "api" in result.stdout
    # "FastAPI" is the stable, distinctive token in the registered
    # help line — unlikely to false-match against any other command's
    # description.
    assert "FastAPI" in result.stdout


def test_ui_registered_and_visible_in_help() -> None:
    """The real ``ui`` command must appear on the top-level help screen.

    Asserting on the subcommand name plus a stable token from the
    registered help text pins both the registration and the
    documented purpose.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "ui" in result.stdout
    # "Vite" is the stable, distinctive token in the registered help
    # line — unlikely to false-match against any other command's
    # description.
    assert "Vite" in result.stdout
