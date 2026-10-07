"""Provider failure categories shared by the boundary and durable workers."""

TRANSIENT_PROVIDER_CODES = frozenset({
    "OPENAI_NETWORK", "OPENAI_TIMEOUT", "OPENAI_RESPONSE_INVALID",
    "OPENAI_OUTPUT_NOT_JSON", "OPENAI_OUTPUT_MISSING",
    "node_timeout", "node_invalid_json", "node_process_exit_1",
    "node_process_exit_-9", "node_process_exit_-15",
})
# These historical boundary failures have no trustworthy generated reply. They
# may only recover through the existing zero-cost template, never a paid loop.
LEGACY_TECHNICAL_CODES = frozenset({"NODE_BOUNDARY_ERROR", "ENOENT"})


def transient_provider_failure(code: str) -> bool:
    return code in TRANSIENT_PROVIDER_CODES or code in {"OPENAI_HTTP_429", "OPENAI_INSUFFICIENT_QUOTA"} or code.startswith("OPENAI_HTTP_5")


def recoverable_technical_failure(code: str) -> bool:
    return code in LEGACY_TECHNICAL_CODES or transient_provider_failure(code)


def provider_cost_uncertain(code: str) -> bool:
    return code in TRANSIENT_PROVIDER_CODES or code == "NODE_BOUNDARY_ERROR"


def provider_backoff(attempt: int, retry_after_seconds: int = 0) -> int:
    return max(60 * 2 ** min(4, max(0, attempt - 1)), max(0, retry_after_seconds))
