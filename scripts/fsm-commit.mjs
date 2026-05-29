#!/usr/bin/env node
// fsm-commit — validate worker output, write state-exit, advance.
//
// Usage:
//   --run-id <id>
//   --outputs <json>            inline JSON of the state's outputs
//   --outputs-file <path>       path to a JSON file with outputs
//   [--transition <state-id>]   for kind=judgement transitions, the
//                               orchestrator picks via this flag
//   [--session-id S]            session must hold the lock
//   [--fsm-path P] [--storage-root D]
//
// Output: JSON brief for the next state on success, or
// { status: "terminal", verdict, run_dir_path } at terminal.
// Exit 0 on success, non-zero on schema/validation failure.

import { readFileSync } from "node:fs";

import { emitJson } from "./lib/emit.mjs";
import {
  appendTraceFile,
  readLock,
  readManifest,
  releaseLock,
  runDirPath,
} from "./lib/fsm-storage.mjs";
import {
  buildBrief,
  countLoopIterations,
  loadFsm,
  resolveTransition,
  runEnv,
  runLoopDecision,
  runPostValidations,
  stateById,
  updateManifest,
  validateOutputs,
  writeEntryTrace,
  writeExitTrace,
  writeFaultTrace,
} from "./lib/fsm-engine.mjs";
import { aggregateLoopOutputs } from "./lib/fsm-aggregator.mjs";
import { resolveSettings } from "./lib/fsm-config.mjs";

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--run-id") args.runId = argv[++i];
    else if (arg === "--outputs") args.outputs = JSON.parse(argv[++i]);
    else if (arg === "--outputs-file") args.outputs = JSON.parse(readFileSync(argv[++i], "utf8"));
    else if (arg === "--transition") args.judgementPick = argv[++i];
    else if (arg === "--session-id") args.sessionId = argv[++i];
    else if (arg === "--fsm" || arg === "--fsm-name") args.fsmName = argv[++i];
    else if (arg === "--fsm-path") args.fsmPath = argv[++i];
    else if (arg === "--storage-root") args.storageRoot = argv[++i];
    else throw new Error(`fsm-commit: unknown argument "${arg}"`);
  }
  if (!args.runId) throw new Error("--run-id is required");
  if (args.outputs === undefined) {
    throw new Error("either --outputs or --outputs-file is required");
  }
  return args;
}

// Delegates to ./lib/emit.mjs which loops fs.writeSync until the full
// payload is written (single writeSync may partial-write) and swallows
// EPIPE when the reader closes early. Issue #12.
const emit = emitJson;

function fail(error, code = 1) {
  process.stderr.write(`fsm-commit: ${error}\n`);
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

const manifest = readManifest(parsed.runId, { storageRoot: settings.storageRoot });
if (!manifest) {
  emit({ error: "run_not_found", run_id: parsed.runId });
  process.exit(1);
}

const lock = readLock(parsed.runId, { storageRoot: settings.storageRoot });
if (!lock || lock.session_id !== settings.sessionId) {
  emit({
    error: "lock_not_held",
    run_id: parsed.runId,
    expected_session: settings.sessionId,
    actual_lock: lock,
  });
  process.exit(1);
}

let fsm;
try {
  fsm = loadFsm({ fsmPath: settings.fsmPath });
} catch (err) {
  fail(err.message, 1);
}

if (manifest.fsm_yaml_hash !== fsm.hash) {
  emit({
    error: "fsm_yaml_changed",
    run_id: parsed.runId,
    run_hash: manifest.fsm_yaml_hash,
    current_hash: fsm.hash,
  });
  process.exit(1);
}

const stateId = manifest.current_state;
if (!stateId) {
  emit({ error: "no_current_state", run_id: parsed.runId });
  process.exit(1);
}

const state = stateById(fsm.doc, stateId);

const validationResult = validateOutputs(state, parsed.outputs);
if (!validationResult.valid) {
  writeFaultTrace(
    parsed.runId,
    {
      state,
      reason: "output_schema_violation",
      details: validationResult.errors,
    },
    { storageRoot: settings.storageRoot },
  );
  updateManifest(
    parsed.runId,
    { status: "faulted", ended_at: new Date().toISOString() },
    { storageRoot: settings.storageRoot },
  );
  releaseLock(parsed.runId, {
    sessionId: settings.sessionId,
    storageRoot: settings.storageRoot,
  });
  emit({
    error: "output_schema_violation",
    state: state.id,
    errors: validationResult.errors,
  });
  process.exit(1);
}

// Loop-state branch: write the iter trace and either re-emit the next
// loop brief (continue) or aggregate + fall through to the regular
// exit-trace + transition path (terminate).
let outputsForFlow = parsed.outputs;
if (state.loop) {
  const iterationN =
    countLoopIterations(parsed.runId, state.id, { storageRoot: settings.storageRoot }) + 1;
  appendTraceFile(
    parsed.runId,
    {
      phase: "iter",
      state: state.id,
      data: { iteration_n: iterationN, outputs: parsed.outputs },
    },
    { storageRoot: settings.storageRoot },
  );
  const decision = runLoopDecision(state, parsed.outputs, iterationN);
  if (!decision.terminate) {
    // Bump the manifest's last_update_at on every loop iteration so
    // run-health and stale-run signals still tick over while the loop
    // is making progress. current_state is reaffirmed defensively (it
    // is unchanged for a continuing loop, but this keeps the patch
    // self-explanatory and survives manifest schema drift).
    updateManifest(
      parsed.runId,
      { current_state: state.id },
      { storageRoot: settings.storageRoot },
    );
    const envSoFar = runEnv(parsed.runId, { storageRoot: settings.storageRoot });
    const continueBrief = buildBrief({
      doc: fsm.doc,
      state,
      env: envSoFar,
      runId: parsed.runId,
      opts: { storageRoot: settings.storageRoot },
    });
    emit({
      ok: true,
      loop_continued: true,
      ...continueBrief,
    });
    process.exit(0);
  }
  // Terminating: aggregate iter outputs into one canonical record and use
  // that as the loop state's outputs for the rest of the commit flow.
  const runDir = runDirPath(parsed.runId, { storageRoot: settings.storageRoot });
  const agg = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
  outputsForFlow = {
    [`aggregated_${state.id}`]: agg.aggregated_path,
    iteration_meta_path: agg.iteration_meta_path,
    total_iterations: agg.iteration_count,
    merged_length: agg.merged_length,
    terminated_by: decision.reason,
  };
}

const postValidations = runPostValidations(state);

const env = runEnv(parsed.runId, { storageRoot: settings.storageRoot });
const envWithCommit = { ...env, ...outputsForFlow };
const { transition, evaluations } = resolveTransition(state, envWithCommit, {
  judgementPick: parsed.judgementPick,
});

writeExitTrace(
  parsed.runId,
  {
    state,
    outputs: outputsForFlow,
    postValidations: postValidations.results,
    transitionEvals: evaluations,
    chosenTransition: transition?.to ?? null,
  },
  { storageRoot: settings.storageRoot },
);

if (!transition) {
  if ((state.transitions ?? []).length > 0) {
    writeFaultTrace(
      parsed.runId,
      {
        state,
        reason: "no_transition_matched",
        details: { evaluations },
      },
      { storageRoot: settings.storageRoot },
    );
    updateManifest(
      parsed.runId,
      { status: "faulted", ended_at: new Date().toISOString() },
      { storageRoot: settings.storageRoot },
    );
    releaseLock(parsed.runId, {
      sessionId: settings.sessionId,
      storageRoot: settings.storageRoot,
    });
    emit({
      error: "no_transition_matched",
      state: state.id,
      evaluations,
    });
    process.exit(1);
  }
  updateManifest(
    parsed.runId,
    {
      status: "completed",
      current_state: state.id,
      next_state: null,
      ended_at: new Date().toISOString(),
      verdict: envWithCommit.verdict ?? null,
      transitions_count: (manifest.transitions_count ?? 0) + 1,
    },
    { storageRoot: settings.storageRoot },
  );
  releaseLock(parsed.runId, {
    sessionId: settings.sessionId,
    storageRoot: settings.storageRoot,
  });
  emit({
    ok: true,
    status: "terminal",
    state: state.id,
    verdict: envWithCommit.verdict ?? null,
    run_dir_path: runDirPath(parsed.runId, { storageRoot: settings.storageRoot }),
  });
  process.exit(0);
}

const nextState = stateById(fsm.doc, transition.to);
// Loop states declare their worker under state.loop.worker; falling back
// here keeps the entry trace's recorded inputs consistent with what
// buildBrief will pass to the worker dispatch (otherwise the trace shows
// no inputs for a loop-entry state, which is misleading on inspection).
const nextInputsDecl =
  nextState.worker?.inputs ?? nextState.loop?.worker?.inputs ?? [];
const nextInputs = nextInputsDecl.reduce((acc, name) => {
  acc[name] = envWithCommit[name];
  return acc;
}, {});
writeEntryTrace(
  parsed.runId,
  { state: nextState, inputs: nextInputs },
  { storageRoot: settings.storageRoot },
);
updateManifest(
  parsed.runId,
  {
    current_state: nextState.id,
    next_state: null,
    transitions_count: (manifest.transitions_count ?? 0) + 1,
  },
  { storageRoot: settings.storageRoot },
);
const brief = buildBrief({
  doc: fsm.doc,
  state: nextState,
  env: envWithCommit,
  runId: parsed.runId,
  opts: { storageRoot: settings.storageRoot },
});
emit({ ok: true, advanced_from: state.id, ...brief });
process.exit(0);
