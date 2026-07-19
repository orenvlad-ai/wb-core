import {PROMPT_BUNDLE_VERSION} from "./constants.mjs";

function requiredString(value, name) {
  const normalized = String(value ?? "").trim();
  if (!normalized) throw new Error(`MISSING_REQUIRED_FIELD:${name}`);
  return normalized;
}

export function makeIdempotencyKey({reviewId, reviewVersion, promptBundleVersion = PROMPT_BUNDLE_VERSION}) {
  return [
    requiredString(reviewId, "review_id"),
    requiredString(reviewVersion, "review_version"),
    requiredString(promptBundleVersion, "prompt_bundle_version")
  ].map((part) => encodeURIComponent(part)).join("|");
}

export function isReusableJob(job) {
  return Boolean(job && ["ready", "fallback", "skipped", "delivered_to_telegram"].includes(job.outcome));
}
