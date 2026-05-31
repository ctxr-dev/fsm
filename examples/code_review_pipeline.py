"""Self-contained example: a 5-state code-review pipeline.

What this example demonstrates
==============================

A miniature, end-to-end FSM that mimics the shape of a code-review
pipeline:

1. ``scan_diff`` (worker) — produces ``files_changed[]`` describing
   which files moved in the diff.
2. ``dispatch_lenses`` (LOOP) — fan-out across six review *lenses*
   (gap, blind-spot, edge-case, infeasibility, divergence,
   missed-step). One loop iteration per lens. The loop terminates
   deterministically after the sixth iteration via ``done=true``.
3. ``collect_findings`` (worker) — consumes the loop's per-iteration
   outputs (aggregated cross-state, see :func:`~ctxr.fsm.core.aggregator.
   aggregate_across_states`) and produces a single ``unified_findings``
   list. The aggregator hint lives in the worker's ``inputs`` list.
4. ``synthesize_verdict`` (worker) — applies the
   ``GO`` / ``CONDITIONAL`` / ``NO-GO`` rule:

       * ``GO``          — zero ``BLOCKER`` findings.
       * ``CONDITIONAL`` — at least one ``BLOCKER`` finding, but every
         such finding carries a ``suggested_fix``.
       * ``NO-GO``       — at least one ``BLOCKER`` finding with no
         ``suggested_fix``.

5. ``done`` (terminal) — no outgoing transitions. The engine surfaces
   the ``verdict`` from the merged env as the run-level verdict.

How it runs
===========

* The FSM is built with the pure Pydantic API in
  :mod:`ctxr.fsm.core.models` and registered against a temporary
  ``Project`` from :mod:`ctxr.fsm.sqlite`.
* The pure engine (:func:`ctxr.fsm.core.engine.advance`) is driven
  in a small loop in this file. Each "worker dispatch" is replaced
  by a deterministic, hard-coded fixture so the example runs offline
  in well under a second.
* After every engine step we persist exactly what the real W4 MCP
  surface persists (see ``ctxr.fsm.mcp.tools_runs``): a state-entry
  row on enter, a state-exit + transition row on advance, and a
  ``run_completed`` event on terminal. This keeps the resulting
  ``state_tree`` and event log faithful to a "real" run.

Swapping in a real MCP-driven worker
====================================

The :func:`SIMULATED_OUTPUTS` table is the only place where this
example diverges from a production FSM driver. In a real deployment
the orchestrator would:

1. Read the :class:`~ctxr.fsm.core.models.Brief` returned by the
   engine (``fsm.get_brief``).
2. Dispatch the brief to a sub-agent over MCP (or any other
   transport) with ``Brief.prompt_template`` rendered against
   ``Brief.inputs``.
3. Receive the structured outputs from the sub-agent, validate them
   against ``Brief.worker.response_schema``, and pass them to
   ``fsm.commit_outputs`` (or, when calling the pure engine
   directly, :func:`advance`).

Replacing :func:`get_simulated_output` with an actual dispatcher is
the entire jump from "demo" to "production": nothing else in the
control loop changes.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from ctxr.fsm.core import (
    FsmSpec,
    Loop,
    Predicate,
    ResponseSchema,
    RunCtx,
    State,
    Transition,
    Worker,
    advance,
    aggregate_across_states,
    build_brief,
)
from ctxr.fsm.core.models import EventKind
from ctxr.fsm.sqlite import Project

# ---------------------------------------------------------------------------
# Spec construction
# ---------------------------------------------------------------------------

# Closed taxonomy of severities. ``BLOCKER`` is the only severity that
# can drive the verdict away from ``GO``; the others are advisory.
SEVERITIES = ["BLOCKER", "SHOULD-FIX", "NICE-TO-HAVE"]

# The six review lenses dispatched by the loop, in iteration order.
LENSES = [
    "gap",
    "blind-spot",
    "edge-case",
    "infeasibility",
    "divergence",
    "missed-step",
]


def build_spec() -> FsmSpec:
    """Build the code-review pipeline FSM.

    Five states, one loop, one cross-state aggregator hint. The
    deterministic transition guards use the predicate DSL defined in
    :mod:`ctxr.fsm.core.predicates`.
    """
    # Worker 1: scan_diff. Produces a list of changed files with
    # additions/deletions stats — the rest of the pipeline branches off
    # the shape of this output.
    scan_diff_state = State(
        id="scan_diff",
        purpose="Scan the diff and produce a list of changed files.",
        worker=Worker(
            role="diff-scanner",
            prompt_template=(
                "Scan the diff between the working tree and origin/main. "
                "Return one entry per changed file with line counts."
            ),
            inputs=[],
            response_schema=ResponseSchema(
                schema={
                    "type": "object",
                    "required": ["files_changed"],
                    "additionalProperties": True,
                    "properties": {
                        "files_changed": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["path", "additions", "deletions"],
                                "properties": {
                                    "path": {"type": "string"},
                                    "additions": {"type": "integer", "minimum": 0},
                                    "deletions": {"type": "integer", "minimum": 0},
                                },
                            },
                        },
                    },
                }
            ),
        ),
        outputs=["files_changed"],
        transitions=[Transition(to="dispatch_lenses", when="always")],
    )

    # Loop body schema. ``done`` MUST be declared in properties so the
    # spec validator accepts ``done_field="done"``.
    lens_iter_schema = ResponseSchema(
        schema={
            "type": "object",
            "required": ["lens", "findings", "done"],
            "additionalProperties": True,
            "properties": {
                "lens": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["severity", "title", "evidence_ref"],
                        "properties": {
                            "severity": {"type": "string", "enum": SEVERITIES},
                            "title": {"type": "string"},
                            "evidence_ref": {"type": "string"},
                            "suggested_fix": {"type": "string"},
                        },
                    },
                },
                "done": {"type": "boolean"},
            },
        }
    )

    # State 2: dispatch_lenses (LOOP). One iteration per lens. The done
    # field flips true on iteration 6 (deterministic completion). The
    # engine evaluates ``done_field`` *before* ``max_iterations``, so
    # the iteration that flips ``done=true`` is the one that terminates
    # the loop (reason = ``"done_field"``).
    dispatch_lenses_state = State(
        id="dispatch_lenses",
        purpose=(
            "Fan out one review lens per iteration. Six iterations total: "
            "gap, blind-spot, edge-case, infeasibility, divergence, "
            "missed-step. Each iteration returns its findings."
        ),
        loop=Loop(
            worker=Worker(
                role="lens-specialist",
                prompt_template=(
                    "You are the lens specialist for one of: gap, blind-spot, "
                    "edge-case, infeasibility, divergence, missed-step. "
                    "Analyse files_changed for issues from your lens's angle. "
                    "Return {lens, findings, done} where done=true iff this "
                    "is the sixth and final iteration."
                ),
                inputs=["files_changed"],
                response_schema=lens_iter_schema,
            ),
            max_iterations=6,
            done_field="done",
        ),
        outputs=["lens", "findings", "done"],
        transitions=[Transition(to="collect_findings", when="always")],
    )

    # State 3: collect_findings. The ``aggregate:dispatch_lenses.findings``
    # hint in ``inputs`` documents the cross-state aggregator — this
    # example performs the aggregation explicitly in the driver loop and
    # threads ``aggregated_findings`` into the env so the worker sees it.
    collect_findings_state = State(
        id="collect_findings",
        purpose=(
            "Aggregate the per-lens findings into a single unified list. "
            "Consumes the cross-state aggregate over dispatch_lenses."
        ),
        worker=Worker(
            role="findings-collector",
            prompt_template=(
                "Merge the per-lens findings into one ordered list. "
                "Deduplicate on (severity, title, evidence_ref) when "
                "two lenses surface the same issue."
            ),
            # The ``aggregate:`` prefix is a convention surfacing the
            # aggregator dependency in the brief; the driver loop below
            # honours it by pre-computing the aggregate before calling
            # ``advance`` on this state.
            inputs=["aggregated_findings"],
            response_schema=ResponseSchema(
                schema={
                    "type": "object",
                    "required": ["unified_findings"],
                    "additionalProperties": True,
                    "properties": {
                        "unified_findings": {
                            "type": "array",
                            "items": {"type": "object"},
                        },
                    },
                }
            ),
        ),
        outputs=["unified_findings"],
        transitions=[Transition(to="synthesize_verdict", when="always")],
    )

    # State 4: synthesize_verdict. Applies the GO/CONDITIONAL/NO-GO
    # rule. The verdict transitions are all ``always`` because the
    # rule is applied inside the worker; a real deployment might split
    # this into three deterministic transitions guarded by predicates
    # against ``verdict``.
    synthesize_verdict_state = State(
        id="synthesize_verdict",
        purpose=(
            "Apply the GO/CONDITIONAL/NO-GO rule:\n"
            "  * GO          — 0 BLOCKER findings.\n"
            "  * CONDITIONAL — every BLOCKER has a suggested_fix.\n"
            "  * NO-GO       — at least one BLOCKER without a suggested_fix."
        ),
        worker=Worker(
            role="verdict-synthesiser",
            prompt_template=(
                "Apply the GO/CONDITIONAL/NO-GO rule to unified_findings. "
                "Return {verdict, findings, explanation}."
            ),
            inputs=["unified_findings"],
            response_schema=ResponseSchema(
                schema={
                    "type": "object",
                    "required": ["verdict", "findings", "explanation"],
                    "additionalProperties": True,
                    "properties": {
                        "verdict": {
                            "type": "string",
                            "enum": ["GO", "CONDITIONAL", "NO-GO"],
                        },
                        "findings": {"type": "array"},
                        "explanation": {"type": "string"},
                    },
                }
            ),
        ),
        outputs=["verdict", "findings", "explanation"],
        # Post-validation: re-assert the verdict-shape invariant inside
        # the engine so a buggy worker is caught before the run is
        # marked terminal.
        post_validations=[
            Predicate(
                "verdict == 'GO' OR verdict == 'CONDITIONAL' OR verdict == 'NO-GO'"
            ),
        ],
        transitions=[Transition(to="done", when="always")],
    )

    # State 5: terminal. No outgoing transitions — the engine reports
    # ``kind="terminal"`` and lifts ``env.verdict`` onto the result.
    done_state = State(
        id="done",
        purpose="Terminal state; carries the final verdict.",
        transitions=[],
    )

    return FsmSpec(
        id="code_review_pipeline",
        version=1,
        entry="scan_diff",
        states=[
            scan_diff_state,
            dispatch_lenses_state,
            collect_findings_state,
            synthesize_verdict_state,
            done_state,
        ],
    )


# ---------------------------------------------------------------------------
# Simulated worker outputs (deterministic fixtures)
# ---------------------------------------------------------------------------

# Per-lens findings keyed by lens name. The fixture is hand-crafted so
# the aggregated stream contains exactly one BLOCKER with a
# ``suggested_fix`` plus one SHOULD-FIX — i.e. the rule resolves to
# ``CONDITIONAL``.
LENS_FINDINGS: dict[str, list[dict[str, Any]]] = {
    "gap": [],
    "blind-spot": [
        {
            "severity": "SHOULD-FIX",
            "title": "Missing rate limiter",
            "evidence_ref": "src/api.py:15",
            "suggested_fix": "Add rate-limit middleware",
        },
    ],
    "edge-case": [],
    "infeasibility": [],
    "divergence": [],
    "missed-step": [
        {
            "severity": "BLOCKER",
            "title": "No CSRF protection",
            "evidence_ref": "src/auth.py:10",
            "suggested_fix": "Use django.middleware.csrf",
        },
    ],
}


def simulated_scan_diff() -> dict[str, Any]:
    """Return the canned ``scan_diff`` worker output."""
    return {
        "files_changed": [
            {"path": "src/auth.py", "additions": 42, "deletions": 0},
            {"path": "src/api.py", "additions": 20, "deletions": 5},
        ],
    }


def simulated_lens_iteration(iteration_n: int) -> dict[str, Any]:
    """Return the canned loop-body output for the given iteration.

    Iteration numbers are 1-based and map onto :data:`LENSES`. The
    sixth iteration sets ``done=true`` so the loop terminates via
    ``done_field`` (the cleanest possible exit; ``max_iterations`` is
    a safety net we never hit here).
    """
    lens = LENSES[iteration_n - 1]
    return {
        "lens": lens,
        "findings": LENS_FINDINGS[lens],
        "done": iteration_n == 6,
    }


def simulated_collect_findings(aggregated: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the canned ``collect_findings`` worker output.

    In the real pipeline the worker would deduplicate / re-rank the
    aggregated findings; here we pass them through verbatim so the
    downstream verdict stage sees the same shape.
    """
    return {"unified_findings": list(aggregated)}


def simulated_synthesize_verdict(unified: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the GO/CONDITIONAL/NO-GO rule and return the worker output.

    The rule is implemented here in the simulated worker so the
    fixture is *self-checking*: if the upstream lens fixtures change
    such that a BLOCKER no longer carries a ``suggested_fix``, the
    verdict will drop to ``NO-GO`` automatically and the post-
    validation on ``synthesize_verdict`` will still pass.
    """
    blockers = [f for f in unified if f.get("severity") == "BLOCKER"]
    if not blockers:
        verdict = "GO"
        explanation = "0 BLOCKER findings => GO"
    elif all(f.get("suggested_fix") for f in blockers):
        verdict = "CONDITIONAL"
        explanation = (
            f"{len(blockers)} BLOCKER with suggested_fix => CONDITIONAL"
        )
    else:
        verdict = "NO-GO"
        explanation = (
            f"{len(blockers)} BLOCKER without suggested_fix => NO-GO"
        )
    return {
        "verdict": verdict,
        "findings": list(unified),
        "explanation": explanation,
    }


def get_simulated_output(
    state_id: str,
    *,
    iteration_n: int | None,
    env: dict[str, Any],
) -> dict[str, Any]:
    """Look up the canned worker output for ``state_id``.

    This is the *only* function a real driver would replace: swap in
    an MCP-backed dispatcher that renders ``Brief.prompt_template``
    against ``Brief.inputs``, awaits a structured response from a
    sub-agent, validates it against the worker's response schema, and
    returns the validated outputs dict.
    """
    if state_id == "scan_diff":
        return simulated_scan_diff()
    if state_id == "dispatch_lenses":
        assert iteration_n is not None, "loop state requires an iteration index"
        return simulated_lens_iteration(iteration_n)
    if state_id == "collect_findings":
        # The driver loop pre-computes ``aggregated_findings`` and
        # threads it into the env. We honour the contract so the
        # simulated worker truly consumes the aggregator's output.
        aggregated = env.get("aggregated_findings", [])
        return simulated_collect_findings(aggregated)
    if state_id == "synthesize_verdict":
        return simulated_synthesize_verdict(env.get("unified_findings", []))
    raise ValueError(f"no simulated worker for state_id={state_id!r}")


# ---------------------------------------------------------------------------
# Persistence helpers (mirror the W4 MCP surface)
# ---------------------------------------------------------------------------

_PRODUCER_KIND = "engine"
_PRODUCER_NAME = "fsm.runtime"


def _ensure_runtime_producer(project: Project) -> str:
    """Upsert the engine producer row and return its id."""
    with project.session_factory() as session, session.begin():
        producer = project.producers.upsert(
            session, kind=_PRODUCER_KIND, name=_PRODUCER_NAME
        )
    return producer.id


def _persist_state_entry(
    project: Project,
    *,
    run_id: str,
    state_id: str,
    inputs: dict[str, Any],
    producer_id: str,
    iteration_n: int | None = None,
) -> str:
    """Create a state-entry row + emit ``state_entered``; return its PK."""
    with project.session_factory() as session, session.begin():
        next_seq = project.states.next_entry_seq(session, run_id)
        state_row = project.states.create(
            session,
            run_id=run_id,
            state_id=state_id,
            inputs=inputs,
            entry_seq=next_seq,
        )
        project.events.emit(
            session,
            producer_id=producer_id,
            kind=EventKind.state_entered.value,
            payload={
                "run_id": run_id,
                "state_id": state_id,
                "entry_seq": next_seq,
                "iteration_n": iteration_n,
            },
            run_id=run_id,
        )
    return state_row.id


def _record_state_exit(
    project: Project,
    *,
    run_id: str,
    state_pk: str,
    outputs: dict[str, Any],
    producer_id: str,
) -> None:
    """Mark a state-entry row exited + emit ``state_exited``."""
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


def _record_worker_artifact(
    project: Project,
    *,
    run_id: str,
    state_pk: str,
    iteration_n: int | None,
    prompt_text: str,
    outputs: dict[str, Any],
) -> None:
    """Record a ``worker_artifacts`` row + emit ``worker_committed``."""
    import hashlib

    prompt_hash = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
    with project.session_factory() as session, session.begin():
        project.worker_artifacts.create(
            session,
            run_id=run_id,
            state_pk=state_pk,
            iteration_n=iteration_n,
            prompt_text=prompt_text,
            prompt_hash=prompt_hash,
            output=outputs,
            validated=True,
        )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _state_tree_to_dict(node: Any) -> dict[str, Any]:
    """Project a :class:`StateNode` into a JSON-printable dict.

    Drops the heavy ``inputs`` / ``outputs`` JSON bags from each node
    (they would dominate stdout) but keeps the structural fields.
    """
    return {
        "state_id": node.state_id,
        "entry_seq": node.entry_seq,
        "status": node.status,
        "iteration_n": node.iteration_n,
        "children": [_state_tree_to_dict(child) for child in node.children],
    }


def run_pipeline(project: Project, spec: FsmSpec) -> dict[str, Any]:
    """Drive the FSM end-to-end and return a summary dict.

    The summary contains the final run status, the verdict, the
    materialised state tree, and the full event log — exactly the
    pieces the example prints to stdout.
    """
    registered = project.register_spec(spec)
    run = project.start_run(spec_id=registered.spec.id)
    producer_id = _ensure_runtime_producer(project)

    # Mirror the MCP surface: persist the entry-state row up front so
    # ``state_entered`` is in the event log before the first commit.
    entry_state = spec.get_state(spec.entry)
    entry_inputs: dict[str, Any] = {}
    if entry_state.worker is not None:
        entry_inputs = {name: None for name in entry_state.worker.inputs}
    current_state_pk = _persist_state_entry(
        project,
        run_id=run.id,
        state_id=entry_state.id,
        inputs=entry_inputs,
        producer_id=producer_id,
    )

    # The driver's local env mirrors what ``_materialise_env`` would
    # reconstruct from the database. Keeping it in-process saves a
    # round-trip per step without losing fidelity (we still persist
    # the same rows the MCP surface would).
    env: dict[str, Any] = dict(run.args or {})
    current_state_id = spec.entry
    current_iteration: int | None = None
    # Per-iteration outputs of the loop, captured so we can aggregate
    # cross-state after the loop exits.
    loop_outputs: list[dict[str, Any]] = []

    while True:
        state = spec.get_state(current_state_id)
        brief = build_brief(
            spec,
            state,
            env=env,
            run_id=uuid.UUID(run.id),
            iteration_n=current_iteration,
        )

        # Terminal states have no worker and no transitions; the
        # engine still needs to be advanced once so it can emit a
        # ``terminal`` result, but we pass an empty outputs dict
        # because there is nothing to dispatch.
        if state.worker is None and state.loop is None:
            outputs = {}
        else:
            outputs = get_simulated_output(
                current_state_id,
                iteration_n=current_iteration,
                env=env,
            )

            # Record the worker artifact (prompt + output) so a real
            # operator could re-derive what the worker saw.
            _record_worker_artifact(
                project,
                run_id=run.id,
                state_pk=current_state_pk,
                iteration_n=current_iteration,
                prompt_text=brief.worker.prompt_template if brief.worker else "",
                outputs=outputs,
            )

        ctx = RunCtx(
            run_id=uuid.UUID(run.id),
            fsm_id=spec.id,
            current_state=current_state_id,
            iteration_n=current_iteration,
            env=env,
        )
        result = advance(spec, ctx, outputs)

        if result.kind == "fault":
            raise RuntimeError(
                f"engine faulted in state {current_state_id!r}: "
                f"reason={result.reason!r} errors={result.errors!r} "
                f"post_validations={[e.model_dump() for e in result.post_validations]!r}"
            )

        if result.kind == "loop_continue":
            # Stash this iteration's payload for the cross-state
            # aggregator, then advance the loop counter. No state
            # exit / transition is recorded — the loop body stays
            # inside the same state entry until ``done_field`` fires.
            loop_outputs.append(outputs)
            current_iteration = result.iteration_n
            continue

        # ``loop_continue`` did not fire, so either we have just
        # exited a non-loop state, or the loop terminated on this
        # very iteration. In the latter case ``outputs`` is still
        # the terminal-iteration payload and we MUST add it to the
        # aggregate.
        if state.loop is not None:
            loop_outputs.append(outputs)

        # Persist the state exit + record outputs on the database row.
        _record_state_exit(
            project,
            run_id=run.id,
            state_pk=current_state_pk,
            outputs=outputs,
            producer_id=producer_id,
        )

        # Merge worker outputs into env *before* moving on. Mirrors
        # the engine's own merge so the next state sees the same env
        # the engine sees.
        env = {**env, **outputs}

        if result.kind == "terminal":
            with project.session_factory() as session, session.begin():
                project.runs.update_status(
                    session,
                    run_id=run.id,
                    status="completed",
                    verdict=(
                        str(result.verdict) if result.verdict is not None else None
                    ),
                )
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.run_completed.value,
                    payload={"run_id": run.id, "verdict": result.verdict},
                    run_id=run.id,
                )
            break

        # result.kind == "advance" — record the transition + enter the
        # next state.
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
            run_id=run.id,
            from_state_pk=current_state_pk,
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

        # If the next state is the collect_findings worker, run the
        # cross-state aggregator over the loop's per-iteration
        # outputs and inject the result into env under the name the
        # worker declared in its ``inputs`` list. This is the
        # "aggregator hint" demonstrated by the example.
        if result.next_state == "collect_findings":
            aggregated = aggregate_across_states(
                states_outputs={"dispatch_lenses_loop": {"findings": []}},
                state_ids=["dispatch_lenses_loop"],
                merge_field="findings",
            )
            # The above is the *cross-state* aggregator surface — we
            # call it explicitly to demonstrate the import, but for
            # this single-loop example the actual aggregate value is
            # the concatenation of per-iteration findings we already
            # captured. Persist it via ``AggregatesRepo`` so the row
            # is visible to downstream consumers, and feed the merged
            # list into the env for the collect_findings worker.
            merged_findings: list[dict[str, Any]] = []
            for payload in loop_outputs:
                merged_findings.extend(payload.get("findings", []))

            with project.session_factory() as session, session.begin():
                project.aggregates.create(
                    session,
                    run_id=run.id,
                    field="findings",
                    from_state_ids=["dispatch_lenses"],
                    merged_length=len(merged_findings),
                    items=merged_findings,
                )
                project.events.emit(
                    session,
                    producer_id=producer_id,
                    kind=EventKind.aggregate_built.value,
                    payload={
                        "run_id": run.id,
                        "field": "findings",
                        "merged_length": len(merged_findings),
                        # surface the cross-state aggregator's
                        # bookkeeping so the event is self-describing
                        "from_states": list(aggregated.from_states),
                        "state_count": aggregated.state_count,
                    },
                    run_id=run.id,
                )
            env["aggregated_findings"] = merged_findings

        # Move on to the next state.
        next_state_id = result.next_state or ""
        next_state = spec.get_state(next_state_id)
        next_inputs: dict[str, Any] = {}
        next_worker = next_state.worker or (
            next_state.loop.worker if next_state.loop is not None else None
        )
        if next_worker is not None:
            next_inputs = {name: env.get(name) for name in next_worker.inputs}
        current_state_pk = _persist_state_entry(
            project,
            run_id=run.id,
            state_id=next_state_id,
            inputs=next_inputs,
            producer_id=producer_id,
            iteration_n=1 if next_state.loop is not None else None,
        )
        current_state_id = next_state_id
        current_iteration = 1 if next_state.loop is not None else None

    # ── Final read-back ───────────────────────────────────────────────
    with project.session_factory() as session:
        tree = project.runs.state_tree(session, run.id)
        events = list(project.runs.events(session, run.id))
    final_run = project.get_run(run.id)
    assert final_run is not None

    return {
        "run_id": run.id,
        "status": final_run.status,
        "verdict": final_run.verdict,
        "final_state": "done",
        "state_tree": _state_tree_to_dict(tree) if tree is not None else None,
        "events": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "payload": event.payload,
            }
            for event in events
        ],
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Build the spec, run the pipeline, and print everything to stdout."""
    spec = build_spec()

    # Sanity-check the spec structurally before we open a database;
    # this surfaces any spec-shape mistake (e.g. a state typo in a
    # transition target) without the DB getting involved.
    validation = spec.validate()
    if not validation.valid:
        raise SystemExit(
            "spec validation failed: "
            f"errors={validation.errors} "
            f"unreachable={validation.unreachable_states} "
            f"dangling={validation.dangling_transitions} "
            f"invalid_predicates={validation.invalid_predicates}"
        )

    with tempfile.TemporaryDirectory(prefix="ctxr-fsm-example-") as tmp:
        db_path = Path(tmp) / "code_review_pipeline.db"
        with Project.open(db_path) as project:
            summary = run_pipeline(project, spec)

    print("== code_review_pipeline.py ==")
    print(f"run_id     : {summary['run_id']}")
    print(f"status     : {summary['status']}")
    print(f"verdict    : {summary['verdict']}")
    print(f"final_state: {summary['final_state']}")
    print()
    print("-- state_tree --")
    print(json.dumps(summary["state_tree"], indent=2, sort_keys=True))
    print()
    print(f"-- event log ({len(summary['events'])} events) --")
    for event in summary["events"]:
        seq = "-" if event["seq"] is None else str(event["seq"])
        print(f"  #{seq:>3} {event['kind']}")


if __name__ == "__main__":
    main()
