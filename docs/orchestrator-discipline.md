# Orchestrator-Shell Discipline

`@ctxr/fsm` is designed around a strict separation of responsibilities:

- The **FSM engine** parses the FSM YAML, validates worker outputs against the per-state JSON Schemas, applies transition predicates, and writes manifest + trace files.
- The **worker** receives a staged prompt (assembled from `fsm/prompt-templates/*.md` + reusable fragments from `@ctxr/fsm/prompt-fragments`) and emits a single JSON document that conforms to the state's `response_schema`.
- The **orchestrator (the skill runner)** is a thin shell around the FSM CLIs. It calls `fsm-next` to get the next dispatch brief, asks the harness to spawn a worker with the staged prompt, hands the worker's JSON output to `fsm-commit`, and loops until the FSM reaches a terminal state.

When the runner stays a shell, the FSM is the single source of truth for control flow. When the runner starts reading YAML, calling LLM tools directly, or assembling prompts inline, control flow leaks back into the runner and the FSM's determinism guarantees erode.

`fsm-lint-runner` is an **advisory** linter that scans runner files for the three most common drift modes. It never modifies files; it exits non-zero only as a signal so authors notice the drift in code review or CI.

## The three rules

### 1. `no-direct-fsm-yaml-read`

> The orchestrator must not read `*.fsm.yaml` directly.

The engine owns YAML parsing. A runner that `readFileSync`s the FSM YAML is either (a) duplicating logic the engine already does, or (b) making decisions the FSM should make. Both cases break the "FSM is the single source of truth" invariant.

The linter flags any of `readFileSync`, `readFile`, `createReadStream`, `open`, `openSync` whose call argument on the same line contains the substring `.fsm.yaml`.

### 2. `no-orchestrator-llm-call`

> The orchestrator must not call LLM tools at orchestrator level.

LLM work happens inside the worker the FSM engine dispatches. If the runner itself calls `messages.create`, `Task(...)`, `Agent(...)`, etc., the FSM cannot validate that work against a per-state schema, cannot record it in a trace file, and cannot replay it.

The linter flags calls to the known LLM-dispatch verbs:

- Anthropic SDK shapes: `messages.create`, `completions.create`, `anthropic.messages`, `client.messages`.
- Harness-tool-shaped invocations from JS: `Task(`, `Agent(`, `Skill(`, `Bash(`, `WebFetch(`, `WebSearch(`. Legitimate orchestrator dispatches that match these shapes (e.g. an `Agent(...)` worker spawn that IS the intended pattern) suppress with `// fsm-lint:ignore`.

This rule has the most false-positive risk (see below).

### 3. `no-inline-prompt-composition`

> The orchestrator must not compose worker prompts inline.

Prompts belong in `fsm/prompt-templates/*.md`, with reusable fragments imported from `@ctxr/fsm/prompt-fragments` (specialist header, output-contract block, forbidden-paths notice, brief block). Composing prompts inline puts the worker contract in the runner where it cannot be reviewed, reused, or diffed independently.

The linter flags multi-line template literals (backtick strings spanning two or more source lines) that contain BOTH:

- A role / contract marker: `You are`, `Role:`, `## Role`, `## Brief`, `Specialist:`, `Output Contract`.
- A schema marker: `$schema`, `"type": "object"`, `"required":`, `response_schema`.

A multi-line template that holds only a message string (no schema, no role header) is fine and not flagged.

## How to invoke

```bash
node scripts/fsm-lint-runner.mjs <path-to-runner> [<more-runner-paths>]
```

The linter accepts one or more positional file paths. It prints one diagnostic per violation to stdout in the form:

```text
<file>:<line>: <rule>: <suggestion>
```

Exit codes:

- `0` clean: every file passes every rule.
- `1` at least one diagnostic was emitted, or a given path was not found.
- `2` invalid CLI arguments (e.g. zero positional arguments and no `--help`).

`--help` (or `-h`) prints the usage block to stdout and exits 0.

The linter never writes to disk, never spawns subprocesses, and never reaches the network. It is safe to run from any pre-commit or CI step.

## Suppressing a false positive

Each heuristic can fire on legitimate code. Authors suppress a known false positive by appending the marker `// fsm-lint:ignore` to the offending line. For multi-line constructs (e.g. a template literal that triggers `no-inline-prompt-composition`), placing the marker on the line immediately above the opening backtick also suppresses the diagnostic.

Example:

```js
// This template literal is documentation, not a worker prompt. // fsm-lint:ignore
const docExample = `
You are a worker.
"type": "object"
`;
```

Use the marker sparingly. The right fix for most flags is to move the offending code into the engine or a prompt template.

## Honest limitations

The linter uses simple line-based heuristics and a hand-rolled template-literal walker, not a full JavaScript parser. Known false-positive and false-negative shapes:

- **`no-orchestrator-llm-call`** matches function names. A renamed import (`import { messages as msg } from "@anthropic-ai/sdk"`) will evade detection. A legitimate `Task(` call that is part of the harness's worker-dispatch protocol may also fire; suppress with `// fsm-lint:ignore`.
- **`no-inline-prompt-composition`** matches string markers inside the literal body. A prompt that uses none of the listed role or schema markers will evade detection. Conversely, a docstring that quotes both markers as examples will fire and need suppression.
- **`no-direct-fsm-yaml-read`** matches the literal substring `.fsm.yaml` on the same line as a read API. A two-line read where the filename is assigned to a variable on line N and passed to `readFileSync` on line N+1 will evade detection.
- The template-literal walker is intentionally simple: it does not parse `${...}` expression blocks. Markers inside an expression block are not recognised as such, which has never mattered in practice for these heuristics.

The linter is advisory because runtime enforcement of the orchestrator-shell discipline would require sandboxing the runner process (cutting off LLM SDK imports, intercepting filesystem reads). That is out of scope for `@ctxr/fsm`; this lint catches the common drift modes at code-review time, and that is enough.

## Related

- `docs/orchestration-design.md` for the broader hub-and-spoke architecture.
- `docs/worker-contract.md` for what staged prompts look like.
- `docs/state-yaml-reference.md` for the FSM YAML shape the engine consumes.
