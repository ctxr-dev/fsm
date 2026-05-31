"""``ctxr-fsm api`` — boot the FastAPI HTTP / SSE server.

This is the W5 implementation that replaces the W3 stub. The command
is intentionally a thin shim: it parses CLI arguments, then hands
control to :func:`ctxr.fsm.api.server.main`, which owns the actual
boot sequence (DB resolution, project open, ephemeral port picking,
uvicorn run loop, deterministic project close).

Why so thin
-----------

Two reasons, mirroring the rationale captured in
:mod:`ctxr.fsm.cli.mcp_cmd`:

1. **Testability.** Keeping the CLI shim trivial lets the W5 server
   tests exercise :func:`ctxr.fsm.api.server.main` directly (with a
   temp DB, in-process, against a chosen ephemeral port) without
   spawning ``ctxr-fsm api`` as a subprocess. The shim itself only
   needs a smoke test that ``--help`` documents the right flags.

2. **Embedding parity.** Notebooks, integration tests, and third-party
   orchestrators import :func:`ctxr.fsm.api.server.main` (or even the
   bare :data:`ctxr.fsm.api.app` instance) and never touch the CLI.
   Having the CLI do anything more than argument plumbing would split
   the boot sequence into two copies that could drift.

Option semantics
----------------

* ``--db`` honours the same layered precedence as every other
  subcommand (``--db`` > ``$CTXR_FSM_DB`` > ``./.ctxr-fsm/fsm.db``) —
  the env-var leg is wired by Typer through :data:`DB_OPTION`. We
  forward the raw value (``Path | None``) to the server so its own
  ``resolve_db_path`` applies the canonical precedence; the CLI does
  not pre-resolve, keeping a single source of truth.
* ``--host`` defaults to ``127.0.0.1`` so the dev loop is
  safe-by-default; the API has no transport encryption and remote
  exposure should be a deliberate operator decision.
* ``--port`` defaults to ``0`` (let the OS pick a free ephemeral
  port). This is the most useful default for the dev loop and for
  tests that read the chosen port back from the server logs; pinned
  deployments should pass an explicit number. The lower bound is
  therefore ``0`` rather than ``1`` so the "let the OS pick" default
  remains expressible from the CLI.
* ``--reload`` forwards to uvicorn's auto-reload watcher. Only useful
  when developing the API itself — the watcher re-execs the worker
  on every source change, which means the entry-point cannot
  pre-open the project (the binding would not survive the re-exec).
  The server module handles that asymmetry; the CLI just passes the
  flag through.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ctxr.fsm.cli._common import DB_OPTION

__all__ = ["api"]


def api(
    db: Path | None = DB_OPTION,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind address for the FastAPI server. Defaults to localhost "
            "so the dev workflow is safe-by-default; production "
            "deployments should set this explicitly (and front the API "
            "with a TLS-terminating reverse proxy)."
        ),
    ),
    port: int = typer.Option(
        0,
        "--port",
        min=0,
        max=65535,
        help=(
            "TCP port for the FastAPI server (0-65535; 0 = let the OS "
            "pick a free ephemeral port — useful for the dev loop and "
            "for tests that read the bound port back from the server "
            "logs)."
        ),
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help=(
            "Enable uvicorn's auto-reload watcher. Only useful when "
            "developing the API itself; the watcher re-execs the worker "
            "on every source change."
        ),
    ),
) -> None:
    """Run the ctxr-fsm FastAPI server until uvicorn returns.

    Blocks until SIGINT / SIGTERM arrives or uvicorn shuts down
    programmatically. The project DB handle is opened by the server's
    boot sequence and closed in a ``finally`` clause so the SQLite
    file lock is released even on abnormal exit.
    """
    # Local import so the CLI module stays cheap to import even when
    # the user never invokes ``api`` — pulling in FastAPI + uvicorn
    # at every ``ctxr-fsm`` startup would add a noticeable warm-up
    # cost to every other subcommand.
    from ctxr.fsm.api.server import main as _server_main

    _server_main(
        host=host,
        port=port,
        reload=reload,
        db_path=db,
    )
