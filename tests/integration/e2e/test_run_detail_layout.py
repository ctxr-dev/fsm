"""E2E coverage: ``/runs/:id`` 50/50 layout cutover (PR 7).

Seeds a single completed run and drives a real browser against the
Vite-served UI to assert:

  - The 2-column grid (graph | timeline) is present at desktop width.
  - The header carries an "Admin" button that opens a sheet.
  - Clicking a graph node opens the StateEntrySheet with three tabs.

The test runs against the same ``live_project`` supervisor fixture as
the rest of the e2e suite, so spin-up cost is paid once across all
e2e tests rather than per-test.
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
from tests.integration.e2e.conftest import LiveProject

_LAYOUT_SPEC_ID = "e2e-layout-fsm"


def _layout_spec() -> FsmSpec:
    """A tiny worker -> terminal spec — two graph nodes is enough to
    prove the node-click handler reaches the StateEntrySheet."""
    return FsmSpec(
        id=_LAYOUT_SPEC_ID,
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


def _seed_run(live_project: LiveProject) -> str:
    db_path = live_project.project_root / ".ctxr-fsm" / "fsm.db"
    project = Project.open(db_path)
    try:
        project.register_spec(_layout_spec())
        result = drive_run_to_completion(
            project,
            spec_id=_LAYOUT_SPEC_ID,
            entry_state_id="emit",
            args={"task": "layout-e2e"},
            worker_outputs={"emit": {"verdict": "GO"}},
        )
        assert result.status == "completed"
        return result.run_id
    finally:
        project.close()


@pytest.mark.e2e
def test_run_detail_50_50_grid_present(
    live_project: LiveProject, page
) -> None:
    """The route renders the 2-column grid with both columns visible."""
    run_id = _seed_run(live_project)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(
        f"{live_project.ui_url}/runs/{run_id}", wait_until="domcontentloaded"
    )

    # The route container carries data-testid="run-detail-route" so the
    # selector pins the right surface even if Tailwind class names move.
    page.wait_for_selector('[data-testid="run-detail-route"]', timeout=15_000)

    # 50/50 grid is identifiable by data-testid + the ``lg:grid-cols-2``
    # Tailwind class. Both must be present.
    grid = page.wait_for_selector(
        '[data-testid="run-detail-grid"]', timeout=10_000
    )
    cls = grid.get_attribute("class") or ""
    assert "lg:grid-cols-2" in cls, (
        f"expected the 50/50 grid class on the run-detail grid; got: {cls}"
    )
    assert "grid-cols-1" in cls, (
        f"expected the mobile-fallback grid-cols-1 class; got: {cls}"
    )


@pytest.mark.e2e
def test_run_detail_admin_button_opens_sheet(
    live_project: LiveProject, page
) -> None:
    """Clicking the header "Admin" button opens the admin sheet."""
    run_id = _seed_run(live_project)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(
        f"{live_project.ui_url}/runs/{run_id}", wait_until="domcontentloaded"
    )

    # Wait for the header to settle so the action buttons are clickable.
    admin_btn = page.wait_for_selector(
        'button:has-text("Admin")', timeout=15_000
    )
    admin_btn.click()

    # The AdminSheet (PR 3) carries the "Run admin" title from the
    # openAdminSheet helper. Confirm by waiting for that text inside an
    # aside / dialog landmark that the Sheet primitive renders.
    page.wait_for_selector("text=Run admin", timeout=10_000)


@pytest.mark.e2e
def test_run_detail_graph_node_click_opens_state_entry_sheet(
    live_project: LiveProject, page
) -> None:
    """Clicking a graph node opens the StateEntrySheet with three tabs."""
    run_id = _seed_run(live_project)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(
        f"{live_project.ui_url}/runs/{run_id}", wait_until="domcontentloaded"
    )

    # Wait until the run progress graph has rendered (the spec fetch +
    # FlowGraph layout pipeline takes a tick). The xyflow nodes carry
    # ``data-id`` matching the spec state id.
    page.wait_for_selector('[data-id="emit"]', timeout=15_000)
    page.click('[data-id="emit"]')

    # StateEntrySheetBody renders three role="tab" buttons (PR 4):
    # "Run values", "Spec definition", "Events for this state".
    page.wait_for_selector('role=tab[name=/Run values/i]', timeout=10_000)
    page.wait_for_selector('role=tab[name=/Spec definition/i]', timeout=10_000)
    page.wait_for_selector(
        'role=tab[name=/Events for this state/i]', timeout=10_000
    )
