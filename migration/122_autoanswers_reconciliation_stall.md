# Migration 122 — Autoanswers reconciliation stall recovery

## Incident and preserved truth

Production evidence showed one automatic sweep repeatedly selecting the same
five stale published 1-star members. `published_preserved` returned without a
durable member-level progress record, so every worker tick reported another
synthetic `+5` and never reached real actions. A stale 1-star
`terminal_error/regeneration_required` member also held the automatic priority
barrier even though only a human could resolve it.

The repair preserves the current transition run, policy epoch, owner-confirmed
membership and run cap. Frozen bundle `1.4.2`, AI job execution, provider-call
boundaries, reservations/costs/holds, publication aggregates, POST attempts
and WB readback evidence are immutable and are never rewritten to simulate
progress.

## Schema v8 and scheduler contract

Canonical `prepare-deploy` creates and verifies the current pre-v8 backup, then
atomically adds
`sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements`.
Its primary identity is exact
`sweep_id + feedback_id + content_version`; the row also binds the content
hash, policy epoch, transition run, outcome/outcome class, candidate
fingerprint and acknowledgement time.

Every selected reconciliation member receives at most one acknowledgement.
Missing acknowledgements remain selectable after restart, including a job
whose action committed immediately before interruption. Progress is rebuilt
from acknowledgement rows, never by adding per-tick return counts.

Candidate ordering is:

1. an automatic action in the current literal priority bucket;
2. preserved terminal/human-only evidence;
3. already-current unchanged bookkeeping.

`needs_review`, `terminal_error`, `skipped` and `published` rows remain visible
but do not hold the automatic barrier. Started WB writes retain mandatory GET
readback priority and cannot create another POST.

Runtime observability exposes exact acknowledged/action/preserved/unchanged
counts, remaining membership, recent delta rate, ETA, last progress/action,
repeated candidate fingerprint, bucket age, real AI completions, confirmed WB
publications and sanitized SQLite contention. A budget-free automatic bucket
with no claimable work or real output for 15 minutes is an explicit stall.
Budget/rate/retry pauses are not mislabeled as stalls.

## Fingerprint-bound production recovery

The repo-owned runner defaults to query-only planning:

```bash
python3 apps/wb_autoanswers_reconciliation_recovery.py dry-run \
  --runtime-dir <canonical-runtime-dir> \
  --sweep-id <exact-sweep-id> \
  --policy-epoch <exact-policy-epoch> \
  --transition-run-id <exact-run-id> \
  --expected-candidates <exact-count>
```

Apply requires the exact plan fingerprint and verified pre-v8 backup:

```bash
python3 apps/wb_autoanswers_reconciliation_recovery.py apply \
  --runtime-dir <canonical-runtime-dir> \
  --sweep-id <exact-sweep-id> \
  --policy-epoch <exact-policy-epoch> \
  --transition-run-id <exact-run-id> \
  --expected-candidates <exact-count> \
  --expected-fingerprint sha256:<exact-plan>
```

Dry-run/readback open SQLite with URI `mode=ro` and
`PRAGMA query_only=ON`. Apply re-plans under `BEGIN IMMEDIATE`, inserts only
the exact missing preservation acknowledgements, rebuilds the derived sweep
projection and appends one audit event. The fingerprint binds exact member
execution/publication/readback projections, run/caps and backup identity.
Apply compares the non-target snapshot before/after and rolls back on any
change. Readback verifies every member fingerprint, the immutable target
execution/publication/cost projection and the sweep identity/caps. Unrelated
queue progress after the committed apply does not invalidate that proof; exact
replay is a confirmed no-op. Ad-hoc SQL and server-only scripts are prohibited.

## Verification and production acceptance

Required checks include:

```text
PYTHONPATH=. python3 -m unittest \
  apps.wb_autoanswers_runtime_test \
  apps.wb_autoanswers_incident_regression_test \
  apps.wb_autoanswers_reconciliation_recovery_test
python3 apps/sqlite_contention_smoke.py
python3 -m compileall -q apps packages
```

The regression suite proves the exact five preserved publications, immutable
job/publication/readback/cost identity, terminal 1-star barrier exclusion,
action-first 2-star selection, no duplicate provider charge or WB POST,
restart/replay idempotency, sanitized bounded SQLite contention and bounded
progress over 40,001 members with indexed acknowledgement lookup.

Production acceptance requires exact deployed Release Train SHA, query-only
before evidence, a matching dry-run fingerprint and backup, bounded apply,
query-only readback, unchanged run/caps and immutable evidence, then real queue
movement. At least one new normal-policy AI completion must reach exactly one
WB POST and matching detail GET readback. The authenticated isolated
Production UI Flow must show the current automatic mode and truthful
progress/priority state with no `5xx`, page error, fatal surface or material
console error before exact LOOP acceptance.

Emergency rollback uses `WB_AUTOANSWERS_FORCE_OFF=true`. Schema v8 is additive
and may remain inert. Restore the verified pre-v8 database only for
demonstrated corruption and only after GET reconciliation of every ambiguous
WB write.
