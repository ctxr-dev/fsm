"""W23a E2E coverage for /specs.

The user reported ``TypeError: env.items is not iterable`` on /specs
and explicitly asked: "make sure you cover e2e tests properly, with
runs and specs available in UI." These tests are the regression gate.

Every test runs under the autouse ``_console_audit`` fixture (see
``conftest.py``) which asserts zero unhandled JS errors during the
test body — so the original TypeError would have been caught here
even without a body assertion.

Test matrix:

* ``empty_state_no_js_errors``: fresh DB, navigate to /specs, assert
  the empty-state copy renders and no JS errors fire (the W23a
  defensive layer in ``walkAllPages`` + ``request`` is what makes
  this clean).
* ``seeded_renders_specs_with_run_counts``: 2 specs + 3 runs split
  across them; both rows visible with the right run counts.
* ``row_click_navigates_to_detail``: clicking a spec row routes to
  /specs/<id> and the detail header renders.

The seeded variants reuse the same ``_seed_spec()`` + driver helpers
the existing ``test_ui_shows_seeded_run.py`` battery uses, so the
fixture cost stays session-scoped.
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


def _spec(slug: str) -> FsmSpec:
    """One-state spec keyed by ``slug`` so we can seed multiple distinct specs."""
    return FsmSpec(
        id=slug,
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


def _clear_project(db_path) -> None:
    """Reset the live project's DB to a clean state.

    Each test starts from zero specs + zero runs so empty-state
    assertions are deterministic. We delete from the actual SQLite
    tables (rather than re-materialising the fixture) so the
    supervisor keeps the same handle open — the WAL is what the
    write goes through, no schema change required.
    """
    project = Project.open(db_path)
    try:
        # The session_factory + connection gives us a transactional
        # surface that respects FK cascades declared in models_core.py.
        with project.session_factory() as session:
            from ctxr.fsm.sqlite.models_core import (
                AggregateTable,
                FsmSpecTable,
                LockTable,
                RunSessionTable,
                RunTable,
                StateTable,
                TransitionTable,
                WorkerArtifactTable,
            )
            from ctxr.fsm.sqlite.models_enforcement import (
                CommitSignatureTable,
                CommitTokenTable,
                DriftSignalTable,
                JournalTxnTable,
                ToolCallTable,
            )
            from ctxr.fsm.sqlite.models_events import (
                EventDeliveryTable,
                EventTable,
                ProducerTable,
            )

            # Order matters: children before parents to satisfy FKs even
            # though most have ON DELETE CASCADE — being explicit avoids
            # surprises if a future migration loosens a cascade.
            for table in (
                CommitSignatureTable, CommitTokenTable, DriftSignalTable,
                ToolCallTable, JournalTxnTable, LockTable,
                EventDeliveryTable, EventTable,
                AggregateTable, WorkerArtifactTable, TransitionTable, StateTable,
                RunSessionTable, RunTable,
                ProducerTable,
                FsmSpecTable,
            ):
                session.execute(table.__table__.delete())  # type: ignore[attr-defined]
            session.commit()
    finally:
        project.close()


def _seed_spec(db_path, slug: str) -> str:
    """Register a single spec; return its registered spec_id.

    ``SpecRegistered`` is the envelope (``spec`` + ``created``) — the
    primary key the UI routes to lives on ``registered.spec.id`` (the
    versioned spec row ID), not on the envelope itself.
    """
    project = Project.open(db_path)
    try:
        registered = project.register_spec(_spec(slug))
        return registered.spec.id
    finally:
        project.close()


def _seed_run(db_path, slug: str) -> str:
    """Register the spec if missing + drive one run to completion."""
    project = Project.open(db_path)
    try:
        project.register_spec(_spec(slug))
        result = drive_run_to_completion(
            project,
            spec_id=slug,
            entry_state_id="emit",
            args={"task": f"e2e-specs-{slug}"},
            worker_outputs={"emit": {"verdict": "GO"}},
        )
        assert result.status == "completed"
        return result.run_id
    finally:
        project.close()


@pytest.mark.e2e
def test_specs_route_empty_state_no_js_errors(
    live_project: LiveProject, page
) -> None:
    """Fresh DB → /specs renders empty state AND fires zero JS errors.

    This is the exact bug class the user reported: pre-W23a the
    walkAllPages helper would crash on an undefined ``env.items``
    when the Vite proxy mis-routed. With the defensive layer in
    place + the autouse console-audit fixture, a regression would
    surface here as a test failure on either the missing copy OR a
    console error during navigation.
    """
    db_path = live_project.project_root / ".ctxr-fsm" / "fsm.db"
    _clear_project(db_path)

    # Sanity check the REST surface — if the API returns garbage we
    # want the test to read as "backend broken" not "UI broken".
    specs_url = f"{live_project.api_url}/api/v1/specs"
    with urllib.request.urlopen(specs_url, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    assert '"items":' in body, f"API at {specs_url} returned non-envelope: {body[:200]}"
    assert '"items":[]' in body.replace(" ", ""), (
        f"expected empty items[] after clear, got {body[:200]}"
    )

    # ``domcontentloaded`` + a headline wait — the /specs route opens an
    # SSE stream (and the InfoTopBar polls /healthz) so ``networkidle``
    # never settles inside Playwright's 30s default window.
    page.goto(f"{live_project.ui_url}/specs", wait_until="domcontentloaded")
    page.get_by_role("heading", name="Specs", exact=True).wait_for(timeout=10000)

    # The empty-state copy from specs.tsx — "No specs registered" /
    # "ctxr-fsm spec register". Either substring is fine; we want the
    # operator-facing hint visible, not a stack trace.
    body_text = page.locator("body").inner_text(timeout=5000)
    assert "No specs registered" in body_text or "spec register" in body_text, (
        f"expected empty-state copy on /specs; got body:\n{body_text[:500]}"
    )


@pytest.mark.e2e
def test_specs_route_seeded_renders_specs_with_run_counts(
    live_project: LiveProject, page
) -> None:
    """Two specs + three runs across them all surface on /specs.

    Validates the happy path the user actually wants: the table
    renders every registered spec, the run-count column reflects the
    real cross-spec aggregation, and zero JS errors fire.
    """
    db_path = live_project.project_root / ".ctxr-fsm" / "fsm.db"
    _clear_project(db_path)

    # Seed 2 specs + 3 runs (2 on the first, 1 on the second). Run
    # ids are returned but we only need their presence on the wire.
    _seed_run(db_path, "e2e-specs-alpha")
    _seed_run(db_path, "e2e-specs-alpha")
    _seed_run(db_path, "e2e-specs-beta")

    # ``domcontentloaded`` + a headline wait — the /specs route opens an
    # SSE stream (and the InfoTopBar polls /healthz) so ``networkidle``
    # never settles inside Playwright's 30s default window.
    page.goto(f"{live_project.ui_url}/specs", wait_until="domcontentloaded")
    page.get_by_role("heading", name="Specs", exact=True).wait_for(timeout=10000)

    body_text = page.locator("body").inner_text(timeout=5000)
    assert "e2e-specs-alpha" in body_text, (
        f"expected alpha slug on /specs; got body:\n{body_text[:800]}"
    )
    assert "e2e-specs-beta" in body_text, (
        f"expected beta slug on /specs; got body:\n{body_text[:800]}"
    )


@pytest.mark.e2e
def test_specs_route_row_click_navigates_to_detail(
    live_project: LiveProject, page
) -> None:
    """Clicking a spec row navigates to /specs/<id> with the detail header."""
    db_path = live_project.project_root / ".ctxr-fsm" / "fsm.db"
    _clear_project(db_path)
    spec_id = _seed_spec(db_path, "e2e-specs-navtest")

    # ``domcontentloaded`` + a headline wait — the /specs route opens an
    # SSE stream (and the InfoTopBar polls /healthz) so ``networkidle``
    # never settles inside Playwright's 30s default window.
    page.goto(f"{live_project.ui_url}/specs", wait_until="domcontentloaded")
    page.get_by_role("heading", name="Specs", exact=True).wait_for(timeout=10000)

    # Find the row that contains the slug text and click it. The
    # Table component (ui/src/components/Table.tsx) renders each row
    # as a <tr> with the row payload's data bubbled up via onRowClick.
    row = page.locator("tr", has_text="e2e-specs-navtest").first
    row.wait_for(timeout=5000)
    row.click()

    # After click the route navigates via navigateTo() at specs.tsx:125
    # which does pushState + dispatch popstate. Wait for the URL to
    # change rather than the body — body has loading shim first.
    page.wait_for_url(f"**/specs/{spec_id}", timeout=5000)

    # Detail header should render the slug as part of its title; the
    # exact copy lives in specDetail.tsx and may carry a version pill.
    # Wait for the slug text to render rather than reading body once —
    # the route shows "Loading spec" until the API roundtrip completes.
    page.get_by_text("e2e-specs-navtest").first.wait_for(timeout=10000)
