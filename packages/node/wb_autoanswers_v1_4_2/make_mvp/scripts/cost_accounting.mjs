import {
  aggregateCaseUsages,
  calculateCostUsd,
  normalizeResponseUsage,
  summarizeApiTrace
} from "../frozen_bundle/tools/usage_metrics.mjs";

export function usageRecord({role, responseId = null, usage = null, latencyMs = null}) {
  return {
    role,
    response_id: responseId,
    latency_ms: Number.isFinite(latencyMs) && latencyMs >= 0 ? latencyMs : null,
    usage_reported: Boolean(usage),
    usage: usage || null,
    normalized_usage: normalizeResponseUsage(usage)
  };
}

export function calculateJobCost(trace, pricingProfile) {
  const evaluatorTrace = trace.map((item) => ({
    role: item.role,
    usage_reported: item.usage_reported,
    usage: item.usage
  }));
  const summary = summarizeApiTrace(evaluatorTrace);
  return {usage: summary, estimated_cost_usd: calculateCostUsd(summary, pricingProfile)};
}

export {aggregateCaseUsages, calculateCostUsd, normalizeResponseUsage, summarizeApiTrace};
