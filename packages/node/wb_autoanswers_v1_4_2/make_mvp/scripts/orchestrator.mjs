import {
  buildClassifierRequest,
  buildRewriteRequest,
  buildValidatorRequest,
  buildWriterRequest,
  readJson
} from "../frozen_bundle/tools/build_context.mjs";
import {auditEvent} from "./audit.mjs";
import {allocateCaseCode, assertCaseCode} from "./case_code.mjs";
import {MAX_REWRITES, PROMPT_BUNDLE_VERSION} from "./constants.mjs";
import {calculateJobCost, usageRecord} from "./cost_accounting.mjs";
import {runDraftGuard} from "./draft_guard.mjs";
import {selectApprovedFallback} from "./fallback.mjs";
import {isReusableJob, makeIdempotencyKey} from "./idempotency.mjs";
import {MemoryStore} from "./memory_store.mjs";
import {normalizeTelegramInput} from "./normalizer.mjs";
import {buildResponsesPayload} from "./payload_builder.mjs";
import {assertGuardInvariants} from "./route_guard.mjs";
import {
  assertContract,
  assertRoleOutput,
  assertRoleRequest
} from "./schema_validation.mjs";
import {stateEvent} from "./state_transitions.mjs";

const VALIDATION_CODE_BY_GUARD = Object.freeze({
  GREETING_INVALID: "GREETING_INVALID",
  LENGTH_HARD: "LENGTH_HARD",
  PROMISE_UNAUTHORIZED: "PROMISE_UNAUTHORIZED",
  CASE_CODE_INVALID: "CASE_CODE_INVALID",
  ROUTE_MISMATCH: "ROUTE_MISMATCH",
  CTA_MISMATCH: "CTA_MISMATCH"
});

function nowDate(now) {
  const value = now();
  return value instanceof Date ? value : new Date(value);
}

function responseEnvelope(response) {
  if (response && typeof response === "object" && "output" in response) return response;
  return {output: response, usage: null, response_id: null, latency_ms: null};
}

function rewriteValidation(rawValidation, guardErrors, mustRewrite) {
  const effective = structuredClone(rawValidation);
  if (guardErrors.length > 0) {
    for (const guardError of guardErrors) {
      effective.violations.push({
        code: VALIDATION_CODE_BY_GUARD[guardError] || "OTHER",
        severity: "error",
        evidence: `Deterministic draft guard: ${guardError}`,
        rewrite_instruction: `Исправить детерминированное нарушение ${guardError}, сохранив classification и final_route.`
      });
      effective.rewrite_instructions.push(`Исправить ${guardError}; classification и final_route неизменны.`);
    }
  }
  if (mustRewrite) effective.status = "rewrite";
  return effective;
}

function normalizedSubtype(subtype) {
  return ["delivery_delay", "seller_support_no_response", "promo_access_blocked"].includes(subtype)
    ? subtype
    : null;
}

function resultVersions() {
  return {
    doctrine: "1.0",
    product_context: "1.0.0",
    classifier_prompt: PROMPT_BUNDLE_VERSION,
    writer_prompt: PROMPT_BUNDLE_VERSION,
    validator_prompt: PROMPT_BUNDLE_VERSION,
    model: "gpt-5.6-terra"
  };
}

function skipResult(reviewInput) {
  return {
    schema_version: "1.0.0",
    review_id: reviewInput.review.review_id,
    review_version: reviewInput.review.review_version,
    publication_action: "skip",
    skip_reason: "empty_five_star",
    review_mode: null,
    primary_issue: null,
    primary_issue_subtype: null,
    positive_signals: [],
    route: null,
    route_reason: null,
    case_code: null,
    requested_materials: [],
    draft_history: [],
    validation_status_history: [],
    final_reply: null,
    outcome: "skipped",
    model_call_count: 0,
    versions: resultVersions()
  };
}

function replyResult({reviewInput, classification, caseCode, drafts, validations, finalDraft, outcome, modelCallCount}) {
  return {
    schema_version: "1.0.0",
    review_id: reviewInput.review.review_id,
    review_version: reviewInput.review.review_version,
    publication_action: "reply",
    skip_reason: null,
    review_mode: classification.review_mode,
    primary_issue: classification.primary_issue,
    primary_issue_subtype: normalizedSubtype(classification.primary_issue_subtype),
    positive_signals: (classification.positive_signals || []).map((item) => item.code),
    route: classification.route,
    route_reason: classification.route_reason,
    case_code: caseCode,
    requested_materials: classification.route === "seller_chat" ? [] : [...(finalDraft.requested_materials || [])],
    draft_history: drafts.map((item) => item.draft_reply),
    validation_status_history: validations,
    final_reply: finalDraft.draft_reply,
    outcome,
    model_call_count: modelCallCount,
    versions: resultVersions()
  };
}

/**
 * Local, adapter-driven orchestration. The function has no network implementation;
 * Make supplies roleRunner and deliverToManager, while tests supply inert mocks.
 */
export async function runJob(rawInput, dependencies = {}) {
  const store = dependencies.store || new MemoryStore();
  const roleRunner = dependencies.roleRunner;
  const deliverToManager = dependencies.deliverToManager || null;
  const now = dependencies.now || (() => new Date());
  if (typeof roleRunner !== "function") throw new Error("ROLE_RUNNER_REQUIRED");

  const [productContext, fallbackLibrary, pricingProfile] = await Promise.all([
    readJson("contracts/product_context.json"),
    readJson("contracts/route_fallbacks.json"),
    readJson("pricing_profiles/terra.json")
  ]);
  const reviewInput = normalizeTelegramInput(rawInput, productContext, {now});
  await assertContract("review_input.schema.json", reviewInput);
  const idempotencyKey = makeIdempotencyKey({
    reviewId: reviewInput.review.review_id,
    reviewVersion: reviewInput.review.review_version,
    promptBundleVersion: PROMPT_BUNDLE_VERSION
  });

  const existing = await store.getJob(idempotencyKey);
  if (isReusableJob(existing)) {
    await store.appendAudit(idempotencyKey, auditEvent("idempotency_reuse", {state: existing.state}, nowDate(now)));
    return {
      idempotency_key: idempotencyKey,
      idempotent_reuse: true,
      model_calls_this_run: 0,
      state: existing.state,
      result: existing.result,
      manager_message: existing.manager_message
    };
  }

  const job = {
    idempotency_key: idempotencyKey,
    state: "received",
    outcome: null,
    state_history: [],
    created_at: nowDate(now).toISOString(),
    updated_at: nowDate(now).toISOString(),
    result: null,
    manager_message: null,
    error: null
  };
  const trace = [];
  let modelCallCount = 0;

  async function persist() {
    job.updated_at = nowDate(now).toISOString();
    await store.putJob(idempotencyKey, job);
  }

  async function transition(next, metadata = {}) {
    const event = stateEvent(job.state, next, nowDate(now), metadata);
    job.state_history.push(event);
    job.state = next;
    await persist();
    await store.appendAudit(idempotencyKey, auditEvent("state_transition", event, nowDate(now)));
  }

  async function callRole(role, request) {
    await assertRoleRequest(role, request);
    const payload = await buildResponsesPayload(role, request, reviewInput.review.review_id);
    const started = nowDate(now);
    const rawResponse = await roleRunner({role, request: structuredClone(request), payload: structuredClone(payload), call_index: modelCallCount});
    const response = responseEnvelope(rawResponse);
    modelCallCount += 1;
    const measuredLatency = Number.isFinite(response.latency_ms)
      ? response.latency_ms
      : Math.max(0, nowDate(now).getTime() - started.getTime());
    const usage = usageRecord({
      role,
      responseId: response.response_id,
      usage: response.usage,
      latencyMs: measuredLatency
    });
    trace.push(usage);
    await store.appendAudit(idempotencyKey, auditEvent("role_call", {
      role,
      request,
      response_id: response.response_id,
      usage,
      payload_metadata: {
        model: payload.model,
        store: payload.store,
        reasoning: payload.reasoning,
        prompt_cache_key: payload.prompt_cache_key || null,
        strict: payload.text?.format?.strict,
        schema_name: payload.text?.format?.name
      },
      output: response.output
    }, nowDate(now)));
    await assertRoleOutput(role, response.output);
    return response.output;
  }

  try {
    await persist();
    await store.appendAudit(idempotencyKey, auditEvent("normalized_input", reviewInput, nowDate(now)));
    await transition("normalized");

    if (!reviewInput.prefilter.model_calls_allowed) {
      const result = skipResult(reviewInput);
      await assertContract("pipeline_result.schema.json", result);
      job.result = result;
      job.outcome = result.outcome;
      job.manager_message = "Отзыв пропущен: пустой пятизвёздочный отзыв не требует публичного ответа.";
      await transition("skipped", {reason: result.skip_reason});
      if (deliverToManager) {
        await deliverToManager({text: job.manager_message, publication_action: "skip", idempotency_key: idempotencyKey});
        await transition("delivered_to_telegram");
      }
      await persist();
      return {idempotency_key: idempotencyKey, idempotent_reuse: false, model_calls_this_run: 0, state: job.state, result, manager_message: job.manager_message};
    }

    const classifierRequest = await buildClassifierRequest(reviewInput);
    const rawClassification = await callRole("classifier", classifierRequest);
    await transition("classified");

    const builtWriter = await buildWriterRequest(reviewInput, rawClassification);
    const classification = builtWriter.request.classification;
    await assertRoleOutput("classifier", classification);
    const invariantErrors = assertGuardInvariants(classification);
    if (invariantErrors.length > 0) throw new Error(`ROUTE_GUARD_INVARIANT:${invariantErrors.join("|")}`);
    await store.appendAudit(idempotencyKey, auditEvent("route_guard", {
      raw_route: rawClassification.route,
      final_route: classification.route,
      events: builtWriter.guard_events
    }, nowDate(now)));
    await transition("guarded", {final_route: classification.route});

    const existingCodes = await store.listCaseCodes();
    const caseCode = allocateCaseCode({
      finalRoute: classification.route,
      reviewId: reviewInput.review.review_id,
      reviewVersion: reviewInput.review.review_version,
      idempotencyKey,
      existing: existingCodes
    });
    assertCaseCode(classification.route, caseCode);
    builtWriter.request.case_code = caseCode;
    if (caseCode) {
      await store.reserveCaseCode({
        case_code: caseCode,
        idempotency_key: idempotencyKey,
        review_id: reviewInput.review.review_id,
        review_version: reviewInput.review.review_version,
        prompt_bundle_version: PROMPT_BUNDLE_VERSION,
        active: true,
        created_at: nowDate(now).toISOString()
      });
    }

    let draft = await callRole("writer", builtWriter.request);
    const drafts = [draft];
    await transition("drafted");

    let validatorRequest = buildValidatorRequest(builtWriter.request, draft, 0);
    let validation = await callRole("validator", validatorRequest);
    let guardErrors = runDraftGuard(draft, builtWriter.request);
    let effectiveValidation = rewriteValidation(validation, guardErrors, validation.status !== "pass" || guardErrors.length > 0);
    const validations = [effectiveValidation.status];
    await transition("validated", {attempt: 0, status: effectiveValidation.status, deterministic_errors: guardErrors});

    let rewriteCount = 0;
    while (effectiveValidation.status !== "pass" && rewriteCount < MAX_REWRITES) {
      rewriteCount += 1;
      await transition(`rewrite_${rewriteCount}`, {attempt: rewriteCount});
      const rewriteRequest = buildRewriteRequest(validatorRequest, effectiveValidation, rewriteCount);
      draft = await callRole("rewrite", rewriteRequest);
      drafts.push(draft);
      validatorRequest = buildValidatorRequest(builtWriter.request, draft, rewriteCount);
      validation = await callRole("validator", validatorRequest);
      guardErrors = runDraftGuard(draft, builtWriter.request);
      effectiveValidation = rewriteValidation(validation, guardErrors, validation.status !== "pass" || guardErrors.length > 0);
      validations.push(effectiveValidation.status);
      await transition("validated", {attempt: rewriteCount, status: effectiveValidation.status, deterministic_errors: guardErrors});
    }

    let finalDraft = draft;
    let outcome = "ready";
    let fallbackId = null;
    if (effectiveValidation.status !== "pass" || guardErrors.length > 0) {
      const fallback = selectApprovedFallback(fallbackLibrary, classification, classification.route, caseCode);
      fallbackId = fallback.fallback_id;
      finalDraft = fallback.draft;
      await assertRoleOutput("writer", finalDraft);
      const fallbackGuardErrors = runDraftGuard(finalDraft, builtWriter.request);
      if (fallbackGuardErrors.length > 0) throw new Error(`APPROVED_FALLBACK_GUARD_FAILED:${fallbackGuardErrors.join("|")}`);
      validations[validations.length - 1] = "fallback";
      outcome = "fallback";
      await store.appendAudit(idempotencyKey, auditEvent("approved_fallback", {fallback_id: fallbackId, final_route: classification.route}, nowDate(now)));
      await transition("fallback_ready", {fallback_id: fallbackId});
    } else {
      await transition("ready");
    }

    const result = replyResult({
      reviewInput,
      classification,
      caseCode,
      drafts,
      validations,
      finalDraft,
      outcome,
      modelCallCount
    });
    await assertContract("pipeline_result.schema.json", result);
    const cost = calculateJobCost(trace, pricingProfile);
    await store.appendAudit(idempotencyKey, auditEvent("job_complete", {
      outcome,
      fallback_id: fallbackId,
      model_call_count: modelCallCount,
      cost,
      final_reply: finalDraft.draft_reply
    }, nowDate(now)));
    await store.rememberReply({
      idempotency_key: idempotencyKey,
      route: classification.route,
      primary_issue: classification.primary_issue,
      reply: finalDraft.draft_reply,
      created_at: nowDate(now).toISOString()
    });
    job.result = result;
    job.outcome = result.outcome;
    job.manager_message = finalDraft.draft_reply;
    await persist();

    if (deliverToManager) {
      await deliverToManager({text: finalDraft.draft_reply, publication_action: "reply", idempotency_key: idempotencyKey});
      await transition("delivered_to_telegram");
    }
    await persist();
    return {
      idempotency_key: idempotencyKey,
      idempotent_reuse: false,
      model_calls_this_run: modelCallCount,
      state: job.state,
      result,
      manager_message: job.manager_message,
      usage: cost.usage,
      estimated_cost_usd: cost.estimated_cost_usd,
      fallback_id: fallbackId
    };
  } catch (error) {
    job.error = {name: error.name, message: error.message};
    job.outcome = "terminal_error";
    const terminalStates = new Set(["technical_failed_terminal", "delivered_to_telegram"]);
    if (!terminalStates.has(job.state)) {
      try {
        await transition("technical_failed_terminal", {error: job.error});
      } catch {
        job.state = "technical_failed_terminal";
        await persist();
      }
    }
    await store.appendAudit(idempotencyKey, auditEvent("technical_failure", job.error, nowDate(now)));
    error.idempotency_key = idempotencyKey;
    throw error;
  }
}
