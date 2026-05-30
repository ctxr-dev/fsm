"""MCP tool registrations.

This module is imported by :mod:`ctxr.fsm.mcp` for the side effect of
registering every ``@mcp.tool()``-decorated function on the package's
FastMCP instance. The actual tool implementations land in the next
phase of W4; for now this file is intentionally empty so the W4
bootstrap (server entry point, error contract, project handle) can
land independently and the smoke test (``from ctxr.fsm.mcp import mcp``)
proves the wiring is sound before any tool surface is committed.

When tools are added, prefer one sub-module per logical group (e.g.
``tools_runs.py``, ``tools_states.py``, ``tools_commit.py``) and
re-export from this aggregator so the package keeps a single, stable
"import this for decorator side effects" entry point.
"""

from __future__ import annotations

# Importing each sub-module runs its ``@mcp.tool()`` decorators, which
# is the side-effect that registers the tools onto the FastMCP instance
# in :mod:`ctxr.fsm.mcp`. Keep this list alphabetical so additions are
# easy to review.
from ctxr.fsm.mcp import (
    tools_events,  # noqa: F401  (side-effect import)
    tools_meta,  # noqa: F401  (side-effect import)
    tools_runs,  # noqa: F401  (side-effect import)
)

__all__: list[str] = []
