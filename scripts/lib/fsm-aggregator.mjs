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

import { atomicWriteJson } from "./fsm-storage.mjs";
import { validateWorkerResponse } from "./fsm-schema.mjs";

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
  const stateId = state.id;
  const loop = state.loop;
  const iterSubdir = (loop.iteration_outputs_dir ?? `${stateId}-iters/`).replace(/^\/+/, "").replace(/\/+$/, "");
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
      let payload;
      try {
        payload = JSON.parse(readFileSync(filePath, "utf8"));
      } catch (err) {
        validationErrors.push({ path: filePath, error: `parse_error: ${err.message}` });
        continue;
      }
      if (schema) {
        const result = validateWorkerResponse(schema, payload);
        if (!result.valid) {
          validationErrors.push({ path: filePath, error: `schema: ${result.errors.join("; ")}` });
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

  const aggregatedRel = join("workers", `${stateId}-aggregated.json`);
  const metaRel = join("workers", `${stateId}-iteration-meta.json`);
  mkdirSync(join(runDir, "workers"), { recursive: true });
  atomicWriteJson(join(runDir, aggregatedRel), { state: stateId, merge_field: mergeField, items: merged });
  atomicWriteJson(join(runDir, metaRel), { state: stateId, iterations: iterationMeta });

  return {
    aggregated_path: aggregatedRel,
    iteration_meta_path: metaRel,
    iteration_count: iters.length,
    merged_length: merged.length,
    validation_errors: validationErrors,
  };
}
