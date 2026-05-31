"""End-to-end test for the ``fsm.healthcheck`` MCP tool.

This integration test boots ``ctxr-fsm mcp`` as a real subprocess via
the official MCP Python SDK's stdio client (``mcp.client.stdio``) and
issues a ``fsm.healthcheck`` call across the JSON-RPC pipe. Asserting
on the wire-level response — rather than calling the tool function
directly — gives us coverage of:

* the CLI shim (``ctxr-fsm mcp`` -> ``ctxr.fsm.cli.mcp_cmd.mcp``);
* the server boot sequence (``Project.open`` + ``set_project``
  + logging-to-stderr + signal handlers);
* the FastMCP tool registration (the ``@mcp.tool(name="fsm.healthcheck")``
  decorator must actually expose the tool over stdio);
* the structured-output round-trip (Pydantic models -> JSON-RPC
  ``structuredContent`` -> Pydantic re-parse in the client).

These tests are significantly slower than the in-process unit tests
(subprocess spawn + MCP initialize handshake + SQLite migration on a
brand-new DB) — expect 5-15s per case — so they are kept narrow and
deliberately small in number. The matching in-process unit tests cover
the tool body's branching exhaustively.

MCP SDK API used
----------------

* ``mcp.client.stdio.StdioServerParameters`` — describes the child
  process (command + args + cwd + env). Used keyword-only here per the
  SDK 1.27 signature.
* ``mcp.client.stdio.stdio_client`` — async context manager yielding
  the ``(read_stream, write_stream)`` pair, spawning the subprocess
  and tearing it down on exit.
* ``mcp.ClientSession`` — wraps the stream pair into a JSON-RPC
  session; we call ``initialize()`` once and then drive ``call_tool``
  to invoke ``fsm.healthcheck``.

Output shape: ``ClientSession.call_tool`` returns
``mcp.types.CallToolResult``. For FastMCP-decorated tools that return a
Pydantic model, the structured payload arrives under
``CallToolResult.structuredContent["result"]`` (a dict with the model
fields). The unstructured ``content`` list also carries the same data
as a single ``TextContent`` JSON-encoded blob — we assert on the
structured form because it is the typed contract clients are expected
to consume.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# The integration loop spawns a subprocess that runs Alembic migrations
# against a brand-new SQLite file and then completes the MCP initialize
# handshake; on cold caches that can comfortably take 10-15 seconds.
# We bound each tool round-trip with a generous async timeout so a hung
# subprocess fails the suite loudly instead of hanging CI indefinitely.
_TOOL_CALL_TIMEOUT_SECONDS: float = 30.0


def _server_params(db_path: Path) -> StdioServerParameters:
    """Build the ``StdioServerParameters`` for a per-test ctxr-fsm mcp run.

    We invoke through ``uv run`` so the subprocess picks up the project's
    virtualenv and lockfile-pinned dependencies (matching what an
    operator would type at the shell). The DB path is passed via the
    ``--db`` flag so the server's ``resolve_db_path`` precedence is
    exercised exactly as a real client would exercise it.

    The child inherits the parent's environment so any locally-set
    ``UV_*`` / ``PYTHONPATH`` overrides are preserved — important for
    running these tests inside the developer-loop virtualenv as well as
    in CI's clean shell. We do NOT pass ``cwd`` explicitly so the child
    starts in the project root (the test runner's cwd), which keeps the
    ``uv`` invocation aligned with the project's ``uv.lock``.
    """
    return StdioServerParameters(
        command="uv",
        args=["run", "ctxr-fsm", "mcp", "--db", str(db_path)],
        env=dict(os.environ),
    )


async def _call_healthcheck(db_path: Path) -> dict[str, Any]:
    """Spawn the MCP server, call ``fsm.healthcheck``, and return the dict.

    The structured payload sits at ``CallToolResult.structuredContent``
    under the ``"result"`` key — FastMCP wraps a single typed return
    value in that envelope so the JSON-RPC ``structuredContent`` field
    is always an object even when the tool returns a list or a scalar.

    On any tool-level error (``isError=True``) we still return the
    structured payload so the test body can render a helpful failure
    message; the assertion that ``isError is False`` lives in the
    caller.
    """
    async with (
        stdio_client(_server_params(db_path)) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await asyncio.wait_for(
            session.call_tool("fsm.healthcheck", {}),
            timeout=_TOOL_CALL_TIMEOUT_SECONDS,
        )

    # ``structuredContent`` is the typed envelope; ``content`` is the
    # plain-text fallback FastMCP also emits for clients that do not
    # yet consume the structured form. We return both so the assertion
    # on the structured shape can fall back to a clear diagnostic if
    # the structured envelope is unexpectedly missing.
    return {
        "isError": bool(result.isError),
        "structuredContent": result.structuredContent,
        "content": [
            {"type": item.type, "text": getattr(item, "text", None)}
            for item in result.content
        ],
    }


def _extract_healthcheck_payload(envelope: dict[str, Any]) -> dict[str, Any]:
    """Pull the ``HealthcheckResult`` dict out of a CallToolResult envelope.

    FastMCP wraps a Pydantic return value as
    ``structuredContent = {"result": <model dict>}``. We assert on the
    presence of that wrapper here (with a helpful message) so the test
    body downstream can focus on field-level assertions instead of
    juggling the envelope shape.
    """
    structured = envelope["structuredContent"]
    assert structured is not None, (
        f"fsm.healthcheck did not return a structuredContent envelope; "
        f"got isError={envelope['isError']!r}, content={envelope['content']!r}"
    )
    assert "result" in structured, (
        f"fsm.healthcheck structuredContent missing 'result' wrapper key; "
        f"got keys={list(structured)!r}"
    )
    payload = structured["result"]
    assert isinstance(payload, dict), (
        f"fsm.healthcheck 'result' should be a dict; got {type(payload).__name__}"
    )
    return payload


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_healthcheck_returns_status_ok() -> None:
    """``fsm.healthcheck`` returns ``status=="ok"`` on a fresh DB.

    The server boots against a brand-new SQLite file (so the Alembic
    upgrade runs as part of ``Project.open(..., migrate=True)``), and
    we expect the healthcheck to report a happy, fully-initialised
    server. Asserting on ``status`` first gives the cleanest
    diagnostic when the round-trip fails for any reason — a non-"ok"
    status means the tool body returned an error envelope, which the
    next assertion would obscure.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        envelope = asyncio.run(_call_healthcheck(db_path))

    assert envelope["isError"] is False, (
        f"fsm.healthcheck returned isError=True; envelope={envelope!r}"
    )

    payload = _extract_healthcheck_payload(envelope)
    assert payload.get("status") == "ok", (
        f"expected status='ok', got payload={payload!r}"
    )


def test_healthcheck_db_path_matches_db_flag() -> None:
    """``fsm.healthcheck`` echoes back the same DB path we passed to ``--db``.

    ``resolve_db_path`` canonicalises the path with ``Path.resolve()``,
    which on macOS prepends ``/private`` to anything under ``/tmp``.
    We therefore compare against ``db_path.resolve()`` rather than the
    raw ``db_path`` we constructed, so the test stays correct on every
    platform (macOS, Linux CI runners, container layers with symlinks
    in the temp tree).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        envelope = asyncio.run(_call_healthcheck(db_path))

    assert envelope["isError"] is False, (
        f"fsm.healthcheck returned isError=True; envelope={envelope!r}"
    )

    payload = _extract_healthcheck_payload(envelope)
    reported = payload.get("db_path")
    assert reported is not None, (
        f"fsm.healthcheck payload missing 'db_path' field; payload={payload!r}"
    )

    # Compare resolved paths — the server canonicalises via Path.resolve(),
    # so a literal string compare against str(db_path) would fail on macOS
    # where /tmp is a symlink to /private/tmp. ``Path(...).resolve()`` on
    # both sides normalises the representation regardless of host quirks.
    assert Path(reported).resolve() == db_path.resolve(), (
        f"reported db_path {reported!r} does not match --db argument "
        f"{db_path!s} (resolved: {db_path.resolve()!s})"
    )


def test_healthcheck_reports_alembic_revision_and_package_version() -> None:
    """The healthcheck surfaces the alembic head and the package version.

    ``Project.open(..., migrate=True)`` runs the Alembic upgrade as part
    of the boot sequence, so the very first call against a fresh DB
    should already report a non-``None`` revision. ``package_version``
    must always be the installed ``ctxr.fsm`` version — a non-empty
    string so client-side version pinning has something to compare.

    This case is folded into the same suite as the basic status check
    because the SDK round-trip dominates the runtime: amortising the
    handshake cost across the two assertions keeps the integration
    suite cheap to run.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        envelope = asyncio.run(_call_healthcheck(db_path))

    assert envelope["isError"] is False, (
        f"fsm.healthcheck returned isError=True; envelope={envelope!r}"
    )

    payload = _extract_healthcheck_payload(envelope)

    # Alembic head — must be present because the server boots with
    # ``migrate=True``. ``None`` here would mean the upgrade silently
    # failed to populate the ``alembic_version`` table.
    revision = payload.get("alembic_revision")
    assert isinstance(revision, str) and revision, (
        f"expected non-empty alembic_revision string; payload={payload!r}"
    )

    # Package version — must be the installed ctxr.fsm version. We
    # don't pin a literal here (the package version evolves on every
    # release) so the assertion is "non-empty string" plus a sanity
    # check that it looks PEP-440-ish (contains a dot).
    pkg_version = payload.get("package_version")
    assert isinstance(pkg_version, str) and "." in pkg_version, (
        f"expected dotted package_version string; payload={payload!r}"
    )


if __name__ == "__main__":  # pragma: no cover - manual debug path
    # Allow ``python tests/integration/mcp/test_mcp_healthcheck.py`` to
    # run the suite under pytest without remembering the full module
    # path. Handy when iterating on the test body locally.
    raise SystemExit(pytest.main([__file__, "-v"]))
