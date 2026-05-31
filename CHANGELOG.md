# Changelog

All notable changes to `ctxr-fsm` (the Python package at this repository root) are documented in this file.

The format is based on [Keep a Changelog v1.1.0](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The pre-rewrite Node.js sources (`@ctxr/fsm`) were retired in W15. The pre-deletion state is preserved at the git tag [`legacy-js-archive`](https://github.com/ctxr-dev/fsm/releases/tag/legacy-js-archive); existing npm pins continue to resolve from the npmjs.com releases unchanged. This changelog covers the Python `ctxr-fsm` only.

## [Unreleased]

In development toward `0.2.0`. Tracked workstreams (post-`0.1.0`):

- W6 UI run-detail polish — keyboard navigation, journal-txn inspector, event-filter facets.
- Verifier specialists — pluggable real-LLM panels beyond the default structural verifier (design-constraints judge, acceptance-criteria judge, security-review judge).
- Per-project supervisor metrics endpoint + Prometheus scrape target.
- W10 CI/CD — automated import-linting, manual-publish workflow polish, release-note generator wired to this file.
- Cross-project drift dashboards in `fsm-ui`.

No public API breakage planned for `0.2.0`. The 17 `fsm.*` MCP tools, the `/api/v1/` REST surface, the SQLite schema (alembic head), and the `ctxr-fsm` CLI command set are stable contracts.

## [0.1.0] — 2026-05-30

First public release. Establishes the full Python substrate end-to-end: pure core, SQLite persistence, three operator surfaces (MCP, REST, CLI), the UI, the supervised service lifecycle, three runnable examples, the principles-injection CLI, and the enforcement primitives. Distributed on PyPI as `ctxr-fsm` (publishes manually via `workflow_dispatch`).

### Added

**W1 — `ctxr.fsm.core` — pure FSM substrate.**
Pydantic models (`FsmSpec`, `State`, `Transition`, `Worker`, `Loop`, `Predicate`, `ResponseSchema`, `VerifierSpec`, `Brief`, `WorkerOutput`, `CommitSignature`, `CommitToken`, `RunCtx`, `ValidationResult`). Pure engine (`build_brief`, `validate_output`, `resolve_transition`, `run_post_validations`, `advance`, `EngineAdvanceResult`). Loop iteration mechanics (`loop_decide`, `outputs_path_for`). Pure aggregator folds (`aggregate_loop_outputs`, `aggregate_across_states`). Spec validation + canonical hashing (`FsmSpec.validate()`, `FsmSpec.hash()`). Sandboxed predicate DSL for guards and post-validations. Adversarial verifier panel runtime. `Repository`, `EventBus`, `JournalProtocol`, `Lock` Protocols.

**W2 — `ctxr.fsm.sqlite` — persistence.**
SQLite STRICT-mode schema with WAL. Alembic migrations (runnable in-process — no `alembic` binary needed). 18 tables across `models_core.py`, `models_enforcement.py`, `models_events.py`. Repositories (`repos_core`, `repos_states`, `repos_events`, `repos_locks_journal`, `repos_enforcement`). `Project` facade as the single object higher layers use. Atomic transactions through `transactions.py` (one SQLite tx + one append-only journal entry per observable mutation). UUIDv7 PKs, ISO-8601 UTC timestamps, canonical JSON columns. Event bus, single-writer lock, drift detector.

**W3 — `ctxr.fsm.cli` — `typer` console script.**
`init`, `migrate`, `serve`, `mcp`, `api`, `ui`, `doctor`. Run commands: `runs ls`, `run show`, `run resume`, `run abort`. Spec commands: `spec validate`, `spec register`, `spec list`. Data: `export`, `import` (`schema_version: 1` JSON, atomic). `install-memory` (see W11). 56 tests. Global `--db` / `$CTXR_FSM_DB` / `./.ctxr-fsm/fsm.db` precedence, `--json` everywhere, structured exit codes (0/1/2).

**W4 — `ctxr.fsm.mcp` — MCP server.**
Anthropic Python SDK-based server exposing 17 `fsm.*` tools across four groups: meta + bootstrap (`fsm.healthcheck`, `fsm.list_specs`, `fsm.register_spec`, `fsm.observe_tool_call`); run lifecycle (`fsm.start_run`, `fsm.get_brief`, `fsm.commit_outputs`, `fsm.confirm_commit`, `fsm.resume_run`, `fsm.abort_run`, `fsm.list_runs`, `fsm.get_run`); events + journal (`fsm.subscribe_events`, `fsm.inspect_journal`, `fsm.recover_journal`, `fsm.list_consumers`, `fsm.list_producers`). Structured `McpToolError` envelope, stdio + HTTP/SSE transports, integration tests.

**W5 — `ctxr.fsm.api` — FastAPI REST + SSE.**
FastAPI app at `ctxr.fsm.api:app` mounted under `/api/v1/`. Mirrors the W4 MCP surface for HTTP clients. SSE event stream at `/events`. OpenAPI/Swagger at `/docs`, schemas at `/openapi.json`. Auth surface, admin routes, request/response models cross-validated with the MCP tool models.

**W6 — `fsm-ui` — observation surface.**
Vite + Preact + Tailwind v4 + TypeScript SPA at `ui/`. SSE consumer over the FastAPI `/events` stream. Run list, run detail, state tree, event timeline. Write actions round-trip through the same REST endpoints any other client would call.

**W7 — Service lifecycle.**
`ctxr-fsm serve` supervisor with one child per subsystem (MCP, FastAPI, Vite UI, drift daemon, healthz probe). One PID file at `.ctxr-fsm/run/supervisor.pid` prevents double-boot. Reserved ports up front, recorded in `.ctxr-fsm/run/ports.json`. Graceful drain reload (`ctxr-fsm serve reload`). `ctxr-fsm doctor` walks PID file, port file, DB migrations, journal integrity, and drift state and reports pass/fail per subsystem. Single SQLite writer enforcement; readers go through WAL.

**W8 — Examples.**
Three runnable, fully-deterministic FSM workflows under `examples/` driving the engine end-to-end with simulated worker outputs (offline, sub-second): `plan_implement_qa_fix.py` (loops, predicates, conditional branches, post-validations), `code_review_pipeline.py` (fan-out loop, cross-state aggregation, GO/CONDITIONAL/NO-GO synthesis), `research_with_retries.py` (judgement transitions, retry back-edges, journal recovery). End-to-end tests for each.

**W11 — Principles + memory injection.**
`ctxr-fsm install-memory` injects (or checks) FSM-usage principles into AI-client memory files: Claude (`CLAUDE.md` or `.claude/CLAUDE.md` via `@.ctxr-fsm/memory/principles.claude.md` import), Codex (`AGENTS.md` body inlined), Cursor (`.cursor/rules/ctxr-fsm.mdc`). Idempotent marker-block format; `--check` exits non-zero for CI gating. Cross-reference into Principle 3 of the common-dev rule set.

**W12 — Enforcement primitives.**
Four orthogonal groups across six layers:

- **Capability**: `State.allowed_tools` surfacing on the brief (layer 2). Claude Code `pre-tool-use.fsm-guard.py` hook (layer 4) blocks off-allowlist calls in CC sessions.
- **Integrity**: spec-hash lock (layer 9) pins a running workflow to its registered spec hash. Commit cosignature (layer 5) — `SHA-256` over canonical JSON of `{brief_id, inputs_hash, outputs_hash, session_id}`. Two-phase commit (layer 12) — `fsm.commit_outputs` mints a 60s-TTL `CommitToken`; `fsm.confirm_commit` consumes it and replays the journal.
- **Adversarial**: verifier panel (layer 3). Default structural verifier (re-applies `response_schema`); `set_verifier_handler()` registers a real LLM panel; majority threshold per `VerifierSpec`.
- **Observational**: drift detector (layer 8). Background task in the supervisor tallies signal weights per run; cumulative score strictly above `score_threshold` flips `run.status = drift_paused` exactly once. Defaults tuned so one accidental off-allowlist call is noise, three trip the pause. Kill switch via `CTXR_FSM_DRIFT_DISABLED=1`.

Plus the operator playbooks in [`docs/enforcement.md`](docs/enforcement.md) and [`docs/recovery.md`](docs/recovery.md).

**W9 — Documentation.**
Production-ready top-level [`README.md`](README.md) and this changelog. Full reference set under [`docs/`](docs/): `architecture.md`, `api.md`, `data-model.md`, `mcp-tools.md`, `http-api.md`, `operating.md`, `enforcement.md`, `examples-tour.md`, `recovery.md`.

### Note about the JS predecessor (retired in W15)

The pre-rewrite Node.js package `@ctxr/fsm` cohabited this repo under `legacy-js/` for W0–W14 so existing JS consumers (notably `skill-code-review` v2) kept working during the Python rewrite. W15 retires that subtree: the pre-deletion state is preserved at the git tag [`legacy-js-archive`](https://github.com/ctxr-dev/fsm/releases/tag/legacy-js-archive), npm consumers continue to resolve from the existing npmjs.com releases unchanged, and `skill-code-review` v3 (the Python port) replaces v2.

### Known issues / future

- **W6 UI run-detail polish** — current dashboard renders run list, state tree, and event timeline; keyboard navigation, journal-txn inspector, and per-event-kind filter facets land in `0.2.0`.
- **Verifier specialists** — `verifier.py` ships with a structural default. Real-LLM specialist panels (design-constraints judge, acceptance-criteria judge, security-review judge) ship in `0.2.0`. Register your own today via `set_verifier_handler()`.
- **W10 CI/CD** — manual `workflow_dispatch` publish is wired and used to cut this release; full import-lint enforcement, branch-protection automation, and auto-generated release notes from this file land alongside `0.2.0`.
- **Engine-driven resume** — `ctxr-fsm run resume` currently performs the bookkeeping half (journal discard/replay + `run_resumed` event). The engine-driven half (re-deriving the next brief from the resumed state) tracks under the W12 follow-up issue.
- **Cross-project dashboards** — the UI scopes to one project DB at a time. Multi-project rollups land post-`0.2.0`.

[Unreleased]: https://github.com/ctxr-dev/fsm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ctxr-dev/fsm/releases/tag/v0.1.0
