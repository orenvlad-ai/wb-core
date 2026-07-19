export const API_ROLES = Object.freeze(["classifier", "writer", "validator", "rewrite"]);
export const TOKEN_FIELDS = Object.freeze([
  "input_tokens",
  "cached_input_tokens",
  "cache_write_input_tokens",
  "output_tokens",
  "reasoning_tokens",
  "total_tokens"
]);

function finiteNonNegative(value) {
  return Number.isFinite(value) && value >= 0 ? value : 0;
}

export function emptyUsageBucket() {
  return {
    api_calls: 0,
    usage_missing_api_calls: 0,
    input_tokens: 0,
    cached_input_tokens: 0,
    cache_write_input_tokens: 0,
    output_tokens: 0,
    reasoning_tokens: 0,
    total_tokens: 0
  };
}

export function normalizeResponseUsage(rawUsage) {
  return {
    input_tokens: finiteNonNegative(rawUsage?.input_tokens),
    cached_input_tokens: finiteNonNegative(rawUsage?.input_tokens_details?.cached_tokens),
    cache_write_input_tokens: finiteNonNegative(rawUsage?.input_tokens_details?.cache_write_tokens),
    output_tokens: finiteNonNegative(rawUsage?.output_tokens),
    reasoning_tokens: finiteNonNegative(rawUsage?.output_tokens_details?.reasoning_tokens),
    total_tokens: finiteNonNegative(rawUsage?.total_tokens)
  };
}

function addBucket(target, source) {
  target.api_calls += finiteNonNegative(source?.api_calls);
  target.usage_missing_api_calls += finiteNonNegative(source?.usage_missing_api_calls);
  for (const field of TOKEN_FIELDS) target[field] += finiteNonNegative(source?.[field]);
  return target;
}

export function summarizeApiTrace(trace = []) {
  const byRole = Object.fromEntries(API_ROLES.map((role) => [role, emptyUsageBucket()]));
  const total = emptyUsageBucket();

  for (const call of trace) {
    if (!API_ROLES.includes(call.role)) continue;
    const callUsage = {
      api_calls: 1,
      usage_missing_api_calls: call.usage_reported === false ? 1 : 0,
      ...normalizeResponseUsage(call.usage)
    };
    addBucket(byRole[call.role], callUsage);
    addBucket(total, callUsage);
  }

  return {
    api_calls: {
      total: total.api_calls,
      ...Object.fromEntries(API_ROLES.map((role) => [role, byRole[role].api_calls]))
    },
    usage_missing_api_calls: total.usage_missing_api_calls,
    input_tokens: total.input_tokens,
    cached_input_tokens: total.cached_input_tokens,
    cache_write_input_tokens: total.cache_write_input_tokens,
    output_tokens: total.output_tokens,
    reasoning_tokens: total.reasoning_tokens,
    total_tokens: total.total_tokens,
    by_role: byRole
  };
}

export function aggregateCaseUsages(caseUsages = []) {
  const byRole = Object.fromEntries(API_ROLES.map((role) => [role, emptyUsageBucket()]));
  const total = emptyUsageBucket();

  for (const usage of caseUsages) {
    const caseBucket = {
      api_calls: usage?.api_calls?.total,
      usage_missing_api_calls: usage?.usage_missing_api_calls,
      ...Object.fromEntries(TOKEN_FIELDS.map((field) => [field, usage?.[field]]))
    };
    addBucket(total, caseBucket);
    for (const role of API_ROLES) addBucket(byRole[role], usage?.by_role?.[role] || emptyUsageBucket());
  }

  return {
    api_calls: {
      total: total.api_calls,
      ...Object.fromEntries(API_ROLES.map((role) => [role, byRole[role].api_calls]))
    },
    usage_missing_api_calls: total.usage_missing_api_calls,
    input_tokens: total.input_tokens,
    cached_input_tokens: total.cached_input_tokens,
    cache_write_input_tokens: total.cache_write_input_tokens,
    output_tokens: total.output_tokens,
    reasoning_tokens: total.reasoning_tokens,
    total_tokens: total.total_tokens,
    by_role: byRole
  };
}

function describeValues(values) {
  if (values.length === 0) return {mean: 0, median: 0, p95: 0};
  const sorted = [...values].sort((left, right) => left - right);
  const mean = sorted.reduce((sum, value) => sum + value, 0) / sorted.length;
  const middle = Math.floor(sorted.length / 2);
  const median = sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
  const p95 = sorted[Math.max(0, Math.ceil(sorted.length * 0.95) - 1)];
  return {mean, median, p95};
}

export function tokenDistribution(caseUsages = []) {
  return Object.fromEntries(TOKEN_FIELDS.map((field) => [
    field,
    describeValues(caseUsages.map((usage) => finiteNonNegative(usage?.[field])))
  ]));
}

export function calculateCostUsd(usage, pricingProfile) {
  if (!pricingProfile?.rates || !pricingProfile?.unit_tokens) return null;
  const inputTokens = finiteNonNegative(usage?.input_tokens);
  const cachedInputTokens = Math.min(inputTokens, finiteNonNegative(usage?.cached_input_tokens));
  const cacheWriteInputTokens = Math.min(
    inputTokens - cachedInputTokens,
    finiteNonNegative(usage?.cache_write_input_tokens)
  );
  const uncachedInputTokens = inputTokens - cachedInputTokens - cacheWriteInputTokens;
  const outputTokens = finiteNonNegative(usage?.output_tokens);
  const cost = (
    uncachedInputTokens * pricingProfile.rates.input_tokens
    + cachedInputTokens * pricingProfile.rates.cached_input_tokens
    + cacheWriteInputTokens * pricingProfile.rates.cache_write_input_tokens
    + outputTokens * pricingProfile.rates.output_tokens
  ) / pricingProfile.unit_tokens;
  return Number(cost.toFixed(8));
}
