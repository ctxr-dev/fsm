# Coding standards

Project-level coding standards every PR must honour. These are the
non-negotiable rules; everything else is in the inline docstrings.

## Closed-vocabulary string literals → StrEnum

**Rule.** Every closed vocabulary used across more than one module
MUST be defined as a `StrEnum`. Every reference (Pydantic field
annotation, model_validator branch, dict-key comparison, JSON wire
value, CLI flag value) MUST go through the enum member, not through
a free-form string literal.

**Why.** This was the user's call-out on PR #39 (W14b+c+d):

> All these `when` and `why` and other things — should not be string
> predicates if they predefined and used across all the python files.
> Instead, they should be extracted to shareable enums and reused
> everywhere. I don't understand why should I explain you basics of
> SOLID, KISS etc.

Magic-string vocabularies scattered across modules are the most
expensive bug class to fix later: every typo silently passes type-check
and surfaces only at the wire boundary; renaming requires a full grep
sweep that no IDE can verify; and the "where is this value enumerated?"
question has no single answer.

**How.** Before pushing a PR that introduces a new Pydantic model OR
a new string-keyed JSON field OR a new CLI flag value:

1. Run `make audit-strings`. The script greps the source tree for the
   patterns the W14i audit identified.
2. If the script flags a hit, either:
   - Reference the existing enum (almost always the right answer), or
   - Define a new StrEnum in the natural home module + import it.
3. If a hit is genuinely a one-off open vocabulary (`Literal["stdio",
   "http"]` on a CLI flag with no cross-module reuse), append
   `# audit-strings: justified` to the line. The justification comment
   forces a maintainer-of-the-day decision: a contributor who silently
   adds the marker without thinking about whether the value is closed
   is making a deliberate choice that PR review will catch.

## Canonical enum homes

- **`ctxr/fsm/core/models.py`** — every lifecycle / domain vocabulary
  consumed by both the engine and at least one persistence /
  serialisation surface. Examples: `RunStatus`, `StateKind`,
  `TransitionKind`, `EngineAdvanceKind`, `EventKind`, `VerifierVerdict`,
  `InlineFaultReason`, `JournalStatus`, `LockAcquireReason`,
  `LockReleaseReason`.
- **`ctxr/fsm/cli/_clients.py`** — vocabularies shared across
  `ensure`, `install-mcp`, and `install-memory` (the W14 bootstrap
  pipeline). Examples: `McpClient`, `EnsureStatus`, `EnsureActionStatus`,
  `McpConfigStatus`, `EnsureMode`, `ConfigFormat`.
- **Per-tool modules** — vocabularies that genuinely don't escape
  their module (e.g. `CommitResultKind` and `JournalAction` in
  `ctxr/fsm/mcp/tools_runs.py`). When a second module reaches for
  them, promote to one of the canonical homes above.

## Dispatcher Open-Closed pattern

Every `match` statement that dispatches on a StrEnum SHOULD end with:

```python
case _ as never:
    raise AssertionError(f"unhandled <EnumName> member: {never!r}")
```

Adding a new enum member then surfaces as a typed test failure rather
than a silent fallthrough. The pattern is enforced by code review;
no automated grep catches it yet.

## CI gate

`pytest tests/test_audit_strings.py` runs `audit_strings.sh` and
fails the build if any unjustified finding surfaces. The smoke test
is fast (single subprocess invocation, no fixture setup) so the gate
is essentially free per PR.

## CLI parsing + pretty-printing (W14j)

**Rule.** Every CLI command in `ctxr-fsm` is a typer subcommand.
Every human-facing pretty-print path uses Rich (via typer's bundled
integration). JSON output uses `json.dumps(sort_keys=True, indent=2)`.

* No `argparse` — fragments the CLI shape (different `--help` style,
  different error handling, different test harness).
* No `click` direct imports — typer wraps click and exposes the
  pieces we need; the project's CLI surface MUST go through typer
  so the decorator vocabulary, default-handling, and Rich integration
  stay consistent across commands.
* No bespoke pretty-printers — `print(...)` of dicts, hand-rolled ANSI
  escapes, `tabulate`, `colorama`, etc. Rich (`rich.console.Console`,
  `rich.table.Table`, `rich.panel.Panel`) is the only allowed pretty
  surface so the look/feel stays uniform.

**Why.** The user explicitly locked this in W14j:

> I want you to add to the plan, to print out to the console (this
> is console command) — with beautiful python typer table — all the
> addresses with ports where I can go to see: fastapi with it's
> swagger, ui, etc. In general, I want you to use typer library for
> all commands, and for pretty printing (if we suppose to pretty
> print anywhere and mode is not json).

One CLI parser + one pretty-print library means a future contributor
can read any subcommand and know which patterns are available.

**How.** `scripts/audit_strings.sh` rule 6 greps the `ctxr/fsm/cli/`
tree for `import argparse` / `from argparse` / `import click` /
`from click`. A genuinely-justified exception (none currently exist)
takes the `# audit-strings: justified` marker. Shared Rich renderers
that surface across commands live in `ctxr/fsm/cli/_render.py` so the
column shape / colour mapping is owned in one place.
