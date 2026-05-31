"""Research-with-retries example for ctxr.fsm.

This example demonstrates several FSM features in concert against the
real :class:`ctxr.fsm.sqlite.Project` substrate:

* A bounded **loop** state (``research``) capped at 10 iterations whose
  body produces a ``converged`` flag — the standard "keep working until
  the worker says it's done" pattern.
* A linear worker state (``cite``) that fans out the aggregated loop
  output into structured citations.
* A **judgement** transition out of ``review`` that branches the run
  into either ``publish`` (terminal) or ``retry_research`` (loop back).
* A ``retry_research`` worker whose only purpose is to unconditionally
  transition back to ``research`` — i.e. the retry-on-failure branch.
* A second, smaller demo that shows how the **journal** can be
  recovered after a simulated crash by inspecting + discarding the
  newest unfinalised txn, mirroring what ``fsm.recover_journal`` does
  over MCP.

Workers are **simulated**: instead of dispatching real agents through
MCP, we feed each state-entry a pre-baked output dict from
:data:`SIMULATED_OUTPUTS`. The shape and order of those outputs are
the only thing this example "models" — a real run would swap the
simulation table for an actual MCP worker dispatch (see the project
README for that wiring). The point here is to exercise the FSM
substrate end-to-end deterministically.

Run with::

    uv run python examples/research_with_retries.py

The script prints (in order):

1. The state-entry tree of the main run.
2. The event log of the main run.
3. The final published location pulled from the last state's outputs.
4. The recovery demonstration showing the journal being cleaned and
   the run being unpaused.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ctxr.fsm import (
    EventKind,
    FsmSpec,
    Loop,
    ResponseSchema,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import RunTable

# ---------------------------------------------------------------------------
# Simulated worker outputs
# ---------------------------------------------------------------------------

# A real engine would dispatch a Worker (via MCP) and parse a structured
# response. To keep the example self-contained + deterministic we hand
# the engine pre-baked outputs keyed by (state_id, attempt). For loop
# states the attempt is the iteration index (1-based); for non-loop
# states it counts re-entries (1-based again so the retry path works).
SIMULATED_OUTPUTS: dict[tuple[str, int], dict[str, Any]] = {
    # ── First pass through `research` (loop runs to convergence) ───────
    ("research", 1): {
        "sources": [{"url": "arxiv:2401.xxxx", "summary": "paper 1"}],
        "converged": False,
    },
    ("research", 2): {
        "sources": [{"url": "arxiv:2402.xxxx", "summary": "paper 2"}],
        "converged": False,
    },
    ("research", 3): {
        "sources": [{"url": "arxiv:2403.xxxx", "summary": "paper 3"}],
        "converged": True,
    },
    # ── First pass through cite + review (reviewer rejects) ────────────
    ("cite", 1): {
        "citations": [
            {"paper_id": "1", "text": "cited 1"},
            {"paper_id": "2", "text": "cited 2"},
            {"paper_id": "3", "text": "cited 3"},
        ],
    },
    ("review", 1): {
        "verdict": "retry",
        "criteria": "needs more sources",
        "evidence_required": False,
    },
    ("retry_research", 1): {
        "reason": "reviewer requested more sources",
    },
    # ── Second pass through `research` (converges immediately) ─────────
    ("research", 4): {
        "sources": [{"url": "arxiv:2404.xxxx", "summary": "paper 4"}],
        "converged": True,
    },
    # ── Second pass through cite + review (reviewer accepts) ───────────
    ("cite", 2): {
        "citations": [
            {"paper_id": "1", "text": "cited 1"},
            {"paper_id": "2", "text": "cited 2"},
            {"paper_id": "3", "text": "cited 3"},
            {"paper_id": "4", "text": "cited 4"},
        ],
    },
    ("review", 2): {
        "verdict": "accept",
        "criteria": "sufficient",
        "evidence_required": False,
    },
    # ── Publish (terminal) ─────────────────────────────────────────────
    ("publish", 1): {
        "published_at": "2026-05-30T12:00:00Z",
        "location": "docs/research.md",
    },
}


# ---------------------------------------------------------------------------
# FSM spec
# ---------------------------------------------------------------------------


def build_spec() -> FsmSpec:
    """Build the five-state research FSM spec.

    Shape::

        research (loop, max 10, done=converged)
            └── always ──▶ cite
                            └── always ──▶ review
                                            ├── judgement(publish)        ──▶ publish (terminal)
                                            └── judgement(retry_research) ──▶ retry_research
                                                                              └── always ──▶ research
    """
    loop_response_schema = ResponseSchema(
        schema={
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["url", "summary"],
                    },
                },
                "converged": {"type": "boolean"},
            },
            "required": ["sources", "converged"],
        },
    )

    research_state = State(
        id="research",
        purpose="Iteratively gather sources until the worker reports convergence.",
        loop=Loop(
            worker=Worker(
                role="researcher",
                prompt_template=(
                    "Continue gathering research sources. Return JSON "
                    "matching the response schema; set `converged: true` "
                    "when no new sources are worth adding."
                ),
                inputs=["topic"],
                response_schema=loop_response_schema,
            ),
            max_iterations=10,
            done_field="converged",
        ),
        outputs=["sources", "converged"],
        transitions=[Transition(to="cite", when="always")],
    )

    cite_state = State(
        id="cite",
        purpose="Turn the aggregated research output into citations.",
        worker=Worker(
            role="citer",
            prompt_template="Produce a structured citations list from the research.",
            inputs=["sources"],
        ),
        outputs=["citations"],
        transitions=[Transition(to="review", when="always")],
    )

    review_state = State(
        id="review",
        purpose="Decide whether the draft is ready to publish or needs more research.",
        worker=Worker(
            role="reviewer",
            prompt_template="Review the citations and choose `accept` or `retry`.",
            inputs=["citations"],
        ),
        outputs=["verdict", "criteria", "evidence_required"],
        transitions=[
            Transition(
                to="publish",
                when={
                    "kind": "judgement",
                    "criteria": "verdict == 'accept'",
                    "evidence_required": False,
                },
            ),
            Transition(
                to="retry_research",
                when={
                    "kind": "judgement",
                    "criteria": "verdict == 'retry'",
                    "evidence_required": False,
                },
            ),
        ],
    )

    retry_state = State(
        id="retry_research",
        purpose="Acknowledge the reviewer's request before looping back to research.",
        worker=Worker(
            role="retry_dispatcher",
            prompt_template="Note why the reviewer rejected the draft.",
            inputs=["criteria"],
        ),
        outputs=["reason"],
        transitions=[Transition(to="research", when="always")],
    )

    publish_state = State(
        id="publish",
        purpose="Publish the final draft. Terminal — no outbound transitions.",
        worker=Worker(
            role="publisher",
            prompt_template="Persist the final draft and report where it landed.",
            inputs=["citations"],
        ),
        outputs=["published_at", "location"],
        transitions=[],
    )

    return FsmSpec(
        id="research_with_retries",
        version=1,
        entry="research",
        states=[
            research_state,
            cite_state,
            review_state,
            retry_state,
            publish_state,
        ],
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


# We tag every engine-side event we emit with the same producer the
# Project facade uses for its own ``run_started`` event so the
# resulting timeline is internally consistent.
_RUNTIME_PRODUCER_KIND = "engine"
_RUNTIME_PRODUCER_NAME = "fsm.runtime"


class SimulatedDriver:
    """Walk an FSM run end-to-end with hard-coded worker outputs.

    The driver is deliberately written as a small class so the example
    stays readable: instance state tracks per-state attempt counters
    plus the rolling outputs that subsequent states consume as inputs.

    A real engine would replace :meth:`_simulate_worker` with an MCP
    dispatch + structured-response parse; everything else (journal,
    state rows, event emission, transition resolution) would stay the
    same.
    """

    def __init__(self, project: Project, run_id: str, spec: FsmSpec) -> None:
        self.project = project
        self.run_id = run_id
        self.spec = spec
        self.producer_id = self._ensure_runtime_producer()

        # Rolling history of finalised outputs per state — the
        # ``cite`` worker needs to see ``research`` output, etc.
        self.history: dict[str, list[dict[str, Any]]] = {}
        # Per-state attempt counter used to index SIMULATED_OUTPUTS.
        self.attempts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run_to_terminal(self) -> str:
        """Drive the FSM from ``entry`` until a state with no transitions.

        Returns the final ``state_id`` that ended the run (the state
        that had no outbound transitions).
        """
        current_state_id = self.spec.entry
        while True:
            state = self.spec.get_state(current_state_id)

            if state.loop is not None:
                outputs = self._drive_loop_state(state)
            else:
                outputs = self._drive_worker_state(state)

            # Stash this state's final outputs in the rolling history so
            # downstream states can use them as inputs.
            self.history.setdefault(state.id, []).append(outputs)

            next_state_id = self._pick_next_state(state, outputs)
            if next_state_id is None:
                # Terminal — no outbound transitions.
                self._mark_run_completed(state.id)
                return state.id
            current_state_id = next_state_id

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_runtime_producer(self) -> str:
        with self.project.session_factory() as session, session.begin():
            producer = self.project.producers.upsert(
                session,
                kind=_RUNTIME_PRODUCER_KIND,
                name=_RUNTIME_PRODUCER_NAME,
            )
        return producer.id

    def _simulate_worker(self, state_id: str) -> dict[str, Any]:
        """Look up the next simulated output for ``state_id``.

        Increments the per-state attempt counter so re-entries (e.g.
        the second pass through ``research``) pick up the next bucket
        in :data:`SIMULATED_OUTPUTS`.
        """
        attempt = self.attempts.get(state_id, 0) + 1
        self.attempts[state_id] = attempt
        key = (state_id, attempt)
        if key not in SIMULATED_OUTPUTS:
            raise KeyError(
                f"no simulated output for {key!r}; check SIMULATED_OUTPUTS "
                "and the spec for an unexpected re-entry"
            )
        return SIMULATED_OUTPUTS[key]

    def _open_state_entry(
        self,
        state_id: str,
        inputs: dict[str, Any],
    ) -> tuple[str, str]:
        """Open a journal txn + insert a state-entry row.

        Returns ``(txn_id, state_pk)`` so the caller can finalise the
        txn and mark the entry exited later in the lifecycle.
        """
        with self.project.session_factory() as session, session.begin():
            txn = self.project.journal.open(session, run_id=self.run_id)
            txn_id = txn.id

        with self.project.session_factory() as session, session.begin():
            entry_seq = self.project.states.next_entry_seq(session, self.run_id)
            state_row = self.project.states.create(
                session,
                run_id=self.run_id,
                state_id=state_id,
                inputs=inputs,
                entry_seq=entry_seq,
            )
            self.project.events.emit(
                session,
                producer_id=self.producer_id,
                kind=EventKind.state_entered.value,
                payload={
                    "run_id": self.run_id,
                    "state": state_id,
                    "entry_seq": state_row.entry_seq,
                },
                run_id=self.run_id,
            )

        self._set_current_state(state_id)
        return txn_id, state_row.id

    def _close_state_entry(
        self,
        *,
        state_id: str,
        state_pk: str,
        outputs: dict[str, Any],
        txn_id: str,
    ) -> None:
        """Mark the state exited, finalise its journal txn, emit the events."""
        with self.project.session_factory() as session, session.begin():
            self.project.states.mark_exited(session, state_pk, outputs=outputs)
            self.project.events.emit(
                session,
                producer_id=self.producer_id,
                kind=EventKind.state_exited.value,
                payload={
                    "run_id": self.run_id,
                    "state": state_id,
                },
                run_id=self.run_id,
            )

        with self.project.session_factory() as session, session.begin():
            self.project.journal.mark_ready(
                session,
                txn_id=txn_id,
                staged_writes=[{"state": state_id}],
            )
            self.project.journal.finalise(session, txn_id=txn_id)

    def _set_current_state(self, state_id: str) -> None:
        with self.project.session_factory() as session, session.begin():
            row = session.get(RunTable, self.run_id)
            if row is None:
                raise LookupError(f"run vanished mid-drive: {self.run_id!r}")
            row.current_state = state_id

    # ── Worker-state drive ────────────────────────────────────────────

    def _drive_worker_state(self, state: State) -> dict[str, Any]:
        """Drive one single-shot worker state and return its outputs."""
        inputs = self._inputs_for(state)
        txn_id, state_pk = self._open_state_entry(state.id, inputs)
        outputs = self._simulate_worker(state.id)
        self._close_state_entry(
            state_id=state.id,
            state_pk=state_pk,
            outputs=outputs,
            txn_id=txn_id,
        )
        return outputs

    # ── Loop-state drive ──────────────────────────────────────────────

    def _drive_loop_state(self, state: State) -> dict[str, Any]:
        """Drive a loop state until ``done_field`` flips or the cap is hit.

        Returns the *aggregated* outputs across every iteration: the
        ``sources`` lists are concatenated, the ``converged`` flag
        reflects the final iteration's value. A real engine uses
        :func:`ctxr.fsm.core.aggregate_loop_outputs` for this; we
        roll a small in-line aggregator to keep the example dependency
        surface small.
        """
        assert state.loop is not None
        inputs = self._inputs_for(state)
        txn_id, state_pk = self._open_state_entry(state.id, inputs)

        aggregated_sources: list[dict[str, Any]] = []
        last_converged = False
        iterations_run = 0

        for iteration_n in range(1, state.loop.max_iterations + 1):
            iteration_outputs = self._simulate_worker(state.id)
            iterations_run = iteration_n
            sources = iteration_outputs.get("sources") or []
            if isinstance(sources, list):
                aggregated_sources.extend(sources)
            last_converged = bool(iteration_outputs.get(state.loop.done_field))

            # Persist the per-iteration artifact so the journal carries
            # the full audit trail of what the loop body produced.
            prompt = state.loop.worker.prompt_template
            with self.project.session_factory() as session, session.begin():
                self.project.worker_artifacts.create(
                    session,
                    run_id=self.run_id,
                    state_pk=state_pk,
                    iteration_n=iteration_n,
                    prompt_text=prompt,
                    prompt_hash=_sha256(prompt),
                    output=iteration_outputs,
                    validated=True,
                )

            if last_converged:
                break

        aggregated = {
            "sources": aggregated_sources,
            "converged": last_converged,
            "iterations_run": iterations_run,
        }
        self._close_state_entry(
            state_id=state.id,
            state_pk=state_pk,
            outputs=aggregated,
            txn_id=txn_id,
        )
        return aggregated

    # ── Transition resolution ─────────────────────────────────────────

    def _pick_next_state(
        self,
        state: State,
        outputs: dict[str, Any],
    ) -> str | None:
        """Resolve the next state, emitting a ``transition_taken`` event.

        Returns ``None`` when ``state`` has no transitions (terminal).
        For ``always`` transitions we take the first one. For
        ``judgement`` transitions we inspect ``outputs['verdict']`` to
        pick the matching target — that mirrors how the engine's
        ``resolve_transition`` resolves a judgement guard from the
        ``judgement_pick`` argument.
        """
        if not state.transitions:
            return None

        # Determine the pick. For judgement guards we map verdict ->
        # target. Anything else is "always" / "otherwise" / deterministic
        # and we just take the first transition (which is what `always`
        # actually means in this spec).
        chosen: Transition | None = None
        for transition in state.transitions:
            when = transition.when
            if when == "always":
                chosen = transition
                break
            if isinstance(when, dict) and when.get("kind") == "judgement":
                verdict = outputs.get("verdict")
                if verdict == "accept" and transition.to == "publish":
                    chosen = transition
                    break
                if verdict == "retry" and transition.to == "retry_research":
                    chosen = transition
                    break

        if chosen is None:
            raise RuntimeError(
                f"no transition matched for state {state.id!r} with outputs={outputs!r}"
            )

        # Look up the source state-entry's PK (the most recent entry for
        # this FSM state) so we can attach the transition to it.
        with self.project.session_factory() as session:
            entries = self.project.states.list_by_run(session, self.run_id)
        source_pk = next(
            row.id for row in reversed(entries) if row.state_id == state.id
        )

        when_kind = (
            "always"
            if chosen.when == "always"
            else ("otherwise" if chosen.when == "otherwise" else "judgement")
        )
        with self.project.session_factory() as session, session.begin():
            self.project.transitions.create(
                session,
                run_id=self.run_id,
                from_state_pk=source_pk,
                to_state_id=chosen.to,
                kind=when_kind,
                predicate=None,
                predicate_result=None,
            )
            self.project.events.emit(
                session,
                producer_id=self.producer_id,
                kind=EventKind.transition_taken.value,
                payload={
                    "run_id": self.run_id,
                    "from": state.id,
                    "to": chosen.to,
                    "kind": when_kind,
                },
                run_id=self.run_id,
            )

        return chosen.to

    # ── Inputs assembly ───────────────────────────────────────────────

    def _inputs_for(self, state: State) -> dict[str, Any]:
        """Build the inputs bag for ``state`` from the rolling history.

        We just pull each declared input by name from the most recent
        outputs of any prior state that produced it — that is enough
        for this example. A real engine would consult the brief's
        declared inputs list (see :class:`ctxr.fsm.core.Brief`).
        """
        worker = state.worker if state.worker is not None else (
            state.loop.worker if state.loop is not None else None
        )
        if worker is None:
            return {}
        inputs: dict[str, Any] = {}
        for name in worker.inputs:
            # Walk the history newest-first looking for the named key.
            for outputs_list in reversed(list(self.history.values())):
                if outputs_list and name in outputs_list[-1]:
                    inputs[name] = outputs_list[-1][name]
                    break
            else:
                # Default to a topic placeholder for the entry state so
                # the example does not crash on the very first iter.
                if name == "topic":
                    inputs[name] = "ctxr-fsm: the research-with-retries demo"
        return inputs

    # ── Run lifecycle ─────────────────────────────────────────────────

    def _mark_run_completed(self, final_state_id: str) -> None:
        with self.project.session_factory() as session, session.begin():
            self.project.runs.update_status(
                session,
                run_id=self.run_id,
                status="completed",
                ended_at=None,
                verdict="ok",
            )
            self.project.events.emit(
                session,
                producer_id=self.producer_id,
                kind=EventKind.run_completed.value,
                payload={
                    "run_id": self.run_id,
                    "final_state": final_state_id,
                },
                run_id=self.run_id,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    """Tiny wrapper so the worker-artifact hash is computed inline."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_state_tree(node: Any, indent: int = 0) -> Iterator[str]:
    """Yield indented ``state_id (entry_seq=N, status=...)`` lines."""
    if node is None:
        return
    pad = "  " * indent
    yield (
        f"{pad}- {node.state_id} "
        f"(entry_seq={node.entry_seq}, status={node.status}, "
        f"iteration_n={node.iteration_n})"
    )
    for child in node.children:
        yield from _format_state_tree(child, indent + 1)


def _print_state_tree(project: Project, run_id: str) -> None:
    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run_id)
    print("State tree:")
    if tree is None:
        print("  <empty>")
        return
    for line in _format_state_tree(tree):
        print(line)


def _print_event_log(project: Project, run_id: str) -> int:
    with project.session_factory() as session:
        events = list(project.runs.events(session, run_id))
    print(f"Event log ({len(events)} events):")
    for event in events:
        payload_str = json.dumps(event.payload, sort_keys=True)
        if len(payload_str) > 80:
            payload_str = payload_str[:77] + "..."
        print(f"  [{event.seq:>3}] {event.kind:<24} {payload_str}")
    return len(events)


# ---------------------------------------------------------------------------
# Main demo: retry-then-converge
# ---------------------------------------------------------------------------


def demo_main_run() -> dict[str, Any]:
    """Drive the FSM through a full retry-then-converge cycle.

    Returns a small summary dict that the wrapping script asserts on.
    """
    spec = build_spec()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "research_demo.sqlite3"
        with Project.open(db_path) as project:
            registered = project.register_spec(spec)
            run = project.start_run(registered.spec.id, args={"topic": "demo"})

            driver = SimulatedDriver(project, run.id, spec)
            final_state_id = driver.run_to_terminal()

            print("=" * 72)
            print("MAIN RUN — retry-then-converge")
            print("=" * 72)
            _print_state_tree(project, run.id)
            print()
            event_count = _print_event_log(project, run.id)
            print()

            # Pull the published artefact out of the last state's outputs.
            with project.session_factory() as session:
                entries = project.states.list_by_run(session, run.id)
            publish_entry = next(
                entry for entry in reversed(entries) if entry.state_id == "publish"
            )
            published_location = publish_entry.outputs.get("location")
            print(f"Final published location: {published_location}")

            # Sanity check on the journal cleanliness — every txn we
            # opened should be finalised (and therefore inspect returns
            # None).
            with project.session_factory() as session:
                pending = project.journal.inspect(session, run_id=run.id)
            runs_clean = pending is None
            print(f"Journal clean after main run: {runs_clean}")

            return {
                "final_state": final_state_id,
                "event_count": event_count,
                "runs_clean": runs_clean,
                "published_location": published_location,
            }


# ---------------------------------------------------------------------------
# Recovery demo: inject a pending JournalTxn, then discard it
# ---------------------------------------------------------------------------


def demo_recovery() -> bool:
    """Simulate a crash mid-state then recover via ``journal.discard``.

    Steps:

    1. Open a fresh Project + spec + run.
    2. Drive exactly one state to completion so the run is past entry.
    3. Manually open a new ``pending`` journal txn — i.e. the engine
       went to start the next state, opened a journal row, then
       crashed before mark_ready.
    4. Show ``journal.inspect`` returns that pending row.
    5. Run the equivalent of ``fsm.recover_journal('discard')`` — look
       up the newest unfinalised row, discard it. Confirm the journal
       is clean afterwards.
    6. "Resume" the run by flipping its status back to ``in_progress``
       (which is what ``project.runs.update_status`` would do for an
       operator-triggered resume).
    """
    spec = build_spec()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "recovery_demo.sqlite3"
        with Project.open(db_path) as project:
            registered = project.register_spec(spec)
            run = project.start_run(registered.spec.id, args={"topic": "recovery"})

            driver = SimulatedDriver(project, run.id, spec)

            # Drive exactly one state — the entry loop state. We can
            # not call ``run_to_terminal`` because we want to stop part
            # way through so the simulated crash is visible.
            entry_state = spec.get_state(spec.entry)
            driver._drive_loop_state(entry_state)

            print("=" * 72)
            print("RECOVERY DEMO — simulated crash + journal cleanup")
            print("=" * 72)
            print("Driven 1 state (research). Now simulating a crash.")

            # ── 1. Inject a pending JournalTxn ────────────────────────
            with project.session_factory() as session, session.begin():
                pending_txn = project.journal.open(session, run_id=run.id)
            print(
                f"Injected pending journal txn: id={pending_txn.id}, "
                f"status={pending_txn.status}"
            )

            # ── 2. Show inspect surfaces the in-flight txn ────────────
            with project.session_factory() as session:
                inspected = project.journal.inspect(session, run_id=run.id)
            assert inspected is not None
            print(
                f"journal.inspect returns: id={inspected.id}, "
                f"status={inspected.status}"
            )

            # ── 3. Mark the run as faulted so the operator path is
            #     realistic — a real crash leaves the run in a non-
            #     ``in_progress`` state and the operator decides what to
            #     do next.
            with project.session_factory() as session, session.begin():
                project.runs.update_status(
                    session,
                    run_id=run.id,
                    status="faulted",
                    ended_at=None,
                    verdict=None,
                )
            faulted = project.get_run(run.id)
            assert faulted is not None
            print(f"Run status after simulated crash: {faulted.status}")

            # ── 4. Run the equivalent of fsm.recover_journal("discard")
            #     — inspect newest unfinalised + discard. This is
            #     exactly what ctxr.fsm.mcp.tools_events.recover_journal
            #     does internally; we inline it here because the example
            #     deliberately avoids the MCP client.
            with project.session_factory() as session, session.begin():
                to_recover = project.journal.inspect(session, run_id=run.id)
                if to_recover is not None:
                    project.journal.discard(session, txn_id=to_recover.id)
                    recovered_action = "discard"
                else:
                    recovered_action = "noop"
            print(f"recover_journal action applied: {recovered_action}")

            with project.session_factory() as session:
                post_recovery = project.journal.inspect(session, run_id=run.id)
            recovery_demonstrated = post_recovery is None
            print(f"Journal clean after recovery: {recovery_demonstrated}")

            # ── 5. Resume the run — flip status back to in_progress so
            #     a subsequent driver could pick up from the entry
            #     state. We deliberately leave the actual resume drive
            #     out of this demo to keep the recovery story crisp.
            with project.session_factory() as session, session.begin():
                resumed = project.runs.update_status(
                    session,
                    run_id=run.id,
                    status="in_progress",
                    ended_at=None,
                    verdict=None,
                )
            assert resumed is not None
            print(f"Run status after resume: {resumed.status}")

            # Finally print the tree + event log for the recovery run
            # so the artefacts are visible.
            print()
            _print_state_tree(project, run.id)
            print()
            _print_event_log(project, run.id)

            return recovery_demonstrated


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    summary = demo_main_run()
    print()
    recovery_ok = demo_recovery()

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Main run final state:     {summary['final_state']}")
    print(f"Main run event count:     {summary['event_count']}")
    print(f"Main run journal clean:   {summary['runs_clean']}")
    print(f"Published location:       {summary['published_location']}")
    print(f"Recovery demonstrated:    {recovery_ok}")

    assert summary["final_state"] == "publish", "expected publish to be terminal"
    assert summary["runs_clean"], "journal should be clean after main run"
    assert recovery_ok, "recovery should clean the journal"


if __name__ == "__main__":
    main()
