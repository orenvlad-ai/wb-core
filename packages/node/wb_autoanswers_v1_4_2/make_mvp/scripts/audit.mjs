import {createHash} from "node:crypto";

const URL_KEYS = /(?:url|signed|link|local_ref|frame_ref)/iu;
const SECRET_KEYS = /(?:authorization|api[_-]?key|token|secret|credential|password)/iu;
const PII_KEYS = /(?:phone|address|full_name|order_number)/iu;

function fingerprint(value) {
  return createHash("sha256").update(String(value)).digest("hex").slice(0, 16);
}

export function sanitizeForAudit(value, key = "") {
  if (value === null || value === undefined) return value;
  if (SECRET_KEYS.test(key)) return "[REDACTED_SECRET]";
  if ((URL_KEYS.test(key) || PII_KEYS.test(key)) && typeof value === "string" && value) {
    return `[REDACTED:${fingerprint(value)}]`;
  }
  if (Array.isArray(value)) return value.map((item) => sanitizeForAudit(item, key));
  if (typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([childKey, child]) => [childKey, sanitizeForAudit(child, childKey)]));
  }
  return value;
}

export function auditEvent(type, payload, at = new Date()) {
  return {type, at: at.toISOString(), payload: sanitizeForAudit(payload)};
}
