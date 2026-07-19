function meaningful(value) {
  return typeof value === "string" && value.trim().length > 0;
}

/** WB tags are deliberately excluded: tags alone never make a five-star review substantive. */
export function evaluatePrefilter({rating, text = "", pros = "", cons = ""}) {
  const emptyFiveStar = Number(rating) === 5
    && !meaningful(text)
    && !meaningful(pros)
    && !meaningful(cons);

  return emptyFiveStar
    ? {publication_action: "skip", reason: "empty_five_star", model_calls_allowed: false}
    : {publication_action: "reply", reason: "process", model_calls_allowed: true};
}
