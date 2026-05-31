"""Integration tests for ``GET /api/v1/runs`` — the run-listing endpoint.

These tests boot the FastAPI app against a per-test
:class:`Project` rooted in a :class:`tempfile.TemporaryDirectory` and
drive the HTTP surface through :class:`fastapi.testclient.TestClient`.
The TestClient handles ASGI lifespan transparently, threads our
``Project`` binding through ``Depends(get_project)`` without any extra
wiring, and avoids the operational overhead (spawning uvicorn,
allocating a free port, threading the server) that the SSE tests pay
for. Sync endpoints are exactly what TestClient is designed for.

The contracts asserted here mirror the spec / MCP coverage at the
HTTP layer:

* An empty database returns the JSON literal ``[]`` — not ``null``, not
  a wrapped envelope. The frontend's "did we get any runs?" check is a
  truthy length test on this array, so the empty case must be a JSON
  array.
* After we register a spec and start runs through the Python API
  (so this test isolates the *list* contract from the *start* contract),
  ``GET /runs`` surfaces every run with the expected ``fsm_spec_id``
  and ``id`` fields.
* ``?status=in_progress`` round-trips through the
  :meth:`RunsRepo.by_status` branch and only returns rows in that
  status — runs we transition to ``completed`` are excluded.

Setup discipline
----------------
Each test:

1. Opens a per-test :class:`tempfile.TemporaryDirectory` for the SQLite
   DB so cases stay hermetic and nothing leaks between them.
2. Builds the :class:`Project` *before* constructing the
   :class:`TestClient` and binds it via
   :func:`ctxr.fsm.api._state.set_project`. The lifespan handler sees a
   project is already bound (``_state.is_open()`` is ``True``) and
   leaves it alone — exactly the "caller owns the project" branch the
   handler documents.
3. Always calls :func:`ctxr.fsm.api._state.reset_project` and
   :meth:`Project.close` in a ``finally`` block so a failing assertion
   never leaves a stale global binding that would poison the next test
   in the file.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ctxr.fsm.api import _state, app
from ctxr.fsm.core.models import FsmSpec, State, Transition
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _linear_spec(spec_id: str = "api_runs_list_test") -> FsmSpec:
    """Build a minimal two-state FSM spec used by the list-endpoint tests.

    The spec is deliberately tiny — ``a`` (entry) → ``b`` (terminal) —
    because we only need *something* registered so :meth:`start_run`
    succeeds. The list endpoint doesn't care about state-machine shape;
    it just iterates the ``runs`` table.
    """
    return FsmSpec(
        id=spec_id,
        version=1,
        entry="a",
        states=[
            State(id="a", transitions=[Transition(to="b", when="always")]),
            State(id="b"),
        ],
    )


@contextmanager
def _bound_project(db_path: Path) -> Iterator[Project]:
    """Open a :class:`Project`, bind it for the API, and unbind on exit.

    The ``finally`` block ensures the process-wide binding is reset
    even when the body raises — a failing assertion in a test must
    never leave the next test inheriting a closed project handle that
    would surface as a confusing ``RuntimeError`` deep inside an
    unrelated route call.
    """
    project = Project.open(db_path, migrate=True)
    _state.set_project(project)
    try:
        yield project
    finally:
        _state.reset_project()
        project.close()


@pytest.fixture
def client_and_project() -> Iterator[tuple[TestClient, Project]]:
    """Yield a ``(TestClient, Project)`` pair sharing the same DB.

    The TestClient drives the lifespan handler on enter/exit so the
    full ASGI app is exercised end-to-end. Because we pre-bound the
    project via ``_state.set_project`` *before* the TestClient context
    opens, the lifespan handler hits the "project already bound" branch
    and leaves our handle alone — that's the canonical path the
    ``server.main`` entry point uses, and the one the API documents.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.db"
        with _bound_project(db_path) as project, TestClient(app) as client:
            yield client, project


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_list_runs_returns_empty_array_on_empty_db(
    client_and_project: tuple[TestClient, Project],
) -> None:
    """An empty database must serialise as the JSON literal ``[]``.

    A wrapped envelope (e.g. ``{"runs": []}``) or ``null`` would force
    every UI caller to type-check the response before iterating; the
    contract is "always an array", and an empty DB is the canonical
    boundary case for that contract.
    """
    client, _project = client_and_project

    response = client.get("/api/v1/runs")

    assert response.status_code == 200, (
        f"GET /api/v1/runs returned {response.status_code}: body={response.text!r}"
    )
    payload = response.json()
    assert payload == [], f"expected [] on empty DB, got {payload!r}"


def test_list_runs_returns_runs_after_python_api_start(
    client_and_project: tuple[TestClient, Project],
) -> None:
    """After ``Project.start_run`` seeds rows, the endpoint surfaces them.

    We register the spec and start two runs through the Python facade
    (not via any other HTTP endpoint or MCP tool) so this test is
    strictly about the list-endpoint behaviour. The two-run case
    catches accidental "first match wins" bugs that a single-run case
    would miss.
    """
    client, project = client_and_project

    registered = project.register_spec(_linear_spec())
    run_one = project.start_run(registered.spec.id, args={"first": True})
    run_two = project.start_run(registered.spec.id, args={"second": True})

    response = client.get("/api/v1/runs")

    assert response.status_code == 200, (
        f"GET /api/v1/runs returned {response.status_code}: body={response.text!r}"
    )
    payload = response.json()
    assert isinstance(payload, list)
    assert len(payload) == 2, f"expected exactly 2 runs, got {len(payload)}: {payload!r}"

    listed_ids = {row["id"] for row in payload}
    assert listed_ids == {run_one.id, run_two.id}, (
        f"expected run ids {{{run_one.id}, {run_two.id}}}, got {listed_ids}"
    )

    # Every row must carry the spec id we registered against; a missing
    # value would mean the summary projection dropped the column.
    for row in payload:
        assert row["fsm_spec_id"] == registered.spec.id, (
            f"row {row!r} has unexpected fsm_spec_id"
        )
        # Freshly-started runs land in ``in_progress`` — ``start_run``
        # does not advance the state machine, so the status never moves
        # past the initial value here.
        assert row["status"] == "in_progress", (
            f"row {row!r} has unexpected status (expected 'in_progress')"
        )


def test_list_runs_status_filter_in_progress(
    client_and_project: tuple[TestClient, Project],
) -> None:
    """``?status=in_progress`` returns only the rows in that status.

    We seed two runs and flip one to ``completed`` via the repo's
    ``update_status`` write so the filter has to *exclude* the
    completed row and *include* the in-progress one. Asserting both
    directions catches the "filter silently ignored" bug (everything
    passes) and the "filter rejects everything" bug (empty result) in
    the same test.
    """
    client, project = client_and_project

    registered = project.register_spec(_linear_spec())
    run_in_progress = project.start_run(registered.spec.id)
    run_to_complete = project.start_run(registered.spec.id)

    # Flip one run to ``completed`` directly through the repo — this
    # mirrors what a real engine would do at run end and gives us a
    # row the ``in_progress`` filter must exclude.
    with project.session_factory() as session, session.begin():
        updated = project.runs.update_status(
            session,
            run_id=run_to_complete.id,
            status="completed",
            ended_at=None,  # let the repo stamp it via last_update_at
            verdict="ok",
        )
        assert updated is not None, "update_status returned None for a known run id"

    # --- positive: the in_progress filter returns exactly the one row ---
    in_progress_response = client.get("/api/v1/runs", params={"status": "in_progress"})
    assert in_progress_response.status_code == 200, (
        f"GET /api/v1/runs?status=in_progress returned "
        f"{in_progress_response.status_code}: body={in_progress_response.text!r}"
    )
    in_progress_payload = in_progress_response.json()
    assert isinstance(in_progress_payload, list)
    assert len(in_progress_payload) == 1, (
        f"expected exactly 1 in-progress run, got {in_progress_payload!r}"
    )
    assert in_progress_payload[0]["id"] == run_in_progress.id
    assert in_progress_payload[0]["status"] == "in_progress"

    # --- negative: the completed filter returns exactly the other row ---
    # Confirms the filter is actually filtering rather than always
    # returning the same set regardless of the ``status`` value.
    completed_response = client.get("/api/v1/runs", params={"status": "completed"})
    assert completed_response.status_code == 200, (
        f"GET /api/v1/runs?status=completed returned "
        f"{completed_response.status_code}: body={completed_response.text!r}"
    )
    completed_payload = completed_response.json()
    assert isinstance(completed_payload, list)
    assert len(completed_payload) == 1, (
        f"expected exactly 1 completed run, got {completed_payload!r}"
    )
    assert completed_payload[0]["id"] == run_to_complete.id
    assert completed_payload[0]["status"] == "completed"


if __name__ == "__main__":
    # Manual smoke convenience — pytest remains the canonical entry point.
    raise SystemExit(pytest.main([__file__, "-v"]))
