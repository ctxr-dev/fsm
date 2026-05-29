// fsm-loop.test.js — unit coverage for the loop-state primitive runtime
// helpers added in A1: buildLoopBrief, countLoopIterations,
// runLoopDecision, plus validateOutputs delegation to loop.worker.

import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  buildBrief,
  buildLoopBrief,
  countLoopIterations,
  runLoopDecision,
  validateOutputs,
} from "../../scripts/lib/fsm-engine.mjs";
import {
  appendTraceFile,
  buildRunId,
  ensureRunDir,
} from "../../scripts/lib/fsm-storage.mjs";

function tmpStore() {
  return mkdtempSync(join(tmpdir(), "fsm-loop-store-"));
}

function loopFsm() {
  return {
    fsm: {
      id: "loop-test",
      version: 1,
      entry: "explore",
      states: [
        {
          id: "explore",
          purpose: "loop test",
          preconditions: [],
          loop: {
            worker: {
              role: "explorer",
              prompt_template: "fsm/workers/x.md",
              inputs: ["args"],
              response_schema: {
                type: "object",
                required: ["done", "findings"],
                properties: {
                  done: { type: "boolean" },
                  findings: { type: "array" },
                },
              },
            },
            max_iterations: 3,
            done_field: "done",
            iteration_outputs_dir: "explore-iters/",
          },
          outputs: ["aggregated_explore"],
          post_validations: [],
          transitions: [{ to: "terminal", when: { kind: "always" } }],
        },
        {
          id: "terminal",
          purpose: "end",
          preconditions: [],
          outputs: [],
          transitions: [],
        },
      ],
    },
  };
}

// ─── runLoopDecision ────────────────────────────────────────────────────

test("runLoopDecision: non-loop state returns isLoop=false", () => {
  const result = runLoopDecision({ id: "x", worker: {} }, { done: true }, 1);
  assert.equal(result.isLoop, false);
});

test("runLoopDecision: terminates on done_field=true", () => {
  const state = loopFsm().fsm.states[0];
  const result = runLoopDecision(state, { done: true, findings: [] }, 1);
  assert.equal(result.isLoop, true);
  assert.equal(result.terminate, true);
  assert.equal(result.reason, "done_field");
  assert.equal(result.iteration_n, 1);
});

test("runLoopDecision: terminates on max_iterations cap", () => {
  const state = loopFsm().fsm.states[0];
  const result = runLoopDecision(state, { done: false, findings: [] }, 3);
  assert.equal(result.terminate, true);
  assert.equal(result.reason, "max_iterations");
});

test("runLoopDecision: continues otherwise", () => {
  const state = loopFsm().fsm.states[0];
  const result = runLoopDecision(state, { done: false, findings: [] }, 2);
  assert.equal(result.terminate, false);
  assert.equal(result.iteration_n, 2);
});

test("runLoopDecision: falsy done values continue", () => {
  const state = loopFsm().fsm.states[0];
  for (const v of [false, 0, null, undefined, ""]) {
    const r = runLoopDecision(state, { done: v, findings: [] }, 1);
    assert.equal(r.terminate, false, `expected continue for done=${JSON.stringify(v)}`);
  }
});

test("runLoopDecision: default max_iterations is 30 when omitted", () => {
  const state = { id: "s", loop: { worker: {}, done_field: "done" } };
  const r29 = runLoopDecision(state, { done: false }, 29);
  assert.equal(r29.terminate, false);
  const r30 = runLoopDecision(state, { done: false }, 30);
  assert.equal(r30.terminate, true);
  assert.equal(r30.reason, "max_iterations");
});

// ─── countLoopIterations ───────────────────────────────────────────────

test("countLoopIterations: returns 0 when no trace dir exists", () => {
  const store = tmpStore();
  try {
    const n = countLoopIterations("20260426-143045-1234567", "explore", { storageRoot: store });
    assert.equal(n, 0);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("countLoopIterations: counts iter records for the named state only", () => {
  const store = tmpStore();
  const runId = buildRunId({ repo: "ctxr-dev/fsm", baseSha: "base", headSha: "head" }).runId;
  try {
    ensureRunDir(runId, { storageRoot: store });
    appendTraceFile(runId, { phase: "entry", state: "explore", data: {} }, { storageRoot: store });
    appendTraceFile(runId, { phase: "iter", state: "explore", data: { iteration_n: 1, outputs: {} } }, { storageRoot: store });
    appendTraceFile(runId, { phase: "iter", state: "explore", data: { iteration_n: 2, outputs: {} } }, { storageRoot: store });
    appendTraceFile(runId, { phase: "iter", state: "other", data: { iteration_n: 1, outputs: {} } }, { storageRoot: store });
    assert.equal(countLoopIterations(runId, "explore", { storageRoot: store }), 2);
    assert.equal(countLoopIterations(runId, "other", { storageRoot: store }), 1);
    assert.equal(countLoopIterations(runId, "absent", { storageRoot: store }), 0);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

// ─── buildLoopBrief / buildBrief delegation ─────────────────────────────

test("buildLoopBrief: emits has_loop=true with iteration_n=1 on first call", () => {
  const store = tmpStore();
  const runId = buildRunId({ repo: "ctxr-dev/fsm", baseSha: "base", headSha: "head" }).runId;
  try {
    ensureRunDir(runId, { storageRoot: store });
    const state = loopFsm().fsm.states[0];
    const brief = buildLoopBrief({
      doc: loopFsm(),
      state,
      env: { args: { hello: "world" } },
      runId,
      opts: { storageRoot: store },
    });
    assert.equal(brief.has_loop, true);
    assert.equal(brief.has_worker, true);
    assert.equal(brief.state, "explore");
    assert.equal(brief.loop.iteration_n, 1);
    assert.equal(brief.loop.max_iterations, 3);
    assert.equal(brief.loop.done_field, "done");
    assert.match(brief.loop.outputs_path, /^workers\/explore-iters\/iter-1\.json$/);
    assert.equal(brief.worker.role, "explorer");
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("buildLoopBrief: iteration_n advances with each iter trace already on disk", () => {
  const store = tmpStore();
  const runId = buildRunId({ repo: "ctxr-dev/fsm", baseSha: "base", headSha: "head" }).runId;
  try {
    ensureRunDir(runId, { storageRoot: store });
    appendTraceFile(runId, { phase: "entry", state: "explore", data: {} }, { storageRoot: store });
    appendTraceFile(runId, { phase: "iter", state: "explore", data: { iteration_n: 1, outputs: {} } }, { storageRoot: store });
    appendTraceFile(runId, { phase: "iter", state: "explore", data: { iteration_n: 2, outputs: {} } }, { storageRoot: store });
    const brief = buildLoopBrief({
      doc: loopFsm(),
      state: loopFsm().fsm.states[0],
      env: { args: {} },
      runId,
      opts: { storageRoot: store },
    });
    assert.equal(brief.loop.iteration_n, 3);
    assert.match(brief.loop.outputs_path, /iter-3\.json$/);
  } finally {
    rmSync(store, { recursive: true, force: true });
  }
});

test("buildLoopBrief: uses default iteration_outputs_dir when omitted", () => {
  const state = {
    id: "myloop",
    purpose: "x",
    preconditions: [],
    loop: {
      worker: {
        role: "r",
        prompt_template: "t",
        inputs: [],
        response_schema: { type: "object", properties: { done: { type: "boolean" } } },
      },
      max_iterations: 5,
      done_field: "done",
    },
    outputs: ["aggregated_myloop"],
    transitions: [],
  };
  const brief = buildLoopBrief({
    doc: { fsm: { id: "f" } },
    state,
    env: {},
    runId: "20260426-143045-abcdef0",
  });
  assert.match(brief.loop.outputs_path, /^workers\/myloop-iters\/iter-1\.json$/);
});

test("buildBrief: delegates to buildLoopBrief for loop states", () => {
  const state = loopFsm().fsm.states[0];
  const brief = buildBrief({
    doc: loopFsm(),
    state,
    env: { args: {} },
    runId: "20260426-143045-abcdef0",
  });
  assert.equal(brief.has_loop, true);
  assert.equal(brief.has_worker, true);
});

test("buildBrief: non-loop states still set has_loop=false", () => {
  const state = loopFsm().fsm.states[1]; // terminal
  const brief = buildBrief({
    doc: loopFsm(),
    state,
    env: {},
    runId: "20260426-143045-abcdef0",
  });
  assert.equal(brief.has_loop, false);
  assert.equal(brief.has_worker, false);
});

// ─── validateOutputs delegation to loop.worker.response_schema ───────────

test("validateOutputs: uses loop.worker.response_schema for loop states", () => {
  const state = loopFsm().fsm.states[0];
  const good = validateOutputs(state, { done: true, findings: [] });
  assert.equal(good.valid, true);
  const bad = validateOutputs(state, { findings: [] }); // missing done
  assert.equal(bad.valid, false);
});
