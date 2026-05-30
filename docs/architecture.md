# Architecture

`ctxr-fsm` is a workstation-scale finite-state-machine runtime for agent orchestration. It is a Python 3.12+ library plus a small set of long-running services (MCP server, FastAPI server, UI dev server, supervisor) that share one SQLite database per project. Every higher layer depends only on the layers below it; nothing in the core imports SQLite, HTTP, MCP, or the UI.

The full workstream plan, including locked decisions and verification gates, lives at `/Users/developer/.claude/plans/how-it-fits-toasty-gray.md`.

## Layered overview

```
┌──────────────────────────────────────────────────────────────────────┐
│                      UI (fsm-ui · Vite + Preact)                     │
│              SSE consumer over the FastAPI event stream              │
└──────────────────────────────────────────────────────────────────────┘
                              ▲
┌─────────────────┬────────────┴───────────┬──────────────────────────┐
│  ctxr.fsm.mcp   │      ctxr.fsm.api      │      ctxr.fsm.cli        │
│  (MCP server)   │ (FastAPI · REST + SSE) │   (typer · operator)     │
│   17 fsm.*      │   /runs /specs /events │   init · serve · doctor  │
│   tools         │   /admin               │   runs · spec · migrate  │
└─────────────────┴────────────┬───────────┴──────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                          ctxr.fsm.sqlite                             │
│   STRICT schema · alembic migrations · 18 tables · repositories      │
│   Project facade · transactions · journal · locks · drift detector   │
└──────────────────────────────────────────────────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                            ctxr.fsm.core                             │
│  Pydantic models · engine (advance) · predicate DSL · loop · agg     │
│  spec validate/hash · verifier panel · Protocols for persistence     │
└──────────────────────────────────────────────────────────────────────┘
```

Arrows point in the dependency direction. The core is dependency-light (Pydantic + jsonschema). SQLite is the only persistence backend; it satisfies the `Repository`, `EventBus`, `JournalProtocol`, and `Lock` Protocols declared in `ctxr.fsm.core.protocols`.

## Layer responsibilities

### `ctxr.fsm.core` — pure FSM substrate

The bedrock. No I/O, no database, no network. Defines what an FSM *is* and how a single step works.

| Module | Responsibility |
|---|---|
| `models.py` | Pydantic models, StrEnums, engine value objects (`Brief`, `WorkerOutput`, `CommitSignature`, `CommitToken`, `RunCtx`, `ValidationResult`, …) |
| `engine.py` | Pure runtime: `build_brief`, `validate_output`, `resolve_transition`, `run_post_validations`, `advance`, `EngineAdvanceResult` |
| `loop.py` | Loop iteration mechanics: `loop_decide`, `outputs_path_for` |
| `aggregator.py` | Pure folds over loop/state outputs |
| `spec.py` | `FsmSpec.validate()`, `FsmSpec.hash()` (canonical hash for spec-lock) |
| `predicates.py` | Sandboxed DSL evaluator for guards and post-validations |
| `verifier.py` | Adversarial verifier panel runtime |
| `protocols.py` | Typing surface (`Repository`, `EventBus`, `JournalProtocol`, `Lock`) the SQLite layer satisfies |

Importing `ctxr.fsm.core` is safe in any environment that has Pydantic. See [core.md](core.md) for the model and engine reference.

```python
from ctxr.fsm import build_brief, advance, FsmSpec

spec = FsmSpec.model_validate_json(open("workflow.json").read())
spec.validate()  # structural check
spec_hash = spec.hash()  # canonical hash used by the spec-lock
```

### `ctxr.fsm.sqlite` — persistence

The only persistence backend. SQLite in STRICT mode with WAL, alembic migrations, and 18 tables across `models_core.py`, `models_enforcement.py`, and `models_events.py`. Repositories (`repos_*.py`) expose Protocol-conforming operations. The `Project` facade in `project.py` is the single object higher layers use to talk to a project's database.

Atomicity is non-negotiable. Every observable mutation goes through `transactions.py`, which serialises writes inside a sqlite transaction plus an append-only journal entry. See [persistence.md](persistence.md) and [transactions.md](transactions.md).

```python
from ctxr.fsm.sqlite import Project

project = Project.open(".ctxr-fsm/fsm.db")
with project.transaction() as tx:
    tx.runs.update_state(run_id, new_state="reviewed")
    tx.events.append(run_id, kind="state_entered", payload={"state": "reviewed"})
# both rows + journal entry commit atomically, or none do
```

### `ctxr.fsm.mcp` · `ctxr.fsm.api` · `ctxr.fsm.cli` — surfaces

Three peers above SQLite. Each is a thin adapter; none owns business logic.

| Surface | Audience | Transport | Key files |
|---|---|---|---|
| `ctxr.fsm.mcp` | Claude Code / agent harnesses | MCP stdio + HTTP | `server.py`, `tools_*.py` (17 `fsm.*` tools) |
| `ctxr.fsm.api` | UI, scripts, external HTTP clients | FastAPI REST + SSE | `server.py`, `routes_*.py` |
| `ctxr.fsm.cli` | Operators, CI | `typer` CLI (`ctxr-fsm …`) | `init_cmd.py`, `serve_cmd.py`, `doctor_cmd.py`, … |

All three open the same `Project` facade and respect the same lock + journal contract. They never bypass it. See [mcp.md](mcp.md), [api.md](api.md), [cli.md](cli.md).

### `fsm-ui` — observation surface

Vite + Preact + Tailwind v4 SPA in `ui/`. Consumes the FastAPI SSE stream at `/events` and renders the run dashboard. Read-mostly — write actions round-trip through the same REST endpoints any other client would call. See [ui.md](ui.md).

## Cross-cutting concerns

These four concerns thread through every layer above the core. They are why the system can claim "no torn state across crashes".

### Events

The event bus is append-only. Every state transition, worker dispatch, validation result, and lifecycle change emits one event into the `events` table. Subscribers receive the same rows via SSE (`/events`) or MCP (`fsm.events.subscribe`).

```
core.advance()  ──▶  Project.transaction()  ──▶  events.append(...)
                                                       │
                                                       ▼
                                            ┌──────────┴──────────┐
                                            │ FastAPI SSE stream  │
                                            │ MCP subscription    │
                                            │ UI dashboard         │
                                            └─────────────────────┘
```

Events are produced inside the same transaction as the state mutation that caused them. There is no "fire-and-forget" path. See [events.md](events.md).

### Journal

Every transaction writes one row to the journal table describing the intent of the change (state-from, state-to, worker output digest, spec hash at the time). The journal is the audit trail used by:

- `ctxr-fsm doctor` for integrity checks,
- the drift detector for replay,
- post-mortem reconstruction after a crash.

The journal is the source of truth for "what did this run do, in what order". See [journal.md](journal.md).

### Lock (spec-hash lock)

W12 enforcement: every committed transition records the canonical hash of the `FsmSpec` it ran under. The lock table guarantees:

1. **Spec immutability mid-run** — a run started under spec hash `H1` cannot advance under hash `H2`.
2. **Cosignature requirement** — a `CommitToken` must carry signatures from the engine *and* the post-validator panel.
3. **Two-phase commit** — `prepare → commit` separates "produced an artefact" from "applied it"; a crash between phases is detectable and recoverable.

See [enforcement.md](enforcement.md).

### Drift detector

`ctxr.fsm.sqlite.drift` periodically replays the journal forward from a checkpoint and asserts that the resulting state matches the live row. Mismatch = drift = quarantine the run and emit a `drift_detected` event. The detector runs inside the supervisor (see below) and on demand via `ctxr-fsm doctor --drift`. See [drift.md](drift.md).

## Service topology — process-per-subsystem

`ctxr-fsm serve` starts a **supervisor** process that owns one child process per subsystem. Subsystems are isolated for crash containment and independent reload.

```
                       ┌────────────────────────┐
                       │  ctxr-fsm serve        │
                       │  (supervisor; PID file)│
                       └────────────┬───────────┘
                                    │ spawns + watches
       ┌────────────────┬───────────┼───────────┬────────────────┐
       ▼                ▼           ▼           ▼                ▼
  ┌─────────┐    ┌──────────┐  ┌────────┐  ┌────────┐    ┌──────────┐
  │  MCP    │    │ FastAPI  │  │ UI dev │  │ drift  │    │ healthz  │
  │ server  │    │  server  │  │ (vite) │  │ daemon │    │ probe    │
  └────┬────┘    └────┬─────┘  └────────┘  └───┬────┘    └──────────┘
       │              │                        │
       └──────────────┴────────┬───────────────┘
                               ▼
                       ┌───────────────┐
                       │  fsm.db       │ (single writer, WAL readers)
                       └───────────────┘
```

Key properties:

- **One PID file** at `.ctxr-fsm/run/supervisor.pid` prevents double-boot.
- **Reserved ports** allocated up front and recorded in `.ctxr-fsm/run/ports.json`; children inherit them.
- **Graceful drain reload** — `ctxr-fsm serve reload` drains in-flight requests, swaps the child, then resumes traffic.
- **Doctor** — `ctxr-fsm doctor` walks the PID file, port file, DB migrations, journal integrity, and drift state, then reports pass/fail per subsystem.
- **Single SQLite writer** — only one process holds the write lock at a time; readers go through WAL. The supervisor enforces this by routing all writes through the MCP/API processes and never the UI.

See [lifecycle.md](lifecycle.md) for the full supervisor contract.

## Dependency rule

```
ui  ──▶ api ──▶ sqlite ──▶ core
        mcp ──▶ sqlite ──▶ core
        cli ──▶ sqlite ──▶ core
```

There are no upward imports. The core never imports SQLite; SQLite never imports MCP/API/CLI; the UI is a separate process that only talks HTTP. This is enforced by import linting in CI (W10).

## Where to go next

- [core.md](core.md) — model and engine reference
- [persistence.md](persistence.md) — schema, migrations, repositories
- [mcp.md](mcp.md) · [api.md](api.md) · [cli.md](cli.md) — surface references
- [lifecycle.md](lifecycle.md) — supervisor, ports, PIDs, reload
- [enforcement.md](enforcement.md) — spec-lock, cosignature, two-phase commit
- [examples.md](examples.md) — three runnable agent workflows
