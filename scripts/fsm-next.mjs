#!/usr/bin/env node
// fsm-next — read disk state, return the next-state brief.
//
// Two modes:
//   --new-run --repo X --base-sha Y --head-sha Z [--args <json>]
//   --resume <run-id>
//
// Required (via flags or .fsmrc.json / fsm.config.json):
//   fsm_path     — path to the FSM YAML
//   storage_root — directory where run dirs live
//
// Optional flags:
//   --session-id S   — defaults to a PID-based identifier
//   --fsm-path P     — overrides config file
//   --storage-root D — overrides config file
//   --args <json>    — for --new-run, the run's argument bag
//   --args-file P    — for --new-run, JSON file with the arg bag
//
// Output: JSON brief on stdout. Exit 0 success; non-zero on lock conflict,
// run-not-found, or fsm_yaml_changed.

import { readFileSync } from "node:fs";

import {
  acquireLock,
  buildRunId,
  ensureRunDir,
  readManifest,
} from "./lib/fsm-storage.mjs";
import {
  buildBrief,
  initialiseManifest,
  loadFsm,
  runEnv,
  stateById,
  updateManifest,
  writeEntryTrace,
} from "./lib/fsm-engine.mjs";
import { resolveSettings } from "./lib/fsm-config.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--new-run") args.newRun = true;
    else if (arg === "--resume") args.resumeRunId = argv[++i];
    else if (arg === "--repo") args.repo = argv[++i];
    else if (arg === "--base-sha") args.baseSha = argv[++i];
    else if (arg === "--head-sha") args.headSha = argv[++i];
    else if (arg === "--args") args.args = JSON.parse(argv[++i]);
    else if (arg === "--args-file") args.args = JSON.parse(readFileSync(argv[++i], "utf8"));
    else if (arg === "--session-id") args.sessionId = argv[++i];
    else if (arg === "--fsm-path") args.fsmPath = argv[++i];
    else if (arg === "--storage-root") args.storageRoot = argv[++i];
    else throw new Error(`fsm-next: unknown argument "${arg}"`);
  }
  return args;
}

function emit(payload) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
}

function fail(error, code = 1) {
  process.stderr.write(`fsm-next: ${error}\n`);
  process.exit(code);
}

let parsed;
try {
  parsed = parseArgs(process.argv.slice(2));
} catch (err) {
  fail(err.message, 2);
}

if (parsed.newRun && parsed.resumeRunId) {
  fail("--new-run and --resume are mutually exclusive", 2);
}
if (!parsed.newRun && !parsed.resumeRunId) {
  fail("must pass --new-run or --resume <run-id>", 2);
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

if (parsed.newRun) {
  if (!parsed.repo) fail("--new-run requires --repo", 2);
  const built = buildRunId({
    repo: parsed.repo,
    baseSha: parsed.baseSha ?? "",
    headSha: parsed.headSha ?? "",
  });
  const runId = built.runId;
  ensureRunDir(runId, { storageRoot: settings.storageRoot });
  const lock = acquireLock(runId, {
    sessionId: settings.sessionId,
    storageRoot: settings.storageRoot,
  });
  if (!lock.acquired) {
    emit({ error: "run_locked", lock: lock.lock });
    process.exit(1);
  }
  initialiseManifest({
    runId,
    fsmDoc: fsm.doc,
    fsmHash: fsm.hash,
    args: parsed.args ?? {},
    repo: parsed.repo,
    baseSha: parsed.baseSha,
    headSha: parsed.headSha,
    storageRoot: settings.storageRoot,
  });
  const entryState = stateById(fsm.doc, fsm.doc.fsm.entry);
  const env = { args: parsed.args ?? {} };
  const inputs = entryState.worker?.inputs?.reduce((acc, name) => {
    acc[name] = env[name];
    return acc;
  }, {}) ?? {};
  writeEntryTrace(
    runId,
    { state: entryState, inputs },
    { storageRoot: settings.storageRoot },
  );
  updateManifest(
    runId,
    { current_state: entryState.id, next_state: null },
    { storageRoot: settings.storageRoot },
  );
  const brief = buildBrief({ doc: fsm.doc, state: entryState, env, runId });
  emit({ ok: true, ...brief });
  process.exit(0);
}

const runId = parsed.resumeRunId;
const manifest = readManifest(runId, { storageRoot: settings.storageRoot });
if (!manifest) {
  emit({ error: "run_not_found", run_id: runId });
  process.exit(1);
}
if (manifest.fsm_yaml_hash !== fsm.hash) {
  emit({
    error: "fsm_yaml_changed",
    run_id: runId,
    run_hash: manifest.fsm_yaml_hash,
    current_hash: fsm.hash,
    current_state: manifest.current_state,
  });
  process.exit(1);
}
if (manifest.status !== "in_progress" && manifest.status !== "paused") {
  emit({
    error: "run_not_resumable",
    run_id: runId,
    status: manifest.status,
  });
  process.exit(1);
}
const lock = acquireLock(runId, {
  sessionId: settings.sessionId,
  storageRoot: settings.storageRoot,
});
if (!lock.acquired) {
  emit({ error: "run_locked", lock: lock.lock });
  process.exit(1);
}
const env = runEnv(runId, { storageRoot: settings.storageRoot });
const stateId = manifest.current_state ?? fsm.doc.fsm.entry;
const state = stateById(fsm.doc, stateId);
const brief = buildBrief({ doc: fsm.doc, state, env, runId });
emit({ ok: true, resumed: true, ...brief });
process.exit(0);
