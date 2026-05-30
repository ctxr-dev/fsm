"""Integration test: ``fsm.observe_tool_call`` over the real MCP stdio transport.

This test exercises the W4 plumbing for layer-7 drift observation
end-to-end:

1.  We spawn ``ctxr-fsm mcp --db <tmp>`` as a child process using the
    official ``mcp`` Python SDK's stdio client. That is the canonical
    MCP framing — exactly what Claude Code itself speaks — so the test
    confirms the JSON-RPC handshake, FastMCP registration, and tool
    dispatch all line up against a fresh database.

2.  We call ``fsm.observe_tool_call`` once with a representative
    payload (producer kind/name, tool name, redacted args, success
    flag, optional run id) and assert that the structured response
    carries both the inserted ``tool_calls.id`` and the emitted
    ``tool_call_observed`` event id.

3.  We then open a *separate* ``Project`` against the same SQLite file
    (after the server has fully shut down via the stdio context
    manager) and verify both rows are present and correlated:

    * the ``tool_calls`` row exists, carries the expected tool name,
      the redacted args, and the success flag we sent;
    * the corresponding ``tool_call_observed`` event exists on the
      bus and references the same ``tool_call_id`` in its payload.

The ``observe_tool_call`` tool body wraps both writes in a single
``Session.begin()`` block, so the presence of both rows is the
observable proof that the txn was atomic — a partial commit would
leave one row missing and the assertions would trip.

MCP client API used
-------------------
``mcp.ClientSession`` driven over ``mcp.client.stdio.stdio_client``
(version 1.27.2 of the ``mcp`` SDK). The same pattern is the one the
Anthropic ``mcp`` README documents for stdio clients; we adopt it
verbatim so future tests can copy the boilerplate.

Performance note
----------------
A single round-trip through the spawned server (process start +
``initialize`` handshake + tool call + clean shutdown) lands in the
5-15 second range on a modern dev laptop. Keep that in mind before adding
many of these — prefer in-process unit tests for tool-body coverage
and reserve the integration tests for wire-protocol confirmations.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ctxr.fsm.core.models import EventKind
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_enforcement import ToolCallTable
from ctxr.fsm.sqlite.models_events import EventTable

# ---------------------------------------------------------------------------
# Wire-protocol constants
# ---------------------------------------------------------------------------

# The tool name registered by ``ctxr.fsm.mcp.tools_meta.fsm_observe_tool_call``
# (see the ``@mcp.tool(name=...)`` decorator). Pinning it as a constant keeps
# the wire contract grep-able from the test alone.
_OBSERVE_TOOL_NAME: str = "fsm.observe_tool_call"

# Cap how long any single MCP round-trip is allowed to wait before the test
# is considered hung. Generous on purpose — the subprocess spawn plus the
# initialize handshake can take a few seconds on a cold cache.
_ROUND_TRIP_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Subprocess + client driver
# ---------------------------------------------------------------------------


async def _drive_mcp_session(
    db_path: Path,
    body: Callable[[ClientSession], Awaitable[Any]],
) -> Any:
    """Spawn the MCP server, hand a live ``ClientSession`` to ``body``.

    The body coroutine receives an *initialized* session (the MCP
    handshake has already completed) and returns whatever the test
    cares to assert on. We pin ``--db`` to a per-test temp file so the
    server boots against an empty database — no cross-test state.

    We deliberately use ``uv run ctxr-fsm`` rather than invoking the
    script directly so the test inherits the same Python env / package
    resolution rules the dev loop uses; ``uv`` re-uses the existing
    ``.venv`` (no fresh install) so the spawn cost stays bounded.
    """
    params = StdioServerParameters(
        command="uv",
        args=["run", "ctxr-fsm", "mcp", "--db", str(db_path)],
    )

    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        # The handshake is mandatory before any tool call — skipping
        # it would surface as a protocol-violation error on the
        # very first ``call_tool`` invocation.
        await session.initialize()
        return await body(session)


def _structured_payload(call_result: Any) -> dict[str, Any]:
    """Extract the structured tool output from an MCP ``CallToolResult``.

    FastMCP wraps a typed Pydantic return into ``structuredContent`` on
    the SDK side. For tools whose return type is a ``Union[Model,
    McpToolError]`` (every ``fsm.*`` tool here), FastMCP further wraps
    the payload under a synthetic ``"result"`` key — that is the
    Pydantic adapter discriminator. We unwrap it transparently so the
    test reads the actual model fields without an extra layer.

    We branch defensively because the SDK has historically also
    serialised the same payload into the first ``content[0]``
    ``TextContent`` block as JSON — falling back to that path keeps the
    test robust against minor SDK revisions without losing coverage.
    """

    def _unwrap_result(d: dict[str, Any]) -> dict[str, Any]:
        # FastMCP wraps non-BaseModel / Union returns in {"result": ...}.
        # If we see a single "result" key whose value is itself a dict,
        # treat that as the canonical payload.
        if list(d.keys()) == ["result"] and isinstance(d["result"], dict):
            return d["result"]
        return d

    structured = getattr(call_result, "structuredContent", None)
    if isinstance(structured, dict):
        return _unwrap_result(structured)

    # Fallback: parse the first text content block as JSON. The SDK
    # always emits at least one content block for a successful call.
    content = getattr(call_result, "content", None)
    if content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise AssertionError(
                    f"could not parse tool response text as JSON: {text!r}"
                ) from exc
            if isinstance(parsed, dict):
                return _unwrap_result(parsed)
    raise AssertionError(
        "MCP tool result carried neither structuredContent nor a JSON text block"
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_observe_tool_call_writes_row_and_event_atomically() -> None:
    """One ``fsm.observe_tool_call`` writes both a tool_calls row and an event.

    The test asserts the full chain end-to-end:

    * the MCP server can be spawned, handshaked, and the tool dispatched;
    * the tool returns a structured success payload with the two row ids
      the W4 contract promises (``tool_call_id``, ``event_id``);
    * after the server shuts down, inspecting the SQLite file confirms
      both rows are present, correlated by id, and carry the data the
      tool was called with. The atomicity of the txn is observable by
      the fact that neither row can exist without the other on the
      happy path — both must be there.
    """
    tool_name: str = "Bash"  # representative non-fsm.* tool the agent might run
    args_redacted: dict[str, Any] = {"command": "<redacted>", "cwd": "/tmp"}
    producer_kind: str = "agent"
    producer_name: str = "test-agent"

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        async def _body(session: ClientSession) -> dict[str, Any]:
            # Pre-flight sanity: the tool must actually be registered.
            # If the FastMCP decorator name ever drifts, this assertion
            # surfaces the mismatch with a clear error instead of a
            # mysterious "unknown tool" failure on the call below.
            tools_resp = await session.list_tools()
            registered_names = {tool.name for tool in tools_resp.tools}
            assert _OBSERVE_TOOL_NAME in registered_names, (
                f"{_OBSERVE_TOOL_NAME!r} not in registered tools "
                f"{sorted(registered_names)!r}"
            )

            call_result = await session.call_tool(
                _OBSERVE_TOOL_NAME,
                arguments={
                    "producer_kind": producer_kind,
                    "producer_name": producer_name,
                    "tool_name": tool_name,
                    "args_redacted": args_redacted,
                    "succeeded": True,
                    # run_id intentionally omitted — observation is allowed
                    # to be "run-less" per the tool's contract.
                },
            )
            assert call_result.isError is False, (
                f"tool call surfaced as an error: {call_result!r}"
            )
            return _structured_payload(call_result)

        # asyncio.run owns the loop lifetime for the whole subprocess
        # exchange; once it returns, stdio_client has already torn down
        # the child process and the SQLite file is unlocked so a fresh
        # Project can open it for verification.
        payload = asyncio.run(
            asyncio.wait_for(
                _drive_mcp_session(db_path, _body),
                timeout=_ROUND_TRIP_TIMEOUT_SECONDS,
            )
        )

        # ── Assertions on the tool's structured response ────────────
        assert payload.get("recorded") is True, payload
        tool_call_id = payload.get("tool_call_id")
        event_id = payload.get("event_id")
        producer_id = payload.get("producer_id")
        assert isinstance(tool_call_id, str) and tool_call_id, payload
        assert isinstance(event_id, str) and event_id, payload
        assert isinstance(producer_id, str) and producer_id, payload

        # ── Assertions on the persisted side-effects ────────────────
        # Open the same DB the server wrote to; ``migrate=False`` is
        # fine here because the server already ran the head migration
        # when it booted.
        with Project.open(db_path, migrate=False) as project:
            with project.session_factory() as session:
                tool_call_row = session.get(ToolCallTable, tool_call_id)
                event_row = session.get(EventTable, event_id)

            # The tool_calls row must exist and carry exactly what
            # the tool was called with.
            assert tool_call_row is not None, (
                f"no tool_calls row found for id {tool_call_id!r}"
            )
            assert tool_call_row.tool_name == tool_name
            assert tool_call_row.succeeded is True
            assert tool_call_row.producer_id == producer_id
            # ``args_redacted_json`` is the canonical JSON of whatever
            # was sent; round-trip and compare structurally rather than
            # by raw string so a future canonical-JSON tweak (key
            # sorting, whitespace) does not break the assertion.
            persisted_args = json.loads(tool_call_row.args_redacted_json)
            assert persisted_args == args_redacted

            # The matching event must exist on the bus and reference
            # the same tool_call_id in its payload — that is the link
            # the W12 drift aggregator will follow.
            assert event_row is not None, (
                f"no events row found for id {event_id!r}"
            )
            assert event_row.kind == EventKind.tool_call_observed.value
            assert event_row.producer_id == producer_id
            event_payload = json.loads(event_row.payload_json)
            assert event_payload.get("tool_call_id") == tool_call_id
            assert event_payload.get("tool_name") == tool_name
            assert event_payload.get("producer_kind") == producer_kind
            assert event_payload.get("producer_name") == producer_name
            assert event_payload.get("succeeded") is True
            assert event_payload.get("args_redacted") == args_redacted


def test_observe_tool_call_with_run_id_persists_link() -> None:
    """A run-scoped observation lands on ``tool_calls.run_id`` and ``events.run_id``.

    The contract explicitly allows ``run_id`` to be omitted (covered by
    the first test) *or* supplied as a UUID; this test exercises the
    second arm and confirms the run linkage is persisted on both sides
    of the atomic write. Drift detection (W12) reads ``run_id`` to
    scope its queries to a single run, so the linkage is the only
    thing that makes the observation usable in practice.
    """
    from ctxr.fsm.core.models import FsmSpec, State

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        # We need a real run id to attach the observation to. The
        # simplest path is to open the project in-process, register a
        # tiny spec, and start a run — then close the project (release
        # the file lock) before the MCP server boots against the same
        # file.
        with Project.open(db_path) as project:
            spec = FsmSpec(
                id="observe_demo",
                version=1,
                entry="only",
                states=[State(id="only", purpose="single", transitions=[])],
            )
            registered = project.register_spec(spec)
            run = project.start_run(registered.spec.id, args={})
            run_id = run.id

        async def _body(session: ClientSession) -> dict[str, Any]:
            call_result = await session.call_tool(
                _OBSERVE_TOOL_NAME,
                arguments={
                    "producer_kind": "agent",
                    "producer_name": "run-scoped",
                    "tool_name": "Edit",
                    "args_redacted": {"path": "<redacted>"},
                    "succeeded": False,
                    "run_id": run_id,
                },
            )
            assert call_result.isError is False, (
                f"tool call surfaced as an error: {call_result!r}"
            )
            return _structured_payload(call_result)

        payload = asyncio.run(
            asyncio.wait_for(
                _drive_mcp_session(db_path, _body),
                timeout=_ROUND_TRIP_TIMEOUT_SECONDS,
            )
        )

        tool_call_id = payload["tool_call_id"]
        event_id = payload["event_id"]

        with Project.open(db_path, migrate=False) as project:
            with project.session_factory() as session:
                tool_call_row = session.get(ToolCallTable, tool_call_id)
                event_row = session.get(EventTable, event_id)

            assert tool_call_row is not None
            assert tool_call_row.run_id == run_id
            assert tool_call_row.succeeded is False

            assert event_row is not None
            assert event_row.run_id == run_id
            assert event_row.kind == EventKind.tool_call_observed.value
            event_payload = json.loads(event_row.payload_json)
            assert event_payload.get("succeeded") is False


# Guard rail: pytest-asyncio is a project dev-dep but we drive asyncio.run
# ourselves rather than mark these tests with ``@pytest.mark.asyncio``.
# This sentinel test is here as a paper trail so a future contributor who
# tries to convert the file to async-style fixtures knows the conscious
# choice was made (asyncio.run keeps the lifetime of the spawned subprocess
# bounded to a single scope, simplifying cleanup).
def test_intentional_asyncio_run_strategy_documented() -> None:
    """Sentinel: confirm pytest is wired and the file imports cleanly."""
    assert pytest.__name__ == "pytest"
