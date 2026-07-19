export function deterministicDraftChecks(draft, writerRequest) {
  const errors = [];
  const text = draft.draft_reply;
  if (!text.startsWith("Здравствуйте.")) errors.push("GREETING_INVALID");
  if (text.length > 900) errors.push("LENGTH_HARD");
  if (text.includes("!")) errors.push("STYLE_FORBIDDEN_EXCLAMATION");
  if (/вам попал(?:ся|ось)|подмен|пересорт/i.test(text)) errors.push("STYLE_FORBIDDEN_WRONG_ITEM_WORDING");
  if (/верн[её]м.{0,20}деньги|компенсир|обязательно замен|гарантируем.{0,30}(возврат|решение)|заявк[ау].{0,20}одобр/i.test(text)) errors.push("PROMISE_UNAUTHORIZED");

  if (writerRequest.final_route === "seller_chat") {
    const matches = text.match(new RegExp(writerRequest.case_code, "g")) || [];
    if (matches.length !== 1) errors.push("CASE_CODE_INVALID");
    const requestsMedia = /(?:прилож|подготов|пришл|отправ|прикреп|покаж|сфотограф|запиш|понадоб|необходим|нужн).{0,60}(?:фото|видео|скриншот|материал)|(?:фото|видео|скриншот|материал).{0,60}(?:прилож|подготов|пришл|отправ|прикреп|покаж|сфотограф|запиш|понадоб|необходим|нужн)|(?:разбор|обращение).{0,30}\sс\s(?:фото|видео|материал)/i.test(text);
    if (requestsMedia || (draft.requested_materials || []).length > 0) {
      errors.push("SELLER_CHAT_PUBLIC_EVIDENCE_REQUEST");
    }
  } else if (/[А-ЯЁ][0-9]{4}/.test(text)) {
    errors.push("CASE_CODE_INVALID");
  }

  if ((writerRequest.classification.issues || []).some((item) => item.code === "SERVICE_GUARANTEE") && /фото.{0,50}(вместо|замен)/i.test(text)) {
    errors.push("INSTALLATION_VIDEO_SUBSTITUTION");
  }
  return errors;
}
