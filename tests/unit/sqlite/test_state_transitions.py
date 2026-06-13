"""Unit tests for the W2 state-tree sub-repositories.

Exercises three slices of the persistence contract that the engine and
higher layers will rely on:

* :meth:`StatesRepo.create` + :meth:`StatesRepo.mark_exited` round-trip
  (status, outputs, exited_at all persisted).
* :meth:`TransitionsRepo.create` round-trip including the tri-state
  ``predicate_result`` storage (True / False / None).
* :meth:`StatesRepo.next_entry_seq` monotonically increments per run.
* :meth:`RunsRepo.state_tree` returns a nested ``StateNode`` that
  reflects the actual transition edges between state entries.

Every test gets its own ``tempfile.TemporaryDirectory()`` and opens a
fresh :class:`Project`, so the suite is fully isolated — no shared DB
state, no cross-test ordering effects.

Imports are limited to the public ``ctxr.fsm.sqlite`` surface so the
tests double as smoke checks for the package's re-export list.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from ctxr.fsm.core import FsmSpec
from ctxr.fsm.core import State as CoreState
from ctxr.fsm.core import Transition as CoreTransition
from ctxr.fsm.sqlite import (
    Project,
    StateRecord,
    StatesRepo,
    StateTransitionError,
    TransitionRecord,
    TransitionsRepo,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project() -> Iterator[Project]:
    """Yield a fresh ``Project`` against an isolated SQLite file.

    Each test gets its own ``TemporaryDirectory`` so the database is
    torn down with the directory at fixture exit; nothing leaks between
    tests, and there is no shared state to reason about.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "fsm.sqlite"
        proj = Project.open(db_path)
        try:
            yield proj
        finally:
            proj.close()


def _make_spec(state_id: str = "draft") -> FsmSpec:
    """Build a trivial single-state FSM spec.

    The tests do not exercise engine semantics — they need an
    FSM-shaped row purely so :meth:`Project.register_spec` /
    :meth:`Project.start_run` have something to anchor the run to.
    """
    state = CoreState(
        id=state_id,
        transitions=[CoreTransition(to="end", when="always")],
    )
    end = CoreState(id="end")
    return FsmSpec(id="test_fsm", version=1, entry=state_id, states=[state, end])


@pytest.fixture
def run_id(project: Project) -> str:
    """Register a spec and start a run, returning its id.

    Most repo-level tests need a real ``runs`` row to satisfy the
    ``states.run_id`` FK; this fixture is the cheapest way to get one
    without hand-rolling the lifecycle inserts.
    """
    spec = project.register_spec(_make_spec())
    run = project.start_run(spec.spec.id)
    return run.id


# ---------------------------------------------------------------------------
# StatesRepo.create + mark_exited round-trip
# ---------------------------------------------------------------------------


def test_states_repo_create_persists_initial_row(project: Project, run_id: str) -> None:
    """``create`` writes a row in ``status='entered'`` with the given inputs."""
    repo = StatesRepo()
    with project.session_factory() as session, session.begin():
        entry = repo.create(
            session,
            run_id=run_id,
            state_id="draft",
            inputs={"topic": "fsm"},
            entry_seq=1,
        )

    assert isinstance(entry, StateRecord)
    assert entry.run_id == run_id
    assert entry.state_id == "draft"
    assert entry.entry_seq == 1
    assert entry.status == "entered"
    assert entry.exited_at is None
    assert entry.inputs == {"topic": "fsm"}
    # Outputs default to an empty bag — the caller fills them at mark_exited.
    assert entry.outputs == {}
    # Timestamp string is non-empty ISO-8601 with millisecond precision.
    assert entry.entered_at
    assert "T" in entry.entered_at


def test_states_repo_mark_exited_round_trips_outputs(
    project: Project, run_id: str
) -> None:
    """``mark_exited`` flips status, stamps exited_at, and persists outputs."""
    repo = StatesRepo()
    with project.session_factory() as session:
        with session.begin():
            entry = repo.create(
                session,
                run_id=run_id,
                state_id="draft",
                inputs={"x": 1},
                entry_seq=1,
            )

        # Re-open the unit-of-work so we exercise an actual round-trip via
        # the second transaction — proves the row is visible after commit.
        with session.begin():
            exited = repo.mark_exited(
                session,
                entry.id,
                outputs={"verdict": "ok", "count": 3},
            )

    assert exited.id == entry.id
    assert exited.status == "exited"
    assert exited.exited_at is not None
    assert exited.outputs == {"verdict": "ok", "count": 3}

    # Inputs survive the exit transition untouched.
    assert exited.inputs == {"x": 1}

    # Read back via a brand-new session to confirm bytes-on-disk match.
    with project.session_factory() as session:
        fetched = repo.get(session, entry.id)
    assert fetched is not None
    assert fetched.status == "exited"
    assert fetched.outputs == {"verdict": "ok", "count": 3}
    assert fetched.exited_at == exited.exited_at


def test_states_repo_mark_exited_raises_when_row_missing(project: Project) -> None:
    """``mark_exited`` on an unknown PK raises ``LookupError``."""
    repo = StatesRepo()
    with project.session_factory() as session, pytest.raises(LookupError), session.begin():
        repo.mark_exited(session, "no-such-id", outputs={})


# ---------------------------------------------------------------------------
# Terminal-write compare-and-swap (issue #97)
# ---------------------------------------------------------------------------


def _open_entry(project: Project, run_id: str, state_id: str = "draft") -> str:
    """Create one ``entered`` state-entry and return its PK.

    A tiny helper so each CAS test starts from the same well-defined
    precondition: exactly one open row in ``status='entered'``.
    """
    repo = StatesRepo()
    with project.session_factory() as session, session.begin():
        entry = repo.create(
            session,
            run_id=run_id,
            state_id=state_id,
            inputs={},
            entry_seq=1,
        )
    return entry.id


def test_mark_exited_second_write_is_rejected_and_preserves_first(
    project: Project, run_id: str
) -> None:
    """A second ``mark_exited`` on an already-exited row raises and is a no-op.

    The compare-and-swap requires the prior status to be ``entered``; once
    the row is ``exited`` the guard misses, so the repeated terminal write is
    rejected with :class:`StateTransitionError` instead of stomping the
    already-persisted outputs.
    """
    repo = StatesRepo()
    pk = _open_entry(project, run_id)

    with project.session_factory() as session, session.begin():
        repo.mark_exited(session, pk, outputs={"verdict": "first"})

    # Second exit attempt: the guard no longer matches ``entered``.
    with (
        project.session_factory() as session,
        pytest.raises(StateTransitionError) as excinfo,
        session.begin(),
    ):
        repo.mark_exited(session, pk, outputs={"verdict": "second"})
    assert excinfo.value.expected == "entered"
    assert excinfo.value.actual == "exited"

    # The first outcome survived untouched: no silent overwrite.
    with project.session_factory() as session:
        fetched = repo.get(session, pk)
    assert fetched is not None
    assert fetched.status == "exited"
    assert fetched.outputs == {"verdict": "first"}


def test_mark_exited_rejects_invalid_faulted_to_exited_transition(
    project: Project, run_id: str
) -> None:
    """An invalid ``faulted -> exited`` transition is rejected, not accepted.

    This is the exact audit-history loss the issue called out: a faulted row
    must not be quietly flipped to ``exited`` (which would erase the fault).
    The CAS guard rejects it because the prior status is ``faulted``, not
    ``entered``.
    """
    repo = StatesRepo()
    pk = _open_entry(project, run_id)

    with project.session_factory() as session, session.begin():
        repo.mark_faulted(session, pk, reason="boom")

    with (
        project.session_factory() as session,
        pytest.raises(StateTransitionError) as excinfo,
        session.begin(),
    ):
        repo.mark_exited(session, pk, outputs={"verdict": "ok"})
    assert excinfo.value.actual == "faulted"

    # The fault narrative is intact: nothing was overwritten.
    with project.session_factory() as session:
        fetched = repo.get(session, pk)
    assert fetched is not None
    assert fetched.status == "faulted"
    assert fetched.outputs == {"error": "boom"}


def test_mark_faulted_round_trips_and_second_fault_is_rejected(
    project: Project, run_id: str
) -> None:
    """``mark_faulted`` stashes the reason; a repeated fault is rejected.

    The happy path stamps ``faulted`` + ``exited_at`` and stows the reason
    under ``outputs.error``. A second fault (or a fault after exit) misses the
    ``entered`` guard and raises, so the original failure narrative cannot be
    clobbered by a late duplicate.
    """
    repo = StatesRepo()
    pk = _open_entry(project, run_id)

    with project.session_factory() as session, session.begin():
        faulted = repo.mark_faulted(session, pk, reason="first failure")
    assert faulted.status == "faulted"
    assert faulted.exited_at is not None
    assert faulted.outputs == {"error": "first failure"}

    with (
        project.session_factory() as session,
        pytest.raises(StateTransitionError) as excinfo,
        session.begin(),
    ):
        repo.mark_faulted(session, pk, reason="second failure")
    assert excinfo.value.actual == "faulted"

    with project.session_factory() as session:
        fetched = repo.get(session, pk)
    assert fetched is not None
    assert fetched.outputs == {"error": "first failure"}


def test_mark_faulted_raises_when_row_missing(project: Project) -> None:
    """``mark_faulted`` on an unknown PK raises ``LookupError`` (not CAS)."""
    repo = StatesRepo()
    with project.session_factory() as session, pytest.raises(LookupError), session.begin():
        repo.mark_faulted(session, "no-such-id", reason="boom")


# ---------------------------------------------------------------------------
# TransitionsRepo.create round-trip
# ---------------------------------------------------------------------------


def test_transitions_repo_create_persists_predicate_true(
    project: Project, run_id: str
) -> None:
    """A True ``predicate_result`` survives the STRICT-mode INTEGER round-trip."""
    states = StatesRepo()
    transitions = TransitionsRepo()
    with project.session_factory() as session:
        with session.begin():
            src = states.create(
                session,
                run_id=run_id,
                state_id="draft",
                inputs={},
                entry_seq=1,
            )

        with session.begin():
            trans = transitions.create(
                session,
                run_id=run_id,
                from_state_pk=src.id,
                to_state_id="end",
                kind="deterministic",
                predicate="x == 1",
                predicate_result=True,
            )

    assert isinstance(trans, TransitionRecord)
    assert trans.run_id == run_id
    assert trans.from_state_id == src.id
    assert trans.to_state_id == "end"
    assert trans.kind == "deterministic"
    assert trans.predicate == "x == 1"
    # The bool must come back as ``True``, not the underlying ``1`` int.
    assert trans.predicate_result is True
    assert isinstance(trans.predicate_result, bool)
    assert trans.decided_at


def test_transitions_repo_create_persists_predicate_false(
    project: Project, run_id: str
) -> None:
    """A False ``predicate_result`` is distinguishable from ``None``."""
    states = StatesRepo()
    transitions = TransitionsRepo()
    with project.session_factory() as session, session.begin():
        src = states.create(
            session,
            run_id=run_id,
            state_id="draft",
            inputs={},
            entry_seq=1,
        )
        trans = transitions.create(
            session,
            run_id=run_id,
            from_state_pk=src.id,
            to_state_id="end",
            kind="deterministic",
            predicate="x == 2",
            predicate_result=False,
        )

    assert trans.predicate_result is False
    assert isinstance(trans.predicate_result, bool)


def test_transitions_repo_create_persists_predicate_none_for_always_kind(
    project: Project, run_id: str
) -> None:
    """``always`` / ``otherwise`` kinds have ``predicate_result=None``."""
    states = StatesRepo()
    transitions = TransitionsRepo()
    with project.session_factory() as session, session.begin():
        src = states.create(
            session,
            run_id=run_id,
            state_id="draft",
            inputs={},
            entry_seq=1,
        )
        trans = transitions.create(
            session,
            run_id=run_id,
            from_state_pk=src.id,
            to_state_id="end",
            kind="always",
            predicate=None,
            predicate_result=None,
        )

    assert trans.predicate_result is None
    assert trans.predicate is None
    assert trans.kind == "always"


# ---------------------------------------------------------------------------
# next_entry_seq monotonicity
# ---------------------------------------------------------------------------


def test_next_entry_seq_starts_at_one_for_empty_run(
    project: Project, run_id: str
) -> None:
    """Fresh run with zero entries returns ``1``."""
    repo = StatesRepo()
    with project.session_factory() as session:
        assert repo.next_entry_seq(session, run_id) == 1


def test_next_entry_seq_increments_per_run(project: Project, run_id: str) -> None:
    """Each create followed by next_entry_seq returns N+1."""
    repo = StatesRepo()
    with project.session_factory() as session:
        seqs: list[int] = []
        for i in range(3):
            with session.begin():
                seq = repo.next_entry_seq(session, run_id)
                seqs.append(seq)
                repo.create(
                    session,
                    run_id=run_id,
                    state_id="draft" if i == 0 else f"state_{i}",
                    inputs={"i": i},
                    entry_seq=seq,
                )

    assert seqs == [1, 2, 3]

    # And one more lookup after the three writes should return 4.
    with project.session_factory() as session:
        assert repo.next_entry_seq(session, run_id) == 4


def test_next_entry_seq_isolated_between_runs(project: Project, run_id: str) -> None:
    """``next_entry_seq`` is scoped per ``run_id`` — siblings never collide."""
    repo = StatesRepo()

    # Start a second independent run sharing the same project + spec.
    second_spec = project.register_spec(_make_spec(state_id="other"))
    second_run = project.start_run(second_spec.spec.id)

    # Insert two entries against the first run.
    with project.session_factory() as session, session.begin():
        for _i in range(2):
            repo.create(
                session,
                run_id=run_id,
                state_id="draft",
                inputs={},
                entry_seq=repo.next_entry_seq(session, run_id),
            )

    # The second run's counter is independent.
    with project.session_factory() as session:
        assert repo.next_entry_seq(session, second_run.id) == 1
        assert repo.next_entry_seq(session, run_id) == 3


# ---------------------------------------------------------------------------
# RunsRepo.state_tree
# ---------------------------------------------------------------------------


def _make_linear_run(
    project: Project, run_id: str, edges: list[tuple[str, str]]
) -> dict[str, str]:
    """Materialise a linear chain of state entries + transitions for ``run_id``.

    ``edges`` is a list of ``(from_state_name, to_state_name)`` pairs.
    The first entry of the first edge is created as ``entry_seq=1``;
    each subsequent destination becomes a fresh entry with the next
    monotonic seq, and a transition row is written from the most recent
    source-entry PK to the destination's state name.

    Returns the map ``state_name -> entry PK`` for the most recent entry
    of each state — handy for assertions further down.
    """
    states = StatesRepo()
    transitions = TransitionsRepo()
    by_name: dict[str, str] = {}
    with project.session_factory() as session, session.begin():
        for index, (src_name, dst_name) in enumerate(edges):
            if index == 0:
                src_seq = states.next_entry_seq(session, run_id)
                src = states.create(
                    session,
                    run_id=run_id,
                    state_id=src_name,
                    inputs={"step": index},
                    entry_seq=src_seq,
                )
                states.mark_exited(session, src.id, outputs={"ok": True})
                by_name[src_name] = src.id
            src_pk = by_name[src_name]

            dst_seq = states.next_entry_seq(session, run_id)
            dst = states.create(
                session,
                run_id=run_id,
                state_id=dst_name,
                inputs={"step": index + 1},
                entry_seq=dst_seq,
            )
            states.mark_exited(session, dst.id, outputs={"ok": True})
            by_name[dst_name] = dst.id

            transitions.create(
                session,
                run_id=run_id,
                from_state_pk=src_pk,
                to_state_id=dst_name,
                kind="always",
                predicate=None,
                predicate_result=None,
            )
    return by_name


def _walk(node: Any) -> list[str]:
    """Pre-order walk yielding ``state_id`` for each node."""
    order: list[str] = [node.state_id]
    for child in node.children:
        order.extend(_walk(child))
    return order


def test_state_tree_returns_none_for_run_with_no_entries(
    project: Project, run_id: str
) -> None:
    """A run with zero state entries returns ``None`` (not an empty node)."""
    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)
    assert tree is None


def test_state_tree_reflects_linear_edges(project: Project, run_id: str) -> None:
    """A → B → C entries plus matching transitions yield a 3-deep linear tree."""
    by_name = _make_linear_run(
        project, run_id, [("draft", "review"), ("review", "publish")]
    )

    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)

    assert tree is not None
    assert tree.state_id == "draft"
    assert tree.entry_id == by_name["draft"]
    assert tree.entry_seq == 1
    # Status is round-tripped from the exited entry.
    assert tree.status == "exited"
    assert tree.outputs == {"ok": True}

    # The walk must be the literal ``draft → review → publish`` chain.
    assert _walk(tree) == ["draft", "review", "publish"]

    # Each non-leaf has exactly one child; the leaf has none.
    assert len(tree.children) == 1
    review = tree.children[0]
    assert review.state_id == "review"
    assert review.entry_id == by_name["review"]
    assert len(review.children) == 1
    publish = review.children[0]
    assert publish.state_id == "publish"
    assert publish.entry_id == by_name["publish"]
    assert publish.children == []


def test_state_tree_only_includes_states_reached_by_transitions(
    project: Project, run_id: str
) -> None:
    """A state entry with no inbound transition is not stitched into the tree."""
    states = StatesRepo()
    transitions = TransitionsRepo()
    with project.session_factory() as session, session.begin():
        root = states.create(
            session,
            run_id=run_id,
            state_id="draft",
            inputs={},
            entry_seq=states.next_entry_seq(session, run_id),
        )
        states.mark_exited(session, root.id, outputs={})
        # An "orphan" entry with no transition row pointing at it.
        orphan = states.create(
            session,
            run_id=run_id,
            state_id="orphan",
            inputs={},
            entry_seq=states.next_entry_seq(session, run_id),
        )
        states.mark_exited(session, orphan.id, outputs={})

        # And a child that IS reached via a transition.
        child = states.create(
            session,
            run_id=run_id,
            state_id="review",
            inputs={},
            entry_seq=states.next_entry_seq(session, run_id),
        )
        states.mark_exited(session, child.id, outputs={})
        transitions.create(
            session,
            run_id=run_id,
            from_state_pk=root.id,
            to_state_id="review",
            kind="always",
            predicate=None,
            predicate_result=None,
        )

    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)

    assert tree is not None
    # Root is the first-entered state, regardless of how many siblings exist.
    assert tree.state_id == "draft"
    # Only the transition-linked child appears under draft; the orphan
    # entry is NOT pulled in by name-matching alone.
    assert [c.state_id for c in tree.children] == ["review"]
