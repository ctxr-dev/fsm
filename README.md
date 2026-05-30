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

## Development

### One-time setup

```bash
git clone https://github.com/ctxr-dev/fsm.git
cd fsm

# Python side
uv sync --all-extras --dev

# UI side
cd ui && npm ci && cd ..
```

Requires Python 3.12+, Node 22+, and uv (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

### Run the test suites

```bash
# Python: unit + integration + CLI + MCP + API + lifecycle + enforcement + examples
uv run pytest tests/

# Python static analysis
uv run ruff check ctxr/ tests/ examples/
uv run mypy ctxr/fsm/

# Frontend
cd ui && npm test && npm run build && cd ..
```

Expected: `456 passed` (Python) + `17 passed` (UI) on a clean checkout.

### Run an example

```bash
uv run python examples/plan_implement_qa_fix.py
uv run python examples/code_review_pipeline.py
uv run python examples/research_with_retries.py
```

Each example uses a tmpdir for the run database, drives the FSM through simulated worker outputs, and prints the resulting state tree + event log. See docs/examples-tour.md for what each one teaches.

### Boot the dev supervisor

```bash
# In one terminal:
uv run ctxr-fsm init                  # creates .ctxr-fsm/fsm.db + applies migrations + patches CLAUDE.md/AGENTS.md
uv run ctxr-fsm serve --mode dev      # supervisor boots MCP (HTTP-SSE) + FastAPI + Vite UI

# In another terminal:
uv run ctxr-fsm doctor                # diagnostic report
uv run ctxr-fsm runs ls               # empty until you start a run
uv run ctxr-fsm spec register examples.plan_implement_qa_fix:spec
```

The supervisor:
- assigns ports automatically (and remembers them in `.ctxr-fsm/ports.json`)
- writes PID files under `.ctxr-fsm/pids/`
- watches `ctxr/fsm/` for file changes and gracefully drains + respawns MCP + API
- never spawns a second copy if you re-invoke from the same project root

Browse the UI at `http://localhost:5173`, the FastAPI docs at `http://localhost:<api_port>/docs` (port from `ctxr-fsm doctor`), and connect an MCP client to `http://localhost:<mcp_port>/sse`.

### Bootstrap from a skill or agent (universal entry point)

Skills and agents that depend on `ctxr-fsm` should follow the bootstrap discipline in [BOOTSTRAP.md](BOOTSTRAP.md) (mirror of [`ctxr/fsm/memory/bootstrap.md`](ctxr/fsm/memory/bootstrap.md)). The TL;DR is one command:

```bash
uv run ctxr-fsm ensure --json
```

`ensure` is idempotent and fast (<500ms when everything is already up). On a cold project it: creates `.ctxr-fsm/fsm.db`, runs migrations, installs principles into CLAUDE.md/AGENTS.md/.cursor/rules, registers `ctxr-fsm` as an MCP server in the active client's config, boots the supervisor (MCP + FastAPI + UI). Parse the JSON output to get `mcp_http_url` for the HTTP-SSE fallback path. Per-client reload semantics (Claude Code vs Codex vs Cursor) are documented in [BOOTSTRAP.md](BOOTSTRAP.md). A `--check` flag does the same probe read-only.

### Run the MCP server standalone (for Claude Code stdio integration)

When you want JUST the MCP child (no FastAPI, no UI), `ctxr-fsm ensure --mode mcp_only --json` is the supervisor-managed path (the legacy hyphenated form `mcp-only` is still accepted with a deprecation warning). For a raw standalone process (debugging, one-off connection):

```bash
uv run ctxr-fsm mcp --db ./.ctxr-fsm/fsm.db
```

This blocks on stdio; pair it with a Claude Code (or other MCP client) session configured to launch the binary on demand. The `.mcp.json` entry that `ctxr-fsm install-mcp` writes calls this same binary in stdio mode and lets it discover the DB by walking up from the inherited cwd to find `.ctxr-fsm/`. See docs/mcp-tools.md.

### Contribution loop

1. Branch off the latest unmerged tip (workstream chain). Use a `<scope>/<short-name>` style branch name.
2. Make changes inside one of `ctxr/fsm/{core,sqlite,cli,mcp,api,memory}`, `ui/`, `examples/`, or `docs/`. Don't cross layer boundaries (see docs/architecture.md for the dependency rule).
3. Run the full verify gauntlet locally (pytest + ruff + mypy + ui).
4. Commit + push + open a PR against the previous workstream branch (or main if it's a follow-on).
5. CI runs `python-ci.yml` on the PR (lint + test matrix on Python 3.12 + 3.13 + UI build).
6. After review + green CI, the human gate is: merge.
7. Publishes to PyPI are MANUAL via `python-publish.yml` workflow_dispatch (see docs/operating.md).

### Useful one-liners

| What | How |
|---|---|
| Watch tests rerun on file change | `uv run pytest tests/ -q --looponfail` (needs pytest-xdist) |
| UI dev mode (HMR) | `cd ui && npm run dev` |
| Show all CLI commands | `uv run ctxr-fsm --help` |
| Inspect a run | `uv run ctxr-fsm run show <run-id>` |
| Find drift signals | `uv run ctxr-fsm doctor` then `curl http://localhost:<api>/api/v1/admin/drift_signals` |
| Clean local state | `rm -rf .ctxr-fsm/ .venv/ ui/node_modules/ ui/dist/` |

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

## License

MIT for both roots. See [`LICENSE`](LICENSE) (Python) and [`legacy-js/LICENSE`](legacy-js/LICENSE) (JS).

## Contributors

Maintained by [ctxr-dev](https://github.com/ctxr-dev). Python work lands under `ctxr/fsm/` on per-workstream branches; JS bug fixes land under `legacy-js/`. Cross-cutting changes (the cohabitation itself, the top-level README, this CHANGELOG) live at this root. PRs welcome — read [docs/architecture.md](docs/architecture.md) first so the layer boundaries stay clean.
