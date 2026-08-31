# Migration 176 — WBC0027 FBS lifecycle identity and quality recovery

## Problem

The applied Stage 7C cutover manifest contained an immutable roster snapshot.
Post-cutover lifecycle mapping still treated that snapshot as the live mapping
authority, so later canonical identity mappings could not admit otherwise exact
orders. The unresolved identities stayed quarantined while current and
historical Web Vitrina presentation continued to describe the partial FBS
ledger as exact.

The accepted read-only incident evidence binds source status sequence
`28050157` and exactly four canonical groups:

- `fff_d67e8c823d5f81dd988d00dbfea6 / 210183919`;
- `fff_d67e8c823d5f81dd988d00dbfea6 / 428855560`;
- `fff_d67e8c823d5f81dd988d00dbfea6 / 428855758`;
- `fff_2579bb2741ed4ab23b11bb4c4183 / 428855758`.

The recovery date boundary is `2026-08-17..2026-08-31`. Prose incident counts
and screenshots are not apply inputs: the planner derives exact target rows,
effects and digests from the coherent operational source.

## Runtime semantics

Lifecycle admission now combines immutable matched order evidence with current
active canonical mappings. A later valid mapping is admitted; missing, stale or
ambiguous identity remains quarantined with no effect. There is no fallback to
the cutover SKU roster. Resolution and lifecycle effects commit atomically.

The query-only lifecycle quality contract includes unresolved pending rows and
statuses above the durable cursor. Relevant FBS facility/SKU, facility TOTAL,
stock TOTAL, capital, WAC and economics become missing/partial until the gap is
resolved. Stored history is not rewritten; the reader overlays the blocker at
the exact source order date.

## Guarded recovery

`apps/wbc0027_fbs_lifecycle_quality_recovery.py` is dry-run by default and binds:

- exact deployed SHA, StoreRegistry generation/schema, active cutover and
  forward generation;
- source sequence, four canonical groups and 15 business dates;
- exact immutable status/order/identity rows and current canonical mapping;
- derived lifecycle/balance/capital effects and unchanged non-target digest;
- same-date history base captures and exact event-date corrections.

Apply requires the reviewed fingerprint and durable Production Apply passport.
It uses the shared warehouse writer lock, persists a private mode-0600 before
image, revalidates every CAS under `BEGIN IMMEDIATE`, submits exactly once and
records append-only recovery run/target/history receipts. The same transaction
resolves identity, applies lifecycle effects and appends full same-date capture
supersessions. Reservations use source creation date; terminal effects use the
source status date. There is no current-value retrocopy, immutable overwrite,
WB write, blind retry, service stop or timer change.

## Release boundary

This PR is `live_runtime`: Release Runner deploys only inert capability and
quality guards. Production mutation remains zero in release. A separate
owner-authorized `fbs-lifecycle-quality-recovery` Production Apply run must
obtain two equal query-only witnesses, issue one submit and finish with
query-only readback. Ambiguous transport consumes the submit and may only be
reconciled through readback.

## Prevention coverage

- roster/facility expansion and identity mapping added after cutover;
- missing and ambiguous current mapping quarantine;
- unresolved quality propagation through FBS, TOTAL, capital, WAC and economics;
- exact source-date append-only history supersession without current retrocopy;
- exact four-group/date/source scope, derived cardinalities and non-target CAS;
- one-submit idempotency and query-only repeat/readback behavior.
