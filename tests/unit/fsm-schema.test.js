// fsm-schema.test.js — unit coverage for FSM YAML schema validation.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  hashFsmYaml,
  validateFsmSchema,
  validateFsmStatic,
  validateWorkerResponse,
} from "../../scripts/lib/fsm-schema.mjs";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// A minimal valid FSM document for happy-path tests.
function validFsm() {
  return {
    fsm: {
      id: "test-fsm",
      version: 1,
      entry: "scan",
      states: [
        {
          id: "scan",
          purpose: "Scan the project.",
          preconditions: [],
          worker: {
            role: "scanner",
            prompt_template: "fsm/workers/scanner.md",
            inputs: ["args"],
            response_schema: {
              type: "object",
              required: ["scanned"],
              properties: { scanned: { type: "boolean" } },
            },
          },
          outputs: ["scanned"],
          post_validations: [],
          transitions: [{ to: "done", when: { kind: "always" } }],
        },
        {
          id: "done",
          purpose: "Terminal.",
          preconditions: ["scanned exists"],
          outputs: [],
          transitions: [],
        },
      ],
    },
  };
}

// ─── validateFsmSchema ──────────────────────────────────────────────────

test("validateFsmSchema accepts a minimal well-formed FSM", () => {
  const { valid, errors } = validateFsmSchema(validFsm());
  assert.equal(valid, true, `expected valid; got errors: ${errors.join("; ")}`);
});

test("validateFsmSchema rejects missing top-level fsm", () => {
  const result = validateFsmSchema({});
  assert.equal(result.valid, false);
  assert.match(result.errors.join(" "), /Missing top-level/);
});

test("validateFsmSchema rejects missing required fields", () => {
  const doc = { fsm: { id: "x", version: 1, entry: "a", states: [] } };
  const { errors } = validateFsmSchema(doc);
  // states must be non-empty
  assert.match(errors.join(" "), /non-empty array/);
});

test("validateFsmSchema rejects malformed state-id", () => {
  const doc = validFsm();
  doc.fsm.states[0].id = "BadStateId";
  const { errors } = validateFsmSchema(doc);
  assert.match(errors.join(" "), /must be snake_case/);
});

test("validateFsmSchema rejects missing transitions array", () => {
  const doc = validFsm();
  delete doc.fsm.states[0].transitions;
  const { errors } = validateFsmSchema(doc);
  assert.match(errors.join(" "), /transitions must be an array/);
});

test("validateFsmSchema rejects unknown when.kind", () => {
  const doc = validFsm();
  doc.fsm.states[0].transitions[0].when = { kind: "magic" };
  const { errors } = validateFsmSchema(doc);
  assert.match(errors.join(" "), /when\.kind must be one of/);
});

test("validateFsmSchema requires when.expression for kind: deterministic", () => {
  const doc = validFsm();
  doc.fsm.states[0].transitions[0].when = { kind: "deterministic" };
  const { errors } = validateFsmSchema(doc);
  assert.match(errors.join(" "), /when\.expression is required/);
});

test("validateFsmSchema requires when.criteria for kind: judgement", () => {
  const doc = validFsm();
  doc.fsm.states[0].transitions[0].when = { kind: "judgement" };
  const { errors } = validateFsmSchema(doc);
  assert.match(errors.join(" "), /when\.criteria is required/);
});

test("validateFsmSchema accepts shorthand string when (always | otherwise)", () => {
  const doc = validFsm();
  doc.fsm.states[0].transitions[0].when = "always";
  const { valid } = validateFsmSchema(doc);
  assert.equal(valid, true);
  doc.fsm.states[0].transitions[0].when = "otherwise";
  const { valid: v2 } = validateFsmSchema(doc);
  assert.equal(v2, true);
});

test("validateFsmSchema rejects invalid response_schema", () => {
  const doc = validFsm();
  doc.fsm.states[0].worker.response_schema = { type: "not-a-real-type" };
  const { errors } = validateFsmSchema(doc);
  assert.match(errors.join(" "), /not a valid JSON Schema/);
});

// ─── validateFsmStatic ─────────────────────────────────────────────────

test("validateFsmStatic accepts a reachable, terminating FSM", () => {
  const { valid, errors } = validateFsmStatic(validFsm());
  assert.equal(valid, true, `errors: ${errors.join("; ")}`);
});

test("validateFsmStatic flags duplicate state ids", () => {
  const doc = validFsm();
  doc.fsm.states.push({ ...doc.fsm.states[0] });
  const { errors } = validateFsmStatic(doc);
  assert.match(errors.join(" "), /Duplicate state id/);
});

test("validateFsmStatic flags transitions to undefined states", () => {
  const doc = validFsm();
  doc.fsm.states[0].transitions[0].to = "undefined_state";
  const { errors } = validateFsmStatic(doc);
  assert.match(errors.join(" "), /transition to undefined state/);
});

test("validateFsmStatic flags unreachable states", () => {
  const doc = validFsm();
  doc.fsm.states.push({
    id: "orphan",
    purpose: "Unreachable.",
    preconditions: [],
    outputs: [],
    transitions: [],
  });
  const { errors } = validateFsmStatic(doc);
  assert.match(errors.join(" "), /unreachable from entry/);
});

test("validateFsmStatic flags missing terminal state", () => {
  const doc = validFsm();
  // Make every state cycle back; remove the terminal.
  doc.fsm.states[1].transitions = [{ to: "scan", when: { kind: "always" } }];
  const { errors } = validateFsmStatic(doc);
  assert.match(errors.join(" "), /no terminal state/);
});

test("validateFsmStatic flags state.worker.inputs not produced upstream", () => {
  const doc = validFsm();
  doc.fsm.states[0].worker.inputs = ["args", "phantom"];
  const { errors } = validateFsmStatic(doc);
  assert.match(errors.join(" "), /references "phantom" which is not produced/);
});

test("validateFsmStatic accepts entry-state worker referencing 'args'", () => {
  // 'args' is the entry-state argument bag and is exempt from the "produced
  // upstream" check.
  const { valid } = validateFsmStatic(validFsm());
  assert.equal(valid, true);
});

// ─── hashFsmYaml ───────────────────────────────────────────────────────

test("hashFsmYaml: stable sha256 of file bytes", () => {
  const tmp = mkdtempSync(join(tmpdir(), "fsm-hash-"));
  try {
    const path = join(tmp, "fsm.yaml");
    writeFileSync(path, "fsm:\n  id: x\n");
    const h1 = hashFsmYaml(path);
    const h2 = hashFsmYaml(path);
    assert.match(h1, /^sha256:[0-9a-f]{64}$/);
    assert.equal(h1, h2);
    writeFileSync(path, "fsm:\n  id: y\n");
    assert.notEqual(hashFsmYaml(path), h1);
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
});

// ─── validateWorkerResponse ────────────────────────────────────────────

test("validateWorkerResponse: passes a valid payload", () => {
  const schema = {
    type: "object",
    required: ["count"],
    properties: { count: { type: "integer", minimum: 0 } },
  };
  assert.deepEqual(validateWorkerResponse(schema, { count: 3 }), { valid: true, errors: [] });
});

test("validateWorkerResponse: surfaces structured errors on a bad payload", () => {
  const schema = {
    type: "object",
    required: ["count"],
    properties: { count: { type: "integer", minimum: 0 } },
  };
  const result = validateWorkerResponse(schema, { count: -1 });
  assert.equal(result.valid, false);
  assert.ok(result.errors.length > 0);
  assert.match(result.errors[0], />=/);
});

test("validateWorkerResponse: schema compilation error returns valid=false", () => {
  const result = validateWorkerResponse({ type: "garbage" }, {});
  assert.equal(result.valid, false);
  assert.match(result.errors[0], /Schema compilation/);
});
