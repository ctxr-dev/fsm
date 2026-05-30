"""Rollback semantics of the ``@atomic`` envelope.

When the user body wrapped by ``@atomic`` raises, the W2 substrate must:

1. Roll back the main session — no rows the body added may survive.
2. Leave the run's :class:`JournalTxn` row in ``status='pending'`` so the
   W3 recovery path can later inspect / replay / discard it.

These two guarantees together are what make the substrate crash-safe at
the application layer (write-ahead-log discipline). The tests below
provoke an explicit raise inside an ``@atomic`` block and assert both
invariants directly against the database.

Pure ``ctxr.fsm.sqlite`` — we only import from the package's public
surface (``Project``, ``atomic``, ``ProjectsRepo``, ``JournalRepo``) so
the suite documents the supported entry points.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from ctxr.fsm.core.models import FsmSpec, State
from ctxr.fsm.sqlite import (
    JournalRepo,
    Project,
    ProjectsRepo,
    atomic,
)
from ctxr.fsm.sqlite.models_core import ProjectTable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a fresh :class:`Project` backed by a per-test SQLite file.

    The tempdir is destroyed on teardown, giving us full isolation
    between tests (no shared DB, no leaked rows, no leaked sessionmaker
    binding because ``Project.close`` resets the context-var token).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.sqlite"
        proj = Project.open(db_path, migrate=True)
        try:
            yield proj
        finally:
            proj.close()


def _minimal_spec() -> FsmSpec:
    """Return the smallest valid :class:`FsmSpec` we can register.

    A single state ``draft`` with no transitions is enough — the engine
    is never asked to advance the run in these tests, so we only need
    the spec to satisfy the registration invariants.
    """
    return FsmSpec(
        id=f"rollback-spec-{uuid.uuid4().hex[:8]}",
        version=1,
        entry="draft",
        states=[State(id="draft", purpose="placeholder")],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_projects(project: Project) -> int:
    """Return the total number of rows in the ``projects`` table."""
    with project.session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(ProjectTable)).scalar_one()
        )


def _latest_journal_txn(project: Project, run_id: str):
    """Return the newest unfinalised journal txn for ``run_id`` (or None)."""
    with project.session_factory() as session:
        return JournalRepo().inspect(session, run_id=run_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class _BoomError(RuntimeError):
    """Sentinel exception raised inside the @atomic body to force rollback."""


def test_atomic_rollback_inserts_no_rows_and_leaves_journal_pending(
    project: Project,
) -> None:
    """The body raises after an insert — every row must vanish, journal stays pending."""
    # Arrange: register a spec and start a run so we have a real run_id
    # for the journal repo to bind the txn to.
    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    baseline_project_count = _count_projects(project)
    rollback_slug = f"rollback-target-{uuid.uuid4().hex[:8]}"

    @atomic
    def insert_then_raise(session, run_id: str) -> None:
        """Insert a project row and then raise — the insert must roll back."""
        ProjectsRepo.create(session, slug=rollback_slug)
        # Sanity check inside the txn: SQLAlchemy has flushed the row so
        # a SELECT inside the same session sees it. The whole point of
        # the rollback is that it should NOT be visible after we raise.
        stmt = select(func.count()).select_from(ProjectTable).where(
            ProjectTable.slug == rollback_slug
        )
        assert session.execute(stmt).scalar_one() == 1
        raise _BoomError("provoked rollback")

    # Act + Assert (raise path)
    with pytest.raises(_BoomError, match="provoked rollback"):
        insert_then_raise(run_id=run.id)

    # Assert: no rows leaked from the rolled-back txn.
    assert _count_projects(project) == baseline_project_count

    # And the specific row the body tried to insert is absent.
    with project.session_factory() as session:
        stmt = select(ProjectTable).where(ProjectTable.slug == rollback_slug)
        assert session.execute(stmt).scalar_one_or_none() is None

    # Assert: the journal row was opened (in its own short txn, so it
    # survived the main-txn abort) and is still in status='pending'
    # waiting for recovery — exactly the WAL contract documented in
    # ctxr.fsm.sqlite.transactions.
    txn = _latest_journal_txn(project, run.id)
    assert txn is not None, "expected a pending journal txn for the run"
    assert txn.run_id == run.id
    assert txn.status == "pending"
    assert txn.ready_at is None
    assert txn.finalised_at is None
    # staged_writes was never recorded (mark_ready is on the happy path
    # only), so the snapshot must be empty.
    assert txn.staged_writes == []


def test_atomic_rollback_preserves_pre_existing_rows(project: Project) -> None:
    """Rows committed by an EARLIER atomic txn must survive a later rollback."""
    # Arrange: register a spec + start a run via the facade. ``start_run``
    # itself runs inside ``session.begin()`` and inserts the run row,
    # producer row, and a ``run_started`` event before this test runs.
    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    # Snapshot the project count AFTER the successful setup work so we
    # are measuring "what survived the rollback" relative to that point.
    baseline_project_count = _count_projects(project)
    durable_slug = f"durable-{uuid.uuid4().hex[:8]}"
    doomed_slug = f"doomed-{uuid.uuid4().hex[:8]}"

    # First atomic txn: commits cleanly, inserts a project we expect to
    # see *after* the second txn rolls back.
    @atomic
    def insert_committed(session, run_id: str) -> str:
        return ProjectsRepo.create(session, slug=durable_slug).id

    committed_project_id = insert_committed(run_id=run.id)
    assert _count_projects(project) == baseline_project_count + 1

    # Second atomic txn: inserts a project and then raises. Only the
    # second insert must roll back; the first must remain visible.
    @atomic
    def insert_then_raise(session, run_id: str) -> None:
        ProjectsRepo.create(session, slug=doomed_slug)
        raise _BoomError("rollback the second insert only")

    with pytest.raises(_BoomError):
        insert_then_raise(run_id=run.id)

    # Assert: durable row survives, doomed row is gone.
    with project.session_factory() as session:
        durable_row = session.get(ProjectTable, committed_project_id)
        assert durable_row is not None
        assert durable_row.slug == durable_slug

        doomed_stmt = select(ProjectTable).where(ProjectTable.slug == doomed_slug)
        assert session.execute(doomed_stmt).scalar_one_or_none() is None

    assert _count_projects(project) == baseline_project_count + 1

    # Assert: the journal txn for the failed atomic call is left
    # pending. ``inspect`` returns the *newest* unfinalised row, so the
    # first (finalised) txn does not mask the second.
    txn = _latest_journal_txn(project, run.id)
    assert txn is not None
    assert txn.status == "pending"
    assert txn.finalised_at is None


def test_pending_journal_txn_blocks_further_atomic_calls(project: Project) -> None:
    """A left-behind pending txn must refuse subsequent @atomic calls for the run.

    This is the recovery handshake: once a journal txn is stuck in
    ``pending``, the next call to ``@atomic`` for the same run raises
    :class:`JournalRefusedError` so the operator is forced to resolve
    the outstanding txn (via the W3 CLI's ``run resume --journal
    {discard,replay}``) before any new work proceeds.
    """
    from ctxr.fsm.sqlite import JournalRefusedError

    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    @atomic
    def insert_then_raise(session, run_id: str) -> None:
        ProjectsRepo.create(session, slug=f"first-attempt-{uuid.uuid4().hex[:8]}")
        raise _BoomError("first attempt fails")

    with pytest.raises(_BoomError):
        insert_then_raise(run_id=run.id)

    # The blocking txn:
    blocking = _latest_journal_txn(project, run.id)
    assert blocking is not None
    assert blocking.status == "pending"

    # A second @atomic call for the *same* run must be refused outright.
    @atomic
    def harmless(session, run_id: str) -> None:
        # Body never runs — the refusal fires in __enter__ before we
        # get here. We still write the body realistically so the test
        # fails loudly if the refusal is silently skipped.
        ProjectsRepo.create(session, slug=f"second-attempt-{uuid.uuid4().hex[:8]}")

    with pytest.raises(JournalRefusedError) as excinfo:
        harmless(run_id=run.id)

    # The refusal surfaces the blocking txn for diagnostic rendering.
    assert excinfo.value.run_id == run.id
    assert excinfo.value.txn.id == blocking.id
    assert excinfo.value.txn.status == "pending"

    # And — crucially — the second body's insert never landed. We
    # double-check by using uuid4() as the seed for the slug above; no
    # row should match the prefix.
    with project.session_factory() as session:
        # Use uuid.uuid4() per the test conventions — we just need
        # *some* unique value for our search; the assertion is that NO
        # row was inserted by the harmless() body.
        _ = uuid4()  # exercised here for convention; not used as id
        count_stmt = select(func.count()).select_from(ProjectTable).where(
            ProjectTable.slug.like("second-attempt-%")
        )
        assert session.execute(count_stmt).scalar_one() == 0
