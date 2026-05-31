"""E2E coverage across every UI route.

Each test seeds the project DB with the minimum data its route needs
to render meaningfully, then asserts the rendered DOM. The coverage
target is every ``<Route>`` declared in ``ui/src/app.tsx``:

  /                   redirect target -> /runs (asserted indirectly)
  /runs               run list
  /runs/:id           run detail (state tree + events + journal)
  /specs              registered FSM specs
  /consumers          event-bus consumers
  /settings           doctor report (db path, ports, drift config)
  (404)               unknown route -> NotFoundRoute

The ``live_project`` session-scoped fixture spins up the supervisor
once; each test seeds its own run / spec so cross-test isolation is
preserved without paying the supervisor-spin-up cost per test.
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

_COVERAGE_SPEC_ID = "e2e-coverage-fsm"


def _coverage_spec() -> FsmSpec:
    return FsmSpec(
        id=_COVERAGE_SPEC_ID,
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


def _ensure_seeded(live_project: LiveProject) -> str:
    """Register the coverage spec + drive a run; return the run id.

    Idempotent across tests: re-registering the same spec is a no-op
    (the repo dedupes by hash); driving another run creates a fresh
    row. We return the LATEST run id so each test can assert on a
    known-fresh run.
    """
    db_path = live_project.project_root / ".ctxr-fsm" / "fsm.db"
    project = Project.open(db_path)
    try:
        project.register_spec(_coverage_spec())
        result = drive_run_to_completion(
            project,
            spec_id=_COVERAGE_SPEC_ID,
            entry_state_id="emit",
            args={"task": "coverage"},
            worker_outputs={"emit": {"verdict": "GO"}},
        )
        assert result.status == "completed"
        return result.run_id
    finally:
        project.close()


# ---------------------------------------------------------------------------
# /  (root redirect / default route)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_ui_root_renders_runs_page(live_project: LiveProject, page) -> None:
    """``GET /`` mounts the Runs route (both ``/`` and ``/runs`` map there)."""
    _ensure_seeded(live_project)
    page.goto(f"{live_project.ui_url}/", wait_until="domcontentloaded")
    # Sidebar Runs entry should be marked current.
    page.wait_for_selector("text=Runs", timeout=10_000)


# ---------------------------------------------------------------------------
# /specs
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_ui_specs_page_lists_registered_spec(
    live_project: LiveProject, page
) -> None:
    """``/specs`` shows the registered ``e2e-coverage-fsm`` spec slug."""
    _ensure_seeded(live_project)

    # Sanity-check the API list first so a UI failure is unambiguous.
    api_specs_url = f"{live_project.api_url}/api/v1/specs"
    with urllib.request.urlopen(api_specs_url, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    assert _COVERAGE_SPEC_ID in body, (
        f"API specs list missing {_COVERAGE_SPEC_ID}; got: {body[:500]}"
    )

    page.goto(f"{live_project.ui_url}/specs", wait_until="domcontentloaded")
    page.wait_for_selector("h1:has-text('Specs')", timeout=10_000)
    page.wait_for_selector(f"text={_COVERAGE_SPEC_ID}", timeout=10_000)


# ---------------------------------------------------------------------------
# /consumers
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_ui_consumers_page_renders_header(
    live_project: LiveProject, page
) -> None:
    """``/consumers`` mounts the route header even when no consumer
    has registered. The empty state is part of the UI contract."""
    _ensure_seeded(live_project)
    page.goto(f"{live_project.ui_url}/consumers", wait_until="domcontentloaded")
    page.wait_for_selector("h1:has-text('Consumers')", timeout=10_000)


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_ui_settings_page_shows_doctor_report(
    live_project: LiveProject, page
) -> None:
    """``/settings`` fetches the doctor report and renders the DB path."""
    _ensure_seeded(live_project)
    page.goto(f"{live_project.ui_url}/settings", wait_until="domcontentloaded")
    page.wait_for_selector("h1:has-text('Settings')", timeout=10_000)
    # The doctor report carries the project's fsm.db path under
    # 'Project metadata'. Asserting on 'fsm.db' is enough to prove
    # the panel is mounted and the doctor POST resolved.
    page.wait_for_selector("text=fsm.db", timeout=15_000)


# ---------------------------------------------------------------------------
# 404 / NotFoundRoute
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_ui_unknown_route_renders_not_found(
    live_project: LiveProject, page
) -> None:
    """Any unmapped path renders the NotFoundRoute stub."""
    page.goto(
        f"{live_project.ui_url}/this-path-does-not-exist",
        wait_until="domcontentloaded",
    )
    page.wait_for_selector("text=Not found", timeout=10_000)


# ---------------------------------------------------------------------------
# /runs/:id — deeper detail assertions
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_ui_run_detail_shows_completed_status_and_verdict(
    live_project: LiveProject, page
) -> None:
    """``/runs/:id`` header surfaces the status pill + verdict pill."""
    run_id = _ensure_seeded(live_project)
    page.goto(
        f"{live_project.ui_url}/runs/{run_id}", wait_until="domcontentloaded"
    )
    page.wait_for_selector("text=completed", timeout=15_000)
    page.wait_for_selector("text=GO", timeout=10_000)
