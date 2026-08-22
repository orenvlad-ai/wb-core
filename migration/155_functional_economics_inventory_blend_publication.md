# Migration 155: ordinary functional-economics WB+FF publication

## Corrective scope

Migration 153 established `our_inventory_wac_wb_ff_v1`, but the ordinary
functional-economics publisher still loaded only the WB compatibility lookup.
It therefore could overwrite the current ready-date `Себестоимость наша` and
Proxy 3 with WB-only WAC while leaving Proxy 4 on an older image. This
correction changes only that derived ready-snapshot publication path.

For every exact ready date the planner now builds the exact-date product
capital lookup first and then calls the shared
`build_inventory_cost_blend_lookup` with the WB compatibility rows and the
same-date functional WB+FF physical inventory evidence. The resulting lookup
is the single operand for per-SKU `Себестоимость наша`, Proxy 3 and Proxy 4.
The publisher fingerprints the WB compatibility input, exact product-capital
image, functional version/freshness/coverage evidence and both effective
parameter versions. Proxy rows and the visible cost therefore cannot publish
from different source images.

TOTAL cost is derived only through `aggregate_inventory_cost_evidence`:

```text
SUM(exact included WB + FF capital) / SUM(exact included physical quantity)
```

Proxy totals sum eligible SKU calculations. Their percentage and unit
denominators are the corresponding eligible expected revenue and expected
buyout quantity. A missing cost for positive orders makes the dependent total
blank; a zero-order SKU may remain blank without suppressing eligible totals.
No global WAC shortcut or arithmetic mean is used.

## Preserved boundaries

The `2026-08-22` forward-only boundary and exact-date functional version
selection remain unchanged. Pre-boundary ready compatibility values and later
closed ready dates are not reinterpreted from current inventory. The correction
does not change Finance/Partner realized COGS, frozen FBS handoff WAC, the six
mutually exclusive capital stages, overhead documents/queues, quantities,
settings or any production data.

The existing optimistic ready-snapshot CAS, target-scoped before-images,
rollback and repeat no-op contracts remain authoritative. Ordinary publication
also writes one source/version evidence marker and the same WB/FF/facility
disclosure used by the live-plan cost cell.

## Acceptance

`apps/inventory_cost_blend_smoke.py` exercises the ordinary publisher with a
synthetic WB-only before-image and a mixed WB plus multiple-location FF exact
after-image. It proves exact SKU and TOTAL ratios, the same blended SKU operand
for Proxy 3 and Proxy 4, eligible TOTAL denominators, source/version/freshness
evidence, pre-boundary compatibility and an exact repeat no-op. Existing
warehouse, Finance, Partner, capital-stage and recovery smokes remain unchanged
and provide non-target coverage.
