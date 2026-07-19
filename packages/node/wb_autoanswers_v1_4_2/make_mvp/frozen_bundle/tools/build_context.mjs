import {createHash} from "node:crypto";
import {readFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import path from "node:path";
import {applyRouteGuards} from "./route_guard.mjs";

export const bundleRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export async function readJson(relativePath) {
  return JSON.parse(await readFile(path.join(bundleRoot, relativePath), "utf8"));
}

export async function readJsonl(relativePath) {
  const text = await readFile(path.join(bundleRoot, relativePath), "utf8");
  return text.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line));
}

function deepMergePatch(base, patchValue) {
  if (Array.isArray(patchValue) || patchValue === null || typeof patchValue !== "object") {
    return structuredClone(patchValue);
  }
  const result = (base && typeof base === "object" && !Array.isArray(base))
    ? structuredClone(base)
    : {};
  for (const [key, value] of Object.entries(patchValue)) {
    result[key] = deepMergePatch(result[key], value);
  }
  return result;
}

export async function applyCaseOverrides(cases, relativePath) {
  const specification = await readJson(relativePath);
  const overrides = new Map(specification.overrides.map((item) => [item.case_id, item.expected_patch]));
  const seen = new Set();
  const patchedCases = cases.map((item) => {
    const expectedPatch = overrides.get(item.case_id);
    if (!expectedPatch) return item;
    seen.add(item.case_id);
    return {
      ...structuredClone(item),
      expected: deepMergePatch(item.expected, expectedPatch)
    };
  });
  const missing = [...overrides.keys()].filter((caseId) => !seen.has(caseId));
  if (missing.length > 0) {
    throw new Error(`Case override targets are missing from source suite: ${missing.join(", ")}`);
  }
  return {cases: patchedCases, specification};
}

let staticContextsPromise;
export function loadStaticContexts() {
  staticContextsPromise ||= Promise.all([
    readJson("contracts/issue_taxonomy.json"),
    readJson("contracts/issue_playbook.json"),
    readJson("contracts/route_policy.json"),
    readJson("contracts/product_context.json"),
    readJson("contracts/approved_examples.json")
  ]).then(([taxonomy, playbook, routePolicy, productContext, approvedExamples]) => ({
    taxonomy,
    playbook,
    routePolicy,
    productContext,
    approvedExamples
  }));
  return staticContextsPromise;
}

export async function buildClassifierRequest(reviewInput) {
  const contexts = await loadStaticContexts();
  const line = reviewInput.product.line;
  const matchingSku = contexts.productContext.sku_index?.find((item) => (
    item.seller_article === reviewInput.product.seller_article
    || item.nm_ids?.includes(reviewInput.product.nm_id)
  ));
  return {
    request_schema_version: "1.0.0",
    taxonomy_context: contexts.taxonomy,
    issue_playbook: contexts.playbook,
    route_policy: contexts.routePolicy,
    product_context: {
      schema_version: contexts.productContext.schema_version,
      doctrine_version: contexts.productContext.doctrine_version,
      source_note: contexts.productContext.source_note,
      universal_facts: contexts.productContext.universal_facts,
      line_contexts: {
        [line]: contexts.productContext.line_contexts?.[line]
          || contexts.productContext.line_contexts?.unknown
      },
      marketing_claim_policy: contexts.productContext.marketing_claim_policy,
      ...(matchingSku ? {sku_match: matchingSku} : {})
    },
    review_input: reviewInput
  };
}

export function generateCaseCode(reviewId, reviewVersion) {
  const letters = [..."АБВГДЕЖЗИКЛМНОПРСТУФХЦЧШЭЮЯ"];
  const digest = createHash("sha256").update(`${reviewId}:${reviewVersion}`).digest();
  const letter = letters[digest[0] % letters.length];
  const number = digest.readUInt32BE(1) % 10000;
  return `${letter}${String(number).padStart(4, "0")}`;
}

function selectProductFacts(reviewInput, productContext) {
  const facts = productContext.universal_facts.map((item) => ({...item, source: "universal"}));
  const line = reviewInput.product.line;
  const lineFacts = productContext.line_contexts?.[line]?.confirmed_facts || [];
  lineFacts.forEach((fact, index) => facts.push({
    id: `LINE_${line.toUpperCase()}_${index + 1}`,
    fact,
    source: "product_line"
  }));
  return facts;
}

function selectPlaybookFragments(classification, playbook) {
  const codes = new Set((classification.issues || []).map((item) => item.code));
  return playbook.issues.filter((item) => codes.has(item.code)).map((item) => ({
    code: item.code,
    label: item.label,
    allowed_routes: item.allowed_routes,
    decision_rule: item.decision_rule,
    potential_evidence: item.potential_evidence,
    prohibited_claims: item.prohibited_claims
  }));
}

function selectPositiveGuidance(classification, playbook) {
  const codes = new Set((classification.positive_signals || []).map((item) => item.code));
  return playbook.positive_signals.filter((item) => codes.has(item.code));
}

function selectApprovedExamples(reviewInput, classification, finalRoute, approvedExamples) {
  const codes = new Set((classification.issues || []).map((item) => item.code));
  const text = reviewInput.review.normalized_text.toLowerCase();
  const selected = approvedExamples.examples.filter((example) => {
    if (example.route !== finalRoute) return false;
    if (example.issue_subtype && example.issue_subtype !== classification.primary_issue_subtype) return false;
    if (!example.issue_codes.every((code) => codes.has(code))) return false;
    if (example.case_id === "A03" && !/(как|где).{0,30}(написать|связаться).{0,30}продав/.test(text)) return false;
    if (example.case_id === "A08" && !/(реш|выслал|новое|новое стекло|всё хорошо)/.test(text)) return false;
    return true;
  });
  return selected.slice(0, 3);
}

export function defaultDraftConstraints(classification, finalRoute) {
  const primary = classification.primary_issue;
  const mustInclude = [];
  if (primary) mustInclude.push(`Обработать основную тему ${primary}.`);
  if (finalRoute === "seller_chat") mustInclude.push("Пригласить в чат продавца и указать case code.");
  if (finalRoute === "wb_return") mustInclude.push("Указать официальный возврат Wildberries как единственный следующий шаг.");
  if (finalRoute === "wb_support") mustInclude.push("Указать поддержку Wildberries как единственный следующий шаг.");
  return {
    must_start_with: "Здравствуйте.",
    must_include: mustInclude,
    must_not_include: [
      "эмодзи, восклицательные знаки, CAPS, имя покупателя или подпись брендом",
      "обещание замены, компенсации, возврата денег или решения WB",
      "обвинение покупателя или неподтверждённая причина",
      "второй маршрут или второй CTA"
    ],
    cta: finalRoute,
    target_sentences: "3-4",
    target_chars: "250-550",
    soft_max_chars: 700,
    hard_max_chars: 900,
    positive_signals_to_reflect: (classification.positive_signals || []).map((item) => item.code)
  };
}

export async function buildWriterRequest(reviewInput, rawClassification, draftConstraints = null) {
  const contexts = await loadStaticContexts();
  const guard = applyRouteGuards(rawClassification);
  const classification = guard.classification;
  const finalRoute = classification.route;
  const runtimePositiveSignals = (classification.positive_signals || []).map((item) => item.code);
  const effectiveDraftConstraints = draftConstraints
    ? {...draftConstraints, positive_signals_to_reflect: runtimePositiveSignals}
    : defaultDraftConstraints(classification, finalRoute);
  const caseCode = finalRoute === "seller_chat"
    ? generateCaseCode(reviewInput.review.review_id, reviewInput.review.review_version)
    : null;
  return {
    request: {
      request_schema_version: "1.0.0",
      review_input: reviewInput,
      classification,
      final_route: finalRoute,
      case_code: caseCode,
      product_facts: selectProductFacts(reviewInput, contexts.productContext),
      playbook_fragments: selectPlaybookFragments(classification, contexts.playbook),
      positive_guidance: selectPositiveGuidance(classification, contexts.playbook),
      approved_examples: selectApprovedExamples(reviewInput, classification, finalRoute, contexts.approvedExamples),
      draft_constraints: effectiveDraftConstraints
    },
    guard_events: guard.events
  };
}

export function buildValidatorRequest(writerRequest, draft, attemptNumber = 0) {
  return {
    request_schema_version: "1.0.0",
    review_input: writerRequest.review_input,
    classification: writerRequest.classification,
    draft,
    final_route: writerRequest.final_route,
    case_code: writerRequest.case_code,
    product_facts: writerRequest.product_facts,
    playbook_fragments: writerRequest.playbook_fragments,
    positive_guidance: writerRequest.positive_guidance,
    approved_examples: writerRequest.approved_examples,
    draft_constraints: writerRequest.draft_constraints,
    attempt_number: attemptNumber,
    max_rewrites: 2
  };
}

export function buildRewriteRequest(validatorRequest, validation, attemptNumber) {
  return {
    request_schema_version: "1.0.0",
    review_input: validatorRequest.review_input,
    classification: validatorRequest.classification,
    previous_draft: validatorRequest.draft,
    validation,
    final_route: validatorRequest.final_route,
    case_code: validatorRequest.case_code,
    product_facts: validatorRequest.product_facts,
    playbook_fragments: validatorRequest.playbook_fragments,
    positive_guidance: validatorRequest.positive_guidance,
    approved_examples: validatorRequest.approved_examples,
    draft_constraints: validatorRequest.draft_constraints,
    attempt_number: attemptNumber,
    max_rewrites: 2
  };
}

export function asTaggedInput(request) {
  return `<request_json>\n${JSON.stringify(request)}\n</request_json>`;
}

function taggedJson(tag, value) {
  return `<${tag}>\n${JSON.stringify(value)}\n</${tag}>`;
}

/**
 * Builds an explicit-cache Responses API input. The breakpoint is placed after
 * the exact reusable prefix; all per-review data is rendered after it.
 * Rewrite is intentionally uncached because it is rare and its system prompt is
 * too short to justify a billable GPT-5.6 cache write.
 */
export function buildCacheableInput(role, request) {
  if (role === "rewrite") {
    return {
      cacheable: false,
      static_prefix_hash: null,
      input: asTaggedInput(request)
    };
  }

  let staticPayload;
  let dynamicPayload;
  if (role === "classifier") {
    const {review_input: reviewInput, product_context: productContext, ...sharedContext} = request;
    const {line_contexts: lineContexts, sku_match: skuMatch, ...universalProductContext} = productContext;
    staticPayload = {
      ...sharedContext,
      universal_product_context: universalProductContext
    };
    dynamicPayload = {
      review_input: reviewInput,
      product_line_context: lineContexts,
      ...(skuMatch ? {sku_match: skuMatch} : {})
    };
  } else {
    staticPayload = {
      cache_protocol_version: "1.1.0",
      role,
      note: "The role instructions rendered before this boundary are static. The request JSON follows after the boundary."
    };
    dynamicPayload = request;
  }

  const staticText = taggedJson("static_context_json", staticPayload);
  const dynamicText = taggedJson("request_json", dynamicPayload);
  return {
    cacheable: true,
    static_prefix_hash: createHash("sha256").update(staticText).digest("hex"),
    input: [{
      type: "message",
      role: "user",
      content: [
        {
          type: "input_text",
          text: staticText,
          prompt_cache_breakpoint: {mode: "explicit"}
        },
        {
          type: "input_text",
          text: dynamicText
        }
      ]
    }]
  };
}

export function schemaForStructuredOutput(schema) {
  const clone = structuredClone(schema);
  delete clone.$schema;
  delete clone.$id;
  return clone;
}
