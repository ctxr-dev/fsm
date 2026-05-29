// fsm-cross-state-aggregator.test.js: unit coverage for the cross-
// state aggregator (A3). Reads exit-trace outputs across multiple
// states in a single run and writes a unified
// <run_dir>/manifest-aggregates/<merge-field>.json file.
//
// Tests exercise: input validation, missing-states accounting, the
// canonical "two states with findings 3 + 5 produce length-8" path,
// default mergeField = "findings", atomic write (no tmp leftovers),
// idempotent re-run, missing states do NOT throw, malformed trace
// files are tolerated, and the manifestAggregateEntry helper shape.

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
import { stringify as stringifyYaml } from "yaml";

import {
  aggregateAcrossStates,
  manifestAggregateEntry,
} from "../../scripts/lib/fsm-aggregator.mjs";

function makeRunDir() {
  return mkdtempSync(join(tmpdir(), "fsm-xagg-rd-"));
}

// writeExit appends a synthetic exit-trace YAML to <runDir>/fsm-trace/
// using the same NNNN-exit-<state>.yaml convention the real engine
// emits. Each call bumps the sequence number so files sort
// chronologically.
function writeExit(runDir, seq, stateId, outputs) {
  const traceDir = join(runDir, "fsm-trace");
  mkdirSync(traceDir, { recursive: true });
  const seqStr = String(seq).padStart(4, "0");
  const fileName = `${seqStr}-exit-${stateId}.yaml`;
  const payload = {
    phase: "exit",
    state: stateId,
    sequence: seq,
    timestamp: "2026-01-01T00:00:00.000Z",
    outputs,
    post_validations: [],
    transition_evaluation: [],
    transition: null,
  };
  writeFileSync(join(traceDir, fileName), stringifyYaml(payload));
}

test("aggregateAcrossStates: rejects empty stateIds", () => {
  const runDir = makeRunDir();
  try {
    assert.throws(
      () => aggregateAcrossStates(runDir, { stateIds: [] }),
      /non-empty array/,
    );
    assert.throws(
      () => aggregateAcrossStates(runDir, {}),
      /non-empty array/,
    );
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: rejects non-string stateIds entries", () => {
  const runDir = makeRunDir();
  try {
    assert.throws(
      () => aggregateAcrossStates(runDir, { stateIds: ["ok", 7] }),
      /non-empty strings/,
    );
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: rejects empty mergeField", () => {
  const runDir = makeRunDir();
  try {
    assert.throws(
      () => aggregateAcrossStates(runDir, { stateIds: ["a"], mergeField: "" }),
      /mergeField must be a non-empty string/,
    );
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: rejects missing runDir", () => {
  assert.throws(
    () => aggregateAcrossStates(undefined, { stateIds: ["a"] }),
    /runDir/,
  );
});

test("aggregateAcrossStates: two states with findings 3 + 5 produce length-8 unified file", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "explore_loop", {
      findings: [
        { id: "f1" },
        { id: "f2" },
        { id: "f3" },
      ],
    });
    writeExit(runDir, 2, "verify_coverage", {
      findings: [
        { id: "f4" },
        { id: "f5" },
        { id: "f6" },
        { id: "f7" },
        { id: "f8" },
      ],
    });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["explore_loop", "verify_coverage"],
      mergeField: "findings",
    });
    assert.equal(result.state_count, 2);
    assert.equal(result.merged_length, 8);
    assert.deepEqual(result.missing_states, []);
    assert.equal(result.aggregated_path, "manifest-aggregates/findings.json");

    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.equal(written.field, "findings");
    assert.deepEqual(written.from_states, ["explore_loop", "verify_coverage"]);
    assert.equal(written.merged_length, 8);
    assert.equal(written.items.length, 8);
    assert.deepEqual(
      written.items.map((i) => i.id),
      ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8"],
    );
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: defaults mergeField to 'findings'", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "a", { findings: [{ x: 1 }, { x: 2 }] });
    const result = aggregateAcrossStates(runDir, { stateIds: ["a"] });
    assert.equal(result.merged_length, 2);
    assert.equal(result.aggregated_path, "manifest-aggregates/findings.json");
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.equal(written.field, "findings");
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: non-default mergeField writes the matching file", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "scan", {
      issues: [{ q: "one" }, { q: "two" }],
    });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["scan"],
      mergeField: "issues",
    });
    assert.equal(result.aggregated_path, "manifest-aggregates/issues.json");
    assert.equal(result.merged_length, 2);
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.equal(written.field, "issues");
    assert.deepEqual(written.items.map((i) => i.q), ["one", "two"]);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: missing states are reported, not thrown", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "present", { findings: [{ a: 1 }, { a: 2 }] });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["present", "missing_a", "missing_b"],
      mergeField: "findings",
    });
    assert.equal(result.state_count, 1);
    assert.equal(result.merged_length, 2);
    assert.deepEqual(result.missing_states, ["missing_a", "missing_b"]);
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.deepEqual(written.missing_states, ["missing_a", "missing_b"]);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: every state missing yields empty array and full missing_states list", () => {
  const runDir = makeRunDir();
  try {
    // No trace dir at all.
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["a", "b"],
      mergeField: "findings",
    });
    assert.equal(result.state_count, 0);
    assert.equal(result.merged_length, 0);
    assert.deepEqual(result.missing_states, ["a", "b"]);
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.deepEqual(written.items, []);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: state with exit trace but no mergeField is counted with zero contribution", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "produces", { findings: [{ a: 1 }] });
    writeExit(runDir, 2, "empty_outputs", { other_field: "nothing here" });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["produces", "empty_outputs"],
      mergeField: "findings",
    });
    // Both states present; only one contributes.
    assert.equal(result.state_count, 2);
    assert.equal(result.merged_length, 1);
    assert.deepEqual(result.missing_states, []);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: state ordering follows caller's stateIds, not trace order", () => {
  const runDir = makeRunDir();
  try {
    // Trace written B first, then A.
    writeExit(runDir, 1, "b", { findings: [{ src: "b1" }, { src: "b2" }] });
    writeExit(runDir, 2, "a", { findings: [{ src: "a1" }] });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["a", "b"],
      mergeField: "findings",
    });
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.deepEqual(written.items.map((i) => i.src), ["a1", "b1", "b2"]);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: most recent exit wins when a state has multiple exit traces", () => {
  const runDir = makeRunDir();
  try {
    // Resume scenario: state A entered twice, two exit traces.
    writeExit(runDir, 1, "a", { findings: [{ id: "old1" }, { id: "old2" }] });
    writeExit(runDir, 5, "a", { findings: [{ id: "new1" }] });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["a"],
      mergeField: "findings",
    });
    assert.equal(result.merged_length, 1);
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.deepEqual(written.items.map((i) => i.id), ["new1"]);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: idempotent re-run produces identical bytes", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "a", { findings: [{ k: 1 }, { k: 2 }] });
    writeExit(runDir, 2, "b", { findings: [{ k: 3 }] });
    const first = aggregateAcrossStates(runDir, {
      stateIds: ["a", "b"],
      mergeField: "findings",
    });
    const firstBody = readFileSync(join(runDir, first.aggregated_path), "utf8");
    const second = aggregateAcrossStates(runDir, {
      stateIds: ["a", "b"],
      mergeField: "findings",
    });
    const secondBody = readFileSync(join(runDir, second.aggregated_path), "utf8");
    assert.equal(first.merged_length, second.merged_length);
    assert.equal(first.state_count, second.state_count);
    assert.equal(firstBody, secondBody);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: atomic write leaves no .tmp files in manifest-aggregates/", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "a", { findings: [{ id: "x" }] });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["a"],
      mergeField: "findings",
    });
    assert.ok(existsSync(join(runDir, result.aggregated_path)));
    const aggDir = join(runDir, "manifest-aggregates");
    const leftovers = readdirSync(aggDir).filter(
      (n) => n.includes(".tmp") || n.endsWith("~"),
    );
    assert.equal(leftovers.length, 0, `tmp leftovers: ${JSON.stringify(leftovers)}`);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: tolerates malformed trace YAML files", () => {
  const runDir = makeRunDir();
  try {
    writeExit(runDir, 1, "good", { findings: [{ k: 1 }] });
    // Synthesize a malformed YAML file with the same naming convention.
    const traceDir = join(runDir, "fsm-trace");
    writeFileSync(
      join(traceDir, "0002-exit-bad.yaml"),
      "this: : : not parseable: [[[\n",
    );
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["good", "bad"],
      mergeField: "findings",
    });
    assert.equal(result.merged_length, 1);
    // "bad" had no PARSEABLE exit trace, so it's missing.
    assert.deepEqual(result.missing_states, ["bad"]);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("aggregateAcrossStates: ignores entry-phase trace files even with matching state", () => {
  const runDir = makeRunDir();
  try {
    const traceDir = join(runDir, "fsm-trace");
    mkdirSync(traceDir, { recursive: true });
    // Entry trace, NOT exit. The aggregator must NOT pick this up.
    writeFileSync(
      join(traceDir, "0001-entry-a.yaml"),
      stringifyYaml({
        phase: "entry",
        state: "a",
        sequence: 1,
        outputs: { findings: [{ ignored: true }] },
      }),
    );
    writeExit(runDir, 2, "b", { findings: [{ kept: true }] });
    const result = aggregateAcrossStates(runDir, {
      stateIds: ["a", "b"],
      mergeField: "findings",
    });
    assert.deepEqual(result.missing_states, ["a"]);
    assert.equal(result.merged_length, 1);
    const written = JSON.parse(readFileSync(join(runDir, result.aggregated_path), "utf8"));
    assert.equal(written.items[0].kept, true);
  } finally {
    rmSync(runDir, { recursive: true, force: true });
  }
});

test("manifestAggregateEntry: produces the canonical shape", () => {
  const entry = manifestAggregateEntry({
    fromStates: ["explore_loop", "verify_coverage"],
    field: "findings",
    aggregatedPath: "manifest-aggregates/findings.json",
    mergedLength: 8,
    missingStates: [],
  });
  assert.deepEqual(entry, {
    from_states: ["explore_loop", "verify_coverage"],
    field: "findings",
    path: "manifest-aggregates/findings.json",
    merged_length: 8,
    missing_states: [],
  });
});

test("manifestAggregateEntry: rejects invalid inputs", () => {
  assert.throws(
    () => manifestAggregateEntry({
      fromStates: [],
      field: "findings",
      aggregatedPath: "x",
      mergedLength: 0,
    }),
    /non-empty array/,
  );
  assert.throws(
    () => manifestAggregateEntry({
      fromStates: ["a"],
      field: "",
      aggregatedPath: "x",
      mergedLength: 0,
    }),
    /field must be a non-empty string/,
  );
  assert.throws(
    () => manifestAggregateEntry({
      fromStates: ["a"],
      field: "findings",
      aggregatedPath: "",
      mergedLength: 0,
    }),
    /aggregatedPath/,
  );
  assert.throws(
    () => manifestAggregateEntry({
      fromStates: ["a"],
      field: "findings",
      aggregatedPath: "p",
      mergedLength: -1,
    }),
    /non-negative integer/,
  );
});

test("manifestAggregateEntry: copies arrays defensively", () => {
  const fromStates = ["a", "b"];
  const missing = ["c"];
  const entry = manifestAggregateEntry({
    fromStates,
    field: "findings",
    aggregatedPath: "p",
    mergedLength: 0,
    missingStates: missing,
  });
  fromStates.push("MUTATED");
  missing.push("MUTATED");
  assert.deepEqual(entry.from_states, ["a", "b"]);
  assert.deepEqual(entry.missing_states, ["c"]);
});
