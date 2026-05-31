"""Baseline E2E: seed a run via the Python facade, assert the real
Vite-served UI shows it on /runs and on /runs/:id.

This is the test the user asked for in W17: the user observed empty
UI pages against ``dummy-fsm-test`` and demanded a Playwright suite
that proves the UI actually displays runs / states / events. This
file is the proof — a real Vite dev server, real FastAPI backend,
real SQLite project, real browser, no mocks.
"""

from __future__ import annotations

import urllib.request

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
from tests.integration.e2e.conftest import LiveProject

_SEED_SPEC_ID = "e2e-baseline-fsm"


def _seed_spec() -> FsmSpec:
    """A small worker -> terminal spec the driver can run end-to-end."""
    return FsmSpec(
        id=_SEED_SPEC_ID,
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


def _seed_run_via_facade(live_project: LiveProject) -> str:
    """Open the live project's DB, register the seed spec, drive a run.

    Returns the run id so the UI test can assert on its presence.
    """
    db_path = live_project.project_root / ".ctxr-fsm" / "fsm.db"
    project = Project.open(db_path)
    try:
        project.register_spec(_seed_spec())
        result = drive_run_to_completion(
            project,
            spec_id=_SEED_SPEC_ID,
            entry_state_id="emit",
            args={"task": "baseline-e2e"},
            worker_outputs={"emit": {"verdict": "GO"}},
        )
        assert result.status == "completed"
        assert result.verdict == "GO"
        return result.run_id
    finally:
        project.close()


@pytest.mark.e2e
def test_ui_runs_list_shows_seeded_run(live_project: LiveProject, page) -> None:
    """``/runs`` lists the seeded run id and a completed status pill."""
    run_id = _seed_run_via_facade(live_project)

    # Sanity check the REST API first: if the API can't see the run,
    # the UI failure is downstream and unactionable from the test.
    runs_url = f"{live_project.api_url}/api/v1/runs"
    with urllib.request.urlopen(runs_url, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    assert run_id in body, (
        f"API list at {runs_url} did not include {run_id}; got: {body[:500]}"
    )

    page.goto(f"{live_project.ui_url}/runs", wait_until="domcontentloaded")

    # The UI shortens ids to the first 7 chars (git-style) in the
    # list, but the full id lands in the row's ``title`` attribute.
    short_id = run_id[:7]
    page.wait_for_selector(f"text={short_id}", timeout=15_000)

    # Status pill should read "completed" (semantic colour green).
    page.wait_for_selector("text=completed", timeout=10_000)


@pytest.mark.e2e
def test_ui_run_detail_shows_state_tree_and_events(
    live_project: LiveProject, page
) -> None:
    """``/runs/:id`` shows the state tree + event timeline for the seeded run."""
    run_id = _seed_run_via_facade(live_project)

    page.goto(
        f"{live_project.ui_url}/runs/{run_id}", wait_until="domcontentloaded"
    )

    # The run id itself appears in the header.
    page.wait_for_selector(f"text={run_id}", timeout=15_000)

    # State tree must surface the worker state id.
    page.wait_for_selector("text=emit", timeout=10_000)
    # Terminal state id is also present.
    page.wait_for_selector("text=done", timeout=10_000)

    # Event timeline must surface at least one of the lifecycle event
    # kinds the run produced. ``run_started`` is the safest pick because
    # every run emits it.
    page.wait_for_selector("text=run_started", timeout=10_000)
    # And the run_completed event must be present too (this asserts
    # commit_and_advance's terminal-detection fired).
    page.wait_for_selector("text=run_completed", timeout=10_000)
