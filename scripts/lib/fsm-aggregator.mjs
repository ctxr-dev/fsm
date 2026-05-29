// fsm-aggregator.mjs: loop-state + cross-state output aggregators.
//
// Two aggregators live here:
//
// 1. aggregateLoopOutputs (A2): within a single loop state, reads every
//    iter-<N>.json under <run_dir>/workers/<state.loop.iteration_outputs_dir>/,
//    validates each against the loop worker's response_schema, concatenates
//    the per-iteration `findings[]` (or any configured mergeField) arrays,
//    and atomic-writes a single aggregated output file plus a sibling
//    iteration-meta file (strategy, ruled_out, rationale, done).
//
// 2. aggregateAcrossStates (A3): across multiple FSM states within one
//    run, reads each state's exit-trace `outputs[mergeField]` array,
//    concatenates them, and atomic-writes
//    <run_dir>/manifest-aggregates/<merge-field>.json. Returns the path,
//    the number of states actually contributing, the total merged length,
//    and a list of state ids that had no exit trace (missing_states[]).
//    This is the substrate a skill runner uses when its FSM produces
//    findings across several states (loop + verify, fan-out + collect,
//    etc.) and the final manifest should carry one unified findings list.
//
// All filesystem operations are atomic; re-running either aggregator on
// the same run_dir is idempotent. Neither aggregator mutates source
// files.
//
// Convention for loop states + cross-state aggregation: aggregateAcross-
// States reads `outputs[mergeField]` directly from each state's exit
// trace. A loop state's raw exit trace does NOT contain the flattened
// findings array (it contains an `aggregated_<state-id>` path produced
// by aggregateLoopOutputs). If a skill runner needs to fold a loop state
// into a cross-state aggregation it must, before calling
// aggregateAcrossStates, either (a) re-stamp the exit trace's outputs
// with the flattened items as `outputs[mergeField]`, or (b) emit a
// follow-on inline state whose outputs carry the flattened array. The
// exact wiring is deferred to the skill-level runner; this library
// intentionally does NOT auto-resolve aggregated_<state-id> paths to
// keep the cross-state contract a one-liner: "outputs[mergeField] must
// be an array".

import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { join } from "node:path";
import { parse as parseYaml } from "yaml";

import { sanitiseLoopOutputsDirSegment } from "./fsm-engine.mjs";
import { atomicWriteJson } from "./fsm-storage.mjs";
import { validateWorkerResponse } from "./fsm-schema.mjs";

// Returned relative paths must be POSIX (forward slash) so consumers on
// any platform see the same wire shape as the rest of the worker
// contract paths under workers/. join() uses os-native separators, which
// produces backslashes on Windows; use this helper for ANY path returned
// to the caller.
function posixRel(...segments) {
  return segments
    .filter((s) => typeof s === "string" && s.length > 0)
    .join("/")
    .replace(/\/{2,}/g, "/");
}

// aggregateLoopOutputs reads per-iteration JSON files for a loop state
// and produces a unified aggregated array. `runDir` is the absolute path
// to the run's directory (the parent of `workers/`). `state` is the loop
// state object from the FSM YAML (used for response_schema + iteration
// outputs_dir). `mergeField` is the top-level array field whose values
// are concatenated (default "findings"). Returns:
//   {
//     aggregated_path: "workers/<state-id>-aggregated.json",
//     iteration_meta_path: "workers/<state-id>-iteration-meta.json",
//     iteration_count: <n>,
//     merged_length: <n>,
//     validation_errors: [],
//   }
// On schema-violation in any iter file, the path appears in validation_errors
// but aggregation still proceeds with the remaining valid files.
export function aggregateLoopOutputs(runDir, state, { mergeField = "findings" } = {}) {
  if (!state?.loop) {
    throw new Error("aggregateLoopOutputs: state is not a loop state");
  }
  if (!runDir || typeof runDir !== "string") {
    throw new TypeError("aggregateLoopOutputs: runDir must be a non-empty string path");
  }
  if (typeof state.id !== "string" || state.id.length === 0) {
    throw new TypeError("aggregateLoopOutputs: state.id must be a non-empty string");
  }
  // Match the schema-layer constraint (snake_case lowercase + digits +
  // underscore). state.id feeds directly into output filenames; a
  // caller-supplied id like "../x" or "a/b" would either escape the
  // workers/ subdir or split into nested dirs that nothing else
  // expects. Validate once here so the public-surface call is safe.
  if (!/^[a-z][a-z0-9_]*$/.test(state.id)) {
    throw new TypeError(
      `aggregateLoopOutputs: state.id must be snake_case (lowercase letters, digits, underscores), got "${state.id}"`,
    );
  }
  if (typeof mergeField !== "string" || mergeField.length === 0) {
    throw new TypeError("aggregateLoopOutputs: mergeField must be a non-empty string");
  }
  const stateId = state.id;
  const loop = state.loop;
  // Sanitise the configured iteration_outputs_dir at runtime too: the
  // schema validates this at load time, but the aggregator is part of
  // the public surface and may be called with a state object whose
  // origin is not under our control. Reject leading "/" and ".." here
  // explicitly to prevent path traversal out of <runDir>/workers/.
  const iterSubdir = sanitiseLoopOutputsDirSegment(loop.iteration_outputs_dir, stateId);
  const iterDir = join(runDir, "workers", iterSubdir);
  const schema = loop.worker?.response_schema;

  const iters = [];
  const validationErrors = [];

  if (existsSync(iterDir)) {
    const entries = readdirSync(iterDir).filter((n) => /^iter-\d+\.json$/.test(n));
    const numbered = entries
      .map((name) => ({ name, n: Number.parseInt(name.match(/^iter-(\d+)\.json$/)[1], 10) }))
      .sort((a, b) => a.n - b.n);
    for (const { name, n } of numbered) {
      const filePath = join(iterDir, name);
      // Report paths relative to runDir with POSIX separators so the
      // aggregator never leaks absolute filesystem paths (which would
      // reveal local layout in logs) and matches the rest of the
      // worker-contract path style.
      const relPath = posixRel("workers", iterSubdir, name);
      let payload;
      try {
        payload = JSON.parse(readFileSync(filePath, "utf8"));
      } catch (err) {
        validationErrors.push({ path: relPath, error: `parse_error: ${err.message}` });
        continue;
      }
      if (schema) {
        const result = validateWorkerResponse(schema, payload);
        if (!result.valid) {
          validationErrors.push({ path: relPath, error: `schema: ${result.errors.join("; ")}` });
          continue;
        }
      }
      iters.push({ n, payload });
    }
  }

  const merged = [];
  const iterationMeta = [];
  for (const { n, payload } of iters) {
    const arr = Array.isArray(payload?.[mergeField]) ? payload[mergeField] : [];
    for (const item of arr) merged.push(item);
    // Use a null-prototype object so worker-controlled keys (e.g. a
    // payload that intentionally or accidentally carries `__proto__`,
    // `constructor`, or `prototype`) cannot mutate Object.prototype
    // when copied in below. JSON.stringify on a null-prototype object
    // serialises the same as a regular literal.
    const meta = Object.assign(Object.create(null), { iteration_n: n });
    for (const key of Object.keys(payload ?? {})) {
      if (key === mergeField) continue;
      // Defensive: skip the well-known prototype-pollution keys
      // explicitly even with the null-prototype object, since some
      // downstream consumers may later spread `meta` into a normal
      // object literal.
      if (key === "__proto__" || key === "constructor" || key === "prototype") {
        continue;
      }
      meta[key] = payload[key];
    }
    iterationMeta.push(meta);
  }

  // Returned relative paths use POSIX separators (the wire format the
  // worker contract is documented in). Filesystem writes go through
  // join() so they use OS-native separators locally.
  const aggregatedRel = posixRel("workers", `${stateId}-aggregated.json`);
  const metaRel = posixRel("workers", `${stateId}-iteration-meta.json`);
  mkdirSync(join(runDir, "workers"), { recursive: true });
  atomicWriteJson(join(runDir, "workers", `${stateId}-aggregated.json`), { state: stateId, merge_field: mergeField, items: merged });
  atomicWriteJson(join(runDir, "workers", `${stateId}-iteration-meta.json`), { state: stateId, iterations: iterationMeta });

  return {
    aggregated_path: aggregatedRel,
    iteration_meta_path: metaRel,
    iteration_count: iters.length,
    merged_length: merged.length,
    validation_errors: validationErrors,
  };
}

// aggregateAcrossStates concatenates `outputs[mergeField]` arrays from
// the exit-trace records of every state in `stateIds`, in the order the
// caller supplied (NOT the order traces were written). Writes the
// unified array to <run_dir>/manifest-aggregates/<merge-field>.json.
//
// Inputs:
//   runDir   absolute (or cwd-relative) path to the run directory.
//   stateIds array of FSM state ids to merge across, in order. Must be
//            a non-empty array of strings; duplicates are allowed (each
//            occurrence is merged once).
//   mergeField top-level array field on the exit-trace `outputs` object
//              to concatenate. Defaults to "findings".
//
// Returns:
//   {
//     aggregated_path: "manifest-aggregates/<merge-field>.json",
//     state_count:    <number of stateIds that contributed any value>,
//     merged_length:  <total length of the concatenated array>,
//     missing_states: [<state-id>, ...],   // had no exit trace in this run
//   }
//
// A state whose exit trace exists but whose `outputs[mergeField]` is
// missing or not an array is treated as contributing zero items (the
// state IS counted in state_count if its trace exists). Only the
// complete absence of an exit-phase trace lands the state in
// missing_states[].
//
// The output file is written atomically (write-tmp + fsync + rename),
// so a re-run on the same inputs produces the same bytes and there are
// no .tmp leftovers.
export function aggregateAcrossStates(runDir, { stateIds, mergeField = "findings" } = {}) {
  if (!runDir || typeof runDir !== "string") {
    throw new TypeError("aggregateAcrossStates: runDir must be a non-empty string path");
  }
  if (!Array.isArray(stateIds) || stateIds.length === 0) {
    throw new TypeError("aggregateAcrossStates: stateIds must be a non-empty array of state ids");
  }
  for (const id of stateIds) {
    if (typeof id !== "string" || id.length === 0) {
      throw new TypeError(`aggregateAcrossStates: stateIds entries must be non-empty strings, got ${JSON.stringify(id)}`);
    }
  }
  if (typeof mergeField !== "string" || mergeField.length === 0) {
    throw new TypeError("aggregateAcrossStates: mergeField must be a non-empty string");
  }

  const traceDir = join(runDir, "fsm-trace");

  // Build a one-pass map: state-id -> outputs of its (last) exit trace.
  // We walk the trace dir once; for the same state, a later exit
  // record overwrites the earlier one. This handles A4-style resume
  // semantics naturally (a re-entered state's most recent exit wins).
  const exitOutputsByState = new Map();
  if (existsSync(traceDir)) {
    const entries = readdirSync(traceDir)
      .filter((n) => /^\d+-exit-.*\.yaml$/.test(n))
      .sort(); // sequence prefix makes lexical sort = chronological.
    for (const name of entries) {
      const path = join(traceDir, name);
      let parsed;
      try {
        parsed = parseYaml(readFileSync(path, "utf8"));
      } catch {
        // Malformed trace file; skip rather than crash the aggregation.
        continue;
      }
      if (!parsed || parsed.phase !== "exit") continue;
      const st = parsed.state;
      if (typeof st !== "string") continue;
      exitOutputsByState.set(st, parsed.outputs ?? {});
    }
  }

  const merged = [];
  const missingStates = [];
  let stateCount = 0;
  for (const id of stateIds) {
    if (!exitOutputsByState.has(id)) {
      missingStates.push(id);
      continue;
    }
    stateCount += 1;
    const outputs = exitOutputsByState.get(id);
    const arr = outputs == null ? undefined : outputs[mergeField];
    if (Array.isArray(arr)) {
      for (const item of arr) merged.push(item);
    }
    // Non-array or missing: contributes zero items, but state is counted.
  }

  const aggregatesDir = join(runDir, "manifest-aggregates");
  mkdirSync(aggregatesDir, { recursive: true });
  const aggregatedRel = join("manifest-aggregates", `${mergeField}.json`);
  atomicWriteJson(join(runDir, aggregatedRel), {
    field: mergeField,
    from_states: [...stateIds],
    state_count: stateCount,
    missing_states: missingStates,
    merged_length: merged.length,
    items: merged,
  });

  return {
    aggregated_path: aggregatedRel,
    state_count: stateCount,
    merged_length: merged.length,
    missing_states: missingStates,
  };
}

// manifestAggregateEntry returns the canonical shape a skill runner
// should stamp into manifest.aggregates[<merge-field>] after calling
// aggregateAcrossStates. Keeping the shape in one place ensures every
// consumer writes the same fields with the same names.
//
// Skill runners typically call:
//
//   const agg = aggregateAcrossStates(runDir, { stateIds: [...], mergeField: "findings" });
//   const entry = manifestAggregateEntry({
//     fromStates: [...],
//     field: "findings",
//     aggregatedPath: agg.aggregated_path,
//     mergedLength: agg.merged_length,
//     missingStates: agg.missing_states,
//   });
//   updateManifest(runId, { aggregates: { ...(manifest.aggregates ?? {}), findings: entry } }, opts);
//
// The library does not wire this into fsm-commit automatically: the
// FSM YAML has no way to declare cross-state aggregates today, and
// adding one would be a schema change outside A3's scope. The wiring
// stays in skill-level runners; this helper keeps the shape consistent.
export function manifestAggregateEntry({ fromStates, field, aggregatedPath, mergedLength, missingStates = [] }) {
  if (!Array.isArray(fromStates) || fromStates.length === 0) {
    throw new TypeError("manifestAggregateEntry: fromStates must be a non-empty array");
  }
  if (typeof field !== "string" || field.length === 0) {
    throw new TypeError("manifestAggregateEntry: field must be a non-empty string");
  }
  if (typeof aggregatedPath !== "string" || aggregatedPath.length === 0) {
    throw new TypeError("manifestAggregateEntry: aggregatedPath must be a non-empty string");
  }
  if (typeof mergedLength !== "number" || mergedLength < 0 || !Number.isInteger(mergedLength)) {
    throw new TypeError("manifestAggregateEntry: mergedLength must be a non-negative integer");
  }
  return {
    from_states: [...fromStates],
    field,
    path: aggregatedPath,
    merged_length: mergedLength,
    missing_states: Array.isArray(missingStates) ? [...missingStates] : [],
  };
}
