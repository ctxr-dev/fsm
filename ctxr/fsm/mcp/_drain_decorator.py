"""``@drain_aware`` decorator for ``@mcp.tool()``-registered functions.

Every ``fsm.*`` MCP tool body has the same two-line shape under the
graceful-drain regime:

1. If the server is in a drain window, reject the call with the
   structured ``server_draining`` error envelope so the client can
   retry against the freshly-restarted child.
2. Otherwise, bump the in-flight counter (so the supervisor's drain
   wait can see the call), run the existing tool body, and decrement
   the counter on exit (success or exception).

Repeating those two lines across 17 tools would be a copy-paste hazard
— every new tool would need a reviewer to verify the pattern was
applied correctly. The :func:`drain_aware` decorator captures the
pattern once so per-tool refactors stay strictly additive: a new tool
adds ``@drain_aware`` directly under ``@mcp.tool()`` and inherits the
contract for free.

Decorator order
---------------

``@mcp.tool()`` registers the function on the FastMCP instance — it
must therefore see the *wrapped* function (the one that knows about
the drain check), not the bare body. The correct stacking is::

    @mcp.tool(name="fsm.example", description="…")
    @drain_aware
    def fsm_example(...) -> ...:
        ...

In Python decorator stacking, the bottom decorator runs first, so
``drain_aware`` wraps the body and ``mcp.tool`` then registers the
wrapped callable. Reversing the order would register the *bare* body
on FastMCP and the drain check would never fire because FastMCP would
call the inner function directly.

Sync vs async
-------------

Every ``fsm.*`` tool in the W4 surface is synchronous, but FastMCP
also supports async tool bodies, and a future tool that talks to a
network might well be ``async def``. We therefore branch on
:func:`inspect.iscoroutinefunction` and emit either a sync or an
async wrapper as appropriate — both wrappers carry the same drain
semantics, expressed in their respective dialects. The detection runs
once at decorator application time so there is no per-call
inspect-cost.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from functools import wraps
from typing import Any, cast

from ctxr.fsm.mcp._drain import (
    in_flight_decrement,
    in_flight_increment,
    is_draining,
)
from ctxr.fsm.mcp._errors import McpToolError, as_error

__all__ = ["drain_aware"]


# Module logger — keeps drain-decorator diagnostics distinct from the
# per-tool loggers in tools_meta / tools_runs / tools_events.
_LOG = logging.getLogger("ctxr.fsm.mcp._drain_decorator")


# Stable error message returned when a call lands during a drain. The
# wording explicitly tells the client this is transient (retry after
# the restart settles) so a downstream LLM driver does not escalate
# the drain into a hard failure narrative.
_DRAIN_ERROR_CODE: str = "server_draining"
_DRAIN_ERROR_DETAIL: str = (
    "MCP server is draining for reload; retry the call in a moment"
)


def _draining_envelope() -> McpToolError:
    """Build the structured ``server_draining`` error envelope.

    Centralised so a future change to the wording / payload only needs
    to land here, not in every wrapper closure. The envelope mirrors
    the rest of the MCP error contract (see
    :mod:`ctxr.fsm.mcp._errors`) so clients route it through the same
    branching they already apply to ``project_not_bound`` and friends.
    """
    return as_error(_DRAIN_ERROR_CODE, detail=_DRAIN_ERROR_DETAIL)


def drain_aware[F: Callable[..., Any]](fn: F) -> F:
    """Wrap a FastMCP tool body with the graceful-drain contract.

    The returned wrapper:

    * Checks :func:`is_draining` on entry. If a drain is in progress,
      it returns the structured ``server_draining`` :class:`McpToolError`
      envelope *without* incrementing the in-flight counter — the
      whole point of draining is to refuse new work, so counting a
      rejected call would only delay the shutdown.
    * Otherwise increments the in-flight counter (so the supervisor's
      :func:`wait_for_drain` can see the call as "still running"),
      runs the wrapped function, and decrements the counter in a
      ``finally`` so an exception in the tool body still releases the
      slot.

    Works on both sync and async tool bodies — FastMCP supports both
    and we want one decorator to cover both call shapes.
    Coroutine-vs-function detection happens once at decorator
    application time so the per-call cost is zero.
    """
    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            # Reject early on drain so the counter never moves for a
            # call we are not going to honour. The bare ``return``
            # of the error envelope mirrors what the existing tool
            # bodies do — FastMCP wraps it under
            # ``structuredContent.result`` just the same.
            if is_draining():
                _LOG.debug(
                    "drain-aware async tool %s refused: server draining",
                    getattr(fn, "__qualname__", fn.__name__),
                )
                return _draining_envelope()

            in_flight_increment()
            try:
                return await fn(*args, **kwargs)
            finally:
                # ``finally`` (not ``except``) — the slot must be
                # released even on a clean return; the decrement is
                # *not* guarded by an exception-only branch.
                in_flight_decrement()

        return cast(F, async_wrapper)

    @wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        if is_draining():
            _LOG.debug(
                "drain-aware tool %s refused: server draining",
                getattr(fn, "__qualname__", fn.__name__),
            )
            return _draining_envelope()

        in_flight_increment()
        try:
            return fn(*args, **kwargs)
        finally:
            in_flight_decrement()

    return cast(F, sync_wrapper)
