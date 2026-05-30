"""Structured error envelopes returned by MCP tools.

The legacy JavaScript MCP server in ``legacy-js/`` returns errors as
``{"error": "<snake_case_code>", ...}`` objects rather than throwing
JSON-RPC errors so clients can handle them as first-class tool
results. We keep the same contract for the Python rewrite so any
existing client that already speaks the legacy shape works unchanged
against the new server.

Every tool wraps its body in ``try/except`` and on failure returns an
:class:`McpToolError` (or a dict produced by :func:`as_error`) rather
than letting the exception propagate to FastMCP — propagating would
produce a JSON-RPC error frame, which is a different code path that
older clients did not have to handle.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "McpToolError",
    "as_error",
]


class McpToolError(BaseModel):
    """Structured error returned by an MCP tool.

    Attributes:
        error: Snake-case error code (e.g. ``"spec_not_found"``,
            ``"invalid_state"``). Stable across versions so clients can
            branch on it; new codes are additive.
        detail: Optional human-readable message giving more context.
            Safe to surface to end users / log lines.
        payload: Optional structured data attached to the error (for
            example the validation report for ``schema_validation_failed``).
            Schema is per-error-code; clients that don't recognise the
            code should treat this as an opaque blob.
    """

    error: str = Field(..., description="Snake-case error code.")
    detail: str | None = Field(
        default=None,
        description="Optional human-readable explanation.",
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description="Optional structured payload attached to the error.",
    )


def as_error(
    code: str,
    detail: str | None = None,
    **payload: Any,
) -> McpToolError:
    """Construct an :class:`McpToolError` from positional fields and kwargs.

    ``code`` is the snake-case error tag (mandatory). ``detail`` is the
    human-readable message. Any remaining keyword arguments are bundled
    into the ``payload`` dict — the ``**kw`` shape lets the call site
    read as ``as_error("invalid_state", state_id="x", expected="ready")``
    without manually constructing a dict.

    Passing zero keyword arguments leaves ``payload`` as ``None`` rather
    than an empty dict, so the serialised JSON omits the field entirely
    in the common case where there is nothing extra to attach.
    """
    return McpToolError(
        error=code,
        detail=detail,
        payload=payload or None,
    )
