# Claude Code pre-tool-use hook (`fsm-guard`)

This document covers the W12 layer-4 reference hook that ships with
`ctxr-fsm`. It is the consumer-facing companion to the in-process
enforcement layers (cosignatures, two-phase commit, verifier panel,
spec-hash lock, drift detector) and is the only enforcement piece that
lives in the **client** rather than the FSM server.

## What the hook does

When an `fsm.*` run is active, the FSM server writes a small marker
file at:

```
<project_root>/.ctxr-fsm/active-run.json
```

with the shape:

```json
{
  "run_id": "0ce4d2a4-7c1a-4d27-9d6e-2c4f8b1e2d3f",
  "set_at": "2026-05-30T12:00:00+00:00",
  "current_state": "draft",
  "allowed_tools": ["Read", "Grep"]
}
```

On every Claude Code tool invocation, the pre-tool-use hook:

1. Walks upwards from `$CLAUDE_PROJECT_DIR` (and, as a fallback, the
   process cwd) looking for the nearest `.ctxr-fsm/active-run.json`.
2. If no marker is found, exits `0` (allow everything — installing the
   hook never bricks a session that does not have an FSM run active).
3. If a marker is found, reads `allowed_tools`, adds the implicit
   `fsm.*` wildcard, and checks whether `tool_name` matches any
   pattern.
4. Match → exit `0` (allow).
   No match → exit `1` and write a structured JSON reason to stderr.

The hook script has **zero non-stdlib dependencies** so a consumer
project can drop it under `.claude/hooks/` without installing
`ctxr-fsm` itself.

## Files in this reference

| Path | Purpose |
|------|---------|
| `.claude/hooks/pre-tool-use.fsm-guard.py` | Canonical Python implementation. |
| `.claude/hooks/pre-tool-use.fsm-guard.sh` | Bash shim that forwards stdin/exit-code to the Python script. |

Either can be wired into Claude Code; the Python one is the recommended
default (faster startup, no `bash` dependency on Windows / NixOS).

## Installing in a consumer project

1. Copy the two files into the consumer's `.claude/hooks/` directory:

   ```bash
   mkdir -p .claude/hooks
   cp <ctxr-fsm-repo>/.claude/hooks/pre-tool-use.fsm-guard.py \
      .claude/hooks/
   chmod +x .claude/hooks/pre-tool-use.fsm-guard.py
   ```

2. Register the hook in `.claude/settings.json`:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "*",
           "hooks": [
             {
               "type": "command",
               "command": ".claude/hooks/pre-tool-use.fsm-guard.py"
             }
           ]
         }
       ]
     }
   }
   ```

   The `matcher: "*"` ensures every tool call goes through the guard;
   the per-call cost is a single small Python script invocation plus a
   tiny JSON parse.

3. Restart Claude Code (or reload settings) — subsequent tool calls
   will consult the marker.

## Marker contract

The marker is produced by `ctxr.fsm.cli.lifecycle.primitives.write_active_run_marker`
and updated by the MCP tools:

| Trigger | Marker action |
|---------|---------------|
| `fsm.start_run` succeeds | Write marker with entry state's `allowed_tools`. |
| `fsm.confirm_commit` advances | Rewrite marker with new state's `allowed_tools`. |
| `fsm.confirm_commit` terminal | Clear marker. |
| `fsm.abort_run` succeeds | Clear marker. |
| Supervisor shutdown | Clear marker (best-effort). |

The marker location is per-project (under `.ctxr-fsm/`, not `~/`) so
multiple checkouts of the same repo on one machine never overwrite
each other.

## Discovery for non-cwd project layouts

When the worker runs in a different cwd than the project root, set:

```bash
export CTXR_FSM_PROJECT_ROOT=/path/to/project
```

and the FSM tools will write the marker under that directory instead.
The hook still walks upwards from `$CLAUDE_PROJECT_DIR`, so as long as
the project root is an ancestor of `$CLAUDE_PROJECT_DIR` the marker is
found without further configuration.

## Soft-fail behaviour

The hook is deliberately conservative:

* No marker → allow.
* Empty `allowed_tools` → allow (unrestricted run).
* Malformed marker → allow (stderr line records the issue).
* Missing `tool_name` in payload → allow.
* Internal exception → allow (stderr line records the issue, exit `0`).

The hook will only ever exit `1` when:

* The marker exists, is well-formed, and declares a non-empty
  `allowed_tools` list, **and**
* The payload carries a `tool_name`, **and**
* That `tool_name` matches neither the `allowed_tools` list nor the
  implicit `fsm.*` wildcard.

This biases towards "agent keeps working" over "hook blocks everything
when it can't tell what to do" — the FSM server's in-process
enforcement layers (cosignatures, two-phase commit) are the hard
backstop; the hook is a fast, advisory filter.

## Blocked payload shape

When the hook blocks a tool call, it writes one line of structured
JSON to stderr before exiting `1`:

```json
{"blocked": true, "tool": "Bash", "allowed": ["Read", "Grep", "fsm.*"], "reason": "tool 'Bash' not in active FSM run's allowed_tools (run_id=…, state=draft, allowed=[…])"}
```

Claude Code surfaces stderr to the agent verbatim, so the agent sees
the structured payload and can decide to call `fsm.commit_outputs` to
move past the restrictive state rather than retrying a forbidden tool.

## Local override (advanced)

For workflows that need to bypass the guard temporarily (e.g. a
human-in-the-loop investigation), simply delete the marker:

```bash
rm .ctxr-fsm/active-run.json
```

The next FSM-driven advance recreates it; in the meantime every tool
call is allowed. This is the right knob for "I'm stepping outside the
FSM for a moment"; for "I want to abort the run entirely" prefer
`fsm.abort_run` (which clears the marker as part of the lifecycle and
records the abort in the audit trail).
