import assert from "node:assert/strict";
import test from "node:test";
import {isUnspecifiedMissingKit, missingKitReplacement, MISSING_KIT_POLICY} from "../scripts/missing_kit_policy.mjs";
import {runJob} from "../scripts/orchestrator.mjs";
import {MemoryStore} from "../scripts/memory_store.mjs";
import {createFixtureRunner, loadScenarios} from "./fixture_runtime.mjs";

const raw = {ingestion_id: "kit-only", review_id: "kit-only", review_version: "1",
  rating: 1, text: "", pros: "", cons: "", wb_tags: ["брак", "плохое качество", "неполный комплект"],
  media: {photos: [], video: {present: false}}};

test("tag-only complaint uses guarded clarification with no model calls and stable code", async () => {
  for (const rating of [1, 2, 3, 4, 5]) {
    const store = new MemoryStore();
    const input = {...raw, rating};
    const dependencies = {store, roleRunner: () => { throw new Error("MODEL_MUST_NOT_RUN"); }};
    const result = await runJob(input, dependencies);
    assert.equal(result.result.route, "seller_chat");
    assert.equal(result.result.outcome, "ready");
    assert.equal(result.model_calls_this_run, 0);
    assert.equal(result.estimated_cost_usd, 0);
    assert.match(result.result.route_reason, new RegExp(MISSING_KIT_POLICY));
    assert.match(result.result.case_code, /^[А-ЯЁ][0-9]{4}$/u);
    assert.match(result.result.final_reply, /уточните, чего не хватило/);
    assert.doesNotMatch(result.result.final_reply, /возврат|фото|видео|вы получили неполный/iu);
    const replay = await runJob(input, dependencies);
    assert.equal(replay.idempotent_reuse, true);
    assert.equal(replay.result.case_code, result.result.case_code);
    assert.equal((await store.listCaseCodes()).length, 1);
  }
});

test("details, media, unknown tags and general quality-only complaints retain ordinary rules", async () => {
  for (const field of ["text", "pros", "cons"]) {
    for (const text of ["Самого стекла не было", "Нет салфетки", "Стекло разбито", "Порезала руку"]) {
      assert.equal(isUnspecifiedMissingKit({...raw, [field]: text}, raw.media), false);
    }
  }
  for (const media of [{photos: [{}]}, {video: {present: true}}, {status: "fetch_failed"}, {video: {frame_refs: ["frame"]}}]) {
    assert.equal(isUnspecifiedMissingKit(raw, media), false);
  }
  for (const tags of [[], ["брак"], ["плохое качество"], ["неполный комплект", "порезала руку"], ["неполный комплект", ""]]) {
    assert.equal(isUnspecifiedMissingKit({...raw, wb_tags: tags}), false);
  }
  const scenarios = await loadScenarios();
  const scenario = structuredClone(scenarios.wb_return);
  scenario.raw.wb_tags = raw.wb_tags;
  const result = await runJob(scenario.raw, {store: new MemoryStore(), roleRunner: createFixtureRunner(scenario)});
  assert.equal(result.result.route, "wb_return");
  assert.ok(result.model_calls_this_run > 0);
});

test("replacement checks scope and respects occupied case codes", () => {
  const args = {review: raw, media: raw.media, processingKey: "kit-only|1|1.4.2"};
  const first = missingKitReplacement(args);
  const second = missingKitReplacement({...args, existing: [{idempotency_key: "other", case_code: first.case_code}]});
  assert.notEqual(first.case_code, second.case_code);
  assert.equal(missingKitReplacement({...args, existing: [{idempotency_key: args.processingKey, case_code: second.case_code}]}).case_code, second.case_code);
  assert.throws(() => missingKitReplacement({...args, review: {...raw, text: "Стекла нет"}}), /NOT_APPLICABLE/);
});
