---
title: "Модуль: ff_stock_ledger_block"
doc_id: "WB-CORE-MODULE-43-FF-STOCK-LEDGER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract server-owned FF quantity and reservation ledgers: единый пользовательский физический/зарезервированный/доступный остаток в `Остатки -> Склады и себестоимость -> Склад FF`, Excel preview/confirm ручных документов, автооприходование supplier shipments, guarded WB movements и расчётный источник `Остатки ФФ`."
scope: "Operator supply contour for FF quantity operations and physically justified WB-supply reservations plus the reused balance source for the unified warehouse screen: runtime SQLite operation/reservation/lifecycle headers and lines, audited inventory reconciliation, original manual Excel storage, protected legacy HTTP routes, idempotent supplier/WB movements, exact FF debit-cost snapshots, identity/availability guards and factory-order/WB regional stock_ff source."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
related_modules:
  - "packages/application/ff_stock_ledger.py"
  - "packages/application/ff_pool_foundation.py"
  - "packages/contracts/ff_pool_foundation.py"
  - "packages/application/ff_pool_documents.py"
  - "packages/application/ff_pool_documents_xlsx.py"
  - "packages/contracts/ff_pool_documents.py"
  - "packages/application/ff_inventory_reconciliation.py"
  - "packages/application/ff_overhead_allocation.py"
  - "packages/application/ff_document_workflow.py"
  - "packages/application/ff_warehouse_documents.py"
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
  - "sheet_vitrina_v1_ff_stock_reservation_operations"
  - "sheet_vitrina_v1_ff_stock_reservation_lines"
  - "sheet_vitrina_v1_ff_stock_wb_supply_lifecycle"
  - "sheet_vitrina_v1_ff_inventory_reconciliations"
  - "sheet_vitrina_v1_ff_inventory_previews"
  - "sheet_vitrina_v1_ff_overhead_previews"
  - "sheet_vitrina_v1_ff_overhead_documents"
  - "sheet_vitrina_v1_ff_workflow_request_aliases"
  - "sheet_vitrina_v1_ff_workflow_events"
  - "sheet_vitrina_v1_ff_facilities"
  - "sheet_vitrina_v1_warehouse_business_operations"
  - "sheet_vitrina_v1_ff_pool_movement_lines"
  - "sheet_vitrina_v1_warehouse_business_operation_relations"
  - "sheet_vitrina_v1_ff_pool_feature_epochs"
  - "sheet_vitrina_v1_ff_pool_balances"
  - "sheet_vitrina_v1_ff_pool_parity_diagnostics"
  - "sheet_vitrina_v1_ff_pool_document_requests"
  - "sheet_vitrina_v1_ff_pool_document_request_aliases"
  - "sheet_vitrina_v1_ff_pool_documents"
  - "sheet_vitrina_v1_ff_pool_document_lines"
  - "sheet_vitrina_v1_ff_pool_document_expense_lines"
  - "sheet_vitrina_v1_ff_pool_document_relations"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks"
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks/export.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/ff-stocks/preview"
  - "POST /v1/sheet-vitrina-v1/supply/ff-stocks/confirm"
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks/operations/{operation_id}/file"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/inventory/template.xlsx"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/inventory/preview"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/inventory/status"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/inventory/confirm"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/inventory/rollback"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/overhead/preview"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/overhead/status"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/overhead/confirm"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/overhead/reversal/preview"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/overhead/reversal/confirm"
related_runners:
  - "apps/warehouse_cost_unified_recovery.py"
  - "apps/ff_stock_targeted_reconciliation.py"
  - "apps/ff_stock_targeted_reconciliation_smoke.py"
  - "apps/ff_stock_targeted_reconciliation_runner_smoke.py"
  - "apps/ff_stock_ledger_smoke.py"
  - "apps/ff_pool_documents_smoke.py"
  - "apps/ff_stock_reservation_smoke.py"
  - "apps/ff_inventory_reconciliation.py"
  - "apps/ff_inventory_reconciliation_smoke.py"
  - "apps/ff_overhead_allocation_smoke.py"
  - "apps/ff_warehouse_documents_smoke.py"
  - "apps/warehouse_targeted_replay_smoke.py"
  - "apps/ff_stock_ledger_http_smoke.py"
  - "apps/factory_order_supply_smoke.py"
  - "apps/wb_regional_supply_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "`Остатки ФФ` are computed from an append-only physical ledger plus separate append-only reservation and WB-supply lifecycle journals. A default-off facility × FBS|FBO foundation, durable documents and protected Stage 3 explanatory surface now exist strictly below this aggregate authority; no current producer or aggregate reader uses them. A WB debit requires exact whole composition, physical availability and a frozen positive same-SKU FF WAC; missing downstream add-ons do not block movement, but missing/stale FF WAC does and keeps an explicit reservation. Confirmed cancellation or two distinct complete official-snapshot gaps returns only the unaccepted remainder at the exact original debit cost. Manager inventory and overhead use durable request/preview/document/replay state machines with exact reload-safe readback."
---

> Functional boundary: конкретные incident values `38 250 / 31 500 / 31 477 / 6 750` ниже — immutable migration/ledger evidence, а не текущие warehouse totals. После `warehouse_functional_cutover_v1` активные `FF`, `FF → WB` и discrepancy projections рассчитывает module 48 из fresh WB state и этого append-only ledger; cutover preflight отдельно доказывает FF-debit/checkpoint coverage каждой gated supply и не подгоняет quantity по историческим числам.

# 1. Contract

`Поставки` exposes top-level section `ФФ` with operational subsections:
- `Услуги ФФ` for the existing fulfillment service upload/payment-validation contour.
- `Операции остатков ФФ` for manual receipt/writeoff and the existing ledger journal.

The old `Остатки ФФ` navigation item is a compatibility transition to `Остатки -> Склады и себестоимость -> Склад FF`. The new screen reads this same ledger and does not own a second FF calculation or balance table.

`Остатки ФФ` is not an editable snapshot table. Current balance is computed from ledger lines:
- manual receipt documents add quantity;
- manual writeoff documents subtract quantity;
- supplier shipment acceptance on ФФ adds quantity;
- eligible WB supplies subtract quantity only after the WB auto-writeoff checkpoint exists, the supply is not part of the baseline-known historical cache, the ledger is activated by a positive non-WB receipt/correction, identity/composition are exact and the whole composition is physically available;
- an eligible but physically unavailable or identity-ambiguous WB supply creates or adjusts a reservation keyed by its exact supply revision and `nmID`; missing cost alone cannot create a reservation. Reservation quantity is not a physical movement and carries no WAC/capital.

Negative balances can still exist from explicit manual documents or older incidents and must be shown as `Отрицательный остаток ФФ`; calculations must not crash only because the ledger balance is negative, but they must surface a clear warning that recommendations are limited by available ФФ stock.

# 2. Operator UI

The unified `Остатки -> Склады и себестоимость -> Склад FF` screen shows the only user-facing current balance registry for active, non-hidden nomenclature SKU and the warehouse opening document. Its summary and each SKU line distinguish `Физический остаток`, `Зарезервировано`, `Доступно` and `Необеспеченный резерв`; a reservation-only line is `Ожидает поступления`, has physical quantity/capital zero and no fabricated WAC. The `Поставки -> ФФ -> Операции остатков ФФ` subsection keeps:
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

На unified FF page рядом с остатками находятся два business-document action:

- `Скачать шаблон` строит полный XLSX по всем active/non-hidden `nmId` на выбранную business date с отдельным текстовым `Штрихкод` из canonical primary barcode; ведущие нули и длинные identifiers сохраняются, а явная строка с нулём обязательна, поэтому отсутствие SKU не может означать ни «не считали», ни «считать нулём»;
- `Загрузить инвентаризацию` сохраняет original bytes/SHA и абсолютный физический target в preview, до состояния ready проверяет полное разрешение номенклатуры и наличие положительной same-SKU cost basis для каждой target-строки, затем предоставляет одну отдельную кнопку `Провести инвентаризацию`;
- `Накладные расходы FF` принимает business date, положительную RUB-сумму и основание, показывает exact allocation preview и только после отдельного confirm создаёт immutable cost-only документ.

Оба action используют server-owned `ff_document_workflow_v1`, а не локальное
состояние вкладки. Клиент создаёт `request_id` до upload, сервер быстро и
идемпотентно фиксирует exact input identity, затем тяжёлый plan выполняется
асинхронно. `GET .../inventory/status` и `GET .../overhead/status` восстанавливают
те же preview/document/replay после network abort, reload или restart. Exact
repeat с новым request id создаёт только alias к тому же content-addressed
preview и остаётся T0.

Публичные стадии фиксированы: `данные приняты` → `проверка завершена` → `готово
к проведению` → `документ проведён` → `распределение/пересчёт завершён`.
Preview/ready остаются жёлтыми и никогда не означают проведение. Большая зелёная
отметка inventory `Инвентаризация проведена: <before> → <target>` и текст
`Остатки обновлены` допустимы только после durable functional/economics
completion и exact target readback. Между commit и replay UI показывает
partial `Документ проведён; пересчёт выполняется`; повторять business document
не требуется. Overhead сохраняет собственную финальную формулировку.

Inventory делает bounded DB-free проверку XLSX до постановки plan job. Row-level
`code/details` сохраняются структурированно; одинаковые date mismatch
группируются в одно русское сообщение с датами, диапазонами и количеством
строк, остальные ошибки получают краткую локализованную сводку и не более
ограниченного набора примеров. Blocker всегда держит confirm disabled и не
очищает file/date fields.

Открытие страницы, раскрытие реестра, фильтрация и download шаблона не проводят документов. Все mutation routes защищены тем же supply-operator auth boundary.

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
- `sheet_vitrina_v1_ff_stock_reservation_operations` stores append-only `reserve/adjust/release/fulfill` revisions with supply id, exact source revision/state fingerprint and provenance.
- `sheet_vitrina_v1_ff_stock_reservation_lines` stores signed reserved quantity by `nmID`; the current reservation is the sum of lines and is never included in physical balance or capital.

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

If composition/status changes before the first writeoff, one idempotent reservation adjustment or release brings the current reserved composition to the exact current supply revision. After a physical writeoff exists, the backend does not auto-recalculate historical movement; correction is a manual document.

Future-arrival reservation contract:
- the whole WB supply composition is the atomic unit; no partial fulfillment is inferred from packed/accepted counters without authoritative partial-shipment evidence;
- insufficient physical availability creates/updates a reservation and never creates negative FF quantity/capital or an FF→WB movement;
- missing/pending downstream cost never creates or keeps a cost-only reservation and does not block the physical debit or unrelated official WB snapshot/publication;
- sufficient physical availability creates the physical debit and reservation `fulfill` in the same transaction. Known FF capital is carried; missing add-ons make WAC/capital preliminary or unavailable with an explicit reason, never synthetic zero;
- physical debit and reservation-fulfill operation IDs are deterministic hashes of the canonical supply source/revision and operation type; retries cannot manufacture a second identity for the same movement;
- a factual supplier FF receipt immediately reconciles active reservations, with no extra operator action;
- repeat cost replay enriches the existing movement and cannot create another quantity debit or reservation.
- validated positive supplemental Seller Portal transit evidence enters the same canonical supply cost layer as official/approved downstream components and triggers bounded reconciliation for that supply; it is never inferred from display text or a tariff formula. One missing cost cannot prevent fulfillment of another fully proven reservation.

Activation and balance guards:
- WB supply sync/backfill/detail enrichment first ensures a WB auto-writeoff checkpoint against the current cache. This captures already-known historical supplies as baseline by `cache_key`, `wb_supply_debit:<cache_key or supply_id>` and `supply_id`.
- Direct WB debit calls without a checkpoint fail closed with `wb_supply_auto_writeoff_checkpoint_missing`; they do not create operations.
- A WB record matching the checkpoint baseline, or whose business timestamp is not later than the checkpoint time, is skipped as `wb_supply_before_auto_writeoff_checkpoint`. This is the repair/mechanics boundary: historical/cache-known WB supplies must not be debited retroactively.
- WB auto writeoff is blocked until the ledger has at least one positive non-WB operation (`manual_excel`, supplier auto receipt or explicit runtime correction). This is the activation/opening-balance boundary.
- A WB record whose business date (`source_created_at` / API `createDate`, then `supply_date` / `fact_date`) is earlier than the activation operation is skipped as historical cache/backfill evidence.
- If the cached WB goods composition would make any SKU balance negative, the whole automatic writeoff is skipped with diagnostics instead of silently creating a negative balance.
- Repeated sync/backfill/detail enrichment remains idempotent by `wb_supply_debit:<cache_key or supply_id>` and does not duplicate an existing writeoff.

### Exact debit cost and automatic return lifecycle

Before a new physical FF debit the ledger freezes `cost_snapshot` on every
line from the active functional `ff` balance: exact `version_id`, business
date, plan fingerprint, positive WAC and signed capital. A publication older
than an intervening positive FF movement is rejected as
`wb_supply_ff_cost_snapshot_missing`; the supply remains reserved until the
functional replay publishes its new FF WAC. Several debits in one bounded run
may reuse the same pre-run WAC because a proportional debit does not change
moving WAC. Transit, FF services, storage and WB acceptance remain downstream
cost layers and may be completed later without repeating the physical debit.

`sheet_vitrina_v1_ff_stock_wb_supply_lifecycle` records complete official
supply observations. One missing response is `missing_debounced`, and repeating
the same observation id does not advance the counter. A proven cancelled
status is immediate evidence; otherwise two distinct complete active-slice
observations are required. Only `packed - accepted` is returned, and every
return line inherits the exact original FF debit WAC/capital. The economic
return identity is one idempotent operation per original debit and canonical
supply-source revision; later observation timestamps/proof revisions cannot
mint another movement after a crash or lost lifecycle pointer. Reappearance
after return is recorded but creates neither a second debit nor a second
return. A cache row with no physical debit needs no stock return and may be
removed after debounce; historical accepted rows retain their ordinary cache
preservation rule.

### Audited manager inventory reconciliation

`apps/ff_inventory_reconciliation.py` is the only runner for a manager XLSX
physical target. Dry-run is default. It validates exact headers/business date,
one resolved active/non-hidden nomenclature identity per row and полное покрытие каждой active/non-hidden identity (включая явные zero targets), current FF balances, confirmed
supply-return proofs and the existence of a positive same-SKU FF cost basis no later than the
business date for every target SKU, including rows whose preview delta is zero. The hierarchy is exact original debit, same-date FF WAC, last
earlier FF WAC, latest certified landed inbound cost, then only an explicit
positive row in `sheet_vitrina_v1_ff_inventory_cost_bases` whose source type is
`exact_original_source_debit` or `business_approved_estimate`; the latter also
requires immutable approval and provenance fields. Proven returns are separate documents. Remaining positive and
negative SKU deltas become one inventory receipt and one inventory writeoff,
respectively; direct balance updates and synthetic zero cost are forbidden.
The confirmation identity is the immutable target intent: source bytes/SHA-256,
business date, complete resolved target quantities and pinned stable
nomenclature identities. It deliberately excludes the volatile global active
functional version identity. That version remains audit context in the plan
and final manifest, but an unrelated publication cannot change the target
confirmation token.

Новый default header profile — `nmId / Штрихкод / Комментарий SKU / Остаток ФФ / Дата остатка`; прежний exact четырёхколоночный `nmId` profile остаётся совместимым. Строка может использовать unique `nmId`, text-only primary/additional barcode из `barcode + barcodes_json` или оба поля, если они разрешаются в одну позицию. Empty/unknown/ambiguous/conflicting identity, duplicate SKU после resolution, numeric/formula/scientific/fractional barcode representation и неполный active target fail closed до confirm.

Confirm approves the target, not the preview delta. It rereads current canonical
FF quantities, return proofs and positive same-SKU cost bases, recomputes the
actual correcting delta to the approved target and rechecks target/non-target
ledger invariants under `BEGIN IMMEDIATE`. A concurrent ledger writer,
publication or SQLite snapshot/busy race triggers a bounded internal reread and
retry; the browser does not own a stale/revalidate workflow. The successful
attempt stores the XLSX content-addressed evidence and an immutable manifest
with actual before/delta/target, chosen positive costs/bases, return proof,
ledger digests and audit-only active functional version, then atomically appends
the parent, at most one linked receipt and one linked writeoff, and exactly one
canonical targeted queue row. HTTP confirm заканчивается после durable
ledger+queue readback и не выполняет functional/economics replay синхронно.
Idempotency is bound to the exact source/date/target intent: double click,
response loss, reload or exact retry returns the same reconciliation and never
applies the target twice. Stored ready previews from the earlier full-manifest
fingerprint format derive this intent from their persisted target and confirm
normally after upgrade without re-upload. Readback proves every target SKU and
total. Recovery tier T1 appends exact inverse-cost
compensating documents; it never deletes or rewrites the source/audit history.

### FF overhead allocation

`sheet_vitrina_v1_ff_overhead_documents` stores one immutable header for `Распределение накладных расходов FF`: business date, positive two-decimal RUB amount, reason, actor, idempotency key, exact source revision/fingerprint, positive physical denominator, per-SKU allocations and readback. Eligible denominator is the sum of positive physical FF quantity on that date. Reservation rows, `FF → WB`, zero/negative quantities and arrivals after the date are excluded.

Allocation uses `Decimal`: `amount * qty_i / total_positive_qty`, rounds down to kopecks and assigns the deterministic remainder by largest fractional part then `nmId`. The exact sum must equal the header amount. Ledger lines carry `quantity_delta=0` and immutable `cost_adjustment` with allocation capital, physical basis quantity, per-unit amount, date, reason and revision. Functional replay applies them at the end of their business date, increases capital/WAC only, proves the physical basis unchanged and publishes only the affected SKU/economics closure. It never copies supply-specific Fulfillment service/storage already accounted in WB supply layers.

Exact input repeat is T0. Reversal requires preview/fingerprint and appends `ff_overhead_reversal` lines with the exact negative original allocations. It neither hard-deletes nor recalculates historical allocation proportions.
Primary overhead confirm also returns immediately after its existing atomic
document+queue commit. The hourly/manual warehouse worker owns the durable
functional/economics continuation and records its economics completion on the
same queue row; an HTTP response loss is therefore recovered by exact
document/status readback and cannot create a second document or replay row.

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
- `python3 apps/ff_stock_reservation_smoke.py`
- `python3 apps/ff_stock_ledger_http_smoke.py`
- `python3 apps/ff_inventory_reconciliation_smoke.py`
- `python3 apps/ff_overhead_allocation_smoke.py`
- `python3 apps/ff_warehouse_documents_smoke.py`

The smokes cover manual receipt/writeoff preview-confirm-balance, Excel export/import roundtrip, negative-balance warning, supplier auto receipt idempotency, WB checkpoint fail-closed behavior, baseline-known historical WB skip, post-checkpoint WB status idempotency, statuses `1/2` skip, `Допринято` skip, future-arrival reservation, multiple reservations of one SKU, supply adjustment/cancellation, waiting-for-cost isolation, full-composition atomic fulfillment, no negative physical/capital and factory-order/WB regional source compatibility.
It also covers operation journal pagination metadata/second page retrieval, default status backward compatibility, archive-off visibility versus archive-on retrieval, and verifies that the archive view filter does not change computed balances.
The FF document pilot smokes additionally cover complete template/async-preview/status/confirm/repeat/compensating rollback, grouped localized validation, network loss at accept and post-commit response, reload/restart recovery, exact retry/double click without duplicate preview/document/reconciliation/queue/event rows, functional-only partial state, economics error and final completion, exact quantity-proportional overhead with quantity invariant and reversal, business/technical projection separation, localized receipt/shipment/reservation rows, server-side filters/search/date/pagination and legacy journal compatibility.
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

From `2026-07-01`, ledger operation lines are the only physical FF quantity truth. Pre-functional `sheet_vitrina_v1_canonical_cost_movement_layers` остаётся migration/audit evidence; после `warehouse_functional_cutover_v1` module 48 replay сам получает supplier acceptance по `actual_ff_acceptance_date`, фиксирует moving FF WAC в момент WB debit и публикует единственную active warehouse/cost version. Derived compatibility tables may cache linkage/components, but never add, remove or reinterpret ledger quantity.

The canonical effective-date resolver does not mutate legacy ledger operations. For WB auto-writeoffs it records deterministic provenance from a valid operation source timestamp (including the bounded targeted-runner compatibility key) or resolves the exact linked persisted WB supply and its acceptance/fact/supply business-date field. A technical ledger write timestamp is not business evidence. Missing supply, ambiguous/mismatched identity, invalid timestamp or absent authoritative business date is a structured blocker. `apps/canonical_cost_engine_preflight.py` audits the full class before heavy replay; `apps/canonical_cost_engine_diagnostic.py` continues through independent pipeline branches on a coherent disposable copy and emits blocker/fixpoint/coverage evidence. Every operation dated before cutover is audit-only. The activation receipt is the cutover opening boundary; exact checkpoint writeoff + linked runtime-repair pairs are preserved as audit history and are not replayed into physical FF twice. A persisted `targeted_pre_activation_remediation` reason explicitly excludes `40561872` from that collapse, so its `2026-07-02` debit remains physical even though the identity is present in the checkpoint.

The later official accepted-line refresh for the same exact `40561872` operation is pinned by operation/source/date plus the complete sent, accepted and combined evidence fingerprints. Raw accepted quantity `31 477` is below sent quantity `31 500`, but two SKU lines each exceed their sent line by one unit while other lines retain a larger same-supply shortage. The exact normalization conserves the aggregate accepted quantity by assigning only those two units inside this supply's shortage pool. Because this targeted pre-activation operation has no legacy WB supply-cost rows, the manifest explicitly permits its complete positive canonical baseline cost references; any missing baseline cost, operation/line fingerprint drift, future operation or cross-supply allocation remains blocked.

The bounded module-45 backfill may read persisted WB cache rows only after matching an existing canonical FF ledger debit source key. This history materialization never creates or repairs a quantity-ledger operation, never bypasses ordinary checkpoint/activation rules and batches capital reconstruction before one bounded daily recalculation.

Module 45 consumes the canonical result of `CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1` and owns no exception of its own. The manifest is bounded to 10 exact persisted supplies / 11 units and verifies the approved diagnostic fingerprint together with every source/date/SKU/full-route/quantity/status/raw-line and current-baseline cost-reference field. Matched evidence is already contained in official WB stock and contributes zero new movement/capital/confirmation/underaccepted quantity; it never calls `record_wb_supply_debit`, never enters direct/FIFO, never changes supply `40561872` and does not generalize to a future row.

Module 45 consumes V2 through the same canonical decision only; it does not merge V1/V2 into a wildcard. V2 contributes 9 exact audit rows / 12 units and zero movement/capital/confirmation/underaccepted delta. Lot-level continuity is evaluated on immutable movement identities, while TOTAL stage WAC remains a ratio of the actual stage composition rather than a cross-stage monotonic sequence.
## Canonical cutover boundary (2026-07-01)

The FF ledger remains the physical source of truth. Legacy operations before cutover are retained and fingerprinted but are not replayed into the new cost movement contour. Exact post-cutover normalization never inserts a receipt, debit or correction and therefore cannot change the authoritative current FF balance.

## Confirmed supplier acceptance boundary

Changing a supplier shipment actual FF-acceptance date is a server-confirmed mutation, not a browser-side Save. Its preview is read-only. A valid one-use confirmation creates at most one receipt with source key `supplier_shipment_acceptance:<shipment_id>`, at most one current supplier FF cost layer, then reconciles WB reservations and eligible `FF → WB` movements. Repeating the consumed token or the same acceptance source is idempotent. A stale shipment/dependency revision fails closed before receipt, layer or movement creation.

## Unified recovery-policy boundary

Manual FF documents and the reviewed targeted WB-supply reconciliation now
derive stable operation identities and use central T1 journal/lifecycle state.
An exact repeat is T0. The policy records the exact document/shipment/SKU
closure and never requests a coherent store backup; the legacy
`--backup-dir` argument is compatibility-only. The former
`ff_reservations_transit_cost_recovery` apply entrypoint remains disabled.
Earlier coherent-backup wording for bounded FF recovery is superseded by
module 51.

## Projection outbox

Every immutable FF operation line creates a durable
`ff_stock_physical_movement` projection request in the same ledger transaction.
Identity is `operation_id + line_no + nm_id`; the business date is the
explicit operation `business_effective_date` (source acceptance/shipment date
when available, otherwise the canonical Yekaterinburg date of a manual
operation), while `created_at` remains audit time. Requests coalesce by
operation revision, earliest date and SKU closure.

An FF-only revision never guesses capital. If exact capital/event proof is not
yet available, the owned projection keeps last-good values visibly
provisional or unavailable and leaves every non-owned Vitrina cell untouched.
The subsequent canonical event/full functional publication consumes the same
source revision; no second FF ledger and no Vitrina-side quantity calculator
are introduced.

## Default-off facility × pool foundation

Migration 133 adds an empty dimensional subledger contract below the existing
aggregate FF ledger. `sheet_vitrina_v1_ff_facilities` is a stable registry with
immutable identity/code and mutable display/active metadata, but deployment
does not seed Moscow, Orenburg or any other business facility. Generic posted
operation headers carry exact source identity, revision, idempotency epoch,
Yekaterinburg `business_date` and UTC audit time. Their pool lines carry
`facility_id`, `FBS|FBO`, `nm_id`, exact SQLite `INTEGER` quantity and Decimal
TEXT capital/WAC. The new header/line contour is append-only. Stage 1 itself
introduced no posting service or route.

`correction_of`, `storno_of` and `late_expense_for` are the only initial typed
relations. A child type must match its relation, the parent cannot be later
than the child, duplicates are rejected and a recursive insert guard prevents
cycles. Relations exist only between new operation roots; legacy FF rows are
not backfilled and their missing root/relation remains normal.

Feature epochs are absent by default, which means both future writer and reader
are off. A reader cannot be configured before the writer and does not become
effective until a current-epoch parity diagnostic passes. Empty detail is a
neutral `detail_empty` state. A populated fixture compares every SKU and the
exact quantity/capital totals against the caller-owned aggregate FF readback;
the reader also requires the same current aggregate revision and unchanged
detail fingerprint. Any mismatch or drift is fail-closed for the future detail
reader and never edits or invalidates the aggregate ledger. Current FF writers,
reservations, warehouse publication, public totals, Vitrina and recommendations
remain unchanged.

Migration 134 adds the Stage 2 posting contract above those same operations and
movements. Immutable request/document/line/expense evidence and a guarded typed
document graph cover future opening, China allocation, transfer children,
FBO↔FBS reallocation, inventory, pool-scoped overhead, correction, storno and
late expense. Open transfer quantity/capital is derived from the immutable root
and children; no transit warehouse or reservation is introduced. T1 recovery
stores only exact request/balance before-images. Quantity remains INTEGER;
capital/expense allocation is Decimal/minor-unit exact and deterministic.

The Stage 2 XLSX helpers generate/parse China allocation and one-table FBS/FBO
inventory workbooks with facility dropdowns. They enforce bounded ZIP/OOXML,
sheet/header/profile/fingerprint limits before openpyxl, text-safe exact
nmId/barcode resolution, complete selected-scope coverage and fail closed on
formulas, macros, external links, malformed/ambiguous evidence or an empty
facility registry. The service is deployed but default-off and non-routed: it
does not seed facilities/epochs, switch current supplier/FF writers or readers,
or apply production business data.

Migration 135 exposes protected bounded facility/pool reads and a compact
operator document lifecycle above this inert foundation. GET paths are
strictly query-only and use current aggregate-revision parity, exact Decimal
summation, pagination/ETag and lazy evidence. Facility management and document
preview/confirm remain behind the existing writer epoch plus supply-role,
write-barrier and same-origin CSRF gates. Deploy adds only an empty immutable
facility-change audit table and creates no facility, epoch, opening, document,
movement, ledger operation or projection. Existing aggregate FF inventory,
overhead, reservations and physical ledger behavior are unchanged.
