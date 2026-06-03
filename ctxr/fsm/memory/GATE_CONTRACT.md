---
name: ctxr-fsm-gate-contract
version: 0.1.0
audience: ai-agents
---

# ctxr-fsm: cross-FSM gate contract

A **gate** is a state kind that pauses a run waiting for a value supplied from outside the run's own state environment. The value may be an LLM-supplied literal, or a binding that pulls another run's state output. Gates make M:N fan-in/fan-out trivially expressible without bespoke orchestrator stitching.

This document specifies the protocol. The engine model lives in [`ctxr/fsm/core/models.py`](../core/models.py); the resolver MCP tool lives in [`ctxr/fsm/mcp/tools_runs.py`](../mcp/tools_runs.py).

## State shape

```python
from ctxr.fsm.core.models import State, Gate, GateBinding, ResponseSchema, StateKind

State(
    id="await_review",
    gate=Gate(
        source_kind="run_output",   # OR "llm_supplied"
        response_schema=ResponseSchema(schema_={...}),
        max_age_ms=86_400_000,        # optional: reject sources older than 24h
    ),
    outputs=["review_verdict"],
    transitions=[Transition(to="ship", when=TransitionKind.always)],
)
```

### Rules

1. Exactly one of `worker | loop | inline | gate | terminal` (`kind` property dispatches on this).
2. `Gate.response_schema` is mandatory and is validated against the resolved value.
3. `source_kind` is either `'run_output'` (read from another run's state) or `'llm_supplied'` (operator / LLM provides the value at resolve time).
4. `Gate.bindings` may be pre-populated by the orchestrator at `start_run` time when the source run is already known; otherwise the LLM supplies the binding at resolve time.

## Brief shape when a gate is current

```jsonc
{
  "kind": "gate",
  "state": "await_review",
  "gate": {
    "source_kind": "run_output",
    "response_schema": { ... },
    "bindings": []
  },
  "has_worker": false,
  "terminal": false
}
```

When you see `brief.kind == 'gate'`, you do NOT call `commit_outputs`. You call `fsm.resolve_gate`.

## Resolver MCP tool

```python
fsm.resolve_gate(
    run_id: str,
    state_entry_seq: int,
    *,
    value: dict | None = None,         # LLM-supplied literal
    binding: GateBinding | None = None,  # OR a reference to another run's state output
)
```

Exactly one of `value` / `binding` is required.

### `value` path

The LLM passes the resolved value directly (e.g. operator typed it, or the LLM computed it from world knowledge). The server validates against `gate.response_schema` and lands the value in the run's environment under the gate's `target_field` (defaults to the gate state's first declared output).

### `binding` path

```python
GateBinding(
    source_run_id="<run-uuidv7>",     # the run to read from
    source_spec_slug=None,             # optional; lets the server enforce spec match
    source_state_id="qa",              # which state's outputs to read
    source_field="verdict",            # which output field
    target_field="review_verdict",     # name under which the value lands in THIS run's env
)
```

The server reads `source_run_id`'s `source_state_id` outputs via the same plumbing that powers `fsm.get_run`. If `Gate.max_age_ms` is set, the source state's `exited_at` must be within that window; otherwise the gate rejects with `error: gate_source_stale`.

## Events emitted

- `gate_resolved` — value validated + landed in env. Payload: `{run_id, state_entry_seq, source_kind, target_field, value_hash}`.
- `gate_resolution_failed` — schema mismatch, missing source, stale source, etc. Payload includes `error` + `details`.
- `gate_binding_recorded` — for the `binding` path, a row lands in `gate_bindings` for cross-run topology queries.

## Persistence

The `gate_bindings` SQLite table records every resolved binding:

```sql
CREATE TABLE gate_bindings (
    id TEXT PRIMARY KEY,
    target_run_id TEXT NOT NULL,
    target_state_entry_seq INTEGER NOT NULL,
    source_run_id TEXT,
    source_spec_slug TEXT,
    source_state_id TEXT NOT NULL,
    source_field TEXT NOT NULL,
    target_field TEXT NOT NULL,
    resolved_value_json TEXT,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX gate_bindings_by_target ON gate_bindings (target_run_id);
CREATE INDEX gate_bindings_by_source ON gate_bindings (source_run_id);
```

The two indexes power the dashboard's Bindings panels:
- `/runs/:id` "Incoming" panel: `SELECT ... WHERE target_run_id = :id`.
- `/runs/:id` "Outgoing" panel: `SELECT ... WHERE source_run_id = :id`.
- `/links` topology view: `SELECT ... ORDER BY created_at DESC` (capped + paged).

## Error envelopes

| `error` | When | Action |
|---|---|---|
| `gate_value_required` | Brief is a gate; you called `commit_outputs`. | Call `fsm.resolve_gate` instead. |
| `gate_value_or_binding_required` | Neither `value` nor `binding` supplied. | Provide exactly one. |
| `gate_value_and_binding_conflict` | Both supplied. | Provide exactly one. |
| `gate_schema_mismatch` | Resolved value fails `Gate.response_schema`. | Fix shape. |
| `gate_source_run_not_found` | `binding.source_run_id` does not exist. | Verify run id. |
| `gate_source_state_not_completed` | Source state has not produced outputs yet. | Wait, or pick a different source. |
| `gate_source_stale` | Source's `exited_at` is older than `Gate.max_age_ms`. | Trigger a fresh source run. |
| `gate_spec_slug_mismatch` | `binding.source_spec_slug` does not match the source run's spec. | Update the binding. |

## Why gates and not subscriptions

Subscriptions (`fsm.subscribe_events`) push events to a consumer; the consumer decides what to do. Gates pull a typed value INTO a state's environment so the engine can advance deterministically based on it. Gates are appropriate when the downstream FSM's behaviour depends on the upstream value; subscriptions are appropriate when the downstream FSM just wants to react.

A skill that says "run code-review first, then if verdict=GO, deploy" is a gate use case: the deploy spec has a `await_review` gate state binding to code-review's `verdict` output. A skill that says "log every run completion to a CSV" is a subscription use case.

## What this file is

Shipped inside the `ctxr-fsm` package at `ctxr/fsm/memory/GATE_CONTRACT.md`. The canonical reference for any cross-run binding work. Installed into consumer projects by `ctxr-fsm install-memory`.
