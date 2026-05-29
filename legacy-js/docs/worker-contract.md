# `@ctxr/fsm` — Worker Contract

A worker is a sub-agent dispatched by the FSM Orchestrator on entry to a state that has a `worker:` block. The package defines the contract every worker must follow:

1. The worker receives a structured prompt assembled from its template + the state's `inputs`.
2. The worker returns a JSON object that conforms to the state's `response_schema`.
3. The package validates the JSON against the schema before advancing.

## Authoring a worker prompt template

Worker templates live wherever the consumer chooses; the FSM YAML's `worker.prompt_template` field is the path (relative to the FSM YAML's directory or absolute).

A template is a Markdown document with these sections (no enforced structure — but the convention is clear):

```markdown
# Worker: <role>

Brief one-line description of the worker's job.

## Inputs

What the worker receives from the FSM Orchestrator (matching the FSM YAML's `worker.inputs`).

## Task

Step-by-step instructions for what the worker should do.

## Output (JSON, schema-validated)

Exact JSON shape the worker must return, matching `worker.response_schema` in the FSM YAML.

## Constraints

What the worker must not do (e.g. don't read other workers' findings, don't make network calls).

## Validation will reject

Common rejection reasons surfaced by the schema validator (helps the worker self-correct on retry).
```

## Response schema

Every worker-driven state declares a `response_schema` in the FSM YAML. The package uses Ajv (JSON Schema 2020-12) to validate the worker's JSON output before advancing.

```yaml
worker:
  role: project-scanner
  prompt_template: workers/project-scanner.md
  inputs: [args]
  response_schema:
    type: object
    required: [project_profile, changed_paths, diff_stats]
    properties:
      project_profile:
        type: object
        required: [languages]
        properties:
          languages:
            type: array
            minItems: 1
            items: { type: string }
      changed_paths:
        type: array
        items: { type: string }
      diff_stats:
        type: object
        required: [lines_changed, files_changed]
        properties:
          lines_changed: { type: integer, minimum: 0 }
          files_changed: { type: integer, minimum: 0 }
```

When the worker's JSON output fails validation:

1. The package writes a fault trace to `fsm-trace/NNNN-fault-<state>.yaml`.
2. The manifest is set to `status: "faulted"` with `ended_at` populated.
3. The lock is released.
4. `fsm-commit` exits 1 with `{ error: "output_schema_violation", state: "...", errors: [...] }`.

The orchestrator can retry with a corrective prompt (passing the schema-validation errors back to the worker), but this is the orchestrator's responsibility — the package does not retry automatically.

## Worker isolation

The package strongly recommends:

- **Workers run blind in parallel.** No worker sees another worker's findings. Cross-talk between workers degrades aggregate quality (arxiv 2509.01494).
- **Workers never write state.** Only the FSM Orchestrator (via the package's CLIs) mutates the manifest, lock, and trace files.
- **Workers receive only what the FSM YAML declares as `inputs[]`.** This keeps each worker's context bounded and reproducible.

## Coordinator pattern (multi-level fan-out)

For states that need to dispatch K parallel sub-workers (e.g. dispatch K specialists), the recommended pattern is:

1. The state's `worker:` references a "coordinator" prompt template.
2. The coordinator (a single sub-agent) dispatches K sub-workers in parallel via the consumer's Agent tool.
3. The coordinator collects all sub-worker JSON outputs and returns one aggregated payload.
4. The aggregated payload conforms to the state's `response_schema`.

The package treats the coordinator as a single worker — it doesn't know about the K sub-workers. This keeps the engine simple and lets the consumer use whatever sub-agent dispatch mechanism is appropriate for their environment.

v0.3 of the package will introduce explicit child-FSM contracts for sub-workers; until then, the coordinator pattern keeps everything within one state.

## "What NOT to flag" guardrails

For workers that emit findings (review, audit, lint), the package recommends including a "What NOT to flag" section in the prompt template. The static validator does not enforce this, but it is the highest-leverage prompt-quality lever based on production deployments (Cloudflare's published architecture cites this as the largest signal-to-noise improvement).

## Logical-certificate scaffold

For states whose worker output is structured findings, the package recommends including a "logical certificate" scaffold in the prompt template. The pattern (premise → execution path → conclusion → severity) reportedly lifts F1 by 10-15 percentage points (Meta SemiFormalReasoning).

```markdown
For each finding, fill this template:
  PREMISE:    The relevant rule or invariant being checked
  EVIDENCE:   The specific code / artifact (cite file:line)
  EXECUTION:  How the artifact violates the premise (1-2 sentences)
  CONCLUSION: The finding statement
  SEVERITY:   <enum> + one-sentence rationale
```

The package's verification-judge (v0.3) will use the certificate fields to re-validate findings post-hoc.

## Returning empty findings is valid

Workers that produce findings should be told explicitly: returning an empty findings array is the expected outcome when nothing is wrong. Without this instruction, models tend to over-flag because they feel obligated to produce something. (GitHub Copilot's published 29% silent rate is the precision-floor reference.)
