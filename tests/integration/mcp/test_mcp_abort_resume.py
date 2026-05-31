"""End-to-end integration tests for ``fsm.abort_run`` and ``fsm.resume_run``.

These tests exercise the MCP server **over its actual transport** — they
spawn ``uv run ctxr-fsm mcp --db <tmp>`` as a subprocess, hand-shake the
JSON-RPC framing through the official MCP Python SDK's stdio client, and
assert the side-effects against the SQLite substrate after the server
exits.

Why subprocess-level coverage matters
-------------------------------------

The W4 plumbing is reached by clients **only** through the stdio
transport. In-process tests against the tool functions can pass while
the transport-level boot sequence (logging-to-stderr, project binding,
signal handlers, FastMCP's input-model unwrap) silently regresses. The
two abort / resume tests both:

* spawn the server fresh against a per-test ``tempfile.TemporaryDirectory``
  so there is zero shared state between cases;
* open an :class:`mcp.ClientSession` over the stdio pipes, ``initialize``
  the protocol, and invoke ``fsm.abort_run`` / ``fsm.resume_run`` exactly
  the way Claude Code would;
* re-open the SQLite DB after the client session exits to inspect what
  landed on disk through the public :class:`Project` facade — the same
  contract every other lifecycle test uses.

Test cost
---------

Each test spawns a Python subprocess (``uv run ctxr-fsm mcp``) and runs
the alembic migration on its way up. Expect 5-15 seconds per test on a warm
machine; the suite is therefore parked under ``tests/integration/mcp``
and is excluded from the fast inner-loop ``pytest -x tests/unit`` run by
file-tree convention.

MCP SDK API used
----------------

* ``mcp.ClientSession`` — the standard client-side façade.
* ``mcp.client.stdio.stdio_client`` — async context manager that yields
  ``(read_stream, write_stream)`` plumbed to the child's stdio.
* ``mcp.client.stdio.StdioServerParameters`` — the subprocess descriptor.
* ``session.call_tool(name, arguments={"input": {...}})`` — FastMCP wraps
  the tool's Pydantic input model under a single ``input`` key in the
  generated tool schema; we mirror that here.

Return shape: ``CallToolResult.structuredContent`` is a JSON object of
the form ``{"result": <pydantic-model-dump>}`` for tools that return a
single Pydantic model. We assert against that nested ``result`` blob
because it is the stable, self-describing contract.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ctxr.fsm.core.models import EventKind, FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Spec + DB helpers
# ---------------------------------------------------------------------------


def _make_spec(spec_id: str = "abort_resume_demo") -> FsmSpec:
    """Build the minimal valid two-state FSM used by every test in this file.

    Shape: ``a`` (entry) → ``b`` (terminal). None of the tests actually
    drive a transition — they only need a run row to exist so the
    abort / resume tools have something to act on — but a registered spec
    still has to pass the cross-cutting structural validator, which is why
    we provide a single ``always`` edge instead of leaving state ``a``
    isolated.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry state",
                transitions=[Transition(to="b", when="always")],
            ),
            State(
                id="b",
                purpose="terminal state",
                transitions=[],
            ),
        ],
    )


def _seed_run(db: Path) -> str:
    """Bootstrap the project DB at ``db`` and return a fresh run id.

    Opens the project in-process (the same way the CLI does), registers
    the demo spec, and calls :meth:`Project.start_run` so the on-disk DB
    already carries a run row by the time the MCP subprocess boots
    against the same file. Returning just the id keeps the helper neutral
    about which other rows the caller may want to seed alongside it
    (journal txns, for instance).
    """
    with Project.open(db, migrate=True, echo=False) as project:
        registered = project.register_spec(_make_spec())
        run = project.start_run(registered.spec.id, args={})
        return run.id


def _seed_run_with_pending_journal(db: Path) -> tuple[str, str]:
    """Seed a run with a ``pending`` journal txn open against it.

    Returns ``(run_id, journal_txn_id)``. The ``--journal discard`` test
    needs both: the run id to address the tool call, and the txn id so it
    can assert the pre-state actually had a row before invoking the tool.
    """
    with Project.open(db, migrate=True, echo=False) as project:
        registered = project.register_spec(_make_spec())
        run = project.start_run(registered.spec.id, args={})
        with project.session_factory() as session, session.begin():
            txn = project.journal.open(session, run_id=run.id)
            txn_id = txn.id
        return run.id, txn_id


def _read_run_status(db: Path, run_id: str) -> tuple[str, str | None]:
    """Return ``(status, ended_at)`` for ``run_id`` from the on-disk DB.

    Re-opens the project read-only-ish (no transaction begun) so the
    assertion observes exactly whatever the MCP server committed.
    """
    with Project.open(db, migrate=False, echo=False) as project:
        run = project.get_run(run_id)
        assert run is not None, f"run {run_id!r} vanished between writes"
        return run.status, run.ended_at


def _read_event_kinds(db: Path, run_id: str) -> list[str]:
    """Return every event kind recorded against ``run_id``, in seq order."""
    with (
        Project.open(db, migrate=False, echo=False) as project,
        project.session_factory() as session,
    ):
        events = list(project.runs.events(session, run_id))
    return [event.kind for event in events]


def _read_last_event_payload(db: Path, run_id: str) -> dict[str, Any]:
    """Return the payload of the most-recent event recorded on ``run_id``."""
    with (
        Project.open(db, migrate=False, echo=False) as project,
        project.session_factory() as session,
    ):
        events = list(project.runs.events(session, run_id))
    assert events, f"no events recorded on run {run_id!r}"
    return dict(events[-1].payload)


def _read_journal_txn(db: Path, run_id: str) -> Any:
    """Return ``JournalRepo.inspect(run_id)`` (or ``None``) for assertions.

    ``inspect`` returns the newest unfinalised txn for the run, so this
    is the correct lens for "did the pending row survive the call?".
    """
    with (
        Project.open(db, migrate=False, echo=False) as project,
        project.session_factory() as session,
    ):
        return project.journal.inspect(session, run_id=run_id)


# ---------------------------------------------------------------------------
# MCP client helpers
# ---------------------------------------------------------------------------


def _server_params(db: Path) -> StdioServerParameters:
    """Build the subprocess descriptor for ``uv run ctxr-fsm mcp --db <db>``.

    ``uv run`` is the project's canonical launcher — it both selects the
    correct Python interpreter and ensures the venv is in sync without
    forcing the test process to know where ``.venv/bin/python`` lives.
    We do **not** pass ``--transport stdio`` explicitly because stdio is
    the default; the explicit argument would couple the test to the CLI
    surface unnecessarily.
    """
    return StdioServerParameters(
        command="uv",
        args=["run", "ctxr-fsm", "mcp", "--db", str(db)],
    )


async def _with_session(
    db: Path,
    body: Callable[[ClientSession], Awaitable[Any]],
) -> Any:
    """Spawn the MCP server, hand ``body`` an initialised client session.

    Single-call wrapper so test bodies stay flat: they get a connected,
    handshaked :class:`ClientSession` and only have to author the
    ``call_tool`` invocations. The async context managers handle process
    lifecycle (terminate on exit) and transport teardown (flush + close
    stdio pipes).
    """
    params = _server_params(db)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        return await body(session)


def _structured_result(result: Any) -> dict[str, Any]:
    """Unwrap the canonical ``{"result": ...}`` envelope from a tool call.

    FastMCP serialises a tool that returns a single Pydantic model into
    ``CallToolResult.structuredContent == {"result": <model.model_dump()>}``.
    Tests assert against the inner dict, so this helper makes that
    explicit and gives a clear failure when the shape changes.
    """
    assert result.isError is False, (
        f"tool call returned an error: {result.content!r}"
    )
    sc = result.structuredContent
    assert isinstance(sc, dict), f"missing structuredContent on {result!r}"
    assert "result" in sc, f"structuredContent missing 'result' key: {sc!r}"
    inner = sc["result"]
    assert isinstance(inner, dict), f"unexpected result shape: {inner!r}"
    return inner


# ---------------------------------------------------------------------------
# fsm.abort_run
# ---------------------------------------------------------------------------


def test_mcp_abort_run_flips_status_and_emits_event() -> None:
    """``fsm.abort_run`` over MCP flips status → aborted and emits the event.

    Mirrors the CLI's ``run abort`` happy-path test but exercises the
    full stdio transport: we open a client session against a fresh
    subprocess, invoke the tool with ``run_id`` + ``reason``, and then
    re-open the DB to confirm both the manifest flip and the
    ``run_aborted`` event landed exactly once.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fsm.db"
        run_id = _seed_run(db)

        # Sanity check: the run starts in_progress with no ended_at.
        pre_status, pre_ended_at = _read_run_status(db, run_id)
        assert pre_status == "in_progress"
        assert pre_ended_at is None

        async def body(session: ClientSession) -> dict[str, Any]:
            result = await session.call_tool(
                "fsm.abort_run",
                arguments={
                    "input": {"run_id": run_id, "reason": "user cancelled"}
                },
            )
            return _structured_result(result)

        payload = asyncio.run(_with_session(db, body))

        # ── Tool payload ────────────────────────────────────────────────
        assert payload["run_id"] == run_id
        assert payload["previous_status"] == "in_progress"
        assert payload["new_status"] == "aborted"
        assert payload["reason"] == "user cancelled"
        assert payload["ended_at"]  # ISO-8601 string, non-empty

        # ── Manifest reflects the abort ─────────────────────────────────
        post_status, post_ended_at = _read_run_status(db, run_id)
        assert post_status == "aborted"
        assert post_ended_at is not None and post_ended_at != ""
        # The payload's ended_at must match what the substrate persisted.
        assert payload["ended_at"] == post_ended_at

        # ── Event log carries exactly one run_aborted ───────────────────
        kinds = _read_event_kinds(db, run_id)
        assert kinds.count(EventKind.run_aborted.value) == 1
        # And it is the last event on the timeline.
        assert kinds[-1] == EventKind.run_aborted.value
        # The very first event is still the run_started recorded by
        # Project.start_run during seeding.
        assert kinds[0] == EventKind.run_started.value

        # The event payload mirrors the tool result.
        event_payload = _read_last_event_payload(db, run_id)
        assert event_payload["run_id"] == run_id
        assert event_payload["reason"] == "user cancelled"
        assert event_payload["previous_status"] == "in_progress"
        assert event_payload["ended_at"] == post_ended_at


# ---------------------------------------------------------------------------
# fsm.resume_run — --from-state X
# ---------------------------------------------------------------------------


def test_mcp_resume_run_from_state_returns_w12_deferral() -> None:
    """``fsm.resume_run`` with ``from_state`` surfaces the W12 stub message.

    W4 ships only the bookkeeping for engine-driven resume; the actual
    replay-into-engine path lands in W12. The tool's contract is that
    every response carries an ``engine_resume`` field whose value
    explicitly names the deferral so scripts and humans both see that
    the engine has not picked the run back up. We assert that the
    deferral string mentions ``W12`` (the stable marker every other
    surface — CLI, docs, source — uses for the same deferral).
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fsm.db"
        run_id = _seed_run(db)

        async def body(session: ClientSession) -> dict[str, Any]:
            result = await session.call_tool(
                "fsm.resume_run",
                arguments={
                    "input": {"run_id": run_id, "from_state": "a"}
                },
            )
            return _structured_result(result)

        payload = asyncio.run(_with_session(db, body))

        assert payload["run_id"] == run_id
        assert payload["from_state"] == "a"
        # No --journal flag was passed and there's no pre-existing
        # journal row, so journal_action stays None.
        assert payload["journal_action"] is None
        # Load-bearing assertion: the deferral message must call out W12.
        assert "W12" in payload["engine_resume"], (
            f"expected W12 deferral in engine_resume, got: "
            f"{payload['engine_resume']!r}"
        )

        # A run_resumed event must land regardless of the engine-resume
        # deferral, so subscribers see the operator's intent.
        kinds = _read_event_kinds(db, run_id)
        assert EventKind.run_resumed.value in kinds


# ---------------------------------------------------------------------------
# fsm.resume_run — --journal discard
# ---------------------------------------------------------------------------


def test_mcp_resume_run_journal_discard_removes_pending_row() -> None:
    """``fsm.resume_run`` with ``journal='discard'`` removes the pending row.

    Pre-condition: the run has a ``pending`` journal txn open against it.
    Action: invoke the resume tool with ``journal='discard'``.
    Post-condition: :meth:`JournalRepo.inspect` returns ``None`` and the
    tool payload reports ``journal_action='discarded'`` plus the id of
    the txn that was removed (so a caller can correlate the action with
    the row it touched).
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fsm.db"
        run_id, txn_id = _seed_run_with_pending_journal(db)

        # Pre-state sanity check: a pending journal row really does
        # exist with the expected status.
        pre = _read_journal_txn(db, run_id)
        assert pre is not None
        assert pre.id == txn_id
        assert pre.status == "pending"

        async def body(session: ClientSession) -> dict[str, Any]:
            result = await session.call_tool(
                "fsm.resume_run",
                arguments={
                    "input": {"run_id": run_id, "journal": "discard"}
                },
            )
            return _structured_result(result)

        payload = asyncio.run(_with_session(db, body))

        assert payload["run_id"] == run_id
        assert payload["journal_action"] == "discarded"
        assert payload["journal_txn_id"] == txn_id
        # The deferral marker is still present — the resume tool always
        # surfaces it regardless of which sub-action fired.
        assert "W12" in payload["engine_resume"]

        # ── The pending row must be gone after discard ──────────────────
        post = _read_journal_txn(db, run_id)
        assert post is None, (
            f"journal row {txn_id!r} was not discarded; still present as "
            f"{post!r}"
        )

        # A run_resumed event must still be on the timeline so the
        # audit log captures the operator's action.
        kinds = _read_event_kinds(db, run_id)
        assert EventKind.run_resumed.value in kinds
