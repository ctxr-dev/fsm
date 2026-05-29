#!/usr/bin/env node
// fsm-resume: rewind a run back to a prior-entered state and re-emit
// that state's brief. Used to recover from a fault or to replay from a
// known-good checkpoint without manual trace surgery.
//
// Usage:
//   --run-id <id>           required; the run to resume.
//   --from-state <state-id> required; must have been entered earlier in
//                           this run (i.e. has an entry trace).
//   [--session-id S]        defaults to a PID-based identifier, matching
//                           fsm-next / fsm-commit.
//   [--fsm-path P]          overrides .fsmrc.json.
//   [--storage-root D]      overrides .fsmrc.json.
//
// Behavior:
//   1. Validate the run exists (read manifest).
//   2. Validate <state-id> appears as a prior entry trace in this run;
//      refuse with a structured error if not.
//   3. Prune trace files past the resume target's entry trace.
//   4. Release any prior lock; acquire a fresh lock for --session-id.
//   5. Reset manifest current_state to <state-id> (status: in_progress).
//   6. Append a structured annotation to manifest.resume_history[].
//   7. Emit the brief for the resumed state on stdout.
//
// Output: JSON brief on stdout. Exit 0 on success; non-zero on lock
// conflict, run-not-found, fsm_yaml_changed, unknown_state, or
// state_not_entered_in_run.

import { writeFileSync, openSync, closeSync, fsyncSync, rmSync, existsSync } from "node:fs";
import { join } from "node:path";

import { emitJson } from "./lib/emit.mjs";
import {
  acquireLock,
  discardJournal,
  discardOrphanedLock,
  journalState,
  pruneTraceAfter,
  readLock,
  readManifest,
  readTrace,
  replayJournal,
  runDirPath,
} from "./lib/fsm-storage.mjs";
import {
  buildBrief,
  loadFsm,
  runEnv,
  stateById,
  updateManifest,
} from "./lib/fsm-engine.mjs";
import { resolveSettings, resolveStorageRoot } from "./lib/fsm-config.mjs";

function parseArgs(argv) {
  const args = {};
  // Helper: every flag below expects exactly one argument value. Reject
  // a flag whose value is missing (end of argv) or another flag, so
  // mistakes like `fsm-resume --journal` (no action) surface as a
  // pointed parse error rather than the generic
  // "either --from-state or --journal ..." fallback.
  const takeValue = (flag, i) => {
    const v = argv[i + 1];
    if (v === undefined || (typeof v === "string" && v.startsWith("--"))) {
      throw new Error(`${flag} requires a value`);
    }
    return v;
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--run-id") args.runId = takeValue(arg, i++);
    else if (arg === "--from-state") args.fromState = takeValue(arg, i++);
    else if (arg === "--journal") args.journalAction = takeValue(arg, i++);
    else if (arg === "--session-id") args.sessionId = takeValue(arg, i++);
    else if (arg === "--fsm" || arg === "--fsm-name") args.fsmName = takeValue(arg, i++);
    else if (arg === "--fsm-path") args.fsmPath = takeValue(arg, i++);
    else if (arg === "--storage-root") args.storageRoot = takeValue(arg, i++);
    else throw new Error(`fsm-resume: unknown argument "${arg}"`);
  }
  if (!args.runId) throw new Error("--run-id is required");
  if (args.journalAction) {
    if (!["discard", "replay"].includes(args.journalAction)) {
      throw new Error(
        `--journal must be "discard" or "replay", got "${args.journalAction}"`,
      );
    }
    if (args.fromState) {
      throw new Error("--journal and --from-state are mutually exclusive");
    }
  } else if (!args.fromState) {
    throw new Error("either --from-state or --journal <discard|replay> is required");
  }
  return args;
}

// Delegates to ./lib/emit.mjs which loops fs.writeSync until the full
// payload is written (single writeSync may partial-write) and swallows
// EPIPE when the reader closes early. Issue #12.
const emit = emitJson;

function fail(error, code = 1) {
  process.stderr.write(`fsm-resume: ${error}\n`);
  process.exit(code);
}

let parsed;
try {
  parsed = parseArgs(process.argv.slice(2));
} catch (err) {
  fail(err.message, 2);
}

// --journal {discard|replay}: dedicated A7 recovery short-circuit.
//
// Runs BEFORE resolveSettings + loadFsm because recovery only needs
// `storageRoot + runId`. resolveSettings requires fsmPath (CLI override
// OR a .fsmrc.json entry), which would block recovery in setups
// without config. Use the lightweight resolveStorageRoot helper to
// honour --storage-root if passed, or fall back to .fsmrc.json's
// storage_root. An operator should be able to discard or replay an
// incomplete commit even when the FSM YAML has been moved / renamed /
// deleted.
if (parsed.journalAction) {
  let recoveryStorageRoot;
  try {
    recoveryStorageRoot = resolveStorageRoot(parsed);
  } catch (err) {
    fail(err.message, 2);
  }
  doJournalRecovery(recoveryStorageRoot);
}

let settings;
try {
  settings = resolveSettings(parsed);
} catch (err) {
  fail(err.message, 2);
}

// Inspects <run_dir>/.journal/ and either rolls back the pending
// transaction (discard) or finalises it idempotently (replay). Does NOT
// touch the lock, the trace, or the manifest beyond what the recovery
// op naturally produces (replay's rename loop finalises any staged
// manifest.json the crash captured). Pre-existing journals from a
// previous crashed fsm-commit are the only target.
//
// Runs BEFORE loadFsm + readManifest because recovery only needs
// storageRoot + runId. An operator should be able to recover an
// incomplete commit even when the FSM YAML has been moved/renamed or
// the run's manifest somehow got truncated — both would make a regular
// --from-state resume impossible, but a journal can still be
// discarded or replayed bytewise.
function doJournalRecovery(storageRoot) {
  const recoveryRunDir = runDirPath(parsed.runId, { storageRoot });
  // Distinguish "run dir does not exist" from "run dir exists but
  // carries no journal". The previous unconditional `no_journal_present`
  // misled operators into thinking recovery succeeded when they had
  // simply mistyped the run-id or storage-root. Emit run_not_found
  // (exit 1) for the missing-run-dir case so automation can tell
  // them apart without needing to load the manifest (which we
  // intentionally avoid in the recovery short-circuit).
  if (!existsSync(recoveryRunDir)) {
    emit({
      error: "run_not_found",
      run_id: parsed.runId,
      journal_action: parsed.journalAction,
    });
    process.exit(1);
  }
  // journalState can throw if `.journal` is unreadable / a regular
  // file. Recovery should still surface a usable error payload
  // rather than crash with a stack trace.
  let jstate;
  try {
    jstate = journalState(recoveryRunDir);
  } catch (err) {
    emit({
      error: "journal_inspect_failed",
      run_id: parsed.runId,
      journal_action: parsed.journalAction,
      detail: err.message,
    });
    process.exit(1);
  }
  if (!jstate.hasJournal) {
    emit({
      ok: true,
      run_id: parsed.runId,
      journal_action: parsed.journalAction,
      result: "no_journal_present",
    });
    process.exit(0);
  }
  if (parsed.journalAction === "discard") {
    let out;
    try {
      // Orphaned-lock branch: if journalState surfaced lock_only AND
      // could not parse the lock payload's txn_id, calling
      // discardJournal(null) would throw "txnId must be a non-empty
      // string". Route to discardOrphanedLock instead, which clears
      // the lock without requiring a txnId match.
      if (jstate.lock_only && !jstate.txnId) {
        out = discardOrphanedLock(recoveryRunDir);
      } else {
        out = discardJournal(recoveryRunDir, jstate.txnId);
      }
    } catch (err) {
      emit({
        error: "discard_failed",
        run_id: parsed.runId,
        txn_id: jstate.txnId,
        detail: err.message,
      });
      process.exit(1);
    }
    // Emit only snake_case keys. Spreading `...out` would mix the
    // helper's camelCase `txnId` into the payload alongside the
    // explicit `txn_id` and confuse downstream consumers.
    emit({
      ok: true,
      run_id: parsed.runId,
      journal_action: "discard",
      txn_id: jstate.txnId,
      status_before: jstate.status,
      discarded: out.discarded,
      reason: out.reason ?? null,
    });
    process.exit(0);
  }
  // replay
  if (jstate.status !== "ready_to_finalise") {
    emit({
      error: "replay_not_safe",
      run_id: parsed.runId,
      txn_id: jstate.txnId,
      status: jstate.status,
      hint:
        "journal status must be ready_to_finalise to replay. " +
        "`pending` means the producing fn never returned; " +
        "`lock_only` means a crash left only the single-writer lock " +
        "(no staged work to replay). In both cases use --journal discard.",
    });
    process.exit(1);
  }
  let out;
  try {
    out = replayJournal(recoveryRunDir, jstate.txnId);
  } catch (err) {
    // replayJournal's tightened checks (symlink containment,
    // missing-source-AND-missing-dest, etc.) all throw. Surface as
    // a structured CLI error so operators and automation can react
    // instead of getting a stack trace.
    emit({
      error: "replay_failed",
      run_id: parsed.runId,
      txn_id: jstate.txnId,
      detail: err.message,
    });
    process.exit(1);
  }
  // Emit only snake_case keys. Spreading `...out` would mix the
  // helper's camelCase `txnId` into the payload alongside the
  // explicit `txn_id`.
  emit({
    ok: true,
    run_id: parsed.runId,
    journal_action: "replay",
    txn_id: jstate.txnId,
    replayed: out.replayed,
    finalised: out.finalised ?? [],
  });
  process.exit(0);
}

let fsm;
try {
  fsm = loadFsm({ fsmPath: settings.fsmPath });
} catch (err) {
  fail(err.message, 1);
}

const manifest = readManifest(parsed.runId, { storageRoot: settings.storageRoot });
if (!manifest) {
  emit({ error: "run_not_found", run_id: parsed.runId });
  process.exit(1);
}

// Resume is for runs that can legitimately be rewound: an `in_progress`
// run the operator wants to pivot, a `paused` run waiting to be picked
// back up, or a `faulted` run the operator wants to retry past the
// fault. Refuse the other lifecycle states (`completed`, `abandoned`,
// `superseded`, `stale`, anything else): rewinding them would silently
// re-open a terminal record and lose the durable verdict.
const RESUMABLE_STATUSES = new Set(["in_progress", "paused", "faulted"]);
if (!RESUMABLE_STATUSES.has(manifest.status)) {
  emit({
    error: "run_not_resumable",
    run_id: parsed.runId,
    status: manifest.status,
    resumable_statuses: [...RESUMABLE_STATUSES],
  });
  process.exit(1);
}

if (manifest.fsm_yaml_hash !== fsm.hash) {
  emit({
    error: "fsm_yaml_changed",
    run_id: parsed.runId,
    run_hash: manifest.fsm_yaml_hash,
    current_hash: fsm.hash,
    current_state: manifest.current_state,
  });
  process.exit(1);
}

// Validate the target state exists in the FSM definition. A typo here
// is a different failure mode from "state existed in the FSM but the run
// never entered it" (the second is state_not_entered_in_run; this one
// is unknown_state).
let targetState;
try {
  targetState = stateById(fsm.doc, parsed.fromState);
} catch (err) {
  emit({
    error: "unknown_state",
    run_id: parsed.runId,
    from_state: parsed.fromState,
    detail: err.message,
  });
  process.exit(1);
}

// Locate the most recent entry trace for the target state. If no entry
// trace exists for that state, the run never entered it: refuse.
const trace = readTrace(parsed.runId, { storageRoot: settings.storageRoot });
const entriesForState = trace.filter(
  (r) => r.data?.phase === "entry" && r.data?.state === parsed.fromState,
);
if (entriesForState.length === 0) {
  const enteredStates = Array.from(
    new Set(
      trace
        .filter((r) => r.data?.phase === "entry")
        .map((r) => r.data?.state)
        .filter((s) => typeof s === "string"),
    ),
  );
  emit({
    error: "state_not_entered_in_run",
    run_id: parsed.runId,
    from_state: parsed.fromState,
    entered_states: enteredStates,
  });
  process.exit(1);
}
// readTrace returns records sorted by filename TEXT, so once trace
// sequences reach five digits (`10000-...`) `0009999-...` would sort
// after `00010000-...` and the array's last element is no longer the
// most recent entry. Pick the highest numeric sequence explicitly so
// resume always prunes back to the latest entry trace for the state.
const entryRecord = entriesForState.reduce((best, r) => {
  const seq = Number.isFinite(r.data?.sequence) ? r.data.sequence : -Infinity;
  const bestSeq = Number.isFinite(best.data?.sequence) ? best.data.sequence : -Infinity;
  return seq > bestSeq ? r : best;
}, entriesForState[0]);
const entrySequence = entryRecord.data.sequence;

// Lock takeover policy. fsm-commit only checks the lock at startup,
// so if we force-stole the lock from an active in_progress writer it
// would keep writing traces and the manifest after we've pruned and
// reset the run, leaving state corrupted. Therefore:
//
//  1. If the existing lock is stale (TTL expired), forcibly remove it
//     and acquire a fresh one.
//  2. Otherwise, only take over when the manifest's status is paused
//     or faulted (those statuses have no active writer by definition,
//     and the lock file is leftover metadata).
//  3. For an active in_progress run with a live lock, refuse: the
//     operator must wait for the writer to release or escalate via
//     pause / fault before retrying resume.
const runDir = runDirPath(parsed.runId, { storageRoot: settings.storageRoot });
const lockPath = join(runDir, "lock.json");
if (existsSync(lockPath)) {
  // Re-read manifest AND lock at takeover time so two concurrent
  // resumers cannot both see a stale "paused / faulted" snapshot and
  // each unlink the other's freshly-acquired lock. The combination
  // protects against:
  //  - manifest status flipped to in_progress between the initial
  //    read at the top of this script and now;
  //  - another resumer just acquired a fresh lock for itself (we'd
  //    see a different session_id / expires_at and must refuse).
  const freshManifest = readManifest(parsed.runId, { storageRoot: settings.storageRoot });
  const existing = readLock(parsed.runId, { storageRoot: settings.storageRoot });
  if (existing) {
    const now = Date.now();
    const expiresAt = Date.parse(existing.expires_at ?? "");
    const isStale = Number.isFinite(expiresAt) && expiresAt < now;
    const fmStatus = freshManifest?.status ?? manifest.status;
    const isPassiveStatus = fmStatus === "paused" || fmStatus === "faulted";
    if (isStale || isPassiveStatus) {
      // Compare-and-swap: unlink ONLY if the lock on disk RIGHT NOW
      // is the same lock we just inspected. If another resumer has
      // already replaced it with their own fresh lock, the
      // re-read here will differ and we refuse to clobber.
      const afterRead = readLock(parsed.runId, { storageRoot: settings.storageRoot });
      const sameLock =
        afterRead &&
        afterRead.session_id === existing.session_id &&
        afterRead.expires_at === existing.expires_at;
      if (sameLock) {
        rmSync(lockPath, { force: true });
      } else {
        emit({
          error: "run_locked",
          run_id: parsed.runId,
          run_status: fmStatus,
          lock: afterRead ?? existing,
          hint:
            "another resume / writer raced ahead and replaced the lock; re-read state and retry if appropriate.",
        });
        process.exit(1);
      }
    } else {
      emit({
        error: "run_locked",
        run_id: parsed.runId,
        run_status: fmStatus,
        lock: existing,
        hint:
          "resume refuses to take a non-stale lock from an in_progress run; pause the run or wait for the writer to release before retrying.",
      });
      process.exit(1);
    }
  }
}

const lockResult = acquireLock(parsed.runId, {
  sessionId: settings.sessionId,
  storageRoot: settings.storageRoot,
});
if (!lockResult.acquired) {
  emit({ error: "run_locked", lock: lockResult.lock });
  process.exit(1);
}

// Prune everything past the target state's entry trace. The entry trace
// itself is kept: the run is now positioned at "just entered fromState",
// ready for a fresh commit.
const pruneResult = pruneTraceAfter(
  parsed.runId,
  entrySequence,
  { storageRoot: settings.storageRoot },
);

const timestamp = new Date().toISOString();
const annotation = {
  from_state: parsed.fromState,
  timestamp,
  pruned_traces_count: pruneResult.removed,
  session_id: settings.sessionId,
};
const priorHistory = Array.isArray(manifest.resume_history)
  ? manifest.resume_history
  : [];

// Recompute transitions_count from the retained trace. Each completed
// state transition leaves an exit trace, so the post-prune exit-trace
// count is the authoritative new transitions_count. Without this, the
// manifest carries the pre-resume count which includes transitions
// whose exit traces have just been removed.
const retainedExitCount = readTrace(parsed.runId, {
  storageRoot: settings.storageRoot,
}).filter((r) => r.data?.phase === "exit").length;

updateManifest(
  parsed.runId,
  {
    status: "in_progress",
    current_state: parsed.fromState,
    next_state: null,
    resume_history: [...priorHistory, annotation],
    transitions_count: retainedExitCount,
    ended_at: null,
    // Clear any pause metadata so the manifest does not advertise a
    // stale paused_at/pause_reason on what is now an in_progress run.
    // These fields only have meaning while status === "paused".
    paused_at: null,
    pause_reason: null,
  },
  { storageRoot: settings.storageRoot },
);

// Re-read the env from the (now pruned) trace so the brief reflects only
// outputs that actually preceded the resumed state.
const env = runEnv(parsed.runId, { storageRoot: settings.storageRoot });
if (!env.args && manifest.args && typeof manifest.args === "object") {
  // Recover args from the manifest if the entry trace for the original
  // run head was pruned (the manifest is the durable source of args).
  env.args = manifest.args;
}
const brief = buildBrief({
  doc: fsm.doc,
  state: targetState,
  env,
  runId: parsed.runId,
});

// Helper kept inline (not exported) so we don't grow the public lib API
// for a single CLI concern: stamp a sidecar file so operators inspecting
// the run dir see the resume annotation at a glance even before reading
// the manifest. The manifest is the source of truth; this is convenience.
function writeResumeSidecar() {
  const sidecarPath = join(runDir, "fsm-trace", `RESUMED-from-${parsed.fromState}-at-${timestamp.replace(/[:.]/g, "-")}.yaml`);
  // The CLI session_id (and, defensively, every other scalar emitted
  // here) is user-supplied and may contain ":", newlines, or quotes;
  // emitting it as an unquoted plain scalar could either produce
  // malformed YAML or, worse, inject extra fields into the sidecar.
  // Wrap each scalar in a single-quoted YAML string and double up any
  // embedded single quote per the YAML 1.2 single-quoted-style spec.
  const quote = (v) => `'${String(v).replace(/'/g, "''")}'`;
  const payload = [
    `# Resume annotation (mirror of manifest.resume_history[-1])`,
    `from_state: ${quote(parsed.fromState)}`,
    `timestamp: ${quote(timestamp)}`,
    `pruned_traces_count: ${Number.isInteger(pruneResult.removed) ? pruneResult.removed : 0}`,
    `session_id: ${quote(settings.sessionId)}`,
    "",
  ].join("\n");
  const fd = openSync(sidecarPath, "w", 0o644);
  try {
    writeFileSync(fd, payload);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}
writeResumeSidecar();

emit({
  ok: true,
  resumed: true,
  resumed_from_state: parsed.fromState,
  pruned_traces_count: pruneResult.removed,
  pruned_traces: pruneResult.files,
  resume_annotation: annotation,
  ...brief,
});
process.exit(0);
