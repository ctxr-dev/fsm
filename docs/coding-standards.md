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
  `McpConfigStatus`, `EnsureMode`.
- **`ctxr/fsm/mcp/_shared_enums.py`** — MCP tool vocabularies that
  escape a single tool module. Example: `JournalAction` is read by
  both `tools_runs` and `tools_events`, so it lives in this shared
  home rather than in either tool module (importing across tool
  modules just to share an enum would drag the producing module's
  whole import graph into the consumer's boot path).
- **Per-tool modules** — vocabularies that genuinely don't escape
  their module (e.g. `CommitResultKind` in
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
