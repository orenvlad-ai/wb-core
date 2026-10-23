#!/usr/bin/env node
/** Versioned stdin/stdout JSON boundary around the untouched frozen v1.4.2 pipeline. */

import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {runJob} from "../wb_autoanswers_v1_4_2/make_mvp/scripts/orchestrator.mjs";
import {calculateJobCost, usageRecord} from "../wb_autoanswers_v1_4_2/make_mvp/scripts/cost_accounting.mjs";
import {MemoryStore} from "../wb_autoanswers_v1_4_2/make_mvp/scripts/memory_store.mjs";
import {runDraftGuard} from "../wb_autoanswers_v1_4_2/make_mvp/scripts/draft_guard.mjs";
import {assertContract} from "../wb_autoanswers_v1_4_2/make_mvp/scripts/schema_validation.mjs";
import {
  EVALUATION_SIGNATURE,
  PROMPT_BUNDLE_VERSION
} from "../wb_autoanswers_v1_4_2/make_mvp/scripts/constants.mjs";
import {
  createFixtureRunner,
  loadScenarios
} from "../wb_autoanswers_v1_4_2/make_mvp/tests/fixture_runtime.mjs";

const BOUNDARY_VERSION = "wb_autoanswers_node_boundary_v1";
const here = path.dirname(fileURLToPath(import.meta.url));
const mvpRoot = path.resolve(here, "../wb_autoanswers_v1_4_2/make_mvp");

function fail(code, message) {
  const error = new Error(message);
  error.code = code;
  throw error;
}

function assertEnvelope(envelope) {
  if (!envelope || typeof envelope !== "object" || Array.isArray(envelope)) fail("INVALID_ENVELOPE", "object required");
  if (envelope.boundary_version !== BOUNDARY_VERSION) fail("BOUNDARY_VERSION_MISMATCH", "unsupported boundary_version");
  if (!envelope.operation || !["verify", "run", "guard_final"].includes(envelope.operation)) fail("INVALID_OPERATION", "operation must be verify, run or guard_final");
  if (envelope.operation === "run") {
    if (!envelope.raw_input || typeof envelope.raw_input !== "object") fail("INVALID_RAW_INPUT", "raw_input object required");
    if (!envelope.processing_key) fail("PROCESSING_KEY_REQUIRED", "processing_key required");
    if (!["fixture", "live"].includes(envelope.execution_mode)) fail("INVALID_EXECUTION_MODE", "execution_mode must be fixture or live");
  }
  if (envelope.operation === "guard_final") {
    const input = envelope.guard_input;
    if (!input || typeof input !== "object" || Array.isArray(input)) fail("INVALID_GUARD_INPUT", "guard_input object required");
    for (const key of ["review_id", "review_version", "route", "reply"]) {
      if (!String(input[key] ?? "").trim()) fail("INVALID_GUARD_INPUT", `${key} required`);
    }
  }
}

const CTA_BY_ROUTE = Object.freeze({
  public_only: "none",
  seller_chat: "seller_chat",
  wb_return: "wb_return",
  wb_support: "wb_support"
});

async function guardFinal(input) {
  const draft = {
    schema_version: "1.0.0",
    review_id: String(input.review_id),
    review_version: String(input.review_version),
    route: String(input.route),
    case_code: input.case_code || null,
    draft_reply: String(input.reply),
    covered_issue_codes: input.primary_issue ? [String(input.primary_issue)] : [],
    covered_positive_codes: [],
    used_fact_ids: [],
    applied_cta: CTA_BY_ROUTE[String(input.route)],
    requested_materials: []
  };
  await assertContract("draft_reply.schema.json", draft);
  const writerRequest = {
    final_route: String(input.route),
    case_code: input.case_code || null,
    classification: {
      issues: input.primary_issue ? [{code: String(input.primary_issue)}] : []
    }
  };
  const errors = runDraftGuard(draft, writerRequest);
  return {passed: errors.length === 0, errors, reply: draft.draft_reply};
}

async function verifyFrozenBundle() {
  const manifest = JSON.parse(await readFile(path.join(mvpRoot, "bundle_manifest.json"), "utf8"));
  if (manifest.bundle_version !== PROMPT_BUNDLE_VERSION || manifest.evaluation_signature !== EVALUATION_SIGNATURE) {
    fail("FROZEN_IDENTITY_MISMATCH", "bundle identity mismatch");
  }
  const mismatches = [];
  for (const [relative, expected] of Object.entries(manifest.artifacts || {})) {
    const body = await readFile(path.join(mvpRoot, "frozen_bundle", relative));
    const actual = `sha256:${createHash("sha256").update(body).digest("hex")}`;
    if (actual !== expected) mismatches.push(relative);
  }
  if (mismatches.length) fail("FROZEN_HASH_MISMATCH", mismatches.join(","));
  return {artifact_count: Object.keys(manifest.artifacts || {}).length};
}

function outputText(response) {
  if (typeof response.output_text === "string" && response.output_text.trim()) return response.output_text;
  for (const item of response.output || []) {
    for (const content of item?.content || []) {
      if (typeof content?.text === "string" && content.text.trim()) return content.text;
    }
  }
  fail("OPENAI_OUTPUT_MISSING", "Responses API output text missing");
}

function liveRoleRunner(observedTrace) {
  const apiKey = String(process.env.OPENAI_API_KEY || "").trim();
  if (!apiKey) fail("OPENAI_API_KEY_MISSING", "OPENAI_API_KEY is not configured");
  const baseUrl = String(process.env.OPENAI_RESPONSES_BASE_URL || "https://api.openai.com/v1").replace(/\/+$/u, "");
  return async ({role, payload}) => {
    const started = Date.now();
    const response = await fetch(`${baseUrl}/responses`, {
      method: "POST",
      headers: {Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json"},
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(120000)
    });
    const body = await response.json().catch(() => null);
    if (body?.usage) {
      observedTrace.push(usageRecord({
        role,
        responseId: body.id || null,
        usage: body.usage,
        latencyMs: Date.now() - started
      }));
    }
    if (!response.ok || !body) {
      const providerCode = String(body?.error?.code || "");
      if (providerCode === "insufficient_quota") fail("OPENAI_INSUFFICIENT_QUOTA", "Responses API quota is exhausted");
      fail(`OPENAI_HTTP_${response.status}`, `Responses API HTTP ${response.status}`);
    }
    let parsed;
    try {
      parsed = JSON.parse(outputText(body));
    } catch (error) {
      fail("OPENAI_OUTPUT_NOT_JSON", error.message);
    }
    return {
      output: parsed,
      usage: body.usage || null,
      response_id: body.id || null,
      latency_ms: Date.now() - started
    };
  };
}

async function fixtureRoleRunner(scenarioId) {
  if (process.env.WB_AUTOANSWERS_TEST_MODE !== "1") fail("FIXTURE_MODE_BLOCKED", "fixture mode is test-only");
  const scenarios = await loadScenarios();
  const scenario = scenarios[String(scenarioId || "")];
  if (!scenario) fail("FIXTURE_NOT_FOUND", "unknown fixture scenario");
  return createFixtureRunner(scenario);
}

async function execute(envelope) {
  assertEnvelope(envelope);
  const verified = await verifyFrozenBundle();
  if (envelope.operation === "verify") return {verified};
  if (envelope.operation === "guard_final") return {verified, guard: await guardFinal(envelope.guard_input)};
  const store = new MemoryStore();
  const observedTrace = [];
  const roleRunner = envelope.execution_mode === "fixture"
    ? await fixtureRoleRunner(envelope.fixture_scenario)
    : liveRoleRunner(observedTrace);
  let result;
  try {
    result = await runJob(envelope.raw_input, {store, roleRunner});
  } catch (error) {
    if (observedTrace.length) {
      const pricingProfile = JSON.parse(await readFile(path.join(mvpRoot, "pricing_profiles/terra.json"), "utf8"));
      const partial = calculateJobCost(observedTrace, pricingProfile);
      error.partialUsage = partial.usage;
      error.partialCostUsd = partial.estimated_cost_usd;
      error.partialRoleCalls = observedTrace.length;
    }
    throw error;
  }
  return {
    processing_key: envelope.processing_key,
    pipeline: result,
    audit: await store.getAudit(result.idempotency_key),
    verified
  };
}

async function main() {
  try {
    const chunks = [];
    for await (const chunk of process.stdin) chunks.push(chunk);
    const envelope = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const data = await execute(envelope);
    process.stdout.write(JSON.stringify({
      boundary_version: BOUNDARY_VERSION,
      bundle_version: PROMPT_BUNDLE_VERSION,
      evaluation_signature: EVALUATION_SIGNATURE,
      ok: true,
      data
    }));
  } catch (error) {
    process.stdout.write(JSON.stringify({
      boundary_version: BOUNDARY_VERSION,
      bundle_version: PROMPT_BUNDLE_VERSION,
      evaluation_signature: EVALUATION_SIGNATURE,
      ok: false,
      error: {
        code: error.code || "NODE_BOUNDARY_ERROR",
        message: String(error.message || error),
        partial_usage: error.partialUsage || null,
        partial_cost_usd: Number(error.partialCostUsd || 0),
        partial_role_calls: Number(error.partialRoleCalls || 0)
      }
    }));
    process.exitCode = 1;
  }
}

await main();
