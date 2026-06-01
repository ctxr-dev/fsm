<!-- generated from ctxr/fsm/memory/bootstrap.md by scripts/sync_bootstrap_doc.py — do not edit directly -->
# ctxr-fsm bootstrap: how a skill or agent ensures fsm is ready

**Before any work in a skill that depends on `ctxr-fsm`, run this once per session.** It is idempotent and fast (<500ms) when the project is already initialised and the supervisor is running.

## Step 1 — detect the package, then install ONCE if missing

**Decision tree (no improvising):**

```bash
# Detect. Clear PACKAGE_MISSING first so a pre-set env var from the
# parent shell can't force-install. Probe via `uv run` (without
# --no-project so a project-installed ctxr-fsm IS picked up; the bare
# `uv run` will fall back to a UV_PROJECT_ENVIRONMENT cache if no
# workdir pyproject.toml exists). The `command -v` fallback catches
# pipx / pip --user installs that don't live in a uv environment.
unset PACKAGE_MISSING
uv run --quiet ctxr-fsm --version 2>/dev/null \
  || command -v ctxr-fsm >/dev/null \
  || PACKAGE_MISSING=1
```

If `PACKAGE_MISSING` is set, follow this rule **exactly once**, in this order:

| Workdir state | Run THIS command (and only this command) |
|---|---|
| `pyproject.toml` exists at workdir root | `uv add 'ctxr-fsm[all]'` |
| No `pyproject.toml`, `pipx` on `PATH` | `pipx install 'ctxr-fsm[all]'` |
| Neither | Return `MissingRequirement` and ASK the user (Principle 1). Do not pick a third path on your own. |

After the install command completes, re-run the version check from the top of this step to confirm.

**Hard rules to prevent the common LLM failure modes:**

- **Resolve "this workdir" BEFORE installing.** Walk up from `cwd` looking for `pyproject.toml`; if none, treat `cwd` as the workdir. Run the install command from that directory and nowhere else. An install in the wrong directory lands in the wrong project venv and the next session sees a "missing" package again.
- **Do not run a second installer in a different directory.** One per workdir, ever. If the first command fails, surface the error to the user, do not silently try the next row.
- **Do not try the three installers in sequence "just in case".** Each one targets a different install scope (project venv vs user-isolated vs system); chaining them creates multiple installs that drift and confuse the next session.
- **Concurrency:** if `.ctxr-fsm/install.lock/` exists, another session is mid-install. Wait or back off; do not run the install command. **The CALLER (you, the skill or agent following this doc) wraps the install in the lock — `uv add` / `pipx install` do NOT manage it themselves.** The contract is `mkdir -p .ctxr-fsm/` followed by `mkdir .ctxr-fsm/install.lock` BEFORE the install command, `rmdir .ctxr-fsm/install.lock` AFTER (success or failure). The first `mkdir -p` is idempotent and just ensures the parent directory exists (cold projects have no `.ctxr-fsm/` until `ctxr-fsm ensure` creates it); the second is the atomic lock acquisition that fails fast when another caller holds the lock. On a crash that leaves the lock dangling, the operator unwedges with `rmdir .ctxr-fsm/install.lock`.

If the install fails for any reason (permission, network, version pin), return `MissingRequirement` to the caller with the captured stderr and STOP. Do not improvise.

## Step 2 — bootstrap the project (`ctxr-fsm ensure`)

**Probe first to short-circuit on a warm project.** `ensure --check` is the read-only probe; if it reports `ready`, you are done and MUST skip the rest of this file:

```bash
uv run ctxr-fsm ensure --check --json
```

Routing rule (no improvising):

| `--check` JSON `status` | Action |
|---|---|
| `ready` | **DONE. Skip every other step in this file** and proceed to your skill's actual work. The project is fully bootstrapped. |
| `missing_init` / `missing_supervisor` / `missing_mcp_config` / `missing_memory` | Run the full `ensure` command below to apply the missing axis. |
| `failed` | The check itself succeeded but the project is in an unrecoverable state (e.g. corrupt DB). Return `MissingRequirement` to the caller with the JSON envelope and STOP — do not re-run `ensure --json` blindly. |
| Exit non-zero AND no JSON on stdout | The package itself isn't installed in this workdir — go back to Step 1. |

When you need to apply changes (anything other than `ready`):

```bash
uv run ctxr-fsm ensure --json
```

This is idempotent. On a cold project it: creates `.ctxr-fsm/fsm.db`, runs migrations, installs principles into CLAUDE.md/AGENTS.md/.cursor/rules, registers `ctxr-fsm` as an MCP server in the active client config(s), boots the supervisor (MCP + FastAPI + UI). On a warm project it returns in <500ms confirming everything is up.

Parse the JSON output. Capture `mcp_http_url` (the HTTP-SSE MCP endpoint for current-session use) and `api_url`.

### Per-client reload semantics

The stdio MCP server registration written by `ctxr-fsm ensure` lands in a client-specific config file. Each client picks the new config up at a different lifecycle point — none of them re-read it mid-session — so the in-session contract is the same everywhere (use the HTTP-SSE fallback from Step 3), but the "force reload now" gesture differs:

- **Claude Code**: stdio config in `.mcp.json` is loaded on session start. In-session: keep using the HTTP-SSE fallback (Step 3); next session will pick up stdio automatically. To force reload now: close the Claude Code window/tab and reopen the project, OR run the `/mcp` reconnect command in Claude Code if available.
- **Codex** (Codex CLI): stdio config in `~/.codex/config.toml` is loaded on next `codex` invocation. In-session: keep using HTTP-SSE fallback; on the next codex command, the stdio MCP becomes available. To force reload now: exit the current codex session and start a new one.
- **Cursor**: stdio config in `~/.cursor/mcp.json` is loaded on Cursor reload. In-session: keep using HTTP-SSE fallback. To force reload now: Cmd/Ctrl+Shift+P -> "Developer: Reload Window" OR fully restart Cursor.

## Step 3 — choose your driver: stdio MCP, Python API, or HTTP-SSE

Three driver paths, in preference order. Pick the FIRST one that works in your session.

**Path A — stdio MCP (the registered, primary path).** Call `fsm.healthcheck()` via the MCP tool surface. If it returns ok, proceed to your skill's actual work; the stdio MCP server is registered with this client and reachable. This is the path you SHOULD be on after the per-client reload listed in Step 2.

**Path B — Python API (recommended fallback when stdio isn't live yet).** If `fsm.healthcheck()` is unavailable (the stdio config was JUST registered and won't be effective until the next session per Step 2's per-client reload table), drive the run directly via Python in the current session. The fsm engine is installable; you can open the project and step it forward without going through any IPC layer:

```bash
uv run python - <<'PY'
from pathlib import Path
from ctxr.fsm.sqlite import Project

project = Project.open(Path(".ctxr-fsm/fsm.db"))
run = project.start_run(spec_id="<your-spec-slug>", args={...})
print("run_id:", run.id)
# Loop: read brief → dispatch sub-agent → commit_outputs → repeat.
# project.runs.get(run.id).brief() ... project.run.commit_outputs(run.id, outputs)
PY
```

This is the path skills should default to when invoked from a Bash-driven shell loop (the cycle 1 sub-agent used this; the MCP-over-SSE path in Path C is harder to drive from a single Bash tool call).

**Path C — HTTP-SSE MCP fallback (advanced, requires an SSE-aware client).** The supervisor's HTTP MCP endpoint is a JSON-RPC 2.0 transport over Server-Sent Events. To exercise it you need an SSE consumer (e.g., the official MCP Python SDK's `ClientSession` over `sse_client`) — you cannot drive it with a single `curl -X POST`. Capture the URL from `ensure --json`'s `subsystems.mcp.http_url` field; pair it with the parallel POST endpoint `/messages/?session_id=...` for the request side. Prefer Path A or B unless your harness already speaks SSE.

## Step 4 — register your skill's spec (one-time per project)

Skills that ship an FsmSpec must register it before `start_run`:

```bash
uv run ctxr-fsm spec register <your.module.path:fsm>
```

Idempotent — re-registering the same spec at the same version is a no-op. If the spec's body changed, fsm bumps `fsm_specs.version` automatically.

## Step 5 — drive the run (LLM-as-orchestrator)

Use the `fsm.*` MCP tool family: `fsm.start_run(spec_id, args)` → loop `fsm.get_brief(run_id)` → for worker states, dispatch a sub-agent with the brief's prompt + inputs → `fsm.commit_outputs(run_id, outputs, signature)` → repeat until brief is terminal. Inline states are advanced server-side; you don't see them as briefs.

See [`principles.md`](./principles.md) for the rules every FSM-driving agent must follow once a run is active.
