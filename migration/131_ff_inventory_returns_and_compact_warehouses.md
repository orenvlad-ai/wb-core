---
title: "Migration 131: FF inventory, WB-supply returns and compact warehouses"
doc_id: "WB-CORE-MIGRATION-131-FF-INVENTORY-RETURNS-COMPACT-WAREHOUSES"
doc_type: "migration"
status: "ready_for_release_train"
business_date: "2026-07-31"
scope: "production-mutation"
source_of_truth_level: "migration_canonical"
---

# 1. Objective and immutable source

This migration aligns the physical FF ledger to the manager workbook for
business date `2026-07-31`, repairs the proven missing WB-supply movement,
publishes coherent quantity/capital, and deploys the compact warehouse/update
and per-warehouse incident-policy contracts in one Release Train item.

Manager source:

- filename: `Остатки на фф на 31.07.2026.xlsx`;
- SHA-256: `2c63ef251398c3f48b76ab72d859a70a987a04e0ea502744d18e731ef689e636`;
- sheet: `Остатки ФФ`;
- exact headers: `nmId`, `Комментарий SKU`, `Остаток ФФ`, `Дата остатка`;
- 33 unique `nmId` rows, all dated `2026-07-31`;
- target physical FF total: `53 750` units.

The workbook is a physical target, not an editable balance snapshot. Its bytes
are stored once in the reconciliation audit and referenced by the same SHA on
the first inventory document. A different file, row set, date or active source
revision produces a different plan and requires a new review.

# 2. Required movement split

The current production preflight observed physical FF `48 250`. Supply
`41132380` owns a `5 250` unit FF debit dated `2026-07-29`, has no current
official-cache row, no accepted units and has exact historical debit-cost
evidence. It enters the ordinary lifecycle debounce and may be returned only
after a confirmed cancelled state or two distinct complete official active
snapshots. That return restores `5 250` units and their exact original capital;
it is not an inventory surplus.

After the return the remaining inventory deltas are:

| nmId | delta, units | document |
|---:|---:|---|
| 245720334 | +1 500 | inventory receipt |
| 259460529 | -1 250 | inventory writeoff |
| 259473237 | -250 | inventory writeoff |
| 497414624 | +250 | inventory receipt |

Receipt total is `+1 750`, writeoff total is `-1 500`, net inventory delta is
`+250`, and the final total is exactly `53 750`. The plan must recompute these
figures from current production state; the numbers above are evidence, not an
authorization to bypass stale-plan guards.

# 3. Cost and capital policy

Every supply return inherits exact original line WAC/capital. Inventory lines
freeze one pre-adjustment same-SKU FF basis on or before `2026-07-31` using:

1. exact proved original source/debit cost;
2. canonical same-SKU/same-stage FF WAC on `2026-07-31`;
3. last valid same-SKU FF WAC before that date;
4. latest certified inbound/landed FF cost no later than the date;
5. a separate versioned owner-approved estimate with explicit provenance.

The last two explicit source classes live in the append-only
`sheet_vitrina_v1_ff_inventory_cost_bases` contract. An estimate must carry a
positive unit cost plus immutable approval and provenance; unsupported source
types are rejected. The current workbook plan uses factual bases and does not
introduce an estimate.

Zero, another SKU/warehouse average, bare China price and future WB WAC are
forbidden. Missing basis is a blocker. Plan/apply/rollback retain Decimal
quantity, WAC and signed capital strings in line-level `cost_snapshot` audit.
Moving WAC and all six product-capital stages are replayed only after physical
documents commit; replay cannot create a second movement.

# 4. Canonical runner and recovery

`apps/ff_inventory_reconciliation.py` and the hosted commands
`ff-inventory-reconciliation-{dry-run,apply,readback,rollback}` are the sole
production mutation path. Dry-run is default and query-only. Its manifest
contains source SHA/date/row count, active functional version, nomenclature and
cost-snapshot digests, confirmed return proofs, exact per-SKU before/return/
inventory/target/WAC/capital rows, document/operation ids, before/target totals,
target-ledger digest and an exact non-target physical FF-ledger digest. Derived
functional-version churn is intentionally outside that rollback guard, so the
required post-apply publication cannot disable bounded T1 compensation.

Apply requires the exact fresh fingerprint and the Release Train human gate
comment reference. Under `BEGIN IMMEDIATE` it rechecks the full manifest,
appends return/receipt/writeoff documents, stores the source evidence and
requires exact per-SKU plus total readback before commit. A repeated exact
apply is T0. Recovery is T1: the retained recovery record and audit manifest
support only append-only inverse-cost compensation after a separate exact
approval; original documents/source bytes are never updated or deleted.

# 5. Derived publication and observability

After apply, one bounded current-source functional sync must publish the new FF,
FF→WB, WB, discrepancy, six-stage product-capital, WB daily WAC and dependent
Vitrina/Finance projections. The missing supply is excluded from transit after
return; active FF→WB is rebuilt only from debited, non-returned, unaccepted
units. Conservation must hold per supply/SKU and in aggregate.

A cost-only replay preserves every last-good SKU and aggregate physical
quantity, including legacy `nm_id=0` totals, and recomputes WAC from the new
capital over that preserved quantity. A historical aggregate mismatch therefore
cannot create a physical movement or abort an otherwise safe cost publication.
Proportional FF debits that consume the final units transfer the whole remaining
pool capital atomically; a repeating Decimal WAC cannot leave an infinitesimal
capital-only residue or block an otherwise exact zero-quantity close. Frozen
line-cost debits retain their stricter exact-cost invariant and are not rounded
or normalized by this rule.

Published warehouse versions also own:

- compact per-warehouse summary/balance read models and ETags;
- a compact WB warehouse-option read model materialized by each functional
  publication, so incident-panel GET is local-only and page open cannot invoke
  an external producer;
- paginated document headers and lazy line/provenance/balance evidence routes;
- a durable automatic/manual run journal for seven named phases;
- last-attempt/last-success/duration/next-run/version/business-date/freshness,
  with last-good retained and degraded state visible on failure;
- status-1 preorder `awaiting_supply_creation` exclusion from the supply-cost
  endpoint; confirmed zero and missing remain different states.

The initial warehouse response must be hundreds of KB, not the former
`25–138 MB`, and production navigation must be subsecond where network/runtime
conditions permit. Page open stays strictly read-only.

# 6. WB incident policy v2

The policy migrates/projections the former selected list/global date to
per-warehouse entries. Existing active selections retain `2026-07-25`; a new
selection defaults to canonical business today (for this release,
`2026-08-02`) and may be edited before Apply. Stable positive numeric ID is the
identity; service bucket ID `0` is not eligible.

All checkbox/date edits are local draft. One Apply validates every selected
date, appends one atomic policy revision and runs one dependent availability
replay from the earliest changed warehouse date. Existing start dates cannot
be rewritten retroactively. Removal closes an interval; draft uncheck/reselect
retains its date until Apply; later persisted re-selection opens a new interval.
Exact repeat is T0. Physical WB, WAC and capital are invariant.

The full-width card uses an internal vertical disclosure. Collapsed summary
shows enabled state, selected count and earliest date. Expanded desktop grid is
4 columns, then 3/2/1 responsively, with no horizontal viewport overflow.
Options sort by physical WB quantity descending, zero last, then name/ID.

# 7. Release and production gates

Before mutation:

1. deploy the exact merged SHA through Release Train;
2. run a fresh query-only production preflight;
3. capture exact source SHA, manifest/fingerprint, counts/totals/per-SKU deltas,
   cost-basis quality, return proof, target/non-target digests;
4. prove T1 capacity/reversibility and post-apply readback;
5. obtain the exact human production-mutation approval required by the current
   release protocol.

After apply:

- all 33 target SKUs and total equal the workbook;
- no negative balance/capital or missing/synthetic-zero cost exists;
- the exact source and approval audit are readable;
- repeat apply is T0;
- FF→WB/WB/discrepancy quantity and capital conserve;
- product capital and relevant daily/Vitrina/Finance projections are current;
- automatic/manual status survives restart and names any degraded phase;
- `/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=wb` renders collapsed and
  expanded incident states, desktop 4-column grid, per-warehouse dates, one
  Apply, no horizontal overflow, `5xx`, `pageerror` or fatal surface;
- warehouse API/UI payload bytes and latency are recorded before/after.

Terminalization uses the current production-mutation Release Train command and
requires deployed merge SHA, gate comment/digest, reconciliation comment/digest
and evidence hash. The migration is complete only at `release:production`.

# 8. Verification

- `python3 -m apps.ff_inventory_reconciliation_smoke`
- `python3 -m apps.ff_stock_reservation_smoke`
- `python3 -m apps.wb_supplies_incremental_sync_smoke`
- `python3 -m apps.wb_supplies_transit_cost_enrichment_smoke`
- `python3 -m apps.wb_incident_policy_smoke`
- `python3 -m apps.warehouse_update_journal_smoke`
- `python3 -m apps.warehouse_functional_smoke`
- `python3 -m apps.sheet_vitrina_v1_web_vitrina_contract_smoke`
- `python3 -m apps.registry_upload_http_entrypoint_hosted_runtime_smoke`
- production warehouse UI Flow plus exact post-apply reconciliation evidence.
