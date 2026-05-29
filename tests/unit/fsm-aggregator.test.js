// fsm-aggregator.test.js — unit coverage for the loop-aware aggregator
// (A2). Aggregator reads iter-N.json files from a run's workers/ subdir,
// validates each against the loop worker's response_schema, concatenates
// the mergeField (default "findings"), and writes a unified aggregated
// JSON plus a per-iteration meta JSON.

import { test } from "node:test";
import assert from "node:assert/strict";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { aggregateLoopOutputs } from "../../scripts/lib/fsm-aggregator.mjs";

function makeRunDir() {
  return mkdtempSync(join(tmpdir(), "fsm-agg-rd-"));
}

function loopState({ outputsDir = "explore-iters/" } = {}) {
  return {
    id: "explore",
    loop: {
      worker: {
        role: "explorer",
        prompt_template: "t.md",
        inputs: [],
        response_schema: {
          type: "object",
          required: ["done", "findings"],
          properties: {
            done: { type: "boolean" },
            findings: { type: "array" },
            strategy: { type: "string" },
          },
        },
      },
      max_iterations: 10,
      done_field: "done",
      iteration_outputs_dir: outputsDir,
    },
  };
}

function writeIter(runDir, outputsDir, n, payload) {
  const dir = join(runDir, "workers", outputsDir.replace(/\/$/, ""));
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `iter-${n}.json`), JSON.stringify(payload));
}

test("aggregateLoopOutputs: empty iter dir produces empty merged array", () => {
  const runDir = makeRunDir();
  try {
    const state = loopState();
    const result = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
    assert.equal(result.iteration_count, 0);
    assert.equal(result.merged_length, 0);
    const aggregated = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.deepEqual(aggregated.items, []);
    assert.equal(aggregated.state, "explore");
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: N iters concatenate findings in sequence order", () => {
  const runDir = makeRunDir();
  try {
    const state = loopState();
    writeIter(runDir, "explore-iters/", 1, {
      done: false,
      findings: [{ a: 1 }],
      strategy: "symbol",
    });
    writeIter(runDir, "explore-iters/", 2, {
      done: false,
      findings: [{ a: 2 }, { a: 3 }],
      strategy: "path",
    });
    writeIter(runDir, "explore-iters/", 3, {
      done: true,
      findings: [{ a: 4 }],
      strategy: "doc",
    });
    const result = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
    assert.equal(result.iteration_count, 3);
    assert.equal(result.merged_length, 4);
    const aggregated = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.deepEqual(
      aggregated.items.map((i) => i.a),
      [1, 2, 3, 4],
    );
    const meta = JSON.parse(readFileSync(join(runDir, result.iteration_meta_path), "utf8"));
    assert.equal(meta.iterations.length, 3);
    assert.equal(meta.iterations[0].strategy, "symbol");
    assert.equal(meta.iterations[2].done, true);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: idempotent re-run produces identical result", () => {
  const runDir = makeRunDir();
  try {
    const state = loopState();
    writeIter(runDir, "explore-iters/", 1, { done: true, findings: [{ x: 1 }] });
    const first = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
    const firstBody = readFileSync(join(runDir, first.aggregated_path), "utf8");
    const second = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
    const secondBody = readFileSync(join(runDir, second.aggregated_path), "utf8");
    assert.equal(first.merged_length, second.merged_length);
    assert.equal(firstBody, secondBody);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: skips schema-invalid iters and records the error", () => {
  const runDir = makeRunDir();
  try {
    const state = loopState();
    writeIter(runDir, "explore-iters/", 1, { done: false, findings: [{ a: 1 }] });
    writeIter(runDir, "explore-iters/", 2, { findings: [{ a: 2 }] }); // missing done
    writeIter(runDir, "explore-iters/", 3, { done: true, findings: [{ a: 3 }] });
    const result = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
    assert.equal(result.iteration_count, 2); // iter-2 dropped
    assert.equal(result.merged_length, 2);
    assert.equal(result.validation_errors.length, 1);
    assert.match(result.validation_errors[0].error, /schema/);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: records parse errors on malformed JSON", () => {
  const runDir = makeRunDir();
  try {
    const state = loopState();
    const dir = join(runDir, "workers", "explore-iters");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "iter-1.json"), "not json");
    const result = aggregateLoopOutputs(runDir, state, { mergeField: "findings" });
    assert.equal(result.iteration_count, 0);
    assert.equal(result.validation_errors.length, 1);
    assert.match(result.validation_errors[0].error, /parse_error/);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: throws on non-loop state input", () => {
  const runDir = makeRunDir();
  try {
    assert.throws(
      () => aggregateLoopOutputs(runDir, { id: "x", worker: {} }, {}),
      /not a loop state/,
    );
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: honours default outputs_dir when state omits it", () => {
  const runDir = makeRunDir();
  try {
    const state = {
      id: "explore",
      loop: {
        worker: {
          role: "r",
          prompt_template: "t",
          inputs: [],
          response_schema: {
            type: "object",
            required: ["done"],
            properties: { done: { type: "boolean" }, findings: { type: "array" } },
          },
        },
        max_iterations: 5,
        done_field: "done",
      },
    };
    writeIter(runDir, "explore-iters/", 1, { done: true, findings: [{ k: 1 }] });
    const result = aggregateLoopOutputs(runDir, state, {});
    assert.equal(result.merged_length, 1);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateLoopOutputs: writes aggregated and meta files atomically (no tmp leftovers)", () => {
  const runDir = makeRunDir();
  try {
    const state = loopState();
    writeIter(runDir, "explore-iters/", 1, { done: true, findings: [{ q: 1 }] });
    const result = aggregateLoopOutputs(runDir, state, {});
    assert.ok(existsSync(join(runDir, result.aggregated_path)));
    assert.ok(existsSync(join(runDir, result.iteration_meta_path)));
    const workersDir = join(runDir, "workers");
    const tmpLeftovers = readdirSync(workersDir).filter(
      (n) => n.endsWith(".tmp") || n.endsWith(".tmp~"),
    );
    assert.equal(tmpLeftovers.length, 0);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});
