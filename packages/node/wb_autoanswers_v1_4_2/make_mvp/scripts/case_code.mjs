import {generateCaseCode} from "../frozen_bundle/tools/build_context.mjs";

export const CASE_CODE_PATTERN = /^[А-ЯЁ][0-9]{4}$/u;

/**
 * Allocates only for seller_chat. Collisions use a deterministic probe while an
 * existing idempotency key always receives its previously reserved code.
 */
export function allocateCaseCode({finalRoute, reviewId, reviewVersion, idempotencyKey, existing = []}) {
  if (finalRoute !== "seller_chat") return null;
  const prior = existing.find((item) => item.idempotency_key === idempotencyKey);
  if (prior) return prior.case_code;

  const occupied = new Set(existing.filter((item) => item.active !== false).map((item) => item.case_code));
  for (let probe = 0; probe < 10000; probe += 1) {
    const versionSeed = probe === 0 ? reviewVersion : `${reviewVersion}:${probe}`;
    const candidate = generateCaseCode(reviewId, versionSeed);
    if (!occupied.has(candidate)) return candidate;
  }
  throw new Error("CASE_CODE_SPACE_EXHAUSTED");
}

export function assertCaseCode(route, caseCode) {
  if (route === "seller_chat" && !CASE_CODE_PATTERN.test(caseCode || "")) {
    throw new Error("CASE_CODE_REQUIRED_FOR_SELLER_CHAT");
  }
  if (route !== "seller_chat" && caseCode !== null) {
    throw new Error("CASE_CODE_FORBIDDEN_OUTSIDE_SELLER_CHAT");
  }
}
