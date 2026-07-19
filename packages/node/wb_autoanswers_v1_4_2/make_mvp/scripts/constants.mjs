export const PROMPT_BUNDLE_VERSION = "1.4.2";
export const DOCTRINE_VERSION = "1.0";
export const MODEL_ID = "gpt-5.6-terra";
export const PRODUCT_CONTEXT_VERSION = "1.0.0";
export const EVALUATION_SIGNATURE = "sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da";
export const MAX_REWRITES = 2;

export const ROLE_OUTPUT_SCHEMA = Object.freeze({
  classifier: "classification.schema.json",
  writer: "draft_reply.schema.json",
  validator: "validation.schema.json",
  rewrite: "draft_reply.schema.json"
});

export const ROLE_REQUEST_SCHEMA = Object.freeze({
  classifier: "classifier_request.schema.json",
  writer: "writer_request.schema.json",
  validator: "validator_request.schema.json",
  rewrite: "rewrite_request.schema.json"
});

export const ROLE_MAX_OUTPUT_TOKENS = Object.freeze({
  classifier: 6000,
  writer: 3000,
  validator: 3000,
  rewrite: 3000
});

export const ROUTES = Object.freeze([
  "public_only",
  "seller_chat",
  "wb_return",
  "wb_support"
]);
