# Migration 164: supported daily Finance transport and bounded recovery

## Problem

Daily Web Vitrina still acquired seller-report rows through the deprecated GET
`reportDetailByPeriod` path. The endpoint's one-request-per-minute quota was not
coordinated with the weekly Finance timer or concurrent Vitrina entrypoints.
`429` could therefore look like an empty/failed daily source, partial pagination
had no common completion contract, and exhausted closed-day retries could become
terminally forgotten. Finance values for 26–27 August 2026 require a narrow,
auditable recovery without rebuilding another business contour.

## Transport decision

`packages/adapters/wb_finance_api.py` owns the shared official
`POST /api/finance/v1/sales-reports/detailed` client. It uses exact date bounds,
`period=daily|weekly`, `limit<=100000`, `rrdId` pagination and terminal `HTTP
204`. One account/endpoint interprocess lease under the canonical runtime holds
the whole pagination session and enforces at least 60 seconds between requests;
later `Retry-After` and `X-RateLimit-*` hints are durable. Weekly timer and every
daily Vitrina entrypoint use this same gate.

`429` is typed `rate_limited`, never empty and never immediately retried. The
safe error records exact date/range, period, completed pages, cursor, allowlisted
header hints and next permitted time. Empty `200`, stuck cursor, deadline,
transport or mid-pagination failure returns no rows to publication. Last-good
accepted slots stay intact. Same-day exhausted Finance closure becomes eligible
again only from a later business day/window.

Daily formulas and shape are unchanged: five Finance metrics for each of the 33
active SKU plus five aggregate TOTAL and account-wide `paidStorage` TOTAL. Sale
and return signs for buyout/WB commission are preserved. `acquiringFee` is
additive as delivered, including return rows. Storage TOTAL includes every
exact-date seller-report row, including rows outside the target roster.

## Exact recovery

`apps/finance_daily_historical_recovery.py` exposes only:

- parity for 2026-08-24 and 2026-08-25;
- plan/apply/readback for 2026-08-26 and 2026-08-27, one date sequentially.

The private reviewed plan pins deployed SHA, ready snapshot generation and
identity, complete source digest/pages/cursor and 33/33 coverage, exact 165 SKU
plus 6 TOTAL cells, typed before states, expected values, before/after plan CAS,
non-target digest and Proxy-gap exclusion. It contains normalized aggregates,
not raw seller-report rows or PII. The immutable evidence also retains the exact
pre-change general/accepted temporal snapshots and closure row with a CAS
digest. Expected values use the same six-decimal numeric-cell normalization as
canonical Vitrina materialization, so harmless IEEE-754 accumulation tails do
not become false parity mismatches. Apply performs no upstream refetch.

The exact-manifest Apply Runner materializes the canonical hosted SSH identity
and strict known-hosts options from production environment secrets before the
four manifest commands. Credentials live only in a temporary mode-0600 contour
and the inherited SSH environment is restored after the command sequence.

One `BEGIN IMMEDIATE` updates the exact ready `plan_json`, existing accepted
temporal snapshot/slot and Finance closure state, then inserts
`sheet_vitrina_v1_finance_daily_recovery_audit`. That table is an operation
receipt with compact before/after images, not a new Finance ledger. Same
operation repeat is a zero-write no-op; an ambiguous transport is reconciled by
query-only readback and is never blindly resubmitted.

Ordinary full/group/auto publication remains enabled. Once a recovery audit is
present, the common ready-snapshot writer rejects any concurrently built plan
that would regress an audited exact Finance cell; a producer carrying the same
171 values passes. This is a narrow fail-closed writer guard, not a blanket
timer/service stop, and the shared Finance gate still serializes acquisitions.

Readback opens the selected operational SQLite generation in `mode=ro` with
`query_only=ON` and verifies terminal 204, source digest, 33/33, 171/171, TOTAL,
no duplicates, ready/closure/audit identity and non-target digest. It recomputes
overall Vitrina semantic health from all STATUS rows; another incomplete source
keeps the day warning/error. All Proxy, stock, cost/WAC, warehouse, ads, orders,
other dates and specifically `2026-08-26 / SKU 428853741 / proxy_profit_3_rub`
remain out of scope.

## Verification

- `python3 apps/fin_report_daily_finance_transport_smoke.py`
- `python3 apps/finance_daily_historical_recovery_smoke.py`
- `python3 apps/wbc0020_finance_daily_recovery_smoke.py`
- `python3 apps/wb_finance_weekly_smoke.py`
- `python3 apps/sheet_vitrina_v1_closed_day_auto_refresh_smoke.py`
- `python3 apps/sheet_vitrina_v1_temporal_closure_retry_smoke.py`
- `python3 apps/sheet_vitrina_v1_web_vitrina_group_refresh_smoke.py`
- `python3 apps/sheet_vitrina_v1_refresh_read_split_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`

Deployment is inert. It creates schema and exposes runners but performs no
external fetch or business mutation. Historical recovery requires a separately
reviewed exact-date plan after the deployed SHA is confirmed.
