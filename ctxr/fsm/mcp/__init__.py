"""``ctxr.fsm.mcp`` — Model Context Protocol server for the FSM substrate.

This package exposes the SQLite-backed FSM (W2) and its core engine
(W1) through an MCP server. The server is the W12 enforcement
substrate: it surfaces :attr:`State.allowed_tools`, returns
:class:`CommitToken` from two-phase ``commit_outputs``, accepts
cosignatures, and observes tool calls. W4 is the **plumbing** wave —
schema validation and basic commit semantics — and intentionally does
NOT yet enforce the hard rules; W12 wires those.

Brief contract (what every ``fsm.get_brief`` / ``fsm.start_run`` /
``fsm.commit_outputs.advanced`` return MUST include and clients MUST
honour)
----------------------------------------------------------------------

The :class:`~ctxr.fsm.core.models.Brief` payload carries — among other
fields — a closed capability surface for the worker:

* ``allowed_tools`` — list of tool names the worker may invoke for
  this state. **MUST be honoured by every MCP client.** Any call the
  worker makes outside this list is "off-allowlist" and the engine
  treats it as a layer-2 drift signal (W12). An empty list means no
  side-effecting tools are allowed for that state.
* ``post_validations`` — predicate expressions the engine will run
  against the committed outputs. A worker should self-check against
  them before committing to avoid a faulted commit.
* ``transitions`` — the outbound guards the engine will evaluate at
  commit time, with their predicate sources verbatim. Used by clients
  that want to preview the next state without committing.
* ``has_worker`` / ``has_loop`` / ``iteration_n`` — discriminators
  for the worker / loop / terminal shapes of a state.

Clients are expected to inspect ``allowed_tools`` *before* dispatching
any tool call and refuse the call locally if the name is not on the
list — the server-side enforcement (layer 2 in W12) is a
defence-in-depth backstop, not the primary gate.

Spec-hash lock (layer 9)
------------------------

Every commit / brief tool checks ``run.fsm_spec_hash`` against the
current registered spec hash and returns the structured
``fsm_spec_changed`` error envelope on mismatch. A run is therefore
"locked" to the spec version it was started against — re-registering a
new version while runs are in flight causes those runs to refuse new
work until they are aborted or the spec change is rolled back.

Commit cosignature (layer 5)
----------------------------

``fsm.commit_outputs`` accepts an optional ``signature`` (computed as
:meth:`CommitSignature.compute`). The signature is REQUIRED when:

* the env var ``CTXR_FSM_REQUIRE_COSIGNATURE`` is set to ``"1"``;
* OR the current state declares any :attr:`State.allowed_tools` entry;
* OR the current state declares a :attr:`State.verifier`.

A valid signature is persisted via ``commit_signatures`` and
``commit_signature_verified`` is emitted on the event bus. A
mismatched signature surfaces as ``signature_mismatch`` and emits
``commit_signature_mismatch``; a missing-but-required signature
surfaces as ``signature_required``.

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


# ----------------------------------------------------------------------
# Health probe (W14c)
# ----------------------------------------------------------------------
#
# FastMCP's HTTP-SSE transport runs under a Starlette app; we mount a
# trivial ``/healthz`` route there so the W7 supervisor (and ``ctxr-fsm
# ensure``) can probe the same liveness URL we expose on every other
# subsystem. Without this the supervisor would never publish the
# discovery file (``active-mcp.json`` only lands once MCP healthz hits
# 200) and ensure would time out.
#
# Stdio transport has no HTTP surface; the route is harmless there
# (FastMCP simply ignores custom_route registrations when the
# transport is stdio).


@mcp.custom_route("/healthz", methods=["GET"])  # type: ignore[untyped-decorator]
async def _healthz(_request: object) -> object:  # pragma: no cover - trivial
    """Return 200 OK; the W7 supervisor probes this to gate readiness.

    Body is the same one-word ``"ok"`` the FastAPI side returns so a
    grep across log lines doesn't have to special-case the MCP
    subsystem. Starlette's PlainTextResponse is the lightest carrier;
    we import lazily so a CLI invocation that never boots the HTTP
    transport doesn't pay the import cost.

    ``type: ignore[untyped-decorator]`` on the decorator: FastMCP's
    ``custom_route`` is typed loosely (it accepts Any callable) and
    mypy flags it as an untyped decorator. The body itself is fully
    typed; the ignore is purely about FastMCP's decorator signature.
    """
    from starlette.responses import PlainTextResponse

    return PlainTextResponse("ok", status_code=200)


# Import the tools module for its decorator side effects. This must
# happen AFTER ``mcp`` is constructed because the decorators reach for
# the live instance. Local import to avoid the top-of-file cycle that
# would otherwise be created (tools imports _state, which is fine, but
# tools also imports back from this package to grab ``mcp``).
from ctxr.fsm.mcp import tools as _tools  # noqa: E402, F401  (side-effect import)
