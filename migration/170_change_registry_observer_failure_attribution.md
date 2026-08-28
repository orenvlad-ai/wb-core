# Migration 170 — deterministic observer failure attribution

Status: additive live-runtime reliability/evidence change.

This bounded migration extends immutable Change Registry observer job events
with typed, sanitized source and persistence failure evidence. Existing event
rows receive inert defaults; no historical event, failed job, checkpoint,
source manifest, observation, fact, health row or lease revision is replayed or
rewritten.

The observer records explicit stages for baseline ingest/result, each source
manifest insert, terminal job event, scheduled health, lease release and commit.
SQLite failures retain only the logical table/operation, numeric/name identity,
an allowlisted constraint category and safe identifier, a bounded generated
message and deterministic digest. SQL, raw WB payloads, file paths, credentials,
tokens and secrets are excluded. Source acquisition failure is a separate typed
origin. If fallback failure persistence rolls back, one rescue transaction
stores both the unchanged primary evidence and typed fallback evidence without
hiding the primary exception.

Atomic checkpoint/result rollback, scheduled-slot idempotency, lease CAS,
two-failure degradation, manual-counter isolation, baseline-zero semantics and
the one-submit/no-blind-retry boundary are unchanged. There is no WB write,
manual scan, historical replay, timer/service schedule change, new writer,
product/UI change, production-mutation manifest or deploy-time business-data
apply.

Verification:

```bash
python3 apps/change_registry_observer_smoke.py
python3 apps/change_registry_baseline_engine_smoke.py
python3 apps/change_registry_smoke.py
python3 apps/change_registry_source_acquisition_smoke.py
python3 apps/change_registry_internal_writers_smoke.py
```

The observer smoke injects real SQLite `IntegrityError` objects at every
applicable persistence stage, proves zero atomic-result rows after rollback,
typed durable primary/fallback evidence, sanitizer and size bounds, replay
idempotency, health/lease behavior and zero WB writes.
