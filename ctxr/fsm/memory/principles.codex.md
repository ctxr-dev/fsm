<!-- This file is generated from ctxr-fsm package memory. Run `ctxr-fsm install-memory --check` to confirm sync. -->

---
name: ctxr-fsm-principles
version: 0.2.0
audience: ai-agents
---

# ctxr-fsm: how an agent must use the FSM

You have access to an MCP server named "ctxr-fsm" exposing fsm.* tools (healthcheck, list_specs, register_spec, start_run, get_brief, commit_outputs, confirm_commit, resume_run, abort_run, list_runs, get_run, subscribe_events, inspect_journal, recover_journal, observe_tool_call, list_consumers, list_producers).

## Principle 0: Bootstrap before work

Before any FSM-using skill or agent does ANYTHING else, follow the bootstrap procedure at `@.ctxr-fsm/memory/bootstrap.md` (inlined below for this client) to ensure `ctxr-fsm` is installed, the project is initialised, the MCP server is registered with the active client, and the supervisor is running. The bootstrap is idempotent and fast (<500ms) when everything is already up, so calling it once per session has negligible cost. Do NOT skip it on the assumption that "it was up yesterday" — sessions, clients, and project state drift. Principles 1 through 10 below describe how to behave once a run is active; Principle 0 is the gate that makes any of them reachable.

<!-- bootstrap-content-begin (inlined for clients that don't follow @ imports) -->

### ctxr-fsm bootstrap: how a skill or agent ensures fsm is ready

**Before any work in a skill that depends on `ctxr-fsm`, run this once per session.** It is idempotent and fast (<500ms) when the project is already initialised and the supervisor is running.

#### Step 1 — confirm the package is installed

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

#### Step 2 — bootstrap the project (`ctxr-fsm ensure`)

```bash
uv run ctxr-fsm ensure --json
```

This is idempotent. On a cold project it: creates `.ctxr-fsm/fsm.db`, runs migrations, installs principles into CLAUDE.md/AGENTS.md/.cursor/rules, registers `ctxr-fsm` as an MCP server in the active client config(s), boots the supervisor (MCP + FastAPI + UI). On a warm project it returns in <500ms confirming everything is up.

Parse the JSON output. Capture `mcp_http_url` (the HTTP-SSE MCP endpoint for current-session use) and `api_url`.

##### Per-client reload semantics

The stdio MCP server registration written by `ctxr-fsm ensure` lands in a client-specific config file. Each client picks the new config up at a different lifecycle point — none of them re-read it mid-session — so the in-session contract is the same everywhere (use the HTTP-SSE fallback from Step 3), but the "force reload now" gesture differs:

- **Claude Code**: stdio config in `.mcp.json` is loaded on session start. In-session: keep using the HTTP-SSE fallback (Step 3); next session will pick up stdio automatically. To force reload now: close the Claude Code window/tab and reopen the project, OR run the `/mcp` reconnect command in Claude Code if available.
- **Codex** (Codex CLI): stdio config in `~/.codex/config.toml` is loaded on next `codex` invocation. In-session: keep using HTTP-SSE fallback; on the next codex command, the stdio MCP becomes available. To force reload now: exit the current codex session and start a new one.
- **Cursor**: stdio config in `~/.cursor/mcp.json` is loaded on Cursor reload. In-session: keep using HTTP-SSE fallback. To force reload now: Cmd/Ctrl+Shift+P -> "Developer: Reload Window" OR fully restart Cursor.

#### Step 3 — choose your driver: stdio MCP, Python API, or HTTP-SSE

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
### Loop: read brief → dispatch sub-agent → commit_outputs → repeat.
### project.runs.get(run.id).brief() ... project.run.commit_outputs(run.id, outputs)
PY
```

This is the path skills should default to when invoked from a Bash-driven shell loop (the cycle 1 sub-agent used this; the MCP-over-SSE path in Path C is harder to drive from a single Bash tool call).

**Path C — HTTP-SSE MCP fallback (advanced, requires an SSE-aware client).** The supervisor's HTTP MCP endpoint is a JSON-RPC 2.0 transport over Server-Sent Events. To exercise it you need an SSE consumer (e.g., the official MCP Python SDK's `ClientSession` over `sse_client`) — you cannot drive it with a single `curl -X POST`. Capture the URL from `ensure --json`'s `subsystems.mcp.http_url` field; pair it with the parallel POST endpoint `/messages/?session_id=...` for the request side. Prefer Path A or B unless your harness already speaks SSE.

#### Step 4 — register your skill's spec (one-time per project)

Skills that ship an FsmSpec must register it before `start_run`:

```bash
uv run ctxr-fsm spec register <your.module.path:fsm>
```

Idempotent — re-registering the same spec at the same version is a no-op. If the spec's body changed, fsm bumps `fsm_specs.version` automatically.

#### Step 5 — drive the run (LLM-as-orchestrator)

Use the `fsm.*` MCP tool family: `fsm.start_run(spec_id, args)` → loop `fsm.get_brief(run_id)` → for worker states, dispatch a sub-agent with the brief's prompt + inputs → `fsm.commit_outputs(run_id, outputs, signature)` → repeat until brief is terminal. Inline states are advanced server-side; you don't see them as briefs.

See [`principles.md`](./principles.md) for the rules every FSM-driving agent must follow once a run is active.

<!-- bootstrap-content-end -->


## Principle 1: pre-check before you act

Before any FSM-using skill or agent does anything, call fsm.healthcheck(). If it fails, return a MissingRequirement reply citing the install command (`pip install ctxr-fsm` or `uv add ctxr-fsm`) and STOP. Do not improvise. Do not fall back to inline behaviour.

## Principle 2: tool surface is constrained during a run

When a run is active, the brief returned by fsm.get_brief carries an `allowed_tools` list. Your tool surface is exactly `fsm.* + current_state.allowed_tools` for the duration of that state. Do NOT call other tools while a run is in progress, even if they look useful. The drift detector will see and pause the run.

## Principle 3: never inline state

- Always read the next step from `fsm.get_brief(run_id)`.
- Always commit work via `fsm.commit_outputs(run_id, outputs, signature)`.
- If two-phase commit is in use (the brief carries `requires_confirm: true`), follow up with `fsm.confirm_commit(token, expected_next_state)`.

Never write run state to /tmp or any side channel. The DB is the source of truth.

## Principle 4: schema rejection means re-do the work

If fsm.commit_outputs returns an error envelope with `error: output_schema_violation`, that is the contract working. Re-read `worker.response_schema` from your last brief, fix the shape, retry. Do NOT route around the schema by emitting a free-form note.

## Principle 5: faults are inspection points, not failure modes

On fault, do not improvise a recovery. Call `fsm.resume_run` (with `from_state` or `journal` action) per the operator's instruction, or `fsm.recover_journal`. A fault is for a human (or you, in inspection mode) to look at; it is not a "retry-with-different-inputs" situation unless the post-validation explicitly says so.

## Principle 6: cosignatures matter

The outputs you commit must be derived from the brief + inputs you were given. The cosignature in fsm.commit_outputs is `sha256(brief_id || canonical_json(inputs) || canonical_json(outputs) || session_id)` and is verified at the server. Fabricating outputs is detectable and will be rejected with `error: signature_mismatch`.

## Principle 7: the verifier is right

If an adversarial verifier rejects your output (commit returns `error: verifier_rejected` with the verifier's findings), the verifier is right and you are wrong. Re-do the work. Do NOT argue with the verifier or attempt to commit the same outputs again.

## Principle 8: the spec is the source of truth

A state that looks "wrong" to you is a bug to surface (open an issue, pause the run via fsm.abort_run with reason, or escalate to a human), not a flow to work around.

## Principle 9: observe non-fsm tool calls during a run

When a run is active and you call a tool outside the fsm.* family (e.g. Bash, Read, WebFetch, sub-agents), call `fsm.observe_tool_call(producer_kind, producer_name, tool_name, args_redacted, succeeded, run_id)` for each one. This feeds the drift detector and the run audit log. Skipping this is itself a drift signal.

## Principle 10: subscribe for events when reasoning across states

If you need to react to events from other producers (UI, another agent, a webhook), use `fsm.subscribe_events` to register as a consumer with the right filter. Don't poll fsm.get_run in a loop.

## Lifecycle quick reference

Skill startup: fsm.healthcheck -> fsm.list_specs (find the spec) -> fsm.start_run (returns run_id + first brief).
State loop: dispatch worker per brief -> fsm.commit_outputs (and confirm_commit if required) -> new brief OR terminal.
On fault: fsm.inspect_journal -> operator decides fsm.recover_journal or fsm.resume_run.
Shutdown: leave the run alone; the supervisor + journal handle cleanup.

## What this file is

This file ships inside the ctxr-fsm Python package at `ctxr/fsm/memory/principles.md` and is the canonical source. The per-client adapters in this same directory (`principles.claude.md`, `principles.codex.md`, `principles.cursor.md`) are generated FROM this file and kept in lockstep. To install into your project memory, run:

    ctxr-fsm install-memory --client auto

To verify the installed version matches the package version, run:

    ctxr-fsm install-memory --check
