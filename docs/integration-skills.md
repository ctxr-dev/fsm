# Integrating ctxr-fsm into a skill or agent

This is the worked-example blueprint for any **skill, agent, or service**
that wants to use `ctxr-fsm` as its FSM substrate — either by migrating an
existing pipeline or by being authored against the FSM from day one.

> **Scope note.** The canonical case study throughout this guide is
> `skill-code-review` (the lens-specialist pipeline that today ships as a
> Node CLI under `legacy-js/`). Its **migration to the Python `ctxr-fsm`
> substrate is explicitly OUT OF SCOPE for the current `fsm/` repo plan**
> — `legacy-js/` is preserved untouched. Use this guide either as the
> blueprint for that future migration **or** as the green-field recipe
> for a brand-new skill that targets `ctxr-fsm` from the start.

Cross-references:
- [`docs/mcp-tools.md`](./mcp-tools.md) — the 17 `fsm.*` MCP tool surface
- [`docs/api.md`](./api.md) and [`docs/http-api.md`](./http-api.md) — Python facade + REST/SSE
- [`docs/enforcement.md`](./enforcement.md) — capability, integrity, adversarial, observational gates
- [`examples/code_review_pipeline.py`](../examples/code_review_pipeline.py) — the runnable end-to-end template
- [`ctxr/fsm/memory/principles.md`](../ctxr/fsm/memory/principles.md) — the agent-facing discipline

---

## TL;DR table

| Step | What you do | Where |
|---|---|---|
| 1. Declare requirement | `SKILL.md` frontmatter: `requires.fsm.{mcp_server, min_version, server_must_be_reachable}` | Skill manifest |
| 2. Pre-check at startup | Call `fsm.healthcheck`; return `MissingRequirement` on failure | Skill preamble |
| 3. Define the FSM spec | Pydantic `FsmSpec` in a Python module | `<skill>/specs/<role>.py` |
| 4. Register the spec | `ctxr-fsm spec register <module>:<attr>` (one-time, or at install) | `ctxr-fsm` CLI |
| 5. Start a run | `fsm.start_run(spec_id, args)` | MCP or API |
| 6. Brief → dispatch → commit loop | `fsm.get_brief` → dispatch worker → `fsm.commit_outputs` (+ `fsm.confirm_commit`) | MCP, API, or Python |
| 7. Honour `allowed_tools` | Read `brief.allowed_tools`; restrict tool surface during the state | Hook + skill prompt |
| 8. Sign every commit | `CommitSignature.compute(brief_id, inputs, outputs, session_id)` | Skill code |
| 9. Observe non-`fsm` tool calls | `fsm.observe_tool_call` for every `Bash`/`Read`/`WebFetch`/sub-agent call | Skill code or harness |
| 10. Handle faults via `fsm.resume_run` / `fsm.recover_journal` | DON'T improvise recovery | Operator step |

The 10 steps map 1:1 onto the lifecycle quick reference in
[`principles.md`](../ctxr/fsm/memory/principles.md): `healthcheck → list_specs
→ start_run → (get_brief → dispatch → commit_outputs [→ confirm_commit])* →
terminal`.

---

## The three integration shapes

Pick **one**. They share the same FSM spec, the same DB, and the same
enforcement model — only the transport changes.

### A. Python in-process

The skill runs in the same Python process as the FSM and calls the
`Project` facade directly. Minimum latency, no MCP, no HTTP. Best for
service skills that already live in a Python codebase.

```python
# my_skill/runner.py
from pathlib import Path
import uuid
from ctxr.fsm.core import RunCtx, advance, build_brief
from ctxr.fsm.core.models import CommitSignature
from ctxr.fsm.sqlite import Project
from my_skill.specs.lens_specialist import spec
from my_skill.workers import dispatch_lens_worker  # your code

def review(target: str, db_path: Path, session_id: str) -> dict:
    with Project.open(db_path) as project:
        registered = project.register_spec(spec, project_slug="skill-code-review")
        run = project.start_run(spec_id=registered.spec.id, args={"target": target})

        env: dict = dict(run.args or {})
        state_id, iteration_n = spec.entry, None
        while True:
            state = spec.get_state(state_id)
            brief = build_brief(spec, state, env=env, run_id=uuid.UUID(run.id),
                                iteration_n=iteration_n)
            if state.worker is None and state.loop is None:
                outputs = {}                                # terminal
            else:
                outputs = dispatch_lens_worker(brief)       # YOUR code
                _ = CommitSignature.compute(                # see step 8
                    brief.brief_id, brief.inputs, outputs, session_id,
                )

            ctx = RunCtx(run_id=uuid.UUID(run.id), fsm_id=spec.id,
                         current_state=state_id, iteration_n=iteration_n, env=env)
            result = advance(spec, ctx, outputs)
            if result.kind == "fault":
                raise RuntimeError(f"FSM fault: {result.reason}")
            if result.kind == "loop_continue":
                iteration_n = result.iteration_n
                continue
            env = {**env, **outputs}
            if result.kind == "terminal":
                return {"verdict": env.get("verdict"), "run_id": run.id}
            state_id = result.next_state or ""
            iteration_n = 1 if spec.get_state(state_id).loop else None
```

The canonical version of this loop — with full persistence, aggregator
hints, post-validation, and event emission — lives in
[`examples/code_review_pipeline.py`](../examples/code_review_pipeline.py).

### B. MCP-driven

The skill is an LLM agent (Claude Code, Codex CLI, Cursor) that calls
`fsm.*` tools over MCP stdio. The skill never imports `ctxr.fsm`; the
skill's "runtime" is the MCP client. This is the default shape for
prompt-defined skills.

```text
# .claude/skills/skill-code-review/SKILL.md   (preamble excerpt)
1. Call fsm.healthcheck(). If it fails, return MissingRequirement with
   install command "pip install ctxr-fsm" and STOP.
2. spec_id = (fsm.list_specs() | first where slug == "code-review-lens-specialist").id
3. run = fsm.start_run(spec_id=spec_id, args={"target": <user input>})
4. Loop:
     brief = fsm.get_brief(run_id=run.id)
     if brief.terminal: return brief.outputs
     outs = dispatch_worker(brief.worker.prompt_template, brief.inputs,
                            schema=brief.worker.response_schema,
                            tools=brief.allowed_tools)   # honour the surface
     sig = CommitSignature.compute(brief.brief_id, brief.inputs,
                                    outs, session_id)
     ack = fsm.commit_outputs(run_id=run.id, outputs=outs, signature=sig)
     if ack.commit_token:
         fsm.confirm_commit(token=ack.commit_token,
                            expected_next_state=ack.next_state)
```

The Claude Code pre-tool-use hook (see [`enforcement.md`](./enforcement.md))
enforces `brief.allowed_tools` server-side, so a wandering LLM cannot
silently call `Bash` from a state that only declared `Read`.

### C. HTTP-driven

The skill drives the FSM via the FastAPI REST + SSE endpoints. Useful
for browser-side tools (the `fsm-ui` SPA), CI pipelines that already speak
HTTP, dashboards, or third-party orchestrators that do not embed MCP.

```bash
# Boot the server (one-shot)
ctxr-fsm api --host 127.0.0.1 --port 8000 --db ./.ctxr-fsm/fsm.db

# In the skill (any language)
curl -fsS http://127.0.0.1:8000/healthz                                 # step 2
SPEC_ID=$(curl -fsS -H "Authorization: Bearer $T" \
    http://127.0.0.1:8000/api/v1/specs | jq -r \
    '.[] | select(.slug=="code-review-lens-specialist") | .id')
RUN_ID=$(curl -fsS -H "Authorization: Bearer $T" -X POST \
    http://127.0.0.1:8000/api/v1/runs \
    -d "{\"spec_id\":\"$SPEC_ID\",\"args\":{\"target\":\"PR #42\"}}" | jq -r .id)

# Live event stream while the loop runs (SSE)
curl -N -H "Authorization: Bearer $T" \
    "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events"
```

Brief / commit / confirm endpoints mirror the MCP surface 1:1 — see
[`docs/http-api.md`](./http-api.md) for the route catalog and auth model.

---

## Worked example: `skill-code-review` (the lens-specialist pipeline)

The skill today dispatches **6 lens specialists** — gap, blind-spot,
edge-case, infeasibility, divergence, missed-step — and synthesizes a
**GO / CONDITIONAL / NO-GO** verdict. Below is what that same pipeline
looks like once expressed against `ctxr-fsm`.

### A. The FSM spec — `skill_code_review/specs/lens_specialist.py`

```python
from ctxr.fsm.core import (
    FsmSpec, State, Worker, Loop, Predicate, Transition,
    ResponseSchema, VerifierSpec,
)

LENSES = ["gap", "blind-spot", "edge-case",
          "infeasibility", "divergence", "missed-step"]

# Schemas elided for brevity — each Worker carries a ResponseSchema
# whose JSON Schema enforces the worker's output shape.
_lens_iter_schema = ResponseSchema(schema={
    "type": "object",
    "required": ["lens", "findings", "done"],
    "properties": {
        "lens": {"type": "string", "enum": LENSES},
        "findings": {"type": "array", "items": {"type": "object"}},
        "done": {"type": "boolean"},
    },
})

spec = FsmSpec(
    id="code-review-lens-specialist", version=1, entry="scan_diff",
    states=[
        State(id="scan_diff",
              worker=Worker(role="diff-scanner",
                            prompt_template="Scan the diff for {target}",
                            inputs=["target"],
                            response_schema=ResponseSchema(schema={...})),
              allowed_tools=["Read", "Bash"],
              outputs=["files_changed"],
              transitions=[Transition(to="dispatch_lenses", when="always")]),
        State(id="dispatch_lenses",
              loop=Loop(worker=Worker(role="lens-specialist",
                                      prompt_template="Lens: {lens_name}",
                                      inputs=["files_changed"],
                                      response_schema=_lens_iter_schema),
                        max_iterations=6, done_field="done"),
              allowed_tools=["Read", "Grep"],
              outputs=["lens", "findings", "done"],
              transitions=[Transition(to="collect_findings", when="always")]),
        State(id="collect_findings",
              worker=Worker(role="findings-collector",
                            prompt_template="Merge per-lens findings.",
                            inputs=["aggregated_findings"],
                            response_schema=ResponseSchema(schema={...})),
              outputs=["unified_findings"],
              transitions=[Transition(to="synthesize_verdict", when="always")]),
        State(id="synthesize_verdict",
              worker=Worker(role="verdict-synthesiser",
                            prompt_template="Apply GO/CONDITIONAL/NO-GO.",
                            inputs=["unified_findings"],
                            response_schema=ResponseSchema(schema={...})),
              verifier=VerifierSpec(
                  role="verdict-auditor",
                  prompt_template="Does the verdict follow the rule?",
                  response_schema=ResponseSchema(schema={...}),
                  parallel_count=3, majority_threshold=2),
              allowed_tools=["Read"],
              post_validations=[Predicate(
                  "verdict == 'GO' OR verdict == 'CONDITIONAL' OR verdict == 'NO-GO'")],
              outputs=["verdict", "explanation"],
              transitions=[Transition(to="done", when="always")]),
        State(id="done", transitions=[]),
    ],
)
```

Notes:

- The `Loop` covers all 6 lenses with `done_field="done"`. The runner
  injects the current lens name into env between iterations.
- Each state declares its **`allowed_tools`** — the synthesizer only
  needs `Read`; the scanner gets `Bash` for `git diff`.
- The verdict state attaches a **3-of-3 verifier panel** with a 2-majority
  threshold. The verifier sees `unified_findings` plus the proposed
  `verdict` and answers boolean — the FSM rejects the commit on `False`.
- A `post_validation` predicate re-asserts the verdict-shape invariant
  inside the engine, catching a buggy worker before the run is marked
  terminal.

### B. The `SKILL.md` frontmatter

```yaml
---
name: skill-code-review
description: Review the current diff via the lens-specialist FSM.
requires:
  fsm:
    mcp_server: ctxr-fsm
    min_version: "0.1.0"
    server_must_be_reachable: true
---
```

The `requires.fsm` block is the **machine-readable pre-check** for
Principle 1 (requirement pre-check, ask-to-satisfy). A host that does
not see a reachable `ctxr-fsm` MCP server stops before any LLM tokens
are spent.

### C. The skill preamble

Installed by `ctxr-fsm install-memory` (the FSM-discipline section) plus
the skill's own dispatch instructions:

> Before doing anything, call `fsm.healthcheck()`. If it fails, return
> `MissingRequirement` with the install command `pip install ctxr-fsm`
> and STOP. Once healthy, `fsm.list_specs` to find the
> `code-review-lens-specialist` spec; `fsm.start_run` with
> `args = {target: <user-supplied diff or PR url>}`; loop:
> `fsm.get_brief` → dispatch the lens specialist as a sub-agent with
> `brief.prompt_template` + `brief.inputs` → sub-agent returns JSON
> matching `brief.worker.response_schema` →
> `fsm.commit_outputs(run_id, outputs, signature=CommitSignature.compute(...))`
> → `fsm.confirm_commit` if the result carries a token → repeat until
> terminal. The verdict + explanation are the run's terminal outputs.

### D. The install hook (registers the spec on first install)

```python
# skill_code_review/install_spec.py
from ctxr.fsm.cli._common import resolve_db_path
from ctxr.fsm.sqlite import Project
from skill_code_review.specs.lens_specialist import spec

def install() -> None:
    db_path = resolve_db_path(None)            # honours .fsmrc.json + env
    with Project.open(db_path) as project:
        result = project.register_spec(spec, project_slug="skill-code-review")
        print(f"registered {result.spec.slug}@v{result.spec.version} "
              f"(created={result.created})")

if __name__ == "__main__":
    install()
```

Wire this into the package's post-install hook, or document it as a
one-liner the operator runs after `pip install`:

```bash
pip install skill-code-review
python -m skill_code_review.install_spec   # → registered code-review-lens-specialist@v1
```

`register_spec` is **idempotent** — re-running it on an unchanged spec
returns `created=False` and does not bump the version. Edit the spec,
re-run, and a new version row is minted automatically (see
[`docs/data-model.md`](./data-model.md)).

### E. Migration checklist for the existing JS-based skill

When the time comes (this is the out-of-scope work flagged at the top
of this guide), the migration from `legacy-js/` to the Python substrate
is:

- [ ] Port `.fsm.yaml` (or the hand-rolled JS FSM in
      `scripts/run-review.mjs`) into a Pydantic `FsmSpec` exactly as
      shown in section A.
- [ ] Replace the Node runner's shell loop with the MCP tool dispatch
      loop in section B of "The three integration shapes".
- [ ] Replace `SKILL.md`'s `requires.fsm.npm` block (if any) with the
      `requires.fsm.mcp_server` block from section B above.
- [ ] Add `install_spec.py` (section D) and call it from the package's
      post-install hook.
- [ ] Update CI to test the Python-driven flow end-to-end via
      `tests/integration/test_pipeline.py` (mirror
      `tests/integration/examples/test_code_review_pipeline.py`).
- [ ] **Leave `legacy-js/` in place** until the new flow has shipped at
      least one production review. The old runner is the rollback path.

---

## Honouring the enforcement layers

Every consumer is responsible for cooperating with the four enforcement
groups documented in [`docs/enforcement.md`](./enforcement.md). The skill
authoring rules:

### `allowed_tools` (Layer 2 + Layer 4)

- Read `brief.allowed_tools` at every state entry. Your effective surface
  is **`fsm.* + allowed_tools`** — nothing else.
- If your worker would call a tool not in that set, **refuse locally
  first**. The Claude Code pre-tool-use hook (Layer 4) is a defence-in-
  depth backstop, not the primary gate.
- Empty `allowed_tools` means the worker may call **only** `fsm.*` for
  that state. This is normal for a `synthesize_*` style state.

### Cosignature (Layer 5)

**Always sign your commits.** The skill computes:

```python
sig = CommitSignature.compute(
    brief_id   = brief.brief_id,
    inputs     = brief.inputs,
    outputs    = my_outputs,
    session_id = session_id,        # stable per FSM-driving session
).signature
ack = fsm.commit_outputs(run_id=run.id, outputs=my_outputs, signature=sig)
```

The server recomputes `sha256(brief_id || canonical_json(inputs) ||
canonical_json(outputs) || session_id)` and rejects mismatches with
`error: signature_mismatch`. Skipping the signature on a state whose
`allowed_tools` is non-empty triggers an automatic `signature_required`
reject — the FSM treats an unsigned commit as a layer-5 bypass attempt.

### Verifier panel (Layer 3)

If the state declares a `verifier`, the FSM runs `parallel_count`
verifier dispatches in parallel and accepts the commit only when at
least `majority_threshold` return `verdict_supported: true`. **Your
skill code does not see the verifier** — it just gets `verifier_passed`
or `verifier_rejected` in the `CommitResult` envelope. On
`verifier_rejected`, re-do the work; do not argue with the panel
(Principle 7).

### Drift detector (Layer 8)

- **Do** call `fsm.observe_tool_call` for every non-`fsm.*` tool you
  invoke during a run (Principle 9). Skipping is itself a drift signal.
- **Don't** call tools outside `allowed_tools`. The drift detector will
  see and pause the run (`drift_paused`); the operator then has to
  resume.

### Two-phase commit (Layer 12)

When `fsm.commit_outputs` returns a `commit_token`, the brief had
`requires_confirm: true`. Follow up immediately with
`fsm.confirm_commit(token, expected_next_state)`. The token TTL is 60
seconds — sit on it longer and you'll get `commit_token_expired` and
have to redo the state.

---

## Distribution

A consumer project that wants to use your skill:

```bash
# 1. One-time bootstrap (writes .fsmrc.json + .ctxr-fsm/fsm.db
#    + installs FSM principles into the AI client's memory).
ctxr-fsm init

# 2. Install the skill itself.
pip install skill-code-review            # or: uv add skill-code-review

# 3. Register the skill's FsmSpec into the project DB (idempotent).
python -m skill_code_review.install_spec

# 4. The AI client (Claude Code, Codex, Cursor) reads the skill's
#    SKILL.md, sees the requires.fsm block, calls fsm.healthcheck,
#    then fsm.list_specs → fsm.start_run → loop. No further setup.
```

Three contracts make this work without bespoke per-host plumbing:

1. **`requires.fsm` in `SKILL.md`** — declares the dependency in a
   machine-readable shape every modern AI client can parse.
2. **`ctxr-fsm install-memory`** — writes the host-specific principles
   adapter (Claude / Codex / Cursor) so the LLM knows the protocol
   without the skill author re-explaining it.
3. **Idempotent `register_spec`** — re-running the install hook on a
   stale checkout is safe. Spec hashing in
   [`docs/data-model.md`](./data-model.md) guarantees version bumps only
   when the spec actually changes.

---

## Where to go next

| You want to… | Read |
|---|---|
| See the full Python control loop with persistence + aggregators | [`examples/code_review_pipeline.py`](../examples/code_review_pipeline.py) |
| Look up an `fsm.*` tool signature | [`docs/mcp-tools.md`](./mcp-tools.md) |
| Hit the FSM from HTTP / a browser | [`docs/http-api.md`](./http-api.md) |
| Understand the gate that just rejected your commit | [`docs/enforcement.md`](./enforcement.md) |
| Recover a faulted run | [`docs/recovery.md`](./recovery.md) |
| Internalise the agent-side discipline | [`ctxr/fsm/memory/principles.md`](../ctxr/fsm/memory/principles.md) |
| Compare more end-to-end pipelines | [`docs/examples-tour.md`](./examples-tour.md) |
