"""Plan → Implement (loop) → QA → Fix → Done example.

This example demonstrates three key FSM features in one self-contained
script:

* The ``Loop`` primitive — the ``implement`` state iterates a bounded
  loop (``max_iterations=4``) and terminates when the worker output's
  ``done`` field flips true.
* ``post_validations`` — the ``plan`` state declares a non-trivial
  guard (``len(commitments) > 0``); the ``qa`` state declares a
  trivially-true one (``len(findings) >= 0``) to show the wiring.
* Conditional transitions — ``qa`` branches to ``fix`` when
  ``verdict == 'NO-GO'`` and to ``done`` otherwise. The first ``qa``
  pass routes to ``fix``; the second routes to ``done``.

The five states are:

1. ``plan``       — worker, produces ``commitments[]``.
2. ``implement``  — loop body, ``done_field='done'``, produces
                    ``findings[]`` of in-progress / done items.
3. ``qa``         — worker, produces ``verdict`` + ``findings[]``.
4. ``fix``        — worker (conditional target), produces ``fixes[]``.
5. ``done``       — terminal, no outputs.

How it runs
-----------

The FSM is driven *in-process* by calling the pure
:func:`ctxr.fsm.core.engine.advance` function directly. We use the
W2 :class:`Project` facade for the SQLite-backed persistence
substrate (state-entry rows, transition rows, the event journal) but
sidestep the W4 MCP tool surface — that surface persists state but
does not yet thread loop-iteration counters across ``commit_outputs``
calls (see ``fsm_commit_outputs`` in :mod:`ctxr.fsm.mcp.tools_runs`),
so a multi-iteration loop driven through the MCP tools would re-enter
iteration 1 on every commit. Driving the engine directly is the
cleanest way to exercise the ``Loop`` primitive end-to-end today.

Worker outputs are *simulated* — a small dispatcher function returns
hard-coded fixtures so the run is deterministic and completes in a few
hundred milliseconds without ever calling a real LLM.

To swap the simulated workers for real MCP-driven sub-agent dispatch:

* Replace the ``simulated_output_for`` body with a function that
  builds a prompt from ``brief.worker.prompt_template`` +
  ``brief.inputs``, spawns the sub-agent (e.g. via the Anthropic Agent
  SDK or a custom MCP client), and parses the structured JSON response
  per ``brief.worker.response_schema``.
* Keep the rest of the driver loop unchanged — the engine is happy
  with any dict that passes the schema check.

Run with::

    uv run python examples/plan_implement_qa_fix.py
"""

from __future__ import annotations

import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ctxr.fsm.core.engine import advance as engine_advance
from ctxr.fsm.core.engine import build_brief
from ctxr.fsm.core.models import (
    Brief,
    EventKind,
    FsmSpec,
    Loop,
    Predicate,
    ResponseSchema,
    RunCtx,
    State,
    Transition,
    Worker,
)
from ctxr.fsm.sqlite import Project
from ctxr.fsm.sqlite.models_core import RunTable
from ctxr.fsm.sqlite.repos_core import _iso_now_ms

# ---------------------------------------------------------------------------
# Spec construction
# ---------------------------------------------------------------------------


def _plan_schema() -> ResponseSchema:
    """JSON Schema for the ``plan`` worker's structured output."""
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "properties": {
                    "commitments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                "expected_evidence": {"type": "string"},
                            },
                            "required": ["id", "description", "expected_evidence"],
                        },
                    },
                },
                "required": ["commitments"],
            }
        }
    )


def _implement_schema() -> ResponseSchema:
    """JSON Schema for one iteration of the ``implement`` loop body.

    The ``done`` field must be declared in ``properties`` because the
    spec validator enforces that ``loop.done_field`` is present in the
    worker's response schema.
    """
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "properties": {
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "commit_id": {"type": "string"},
                                "file": {"type": "string"},
                                "line": {"type": "integer"},
                                "status": {
                                    "type": "string",
                                    "enum": ["in_progress", "done"],
                                },
                            },
                            "required": ["commit_id", "file", "line", "status"],
                        },
                    },
                    "done": {"type": "boolean"},
                },
                "required": ["findings", "done"],
            }
        }
    )


def _qa_schema() -> ResponseSchema:
    """JSON Schema for the ``qa`` worker's structured output."""
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["GO", "NO-GO"]},
                    "findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "commit_id": {"type": "string"},
                                "severity": {"type": "string"},
                                "message": {"type": "string"},
                            },
                            "required": ["commit_id", "severity", "message"],
                        },
                    },
                },
                "required": ["verdict", "findings"],
            }
        }
    )


def _fix_schema() -> ResponseSchema:
    """JSON Schema for the ``fix`` worker's structured output."""
    return ResponseSchema.model_validate(
        {
            "schema": {
                "type": "object",
                "properties": {
                    "fixes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "finding_id": {"type": "integer"},
                                "file_change": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["finding_id", "file_change", "description"],
                        },
                    },
                },
                "required": ["fixes"],
            }
        }
    )


def build_spec() -> FsmSpec:
    """Build the five-state FSM spec for this example.

    Edges:

    * ``plan``      → ``implement``  (always)
    * ``implement`` → ``qa``         (always; fires only when the loop
                                       terminates via ``done=True`` or
                                       ``max_iterations``)
    * ``qa``        → ``fix``        (when ``verdict == 'NO-GO'``)
    * ``qa``        → ``done``       (otherwise)
    * ``fix``       → ``qa``         (always — re-runs QA after each fix)

    ``post_validations`` are declared on ``plan`` (non-trivial) and
    ``qa`` (trivially true) to demonstrate the wiring.
    """
    plan_state = State(
        id="plan",
        purpose="produce the list of commitments to implement",
        worker=Worker(
            role="planner",
            prompt_template="Plan the work and emit commitments[].",
            response_schema=_plan_schema(),
        ),
        outputs=["commitments"],
        post_validations=[Predicate("len(commitments) > 0")],
        transitions=[Transition(to="implement", when="always")],
    )

    implement_state = State(
        id="implement",
        purpose="iterate over commitments until each is done",
        loop=Loop(
            worker=Worker(
                role="implementer",
                prompt_template="Implement the next commitment slice.",
                response_schema=_implement_schema(),
            ),
            max_iterations=4,
            done_field="done",
        ),
        outputs=["findings", "done"],
        transitions=[Transition(to="qa", when="always")],
    )

    qa_state = State(
        id="qa",
        purpose="evaluate the implementation and emit a verdict",
        worker=Worker(
            role="qa",
            prompt_template="Review the implementation and emit verdict.",
            response_schema=_qa_schema(),
        ),
        outputs=["verdict", "findings"],
        # Trivially-true post-validation; demonstrates that the wiring
        # runs without obstructing the happy path.
        post_validations=[Predicate("len(findings) >= 0")],
        transitions=[
            Transition(to="fix", when=Predicate("verdict == 'NO-GO'")),
            Transition(to="done", when="otherwise"),
        ],
    )

    fix_state = State(
        id="fix",
        purpose="apply fixes for QA findings then re-run QA",
        worker=Worker(
            role="fixer",
            prompt_template="Apply fixes for the QA findings.",
            response_schema=_fix_schema(),
        ),
        outputs=["fixes"],
        transitions=[Transition(to="qa", when="always")],
    )

    done_state = State(
        id="done",
        purpose="terminal state",
    )

    return FsmSpec(
        id="plan_implement_qa_fix",
        version=1,
        entry="plan",
        states=[plan_state, implement_state, qa_state, fix_state, done_state],
    )


# ---------------------------------------------------------------------------
# Simulated worker outputs
# ---------------------------------------------------------------------------


# Per-iteration outputs for the ``implement`` loop. Indexed by 1-based
# iteration number to match what ``Brief.iteration_n`` reports.
_IMPLEMENT_ITERATIONS: list[dict[str, Any]] = [
    {
        "findings": [
            {
                "commit_id": "c1",
                "file": "login.py",
                "line": 1,
                "status": "in_progress",
            }
        ],
        "done": False,
    },
    {
        "findings": [
            {
                "commit_id": "c1",
                "file": "login.py",
                "line": 42,
                "status": "in_progress",
            }
        ],
        "done": False,
    },
    {
        "findings": [
            {
                "commit_id": "c1",
                "file": "login.py",
                "line": 80,
                "status": "done",
            }
        ],
        "done": True,
    },
]


# QA emits a different verdict on each pass: the first cycle finds a
# blocker, the second (after ``fix`` has run) returns a clean GO.
_QA_PASSES: list[dict[str, Any]] = [
    {
        "verdict": "NO-GO",
        "findings": [
            {
                "commit_id": "c1",
                "severity": "BLOCKER",
                "message": "missing CSRF token",
            }
        ],
    },
    {
        "verdict": "GO",
        "findings": [],
    },
]


# Mutable counters so the dispatcher knows which fixture to return on
# repeated entries to the same state.
_state_counters: dict[str, int] = {"qa": 0, "fix": 0}


def simulated_output_for(
    state_id: str,
    iteration_n: int | None,
) -> dict[str, Any]:
    """Return the hard-coded output for ``state_id`` (and iteration).

    The dispatcher is deterministic — the same ``(state_id, iteration)``
    pair always returns the same dict. For multi-entry states (``qa``
    runs twice, ``fix`` runs once) we advance a per-state counter so
    successive entries see successive fixtures.

    Swap this function for a real MCP-driven sub-agent dispatcher and
    the rest of the driver loop is unchanged.
    """
    if state_id == "plan":
        return {
            "commitments": [
                {
                    "id": "c1",
                    "description": "Build login form",
                    "expected_evidence": "auth_test.py passes",
                }
            ]
        }
    if state_id == "implement":
        # ``iteration_n`` is 1-based; clamp to the last fixture if the
        # spec's ``max_iterations`` ever exceeds our pre-baked list.
        idx = max(1, iteration_n or 1) - 1
        idx = min(idx, len(_IMPLEMENT_ITERATIONS) - 1)
        return _IMPLEMENT_ITERATIONS[idx]
    if state_id == "qa":
        idx = min(_state_counters["qa"], len(_QA_PASSES) - 1)
        _state_counters["qa"] += 1
        return _QA_PASSES[idx]
    if state_id == "fix":
        _state_counters["fix"] += 1
        return {
            "fixes": [
                {
                    "finding_id": 0,
                    "file_change": "login.py:added csrf middleware",
                    "description": "CSRF token added",
                }
            ]
        }
    if state_id == "done":
        # Terminal state has no worker and declares no outputs.
        return {}
    raise KeyError(f"no simulated output for state {state_id!r}")


# ---------------------------------------------------------------------------
# Persistence helpers (thin wrappers around the W2 Project facade)
# ---------------------------------------------------------------------------


# Producer identity mirrors the value the W2 Project facade uses for its
# own ``run_started`` emit so every event in the journal is attributed
# to one logical "engine" producer regardless of who emitted it.
_PRODUCER_KIND = "engine"
_PRODUCER_NAME = "fsm.runtime"


def _ensure_producer(project: Project) -> str:
    """Idempotently upsert the engine producer and return its id."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session, kind=_PRODUCER_KIND, name=_PRODUCER_NAME
        )
    return producer.id


def _enter_state(
    project: Project,
    *,
    run_id: str,
    state_id: str,
    inputs: dict[str, Any],
    producer_id: str,
    iteration_n: int | None = None,
) -> str:
    """Persist a state-entry row + emit ``state_entered``. Returns the row PK."""
    with project.session_factory() as session, session.begin():
        seq = project.states.next_entry_seq(session, run_id)
        row = project.states.create(
            session,
            run_id=run_id,
            state_id=state_id,
            inputs=inputs,
            entry_seq=seq,
        )
        run_row = session.get(RunTable, run_id)
        if run_row is not None:
            run_row.current_state = state_id
            run_row.last_update_at = _iso_now_ms()
            session.add(run_row)
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.state_entered.value,
            payload={
                "run_id": run_id,
                "state_id": state_id,
                "entry_seq": seq,
                "iteration_n": iteration_n,
            },
            run_id=run_id,
        )
    return row.id


def _exit_state(
    project: Project,
    *,
    run_id: str,
    state_pk: str,
    outputs: dict[str, Any],
    producer_id: str,
) -> None:
    """Mark a state entry as exited + emit ``state_exited``."""
    with project.session_factory() as session, session.begin():
        project.states.mark_exited(session, state_pk, outputs)
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.state_exited.value,
            payload={"run_id": run_id, "state_pk": state_pk},
            run_id=run_id,
        )


def _record_transition(
    project: Project,
    *,
    run_id: str,
    from_state_pk: str,
    to_state_id: str,
    kind: str,
    predicate: str | None,
    predicate_result: bool | None,
    producer_id: str,
) -> None:
    """Insert a transitions row + emit ``transition_taken``."""
    with project.session_factory() as session, session.begin():
        project.transitions.create(
            session,
            run_id=run_id,
            from_state_pk=from_state_pk,
            to_state_id=to_state_id,
            kind=kind,
            predicate=predicate,
            predicate_result=predicate_result,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.transition_taken.value,
            payload={
                "run_id": run_id,
                "from_state_pk": from_state_pk,
                "to_state_id": to_state_id,
                "kind": kind,
                "predicate": predicate,
                "predicate_result": predicate_result,
            },
            run_id=run_id,
        )


def _complete_run(
    project: Project,
    *,
    run_id: str,
    verdict: Any,
    producer_id: str,
) -> None:
    """Flip the run to ``completed`` + emit ``run_completed``."""
    now = _iso_now_ms()
    with project.session_factory() as session, session.begin():
        project.runs.update_status(
            session,
            run_id=run_id,
            status="completed",
            ended_at=now,
            verdict=str(verdict) if verdict is not None else None,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.run_completed.value,
            payload={"run_id": run_id, "verdict": verdict, "ended_at": now},
            run_id=run_id,
        )


# ---------------------------------------------------------------------------
# Pretty-printers
# ---------------------------------------------------------------------------


def _render_state_tree(node: dict[str, Any], depth: int = 0) -> list[str]:
    """Render a ``StateNode`` dict as an indented ASCII tree."""
    indent = "  " * depth
    iter_suffix = (
        f" [iter={node['iteration_n']}]" if node.get("iteration_n") is not None else ""
    )
    line = (
        f"{indent}- {node['state_id']}"
        f" (seq={node['entry_seq']}, status={node['status']}){iter_suffix}"
    )
    lines = [line]
    for child in node.get("children", []):
        lines.extend(_render_state_tree(child, depth + 1))
    return lines


def _print_run_summary(project: Project, run_id: str) -> int:
    """Print run id, state tree, and last 20 events; return total event count."""
    with project.session_factory() as session:
        run = project.runs.get(session, run_id)
        tree = project.runs.state_tree(session, run_id)
        events = list(project.runs.events(session, run_id))

    assert run is not None, "run disappeared mid-driver"
    print()
    print("=" * 72)
    print(f"run_id     : {run_id}")
    print(f"status     : {run.status}")
    print(f"verdict    : {run.verdict}")
    print(f"current    : {run.current_state}")
    print("=" * 72)

    print("\nstate_tree:")
    if tree is None:
        print("  (empty)")
    else:
        for line in _render_state_tree(tree.model_dump(mode="json")):
            print(line)

    print(f"\nlast 20 of {len(events)} events:")
    for ev in events[-20:]:
        payload = ev.payload or {}
        notable_keys = (
            "state_id",
            "state",
            "to_state_id",
            "iteration_n",
            "verdict",
            "reason",
        )
        snippet_parts = [
            f"{k}={payload[k]!r}" for k in notable_keys if k in payload
        ]
        snippet = " " + " ".join(snippet_parts) if snippet_parts else ""
        seq = ev.seq if ev.seq is not None else "-"
        print(f"  [{seq:>4}] {ev.created_at}  {ev.kind}{snippet}")
    return len(events)


# ---------------------------------------------------------------------------
# Driver loop
# ---------------------------------------------------------------------------


def drive_run(
    project: Project,
    spec: FsmSpec,
    spec_uuid_str: str,
) -> tuple[str, str]:
    """Drive the FSM from entry to terminal using simulated worker outputs.

    Uses the W2 :class:`Project` facade for run creation + journaling
    and drives the pure :func:`engine.advance` directly. Returns a
    ``(run_id, final_state)`` tuple.
    """
    run = project.start_run(spec_id=spec_uuid_str, args={})
    run_id = run.id
    producer_id = _ensure_producer(project)

    # Walk the env forward as state outputs accumulate. Engine-pure
    # transitions evaluate against (env union latest_outputs), and post-
    # validations only see the latest outputs — we track both.
    env: dict[str, Any] = {}

    # Enter the entry state.
    entry_state = spec.get_state(spec.entry)
    entry_inputs: dict[str, Any] = {}
    state_pk = _enter_state(
        project,
        run_id=run_id,
        state_id=entry_state.id,
        inputs=entry_inputs,
        producer_id=producer_id,
    )

    current_state_id = entry_state.id
    iteration_n: int | None = (
        1 if spec.get_state(current_state_id).loop is not None else None
    )

    # Build the first brief.
    brief: Brief = build_brief(
        spec,
        entry_state,
        env=env,
        run_id=uuid.UUID(run_id),
        iteration_n=iteration_n,
    )

    final_state: str = current_state_id
    max_steps = 64
    for step in range(max_steps):
        outputs = simulated_output_for(brief.state, brief.iteration_n)

        ctx = RunCtx(
            run_id=uuid.UUID(run_id),
            fsm_id=spec.id,
            current_state=current_state_id,
            iteration_n=iteration_n,
            env=env,
        )
        result = engine_advance(spec, ctx, outputs)

        if result.kind == "fault":
            raise RuntimeError(
                f"engine faulted at step {step} in state {current_state_id!r}: "
                f"{result.reason} errors={result.errors} "
                f"post_validations={[e.model_dump() for e in result.post_validations]}"
            )

        if result.kind == "loop_continue":
            # Stay in the same state-entry row; just bump iteration and
            # build the next brief (engine has already prepared it).
            iteration_n = result.iteration_n
            brief = result.brief or brief
            continue

        # Both ``advance`` and ``terminal`` end the current state entry.
        _exit_state(
            project,
            run_id=run_id,
            state_pk=state_pk,
            outputs=outputs,
            producer_id=producer_id,
        )
        # Merge the just-committed outputs into env so subsequent
        # transitions and briefs see them.
        env = {**env, **outputs}

        if result.kind == "terminal":
            final_state = current_state_id
            _complete_run(
                project,
                run_id=run_id,
                verdict=result.verdict,
                producer_id=producer_id,
            )
            break

        # result.kind == "advance"
        # Pull the winning evaluation out of the trace so the
        # transitions row records the right kind / predicate.
        winning = next(
            (
                ev
                for ev in result.evaluations
                if ev.result and ev.to == result.next_state
            ),
            None,
        )
        _record_transition(
            project,
            run_id=run_id,
            from_state_pk=state_pk,
            to_state_id=result.next_state or "",
            kind=(winning.kind if winning else "always") or "always",
            predicate=(winning.expression if winning else None),
            predicate_result=(
                None
                if winning is None or winning.kind in {"always", "otherwise"}
                else bool(winning.result)
            ),
            producer_id=producer_id,
        )

        # Enter the next state.
        next_state_id = result.next_state or ""
        next_state = spec.get_state(next_state_id)
        next_worker = next_state.worker or (
            next_state.loop.worker if next_state.loop is not None else None
        )
        next_inputs: dict[str, Any] = (
            {name: env.get(name) for name in next_worker.inputs}
            if next_worker is not None
            else {}
        )
        iteration_n = 1 if next_state.loop is not None else None
        state_pk = _enter_state(
            project,
            run_id=run_id,
            state_id=next_state_id,
            inputs=next_inputs,
            producer_id=producer_id,
            iteration_n=iteration_n,
        )
        current_state_id = next_state_id
        brief = result.brief or build_brief(
            spec,
            next_state,
            env=env,
            run_id=uuid.UUID(run_id),
            iteration_n=iteration_n,
        )
    else:
        raise RuntimeError(
            f"FSM did not reach terminal within {max_steps} steps"
        )

    return run_id, final_state


def main() -> int:
    """Entry point — open a tmp Project, drive the run, print the report."""
    spec = build_spec()

    with tempfile.TemporaryDirectory(prefix="ctxr_fsm_example_") as tmpdir:
        db_path = Path(tmpdir) / "fsm.db"
        with Project.open(db_path) as project:
            registered = project.register_spec(spec, project_slug="examples")
            run_id, final_state = drive_run(project, spec, registered.spec.id)
            event_count = _print_run_summary(project, run_id)
            print()
            print(f"final_state : {final_state}")
            print(f"event_count : {event_count}")
            return 0


if __name__ == "__main__":
    sys.exit(main())
