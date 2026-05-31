# Operating ctxr-fsm

CLI reference for the `ctxr-fsm` console script. Every command honours the same
`--db` / `$CTXR_FSM_DB` / `./.ctxr-fsm/fsm.db` precedence and supports
`--json` for machine-readable output.

```
ctxr-fsm <command> [<subcommand>] [OPTIONS]
```

See also: [architecture](architecture.md), [specs](specs.md), [runs](runs.md).

## Global conventions

| Flag         | Effect                                                          |
| ------------ | --------------------------------------------------------------- |
| `--db`, `-d` | Override the project database path.                             |
| `--json`     | Emit a sorted, indented JSON document instead of pretty output. |
| `--help`     | Print help for any command or subcommand.                       |

DB precedence (highest first): `--db PATH` > `$CTXR_FSM_DB` > `./.ctxr-fsm/fsm.db`.

Exit codes: `0` success, `1` domain failure (banner on stderr), `2` usage error.

## Lifecycle commands

These bring a project up, keep its schema current, and tear off diagnostics.

### `init`

Bootstrap a project directory.

```
ctxr-fsm init [--db PATH] [--json] [--no-memory]
```

Performs, in order:

1. Creates `./.ctxr-fsm/` and `./.ctxr-fsm/pids/`.
2. Runs `alembic upgrade head` in-process (no `alembic` binary required).
3. Appends `.ctxr-fsm/` to `.gitignore` (idempotent) when inside a git checkout.
4. Unless `--no-memory`, invokes `install-memory --client auto`.
5. Prints a summary (db path, pids dir, alembic revision, memory outcome).

```bash
ctxr-fsm init
ctxr-fsm init --no-memory --json
```

### `migrate`

Run `alembic upgrade head` against the project DB and report before/after
revisions.

```
ctxr-fsm migrate [--db PATH] [--json]
```

```bash
ctxr-fsm migrate --json
# { "upgraded": true, "revision_before": "ab12...", "revision_after": "cd34..." }
```

### `serve`

Run the unified supervisor (MCP + API + UI in dev mode).

```
ctxr-fsm serve [--db PATH] [--mode dev|prod]
```

| Flag      | Default | Notes                                                |
| --------- | ------- | ---------------------------------------------------- |
| `--mode`  | `dev`   | `dev` adds watchfiles reload and the Vite UI server. |

```
                supervisor
                    |
        +-----------+-----------+
        |           |           |
       MCP         API         UI    (UI only in --mode dev)
      (stdio /   (FastAPI)   (Vite)
        http)
```

SIGINT / SIGTERM drain children with a 5s budget per child, then escalate to
`kill()`.

### `mcp`

Boot the Model Context Protocol server. Used as a Claude Code stdio child or
hosted over HTTP/SSE.

```
ctxr-fsm mcp [--db PATH] [--transport stdio|http] [--host HOST] [--port PORT]
```

| Flag          | Default       | Notes                                          |
| ------------- | ------------- | ---------------------------------------------- |
| `--transport` | `stdio`       | `http` enables FastMCP's SSE transport.        |
| `--host`      | `127.0.0.1`   | Bind address for `http`.                       |
| `--port`      | `0`           | `0` = pick an ephemeral port.                  |

```bash
ctxr-fsm mcp                            # stdio, for Claude Code
ctxr-fsm mcp --transport http --port 0  # OS-picked port, logged on start
```

### `api`

Boot the FastAPI HTTP / SSE server.

```
ctxr-fsm api [--db PATH] [--host HOST] [--port PORT] [--reload]
```

| Flag       | Default       | Notes                                              |
| ---------- | ------------- | -------------------------------------------------- |
| `--host`   | `127.0.0.1`   | Bind explicitly for non-loopback exposure.         |
| `--port`   | `0`           | `0` = OS-picked port (handy for tests and dev).    |
| `--reload` | `False`       | Forwards to uvicorn's auto-reload watcher.         |

### `ui`

Boot the Vite dev server for the UI subproject.

```
ctxr-fsm ui [--port PORT] [--api-port PORT] [--no-install]
```

| Flag           | Default | Notes                                                  |
| -------------- | ------- | ------------------------------------------------------ |
| `--port`       | `5173`  | Vite's canonical port.                                 |
| `--api-port`   | `8765`  | Exported as `VITE_API_PORT`; rewires the proxy target. |
| `--no-install` | `False` | Skip lazy `npm install` on first run.                  |

### `doctor`

Diagnostic dump: DB facts plus supervisor health.

```
ctxr-fsm doctor [--db PATH] [--json]
```

Reports DB path & size, SQLite version, PRAGMAs, alembic revision, per-table
row counts, journal status breakdown, lock count, and for each managed
subsystem (`mcp`, `api`, `ui`): port, pid, `pid_alive`, `probe_url`, and the
`/healthz` body.

```bash
ctxr-fsm doctor --json | jq '.supervisor.subsystems.api'
```

## Run commands

`runs` (plural) groups cross-run queries; `run` (singular) groups per-run
commands.

### `runs ls`

```
ctxr-fsm runs ls [--status STATUS] [--since ISO8601] [--limit N] [--db PATH] [--json]
```

`--status` accepts any single status (`in_progress`, `paused`, `faulted`,
`completed`, `aborted`) plus the keywords `incomplete` and `resumable`.
Defaults to most-recently-updated.

```bash
ctxr-fsm runs ls --status incomplete --limit 5
```

### `run show`

```
ctxr-fsm run show <RUN_ID> [--db PATH] [--json]
```

Prints the run manifest, ASCII state tree, last 20 events, and the newest
unfinalised journal txn (if any).

```
state tree
  plan [completed] (seq=0)
  ├── implement [completed] (seq=1)
  │   └── qa [completed] (seq=2)
  └── fix [in_progress] (seq=3)
```

### `run resume`

```
ctxr-fsm run resume <RUN_ID> [--from-state STATE] [--journal discard|replay] [--db PATH] [--json]
```

Performs the bookkeeping half of resume: optionally discards/marks the open
journal txn, then emits a `run_resumed` event. Engine-driven resume itself
lands in W12.

### `run abort`

```
ctxr-fsm run abort <RUN_ID> [--reason TEXT] [--db PATH] [--json]
```

Atomically flips status to `aborted` and emits `run_aborted`. Refuses runs
already in a terminal state.

### `export`

```
ctxr-fsm export <RUN_ID> <OUTPUT_PATH> [--overwrite] [--db PATH] [--json]
```

Writes a `schema_version: 1` JSON document containing the run, state tree,
events, worker artifacts, aggregates, commit signatures, and the newest
unfinalised journal txn. Pass `-` as `OUTPUT_PATH` to stream to stdout.

```bash
ctxr-fsm export 018f...e2 ./run.json
ctxr-fsm export 018f...e2 - | jq '.counts'
```

### `import`

```
ctxr-fsm import <INPUT_PATH> [--replace] [--db PATH] [--json]
```

Inserts a run from a JSON document produced by `export`. Refuses to clobber an
existing run id without `--replace`. The whole operation is wrapped in one
`@atomic` envelope so a failure rolls back cleanly. See [runs](runs.md) for
schema details.

## Spec commands

Validate and register `FsmSpec` objects from a `<module>:<attribute>` import
path.

### `spec validate`

```
ctxr-fsm spec validate <SPEC_IMPORT_PATH> [--json]
```

In-memory validation only; the DB is never opened. Non-zero exit on failure
makes this safe as a pre-commit / CI gate.

```bash
ctxr-fsm spec validate examples.plan_implement_qa_fix:spec
```

### `spec register`

```
ctxr-fsm spec register <SPEC_IMPORT_PATH> [--project-slug SLUG] [--db PATH] [--json]
```

Validates first, then persists. Reports whether a new version was minted or an
existing content-hash matched. Default project slug is `default`.

### `spec list`

```
ctxr-fsm spec list [--project-slug SLUG] [--db PATH] [--json]
```

Enumerates registered specs grouped by slug. Omit `--project-slug` to walk every
project.

## `install-memory`

Inject (or check) the FSM-usage principles into AI-client memory files.

```
ctxr-fsm install-memory [--target DIR] [--client auto|claude|codex|cursor]
                        [--check] [--dry-run] [--no-symlink] [--json]
```

| Client   | Target                                          | Idiom                                |
| -------- | ----------------------------------------------- | ------------------------------------ |
| Claude   | `CLAUDE.md` or `.claude/CLAUDE.md`              | `@.ctxr-fsm/memory/principles.claude.md` import inside a marker block. |
| Codex    | `AGENTS.md`                                     | Full body inlined inside a marker block. |
| Cursor   | `.cursor/rules/ctxr-fsm.mdc`                    | Standalone rule file (overwrites).   |

Marker block format (idempotent — re-running produces a byte-identical file):

```
<!-- ctxr-fsm:begin v=0.1.0 -->
...payload...
<!-- ctxr-fsm:end -->
```

`--check` exits non-zero when any detected client is missing or out of date —
suitable for CI.

## Environment variables

| Variable         | Used by                  | Effect                                              |
| ---------------- | ------------------------ | --------------------------------------------------- |
| `CTXR_FSM_DB`    | every command            | Default DB path when `--db` is not supplied.        |
| `VITE_API_PORT`  | `ui` (child env)         | Tells the Vite proxy where the API is listening.    |
| `PWD`            | `spec validate/register` | Prepended to `sys.path` so local modules import.    |

## Port and PID files

The supervisor stores lifecycle state under `<project_root>/.ctxr-fsm/`.

```
.ctxr-fsm/
  fsm.db                    # SQLite project database
  ports.json                # { "mcp": 8123, "api": 8765, "ui": 5173 }
  pids/
    mcp.pid                 # { "pid": 12345, "probe_url": "...", "acquired_at": "..." }
    api.pid
    ui.pid
  active-run.json           # { "run_id": "...", "set_at": "..." } (optional)
  memory/
    principles.claude.md    # symlink or copy of the package principles file
```

All three documents are written via `tmp + rename` so a crash mid-write never
leaves a half-written file. PID files are *hints*, not mutexes: callers probe
`/healthz` and check `pid_is_alive` before reusing a slot. `ctxr-fsm doctor`
surfaces all of this for inspection.
