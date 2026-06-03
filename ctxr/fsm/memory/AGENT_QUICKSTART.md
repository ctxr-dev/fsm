---
name: ctxr-fsm-agent-quickstart
version: 0.1.0
audience: ai-agents
---

# ctxr-fsm: agent quickstart

You are an AI agent driving an FSM-managed workflow. This is the five-minute version of how to do that correctly. The deep contract is in [`principles.md`](./principles.md); the bootstrap procedure is in [`bootstrap.md`](./bootstrap.md); the skill-authoring template is in [`SKILL_TEMPLATE.md`](./SKILL_TEMPLATE.md); the cross-FSM gate protocol is in [`GATE_CONTRACT.md`](./GATE_CONTRACT.md).

## What an FSM run is

A run is one execution of an FSM spec against a project DB. The spec declares states (worker / loop / inline / gate / terminal) and the transitions between them. The engine sequences advance; you (the LLM) supply only the worker outputs the engine asks for. You never decide what state runs next.

## Lifecycle on the happy path

1. `fsm.healthcheck()` — must return ok before any other call. If it fails, return MissingRequirement and STOP.
2. `fsm.list_specs()` — find the spec slug you need; record its id.
3. `fsm.start_run(spec_id, args)` — engine returns `run_id` + the first `Brief`.
4. Loop:
   - Read the `Brief` returned by `start_run` / the previous `commit_outputs`.
   - If `brief.terminal == true`, the run is done. Stop.
   - If `brief.has_worker == true`, dispatch a sub-agent with `brief.worker.prompt_template` + `brief.inputs`. Receive its outputs matching `brief.worker.response_schema`. Compute the cosignature. Call `fsm.commit_outputs(run_id, outputs, signature)`. Receive the next `Brief`.
   - If the brief carries a gate (W23g): follow `GATE_CONTRACT.md`.
5. On any error envelope from `commit_outputs`: re-read the brief, fix the output shape, retry. Never route around schema rejection.

## The five things to remember

1. **Bootstrap is one command, idempotent, fast.** `uv run ctxr-fsm ensure --json` returns `{status: "ready"}` in <500 ms on a warm project. Call it once per session. Do NOT skip it. See `bootstrap.md`.
2. **The brief is your only input.** `brief.inputs` is what the engine resolved for you. `brief.worker.prompt_template` is what you (or the sub-agent you dispatch) consumes. If the template embeds `{{ ... }}` placeholders, the engine has already rendered them; you see the resolved string.
3. **The schema is your only output shape.** `brief.worker.response_schema` is the contract. Validate yourself before commit. The server validates again and rejects mismatches with `error: output_schema_violation`. Fix the shape; do not work around it.
4. **Your tool surface shrinks during a run.** `brief.allowed_tools` is the allowlist. You may also call `fsm.*`. Anything else feeds the drift detector and may auto-pause the run.
5. **Observe every non-fsm tool call.** When you call Bash / Read / WebFetch / a sub-agent during an active run, follow with `fsm.observe_tool_call(...)`. Skipping this is itself a drift signal.

## The cosignature

Every `commit_outputs` carries `signature = sha256(brief_id || canonical_json(inputs) || canonical_json(outputs) || session_id)`. The server recomputes it and rejects mismatches with `error: signature_mismatch`. This makes fabricated outputs detectable. Do not skip the signature.

`canonical_json` here is sorted keys, compact separators, UTF-8. The TypeScript companion lives at `ui/src/lib/canonicalJson.ts`.

## What to do on a fault

Run faulted? Do NOT improvise a retry. Call `fsm.inspect_journal(run_id)`, then either:
- `fsm.recover_journal(run_id, action='discard'|'replay')` if the journal points at the right action, or
- `fsm.resume_run(run_id, from_state=…)` if a specific state needs re-entry, or
- escalate to a human via `fsm.abort_run(run_id, reason=…)`.

## What to do on a gate

A gate state (`state.kind == 'gate'`) pauses the run waiting for cross-run data. Call `fsm.resolve_gate(run_id, state_entry_seq, value=...)` with an inline value OR `binding=...` referencing another run's state output. See [`GATE_CONTRACT.md`](./GATE_CONTRACT.md) for the full protocol.

## What to do if you want to AUTHOR a skill that uses fsm

Read [`SKILL_TEMPLATE.md`](./SKILL_TEMPLATE.md). The template covers the spec module shape, inline-handler registration, worker prompt files, the SKILL.md preamble, and the test layout.

## What this file is

Shipped inside the `ctxr-fsm` package at `ctxr/fsm/memory/AGENT_QUICKSTART.md`. The canonical place for any agent's "how do I use this thing" question. Installed into consumer projects by `ctxr-fsm install-memory`; available directly via `importlib.resources` from any Python consumer.
