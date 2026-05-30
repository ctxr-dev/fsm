"""End-to-end MCP handshake tests for the ``ctxr-fsm mcp`` server.

These tests spawn ``ctxr-fsm mcp --db <tmp>`` as a real subprocess
and drive it through the official MCP Python SDK's stdio client
(``mcp.client.stdio.stdio_client`` + ``mcp.ClientSession``). They lock
two wire contracts independently from the in-process unit tests:

1. The MCP ``initialize`` handshake completes successfully and the
   advertised :class:`~mcp.types.ServerCapabilities` includes a
   non-empty ``tools`` capability. Without this, MCP clients refuse to
   call any tool — so this is the smallest possible smoke test that
   proves the FastMCP wiring is sound.

2. ``tools/list`` returns every ``fsm.*`` tool the W4 wave promised.
   The expected set is enumerated below; new tools added in later
   waves should append to the set so a missing registration fails the
   suite at the source of the regression (the ``tools/list`` round
   trip) rather than later when a downstream test tries to invoke a
   ghost tool.

Implementation notes
--------------------

* MCP SDK API used: ``StdioServerParameters(command=..., args=...)``
  →  ``async with stdio_client(params) as (read, write):`` →
  ``async with ClientSession(read, write) as session:`` →
  ``await session.initialize()`` / ``await session.list_tools()``.
  This matches the public surface documented for the 1.x SDK line and
  is what the SDK's own examples use.

* Why ``asyncio.run`` instead of a ``pytest-asyncio`` fixture? The
  surrounding repo has no ``asyncio_mode = "auto"`` pytest config, so
  we keep each test a sync function that drives a single
  ``asyncio.run(...)`` call. This avoids depending on the plugin's
  event-loop scope semantics and keeps the test trivially
  reproducible at the REPL (you can copy the inner coroutine into a
  Python shell and run it directly).

* Each test creates its own ``TemporaryDirectory`` so the SQLite file
  is isolated. The server boots with ``migrate=True`` so the empty
  database is auto-upgraded to the current alembic head — no test
  setup needs to ``alembic upgrade head`` first.

* The subprocess is launched as ``uv run ctxr-fsm mcp --db <tmp>`` so
  the test honours the project's tooling contract (no manual venv
  activation, the same command operators would copy from the README).
  We pass ``cwd`` explicitly to insulate the test from whatever
  directory pytest was invoked from.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# The complete set of MCP tools the W4 wave promises. Any new tool
# added to ``ctxr.fsm.mcp`` should be appended here so a missing
# decorator-side-effect registration trips this test immediately
# instead of failing further downstream when something tries to call
# the unregistered name.
#
# Names use the canonical ``fsm.<verb>`` namespace so the MCP client
# never has to guess whether a tool came from this server.
EXPECTED_TOOLS: frozenset[str] = frozenset(
    {
        # tools_meta.py
        "fsm.healthcheck",
        "fsm.list_specs",
        "fsm.register_spec",
        "fsm.observe_tool_call",
        # tools_runs.py
        "fsm.start_run",
        "fsm.get_brief",
        "fsm.commit_outputs",
        "fsm.confirm_commit",
        "fsm.resume_run",
        "fsm.abort_run",
        "fsm.list_runs",
        "fsm.get_run",
        # tools_events.py
        "fsm.subscribe_events",
        "fsm.inspect_journal",
        "fsm.recover_journal",
        "fsm.list_consumers",
        "fsm.list_producers",
    }
)


# Generous timeout for the full spawn + handshake + tools/list round
# trip. The dominant cost is the ``uv run`` import-time penalty on a
# cold cache (FastMCP + SQLAlchemy + Alembic pulled in for every fresh
# Python process); 60 s leaves plenty of slack for slow CI runners
# without masking a genuinely hung handshake.
HANDSHAKE_TIMEOUT_S: float = 60.0


def _server_params(db_path: Path) -> StdioServerParameters:
    """Build the stdio-client launch parameters.

    ``uv run`` is the project's canonical "run this command inside the
    managed venv" wrapper — invoking it (rather than the
    ``.venv/bin/ctxr-fsm`` script directly) keeps the test honest about
    the operator-facing command and means it works on a clean checkout
    where ``.venv`` may not yet exist.

    The repo root is computed from ``__file__`` so the test runs
    correctly regardless of the directory pytest was launched from.
    """

    repo_root = Path(__file__).resolve().parents[3]
    return StdioServerParameters(
        command="uv",
        args=["run", "ctxr-fsm", "mcp", "--db", str(db_path)],
        cwd=str(repo_root),
    )


async def _handshake_and_list(db_path: Path) -> tuple[object, list[object]]:
    """Run the full ``initialize`` + ``tools/list`` round trip.

    Returns the raw ``InitializeResult`` and the list of ``Tool``
    objects so the individual tests can make their own assertions
    without re-establishing the connection (which is the slow part).
    """

    params = _server_params(db_path)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init_result = await session.initialize()
        list_result = await session.list_tools()
        return init_result, list_result.tools


def _run_handshake(db_path: Path) -> tuple[object, list[object]]:
    """Synchronous wrapper around :func:`_handshake_and_list`.

    Centralises the ``asyncio.run`` + ``asyncio.wait_for`` boilerplate
    so each test body stays a flat sequence of assertions. The wrapper
    raises :class:`asyncio.TimeoutError` if the handshake exceeds
    :data:`HANDSHAKE_TIMEOUT_S` — a deliberate fail-loud signal that
    the server is hung rather than the test silently waiting forever.
    """

    async def _wrapped() -> tuple[object, list[object]]:
        return await asyncio.wait_for(
            _handshake_and_list(db_path), timeout=HANDSHAKE_TIMEOUT_S
        )

    return asyncio.run(_wrapped())


def test_initialize_advertises_tools_capability() -> None:
    """``initialize`` must succeed and advertise the ``tools`` capability.

    MCP clients gate every ``tools/*`` call on the server's advertised
    capabilities — if ``capabilities.tools`` is ``None`` the client
    will refuse to send ``tools/list`` even when the server has tools
    registered. This test therefore asserts the *advertisement*, not
    just the presence of the tools themselves (covered by the sibling
    test below).

    We also pin a couple of identity fields (``serverInfo.name`` and
    ``protocolVersion``) so a regression that flips the FastMCP
    instance's name or forgets to negotiate a protocol version is
    caught here instead of in a downstream client.
    """

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        init_result, _tools = _run_handshake(db_path)

        # ``capabilities.tools`` is an Optional[ToolsCapability]; the
        # *presence* of the object (vs. ``None``) is what tells clients
        # they may call ``tools/list``. We don't care about the inner
        # ``listChanged`` flag — FastMCP defaults it sensibly.
        assert init_result.capabilities.tools is not None, (
            "MCP server must advertise the 'tools' capability so clients "
            "may call tools/list; capabilities.tools was None"
        )

        # Identity guard: a typo in the FastMCP constructor name would
        # silently break every client that looks the server up by name
        # (mcp.json registries do exactly that).
        assert init_result.serverInfo.name == "ctxr-fsm", (
            f"unexpected MCP serverInfo.name {init_result.serverInfo.name!r}; "
            "expected 'ctxr-fsm'"
        )

        # protocolVersion is a free-form string negotiated by the SDK;
        # we only assert it is set so a future SDK upgrade that forgets
        # to negotiate a version fails loudly.
        assert init_result.protocolVersion, "protocolVersion must be a non-empty string"


def test_tools_list_contains_every_expected_fsm_tool() -> None:
    """``tools/list`` must return every ``fsm.*`` tool W4 promised.

    The assertion is a *superset* check rather than equality so future
    waves can add tools without breaking this test — but every tool in
    :data:`EXPECTED_TOOLS` MUST be present. A missing tool means a
    decorator side-effect registration was lost (typically the
    ``from ctxr.fsm.mcp import tools_X`` import got dropped from the
    aggregator).

    We deliberately compare names only — the tool schemas are a
    separate contract validated by the per-tool integration tests.
    Comparing only names also keeps this test resilient to harmless
    docstring / description tweaks.
    """

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        _init_result, tools = _run_handshake(db_path)

        listed = {t.name for t in tools}
        missing = EXPECTED_TOOLS - listed
        assert not missing, (
            f"MCP server is missing expected fsm.* tools: {sorted(missing)}; "
            f"listed tools were: {sorted(listed)}"
        )

        # Sanity guard: every tool the server lists must live under the
        # ``fsm.`` namespace. A stray non-namespaced tool would collide
        # with whatever other servers a client has mounted and is
        # almost certainly a bug.
        non_namespaced = {n for n in listed if not n.startswith("fsm.")}
        assert not non_namespaced, (
            f"MCP server registered non-namespaced tools (must start "
            f"with 'fsm.'): {sorted(non_namespaced)}"
        )


if __name__ == "__main__":
    # Allow ``python tests/integration/mcp/test_mcp_handshake.py`` as a
    # quick manual smoke. Pytest is still the canonical entry point.
    raise SystemExit(pytest.main([__file__, "-v"]))
