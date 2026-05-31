"""Integration tests for the W12 adversarial verifier panel.

This file is a tight, behaviour-focused suite for
:mod:`ctxr.fsm.core.verifier` — both the runtime entry point
(:func:`run_verifier`) and its integration with the MCP commit
pipeline (:func:`fsm_commit_outputs`). It complements the broader
``test_two_phase_commit_verifier.py`` smoke by drilling into the four
panel behaviours that W12 must guarantee:

1. A state declaring a verifier whose payload passes the BUILT-IN
   structural verifier emits ``verifier_passed`` and mints a token.
2. A state whose verifier is overridden via :func:`set_verifier_handler`
   to a panel that votes mostly ``rejected`` emits ``verifier_rejected``
   and does NOT mint a token.
3. The built-in structural verifier, called directly with an outputs
   payload that violates the worker's response schema, returns a
   ``rejected`` outcome with one rejection per panel slot.
4. The majority rule is honest about its threshold: with
   ``parallel_count=3`` and ``majority_threshold=2``, two ``passed``
   votes + one ``rejected`` ⇒ panel passes; the inverse
   (one ``passed`` + two ``rejected``) ⇒ panel rejects.

Tests 1 and 2 drive the full MCP tool body in-process so the verifier
participates in the real commit pipeline (token issuance, journal
staging, event emission). Tests 3 and 4 call :func:`run_verifier`
directly with a manually-built :class:`Brief` because the engine's
``validate_output`` step runs *before* the verifier and would
short-circuit any schema-bad payload before it ever reached the panel
— the only way to exercise the structural verifier's reject path (and
the majority math in isolation) is to bypass the engine.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from ctxr.fsm.core import spec as _spec_module  # noqa: F401 (binds .hash/.validate)
from ctxr.fsm.core.engine import build_brief
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
    VerifierOutcome,
    VerifierVote,
    get_verifier_handler,
    run_verifier,
    set_verifier_handler,
)
from ctxr.fsm.mcp import _state as _mcp_state
from ctxr.fsm.mcp.tools_runs import (
    CommitOutputsInput,
    GetBriefInput,
    StartRunInput,
    fsm_commit_outputs,
    fsm_get_brief,
    fsm_start_run,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_enforcement import CommitTokenTable, JournalTxnTable
from ctxr.fsm.sqlite.models_events import EventTable

# ---------------------------------------------------------------------------
# Shared fixtures + helpers
# ---------------------------------------------------------------------------


# Worker schema accepted by ``validate_output`` (engine step 2) AND
# re-checked by the built-in structural verifier. Keeping it strict
# (``additionalProperties=False``, ``required=['verdict']``) means a
# stray field or a missing one will be caught by the schema validator;
# the ``verdict`` enum gives us a single field we can drive in tests.
_OK_SCHEMA = ResponseSchema(
    schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["ok", "bad"]},
        },
        "required": ["verdict"],
        "additionalProperties": False,
    }
)


# Panel's own output schema. Required by the VerifierSpec contract but
# not used by the structural fallback (which re-checks the WORKER's
# schema, not its own); kept here so the spec is well-formed.
_PANEL_SCHEMA = ResponseSchema(
    schema={
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["passed", "rejected"]},
            "reason": {"type": "string"},
        },
        "required": ["verdict"],
    }
)


_TEST_SESSION_ID = "w12-verifier-suite"


def _spec_with_verifier(
    *,
    spec_id: str = "w12_verifier_suite",
    majority_threshold: int = 2,
    parallel_count: int = 3,
) -> FsmSpec:
    """Build a two-state FSM whose entry state declares a verifier panel.

    The entry state's worker emits ``{verdict: "ok"|"bad"}`` and the
    verifier panel sits on the same state. Threshold + parallel count
    are knobs so the majority-rule test can spin one spec per scenario.
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
                    response_schema=_PANEL_SCHEMA,
                    majority_threshold=majority_threshold,
                    parallel_count=parallel_count,
                ),
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


@pytest.fixture
def project_factory():
    """Open a fresh Project per test on a temp SQLite database.

    The MCP module-global project handle is reset between every test so
    the in-process tool calls always see the latest binding. The
    verifier handler is also restored on teardown — tests that
    register a custom handler must NOT leak it into the next case.
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


def _start_run(project: Project, spec: FsmSpec):
    """Register the spec + start a run via the MCP start tool.

    Using the MCP body (rather than ``project.start_run`` directly)
    guarantees the entry state-entry row is persisted — the verifier /
    commit pipeline both expect that row to exist when looking up
    ``current_state_pk``.
    """
    registered = project.register_spec(spec)
    started = fsm_start_run(
        StartRunInput(spec_id=uuid.UUID(registered.spec.id), args={})
    )
    assert not hasattr(started, "error"), f"unexpected error: {started!r}"
    return registered, started


def _commit_with_signature(run_id: uuid.UUID, outputs: dict):
    """Commit ``outputs`` for ``run_id`` carrying a valid cosignature.

    Any state with a verifier or allowed_tools requires a cosignature
    (W12 layer-5). Every state in this suite declares a verifier, so
    every commit needs the signature envelope.
    """
    brief = fsm_get_brief(GetBriefInput(run_id=run_id))
    assert not hasattr(brief, "error"), f"unexpected error: {brief!r}"
    envelope = CommitSignature.compute(
        brief_id=brief.brief_id,
        inputs={},
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
# 1. Built-in structural verifier — pass path through the commit pipeline
# ---------------------------------------------------------------------------


def test_structural_verifier_passes_emits_event_and_mints_token(
    project_factory,
) -> None:
    """State with verifier set + schema-valid outputs ⇒ pass + token.

    No custom handler is registered, so the built-in structural
    verifier is the active panel. The outputs ``{"verdict": "ok"}``
    satisfy the worker's response schema, so every structural-panel
    slot votes ``passed`` and the panel's aggregate verdict is
    ``passed`` — which:

    * surfaces a ``verifier_passed`` event with the full vote list,
    * mints a single-use commit token (the result envelope carries it,
      and the ``commit_tokens`` row is live),
    * stages a ``ready_to_finalise`` journal txn (writes deferred to
      ``confirm_commit``; the run stays on ``"a"`` until then).
    """
    # Explicitly clear any leftover handler so the structural fallback
    # is unambiguously the panel under test.
    set_verifier_handler(None)
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())

        result = _commit_with_signature(started.run_id, {"verdict": "ok"})

        assert not hasattr(result, "error"), (
            f"expected success, got error envelope {result!r}"
        )
        assert result.kind == "advanced"
        assert result.next_state == "b"
        assert result.expected_next_state == "b"
        assert result.token is not None, "structural pass MUST mint a token"
        assert result.token.expected_next_state == "b"

        # verifier_passed event carries the full panel forensic trail.
        with project.session_factory() as session:
            rows = (
                session.execute(
                    select(EventTable).where(
                        EventTable.kind == EventKind.verifier_passed.value
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, (
            f"expected exactly one verifier_passed event, got {rows!r}"
        )
        payload = json.loads(rows[0].payload_json)
        assert payload["verdict"] == "passed"
        # Structural verifier emits one vote per parallel slot.
        assert payload["passed_count"] == 3
        assert payload["rejected_count"] == 0
        assert payload["majority_threshold"] == 2
        assert len(payload["votes"]) == 3
        for vote in payload["votes"]:
            assert vote["verdict"] == "passed"
            # Built-in fallback tags its reason explicitly so an
            # operator can tell the structural verifier ran (vs a
            # real LLM panel).
            assert vote["reason"] == "structural_check_ok"

        # Journal txn is staged (not yet applied) and the token row is
        # live — the engine sat at the boundary; confirm hasn't run.
        with project.session_factory() as session:
            txn_rows = (
                session.execute(
                    select(JournalTxnTable).where(
                        JournalTxnTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
            tok_rows = (
                session.execute(
                    select(CommitTokenTable).where(
                        CommitTokenTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(txn_rows) == 1 and txn_rows[0].status == "ready_to_finalise"
        assert len(tok_rows) == 1 and tok_rows[0].consumed_at is None
        assert tok_rows[0].expected_next_state == "b"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 2. Custom handler — mostly-rejected verdicts block the commit
# ---------------------------------------------------------------------------


def test_custom_handler_mostly_rejected_blocks_token(project_factory) -> None:
    """Custom handler returning mostly rejected ⇒ verifier_rejected, no token.

    Register a handler that votes ``rejected`` twice and ``passed``
    once. With ``parallel_count=3`` and ``majority_threshold=2`` the
    aggregate verdict is ``rejected`` — the panel demands two
    affirmative votes and only one was cast. The MCP commit body MUST:

    * return a ``verifier_rejected`` error envelope (no token),
    * emit a ``verifier_rejected`` event on the bus,
    * leave the journal_txn and commit_tokens tables empty for this run,
    * keep the run pinned to its current state.
    """

    def mostly_rejected(verifier, brief, outputs):
        # Two rejections, one acceptance — explicit per-slot so the
        # caller can tell the panel was the reason for the block.
        return [
            VerifierVote(verdict="rejected", reason="judge_1_says_no"),
            VerifierVote(verdict="rejected", reason="judge_2_says_no"),
            VerifierVote(verdict="passed", reason="judge_3_says_yes"),
        ]

    set_verifier_handler(mostly_rejected)
    project = project_factory()
    try:
        _, started = _start_run(project, _spec_with_verifier())

        result = _commit_with_signature(started.run_id, {"verdict": "ok"})

        # Error envelope — verifier rejection blocks the commit.
        assert hasattr(result, "error"), (
            f"expected verifier_rejected envelope, got success {result!r}"
        )
        assert result.error == "verifier_rejected"
        assert result.payload is not None
        assert result.payload["state"] == "a"
        assert result.payload["passed_count"] == 1
        assert result.payload["rejected_count"] == 2
        assert result.payload["majority_threshold"] == 2
        assert len(result.payload["reasons"]) == 3
        # No token field on the rejection envelope.
        assert getattr(result, "token", None) is None

        # No journal txn was staged — rejection happens BEFORE staging.
        with project.session_factory() as session:
            txn_rows = (
                session.execute(
                    select(JournalTxnTable).where(
                        JournalTxnTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
            tok_rows = (
                session.execute(
                    select(CommitTokenTable).where(
                        CommitTokenTable.run_id == str(started.run_id)
                    )
                )
                .scalars()
                .all()
            )
        assert txn_rows == [], "rejected verifier MUST NOT stage a journal txn"
        assert tok_rows == [], "rejected verifier MUST NOT mint a token"

        # verifier_rejected event landed on the bus with the panel's
        # forensic trail.
        with project.session_factory() as session:
            event_rows = (
                session.execute(
                    select(EventTable).where(
                        EventTable.kind == EventKind.verifier_rejected.value
                    )
                )
                .scalars()
                .all()
            )
        assert len(event_rows) == 1
        payload = json.loads(event_rows[0].payload_json)
        assert payload["verdict"] == "rejected"
        assert payload["passed_count"] == 1
        assert payload["rejected_count"] == 2

        # Run remains on the entry state — no advance happened.
        run_now = project.get_run(str(started.run_id))
        assert run_now is not None
        assert run_now.current_state == "a"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 3. Built-in structural verifier — reject path on bad outputs
# ---------------------------------------------------------------------------


def test_structural_verifier_rejects_bad_outputs_directly() -> None:
    """Default structural verifier on schema-invalid outputs ⇒ rejected.

    This test calls :func:`run_verifier` directly with a hand-built
    :class:`Brief` rather than driving the MCP pipeline. Reason: the
    engine's :func:`validate_output` step runs *before* the verifier
    and would short-circuit any schema-bad payload before the panel
    ever saw it. Direct invocation is the only way to exercise the
    structural verifier's reject branch.

    Asserts:

    * the aggregate verdict is ``rejected``,
    * one rejected vote per panel slot (``parallel_count=3``),
    * the rejection ``reason`` carries the structural error string
      (proves the structural verifier — not some other handler —
      produced the verdict).
    """
    # Make sure no leftover handler from another test interferes —
    # this scenario asserts the BUILT-IN fallback specifically.
    previous = get_verifier_handler()
    set_verifier_handler(None)
    try:
        spec = _spec_with_verifier()
        state = spec.get_state("a")
        assert state.verifier is not None
        brief = build_brief(spec, state, env={}, run_id=uuid.uuid4())

        # ``{"verdict": "nope"}`` violates the enum constraint on
        # ``verdict``; the structural verifier re-checks the worker's
        # response_schema and MUST reject.
        outcome: VerifierOutcome = run_verifier(
            state.verifier, brief, {"verdict": "nope"}
        )

        assert outcome.verdict == "rejected"
        assert outcome.passed_count == 0
        assert outcome.rejected_count == 3
        assert outcome.parallel_count == 3
        assert outcome.majority_threshold == 2
        assert len(outcome.votes) == 3
        for vote in outcome.votes:
            assert vote.verdict == "rejected"
            # Structural verifier surfaces jsonschema's error text
            # (the worker's enum violation), not a fixed string.
            assert "verdict" in vote.reason
    finally:
        set_verifier_handler(previous)


# ---------------------------------------------------------------------------
# 4. Majority rule — threshold honesty under split panels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("passed_votes", "rejected_votes", "expected_verdict"),
    [
        # 2 passed + 1 rejected meets the threshold of 2 ⇒ passes.
        (2, 1, "passed"),
        # 1 passed + 2 rejected falls below the threshold ⇒ rejects.
        (1, 2, "rejected"),
    ],
    ids=["two_pass_one_reject_passes", "one_pass_two_reject_rejects"],
)
def test_majority_rule_threshold_split_panels(
    passed_votes: int,
    rejected_votes: int,
    expected_verdict: str,
) -> None:
    """Majority math is honest at the panel boundary.

    With ``parallel_count=3`` and ``majority_threshold=2``, the
    aggregate verdict is ``passed`` iff ``passed_count >=
    majority_threshold``. This test feeds two split panels through
    :func:`run_verifier` via a custom handler and asserts the
    aggregate flips at exactly the right boundary.

    Calling :func:`run_verifier` directly (rather than the MCP body)
    keeps this test focused on the aggregation math — no token
    issuance, no journal, no events to muddy the assertion surface.
    """
    previous = get_verifier_handler()
    spec = _spec_with_verifier(majority_threshold=2, parallel_count=3)
    state = spec.get_state("a")
    assert state.verifier is not None
    brief = build_brief(spec, state, env={}, run_id=uuid.uuid4())

    def split_panel(verifier, _brief, _outputs):
        # Materialise the exact vote ratio for this scenario.
        votes: list[VerifierVote] = []
        for i in range(passed_votes):
            votes.append(VerifierVote(verdict="passed", reason=f"yes_{i}"))
        for i in range(rejected_votes):
            votes.append(VerifierVote(verdict="rejected", reason=f"no_{i}"))
        return votes

    set_verifier_handler(split_panel)
    try:
        outcome: VerifierOutcome = run_verifier(
            state.verifier, brief, {"verdict": "ok"}
        )
        assert outcome.verdict == expected_verdict
        assert outcome.passed_count == passed_votes
        assert outcome.rejected_count == rejected_votes
        assert outcome.parallel_count == 3
        assert outcome.majority_threshold == 2
        assert len(outcome.votes) == passed_votes + rejected_votes
    finally:
        set_verifier_handler(previous)
