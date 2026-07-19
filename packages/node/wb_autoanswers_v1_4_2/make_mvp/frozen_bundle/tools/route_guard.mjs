const RETURN_ISSUES = new Set([
  "INJURY_CLAIM",
  "DEVICE_DAMAGE_CLAIM",
  "WRONG_ITEM",
  "MISSING_PARTS",
  "DEFECT_OUT_OF_BOX",
  "OPENED_USED",
  "KIT_QUALITY"
]);

const PUBLIC_DEFAULT_ISSUES = new Set([
  "VAGUE_QUALITY",
  "MEDIA_ONLY_UNCLEAR",
  "POSITIVE_NO_PROBLEM"
]);

function issueCodes(classification) {
  return new Set((classification.issues || []).map((item) => item.code));
}

function hasDirectFrameOverlapEvidence(classification) {
  const excerpts = (classification.issues || [])
    .filter((item) => item.code === "FRAME_OVERLAP")
    .flatMap((item) => item.evidence || [])
    .map((item) => item.excerpt || "")
    .join(" ");
  return /(?:перекры|закрыва|закрыл|заход(?:ит|ят).{0,25}(?:экран|изображ)|обрез(?:ает|ал)|съед(?:ает|ал)|не\s+видн|рабоч\w*\s+област|част\w*\s+(?:экрана|изображения)|пиксел|значк|текст)/iu.test(excerpts);
}

function hasDirectSizeFitMismatch(classification) {
  const excerpts = (classification.issues || [])
    .filter((item) => item.code === "SIZE_FIT")
    .flatMap((item) => item.evidence || [])
    .map((item) => item.excerpt || "")
    .join(" ");
  return /(?:(?:не\s+подош(?:ло|ёл|ла|ли)?|не\s+подход(?:ит|ят|ило|или)?).{0,40}(?:телефон|модел|размер|форм)|(?:телефон|модел).{0,40}(?:не\s+подош|не\s+подход)|(?:существен|значитель|слишком|чрезмерн).{0,20}(?:меньш|узк|короч)|(?:неверн|друг).{0,20}(?:размер|форм))/iu.test(excerpts);
}

function hasExplicitlyResolvedMixedPublic(classification) {
  const resolutionReason = classification.route_reason || "";
  return classification.route === "public_only"
    && classification.review_mode === "mixed"
    && (classification.positive_signals || []).length > 0
    && Boolean(classification.primary_positive_signal)
    && classification.seller_investigation_subject === false
    && classification.evidence_potential === false
    && (classification.required_evidence || []).length === 0
    && /(?:^|[\s,.;:])(?:уже\s+)?(?:реш[её]н|разреш[её]н|устран[её]н|урегулирован)\p{L}*(?=$|[\s,.;:])|удалось\s+(?:полностью\s+)?решить|нов\p{L}*\s+действи\p{L}*.{0,20}не\s+треб|не\s+(?:заявляет|описывает|указывает).{0,60}(?:нереш[её]н\p{L}*|сохраняющ\p{L}*|нов\p{L}*\s+(?:требован|действ))|(?:был[аои]?|были)\s+(?:успешно\s+)?замен[её]н\p{L}*\s+продавц/iu.test(resolutionReason);
}

function isExplicitlyResolvedMixedPublic(classification, codes) {
  const hasNonRiskReturnIssue = [...RETURN_ISSUES].some((code) => (
    code !== "INJURY_CLAIM" && code !== "DEVICE_DAMAGE_CLAIM" && codes.has(code)
  ));
  return hasNonRiskReturnIssue && hasExplicitlyResolvedMixedPublic(classification);
}

export function applyRouteGuards(classification) {
  const guarded = structuredClone(classification);
  const codes = issueCodes(guarded);
  const events = [];

  function setRoute(route, guardId, reason) {
    if (guarded.route !== route) {
      events.push({guard_id: guardId, from: guarded.route, to: route, reason});
      guarded.route = route;
      guarded.route_reason = `${reason} [${guardId}]`;
    }
  }

  if ([...RETURN_ISSUES].some((code) => codes.has(code)) && !isExplicitlyResolvedMixedPublic(guarded, codes)) {
    setRoute("wb_return", "G004/G-RETURN", "Высокоприоритетная товарная или безопасностная проблема требует официального возврата");
  } else if (codes.has("SIZE_FIT") && guarded.route === "public_only" && hasDirectSizeFitMismatch(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    guarded.seller_investigation_subject = false;
    guarded.evidence_potential = true;
    for (const material of ["installed_glass_photo", "package_label_photo"]) {
      if (!guarded.required_evidence.includes(material)) guarded.required_evidence.push(material);
    }
    setRoute("wb_return", "G023/G-SIZE-FIT", "Прямое физическое несовпадение стекла с телефоном требует официального возврата");
  } else if (guarded.primary_issue === "FRAME_OVERLAP" && guarded.route === "wb_return" && !hasDirectFrameOverlapEvidence(guarded)) {
    guarded.seller_investigation_subject = false;
    guarded.evidence_potential = false;
    if ((guarded.required_evidence || []).length > 0) {
      events.push({
        guard_id: "G018",
        from: "required_evidence",
        to: "[]",
        reason: "Предположение о визуально широкой рамке без прямого перекрытия не требует материалов для возврата"
      });
      guarded.required_evidence = [];
    }
    setRoute("public_only", "G018", "Прямое перекрытие рамкой изображения или рабочей области не заявлено");
  } else if (codes.has("WB_REFUND_STATUS")) {
    setRoute("wb_support", "G005", "Статус возврата проверяется Wildberries");
  } else if (guarded.primary_issue_subtype === "delivery_delay") {
    setRoute("public_only", "G014", "Завершившаяся задержка уже полученного заказа не требует нового обращения");
  } else if (guarded.primary_issue_subtype === "promo_access_blocked") {
    guarded.seller_investigation_subject = true;
    guarded.evidence_potential = true;
    setRoute("seller_chat", "G017", "Доступ к промо продавца является проверяемым предметом разбирательства");
  } else if (guarded.primary_issue_subtype === "seller_support_no_response") {
    guarded.seller_investigation_subject = true;
    guarded.evidence_potential = true;
    setRoute("seller_chat", "G016", "История оставшегося без ответа обращения проверяется продавцом");
  } else if (codes.has("SERVICE_GUARANTEE")) {
    guarded.seller_investigation_subject = true;
    guarded.evidence_potential = true;
    if (!guarded.required_evidence.includes("installation_video_required")) {
      guarded.required_evidence.push("installation_video_required");
      events.push({guard_id: "G011", from: "required_evidence", to: "installation_video_required", reason: "Условия гарантии установки проверяются по обязательному видео"});
    }
    setRoute("seller_chat", "G011", "Условия гарантии установки проверяются только в чате продавца");
  } else if (PUBLIC_DEFAULT_ISSUES.has(guarded.primary_issue)) {
    setRoute("public_only", "G006/G007", "Недостаточно данных для проверяемого разбирательства либо проблема отсутствует");
  }

  if (guarded.route === "seller_chat" && !(guarded.seller_investigation_subject && guarded.evidence_potential)) {
    setRoute("public_only", "G002", "Для seller_chat отсутствует одновременно проверяемый предмет и потенциал доказательств");
  }

  return {classification: guarded, events};
}

export function assertGuardInvariants(classification) {
  const errors = [];
  const codes = issueCodes(classification);
  if (classification.route === "seller_chat" && !(classification.seller_investigation_subject && classification.evidence_potential)) {
    errors.push("G002: seller_chat без предмета и потенциала доказательств");
  }
  if ((codes.has("INJURY_CLAIM") || codes.has("DEVICE_DAMAGE_CLAIM")) && classification.route !== "wb_return") {
    errors.push("G004: риск травмы или повреждения устройства не направлен в wb_return");
  }
  if (codes.has("WB_REFUND_STATUS") && classification.route !== "wb_support") {
    errors.push("G005: статус возврата не направлен в wb_support");
  }
  if (classification.primary_issue_subtype === "delivery_delay" && classification.route !== "public_only") {
    errors.push("G014: завершившаяся задержка не оставлена public_only");
  }
  if (classification.primary_issue_subtype === "seller_support_no_response" && classification.route !== "seller_chat") {
    errors.push("G016: оставшееся без ответа обращение не направлено в seller_chat");
  }
  if (classification.primary_issue_subtype === "promo_access_blocked" && classification.route !== "seller_chat") {
    errors.push("G017: блокировка промо не направлена в seller_chat");
  }
  if (classification.primary_issue === "FRAME_OVERLAP" && classification.route === "wb_return" && !hasDirectFrameOverlapEvidence(classification)) {
    errors.push("G018: возврат по рамке назначен без прямого сообщения о перекрытии изображения");
  }
  if (codes.has("SIZE_FIT") && hasDirectSizeFitMismatch(classification) && !hasExplicitlyResolvedMixedPublic(classification) && classification.route !== "wb_return") {
    errors.push("G023: прямое физическое несовпадение стекла не направлено в wb_return");
  }
  return errors;
}
