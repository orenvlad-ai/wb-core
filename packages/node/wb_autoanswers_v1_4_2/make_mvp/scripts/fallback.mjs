function codes(items = []) {
  return new Set(items.map((item) => typeof item === "string" ? item : item.code));
}

function applicable(item, classification, finalRoute) {
  if (item.owner_approval_status?.startsWith("approved_") !== true) return false;
  if (item.route !== finalRoute || item.review_mode !== classification.review_mode) return false;
  if (item.issue_subtype && item.issue_subtype !== classification.primary_issue_subtype) return false;

  const issueCodes = codes(classification.issues);
  if ((item.issue_codes || []).length > 0 && !item.issue_codes.some((code) => issueCodes.has(code))) return false;
  if ((item.issue_codes || []).length === 0 && issueCodes.size > 0) return false;

  const positiveCodes = codes(classification.positive_signals);
  if ((item.positive_signal_codes || []).some((code) => !positiveCodes.has(code))) return false;
  const riskFlags = new Set(classification.risk_flags || []);
  if ((item.risk_flags || []).some((flag) => !riskFlags.has(flag))) return false;
  return true;
}

function specificity(item) {
  return (item.issue_subtype ? 100 : 0)
    + (item.risk_flags?.length || 0) * 10
    + (item.positive_signal_codes?.length || 0) * 5
    + Math.min(item.issue_codes?.length || 0, 4);
}

export function selectApprovedFallback(library, classification, finalRoute, caseCode) {
  if (library.approval_status !== "approved") throw new Error("FALLBACK_LIBRARY_NOT_APPROVED");
  const item = library.fallbacks
    .filter((candidate) => applicable(candidate, classification, finalRoute))
    .sort((left, right) => specificity(right) - specificity(left))[0];
  if (!item) throw new Error(`NO_APPROVED_FALLBACK:${finalRoute}:${classification.primary_issue || "none"}`);

  const reply = item.reply_template
    ? item.reply_template.replaceAll("{{case_code}}", caseCode || "")
    : item.reply;
  const issueCodes = codes(classification.issues);
  const positiveCodes = codes(classification.positive_signals);
  return {
    fallback_id: item.id,
    draft: {
      schema_version: "1.0.0",
      review_id: classification.review_id,
      review_version: classification.review_version,
      route: finalRoute,
      case_code: finalRoute === "seller_chat" ? caseCode : null,
      draft_reply: reply,
      covered_issue_codes: [...issueCodes],
      covered_positive_codes: [...positiveCodes],
      used_fact_ids: [],
      applied_cta: finalRoute === "public_only" ? "none" : finalRoute,
      requested_materials: []
    }
  };
}
