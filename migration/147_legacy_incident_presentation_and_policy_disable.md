# Migration 147 — legacy incident presentation and policy disable

Date: 2026-08-17
Effective policy date: 2026-08-16
Contour: Web Vitrina live presentation plus one separately gated production-data append

## Decision

The WB warehouse incident policy is retained as immutable history but leaves the
ordinary planning interface. This migration does not delete or rewrite ready
snapshots, policy revisions, incident quantity evidence, projection cache, audit
records, raw WB/FBS quantities or six-stage accounting.

The canonical archived-metric boundary now excludes every
`wb_stock_fact_qty*`, `wb_stock_incident_qty*` and
`wb_stock_effective_qty*` SKU/TOTAL/region key, plus the current-planning
incident-aware aliases `inventory_wb_effective_qty_v1` and
`wb_stock_effective_qty`, from public read rows, metric catalog, picker,
settings and saved presentation profiles. Their evaluator definitions and
persisted historical cells remain available to code/audit reads; no migration
rewrites them.

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
the ordinary warehouse tab does not load policy/options. The two GETs run only
after the user explicitly opens the legacy disclosure.

The legacy response includes a bounded newest-first append-only revision list
and the complete latest `warehouse_entries`, including closed/historical
entries. The disclosure unions those identities with current warehouse options,
so exact warehouse IDs, names, interval dates, revisions, actor/status/source
remain readable even when the current official WB snapshot contains only the
aggregate sentinel. Legacy-only identities are read-only and cannot become a
new operational destination without the existing exact current-snapshot gate.

Configured and effective state are separate and truthful: an inactive latest
revision renders configured/effective disabled, has no effective excluded IDs,
and retains its historical entries only through the legacy/audit fields.
Ordinary table headers, SKU Management summary, Supply selector, calculation
registry column and regional diagnostics no longer render incident badges,
warnings, quantities or N/A explanations. Their server payload/audit evidence
is retained and calculation request/readback behavior is unchanged.

## Append-only production disable

`apps/wb_incident_policy_legacy_disable.py` is the sole repository-owned runner
for this migration. Its default invocation is query-only dry-run. It resolves
the canonical operational generation through `StoreRegistry`, requires
`PRAGMA query_only=ON`, verifies both deployed markers for one exact SHA, reads
the latest seller revision and produces a machine-readable
`wb_incident_policy_legacy_disable_plan_v1` manifest.

The planned write is exactly one INSERT into
`sheet_vitrina_v1_wb_incident_policy_revisions`:

- next seller revision;
- `active=0`, `policy_status=disabled`;
- `effective_from=2026-08-16`, no overall end date;
- source `incident_policy_legacy_disable_v1`;
- exact prior `warehouse_ids_json`, `warehouse_identities_json`,
  `warehouse_entries_json` and `legacy_payloads_json` preserved.

Apply requires the exact outside-Git reviewed manifest file SHA, its embedded
fingerprint, exact deployed SHA, immutable owner apply-gate reference/digest and
an outside-Git evidence directory. Before INSERT it rechecks the storage
generation, source revision/row digest, complete prior policy-history digest and
protected-table digests; under `BEGIN IMMEDIATE` it repeats the source revision
check. It creates one coherent mode-0600 SQLite backup and then reconciles:

- the appended row equals the reviewed target;
- all prior policy rows are byte-semantic identical;
- ready snapshots, raw WB snapshots, projection cache, incident
  rematerialization audit and incident quantity evidence tables are unchanged;
- no incident rematerialization function is invoked;
- exact repeat is `already_applied` / T0 with no second revision.

Standalone `--readback` is query-only and proves the current disabled revision
and protected-table fingerprints. Recovery is fail-closed and forward-only by
default: never UPDATE/DELETE the append-only policy history. A new superseding
revision requires a separate reviewed owner gate; coherent backup restoration
is allowed only with all writers quiesced and proof that no later valid writes
would be lost.

Production apply uses the normal `scope:production-mutation` two-gate contract:
the pre-merge release gate authorizes the exact PR head; after merge/deploy, the
dry-run manifest is generated against the exact deployed merge SHA; a distinct
post-merge apply gate authorizes that manifest; reconciliation and trusted-main
terminalization bind the exact gate/comment/manifest/evidence digests.

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
  names/dates/revisions and configured/effective consistency;
- `apps/wb_incident_policy_legacy_ui_browser_smoke.py` proves default collapse,
  explicit lazy load, keyboard disclosure, aggregate-only identity retention,
  exact history and mobile/dark/console health;
- `apps/wb_warehouse_exclusion_browser_smoke.py` proves Supply retains hidden
  backend calculation state without ordinary incident UI or browser writes;
- `apps/wb_incident_policy_legacy_disable_smoke.py` proves dry-run/apply gates,
  coherent backup, one append, non-target/history invariance, query-only
  readback and idempotent T0 repeat.

## Explicit exclusions

No incident loss quantity is created, restored or inferred. No historical
incident snapshot is rematerialized. Factory order, supply/replenishment,
recommendation, SKU planning, regional allocation, raw WB/FBS, FF/FBS ledger,
mappings/facilities, seller readback, 26GN527/acceptance, collectors/lifecycle,
WB writes and unrelated promo/historical recovery remain outside scope.
