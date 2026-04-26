#!/usr/bin/env node
// fsm-inspect — debug dump for an FSM run.
//
// Usage:
//   --run-id <id> [--storage-root D]
//
// Output: JSON with manifest + lock state + ordered list of trace records.

import {
  readLock,
  readManifest,
  readTrace,
  runDirPath,
} from "./lib/fsm-storage.mjs";
import { resolveSettings } from "./lib/fsm-config.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--run-id") args.runId = argv[++i];
    else if (arg === "--storage-root") args.storageRoot = argv[++i];
    // Tolerate --fsm-path even though we don't use it; lets callers pass
    // a single shared arg set across all CLIs.
    else if (arg === "--fsm-path") args.fsmPath = argv[++i];
    else if (arg === "--session-id") args.sessionId = argv[++i];
    else throw new Error(`fsm-inspect: unknown argument "${arg}"`);
  }
  if (!args.runId) throw new Error("--run-id is required");
  return args;
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

let parsed;
try {
  parsed = parseArgs(process.argv.slice(2));
} catch (err) {
  process.stderr.write(`fsm-inspect: ${err.message}\n`);
  process.exit(2);
}

let settings;
try {
  // fsm-inspect doesn't need fsmPath; resolveSettings would throw without
  // it. Synthesise a placeholder so we can reuse the storageRoot resolver.
  settings = resolveSettings({ ...parsed, fsmPath: parsed.fsmPath ?? "PLACEHOLDER" });
} catch (err) {
  process.stderr.write(`fsm-inspect: ${err.message}\n`);
  process.exit(2);
}

const manifest = readManifest(parsed.runId, { storageRoot: settings.storageRoot });
if (!manifest) {
  emit({ error: "run_not_found", run_id: parsed.runId });
  process.exit(1);
}

const lock = readLock(parsed.runId, { storageRoot: settings.storageRoot });
const trace = readTrace(parsed.runId, { storageRoot: settings.storageRoot });

emit({
  ok: true,
  run_id: parsed.runId,
  run_dir_path: runDirPath(parsed.runId, { storageRoot: settings.storageRoot }),
  manifest,
  lock,
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
