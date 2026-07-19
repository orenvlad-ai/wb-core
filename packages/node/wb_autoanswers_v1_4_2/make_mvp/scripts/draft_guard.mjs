import {deterministicDraftChecks} from "../frozen_bundle/tools/draft_guard.mjs";

const CTA_BY_ROUTE = Object.freeze({
  public_only: "none",
  seller_chat: "seller_chat",
  wb_return: "wb_return",
  wb_support: "wb_support"
});

const EVIDENCE_WORDS = /(?:фото(?:граф(?:ию|ии|ий|ия|ии))?|видео(?:запись)?|скриншот(?:ы|а|ов)?|этикетк(?:у|и)|маркировк(?:у|и)|доказательств(?:о|а)?|материал(?:ы|ов)?)/iu;
const CHAT_CTA = /(?:напиш\w*.{0,45}(?:чат|продавц)|чат.{0,30}продавц)/iu;
const RETURN_CTA = /(?:оформ\w*.{0,35}возврат|заявк\w*.{0,25}(?:на\s+)?возврат)/iu;
const SUPPORT_CTA = /(?:поддержк\w*.{0,25}(?:wildberries|wb)|(?:wildberries|wb).{0,25}поддержк|раздел\w*.{0,20}поддержк)/iu;
const EXTRA_PROMISE = /(?:деньги.{0,25}(?:вернут|возвратят|будут\s+возвращены)|(?:мы\s+)?заменим|получите.{0,25}(?:деньги|компенсац|замен)|возврат.{0,25}(?:будет\s+)?одобр|(?:wb|wildberries).{0,25}одобрит)/iu;

/** Frozen guard plus orchestration invariants. It only adds stricter checks. */
export function runDraftGuard(draft, writerRequest) {
  const errors = [...deterministicDraftChecks(draft, writerRequest)];
  const finalRoute = writerRequest.final_route;
  if (draft.route !== finalRoute) errors.push("ROUTE_MISMATCH");
  if (draft.applied_cta !== CTA_BY_ROUTE[finalRoute]) errors.push("CTA_MISMATCH");
  if (draft.case_code !== writerRequest.case_code) errors.push("CASE_CODE_INVALID");
  if (EXTRA_PROMISE.test(draft.draft_reply)) errors.push("PROMISE_UNAUTHORIZED");

  const hasChatCta = CHAT_CTA.test(draft.draft_reply);
  const hasReturnCta = RETURN_CTA.test(draft.draft_reply);
  const hasSupportCta = SUPPORT_CTA.test(draft.draft_reply);
  if (finalRoute === "public_only" && (hasChatCta || hasReturnCta || hasSupportCta)) errors.push("CTA_MISMATCH");
  if (finalRoute === "seller_chat" && (hasReturnCta || hasSupportCta)) errors.push("MULTIPLE_ROUTES");
  if (finalRoute === "wb_return" && (!hasReturnCta || hasChatCta || hasSupportCta)) errors.push("CTA_MISMATCH");
  if (finalRoute === "wb_support" && (!hasSupportCta || hasChatCta || hasReturnCta)) errors.push("CTA_MISMATCH");

  if (finalRoute === "seller_chat") {
    if (!hasChatCta) {
      errors.push("SELLER_CHAT_INVITATION_MISSING");
    }
    if (EVIDENCE_WORDS.test(draft.draft_reply)) errors.push("SELLER_CHAT_PUBLIC_EVIDENCE_REQUEST");
    if ((draft.requested_materials || []).length !== 0) errors.push("SELLER_CHAT_PUBLIC_EVIDENCE_REQUEST");
  }

  return [...new Set(errors)];
}

export {deterministicDraftChecks};
