# `@ctxr/fsm` — CLI Reference

Four executables ship with the package, all installed as `bin` entries on `npm install`. All emit JSON to stdout. Exit codes: `0` success, `1` runtime error (lock conflict, schema violation, run not found), `2` argument error.

Required configuration is supplied via flags or via a `.fsmrc.json` (or `fsm.config.json`) at the cwd. Flags override the config file.

## Configuration file

The CLIs auto-load `.fsmrc.json` (then `fsm.config.json`) from `process.cwd()` if present. The `fsms[]` array supports multiple named FSMs in one project (e.g. one per agent or logical pipeline):

```json
{
  "fsms": [
    {
      "name": "code-review",
      "fsm_path": "fsm/code-review.fsm.yaml",
      "storage_root": ".my-app/runs/code-review"
    },
    {
      "name": "report-builder",
      "fsm_path": "fsm/report-builder.fsm.yaml",
      "storage_root": ".my-app/runs/reports"
    }
  ]
}
```

### Allowed entry keys

Each entry in `fsms[]` accepts exactly three keys: `name`, `fsm_path`, `storage_root`. Unknown keys are rejected at load time. The config is purely static project setup — runtime concerns (`session_id`, run-id, process state) are never persisted here.

### Selection rules

- **Multiple FSMs configured.** Pass `--fsm <name>` to pick one. Omitting `--fsm` errors with the list of available names.
- **Single FSM configured.** `--fsm` is optional; the only entry is used.
- **No config file.** You must pass `--fsm-path` + `--storage-root` on every CLI invocation.
- **Direct override.** Passing `--fsm-path` + `--storage-root` always bypasses the config file. Useful for ad-hoc invocations.

Relative paths are resolved against `process.cwd()`.

## `fsm-next`

Acquire the lock for a run and return the next-state brief.

### Modes

```bash
# Start a new run
fsm-next --new-run --repo <name> --base-sha <sha> --head-sha <sha> [--args <json>]

# Resume an in-progress or paused run
fsm-next --resume <run-id>
```

### Common flags

| Flag | Required | Description |
|------|----------|-------------|
| `--fsm <name>` | when multiple FSMs configured | Pick a named entry from `.fsmrc.json` `fsms[]`. Single-FSM configs don't need this. |
| `--fsm-path <path>` | when no config + no `--fsm` | Path to the FSM YAML. Direct override; bypasses config. |
| `--storage-root <dir>` | when no config + no `--fsm` | Storage directory under which run dirs live. Direct override. |
| `--session-id <id>` | no | Session identifier; defaults to `session-<pid>-<timestamp>` |
| `--repo <name>` | yes for `--new-run` | Consumer-supplied identifier (recorded in the manifest) |
| `--base-sha <sha>` | for `--new-run` | Recorded in the manifest |
| `--head-sha <sha>` | for `--new-run` | Recorded in the manifest |
| `--args <json>` | no | Inline JSON of the run's argument bag |
| `--args-file <path>` | no | Path to a JSON file with the argument bag |

### Output

JSON brief with fields:

```json
{
  "ok": true,
  "run_id": "<YYYYMMDD>-<HHMMSS>-<hash7>",
  "fsm_id": "<from fsm.id>",
  "state": "<entry state's id>",
  "purpose": "<from state.purpose>",
  "preconditions": [...],
  "inputs": { ... },
  "outputs_expected": [...],
  "post_validations": [...],
  "transitions": [
    { "to": "<state-id>", "when": <when-clause> }
  ],
  "has_worker": true,
  "worker": {
    "role": "<role>",
    "prompt_template": "<path>",
    "inputs": [...],
    "response_schema": { ... }
  }
}
```

### Error shapes

```json
{ "error": "run_locked",       "lock": { ... } }
{ "error": "run_not_found",    "run_id": "<id>" }
{ "error": "fsm_yaml_changed", "run_hash": "<old>", "current_hash": "<new>", "current_state": "<id>" }
{ "error": "run_not_resumable","status": "completed" | "faulted" | "abandoned" }
```

## `fsm-commit`

Validate a state's outputs against the FSM-declared `response_schema`, write the exit trace, evaluate transition predicates, and advance to the next state.

### Usage

```bash
fsm-commit --run-id <id> --outputs <json> [--transition <state-id>] [--session-id <id>]
fsm-commit --run-id <id> --outputs-file <path>  ...
```

### Common flags

| Flag | Required | Description |
|------|----------|-------------|
| `--run-id <id>` | yes | The run-id from `fsm-next`'s output |
| `--outputs <json>` | one of | Inline JSON of the state's outputs |
| `--outputs-file <path>` | one of | JSON file with the state's outputs |
| `--transition <state-id>` | for `kind: judgement` | Caller's pick when current state has a judgement transition |
| `--session-id <id>` | yes | Must match the lock holder |
| `--fsm <name>` | when multiple FSMs configured | Pick a named entry from `.fsmrc.json` `fsms[]` |
| `--fsm-path <path>` | when no config + no `--fsm` | Direct override |
| `--storage-root <dir>` | when no config + no `--fsm` | Direct override |

### Output

When the run advances:

```json
{
  "ok": true,
  "advanced_from": "<previous-state-id>",
  "run_id": "...",
  "state": "<next-state-id>",
  ...full state brief identical to fsm-next's output...
}
```

When the run reaches a terminal state:

```json
{
  "ok": true,
  "status": "terminal",
  "state": "<terminal-state-id>",
  "verdict": "<from env.verdict, may be null>",
  "run_dir_path": "<absolute path>"
}
```

### Error shapes

- `output_schema_violation` — worker output failed Ajv validation against the state's `response_schema`. Manifest is set to `status: "faulted"`, fault trace written, lock released.
- `lock_not_held` — caller's `--session-id` does not match the lock holder.
- `fsm_yaml_changed` — the FSM YAML hash on disk differs from what was recorded at run-start. Caller must reconcile (resume the original FSM via git, abandon, or pivot).
- `no_transition_matched` — no transition predicate evaluated true on a state with non-empty `transitions[]`. Manifest faulted; fault trace written.

## `fsm-inspect`

Debug dump for a run.

### Usage

```bash
fsm-inspect --run-id <id> [--storage-root <dir>]
```

### Output

```json
{
  "ok": true,
  "run_id": "...",
  "run_dir_path": "<absolute path>",
  "manifest": { ... full manifest.json ... },
  "lock": { ... lock.json or null ... },
  "trace_count": 5,
  "trace": [
    { "file": "0001-entry-...", "sequence": 1, "phase": "entry", "state": "...", "timestamp": "..." }
  ]
}
```

## `fsm-validate-static`

Static well-formedness check on FSM YAML files.

### Usage

```bash
fsm-validate-static <path-to-fsm-yaml> [<more> ...]
```

Pass any number of FSM YAML paths; the CLI validates each and reports per-file results.

### Checks performed

- Schema phase: structural well-formedness — required fields, type checks, snake_case state ids, valid `kind: deterministic | judgement | always`, valid JSON Schema in `worker.response_schema`.
- Static phase: cross-state — no duplicate ids, every transition resolves, every state reachable from `entry`, at least one terminal state, worker `prompt_template` paths exist on disk, `worker.inputs[]` reference outputs of upstream states (or `args`).

### Output

```json
{
  "ok": true,
  "total_errors": 0,
  "files": [
    { "file": "...", "phase": "passed", "errors": [], "states": 14 }
  ]
}
```

On failure, `phase` is `"schema"` or `"static"` and `errors[]` enumerates every detected issue.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Runtime error: lock conflict, schema violation, run not found, fsm_yaml_changed, etc. |
| 2 | Argument error: missing required flag, malformed flag value |

All error responses are also written to stdout as JSON with an `error:` field, so callers can parse them programmatically while still using exit codes for fast-path branching.
