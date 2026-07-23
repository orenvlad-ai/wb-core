# Ads upstream shape evidence

The production `ads_historical_recovery_v3` dry-run still received a mapping
from a singleton `fullstats` request that did not match the exact accepted
no-statistics envelope. The recovery correctly produced no write set, but the
former blocker exposed only the Python type and was insufficient to distinguish
a documented wrapper from an unrelated upstream failure.

The fail-closed diagnostic now records only a bounded safe shape: payload type,
canonical digest, at most 50 key names and the allowlisted scalar fields
`status`, `origin`, `detail` and `title` truncated to 500 characters. It never
copies raw payload values, request IDs, authorization data or arbitrary nested
content. No additional response is accepted and apply semantics do not change.
