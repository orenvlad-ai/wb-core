# Migration 127 — July 2026 warehouse and product-capital recovery

Status: implementation and human-gated production correction.

## Boundary

Migration 126 intentionally shipped the business-time projection without a
historical correction. Migration 127 adds the single repo-owned recovery
runner and the Seller Portal transit-fact durability needed to recover only
the July evidence closure. It does not change supplier, CNY, financial,
FF-ledger, WB-supply or raw-WB business facts.

`apps/warehouse_historical_recovery.py` is dry-run by default and exposes three
independent submanifests:

- Batch A, `2026-07-19..2026-07-29`: exact six-stage functional versions,
  owned warehouse/product-capital projection and exact ready-snapshot cells;
- Batch B, `2026-07-01..2026-07-18`: only the 610 persisted exact WB
  quantity/WAC/capital rows, their applicable cost/Proxy consumers and owned
  ready cells. The other five stages remain unavailable; 10,692 stage cells
  are never synthesized or zero-filled;
- transit evidence: query-only comparison of the eight reviewed supply facts,
  28 SKU and 126 current cost-layer rows against an exact canonical backup.
  A revision/amount drift keeps backup restoration unavailable. Canonical
  recovery then requires fresh Seller Portal success unless another exact
  human-gated contract is reviewed.

Each apply requires the current fingerprint and human approval reference.
Batch B additionally requires retained/reconciled Batch A identity. T1 stores
exact target before-images and rollback metadata; repeat after successful
reconciliation is T0. Dates from `2026-07-30` onward, active functional
pointer, non-target SKU and non-owned Vitrina cells are invariants.

The fresh production trace classifies the visible `23–29.07` break at the
publication seam: exact-date functional versions exist, while the owned
business-projection/current-ready representation is absent or stale. Dates
`30–31.07` are visible because ordinary post-rollout revisions published those
business dates. Migration 126 explicitly did not backfill older history. The
separate `19–25.07` source/event corrections are folded into the same target
manifest. This evidence does not attribute the incident to a database
refactor.

## Exact evidence rules

Batch A reconstructs each date from the earliest exact non-WB opening, dated
supplier cost components, immutable FF debits, WB supply compositions,
official exact-date WB replacement rows and exact discrepancy receipts. It
does not reverse a later depleted aggregate or copy an adjacent/current day.
Every positive quantity requires positive cost; cost-only components preserve
quantity; physical transitions follow one-unit-one-stage conservation.

Batch B accepts only the persisted daily WB rows. The 594 configured SKU/date
pairs plus 16 exact iPhone rows are published. The `2026-07-17..18` quantity
rows require provenance tying `stock_total` to the same business-date ready
column. The archival 100 RUB overlay remains a cost projection and creates no
quantity, capital movement or stage history.

The production dry-run manifest carries source identities/revisions/dates,
before/after totals, per-date and per-SKU fingerprints, changed/skipped/
unavailable counts, target and non-target digests, rollback identity and the
T0 criterion. Any scope, source, aggregate or digest drift fails before
mutation and requires a newly reviewed manifest.

## Transit last-success and replay

`sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts` is append-only
attempt audit. The existing enrichment row is canonical last-success plus
last-attempt freshness/error and recalculation state. A failed, logged-out,
timeout, not-found or session-expired attempt never overwrites a successful
amount/currency/evidence. Unknown is NULL; confirmed zero is a separate
successful fact.

Every successful supply is committed independently. An identical fact keeps
the same source/success revision; a changed fact increments exactly one
revision while earlier successes remain in attempt history. Missing, stale,
`awaiting_recalculation` and `recalculation_error` supplies are retryable.

A positive late transit fact materializes only the originating supply/SKU cost
layers using full packed composition as denominator and accepted quantity for
accepted capitalization. It enqueues stable
`wb_transit_cost:<supply_id>` replay from the originating business date. Only
dependent WAC/capital/COGS/Finance/Proxy/Vitrina projections may change. No FF
debit, reservation, quantity event, compensation or global rebuild is
created. If fact commit succeeds and materialization/enqueue fails, durable
`recalculation_error` preserves the fact for a safe retry; unchanged replay is
T0. The hourly/manual pipeline finalizes `complete` only after the exact
functional, economics and Finance phases succeed, so a crash between fact
commit, queue consumption and downstream publication cannot report a false
terminal state.

## Production sequence

1. Deploy through the canonical Release Train.
2. Generate external mode-0600 dry-run evidence for Batch A, Batch B and the
   transit submanifest; bind the human gate to exact deployed SHA and
   fingerprints.
3. Apply Batch A, reconcile API/read path and repeat for T0.
4. Apply Batch B only after Batch A is retained, reconcile and repeat for T0.
5. Keep transit unavailable if exact source revision is not approved; after a
   real Seller Portal login, one bounded enrichment processes only missing or
   stale supplies and drives targeted replay.
6. Verify `2026-07-18..31` SKU and TOTAL product-capital cells through the
   public API and isolated production UI, including revision-driven reread and
   unchanged filters/presets/disclosure/scroll.

## Verification

- `python3 apps/wb_supplies_transit_cost_enrichment_smoke.py`;
- `python3 apps/wb_transit_cost_replay_smoke.py`;
- `python3 apps/warehouse_recovery_policy_smoke.py`;
- `python3 apps/warehouse_recovery_policy_static_smoke.py`;
- Batch A/B fixture apply, readback, rollback, stale-fingerprint and T0 tests;
- hosted dry-run/apply/rollback plus production API/UI reconciliation.
