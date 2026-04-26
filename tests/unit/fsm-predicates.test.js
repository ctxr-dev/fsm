// fsm-predicates.test.js — unit coverage for the deterministic predicate
// evaluator. Drives the safe DSL through every supported operator,
// function, and error mode.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  evaluatePredicate,
  parsePredicate,
} from "../../scripts/lib/fsm-predicates.mjs";

// ─── Literals + identifiers ────────────────────────────────────────────

test("literal true / false / null", () => {
  assert.equal(evaluatePredicate("true"), true);
  assert.equal(evaluatePredicate("false"), false);
  assert.equal(evaluatePredicate("null == null"), true);
});

test("always evaluates to true (used for unconditional transitions)", () => {
  assert.equal(evaluatePredicate("always"), true);
});

test("identifier resolution from environment", () => {
  assert.equal(evaluatePredicate("tier", { tier: "trivial" }), true);
  assert.equal(evaluatePredicate("tier == 'trivial'", { tier: "trivial" }), true);
  assert.equal(evaluatePredicate("tier == 'lite'", { tier: "trivial" }), false);
});

test("dotted identifier path resolves nested objects", () => {
  const env = { project_profile: { languages: ["python"] } };
  assert.deepEqual(
    parsePredicate("project_profile.languages").type,
    "ident",
  );
  assert.equal(
    evaluatePredicate("len(project_profile.languages) == 1", env),
    true,
  );
});

test("missing identifier evaluates to undefined; comparison to null works", () => {
  assert.equal(evaluatePredicate("missing == null", {}), false);
  assert.equal(evaluatePredicate("missing != null", {}), true);
});

// ─── Comparison operators ──────────────────────────────────────────────

test("comparison operators ==, !=, <, >, <=, >=", () => {
  assert.equal(evaluatePredicate("3 == 3"), true);
  assert.equal(evaluatePredicate("3 != 3"), false);
  assert.equal(evaluatePredicate("3 < 4"), true);
  assert.equal(evaluatePredicate("3 > 4"), false);
  assert.equal(evaluatePredicate("3 <= 3"), true);
  assert.equal(evaluatePredicate("3 >= 4"), false);
});

test("string equality", () => {
  assert.equal(evaluatePredicate("'a' == 'a'"), true);
  assert.equal(evaluatePredicate('"a" == "b"'), false);
});

// ─── Logical operators ─────────────────────────────────────────────────

test("AND / OR / NOT (uppercase keywords)", () => {
  assert.equal(evaluatePredicate("true AND false"), false);
  assert.equal(evaluatePredicate("true AND true"), true);
  assert.equal(evaluatePredicate("false OR true"), true);
  assert.equal(evaluatePredicate("NOT false"), true);
  assert.equal(evaluatePredicate("NOT (true AND false)"), true);
});

test("&& and || aliases", () => {
  assert.equal(evaluatePredicate("true && true"), true);
  assert.equal(evaluatePredicate("false || true"), true);
});

test("operator precedence: AND binds tighter than OR", () => {
  // true OR false AND false  →  true OR (false AND false) → true
  assert.equal(evaluatePredicate("true OR false AND false"), true);
  // (true OR false) AND false → true AND false → false
  assert.equal(evaluatePredicate("(true OR false) AND false"), false);
});

test("NOT binds tighter than AND/OR", () => {
  assert.equal(evaluatePredicate("NOT true AND true"), false);
  assert.equal(evaluatePredicate("NOT false OR false"), true);
});

// ─── Functions ─────────────────────────────────────────────────────────

test("len() on strings and arrays", () => {
  // evaluatePredicate always coerces to boolean (Boolean(3) === true).
  // Direct numeric checks compare via the comparison operators below.
  assert.equal(evaluatePredicate("len('abc') == 3"), true);
  assert.equal(evaluatePredicate("len(items) == 0", { items: [] }), true);
  assert.equal(evaluatePredicate("len(items) == 3", { items: ["a", "b", "c"] }), true);
});

test("len() on null / missing returns 0", () => {
  assert.equal(evaluatePredicate("len(missing) == 0"), true);
  assert.equal(evaluatePredicate("len(null) == 0"), true);
});

test("len() throws on non-string / non-array values", () => {
  assert.throws(() => evaluatePredicate("len(x) == 0", { x: 42 }), /expects a string or array/);
});

test("empty() shortcut", () => {
  assert.equal(evaluatePredicate("empty('')"), true);
  assert.equal(evaluatePredicate("empty('abc')"), false);
  assert.equal(evaluatePredicate("empty(items)", { items: [] }), true);
  assert.equal(evaluatePredicate("empty(items)", { items: ["a"] }), false);
});

// ─── Realistic FSM transition predicates ───────────────────────────────

test("Sprint 1 short-circuit predicate", () => {
  const env = {
    tier: "trivial",
    stage_a_candidates: [],
    scope_overrides: [],
  };
  assert.equal(
    evaluatePredicate(
      "tier == 'trivial' AND len(stage_a_candidates) == 0 AND len(scope_overrides) == 0",
      env,
    ),
    true,
  );
});

test("predicate with mixed deterministic + identifier flow", () => {
  const env = { tier: "full", cap: 20 };
  assert.equal(evaluatePredicate("tier == 'full' AND cap >= 20", env), true);
  assert.equal(evaluatePredicate("tier == 'full' AND cap > 30", env), false);
});

// ─── Error modes ───────────────────────────────────────────────────────

test("parsePredicate rejects empty / non-string source", () => {
  assert.throws(() => parsePredicate(""), /empty source/);
  assert.throws(() => parsePredicate("   "), /empty source/);
  assert.throws(() => parsePredicate(123), /must be a string/);
});

test("parsePredicate rejects unknown character", () => {
  assert.throws(() => parsePredicate("@"), /Unexpected character/);
});

test("parsePredicate rejects unterminated string", () => {
  assert.throws(() => parsePredicate("'abc"), /Unterminated string/);
});

test("parsePredicate rejects unbalanced parens", () => {
  assert.throws(() => parsePredicate("(true"), /Expected '\)'/);
});

test("evaluatePredicate rejects unknown function", () => {
  assert.throws(() => evaluatePredicate("nope(x)", { x: 1 }), /Unknown function/);
});
