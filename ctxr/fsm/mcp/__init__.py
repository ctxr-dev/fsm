"""``ctxr.fsm.mcp`` — Model Context Protocol server for the FSM substrate.

This package exposes the SQLite-backed FSM (W2) and its core engine
(W1) through an MCP server. The server is the W12 enforcement
substrate: it surfaces :attr:`State.allowed_tools`, returns
:class:`CommitToken` from two-phase ``commit_outputs``, accepts
cosignatures, and observes tool calls. W4 is the **plumbing** wave —
schema validation and basic commit semantics — and intentionally does
NOT yet enforce the hard rules; W12 wires those.

Module layout
-------------

* :mod:`ctxr.fsm.mcp.server` — the entry point (``main()``) plus
  transport selection. The CLI's ``ctxr-fsm mcp`` subcommand thunks
  here.
* :mod:`ctxr.fsm.mcp._state` — process-wide :class:`Project` handle
  shared between every tool body.
* :mod:`ctxr.fsm.mcp._errors` — structured error envelope (the
  legacy JS contract).
* :mod:`ctxr.fsm.mcp.tools` — re-exports every ``@mcp.tool()``-
  decorated function. Importing this module is what *registers* the
  tools on the FastMCP instance, so the package ``__init__`` does
  the import for its side effect.

The :data:`mcp` instance is exported at package scope so external
embedders (notebooks, third-party orchestrators) can ``from
ctxr.fsm.mcp import mcp`` and mount it as a sub-app, run alternate
transports, or attach extra tools without reaching into the server
entry point.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

__all__ = ["mcp"]


# The single FastMCP instance. Module-level so the ``@mcp.tool()``
# decorators in :mod:`ctxr.fsm.mcp.tools` can find it at import time
# without threading the server through a fixture.
#
# ``instructions`` is surfaced to MCP clients in the ``initialize``
# response — it is the first thing an LLM-driven client sees about the
# server, so we keep it short and action-oriented (what the server is
# + how to use it) rather than describing internals.
mcp: FastMCP = FastMCP(
    name="ctxr-fsm",
    instructions=(
        "SQLite-backed FSM substrate. Use fsm.* tools to drive "
        "deterministic state machines for agent workflows."
    ),
)


# Import the tools module for its decorator side effects. This must
# happen AFTER ``mcp`` is constructed because the decorators reach for
# the live instance. Local import to avoid the top-of-file cycle that
# would otherwise be created (tools imports _state, which is fine, but
# tools also imports back from this package to grab ``mcp``).
from ctxr.fsm.mcp import tools as _tools  # noqa: E402, F401  (side-effect import)
