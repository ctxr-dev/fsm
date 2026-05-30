"""Integration tests for W12 two-phase commit + adversarial verifier.

The two W12 enforcement primitives wired in this workstream are:

* Two-phase ``commit_outputs`` / ``confirm_commit`` — a commit stages
  the deferred writes in a ``journal_txn`` (status
  ``ready_to_finalise``) and mints a single-use :class:`CommitToken`;
  ``confirm_commit`` validates the token and replays the staged writes
  so the run actually advances.
* Adversarial verifier panel — when a :class:`~ctxr.fsm.core.models.State`
  declares ``verifier``, the commit pipeline runs the panel between
  the engine's non-fault outcome and token issuance. A rejected panel
  surfaces ``verifier_rejected`` (no token); a passing panel emits
  ``verifier_passed`` and continues to token issuance.

These tests drive the MCP tool bodies in-process (no subprocess spawn)
so the runtime stays well under a second per case while still
exercising the full Project facade + W2 substrate end-to-end.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from ctxr.fsm.core import spec as _spec_module  # noqa: F401  (binds .hash/.validate)
from ctxr.fsm.core.models import (
    CommitSignature,
    EventKind,
    FsmSpec,
    ResponseSchema,
    State,
    Transition,
    VerifierSpec,
    Worker,
)
from ctxr.fsm.core.verifier import (
    VerifierVote,
    get_verifier_handler,
    set_verifier_handler,
)
from ctxr.fsm.mcp import _state as _mcp_state
from ctxr.fsm.mcp.tools_runs import (
    CommitOutputsInput,
    ConfirmCommitInput,
    GetBriefInput,
    StartRunInput,
    fsm_commit_outputs,
    fsm_confirm_commit,
    fsm_get_brief,
    fsm_start_run,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_enforcement import CommitTokenTable, JournalTxnTable
from ctxr.fsm.sqlite.models_events import EventTable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_factory():
    """Open a fresh Project per test on a temp SQLite database.

    The MCP module-global project handle is reset between every test so
    the in-process tool calls always see the latest binding. The
    verifier handler is also restored on teardown — tests that
    register an LLM-style handler must not leak it into the next case.
    """
    previous_handler = get_verifier_handler()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        def _open() -> Project:
            project = Project.open(db_path, migrate=True)
            _mcp_state.set_project(project)
            return project

        yield _open

        _mcp_state.reset_project()
        set_verifier_handler(previous_handler)


# Response schema used by the worker so ``validate_output`` and the
# built-in structural verifier both agree on what a well-formed
# payload looks like.
_OK_SCHEMA = ResponseSchema(
    schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ok", "bad"]},
        },
        "required": ["verdict"],
        "additionalProperties": True,
    }
)


# The verifier panel's own output schema. Not used by the structural
# fallback in our tests but required by the VerifierSpec contract.
_VERIFIER_PANEL_SCHEMA = ResponseSchema(
    schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["passed", "rejected"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict"],
    }
)


def _spec_with_verifier(spec_id: str = "w12_verifier_demo") -> FsmSpec:
    """A two-state FSM whose entry state declares a verifier panel.

    The entry state's worker emits ``{verdict: "ok"|"bad"}``; the
    verifier panel is the trigger for the W12 layer-3 enforcement
    path. The custom verifier handler each test registers can branch
    on ``outputs["verdict"]`` to simulate pass / reject without
    relying on schema validation (which would short-circuit upstream).
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry — verifier panel watches the outputs",
                worker=Worker(
                    role="emitter",
                    prompt_template="emit {verdict}",
                    response_schema=_OK_SCHEMA,
                ),
                verifier=VerifierSpec(
                    role="majority_judge",
                    prompt_template="judge {verdict}",
                    response_schema=_VERIFIER_PANEL_SCHEMA,
                    majority_threshold=2,
                    parallel_count=3,
                ),
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def _start_run(project: Project, spec: FsmSpec, args: dict | None = None):
    """Helper: register the spec + start a run via the MCP start tool.

    Using the MCP body (rather than ``project.start_run`` directly)
    ensures the entry state-entry row is persisted — the verifier /
    confirm pipeline both expect that row to exist when looking up
    ``current_state_pk``.
    """
    registered = project.register_spec(spec)
    started = fsm_start_run(
        StartRunInput(spec_id=uuid.UUID(registered.spec.id), args=args or {})
    )
    assert not hasattr(started, "error"), f"unexpected error: {started!r}"
    return registered, started


_TEST_SESSION_ID = "w12-verifier-test"


def _commit_with_signature(
    run_id: uuid.UUID,
    outputs: dict,
    *,
    args: dict,
) -> object:
    """Commit ``outputs`` for ``run_id`` carrying a valid cosignature.

    Any state with a verifier or allowed_tools requires a cosignature
    (W12 layer-5). The smoke test states all declare a verifier, so
    every commit needs to compute a signature against the brief_id.
    """
    brief = fsm_get_brief(GetBriefInput(run_id=run_id))
    assert not hasattr(brief, "error"), f"unexpected error: {brief!r}"
    envelope = CommitSignature.compute(
        brief_id=brief.brief_id,
        inputs=dict(args),
        outputs=dict(outputs),
        session_id=_TEST_SESSION_ID,
    )
    return fsm_commit_outputs(
        CommitOutputsInput(
            run_id=run_id,
            outputs=outputs,
            signature=envelope.signature,
            brief_id=brief.brief_id,
            session_id=_TEST_SESSION_ID,
        )
    )


# ---------------------------------------------------------------------------
# Verifier — pass path
# ---------------------------------------------------------------------------


def test_commit_passes_verifier_emits_event_and_mints_token(project_factory) -> None:
    """Valid commit + verifier-passing handler ⇒ verifier_passed + token issued.

    Register a custom handler that always votes ``passed`` so the
    panel's verdict is deterministic; assert the panel event lands on
    the bus and the CommitResult carries both a token and the
    expected next state.
    """
    set_verifier_handler(
        lambda verifier, brief, outputs: [
            VerifierVote(verdict="passed", reason="test_pass")
            for _ in range(verifier.parallel_count)
        ]
    )
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())

        result = _commit_with_signature(
            started.run_id, {"verdict": "ok"}, args={}
        )

        # Token + advanced kind + expected_next_state populated.
        assert not hasattr(result, "error"), (
            f"expected success, got error envelope {result!r}"
        )
        assert result.kind == "advanced"
        assert result.next_state == "b"
        assert result.expected_next_state == "b"
        assert result.token is not None
        assert result.token.expected_next_state == "b"

        # The verifier_passed event must be on the bus with vote details.
        with project.session_factory() as session:
            rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.verifier_passed.value
                )
            ).scalars().all()
        assert len(rows) == 1, f"expected one verifier_passed event, got {rows!r}"
        payload = json.loads(rows[0].payload_json)
        assert payload["verdict"] == "passed"
        assert payload["passed_count"] == 3
        assert payload["rejected_count"] == 0
        assert payload["majority_threshold"] == 2
        assert len(payload["votes"]) == 3

        # The journal_txn row is in ``ready_to_finalise`` state — the
        # writes have NOT been applied yet, only staged.
        with project.session_factory() as session:
            txn_rows = session.execute(
                select(JournalTxnTable).where(
                    JournalTxnTable.run_id == str(started.run_id)
                )
            ).scalars().all()
        assert len(txn_rows) == 1
        assert txn_rows[0].status == "ready_to_finalise"

        # The commit_token row is live (not yet consumed).
        with project.session_factory() as session:
            tok_rows = session.execute(
                select(CommitTokenTable).where(
                    CommitTokenTable.run_id == str(started.run_id)
                )
            ).scalars().all()
        assert len(tok_rows) == 1
        assert tok_rows[0].consumed_at is None
        assert tok_rows[0].expected_next_state == "b"

        # The run's current_state must still point at "a" — confirm
        # has not yet been called so the manifest update is still
        # staged in the journal txn.
        run_now = project.get_run(str(started.run_id))
        assert run_now is not None
        assert run_now.current_state == "a"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Two-phase commit — confirm advances the run
# ---------------------------------------------------------------------------


def test_confirm_commit_replays_journal_and_advances_state(project_factory) -> None:
    """``confirm_commit`` consumes the token, replays the txn, advances state.

    Drives the full pass-path: commit (token issued), then confirm
    (token consumed, journal finalised, state advances). Asserts both
    the runtime side-effects (events, manifest update) and the
    confirm response shape (``next_brief``, ``manifest``).
    """
    set_verifier_handler(
        lambda verifier, brief, outputs: [
            VerifierVote(verdict="passed", reason="ok")
            for _ in range(verifier.parallel_count)
        ]
    )
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())

        commit = _commit_with_signature(
            started.run_id, {"verdict": "ok"}, args={}
        )
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

        # The journal txn was finalised; the inspector (which only
        # surfaces unfinalised rows) therefore sees nothing.
        with project.session_factory() as session:
            txn = project.journal.inspect(session, run_id=str(started.run_id))
        assert txn is None

        # The token row is now marked consumed.
        with project.session_factory() as session:
            tok_rows = session.execute(
                select(CommitTokenTable).where(
                    CommitTokenTable.run_id == str(started.run_id)
                )
            ).scalars().all()
        assert len(tok_rows) == 1
        assert tok_rows[0].consumed_at is not None

        # The expected lifecycle events landed on the bus.
        with project.session_factory() as session:
            events = session.execute(
                select(EventTable).where(EventTable.run_id == str(started.run_id))
            ).scalars().all()
        kinds = [e.kind for e in events]
        assert EventKind.commit_token_issued.value in kinds
        assert EventKind.commit_token_consumed.value in kinds
        assert EventKind.journal_finalised.value in kinds
        assert EventKind.state_exited.value in kinds
        assert EventKind.transition_taken.value in kinds
        # state_entered fires twice: once on start_run (entry state),
        # once on the replay (destination of the advance).
        assert kinds.count(EventKind.state_entered.value) >= 2
    finally:
        project.close()


def test_confirm_commit_refuses_stale_token(project_factory) -> None:
    """Replaying the same token twice fails with ``already_consumed``."""
    set_verifier_handler(
        lambda verifier, brief, outputs: [
            VerifierVote(verdict="passed", reason="ok")
            for _ in range(verifier.parallel_count)
        ]
    )
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())
        commit = _commit_with_signature(
            started.run_id, {"verdict": "ok"}, args={}
        )
        assert commit.token is not None
        # First confirm wins.
        ok = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.expected_next_state or "",
            )
        )
        assert ok.confirmed is True

        # Second confirm against the same token is rejected.
        again = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.expected_next_state or "",
            )
        )
        assert hasattr(again, "error")
        assert again.error == "commit_token_invalid"
        assert again.payload is not None
        assert again.payload["reason"] == "already_consumed"
    finally:
        project.close()


def test_confirm_commit_refuses_state_mismatch(project_factory) -> None:
    """Wrong ``expected_next_state`` ⇒ ``state_mismatch`` rejection."""
    set_verifier_handler(
        lambda verifier, brief, outputs: [
            VerifierVote(verdict="passed", reason="ok")
            for _ in range(verifier.parallel_count)
        ]
    )
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())
        commit = _commit_with_signature(
            started.run_id, {"verdict": "ok"}, args={}
        )
        assert commit.token is not None

        bogus = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state="not_a_state",
            )
        )
        assert hasattr(bogus, "error")
        assert bogus.error == "commit_token_invalid"
        assert bogus.payload is not None
        assert bogus.payload["reason"] == "state_mismatch"

        # The journal txn is still ready_to_finalise — a failed confirm
        # does NOT discard the staged writes; the operator can retry.
        with project.session_factory() as session:
            txn = project.journal.inspect(session, run_id=str(started.run_id))
        assert txn is not None
        assert txn.status == "ready_to_finalise"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Verifier — reject path
# ---------------------------------------------------------------------------


def test_commit_verifier_rejection_blocks_token_and_emits_event(
    project_factory,
) -> None:
    """Verifier rejection ⇒ no token, no journal_txn, ``verifier_rejected`` event.

    Register a handler that votes ``rejected`` based on the worker's
    ``verdict`` field (a separate condition from the response_schema,
    so ``validate_output`` upstream cannot short-circuit). The
    structural fallback would also accept the schema-valid payload,
    so the custom handler is what makes this a verifier-side rejection.
    """

    def reject_when_bad(verifier, brief, outputs):
        bad = outputs.get("verdict") == "bad"
        verdict: str = "rejected" if bad else "passed"
        return [
            VerifierVote(verdict=verdict, reason=f"saw_{outputs.get('verdict')!r}")
            for _ in range(verifier.parallel_count)
        ]

    set_verifier_handler(reject_when_bad)
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())

        # The outputs are schema-valid (``"bad"`` is allowed by the
        # enum), so validate_output does NOT fault — the verifier is
        # the gate that catches the issue.
        result = _commit_with_signature(
            started.run_id, {"verdict": "bad"}, args={}
        )

        assert hasattr(result, "error"), (
            f"expected verifier_rejected envelope, got success {result!r}"
        )
        assert result.error == "verifier_rejected"
        assert result.payload is not None
        assert result.payload["state"] == "a"
        assert result.payload["rejected_count"] == 3
        assert result.payload["passed_count"] == 0
        assert len(result.payload["reasons"]) == 3

        # No journal_txn was staged (rejection happens BEFORE staging).
        with project.session_factory() as session:
            txn_rows = session.execute(
                select(JournalTxnTable).where(
                    JournalTxnTable.run_id == str(started.run_id)
                )
            ).scalars().all()
        assert txn_rows == []

        # No commit_token was minted.
        with project.session_factory() as session:
            tok_rows = session.execute(
                select(CommitTokenTable).where(
                    CommitTokenTable.run_id == str(started.run_id)
                )
            ).scalars().all()
        assert tok_rows == []

        # The verifier_rejected event landed on the bus.
        with project.session_factory() as session:
            rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.verifier_rejected.value
                )
            ).scalars().all()
        assert len(rows) == 1
        payload = json.loads(rows[0].payload_json)
        assert payload["verdict"] == "rejected"
        assert payload["rejected_count"] == 3

        # Run remained on state "a" — rejection blocks the advance.
        run_now = project.get_run(str(started.run_id))
        assert run_now is not None
        assert run_now.current_state == "a"
    finally:
        project.close()
