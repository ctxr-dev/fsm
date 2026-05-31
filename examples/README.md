# ctxr-fsm examples — runnable agent-workflow tours

A small gallery of self-contained Python scripts that drive the
`ctxr.fsm` engine end-to-end against a real SQLite substrate, using
*simulated* worker outputs so each example runs offline in well under a
second. Each script is a tour of a different combination of FSM
features — loops, conditional transitions, cross-state aggregation,
journal recovery — and is structured so the only thing you need to
swap out to go from "demo" to "production" is the worker dispatcher.

## Concept overview

**What an FSM-driven workflow IS.** A `ctxr.fsm` workflow is a small
graph of *states*, each of which is either a single worker dispatch or
a bounded loop of worker dispatches. The engine walks the graph by
consuming a state's structured output, evaluating that state's
*transitions* (always / otherwise / predicate / judgement), and
emitting the next state to enter. Every transition is atomic — a state
entry, its worker artifacts, the transition row, and the next
state-entry row are all journaled so a crash mid-step leaves a clean
recoverable boundary. The materialised view of "where we are now" is a
state *tree* (one node per entry, oldest at the root), and the audit
trail is a monotonically-sequenced event log.

**Why simulated workers in the examples.** Each example replaces the
real worker-dispatch surface with a hard-coded fixture table indexed
by `(state_id, iteration)`. This keeps the run deterministic (every
invocation produces an identical state tree + event log), fast (no LLM
round-trip — examples complete in a few hundred milliseconds), free
(no API cost), and offline (CI can run them without network). The
shape of the simulated output exactly matches the worker's
`response_schema`, so the engine validates and merges it the same way
it would validate and merge a real worker's output.

**How to swap simulated workers for real MCP-driven sub-agent
dispatch.** Every example funnels its worker outputs through a single
function (typically `simulated_output_for` or `get_simulated_output`).
Replace that function's body with one that (1) reads the
`Brief` returned by `ctxr.fsm.core.engine.build_brief`, (2) renders
`brief.worker.prompt_template` against `brief.inputs`, (3) dispatches
the rendered brief to a sub-agent over the `ctxr-fsm` MCP server (or
any other transport) and awaits a structured response, and (4)
validates the response against `brief.worker.response_schema` before
returning the dict. The rest of the driver loop — `engine.advance`,
state-entry persistence, transition recording, journal finalisation —
stays unchanged. The `subscribe_events` MCP tool lets an orchestrator
stream the resulting state-entered / state-exited / transition-taken
events in real time and react to them.

**How to run.** Each example is a single Python file under
`fsm/examples/`. From the `fsm/` root:

```bash
uv run python examples/plan_implement_qa_fix.py
uv run python examples/code_review_pipeline.py
uv run python examples/research_with_retries.py
```

No setup is required beyond `uv sync` at the repo root — every example
opens its own ephemeral SQLite database and applies the Alembic
schema in-process on first connection.

**Where the run data lives.** Each example calls
`tempfile.TemporaryDirectory(...)` and opens a `Project` against an
SQLite file inside it. The temporary directory is cleaned up when the
script exits, so the run data is intentionally throwaway — the
example's *output* (state tree + event log printed to stdout) is the
artefact you read. The `run_id` is printed at the top of the summary
block; if you want to keep a run for forensic inspection, change the
`tempfile.TemporaryDirectory(...)` block to a fixed `Path(...)` and
re-run.

## Examples

### `plan_implement_qa_fix.py` — loops, predicates, conditional branches

**Teaches:** the `Loop` primitive with `done_field` + `max_iterations`,
`post_validations` (non-trivial on `plan`, trivially-true on `qa` to
demonstrate the wiring), and conditional transitions
(`qa → fix` when `verdict == 'NO-GO'`, otherwise `qa → done`). The
first QA pass returns `NO-GO`, runs `fix`, re-enters `qa`, returns
`GO`, exits to `done`.

```
plan ─always─▶ implement (loop, max=4, done_field=done) ─always─▶ qa
                                                                   │
                                            verdict=='NO-GO' ◀─────┤
                                                                   │
                                            otherwise ─────────────┴─▶ done
                                                ▲
                                                │
                                              fix ─always─▶ qa
```

Expected output excerpt:

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

### `code_review_pipeline.py` — fan-out loop + cross-state aggregation

**Teaches:** a fan-out loop that iterates one *lens* per pass
(`gap`, `blind-spot`, `edge-case`, `infeasibility`, `divergence`,
`missed-step`), the `aggregate_across_states` helper for collapsing
per-iteration outputs into a single list, the
`GO`/`CONDITIONAL`/`NO-GO` verdict rule, and a `post_validation`
that re-asserts the verdict shape inside the engine.

```
scan_diff ─always─▶ dispatch_lenses (loop, max=6, done on iter 6)
                                  │
                                  └─always─▶ collect_findings
                                                       │
                                  (aggregate_built) ───┤
                                                       │
                                                       └─always─▶ synthesize_verdict
                                                                              │
                                                                              └─always─▶ done
```

Expected output excerpt:

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

### `research_with_retries.py` — judgement guards + journal recovery

**Teaches:** a long-running loop capped at `max_iterations=10` whose
body sets `converged=true` to exit early, `judgement` transitions out
of `review` that branch on `verdict in {'accept', 'retry'}`, an
unconditional retry back-edge (`retry_research → research`), and a
second smaller demo that *injects a pending journal txn*, calls
`journal.inspect` + `journal.discard`, and shows the run resuming
clean — i.e. exactly what `fsm.recover_journal('discard')` does over
MCP.

```
research (loop, max=10, done=converged)
   │
   └─always─▶ cite ─always─▶ review
                              │
                              ├─judgement(verdict=='accept')─▶ publish (terminal)
                              │
                              └─judgement(verdict=='retry')──▶ retry_research
                                                                       │
                                                                       └─always─▶ research
```

Expected output excerpt:

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

## Next steps

* **Write your own `FsmSpec`.** Start from one of the
  `build_spec()` functions — they are the most compact, copy-pasteable
  spec examples in the repo. Each `State` needs an `id`, a `purpose`,
  optionally a `Worker` (or a `Loop` containing one), `outputs`, and
  `transitions`. Validate the spec with `spec.validate()` before
  registering it against a `Project`; the validator catches
  unreachable states, dangling transition targets, and predicate
  syntax errors before any DB writes happen.
* **Read the principles.** The full set of engine invariants and
  authoring guidelines lives at
  [`ctxr/fsm/memory/principles.md`](../ctxr/fsm/memory/principles.md).
  Skim it before writing a non-trivial spec — it covers the state-tree
  contract, the loop semantics, the predicate DSL, the aggregator
  surfaces, and the journal-recovery model.
* **Promote a simulated example to a real one.** Replace the
  hard-coded fixture dispatcher with an MCP-driven sub-agent dispatch
  using the `ctxr-fsm mcp` server. The
  [`subscribe_events`](../ctxr/fsm/mcp/tools_events.py) tool gives you
  a real-time stream of state transitions you can use as the
  orchestrator's event loop.
