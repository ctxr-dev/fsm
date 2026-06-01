---
name: ctxr-fsm-skill-template
version: 0.1.0
audience: skill-authors
---

# ctxr-fsm: skill authoring template

You're writing a skill that drives an FSM. This document is the contract: every skill in the ctxr-dev workspace follows the same shape so the LLM orchestrator can pick up any of them without bespoke wiring.

The proof of this template in production is `skill-code-review` (15-state pipeline, 5 worker prompts, 9 inline handlers). Copy its shape.

## Package layout

```
my-skill/
├── pyproject.toml                       # name = "ctxr-skill-<name>"
├── SKILL.md                             # bootstrap preamble + orchestrator loop
├── my_skill/
│   ├── __init__.py
│   ├── spec.py                          # Pydantic FsmSpec instance + export
│   ├── handlers.py                      # INLINE_HANDLERS dict[str, Callable]
│   ├── install.py                       # register() one-shot
│   └── workers/
│       ├── planner.md
│       ├── implementer.md
│       └── verifier.md
└── tests/
    ├── test_inline_handlers/
    └── test_end_to_end.py
```

## spec.py — the FsmSpec instance

```python
from ctxr.fsm.core.models import (
    FsmSpec, State, Worker, Loop, InlineSpec, Transition,
    Predicate, ResponseSchema, TransitionKind, StateKind,
)

PLAN_OUTPUT_SCHEMA = ResponseSchema(schema_={
    "type": "object",
    "required": ["commitments"],
    "properties": {
        "commitments": {"type": "array", "items": {"type": "string"}},
    },
})

fsm: FsmSpec = FsmSpec(
    id="my-skill",
    version=1,
    entry="plan",
    states=[
        State(
            id="plan",
            worker=Worker(
                role="planner",
                prompt_template=(
                    "Plan the work.\n\n"
                    "Output must match this shape:\n"
                    "{{ response_schema | typescript }}"
                ),
                inputs=["args"],
                response_schema=PLAN_OUTPUT_SCHEMA,
            ),
            outputs=["commitments"],
            transitions=[Transition(to="implement", when=TransitionKind.always)],
            allowed_tools=["Read"],
        ),
        # ... more states ...
        State(id="done", outputs=[], transitions=[]),
    ],
)
```

### Rules

1. **One `FsmSpec` instance per skill, named `fsm`.** Importers find it via `import my_skill.spec; spec = my_skill.spec.fsm`. The CLI's `ctxr-fsm spec register my_skill.spec:fsm` follows the same convention.
2. **Use the Pydantic models from `ctxr.fsm.core.models`.** Never construct dicts by hand. The Pydantic constructors validate eagerly; broken specs fail at import, not at register.
3. **Worker prompts may use Jinja2.** Reference `{{ response_schema | typescript }}`, `{{ inputs_schema | json }}`, `{{ allowed_tools }}`, `{{ spec.slug }}`, etc. See `ctxr/fsm/core/prompts.py` for the surface. Register-time validation smoke-renders every template; broken templates reject the spec.
4. **Every transition is typed.** Use `TransitionKind.always` for unconditional, `TransitionKind.otherwise` for the catch-all of a conditional fan-out, `Predicate(expression="...")` for guarded edges. The DSL is documented in `ctxr/fsm/core/predicates.py`.
5. **Every state lists `allowed_tools` explicitly.** Empty list means fsm.* only. Be precise; the drift detector watches.

## handlers.py — inline handler registry

```python
from collections.abc import Callable
from ctxr.fsm.core.inline_registry import InlineContext

def _risk_tier_triage(ctx: InlineContext) -> dict:
    # Deterministic: inspect ctx.inputs, return outputs matching
    # the inline state's response_schema. No LLM involvement.
    return {"tier": "low"}

INLINE_HANDLERS: dict[str, Callable[[InlineContext], dict]] = {
    "risk_tier_triage": _risk_tier_triage,
    # one entry per state whose kind is 'inline'
}
```

### Rules

1. **Inline handlers are pure deterministic functions.** No LLM dispatch, no network calls, no filesystem mutation outside what the engine surfaces via `ctx.project`.
2. **The dict key matches `InlineSpec.handler_id` in the spec.** Misalignment surfaces at engine advance time as `inline_handler_unregistered`.
3. **Return values must match `inline_spec.response_schema`.** Engine validates; bad shapes fault the run.
4. **Handlers may read prior-state outputs via `ctx.project`.** Examples: `ctx.project.aggregates.get(ctx.run_id, field='findings')`. Read-only; mutations are illegal.

## install.py — one-shot registration

```python
from pathlib import Path
from ctxr.fsm.sqlite import open_project
from .handlers import INLINE_HANDLERS
from .spec import fsm

def register() -> None:
    project = open_project(Path(".ctxr-fsm/fsm.db"))
    project.register_spec(fsm)
    project.register_inline_handlers(fsm.id, INLINE_HANDLERS)
```

Run via `uv run python -m my_skill.install`. Idempotent (registering the same spec at the same version is a no-op). Skill bootstrap should call this once per project.

## SKILL.md — orchestrator preamble

```markdown
---
name: my-skill
requires:
  fsm:
    mcp_server: ctxr-fsm
    min_version: "0.2.0"
---

# my-skill

## Bootstrap

Follow [`@.ctxr-fsm/memory/bootstrap.md`](.ctxr-fsm/memory/bootstrap.md) before any work below.

Register this skill's handlers + spec once per project:

```bash
uv run python -m my_skill.install
```

## Run

Drive the run via the fsm.* MCP tool family per [`@.ctxr-fsm/memory/AGENT_QUICKSTART.md`](.ctxr-fsm/memory/AGENT_QUICKSTART.md):

1. `fsm.start_run('my-skill', {goal: "..."})` → captures `run_id` + first `Brief`.
2. Loop: if `brief.terminal == true`, stop. Otherwise dispatch a sub-agent with `brief.worker.prompt_template` + `brief.inputs`; call `fsm.commit_outputs(run_id, outputs, signature)`; repeat.
3. Inline states advance server-side without a brief surfacing to you.
4. On error envelopes, follow Principle 4 in `principles.md`.
```

## tests/

```
tests/
├── test_inline_handlers/
│   └── test_risk_tier_triage.py      # one test file per handler
└── test_end_to_end.py                 # one happy-path drive against a tmpdir DB
```

### Conventions

- **One test file per inline handler.** Pure-function-style: feed an `InlineContext` fixture, assert the return value.
- **One end-to-end test that drives the spec start-to-finish.** Simulates worker outputs (no real LLM). Asserts the final state, the event histogram, and the absence of pending journal txns.
- **Test naming mirrors the Node legacy:** `test_<state_id>_*.py` for handler tests; the e2e file is the contract.

## Out of template

- Cross-skill gates: see [`GATE_CONTRACT.md`](./GATE_CONTRACT.md).
- Verifier states: declare `State.verifier=VerifierSpec(...)` to gate `commit_outputs` behind majority-vote adversarial review. Optional; default is no verifier.
- Worker prompt files in `workers/` are referenced as `prompt_template=Path("workers/planner.md").read_text()`. Keep them version-controlled alongside the spec.

## What this file is

Shipped inside the `ctxr-fsm` package at `ctxr/fsm/memory/SKILL_TEMPLATE.md`. The canonical reference for any skill author. Installed into consumer projects by `ctxr-fsm install-memory`.
