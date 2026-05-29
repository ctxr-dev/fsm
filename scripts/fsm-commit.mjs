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
//
// A7 atomic-tx: every multi-file write sequence below runs inside
// withJournal(). On crash, the journal stays on disk and either
// fsm-resume --journal discard rolls back or --journal replay finalises.

import { readFileSync } from "node:fs";

import { emitJson } from "./lib/emit.mjs";
import {
  appendTraceFile,
  journalState,
  publicJournalProjection,
  readLock,
  readManifest,
  releaseLock,
  runDirPath,
  withJournal,
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
const runDir = runDirPath(parsed.runId, { storageRoot: settings.storageRoot });

// A7: a previous commit may have crashed between "ready_to_finalise" and
// the rename loop, leaving a journal on disk. Refuse to commit anything
// new until the user recovers (fsm-resume --journal discard|replay).
//
// journalState can throw if `.journal` exists but is unreadable (filesystem
// error, permission issue, or `.journal` is a regular file rather than a
// directory). Surface that as a structured payload instead of an
// unhandled stack trace so automation can react.
let existingJournal;
try {
  existingJournal = journalState(runDir);
} catch (err) {
  emit({
    error: "journal_inspect_failed",
    run_id: parsed.runId,
    detail: err.message,
  });
  process.exit(1);
}
if (existingJournal.hasJournal) {
  emit({
    error: "incomplete_commit_detected",
    run_id: parsed.runId,
    journal: publicJournalProjection(existingJournal),
    recovery: {
      discard: `fsm-resume --run-id ${parsed.runId} --journal discard --storage-root ${settings.storageRoot}`,
      replay: `fsm-resume --run-id ${parsed.runId} --journal replay --storage-root ${settings.storageRoot}`,
    },
  });
  process.exit(1);
}

// commitPlan holds the post-finalise actions decided inside the journal.
// Each branch sets one shape:
//   { kind: "loop_continued" } — re-emit current state's next-iter brief.
//   { kind: "fault", releaseLock: true, payload, exitCode: 1 } — schema
//        violation, post-validation failure, or no-transition fault.
//   { kind: "terminal", releaseLock: true, payload } — run completed.
//   { kind: "advance", nextStateId, envWithCommit } — advance to next.
let commitPlan;

try {
  withJournal(runDir, (txn) => {
    const txOpts = { storageRoot: settings.storageRoot, transaction: txn };

    const validationResult = validateOutputs(state, parsed.outputs);
    if (!validationResult.valid) {
      writeFaultTrace(
        parsed.runId,
        {
          state,
          reason: "output_schema_violation",
          details: validationResult.errors,
        },
        txOpts,
      );
      updateManifest(
        parsed.runId,
        { status: "faulted", ended_at: new Date().toISOString() },
        txOpts,
      );
      commitPlan = {
        kind: "fault",
        releaseLock: true,
        payload: {
          error: "output_schema_violation",
          state: state.id,
          errors: validationResult.errors,
        },
        exitCode: 1,
      };
      return;
    }

    // Loop-state branch: write the iter trace and either re-emit the
    // next loop brief (continue) or aggregate + fall through to the
    // regular exit-trace + transition path (terminate).
    let outputsForFlow = parsed.outputs;
    if (state.loop) {
      const iterationN =
        countLoopIterations(parsed.runId, state.id, txOpts) + 1;
      appendTraceFile(
        parsed.runId,
        {
          phase: "iter",
          state: state.id,
          data: { iteration_n: iterationN, outputs: parsed.outputs },
        },
        txOpts,
      );
      const decision = runLoopDecision(state, parsed.outputs, iterationN);
      if (!decision.terminate) {
        // Bump the manifest's last_update_at on every loop iteration so
        // run-health and stale-run signals still tick over while the
        // loop is making progress. current_state is reaffirmed
        // defensively (unchanged for a continuing loop, but keeps the
        // patch self-explanatory and survives manifest schema drift).
        updateManifest(
          parsed.runId,
          { current_state: state.id },
          txOpts,
        );
        commitPlan = { kind: "loop_continued" };
        return;
      }
      // Terminating: aggregate iter outputs into one canonical record
      // and use that as the loop state's outputs for the rest of the
      // commit flow. The aggregator writes through the journal so the
      // aggregated.json + iteration-meta.json land in the same atomic
      // step as the exit trace.
      const agg = aggregateLoopOutputs(runDir, state, {
        mergeField: "findings",
        transaction: txn,
      });
      outputsForFlow = {
        [`aggregated_${state.id}`]: agg.aggregated_path,
        iteration_meta_path: agg.iteration_meta_path,
        // total_iterations is the COMMITTED iteration count (taken from
        // iterationN, the trace-driven counter), NOT agg.iteration_count
        // which only counts iter-N.json files the aggregator could
        // parse and schema-validate. Otherwise tolerated invalid iters
        // would make the manifest's iteration count disagree with
        // max_iterations bookkeeping and the trace.
        total_iterations: iterationN,
        aggregated_iteration_count: agg.iteration_count,
        aggregator_validation_errors: agg.validation_errors,
        merged_length: agg.merged_length,
        terminated_by: decision.reason,
      };
    }

    // Post-validations run against the freshly-committed outputs. For
    // a terminating loop state, outputsForFlow is the aggregated record
    // (the canonical post-loop output), so predicates that need to
    // inspect the aggregate (`total_iterations`, etc.) see them; for a
    // non-loop state outputsForFlow === parsed.outputs.
    const postValidations = runPostValidations(state, outputsForFlow);
    if (!postValidations.valid) {
      writeFaultTrace(
        parsed.runId,
        {
          state,
          reason: "post_validation_failed",
          details: { post_validations: postValidations.results },
        },
        txOpts,
      );
      updateManifest(
        parsed.runId,
        { status: "faulted", ended_at: new Date().toISOString() },
        txOpts,
      );
      commitPlan = {
        kind: "fault",
        releaseLock: true,
        payload: {
          error: "post_validation_failed",
          state: state.id,
          post_validations: postValidations.results,
        },
        exitCode: 1,
      };
      return;
    }

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
      txOpts,
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
          txOpts,
        );
        updateManifest(
          parsed.runId,
          { status: "faulted", ended_at: new Date().toISOString() },
          txOpts,
        );
        commitPlan = {
          kind: "fault",
          releaseLock: true,
          payload: {
            error: "no_transition_matched",
            state: state.id,
            evaluations,
          },
          exitCode: 1,
        };
        return;
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
        txOpts,
      );
      commitPlan = {
        kind: "terminal",
        releaseLock: true,
        payload: {
          ok: true,
          status: "terminal",
          state: state.id,
          verdict: envWithCommit.verdict ?? null,
          run_dir_path: runDir,
        },
      };
      return;
    }

    const nextState = stateById(fsm.doc, transition.to);
    // Loop states declare their worker under state.loop.worker; falling
    // back here keeps the entry trace's recorded inputs consistent with
    // what buildBrief will pass to the worker dispatch.
    const nextInputsDecl =
      nextState.worker?.inputs ?? nextState.loop?.worker?.inputs ?? [];
    const nextInputs = nextInputsDecl.reduce((acc, name) => {
      acc[name] = envWithCommit[name];
      return acc;
    }, {});
    writeEntryTrace(
      parsed.runId,
      { state: nextState, inputs: nextInputs },
      txOpts,
    );
    updateManifest(
      parsed.runId,
      {
        current_state: nextState.id,
        next_state: null,
        transitions_count: (manifest.transitions_count ?? 0) + 1,
      },
      txOpts,
    );
    commitPlan = {
      kind: "advance",
      nextStateId: nextState.id,
      envWithCommit,
      advancedFrom: state.id,
    };
  });
} catch (err) {
  // If withJournal itself refused (e.g. a journal raced into existence
  // between our pre-check and the start of the txn) surface it as a
  // structured error rather than a stack trace.
  if (err && err.code === "JOURNAL_PRESENT") {
    // Same stable shape as the pre-check above — the CLI error payload
    // must not vary based on which code path detected the journal.
    emit({
      error: "incomplete_commit_detected",
      run_id: parsed.runId,
      journal: publicJournalProjection(err.journal),
      recovery: {
        discard: `fsm-resume --run-id ${parsed.runId} --journal discard --storage-root ${settings.storageRoot}`,
        replay: `fsm-resume --run-id ${parsed.runId} --journal replay --storage-root ${settings.storageRoot}`,
      },
    });
    process.exit(1);
  }
  fail(err.message, 1);
}

// All journal writes have finalised. The post-finalise phase reads from
// disk to compute emit-only payloads (briefs, etc.) and optionally
// releases the lock. None of the work below is required for crash
// recovery: the manifest already records the new state.

if (commitPlan.releaseLock) {
  releaseLock(parsed.runId, {
    sessionId: settings.sessionId,
    storageRoot: settings.storageRoot,
  });
}

if (commitPlan.kind === "fault" || commitPlan.kind === "terminal") {
  emit(commitPlan.payload);
  process.exit(commitPlan.exitCode ?? 0);
}

if (commitPlan.kind === "loop_continued") {
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

// kind === "advance"
const nextState = stateById(fsm.doc, commitPlan.nextStateId);
const brief = buildBrief({
  doc: fsm.doc,
  state: nextState,
  env: commitPlan.envWithCommit,
  runId: parsed.runId,
  opts: { storageRoot: settings.storageRoot },
});
emit({ ok: true, advanced_from: commitPlan.advancedFrom, ...brief });
process.exit(0);
