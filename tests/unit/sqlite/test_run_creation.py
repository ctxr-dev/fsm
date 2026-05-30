"""Unit tests for :meth:`Project.start_run` atomicity.

The contract under test:

* :meth:`Project.start_run` is wrapped in a single ``Session.begin()``
  block. After it returns, **all** of the following must be visible:
    - the new ``runs`` row,
    - the engine producer row (``producers`` table, ``kind='engine'`` +
      ``name='fsm.runtime'``), and
    - the journal event the call emits (the canonical "run started"
      event, which the engine ships as :class:`EventKind.run_started`).
* If *any* step inside that begin-block raises, the whole transaction
  must roll back so **no** partial rows linger. We provoke a failure by
  monkey-patching :meth:`EventsRepo.emit` to raise mid-transaction and
  then assert the runs / producers / events tables are pristine.

The tests use a real on-disk SQLite database under a per-test
``tempfile.TemporaryDirectory()`` so the isolation guarantee mirrors
production behaviour (WAL + PRAGMAs from ``open_engine``). Each test
opens its own :class:`Project`, runs ``alembic upgrade head``, and
closes the project in a ``finally`` block.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from ctxr.fsm.core.models import (
    EventKind,
    FsmSpec,
    Predicate,
    State,
    Transition,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import RunTable
from ctxr.fsm.sqlite.models_events import EventTable, ProducerTable

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


_RUNTIME_PRODUCER_KIND = "engine"
_RUNTIME_PRODUCER_NAME = "fsm.runtime"


def _minimal_spec() -> FsmSpec:
    """Build the smallest valid :class:`FsmSpec`.

    A two-state machine with a deterministic transition is enough — the
    engine never executes here, we just need a registrable spec so
    :meth:`Project.start_run` has something to attach the new run to.
    """
    return FsmSpec(
        id="test_fsm",
        version=1,
        entry="draft",
        states=[
            State(
                id="draft",
                purpose="initial state",
                transitions=[
                    Transition(to="done", when=Predicate("true == true")),
                ],
            ),
            State(id="done", purpose="terminal"),
        ],
    )


@pytest.fixture
def project() -> Any:
    """Yield a fresh :class:`Project` bound to a per-test tempdir.

    ``Project.open`` runs ``alembic upgrade head`` so the schema is in
    place; ``project.close()`` in the finally block disposes the engine
    and unbinds the session factory so the next test starts clean.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "fsm.sqlite"
        proj = Project.open(db_path, migrate=True)
        try:
            yield proj
        finally:
            proj.close()


# ---------------------------------------------------------------------------
# Success path: every row visible after commit
# ---------------------------------------------------------------------------


def test_start_run_inserts_run_producer_and_event_atomically(project: Project) -> None:
    """All three side effects are visible in a fresh session after commit.

    We open a *new* session via ``project.session_factory`` so we never
    read stale objects from the in-flight session ``start_run`` itself
    used. That makes the assertion "the commit reached the on-disk
    database" instead of "the ORM identity map remembers what we
    inserted".
    """
    registered = project.register_spec(_minimal_spec())
    spec = registered.spec

    run = project.start_run(spec.id, args={"hello": "world"})

    # --- runs row ----------------------------------------------------------
    with project.session_factory() as session:
        run_row = session.get(RunTable, run.id)
        assert run_row is not None, "runs row must be visible post-commit"
        assert run_row.project_id == spec.project_id
        assert run_row.fsm_spec_id == spec.id
        assert run_row.fsm_spec_hash == spec.hash
        assert run_row.status == "in_progress"

    # --- producers row -----------------------------------------------------
    with project.session_factory() as session:
        producer_row = session.execute(
            select(ProducerTable).where(
                ProducerTable.kind == _RUNTIME_PRODUCER_KIND,
                ProducerTable.name == _RUNTIME_PRODUCER_NAME,
            )
        ).scalar_one_or_none()
        assert producer_row is not None, "engine producer must be upserted"

    # --- start event -------------------------------------------------------
    # ``Project.start_run`` emits exactly one event for the new run; the
    # event kind is the canonical engine "run started" signal (the
    # downstream state-entered event is emitted by the engine itself
    # once it advances into the entry state, which start_run does not
    # do).
    with project.session_factory() as session:
        events = (
            session.execute(
                select(EventTable)
                .where(EventTable.run_id == run.id)
                .order_by(EventTable.seq.asc())
            )
            .scalars()
            .all()
        )
        assert len(events) == 1, (
            f"start_run must emit exactly one event for the new run, "
            f"got {len(events)}"
        )
        evt = events[0]
        assert evt.kind == EventKind.run_started.value
        assert evt.producer_id == producer_row.id
        assert evt.seq == 1


def test_start_run_returns_run_with_args_preserved(project: Project) -> None:
    """The returned :class:`Run` value-object reflects the inserted row.

    Sanity-check that the convenience facade does not silently drop the
    ``args`` payload between Pydantic validation and the SQL insert.
    """
    registered = project.register_spec(_minimal_spec())
    spec = registered.spec

    payload = {"flag": True, "n": 42}
    run = project.start_run(spec.id, args=payload)

    assert run.args == payload
    assert run.fsm_spec_id == spec.id
    assert run.status == "in_progress"


def test_start_run_unknown_spec_raises_lookup_error(project: Project) -> None:
    """An unknown ``spec_id`` is rejected at the facade boundary.

    The facade refuses to mint a run with no associated FSM definition —
    the engine would have nothing to load. We assert the error surfaces
    as :class:`LookupError` and that nothing was inserted as a side
    effect.
    """
    with pytest.raises(LookupError):
        project.start_run("00000000-0000-0000-0000-000000000000")

    # Nothing was inserted into any of the three tables touched by a
    # successful start_run.
    with project.session_factory() as session:
        assert session.execute(select(RunTable)).first() is None
        assert session.execute(select(EventTable)).first() is None
        # The producer table is intentionally untouched on the failure
        # path because the LookupError fires before the upsert runs.
        producer_row = session.execute(
            select(ProducerTable).where(
                ProducerTable.kind == _RUNTIME_PRODUCER_KIND,
                ProducerTable.name == _RUNTIME_PRODUCER_NAME,
            )
        ).scalar_one_or_none()
        assert producer_row is None


# ---------------------------------------------------------------------------
# Rollback path: mid-transaction failure leaves no partial rows
# ---------------------------------------------------------------------------


def test_start_run_rolls_back_when_event_emit_raises(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside the @atomic block must roll the whole thing back.

    We patch :meth:`EventsRepo.emit` on the bound repo instance so the
    *third* operation inside ``start_run`` raises after the runs row
    and the producer row have already been inserted into the session.
    Because everything is wrapped in a single ``Session.begin()``
    context manager, the commit never happens and the database must
    end up with zero new rows.

    A Pydantic ``ValidationError`` is the canonical mid-flight failure
    the spec calls out, but the rollback contract is "any exception
    rolls back". We raise a ``ValueError`` here because it is unambiguous
    and because instantiating a real Pydantic ``ValidationError`` from
    user code is awkward without a model to anchor it to.
    """
    registered = project.register_spec(_minimal_spec())
    spec = registered.spec

    class _BoomError(ValueError):
        """Sentinel to make the assertion-on-raise unambiguous."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise _BoomError("simulated mid-transaction failure")

    monkeypatch.setattr(project.events, "emit", _boom)

    with pytest.raises(_BoomError):
        project.start_run(spec.id, args={"hello": "world"})

    # --- runs table: pristine ---------------------------------------------
    with project.session_factory() as session:
        runs = session.execute(select(RunTable)).scalars().all()
        assert runs == [], (
            f"runs table must be empty after rollback, got {len(runs)} rows"
        )

    # --- events table: pristine -------------------------------------------
    # Defensive: even though the patched emit raised before inserting an
    # event row, a buggy implementation could have inserted one before
    # the validation step. The rollback must wipe it either way.
    with project.session_factory() as session:
        events = session.execute(select(EventTable)).scalars().all()
        assert events == [], (
            f"events table must be empty after rollback, got {len(events)} rows"
        )

    # --- producers table: pristine ----------------------------------------
    # The engine producer upsert happens *inside* the same @atomic block
    # and therefore must also be rolled back.
    with project.session_factory() as session:
        engine_producer = session.execute(
            select(ProducerTable).where(
                ProducerTable.kind == _RUNTIME_PRODUCER_KIND,
                ProducerTable.name == _RUNTIME_PRODUCER_NAME,
            )
        ).scalar_one_or_none()
        assert engine_producer is None, (
            "engine producer row must be rolled back when emit fails"
        )


def test_start_run_second_attempt_succeeds_after_rollback(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a rollback, a subsequent ``start_run`` works normally.

    Belt-and-suspenders: a leaked transaction or a half-committed
    sessionmaker would surface here as a second-call failure. The
    second call must produce a brand-new run with seq=1 (since the
    failed first attempt should not have allocated any per-run seq
    values either).
    """
    registered = project.register_spec(_minimal_spec())
    spec = registered.spec

    # Phase 1: force a failure.
    def _boom(*args: Any, **kwargs: Any) -> None:
        raise ValueError("simulated failure")

    monkeypatch.setattr(project.events, "emit", _boom)
    with pytest.raises(ValueError):
        project.start_run(spec.id)

    # Phase 2: undo the patch and try again.
    monkeypatch.undo()
    run = project.start_run(spec.id, args={"retry": True})

    with project.session_factory() as session:
        run_row = session.get(RunTable, run.id)
        assert run_row is not None
        assert run_row.status == "in_progress"

        events = (
            session.execute(
                select(EventTable)
                .where(EventTable.run_id == run.id)
                .order_by(EventTable.seq.asc())
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].kind == EventKind.run_started.value
        assert events[0].seq == 1
