const RETURN_ISSUES = new Set([
  "INJURY_CLAIM",
  "DEVICE_DAMAGE_CLAIM",
  "WRONG_ITEM",
  "DEFECT_OUT_OF_BOX",
  "OPENED_USED"
]);

const PUBLIC_DEFAULT_ISSUES = new Set([
  "VAGUE_QUALITY",
  "MEDIA_ONLY_UNCLEAR",
  "POSITIVE_NO_PROBLEM"
]);

function issueCodes(classification) {
  return new Set((classification.issues || []).map((item) => item.code));
}

function issueEvidence(classification, code) {
  return (classification.issues || [])
    .filter((item) => item.code === code)
    .flatMap((item) => item.evidence || [])
    .map((item) => item.excerpt || "")
    .join(" ").toLowerCase();
}

function isCleaningConsumable(classification) {
  const evidence = issueEvidence(classification, classification.primary_issue);
  const installedDustWithoutSticker = /стикер|наклейк/iu.test(evidence)
    && /под (?:уже )?(?:наклеенным )?стеклом|уже накле|после установк/iu.test(
      issueEvidence(classification, "BUBBLES_DUST")
    );
  return /салфет|микрофибр|тряпоч|стикер|наклейк|чистящ/iu.test(evidence)
    && !installedDustWithoutSticker
    && !/самого стекла|стекла не было|бокс|механизм|аппликатор|платформ|установщик|стикер.{0,50}(?:под стеклом|уже накле)|(?:под стеклом|уже накле).{0,50}стикер/iu.test(evidence);
}

function failedInstallationRemedy(classification) {
  const evidence = ["BUBBLES_DUST", "ADHESION", "MISSING_PARTS", "KIT_QUALITY"]
    .map((code) => issueEvidence(classification, code)).join(" ")
    .replace(/(?:как\s+бы|сколько(?:\s+бы)?)\s+н[ие]\s+(?:пытал|пробовал)\p{L}*/giu, " пытался ")
    .replace(/(?:^|[^\p{L}])не\s+(?:пробовал|пытал|приподнимал|разглаживал(?!\p{L}*(?:ся|сь)(?!\p{L}))|протирал|очищал)\p{L}*(?:\s+(?:приподнимат|разглаживат|протират|очищат|выдавливат|убират)\p{L}*)?/giu, " ");
  if (/сколько\s+ни\s+пытайся.{0,100}(?:не\s+прикле|не\s+фиксир|ничего\s+не\s+получ)/iu.test(evidence)) return true;
  return /(?:пробовал|пытал|приподнимал|разглаживал|протирал|очищал|выждал|выдавливал|убирал|приглаживал).{0,140}(?:не помог|остал|сохранил|вс[её]\s+равно|по-прежнему|не получ(?:ил|илось|ается|ить)|не разглажива|без результата)|(?:не помог|остал|сохранил|вс[её]\s+равно|по-прежнему|не разглажива|без результата).{0,140}(?:после|пробовал|пытал|приподнимал|разглаживал|протирал|очищал)/iu.test(evidence);
}

function unavailableInstallationRemedy(classification) {
  const evidence = ["ADHESION", "BUBBLES_DUST", "INSTALL_GENERAL"]
    .map((code) => issueEvidence(classification, code)).join(" ");
  return /не\s+прикле\p{L}*|откле\p{L}*|не\s+держ\p{L}*|пузыр\p{L}*/iu.test(evidence)
    && /стекл\p{L}*.{0,70}(?:оба|обе|их|его)\s+(?:выброс|выкин|утилиз)\p{L}*|(?:выброс|выкин|утилиз)\p{L}*\s+стекл\p{L}*|стекл\p{L}*\s+(?:выброс|выкин|утилиз)\p{L}*/iu.test(evidence);
}

function establishedIndependentReturn(classification) {
  const mechanism = issueEvidence(classification, "INSTALL_MECHANISM")
    .replace(/(?:^|[^\p{L}])не\s+слом\p{L}*/giu, " ");
  const missing = issueEvidence(classification, "MISSING_PARTS");
  const adhesion = issueEvidence(classification, "ADHESION");
  const failedFunction = ["TOUCH_SENSITIVITY", "FRONT_CAMERA", "FACE_ID"]
    .some((code) => /(?:после|уже).{0,60}(?:очист|протер|провер).{0,80}(?:всё равно|не работ|пропуска|не распозна)/iu.test(issueEvidence(classification, code)));
  return /(?:механизм|язычок|аппликатор|платформа|установщик|фиксатор).{0,55}(?:заеда|слом|не\s+работа|не\s+фиксир|не\s+двига|не\s+поддава|не\s+вытягива)|(?:заеда|слом|не\s+работа|не\s+фиксир|не\s+двига|не\s+поддава|не\s+вытягива).{0,55}(?:механизм|язычок|аппликатор|платформа|установщик|фиксатор)/iu.test(mechanism)
    || /(?:нет|не было|не положили|отсутствует).{0,35}(?:самого\s+)?стекла|(?:самого\s+)?стекла.{0,35}(?:нет|не было|не положили|отсутствует)/iu.test(missing)
    || /(?:скол\s+с\s+клеев|пришл[оаи]?\s+с\s+браком)/iu.test(adhesion)
    || failedFunction;
}

function receivedBeforeUseBreakage(classification) {
  const evidence = issueEvidence(classification, "SPONTANEOUS_BREAKAGE");
  return /(?:стекло|товар).{0,20}(?:пришл|приехал|получ).{0,35}(?:с\s+)?(?:трещин|треснут|скол|разбит|поврежден)|(?:при получении|из коробки).{0,35}(?:трещин|треснут|скол|разбит|поврежден)/iu.test(evidence)
    && !/(?:пришл|приехал|получ).{0,35}(?:без\s+трещин|целым|не\s+треснут)/iu.test(evidence);
}

function isUnverifiedFit(classification) {
  const evidence = issueEvidence(classification, "SIZE_FIT");
  const models = [...evidence.matchAll(/(?:iphone|айфон)\s*(\d{2}(?:\s*(?:pro|max|plus))?)/giu)]
    .map((match) => match[1].replace(/\s+/g, ""));
  const ordered = evidence.match(/заказал[аи]?\s+(?:стекло\s+)?на\s+(\d{2})/iu)?.[1];
  const confirmedSameModel = models.length >= 2 && models[0] === models[1]
    || Boolean(ordered && models.some((model) => model === ordered));
  return /не подош|не подход/iu.test(evidence)
    && !confirmedSameModel;
}

function buyerConfirmedWrongVariant(classification) {
  const evidence = issueEvidence(classification, "SIZE_FIT");
  return /(?:перепутал[аи]?|ошиб(?:ся|лась)\s+с\s+(?:моделью|выбором)).{0,90}(?:взял[аи]?|заказал[аи]?)?.{0,90}(?:а\s+у\s+меня|мой\s+телефон).{0,90}(?:не\s+подош|про\s+макс|pro\s*max|просто\s+про)/iu.test(evidence);
}

function unverifiedSensorAreaFit(classification) {
  const evidence = ["SIZE_FIT", "EARPIECE", "FACE_ID", "FRONT_CAMERA"]
    .map((code) => issueEvidence(classification, code)).join(" ");
  const namedModels = [...evidence.matchAll(/(?:iphone|айфон)\s*\d{2}(?:\s*(?:pro|max|plus))?/giu)];
  return /(?:динамик|вырез|отверст|остров|айленд|island|камер)/iu.test(evidence)
    && /(?:не\s+совпад|несовпад|не\s+подход|смещен|смещён|не\s+по\s+центру)/iu.test(evidence)
    && namedModels.length < 2
    && !/(?:не\s+слыш|плохо\s+слыш|звук|громк|слышимость|разговор|не\s+работа|не\s+распозна|разблокировк)/iu.test(evidence);
}

function isInstallationBreakage(classification) {
  const evidence = issueEvidence(classification, "INSTALL_BREAKAGE");
  const stage = "(?:(?:во время|при|в процессе)\\s+(?:\\p{L}+\\s+){0,2}(?:установк\\p{L}*|наклейк\\p{L}*|наклеиван\\p{L}*|поклейк\\p{L}*)|пока\\s+устанавливал\\p{L}*)";
  const deniedDuringInstallation = new RegExp(`${stage}.{0,30}не\\s+(?:трес|трещ|разб|скол|лоп|повреж)|${stage}.{0,45}(?:трещин|скол|поврежден).{0,15}(?:нет|не\\s+было)`, "iu").test(evidence);
  const duringInstallation = Boolean(evidence) && !deniedDuringInstallation
    && new RegExp(`(?:трес|трещ|разб|скол|лоп|повреж).{0,45}${stage}|${stage}.{0,45}(?:трес|трещ|разб|скол|лоп|повреж)`, "iu").test(evidence);
  return duringInstallation;
}

function hasEarlyFirstInspection(classification) {
  const evidence = issueEvidence(classification, "SPONTANEOUS_BREAKAGE");
  const segments = evidence.split(/\b(?:через|спустя|потом|позже)\b/iu);
  return segments.some((segment) => /трес|трещ|разб|скол|лоп|повреж/iu.test(segment)
    && /(?:при первом осмотре|сразу после накле).{0,60}до (?:начала )?использования/iu.test(segment));
}

function installedDustWithoutSticker(classification) {
  const evidence = issueEvidence(classification, "BUBBLES_DUST");
  return /пылинк|соринк/iu.test(evidence)
    && /под.{0,30}(?:наклеенн|установленн|стекл)/iu.test(evidence)
    && /(?:стикер|наклейк).{0,25}(?:нет|не положили)|(?:нет|не положили).{0,25}(?:стикер|наклейк)/iu.test(evidence);
}

function isPostUseBreakage(classification) {
  const evidence = issueEvidence(classification, "SPONTANEOUS_BREAKAGE");
  if (/(?:при|во время|в процессе)\s+(?:\p{L}+\s+){0,2}(?:использован|эксплуатац)\p{L}*|(?:в течени[еи]|через)\s+.{0,45}(?:использован|эксплуатац)\p{L}*|после(?![^.!?]{0,40}до\s+(?:начала\s+)?использован).{0,45}(?:использован|эксплуатац)\p{L}*|(?:неделю|месяц|день)\s+пользовал/iu.test(evidence)) return true;
  return /пользовал/iu.test(evidence)
    && !/не\s+пользовал(?:ся|ась)?\s+(?:телефоном|стеклом)|(?:телефоном|стеклом)\s+не\s+пользовал/iu.test(evidence);
}

function isClearInstallationResult(classification) {
  const code = classification.primary_issue;
  const evidence = issueEvidence(classification, code);
  const relatedOnly = new Set(["BUBBLES_DUST", "ADHESION", "FRAME_OVERLAP"]);
  if (classification.route === "wb_return"
    && [...issueCodes(classification)].some((item) => !relatedOnly.has(item))) return false;
  if (code === "BUBBLES_DUST") {
    if (installedDustWithoutSticker(classification)) return false;
    return /воздушн.{0,25}пузыр|пузыр.{0,25}без пыли|пузыр(?!.*(?:пылинк|соринк))|пылинк/iu.test(evidence)
      && !/непонятно|неясно|то ли|или пылинк/iu.test(evidence)
      && !/пылинк.{0,50}нет стикер|нет стикер.{0,50}пылинк/iu.test(evidence);
  }
  return code === "ADHESION" && /не прикле|отход.{0,25}край|край.{0,25}отход/iu.test(evidence)
    && !/плёнк|пленк|чехол|чехл/iu.test(evidence);
}

function hasDirectFrameOverlapEvidence(classification) {
  const excerpts = (classification.issues || [])
    .filter((item) => item.code === "FRAME_OVERLAP")
    .flatMap((item) => item.evidence || [])
    .map((item) => item.excerpt || "")
    .join(" ");
  return /(?:перекры|закрыва|закрыл|заход(?:ит|ят).{0,25}(?:экран|изображ)|обрез(?:ает|ал)|съед(?:ает|ал)|не\s+видн|рабоч\w*\s+област|част\w*\s+(?:экрана|изображения)|пиксел|значк|текст)/iu.test(excerpts);
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
  guarded.required_evidence = (guarded.required_evidence || []).filter((item) => (
    item !== "installation_video_required"
      && !(codes.has("TOUCH_SENSITIVITY") && item === "touch_issue_video")
  ));

  function setRoute(route, guardId, reason) {
    if (guarded.route !== route) {
      events.push({guard_id: guardId, from: guarded.route, to: route, reason});
      guarded.route = route;
      guarded.route_reason = `${reason} [${guardId}]`;
    }
  }

  if (([...RETURN_ISSUES].some((code) => codes.has(code)) || establishedIndependentReturn(guarded) || receivedBeforeUseBreakage(guarded)) && !isExplicitlyResolvedMixedPublic(guarded, codes)) {
    setRoute("wb_return", "G004/G-RETURN", "Высокоприоритетная товарная или безопасностная проблема требует официального возврата");
  } else if (isInstallationBreakage(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    setRoute("wb_return", "G-INSTALL-BREAKAGE", "Повреждение при установке или первом осмотре до использования ведёт к возврату");
  } else if (installedDustWithoutSticker(guarded)) {
    setRoute("wb_return", "G-DUST-NO-STICKER", "Пылинка под уже установленным стеклом без стикера не имеет подходящего безопасного способа устранения");
  } else if (codes.has("SPONTANEOUS_BREAKAGE") && !hasExplicitlyResolvedMixedPublic(guarded)) {
    if (isPostUseBreakage(guarded)) {
      guarded.seller_investigation_subject = false;
      guarded.evidence_potential = false;
      guarded.required_evidence = [];
      setRoute("public_only", "G-POST-USE-BREAKAGE", "Одна трещина после начала использования не доказывает недостаток");
    } else if (hasEarlyFirstInspection(guarded) && guarded.route === "wb_return") {
      guarded.required_evidence = [];
    } else {
      guarded.seller_investigation_subject = true;
      guarded.evidence_potential = true;
      guarded.required_evidence = [];
      setRoute("seller_chat", "G-UNKNOWN-BREAKAGE-STAGE", "Этап появления трещины существенно неясен");
    }
  } else if ((codes.has("BUBBLES_DUST") || codes.has("ADHESION")) && failedInstallationRemedy(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    setRoute("wb_return", "G-INSTALL-UNRESOLVED", "Уже описанная неудачная попытка устранить проблему установки ведёт к возврату");
  } else if ((codes.has("ADHESION") || codes.has("BUBBLES_DUST") || codes.has("INSTALL_GENERAL")) && unavailableInstallationRemedy(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    setRoute("wb_return", "G-INSTALL-UNAVAILABLE", "Описанное стекло уже недоступно для повторной попытки исправления результата установки");
  } else if (isClearInstallationResult(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    guarded.seller_investigation_subject = false;
    guarded.evidence_potential = false;
    guarded.required_evidence = [];
    setRoute("public_only", "G-INSTALL-ADVICE", "Для понятного результата установки сначала достаточно одной безопасной инструкции");
  } else if (["MISSING_PARTS", "KIT_QUALITY"].includes(guarded.primary_issue) && !isExplicitlyResolvedMixedPublic(guarded, codes)) {
    if (isCleaningConsumable(guarded) && !failedInstallationRemedy(guarded)) {
      guarded.seller_investigation_subject = false;
      guarded.evidence_potential = false;
      guarded.required_evidence = [];
      setRoute("public_only", "G-CLEANING-ADVICE", "Для очистительного расходника сначала достаточно совета из оставшегося комплекта");
    } else {
      setRoute("wb_return", "G-KIT-RETURN", "Недостающий основной товар или сохраняющаяся проблема комплекта требует возврата");
    }
  } else if (codes.has("SIZE_FIT") && buyerConfirmedWrongVariant(guarded) && ![...RETURN_ISSUES].some((code) => codes.has(code)) && !establishedIndependentReturn(guarded)) {
    guarded.seller_investigation_subject = false;
    guarded.evidence_potential = false;
    guarded.required_evidence = [];
    setRoute("public_only", "G-BUYER-FIT", "Покупатель прямо указал, что сам выбрал вариант для другой модели");
  } else if (codes.has("SIZE_FIT") && isUnverifiedFit(guarded) && ![...RETURN_ISSUES].some((code) => codes.has(code)) && !establishedIndependentReturn(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    guarded.seller_investigation_subject = true;
    guarded.evidence_potential = true;
    guarded.required_evidence = [];
    setRoute("seller_chat", "G023/G-SIZE-FIT", "Модель и соответствие заказу нельзя установить из отзыва");
  } else if (unverifiedSensorAreaFit(guarded) && ![...RETURN_ISSUES].some((code) => codes.has(code)) && !establishedIndependentReturn(guarded) && !hasExplicitlyResolvedMixedPublic(guarded)) {
    guarded.seller_investigation_subject = true;
    guarded.evidence_potential = true;
    guarded.required_evidence = [];
    setRoute("seller_chat", "G-SENSOR-AREA-FIT", "Совпадение геометрии зоны выреза с заказанной моделью не установлено");
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
    guarded.seller_investigation_subject = false;
    guarded.evidence_potential = false;
    guarded.required_evidence = [];
    setRoute("public_only", "G017", "По технической проблеме розыгрыша достаточно совета повторить попытку позже");
  } else if (guarded.primary_issue_subtype === "seller_support_no_response") {
    guarded.seller_investigation_subject = true;
    guarded.evidence_potential = true;
    setRoute("seller_chat", "G016", "История оставшегося без ответа обращения проверяется продавцом");
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
  if (classification.primary_issue_subtype === "promo_access_blocked" && classification.route !== "public_only") {
    errors.push("G017: техническая проблема промо не получила законченный публичный совет");
  }
  if (classification.primary_issue === "FRAME_OVERLAP" && classification.route === "wb_return" && !hasDirectFrameOverlapEvidence(classification)) {
    errors.push("G018: возврат по рамке назначен без прямого сообщения о перекрытии изображения");
  }
  if (codes.has("SIZE_FIT") && isUnverifiedFit(classification) && !buyerConfirmedWrongVariant(classification) && ![...RETURN_ISSUES].some((code) => codes.has(code)) && !establishedIndependentReturn(classification) && !hasExplicitlyResolvedMixedPublic(classification) && classification.route !== "seller_chat") {
    errors.push("G023: неизвестная совместимость не направлена на уточнение в чат");
  }
  return errors;
}
