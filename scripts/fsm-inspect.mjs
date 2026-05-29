#!/usr/bin/env node
// fsm-inspect — debug dump for an FSM run.
//
// Usage:
//   --run-id <id> [--storage-root D]
//
// Output: JSON with manifest + lock state + ordered list of trace records.

import { emitJson } from "./lib/emit.mjs";
import {
  journalState,
  publicJournalProjection,
  readLock,
  readManifest,
  readTrace,
  runDirPath,
} from "./lib/fsm-storage.mjs";
import { resolveStorageRoot as resolveStorageRootFromConfig } from "./lib/fsm-config.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--run-id") args.runId = argv[++i];
    else if (arg === "--storage-root") args.storageRoot = argv[++i];
    // Tolerate --fsm-path / --fsm even though fsm-inspect only needs
    // storage_root; lets callers pass a single shared arg set across CLIs.
    else if (arg === "--fsm-path") args.fsmPath = argv[++i];
    else if (arg === "--fsm" || arg === "--fsm-name") args.fsmName = argv[++i];
    else if (arg === "--session-id") args.sessionId = argv[++i];
    else throw new Error(`fsm-inspect: unknown argument "${arg}"`);
  }
  if (!args.runId) throw new Error("--run-id is required");
  return args;
}

// Delegates to ./lib/emit.mjs which loops fs.writeSync until the full
// payload is written (single writeSync may partial-write) and swallows
// EPIPE when the reader closes early. Issue #12.
const emit = emitJson;

let parsed;
try {
  parsed = parseArgs(process.argv.slice(2));
} catch (err) {
  process.stderr.write(`fsm-inspect: ${err.message}\n`);
  process.exit(2);
}

// fsm-inspect only needs storage_root (no FSM YAML loaded). Use the
// shared resolveStorageRoot helper (also used by fsm-resume's journal
// recovery short-circuit) so the resolution rules stay in one place.
let storageRoot;
try {
  storageRoot = resolveStorageRootFromConfig(parsed);
} catch (err) {
  process.stderr.write(`fsm-inspect: ${err.message}\n`);
  process.exit(2);
}

const manifest = readManifest(parsed.runId, { storageRoot });
if (!manifest) {
  emit({ error: "run_not_found", run_id: parsed.runId });
  process.exit(1);
}

const lock = readLock(parsed.runId, { storageRoot });
const trace = readTrace(parsed.runId, { storageRoot });
const runDir = runDirPath(parsed.runId, { storageRoot });
// journalState can throw if `.journal` is unreadable or is a regular
// file rather than a directory. Inspect should still emit a usable
// payload — surface the inspection failure under a structured
// `journal.error` field, leave the rest of the output intact.
let jstate;
let journalInspectError;
try {
  jstate = journalState(runDir);
} catch (err) {
  jstate = { hasJournal: false };
  journalInspectError = err.message;
}
const journal = journalInspectError
  ? { present: false, error: journalInspectError }
  : jstate.hasJournal
  ? {
      present: true,
      ...publicJournalProjection(jstate),
      recovery: {
        discard: `fsm-resume --run-id ${parsed.runId} --journal discard --storage-root ${storageRoot}`,
        replay: `fsm-resume --run-id ${parsed.runId} --journal replay --storage-root ${storageRoot}`,
      },
    }
  : { present: false };

emit({
  ok: true,
  run_id: parsed.runId,
  run_dir_path: runDir,
  manifest,
  lock,
  journal,
  trace_count: trace.length,
  trace: trace.map((r) => ({
    file: r.fileName,
    sequence: r.data.sequence,
    phase: r.data.phase,
    state: r.data.state,
    timestamp: r.data.timestamp,
  })),
});
process.exit(0);
