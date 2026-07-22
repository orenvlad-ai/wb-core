# Ads historical source completion

## Incident

LOOP root #731 still had 61 `ads_date_missing` blockers after the Partner
single-count formula recovery. The first canonical production dry-run returned
no write set because `adv/v3/fullstats` omitted requested campaign IDs. The
former recovery treated that batch omission as a terminally incomplete source,
even when the omitted campaigns were already status 7 and completed before the
requested dates.

## Source contract

The recovery remains official-source-only and fail-closed:

- `adv/v1/promotion/count` is the complete campaign manifest;
- `adv/v3/fullstats` is queried only for its documented statuses 7, 9 and 11,
  with at most 50 IDs, at most 31 inclusive days and at most 3 requests/minute;
- a status-7 campaign with official `changeTime` before the exact scope start
  is retained in evidence and excluded as completed before scope;
- an unsupported-status campaign with missing `changeTime` or a change date
  overlapping scope is a blocker;
- a campaign omitted by a batch response is retried alone for the same exact
  window;
- only a complete singleton payload or WB's exact structured HTTP-400
  `there are no statistics for this advertising period` response confirms the
  campaign/window;
- an empty/malformed singleton, a transport failure or any different HTTP
  error remains a blocker.

The plan/schema contract is `ads_historical_recovery_v2`. The source manifest
digests all campaign decisions and every batch/singleton
response outcome. No raw authorization data or upstream request IDs are
persisted in evidence.

## Mutation boundary

Dry-run remains read-only. Apply still requires the exact fresh fingerprint
and approval reference, creates and verifies a mode-0600 coherent SQLite
backup, locks all warehouse writers, refuses target/non-target drift, inserts
only absent accepted slots in one transaction and performs transactional plus
post-commit readback. Existing snapshots and Partner settings are never
overwritten. A zero is persisted only as a globally confirmed `kind=empty`;
the recovery never creates a synthetic per-SKU zero.

## Verification

`python3 apps/ads_historical_recovery_smoke.py` covers completed-before-scope
exclusion, successful singleton recovery of a batch omission, exact official
no-statistics confirmation, rejection of an incomplete singleton, overlapping
unsupported statuses, source limits, fingerprint drift, backup verification,
rollback, idempotency and non-target invariants.
