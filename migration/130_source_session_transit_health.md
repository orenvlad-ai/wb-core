# Migration 130 — source/session health and autonomous transit costs

Status: repository implementation complete; production activation and evidence
are completed only after the LOOP deploy and exact readback described below.

## Incident boundary

The pre-change production read showed 485 cached WB supplies, 95 currently
eligible transit supplies and only 40 confirmed transit-cost successes. The
remaining 55 rows were not a Seller-login boolean: they included failed and
session-expired attempts, while the hourly warehouse pipeline did not own a
global collector. Opening a particular page or pressing its bounded button was
therefore capable of showing a valid generic Seller session without repairing
transit coverage.

## Repository contract

- `sheet_vitrina_v1_source_health_status` stores only sanitized cached source
  health JSON by stable source key and check time. Normal runtime schema ensure
  creates it idempotently; no destructive SQL, table copy or data rewrite is
  required.
- Seller Portal and WB Buyer retain their separate storage and adapters. Their
  login/recovery controls move to `Настройки → Источники и сессии`; public WB
  Card/SPP Proxy is explicitly anonymous.
- The first settings read is cached. A 180-second stale TTL triggers exact
  source checks with browser single-flight; only an active recovery uses short
  polling.
- Transit status separates Seller auth, exact `supply/cost` route, collector,
  freshness and coverage. A generic valid session never makes the route or
  collector green.
- Every successful ordinary official-supply sync and every hourly/manual
  warehouse sync runs the bounded global due collector. It selects all eligible
  supplies rather than the visible list, joins duplicate active work, reconciles
  stale runs, persists classified attempts, applies bounded retry/backoff and
  preserves the last successful amount when a later attempt fails.
- The reviewed optimistic `warehouse-functional sync-apply` contour does not
  run this supplemental mutation before its exact-plan recheck.

## Production activation and replay

Deployment uses the ordinary Release Train; `_ensure_schema` creates the source
health table during normal runtime startup. After deploy, the repo-owned
`warehouse-functional manual-sync` command is the authorized bounded replay for
current eligible gaps. No ad-hoc SQL or browser-page scope is an apply path.

Before and after replay, query-only SQLite readback must use `mode=ro` plus
`PRAGMA query_only=ON` and record:

- total and eligible supply identities;
- canonical confirmed transit amounts;
- immediately due, backed-off and terminal/classified failures;
- latest run status/counters and append-only attempt taxonomy;
- source-health rows for Seller transit, Buyer capability and anonymous public
  card availability when those exact checks have run.

Acceptance requires confirmed coverage to increase or reach completion. Any
residual rows must retain an explicit classified reason and next-attempt state;
auth-valid alone is not acceptance. Canonical amounts, official WB evidence and
unrelated supplies are non-targets.

Repository verification includes the transit/runtime/HTTP smokes plus the
central Settings contract and Playwright browser smoke. The browser proof opens
the hash-targeted group, verifies three separate source cards and anonymous
public-source semantics, asserts exact Seller/Buyer/public route checks and
same-source single-flight, and checks 760 px / 560 px layouts without overflow.

## Rollback

Code rollback stops future autonomous collection but does not delete confirmed
transit amounts, append-only attempt evidence or cached source health. The new
table is additive and may remain unused. A later replay is idempotent over fresh
successes and respects durable backoff; no rollback SQL is required.
