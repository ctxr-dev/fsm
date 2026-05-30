"""Unit tests for :class:`JournalRepo` and the journal-txn lifecycle.

Covers:

* :meth:`JournalRepo.open` / :meth:`mark_ready` / :meth:`finalise` move a
  row through the three-step lifecycle, populating the matching
  timestamps in order.
* :meth:`JournalRepo.inspect` returns the newest *unfinalised* row for a
  run (pending or ready_to_finalise) and ``None`` once everything is
  either finalised or discarded.
* :meth:`JournalRepo.discard` removes the row entirely.
* Re-entering :class:`TransactionContext` for the same ``run_id`` while
  a previous journal txn is still ``pending`` raises
  :class:`JournalRefusedError` — this is the substrate's "the engine
  crashed; resolve outstanding work before opening a fresh txn" guard.

Each test allocates its own temp directory via
:class:`tempfile.TemporaryDirectory` and bootstraps a fresh ``Project``
(which runs Alembic upgrade head against the empty SQLite file). No state
is shared between tests.

The journal row carries a FK to ``runs.id``, so before exercising the
repo we go through the public ``Project`` facade to register a minimal
:class:`FsmSpec` and start a run. That gives us a real ``run_id`` to
hand to the repo without poking at internal tables.
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ctxr.fsm.core.models import FsmSpec, State, Transition, Worker
from ctxr.fsm.sqlite import (
    JournalRefusedError,
    JournalRepo,
    Project,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _minimal_spec(spec_id: str = "demo") -> FsmSpec:
    """Two-state FsmSpec with an `always` transition (enough for start_run)."""
    states = [
        State(
            id="start",
            purpose="starting state",
            worker=Worker(role="start", prompt_template="go"),
            transitions=[Transition(to="finish", when="always")],
        ),
        State(
            id="finish",
            purpose="terminal state",
            worker=Worker(role="finish", prompt_template="done"),
            transitions=[],
        ),
    ]
    return FsmSpec(id=spec_id, entry="start", states=states)


@pytest.fixture()
def project_with_run() -> Iterator[tuple[Project, str]]:
    """Open a fresh project in a temp dir and yield ``(project, run_id)``.

    Each test gets its own ``TemporaryDirectory`` so the SQLite file is
    completely isolated; tear-down disposes the engine and removes the
    directory.
    """
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "fsm.sqlite3"
        project = Project.open(db_path)
        try:
            registered = project.register_spec(_minimal_spec())
            run = project.start_run(spec_id=registered.spec.id)
            yield project, run.id
        finally:
            project.close()


# ---------------------------------------------------------------------------
# Lifecycle: open → mark_ready → finalise
# ---------------------------------------------------------------------------


def test_open_inserts_pending_row_with_started_at(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    before = datetime.now(UTC)
    with project.session_factory() as session, session.begin():
        txn = repo.open(session, run_id=run_id)
    after = datetime.now(UTC)

    assert txn.run_id == run_id
    assert txn.status == "pending"
    assert txn.staged_writes == []
    assert txn.ready_at is None
    assert txn.finalised_at is None
    # started_at is stamped at open-time and is between our before/after
    # fence (with a 1-second slop on either side to absorb clock jitter
    # / millisecond truncation).
    assert before.timestamp() - 1.0 <= txn.started_at.timestamp() <= after.timestamp() + 1.0


def test_mark_ready_sets_ready_at_and_stages_writes(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)

    staged = [{"path": "manifest.json", "hash": "abc123"}]

    # Sleep a couple of ms so ready_at is strictly after started_at even
    # at millisecond precision; otherwise the equality below is racy.
    time.sleep(0.005)

    with project.session_factory() as session, session.begin():
        ready = repo.mark_ready(
            session, txn_id=opened.id, staged_writes=staged
        )

    assert ready.id == opened.id
    assert ready.status == "ready_to_finalise"
    assert ready.staged_writes == staged
    assert ready.ready_at is not None
    assert ready.started_at == opened.started_at
    assert ready.ready_at >= opened.started_at
    assert ready.finalised_at is None


def test_finalise_sets_finalised_at_and_terminal_status(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)
        ready = repo.mark_ready(session, txn_id=opened.id, staged_writes=[])

    time.sleep(0.005)

    with project.session_factory() as session, session.begin():
        finalised = repo.finalise(session, txn_id=opened.id)

    assert finalised.id == opened.id
    assert finalised.status == "finalised"
    assert finalised.finalised_at is not None
    assert finalised.ready_at == ready.ready_at
    assert finalised.finalised_at >= ready.ready_at


# ---------------------------------------------------------------------------
# inspect()
# ---------------------------------------------------------------------------


def test_inspect_returns_none_when_run_has_no_journal(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session:
        result = repo.inspect(session, run_id=run_id)

    assert result is None


def test_inspect_returns_pending_row(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)

    with project.session_factory() as session:
        snapshot = repo.inspect(session, run_id=run_id)

    assert snapshot is not None
    assert snapshot.id == opened.id
    assert snapshot.status == "pending"


def test_inspect_returns_ready_to_finalise_row(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)
        repo.mark_ready(session, txn_id=opened.id, staged_writes=[])

    with project.session_factory() as session:
        snapshot = repo.inspect(session, run_id=run_id)

    assert snapshot is not None
    assert snapshot.id == opened.id
    assert snapshot.status == "ready_to_finalise"


def test_inspect_skips_finalised_rows(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)
        repo.mark_ready(session, txn_id=opened.id, staged_writes=[])
        repo.finalise(session, txn_id=opened.id)

    with project.session_factory() as session:
        snapshot = repo.inspect(session, run_id=run_id)

    # A finalised row must not be returned by inspect — only pending /
    # ready_to_finalise are considered "needs recovery".
    assert snapshot is None


# ---------------------------------------------------------------------------
# discard()
# ---------------------------------------------------------------------------


def test_discard_removes_pending_row(
    project_with_run: tuple[Project, str],
) -> None:
    project, run_id = project_with_run
    repo = JournalRepo()

    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)

    with project.session_factory() as session, session.begin():
        repo.discard(session, txn_id=opened.id)

    with project.session_factory() as session:
        snapshot = repo.inspect(session, run_id=run_id)

    assert snapshot is None


def test_discard_is_idempotent_on_missing_row(
    project_with_run: tuple[Project, str],
) -> None:
    project, _run_id = project_with_run
    repo = JournalRepo()

    # Discarding a non-existent txn_id must be a no-op rather than an
    # error — matches the docstring contract.
    with project.session_factory() as session, session.begin():
        repo.discard(session, txn_id="00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Double-open refusal (substrate-level, via TransactionContext)
# ---------------------------------------------------------------------------


def test_second_transaction_for_same_run_raises_journal_refused(
    project_with_run: tuple[Project, str],
) -> None:
    """Opening a second atomic txn while a pending one exists must refuse.

    The refusal lives at the ``TransactionContext`` / ``@atomic`` layer
    (the bare :meth:`JournalRepo.open` is happy to insert another row
    by design — the journal is append-only); the refusal check is
    :func:`_refuse_if_outstanding`, which inspects the journal and
    raises :class:`JournalRefusedError` when the newest row for the run
    is in ``pending`` or ``ready_to_finalise`` status.
    """
    project, run_id = project_with_run
    repo = JournalRepo()

    # Seed an unfinalised journal row by hand so we model the
    # "previous run crashed mid-flight" scenario without needing a
    # full @atomic invocation to crash for us.
    with project.session_factory() as session, session.begin():
        opened = repo.open(session, run_id=run_id)

    # Now try to open a TransactionContext for the same run — it must
    # refuse with the typed substrate error.
    with pytest.raises(JournalRefusedError) as excinfo, project.transaction(run_id=run_id):
        pass  # pragma: no cover - body must not execute

    err = excinfo.value
    assert err.run_id == run_id
    assert err.txn.id == opened.id
    assert err.txn.status == "pending"
