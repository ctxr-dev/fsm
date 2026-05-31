"""Integration test: a complete run lifecycle through the W2 SQLite substrate.

This test exercises a deliberately small, end-to-end happy-path run that
walks three states (``state_a`` → ``state_b`` → ``state_c``) and validates
the manifest, the state-entry tree, the event log, and the journal as
side-effects of driving the lifecycle through the public ``Project``
facade.

What is being asserted (mapped to the brief's coverage list):

* The ``runs`` table manifest reflects ``current_state`` advancing from the
  entry state through to the terminal state, ending with status
  ``completed``.
* The state-entry tree returned by ``RunsRepo.state_tree`` mirrors the
  linear path: a root for ``state_a`` with one child (``state_b``), which
  in turn has one child (``state_c``).
* The event journal contains the canonical lifecycle event kinds emitted
  by the engine: ``state_entered``, ``state_exited``,
  ``transition_taken``, and ``run_completed`` (alongside the
  ``run_started`` emitted by ``Project.start_run``).
* The journal-txn table is clear after the run completes — every txn the
  driver opened is either ``finalised`` (and therefore not returned by
  ``JournalRepo.inspect``) or ``discard``-ed.

We deliberately drive the lifecycle through the public ``Project`` /
sub-repository surface rather than through any internal engine helper:
W2's job is to expose the substrate, not the engine, and a real engine
sitting on top of these repositories would invoke them in exactly the
shape this test does.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from ctxr.fsm.core.models import (
    EventKind,
    FsmSpec,
    State,
    Transition,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import RunTable

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_db() -> Iterator[Project]:
    """Yield a freshly-migrated ``Project`` rooted at a per-test temp dir.

    Each test gets its own ``tempfile.TemporaryDirectory`` so there is no
    cross-test state. The fixture handles migration (Alembic upgrade head
    on ``Project.open``) and engine disposal via the context-manager
    protocol.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite3"
        with Project.open(db_path) as proj:
            yield proj


@pytest.fixture
def linear_three_state_spec() -> FsmSpec:
    """Build a small three-state linear FSM spec used by the lifecycle test.

    Shape: ``state_a`` (entry) → ``state_b`` → ``state_c`` (terminal).
    Transitions use the bare ``"always"`` form because we are testing the
    persistence substrate, not the predicate evaluator.
    """
    return FsmSpec(
        id="lifecycle_demo",
        version=1,
        entry="state_a",
        states=[
            State(
                id="state_a",
                purpose="entry state",
                transitions=[Transition(to="state_b", when="always")],
            ),
            State(
                id="state_b",
                purpose="middle state",
                transitions=[Transition(to="state_c", when="always")],
            ),
            State(
                id="state_c",
                purpose="terminal state",
                transitions=[],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_current_state(project: Project, run_id: str, state_name: str) -> None:
    """Mutate ``runs.current_state`` directly inside an atomic block.

    The W2 ``RunsRepo`` exposes ``update_status`` but no dedicated
    ``set_current_state`` method — that is engine-layer policy in W3+.
    For this test we do the same direct row mutation a future engine
    would, keeping it inside the project's ``Session.begin()`` block so
    it commits atomically.
    """
    with project.session_factory() as session, session.begin():
        row = session.get(RunTable, run_id)
        assert row is not None, f"run {run_id!r} disappeared mid-test"
        row.current_state = state_name


def _runtime_producer_id(project: Project) -> str:
    """Return the runtime producer's id, registering it if absent.

    ``Project.start_run`` already upserts the ``engine/fsm.runtime``
    producer; this helper is just a typed way to retrieve it after the
    fact so the driver can attribute its own follow-up events to the
    same producer.
    """
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session,
            kind="engine",
            name="fsm.runtime",
        )
    return producer.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_full_run_lifecycle_three_state_path(
    project_db: Project, linear_three_state_spec: FsmSpec
) -> None:
    """Drive a 3-state run through the substrate and assert every side-effect.

    This is the canonical happy-path integration: register a spec, start
    a run, walk three states, finish, then verify that the manifest, the
    state tree, the event log, and the journal all reflect what
    happened.
    """
    project = project_db

    # ── 1. Register spec ────────────────────────────────────────────────
    registered = project.register_spec(linear_three_state_spec)
    assert registered.created is True
    spec = registered.spec
    assert spec.slug == "lifecycle_demo"
    assert spec.version == 1

    # ── 2. Start a run ──────────────────────────────────────────────────
    run = project.start_run(spec.id, args={"demo": True})
    assert run.status == "in_progress"
    assert run.fsm_spec_id == spec.id
    assert run.fsm_spec_hash == spec.hash
    assert run.current_state is None  # not entered yet

    run_id = run.id
    producer_id = _runtime_producer_id(project)

    # ── 3. Drive the lifecycle ──────────────────────────────────────────
    # The driver below mimics what a real engine would do for each state:
    #
    #   * acquire (no-op here; we are single-writer in the test)
    #   * open a journal txn
    #   * insert a state-entry row, emit ``state_entered``
    #   * set ``current_state`` on the run manifest
    #   * record a transition decision, emit ``transition_taken``
    #     (skipped for the terminal state)
    #   * mark the state exited, emit ``state_exited``
    #   * finalise the journal txn
    #
    # We use ``always`` transitions so ``predicate_result`` is ``None``
    # and ``kind`` is ``"always"`` — see TransitionsRepo.create.

    walk = ["state_a", "state_b", "state_c"]
    state_pks: dict[str, str] = {}

    for index, state_name in enumerate(walk):
        # Open a journal txn for this state's pre-commit ledger.
        with project.session_factory() as session, session.begin():
            txn = project.journal.open(session, run_id=run_id)
            txn_id = txn.id

        # Allocate the entry_seq and insert the state row.
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

            # state_entered event for this entry.
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

        # Reflect the advance on the manifest.
        _set_current_state(project, run_id, state_name)

        # If this is not the terminal state, record the transition out of
        # it and emit ``transition_taken``. We do this *before* exiting
        # the current state so the temporal ordering in the event log
        # matches what a real engine produces.
        if index < len(walk) - 1:
            next_state_name = walk[index + 1]
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

        # Mark the state exited and emit ``state_exited``.
        with project.session_factory() as session, session.begin():
            project.states.mark_exited(
                session, state_pks[state_name], outputs={"ok": True}
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

        # Mark the journal txn ready and finalise it — this is what
        # "journal cleared" looks like at the end of a successful state
        # commit.
        with project.session_factory() as session, session.begin():
            project.journal.mark_ready(
                session,
                txn_id=txn_id,
                staged_writes=[{"state": state_name}],
            )
            project.journal.finalise(session, txn_id=txn_id)

    # Mark the run completed and emit the matching event.
    with project.session_factory() as session, session.begin():
        completed = project.runs.update_status(
            session,
            run_id=run_id,
            status="completed",
            ended_at=None,  # let the repo stamp it via last_update_at
            verdict="ok",
        )
        assert completed is not None
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.run_completed.value,
            payload={"run_id": run_id, "verdict": "ok"},
            run_id=run_id,
        )

    # ── 4. Assert manifest reflects the final state ─────────────────────
    final_run = project.get_run(run_id)
    assert final_run is not None
    assert final_run.status == "completed"
    assert final_run.current_state == "state_c"
    assert final_run.verdict == "ok"

    # ── 5. Assert state_tree shape mirrors the linear walk ──────────────
    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)
    assert tree is not None
    assert tree.state_id == "state_a"
    assert tree.entry_seq == 1
    assert len(tree.children) == 1
    middle = tree.children[0]
    assert middle.state_id == "state_b"
    assert middle.entry_seq == 2
    assert len(middle.children) == 1
    terminal = middle.children[0]
    assert terminal.state_id == "state_c"
    assert terminal.entry_seq == 3
    assert terminal.children == []

    # ── 6. Assert event log contains the required kinds ─────────────────
    with project.session_factory() as session:
        events = list(project.runs.events(session, run_id))
    kinds = [event.kind for event in events]

    # Every required kind must appear at least once.
    required_kinds = {
        EventKind.state_entered.value,
        EventKind.state_exited.value,
        EventKind.transition_taken.value,
        EventKind.run_completed.value,
    }
    assert required_kinds.issubset(set(kinds)), (
        f"missing required event kinds: {required_kinds - set(kinds)!r}"
    )

    # Three entries → three state_entered + three state_exited rows.
    assert kinds.count(EventKind.state_entered.value) == 3
    assert kinds.count(EventKind.state_exited.value) == 3
    # Two transitions (a→b, b→c); the terminal state emits none.
    assert kinds.count(EventKind.transition_taken.value) == 2
    # Exactly one run_completed at the end.
    assert kinds.count(EventKind.run_completed.value) == 1

    # The very first event recorded by ``Project.start_run`` is
    # ``run_started``; confirm it landed on this run's timeline so the
    # whole journal is coherent.
    assert kinds[0] == EventKind.run_started.value
    # And the very last event we recorded is ``run_completed``.
    assert kinds[-1] == EventKind.run_completed.value

    # Per-run ``seq`` must be a contiguous strictly-monotonic sequence
    # starting at 1 — that is the bus's documented invariant.
    seqs = [event.seq for event in events]
    assert seqs == list(range(1, len(events) + 1))

    # ── 7. Assert journal is cleared ────────────────────────────────────
    with project.session_factory() as session:
        pending = project.journal.inspect(session, run_id=run_id)
    assert pending is None, (
        f"journal was not cleared at run end; still has txn {pending!r}"
    )
