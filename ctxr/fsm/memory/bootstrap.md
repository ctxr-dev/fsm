# ctxr-fsm bootstrap: how a skill or agent ensures fsm is ready

**Before any work in a skill that depends on `ctxr-fsm`, run this once per session.** It is idempotent and fast (<500ms) when the project is already initialised and the supervisor is running.

## Step 1 — confirm the package is installed

```bash
uv run ctxr-fsm --version  # or: pipx run ctxr-fsm --version
```

If this prints a version, skip to Step 2. If it errors with "command not found":

```bash
uv add ctxr-fsm                              # in a uv project
pipx install ctxr-fsm                        # globally, isolated
pip install --user ctxr-fsm                  # last resort
```

Then retry the version check. If it still fails, return `MissingRequirement` and STOP — do NOT improvise.

## Step 2 — bootstrap the project (`ctxr-fsm ensure`)

```bash
uv run ctxr-fsm ensure --json
```

This is idempotent. On a cold project it: creates `.ctxr-fsm/fsm.db`, runs migrations, installs principles into CLAUDE.md/AGENTS.md/.cursor/rules, registers `ctxr-fsm` as an MCP server in the active client config(s), boots the supervisor (MCP + FastAPI + UI). On a warm project it returns in <500ms confirming everything is up.

Parse the JSON output. Subsystem URLs live under `subsystems.<name>.http_url`. Capture `subsystems.mcp.http_url` (the HTTP-SSE MCP endpoint for current-session use), `subsystems.api.http_url`, and `subsystems.api.docs_url` (Swagger). Health probes are at `subsystems.<name>.healthz_url`.

### Per-client reload semantics

The stdio MCP server registration written by `ctxr-fsm ensure` lands in a client-specific config file. Each client picks the new config up at a different lifecycle point — none of them re-read it mid-session — so the in-session contract is the same everywhere (use the HTTP-SSE fallback from Step 3), but the "force reload now" gesture differs:

- **Claude Code**: stdio config in `.mcp.json` is loaded on session start. In-session: keep using the HTTP-SSE fallback (Step 3); next session will pick up stdio automatically. To force reload now: close the Claude Code window/tab and reopen the project, OR run the `/mcp` reconnect command in Claude Code if available.
- **Codex** (Codex CLI): stdio config in `~/.codex/config.toml` is loaded on next `codex` invocation. In-session: keep using HTTP-SSE fallback; on the next codex command, the stdio MCP becomes available. To force reload now: exit the current codex session and start a new one.
- **Cursor**: stdio config in `~/.cursor/mcp.json` is loaded on Cursor reload. In-session: keep using HTTP-SSE fallback. To force reload now: Cmd/Ctrl+Shift+P -> "Developer: Reload Window" OR fully restart Cursor.

## Step 3 — try stdio MCP first, fall back to HTTP-SSE

Call `fsm.healthcheck()` via the MCP tool surface. If it returns ok, proceed to your skill's actual work — the stdio MCP server is registered with this client and reachable.

If `fsm.healthcheck()` is unavailable (the stdio config was JUST registered and won't be effective until next session) call the HTTP-SSE fallback via Bash:

```bash
MCP_HEALTHZ=$(uv run ctxr-fsm ensure --json | jq -r .subsystems.mcp.healthz_url)
curl -s "$MCP_HEALTHZ"
```

and continue calling fsm primitives via the URL captured from `subsystems.mcp.http_url` for THIS session. The next session will pick up the stdio config automatically.

## Step 4 — register your skill's spec (one-time per project)

Skills that ship an FsmSpec must register it before `start_run`:

```bash
uv run ctxr-fsm spec register <your.module.path:fsm>
```

Idempotent — re-registering the same spec at the same version is a no-op. If the spec's body changed, fsm bumps `fsm_specs.version` automatically.

## Step 5 — drive the run (LLM-as-orchestrator)

Use the `fsm.*` MCP tool family: `fsm.start_run(spec_id, args)` → loop `fsm.get_brief(run_id)` → for worker states, dispatch a sub-agent with the brief's prompt + inputs → `fsm.commit_outputs(run_id, outputs, signature)` → repeat until brief is terminal. Inline states are advanced server-side; you don't see them as briefs.

See the ctxr-fsm principles your client already loaded for the rules every FSM-driving agent must follow once a run is active. Where to find them depends on the client: Claude Code stages them at `.ctxr-fsm/memory/principles.claude.md` (next to this file) and imports them via the `@`-marker block in `CLAUDE.md`; Codex inlines them into `AGENTS.md`; Cursor inlines them into `.cursor/rules/ctxr-fsm.mdc`. The body is the same in every adapter and is the source of truth.
