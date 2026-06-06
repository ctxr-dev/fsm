---
name: ctxr-fsm-operational-rules
version: 0.1.0
audience: ai-agents-and-humans
---

# ctxr-fsm operational rules (for developers + AI agents driving fsm)

These rules exist because we kept tripping over the same operational footguns. Each rule below was added after a real incident in development; the incident is cited so the rule's reason is never lost.

## Rule 1: NEVER claim "fixed" without end-to-end visual proof

When the fix is anything UI-related (Vite, layout, render), the only valid proof is a **screenshot of the actual served URL** plus a check that the page loaded without errors. Source code looking right, `npm install` succeeding, and HTTP 200 on the asset URL are NOT proof.

**Required validation pattern when touching the UI layer:**

```python
# Take a screenshot via Playwright headless chromium
# Read the screenshot back via the multimodal Read tool
# Score against explicit criteria
# Show the user the screenshot
```

**Incident**: I "fixed" a Vite elkjs resolve error twice by inspecting source + running `npm install`, then declared it fixed. The user's browser still showed the error both times. Root cause was env-var (`VITE_API_PORT`) missing + Vite dep cache + supervisor pid divergence. Only a screenshot caught the truth.

## Rule 2: Vite has THREE caches, all must be reset together when a dep is added

When a new npm dep lands on main:

1. `npm install` to update `node_modules/` (Rule 2a)
2. `rm -rf node_modules/.vite/deps` to clear Vite's dep optimizer cache (Rule 2b)
3. **Kill the running Vite process** and start a fresh one (Rule 2c). Vite reads the dep optimizer cache at startup; a running Vite won't re-scan.

HMR alone does NOT trigger dep re-resolution. `touch src/lib/foo.ts` will not re-resolve a missing import.

## Rule 3: Vite is started by the ctxr-fsm supervisor with specific env vars

When manually restarting Vite (avoid this when possible), pass at minimum:

```bash
VITE_API_PORT=<api_port> npm run dev -- --host 127.0.0.1 --port <ui_port> --strictPort
```

`apiPort` defaults to **8765** in `vite.config.ts` if env is missing. A manually-spawned Vite without `VITE_API_PORT` will proxy `/api/v1` to a port nothing answers on; the page will render but every API call 404s and the project field shows stale test fixture state.

**Better**: always restart the UI via the supervisor (`kill <ui_pid>; rm pids/ui.pid; ctxr-fsm ensure`) so the env vars are passed correctly.

## Rule 4: Each project has its OWN supervisor, ports, and DB

A workstation can have N concurrent `ctxr-fsm ensure`-managed projects, each with separate ports + separate fsm.db. They do not share specs.

Concrete reality of this workspace today:
- **dummy-fsm-test**: supervisor on api `64526`, mcp `64538`, ui `64547`, DB at `dummy-fsm-test/.ctxr-fsm/fsm.db`
- **fsm (as target for testing skill against itself)**: supervisor on api `61382`, mcp `61403`, ui `61424`, DB at `fsm/.ctxr-fsm/fsm.db`

If you registered a spec by running `python -m skill.install` from project A, project B does not have that spec. There is no global spec registry.

**When the user asks "show me the latest spec"**, ask WHICH project. If unclear, list the specs from BOTH supervisors' APIs before guessing.

## Rule 5: Spec versions are per-project, sequential per-slug

`spec_version` is per `(project_id, slug)` and is computed by counting prior versions with the same slug. A spec body change registered against project A starts at v2; the SAME body registered against project B for the first time also starts at v1 (no slug history).

**Never assume "v4" means the same thing across projects**. Use spec_id (UUIDv7) or hash for cross-project comparison.

**Incident**: I told the visual-iteration agent to score against "v4" but the agent looked at dummy-fsm-test which only had v1 (15 states, no Loop). The agent scored 10/10 on v1; v4-equivalent on dummy-fsm-test only existed after a fresh install which registered it as v2.

## Rule 6: Long-LLM workers will trip the drift detector unless properly signalled

Real LLM worker dispatches take 30s-10min. The drift detector's default `idle_too_long` window was 60s (PR #87 raised to 300s). Even 300s can trip for huge prompts. **Always** during a worker dispatch:

1. The MCP `get_brief` for a worker brief emits `worker_dispatched` (PR #87) which resets the idle counter
2. For prompts expected to run > 5 min, set `Worker.expected_max_wait_seconds` on the spec
3. For runs you're actively driving, call `fsm.heartbeat(run_id)` between Agent dispatches if the loop is slow
4. `drift_paused` status auto-clears on next commit/dispatch/heartbeat ONLY if the contributing signals were idle-only (PR #87). Real drift signals (`off_allowlist_tool_call`, `signature_mismatch`) require explicit `fsm.resume_run`.

## Rule 7: Multi-project environments require URL-disambiguated reporting

When telling the user "open this URL" or "look at this run", always include:
- The full URL with port (so they know which supervisor)
- The project root (so they know which DB)
- The spec_id (not just slug or version number)
- The run_id (UUIDv7 is globally unique; safe to use everywhere)

Example: `http://127.0.0.1:64547/runs/019e94f8-... (project: dummy-fsm-test, spec: code-reviewer v2 hash 3fba6945ba4e)`

## Rule 8: Worktrees leak

Each `git worktree add` creates an isolated checkout with its OWN `node_modules` (resolved by symlink to the parent's, but vite/.cache can diverge). If the worktree's UI dev server is started, you end up with zombie Vite processes bound to random high ports.

**Discipline**:
- After every PR merge that ran in a worktree: `git worktree remove --force <path>` AND verify with `git worktree list`
- Before any visual iteration session: `pkill -f "/private/.../tmp\..*/ui"` to clean stale worktree Vites
- The `lsof -nP -iTCP:LISTEN | grep node` command surfaces every bound port; cross-check against `.ctxr-fsm/ports.json` for the actively-managed supervisors

## Rule 9: Validate against the spec the user is actually looking at

When tuning layout or any visual surface:
1. Ask the user which URL they're hitting (or capture from their reported error)
2. Resolve that URL to a (supervisor_port, project_root, spec_id) tuple via the API
3. Visually iterate against THAT specific spec_id, not against "the latest" or "the canonical one"

**Incident**: User said "still looks bad", I tuned against v1 (because the iteration agent picked the first spec returned by the API), claimed 10/10, user still saw bad layout because they were on v2 with the new wide loop node.

## Rule 10: Show, don't claim

For any finished work:
- If UI: take a screenshot + read it + show it to the user inline
- If backend: curl the endpoint + show the response shape
- If a run: print the run_id + URL + read the report.md + summarise
- If a PR: report PR number + merge SHA + final test counts

Avoid: "should work now", "the fix is in", "verified the import resolves" without showing the actual evidence.

## Rule 11: When confused, list the world

Symptoms of being confused: assuming, guessing, "I think", "probably". Cure:

```bash
# What's running where
ps -ef | grep -E "vite|ctxr-fsm" | grep -v grep
lsof -nP -iTCP:LISTEN | grep node
# What supervisors exist
find /Users/developer/work/projects/ctxr-dev -name "active-mcp.json" -path "*/.ctxr-fsm/*" 2>/dev/null | xargs -I {} sh -c 'echo "=== {} ==="; cat {}'
# Which DBs have which specs
for db in $(find /Users/developer/work/projects/ctxr-dev -name "fsm.db" -path "*/.ctxr-fsm/*" 2>/dev/null); do
  echo "=== $db ==="
  sqlite3 "$db" "SELECT id, slug, version, substr(hash,1,12) FROM fsm_specs ORDER BY slug, version"
done
```

## Rule 12: Per-supervisor pid hygiene

`fsm/.ctxr-fsm/pids/{api,mcp,ui}.pid` and `dummy-fsm-test/.ctxr-fsm/pids/{api,mcp,ui}.pid` track the supervisor's child PIDs. If a child process is killed but the pid file is stale, the next `ctxr-fsm ensure` thinks the subsystem is up + reuses the stale URL + nothing works.

**After any manual kill of a UI/api/mcp subprocess**:
1. Delete the corresponding `pids/<name>.pid` file
2. Delete `pids/supervisor.pid` if it points at a dead supervisor
3. Run `ctxr-fsm ensure --json` to respawn properly

There is also a known bug (per PR #87 notes): supervisor doesn't write `supervisor.pid` reliably on macOS. So `pids/supervisor.pid` may legitimately not exist. That's tolerable.

## What this file is

Shipped inside the `ctxr-fsm` package at `ctxr/fsm/memory/OPERATIONAL_RULES.md`. Reachable from any Python consumer via `get_ssot_doc_path("operational_rules")` after this file is registered in `__init__.py`'s `_SSOT_FILENAMES`. Installed into consumer projects by `ctxr-fsm install-memory` alongside the other SSOT docs.

Treat this file as living. Every time an operational incident wastes more than 10 minutes diagnosing, add a rule here.
