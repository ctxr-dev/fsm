# `@ctxr/fsm` — Design Reference

This is the design reference for `@ctxr/fsm`, a generic finite-state-machine substrate for deterministic LLM-orchestrated workflows. Consumers (skills, agents, CI workflows) declare their own FSM YAML and worker prompt templates and call the package's CLIs to drive a run.

The package is consumer-agnostic: no project-specific state names, paths, or assumptions. Consumers configure `fsm_path` and `storage_root` either via CLI flags or a `.fsmrc.json` config file at the consumer's project root.

## The problem

Long prose orchestrators executing multi-step workflows drift. Seven concrete failure modes:

| # | Failure mode | What goes wrong |
|---|-------------|-----------------|
| F1 | Step skipping | LLM judges a step "unnecessary this time", silently skips. |
| F2 | Out-of-order execution | LLM does step 5 before step 3. |
| F3 | Missing decision branch | LLM doesn't notice "if X, do Y else do Z". |
| F4 | Forgotten edge case | LLM doesn't handle the "what if N is empty" path. |
| F5 | Hallucinated step | LLM invents a step not in spec. |
| F6 | Decision drift | Same input, different decision criteria across sessions. |
| F7 | Apology recurrence | "Sorry, you're right, I forgot" — same drift recurs next session. |

## How the FSM addresses each failure

| Failure | Mechanism that blocks it |
|---|---|
| F1 (skip) | Each state's `preconditions[]` reference earlier states' `outputs[]`. The state-advance script refuses to enter state N+1 until N's outputs are persisted on disk. |
| F2 (out-of-order) | Same mechanism — preconditions enforce ordering. |
| F3 (missing branch) | Each state's `transitions[]` enumerates ALL valid next states with explicit predicates. The script evaluates every predicate on captured inputs; AI cannot bypass. |
| F4 (edge case) | Edge cases get their own state. Explicit transitions, no silent fall-through. |
| F5 (hallucination) | The state set is closed. The script rejects transitions to undefined state-ids. |
| F6 (decision drift) | Predicates are fixed strings in YAML. The script (not the AI) evaluates them. |
| F7 (apology recurrence) | Trace files on disk show every transition + evidence. Concrete correction, not vibes. |

## Architecture (recommended consumer pattern)

The package supports any orchestration topology, but the design is optimised for hub-and-spoke: a single FSM Orchestrator subagent talks to the package's CLIs; workers are ephemeral subagents that report back to the FSM Orchestrator.

```text
                            User
                              │
                              ▼
                       ┌──────────────┐
                       │ Main Session │  (consumer's top-level)
                       └──────┬───────┘
                              │ spawns 1 subagent
                              ▼
                    ┌─────────────────────┐
                    │  FSM Orchestrator   │  (the only state mutator)
                    └──┬──────────────────┘
                       │ calls @ctxr/fsm CLIs;
                       │ spawns workers via Agent tool;
                       │ collects schema-validated JSON responses
        ┌──────────────┼──────────────┬────────────────┐
        ▼              ▼              ▼                ▼
   ┌────────┐    ┌────────┐    ┌──────────────┐  ┌────────────┐
   │ Worker │    │ Worker │    │  Worker      │  │ Worker:    │
   │   A    │    │   B    │    │  C (or       │  │ context-   │
   │        │    │        │    │  coordinator)│  │ cleanup    │
   └────────┘    └────────┘    └──────────────┘  └────────────┘
```

### Roles

| Component | What it does | Persists state? |
|-----------|--------------|-----------------|
| **Main Session** | Receives user request; spawns FSM Orchestrator; monitors for `paused-for-context` and re-spawns; surfaces final result. | No |
| **FSM Orchestrator** | Calls `fsm-next` / `fsm-commit` CLIs; spawns workers per state; validates worker responses against schema. Never reads the FSM YAML directly. | No (state lives on disk) |
| **Worker subagent** | Executes one bounded task. Returns JSON conforming to the state's `response_schema`. Never mutates state. | No |
| **`@ctxr/fsm` scripts** | Parse FSM YAML; read/write state files atomically; acquire/release locks; validate worker output. The scripts ARE the engine. | Yes |
| **State files on disk** | `manifest.json` per run + `fsm-trace/NNNN-*.yaml` per transition + `lock.json` for in-progress runs. Atomic writes. | Canonical truth |

### What's never allowed

- AI agents reading FSM YAML files directly.
- Workers writing state files.
- Two callers advancing the same run-id concurrently (per-run `lock.json` blocks this).
- Free-form worker outputs (every response validates against the state's declared JSON Schema).

## Disk layout

The package never assumes a path. Consumers pass `--storage-root <dir>` to every CLI (or set `storage_root` in `.fsmrc.json`). Under that directory the engine creates:

```text
<storage-root>/
  <yyyy>/                           ← year folder (UTC)
    <mm>/                           ← month folder
      <dd>/                         ← day folder (human navigation)
        <ab>/                       ← first 2 hex chars of run-id hash (256 shards)
          <rest>/                   ← remaining 5 hex chars of run-id → unique run dir
            manifest.json           ← run summary + status (atomic via tmp+rename)
            lock.json               ← present while a session holds the run lock
            fsm-trace/              ← state transition records, sequential
              0001-entry-<state>.yaml
              0002-exit-<state>.yaml
              ...
            workers/                ← consumer-managed worker artifacts (optional)
```

### Why each path component

- `<yyyy>/<mm>/<dd>/`: human navigation — `ls <storage-root>/2026/04/26/` shows everything from one day.
- `<ab>/<rest>/`: filesystem-perf shard within day. APFS / ext4 directory lookups degrade around 10k flat entries. With 256 shards × ≤40 entries/day, lookups stay fast at any plausible volume.
- Per-run subdirectory: all artifacts colocated; trivial to archive, share, or `git add -f`.

## File schemas

### `manifest.json` — top-level run summary

Atomic updates via write-tmp-then-rename. The FSM Orchestrator never edits this directly; the `fsm-commit` script does.

```json
{
  "run_id": "20260426-001512-a3f7c9b",
  "parent_run_id": null,
  "forked_from": null,
  "fsm_id": "<from fsm.id in the FSM YAML>",
  "fsm_yaml_hash": "sha256:abc123...",
  "fsm_yaml_version": 1,
  "status": "in_progress",
  "current_state": "<state-id>",
  "next_state": null,
  "started_at": "2026-04-26T00:15:12.345Z",
  "last_update_at": "2026-04-26T00:18:42.812Z",
  "ended_at": null,
  "paused_at": null,
  "pause_reason": null,
  "abandoned_at": null,
  "abandon_reason": null,
  "repo": "<consumer-supplied>",
  "base_sha": "<consumer-supplied>",
  "head_sha": "<consumer-supplied>",
  "args": { "...": "..." },
  "verdict": null,
  "transitions_count": 5
}
```

**Status enum:**

- `in_progress` — actively executing; expect a `lock.json` alongside.
- `paused` — explicitly paused; resumable via `fsm-next --resume`.
- `completed` — reached terminal state.
- `faulted` — hit unrecoverable fault; not resumable without manual intervention.
- `abandoned` — explicitly abandoned; not resumable. (Sprint B introduces the `fsm-abandon` CLI.)
- `stale` — `last_update_at` older than TTL; needs explicit revive. (Sprint B introduces `fsm-stale-cleanup`.)
- `superseded` — this run was forked-from; status set when a new run with `forked_from = this.run_id` is created. (Sprint B introduces `fsm-pivot`.)

### `lock.json` — per-run lock with TTL

Created via `O_EXCL` open. Removed on graceful exit or on stale acquisition by another session.

```json
{
  "run_id": "20260426-001512-a3f7c9b",
  "session_id": "<caller's session identifier>",
  "pid": 41928,
  "acquired_at": "2026-04-26T00:18:42.812Z",
  "expires_at": "2026-04-26T01:18:42.812Z"
}
```

**Acquisition algorithm** (in `fsm-next` and `fsm-commit`):

1. Try `open(lock.json, O_CREAT | O_EXCL | O_WRONLY)`. On success, write contents, hold lock.
2. On `EEXIST`, read existing lock. If `expires_at < now`, the lock is stale: delete and retry from step 1.
3. If still active, refuse with structured error: `{ error: "run_locked", lock: { ... } }`.
4. On graceful exit, unlink `lock.json`.

This gives atomic single-writer guarantees without a database.

### `fsm-trace/NNNN-{entry|exit|fault}-<state>.yaml` — sequential transition records

Each state transition writes one or two trace files:

- Entry record: written when `fsm-next` returns the brief for state X.
- Exit record: written when `fsm-commit` validates outputs and advances past state X.
- Fault record: written when a precondition fails or a worker output fails schema validation; replaces the exit.

```yaml
# 0001-entry-<state>.yaml
phase: entry
state: <state-id>
sequence: 1
timestamp: "2026-04-26T00:15:12.345Z"
preconditions: []     # entry state — no upstream
inputs:
  args:
    base: auto
    head: HEAD
```

```yaml
# 0002-exit-<state>.yaml
phase: exit
state: <state-id>
sequence: 2
timestamp: "2026-04-26T00:15:42.812Z"
outputs:
  <output-1>: <value>
post_validations:
  - check: "<check-1>"
    result: pass
transition_evaluation:
  - to: <next-state>
    when: "always"
    result: true
transition: <next-state>
```

The AI never authors these. `fsm-commit` writes them after validating the worker's JSON response.

## State YAML structure

Consumers author one or more FSM YAML files. Each state declares:

```yaml
fsm:
  id: <consumer-fsm-id>
  version: 1
  entry: <entry-state-id>
  states:
    - id: <snake_case_id>
      purpose: "One-line description of what this state does."
      preconditions:
        - "<free-form English describing required prior outputs>"
      worker:                    # optional — inline states omit this
        role: <worker-role>
        prompt_template: <path-relative-to-package-or-absolute>
        inputs:
          - <name-of-upstream-output>
        response_schema:         # JSON Schema 2020-12; Ajv-validated
          type: object
          required: [<output>]
          properties:
            <output>: { type: <type>, ... }
      outputs:
        - <name>
      post_validations:
        - "<free-form check description; runtime evaluation deferred>"
      transitions:
        - to: <next-state-id>
          when: <expression>     # see below
```

### Transition `when:` shapes

```yaml
# Unconditional
when: always

# Catch-all — true iff no earlier transition matched
when: otherwise

# Deterministic predicate (engine evaluates against captured inputs)
when:
  kind: deterministic
  expression: "tier == 'trivial' AND len(stage_a_candidates) == 0"

# Judgement (caller picks via fsm-commit --transition <state>)
when:
  kind: judgement
  criteria: "Free-form English describing how the orchestrator should pick."
  evidence_required: "What the caller must justify in their pick."
```

### Predicate DSL (deterministic)

The package's predicate evaluator is a hand-rolled tokeniser + recursive-descent parser + AST evaluator. No `eval()`, no template-literal escape, no implicit type coercion beyond the operator semantics.

- **Literals**: numbers, strings (single or double quoted), `true`, `false`, `null`, `always`.
- **Identifiers**: dotted paths into the cumulative environment (`project_profile.languages`).
- **Comparison**: `==`, `!=`, `<`, `>`, `<=`, `>=`.
- **Logical**: `AND`, `OR`, `NOT` (case-insensitive); aliases `&&`, `||`, `!`.
- **Functions**: `len(x)`, `empty(x)`, `in(x, list)`.

## CLI reference (summary)

See `cli-reference.md` for the full reference. Quick summary:

| Script | Purpose |
|--------|---------|
| `fsm-next` | Acquire lock; read disk state; return next-state brief. Modes: `--new-run` or `--resume <run-id>`. |
| `fsm-commit` | Validate worker output against schema; run post_validations; evaluate transitions; write state-exit; advance. On terminal: set status=completed + verdict + release lock. |
| `fsm-inspect` | Debug: dump a run's manifest + transition history + lock status. |
| `fsm-validate-static` | Static FSM YAML well-formedness check. |

All scripts emit JSON to stdout; non-zero exit on error.

## Configuration: `.fsmrc.json` (or `fsm.config.json`)

CLI args override config-file values. The config file lives at the consumer's project root (cwd from which CLIs are invoked).

The `fsms[]` array supports multiple named FSMs in one project (e.g. one per agent / logical pipeline). CLIs select via `--fsm <name>`:

```json
{
  "fsms": [
    {
      "name": "<unique-name>",
      "fsm_path": "fsm/<name>.fsm.yaml",
      "storage_root": ".<consumer>/runs/<name>",
      "session_id": "<optional, defaults to PID-based>"
    }
  ]
}
```

The legacy single-FSM shape (top-level `fsm_path` / `storage_root`) is still accepted and is auto-wrapped as `fsms: [{ name: "default", ... }]`.

## What this still doesn't solve

Honest disclosure of limits:

- **AI can fabricate evidence on judgement predicates.** Mitigation: log all judgement evidence; periodic human spot-check.
- **AI can return malformed JSON from a worker.** Mitigation: schema validation at the boundary, one retry with corrective prompt, then fault.
- **Plan changes mid-run** require explicit user action (fork-pivot or abandon — `fsm-pivot` and `fsm-abandon` arrive in v0.2). Not seamless, but the script flags it instead of silently running stale.
- **Race conditions on the same run-id** are blocked by `lock.json`. Race conditions on different run-ids are not a problem (each session has its own run dir).
- **MCP migration** is deferred. Sprint A uses Agent tool with JSON-Schema-validated responses; sufficient for hub-and-spoke. MCP transport is a future optimisation.
- **Cross-run analytics** require either filesystem walks or a separately-built index. Not currently in scope; can be added later as a derived index without changing the canonical truth (filesystem).

## Roadmap

### v0.1 — Foundations (THIS RELEASE)

- Storage helpers (atomic writes, locks with TTL, run-id sharding).
- Predicate DSL (tokeniser + parser + evaluator).
- FSM YAML schema + static validator.
- Engine (loadFsm, runEnv, buildBrief, resolveTransition, validators).
- CLIs: `fsm-next`, `fsm-commit`, `fsm-inspect`, `fsm-validate-static`.
- Config-file loader (`.fsmrc.json`).
- ~70 unit + integration tests.

### v0.2 — Live trace + lifecycle CLIs

- `fsm-resume` — list resumable runs (in_progress / paused) matching filters.
- `fsm-pause` — pause an in-progress run.
- `fsm-abandon` — mark a run abandoned.
- `fsm-pivot` — pause old run + create new run with `forked_from`.
- `fsm-stale-cleanup` — TTL sweep for stale runs.
- `fsm-validate-trace` — runtime trace audit; re-runs deterministic predicates.
- Manifest gains `protocol_warnings[]` field.
- Soft-validate: violations log warnings but don't downgrade verdict.

### v0.3 — Strict trace + child FSMs

- Trace-validate violations on deterministic predicates downgrade verdict to CONDITIONAL.
- Child-FSM contract for sub-orchestrators.
- Specialist coordinator pattern: parallel fan-out under one parent FSM.

### v0.4 — Generator

- Render-from-YAML script for consumer's prose docs.
- YAML becomes single source of truth; consumer's MD docs are regenerated.

## Sources of inspiration

- Cloudflare's production AI code review architecture (orchestrator + sub-reviewers + judge).
- arxiv 2509.01494 (LLM code review benchmark) — multi-pass independent + aggregate beats cross-talking agents.
- Meta SemiFormalReasoning (premise → execution path → conclusion scaffold).
- GitHub Copilot Code Review (29% silent rate as a precision baseline).
- CodeRabbit, Greptile, Cursor Bugbot, Snyk Code (verification judge convergence).
- skill-llm-wiki (similarity-cache 256-hex-shard pattern).
