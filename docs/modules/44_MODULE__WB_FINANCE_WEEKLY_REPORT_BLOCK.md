# WB Finance Weekly Report Block

## Status

`ACTIVE / HOSTED RUNTIME / CANONICAL-COST V4 / STORAGE PRE-CUTOVER`

## Purpose and source boundary

`Отчёты → Финотчёт ВБ` is the canonical weekly management P&L read side for the official Wildberries Finance API. The server-owned chain is:

`Finance API → immutable raw rows → weekly/per-SKU derived projections → canonical Our WB Cost resolver → operator UI`.

Raw identity is `(seller_id, report_id, rrd_id)`. Different `reportType` values remain separate evidence and combine only in derived aggregates. Finance money is accumulated with `Decimal`; absence is never converted to zero.

## Storage and rebuildability

The logical store registry currently resolves both `finance_raw` and
`operational` to the existing canonical runtime SQLite. This implicit-monolith
default creates no manifest or destination file and preserves every existing
reader/writer. Finance and Partner runtime opens use the logical registry and
emit store/mode/operation observations; CI rejects a direct-open regression in
those migrated modules.

The current canonical runtime SQLite owns:

- `wb_finance_weekly_raw_rows`, reports and sync state;
- replaceable `wb_finance_weekly_aggregates`, coverage and reconciliation;
- indexed `wb_finance_weekly_sku_aggregates` keyed by `seller + week + nmId + formula version`;
- `wb_finance_projection_audit` for reviewed canonical applies.

The per-SKU projection stores metrics, source digest, weekly raw content hash, canonical-cost dependency hash, coverage and formula version. Active aggregate contract is `wb_finance_weekly_sku_aggregate_v4`; its coverage dependencies also pin `canonical_our_wb_cost_temporal_policy_v4`. It is fully rebuildable from immutable Finance rows and canonical sources. Preview consumers reject a stale raw hash, aggregate/cost formula version or canonical cost digest.

The staged split adds `finance_raw_ingest_batches`, immutable
`finance_raw_rows`, transactional `finance_raw_outbox` and raw consumer
cursors, plus operational inbox/receipts/cursors/dead letters and shadow
comparisons. Their schema is inert unless explicitly invoked. Optional shadow
ingest is disabled by default; while the monolith is selected it may be enabled
only so legacy Finance rows and raw/outbox evidence commit in that same
transaction. It cannot write across two files.

`wb_finance_retro_cost_map` is not created, read or written by the active schema/calculation/apply path. A table left by an earlier deployed revision may remain untouched as historical migration evidence, but its fixed `unit_cost_rub` is not business truth and cannot affect COGS.

## Single canonical COGS contract

Formula version is `canonical_our_wb_cost_temporal_policy_v4`. Finance calls the same shared warehouse-domain resolver as Vitrina, Partner and Proxy 3 and does not reproduce warehouse cost-engine rules.

For each sale/return operation of the same deterministically resolved `nmId`:

- operation date is `rrDate → saleDt → orderDt`; a missing operation date is a blocker;
- before `2026-07-01`, use the exact canonical `Себестоимость WB наша` row of that `nmId` on `2026-07-01` as a business-approved retrospective projection across all loaded Finance history;
- on/after `2026-07-01`, use the exact canonical daily row for the operation date;
- `2026-06-30` therefore uses the 01.07 row, while `2026-07-01` uses the exact 01.07 row;
- sale quantity adds COGS; return quantity subtracts COGS using the same operation-date policy. Sale-return linkage is intentionally not required.

The resolver accepts the posted functional daily contour and the warehouse-domain active versioned archival estimate source from migration 109. The latter is limited to its exact 18-SKU manifest, quality `business_approved_archival_estimate`, 100 ₽ and effective date 01.07.2026; it is not a Finance fallback or an ID branch. Missing/non-positive cost and qualities `fallback`, `fallback_average` or `zero_quantity_without_cost_basis` remain explicit blockers. Cross-SKU guesses, similar-item cost, all-SKU averages, legacy `COST_PRICE`, mutable current cost and silent zero are prohibited.

Lineage contains operation date, canonical source date/identity/version/digest, quality, selection method and formula version. Archival-estimate lineage additionally pins owner approval, effective date, 100 ₽, target manifest, production dry-run source SHA, source digest and calculation/row fingerprints. Before 01.07 the UI/tooltips describe the value as a retrospective management projection, not factual historical warehouse capital. A canonical source correction changes the digest and invalidates/rebuilds affected Finance projections automatically.

Within one plan/apply/readback connection the runner loads the small canonical daily-cost surface, active archival overlay and first factual receipt boundary once, then caches canonical resolution by `nmId + operation date`. Nomenclature identities use the same connection-bound cache. No cache survives into a new connection; apply still re-plans under its single `BEGIN IMMEDIATE` transaction and rejects a dry-run fingerprint after any intervening hourly source change. Future factual receipts therefore invalidate the next plan/apply/readback while the 18-SKU overlay cannot amplify into repeated functional-event scans.

Coverage counts gross sale/return units, so a symmetric sale/return pair cannot hide missing cost even if net COGS is zero. Fully covered weeks become calculated; a real gap lists exact `nmId`, operation date, canonical source date and reason. Repeated missing operations are losslessly collapsed by `week + nmId + operation date + reason`: the evidence retains operation count, separate sale/return quantities, gross unmatched units and signed net units, while per-row report/rrd dependencies remain bound by the cost-state hash. The plan therefore does not duplicate one blocker/matrix row per raw operation.

## Agent remuneration, acquiring and WB correction

Classifier version is `wb_finance_weekly_classifier_v3_signed_review_points`.

For sale/return signed by document type:

```text
combined_commission_control = retailPriceWithDisc − forPay
acquiring                   = acquiringFee
agent_remuneration          = combined_commission_control − acquiring
```

`agent_remuneration + acquiring` must reconcile exactly to the former combined control. The UI rows are `Агентское вознаграждение WB` and `Эквайринг`; each enters total expenses exactly once. `ppvzSalesCommission` is not used as the full agent amount.

Official `additionalPayment` / XLSX `Корректировка Вознаграждения Вайлдберриз (ВВ)` is retained as an explicit disclosure. On sale/return it is already reflected by `forPay` and is not added a second time. A standalone positive/negative correction row is classified once as positive adjustment or period correction. Tests include a non-zero correction.

Deduction money preserves the signed official value. A negative deduction is an expense reversal/refund and therefore reduces its bucket; it is never converted into a second positive expense with `abs()`. Exact production names containing `Баллы за отзывы` or `Списание за отзыв` use the separate `review_points` bucket and Finance UI row. The existing Finance `marketing` bucket remains separate and continues to participate in both expense-with-marketing and expense-without-marketing disclosures. Negative acceptance or transit cannot become a positive capitalization candidate.

## Expense and profit semantics

`total_wb_expenses` is displayed as `Расходы WB с маркетингом`. `wb_expenses_without_marketing = total_wb_expenses − marketing` is displayed last in the expense block. Both cells include amount and percentage of positive weekly net revenue. The duplicate percentage row and user-facing `Расходы периода, учитываемые в прибыли` row are absent; the latter remains an internal formula field.

Paid acceptance/transit addback is allowed only when exact Finance `giId/supplyId + canonical nmId` matches a current canonical supply cost layer with a source fingerprint. Each layer's acceptance/transit cap is allocated deterministically in Finance-operation chronology across the entire loaded history, not reset per week. The aggregate addback can therefore never exceed the layer's current capitalized amount. Unmatched, later-after-cap, or excess Finance amount stays in period expenses and is disclosed in technical lineage. A canonical layer manifest participates in the calculation fingerprint, so a corrected layer forces rebuild/dry-run drift detection. This prevents both blanket addback and double capitalization.

Global capitalization allocations are built once for the coherent SQLite connection used by one plan/apply/readback pass and then reused by the global and every per-SKU aggregate. A new connection always rebuilds the raw/supply-layer manifest and allocations, so a later Finance sync or canonical layer correction invalidates the cache. This avoids the accidental `weeks × SKUs × all cost layers` re-hashing path without weakening source drift detection.

The existing stale-derived hook now checks every loaded week, including the backward historical projection. It compares canonical cost state, classifier version and the complete deterministic metrics payload, so both a corrected 01.07 cost and a corrected supply-layer cap invalidate every affected historical projection instead of only post-cutover COGS.

```text
profit_period_expenses = total_wb_expenses
                         − proven_capped_acceptance_addback
                         − proven_capped_transit_addback
profit_after_cogs = net_revenue − profit_period_expenses
                    + positive_adjustments − COGS
```

Every expense money cell shows amount plus only `%`, arrow and color. An increased expense share is deterioration: red/pink `↓`. A decreased share is improvement: green `↑`. Effectively unchanged is neutral/yellow `→`; the first/missing-base week has no arrow. Numeric deltas and `п.п.` are forbidden.

## Ads compatibility

Finance preflight and Partner consumers share `resolve_ads_snapshot_payload`. It accepts either a valid nested `result` or the persisted root envelope `{kind,snapshot_date,items}`. Invalid/missing data remains missing; a confirmed `kind=empty` is the only empty-source zero. Finance apply never writes ads rows and never materializes missing ads pairs as zero.

The bounded read-only `partner-finance-diagnostic` hosted action pins the
selected storage manifest and reads immutable raw rows plus indexed operational
projections from their exact files through SQLite `mode=ro`,
`PRAGMA query_only=ON` and a rolled-back coherent transaction. For the exact
server-owned Partner `nmId`/weeks it reconciles ads, direct and account-level
Finance marketing, the revenue allocation coefficient, the former catch-all
residual, current explicitly routed Partner categories, classifier buckets,
signed versus legacy-absolute amounts, duplicate identities and
negative-deduction uplift. Its exact semantic-category totals cover every
retained operation group even when detailed output is truncated. It is
evidence only: it cannot rebuild Finance or mutate Partner/ads state.

## Production-safe all-history runner

Local application runner:

```bash
python3 apps/wb_finance_weekly.py canonical-cost-backfill \
  --runtime-dir /canonical/runtime
```

Dry-run is default and read-only. With no date bounds it covers all loaded Finance history and emits:

- date/week/raw-row/nmId scope;
- Finance, ads and canonical-cost manifests/digests;
- canonical 01.07 and post-01.07 exact-date rows;
- week × nmId × operation-date matrix with sale/return quantities, unit cost and signed COGS;
- before/after COGS, profit and margin plus before/after/delta for every profit input, the explicit identity `profit delta = before-COGS delta − COGS delta`, fields/sources and explanation;
- agent/acquiring reconciliation and capitalization lineage;
- before state and expected readback of every indexed per-SKU weekly projection consumed by Partner Report;
- target/non-target digests, write set, blockers, backup/recovery plan and exact fingerprint;
- explicit invariants: no fallback average, silent zero, legacy cost or retro-map read/write; raw Finance, ads and canonical cost are non-target.

The all-history evidence path is bounded-memory: ordered raw and non-target identities are fed into streaming JSON-array digests instead of being retained as Python lists, and expected target evidence contains only the persisted aggregate/coverage/per-SKU state that apply reads back. Each week calculates canonical COGS details once, reuses that coverage for the global metrics, and reuses the already parsed rows for its per-SKU projections; details are then released after collapse into the required operation-date matrix. Expected and persisted per-SKU readback rows use the same canonical numeric-nmID/non-numeric ordering, so mixed 9- and 10-digit catalogues cannot create a false digest mismatch from SQLite TEXT ordering. This changes neither formulas nor evidence scope. `apps/wb_finance_weekly_canonical_scale_smoke.py` exercises 295,919 sale rows across certified/archival-estimate and deliberately missing cost states plus 50,000 functional events and 50,000 supply cost layers, and fails on row/quantity/layer loss, archival-quality rejection, duplicated gap evidence, excessive runtime or excessive peak RSS.

The former `business-approved-backfill` runner and every former fingerprint are permanently revoked.

Canonical-cost hosted operations expose only:

- `finance-canonical-dry-run`;
- `finance-canonical-apply`;
- `finance-canonical-readback`.

The separate storage-split lifecycle exposes:

- `finance-storage-snapshot-plan|apply|integrity`;
- `finance-storage-snapshot-retention-plan|apply|readback` for exact
  archive-first release of older-SHA coherent snapshots to the dedicated
  backup device when they block pre-candidate capacity;
- `finance-storage-stale-writer-plan|stop` for one exact audited stale
  closure-retry generation before a completely fresh snapshot plan;
- `finance-storage-candidate-abort-plan|apply|readback` for an exact partial or
  completed-but-unselected generation after exact shadow deactivation, never a
  selected generation;
- `finance-storage-split-dry-run|apply|health`;
- `finance-storage-shadow-status|activate|reconcile|verify|deactivate` and
  `finance-storage-live-tail-apply`;
- `finance-storage-cutover-plan|apply`;
- `finance-storage-rollback-plan|prepare|apply`.

Plan, health and status are query-only. Snapshot apply automatically acquires
the manual HTTP/UI write barrier, captures exact writer/timer intent, drains
known writers, copies a coherent SQLite image, restores exact prior intent and
releases the barrier; the full integrity scan then runs on the copy outside the
live DB. Candidate build, live-tail and soak do not keep a broad hold.
Snapshot retention never opens the live monolith or business stores for write:
it copies only stale migration snapshots, verifies every archived byte and a
durable crash-resume transaction, then releases the root copy with its
manifest last. The archive remains byte-exact evidence, while ambiguity leaves
the source intact.
Candidate abort likewise preserves the canonical monolith and every snapshot.
It deletes only a fingerprint-bound allowlist from one unselected generation;
active/mismatched shadow state, incomplete completed checkpoints, a global
selected manifest, an opener or any unknown file fails closed.
Cutover and rollback apply require their exact held barrier and perform a fresh
operational reconciliation before one fsynced manifest switch. Only the
registry HTTP service is restarted. An ambiguous post-switch failure keeps
manual and automatic writes fail closed. Original monolith and split
generations remain retained; retirement is a different exact gate. See
migration 124.

After split cutover, weekly ingestion commits the immutable raw snapshot,
batch-row links and outbox event to the raw store before updating operational
reports/projections. Operational connections registry-attach raw as read-only
and expose a connection-local current-snapshot view; the operational file
never recreates `wb_finance_weekly_raw_rows`. Repeated unchanged source
snapshots are no-ops, changed snapshots retain history without duplicating the
current view, and every raw attach is generation-identity checked and observed.

Apply requires a newly reviewed exact fingerprint, external plan file, approval
reference and the retained compatibility backup-directory argument. That
argument cannot redirect recovery artifacts or regain a full-store backup. The
runner holds the canonical `.warehouse-functional-sync.lock` from its
current-plan recheck through central T2 warehouse/cost/derived-Finance
checkpoint, `BEGIN IMMEDIATE` apply and transactional readback, so
hourly/manual warehouse sync, replay, downstream cost-layer materialization and
economics publication cannot change the canonical cost inputs inside that
interval. Finance raw is read only as calculation input and is excluded from
the checkpoint. For a long production apply the separate repo-owned
`warehouse-functional-maintenance status|hold|restore` lifecycle stops only the
hourly timer, waits for an already-running service without killing it, persists
the exact mode-`0600` timer/service baseline and later restores its
enabled/active state; an explicitly authorized broader quiet window uses
`business-data-maintenance hold`, whose durable warehouse sub-mode retains that
restorable baseline while leaving the timer disabled and inactive. Neither path
weakens or normalizes the reviewed fingerprint. The runner rejects
drift/blockers, writes only derived Finance/audit rows, verifies global and
per-SKU target readback/non-target digest, and rolls back on any mismatch. It
persists a separate post-apply fingerprint: an unchanged exact repeat returns
an audited T0 no-op without a second checkpoint, while any later
raw/ads/cost/target drift invalidates the old approval.

Production apply is not implied by merge/deploy and remains forbidden until the new all-history dry-run receives explicit human approval.

## UI and verification

The operator table has clean calculated headers, separate agent/acquiring/review-points rows, compact expense microcells, sticky metric column and table-local horizontal scroll. Real coverage errors appear once at report level with SKU reasons. Its storage card shows exact generation/schema ids, cursors, lag, mismatches, actionable dead letters, free capacity and rollback/cutover readiness without creating schema or switching a store.

After split cutover, Finance and Partner attach the manifest-selected raw
generation read-only and expose only the connection-local current-row
compatibility view. A weekly raw/outbox commit is acknowledged only after the
complete operational report/aggregate/SKU/coverage/reconciliation projection
for the exact source hash reads back; a raw-first crash stays pending and is
resolved by the idempotent scheduled replay. The production Partner/Finance
diagnostic pins the same manifest and opens both selected files `mode=ro` with
`query_only=ON`.

Targeted checks:

- `python3 apps/wb_finance_weekly_smoke.py`;
- `python3 apps/wb_finance_weekly_cost_cutover_smoke.py`;
- `python3 apps/wb_finance_weekly_business_approved_backfill_smoke.py`;
- `python3 apps/wb_finance_weekly_canonical_scale_smoke.py`;
- `python3 apps/wb_finance_weekly_stale_cost_safety_smoke.py`;
- `python3 apps/wb_finance_weekly_browser_smoke.py`;
- `python3 apps/finance_storage_split_smoke.py`;
- `python3 apps/finance_storage_candidate_abort_smoke.py`;
- `python3 apps/finance_storage_stale_writer_recovery_smoke.py`;
- `python3 apps/finance_storage_sqlite_open_inventory.py --check-migrated`;
- `python3 apps/warehouse_functional_maintenance_smoke.py`;
- `python3 apps/partner_report_smoke.py`;
- `python3 apps/partner_finance_production_diagnostic_smoke.py`;
- `python3 apps/partner_report_browser_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`.

Authenticated production acceptance uses `finance-ui-flow` in a fresh isolated Chromium context. It is calculation/read-only: it may POST preview/XLSX generation but never saves settings, finalizes a partner report or changes Finance/business data. It also requires the storage API/card to prove the implicit canonical monolith, identical logical generation ids, rollback readiness and no selected cutover. Acceptance is fail-closed: `preview.attempted=true`, `preview.ready=true`, empty blockers, visible table, enabled download and an actually downloaded/opened semantic XLSX that reconciles nmId, selected weeks, source/formula digest and displayed Decimal amounts are all mandatory. A non-empty but wrong workbook, hidden sheet, external link, missing download or incomplete preview fails the flow.

## Unified recovery-policy boundary

The weekly Finance source table remains Finance raw and is never recoverable
through T1/T2 warehouse artifacts. A bounded stale-cost correction uses T1
exact derived-row before images; a wide canonical derived publication uses T2
and checkpoints only the warehouse/cost/derived-Finance domain. The reviewed
raw Finance input is read for calculation only and cannot enter the checkpoint.
T0 repeats create no recovery bytes. Earlier full/coherent-backup wording for
these publication paths is superseded by module 51; T3 remains available only
to explicit allowlisted schema/store migrations.
