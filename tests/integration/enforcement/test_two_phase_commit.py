"""Integration tests for the W12 two-phase commit primitive.

The W12 brief replaced the single-call ``commit_outputs`` of W4 with a
two-phase handshake:

* :func:`fsm_commit_outputs` no longer advances the run. Instead it
  validates the inputs (engine + verifier + cosignature), stages the
  deferred state / transition / manifest writes in a ``journal_txn``
  row marked ``ready_to_finalise``, mints a single-use commit token,
  and returns ``{token, expected_next_state}`` so the worker has
  something to hand back. The run's ``current_state`` does NOT move.

* :func:`fsm_confirm_commit` is the second leg: the worker (or its
  scheduler) replays the staged writes by presenting the token. Only
  then does the run actually advance, the ``commit_token_consumed``
  event land, and the next brief / manifest become visible.

This module's tests pin down the five contract-level behaviours of
that primitive — independent of the (separately-tested) verifier panel
and cosignature gates:

1. ``commit_outputs`` returns ``{token, expected_next_state}`` and the
   run's ``current_state`` is unchanged.
2. ``confirm_commit(token, expected_next_state)`` advances the run and
   emits a ``commit_token_consumed`` event.
3. ``confirm_commit(token, WRONG_state)`` refuses with a state-mismatch
   rejection.
4. Reusing a consumed token refuses (replay attack defence).
5. Expired token (TTL elapsed) refuses with a ``commit_token_expired``
   event on the bus.

The spec used here is intentionally minimal — two states ``a → b`` with
no verifier and no ``allowed_tools`` — so the cosignature gate is OFF
and every test exercises ONLY the two-phase commit path. The richer
verifier / cosignature interactions live in
``test_two_phase_commit_verifier.py``.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

# Side-effect import: binds ``FsmSpec.hash()`` / ``FsmSpec.validate()``
# onto the core model so ``project.register_spec`` can hash the spec.
from ctxr.fsm.core import spec as _spec_module  # noqa: F401
from ctxr.fsm.core.models import (
    EventKind,
    FsmSpec,
    ResponseSchema,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.mcp import _state as _mcp_state
from ctxr.fsm.mcp.tools_runs import (
    CommitOutputsInput,
    ConfirmCommitInput,
    StartRunInput,
    fsm_commit_outputs,
    fsm_confirm_commit,
    fsm_start_run,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_enforcement import CommitTokenTable
from ctxr.fsm.sqlite.models_events import EventTable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_factory():
    """Open a fresh :class:`Project` per test on a temp SQLite database.

    The MCP module-global project handle is reset between every test so
    the in-process tool calls always see the latest binding. Each test
    opens its own project (via the factory) and is responsible for
    closing it in a ``try/finally``.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        def _open() -> Project:
            project = Project.open(db_path, migrate=True)
            _mcp_state.set_project(project)
            return project

        yield _open

        _mcp_state.reset_project()


# A worker response schema that accepts the single field the entry-state
# worker emits. Kept permissive (``additionalProperties: True``) so a
# test that wants to attach extra debug fields can do so without
# re-declaring the whole spec.
_OK_SCHEMA = ResponseSchema(
    schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ok"]},
        },
        "required": ["verdict"],
        "additionalProperties": True,
    }
)


def _two_state_spec(spec_id: str = "w12_two_phase_commit_demo") -> FsmSpec:
    """A two-state FSM (``a → b``) with no verifier and no allowed_tools.

    Cosignature is NOT required for this state (neither
    ``allowed_tools`` nor ``verifier`` is set, and the test process
    does not flip ``CTXR_FSM_REQUIRE_COSIGNATURE``), so the tests can
    drive ``commit_outputs`` without computing a signature. That
    isolates the two-phase commit primitive from the cosignature /
    verifier gates.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry — emit ok, expect to transition to b",
                worker=Worker(
                    role="emitter",
                    prompt_template="emit {verdict}",
                    response_schema=_OK_SCHEMA,
                ),
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def _start_run(project: Project, spec: FsmSpec):
    """Register ``spec`` and start a run via the MCP ``fsm.start_run`` tool.

    Driving through the MCP body (rather than ``project.start_run``)
    ensures the entry state-entry row is persisted — the commit and
    confirm pipelines both expect that row to exist when looking up
    ``current_state_pk``.
    """
    registered = project.register_spec(spec)
    started = fsm_start_run(
        StartRunInput(spec_id=uuid.UUID(registered.spec.id), args={})
    )
    assert not hasattr(started, "error"), f"unexpected error: {started!r}"
    return registered, started


def _commit(run_id: uuid.UUID, outputs: dict):
    """Drive ``fsm.commit_outputs`` without a signature.

    The fixture spec has no verifier / allowed_tools / env-var trigger,
    so the W12 layer-5 cosignature is not required and we can call the
    tool with ``signature=None`` to focus on the token mechanics.
    """
    return fsm_commit_outputs(
        CommitOutputsInput(
            run_id=run_id,
            outputs=outputs,
        )
    )


# ---------------------------------------------------------------------------
# 1. commit_outputs returns {token, expected_next_state}; state not advanced
# ---------------------------------------------------------------------------


def test_commit_outputs_returns_token_and_does_not_advance_state(
    project_factory,
) -> None:
    """``commit_outputs`` mints a token + records expected_next_state.

    Asserts the full success-shape contract:

    * The result is ``kind="advanced"`` with the expected
      ``next_state`` / ``expected_next_state`` ("b" for this spec).
    * A :class:`CommitToken` is populated on the result and stored in
      the ``commit_tokens`` table, unconsumed.
    * The run's ``current_state`` is STILL ``"a"`` — the journal txn
      is staged but no replay has run yet, so the manifest reflects
      the pre-commit state.
    """
    project = project_factory()
    try:
        _, started = _start_run(project, _two_state_spec())

        result = _commit(started.run_id, {"verdict": "ok"})

        assert not hasattr(result, "error"), (
            f"expected success, got error envelope {result!r}"
        )
        assert result.kind == "advanced"
        assert result.next_state == "b"
        assert result.expected_next_state == "b"
        assert result.token is not None
        assert result.token.expected_next_state == "b"
        assert result.token.run_id == started.run_id
        assert result.token.state_id == "a"

        # The commit_token row exists, points at the same run, and has
        # NOT been consumed (consumed_at is still NULL).
        with project.session_factory() as session:
            tok_rows = (
                session.execute(
                    select(CommitTokenTable).where(
                        CommitTokenTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(tok_rows) == 1
        assert tok_rows[0].consumed_at is None
        assert tok_rows[0].expected_next_state == "b"
        assert tok_rows[0].token == str(result.token.token)

        # The run is still on the entry state — confirm_commit has not
        # been called, so the staged advance has not been applied.
        run_now = project.get_run(str(started.run_id))
        assert run_now is not None
        assert run_now.current_state == "a"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 2. confirm_commit advances the run and emits commit_token_consumed
# ---------------------------------------------------------------------------


def test_confirm_commit_advances_run_and_emits_consumed_event(
    project_factory,
) -> None:
    """Valid token + matching ``expected_next_state`` ⇒ run advances.

    Drives the full pass-path: commit (token issued) → confirm (token
    consumed, journal replayed, state advances to ``"b"``). Asserts:

    * ``confirm_commit`` returns ``confirmed=True``.
    * The run's ``current_state`` moved to ``"b"``.
    * The token row's ``consumed_at`` is no longer NULL.
    * A ``commit_token_consumed`` event landed on the bus and carries
      the token id + the expected_next_state the confirm validated
      against.
    """
    project = project_factory()
    try:
        _, started = _start_run(project, _two_state_spec())

        commit = _commit(started.run_id, {"verdict": "ok"})
        assert not hasattr(commit, "error")
        assert commit.token is not None

        confirm = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.expected_next_state or "",
            )
        )
        assert not hasattr(confirm, "error"), (
            f"expected confirm success, got error envelope {confirm!r}"
        )
        assert confirm.confirmed is True
        # next_brief is for the destination state.
        assert confirm.next_brief is not None
        assert confirm.next_brief.state == "b"
        # manifest snapshot reflects the post-replay state.
        assert confirm.manifest is not None
        assert confirm.manifest["current_state"] == "b"

        # The token row was atomically marked consumed during confirm.
        with project.session_factory() as session:
            tok_rows = (
                session.execute(
                    select(CommitTokenTable).where(
                        CommitTokenTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(tok_rows) == 1
        assert tok_rows[0].consumed_at is not None

        # The ``commit_token_consumed`` event must be on the bus, with
        # a payload that includes the token id and expected_next_state.
        with project.session_factory() as session:
            rows = (
                session.execute(
                    select(EventTable)
                    .where(EventTable.run_id == str(started.run_id))
                    .where(
                        EventTable.kind == EventKind.commit_token_consumed.value
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, (
            f"expected one commit_token_consumed event, got {rows!r}"
        )
        payload = json.loads(rows[0].payload_json)
        assert payload["token"] == str(commit.token.token)
        assert payload["expected_next_state"] == "b"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 3. confirm_commit refuses when expected_next_state does not match
# ---------------------------------------------------------------------------


def test_confirm_commit_refuses_state_mismatch(project_factory) -> None:
    """Wrong ``expected_next_state`` ⇒ rejection, run remains on the source state.

    The commit_tokens repo distinguishes four rejection reasons
    (``not_found`` / ``already_consumed`` / ``expired`` / ``state_mismatch``);
    the MCP layer surfaces ``state_mismatch`` as the discriminator slug
    inside a ``commit_token_invalid`` error envelope. The test pins
    down both the error shape AND the side effect: a mismatched
    confirm does NOT consume the token or discard the staged journal,
    so the operator can retry with the right value.
    """
    project = project_factory()
    try:
        _, started = _start_run(project, _two_state_spec())

        commit = _commit(started.run_id, {"verdict": "ok"})
        assert not hasattr(commit, "error")
        assert commit.token is not None

        bogus = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state="not_a_real_state",
            )
        )
        assert hasattr(bogus, "error"), (
            f"expected rejection envelope, got success {bogus!r}"
        )
        # The MCP layer wraps the repo's rejection reason in a
        # ``commit_token_invalid`` envelope; the slug is in ``payload.reason``.
        assert bogus.error == "commit_token_invalid"
        assert bogus.payload is not None
        assert bogus.payload["reason"] == "state_mismatch"

        # The run remains on the source state — a failed confirm
        # neither replays the journal nor moves the manifest pointer.
        run_now = project.get_run(str(started.run_id))
        assert run_now is not None
        assert run_now.current_state == "a"

        # The token row is NOT marked consumed on a state_mismatch
        # rejection — the operator can retry with the correct value.
        with project.session_factory() as session:
            tok_rows = (
                session.execute(
                    select(CommitTokenTable).where(
                        CommitTokenTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(tok_rows) == 1
        assert tok_rows[0].consumed_at is None
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 4. Reusing a consumed token refuses
# ---------------------------------------------------------------------------


def test_confirm_commit_refuses_reused_token(project_factory) -> None:
    """Calling ``confirm_commit`` twice with the same token rejects the second.

    The first confirm consumes the token and advances the run. The
    second call MUST be rejected with ``reason="already_consumed"`` —
    this is the replay-attack defence: even if a malicious caller
    captures a valid token, it cannot re-use it after the legitimate
    worker has confirmed.
    """
    project = project_factory()
    try:
        _, started = _start_run(project, _two_state_spec())

        commit = _commit(started.run_id, {"verdict": "ok"})
        assert commit.token is not None
        # First confirm wins.
        ok = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.expected_next_state or "",
            )
        )
        assert not hasattr(ok, "error"), f"first confirm should succeed, got {ok!r}"
        assert ok.confirmed is True

        # Second confirm against the same token is rejected with
        # ``already_consumed`` — the row's consumed_at is set so the
        # repo's consume() short-circuits on the very first check.
        again = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.expected_next_state or "",
            )
        )
        assert hasattr(again, "error"), (
            f"expected rejection envelope on replay, got {again!r}"
        )
        assert again.error == "commit_token_invalid"
        assert again.payload is not None
        assert again.payload["reason"] == "already_consumed"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 5. Expired token (TTL elapsed) refuses with commit_token_expired event
# ---------------------------------------------------------------------------


def test_confirm_commit_refuses_expired_token(project_factory) -> None:
    """A token whose TTL has elapsed is rejected and emits ``commit_token_expired``.

    Rather than ``sleep(TTL)`` for 60s, we age the token by writing a
    past ``expires_at`` directly onto the row — the consume path uses
    ``datetime.now(UTC) > expires_dt`` so any past timestamp triggers
    the expired branch.

    Asserts:

    * The confirm fails with ``commit_token_invalid`` + reason
      ``"expired"``.
    * A ``commit_token_expired`` event lands on the bus, carrying the
      token id and the run id so a drift aggregator can attribute the
      expiry to the correct run.
    * The token row is NOT consumed by a fresh worker — the expired
      check fires before the consume mutation.
    * The run remains on the source state.
    """
    project = project_factory()
    try:
        _, started = _start_run(project, _two_state_spec())

        commit = _commit(started.run_id, {"verdict": "ok"})
        assert commit.token is not None
        token_str = str(commit.token.token)

        # Age the token: rewrite its ``expires_at`` to a past timestamp
        # so the consume() time-check fails. Using year 2000 means the
        # ``now > expires`` branch is unambiguously triggered regardless
        # of wallclock skew on the test host.
        past_iso = "2000-01-01T00:00:00.000Z"
        with project.session_factory() as session, session.begin():
            row = session.get(CommitTokenTable, token_str)
            assert row is not None, "token row should exist after commit"
            row.expires_at = past_iso
            session.add(row)

        expired = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.expected_next_state or "",
            )
        )
        assert hasattr(expired, "error"), (
            f"expected expired rejection envelope, got {expired!r}"
        )
        assert expired.error == "commit_token_invalid"
        assert expired.payload is not None
        assert expired.payload["reason"] == "expired"

        # The ``commit_token_expired`` event must land on the bus so a
        # drift aggregator / observer can react to the expiry.
        with project.session_factory() as session:
            rows = (
                session.execute(
                    select(EventTable)
                    .where(EventTable.run_id == str(started.run_id))
                    .where(
                        EventTable.kind == EventKind.commit_token_expired.value
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, (
            f"expected one commit_token_expired event, got {rows!r}"
        )
        payload = json.loads(rows[0].payload_json)
        assert payload["token"] == token_str
        assert payload["run_id"] == str(started.run_id)

        # The run stays on its source state — an expired token does
        # not replay the journal, so the manifest pointer is unchanged.
        run_now = project.get_run(str(started.run_id))
        assert run_now is not None
        assert run_now.current_state == "a"
    finally:
        project.close()
