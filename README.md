# ctxr-fsm — SQLite-backed FSM substrate for deterministic LLM-orchestrated workflows

A Python 3.12+ runtime that turns an `FsmSpec` into a crash-safe, auditable agent workflow on top of a single SQLite database. Workers (LLMs, scripts, humans) advance the run through `fsm.*` tools served over MCP, REST, or stdio; the substrate enforces what each state may do, cosigns every commit, runs an adversarial verifier panel, and quarantines runs that drift. One DB, one process tree, one observable timeline — no Redis, no queue broker, no Kubernetes.

## Architecture at a glance

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

Arrows point in the dependency direction. The core never imports SQLite; SQLite never imports MCP/API/CLI; the UI talks HTTP only. Full reference: [docs/architecture.md](docs/architecture.md).

## Quick start

```bash
uv add ctxr-fsm                                       # or: pip install 'ctxr-fsm[all]'
ctxr-fsm init                                         # .ctxr-fsm/fsm.db + migrations + AI-memory patch
ctxr-fsm spec register examples.plan_implement_qa_fix:spec
ctxr-fsm serve --mode dev                             # boots MCP + FastAPI + Vite UI
open http://localhost:5173                            # browse the run dashboard
```

`ctxr-fsm init` is idempotent — re-run it any time to top up migrations or refresh the principles installed into `CLAUDE.md` / `AGENTS.md`. `serve --mode dev` is a single supervisor process; `ctxr-fsm doctor` prints DB facts plus per-subsystem health.

## What's included

| Subsystem | What it does | Doc |
|---|---|---|
| `ctxr.fsm.core` | Pydantic spec models, pure engine (`advance`, `build_brief`), predicate DSL, loop + aggregator, spec hashing, verifier panel, Protocol surface | [docs/api.md](docs/api.md) |
| `ctxr.fsm.sqlite` | STRICT-mode schema across 18 tables, alembic migrations, repositories, `Project` facade, single-writer lock, atomic-tx journal, drift detector | [docs/data-model.md](docs/data-model.md) |
| `ctxr.fsm.mcp` | MCP server (stdio + HTTP/SSE) exposing 17 `fsm.*` tools — start runs, get briefs, commit + confirm outputs, subscribe to events, inspect the journal | [docs/mcp-tools.md](docs/mcp-tools.md) |
| `ctxr.fsm.api` | FastAPI app with REST + SSE under `/api/v1/`, OpenAPI at `/docs`, auth surface, mirrors the MCP tool set for browser / HTTP clients | [docs/http-api.md](docs/http-api.md) |
| `ctxr.fsm.cli` | `typer` console script: `init`, `migrate`, `serve`, `mcp`, `api`, `ui`, `doctor`, `runs ls`, `run show/resume/abort`, `spec validate/register/list`, `export`, `import`, `install-memory` | [docs/operating.md](docs/operating.md) |
| `fsm-ui` | Vite + Preact + Tailwind v4 SPA in `ui/`. SSE-driven run dashboard | [docs/architecture.md](docs/architecture.md) |
| Service lifecycle | Supervisor with one child per subsystem, reserved ports, PID singleton, graceful drain reload, `doctor` integrity sweep | [docs/operating.md](docs/operating.md) |
| Enforcement | Spec-hash lock, commit cosignature, two-phase commit, adversarial verifier panel, drift detector, Claude Code pre-tool-use hook | [docs/enforcement.md](docs/enforcement.md) |
| Examples | Three runnable simulated-worker workflows: plan/implement/qa/fix, code-review pipeline, research-with-retries | [docs/examples-tour.md](docs/examples-tour.md) |
| Recovery | Operator playbook for crashes, stalled journal txns, drift quarantine, replay | [docs/recovery.md](docs/recovery.md) |

## Why an FSM substrate?

A bare LLM loop is a probability cloud over tool calls; an FSM is a contract. Each state declares what tools the worker may call, what shape its output must take, what predicate decides the next state, and whether an adversarial verifier panel has to second the result. The substrate then enforces that contract with a two-phase commit, a cosignature over the brief + inputs + outputs, a spec-hash lock that pins a running workflow to its declared shape, and a background drift detector that quarantines runs whose accumulated misbehaviour crosses a configurable threshold. The result is a workflow you can replay, audit, and resume — not just one you can rerun and hope. Full layer-by-layer reference: [docs/enforcement.md](docs/enforcement.md).

## What's at `legacy-js/`

The pre-rewrite Node.js package `@ctxr/fsm` lives at [`legacy-js/`](legacy-js/) and still publishes to npm from there via its own workflow. It cohabits this repository so existing JS consumers (notably `skill-code-review`) keep working unchanged while the Python rewrite stabilises; migrating JS consumers onto `ctxr-fsm` is tracked separately and is not a precondition for using either side.

## Documentation

- [docs/architecture.md](docs/architecture.md) — layered design, dependency rule, service topology
- [docs/api.md](docs/api.md) — Python API reference (`ctxr.fsm.core` + `ctxr.fsm.sqlite`)
- [docs/data-model.md](docs/data-model.md) — every SQLite table, enum vocabulary, ER diagram, journal contract
- [docs/mcp-tools.md](docs/mcp-tools.md) — the 17 `fsm.*` MCP tools, error envelope, examples
- [docs/http-api.md](docs/http-api.md) — REST + SSE surface, auth, OpenAPI
- [docs/operating.md](docs/operating.md) — CLI reference, env vars, port + PID layout
- [docs/enforcement.md](docs/enforcement.md) — spec-hash lock, cosignature, two-phase commit, verifier, drift detector, CC hook
- [docs/examples-tour.md](docs/examples-tour.md) — walkthrough of the three runnable examples
- [docs/recovery.md](docs/recovery.md) — crash + drift + replay operator playbook

## Quick links

- PyPI: [`ctxr-fsm`](https://pypi.org/project/ctxr-fsm/) (publishes from this repo — manual `workflow_dispatch` only)
- GitHub: [ctxr-dev/fsm](https://github.com/ctxr-dev/fsm) — issues, PRs, CI
- Plan: `/Users/developer/.claude/plans/how-it-fits-toasty-gray.md` — single source of truth for workstreams (W0–W12), locked decisions, verification gates

## License

MIT for both roots. See [`LICENSE`](LICENSE) (Python) and [`legacy-js/LICENSE`](legacy-js/LICENSE) (JS).

## Contributors

Maintained by [ctxr-dev](https://github.com/ctxr-dev). Python work lands under `ctxr/fsm/` on per-workstream branches; JS bug fixes land under `legacy-js/`. Cross-cutting changes (the cohabitation itself, the top-level README, this CHANGELOG) live at this root. PRs welcome — read [docs/architecture.md](docs/architecture.md) first so the layer boundaries stay clean.
