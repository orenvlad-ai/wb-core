import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import {runDraftGuard} from "../scripts/draft_guard.mjs";
import {MemoryStore} from "../scripts/memory_store.mjs";
import {runJob} from "../scripts/orchestrator.mjs";
import {compileAllSchemas} from "../scripts/schema_validation.mjs";
import {createFixtureRunner, loadScenarios, mvpRoot} from "./fixture_runtime.mjs";

const scenarios = await loadScenarios();

test("all bundled runtime schemas compile", async () => {
  const ids = await compileAllSchemas();
  assert.equal(ids.length, 10);
  assert.equal(new Set(ids).size, ids.length);
});

for (const name of ["public_only", "seller_chat", "wb_return", "wb_support"]) {
  test(`normal path returns ${name} after exactly classifier, writer, validator`, async () => {
    const scenario = scenarios[name];
    const runner = createFixtureRunner(scenario);
    const delivered = [];
    const result = await runJob(scenario.raw, {
      roleRunner: runner,
      store: new MemoryStore(),
      deliverToManager: async (message) => delivered.push(message)
    });
    assert.deepEqual(runner.calls, ["classifier", "writer", "validator"]);
    assert.equal(result.model_calls_this_run, scenario.expected_calls);
    assert.equal(result.result.route, scenario.expected_route);
    assert.equal(result.result.outcome, "ready");
    assert.equal(result.state, "delivered_to_telegram");
    assert.equal(delivered.length, 1);
    assert.equal(delivered[0].text, result.result.final_reply);
  });
}

test("empty five-star review skips before every model role", async () => {
  const scenario = scenarios.empty_five_star;
  let called = false;
  const result = await runJob(scenario.raw, {
    store: new MemoryStore(),
    roleRunner: async () => { called = true; throw new Error("must not run"); }
  });
  assert.equal(called, false);
  assert.equal(result.model_calls_this_run, 0);
  assert.equal(result.result.publication_action, "skip");
  assert.equal(result.result.outcome, "skipped");
});

test("unknown SKU exposes only unknown line context and no invented SKU match", async () => {
  const scenario = scenarios.unknown_sku;
  let classifierChecked = false;
  const runner = createFixtureRunner(scenario, ({role, request}) => {
    if (role !== "classifier") return;
    classifierChecked = true;
    assert.equal(request.review_input.product.context_status, "unknown");
    assert.equal(request.review_input.product.line, "unknown");
    assert.equal("sku_match" in request.product_context, false);
    assert.deepEqual(request.product_context.line_contexts.unknown.confirmed_facts, []);
  });
  const result = await runJob(scenario.raw, {roleRunner: runner, store: new MemoryStore()});
  assert.equal(classifierChecked, true);
  assert.equal(result.result.route, "public_only");
});

test("idempotency reuses ready seller_chat result without calls or a second case code", async () => {
  const scenario = scenarios.seller_chat;
  const store = new MemoryStore();
  const firstRunner = createFixtureRunner(scenario);
  const first = await runJob(scenario.raw, {roleRunner: firstRunner, store});
  let repeatedCalls = 0;
  const second = await runJob(scenario.raw, {
    store,
    roleRunner: async () => { repeatedCalls += 1; throw new Error("must not run"); }
  });
  assert.equal(first.result.case_code, second.result.case_code);
  assert.equal(second.idempotent_reuse, true);
  assert.equal(second.model_calls_this_run, 0);
  assert.equal(repeatedCalls, 0);
  assert.equal((await store.listCaseCodes()).length, 1);
});

test("deterministic draft guard forces one rewrite while preserving route and case code", async () => {
  const scenario = scenarios.rewrite_then_pass;
  const runner = createFixtureRunner(scenario);
  const result = await runJob(scenario.raw, {roleRunner: runner, store: new MemoryStore()});
  assert.deepEqual(runner.calls, ["classifier", "writer", "validator", "rewrite", "validator"]);
  assert.equal(result.model_calls_this_run, 5);
  assert.equal(result.result.route, "seller_chat");
  assert.equal(result.result.outcome, "ready");
  assert.equal(result.result.draft_history.length, 2);
  assert.equal((result.result.final_reply.match(new RegExp(result.result.case_code, "gu")) || []).length, 1);
  assert.doesNotMatch(result.result.final_reply, /фото|видео|скриншот|этикетк|доказательств/iu);
});

test("two failed rewrites use only the approved same-route fallback", async () => {
  const scenario = scenarios.fallback_after_two;
  const runner = createFixtureRunner(scenario);
  const result = await runJob(scenario.raw, {roleRunner: runner, store: new MemoryStore()});
  assert.equal(result.model_calls_this_run, 7);
  assert.equal(runner.calls.filter((role) => role === "rewrite").length, 2);
  assert.equal(result.result.outcome, "fallback");
  assert.equal(result.fallback_id, scenario.expected_fallback_id);
  assert.equal(result.result.route, "seller_chat");
  assert.equal((result.result.final_reply.match(new RegExp(result.result.case_code, "gu")) || []).length, 1);
  assert.doesNotMatch(result.result.final_reply, /фото|видео|скриншот|этикетк|доказательств/iu);
});

test("frozen route guard corrects device damage to wb_return before writer", async () => {
  const scenario = scenarios.route_guard_return;
  let writerRoute = null;
  const runner = createFixtureRunner(scenario, ({role, request}) => {
    if (role === "writer") writerRoute = request.final_route;
  });
  const result = await runJob(scenario.raw, {roleRunner: runner, store: new MemoryStore()});
  assert.equal(writerRoute, "wb_return");
  assert.equal(result.result.route, "wb_return");
  assert.equal(result.result.case_code, null);
});

test("seller_chat draft guard blocks public requests for every prohibited material family", () => {
  const writerRequest = {
    final_route: "seller_chat",
    case_code: "К4827",
    classification: {issues: [{code: "SERVICE_GUARANTEE"}]}
  };
  for (const phrase of [
    "подготовьте фото",
    "пришлите видео",
    "приложите скриншоты",
    "возьмите этикетку",
    "доказательства пригодятся",
    "материалы не нужны"
  ]) {
    const draft = {
      route: "seller_chat",
      case_code: "К4827",
      applied_cta: "seller_chat",
      requested_materials: [],
      draft_reply: `Здравствуйте. Напишите нам в чат с продавцом, ${phrase} и укажите номер обращения К4827.`
    };
    assert.ok(runDraftGuard(draft, writerRequest).includes("SELLER_CHAT_PUBLIC_EVIDENCE_REQUEST"), phrase);
  }
});

test("draft guard rejects unauthorized outcome promises and cross-route CTA", () => {
  const classification = {issues: [{code: "WRONG_ITEM"}]};
  const promised = {
    route: "wb_return",
    case_code: null,
    applied_cta: "wb_return",
    requested_materials: [],
    draft_reply: "Здравствуйте. Оформите возврат через Wildberries, после чего деньги обязательно вернут."
  };
  assert.ok(runDraftGuard(promised, {final_route: "wb_return", case_code: null, classification}).includes("PROMISE_UNAUTHORIZED"));
  const wrongCta = {...promised, draft_reply: "Здравствуйте. Напишите нам в чат с продавцом."};
  assert.ok(runDraftGuard(wrongCta, {final_route: "wb_return", case_code: null, classification}).includes("CTA_MISMATCH"));
});

test("audit redacts signed media URL while retaining role outputs", async () => {
  const scenario = scenarios.seller_chat;
  const store = new MemoryStore();
  const result = await runJob(scenario.raw, {roleRunner: createFixtureRunner(scenario), store});
  const audit = await store.getAudit(result.idempotency_key);
  const serialized = JSON.stringify(audit);
  assert.doesNotMatch(serialized, /example\.invalid\/signed/iu);
  assert.match(serialized, /REDACTED/iu);
  assert.match(serialized, /role_call/iu);
});

test("fixture file remains parseable JSON", async () => {
  const raw = await readFile(path.join(mvpRoot, "fixtures", "scenarios.json"), "utf8");
  assert.doesNotThrow(() => JSON.parse(raw));
});
