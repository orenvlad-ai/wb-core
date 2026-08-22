# Migration 153: forward-only Vitrina WB+FF inventory cost blend

## Scope and boundary

From business date `2026-08-22`, the visible Vitrina metric
`Себестоимость наша` and indicative Proxy 3/4 share
`our_inventory_wac_wb_ff_v1`. This is a repo/live read-model change, not a
production data backfill. Earlier ready dates keep their persisted WB
compatibility values and later overhead/current stock cannot rewrite them
through ordinary refresh.

Finance weekly/per-SKU and Partner Report are non-target consumers. They retain
`canonical_our_cost_channel_location_v1`: FBS exact facility frozen WAC at
durable handoff, WB/FBO exact daily WB WAC, uncovered revenue excluded with
partial coverage, and fulfilled sales immutable.

## Exact informational formula

The only included physical stages are mutually exclusive `WB` and `FF`.

```text
SKU capital  = SUM(exact included WB/FF location capital)
SKU quantity = SUM(exact included WB/FF physical quantity)
SKU WAC      = SKU capital / SKU quantity

TOTAL capital  = SUM(all included SKU/location capital)
TOTAL quantity = SUM(all included SKU/location quantity)
TOTAL WAC      = TOTAL capital / TOTAL quantity
```

The calculation never averages SKU, facility or warehouse WACs. Exact Decimal
stage evidence remains authoritative; legacy public float stage fields are
only compatibility projections and may compare within one micro-ruble. FF
facility/pool rows embedded in immutable balance provenance must reconcile to
the FF aggregate and are disclosure only. WB/FF split, every positive FF
facility/pool, functional version, effective/published timestamps, source
watermarks and cost coverage are retained in server cell evidence.

Positive quantity with missing version, cost coverage, FF facility mapping or
pool evidence produces no WAC and explicit reason codes. Another SKU/facility,
FBO/WB realized cost fallback, arithmetic mean, legacy value and zero are
forbidden. Reservations use physical inventory before reservation and create
zero capital. FF→WB movement preserves aggregate quantity/capital while placing
each unit in exactly one included stage.

## Proxy and capital contracts

On and after `2026-08-22`, Proxy 3 and Proxy 4 use full date-specific
informational order/count/ads operands and the per-SKU blended WAC. They do not
restrict new rows to Finance-covered sales and do not derive their WAC from
covered COGS. Dates before the boundary keep the previous ready compatibility
semantics, including its covered-sales proxy operands; deployment cannot
reinterpret them. SKU missing new informational cost with positive order
revenue makes its Proxy row and aggregate blank. TOTAL proxy profit sums
complete SKU calculations; TOTAL proxy margin divides summed profit by summed
expected revenue. It never multiplies a global WAC shortcut.

The six-stage `Общий товарный капитал` contract is unchanged: production,
China→FF, FF, FF→WB, WB and acceptance discrepancy remain mutually exclusive.
The blend consumes only FF and WB, so facility/pool disclosures cannot add
capital twice and transfer/discrepancy stages cannot leak into inventory WAC.

## Finance contention correction

Heavy Finance week planning and after-image construction remain outside
`BEGIN IMMEDIATE`. The prior global `PRAGMA data_version` CAS was too broad: an
unrelated interactive document/status commit invalidated a correct multi-minute
projection and produced repeated hourly failures. The writer boundary now
recomputes a bounded exact dependency fingerprint over canonical WB/FBS cost
identities, nomenclature routing, target raw/report rows and the target-before
image. Actual dependency drift fails closed before replacement; unrelated
SQLite writes are admitted. Target readback and non-target digest remain
transactional. Phase timings prove the writer section excludes heavy planning.

The warehouse UI error classifier recognizes exact Finance dependency/target
CAS drift before generic SQLite timeout metadata, so it no longer reports a
source-drift failure as `временная ошибка хранилища`. Hourly capture cadence,
last-good state and normal timer recovery are unchanged; no blind retry or
service restart is introduced.

## Acceptance

Focused smokes cover WB-only, FF-only, multiple FF facilities/pools, mixed SKU,
exact SKU/TOTAL capital-over-quantity, Decimal/float projection tolerance,
missing mapping/cost, zero quantity, reserve, transfers, non-target stages,
history freeze and Proxy 3/4 per-SKU/TOTAL formulas. Finance contention smokes
pause lock-free projection, prove an interactive writer commits promptly,
admit that unrelated commit, reject an exact canonical-cost change, retain
non-target state and verify idempotent repeat.
