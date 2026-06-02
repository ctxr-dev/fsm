<!-- This file is generated from ctxr-fsm package memory. Run `ctxr-fsm install-memory --check` to confirm sync. -->

---
name: ctxr-fsm-principles
version: 0.2.0
audience: ai-agents
---

# ctxr-fsm: how an agent must use the FSM

You have access to an MCP server named "ctxr-fsm" exposing fsm.* tools (healthcheck, list_specs, register_spec, start_run, get_brief, commit_outputs, confirm_commit, resume_run, abort_run, list_runs, get_run, subscribe_events, inspect_journal, recover_journal, observe_tool_call, list_consumers, list_producers).

## Principle 0: Bootstrap before work

Before any FSM-using skill or agent does ANYTHING else, follow the bootstrap procedure at @.ctxr-fsm/memory/bootstrap.md to ensure `ctxr-fsm` is installed, the project is initialised, the MCP server is registered with the active client, and the supervisor is running. The bootstrap is idempotent and intended to be fast on the warm path (measured 1.5-1.8s on cycle 1) when everything is already up, so calling it once per session has negligible cost. Do NOT skip it on the assumption that "it was up yesterday" — sessions, clients, and project state drift. Principles 1 through 10 below describe how to behave once a run is active; Principle 0 is the gate that makes any of them reachable.

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
