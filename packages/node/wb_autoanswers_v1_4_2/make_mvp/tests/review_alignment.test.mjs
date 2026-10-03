import assert from "node:assert/strict";
import test from "node:test";
import {applyRouteGuards, assertGuardInvariants} from "../scripts/route_guard.mjs";
import {runDraftGuard} from "../scripts/draft_guard.mjs";

function classification(code, excerpt, route = "public_only", options = {}) {
  return {
    review_mode: "negative",
    route,
    route_reason: "synthetic review",
    primary_issue: code,
    primary_issue_subtype: null,
    issues: [{code, evidence: [{excerpt}]}],
    positive_signals: [],
    required_evidence: [],
    seller_investigation_subject: route === "seller_chat",
    evidence_potential: route === "seller_chat",
    ...options
  };
}

function guarded(input) {
  const result = applyRouteGuards(input).classification;
  assert.deepEqual(assertGuardInvariants(result), []);
  return result;
}

test("installation damage returns without the retired guarantee video", () => {
  const result = guarded(classification("INSTALL_BREAKAGE", "Стекло треснуло, пока устанавливал", "seller_chat", {
    required_evidence: ["installation_video_required"]
  }));
  assert.equal(result.route, "wb_return");
  assert.deepEqual(result.required_evidence, []);
  assert.equal(guarded(classification("INSTALL_BREAKAGE", "Стекло треснуло при установке, потом уже пользовался телефоном", "seller_chat")).route, "wb_return");
  assert.equal(guarded(classification("INSTALL_BREAKAGE", "Стекло треснуло прямо в процессе наклейки", "seller_chat")).route, "wb_return");
  assert.equal(guarded(classification("INSTALL_BREAKAGE", "При наклеивании стекло треснуло", "seller_chat")).route, "wb_return");
  assert.equal(guarded(classification("INSTALL_BREAKAGE", "В процессе наклеивания появилась трещина", "seller_chat")).route, "wb_return");
  assert.equal(guarded(classification("INSTALL_BREAKAGE", "При первой наклейке трещин не было, но при повторной наклейке стекло треснуло", "seller_chat")).route, "wb_return");
  assert.notEqual(guarded(classification("INSTALL_BREAKAGE", "При первой наклейке трещин не было", "seller_chat")).route, "wb_return");
});

test("use stage, not elapsed hours, separates crack routes", () => {
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Неделю пользовался, стекло треснуло", "wb_return")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "При первом осмотре после установки до начала использования заметил трещину", "wb_return")).route, "wb_return");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Стекло треснуло сразу после установки")).route, "seller_chat");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "При установке стекло не треснуло, через неделю использования появилась трещина")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Неделю пользовался телефоном, чехлом не пользовался, стекло треснуло")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "При первом осмотре до использования всё было целым, через неделю появилась трещина")).route, "seller_chat");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "При первом осмотре до использования всё было целым, через неделю использования появилась трещина")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "При установке трещин не было, через неделю использования появилась трещина", "wb_return")).route, "public_only");
  assert.notEqual(guarded(classification("INSTALL_BREAKAGE", "При установке трещин не было, через неделю использования появилась трещина", "seller_chat")).route, "wb_return");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "При использовании на стекле появилась трещина", "seller_chat")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Стекло треснуло после 5 минут использования", "seller_chat")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Стекло треснуло в течении 24 часов использования телефона", "seller_chat")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Треснуло при использование телефона", "seller_chat")).route, "public_only");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Стекло треснуло после наклейки, до начала эксплуатации телефона", "public_only")).route, "seller_chat");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Стекло треснуло после наклейки, до начала использования телефона", "public_only")).route, "seller_chat");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Треснуло спустя сутки, этап использования не описан", "seller_chat")).route, "seller_chat");
  assert.equal(guarded(classification("SPONTANEOUS_BREAKAGE", "Стекло пришло с трещиной в углу", "seller_chat")).route, "wb_return");
});

test("one public bubble answer changes only after a failed remedy", () => {
  assert.equal(guarded(classification("BUBBLES_DUST", "После установки остались мелкие воздушные пузырьки без пыли", "seller_chat")).route, "public_only");
  assert.equal(guarded(classification("BUBBLES_DUST", "Стекло всё в пузырьках, делал всё по инструкции", "wb_return")).route, "public_only");
  assert.equal(guarded(classification("BUBBLES_DUST", "Пробовал приподнимать и разглаживать, сутки прошли, пузыри остались")).route, "wb_return");
  assert.equal(guarded(classification("BUBBLES_DUST", "Не пробовал приподнимать, пузыри остались")).route, "public_only");
  assert.equal(guarded(classification("BUBBLES_DUST", "Не пробовал приподнимать, но разглаживал, пузыри остались")).route, "wb_return");
  assert.equal(guarded(classification("BUBBLES_DUST", "Сначала не пробовал разглаживать, потом попробовал, пузыри остались")).route, "wb_return");
  assert.equal(guarded(classification("BUBBLES_DUST", "Попытался выдавить пузыри, не получается", "public_only")).route, "wb_return");
  assert.equal(guarded(classification("BUBBLES_DUST", "Как бы не пыталсч, всё равно пузырь не разглаживался", "public_only")).route, "wb_return");
  assert.equal(guarded(classification("BUBBLES_DUST", "Не разглаживал пузырь, он остался", "public_only")).route, "public_only");
  assert.equal(guarded(classification("ADHESION", "Стекло вообще не фиксируется, сколько ни пытайся, ничего не получилось", "public_only")).route, "wb_return");
  const independentTouch = classification("BUBBLES_DUST", "После установки остались воздушные пузыри без пыли", "wb_return", {
    issues: [
      {code: "BUBBLES_DUST", evidence: [{excerpt: "После установки остались воздушные пузыри без пыли"}]},
      {code: "TOUCH_SENSITIVITY", evidence: [{excerpt: "После очистки сенсор всё равно пропускает касания"}]}
    ]
  });
  assert.equal(guarded(independentTouch).route, "wb_return");
  const mixedMechanism = classification("SIZE_FIT", "Размер стекла не подошёл", "seller_chat", {
    issues: [
      {code: "SIZE_FIT", evidence: [{excerpt: "Размер стекла не подошёл"}]},
      {code: "INSTALL_MECHANISM", evidence: [{excerpt: "Механизм установки не работает корректно"}]}
    ]
  });
  assert.equal(guarded(mixedMechanism).route, "wb_return");
  const intactMechanism = classification("SIZE_FIT", "Стекло слишком маленькое", "seller_chat", {
    issues: [
      {code: "SIZE_FIT", evidence: [{excerpt: "Стекло слишком маленькое"}]},
      {code: "INSTALL_MECHANISM", evidence: [{excerpt: "Механизм не сломан, просто стекло слишком маленькое"}]}
    ]
  });
  assert.notEqual(guarded(intactMechanism).route, "wb_return");
});

test("discarded glass cannot receive another installation remedy", () => {
  assert.equal(guarded(classification("ADHESION", "Стекло не приклеилось. Оба выбросил", "public_only")).route, "wb_return");
  assert.equal(guarded(classification("ADHESION", "Стекло не приклеилось, но выбросил только упаковку", "public_only")).route, "public_only");
  assert.equal(guarded(classification("ADHESION", "Стекло не приклеилось. Я не выбросил стекло, могу разгладить край", "public_only")).route, "public_only");
  assert.equal(guarded(classification("ADHESION", "Стекло не приклеилось. Если не получится разгладить, придётся выбросить стекло", "public_only")).route, "public_only");
});

test("cleaning consumables do not hide a missing glass or another defect", () => {
  assert.equal(guarded(classification("MISSING_PARTS", "До установки нет стикеров, тряпочка из комплекта есть", "wb_return")).route, "public_only");
  assert.equal(guarded(classification("MISSING_PARTS", "В комплекте не было самого стекла", "public_only")).route, "wb_return");
  const sensor = classification("TOUCH_SENSITIVITY", "Сенсор не работает после очистки", "wb_return", {
    issues: [
      {code: "TOUCH_SENSITIVITY", evidence: [{excerpt: "Сенсор не работает после очистки"}]},
      {code: "MISSING_PARTS", evidence: [{excerpt: "Нет стикеров"}]}
    ],
    required_evidence: ["touch_issue_video"]
  });
  const result = guarded(sensor);
  assert.equal(result.route, "wb_return");
  assert.deepEqual(result.required_evidence, []);
  assert.equal(guarded(classification("BUBBLES_DUST", "Под уже наклеенным стеклом пылинка. Стикеров нет.", "seller_chat")).route, "wb_return");
  assert.equal(guarded(classification("BUBBLES_DUST", "Не положили стикер от пыли, под стеклом осталась пылинка", "public_only")).route, "wb_return");
  assert.equal(guarded(classification("MISSING_PARTS", "До установки не положили стикер от пыли, тряпочка есть", "wb_return")).route, "public_only");
});

test("unknown model uses one chat invitation while established mismatch can return", () => {
  assert.equal(guarded(classification("SIZE_FIT", "Заказала стекло, не подошло к телефону", "wb_return")).route, "seller_chat");
  assert.equal(guarded(classification("SIZE_FIT", "Стекло для iPhone 17, телефон iPhone 17, размер не подходит", "wb_return")).route, "wb_return");
  assert.equal(guarded(classification("SIZE_FIT", "Перепутала, взяла на Про Макс, а у меня просто Про и не подошло", "seller_chat")).route, "public_only");
  assert.equal(guarded(classification("EARPIECE", "Стекло хорошее, но область динамика и Айленд не совпадает", "public_only")).route, "seller_chat");
  assert.equal(guarded(classification("SIZE_FIT", "Вырез динамика не совпадает, но звук нормальный. Модель телефона не указана", "public_only")).route, "seller_chat");
  assert.equal(guarded(classification("EARPIECE", "После установки разговорный динамик не слышно", "public_only")).route, "public_only");
  assert.equal(guarded(classification("SIZE_FIT", "Заказал для iPhone 17 Pro, на iPhone 17 Pro вырез не совпадает", "wb_return")).route, "wb_return");
});

test("raffle access gets a finished public answer route", () => {
  const result = guarded(classification("OTHER_SPECIFIC", "Не могу отправить заявку на розыгрыш", "seller_chat", {
    primary_issue_subtype: "promo_access_blocked"
  }));
  assert.equal(result.route, "public_only");
  assert.deepEqual(result.required_evidence, []);
});

test("draft guard rejects a public follow-up question and retired video requirement", () => {
  const writerRequest = {final_route: "public_only", case_code: null, classification: {issues: []}};
  const draft = {draft_reply: "Здравствуйте. Жаль, что появились пузыри. Напишите, пожалуйста, под отзывом, пробовали ли их разгладить.", requested_materials: []};
  assert.ok(runDraftGuard(draft, writerRequest).includes("PUBLIC_FOLLOWUP_QUESTION"));
  assert.ok(runDraftGuard({...draft, draft_reply: "Здравствуйте. Для возврата обязательно видео установки."}, writerRequest).includes("RETIRED_INSTALLATION_GUARANTEE"));
});
