import {evaluatePrefilter} from "./prefilter.mjs";
import {
  DOCTRINE_VERSION,
  MODEL_ID,
  PRODUCT_CONTEXT_VERSION,
  PROMPT_BUNDLE_VERSION
} from "./constants.mjs";

function cleanText(value) {
  return String(value ?? "").replace(/\r\n?/gu, "\n").trim();
}

function requireText(value, name) {
  const normalized = cleanText(value);
  if (!normalized) throw new Error(`MISSING_REQUIRED_FIELD:${name}`);
  return normalized;
}

function normalizeRating(value) {
  const rating = Number(value);
  if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
    throw new Error("INVALID_REQUIRED_FIELD:rating");
  }
  return rating;
}

function normalizeNullableInteger(value, name) {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) throw new Error(`INVALID_FIELD:${name}`);
  return parsed;
}

function normalizedReviewText({text, pros, cons}) {
  const sections = [];
  if (text) sections.push(text);
  if (pros) sections.push(`Плюсы: ${pros}`);
  if (cons) sections.push(`Минусы: ${cons}`);
  return sections.join("\n");
}

function resolveProduct(raw, productContext) {
  const nmId = normalizeNullableInteger(raw.nm_id, "nm_id");
  const sellerArticle = cleanText(raw.seller_article) || null;
  const match = productContext.sku_index?.find((item) => (
    (sellerArticle && item.seller_article === sellerArticle)
    || (nmId !== null && item.nm_ids?.includes(nmId))
  ));

  if (!match) {
    return {
      nm_id: nmId,
      seller_article: sellerArticle,
      product_name: cleanText(raw.product_name) || null,
      phone_models: [],
      line: "unknown",
      context_status: "unknown"
    };
  }

  return {
    nm_id: nmId,
    seller_article: sellerArticle ?? match.seller_article,
    product_name: cleanText(raw.product_name) || match.product_names?.[0] || null,
    phone_models: [...(match.phone_models || [])],
    line: match.line,
    context_status: match.context_status
  };
}

function normalizePhotos(photos = []) {
  if (!Array.isArray(photos)) throw new Error("INVALID_FIELD:photos");
  return photos.map((photo, index) => ({
    index: index + 1,
    full_size_url: cleanText(photo?.full_size_url) || null,
    mini_size_url: cleanText(photo?.mini_size_url) || null,
    fetch_status: ["not_requested", "downloaded", "fetch_failed"].includes(photo?.fetch_status)
      ? photo.fetch_status
      : "not_requested",
    local_ref: cleanText(photo?.local_ref) || null
  }));
}

function normalizeMedia(raw = {}) {
  const photos = normalizePhotos(raw.photos || []);
  const videoPresent = Boolean(raw.video?.present);
  const processingStatus = videoPresent
    ? (["not_processed", "frames_extracted", "fetch_failed"].includes(raw.video?.processing_status)
        ? raw.video.processing_status
        : "not_processed")
    : "none";
  let status = "none";
  if (photos.some((item) => item.fetch_status === "fetch_failed") || processingStatus === "fetch_failed") {
    status = "fetch_failed";
  } else if (videoPresent && processingStatus !== "frames_extracted") {
    status = "video_present_unprocessed";
  } else if (photos.length > 0 || processingStatus === "frames_extracted") {
    status = raw.status === "unclear" ? "unclear" : "analyzed";
  }
  return {
    status,
    photo_count: photos.length,
    photos,
    video: {
      present: videoPresent,
      source_url: cleanText(raw.video?.source_url) || null,
      processing_status: processingStatus,
      frame_refs: Array.isArray(raw.video?.frame_refs)
        ? raw.video.frame_refs.map(cleanText).filter(Boolean).slice(0, 20)
        : []
    }
  };
}

/**
 * Converts the Telegram parser envelope into the frozen review_input contract.
 * Missing SKU context is represented as unknown; no line or phone property is inferred.
 */
export function normalizeTelegramInput(raw, productContext, {now = () => new Date()} = {}) {
  if (!raw || typeof raw !== "object") throw new Error("INVALID_INPUT:telegram_envelope");
  const rating = normalizeRating(raw.rating);
  const text = cleanText(raw.text);
  const pros = cleanText(raw.pros);
  const cons = cleanText(raw.cons);
  const prefilter = evaluatePrefilter({rating, text, pros, cons});
  const receivedAt = cleanText(raw.received_at) || now().toISOString();

  return {
    schema_version: "1.0.0",
    review: {
      review_id: requireText(raw.review_id, "review_id"),
      review_version: requireText(raw.review_version, "review_version"),
      created_at: cleanText(raw.created_at) || receivedAt,
      rating,
      text,
      pros,
      cons,
      wb_tags: Array.isArray(raw.wb_tags) ? raw.wb_tags.map(cleanText).filter(Boolean).slice(0, 50) : [],
      normalized_text: normalizedReviewText({text, pros, cons})
    },
    product: resolveProduct(raw, productContext),
    media: normalizeMedia(raw.media),
    history: {
      previous_review_version: cleanText(raw.history?.previous_review_version) || null,
      previous_review_text: cleanText(raw.history?.previous_review_text) || null,
      previous_public_reply: cleanText(raw.history?.previous_public_reply) || null
    },
    prefilter,
    operational: {
      source: "telegram",
      ingestion_id: requireText(raw.ingestion_id, "ingestion_id"),
      received_at: receivedAt
    },
    versions: {
      doctrine: DOCTRINE_VERSION,
      product_context: productContext.schema_version || PRODUCT_CONTEXT_VERSION,
      prompt_bundle: PROMPT_BUNDLE_VERSION,
      model: MODEL_ID
    },
    untrusted_content: true
  };
}
