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

import typer

from ctxr.fsm.cli import (
    api_cmd,
    doctor_cmd,
    export_cmd,
    import_cmd,
    init_cmd,
    mcp_cmd,
    migrate_cmd,
    runs_cmd,
    serve_cmd,
    spec_cmd,
    ui_cmd,
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
app.command(name="serve", help="(Stub) long-running orchestrator — ships in W7.")(
    serve_cmd.serve
)
# ``mcp`` is the W4 entry point — boots the FastMCP server over the
# selected transport. The help string mentions both transports so
# operators see the shape on ``ctxr-fsm --help`` without drilling in.
app.command(
    name="mcp",
    help="Run the Model Context Protocol server (stdio or http transport).",
)(mcp_cmd.mcp)
app.command(name="api", help="(Stub) FastAPI HTTP / SSE surface — ships in W5.")(
    api_cmd.api
)
app.command(name="ui", help="(Stub) local UI launcher — ships in W6/W7.")(
    ui_cmd.ui
)
