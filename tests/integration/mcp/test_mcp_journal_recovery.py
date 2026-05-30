"""Integration tests for ``fsm.inspect_journal`` and ``fsm.recover_journal``.

These tests spawn a real ``ctxr-fsm mcp`` subprocess via the official MCP
Python SDK's stdio client transport, then drive the journal-recovery
tools end-to-end. The fixtures pre-populate the SQLite database with a
registered spec, an in-flight run, and a journal-txn row in a known
status BEFORE the server boots, so the tools see realistic state on
first contact.

Coverage matrix (matches the W4 brief):

* ``fsm.inspect_journal`` returns the newest unfinalised txn for a run
  when one exists. We seed a ``pending`` txn and confirm the wire
  payload echoes its id / status / staged_writes.
* ``fsm.recover_journal`` with ``action="discard"`` deletes the staged
  ledger row (effective rollback). After the call we re-poll
  ``inspect_journal`` and confirm the txn is gone.
* ``fsm.recover_journal`` with ``action="replay"`` finalises a
  ``ready_to_finalise`` txn (effective roll-forward — W4 only flips
  the status, the engine on next boot re-materialises the writes).
  After the call we re-poll ``inspect_journal`` and confirm the txn no
  longer surfaces (``inspect`` only returns ``pending`` /
  ``ready_to_finalise`` rows).

MCP client API used
-------------------
* ``mcp.ClientSession`` — async high-level client surface.
* ``mcp.client.stdio.stdio_client`` + ``StdioServerParameters`` —
  spawn the server as a subprocess and yield the
  (read_stream, write_stream) pair the ``ClientSession`` consumes.
* ``ClientSession.initialize()`` — performs the MCP initialize
  handshake before any tool call.
* ``ClientSession.call_tool(name, arguments)`` — single tool round-trip;
  returns a ``CallToolResult`` whose ``structuredContent`` field carries
  the typed Pydantic payload (FastMCP serialises ``BaseModel`` returns
  to ``structuredContent`` automatically).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ctxr.fsm.core.models import FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


# Generous deadline for a single MCP tool round-trip in this test module.
# The wall-clock cost is dominated by subprocess spawn + the MCP
# initialize handshake, not the tool body, so 30s is conservative even
# on a slow CI runner.
_TOOL_TIMEOUT_SECONDS: float = 30.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db_path() -> Iterator[Path]:
    """Yield a per-test SQLite path under a fresh ``TemporaryDirectory``.

    Each test gets its own directory so the journal-row contents one
    test seeds never bleed into another. The path is returned (not the
    directory) because the MCP server's ``--db`` option wants a file
    path; the parent directory is the context-managed handle.
    """
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp) / "fsm.sqlite3"


@pytest.fixture
def linear_two_state_spec() -> FsmSpec:
    """A minimal two-state FSM used to mint runs whose journal we exercise.

    We do not actually drive the FSM through ``commit_outputs`` in these
    tests — we just need a registered spec so :meth:`Project.start_run`
    can mint a run row whose ``run_id`` the journal-txn rows hang off.
    """
    return FsmSpec(
        id="journal_recovery_demo",
        version=1,
        entry="state_a",
        states=[
            State(
                id="state_a",
                purpose="entry state",
                transitions=[Transition(to="state_b", when="always")],
            ),
            State(
                id="state_b",
                purpose="terminal state",
                transitions=[],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# DB-priming helpers
# ---------------------------------------------------------------------------


def _seed_run(db_path: Path, spec: FsmSpec) -> str:
    """Register ``spec`` and start a run on a fresh project at ``db_path``.

    Returns the ``run_id`` of the newly-minted run. Opens and closes the
    Project inline so the SQLite file lock is released before the MCP
    server subprocess tries to open the same file.
    """
    with Project.open(db_path) as project:
        registered = project.register_spec(spec)
        run = project.start_run(spec_id=registered.spec.id, args={})
        return run.id


def _seed_pending_txn(db_path: Path, run_id: str) -> str:
    """Insert a ``pending`` journal txn for ``run_id``; return the txn id.

    Re-opens the project so we get a fresh session-factory; the row is
    committed via the project's atomic begin() block before the project
    closes.
    """
    with (
        Project.open(db_path) as project,
        project.session_factory() as session,
        session.begin(),
    ):
        txn = project.journal.open(session, run_id=run_id)
        return txn.id


def _seed_ready_txn(db_path: Path, run_id: str) -> str:
    """Insert a ``ready_to_finalise`` journal txn for ``run_id``.

    Walks the lifecycle ``open() → mark_ready()`` so the row is in the
    state that ``recover_journal action=replay`` will accept.
    """
    with (
        Project.open(db_path) as project,
        project.session_factory() as session,
        session.begin(),
    ):
        txn = project.journal.open(session, run_id=run_id)
        project.journal.mark_ready(
            session,
            txn_id=txn.id,
            staged_writes=[{"kind": "demo", "payload": {"ok": True}}],
        )
        return txn.id


def _read_journal_status(db_path: Path, run_id: str) -> str | None:
    """Return the current status of the newest unfinalised txn for ``run_id``.

    Returns ``None`` when no unfinalised txn exists — that is the
    post-discard / post-replay shape we assert against. We re-open the
    project after the MCP subprocess has shut down so the SQLite file
    is fully released.
    """
    with Project.open(db_path) as project, project.session_factory() as session:
        txn = project.journal.inspect(session, run_id=run_id)
    return txn.status if txn is not None else None


# ---------------------------------------------------------------------------
# MCP client helpers
# ---------------------------------------------------------------------------


def _server_params(db_path: Path) -> StdioServerParameters:
    """Build the stdio-spawn parameters for the ``ctxr-fsm mcp`` subprocess.

    We invoke the server via ``uv run ctxr-fsm mcp --db <path>`` so the
    project's virtualenv + entry-point script wiring is exercised
    end-to-end. The CWD is left at the project root (uv resolves the
    venv from there).
    """
    return StdioServerParameters(
        command="uv",
        args=["run", "ctxr-fsm", "mcp", "--db", str(db_path)],
    )


async def _call_tool(
    db_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Spawn the MCP server, call one tool, return its ``structuredContent``.

    The whole lifecycle (spawn → initialize → call_tool → shutdown) is
    bracketed inside one coroutine so the subprocess is fully torn down
    before the function returns; the SQLite file lock is released by
    the time the caller inspects the DB directly via
    :func:`_read_journal_status`.

    FastMCP serialises a ``BaseModel`` return value into the
    ``structuredContent`` field of the ``CallToolResult``. The SDK
    wraps non-dict-root return types (and *every* return type when the
    tool's declared return is a union — e.g. ``JournalState |
    McpToolError``) inside a ``{"result": ...}`` envelope so the
    ``structuredContent`` field always carries a JSON object. We unwrap
    that single-key envelope here so per-test assertions read the
    Pydantic payload directly without each test re-implementing the
    unwrap.
    """
    async with (
        stdio_client(_server_params(db_path)) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.call_tool(tool_name, arguments)
        # ``isError`` is the SDK's transport-level error flag — a
        # tool that returned our McpToolError envelope is NOT
        # transport-error (it still returns a 200-equivalent), so
        # we only assert here against actual JSON-RPC errors.
        assert not result.isError, (
            f"tool {tool_name!r} returned transport error: {result}"
        )
        assert result.structuredContent is not None, (
            f"tool {tool_name!r} returned no structuredContent: {result}"
        )
        structured = dict(result.structuredContent)
        # FastMCP wraps Union-typed returns under a single ``result``
        # key — peel it off so callers see the raw Pydantic dict.
        # On the rare tool that returns a bare dict (no wrapper) we
        # leave it alone.
        if (
            list(structured.keys()) == ["result"]
            and isinstance(structured["result"], dict)
        ):
            return dict(structured["result"])
        return structured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_inspect_journal_returns_pending_txn(
    tmp_db_path: Path, linear_two_state_spec: FsmSpec
) -> None:
    """``fsm.inspect_journal`` surfaces a pre-seeded ``pending`` txn row.

    Flow:
    1. Pre-populate: register spec, start run, open a journal txn (which
       is ``pending`` because we never call ``mark_ready``).
    2. Spawn the MCP server, call ``fsm.inspect_journal`` with the
       seeded run_id.
    3. Assert the wire payload echoes the txn id, status, run_id, and
       carries the empty ``staged_writes`` list.
    """
    run_id = _seed_run(tmp_db_path, linear_two_state_spec)
    txn_id = _seed_pending_txn(tmp_db_path, run_id)

    payload = asyncio.run(
        asyncio.wait_for(
            _call_tool(tmp_db_path, "fsm.inspect_journal", {"run_id": run_id}),
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
    )

    # JournalState wire shape: {run_id, txn: {...}}
    assert payload["run_id"] == run_id, (
        f"inspect_journal returned wrong run_id: {payload}"
    )
    txn = payload["txn"]
    assert txn is not None, (
        f"inspect_journal returned txn=None despite seeded pending row: {payload}"
    )
    assert txn["id"] == txn_id, (
        f"inspect_journal returned wrong txn_id: {txn}"
    )
    assert txn["run_id"] == run_id
    assert txn["status"] == "pending"
    # Freshly-opened pending txns carry an empty staged_writes list
    # (canonical "[]") — assert that to lock the shape in.
    assert txn["staged_writes"] == []


def test_recover_journal_discard_removes_pending_txn(
    tmp_db_path: Path, linear_two_state_spec: FsmSpec
) -> None:
    """``fsm.recover_journal action=discard`` deletes a ``pending`` row.

    Flow:
    1. Pre-populate: seed a ``pending`` txn against a fresh run.
    2. Call ``fsm.recover_journal`` with ``action="discard"``.
    3. Assert the wire payload reports ``action=discard``, echoes the
       txn id, and notes the previous status was ``pending``.
    4. Re-open the SQLite DB out-of-band and confirm the row is gone
       (``journal.inspect`` returns ``None`` for this run).
    """
    run_id = _seed_run(tmp_db_path, linear_two_state_spec)
    txn_id = _seed_pending_txn(tmp_db_path, run_id)

    payload = asyncio.run(
        asyncio.wait_for(
            _call_tool(
                tmp_db_path,
                "fsm.recover_journal",
                {"run_id": run_id, "action": "discard"},
            ),
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
    )

    assert payload["run_id"] == run_id
    assert payload["action"] == "discard"
    assert payload["txn_id"] == txn_id
    assert payload["previous_status"] == "pending"

    # Out-of-band verification: the row is gone, so inspect() returns
    # None on a fresh project handle.
    assert _read_journal_status(tmp_db_path, run_id) is None


def test_recover_journal_replay_finalises_ready_txn(
    tmp_db_path: Path, linear_two_state_spec: FsmSpec
) -> None:
    """``fsm.recover_journal action=replay`` finalises a ``ready_to_finalise`` row.

    Flow:
    1. Pre-populate: open a journal txn AND ``mark_ready`` it so the
       row carries staged_writes and status ``ready_to_finalise``.
    2. Call ``fsm.recover_journal`` with ``action="replay"``.
    3. Assert the wire payload reports the replay action, echoes the
       txn id, and notes the previous status was ``ready_to_finalise``.
    4. Re-open the SQLite DB out-of-band and confirm ``inspect`` no
       longer surfaces the row (it's now ``finalised``, which is
       outside ``inspect``'s ``(pending, ready_to_finalise)`` filter).
    """
    run_id = _seed_run(tmp_db_path, linear_two_state_spec)
    txn_id = _seed_ready_txn(tmp_db_path, run_id)

    payload = asyncio.run(
        asyncio.wait_for(
            _call_tool(
                tmp_db_path,
                "fsm.recover_journal",
                {"run_id": run_id, "action": "replay"},
            ),
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
    )

    assert payload["run_id"] == run_id
    assert payload["action"] == "replay"
    assert payload["txn_id"] == txn_id
    assert payload["previous_status"] == "ready_to_finalise"

    # The row is now ``finalised``, which falls outside the
    # ``inspect`` filter, so a re-poll returns None.
    assert _read_journal_status(tmp_db_path, run_id) is None
