# Changelog

All notable changes to `@ctxr/fsm` are documented here. The format is
loosely [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project follows [SemVer](https://semver.org/).

## [Unreleased]

### Added

- **A7: atomic-tx storage journal.** Every `fsm-commit` now runs inside a
  journal-style transaction (`withJournal`). All participating files
  (manifest patch, trace records, aggregator outputs) stage to
  `<run_dir>/.journal/<txn-id>/<mirror-of-rel-path>` and the rename loop
  finalises them in one atomic step. A crash between "fn returned" and
  the rename loop leaves a `ready_to_finalise` journal on disk;
  `fsm-next --resume` and `fsm-commit` both refuse to advance until the
  user explicitly recovers with `fsm-resume --journal {discard|replay}`.
  `fsm-inspect` surfaces the journal state in its standard output.
- New storage helpers exported from `@ctxr/fsm/storage`:
  `withJournal(runDir, fn)`, `journalState(runDir)`,
  `discardJournal(runDir, txnId)`, `replayJournal(runDir, txnId)`.
- `fsm-resume` gained a `--journal {discard|replay}` mode that does NOT
  require `--from-state`. Use `discard` to roll back an incomplete
  commit (e.g. `pending` status) or `replay` to idempotently finalise a
  `ready_to_finalise` commit.
- `appendTraceFile`, `readManifest`, `writeManifest`, `updateManifest`,
  `aggregateLoopOutputs`, and `aggregateAcrossStates` now honour an
  optional `transaction` option that routes writes through the journal
  staging area.
- Test injection hook: `FSM_TEST_PAUSE_BEFORE_FINALISE=<ms>` env var
  inserts a synchronous sleep (implemented via `Atomics.wait` on a
  private `SharedArrayBuffer`, so no CPU is burned) between the
  "ready_to_finalise" journal write and the rename loop. Integration
  tests (`tests/integration/atomic-tx.test.js`) SIGKILL the child
  during the pause window to assert journal recovery semantics.

### Changed

- `fsm-commit` was restructured around `withJournal`: every multi-write
  sequence (loop-iteration commit, post-validation fault, no-transition
  fault, terminal completion, advance with entry trace + manifest
  patch) runs inside one transaction. Brief computation for the emitted
  payload now runs AFTER the journal finalises so disk-read counters
  (`countLoopIterations`, `runEnv`) observe the just-written records.
- `fsm-next --resume` refuses with
  `{"error":"incomplete_commit_detected"}` when a journal is present,
  carrying the `recovery` commands the operator should run.
- `fsm-inspect` output now includes a `journal` field
  (`{ present, txn_id, status, staged, recovery }`).
- Package `scripts` split into `test` (unit + integration), `test:unit`,
  and `test:integration`.

## [0.1.0] - 2026-05-29

Initial publish. FSM substrate with file-only state storage,
JSON-Schema-validated worker contracts, predicate DSL for transitions,
hub-and-spoke orchestrator-shell architecture.
