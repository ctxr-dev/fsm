"""``ctxr-fsm`` Typer application — the public CLI entry point.

This module instantiates the top-level :class:`typer.Typer` app and
registers every concrete command on it. The ``ctxr-fsm`` console script
declared in ``pyproject.toml`` points at the :data:`app` symbol exposed
below, so any side-effect performed during import (the
``@app.command`` registrations triggered by importing each submodule)
runs once at CLI startup.

Command surface (W3 scope)
--------------------------

* ``init`` — bootstrap ``./.ctxr-fsm/`` and migrate the project DB.
* ``migrate`` — run ``alembic upgrade head`` against the project DB.
* ``doctor`` — diagnostic dump of the project DB.
* ``spec`` — validate / register / list FSM specs (subcommand group).
* ``runs`` — list FSM runs (subcommand group).
* ``run`` — show / resume / abort a single FSM run (subcommand group).
* ``export`` — dump a run to a single versioned JSON document.
* ``import`` — re-insert a run from a JSON dump (with ``--replace``
  for clobber semantics).

Implemented (W4)
----------------

* ``mcp`` — Model Context Protocol server. Boots the FastMCP
  instance over stdio (default) or HTTP-SSE.

Stubbed commands (filled in later workstreams)
----------------------------------------------

* ``serve`` — long-running orchestrator (W7).
* ``api`` — FastAPI HTTP / SSE surface (W5).
* ``ui`` — local UI launcher (W6 / W7).

Each stub lives in its own ``*_cmd.py`` module so the eventual
implementation can grow without touching the top-level wiring; the
stub bodies print a friendly "not yet implemented" message and exit
with status ``1`` so any script that pipes these commands through
``set -e`` fails loudly rather than silently doing nothing.
"""

from __future__ import annotations

import importlib.metadata

import typer

from ctxr.fsm.cli import (
    api_cmd,
    doctor_cmd,
    ensure_cmd,
    export_cmd,
    import_cmd,
    init_cmd,
    install_mcp_cmd,
    install_memory_cmd,
    mcp_cmd,
    migrate_cmd,
    runs_cmd,
    serve_cmd,
    spec_cmd,
    ui_cmd,
    urls_cmd,
)

__all__ = ["app"]


# ``no_args_is_help=True`` makes ``ctxr-fsm`` (no args) print the help
# screen instead of doing nothing — friendlier for first-time users.
app: typer.Typer = typer.Typer(
    name="ctxr-fsm",
    help="SQLite-backed FSM substrate.",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_package_version() -> str:
    """Return the installed ``ctxr-fsm`` distribution version.

    Falls back to the literal ``"unknown"`` rather than raising — the
    version probe must succeed even when ``ctxr-fsm`` is being run from
    a source checkout that has no installed dist-info (e.g. a sibling
    ``file://`` link during development).
    """
    try:
        return importlib.metadata.version("ctxr-fsm")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _version_callback(value: bool) -> None:
    """Typer eager-callback for ``--version``.

    Prints ``ctxr-fsm <version>`` and exits 0 before any subcommand is
    dispatched. The bootstrap procedure (``@.ctxr-fsm/memory/bootstrap.md``
    Step 1) probes this flag to decide whether the package is installed
    in the current workdir, so the contract is: exit 0 with one line of
    output on stdout. ``value`` is the resolved option value: only fire
    when explicitly set to ``True`` (``--version``), never on the
    default ``False`` path.
    """
    if not value:
        return
    typer.echo(f"ctxr-fsm {_resolve_package_version()}")
    raise typer.Exit(code=0)


@app.callback(invoke_without_command=True)
def _main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        help="Print the installed ctxr-fsm version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Top-level Typer callback.

    Owns the global ``--version`` flag (eager callback, exits 0). With
    ``invoke_without_command=True``, the CLI tolerates being called
    with ONLY ``--version`` (no subcommand). The eager callback above
    handles the print + exit; when the flag is not set and no
    subcommand was supplied, we fall through to the help screen the
    same way ``no_args_is_help=True`` would have done for the bare
    invocation. The ``version`` parameter is consumed by Typer via the
    callback; we do not need to inspect it here.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# Concrete commands
# ---------------------------------------------------------------------------

# We register each command explicitly (rather than relying on the
# decorator side-effect at import time) so the wiring is
# self-documenting at a glance — readers can see exactly which
# functions become subcommands. Typer treats the keyword-arg ``name``
# as the subcommand label, so ``init`` keeps its short canonical name
# even though the implementation function is also called ``init``.
app.command(name="init", help="Initialise a ctxr-fsm project under the current directory.")(
    init_cmd.init
)
app.command(name="migrate", help="Run alembic upgrade head against the project DB.")(
    migrate_cmd.migrate
)
app.command(name="doctor", help="Print a diagnostic report for the project DB.")(
    doctor_cmd.doctor
)
# ``urls`` (W16) is the one-keystroke "where do I go?" shortcut. Prints
# ONLY the Rich subsystem table — no DB diagnostic panel, no alembic
# chatter. ``show`` is registered as an alias for muscle-memory parity
# (different operators reach for different verbs).
app.command(
    name="urls",
    help="Print the subsystem URL table (UI / Swagger / MCP / API). One-keystroke 'where do I go?' shortcut.",
)(urls_cmd.urls)
app.command(
    name="show",
    help="Alias for `urls` — prints the subsystem URL table.",
    hidden=True,
)(urls_cmd.urls)
# ``install-memory`` (W11) writes the FSM-usage principles into the
# consumer project's AI-client memory files (CLAUDE.md, AGENTS.md,
# .cursor/rules/). Idempotent via marker-fenced blocks.
app.command(
    name="install-memory",
    help="Install (or check) FSM-usage principles into AI-client memory.",
)(install_memory_cmd.install_memory)
# ``install-mcp`` (W14d) registers ctxr-fsm as a stdio MCP server in
# each detected client's config file: .mcp.json / .claude/settings.json
# (Claude Code project-local), ~/.codex/config.toml (Codex user-level,
# preferring the ``codex mcp add`` CLI when available), and
# ~/.cursor/mcp.json (Cursor user-level). Idempotent: only the
# ``ctxr-fsm`` entry is owned; every other entry passes through.
app.command(
    name="install-mcp",
    help="Register (or check) ctxr-fsm as a stdio MCP server in client configs.",
)(install_mcp_cmd.install_mcp)
# ``ensure`` (W14b) is the single bootstrap entry point every skill
# invokes from its SKILL.md preamble. Idempotent, fast on the warm
# path (<500ms), self-heals init + memory + mcp-config + supervisor.
# Emits a JSON document on stdout (default when piped; --no-json for
# interactive pretty output).
app.command(
    name="ensure",
    help="Ensure the project is bootstrapped and the supervisor is up.",
)(ensure_cmd.ensure)

# The ``spec`` group bundles spec validate / register / list under one
# namespace so the top-level help screen stays short. Adding it via
# ``add_typer`` (rather than ``command``) is the canonical Typer way
# to mount a nested app.
app.add_typer(spec_cmd.spec_app, name="spec")

# ``runs`` (plural) covers cross-run queries; ``run`` (singular) covers
# per-run commands. We register both because operator muscle memory
# expects "list runs" to read as ``runs ls`` and "abort this run" to
# read as ``run abort <id>``.
app.add_typer(runs_cmd.runs_app, name="runs")
app.add_typer(runs_cmd.run_app, name="run")

# Export / import bracket the substrate's data-portability story:
# ``export`` produces a versioned JSON dump of a run; ``import``
# re-inserts a dump into another project DB. We register ``import_cmd``
# under the ``import`` name even though ``import`` is a Python keyword
# — Typer keys on the string, not the symbol, so there is no clash.
app.command(name="export", help="Export a run to a self-contained JSON file.")(
    export_cmd.export
)
app.command(
    name="import",
    help="Import a run from a JSON file produced by `ctxr-fsm export`.",
)(import_cmd.import_run_cmd)


# ---------------------------------------------------------------------------
# Stubs for future workstreams
# ---------------------------------------------------------------------------

# These commands appear in --help today so the surface is stable.
# The ``mcp`` body is the real W4 implementation; the others remain
# stubs (each delegates to a sibling module that prints a deferral
# message and exits non-zero) until the workstream that owns them
# lands. Moving the bodies out of this file means a later workstream
# (W5/W6/W7) can flesh them out without touching the top-level wiring
# at all.
app.command(
    name="serve",
    help="Run the unified supervisor (MCP + API + UI in dev mode).",
)(serve_cmd.serve)
# ``mcp`` is the W4 entry point — boots the FastMCP server over the
# selected transport. The help string mentions both transports so
# operators see the shape on ``ctxr-fsm --help`` without drilling in.
app.command(
    name="mcp",
    help="Run the Model Context Protocol server (stdio or http transport).",
)(mcp_cmd.mcp)
# ``api`` is the W5 entry point — boots the FastAPI HTTP / SSE
# server. The help string mentions both surfaces (HTTP + SSE) so
# operators see the shape on ``ctxr-fsm --help`` without drilling in.
app.command(
    name="api",
    help="Run the FastAPI HTTP / SSE server (binds --host/--port).",
)(api_cmd.api)
app.command(
    name="ui",
    help="Run the Vite dev server for the UI subproject (binds --port).",
)(ui_cmd.ui)
