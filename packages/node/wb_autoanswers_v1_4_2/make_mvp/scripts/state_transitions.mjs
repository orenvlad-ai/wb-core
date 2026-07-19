const ALLOWED = Object.freeze({
  received: new Set(["normalized", "technical_failed_terminal"]),
  normalized: new Set(["skipped", "classified", "technical_failed_retryable", "technical_failed_terminal"]),
  classified: new Set(["guarded", "technical_failed_retryable", "technical_failed_terminal"]),
  guarded: new Set(["drafted", "technical_failed_retryable", "technical_failed_terminal"]),
  drafted: new Set(["validated", "technical_failed_retryable", "technical_failed_terminal"]),
  validated: new Set(["ready", "rewrite_1", "rewrite_2", "fallback_ready", "technical_failed_retryable", "technical_failed_terminal"]),
  rewrite_1: new Set(["validated", "technical_failed_retryable", "technical_failed_terminal"]),
  rewrite_2: new Set(["validated", "technical_failed_retryable", "technical_failed_terminal"]),
  fallback_ready: new Set(["delivered_to_telegram", "technical_failed_retryable", "technical_failed_terminal"]),
  ready: new Set(["delivered_to_telegram", "technical_failed_retryable", "technical_failed_terminal"]),
  skipped: new Set(["delivered_to_telegram"]),
  technical_failed_retryable: new Set(["normalized", "classified", "guarded", "drafted", "validated", "technical_failed_terminal"]),
  technical_failed_terminal: new Set(),
  delivered_to_telegram: new Set()
});

export function transitionState(current, next) {
  if (!ALLOWED[current]) throw new Error(`UNKNOWN_STATE:${current}`);
  if (!ALLOWED[current].has(next)) throw new Error(`ILLEGAL_STATE_TRANSITION:${current}->${next}`);
  return next;
}

export function stateEvent(from, to, now = new Date(), metadata = {}) {
  return {
    from,
    to: transitionState(from, to),
    at: now.toISOString(),
    metadata: structuredClone(metadata)
  };
}

export const STATES = Object.freeze(Object.keys(ALLOWED));
