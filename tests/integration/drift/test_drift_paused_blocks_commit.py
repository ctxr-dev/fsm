"""``drift_paused`` blocks ``commit_outputs`` when real drift signals contributed.

Long-LLM-friendliness fix: a real drift pause is a REAL commit gate
(not just a status flag silently overwritten by the next advance).
The gate refuses with ``run_drift_paused`` until ``fsm.resume_run``
clears the operator-confirmed drift.
"""

from __future__ import annotations

import uuid

from ctxr.fsm.core.models import EventKind, RunStatus
from ctxr.fsm.mcp.tools_runs import CommitOutputsInput, fsm_commit_outputs

from .conftest import emit_simple, run_one_sweep, seed_run


def _pause_via_real_drift(project, run_id) -> None:
    """Force ``run_id`` into drift_paused with REAL drift signals.

    Two ``commit_signature_mismatch`` events (weight 8 each) + one
    ``verifier_rejected`` (weight 6) put the score at 22 > 10 so the
    next sweep flips the run to drift_paused. The signal taxonomy here
    is intentionally NOT idle — that's the whole point of the test.
    """
    emit_simple(
        project, run_id=run_id, kind=EventKind.commit_signature_mismatch
    )
    emit_simple(
        project, run_id=run_id, kind=EventKind.commit_signature_mismatch
    )
    emit_simple(project, run_id=run_id, kind=EventKind.verifier_rejected)
    run_one_sweep(project)


def test_real_drift_pause_refuses_commit_outputs(project_factory) -> None:
    """Commit_outputs returns ``run_drift_paused`` when real drift contributed.

    Flow:
    1. Seed a run + force it into drift_paused via signature_mismatch
       + verifier_rejected (real-drift evidence).
    2. Call commit_outputs.
    3. Assert the response is an error envelope with code
       ``run_drift_paused`` and the contributing signal kinds
       listed in the payload.
    4. Assert the run remains drift_paused (no auto-clear fired).
    """
    project = project_factory()
    try:
        run_id = seed_run(project)
        _pause_via_real_drift(project, run_id)

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == RunStatus.drift_paused.value

        result = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=uuid.UUID(run_id),
                outputs={"hello": "world"},
            )
        )
        assert hasattr(result, "error"), (
            f"expected refusal envelope, got {result!r}"
        )
        assert result.error == "run_drift_paused"
        # Contributing kinds are surfaced on the structured payload for
        # the dashboard / operator.
        assert result.payload is not None
        contributing = result.payload.get("contributing_signal_kinds")
        assert contributing is not None
        assert "signature_mismatch" in contributing
        assert "verifier_rejection" in contributing

        # Status is still paused — auto-clear did NOT fire.
        run_after = project.get_run(run_id)
        assert run_after is not None
        assert run_after.status == RunStatus.drift_paused.value
    finally:
        project.close()
