// fsm-engine.mjs — shared engine logic used by fsm-next + fsm-commit.
//
// Reads a parsed FSM YAML, builds the run-state environment from disk traces,
// resolves transitions, and produces the next-state brief that the orchestrator
// consumes via stdout.
//
// All filesystem-bound helpers take `storageRoot` (the storage directory).
// All FSM-loading helpers take `fsmPath` (the YAML file path). No defaults
// for either — consumers pass them explicitly via the CLIs.

import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { parse as parseYaml } from "yaml";

import { evaluatePredicate } from "./fsm-predicates.mjs";
import {
  hashFsmYaml,
  validateFsmSchema,
  validateFsmStatic,
  validateWorkerResponse,
} from "./fsm-schema.mjs";
import {
  appendTraceFile,
  readManifest,
  readTrace,
  writeManifest,
} from "./fsm-storage.mjs";

// loadFsm returns the parsed FSM document and its hash. Throws on parse or
// validation failure.
export function loadFsm({ fsmPath } = {}) {
  if (!fsmPath) {
    throw new Error("loadFsm: fsmPath is required");
  }
  const path = resolve(fsmPath);
  if (!existsSync(path)) {
    throw new Error(`loadFsm: FSM YAML not found at ${path}`);
  }
  const doc = parseYaml(readFileSync(path, "utf8"));
  const schemaResult = validateFsmSchema(doc);
  if (!schemaResult.valid) {
    throw new Error(
      `loadFsm: FSM YAML failed structural validation: ${schemaResult.errors.join("; ")}`,
    );
  }
  const staticResult = validateFsmStatic(doc, { fsmFilePath: path });
  if (!staticResult.valid) {
    throw new Error(
      `loadFsm: FSM YAML failed static validation: ${staticResult.errors.join("; ")}`,
    );
  }
  return { doc, hash: hashFsmYaml(path), path };
}

// stateById returns the state object for the given id or throws.
export function stateById(doc, id) {
  const state = doc.fsm.states.find((s) => s.id === id);
  if (!state) {
    throw new Error(`stateById: no state with id "${id}" in FSM "${doc.fsm.id}"`);
  }
  return state;
}

// runEnv reads all exit trace records for a run and produces the cumulative
// environment of outputs collected so far. Pulls args from the entry trace
// of the entry state if present.
export function runEnv(runId, opts = {}) {
  const trace = readTrace(runId, opts);
  const env = {};
  for (const record of trace) {
    if (record.data?.phase !== "exit") continue;
    const outputs = record.data?.outputs;
    if (outputs && typeof outputs === "object") {
      Object.assign(env, outputs);
    }
  }
  for (const record of trace) {
    if (record.data?.phase === "entry" && record.data?.inputs?.args) {
      env.args = record.data.inputs.args;
      break;
    }
  }
  return env;
}

// buildBrief returns the JSON object that the orchestrator consumes.
// Includes the state spec + resolved inputs + transitions + the worker
// response_schema if present. The orchestrator never reads the FSM YAML;
// the brief is the only contract.
//
// For loop states (state.loop present), the brief carries a `loop` section
// with the current iteration counter and the outputs_path the worker must
// write to. The iteration counter is derived from existing "iter" trace
// records for this state in this run; pass runId+opts so we can read the
// trace from disk. Callers that don't supply opts (no storageRoot) get
// iteration_n = 1 as a safe default for non-resume flows where the count
// is already known by the caller.
export function buildBrief({ doc, state, env, runId, opts = {} }) {
  if (state.loop) {
    return buildLoopBrief({ doc, state, env, runId, opts });
  }
  const brief = {
    run_id: runId,
    fsm_id: doc.fsm.id,
    state: state.id,
    purpose: state.purpose,
    preconditions: state.preconditions ?? [],
    inputs: resolveInputs(state, env),
    outputs_expected: state.outputs ?? [],
    post_validations: state.post_validations ?? [],
    transitions: (state.transitions ?? []).map((t) => ({ to: t.to, when: t.when })),
    has_worker: Boolean(state.worker),
    has_loop: false,
  };
  if (state.worker) {
    brief.worker = {
      role: state.worker.role,
      prompt_template: state.worker.prompt_template,
      inputs: state.worker.inputs ?? [],
      response_schema: state.worker.response_schema,
    };
  }
  return brief;
}

// sanitiseLoopOutputsDirSegment normalises a loop state's
// iteration_outputs_dir into a single relative subpath under workers/.
// Schema validation rejects "/"-leading and ".." segments at FSM load
// time, but the engine is also called from contexts that may not have
// re-validated the doc (tests, dynamic FSMs). Re-running the checks here
// keeps the runtime defensive: a malicious or stale state cannot escape
// <run_dir>/workers/ via path traversal.
export function sanitiseLoopOutputsDirSegment(raw, fallbackStateId) {
  const candidate = raw && typeof raw === "string" && raw.length > 0
    ? raw
    : `${fallbackStateId}-iters/`;
  // Backslashes are a path separator on Windows where `path.join`
  // interprets them as such; allowing them through the POSIX-split
  // check below would let `foo\..\bar` traverse out of workers/ on a
  // Windows host. Reject the whole value up-front so the guard is
  // platform-portable.
  if (candidate.includes("\\")) {
    throw new Error(
      `sanitiseLoopOutputsDirSegment: iteration_outputs_dir "${raw}" must not contain backslashes (use forward slashes)`,
    );
  }
  // Reject leading "/" AND Windows drive-letter / colon-bearing
  // segments. `path.join("workers", "C:/abs")` produces "workers/C:/abs"
  // on POSIX but ends up absolute under path.win32.join, so the guard
  // has to cover both. Disallow any ":" anywhere in the segment to keep
  // the rule portable (a legitimate ":" inside a directory name is
  // exotic and not worth supporting at the cost of the safety bound).
  if (candidate.startsWith("/")) {
    throw new Error(
      `sanitiseLoopOutputsDirSegment: iteration_outputs_dir "${raw}" must be relative; absolute paths are not allowed`,
    );
  }
  if (/^[A-Za-z]:[\\/]/.test(candidate) || candidate.includes(":")) {
    throw new Error(
      `sanitiseLoopOutputsDirSegment: iteration_outputs_dir "${raw}" must not contain ":" (Windows drive letters or colon segments could escape workers/)`,
    );
  }
  const trimmed = candidate.replace(/\/+$/, "");
  if (trimmed.length === 0) {
    throw new Error(
      "sanitiseLoopOutputsDirSegment: iteration_outputs_dir collapses to empty after trimming slashes",
    );
  }
  const parts = trimmed.split("/");
  for (const part of parts) {
    if (part === "" || part === "." || part === "..") {
      throw new Error(
        `sanitiseLoopOutputsDirSegment: iteration_outputs_dir "${raw}" contains an invalid segment ("${part}")`,
      );
    }
  }
  return trimmed;
}

// buildLoopBrief composes the per-iteration brief for a loop state. The
// orchestrator dispatches the worker with `worker.prompt_template` and is
// expected to write the JSON output to `loop.outputs_path`. Subsequent
// fsm-commit + fsm-next calls advance the iteration counter or terminate.
export function buildLoopBrief({ doc, state, env, runId, opts = {} }) {
  const loop = state.loop;
  const max = loop.max_iterations ?? 30;
  const dirSegment = sanitiseLoopOutputsDirSegment(loop.iteration_outputs_dir, state.id);
  const iterationN = countLoopIterations(runId, state.id, opts) + 1;
  // outputs_path is part of the worker contract and is consumed across
  // platforms; always emit POSIX forward slashes regardless of host OS.
  const outputsPath = `workers/${dirSegment}/iter-${iterationN}.json`;
  const fakeStateForInputs = { worker: { inputs: loop.worker.inputs ?? [] } };
  return {
    run_id: runId,
    fsm_id: doc.fsm.id,
    state: state.id,
    purpose: state.purpose,
    preconditions: state.preconditions ?? [],
    inputs: resolveInputs(fakeStateForInputs, env),
    outputs_expected: state.outputs ?? [],
    post_validations: state.post_validations ?? [],
    transitions: (state.transitions ?? []).map((t) => ({ to: t.to, when: t.when })),
    has_worker: true,
    has_loop: true,
    loop: {
      iteration_n: iterationN,
      max_iterations: max,
      done_field: loop.done_field,
      outputs_path: outputsPath,
    },
    worker: {
      role: loop.worker.role,
      prompt_template: loop.worker.prompt_template,
      inputs: loop.worker.inputs ?? [],
      response_schema: loop.worker.response_schema,
    },
  };
}

// countLoopIterations returns the number of "iter" phase trace records
// recorded so far for the given state. Returns 0 when the run dir has no
// trace yet (or no opts.storageRoot is supplied, the test-fixture case).
// Real readTrace failures (corruption, EIO, parse errors) MUST propagate
// so buildLoopBrief cannot silently reset to iter-1 and overwrite an
// existing iteration output file.
export function countLoopIterations(runId, stateId, opts = {}) {
  if (!opts || typeof opts.storageRoot !== "string" || opts.storageRoot.length === 0) {
    return 0;
  }
  const trace = readTrace(runId, opts);
  let n = 0;
  for (const record of trace) {
    const payload = record.data ?? record;
    if (payload?.phase === "iter" && payload?.state === stateId) {
      n++;
    }
  }
  return n;
}

// runLoopDecision decides whether a loop state should terminate given the
// just-committed iteration output. Returns:
//   { isLoop: false } for non-loop states.
//   { isLoop: true, terminate: true, reason: "done_field" | "max_iterations", iteration_n }
//   { isLoop: true, terminate: false, iteration_n }
export function runLoopDecision(state, outputs, iterationN) {
  const loop = state.loop;
  if (!loop) return { isLoop: false };
  const max = loop.max_iterations ?? 30;
  const doneField = loop.done_field;
  const done = Boolean(outputs?.[doneField]);
  if (done) {
    return { isLoop: true, terminate: true, reason: "done_field", iteration_n: iterationN };
  }
  if (iterationN >= max) {
    return { isLoop: true, terminate: true, reason: "max_iterations", iteration_n: iterationN };
  }
  return { isLoop: true, terminate: false, iteration_n: iterationN };
}

function resolveInputs(state, env) {
  const declared = state.worker?.inputs ?? [];
  const out = {};
  for (const name of declared) {
    out[name] = env[name];
  }
  return out;
}

// resolveTransition picks the first transition whose predicate evaluates
// true against the env. Supports:
//   when: "always"           — unconditional
//   when: "otherwise"        — true iff no earlier transition matched
//   when: { kind: "deterministic", expression: "..." }
//   when: { kind: "judgement", criteria: "..." } — caller supplies
//                              `judgementPick` (a target state id)
//   when: { kind: "always" } — unconditional
//
// Returns { transition, evaluations[] } where evaluations records per-
// transition results for the trace.
export function resolveTransition(state, env, { judgementPick } = {}) {
  const transitions = state.transitions ?? [];
  const evaluations = [];
  let firstMatch = null;
  let matchedAny = false;
  for (const t of transitions) {
    const evalRecord = { to: t.to, when: t.when };
    if (t.when === "always" || t.when?.kind === "always") {
      evalRecord.result = true;
      evaluations.push(evalRecord);
      if (!firstMatch) firstMatch = t;
      matchedAny = true;
      continue;
    }
    if (t.when === "otherwise") {
      const result = !matchedAny;
      evalRecord.result = result;
      evaluations.push(evalRecord);
      if (result && !firstMatch) firstMatch = t;
      continue;
    }
    if (t.when?.kind === "deterministic") {
      try {
        const result = evaluatePredicate(t.when.expression, env);
        evalRecord.result = result;
        evalRecord.expression = t.when.expression;
        evaluations.push(evalRecord);
        if (result && !firstMatch) firstMatch = t;
        if (result) matchedAny = true;
      } catch (err) {
        evalRecord.result = false;
        evalRecord.error = err.message;
        evaluations.push(evalRecord);
      }
      continue;
    }
    if (t.when?.kind === "judgement") {
      const picked = judgementPick === t.to;
      evalRecord.kind = "judgement";
      evalRecord.criteria = t.when.criteria;
      evalRecord.result = picked;
      evaluations.push(evalRecord);
      if (picked && !firstMatch) firstMatch = t;
      if (picked) matchedAny = true;
      continue;
    }
    evalRecord.result = false;
    evalRecord.error = `unsupported when shape: ${JSON.stringify(t.when)}`;
    evaluations.push(evalRecord);
  }
  return { transition: firstMatch, evaluations };
}

// runPostValidations is a stub for v0.1 — the post_validations array is
// declarative documentation today. v0.2 can wire predicate evaluation
// here. Returns { valid, results[] } for trace recording.
export function runPostValidations(state) {
  const results = (state.post_validations ?? []).map((check) => ({
    check,
    result: "skipped",
    note: "post_validations are declarative in v0.1; runtime evaluation deferred",
  }));
  return { valid: true, results };
}

// validateOutputs runs the worker.response_schema (if any) over the
// supplied payload. Returns { valid, errors[] }. Inline states (no worker)
// skip schema validation and return valid=true. For loop states the
// schema lives at state.loop.worker.response_schema.
export function validateOutputs(state, outputs) {
  const schema = state.loop?.worker?.response_schema ?? state.worker?.response_schema;
  if (!schema) {
    return { valid: true, errors: [] };
  }
  return validateWorkerResponse(schema, outputs);
}

// initialiseManifest writes the very first manifest.json for a new run.
export function initialiseManifest({
  runId,
  fsmDoc,
  fsmHash,
  args,
  repo,
  baseSha,
  headSha,
  now = new Date(),
  storageRoot,
}) {
  const data = {
    run_id: runId,
    parent_run_id: null,
    forked_from: null,
    fsm_id: fsmDoc.fsm.id,
    fsm_yaml_hash: fsmHash,
    fsm_yaml_version: fsmDoc.fsm.version,
    status: "in_progress",
    current_state: null,
    next_state: fsmDoc.fsm.entry,
    started_at: now.toISOString(),
    last_update_at: now.toISOString(),
    ended_at: null,
    paused_at: null,
    pause_reason: null,
    abandoned_at: null,
    abandon_reason: null,
    repo: repo ?? null,
    base_sha: baseSha ?? null,
    head_sha: headSha ?? null,
    args: args ?? {},
    verdict: null,
    transitions_count: 0,
  };
  writeManifest(runId, data, { storageRoot });
  return data;
}

export function updateManifest(runId, patch, { storageRoot, now = new Date() } = {}) {
  const existing = readManifest(runId, { storageRoot });
  if (!existing) {
    throw new Error(`updateManifest: no manifest at run-id "${runId}"`);
  }
  const updated = {
    ...existing,
    ...patch,
    last_update_at: now.toISOString(),
  };
  writeManifest(runId, updated, { storageRoot });
  return updated;
}

export function writeEntryTrace(runId, { state, inputs, preconditionsResult }, opts = {}) {
  return appendTraceFile(
    runId,
    {
      phase: "entry",
      state: state.id,
      data: {
        purpose: state.purpose,
        preconditions: preconditionsResult ?? state.preconditions ?? [],
        inputs: inputs ?? {},
      },
    },
    opts,
  );
}

export function writeExitTrace(runId, { state, outputs, postValidations, transitionEvals, chosenTransition }, opts = {}) {
  return appendTraceFile(
    runId,
    {
      phase: "exit",
      state: state.id,
      data: {
        outputs: outputs ?? {},
        post_validations: postValidations ?? [],
        transition_evaluation: transitionEvals ?? [],
        transition: chosenTransition ?? null,
      },
    },
    opts,
  );
}

export function writeFaultTrace(runId, { state, reason, details }, opts = {}) {
  return appendTraceFile(
    runId,
    {
      phase: "fault",
      state: state.id,
      data: {
        reason,
        details: details ?? null,
      },
    },
    opts,
  );
}
