"""Unit tests for the SQLite query helpers in :mod:`ctxr.fsm.sqlite`.

Coverage focus
--------------
* :meth:`RunsRepo.latest` / :meth:`incomplete` / :meth:`resumable` /
  :meth:`by_status` / :meth:`by_session` / :meth:`by_project` return
  typed :class:`RunSummary` lists with the documented filters.
* :meth:`RunsRepo.state_tree` reconstructs the nested
  :class:`StateNode` tree from real ``states`` + ``transitions`` rows.
* :meth:`TransitionsRepo.by_status` honours the tri-state
  ``predicate_result`` filter (``True`` / ``False`` / ``None``).
* :meth:`EventsRepo.by_producer` returns events ordered by
  ``created_at DESC``.

The tests own one fresh on-disk SQLite database per test (via
``tempfile.TemporaryDirectory``) so there is no cross-test leakage.
We exercise the public ``Project`` facade plus the typed sub-repos
re-exported from :mod:`ctxr.fsm.sqlite` — no internal modules are
imported.
"""

from __future__ import annotations

import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from ctxr.fsm.sqlite import (
    Project,
    RunSummary,
    TransitionRecord,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a freshly-migrated ``Project`` bound to a tempdir-scoped DB.

    Each test gets its own database file so query results never bleed
    between tests. ``Project.open(migrate=True)`` runs ``alembic upgrade
    head`` against the brand-new file before yielding.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.sqlite"
        with Project.open(db_path) as proj:
            yield proj


def _create_project_row(project: Project, slug: str | None = None) -> str:
    """Insert a project row and return its id.

    Used by tests that need a real ``project_id`` to anchor runs against
    without going through ``register_spec`` (which we do not want to
    couple every test to).
    """
    slug = slug or f"proj-{uuid.uuid4().hex[:8]}"
    with project.session_factory() as session, session.begin():
        row = project.projects.create(session, slug=slug)
        return row.id


def _create_spec_row(project: Project, project_id: str) -> str:
    """Insert a synthetic ``fsm_specs`` row and return its id.

    We bypass :meth:`Project.register_spec` here because tests in this
    module do not exercise spec-registration semantics — they only need
    a FK target so the ``runs.fsm_spec_id`` column is satisfiable.
    """
    from ctxr.fsm.sqlite.models_core import FsmSpecTable

    spec_id = str(uuid.uuid4())
    with project.session_factory() as session, session.begin():
        row = FsmSpecTable(
            id=spec_id,
            project_id=project_id,
            slug="test-spec",
            version=1,
            hash="0" * 64,
            definition_json="{}",
            created_at="2026-01-01T00:00:00.000+00:00",
        )
        session.add(row)
    return spec_id


def _create_run(
    project: Project,
    project_id: str,
    spec_id: str,
    status: str = "in_progress",
) -> str:
    """Insert a run row directly via the repo and return its id.

    We use :meth:`RunsRepo.create` followed by an optional
    :meth:`update_status` rather than ``Project.start_run`` because
    ``start_run`` also emits an event and requires a valid registered
    spec — overkill for the query tests below.
    """
    with project.session_factory() as session, session.begin():
        run = project.runs.create(
            session,
            project_id=project_id,
            spec_id=spec_id,
            args={},
            fsm_spec_hash="0" * 64,
        )
        if status != "in_progress":
            project.runs.update_status(session, run.id, status=status)
        return run.id


# ---------------------------------------------------------------------------
# RunsRepo.latest / incomplete / resumable / by_status / by_project
# ---------------------------------------------------------------------------


def test_latest_returns_run_summaries_newest_first(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)

    first_id = _create_run(project, project_id, spec_id)
    # Sleep a touch so ``last_update_at`` (ms-precision ISO strings)
    # differs lexicographically between rows.
    time.sleep(0.01)
    second_id = _create_run(project, project_id, spec_id)
    time.sleep(0.01)
    third_id = _create_run(project, project_id, spec_id)

    with project.session_factory() as session:
        result = project.runs.latest(session, limit=10)

    assert isinstance(result, list)
    assert all(isinstance(r, RunSummary) for r in result)
    # Newest first by last_update_at DESC.
    assert [r.id for r in result] == [third_id, second_id, first_id]


def test_latest_respects_limit(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)

    for _ in range(5):
        _create_run(project, project_id, spec_id)
        time.sleep(0.005)

    with project.session_factory() as session:
        result = project.runs.latest(session, limit=2)

    assert len(result) == 2


def test_incomplete_excludes_terminal_statuses(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)

    in_progress_id = _create_run(project, project_id, spec_id, status="in_progress")
    paused_id = _create_run(project, project_id, spec_id, status="paused")
    faulted_id = _create_run(project, project_id, spec_id, status="faulted")
    drift_paused_id = _create_run(project, project_id, spec_id, status="drift_paused")
    # Terminal statuses — must NOT appear in `incomplete()`.
    _create_run(project, project_id, spec_id, status="completed")
    _create_run(project, project_id, spec_id, status="aborted")
    _create_run(project, project_id, spec_id, status="superseded")

    with project.session_factory() as session:
        result = project.runs.incomplete(session)

    assert isinstance(result, list)
    assert all(isinstance(r, RunSummary) for r in result)
    ids = {r.id for r in result}
    assert ids == {in_progress_id, paused_id, faulted_id, drift_paused_id}


def test_resumable_is_strict_subset_of_incomplete(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)

    # in_progress is incomplete BUT NOT resumable.
    _create_run(project, project_id, spec_id, status="in_progress")
    paused_id = _create_run(project, project_id, spec_id, status="paused")
    faulted_id = _create_run(project, project_id, spec_id, status="faulted")
    drift_paused_id = _create_run(project, project_id, spec_id, status="drift_paused")
    _create_run(project, project_id, spec_id, status="completed")

    with project.session_factory() as session:
        result = project.runs.resumable(session)

    assert isinstance(result, list)
    assert all(isinstance(r, RunSummary) for r in result)
    ids = {r.id for r in result}
    assert ids == {paused_id, faulted_id, drift_paused_id}


def test_by_status_filters_by_exact_status(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)

    completed_a = _create_run(project, project_id, spec_id, status="completed")
    time.sleep(0.01)
    completed_b = _create_run(project, project_id, spec_id, status="completed")
    _create_run(project, project_id, spec_id, status="in_progress")
    _create_run(project, project_id, spec_id, status="faulted")

    with project.session_factory() as session:
        completed = project.runs.by_status(session, "completed")
        faulted = project.runs.by_status(session, "faulted")
        empty = project.runs.by_status(session, "no_such_status")

    assert isinstance(completed, list)
    assert all(isinstance(r, RunSummary) for r in completed)
    assert {r.id for r in completed} == {completed_a, completed_b}
    # Freshest first.
    assert completed[0].id == completed_b

    assert len(faulted) == 1
    assert empty == []


def test_by_project_scopes_to_one_project(project: Project) -> None:
    project_a = _create_project_row(project, slug="proj-a")
    project_b = _create_project_row(project, slug="proj-b")
    spec_a = _create_spec_row(project, project_a)
    spec_b = _create_spec_row(project, project_b)

    a1 = _create_run(project, project_a, spec_a)
    time.sleep(0.01)
    a2 = _create_run(project, project_a, spec_a)
    b1 = _create_run(project, project_b, spec_b)

    with project.session_factory() as session:
        runs_for_a = project.runs.by_project(session, project_a)
        runs_for_b = project.runs.by_project(session, project_b)

    assert isinstance(runs_for_a, list)
    assert all(isinstance(r, RunSummary) for r in runs_for_a)
    assert {r.id for r in runs_for_a} == {a1, a2}
    # Freshest first.
    assert runs_for_a[0].id == a2
    assert [r.id for r in runs_for_b] == [b1]


def test_by_session_returns_distinct_runs(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)

    run_a = _create_run(project, project_id, spec_id)
    run_b = _create_run(project, project_id, spec_id)
    # A third run that is NEVER bound to our session_id — must not appear.
    _create_run(project, project_id, spec_id)

    session_id = "worker-7"
    other_session_id = "worker-other"

    with project.session_factory() as session, session.begin():
        # Bind both runs to the same session id; bind run_a twice to
        # exercise the DISTINCT collapsing.
        project.run_sessions.open(session, run_id=run_a, session_id=session_id)
        project.run_sessions.open(session, run_id=run_a, session_id=session_id)
        project.run_sessions.open(session, run_id=run_b, session_id=session_id)
        # An unrelated binding that must NOT pull a run into this view.
        project.run_sessions.open(session, run_id=run_a, session_id=other_session_id)

    with project.session_factory() as session:
        result = project.runs.by_session(session, session_id=session_id)
        unrelated = project.runs.by_session(session, session_id="never-bound")

    assert isinstance(result, list)
    assert all(isinstance(r, RunSummary) for r in result)
    # DISTINCT: run_a appears once even though it has 2 bindings to this
    # session_id.
    ids = [r.id for r in result]
    assert sorted(ids) == sorted([run_a, run_b])
    assert len(ids) == 2
    assert unrelated == []


# ---------------------------------------------------------------------------
# RunsRepo.state_tree
# ---------------------------------------------------------------------------


def test_state_tree_returns_none_for_run_without_entries(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)
    run_id = _create_run(project, project_id, spec_id)

    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)

    assert tree is None


def test_state_tree_reconstructs_linear_chain(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)
    run_id = _create_run(project, project_id, spec_id)

    # Build draft -> review -> publish via real states + transitions.
    with project.session_factory() as session, session.begin():
        draft = project.states.create(
            session, run_id=run_id, state_id="draft", inputs={}, entry_seq=1
        )
        review = project.states.create(
            session, run_id=run_id, state_id="review", inputs={}, entry_seq=2
        )
        publish = project.states.create(
            session, run_id=run_id, state_id="publish", inputs={}, entry_seq=3
        )
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=draft.id,
            to_state_id="review",
            kind="always",
            predicate=None,
            predicate_result=None,
        )
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=review.id,
            to_state_id="publish",
            kind="deterministic",
            predicate="approved == true",
            predicate_result=True,
        )

    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)

    # ``RunsRepo.state_tree`` returns a ``repos_core.StateNode`` (a
    # different class than the ``repos_states.StateNode`` re-exported as
    # the public ``StateNode``) so we check the value-object shape
    # (entry_id / state_id / children) rather than ``isinstance``.
    assert tree is not None
    assert tree.state_id == "draft"
    assert tree.entry_id == draft.id
    assert tree.entry_seq == 1
    assert len(tree.children) == 1

    review_node = tree.children[0]
    assert review_node.state_id == "review"
    assert review_node.entry_id == review.id
    assert len(review_node.children) == 1

    publish_node = review_node.children[0]
    assert publish_node.state_id == "publish"
    assert publish_node.entry_id == publish.id
    assert publish_node.children == []


def test_state_tree_handles_branching(project: Project) -> None:
    """A state with two outbound transitions yields two children."""
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)
    run_id = _create_run(project, project_id, spec_id)

    with project.session_factory() as session, session.begin():
        root = project.states.create(
            session, run_id=run_id, state_id="root", inputs={}, entry_seq=1
        )
        left = project.states.create(
            session, run_id=run_id, state_id="left", inputs={}, entry_seq=2
        )
        right = project.states.create(
            session, run_id=run_id, state_id="right", inputs={}, entry_seq=3
        )
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=root.id,
            to_state_id="left",
            kind="deterministic",
            predicate="x > 0",
            predicate_result=True,
        )
        # Tiny delay to ensure decided_at differs.
        time.sleep(0.005)
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=root.id,
            to_state_id="right",
            kind="deterministic",
            predicate="x < 0",
            predicate_result=True,
        )

    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)

    assert tree is not None
    assert tree.state_id == "root"
    child_ids = [c.entry_id for c in tree.children]
    assert child_ids == [left.id, right.id]


# ---------------------------------------------------------------------------
# TransitionsRepo.by_status
# ---------------------------------------------------------------------------


def test_transitions_by_status_filters_tri_state(project: Project) -> None:
    project_id = _create_project_row(project)
    spec_id = _create_spec_row(project, project_id)
    run_id = _create_run(project, project_id, spec_id)

    with project.session_factory() as session, session.begin():
        source = project.states.create(
            session, run_id=run_id, state_id="source", inputs={}, entry_seq=1
        )
        # one transition per predicate_result bucket
        fired = project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=source.id,
            to_state_id="next",
            kind="deterministic",
            predicate="ok == true",
            predicate_result=True,
        )
        time.sleep(0.005)
        rejected = project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=source.id,
            to_state_id="next",
            kind="deterministic",
            predicate="bad == true",
            predicate_result=False,
        )
        time.sleep(0.005)
        always = project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=source.id,
            to_state_id="next",
            kind="always",
            predicate=None,
            predicate_result=None,
        )

    with project.session_factory() as session:
        fired_rows = project.transitions.by_status(session, predicate_result=True)
        rejected_rows = project.transitions.by_status(session, predicate_result=False)
        always_rows = project.transitions.by_status(session, predicate_result=None)

    assert all(isinstance(t, TransitionRecord) for t in fired_rows)
    assert [t.id for t in fired_rows] == [fired.id]
    assert all(t.predicate_result is True for t in fired_rows)

    assert [t.id for t in rejected_rows] == [rejected.id]
    assert all(t.predicate_result is False for t in rejected_rows)

    assert [t.id for t in always_rows] == [always.id]
    assert all(t.predicate_result is None for t in always_rows)


# ---------------------------------------------------------------------------
# EventsRepo.by_producer
# ---------------------------------------------------------------------------


def test_events_by_producer_ordered_desc(project: Project) -> None:
    """Events for one producer come back newest first by ``created_at``.

    We also assert that events from a *different* producer do not bleed
    into the result — the filter is exact on ``producer_id``.
    """
    with project.session_factory() as session, session.begin():
        producer_a = project.producers.upsert(session, kind="engine", name="prod-a")
        producer_b = project.producers.upsert(session, kind="engine", name="prod-b")

        e1 = project.events.emit(
            session,
            producer_id=producer_a.id,
            kind="run_started",
            payload={"i": 1},
        )
    # Force created_at progression (ms precision) between emits.
    time.sleep(0.01)
    with project.session_factory() as session, session.begin():
        e2 = project.events.emit(
            session,
            producer_id=producer_a.id,
            kind="state_entered",
            payload={"i": 2},
        )
    time.sleep(0.01)
    with project.session_factory() as session, session.begin():
        e3 = project.events.emit(
            session,
            producer_id=producer_a.id,
            kind="state_exited",
            payload={"i": 3},
        )
        # An event from another producer that MUST NOT show up.
        project.events.emit(
            session,
            producer_id=producer_b.id,
            kind="run_started",
            payload={"i": 99},
        )

    with project.session_factory() as session:
        result = project.events.by_producer(session, producer_id=producer_a.id)

    assert isinstance(result, list)
    # ``EventsRepo.by_producer`` returns ``repos_events.Event`` (the
    # bus-side value object) — duck-typed shape check rather than
    # ``isinstance`` keeps the assertion stable if the public surface
    # rewires the re-export later.
    assert all(hasattr(ev, "id") and hasattr(ev, "producer_id") for ev in result)
    # Newest first: e3, e2, e1.
    assert [ev.id for ev in result] == [e3.id, e2.id, e1.id]
    # All belong to producer_a.
    assert all(ev.producer_id == producer_a.id for ev in result)


def test_events_by_producer_kinds_filter(project: Project) -> None:
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(session, kind="engine", name="prod")
        e1 = project.events.emit(
            session, producer_id=producer.id, kind="run_started", payload={}
        )
    time.sleep(0.01)
    with project.session_factory() as session, session.begin():
        project.events.emit(
            session, producer_id=producer.id, kind="state_entered", payload={}
        )
    time.sleep(0.01)
    with project.session_factory() as session, session.begin():
        e3 = project.events.emit(
            session, producer_id=producer.id, kind="run_completed", payload={}
        )

    with project.session_factory() as session:
        result = project.events.by_producer(
            session,
            producer_id=producer.id,
            kinds=["run_started", "run_completed"],
        )

    # DESC ordering preserved within the filtered subset.
    assert [ev.id for ev in result] == [e3.id, e1.id]
    assert {ev.kind for ev in result} == {"run_started", "run_completed"}
