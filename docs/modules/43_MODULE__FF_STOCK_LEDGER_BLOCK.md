---
title: "Модуль: ff_stock_ledger_block"
doc_id: "WB-CORE-MODULE-43-FF-STOCK-LEDGER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract server-owned FF quantity ledger: единый пользовательский остаток в `Остатки / Склады -> Склад FF`, Excel preview/confirm ручных документов, автооприходование supplier shipments, автосписание WB supplies и расчётный источник `Остатки ФФ`."
scope: "Operator supply contour for FF quantity operations plus the reused balance source for the unified warehouse screen: runtime SQLite operation headers/lines/previews, original manual Excel storage, protected legacy HTTP routes, operator operation journal, idempotent supplier/WB auto movements, and factory-order/WB regional stock_ff source. FIFO, партии, себестоимость, бухгалтерский склад, 1C writes, WB mutations and Google Sheets/GAS are out of scope."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
related_modules:
  - "packages/application/ff_stock_ledger.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/factory_order_supply.py"
  - "packages/application/wb_regional_supply.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/wb_supplies.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
related_tables:
  - "sheet_vitrina_v1_ff_stock_operation_previews"
  - "sheet_vitrina_v1_ff_stock_operations"
  - "sheet_vitrina_v1_ff_stock_operation_lines"
  - "sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks"
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks/export.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/ff-stocks/preview"
  - "POST /v1/sheet-vitrina-v1/supply/ff-stocks/confirm"
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks/operations/{operation_id}/file"
related_runners:
  - "apps/ff_stock_targeted_reconciliation.py"
  - "apps/ff_stock_targeted_reconciliation_smoke.py"
  - "apps/ff_stock_targeted_reconciliation_runner_smoke.py"
  - "apps/ff_stock_ledger_smoke.py"
  - "apps/ff_stock_ledger_http_smoke.py"
  - "apps/factory_order_supply_smoke.py"
  - "apps/wb_regional_supply_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "`Остатки ФФ` are computed from an append-only quantity ledger. Manual Excel documents require preview then explicit confirm, auto supplier movements are idempotent by source key, and ordinary WB auto writeoffs remain guarded by an explicit repair/mechanics checkpoint with baseline-known WB cache/source/supply keys, ledger activation/opening balance and no-negative-balance checks. A separate repo-owned v2 runner is bounded to WB supply `40561872`: it can bypass only the exact pair `wb_supply_before_auto_writeoff_checkpoint` + `wb_supply_before_ledger_activation` after a read-only dry-run, exact fingerprint confirmation, integrity-checked SQLite backup and atomic cache/goods/status/activation/nomenclature/balance/total recheck; reversal is an audited compensating receipt and never deletes history. Ordinary pre-activation supplies remain fail closed. Calculations can choose `stock_ff_source=ff_stock_ledger` without removing manual Excel or `1С / Фулфилмент` sources."
---

# 1. Contract

`Поставки` exposes top-level section `ФФ` with operational subsections:
- `Услуги ФФ` for the existing fulfillment service upload/payment-validation contour.
- `Операции остатков ФФ` for manual receipt/writeoff and the existing ledger journal.

The old `Остатки ФФ` navigation item is a compatibility transition to `Остатки / Склады -> Склад FF`. The new screen reads this same ledger and does not own a second FF calculation or balance table.

`Остатки ФФ` is not an editable snapshot table. Current balance is computed from ledger lines:
- manual receipt documents add quantity;
- manual writeoff documents subtract quantity;
- supplier shipment acceptance on ФФ adds quantity;
- eligible WB supplies subtract quantity only after the WB auto-writeoff checkpoint exists, the supply is not part of the baseline-known historical cache, the ledger is activated by a positive non-WB receipt/correction and the movement would not make the SKU balance negative.

Negative balances can still exist from explicit manual documents or older incidents and must be shown as `Отрицательный остаток ФФ`; calculations must not crash only because the ledger balance is negative, but they must surface a clear warning that recommendations are limited by available ФФ stock.

# 2. Operator UI

The unified `Остатки / Склады -> Склад FF` screen shows the only user-facing current balance registry for active, non-hidden nomenclature SKU and the warehouse opening document. The `Поставки -> ФФ -> Операции остатков ФФ` subsection keeps:
- operation journal;
- current-balance XLSX export;
- manual `Оприходовать`;
- manual `Списать`.

The legacy protected status/export endpoints remain compatible for integrations and operational tooling. They are not a second source of truth.

The operation journal shows operation datetime, operation type, source type, linked source object label/id, actor when available, SKU count, total quantity, warnings and source-file link for manual Excel documents. Auto operations link to their source object by label/id and do not have a file. When diagnostics are present, the object cell may also show technical identifiers such as `source_key`, WB `cache_key` or repair reversal ids so archived incidents remain auditable without a physical delete.

The journal is paginated server-side. Operator UI requests `GET .../ff-stocks?operations_limit=50&operations_page=1&show_technical_archive=0` by default and supports page sizes `50`, `100` and `200`. Navigation changes only the ФФ status/journal block, not the whole operator UI.

`show_technical_archive=0` is a soft view filter only:
- hides `runtime_repair` operations;
- when a WB auto-writeoff checkpoint exists, hides operations with `created_at <= checkpoint.created_at`;
- returns `hidden_archive_count`, `total_count` and `total_all_count` so the operator can see that archived rows exist.

Enabling `Показать технический архив` requests `show_technical_archive=1` and exposes old erroneous WB `auto_writeoff` rows, runtime repair/correction rows and their source diagnostics. It does not mutate balances. Physical delete is not part of this UI.

Access uses the same supply-operator boundary as `Поставки`: owner/admin and users with access to `Поставки`. Supplier-only users are not granted this contour.

# 3. Excel Form And Manual Documents

The same XLSX shape is used for export, receipt upload and writeoff upload:
1. `barcode`
2. `nmId`
3. `SKU/название/комментарий`
4. `группа`
5. `количество`

Upload flow:
- `POST .../ff-stocks/preview` parses the workbook and stores a temporary preview with original file bytes;
- preview returns SKU count, absolute/signed quantity totals, warnings and row errors;
- rows with zero quantity are ignored so exported current balances can be reused as a stable input form after edits;
- negative quantities are row errors;
- `POST .../ff-stocks/confirm` applies only a clean non-empty preview and creates the durable operation/document;
- the original uploaded Excel file is stored on the operation and can be downloaded later from `GET .../operations/{operation_id}/file`.

There is no cell-level balance editing. Corrections are represented by new reverse manual documents or bounded runtime repair/correction operations with their own source keys.

# 4. Runtime Persistence

Runtime SQLite tables:
- `sheet_vitrina_v1_ff_stock_operation_previews` stores pending manual preview payload, summary, warnings/errors and original Excel blob until confirm/cancel.
- `sheet_vitrina_v1_ff_stock_operations` stores durable operation header: operation id, operation type, source type, idempotency/source key, source object id/label, created time, actor, SKU/quantity totals, warnings/diagnostics and optional source file metadata/blob.
- `sheet_vitrina_v1_ff_stock_operation_lines` stores signed quantity deltas by `nmId`, plus barcode/SKU/group display fields and raw row/source metadata.
- `sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint` stores the current WB auto-writeoff boundary: checkpoint id/time, actor/reason, baseline-known `cache_key`, `source_key` and `supply_id` sets, source-date watermark and diagnostics.

Balance is read as `SUM(quantity_delta)` grouped by `nmId` over durable lines. There is no separate balance snapshot source of truth.

`RegistryUploadDbBackedRuntime.list_ff_stock_operations` supports bounded pagination with `limit`, `offset`, total counting via `count_ff_stock_operations`, sorting by `created_at DESC, operation_id DESC`, and the same soft archive filter used by the operator UI. The legacy `limit`-only call still returns the first page.

# 5. Auto Receipt From Supplier Shipments

When a saved supplier shipment gets `actual_ff_acceptance_date`, the supplier registry sets `order_status=accepted_ff` and creates an automatic ФФ receipt from matched product lines.

Idempotency key:
- `supplier_shipment_acceptance:<shipment_id>`

Repeated saves/syncs/page opens do not add quantity again. Rolling status back from `Принято на ФФ` is not automated in this bounded stage; correction is a reverse manual document.

# 6. Auto Writeoff From WB Supplies

WB supply sync/backfill/detail enrichment creates automatic ФФ writeoffs from cached goods composition, not from `acceptedQuantity`.

Eligible statuses:
- `3` — Отгрузка разрешена
- `4` — Идёт приёмка
- `5` — Принято
- `6` — Отгружено на воротах

Skipped statuses:
- `1` — Не запланировано
- `2` — Запланировано

`Допринято` is skipped when `virtual_type_id == 5`, and also by safety fallback when `type_label == "Допринято"`.

Idempotency key:
- `wb_supply_debit:<cache_key or supply_id>`

If composition changes before the first writeoff, the current cached composition is used. After a writeoff exists, the backend does not auto-recalculate historical movement; correction is a manual document.

Activation and balance guards:
- WB supply sync/backfill/detail enrichment first ensures a WB auto-writeoff checkpoint against the current cache. This captures already-known historical supplies as baseline by `cache_key`, `wb_supply_debit:<cache_key or supply_id>` and `supply_id`.
- Direct WB debit calls without a checkpoint fail closed with `wb_supply_auto_writeoff_checkpoint_missing`; they do not create operations.
- A WB record matching the checkpoint baseline, or whose business timestamp is not later than the checkpoint time, is skipped as `wb_supply_before_auto_writeoff_checkpoint`. This is the repair/mechanics boundary: historical/cache-known WB supplies must not be debited retroactively.
- WB auto writeoff is blocked until the ledger has at least one positive non-WB operation (`manual_excel`, supplier auto receipt or explicit runtime correction). This is the activation/opening-balance boundary.
- A WB record whose business date (`source_created_at` / API `createDate`, then `supply_date` / `fact_date`) is earlier than the activation operation is skipped as historical cache/backfill evidence.
- If the cached WB goods composition would make any SKU balance negative, the whole automatic writeoff is skipped with diagnostics instead of silently creating a negative balance.
- Repeated sync/backfill/detail enrichment remains idempotent by `wb_supply_debit:<cache_key or supply_id>` and does not duplicate an existing writeoff.

## 6.1. Bounded Targeted Checkpoint Reconciliation

`apps/ff_stock_targeted_reconciliation.py` is the canonical operational path for the one known baseline and pre-activation incident `supply_id=40561872`. The runner rejects every other supply id, accepts no manual goods/SKU/quantity payload, reads `raw_goods` and current active nomenclature only from the server-owned runtime, and is not exposed through the operator UI or HTTP API. Plan version `v2` requires both ordinary blockers: `wb_supply_before_auto_writeoff_checkpoint` from exact baseline matches for cache/source/supply keys, and `wb_supply_before_ledger_activation` from the source timestamp being earlier than the valid activation operation timestamp.

Read-only preflight/dry-run:

```bash
python3 apps/ff_stock_targeted_reconciliation.py \
  --runtime-dir "$REGISTRY_UPLOAD_RUNTIME_DIR" \
  --supply-id 40561872
```

The plan requires exact identity `supply_id=40561872`, `cache_key=supply:40561872` and `source_key=wb_supply_debit:supply:40561872`; the incident invariants of exactly 13 SKU, total cached debit `31 500`, whole-ledger total before `38 250` and projected total after `6 750`; status `3/4/5/6`; non-`Допринято`; non-empty strictly positive cached goods rows whose `nmId` exists in active, non-hidden nomenclature; an existing ledger activation with non-empty operation id and valid timestamp; source timestamp earlier than that activation; and sufficient balance for every SKU. It reports supply/status/checkpoint/activation evidence, both bypassed ordinary blockers, every SKU balance/debit/projected balance, totals, blockers and a stable `sha256:` fingerprint. Missing/invalid activation, incomplete baseline match, changed global total or a supply that does not match this exact pre-activation incident fails closed.

Apply requires all three human gates: explicit `--apply`, the exact current dry-run fingerprint in `--confirm-fingerprint`, and an explicit `--backup-dir`. Before any ledger write the runner creates and hashes a coherent SQLite backup and requires its `PRAGMA integrity_check` result to be `ok`. The application then opens one immediate SQLite transaction, rechecks supply identity, cache key, canonical source key, status/type/source timestamps, raw goods, current checkpoint, the exact activation operation id/timestamp, active/non-hidden nomenclature rows, every affected SKU balance and the whole-ledger before/delta/after totals, and only then appends the linked `auto_writeoff / wb_supply` operation. A change to goods, status, activation, nomenclature, affected balances or non-target global total makes the fingerprint/atomic guard stale. The checkpoint and activation operations are never updated.

The target writeoff uses:
- `operation_type=auto_writeoff`;
- `source_type=wb_supply`;
- `source_key=wb_supply_debit:supply:40561872`;
- `source_object_id=40561872` and an explicit WB-supply label;
- diagnostics reason/remediation `targeted_pre_activation_remediation`, plan version `v2`, supply timestamp, activation operation id/timestamp, both bypassed ordinary blockers, the dry-run fingerprint, checkpoint evidence and before/debit/after totals.

The canonical source key keeps apply and all later ordinary WB sync/backfill/detail flows idempotent. Per-SKU negative projections block the whole operation and report `nm_id`, current balance, required debit and projected balance.

Reversibility uses the same runner with `--reversal`. It has its own dry-run/fingerprint/backup/apply gate and appends one idempotent `correction_receipt` with source type `wb_supply_targeted_reconciliation` and source key `wb_supply_debit_reversal:supply:40561872`. Diagnostics link the compensation to the original operation. Neither the original header nor its lines are deleted or rewritten.

# 7. Calculation Source

`Поставки -> Расчёты` supports three mutually exclusive `stock_ff_source` values:
- `manual_excel` — existing manual Excel `Остатки ФФ`;
- `onec_ff_stock` — existing read-only `1С / Фулфилмент`;
- `ff_stock_ledger` — new server ledger source labeled `Остатки ФФ`.

Factory-order and WB regional calculations resolve `ff_stock_ledger` into the same row contract as the other sources. Negative balances are passed through with warnings instead of being treated as missing or fatal. WB regional result diagnostics also include the ledger source state (`total_stock_ff`, `negative_sku_count`, warnings) so a zero recommendation caused by invalid/negative ФФ balances is explainable from `last_result`.

For calculation-only `Учесть WB-поставки`, statuses `3/4/6` still add future inbound/projection evidence, but selected WB supplies do not reduce `stock_ff` again when `stock_ff_source=ff_stock_ledger`: the ledger balance is already current after WB auto writeoffs. Manual Excel and `1С / Фулфилмент` keep the older transfer behavior where selected WB supplies reduce available ФФ stock and add the same quantity to inbound/projection. Ledger auto writeoff remains broader than calculation overlay and still records statuses `3/4/5/6`, while statuses `1/2` and `Допринято` are skipped.

The existing manual Excel and `1С / Фулфилмент` sources remain valid.

# 8. Smokes

Targeted smoke:
- `python3 apps/ff_stock_targeted_reconciliation_smoke.py`
- `python3 apps/ff_stock_targeted_reconciliation_runner_smoke.py`
- `python3 apps/ff_stock_ledger_smoke.py`
- `python3 apps/ff_stock_ledger_http_smoke.py`

The smoke covers manual receipt/writeoff preview-confirm-balance, Excel export/import roundtrip, negative-balance warning, supplier auto receipt idempotency, WB checkpoint fail-closed behavior, baseline-known historical WB skip, post-checkpoint WB status writeoff idempotency, statuses `1/2` skip, `Допринято` skip, factory-order ledger source without duplicate selected-WB deduction, selected-WB inbound/projection for ledger source and WB regional ledger source.
It also covers operation journal pagination metadata/second page retrieval, default status backward compatibility, archive-off visibility versus archive-on retrieval, and verifies that the archive view filter does not change computed balances.
The targeted reconciliation smokes separately cover the baseline-known status `1/2 -> 3` transition with source timestamp earlier than activation, preservation of ordinary checkpoint and ordinary pre-activation fail-closed paths, missing/invalid activation and other-supply blockers, hard-guarded exact `38 250 - 31 500 = 6 750` totals across 13 SKU, dry-run immutability and stable v2 fingerprint, one linked apply plus ordinary-sync idempotency, statuses `1/2`, `Допринято`, missing goods, inactive nomenclature, changed activation/global total and per-SKU shortage blockers, stale goods/status/nomenclature/activation/balance/total plans, coherent CLI backup with `integrity_check=ok`, post-run reconciliation and an audited history-preserving reversal.

# 9. Explicit Non-Scope

This module does not implement:
- FIFO or lot accounting;
- бухгалтерский склад;
- final товарная себестоимость;
- 1C stock writes;
- WB create/update/delete mutations;
- deletion of operations as a correction mechanism;
- Google Sheets/GAS.

# 11. Cost snapshot and reconciliation boundary

The FF quantity ledger remains the physical movement authority. Module 45 listens to its idempotent receipt/writeoff evidence, maintains a separate Decimal moving weighted cost per SKU and freezes the current FF unit cost into each WB writeoff layer. Proportional writeoff does not change the average of remaining units; confirmed/estimated quantities and capital move together.

Ordinary WB status/facts can create one guarded FF writeoff. Status `4` transfers only the cumulative `acceptedQuantity` delta into WB capital and leaves `sent - accepted` in `ФФ → WB`; transitions `3 → 4 → 5` reuse the original debit/cost snapshot and reject accepted regression. `Допринято` explicitly creates no second debit and only reconciles persisted `Недопринято WB` outstanding state. Unknown nomenclature, negative stock/capital or ambiguous identity fail closed. Pre-cutover surplus is immutable audit-only and is outside replay. A post-cutover composition mismatch is normalized only when its exact persisted operation is pinned by `CUTOVER_POSTCUTOVER_SOURCE_NORMALIZATION_V1`; no future or fingerprint-drifted operation inherits that policy.

The one-time `CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1` pins exactly 10 final-accepted supply rows / 11 units from the approved production diagnostic fixpoint. Matching happens before direct/FIFO and verifies source/date/SKU/full route/quantity/status/raw-line fingerprint plus a positive current baseline cost reference. A match is source evidence already absorbed by official WB stock: it creates no FF ledger or canonical movement row and cannot close another supply's outstanding. Any drift or future row remains a blocker.

The independent `CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V2` pins another 9 exact supply/SKU rows / 12 units without changing V1. Its stronger row contract includes empty original-supply identity plus raw persisted supply-row, goods-line, combined and semantic fingerprints. It is also audit-only and cannot create an FF receipt/debit or alter the authoritative `6 750` balance. The runner's layer-continuity gate checks that the exact recognized/paid FF debit snapshot remains unchanged in every downstream outstanding child.

From `2026-07-01`, ledger operation lines are the only physical FF quantity truth consumed by product-capital and WB-cost projections. Canonical replay applies supplier acceptance on `actual_ff_acceptance_date`, and WB debit on its persisted source business timestamp. Receipt changes FF moving WAC; writeoff freezes exact recognized/paid WAC in `sheet_vitrina_v1_canonical_cost_movement_layers`; ordinary writeoff does not change remaining WAC. Derived cost tables may cache linkage/cost, but never add, remove or reinterpret ledger quantity.

The canonical effective-date resolver does not mutate legacy ledger operations. For WB auto-writeoffs it records deterministic provenance from a valid operation source timestamp (including the bounded targeted-runner compatibility key) or resolves the exact linked persisted WB supply and its acceptance/fact/supply business-date field. A technical ledger write timestamp is not business evidence. Missing supply, ambiguous/mismatched identity, invalid timestamp or absent authoritative business date is a structured blocker. `apps/canonical_cost_engine_preflight.py` audits the full class before heavy replay; `apps/canonical_cost_engine_diagnostic.py` continues through independent pipeline branches on a coherent disposable copy and emits blocker/fixpoint/coverage evidence. Every operation dated before cutover is audit-only. The activation receipt is the cutover opening boundary; exact checkpoint writeoff + linked runtime-repair pairs are preserved as audit history and are not replayed into physical FF twice. A persisted `targeted_pre_activation_remediation` reason explicitly excludes `40561872` from that collapse, so its `2026-07-02` debit remains physical even though the identity is present in the checkpoint.

The later official accepted-line refresh for the same exact `40561872` operation is pinned by operation/source/date plus the complete sent, accepted and combined evidence fingerprints. Raw accepted quantity `31 477` is below sent quantity `31 500`, but two SKU lines each exceed their sent line by one unit while other lines retain a larger same-supply shortage. The exact normalization conserves the aggregate accepted quantity by assigning only those two units inside this supply's shortage pool. Because this targeted pre-activation operation has no legacy WB supply-cost rows, the manifest explicitly permits its complete positive canonical baseline cost references; any missing baseline cost, operation/line fingerprint drift, future operation or cross-supply allocation remains blocked.

The bounded module-45 backfill may read persisted WB cache rows only after matching an existing canonical FF ledger debit source key. This history materialization never creates or repairs a quantity-ledger operation, never bypasses ordinary checkpoint/activation rules and batches capital reconstruction before one bounded daily recalculation.

Module 45 consumes the canonical result of `CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1` and owns no exception of its own. The manifest is bounded to 10 exact persisted supplies / 11 units and verifies the approved diagnostic fingerprint together with every source/date/SKU/full-route/quantity/status/raw-line and current-baseline cost-reference field. Matched evidence is already contained in official WB stock and contributes zero new movement/capital/confirmation/underaccepted quantity; it never calls `record_wb_supply_debit`, never enters direct/FIFO, never changes supply `40561872` and does not generalize to a future row.

Module 45 consumes V2 through the same canonical decision only; it does not merge V1/V2 into a wildcard. V2 contributes 9 exact audit rows / 12 units and zero movement/capital/confirmation/underaccepted delta. Lot-level continuity is evaluated on immutable movement identities, while TOTAL stage WAC remains a ratio of the actual stage composition rather than a cross-stage monotonic sequence.
## Canonical cutover boundary (2026-07-01)

The FF ledger remains the physical source of truth. Legacy operations before cutover are retained and fingerprinted but are not replayed into the new cost movement contour. Exact post-cutover normalization never inserts a receipt, debit or correction and therefore cannot change the authoritative current FF balance.
