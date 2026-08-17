# Migration 147 — legacy incident presentation

Date: 2026-08-17
Contour: Web Vitrina live presentation only

## Decision

The WB warehouse incident policy remains an append-only production record and
leaves only the ordinary planning presentation. This migration does not append,
disable, update or delete policy revisions. It does not rewrite ready snapshots,
incident quantity evidence, projection cache, audit records, raw WB/FBS
quantities or six-stage accounting.

The canonical archived-metric boundary excludes every
`wb_stock_fact_qty*`, `wb_stock_incident_qty*` and
`wb_stock_effective_qty*` SKU/TOTAL/region key, plus the incident-aware planning
aliases `inventory_wb_effective_qty_v1` and `wb_stock_effective_qty`, from public
read rows, metric catalog, picker, settings and saved presentation profiles.
Their evaluator definitions, accepted evidence and persisted historical cells
remain available to internal code and audit reads.

The ordinary main Vitrina exposes only the applicable base planning families:

- `Остаток WB: всего` = exact current official WB aggregate;
- `Остаток FBS: всего` = signed sum of active FBS-facility available balances;
- dynamic `Остаток FBS: <склад>` = that active facility's signed available
  balance;
- raw `Остаток: всего` = WB total + FBS total exactly once.

The existing `inventory_planning_v1` read model continues to calculate its
internal incident/effective operands for audit and existing non-presentation
consumers. No factory-order, supply/replenishment, recommendation, regional
allocation, SKU planning or FBS lifecycle import/formula changes in this
migration.

## Legacy disclosure

`Остатки → Склад WB` keeps the former policy card inside a native
default-collapsed disclosure named `Legacy: инциденты на складах WB`. Opening
the ordinary warehouse tab does not load policy/options. The two existing GETs
run only after the user explicitly opens the legacy disclosure.

The legacy response includes a bounded newest-first append-only revision list
and the complete latest `warehouse_entries`, including closed/historical
entries. The disclosure unions those identities with current warehouse options,
so exact warehouse IDs, names, interval dates, revisions, actor/status/source
remain readable even when the current official WB snapshot contains only the
aggregate sentinel. Legacy-only identities are read-only and cannot become a
new operational destination without the existing exact current-snapshot gate.

Configured and effective state are separate and truthful. The server derives
both from the retained current registry row and exact business date; the UI does
not synthesize an opposite state. Ordinary table headers, SKU Management
summary, Supply selector, calculation registry column and regional diagnostics
no longer render incident badges, warnings, quantities or N/A explanations.
Their server payload/audit evidence and calculation request/readback behavior
remain unchanged.

## Production boundary

This is `scope:live-runtime`, not `scope:production-mutation`. There is no
business-data runner, apply command, owner apply gate, policy revision append or
incident rematerialization in this pass. Deployment changes only code/templates
and ordinary UI behavior. Canonical production acceptance is query-only: exact
deployed SHA, service/probe health, a read-only policy/history invariant and an
authenticated isolated GET-only browser flow.

## Verification

- `apps/sheet_vitrina_v1_metric_retirement_smoke.py` proves every incident and
  incident-aware planning key is hidden while evaluator definitions remain;
- `apps/sheet_vitrina_v1_inventory_planning_smoke.py` proves only the four base
  row families remain, exact WB/FBS/raw-total formulas and no-double-count hold,
  incident evidence still resolves internally, ready history is unchanged and
  calculation consumers do not import the new presentation keys;
- `apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py` proves no incident
  row/N/A/picker/persisted preference leakage and mobile/dark/console health;
- `apps/wb_incident_policy_legacy_readback_smoke.py` proves exact historical
  names/dates/revisions and configured/effective consistency without a policy
  disable;
- `apps/wb_incident_policy_legacy_ui_browser_smoke.py` proves default collapse,
  explicit lazy load, keyboard disclosure, aggregate-only identity retention,
  exact history and mobile/dark/console health;
- `apps/wb_warehouse_exclusion_browser_smoke.py` proves Supply retains hidden
  backend calculation state without ordinary incident UI or browser writes;
- `apps/warehouse_stocks_production_ui_flow.py` proves production-style
  acceptance opens the legacy disclosure consciously before reading its audit.

## Explicit exclusions

No policy revision or incident loss quantity is created, disabled, restored or
inferred. No historical incident snapshot is rematerialized. Factory order,
supply/replenishment, recommendation, SKU planning, regional allocation, raw
WB/FBS, FF/FBS ledger, mappings/facilities, seller readback,
26GN527/acceptance, collectors/lifecycle, WB writes and unrelated
promo/historical recovery remain outside scope.
