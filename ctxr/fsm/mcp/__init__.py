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

import os
import re

from mcp.server.fastmcp import FastMCP

__all__ = ["mcp"]


# CORS allowlist (mirrors ctxr.fsm.api)
# The dashboard polls this MCP ``/healthz`` from the Vite dev origin
# (and from any operator-extended origin). Without these headers the
# browser blocks the response and the InfoTopBar health pill never
# turns green. We mirror the FastAPI defaults + env-var hook so the
# two subsystems share one allowlist surface.
_DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)
_CORS_ENV_VAR: str = "CTXR_FSM_API_CORS_ORIGINS"

# Permissive regex for ANY loopback origin on ANY port. The supervisor
# can pick ephemeral ports for Vite (and for itself) when 5173 is
# busy; in that case the static allowlist above misses and the
# dashboard pill drops to "degraded" purely from a CORS rejection
# even though the service is healthy. Loopback origins are private to
# the operator's machine, so widening to ``http://127.0.0.1:<port>``
# and ``http://localhost:<port>`` is the right safety/usability trade.
_LOOPBACK_ORIGIN_RE: re.Pattern[str] = re.compile(
    r"^http://(127\.0\.0\.1|localhost):\d+$"
)


def _resolve_cors_origins() -> list[str]:
    """Return the combined CORS allowlist for the MCP healthz route."""
    extra = os.environ.get(_CORS_ENV_VAR, "")
    parsed = [o.strip() for o in extra.split(",") if o.strip()]
    return list(dict.fromkeys([*_DEFAULT_CORS_ORIGINS, *parsed]))


def _is_allowed_origin(origin: str) -> bool:
    """Return True for any allowlisted or loopback origin."""
    if origin in _resolve_cors_origins():
        return True
    return bool(_LOOPBACK_ORIGIN_RE.match(origin))


def _cors_headers_for(origin: str | None) -> dict[str, str]:
    """Return ``Access-Control-*`` headers when ``origin`` is allowed.

    Returns an empty dict for missing / non-allowlisted origins so
    same-origin (e.g. curl, the supervisor probe) callers get no
    spurious CORS headers and unknown origins are silently refused.
    """
    if not origin or not _is_allowed_origin(origin):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
    }


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


@mcp.custom_route("/healthz", methods=["GET", "OPTIONS"])  # type: ignore[untyped-decorator]
async def _healthz(request: object) -> object:  # pragma: no cover - trivial
    """Return 200 OK; the W7 supervisor probes this to gate readiness.

    Body is the same one-word ``"ok"`` the FastAPI side returns so a
    grep across log lines doesn't have to special-case the MCP
    subsystem. Starlette's PlainTextResponse is the lightest carrier;
    we import lazily so a CLI invocation that never boots the HTTP
    transport doesn't pay the import cost.

    The dashboard ``InfoTopBar`` polls this from the Vite dev origin,
    so we echo CORS headers from :func:`_cors_headers_for` for any
    request that carries an allowlisted ``Origin``. Same-origin
    probes (supervisor / curl) get no extra headers and are
    unaffected. The handler also responds to OPTIONS preflights so
    a future client that adds custom request headers still works.

    ``type: ignore[untyped-decorator]`` on the decorator: FastMCP's
    ``custom_route`` is typed loosely (it accepts Any callable) and
    mypy flags it as an untyped decorator. The body itself is fully
    typed; the ignore is purely about FastMCP's decorator signature.
    """
    from starlette.requests import Request
    from starlette.responses import PlainTextResponse, Response

    origin: str | None = None
    if isinstance(request, Request):
        origin = request.headers.get("origin")
    headers = _cors_headers_for(origin)
    # Preflight: empty body, full method/header allowlist mirroring
    # the FastAPI side so any future header tweak in the dashboard
    # works without another round-trip here.
    if isinstance(request, Request) and request.method == "OPTIONS":
        preflight_headers = {
            **headers,
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Max-Age": "600",
        } if headers else {}
        return Response(status_code=204, headers=preflight_headers)
    return PlainTextResponse("ok", status_code=200, headers=headers)


# Import the tools module for its decorator side effects. This must
# happen AFTER ``mcp`` is constructed because the decorators reach for
# the live instance. Local import to avoid the top-of-file cycle that
# would otherwise be created (tools imports _state, which is fine, but
# tools also imports back from this package to grab ``mcp``).
from ctxr.fsm.mcp import tools as _tools  # noqa: E402, F401  (side-effect import)
