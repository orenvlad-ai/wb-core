import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import {allocateCaseCode} from "../scripts/case_code.mjs";
import {calculateJobCost} from "../scripts/cost_accounting.mjs";
import {mvpRoot} from "./fixture_runtime.mjs";

const readJson = async (name) => JSON.parse(await readFile(path.join(mvpRoot, name), "utf8"));

test("frozen bundle manifest matches every packaged byte", async () => {
  const manifest = await readJson("bundle_manifest.json");
  assert.equal(manifest.bundle_version, "1.4.2");
  assert.equal(manifest.evaluation_signature, "sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da");
  assert.equal(Object.keys(manifest.artifacts).length, manifest.artifact_count);
  for (const [relative, expected] of Object.entries(manifest.artifacts)) {
    const actual = `sha256:${createHash("sha256").update(await readFile(path.join(mvpRoot, "frozen_bundle", relative))).digest("hex")}`;
    assert.equal(actual, expected, relative);
  }
});

test("build map is explicitly non-importable and contains no Wildberries operation", async () => {
  const map = await readJson("blueprint_build_map.json");
  assert.equal(map.artifact_type, "verified_build_map_not_importable_blueprint");
  const modules = [...map.scenario_telegram_collector.modules, ...map.scenario_scheduled_worker.modules];
  assert.equal(modules.some((module) => /wildberries/iu.test(`${module.app} ${module.operation}`)), false);
  assert.equal(map.frozen.max_rewrites, 2);
});

test("connection example contains names only and all required stores exist", async () => {
  const connections = await readJson("connections.example.json");
  assert.deepEqual(Object.keys(connections).sort(), ["required_connection_names", "required_environment_names"]);
  assert.deepEqual(connections.required_connection_names, ["telegram_bot_mvp", "openai_responses_mvp"]);
  const stores = await readJson("data_stores.json");
  const names = new Set(stores.stores.map((item) => item.name));
  for (const required of ["wb_review_jobs", "wb_case_codes", "wb_ai_audit", "wb_recent_replies"]) {
    assert.ok(names.has(required), required);
  }
});

test("case code allocator is seller_chat-only, idempotent, and collision-safe", () => {
  const base = {reviewId: "review", reviewVersion: "1", idempotencyKey: "review|1|1.4.2"};
  assert.equal(allocateCaseCode({...base, finalRoute: "public_only", existing: []}), null);
  const first = allocateCaseCode({...base, finalRoute: "seller_chat", existing: []});
  assert.match(first, /^[А-ЯЁ][0-9]{4}$/u);
  const existing = [{case_code: first, idempotency_key: base.idempotencyKey, active: true}];
  assert.equal(allocateCaseCode({...base, finalRoute: "seller_chat", existing}), first);
  const collided = allocateCaseCode({...base, idempotencyKey: "other", finalRoute: "seller_chat", existing});
  assert.notEqual(collided, first);
});

test("cost accounting separates cached and cache-write tokens", async () => {
  const pricing = await readJson("frozen_bundle/pricing_profiles/terra.json");
  const trace = [{
    role: "classifier",
    usage_reported: true,
    usage: {
      input_tokens: 1000,
      input_tokens_details: {cached_tokens: 600, cache_write_tokens: 100},
      output_tokens: 200,
      output_tokens_details: {reasoning_tokens: 50},
      total_tokens: 1200
    }
  }];
  const result = calculateJobCost(trace, pricing);
  assert.equal(result.usage.cached_input_tokens, 600);
  assert.equal(result.usage.cache_write_input_tokens, 100);
  assert.equal(result.usage.reasoning_tokens, 50);
  assert.equal(result.estimated_cost_usd, 0.0042125);
});
