"""Integration coverage for the per-run read endpoints under ``/api/v1/runs``.

This module pairs with :mod:`tests.integration.api.test_api_health` and
exercises the *read* side of the runs router that lands in W5:

* ``GET /api/v1/runs/{id}`` — full :class:`RunDetail` payload (manifest,
  state tree, events count, journal, lock). The shape is contract for
  the UI's run-detail panel and for every third-party orchestrator that
  consumes the same JSON the W4 MCP ``fsm.get_run`` tool already emits.
* ``GET /api/v1/runs/{id}/state-tree`` — the nested
  :class:`StateNode` tree rooted at the entry state. Used by the UI's
  state-graph panel to render the linear/branching walk.
* ``GET /api/v1/runs/{id}/events`` — the run's event journal with
  cursor-style pagination via ``since_seq`` and an optional ``kinds``
  whitelist filter.

Why TestClient and not a real uvicorn process?
----------------------------------------------

All three endpoints are plain JSON reads (no SSE), so
:class:`fastapi.testclient.TestClient` is the correct tool: it drives
the ASGI app in-process, honours the FastAPI lifespan handler when
used as a context manager, and resolves every ``Depends`` chain
exactly the way uvicorn would. Spawning a real server here would
quadruple the per-test latency without adding any coverage that
TestClient does not already provide.

Why pre-bind the project via ``_state.set_project``?
----------------------------------------------------

The lifespan handler in :mod:`ctxr.fsm.api` has two branches:

1. *Pre-bound* — a caller (the ``ctxr-fsm api`` entry point or a test
   fixture) has already opened a :class:`Project` and bound it via
   :func:`_state.set_project`. The lifespan does nothing on entry and
   leaves the handle alone on exit.
2. *Self-opening* — no project is bound at startup, so the lifespan
   opens one against the resolved default path.

The production boot sequence takes branch (1); tests staying on the
same branch catch regressions there that branch (2) would silently
hide. Each test gets its own :class:`tempfile.TemporaryDirectory` so
the SQLite file cannot collide across tests, and the fixture's
``finally`` block calls :func:`_state.reset_project` so the
module-global does not leak between tests.

Why seed a run inside the test fixture rather than via the API itself?
---------------------------------------------------------------------

W5 does not yet expose write endpoints for the run lifecycle (those
land in W6+); the substrate-level :class:`Project` facade is the only
way to materialise a run today. Driving the substrate directly mirrors
how :mod:`tests.integration.sqlite.test_full_run_lifecycle` builds its
fixtures and keeps these tests focused on the HTTP surface rather than
on a bootstrap dance through a hypothetical future write API.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ctxr.fsm.api import _state, app
from ctxr.fsm.core.models import (
    EventKind,
    FsmSpec,
    State,
    Transition,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import RunTable

# ---------------------------------------------------------------------------
# Fixture spec — two linear states so the state tree has a parent + child
# ---------------------------------------------------------------------------


# A deliberately tiny linear FSM:
#
#   state_a (entry, ``always`` → state_b)
#     → state_b (no transitions == terminal)
#
# Two states is the minimum that lets us assert the state tree has a
# root *and* a child (a single-state spec would only ever produce a
# leaf). ``always`` guards keep the predicate evaluator out of the
# picture — this module tests the HTTP read surface, not transition
# semantics, so we drive the substrate directly using the same shape a
# real engine would produce.
def _build_spec() -> FsmSpec:
    """Build the two-state linear spec used by every test in this module.

    Returned fresh on each call so a test that mutates the value
    object (none currently do, but the contract is cheap) cannot poison
    its neighbours. Equivalent to the in-line construction in
    :mod:`tests.integration.sqlite.test_full_run_lifecycle` collapsed
    onto two states.
    """
    return FsmSpec(
        id="api_runs_demo",
        version=1,
        entry="state_a",
        states=[
            State(
                id="state_a",
                purpose="entry state for the api-runs read tests",
                transitions=[Transition(to="state_b", when="always")],
            ),
            State(
                id="state_b",
                purpose="terminal state for the api-runs read tests",
                transitions=[],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def project_and_client() -> Iterator[tuple[Project, TestClient, str]]:
    """Yield ``(Project, TestClient, run_id)`` with a fully-walked run seeded.

    Setup steps:

    1. Allocate a per-test :class:`tempfile.TemporaryDirectory` so the
       SQLite file is isolated from other tests and from leftover
       state from prior runs.
    2. Open the :class:`Project` with ``migrate=True`` so the Alembic
       head is applied — the same path the production
       :func:`ctxr.fsm.api.server.main` boot sequence takes.
    3. Register the two-state fixture spec, start a run, and walk it
       to completion through the same sub-repo calls a real engine
       would invoke. The result is one run that has:

       * A ``completed`` manifest with ``current_state='state_b'`` and
         a verdict.
       * A state-entry tree of shape ``state_a → state_b``, each entry
         carrying realistic ``inputs`` / ``outputs``.
       * Five events at minimum: ``run_started`` (from
         :meth:`Project.start_run`) plus the canonical
         ``state_entered`` / ``state_exited`` / ``transition_taken`` /
         ``run_completed`` set the driver records.

    4. Bind the project as the module-global handle via
       :func:`_state.set_project` *before* constructing
       :class:`TestClient` — this drives the lifespan handler's
       "already open" branch, the canonical production path.
    5. Enter the :class:`TestClient` as a context manager so the
       FastAPI lifespan startup hook actually fires before any test
       body issues a request.

    Teardown unwinds in strict reverse order so the module-global is
    cleared before the underlying :class:`Project` is closed —
    otherwise an in-flight handler that introspects
    :func:`_state.is_open` could observe a Project whose engine has
    already been disposed and emit a confusing traceback in the test
    log.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        project = Project.open(db_path, migrate=True)
        try:
            run_id = _seed_completed_two_state_run(project)
            _state.set_project(project)
            try:
                # ``with TestClient(app) as client`` is the documented
                # FastAPI test idiom — without the ``with``, the
                # lifespan hook never fires. The hook is a no-op in
                # the pre-bound branch (which is what we want here) but
                # we still go through it so the test exercises the
                # exact code path the production boot uses.
                with TestClient(app) as client:
                    yield project, client, run_id
            finally:
                # Order matters: clear the module-global first so any
                # straggler request introspecting the handle sees
                # ``is_open() == False`` rather than a closed Project.
                _state.reset_project()
        finally:
            project.close()


# ---------------------------------------------------------------------------
# Helpers — substrate seeding
# ---------------------------------------------------------------------------


def _seed_completed_two_state_run(project: Project) -> str:
    """Register the fixture spec and walk a run through ``state_a → state_b``.

    Returns the seeded run's id so each test can plug it straight into
    the URL path. The walk is intentionally the *same* sequence of
    sub-repo calls that
    :mod:`tests.integration.sqlite.test_full_run_lifecycle` performs —
    keeping the two test modules in sync about what "a real run looks
    like" prevents drift between the substrate-level integration test
    and the HTTP-layer integration test.

    The driver mimics a real engine for each state:

    * Open a journal txn (so the journal exercises ``ready`` →
      ``finalised`` and the run ends with a clean journal).
    * Allocate the next ``entry_seq`` and insert the state-entry row,
      emit ``state_entered``.
    * Update ``runs.current_state`` to mirror the advance on the
      manifest (no dedicated repo method for this today; we mutate
      the column directly inside a ``Session.begin()`` block, same as
      the lifecycle test).
    * For non-terminal states, record the outbound transition and
      emit ``transition_taken``.
    * Mark the state exited with realistic ``outputs`` and emit
      ``state_exited``.
    * Finalise the journal txn so the run ends quiescent
      (``journal is None`` in the detail payload).

    After both states are walked, mark the run ``completed`` and emit
    ``run_completed`` so the manifest carries the terminal status the
    UI's run-list filter keys off.
    """
    # ── Register the spec ──────────────────────────────────────────────
    registered = project.register_spec(_build_spec())
    spec = registered.spec

    # ── Start the run ──────────────────────────────────────────────────
    run = project.start_run(spec.id, args={"seed": "api-runs-demo"})
    run_id = run.id

    # Producer id needed for every event the driver emits. Upsert is
    # idempotent — ``Project.start_run`` has already created the same
    # row, so this call is effectively a read.
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="fsm.runtime",
        )
        producer_id = producer.id

    walk: list[tuple[str, dict[str, Any]]] = [
        ("state_a", {"hello": "world"}),
        ("state_b", {"verdict": "ok"}),
    ]
    state_pks: dict[str, str] = {}

    for index, (state_name, outputs) in enumerate(walk):
        # 1. Open a journal txn for this state's pre-commit ledger.
        with project.session_factory() as session, session.begin():
            txn = project.journal.open(session, run_id=run_id)
            txn_id = txn.id

        # 2. Insert the state-entry row and emit ``state_entered``.
        with project.session_factory() as session, session.begin():
            next_seq = project.states.next_entry_seq(session, run_id)
            state_row = project.states.create(
                session,
                run_id=run_id,
                state_id=state_name,
                inputs={"step": index},
                entry_seq=next_seq,
            )
            state_pks[state_name] = state_row.id
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.state_entered.value,
                payload={
                    "run_id": run_id,
                    "state": state_name,
                    "entry_seq": state_row.entry_seq,
                },
                run_id=run_id,
            )

        # 3. Mirror the advance on the manifest. The W2 RunsRepo
        #    exposes ``update_status`` but no dedicated
        #    ``set_current_state`` method — that is engine-layer
        #    policy. We mutate the column directly inside a
        #    ``Session.begin()`` block, same shape as the lifecycle
        #    test does.
        with project.session_factory() as session, session.begin():
            row = session.get(RunTable, run_id)
            assert row is not None, f"run {run_id!r} disappeared mid-seed"
            row.current_state = state_name

        # 4. For non-terminal states, record the outbound transition
        #    and emit ``transition_taken``.
        if index < len(walk) - 1:
            next_state_name = walk[index + 1][0]
            with project.session_factory() as session, session.begin():
                project.transitions.create(
                    session,
                    run_id=run_id,
                    from_state_pk=state_row.id,
                    to_state_id=next_state_name,
                    kind="always",
                    predicate=None,
                    predicate_result=None,
                )
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.transition_taken.value,
                    payload={
                        "run_id": run_id,
                        "from": state_name,
                        "to": next_state_name,
                    },
                    run_id=run_id,
                )

        # 5. Mark the state exited and emit ``state_exited``.
        with project.session_factory() as session, session.begin():
            project.states.mark_exited(
                session, state_pks[state_name], outputs=outputs
            )
            project.events.emit(
                session,
                producer_id=producer_id,
                kind=EventKind.state_exited.value,
                payload={
                    "run_id": run_id,
                    "state": state_name,
                },
                run_id=run_id,
            )

        # 6. Mark + finalise the journal txn so the run ends with no
        #    pending journal (``journal is None`` in the API payload).
        with project.session_factory() as session, session.begin():
            project.journal.mark_ready(
                session,
                txn_id=txn_id,
                staged_writes=[{"state": state_name}],
            )
            project.journal.finalise(session, txn_id=txn_id)

    # ── Mark the run completed and emit the matching event. ──────────
    with project.session_factory() as session, session.begin():
        completed = project.runs.update_status(
            session,
            run_id=run_id,
            status="completed",
            ended_at=None,  # the repo stamps it via ``last_update_at``
            verdict="ok",
        )
        assert completed is not None, "update_status returned None mid-seed"
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.run_completed.value,
            payload={"run_id": run_id, "verdict": "ok"},
            run_id=run_id,
        )

    return run_id


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/runs/{id}
# ---------------------------------------------------------------------------


def test_get_run_returns_full_run_detail(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """``GET /api/v1/runs/{id}`` returns the full :class:`RunDetail` payload.

    The response must carry every field the W4 ``fsm.get_run`` MCP
    tool returns so HTTP and MCP clients consume an identical shape:

    * ``manifest`` — the :class:`Run` value object dumped as a JSON
      dict, with ``status='completed'``, ``current_state='state_b'``,
      and ``verdict='ok'`` reflecting the seeded walk.
    * ``state_tree`` — the nested :class:`StateNode` rooted at the
      entry state. Asserted in detail by the dedicated state-tree
      test below; here we only confirm it is present and rooted
      correctly.
    * ``events_count`` — total events recorded against the run. We
      assert the lower bound rather than an exact count so the test
      is robust to future engine-side instrumentation adding new
      event kinds.
    * ``journal`` — ``None`` because every txn the seed driver
      opened was finalised.
    * ``lock`` — ``None`` because the seed driver never acquired one.
    """
    _, client, run_id = project_and_client

    response = client.get(f"/api/v1/runs/{run_id}")

    assert response.status_code == 200, (
        f"expected 200 from /api/v1/runs/{run_id}, got "
        f"{response.status_code}; body={response.text!r}"
    )
    body = response.json()

    # Top-level fields — assert each one explicitly so a future
    # accidental rename of a RunDetail field is caught at the wire
    # boundary rather than landing as a silent UI regression.
    assert set(body.keys()) >= {
        "manifest",
        "state_tree",
        "events_count",
        "journal",
        "lock",
    }, f"missing required RunDetail fields; body keys={sorted(body.keys())!r}"

    manifest = body["manifest"]
    assert manifest["id"] == run_id, (
        f"manifest carries the wrong run id: {manifest['id']!r}"
    )
    assert manifest["status"] == "completed", (
        f"expected completed run, got status={manifest['status']!r}"
    )
    assert manifest["current_state"] == "state_b", (
        f"expected current_state=state_b, got {manifest['current_state']!r}"
    )
    assert manifest["verdict"] == "ok", (
        f"expected verdict=ok, got {manifest['verdict']!r}"
    )

    # State tree present and rooted at the entry state. Deep-shape
    # assertions live in the dedicated state-tree test below.
    tree = body["state_tree"]
    assert tree is not None, "state_tree must be present once the run is committed"
    assert tree["state_id"] == "state_a", (
        f"state tree must be rooted at the entry state, got {tree['state_id']!r}"
    )

    # Events count — five canonical kinds at minimum (run_started +
    # two state_entered + two state_exited + transition_taken +
    # run_completed = 7). Asserting >= keeps the test robust to
    # future engine instrumentation that adds extra event kinds.
    assert isinstance(body["events_count"], int)
    assert body["events_count"] >= 7, (
        f"expected at least 7 events on the seeded run, got "
        f"{body['events_count']}"
    )

    # Journal and lock are both ``None`` on a quiescent completed run.
    assert body["journal"] is None, (
        f"journal must be cleared on a quiescent run, got {body['journal']!r}"
    )
    assert body["lock"] is None, (
        f"lock must be absent — the seed driver never acquired one; "
        f"got {body['lock']!r}"
    )


def test_get_run_unknown_id_returns_404(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """An unknown run id surfaces as 404 with a JSON ``detail`` body.

    The 404 contract is part of the public API: the UI uses it to
    distinguish "stale link" from "API outage" without parsing the
    response text. We assert on the status code and the presence of a
    ``detail`` field; we deliberately do not pin the message string so
    future copy-edits do not break the test.
    """
    _, client, _ = project_and_client

    response = client.get("/api/v1/runs/00000000-0000-7000-8000-000000000000")

    assert response.status_code == 404, (
        f"expected 404 for unknown run, got {response.status_code}; "
        f"body={response.text!r}"
    )
    body = response.json()
    assert "detail" in body, (
        f"404 response must carry a ``detail`` field; body={body!r}"
    )


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/runs/{id}/state-tree
# ---------------------------------------------------------------------------


def test_get_state_tree_returns_nested_walk(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """``GET /api/v1/runs/{id}/state-tree`` returns the seeded walk's tree.

    The seeded run walks ``state_a → state_b`` linearly, so the tree
    must be a two-node chain:

    * Root ``state_a``, ``entry_seq=1``, ``status='exited'``, outputs
      ``{"hello": "world"}``, exactly one child.
    * Child ``state_b``, ``entry_seq=2``, ``status='exited'``, outputs
      ``{"verdict": "ok"}``, no children.

    Asserting the shape end-to-end rather than just the root id
    catches regressions in :meth:`RunsRepo.state_tree` that would
    otherwise only surface in the (more expensive) end-to-end MCP
    test.
    """
    _, client, run_id = project_and_client

    response = client.get(f"/api/v1/runs/{run_id}/state-tree")

    assert response.status_code == 200, (
        f"expected 200 from /api/v1/runs/{run_id}/state-tree, got "
        f"{response.status_code}; body={response.text!r}"
    )
    tree = response.json()

    # Root node — must be the entry state with the expected outputs.
    assert tree["state_id"] == "state_a", (
        f"tree must be rooted at the entry state, got {tree['state_id']!r}"
    )
    assert tree["entry_seq"] == 1, (
        f"root entry_seq must be 1, got {tree['entry_seq']!r}"
    )
    assert tree["status"] == "exited", (
        f"root status must be ``exited`` on a completed run, got "
        f"{tree['status']!r}"
    )
    assert tree["outputs"] == {"hello": "world"}, (
        f"root outputs mismatch: {tree['outputs']!r}"
    )

    # Exactly one child — the terminal state.
    assert len(tree["children"]) == 1, (
        f"expected a single child on state_a, got {tree['children']!r}"
    )
    child = tree["children"][0]
    assert child["state_id"] == "state_b", (
        f"child must be state_b, got {child['state_id']!r}"
    )
    assert child["entry_seq"] == 2, (
        f"child entry_seq must be 2, got {child['entry_seq']!r}"
    )
    assert child["status"] == "exited", (
        f"child status must be ``exited``, got {child['status']!r}"
    )
    assert child["outputs"] == {"verdict": "ok"}, (
        f"child outputs mismatch: {child['outputs']!r}"
    )
    assert child["children"] == [], (
        f"state_b is terminal — expected no grandchildren, got "
        f"{child['children']!r}"
    )


def test_get_state_tree_unknown_run_returns_404(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """An unknown run id on the state-tree endpoint returns 404.

    The route collapses "unknown run" and "known run with no entries"
    into a single 404 — see the route's docstring. We can only
    exercise the "unknown" branch here because every seeded run has
    entries; the collapse itself is asserted by the route's own
    docstring contract.
    """
    _, client, _ = project_and_client

    response = client.get(
        "/api/v1/runs/00000000-0000-7000-8000-000000000000/state-tree"
    )

    assert response.status_code == 404, (
        f"expected 404 for unknown run state-tree, got "
        f"{response.status_code}; body={response.text!r}"
    )


# ---------------------------------------------------------------------------
# Tests — GET /api/v1/runs/{id}/events
# ---------------------------------------------------------------------------


def test_get_events_returns_journal_in_seq_order(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """``GET /api/v1/runs/{id}/events`` returns events ordered by ``seq``.

    The route returns a JSON array of :class:`Event` objects ordered
    by ``seq`` ascending — the cursor contract is "the next call
    passes the last ``seq`` you received as ``since_seq``", which only
    works if the order is monotonic. We assert:

    * The response is a non-empty array (the seed driver emitted at
      least the canonical lifecycle events).
    * Every required event kind from the W2 lifecycle contract is
      present.
    * ``seq`` values are strictly monotonic increasing starting at 1
      — the bus's documented invariant.
    * Every event carries the seeded ``run_id`` (no cross-run leakage
      through the filter).
    """
    _, client, run_id = project_and_client

    response = client.get(f"/api/v1/runs/{run_id}/events")

    assert response.status_code == 200, (
        f"expected 200 from /api/v1/runs/{run_id}/events, got "
        f"{response.status_code}; body={response.text!r}"
    )
    events = response.json()

    assert isinstance(events, list), (
        f"events endpoint must return a JSON array, got {type(events).__name__}"
    )
    assert len(events) > 0, "seeded run has events; got an empty list"

    # Every event must belong to the run we asked about.
    for event in events:
        assert event["run_id"] == run_id, (
            f"event leaked from another run: {event!r}"
        )

    kinds = [event["kind"] for event in events]
    required_kinds = {
        EventKind.run_started.value,
        EventKind.state_entered.value,
        EventKind.state_exited.value,
        EventKind.transition_taken.value,
        EventKind.run_completed.value,
    }
    assert required_kinds.issubset(set(kinds)), (
        f"missing required event kinds: "
        f"{sorted(required_kinds - set(kinds))!r}; observed={kinds!r}"
    )

    # Per-run seq must be strictly monotonic starting at 1 — the bus's
    # documented invariant. Asserting on the full sequence (rather
    # than just "increasing") catches off-by-one regressions that
    # would otherwise only show up at the cursor boundary.
    seqs = [event["seq"] for event in events]
    assert seqs == list(range(1, len(events) + 1)), (
        f"event seq sequence is not contiguous starting at 1: {seqs!r}"
    )


def test_get_events_supports_since_seq_cursor(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """``?since_seq=N`` returns only events with ``seq > N``.

    Exercises the cursor contract: a caller passes the last ``seq``
    it received and gets back everything strictly after it. We pull
    the full journal first, pick the first event's ``seq`` as the
    cursor, then assert the second call returns the remainder
    starting at ``cursor + 1``.
    """
    _, client, run_id = project_and_client

    full = client.get(f"/api/v1/runs/{run_id}/events").json()
    assert len(full) >= 2, (
        f"need at least 2 events to test the cursor; got {len(full)}"
    )

    cursor = full[0]["seq"]
    cursored = client.get(
        f"/api/v1/runs/{run_id}/events", params={"since_seq": cursor}
    )

    assert cursored.status_code == 200, (
        f"expected 200 with since_seq, got {cursored.status_code}; "
        f"body={cursored.text!r}"
    )
    rows = cursored.json()
    # All returned rows must have seq strictly greater than the cursor.
    assert all(row["seq"] > cursor for row in rows), (
        f"since_seq={cursor} returned a row with seq<={cursor}: {rows!r}"
    )
    # And the count must drop by exactly one — we pinned the cursor at
    # the very first event.
    assert len(rows) == len(full) - 1, (
        f"expected len(full) - 1 = {len(full) - 1} cursored rows, got "
        f"{len(rows)} (full={len(full)})"
    )


def test_get_events_supports_kinds_whitelist(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """``?kinds=...`` whitelists the returned event kinds.

    The repo applies the filter at the SQL level; we assert the wire
    surface honours it by asking for a single kind
    (``state_entered``) and confirming every returned row carries
    that kind. The seed driver emits two ``state_entered`` events
    (one per state) so we also pin the count.
    """
    _, client, run_id = project_and_client

    response = client.get(
        f"/api/v1/runs/{run_id}/events",
        params={"kinds": EventKind.state_entered.value},
    )

    assert response.status_code == 200, (
        f"expected 200 with kinds filter, got {response.status_code}; "
        f"body={response.text!r}"
    )
    rows = response.json()
    assert len(rows) == 2, (
        f"seeded run has two state_entered events; got {len(rows)}: {rows!r}"
    )
    for row in rows:
        assert row["kind"] == EventKind.state_entered.value, (
            f"kinds filter leaked a non-matching event: {row!r}"
        )


def test_get_events_unknown_run_returns_404(
    project_and_client: tuple[Project, TestClient, str],
) -> None:
    """An unknown run id on the events endpoint returns 404.

    The route distinguishes "unknown run" (404) from "run with no
    events yet" (200 + empty list); we exercise the former here.
    """
    _, client, _ = project_and_client

    response = client.get(
        "/api/v1/runs/00000000-0000-7000-8000-000000000000/events"
    )

    assert response.status_code == 404, (
        f"expected 404 for unknown run events, got {response.status_code}; "
        f"body={response.text!r}"
    )


if __name__ == "__main__":  # pragma: no cover - manual debug path
    # Allow ``python tests/integration/api/test_api_run_detail.py`` to
    # run the suite under pytest without remembering the full module
    # path. Handy when iterating on the test body locally.
    raise SystemExit(pytest.main([__file__, "-v"]))
