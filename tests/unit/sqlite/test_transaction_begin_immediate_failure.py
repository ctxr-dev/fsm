"""Acquisition-failure cleanup for the ``@atomic`` envelope.

When ``BEGIN IMMEDIATE`` (step 3 of the atomic lifecycle) fails to take
the write-lock (the classic transient ``database is locked`` while a
peer writer holds it), the journal row opened in step 2 was never used:
no work could have been staged against a lock that was never acquired.

The W2 substrate must therefore *discard* that just-opened ``pending``
journal row rather than leave it behind. A left-behind pending row would
wedge the run: the next ``@atomic`` call for the same run hits
:class:`JournalRefusedError` and the run sits in ``pending`` until a
manual ``run resume``. Since the underlying condition (a peer holding the
write-lock) is transient, the failure must instead be *retryable*.

Contrast :mod:`tests.unit.sqlite.test_transaction_rollback`, which pins
the *other* invariant: when the lock WAS held and the caller body raised,
the pending row is deliberately kept for W3 recovery.

These tests provoke the failure by monkeypatching
:func:`ctxr.fsm.sqlite.transactions._begin_immediate` to raise the same
``OperationalError`` SQLite surfaces on a busy database, then assert the
journal table is clean and a retry succeeds.
"""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from ctxr.fsm.core.models import FsmSpec, State
from ctxr.fsm.sqlite import (
    JournalRepo,
    Project,
    ProjectsRepo,
    TransactionContext,
    atomic,
)
from ctxr.fsm.sqlite import transactions as txn_module
from ctxr.fsm.sqlite.models_enforcement import JournalTxnTable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a fresh :class:`Project` backed by a per-test SQLite file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "fsm.sqlite"
        proj = Project.open(db_path, migrate=True)
        try:
            yield proj
        finally:
            proj.close()


def _minimal_spec() -> FsmSpec:
    """Return the smallest valid :class:`FsmSpec` we can register."""
    return FsmSpec(
        id=f"begin-immediate-spec-{uuid.uuid4().hex[:8]}",
        version=1,
        entry="draft",
        states=[State(id="draft", purpose="placeholder")],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lock_error() -> OperationalError:
    """Return an ``OperationalError`` shaped like SQLite's busy failure.

    SQLite raises ``sqlite3.OperationalError('database is locked')`` when
    ``BEGIN IMMEDIATE`` cannot take the reserved write-lock; SQLAlchemy
    wraps it in :class:`sqlalchemy.exc.OperationalError`. We construct the
    wrapper directly so the test does not need a second live connection
    racing for the lock; the *behaviour under the failure* is what we are
    pinning, not SQLite's own locking.
    """
    return OperationalError(
        statement="BEGIN IMMEDIATE",
        params=None,
        orig=Exception("database is locked"),
    )


def _count_all_journal_rows(project: Project) -> int:
    """Return the total number of rows in ``journal_txns`` (any status)."""
    with project.session_factory() as session:
        return int(
            session.execute(
                select(func.count()).select_from(JournalTxnTable)
            ).scalar_one()
        )


def _latest_unfinalised_txn(project: Project, run_id: str):
    """Return the newest unfinalised journal txn for ``run_id`` (or None)."""
    with project.session_factory() as session:
        return JournalRepo().inspect(session, run_id=run_id)


# ---------------------------------------------------------------------------
# @atomic decorator
# ---------------------------------------------------------------------------


def test_atomic_begin_immediate_failure_discards_orphaned_journal_row(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed BEGIN IMMEDIATE must leave NO pending journal row behind."""
    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    # ``start_run`` runs through the engine but does not leave an
    # unfinalised journal txn; capture the baseline so we can assert the
    # failed attempt adds nothing durable.
    baseline_rows = _count_all_journal_rows(project)
    assert _latest_unfinalised_txn(project, run.id) is None

    # Provoke a transient lock failure at BEGIN IMMEDIATE.
    monkeypatch.setattr(
        txn_module,
        "_begin_immediate",
        lambda _session: (_ for _ in ()).throw(_make_lock_error()),
    )

    @atomic
    def do_work(session, run_id: str) -> None:
        # Never reached: BEGIN IMMEDIATE fails before the body runs.
        ProjectsRepo.create(session, slug=f"never-{uuid.uuid4().hex[:8]}")

    # The transient failure propagates to the caller...
    with pytest.raises(OperationalError, match="database is locked"):
        do_work(run_id=run.id)

    # ...but the orphaned pending row was cleaned up: no unfinalised txn
    # for the run, and the table row count is back to baseline.
    assert _latest_unfinalised_txn(project, run.id) is None
    assert _count_all_journal_rows(project) == baseline_rows


def test_atomic_retry_succeeds_after_begin_immediate_failure(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient BEGIN IMMEDIATE failure must be retryable, not wedging."""
    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    # First attempt: BEGIN IMMEDIATE fails (peer holds the write-lock).
    monkeypatch.setattr(
        txn_module,
        "_begin_immediate",
        lambda _session: (_ for _ in ()).throw(_make_lock_error()),
    )

    durable_slug = f"after-retry-{uuid.uuid4().hex[:8]}"

    @atomic
    def do_work(session, run_id: str) -> str:
        return ProjectsRepo.create(session, slug=durable_slug).id

    with pytest.raises(OperationalError):
        do_work(run_id=run.id)

    # Lift the simulated lock contention and retry the *same* run. If the
    # failed attempt had wedged the run, this second call would raise
    # JournalRefusedError instead of doing the work.
    monkeypatch.undo()

    committed_id = do_work(run_id=run.id)

    # The retry committed cleanly: the row exists and the journal txn for
    # the successful attempt is finalised (no unfinalised txn lingers).
    with project.session_factory() as session:
        from ctxr.fsm.sqlite.models_core import ProjectTable

        stmt = select(ProjectTable).where(ProjectTable.id == committed_id)
        row = session.execute(stmt).scalar_one_or_none()
        assert row is not None
        assert row.slug == durable_slug

    assert _latest_unfinalised_txn(project, run.id) is None


def test_atomic_discard_failure_does_not_mask_acquisition_error(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed best-effort discard must not mask the original lock error.

    The cleanup runs its own short txn, which can itself hit the same
    ``database is locked`` condition that triggered the path (the rival
    writer still holds the lock). When that happens the caller must still
    see the *original* acquisition ``OperationalError`` — never the
    secondary cleanup failure — so the failure stays a recognisable,
    retryable lock error rather than a confusing cleanup crash.
    """
    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    # BEGIN IMMEDIATE fails with the canonical lock error.
    monkeypatch.setattr(
        txn_module,
        "_begin_immediate",
        lambda _session: (_ for _ in ()).throw(_make_lock_error()),
    )

    # And the cleanup's own DELETE ALSO fails (a different, loud error so
    # the test can tell which one would surface if masking occurred). We
    # patch JournalRepo.discard rather than the shared session helper so
    # the earlier refusal-check / journal-open steps still run normally;
    # only the best-effort discard inside _discard_journal_row blows up.
    class _CleanupBoomError(RuntimeError):
        pass

    def _boom_discard(_self, _session, *, txn_id: str) -> None:
        raise _CleanupBoomError("discard could not delete the orphan row")

    monkeypatch.setattr(JournalRepo, "discard", _boom_discard)

    @atomic
    def do_work(session, run_id: str) -> None:
        ProjectsRepo.create(session, slug=f"never-{uuid.uuid4().hex[:8]}")

    # The ORIGINAL OperationalError propagates; the _CleanupBoomError is
    # swallowed by the best-effort discard.
    with pytest.raises(OperationalError, match="database is locked"):
        do_work(run_id=run.id)


# ---------------------------------------------------------------------------
# TransactionContext
# ---------------------------------------------------------------------------


def test_transaction_context_begin_immediate_failure_discards_journal_row(
    project: Project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The imperative flavour must clean up the orphaned row too."""
    registered = project.register_spec(_minimal_spec(), project_slug="default")
    run = project.start_run(registered.spec.id)

    baseline_rows = _count_all_journal_rows(project)
    assert _latest_unfinalised_txn(project, run.id) is None

    monkeypatch.setattr(
        txn_module,
        "_begin_immediate",
        lambda _session: (_ for _ in ()).throw(_make_lock_error()),
    )

    with (
        pytest.raises(OperationalError, match="database is locked"),
        TransactionContext(project.engine, run_id=run.id),
    ):
        pass  # never reached: __enter__ fails at BEGIN IMMEDIATE

    assert _latest_unfinalised_txn(project, run.id) is None
    assert _count_all_journal_rows(project) == baseline_rows
