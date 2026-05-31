"""Integration tests for the W12 commit cosignature enforcement primitive.

The cosignature is W12 layer-5: a SHA-256 commitment that binds a
brief (and its inputs) to the outputs a worker is about to commit.
The MCP commit pipeline (:func:`fsm_commit_outputs`) evaluates the
gate before any engine work runs:

* When the current state declares ``allowed_tools`` or a ``verifier``,
  or when the ``CTXR_FSM_REQUIRE_COSIGNATURE=1`` env var is set, the
  signature is REQUIRED. Missing => ``signature_required``. Wrong =>
  ``signature_mismatch`` + a ``commit_signature_mismatch`` bus event.
  Valid => a ``commit_signatures`` row is inserted with ``verified=True``
  and a ``commit_signature_verified`` bus event is emitted.
* When no trigger applies, the gate is skipped for back-compat — the
  commit advances as if signature support did not exist. This lets
  pre-W12 FSMs keep working unchanged.

The five tests in this module pin down the full truth table:

1. :func:`test_valid_signature_commits_and_emits_verified_event` —
   valid signature ⇒ commit succeeds, ``commit_signature_verified``
   event lands on the bus, a ``commit_signatures`` row is persisted.
2. :func:`test_wrong_signature_returns_mismatch_and_emits_event` —
   wrong signature ⇒ ``signature_mismatch`` envelope,
   ``commit_signature_mismatch`` event lands on the bus, no
   ``commit_signatures`` row is inserted and the run stays on the
   current state.
3. :func:`test_missing_signature_when_required_returns_signature_required`
   — state declares ``allowed_tools`` and the worker omits the
   signature ⇒ ``signature_required`` envelope.
4. :func:`test_missing_signature_when_not_required_is_allowed` — state
   has no ``allowed_tools`` / verifier and no env-var override ⇒ the
   commit succeeds without a signature (back-compat).
5. :func:`test_commit_signature_compute_is_deterministic` — the same
   inputs yield the same signature across repeated computes and across
   distinct compute call sites.

Tests drive the MCP tool bodies directly so each case stays fast (no
subprocess spawn) while still exercising the full Project facade +
W2 SQLite substrate end-to-end (real DB file, real Pydantic
validation, real event-bus persistence).
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
    State,
    Transition,
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
from ctxr.fsm.sqlite.models_enforcement import CommitSignatureTable
from ctxr.fsm.sqlite.models_events import EventTable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_TEST_SESSION_ID = "w12-cosignature-test"


@pytest.fixture
def project_factory():
    """Yield a callable that opens a Project on a per-test temp DB.

    The MCP module-global project handle is reset on teardown so the
    in-process tool calls in this module never leak a binding into the
    next test. A fresh SQLite file per test guarantees row counts and
    event-bus state are isolated.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"

        def _open() -> Project:
            project = Project.open(db_path, migrate=True)
            _mcp_state.set_project(project)
            return project

        yield _open

        _mcp_state.reset_project()


# ---------------------------------------------------------------------------
# Spec helpers
# ---------------------------------------------------------------------------


def _spec_with_allowed_tools(spec_id: str = "cosig_required_demo") -> FsmSpec:
    """A two-state spec whose entry state declares ``allowed_tools``.

    The presence of ``allowed_tools`` is enough on its own to flip the
    W12 cosignature gate on — no env var poking required. The entry
    state's ``transitions=[always → b]`` keeps the engine happy without
    needing a worker or schema; the commit body advances on any outputs
    dict because there is no schema to validate against.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry — allowed_tools forces cosignature on",
                allowed_tools=["Read", "Edit"],
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def _spec_without_enforcement(spec_id: str = "cosig_optional_demo") -> FsmSpec:
    """A two-state spec with no allowed_tools, no verifier, no worker.

    With nothing on the state that would trigger the W12 cosignature
    gate, the commit body must accept a commit that omits the
    signature entirely. This is the back-compat path: pre-W12 FSMs
    keep working unchanged.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(
                id="a",
                purpose="entry — no cosignature trigger",
                transitions=[Transition(to="b", when="always")],
            ),
            State(id="b", purpose="terminal", transitions=[]),
        ],
    )


def _start_run_via_mcp(
    project: Project,
    spec: FsmSpec,
    args: dict | None = None,
) -> tuple[object, uuid.UUID]:
    """Register ``spec`` + start a run through the MCP start tool body.

    Going through the MCP body (rather than ``project.start_run``
    directly) ensures the entry state-entry row is persisted — the
    commit pipeline expects that row to exist when it looks up
    ``current_state_pk`` to bind the commit_signatures row against.
    Returns ``(registered, run_id)`` for caller convenience.
    """
    registered = project.register_spec(spec)
    started = fsm_start_run(
        StartRunInput(spec_id=uuid.UUID(registered.spec.id), args=args or {})
    )
    assert not hasattr(started, "error"), f"unexpected error: {started!r}"
    return registered, started.run_id


# ---------------------------------------------------------------------------
# 1. Valid signature ⇒ commit succeeds + verified event + signatures row
# ---------------------------------------------------------------------------


def test_valid_signature_commits_and_emits_verified_event(project_factory) -> None:
    """Valid cosignature ⇒ advance, verified event, persisted row.

    Flow:
    1. Register an ``allowed_tools`` spec (gate is on) and start a run.
    2. Read the brief to discover ``brief_id`` — the worker would do
       the same so its signature can be reproduced server-side.
    3. Compute the signature against the same env (= the run's seed
       args, materialised by the commit body) and submit the commit.
    4. Assert the commit advanced, a ``commit_signatures`` row was
       inserted with ``verified=True``, and a single
       ``commit_signature_verified`` event landed on the bus.
    """
    project = project_factory()
    try:
        seed_args = {"seed": "x"}
        _registered, run_id = _start_run_via_mcp(
            project, _spec_with_allowed_tools(), args=seed_args
        )

        brief = fsm_get_brief(GetBriefInput(run_id=run_id))
        assert not hasattr(brief, "error"), f"unexpected error: {brief!r}"
        brief_id = brief.brief_id

        outputs = {"hello": "world"}
        envelope = CommitSignature.compute(
            brief_id=brief_id,
            inputs=dict(seed_args),
            outputs=outputs,
            session_id=_TEST_SESSION_ID,
        )

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=run_id,
                outputs=outputs,
                signature=envelope.signature,
                brief_id=brief_id,
                session_id=_TEST_SESSION_ID,
            )
        )

        assert not hasattr(result, "error"), (
            f"expected a successful CommitResult, got error envelope {result!r}"
        )
        assert result.kind == "advanced", (
            f"expected kind=advanced, got {result.kind!r}"
        )
        assert result.next_state == "b"

        # commit_signatures row must exist with verified=True and the
        # full envelope persisted.
        with project.session_factory() as session:
            sig_rows = session.execute(
                select(CommitSignatureTable).where(
                    CommitSignatureTable.run_id == str(run_id)
                )
            ).scalars().all()
        assert len(sig_rows) == 1, (
            f"expected exactly one commit_signatures row, got {sig_rows!r}"
        )
        sig = sig_rows[0]
        assert sig.signature == envelope.signature
        assert sig.inputs_hash == envelope.inputs_hash
        assert sig.outputs_hash == envelope.outputs_hash
        assert sig.session_id == _TEST_SESSION_ID
        assert sig.brief_id == str(brief_id)
        assert bool(sig.verified) is True

        # commit_signature_verified event on the bus, exactly one.
        with project.session_factory() as session:
            event_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_verified.value
                )
            ).scalars().all()
        assert len(event_rows) == 1, (
            f"expected one commit_signature_verified event, "
            f"got {event_rows!r}"
        )
        payload = json.loads(event_rows[0].payload_json)
        assert payload["run_id"] == str(run_id)
        assert payload["state"] == "a"
        assert payload["signature"] == envelope.signature
        assert payload["brief_id"] == str(brief_id)
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 2. Wrong signature ⇒ signature_mismatch + mismatch event + no commit
# ---------------------------------------------------------------------------


def test_wrong_signature_returns_mismatch_and_emits_event(project_factory) -> None:
    """Wrong cosignature ⇒ mismatch envelope, mismatch event, no advance.

    Compute the correct signature, flip one hex character so it stays
    structurally valid (64 lowercase hex chars) but semantically wrong,
    then submit. The commit body must:

    * Return the ``signature_mismatch`` error envelope carrying both
      the ``expected`` and ``got`` values for the operator to compare.
    * Emit a single ``commit_signature_mismatch`` event with the same
      values so the drift aggregator can pick it up.
    * NOT insert a ``commit_signatures`` row (the gate rejects before
      the verified-row persistence path runs).
    * Leave the run on state ``a`` so a retry with a valid signature
      is still possible.
    """
    project = project_factory()
    try:
        seed_args = {"seed": "x"}
        _registered, run_id = _start_run_via_mcp(
            project, _spec_with_allowed_tools(), args=seed_args
        )

        brief = fsm_get_brief(GetBriefInput(run_id=run_id))
        assert not hasattr(brief, "error"), f"unexpected error: {brief!r}"
        brief_id = brief.brief_id

        outputs = {"hello": "world"}
        envelope = CommitSignature.compute(
            brief_id=brief_id,
            inputs=dict(seed_args),
            outputs=outputs,
            session_id=_TEST_SESSION_ID,
        )
        # Flip the first hex char so the signature still looks valid
        # but no longer matches the recomputed value.
        bad_signature = (
            ("f" if envelope.signature[0] != "f" else "0") + envelope.signature[1:]
        )

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=run_id,
                outputs=outputs,
                signature=bad_signature,
                brief_id=brief_id,
                session_id=_TEST_SESSION_ID,
            )
        )

        assert hasattr(result, "error"), (
            f"expected error envelope, got success {result!r}"
        )
        assert result.error == "signature_mismatch"
        assert result.payload is not None
        assert result.payload["expected"] == envelope.signature
        assert result.payload["got"] == bad_signature

        # One mismatch event on the bus, no verified events.
        with project.session_factory() as session:
            mismatch_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_mismatch.value
                )
            ).scalars().all()
            verified_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_verified.value
                )
            ).scalars().all()
        assert len(mismatch_rows) == 1, (
            f"expected exactly one commit_signature_mismatch event, "
            f"got {mismatch_rows!r}"
        )
        assert verified_rows == [], (
            f"a rejection must NOT emit verified, got {verified_rows!r}"
        )
        payload = json.loads(mismatch_rows[0].payload_json)
        assert payload["run_id"] == str(run_id)
        assert payload["state"] == "a"
        assert payload["expected"] == envelope.signature
        assert payload["got"] == bad_signature
        assert payload["brief_id"] == str(brief_id)

        # No commit_signatures row was persisted.
        with project.session_factory() as session:
            sig_rows = session.execute(
                select(CommitSignatureTable).where(
                    CommitSignatureTable.run_id == str(run_id)
                )
            ).scalars().all()
        assert sig_rows == [], (
            f"mismatch must NOT persist a commit_signatures row, "
            f"got {sig_rows!r}"
        )

        # Run did not advance.
        run_now = project.get_run(str(run_id))
        assert run_now is not None
        assert run_now.current_state == "a", (
            f"a rejected commit must leave the run on 'a', "
            f"got {run_now.current_state!r}"
        )
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 3. Missing signature AND required ⇒ signature_required
# ---------------------------------------------------------------------------


def test_missing_signature_when_required_returns_signature_required(
    project_factory,
) -> None:
    """``allowed_tools`` non-empty + no signature ⇒ ``signature_required``.

    The allowed_tools trigger means the W12 cosignature gate is on for
    this state. Submitting a commit without the signature field must
    surface the ``signature_required`` error envelope carrying the
    offending state id so the worker can branch on it.

    No bus event is expected for this branch (the comment in
    ``fsm_commit_outputs`` notes that the call has not produced any
    outputs to bind / persist; the worker is expected to retry with
    the signature attached). We pin that absence here so a future
    drift-signal addition is a deliberate behaviour change.
    """
    project = project_factory()
    try:
        _registered, run_id = _start_run_via_mcp(
            project, _spec_with_allowed_tools(), args={}
        )

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=run_id,
                outputs={"hello": "world"},
                # signature, brief_id, session_id deliberately omitted
            )
        )

        assert hasattr(result, "error"), (
            f"expected error envelope, got success {result!r}"
        )
        assert result.error == "signature_required"
        assert result.payload is not None
        assert result.payload["state"] == "a"

        # No signature-related events on the bus — neither verified
        # nor mismatched fits the "no outputs were bound" semantics.
        with project.session_factory() as session:
            verified_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_verified.value
                )
            ).scalars().all()
            mismatch_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_mismatch.value
                )
            ).scalars().all()
        assert verified_rows == []
        assert mismatch_rows == []

        # No commit_signatures row.
        with project.session_factory() as session:
            sig_rows = session.execute(
                select(CommitSignatureTable).where(
                    CommitSignatureTable.run_id == str(run_id)
                )
            ).scalars().all()
        assert sig_rows == []

        # Run did not advance.
        run_now = project.get_run(str(run_id))
        assert run_now is not None
        assert run_now.current_state == "a"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 4. Missing signature AND not required ⇒ allowed (back-compat)
# ---------------------------------------------------------------------------


def test_missing_signature_when_not_required_is_allowed(project_factory) -> None:
    """No trigger ⇒ commit without signature advances normally.

    The state has no ``allowed_tools``, no ``verifier``, and the
    process env var is not set. The W12 gate is therefore inactive
    and the commit body must skip the signature check entirely. This
    is the back-compat contract for pre-W12 FSMs.

    We monkeypatch the env var off in case a previous test (or a CI
    pipeline) left it set; the contract is "no trigger AND no env var".
    """
    project = project_factory()
    try:
        # Defensively clear the env var so the gate is genuinely off.
        # We use ``pytest.MonkeyPatch`` inline (rather than the fixture)
        # so the fixture surface stays minimal and the cleanup is
        # explicit even if the test errors before the finally block.
        with pytest.MonkeyPatch.context() as monkey:
            monkey.delenv("CTXR_FSM_REQUIRE_COSIGNATURE", raising=False)

            _registered, run_id = _start_run_via_mcp(
                project, _spec_without_enforcement(), args={}
            )

            result = fsm_commit_outputs(
                CommitOutputsInput(
                    run_id=run_id,
                    outputs={"hello": "world"},
                    # signature deliberately omitted — the gate is off.
                )
            )

        assert not hasattr(result, "error"), (
            f"expected a successful CommitResult, got error envelope {result!r}"
        )
        assert result.kind == "advanced", (
            f"expected kind=advanced for the back-compat path, "
            f"got {result.kind!r}"
        )
        assert result.next_state == "b"

        # No commit_signatures row — the gate did not run so nothing
        # got persisted into the audit table.
        with project.session_factory() as session:
            sig_rows = session.execute(
                select(CommitSignatureTable).where(
                    CommitSignatureTable.run_id == str(run_id)
                )
            ).scalars().all()
        assert sig_rows == [], (
            f"back-compat path must not insert a commit_signatures row, "
            f"got {sig_rows!r}"
        )

        # No verified / mismatch events either — the gate did not fire.
        with project.session_factory() as session:
            verified_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_verified.value
                )
            ).scalars().all()
            mismatch_rows = session.execute(
                select(EventTable).where(
                    EventTable.kind == EventKind.commit_signature_mismatch.value
                )
            ).scalars().all()
        assert verified_rows == []
        assert mismatch_rows == []
    finally:
        project.close()


# ---------------------------------------------------------------------------
# 5. CommitSignature.compute determinism
# ---------------------------------------------------------------------------


def test_commit_signature_compute_is_deterministic() -> None:
    """Same inputs ⇒ same signature, across repeated computes.

    The cosignature contract relies on both sides (worker and server)
    independently computing the same value from the same fields. This
    test pins the determinism guarantee at the model level so a
    regression in the canonical-JSON helper, the hash function, or the
    envelope ordering surfaces here before it confuses the wire
    interaction tests.

    Specifically:

    * Two calls with identical inputs return byte-identical signatures
      and identical component hashes (``inputs_hash`` / ``outputs_hash``).
    * Reordering dict keys in the inputs/outputs dicts does NOT change
      the signature — canonical JSON sorts keys.
    * Changing any one component (brief_id, inputs, outputs,
      session_id) does change the signature — the envelope hash binds
      all four fields.
    """
    brief_id = uuid.UUID("11111111-1111-7111-8111-111111111111")
    inputs = {"seed": "x", "k": 42}
    outputs = {"hello": "world", "n": 1}
    session_id = "deterministic-session"

    a = CommitSignature.compute(
        brief_id=brief_id,
        inputs=inputs,
        outputs=outputs,
        session_id=session_id,
    )
    b = CommitSignature.compute(
        brief_id=brief_id,
        inputs=inputs,
        outputs=outputs,
        session_id=session_id,
    )

    # Identical inputs ⇒ identical signature + identical component
    # hashes. The signature is the SHA-256 of the canonical envelope,
    # so collisions across repeated calls are vanishingly unlikely
    # unless the hashing path is broken.
    assert a.signature == b.signature
    assert a.inputs_hash == b.inputs_hash
    assert a.outputs_hash == b.outputs_hash
    assert a.brief_id == b.brief_id == brief_id
    assert a.session_id == b.session_id == session_id

    # Key order in dicts is irrelevant — canonical JSON sorts keys.
    c = CommitSignature.compute(
        brief_id=brief_id,
        inputs={"k": 42, "seed": "x"},  # reordered
        outputs={"n": 1, "hello": "world"},  # reordered
        session_id=session_id,
    )
    assert c.signature == a.signature
    assert c.inputs_hash == a.inputs_hash
    assert c.outputs_hash == a.outputs_hash

    # Any single-field change MUST flip the signature so the envelope
    # cannot be silently replayed against different bound values.
    other_brief = uuid.UUID("22222222-2222-7222-8222-222222222222")
    diff_brief = CommitSignature.compute(
        brief_id=other_brief,
        inputs=inputs,
        outputs=outputs,
        session_id=session_id,
    )
    diff_inputs = CommitSignature.compute(
        brief_id=brief_id,
        inputs={"seed": "y", "k": 42},
        outputs=outputs,
        session_id=session_id,
    )
    diff_outputs = CommitSignature.compute(
        brief_id=brief_id,
        inputs=inputs,
        outputs={"hello": "WORLD", "n": 1},
        session_id=session_id,
    )
    diff_session = CommitSignature.compute(
        brief_id=brief_id,
        inputs=inputs,
        outputs=outputs,
        session_id="other-session",
    )

    distinct = {
        a.signature,
        diff_brief.signature,
        diff_inputs.signature,
        diff_outputs.signature,
        diff_session.signature,
    }
    assert len(distinct) == 5, (
        f"every one-field change must produce a distinct signature; "
        f"got {len(distinct)} distinct values out of 5 computes: {distinct!r}"
    )

    # The hex shape is locked: 64 lowercase hex chars (SHA-256 hex
    # digest). A regression that switched to bytes / b64 / uppercase
    # would silently break interop with the server-side recompute.
    assert len(a.signature) == 64
    assert all(ch in "0123456789abcdef" for ch in a.signature)
    assert len(a.inputs_hash) == 64
    assert len(a.outputs_hash) == 64
