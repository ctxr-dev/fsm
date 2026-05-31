"""Unit tests for ``ctxr.fsm.testing.drive_run_to_completion``.

The driver helper is exercised against a small, hand-built
two-state FSM (one worker -> one terminal) so the test does not
depend on the skill-code-review package being installed in the test
runtime.
"""

from __future__ import annotations

import pytest

from ctxr.fsm.core.models import (
    FsmSpec,
    ResponseSchema,
    State,
    Transition,
    TransitionKind,
    Worker,
)
from ctxr.fsm.sqlite.project import Project
from ctxr.fsm.testing import drive_run_to_completion


def _make_spec() -> FsmSpec:
    return FsmSpec(
        id="driver-fixture",
        version=1,
        entry="emit",
        states=[
            State(
                id="emit",
                worker=Worker(
                    role="emitter",
                    prompt_template="unused.md",
                    inputs=[],
                    response_schema=ResponseSchema(
                        schema={
                            "type": "object",
                            "properties": {"verdict": {"type": "string"}},
                            "required": ["verdict"],
                            "additionalProperties": False,
                        },
                    ),
                ),
                outputs=["verdict"],
                transitions=[
                    Transition(to="done", when=TransitionKind.always.value),
                ],
            ),
            State(id="done", outputs=[], transitions=[]),
        ],
    )


def test_drive_run_to_completion_commits_canned_output(tmp_path) -> None:
    db = tmp_path / "fsm.db"
    project = Project.open(db)
    spec = _make_spec()
    project.register_spec(spec)

    result = drive_run_to_completion(
        project,
        spec_id="driver-fixture",
        entry_state_id="emit",
        args={"task": "x"},
        worker_outputs={"emit": {"verdict": "GO"}},
    )

    assert result.status == "completed"
    assert result.verdict == "GO"
    assert result.visited_worker_states == ["emit"]


def test_drive_run_to_completion_raises_on_missing_output(tmp_path) -> None:
    db = tmp_path / "fsm.db"
    project = Project.open(db)
    spec = _make_spec()
    project.register_spec(spec)

    with pytest.raises(KeyError, match="emit"):
        drive_run_to_completion(
            project,
            spec_id="driver-fixture",
            entry_state_id="emit",
            args={},
            worker_outputs={},
        )


def test_drive_run_to_completion_raises_on_unknown_spec(tmp_path) -> None:
    db = tmp_path / "fsm.db"
    project = Project.open(db)
    with pytest.raises(ValueError, match="not registered"):
        drive_run_to_completion(
            project,
            spec_id="nope",
            entry_state_id="x",
            args={},
            worker_outputs={},
        )
