import assert from "node:assert/strict";
import test from "node:test";
import {runJob} from "../scripts/orchestrator.mjs";
import {MemoryStore} from "../scripts/memory_store.mjs";
import {createFixtureRunner, loadScenarios} from "./fixture_runtime.mjs";
import {noframeGapCandidate, useNoframeGapReply, NOFRAME_GAP_POLICY, NOFRAME_GAP_REPLY} from "../scripts/noframe_gap_policy.mjs";

const scenarios = await loadScenarios();
function scenario(text = "Стекло не по размеру, меньше экрана") {
  const s = structuredClone(scenarios.public_only);
  s.raw = {...s.raw, seller_article: "No Frame Clean iPhone 15 / 16", nm_id: null, text};
  s.classification = {...s.classification, primary_issue: "SIZE_FIT", issue_codes: ["SIZE_FIT"]};
  return s;
}
function markedRunner(s, observe = () => {}) {
  const runner = createFixtureRunner(s, observe);
  return async args => {
    const response = await runner(args);
    if (args.role === "classifier") response.output.route_reason = `Предусмотренный отступ [${NOFRAME_GAP_POLICY}]`;
    return response;
  };
}

test("sole gap complaint uses approved public text, one paid classification and stable replay", async () => {
  for (const article of ["No Frame Clean iPhone 15 / 16", "No Frame Matte iPhone 17 Pro", "No Frame Anti-Spy iPhone 14 / 13 / 13Pro"]) {
    for (const text of ["Стекло меньше экрана", "Стекло не по размеру", "Не доходит до краёв экрана, остаются зазоры"]) {
      const s = scenario(text); s.raw.seller_article = article;
      const store = new MemoryStore();
      const result = await runJob(s.raw, {store, roleRunner: markedRunner(s, ({role, payload}) => {
        assert.equal(role, "classifier");
        assert.ok(payload.instructions.includes(NOFRAME_GAP_POLICY));
      })});
      assert.equal(result.result.route, "public_only");
      assert.equal(result.result.final_reply, NOFRAME_GAP_REPLY);
      assert.match(result.result.final_reply, /No Frame \(без рамок\)/);
      assert.equal(result.result.case_code, null);
      assert.equal(result.model_calls_this_run, 1);
      assert.ok(result.estimated_cost_usd > 0);
      assert.equal(result.result.outcome, "ready");
      assert.equal((await store.listCaseCodes()).length, 0);
      const again = await runJob(s.raw, {store, roleRunner: () => { throw new Error("NO_REPEAT_CALL"); }});
      assert.equal(again.idempotent_reuse, true);
      assert.equal(again.result.final_reply, NOFRAME_GAP_REPLY);
    }
  }
});

test("other products and explicit defects cannot receive the gap template even with a mistaken model marker", async () => {
  let input, classification;
  const s = scenario();
  await runJob(s.raw, {roleRunner: async args => {
    const response = await markedRunner(s)(args);
    input = args.request.review_input;
    classification = response.output;
    return response;
  }});
  assert.equal(useNoframeGapReply(input, classification), true);
  for (const article of [null, "Clean iPhone 15", "No Frame Other iPhone 15"]) {
    assert.equal(noframeGapCandidate({...input, product: {...input.product, seller_article: article}}), false);
  }
  for (const field of ["text", "pros", "cons"]) {
    for (const text of ["Пришло не то стекло", "Вырез камеры не совпадает", "Стекло криво наклеилось", "Есть пузыри", "Треснуло и порезало палец", "Не работает сенсор", "Отступ 5 мм", "Вместо iPhone 15 прислали для iPhone 14"]) {
      const changed = structuredClone(input); changed.review[field] = `${input.review.text}. ${text}`;
      assert.equal(useNoframeGapReply(changed, classification), false, `${field}: ${text}`);
    }
  }
  for (const patch of [{risk_flags: ["safety"]}, {route: "wb_return"}, {classification_confidence: 0.6},
    {issues: [{code: "SIZE_FIT"}, {code: "ADHESION"}]}, {route_reason: "General size complaint"},
    {review_mode: "positive"}, {seller_investigation_subject: true}]) {
    assert.equal(useNoframeGapReply(input, {...classification, ...patch}), false);
  }
  for (const patch of [{status: "unclear"}, {photos: [{}]}, {video: {present: true}}]) {
    assert.equal(useNoframeGapReply({...input, media: {...input.media, ...patch}}, classification), false);
  }
  assert.equal(useNoframeGapReply({...input, review: {...input.review, wb_tags: ["неполный комплект"]}}, classification), false);
});

test("wrong-model No Frame complaint keeps existing return route, and old line has no new instructions", async () => {
  const s = structuredClone(scenarios.wb_return);
  s.raw.seller_article = "No Frame Clean iPhone 15 / 16";
  const result = await runJob(s.raw, {roleRunner: createFixtureRunner(s)});
  assert.equal(result.result.route, "wb_return");
  assert.notEqual(result.result.final_reply, NOFRAME_GAP_REPLY);
  const old = structuredClone(scenarios.public_only);
  await runJob(old.raw, {roleRunner: createFixtureRunner(old, ({payload}) => {
    assert.ok(!payload.instructions.includes(NOFRAME_GAP_POLICY));
  })});
});
