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
The recovery-policy registry classifies `ff_inventory_reconciliation` as an
enabled `sku_date` T1 mutation before capacity reservation; an unregistered or
differently scoped operation fails before the inventory transaction starts.

# 5. Derived publication and observability

After apply, one bounded current-source functional sync must publish the new FF,
FF→WB, WB, discrepancy, six-stage product-capital, WB daily WAC and dependent
Vitrina/Finance projections. The missing supply is excluded from transit after
return; active FF→WB is rebuilt only from debited, non-returned, unaccepted
units. Conservation must hold per supply/SKU and in aggregate.

The post-apply production diagnosis found that the physical/functional readback
was correct while Web Vitrina had revision `whbpr_events_*`: capital from the
legacy event daily state was combined with absent/preserved quantity cells and
the header still reported freshness `2026-07-24`. The source was not another
inventory movement. A physical-only FF outbox publication had no complete event
proof, yet superseded the exact functional rows after the functional sync.

The corrected contract has two guards:

- each successful hourly/emergency/targeted functional apply atomically
  publishes the complete six-stage exact-date projection with its new version;
- a `functional_*` or `ff_stock_*` outbox request without canonical event proof
  is consumed only as an awaiting-replay signal and leaves last-good projection
  rows/state unchanged.

The one-off `warehouse-july-recovery --batch projection` runner repairs only the
derived `2026-07-30..active business date` rows. For 31 July and later it applies
an already-committed inventory line only when the selected exact version's FF
watermark proves that operation absent; a current version whose append-only
prefix already contains the operation is not adjusted twice. Every line uses
its frozen `cost_snapshot.capital_delta_rub`; no WAC is re-estimated. Dry-run
pins the source/reconciliation, per-date versions/watermarks, current target
digest, active physical/function non-target digest and candidate fingerprints.
Apply is projection-only `sku_date` T1, exact repeat is T0 and rollback restores
only projection revision/current/state before-images. It never reruns the
inventory apply or changes a physical ledger/function version.

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

The locked pre-apply evidence refreshed after recovery PR `#904` deployed as
`27f6f429b7adb21872653270e53d73653abe4167` is:

- successful current-source sync version
  `whfv_36a244decde8ba5364e8815e`, with complete official stocks, zero negative
  balances/cost gaps and retained T2 operation
  `recovery_5d3e3d1e20c39c8fb783c475f0abfdcd`;
- deployed recovery-policy readback classifies
  `ff_inventory_reconciliation` as enabled `T1` with closure `sku_date`, while
  canary fingerprint
  `sha256:daa1e41e7943a66a4a7be2d98f5d8fbf387c81a3cf5fbacd1b67f58977b374eb`
  covers T0/T1/T2, non-target digest and orphan scanning without business-data
  mutation;
- fresh query-only inventory fingerprint
  `sha256:31ecb7cf69fba0adc0d5cefbe288d9663c8a0ce19af61b49fe1a0e2357e5afba`:
  `48 250 -> 53 750`, return `5 250`, net inventory `+250`, 33 target SKUs,
  three documents, zero blockers/missing costs/negative targets/unmatched or
  ambiguous rows, and repeat apply specified as T0;
- relevant ledger digest
  `sha256:995cf60843a0fe2662f6b432e22dc17935879bce6c883b44c0eb9c444b9ebcaf`
  and non-target digest
  `sha256:044fcfdad4c53e8422686d7a0f4377a29a73be81f65bcd5541e53790842ea33b`
  are unchanged from the prior reviewed plan. The fingerprint changed only
  because the mandatory fresh sync advanced the active functional source and
  complete-snapshot return proof; the quantity, cost, capital, document and
  target/non-target invariants stayed exact.

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

## 7.1 Exact projection-only recovery manifest (2026-08-03)

The runtime correction shipped by PR `#906` as merge/deployed SHA
`146865c8eb68c4ec05b0b3835cff8afb63b5ed71`. A fresh production query-only
dry-run after that deploy produced the following locked projection manifest:

- contract `warehouse_business_projection_exact_functional_recovery_v1`;
- source workbook
  `sha256:2c63ef251398c3f48b76ab72d859a70a987a04e0ea502744d18e731ef689e636`,
  business date `2026-07-31`, manager target `33 SKU / 53 750 units`;
- active exact functional version `whfv_3e703f436691ae5c155a0470`, business
  date `2026-08-03`, plan fingerprint
  `sha256:3e703f436691ae5c155a0470c75bef23902e30ced9756c96730e8333259d6846`;
- projection recovery fingerprint
  `sha256:8fcabb108cc792d6bb02e248c53f85fc43222a11f3e7a48df9b7f134f8470b8e`,
  revision `whbpr_exact_recovery_8fcabb108cc792d6bb02`;
- source digest
  `sha256:f08c2955788bbf7b44a6e96f31d146eecb6edff85c936a7da5ce22464dc70d99`,
  active physical/functional non-target digest
  `sha256:9944de7a83c9ff959cd177326601d8886b310f0a2d47ebefc854dbb703030dc8`
  and current projection target digest
  `sha256:f7cc639aac4cadadb79fc9bd854d68b951050ae531689f00190bf59a85cae217`;
- target dates `2026-07-30..2026-08-03`, `360` candidate rows, `444`
  changed revision/current keys, `292` existing current rows, three already
  committed inventory operations and `18` frozen-cost audit lines;
- July 31 and August 1 use the three inventory operations only because their
  exact version FF watermarks stop at row `359`; July 30 receives none and the
  August 2 and current August 3 watermarks already include all three at row
  `362`, so neither date is adjusted twice;
- July 31 and August 1 FF readback is exactly `53 750` units and
  `6 048 120.11650603214091306923 RUB`; current August 3 is the same, while
  FF→WB is `20` units and `2 160.370426449805267145989195 RUB`;
- current six-stage product capital is
  `42 256 023.96196596358327688035 RUB`: production
  `1 566 550.109999999999999999998`, China→FF
  `6 361 497.429999999999999999999`, FF
  `6 048 120.11650603214091306923`, FF→WB
  `2 160.370426449805267145989195`, WB
  `28 273 451.02541949418966756378` and discrepancy
  `4 244.909613987447429101353303`.

The earlier readback total `42 295 411.19003982564979548198 RUB` was correct for
functional version `whfv_47382844b1b74662b8dc34ca` at `17:18 UTC`. The next
complete official WB snapshot in active version `whfv_caf7bbfb0bf72204899d8bf3`
reduced WB by `287` units and `31 822.01199834836894003457 RUB`; all other current-stage
quantities/capital stayed identical. After the business-date rollover, version
`whfv_3e703f436691ae5c155a0470` reduced WB by another `70` units and
`7 565.21607551369757856706 RUB`, again without changing any other stage. This
is source-version advancement, not a projection loss, and the recovery must
publish the newest canonical value.

The deployed recovery-policy canary is query-only, covers T0/T1/T2,
non-target digest and orphan scanning, and is bound to deployed SHA
`146865c8eb68c4ec05b0b3835cff8afb63b5ed71` by fingerprint
`sha256:2649c59f3b063509afe4ab0e740ab7bc48b8aad0068e91608f01bf0cbabbb81c`.
Apply is allowed only with the exact manifest above and an exact human-gate
reference. It registers `targeted_warehouse_publication` as T1/`sku_date`,
captures projection-only before-images and rechecks source, target and
non-target digests under the shared warehouse lock before `BEGIN IMMEDIATE`.
Any hourly version/digest drift fails closed and requires a new dry-run; no
inventory apply, physical ledger write or functional active-pointer mutation is
part of this operation.

After a successful apply, the hosted runner rebuilds a query-only current plan
before handling a repeated command. That plan is expected to be `would_change =
false` and therefore has a different no-op fingerprint. Exact repeat resolves
the requested fingerprint through the retained recovery registry first, then
requires the same source/date/scope/approval/non-target digest and verifies the
active current rows against the durable revision rows and retained after
digest. Only that proof returns T0; a changed target/source remains fail-closed.

# 8. Verification

- `python3 -m apps.ff_inventory_reconciliation_smoke`
- `python3 -m apps.ff_stock_reservation_smoke`
- `python3 -m apps.wb_supplies_incremental_sync_smoke`
- `python3 -m apps.wb_supplies_transit_cost_enrichment_smoke`
- `python3 -m apps.wb_incident_policy_smoke`
- `python3 -m apps.warehouse_update_journal_smoke`
- `python3 -m apps.warehouse_functional_smoke`
- `python3 -m apps.warehouse_business_projection_smoke`
- `python3 -m apps.warehouse_business_projection_recovery_smoke`
- `python3 -m apps.sheet_vitrina_v1_web_vitrina_contract_smoke`
- `python3 -m apps.registry_upload_http_entrypoint_hosted_runtime_smoke`
- production warehouse UI Flow plus exact post-apply reconciliation evidence.
