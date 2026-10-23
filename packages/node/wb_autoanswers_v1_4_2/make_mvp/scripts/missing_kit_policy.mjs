// Owner-approved clarification for tag-only complaints, outside frozen 1.4.2.
import {allocateCaseCode, assertCaseCode} from "./case_code.mjs";
import {runDraftGuard} from "./draft_guard.mjs";

export const MISSING_KIT_POLICY = "unspecified-missing-kit-2026-10-23-v1";
const ALLOWED_TAGS = new Set(["неполный комплект", "брак", "плохое качество"]);

export function isUnspecifiedMissingKit(review, media = {}) {
  if (![1, 2, 3, 4, 5].includes(review.rating)
    || [review.text, review.pros, review.cons].some(x => String(x || "").trim())
    || !Array.isArray(review.wb_tags) || !review.wb_tags.length
    || (media.photos || []).length || media.video?.present
    || (media.video?.frame_refs || []).length
    || (media.status && media.status !== "none")) return false;
  const tags = review.wb_tags.map(x => String(x).normalize("NFKC").trim().toLowerCase());
  return tags.includes("неполный комплект") && tags.every(x => ALLOWED_TAGS.has(x));
}

export function missingKitReplacement({review, media = {}, processingKey, existing = []}) {
  if (!isUnspecifiedMissingKit(review, media)) throw new Error("MISSING_KIT_POLICY_NOT_APPLICABLE");
  const code = allocateCaseCode({finalRoute: "seller_chat", reviewId: review.review_id,
    reviewVersion: review.review_version, idempotencyKey: processingKey, existing});
  assertCaseCode("seller_chat", code);
  const reply = `Здравствуйте. Сожалеем, что покупка вас разочаровала. Напишите, пожалуйста, в чат с продавцом и уточните, чего не хватило в комплекте — постараемся разобраться. Код обращения: ${code}.`;
  const draft = {route: "seller_chat", case_code: code, draft_reply: reply,
    applied_cta: "seller_chat", requested_materials: []};
  const errors = runDraftGuard(draft, {final_route: "seller_chat", case_code: code});
  if (errors.length) throw new Error(`MISSING_KIT_GUARD:${errors.join("|")}`);
  return {policy_version: MISSING_KIT_POLICY, final_route: "seller_chat", case_code: code,
    final_reply: reply, hard_gates_passed: true, node_contract_valid: true,
    fallback_used: false, media_uncertain: false};
}
