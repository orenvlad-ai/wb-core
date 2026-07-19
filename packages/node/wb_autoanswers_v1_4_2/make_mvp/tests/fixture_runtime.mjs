import {readFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

const mvpRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export async function loadScenarios() {
  return JSON.parse(await readFile(path.join(mvpRoot, "fixtures", "scenarios.json"), "utf8"));
}

function evidence(reviewInput) {
  return [{
    source_type: "review_text",
    source_ref: "review.text",
    excerpt: reviewInput.review.text || reviewInput.review.normalized_text,
    observed_fact: reviewInput.review.normalized_text || "Содержательный отзыв"
  }];
}

function classificationOutput(specification, reviewInput) {
  const spec = specification.classification;
  return {
    schema_version: "1.0.0",
    review_id: reviewInput.review.review_id,
    review_version: reviewInput.review.review_version,
    review_mode: spec.review_mode,
    positive_signals: [],
    issues: spec.issue_codes.map((code) => ({code, confidence: 0.95, evidence: evidence(reviewInput)})),
    primary_issue: spec.primary_issue,
    primary_issue_subtype: spec.primary_issue_subtype || null,
    primary_positive_signal: null,
    tone: "correct_negative",
    factuality: "concrete",
    classification_confidence: 0.95,
    media_status: reviewInput.media.status,
    visual_findings: [],
    text_image_consistency: reviewInput.media.status === "none" ? "not_applicable" : "unknown",
    risk_flags: spec.risk_flags || [],
    route: spec.route,
    route_reason: `Fixture route ${spec.route}`,
    seller_investigation_subject: spec.seller_investigation_subject,
    evidence_potential: spec.evidence_potential,
    required_evidence: spec.required_evidence,
    publication_action: "reply"
  };
}

function draftOutput(textTemplate, request, unsafe = false) {
  const text = textTemplate.replaceAll("{{case_code}}", request.case_code || "");
  const routeMaterials = request.final_route === "wb_return"
    ? ["product_photo", "package_label_photo"]
    : request.final_route === "wb_support" ? ["status_screenshot"] : [];
  return {
    schema_version: "1.0.0",
    review_id: request.review_input.review.review_id,
    review_version: request.review_input.review.review_version,
    route: request.final_route,
    case_code: request.final_route === "seller_chat" ? request.case_code : null,
    draft_reply: text,
    covered_issue_codes: request.classification.issues.map((item) => item.code),
    covered_positive_codes: [],
    used_fact_ids: [],
    applied_cta: request.final_route === "public_only" ? "none" : request.final_route,
    requested_materials: unsafe && request.final_route === "seller_chat" ? ["photo"] : routeMaterials
  };
}

function validationOutput(status, request) {
  const violation = status === "pass" ? [] : [{
    code: "OTHER",
    severity: "error",
    evidence: "Fixture requests a rewrite",
    rewrite_instruction: "Исправить нарушение"
  }];
  return {
    schema_version: "1.0.0",
    review_id: request.review_input.review.review_id,
    review_version: request.review_input.review.review_version,
    status,
    violations: violation,
    rewrite_instructions: status === "pass" ? [] : ["Исправить нарушение"],
    confidence: 0.99
  };
}

export function createFixtureRunner(specification, observer = () => {}) {
  let draftIndex = 0;
  let validationIndex = 0;
  const calls = [];
  const runner = async ({role, request, payload}) => {
    calls.push(role);
    observer({role, request, payload});
    let output;
    if (role === "classifier") {
      output = classificationOutput(specification, request.review_input);
    } else if (role === "writer") {
      const template = specification.drafts[0];
      output = draftOutput(template, request, /(?:фото|видео|скриншот)/iu.test(template));
    } else if (role === "rewrite") {
      draftIndex += 1;
      const template = specification.drafts[draftIndex];
      output = draftOutput(template, request, /(?:фото|видео|скриншот)/iu.test(template));
    } else if (role === "validator") {
      output = validationOutput(specification.validator_statuses[validationIndex] || "pass", request);
      validationIndex += 1;
    } else {
      throw new Error(`Unexpected fixture role ${role}`);
    }
    return {
      output,
      response_id: `fixture-${calls.length}`,
      latency_ms: 1,
      usage: {
        input_tokens: 100,
        input_tokens_details: {cached_tokens: 40, cache_write_tokens: 0},
        output_tokens: 20,
        output_tokens_details: {reasoning_tokens: 5},
        total_tokens: 120
      }
    };
  };
  runner.calls = calls;
  return runner;
}

export {mvpRoot};
