// Owner-authorized rule; the frozen 1.4.2 bundle remains byte-for-byte unchanged.
import {allocateCaseCode, assertCaseCode} from "./case_code.mjs";
import {runDraftGuard} from "./draft_guard.mjs";
export const RETURN_QUESTION_POLICY = "generic-return-question-2026-10-08-v1";

// A bounded grammar consumes the entire content. No money keyword or substring
// is evidence of an existing return; extra facts leave the frozen routing intact.
const POLITE = "(?:подскажите(?:[, ]+пожалуйста)?[, ]*|пожалуйста[, ]*)?";
const RETURN_OBJECT = "(?:возврат(?:\\s+товара)?|(?:заявку|заявление)\\s+на\\s+возврат(?:\\s+товара)?)";
const ACTION = `(?:(?:оформить|сделать)\\s+${RETURN_OBJECT}|вернуть\\s+(?:товар|деньги|денежные\\s+средства)(?:\\s+за\\s+товар)?)`;
const QUESTION = `(?:как(?:\\s+(?:мне|можно)){0,2}\\s+${ACTION}|(?:можно|могу)(?:\\s+(?:ли|я|мне)){0,2}\\s+${ACTION}|${RETURN_OBJECT}\\s+как(?:\\s+(?:мне|можно)){0,2}\\s+(?:оформить|сделать)|хочу\\s+${ACTION}(?:[.,]?\\s*${POLITE}как)?)`;
const GENERIC_RETURN = new RegExp(`^(?:здравствуйте[.,!]?\\s*)?${POLITE}${QUESTION}[?.!]*(?:\\s+пожалуйста[?.!]*)?$`, "iu");

export function isGenericReturnQuestion(review, media = {}) {
  const text = [review.text, review.pros, review.cons]
    .map(x => String(x || "").trim()).filter(Boolean).join(" ").replace(/\s+/gu, " ");
  if ((review.wb_tags || []).length || (media.photos || []).length || media.video?.present
    || (media.status && media.status !== "none")) return false;
  return GENERIC_RETURN.test(text);
}

export function applyReturnQuestionPolicy(classification, reviewInput) {
  if (!isGenericReturnQuestion(reviewInput.review, reviewInput.media)
    || classification.media_status !== "none"
    || (classification.risk_flags || []).length
    || (classification.issues || []).some(x => !["WB_REFUND_STATUS", "OTHER_SPECIFIC"].includes(x.code))) return classification;
  const corrected = structuredClone(classification);
  corrected.issues = [{code: "OTHER_SPECIFIC", confidence: 1, evidence: [{source_type: "review_text", source_ref: "review.text", excerpt: reviewInput.review.normalized_text, observed_fact: "Общий вопрос о возврате без описанной причины или текущего статуса"}]}];
  corrected.primary_issue = "OTHER_SPECIFIC";
  corrected.primary_issue_subtype = null;
  corrected.route = "seller_chat";
  corrected.route_reason = `Общий вопрос о возврате сначала обсуждается с продавцом [${RETURN_QUESTION_POLICY}]`;
  corrected.seller_investigation_subject = true;
  corrected.evidence_potential = true;
  corrected.required_evidence = [];
  return corrected;
}

export function returnQuestionReplacement({review, media = {}, processingKey, existing = []}) {
  if (!isGenericReturnQuestion(review, media)) throw new Error("RETURN_QUESTION_POLICY_NOT_APPLICABLE");
  const code = allocateCaseCode({finalRoute: "seller_chat", reviewId: review.review_id, reviewVersion: review.review_version, idempotencyKey: processingKey, existing});
  assertCaseCode("seller_chat", code);
  const reply = `Здравствуйте. Напишите, пожалуйста, в чат с продавцом — мы постараемся разобраться в вашей ситуации. Код обращения: ${code}.`;
  const draft = {route: "seller_chat", case_code: code, draft_reply: reply, applied_cta: "seller_chat", requested_materials: []};
  const errors = runDraftGuard(draft, {final_route: "seller_chat", case_code: code});
  if (errors.length) throw new Error(`RETURN_QUESTION_GUARD:${errors.join("|")}`);
  return {policy_version: RETURN_QUESTION_POLICY, final_route: "seller_chat", case_code: code, final_reply: reply, hard_gates_passed: true, node_contract_valid: true, fallback_used: false, media_uncertain: false};
}
