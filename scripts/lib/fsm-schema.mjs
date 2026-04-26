// fsm-schema.mjs — schema validation for FSM YAML files.
//
// Two public entry points:
//   validateFsmSchema(doc)    — structural validation of one FSM document
//   validateFsmStatic(doc)    — static cross-state checks (reachability,
//                               transition resolution, input/output flow)
//
// Both return { valid: boolean, errors: string[] } and never throw on
// invalid input — they accumulate every detected problem so callers can
// print one report rather than fix-build-fail-fix-build cycles.

import Ajv from "ajv";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, resolve } from "node:path";

const ajv = new Ajv({ allErrors: true, strict: false });

const VALID_KINDS = new Set(["deterministic", "judgement", "always"]);

// ─── Structural validation ──────────────────────────────────────────────

export function validateFsmSchema(doc) {
  const errors = [];
  if (!doc || typeof doc !== "object") {
    return { valid: false, errors: ["Document must be a YAML mapping"] };
  }
  if (!doc.fsm || typeof doc.fsm !== "object") {
    return { valid: false, errors: ["Missing top-level `fsm:` key"] };
  }
  const fsm = doc.fsm;
  // Required top-level fields.
  for (const field of ["id", "version", "entry", "states"]) {
    if (!Object.prototype.hasOwnProperty.call(fsm, field)) {
      errors.push(`fsm.${field} is required`);
    }
  }
  if (typeof fsm.id !== "string" || !fsm.id) {
    errors.push("fsm.id must be a non-empty string");
  }
  if (!Number.isInteger(fsm.version)) {
    errors.push("fsm.version must be an integer");
  }
  if (typeof fsm.entry !== "string" || !fsm.entry) {
    errors.push("fsm.entry must be a non-empty string");
  }
  if (!Array.isArray(fsm.states) || fsm.states.length === 0) {
    errors.push("fsm.states must be a non-empty array");
    return { valid: false, errors };
  }
  // Validate each state.
  fsm.states.forEach((state, idx) => {
    const prefix = `fsm.states[${idx}]`;
    validateState(state, prefix, errors);
  });
  return { valid: errors.length === 0, errors };
}

function validateState(state, prefix, errors) {
  if (!state || typeof state !== "object") {
    errors.push(`${prefix} must be a mapping`);
    return;
  }
  if (typeof state.id !== "string" || !state.id) {
    errors.push(`${prefix}.id must be a non-empty string`);
  } else if (!/^[a-z][a-z0-9_]*$/.test(state.id)) {
    errors.push(`${prefix}.id "${state.id}" must be snake_case (lowercase + underscores)`);
  }
  if (typeof state.purpose !== "string" || !state.purpose) {
    errors.push(`${prefix}.purpose must be a non-empty string`);
  }
  if (!Array.isArray(state.preconditions)) {
    errors.push(`${prefix}.preconditions must be an array (use [] for entry states)`);
  } else {
    state.preconditions.forEach((p, i) => {
      if (typeof p !== "string") {
        errors.push(`${prefix}.preconditions[${i}] must be a string`);
      }
    });
  }
  if (state.worker !== undefined) {
    validateWorker(state.worker, `${prefix}.worker`, errors);
  }
  if (!Array.isArray(state.outputs)) {
    errors.push(`${prefix}.outputs must be an array (use [] for terminal states)`);
  } else {
    state.outputs.forEach((o, i) => {
      if (typeof o !== "string") {
        errors.push(`${prefix}.outputs[${i}] must be a string`);
      }
    });
  }
  if (state.post_validations !== undefined && !Array.isArray(state.post_validations)) {
    errors.push(`${prefix}.post_validations must be an array of strings (or omitted)`);
  }
  if (!Array.isArray(state.transitions)) {
    errors.push(`${prefix}.transitions must be an array (use [] for terminal states)`);
    return;
  }
  state.transitions.forEach((t, i) => {
    validateTransition(t, `${prefix}.transitions[${i}]`, errors);
  });
}

function validateWorker(worker, prefix, errors) {
  if (!worker || typeof worker !== "object") {
    errors.push(`${prefix} must be a mapping`);
    return;
  }
  for (const field of ["role", "prompt_template", "inputs", "response_schema"]) {
    if (!Object.prototype.hasOwnProperty.call(worker, field)) {
      errors.push(`${prefix}.${field} is required`);
    }
  }
  if (worker.role !== undefined && (typeof worker.role !== "string" || !worker.role)) {
    errors.push(`${prefix}.role must be a non-empty string`);
  }
  if (worker.prompt_template !== undefined && typeof worker.prompt_template !== "string") {
    errors.push(`${prefix}.prompt_template must be a string path`);
  }
  if (worker.inputs !== undefined && !Array.isArray(worker.inputs)) {
    errors.push(`${prefix}.inputs must be an array of strings`);
  }
  if (worker.response_schema !== undefined) {
    try {
      ajv.compile(worker.response_schema);
    } catch (err) {
      errors.push(`${prefix}.response_schema is not a valid JSON Schema: ${err.message}`);
    }
  }
}

function validateTransition(t, prefix, errors) {
  if (!t || typeof t !== "object") {
    errors.push(`${prefix} must be a mapping`);
    return;
  }
  if (typeof t.to !== "string" || !t.to) {
    errors.push(`${prefix}.to must be a non-empty state-id string`);
  }
  if (!t.when) {
    errors.push(`${prefix}.when is required`);
    return;
  }
  // Accept shorthand: when: "always" | "otherwise"
  if (typeof t.when === "string") {
    if (t.when !== "always" && t.when !== "otherwise") {
      errors.push(
        `${prefix}.when must be "always", "otherwise", or a {kind, ...} mapping`,
      );
    }
    return;
  }
  if (typeof t.when !== "object") {
    errors.push(`${prefix}.when must be a mapping or "always"/"otherwise" string`);
    return;
  }
  if (!VALID_KINDS.has(t.when.kind)) {
    errors.push(
      `${prefix}.when.kind must be one of {deterministic, judgement, always}, got "${t.when.kind}"`,
    );
  }
  if (t.when.kind === "deterministic" && (typeof t.when.expression !== "string" || !t.when.expression)) {
    errors.push(`${prefix}.when.expression is required when kind is deterministic`);
  }
  if (t.when.kind === "judgement" && (typeof t.when.criteria !== "string" || !t.when.criteria)) {
    errors.push(`${prefix}.when.criteria is required when kind is judgement`);
  }
}

// ─── Static cross-state checks ─────────────────────────────────────────

export function validateFsmStatic(doc, { fsmFilePath } = {}) {
  const errors = [];
  if (!doc?.fsm?.states || !Array.isArray(doc.fsm.states)) {
    return { valid: false, errors: ["validateFsmStatic: doc.fsm.states must be an array"] };
  }
  const fsm = doc.fsm;
  const stateById = new Map();
  for (const state of fsm.states) {
    if (typeof state?.id !== "string") continue;
    if (stateById.has(state.id)) {
      errors.push(`Duplicate state id "${state.id}"`);
    } else {
      stateById.set(state.id, state);
    }
  }
  // Entry state must exist.
  if (typeof fsm.entry === "string" && !stateById.has(fsm.entry)) {
    errors.push(`Entry state "${fsm.entry}" is not defined`);
  }
  // Every transition must resolve.
  for (const state of fsm.states) {
    if (!Array.isArray(state?.transitions)) continue;
    for (const t of state.transitions) {
      if (typeof t?.to !== "string") continue;
      if (!stateById.has(t.to)) {
        errors.push(`State "${state.id}" has transition to undefined state "${t.to}"`);
      }
    }
  }
  // Reachability: BFS from entry.
  if (typeof fsm.entry === "string" && stateById.has(fsm.entry)) {
    const reachable = new Set([fsm.entry]);
    const queue = [fsm.entry];
    while (queue.length > 0) {
      const current = queue.shift();
      const state = stateById.get(current);
      if (!state || !Array.isArray(state.transitions)) continue;
      for (const t of state.transitions) {
        if (typeof t?.to !== "string") continue;
        if (!reachable.has(t.to) && stateById.has(t.to)) {
          reachable.add(t.to);
          queue.push(t.to);
        }
      }
    }
    for (const id of stateById.keys()) {
      if (!reachable.has(id)) {
        errors.push(`State "${id}" is unreachable from entry "${fsm.entry}"`);
      }
    }
  }
  // At least one terminal state (transitions: []).
  const terminals = fsm.states.filter((s) => Array.isArray(s?.transitions) && s.transitions.length === 0);
  if (terminals.length === 0) {
    errors.push(
      "FSM has no terminal state — at least one state must have an empty transitions[] to halt the machine",
    );
  }
  // Worker prompt template files must exist on disk. Relative paths
  // resolve against the FSM YAML's directory; absolute paths are taken
  // verbatim. Convention: paths in the YAML are local to the YAML.
  if (fsmFilePath) {
    const fsmDir = dirname(fsmFilePath);
    for (const state of fsm.states) {
      if (!state?.worker?.prompt_template) continue;
      const tpl = state.worker.prompt_template;
      const candidate = isAbsolute(tpl) ? tpl : resolve(fsmDir, tpl);
      if (!existsSync(candidate)) {
        errors.push(
          `State "${state.id}" worker prompt_template "${tpl}" not found at ${candidate}`,
        );
      }
    }
  }
  // Best-effort input/output flow check: each state's preconditions reference
  // a name that is produced by some upstream state's outputs. We do this as
  // a substring check rather than a full dataflow analysis — preconditions
  // are free-form English sentences that often contain the variable name
  // (e.g. "project_profile exists in run state").
  const allOutputs = new Set();
  for (const state of fsm.states) {
    for (const o of state?.outputs ?? []) {
      if (typeof o === "string") allOutputs.add(o);
    }
  }
  for (const state of fsm.states) {
    if (!state?.worker?.inputs) continue;
    for (const inputName of state.worker.inputs) {
      if (inputName === "args") continue; // entry-state argument bag
      if (typeof inputName !== "string") continue;
      if (!allOutputs.has(inputName)) {
        errors.push(
          `State "${state.id}" worker.inputs references "${inputName}" which is not produced by any state's outputs[]`,
        );
      }
    }
  }
  return { valid: errors.length === 0, errors };
}

// ─── Helpers ───────────────────────────────────────────────────────────

// hashFsmYaml returns a stable SHA-256 of the raw YAML file bytes. Used for
// the manifest.json's fsm_yaml_hash field; lets fsm-next detect mid-flight
// FSM-YAML mutations.
export function hashFsmYaml(filePath) {
  const bytes = readFileSync(filePath);
  return `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
}

// validateWorkerResponse compiles the state's response_schema and validates
// a worker's JSON output against it. Returns { valid, errors }.
export function validateWorkerResponse(responseSchema, payload) {
  let validate;
  try {
    validate = ajv.compile(responseSchema);
  } catch (err) {
    return { valid: false, errors: [`Schema compilation failed: ${err.message}`] };
  }
  const ok = validate(payload);
  if (ok) return { valid: true, errors: [] };
  const messages = (validate.errors ?? []).map(
    (e) => `${e.instancePath || "<root>"} ${e.message ?? "is invalid"}`,
  );
  return { valid: false, errors: messages };
}
