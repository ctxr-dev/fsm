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
  pruneTraceAfter,
  readLock,
  readManifest,
  readTrace,
  runDirPath,
} from "./lib/fsm-storage.mjs";
import {
  buildBrief,
  loadFsm,
  runEnv,
  stateById,
  updateManifest,
} from "./lib/fsm-engine.mjs";
import { resolveSettings } from "./lib/fsm-config.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--run-id") args.runId = argv[++i];
    else if (arg === "--from-state") args.fromState = argv[++i];
    else if (arg === "--session-id") args.sessionId = argv[++i];
    else if (arg === "--fsm" || arg === "--fsm-name") args.fsmName = argv[++i];
    else if (arg === "--fsm-path") args.fsmPath = argv[++i];
    else if (arg === "--storage-root") args.storageRoot = argv[++i];
    else throw new Error(`fsm-resume: unknown argument "${arg}"`);
  }
  if (!args.runId) throw new Error("--run-id is required");
  if (!args.fromState) throw new Error("--from-state is required");
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

let settings;
try {
  settings = resolveSettings(parsed);
} catch (err) {
  fail(err.message, 2);
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
const entryRecord = entriesForState[entriesForState.length - 1];
const entrySequence = entryRecord.data.sequence;

// Release any prior lock regardless of holder, since resume is an explicit
// operator action. The lock file path lives under the run dir; release
// via session match first, then force-delete if a stale or alien lock
// remains. This guarantees we acquire a fresh lock cleanly.
const runDir = runDirPath(parsed.runId, { storageRoot: settings.storageRoot });
const lockPath = join(runDir, "lock.json");
if (existsSync(lockPath)) {
  // Best-effort owner release: if the existing lock belongs to the
  // calling session, take the friendly path. Otherwise, force-unlink.
  const existing = readLock(parsed.runId, { storageRoot: settings.storageRoot });
  if (existing) {
    rmSync(lockPath, { force: true });
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
  const payload = [
    `# Resume annotation (mirror of manifest.resume_history[-1])`,
    `from_state: ${parsed.fromState}`,
    `timestamp: ${timestamp}`,
    `pruned_traces_count: ${pruneResult.removed}`,
    `session_id: ${settings.sessionId}`,
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
