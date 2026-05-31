"""Integration test for the W14k BLOCKER-2 inline-chain driver.

Proves that after a worker commits via the MCP commit/confirm path,
the engine's ``execute_inline`` is invoked SERVER-SIDE for any inline
states the run advances through. Without this wiring, inline state
handlers (like the critical ``write_run_directory`` that produces
``report.md`` in skill-code-review) never run and the
LLM-as-orchestrator template falls apart.

The fixture FSM is intentionally minimal: one worker state →
one inline state → one terminal state. The inline state's handler
records that it ran into a process-local list so the test can assert
"the inline handler actually executed".
"""

from __future__ import annotations

import os
from typing import Any

from ctxr.fsm.core import (
    FsmSpec,
    InlineContext,
    InlineHandlerRegistry,
    get_default_registry,
)
from ctxr.fsm.core.models import (
    EngineAdvanceKind,
    EventKind,
    InlineFaultReason,
    StateKind,
)
from ctxr.fsm.mcp._state import reset_project, set_project
from ctxr.fsm.mcp.tools_runs import (
    CommitOutputsInput,
    ConfirmCommitInput,
    StartRunInput,
    fsm_commit_outputs,
    fsm_confirm_commit,
    fsm_start_run,
)
from ctxr.fsm.sqlite.project import Project


_HANDLER_INVOCATIONS: list[InlineContext] = []
"""Module-global recording slot. Each call to the inline handler in this
fixture appends its ``InlineContext`` so tests can assert against the
exact context the engine passed (run_id, fsm_id, state_id, inputs)."""


def _record_invocation(ctx: InlineContext) -> dict[str, Any]:
    """Test inline handler: record the call + return a stable output dict."""

    _HANDLER_INVOCATIONS.append(ctx)
    return {"recorded": True, "by_handler": "inline_recorder"}


def _build_spec() -> FsmSpec:
    """Build a minimal worker → inline → terminal spec.

    The worker state has a trivial response_schema so the test can
    commit a small valid output. The inline state has a matching
    response_schema for its handler's return dict. The terminal state
    has no body and no transitions.
    """

    return FsmSpec.model_validate(
        {
            "id": "inline_chain_demo",
            "version": 1,
            "entry": "worker_step",
            "states": [
                {
                    "id": "worker_step",
                    "purpose": "the LLM-driven step",
                    "worker": {
                        "role": "dummy",
                        "prompt_template": "say something",
                        "inputs": ["seed"],
                        "response_schema": {
                            "schema": {
                                "type": "object",
                                "required": ["worker_said"],
                                "properties": {"worker_said": {"type": "string"}},
                                "additionalProperties": False,
                            }
                        },
                    },
                    "outputs": ["worker_said"],
                    "transitions": [{"to": "inline_step", "when": "always"}],
                },
                {
                    "id": "inline_step",
                    "purpose": "deterministic server-side step",
                    "inline": {
                        "handler_id": "test_recorder",
                        "response_schema": {
                            "schema": {
                                "type": "object",
                                "required": ["recorded", "by_handler"],
                                "properties": {
                                    "recorded": {"type": "boolean"},
                                    "by_handler": {"type": "string"},
                                },
                                "additionalProperties": False,
                            }
                        },
                    },
                    "outputs": ["recorded", "by_handler"],
                    "transitions": [{"to": "done", "when": "always"}],
                },
                {
                    "id": "done",
                    "purpose": "terminal",
                    "outputs": [],
                    "transitions": [],
                },
            ],
        }
    )


def _setup_project_and_register(tmp_path: Any) -> Project:
    """Open a Project, register the fixture spec + inline handler, prime MCP."""

    project = Project.open(tmp_path / ".ctxr-fsm" / "fsm.db")
    project.register_spec(_build_spec())
    registry = get_default_registry()
    registry.register("inline_chain_demo", "test_recorder", _record_invocation)
    set_project(project)
    return project


def test_inline_handler_runs_server_side_after_worker_commit(tmp_path: Any) -> None:
    """End-to-end: commit a worker output → inline handler runs → terminal.

    Without the BLOCKER-2 wiring this test would advance to ``inline_step``
    but the handler would never run; the test would see an empty
    ``_HANDLER_INVOCATIONS`` list and a non-terminal post-confirm brief.
    With the wiring, the inline driver kicks in after confirm replays
    the worker commit, runs the handler, advances through ``inline_step``,
    and lands the run at ``done`` (terminal).
    """

    _HANDLER_INVOCATIONS.clear()
    project = _setup_project_and_register(tmp_path)
    try:
        # Start a run.
        started = fsm_start_run(
            StartRunInput(spec_id="inline_chain_demo", args={"seed": "test"})
        )
        assert not isinstance(started, dict), f"start_run errored: {started}"
        run_id = str(started.run_id)

        # Commit worker output → two-phase token returned.
        commit = fsm_commit_outputs(
            CommitOutputsInput(
                run_id=run_id,
                outputs={"worker_said": "hello"},
            )
        )
        assert not isinstance(commit, dict), f"commit_outputs errored: {commit}"
        assert commit.kind == "advanced", f"unexpected commit kind: {commit.kind}"
        assert commit.token is not None

        # Confirm the commit → replay runs the worker commit + the
        # inline driver kicks in for ``inline_step`` + the run lands
        # at ``done``.
        confirm = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.token.expected_next_state,
            )
        )
        assert not isinstance(confirm, dict), f"confirm_commit errored: {confirm}"
        assert confirm.confirmed

        # ---- The actual W14k assertion ----
        # The inline handler MUST have been invoked exactly once.
        assert len(_HANDLER_INVOCATIONS) == 1, (
            f"inline handler should have run exactly once; got "
            f"{len(_HANDLER_INVOCATIONS)} invocations"
        )

        recorded_ctx = _HANDLER_INVOCATIONS[0]
        assert str(recorded_ctx.run_id) == run_id
        assert recorded_ctx.fsm_id == "inline_chain_demo"
        assert recorded_ctx.state_id == "inline_step"

        # The post-confirm brief carries the TERMINAL state, NOT
        # the inline state (the chain driver walked through it).
        assert confirm.next_brief is not None
        assert confirm.next_brief.state == "done"

        # The run row's status was flipped to completed by the
        # terminal-handling branch in the chain driver.
        run = project.get_run(run_id)
        assert run is not None
        assert run.status == "completed"

        # The audit trail shows inline_executed event between the
        # worker_committed and the run_completed events.
        with project.session_factory() as session:
            events = list(project.events.by_run(session, run_id))
        kinds = [e.kind for e in events]
        assert EventKind.inline_executed.value in kinds, (
            f"expected inline_executed in event log; got kinds={kinds}"
        )
    finally:
        get_default_registry().clear()
        reset_project()


def test_inline_handler_missing_faults_run_with_clear_reason(tmp_path: Any) -> None:
    """When the inline handler isn't registered, the run pauses with a clear fault."""

    _HANDLER_INVOCATIONS.clear()
    project = Project.open(tmp_path / ".ctxr-fsm" / "fsm.db")
    project.register_spec(_build_spec())
    # NOTE: we deliberately do NOT register the inline handler. The
    # entry-points discovery walk runs once + finds nothing relevant +
    # the registry stays empty for this spec.
    get_default_registry().clear()
    set_project(project)
    try:
        started = fsm_start_run(
            StartRunInput(spec_id="inline_chain_demo", args={"seed": "x"})
        )
        assert not isinstance(started, dict)
        run_id = str(started.run_id)

        commit = fsm_commit_outputs(
            CommitOutputsInput(run_id=run_id, outputs={"worker_said": "x"})
        )
        assert not isinstance(commit, dict)
        assert commit.token is not None

        confirm = fsm_confirm_commit(
            ConfirmCommitInput(
                token=commit.token.token,
                expected_next_state=commit.token.expected_next_state,
            )
        )
        # confirm_commit still returns confirmed=True (the worker
        # commit + transition did happen); the inline-chain fault is
        # surfaced via the run_row.status + the inline_failed event.
        assert not isinstance(confirm, dict)

        run = project.get_run(run_id)
        assert run is not None
        assert run.status == "faulted", (
            f"missing handler should fault the run; got status={run.status}"
        )

        with project.session_factory() as session:
            events = list(project.events.by_run(session, run_id))
        kinds = [e.kind for e in events]
        assert EventKind.inline_failed.value in kinds, (
            f"expected inline_failed in event log; got kinds={kinds}"
        )

        # The fault payload identifies the handler that was missing
        # so an operator can wire it up + resume.
        failed = next(
            e for e in events if e.kind == EventKind.inline_failed.value
        )
        assert failed.payload["state_id"] == "inline_step"
        assert failed.payload["handler_id"] == "test_recorder"
        assert failed.payload["reason"] == InlineFaultReason.unregistered.value
    finally:
        get_default_registry().clear()
        reset_project()
