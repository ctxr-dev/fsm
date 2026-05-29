// prompt-fragments.test.js - unit coverage for the reusable worker-dispatch
// prompt fragments. Each fragment is exercised for happy-path content, input
// validation, and tool-agnostic wording. A final composition test asserts the
// fragments chain into a coherent prompt.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  briefBlock,
  forbiddenPathsNotice,
  outputContractBlock,
  specialistHeader,
} from "../../scripts/lib/prompt-fragments.mjs";

// ─── specialistHeader ──────────────────────────────────────────────────

test("specialistHeader includes role, run_id, and state_id verbatim", () => {
  const text = specialistHeader({
    role: "lens-gap",
    run_id: "run-2026-05-29-abcd",
    state_id: "dispatch_lens_specialists",
  });
  assert.match(text, /Specialist dispatch: lens-gap/);
  assert.match(text, /run-2026-05-29-abcd/);
  assert.match(text, /dispatch_lens_specialists/);
  assert.match(text, /\*\*lens-gap\*\*/);
});

test("specialistHeader rejects missing fields", () => {
  assert.throws(() => specialistHeader(), /requires an options object/);
  assert.throws(
    () => specialistHeader({ run_id: "r", state_id: "s" }),
    /role must be a non-empty string/,
  );
  assert.throws(
    () => specialistHeader({ role: "r", state_id: "s" }),
    /run_id must be a non-empty string/,
  );
  assert.throws(
    () => specialistHeader({ role: "r", run_id: "r" }),
    /state_id must be a non-empty string/,
  );
});

test("specialistHeader rejects wrong-type fields", () => {
  assert.throws(
    () => specialistHeader({ role: 1, run_id: "r", state_id: "s" }),
    /role must be a non-empty string/,
  );
  assert.throws(
    () => specialistHeader({ role: "", run_id: "r", state_id: "s" }),
    /role must be a non-empty string/,
  );
});

// ─── outputContractBlock ───────────────────────────────────────────────

test("outputContractBlock embeds outputs_path and pretty-printed schema", () => {
  const schema = {
    $schema: "http://json-schema.org/draft-07/schema#",
    type: "object",
    required: ["findings"],
    properties: {
      findings: {
        type: "array",
        items: {
          type: "object",
          required: ["title"],
          properties: { title: { type: "string" } },
        },
      },
    },
  };
  const text = outputContractBlock({
    outputs_path: "/tmp/run-xyz/workers/lens-gap.out.json",
    response_schema: schema,
  });

  // Path appears verbatim.
  assert.match(text, /\/tmp\/run-xyz\/workers\/lens-gap\.out\.json/);
  // The fenced code block is present.
  assert.match(text, /```json\n[\s\S]+\n```/);
  // Schema is pretty-printed (two-space indentation, line per field).
  const pretty = JSON.stringify(schema, null, 2);
  assert.ok(text.includes(pretty), "pretty-printed schema is embedded verbatim");
  // The wording emphasises "one" JSON object and JSON Schema compliance.
  assert.match(text, /one/i);
  assert.match(text, /Output contract/);
});

test("outputContractBlock rejects missing or wrong-type inputs", () => {
  const schema = { type: "object" };
  assert.throws(
    () => outputContractBlock({ response_schema: schema }),
    /outputs_path must be a non-empty string/,
  );
  assert.throws(
    () => outputContractBlock({ outputs_path: "/x" }),
    /response_schema must be a JSON Schema object/,
  );
  assert.throws(
    () => outputContractBlock({ outputs_path: "/x", response_schema: "schema" }),
    /response_schema must be a JSON Schema object/,
  );
  assert.throws(
    () => outputContractBlock({ outputs_path: "/x", response_schema: [1, 2] }),
    /response_schema must be a JSON Schema object/,
  );
});

// ─── forbiddenPathsNotice ──────────────────────────────────────────────

test("forbiddenPathsNotice references run_dir/workers/ exactly", () => {
  const text = forbiddenPathsNotice({ run_dir: "/var/runs/run-42" });
  assert.match(text, /\/var\/runs\/run-42\/workers\//);
  assert.match(text, /Forbidden write paths/);
  assert.match(text, /Never write anywhere outside/);
});

test("forbiddenPathsNotice normalises a trailing slash on run_dir", () => {
  const text = forbiddenPathsNotice({ run_dir: "/var/runs/run-42/" });
  // Should not produce `//workers/`.
  assert.doesNotMatch(text, /run-42\/\/workers\//);
  assert.match(text, /\/var\/runs\/run-42\/workers\//);
});

test("forbiddenPathsNotice rejects missing run_dir", () => {
  assert.throws(() => forbiddenPathsNotice({}), /run_dir must be a non-empty string/);
  assert.throws(() => forbiddenPathsNotice(), /requires an options object/);
});

// ─── briefBlock ────────────────────────────────────────────────────────

test("briefBlock renders a string brief verbatim under a Brief heading", () => {
  const text = briefBlock({ brief: "Review the plan at /tmp/plan.md against the lens." });
  assert.match(text, /## Brief/);
  assert.match(text, /Review the plan at \/tmp\/plan\.md against the lens\./);
});

test("briefBlock pretty-prints an object brief inside a fenced JSON block", () => {
  const briefObj = { plan_path: "/tmp/plan.md", lens: "gap" };
  const text = briefBlock({ brief: briefObj });
  const pretty = JSON.stringify(briefObj, null, 2);
  assert.ok(text.includes(pretty), "object brief is pretty-printed");
  assert.match(text, /```json/);
  assert.match(text, /```$/m);
});

test("briefBlock pretty-prints an array brief", () => {
  const briefArr = [{ id: "c1" }, { id: "c2" }];
  const text = briefBlock({ brief: briefArr });
  assert.ok(text.includes(JSON.stringify(briefArr, null, 2)));
});

test("briefBlock rejects empty / nullish / wrong-type briefs", () => {
  assert.throws(() => briefBlock({}), /brief is required/);
  assert.throws(() => briefBlock({ brief: null }), /brief is required/);
  assert.throws(() => briefBlock({ brief: "" }), /non-empty string/);
  assert.throws(() => briefBlock({ brief: 42 }), /string, array, or plain object/);
  assert.throws(() => briefBlock({ brief: true }), /string, array, or plain object/);
});

// ─── Tool-agnostic wording ─────────────────────────────────────────────

test("fragments do not name any specific MCP server, CLI, or harness", () => {
  const composed = [
    specialistHeader({ role: "lens-gap", run_id: "r1", state_id: "s1" }),
    briefBlock({ brief: "do the thing" }),
    outputContractBlock({
      outputs_path: "/x.json",
      response_schema: { type: "object" },
    }),
    forbiddenPathsNotice({ run_dir: "/x" }),
  ].join("\n\n");

  // Sample of forbidden brand / harness names. The fragments must stay
  // generic so any worker can consume them.
  const forbiddenTerms = [
    "Claude Code",
    "Anthropic",
    "ChatGPT",
    "OpenAI",
    "Cursor",
    "Aider",
    "gh CLI",
    "MCP server",
  ];
  for (const term of forbiddenTerms) {
    assert.ok(
      !composed.includes(term),
      `composed prompt must not name "${term}"`,
    );
  }
});

// ─── Composition ───────────────────────────────────────────────────────

test("fragments compose into a coherent worker-dispatch prompt", () => {
  const schema = {
    $schema: "http://json-schema.org/draft-07/schema#",
    type: "object",
    required: ["findings"],
    properties: {
      findings: { type: "array", items: { type: "object" } },
    },
  };
  const prompt = [
    specialistHeader({
      role: "lens-gap",
      run_id: "run-2026-05-29-coherence",
      state_id: "dispatch_lens_specialists",
    }),
    briefBlock({ brief: { plan_path: "/tmp/plan.md", lens: "gap" } }),
    outputContractBlock({
      outputs_path: "/tmp/run-2026-05-29-coherence/workers/lens-gap.out.json",
      response_schema: schema,
    }),
    forbiddenPathsNotice({ run_dir: "/tmp/run-2026-05-29-coherence" }),
  ].join("\n\n");

  // Every key element survives composition.
  assert.match(prompt, /Specialist dispatch: lens-gap/);
  assert.match(prompt, /run-2026-05-29-coherence/);
  assert.match(prompt, /## Brief/);
  assert.match(prompt, /plan_path/);
  assert.match(prompt, /## Output contract/);
  assert.match(prompt, /lens-gap\.out\.json/);
  assert.match(prompt, /## Forbidden write paths/);
  assert.match(prompt, /run-2026-05-29-coherence\/workers\//);

  // The four section headings appear in the order the caller composed them.
  const headerIdx = prompt.indexOf("# Specialist dispatch");
  const briefIdx = prompt.indexOf("## Brief");
  const contractIdx = prompt.indexOf("## Output contract");
  const forbiddenIdx = prompt.indexOf("## Forbidden write paths");
  assert.ok(headerIdx >= 0 && briefIdx > headerIdx, "brief follows header");
  assert.ok(contractIdx > briefIdx, "contract follows brief");
  assert.ok(forbiddenIdx > contractIdx, "forbidden notice follows contract");

  // The pretty-printed schema survives composition unchanged.
  assert.ok(prompt.includes(JSON.stringify(schema, null, 2)));
});
