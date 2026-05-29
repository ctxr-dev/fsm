// fsm-aggregator.mjs — loop-state output aggregator.
//
// Loop states write per-iteration JSON outputs to
// <run_dir>/workers/<state.loop.iteration_outputs_dir>/iter-<N>.json.
// When the loop terminates, this helper reads every iter-*.json file in
// sequence order, validates each against the loop worker's response_schema,
// concatenates the per-iteration `findings[]` (or any configured mergeField)
// arrays, and atomic-writes a single aggregated output file under
// <run_dir>/workers/. A sibling meta file captures per-iteration metadata
// (strategy, ruled_out, rationale, done) so the manifest can summarise the
// loop without re-reading every iter file.
//
// All filesystem operations are atomic; re-running the aggregator on the
// same run_dir is idempotent. The aggregator never mutates iter files.

import {
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
} from "node:fs";
import { join } from "node:path";

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
    const meta = { iteration_n: n };
    for (const key of Object.keys(payload ?? {})) {
      if (key === mergeField) continue;
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
