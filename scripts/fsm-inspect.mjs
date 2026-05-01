#!/usr/bin/env node
// fsm-inspect — debug dump for an FSM run.
//
// Usage:
//   --run-id <id> [--storage-root D]
//
// Output: JSON with manifest + lock state + ordered list of trace records.

import { writeSync } from "node:fs";
import { resolve } from "node:path";

import {
  readLock,
  readManifest,
  readTrace,
  runDirPath,
} from "./lib/fsm-storage.mjs";
import { loadConfig } from "./lib/fsm-config.mjs";

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

function emit(payload) {
  // writeSync to fd 1 (stdout) — a blocking POSIX write that does NOT
  // queue in Node's userspace stream buffer. The previous shape
  // (`process.stdout.write(...)` followed by `process.exit(...)`)
  // truncated payloads larger than the kernel pipe buffer (~64KB on
  // macOS) because the userspace queue was dropped at exit before it
  // could drain. Issue #12.
  writeSync(1, `${JSON.stringify(payload, null, 2)}\n`);
}

let parsed;
try {
  parsed = parseArgs(process.argv.slice(2));
} catch (err) {
  process.stderr.write(`fsm-inspect: ${err.message}\n`);
  process.exit(2);
}

// fsm-inspect only needs storage_root (no FSM YAML loaded). Resolve it
// directly via loadConfig + CLI override, without the full resolveSettings
// machinery that requires fsmPath.
let storageRoot;
try {
  storageRoot = resolveStorageRoot(parsed);
} catch (err) {
  process.stderr.write(`fsm-inspect: ${err.message}\n`);
  process.exit(2);
}

function resolveStorageRoot(cliArgs) {
  const cwd = process.cwd();
  if (cliArgs.storageRoot) return resolve(cwd, cliArgs.storageRoot);
  const cfg = loadConfig(cwd);
  let entry;
  if (cliArgs.fsmName) {
    entry = cfg.fsms.find((f) => f.name === cliArgs.fsmName);
    if (!entry) {
      const available = cfg.fsms.map((f) => f.name).join(", ") || "(none)";
      throw new Error(
        `--fsm "${cliArgs.fsmName}" not found in ${cfg._config_path ?? ".fsmrc.json"}. Available: ${available}`,
      );
    }
  } else if (cfg.fsms.length === 1) {
    entry = cfg.fsms[0];
  } else if (cfg.fsms.length === 0) {
    throw new Error("must pass --storage-root or have a .fsmrc.json with fsms[]");
  } else {
    const available = cfg.fsms.map((f) => f.name).join(", ");
    throw new Error(
      `multiple FSMs configured (${available}); pass --fsm <name> or --storage-root`,
    );
  }
  return resolve(cwd, entry.storage_root);
}

const manifest = readManifest(parsed.runId, { storageRoot });
if (!manifest) {
  emit({ error: "run_not_found", run_id: parsed.runId });
  process.exit(1);
}

const lock = readLock(parsed.runId, { storageRoot });
const trace = readTrace(parsed.runId, { storageRoot });

emit({
  ok: true,
  run_id: parsed.runId,
  run_dir_path: runDirPath(parsed.runId, { storageRoot }),
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
