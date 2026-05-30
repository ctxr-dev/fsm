# Examples Tour

Three runnable, fully-deterministic FSM workflows ship under
[`fsm/examples/`](../examples/). Each script is a single Python file that
opens an ephemeral SQLite project, registers an `FsmSpec`, and drives it
to completion using *simulated* worker outputs so the whole tour runs
offline in well under a second.

This page walks each example end-to-end:

| Example | Teaches | LOC |
| --- | --- | --- |
| [`plan_implement_qa_fix.py`](#1-plan_implement_qa_fix-py) | Loops, predicates, conditional branches, post-validations | ~800 |
| [`code_review_pipeline.py`](#2-code_review_pipeline-py) | Fan-out loop, cross-state aggregation, GO/CONDITIONAL/NO-GO synthesis | ~900 |
| [`research_with_retries.py`](#3-research_with_retries-py) | Judgement transitions, retry back-edges, journal recovery | ~900 |

> Cross-references:
> [`fsm-spec.md`](./fsm-spec.md) for the spec data model,
> [`predicates.md`](./predicates.md) for the guard DSL,
> [`aggregator.md`](./aggregator.md) for `aggregate_across_states`,
> [`journal.md`](./journal.md) for the recovery semantics.

## Running the tour

From the `fsm/` root, after `uv sync`:

```bash
uv run python examples/plan_implement_qa_fix.py
uv run python examples/code_review_pipeline.py
uv run python examples/research_with_retries.py
```

No setup beyond `uv sync` — every script opens its own
`tempfile.TemporaryDirectory()`, applies the Alembic schema in-process,
and tears the DB down on exit. The artefact you keep is the state-tree
and event-log printed to stdout.

## 1. `plan_implement_qa_fix.py`

A four-stage build/QA loop: plan the work, implement it iteratively,
review it, fix and re-review if needed.

### What it teaches

- The **`Loop`** primitive with `done_field` + `max_iterations`.
- **Conditional transitions** using the predicate DSL
  (`verdict == 'NO-GO'` vs. `otherwise`).
- **`post_validations`** — declared on `plan` (non-trivial:
  `len(commitments) > 0`) and `qa` (trivially true) to demonstrate the
  wiring runs without blocking the happy path.
- Re-entry into a previously-visited state (`qa` is entered twice,
  producing two distinct state-tree nodes).

### FSM diagram

```
plan ─always─▶ implement [loop, max=4, done=done]
                         │
                         └─always─▶ qa ◀────────────┐
                                    │               │
                  verdict=='NO-GO' ─┤               │
                                    │               │
                                    ▼               │
                                   fix ──always─────┘
                                    │
                            otherwise (GO)
                                    │
                                    ▼
                                  done
```

### Spec excerpt

```python
qa_state = State(
    id="qa",
    purpose="evaluate the implementation and emit a verdict",
    worker=Worker(role="qa", ..., response_schema=_qa_schema()),
    outputs=["verdict", "findings"],
    post_validations=[Predicate("len(findings) >= 0")],
    transitions=[
        Transition(to="fix",  when=Predicate("verdict == 'NO-GO'")),
        Transition(to="done", when="otherwise"),
    ],
)
```

### Expected output

```
status     : completed
verdict    : GO
current    : done

state_tree:
- plan (seq=1, status=exited)
  - implement (seq=2, status=exited)
    - qa (seq=3, status=exited)
      - fix (seq=4, status=exited)
        - qa (seq=5, status=exited)
          - done (seq=6, status=exited)
```

The two `qa` nodes (seq=3 and seq=5) are the proof that re-entry
materialises as **distinct tree nodes** rather than mutation in place.

### Adapt it to your FSM

| Want to... | Change... |
| --- | --- |
| Add a third verdict (e.g. `WAIT`) | Add another `Transition(to=..., when=Predicate("verdict == 'WAIT'"))` *before* the `otherwise` branch. |
| Cap fix attempts | Wrap `fix` in a `Loop(max_iterations=N, done_field=...)` and let it self-exit. |
| Reject a bad plan instead of silently passing | Replace the trivial `qa` post-validation with a real one — failures raise `PostValidationError` and journal the run as `failed`. |
| Run real workers | Replace `simulated_output_for(...)` with a call that dispatches `brief.worker.prompt_template` to a sub-agent and validates the response against `brief.worker.response_schema`. |

## 2. `code_review_pipeline.py`

Fan out N review *lenses* over a diff, collect the per-lens findings,
synthesise a GO/CONDITIONAL/NO-GO verdict.

### What it teaches

- A **fan-out loop** that iterates one lens per pass:
  `gap`, `blind-spot`, `edge-case`, `infeasibility`, `divergence`,
  `missed-step`.
- The **`aggregate_across_states`** helper that collapses per-iteration
  outputs into a single list keyed by `(state_id, iteration_n)`.
- The `aggregate:<state>.<field>` **input hint** convention that
  surfaces the aggregator dependency in the brief.
- A `post_validation` re-asserting the verdict shape inside the engine
  even though the worker already produced it.

### FSM diagram

```
scan_diff
   │ always
   ▼
dispatch_lenses [loop, max=6, done on iter 6]
   │ always
   ▼
collect_findings   (consumes aggregate:dispatch_lenses.findings)
   │ always
   ▼
synthesize_verdict (applies GO / CONDITIONAL / NO-GO rule)
   │ always
   ▼
done
```

### Verdict rule

| Verdict | Condition |
| --- | --- |
| `GO` | 0 BLOCKER findings |
| `CONDITIONAL` | Every BLOCKER has a `suggested_fix` |
| `NO-GO` | At least one BLOCKER without a `suggested_fix` |

### Aggregation excerpt

The driver pre-computes the cross-state aggregate before calling
`engine.advance` on `collect_findings` and threads it into the env:

```python
aggregated = aggregate_across_states(
    project=project,
    run_id=run_id,
    source_state="dispatch_lenses",
    field="findings",
)
env["aggregated_findings"] = aggregated
```

### Expected output

```
== code_review_pipeline.py ==
status     : completed
verdict    : CONDITIONAL
final_state: done

-- event log (17 events) --
  #  1 run_started
  ...
  #  8 aggregate_built
  ...
  # 17 run_completed
```

The `aggregate_built` event (emitted at #8) marks the moment the
per-lens findings were collapsed into `unified_findings`.

### Adapt it to your FSM

| Want to... | Change... |
| --- | --- |
| Add or drop lenses | Edit `LENSES` and bump `Loop.max_iterations` to match. The loop terminates on whichever fires first — `done_field` or `max_iterations`. |
| Switch the verdict rule | Edit `synthesize_verdict`'s worker logic *and* its `post_validations` together — the validator catches drift between them. |
| Aggregate something other than `findings` | Change the `field` argument to `aggregate_across_states` and update the consuming state's `inputs=[]`. |
| Make verdict transitions deterministic | Split the `synthesize_verdict → done` edge into three predicate-guarded edges on `verdict == 'GO' / 'CONDITIONAL' / 'NO-GO'`. |

## 3. `research_with_retries.py`

A research → cite → review loop that can retry, plus a second demo that
shows journal recovery after a simulated mid-step crash.

### What it teaches

- A long-running **loop** capped at `max_iterations=10` whose body sets
  `converged=true` to exit early.
- **`judgement` transitions** out of `review` — the engine asks the
  caller to pick between two named branches (`publish` / `retry_research`)
  rather than evaluating a predicate.
- An **unconditional retry back-edge** (`retry_research → research`)
  that re-enters the loop as a fresh state-tree subtree.
- A second mini-demo that **injects a pending journal txn**, calls
  `journal.inspect` + `journal.discard`, and resumes the run cleanly —
  the same recovery path that `fsm.recover_journal('discard')` follows
  over MCP.

### FSM diagram

```
research [loop, max=10, done=converged]
   │ always
   ▼
cite
   │ always
   ▼
review
   │
   ├─ judgement(verdict=='accept') ──▶ publish (terminal)
   │
   └─ judgement(verdict=='retry')  ──▶ retry_research
                                              │ always
                                              ▼
                                         research  (loops again)
```

### Spec excerpt — judgement guards

```python
review_state = State(
    id="review",
    ...
    transitions=[
        Transition(to="publish",
                   when={"kind": "judgement", "pick": "accept"}),
        Transition(to="retry_research",
                   when={"kind": "judgement", "pick": "retry"}),
    ],
)
```

The driver resolves the judgement by mapping `outputs['verdict']` to
the chosen `pick` and passing it into `engine.resolve_transition`.

### Expected output

```
========================================================================
MAIN RUN — retry-then-converge
========================================================================
State tree:
- research (entry_seq=1, status=exited, iteration_n=None)
  - cite (entry_seq=2, status=exited, iteration_n=None)
    ...
      - publish (entry_seq=8, status=exited, iteration_n=None)

Final published location: docs/research.md
Journal clean after main run: True

RECOVERY DEMO — simulated crash + journal cleanup
...
recover_journal action applied: discard
Journal clean after recovery: True
Run status after resume: in_progress

SUMMARY
Main run final state:     publish
Published location:       docs/research.md
Recovery demonstrated:    True
```

### Adapt it to your FSM

| Want to... | Change... |
| --- | --- |
| Add a third review outcome | Add a third `Transition(to=..., when={"kind": "judgement", "pick": "..."})` and teach your driver how to resolve the new `pick`. |
| Cap total retry attempts | Wrap the `research → cite → review → retry_research` cycle in a counter visible to a predicate, or move `retry_research` itself into a `Loop`. |
| Replace `discard` with `replay` on recovery | Call `journal.replay(...)` instead of `journal.discard(...)`; the engine will re-apply the staged txn idempotently. |
| Stream events to an orchestrator | Subscribe to the run via the `fsm.subscribe_events` MCP tool — see [`mcp-server.md`](./mcp-server.md). |

## Promoting a simulated example to a real one

Every example funnels its worker outputs through a single function —
typically `simulated_output_for(state_id, iteration_n, ...)`. To go
from demo to production, replace **only that function's body**:

```python
def real_output_for(state_id, iteration_n, env):
    brief = engine.build_brief(project, run_id, state_id, iteration_n)
    rendered = render(brief.worker.prompt_template, brief.inputs)
    response = mcp.dispatch_subagent(
        role=brief.worker.role,
        prompt=rendered,
        response_schema=brief.worker.response_schema,
    )
    validate(response, brief.worker.response_schema)
    return response
```

The rest of the driver loop — `engine.advance`, state-entry
persistence, transition recording, journal finalisation — stays
unchanged. See [`engine.md`](./engine.md) for the full `advance`
contract.

## Next steps

- Skim [`principles.md`](../ctxr/fsm/memory/principles.md) for the
  engine invariants and authoring guidelines.
- Read [`fsm-spec.md`](./fsm-spec.md) for the full `FsmSpec` /
  `State` / `Worker` / `Loop` reference.
- Read [`predicates.md`](./predicates.md) for the guard DSL grammar
  and `always` / `otherwise` semantics.
- Read [`journal.md`](./journal.md) for crash recovery, txn lifecycle,
  and the `inspect` / `discard` / `replay` surface.
- Read [`mcp-server.md`](./mcp-server.md) to expose any of these
  workflows to an external orchestrator via `subscribe_events`.
