"""End-to-end MCP integration test: register_spec → start_run → commit_outputs → terminal.

This test spawns the real ``ctxr-fsm mcp`` binary as a subprocess and
drives it through the official ``mcp`` Python SDK's stdio client. The
goal is to confirm the JSON-RPC framing, FastMCP tool registration, and
stdio plumbing all line up end-to-end for the W4 happy path:

1. Register a small two-state FSM spec via ``fsm.register_spec``.
2. Start a run via ``fsm.start_run`` — get back ``run_id`` + the brief
   for the entry state.
3. Commit outputs for the entry state via ``fsm.commit_outputs`` —
   expect ``kind="advanced"`` with the next state's brief embedded.
4. Commit outputs for the terminal state — expect ``kind="terminal"``.
5. Read the final picture via ``fsm.get_run`` and assert the
   ``state_tree`` mirrors the linear walk (root ``state_a`` → child
   ``state_b``) and the run is ``completed``.

The whole flow drives two states to terminal so the substrate's
``advanced`` and ``terminal`` branches of the commit result discriminator
are both exercised in one run.

MCP client API used
-------------------

``mcp`` 1.27.2 — ``ClientSession`` + ``stdio_client`` +
``StdioServerParameters``. ``call_tool`` returns a ``CallToolResult``
whose ``structuredContent`` field carries the Pydantic-typed payload
returned by each tool body. FastMCP wraps non-dict Pydantic return
values under a top-level ``"result"`` key on the wire; the
:func:`_unwrap` helper below normalises both shapes so the rest of the
test reads the same way whether or not the wrapping happened.

Stdio framing means logs the server prints to stderr are invisible to
the client; the smoke check at the top of the test confirms the
``initialize`` handshake completes (i.e. stdout was clean JSON-RPC).
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

# ---------------------------------------------------------------------------
# Fixture spec
# ---------------------------------------------------------------------------


# A deliberately tiny two-state linear FSM:
#
#   state_a (entry, ``always`` → state_b)
#     → state_b (no transitions == terminal)
#
# We use ``"always"`` guards so the predicate evaluator is not in the
# picture — this test is about the MCP wire surface, not transition
# semantics. The shape is intentionally identical to what
# ``fsm.register_spec``'s ``definition_json`` argument expects after
# ``json.dumps``: the same JSON FsmSpec.model_validate_json parses
# in-process.
_FIXTURE_SPEC: dict[str, Any] = {
    "id": "mcp_demo_two_state",
    "version": 1,
    "entry": "state_a",
    "states": [
        {
            "id": "state_a",
            "purpose": "entry state for the MCP integration test",
            "transitions": [{"to": "state_b", "when": "always"}],
        },
        {
            "id": "state_b",
            "purpose": "terminal state for the MCP integration test",
            "transitions": [],
        },
    ],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unwrap(structured: dict[str, Any] | None) -> dict[str, Any]:
    """Return the tool payload, peeling FastMCP's ``{"result": ...}`` wrapper.

    FastMCP serialises a Pydantic return value by dumping it under a
    top-level ``"result"`` key when the type is a non-dict model;
    ``dict``-typed returns land flat. We accept either shape so the
    rest of the test does not have to special-case the wrapping.
    """
    assert structured is not None, "tool returned no structuredContent"
    if list(structured.keys()) == ["result"] and isinstance(structured["result"], dict):
        return structured["result"]
    return structured


@asynccontextmanager
async def _mcp_session(db_path: Path) -> AsyncIterator[ClientSession]:
    """Spawn ``ctxr-fsm mcp`` as a subprocess and yield an initialised session.

    The subprocess is launched via ``uv run`` so the test inherits the
    same dependency resolution as ``cd <repo> && uv run pytest`` — no
    activation-side dependency on the developer's current
    ``VIRTUAL_ENV``. ``cwd`` is pinned to the repo root so ``uv``
    discovers the right project regardless of where pytest is invoked
    from.

    The double async-with (``stdio_client`` then ``ClientSession``) is
    the canonical pattern from the SDK: the outer manager owns the
    transport streams, the inner manager owns the session lifecycle
    (initialize on entry, shutdown on exit).
    """
    repo_root = Path(__file__).resolve().parents[3]
    params = StdioServerParameters(
        command="uv",
        args=[
            "run",
            "--project",
            str(repo_root),
            "ctxr-fsm",
            "mcp",
            "--db",
            str(db_path),
        ],
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


async def _drive_two_state_run_to_terminal(db_path: Path) -> dict[str, Any]:
    """Run register -> start -> (commit + confirm) x2 -> get_run.

    W12 made ``fsm.commit_outputs`` two-phase: the commit stages the
    deferred writes and mints a single-use CommitToken; the client
    must call ``fsm.confirm_commit`` with the token before the run
    actually advances. We drive both halves of the flow here so the
    test ends in the same terminal manifest the original W4 walk
    produced.
    """
    async with _mcp_session(db_path) as session:
        # --- 1. Register the fixture spec ---------------------------------
        register_resp = await session.call_tool(
            "fsm.register_spec",
            {
                "definition_json": json.dumps(_FIXTURE_SPEC),
                "project_slug": "mcp_integration_demo",
            },
        )
        assert register_resp.isError is False, (
            f"fsm.register_spec returned an error: {register_resp}"
        )
        register = _unwrap(register_resp.structuredContent)

        # --- 2. Start a run -----------------------------------------------
        start_resp = await session.call_tool(
            "fsm.start_run",
            {
                "input": {
                    "spec_id": register["spec_id"],
                    "args": {"seed": "mcp-integration"},
                }
            },
        )
        assert start_resp.isError is False, (
            f"fsm.start_run returned an error: {start_resp}"
        )
        start = _unwrap(start_resp.structuredContent)

        # --- 3. Commit outputs for state_a (expect advanced + token) ------
        first_commit_resp = await session.call_tool(
            "fsm.commit_outputs",
            {
                "input": {
                    "run_id": start["run_id"],
                    "outputs": {"hello": "world"},
                }
            },
        )
        assert first_commit_resp.isError is False, (
            f"fsm.commit_outputs (first) returned an error: {first_commit_resp}"
        )
        first_commit = _unwrap(first_commit_resp.structuredContent)

        # --- 3b. Confirm the first commit to actually advance the run ----
        first_token = first_commit["token"]["token"]
        first_confirm_resp = await session.call_tool(
            "fsm.confirm_commit",
            {
                "input": {
                    "token": first_token,
                    "expected_next_state": first_commit["expected_next_state"],
                }
            },
        )
        assert first_confirm_resp.isError is False, (
            f"fsm.confirm_commit (first) returned an error: {first_confirm_resp}"
        )

        # --- 4. Commit outputs for state_b (expect terminal + token) ------
        second_commit_resp = await session.call_tool(
            "fsm.commit_outputs",
            {
                "input": {
                    "run_id": start["run_id"],
                    "outputs": {"verdict": "ok"},
                }
            },
        )
        assert second_commit_resp.isError is False, (
            f"fsm.commit_outputs (second) returned an error: {second_commit_resp}"
        )
        second_commit = _unwrap(second_commit_resp.structuredContent)

        # --- 4b. Confirm the terminal commit to flip the run to complete -
        second_token = second_commit["token"]["token"]
        second_confirm_resp = await session.call_tool(
            "fsm.confirm_commit",
            {
                "input": {
                    "token": second_token,
                    "expected_next_state": second_commit["expected_next_state"],
                }
            },
        )
        assert second_confirm_resp.isError is False, (
            f"fsm.confirm_commit (second) returned an error: {second_confirm_resp}"
        )

        # --- 5. Read the final picture for state_tree assertions ----------
        detail_resp = await session.call_tool(
            "fsm.get_run",
            {"input": {"run_id": start["run_id"]}},
        )
        assert detail_resp.isError is False, (
            f"fsm.get_run returned an error: {detail_resp}"
        )
        detail = _unwrap(detail_resp.structuredContent)

        return {
            "register": register,
            "start": start,
            "first_commit": first_commit,
            "second_commit": second_commit,
            "detail": detail,
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mcp_start_brief_commit_drives_two_states_to_terminal() -> None:
    """End-to-end MCP happy path: register, start, commit twice, inspect.

    Exercises every contract from the brief in one subprocess:

    * ``fsm.register_spec`` returns a created row with the right slug,
      version, and a stable hash.
    * ``fsm.start_run`` returns a ``run_id`` plus the brief for the
      entry state.
    * ``fsm.commit_outputs`` (first call) returns ``kind="advanced"``
      with the next brief embedded.
    * ``fsm.commit_outputs`` (second call) returns ``kind="terminal"``.
    * ``fsm.get_run``'s ``state_tree`` reflects the linear walk
      ``state_a → state_b``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        result = asyncio.run(_drive_two_state_run_to_terminal(db_path))

    register = result["register"]
    start = result["start"]
    first_commit = result["first_commit"]
    second_commit = result["second_commit"]
    detail = result["detail"]

    # --- register_spec --------------------------------------------------
    assert register["created"] is True, "first registration should mint a row"
    assert register["slug"] == _FIXTURE_SPEC["id"]
    assert register["version"] == 1
    assert register["project_slug"] == "mcp_integration_demo"
    # ``hash`` is the SHA-256 canonical hash — 64 hex chars.
    assert isinstance(register["hash"], str)
    assert len(register["hash"]) == 64
    # ``spec_id`` is a UUIDv7 string; we only check it's non-empty and
    # the right length, not the timestamp embedded in it.
    assert isinstance(register["spec_id"], str)
    assert len(register["spec_id"]) == 36

    # --- start_run ------------------------------------------------------
    assert isinstance(start["run_id"], str)
    assert len(start["run_id"]) == 36
    assert start["fsm_spec_hash"] == register["hash"]
    brief = start["brief"]
    assert brief["state"] == "state_a"
    assert brief["fsm_id"] == _FIXTURE_SPEC["id"]
    # The entry state declares no worker so the brief carries an empty
    # outputs_expected list and the ``has_worker`` flag is False.
    assert brief["has_worker"] is False
    assert brief["has_loop"] is False
    # Transitions on the brief should match the spec: a single
    # always-guard transition to state_b.
    assert len(brief["transitions"]) == 1
    assert brief["transitions"][0]["to"] == "state_b"

    # --- first commit: advanced ----------------------------------------
    assert first_commit["kind"] == "advanced", (
        f"expected advanced after committing state_a, got {first_commit!r}"
    )
    assert first_commit["next_state"] == "state_b"
    assert first_commit["brief"] is not None
    assert first_commit["brief"]["state"] == "state_b"
    # The winning transition evaluation should be the ``always`` guard
    # we declared on state_a.
    evaluations = first_commit.get("evaluations") or []
    assert any(
        ev["to"] == "state_b" and ev["result"] is True for ev in evaluations
    ), f"expected an `always→state_b` evaluation in {evaluations!r}"

    # --- second commit: terminal ---------------------------------------
    assert second_commit["kind"] == "terminal", (
        f"expected terminal after committing state_b, got {second_commit!r}"
    )
    # The ``verdict`` key was passed in the outputs of the terminal
    # commit and the engine surfaces it on the result.
    assert second_commit.get("verdict") == "ok"

    # --- get_run: manifest + state_tree --------------------------------
    manifest = detail["manifest"]
    assert manifest["status"] == "completed"
    assert manifest["current_state"] == "state_b"
    assert manifest["verdict"] == "ok"
    assert manifest["fsm_spec_id"] == register["spec_id"]

    tree = detail["state_tree"]
    assert tree is not None, "state_tree must be present once the run is committed"
    # Root of the tree is the entry state.
    assert tree["state_id"] == "state_a"
    assert tree["entry_seq"] == 1
    assert tree["status"] == "exited"
    # The entry state's outputs should reflect what we committed.
    assert tree["outputs"] == {"hello": "world"}

    # Exactly one child: state_b.
    assert len(tree["children"]) == 1, (
        f"expected a single child on state_a, got {tree['children']!r}"
    )
    child = tree["children"][0]
    assert child["state_id"] == "state_b"
    assert child["entry_seq"] == 2
    assert child["status"] == "exited"
    assert child["outputs"] == {"verdict": "ok"}
    assert child["children"] == [], (
        f"state_b is terminal — expected no children, got {child['children']!r}"
    )

    # The recent-events tail returned by fsm.get_run must include the
    # canonical lifecycle events: start, two state_entered/state_exited
    # pairs, a transition_taken, and a run_completed. We assert by kind
    # rather than exact count so the test is robust to future event
    # types being added by the substrate.
    event_kinds = [event["kind"] for event in detail["events"]]
    required = {
        "run_started",
        "state_entered",
        "state_exited",
        "transition_taken",
        "run_completed",
    }
    assert required.issubset(set(event_kinds)), (
        f"missing required event kinds: {sorted(required - set(event_kinds))!r}; "
        f"observed kinds={event_kinds!r}"
    )

    # Journal must be empty — every commit finalised its transactions.
    assert detail["journal"] is None, (
        f"journal was not cleared on a clean run: {detail['journal']!r}"
    )


# A pytest marker that surfaces clearly when someone runs ``pytest -m
# integration`` to scope to integration tests. Registering the marker
# in ``pyproject.toml`` would silence the ``unknown marker`` warning;
# until that lands we apply it here so the test still works under
# ``--strict-markers`` with an inline filter.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnknownMarkWarning")
