// prompt-fragments.mjs - reusable text fragments for FSM worker-dispatch
// prompts.
//
// Skill runners that dispatch a worker for a given FSM state assemble the
// worker's prompt from a fixed set of building blocks: a greeting that names
// the role and the run, the brief itself, an output contract that pins down
// where the worker must write its JSON and what shape that JSON must take,
// and a notice about forbidden write paths. This module factors those four
// pieces out of every per-skill runner so the dispatch wording stays
// consistent across the substrate and the per-skill runners stay short.
//
// The fragments are intentionally tool-agnostic - they never name a specific
// MCP server, CLI, or orchestrator harness. The output is plain Markdown so
// any worker (LLM or otherwise) can consume it. Each fragment validates its
// inputs and throws synchronously on misuse so callers fail fast at compose
// time rather than at dispatch time.
//
// Composition is the caller's job: assemble the fragments in whatever order
// suits the skill, joined by blank lines. A typical sequence is
//   specialistHeader -> briefBlock -> outputContractBlock -> forbiddenPathsNotice
// but the fragments themselves do not assume any ordering.

// ─── Input validation helpers ──────────────────────────────────────────

function requireNonEmptyString(value, label) {
  if (typeof value !== "string" || value.length === 0) {
    throw new TypeError(`${label} must be a non-empty string`);
  }
}

function requireOptions(opts, label) {
  if (!opts || typeof opts !== "object" || Array.isArray(opts)) {
    throw new TypeError(`${label} requires an options object`);
  }
}

// ─── specialistHeader ──────────────────────────────────────────────────

/**
 * Standard opening text for a specialist worker prompt.
 *
 * @param {object} opts
 * @param {string} opts.role     - the worker role identifier (e.g. "lens-gap").
 * @param {string} opts.run_id   - the FSM run id this dispatch belongs to.
 * @param {string} opts.state_id - the FSM state id that is dispatching this worker.
 * @returns {string} Markdown header block addressed to the worker.
 */
export function specialistHeader(opts) {
  requireOptions(opts, "specialistHeader");
  const { role, run_id, state_id } = opts;
  requireNonEmptyString(role, "specialistHeader.role");
  requireNonEmptyString(run_id, "specialistHeader.run_id");
  requireNonEmptyString(state_id, "specialistHeader.state_id");

  return [
    `# Specialist dispatch: ${role}`,
    "",
    `You are the **${role}** specialist for FSM run \`${run_id}\`, state \`${state_id}\`.`,
    "",
    "Work strictly within the contract below. Do not improvise outputs, do",
    "not invent fields, and do not narrate your reasoning outside the",
    "structured output file.",
  ].join("\n");
}

// ─── outputContractBlock ───────────────────────────────────────────────

/**
 * Output-contract block instructing the worker to write a single JSON
 * object to `outputs_path` that matches `response_schema`. The JSON Schema
 * is pretty-printed inside a fenced code block so the worker can read it
 * directly.
 *
 * @param {object} opts
 * @param {string} opts.outputs_path    - absolute path the worker must write its JSON to.
 * @param {object} opts.response_schema - JSON Schema (Draft-07) the output must satisfy.
 * @returns {string} Markdown output-contract block.
 */
export function outputContractBlock(opts) {
  requireOptions(opts, "outputContractBlock");
  const { outputs_path, response_schema } = opts;
  requireNonEmptyString(outputs_path, "outputContractBlock.outputs_path");
  if (
    !response_schema ||
    typeof response_schema !== "object" ||
    Array.isArray(response_schema)
  ) {
    throw new TypeError(
      "outputContractBlock.response_schema must be a JSON Schema object",
    );
  }

  const prettySchema = JSON.stringify(response_schema, null, 2);

  return [
    "## Output contract",
    "",
    `Write **one** JSON object to:`,
    "",
    `    ${outputs_path}`,
    "",
    "The object MUST validate against the JSON Schema below. Do not write",
    "any other file. Do not print the JSON to stdout. Do not wrap the JSON",
    "in code fences inside the output file.",
    "",
    "```json",
    prettySchema,
    "```",
  ].join("\n");
}

// ─── forbiddenPathsNotice ──────────────────────────────────────────────

/**
 * Standard notice forbidding writes outside `<run_dir>/workers/`.
 *
 * @param {object} opts
 * @param {string} opts.run_dir - absolute path to the FSM run directory.
 * @returns {string} Markdown notice block.
 */
export function forbiddenPathsNotice(opts) {
  requireOptions(opts, "forbiddenPathsNotice");
  const { run_dir } = opts;
  requireNonEmptyString(run_dir, "forbiddenPathsNotice.run_dir");

  const workersDir = `${run_dir.replace(/\/+$/, "")}/workers/`;

  return [
    "## Forbidden write paths",
    "",
    `Never write anywhere outside \`${workersDir}\`. In particular:`,
    "",
    "- Do not modify any file under the project source tree.",
    "- Do not create files in `/tmp`, the home directory, or any system path.",
    "- Do not append to files outside the run directory.",
    "",
    "If you believe you need to write elsewhere, stop and surface the need",
    "in your structured output instead of writing the file.",
  ].join("\n");
}

// ─── briefBlock ────────────────────────────────────────────────────────

/**
 * Labelled brief section. The brief may be a plain string or a JSON-
 * serialisable object; objects are pretty-printed inside a fenced JSON
 * block so workers receive a stable, parseable representation.
 *
 * @param {object} opts
 * @param {string|object} opts.brief - the brief payload for this worker.
 * @returns {string} Markdown brief block.
 */
export function briefBlock(opts) {
  requireOptions(opts, "briefBlock");
  const { brief } = opts;
  if (brief === undefined || brief === null) {
    throw new TypeError("briefBlock.brief is required");
  }

  let body;
  if (typeof brief === "string") {
    if (brief.length === 0) {
      throw new TypeError("briefBlock.brief must be a non-empty string");
    }
    body = brief;
  } else if (typeof brief === "object" && !Array.isArray(brief)) {
    body = ["```json", JSON.stringify(brief, null, 2), "```"].join("\n");
  } else if (Array.isArray(brief)) {
    body = ["```json", JSON.stringify(brief, null, 2), "```"].join("\n");
  } else {
    throw new TypeError(
      "briefBlock.brief must be a string, array, or plain object",
    );
  }

  return ["## Brief", "", body].join("\n");
}
