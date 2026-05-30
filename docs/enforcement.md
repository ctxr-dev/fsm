# Enforcement layers

The FSM gives state determinism. Enforcement gives you what the FSM alone
cannot: **a worker LLM that actually plays by the rules of the state it is
in**. Layer-by-layer reference: what each gate catches, how to enable/disable
it, where the tests live, plus the two-phase commit walkthrough and Claude
Code hook install.

Cross-refs: [models.md](models.md), [mcp.md](mcp.md), [drift.md](drift.md).

## The four groups

Enforcement layers cluster into four orthogonal responsibilities:

| Group | Layers | What it constrains |
|-------|--------|--------------------|
| **Capability** | 2 (allowed-tools surfacing), 4 (CC hook) | What tools the worker may call inside a state |
| **Integrity** | 5 (cosignature), 9 (spec-hash lock), 12 (two-phase commit) | That the commit *actually* matches what the worker saw and what the operator approved |
| **Adversarial** | 3 (verifier panel) | That a second opinion agrees the outputs satisfy the brief |
| **Observational** | 8 (drift detector) | That accrued misbehaviour eventually trips an auto-pause |

They compose as a funnel: capability *before* the call, integrity *during*
the commit, adversarial *if the state declares it*, observation *always,
asynchronously*. A worker slipping past one layer is caught by the next.

```
worker ──tool call──▶ CC hook (4) ──▶ tool runs
                       │
                       │ disallowed → exit 1
                       ▼
worker ──fsm.commit_outputs──▶ spec-hash lock (9)
                                cosignature (5)
                                engine.advance
                                verifier panel (3)
                                ──▶ CommitToken (12)
                                              │
worker ──fsm.confirm_commit──▶ consume token + replay journal
                                              │
event bus ──▶ drift detector (8) ──▶ score > threshold → drift_paused
```

## Layer 2 — allowed-tools surfacing

| | |
|---|---|
| **Catches** | A worker invoking a tool outside the state's declared surface. |
| **Where** | `State.allowed_tools` → `Brief.allowed_tools`. |
| **Enable** | Declare `allowed_tools` on the state in the FSM spec. |
| **Disable** | Omit the field, or declare `[]` (every non-`fsm.*` call is then off-allowlist). |
| **Tests** | `tests/integration/enforcement/test_drift_detector.py::test_classifier_*` |

The brief carries the closed capability set. **Every MCP client MUST refuse
locally**; layer 8 is a defence-in-depth backstop, not the primary gate.
`fsm.*` tools are always allowed regardless of the list — they are how a
worker advances the FSM.

```yaml
states:
  - id: draft
    allowed_tools: [Read, Grep]   # worker may call Read and Grep only
```

## Layer 4 — Claude Code pre-tool-use hook

| | |
|---|---|
| **Catches** | A worker that ignores its brief and tries to call a non-allowlisted tool from a Claude Code session. |
| **Where** | `.claude/hooks/pre-tool-use.fsm-guard.py` (consumer project). |
| **Enable** | Copy the hook + register it in `.claude/settings.json`. |
| **Disable** | Remove the marker file (`rm .ctxr-fsm/active-run.json`) or unregister the hook. |
| **Tests** | `tests/integration/enforcement/test_claude_code_hook.py` (10 scenarios). |

The hook reads `<project_root>/.ctxr-fsm/active-run.json`, maintained by
`fsm.start_run` / `fsm.confirm_commit` / `fsm.abort_run`. Soft-fails (exit 0)
on no marker, empty allowlist, malformed JSON, or missing `tool_name` — only
blocks on a confident negative.

### Install in a consumer project

```bash
mkdir -p .claude/hooks
cp <ctxr-fsm-repo>/.claude/hooks/pre-tool-use.fsm-guard.py .claude/hooks/
chmod +x .claude/hooks/pre-tool-use.fsm-guard.py
```

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": ".claude/hooks/pre-tool-use.fsm-guard.py" }
        ]
      }
    ]
  }
}
```

For non-cwd layouts: `export CTXR_FSM_PROJECT_ROOT=/path/to/project`.

Blocked stderr payload (Claude Code surfaces this to the agent verbatim):

```json
{"blocked": true, "tool": "Bash", "allowed": ["Read", "Grep", "fsm.*"], "reason": "tool 'Bash' not in active FSM run's allowed_tools"}
```

Full contract: [HOOK_README.md](../ctxr/fsm/memory/HOOK_README.md).

## Layer 9 — spec-hash lock

| | |
|---|---|
| **Catches** | The FSM spec being re-registered under a different shape while a run is in flight. |
| **Where** | `ctxr/fsm/mcp/tools_runs.py::_current_spec_hash_for_run`. |
| **Enable** | Always on. |
| **Disable** | Not configurable. Bump the spec version and start a fresh run. |
| **Tests** | `tests/integration/enforcement/test_spec_hash_lock.py` (3 tests). |

On every `fsm.get_brief` / `fsm.commit_outputs` / `POST /runs/{id}/resume`,
the server compares `run.fsm_spec_hash` against the latest spec hash for the
same slug. On mismatch the run is locked to the original spec version;
clients branch on `error == "fsm_spec_changed"` and either abort or roll
the spec change back:

```json
{
  "error": "fsm_spec_changed",
  "run_hash": "sha256:…",
  "current_hash": "sha256:…"
}
```

## Layer 5 — commit cosignature

| | |
|---|---|
| **Catches** | A commit whose `outputs` were tampered with between worker production and server consumption (or a worker that signed against the wrong brief / inputs). |
| **Where** | `ctxr/fsm/core/models.py::CommitSignature.compute` + `mcp/tools_runs.py`. |
| **Enable** | Set `CTXR_FSM_REQUIRE_COSIGNATURE=1`, OR declare `allowed_tools`, OR declare a `verifier` on the state. |
| **Disable** | Omit the three triggers above and the cosignature stays optional. |
| **Tests** | `tests/integration/enforcement/test_cosignature.py` (5 tests). |

The signature is `SHA-256` over canonical JSON of `{brief_id, inputs_hash,
outputs_hash, session_id}` (both inner hashes also canonical JSON SHA-256):

```python
from ctxr.fsm.core.models import CommitSignature

sig = CommitSignature.compute(
    brief_id=brief.id,
    inputs=env,           # the materialised run env
    outputs=worker_output,
    session_id="agent-session-42",
)
# pass sig.signature as the `signature` arg of fsm.commit_outputs
```

Outcomes:

| Path | Error | Event |
|------|-------|-------|
| Valid | — | `commit_signature_verified` |
| Mismatched | `signature_mismatch` | `commit_signature_mismatch` |
| Missing-but-required | `signature_required` | — |

## Layer 3 — adversarial verifier panel

| | |
|---|---|
| **Catches** | Outputs that *look* schema-valid but a second judge disagrees with. |
| **Where** | `ctxr/fsm/core/verifier.py` + `mcp/tools_runs.py`. |
| **Enable** | Declare `verifier: VerifierSpec` on the state. |
| **Disable** | Omit `verifier`. The cosignature is *not* required when no verifier is set unless `allowed_tools` is also declared. |
| **Tests** | `tests/integration/enforcement/test_verifier.py` (5 tests). |

Without a registered handler, the **structural verifier** runs by default —
re-applying the worker's `response_schema`. Every commit on a state with a
declared `verifier` thus gets some form of independent re-check.

To plug in a real LLM panel, register a process-wide handler at server boot:

```python
from ctxr.fsm.core.verifier import set_verifier_handler, VerifierVote

def my_handler(verifier, brief, outputs):
    # Dispatch verifier.parallel_count sub-agents; collect their verdicts.
    return [
        VerifierVote(verdict="passed", reason="design constraints met"),
        VerifierVote(verdict="passed", reason="acceptance criteria covered"),
        VerifierVote(verdict="rejected", reason="missing edge case"),
    ]

set_verifier_handler(my_handler)
```

Pass iff `passed_count >= verifier.majority_threshold`. Pass → `verifier_passed`
event + token issuance proceeds. Reject → `verifier_rejected` error + event,
no token minted, run sits on the current state until the worker retries.

## Layer 12 — two-phase commit

| | |
|---|---|
| **Catches** | Half-applied commits (crash mid-write); also forces clients to acknowledge they received the new brief before the substrate considers them committed. |
| **Where** | `ctxr/fsm/mcp/tools_runs.py::fsm_commit_outputs` + `fsm_confirm_commit`. |
| **Enable** | Always on. |
| **Disable** | Not configurable. |
| **Tests** | `tests/integration/enforcement/test_two_phase_commit*.py` (11 tests). |

`fsm.commit_outputs` no longer applies state-row changes directly. It
stages them, mints a 60s-TTL `CommitToken`, and returns. The client must
call `fsm.confirm_commit` to actually advance the run.

```
Client                       Server
  │                             │
  ├─ fsm.commit_outputs ───────▶│
  │                             ├─ spec-hash lock
  │                             ├─ cosignature check
  │                             ├─ engine.advance
  │                             ├─ verifier panel (if set)
  │                             ├─ stage writes in journal_txn
  │                             │   status=ready_to_finalise
  │                             ├─ mint CommitToken (TTL 60s)
  │                             ├─ emit commit_token_issued
  │◀─── CommitResult ───────────┤
  │     {kind, token, next…}    │
  │                             │
  ├─ fsm.confirm_commit ───────▶│
  │   {token, expected_next…}   ├─ commit_tokens.consume
  │                             ├─ replay journal_txn
  │                             ├─ mark journal finalised
  │                             ├─ emit commit_token_consumed
  │                             │        + journal_finalised
  │◀─── ConfirmResult ──────────┤
  │     {confirmed, next_brief} │
```

Token failure modes (each returns `commit_token_invalid` with `reason`):

| Reason | Meaning |
|--------|---------|
| `not_found` | Token never minted (or already reaped). |
| `already_consumed` | A previous `confirm_commit` already replayed it. |
| `expired` | TTL elapsed. ALSO emits `commit_token_expired` for the drift detector. |
| `state_mismatch` | `expected_next_state` differs from what the token was minted against. |

## Layer 8 — drift detector

| | |
|---|---|
| **Catches** | Cumulative misbehaviour — a worker that *occasionally* slips past the per-commit gates eventually trips the scoreboard. |
| **Where** | `ctxr/fsm/sqlite/drift.py` (loop in `cli/lifecycle/supervisor.py`). |
| **Enable** | Always on under `ctxr-fsm serve`. |
| **Disable** | `CTXR_FSM_DRIFT_DISABLED=1`. |
| **Tests** | `tests/integration/enforcement/test_drift_detector.py` (10 tests). |

An async background task in the supervisor polls every `poll_interval`
seconds, scans new events per run, classifies them into `SignalKind`,
records `drift_signals` rows with configured weights, and emits
`drift_signal_recorded`. When the cumulative score **strictly exceeds**
`score_threshold`, the loop atomically flips `run.status = drift_paused`
and emits `drift_pause_triggered` exactly once.

### Default weights + thresholds

| Signal | Weight | Trigger |
|--------|--------:|---------|
| `signature_mismatch` | 8.0 | `commit_signature_mismatch` event |
| `verifier_rejection` | 6.0 | `verifier_rejected` event |
| `off_allowlist_tool_call` | 5.0 | `tool_call_observed` with tool ∉ `allowed_tools` and not `fsm.*` |
| `repeated_validation_failed` | 3.0 | 2nd-or-later `validation_failed` in a row |
| `output_shape_near_miss` | 2.0 | Reserved for future near-miss classifier |
| `idle_too_long` | 1.0 | No event for `window_seconds` |

Defaults: `score_threshold = 10.0` (strict `>`), `window_seconds = 60.0`,
`poll_interval = 2.0`. Tuned so one accidental `Bash` (5) is noise, two (10)
is still under threshold, three (15) — or one signature forge + one repeated
validation fail — trips the pause.

### Tuning

Pass a custom `DriftConfig` when constructing the loop:

```python
from ctxr.fsm.sqlite.drift import DriftConfig, drift_detector_loop

cfg = DriftConfig(
    score_threshold=20.0,
    window_seconds=120.0,
    kind_weights={
        "off_allowlist_tool_call": 10.0,   # stricter
        "signature_mismatch": 15.0,
        "verifier_rejection": 8.0,
        "repeated_validation_failed": 3.0,
        "output_shape_near_miss": 2.0,
        "idle_too_long": 1.0,
    },
)
await drift_detector_loop(project, config=cfg, poll_interval=5.0)
```

### Kill switch

```bash
CTXR_FSM_DRIFT_DISABLED=1 ctxr-fsm serve
```

The loop logs one pre-flight line and returns immediately. Unset the var on
the next reload to re-enable.

## Putting it together

A correctly-instrumented run, end-to-end:

1. Operator declares `allowed_tools` + `verifier` on the state.
2. Consumer project installs the CC hook + sets `CTXR_FSM_REQUIRE_COSIGNATURE=1`.
3. `fsm.start_run` writes the marker; layer 4 is armed.
4. Worker reads `brief.allowed_tools`; CC hook blocks off-allowlist calls (4+2).
5. Worker calls `fsm.commit_outputs` with a `CommitSignature` (5).
6. Server runs spec-hash lock (9) → signature (5) → engine → verifier (3) →
   stages journal_txn → mints token (12).
7. Worker calls `fsm.confirm_commit` (12); substrate replays + rewrites the marker.
8. Drift detector tallies every event on the bus (8); misbehaviour eventually
   trips `drift_paused`.

Layers are independent — disable any one and the others still hold their line.
