"""``ctxr-fsm mcp`` — boot the Model Context Protocol server.

This is the W4 implementation that replaces the W3 stub. The command
is intentionally a thin shim: it parses CLI arguments, then hands
control to :func:`ctxr.fsm.mcp.server.main`, which owns the actual
boot sequence (DB resolution, project open, logging setup, signal
handlers, transport loop).

Why so thin
-----------

Two reasons:

1. **Testability.** Keeping the CLI shim trivial lets the W4 server
   tests exercise :func:`main` directly (with a temp DB, in-process)
   without spawning ``ctxr-fsm mcp`` as a subprocess. The shim itself
   only needs a smoke test that ``--help`` documents the right flags.

2. **Embedding parity.** Notebooks and third-party orchestrators
   import :func:`ctxr.fsm.mcp.server.main` (or even the bare
   ``ctxr.fsm.mcp.mcp`` instance) and never touch the CLI. Having the
   CLI do anything more than argument plumbing would split the boot
   sequence into two copies that could drift.

Transport semantics
-------------------

* ``--transport stdio`` (the default): the process becomes the MCP
  server, reading JSON-RPC frames on stdin and writing them on stdout,
  until the client closes the pipe or SIGINT / SIGTERM arrives. This
  is the canonical Claude Code path — the host launches the binary
  and speaks MCP over the resulting pipes.
* ``--transport http``: FastMCP's SSE transport binds
  ``--host`` / ``--port`` and the process blocks on the underlying
  uvicorn loop. Port ``0`` (the default) lets the OS pick an
  ephemeral port, which is handy for local-loop scripts and tests
  where the caller reads the bound port out-of-band; production
  deployments should pass an explicit non-zero port.

``--db`` honours the same layered precedence as every other
subcommand (``--db`` > ``$CTXR_FSM_DB`` > ``./.ctxr-fsm/fsm.db``) —
the env-var leg is wired by Typer through :data:`DB_OPTION`.

A note on validation
--------------------

The transport set (``stdio``, ``http``) is enforced here, mirroring
the W3 stub's contract so any operator script pinned against the
stub keeps working unchanged. ``--port`` accepts ``0`` (special-cased
to "OS-picked ephemeral port") through ``65535``; the lower bound is
``0`` rather than ``1`` precisely so the "let the OS pick" default
remains expressible from the CLI.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ctxr.fsm.cli._common import DB_OPTION

__all__ = ["mcp"]


# Allowed values for the ``--transport`` flag. Pinned as a tuple of
# literals so adding a new transport (e.g. ``websocket``) is a one-
# line change and there is exactly one place to grep for the contract.
_VALID_TRANSPORTS: tuple[str, ...] = ("stdio", "http")


def mcp(
    db: Path | None = DB_OPTION,
    transport: str = typer.Option(
        "stdio",
        "--transport",
        help=(
            "MCP transport: 'stdio' (the canonical client-launched "
            "framing, default) or 'http' (FastMCP's SSE transport on "
            "the host/port below)."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Bind address for the HTTP transport. Ignored for stdio. "
            "Defaults to localhost so the dev loop is safe-by-default; "
            "production deployments should set this explicitly."
        ),
    ),
    port: int = typer.Option(
        0,
        "--port",
        min=0,
        max=65535,
        help=(
            "TCP port for the HTTP transport (0-65535; 0 = let the OS "
            "pick a free ephemeral port). Ignored for stdio."
        ),
    ),
) -> None:
    """Run the ctxr-fsm MCP server until the transport returns.

    For ``--transport stdio`` (the default) the process becomes the
    MCP server until the client closes the pipe or SIGINT / SIGTERM
    arrives. For ``--transport http`` the process blocks on the
    underlying uvicorn / SSE loop until the same signals arrive.
    """
    if transport not in _VALID_TRANSPORTS:
        # Use Typer's standard validation rendering so the UX matches
        # any other bad-flag error (exit code 2, usage banner).
        raise typer.BadParameter(
            f"--transport must be one of {_VALID_TRANSPORTS!r} "
            f"(got {transport!r})"
        )

    # Local import so the CLI module stays cheap to import even when
    # the user never invokes ``mcp`` — pulling in FastMCP + uvicorn
    # at every ``ctxr-fsm`` startup would add a noticeable warm-up
    # cost to every other subcommand.
    from ctxr.fsm.mcp.server import main as _server_main

    # ``transport`` is already narrowed to the literal set above; pass
    # it through verbatim. ``db`` is forwarded as-is so the server's
    # own ``resolve_db_path`` applies the canonical precedence.
    _server_main(
        transport=transport,  # type: ignore[arg-type]
        db_path=db,
        host=host,
        port=port,
    )
