import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import path from "node:path";
import {
  buildCacheableInput,
  schemaForStructuredOutput
} from "../frozen_bundle/tools/build_context.mjs";
import {
  MODEL_ID,
  ROLE_MAX_OUTPUT_TOKENS,
  ROLE_OUTPUT_SCHEMA
} from "./constants.mjs";
import {bundleRoot} from "./schema_validation.mjs";

const CACHEABLE_ROLES = new Set(["classifier", "writer", "validator"]);
const MAX_CLASSIFIER_PHOTOS = 8;
const MAX_CLASSIFIER_VIDEO_IMAGES = 5;

function sha256Hex(value) {
  return createHash("sha256").update(value).digest("hex");
}

/** Keep binary media out of repeated tagged JSON while retaining stable facts. */
function withoutEmbeddedMedia(value) {
  if (Array.isArray(value)) return value.map(withoutEmbeddedMedia);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, withoutEmbeddedMedia(child)]));
  }
  if (typeof value === "string" && /^data:image\/[a-z0-9.+-]+;base64,/iu.test(value)) {
    return `attached_image_sha256:${sha256Hex(value).slice(0, 16)}`;
  }
  return value;
}

export function stableSafetyIdentifier(reviewId) {
  return `wb_${sha256Hex(String(reviewId)).slice(0, 32)}`;
}

async function roleAssets(role) {
  if (!ROLE_OUTPUT_SCHEMA[role]) throw new Error(`UNKNOWN_ROLE:${role}`);
  const [instructions, schema] = await Promise.all([
    readFile(path.join(bundleRoot, "prompts", `${role}.system.md`), "utf8"),
    readFile(path.join(bundleRoot, "contracts", ROLE_OUTPUT_SCHEMA[role]), "utf8").then(JSON.parse)
  ]);
  return {instructions, schema};
}

/** Builds the same Responses API body shape as the frozen evaluator, without credentials. */
export async function buildResponsesPayload(role, request, reviewId) {
  const {instructions, schema} = await roleAssets(role);
  const cachePlan = buildCacheableInput(role, withoutEmbeddedMedia(request));
  const body = {
    model: MODEL_ID,
    store: false,
    reasoning: {effort: "medium"},
    safety_identifier: stableSafetyIdentifier(reviewId),
    instructions,
    input: cachePlan.input,
    prompt_cache_options: {mode: "explicit", ttl: "30m"},
    max_output_tokens: ROLE_MAX_OUTPUT_TOKENS[role],
    text: {
      format: {
        type: "json_schema",
        name: `wb_${role}_v1`,
        strict: true,
        schema: schemaForStructuredOutput(schema)
      }
    }
  };

  if (CACHEABLE_ROLES.has(role)) {
    body.prompt_cache_key = [
      "wb13",
      "terra",
      sha256Hex(MODEL_ID).slice(0, 8),
      role,
      sha256Hex(instructions).slice(0, 8),
      cachePlan.static_prefix_hash.slice(0, 8),
      "s0"
    ].join(":");
  }
  if (role === "classifier" && Array.isArray(body.input)) {
    const photos = (request.review_input?.media?.photos || [])
      .filter((item) => item.fetch_status === "downloaded")
      .map((item) => item.local_ref || item.full_size_url)
      .filter(Boolean)
      .slice(0, MAX_CLASSIFIER_PHOTOS);
    const frames = request.review_input?.media?.video?.processing_status === "frames_extracted"
      ? (request.review_input.media.video.frame_refs || []).slice(0, MAX_CLASSIFIER_VIDEO_IMAGES)
      : [];
    const content = body.input[0]?.content;
    for (const imageUrl of [...photos, ...frames]) {
      content.push({type: "input_image", image_url: imageUrl, detail: "high"});
    }
  }
  return body;
}

export function containsCacheBreakpoint(payload) {
  return JSON.stringify(payload.input).includes("prompt_cache_breakpoint");
}
