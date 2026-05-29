# `@ctxr/fsm` — State YAML Reference

The FSM YAML is the single source of truth for the orchestrator's flow. The package's CLIs read it via `--fsm-path` (or `fsm_path` in `.fsmrc.json`).

## Top-level structure

```yaml
fsm:
  id: <string, required>          # the FSM identifier (recorded in manifest.fsm_id)
  version: <integer, required>    # the FSM version (recorded in manifest.fsm_yaml_version)
  entry: <state-id, required>     # which state runs first
  states:                         # required, non-empty array
    - id: <snake_case_id>
      ...
```

## State

```yaml
- id: <snake_case_id>             # required; matches /^[a-z][a-z0-9_]*$/
  purpose: <one-line-string>      # required; copied verbatim into trace records
  preconditions:                  # required (use [] for entry states)
    - "<free-form English>"
  worker:                         # optional; omit for inline / deterministic-only states
    role: <string>
    prompt_template: <path>
    inputs:
      - <name-of-upstream-output>
    response_schema:              # JSON Schema 2020-12; Ajv-validated
      type: object
      required: [...]
      properties: {...}
  outputs:                        # required (use [] for terminal states)
    - <name>
  post_validations:               # optional; evaluated at runtime via the predicate DSL
    - "<predicate expression>"
  transitions:                    # required (use [] for terminal states)
    - to: <state-id>
      when: <when-clause>
```

## Worker block

A state with a `worker:` block dispatches a sub-agent on entry. The package validates the worker's JSON output against `response_schema` before advancing. States without a `worker:` are inline — the orchestrator computes outputs deterministically without a sub-agent.

| Field | Required | Description |
|-------|----------|-------------|
| `role` | yes | Identifier for the worker's role (e.g. `project-scanner`) |
| `prompt_template` | yes | Path to the worker's markdown prompt template |
| `inputs` | yes | List of names that must appear in some upstream state's `outputs[]` (or `args` for the entry state) |
| `response_schema` | yes | JSON Schema describing the worker's expected output |

The static validator checks that the prompt-template path exists on disk and that the `response_schema` compiles as valid JSON Schema.

## Transition `when:` shapes

The `when` clause determines whether a transition fires.

### `when: always`

Unconditional. Always fires.

```yaml
transitions:
  - to: next_state
    when: always
```

### `when: otherwise`

Catch-all. Fires iff no earlier transition in the list matched.

```yaml
transitions:
  - to: short_circuit
    when:
      kind: deterministic
      expression: "tier == 'trivial'"
  - to: full_flow
    when: otherwise
```

### `when: { kind: deterministic, expression: ... }`

The package's predicate evaluator runs `expression` against the cumulative run env (every prior state's outputs + `args`).

```yaml
transitions:
  - to: short_circuit_exit
    when:
      kind: deterministic
      expression: "tier == 'trivial' AND len(stage_a_candidates) == 0"
  - to: stage_b_trim
    when: always
```

### `when: { kind: judgement, criteria: ..., evidence_required: ... }`

The orchestrator chooses via `fsm-commit --transition <state-id>`. The package records the choice + criteria + (optional) `evidence_required` in the trace; runtime evaluation is the orchestrator's responsibility.

```yaml
transitions:
  - to: rebuild
    when:
      kind: judgement
      criteria: "Did the user request a rebuild from scratch?"
      evidence_required: "Quote the user's request verbatim."
  - to: incremental
    when: otherwise
```

## Predicate DSL (deterministic transitions)

The expression is parsed by a hand-rolled tokeniser + recursive-descent parser. There is no `eval()`, no string interpolation, no implicit type coercion beyond explicit operator semantics.

### Literals

| Form | Example |
|------|---------|
| Integer | `42` |
| Float | `3.14` |
| Single-quoted string | `'hello'` |
| Double-quoted string | `"hello"` |
| Boolean | `true`, `false` |
| Null | `null` |
| Always-true keyword | `always` |

### Identifiers

Dotted paths into the cumulative run env:

```text
tier
project_profile.languages
diff_stats.lines_changed
```

A missing identifier resolves to `undefined`. Comparisons with `undefined` follow JavaScript loose-comparison rules; recommended practice is to test for `== null`.

### Operators

| Category | Tokens |
|----------|--------|
| Comparison | `==`, `!=`, `<`, `>`, `<=`, `>=` |
| Logical | `AND`, `OR`, `NOT` (case-insensitive); aliases `&&`, `||`, `!` |
| Grouping | `(`, `)` |

Operator precedence (highest to lowest): grouping, function-call, `NOT`, comparison, `AND`, `OR`.

### Functions

| Function | Description |
|----------|-------------|
| `len(x)` | Length of a string or array. `len(null) == 0`. Throws on other types. |
| `empty(x)` | Shortcut for `len(x) == 0`. |
| `in(x, list)` | Membership test. `list` must be an array. |

### Examples

```yaml
expression: "always"
expression: "tier == 'trivial'"
expression: "len(stage_a_candidates) == 0"
expression: "tier == 'sensitive' AND empty(picked_leaves)"
expression: "in(language, ['python', 'go', 'typescript'])"
expression: "diff_stats.lines_changed > 100 OR diff_stats.files_changed > 5"
```

## Inline states (no worker)

States without a `worker:` block are computed by the orchestrator deterministically — typically aggregations, simple branching, or filesystem side effects. Their `outputs[]` are still declared so downstream states' inputs can reference them. The orchestrator passes outputs to `fsm-commit --outputs`; the package validates nothing schema-side (no `response_schema` to check) but still writes the exit trace and evaluates transitions.

## Post-validations (runtime predicates)

The optional `post_validations:` array runs after JSON-Schema validation of the worker output (or after `fsm-commit --outputs` for inline states) and before transition resolution. Each entry is a predicate expression in the same DSL that powers deterministic `when:` clauses (see "Predicate DSL" above). The predicates are evaluated against the just-committed outputs map, so the field names refer directly to keys of `outputs`.

Multiple predicates are AND-composed: the state is considered valid only when every predicate evaluates true. If any predicate evaluates false, or fails to parse / evaluate, `fsm-commit`:

1. Writes a `fault` trace with `reason: "post_validation_failed"` and details of every predicate result.
2. Sets `manifest.status = "faulted"` and stamps `ended_at`.
3. Releases the run's lock.
4. Emits a structured error on stdout (`{ error: "post_validation_failed", state, post_validations: [...] }`) and exits with code `1`.

The state does NOT advance; no transition is resolved when post-validations fail.

A malformed expression captures the parser/runtime error in the corresponding `results[].error` field and counts as a failed validation.

### Available operators

The post-validation DSL is the same as the transition DSL, summarised here for convenience:

| Category | Tokens |
|----------|--------|
| Literals | numbers, `'...'` / `"..."` strings, `true`, `false`, `null` |
| Identifiers | dotted paths into `outputs` (e.g. `findings`, `summary.total`) |
| Comparison | `==`, `!=`, `<`, `>`, `<=`, `>=` |
| Logical | `AND`, `OR`, `NOT` (case-insensitive); aliases `&&`, `||`, `!` |
| Grouping | `(`, `)` |
| Functions | `len(x)`, `empty(x)`, `in(x, list)` |

### Examples

```yaml
post_validations:
  - "len(findings) > 0"
  - "verdict == 'PASS' OR verdict == 'CONDITIONAL'"
  - "NOT empty(summary)"
```

### Limitations

Predicates can express comparisons, membership, length, and boolean combinations over scalar/array fields of `outputs`. They cannot express structural JSON-Schema-grade checks beyond what the worker's `response_schema` already validates. Use the worker `response_schema` for structural shape; use `post_validations` for runtime business-rule checks (non-empty result set, expected verdict values, cross-field invariants).

## Validation

`fsm-validate-static <fsm.yaml>` runs:

- **Schema phase**: required fields, types, snake_case ids, valid `when.kind`, valid JSON Schema.
- **Static phase**: no duplicate ids, every `transitions[].to` resolves, every state reachable from `entry`, at least one terminal state (empty `transitions[]`), worker `prompt_template` paths exist, `worker.inputs[]` reference upstream `outputs[]` (or `args`).

Wire `fsm-validate-static` into your consumer's pre-commit hook to catch FSM authoring errors early.

## Hash-locking

The package records `manifest.fsm_yaml_hash = sha256(<file-bytes>)` at run-start. On every `fsm-next --resume` and `fsm-commit`, the recorded hash is compared with the current file's hash. Mismatch → `error: fsm_yaml_changed`. Consumers must reconcile (resume against the recorded hash via git, fork-pivot to the new FSM, or abandon).

This is the package's mechanism for catching mid-flight FSM edits.
