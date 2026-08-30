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
  - "migration/152_fbs_handoff_cost_and_overhead_backfill.md"
  - "migration/157_fbs_lifecycle_forward_recovery.md"
  - "migration/161_applicability_gated_dense_fbs.md"
  - "migration/162_dense_fbs_zero_and_historical_material_recovery.md"
related_modules:
  - "packages/application/ff_stock_ledger.py"
  - "packages/application/ff_pool_foundation.py"
  - "packages/contracts/ff_pool_foundation.py"
  - "packages/application/ff_pool_documents.py"
  - "packages/application/ff_pool_fbs_lifecycle.py"
  - "packages/application/ff_pool_fbs_applicability.py"
  - "packages/application/ff_pool_dense_fbs.py"
  - "packages/application/warehouse_fbs_material_rematerialization.py"
  - "apps/ff_pool_dense_fbs.py"
  - "apps/warehouse_fbs_historical_recovery.py"
  - "apps/wbc0013_fbs_recovery.py"
  - "packages/application/ff_pool_fbs_forward_recovery.py"
  - "packages/application/ff_pool_overhead_backfill.py"
  - "packages/application/russian_payment_orders.py"
  - "packages/application/ff_pool_zero_physical_production.py"
  - "packages/application/ff_fbs_mapping_extension_production.py"
  - "packages/application/ff_pool_documents_xlsx.py"
  - "packages/application/ff_wb_supply_origins.py"
  - "packages/application/wb_fbs_orders.py"
  - "packages/application/wb_fbs_warehouse_registry.py"
  - "packages/application/wb_fbs_shadow_polling.py"
  - "packages/application/ff_stage_7a_production.py"
  - "packages/contracts/ff_pool_documents.py"
  - "packages/application/ff_inventory_reconciliation.py"
  - "packages/application/ff_overhead_allocation.py"
  - "packages/application/ff_document_workflow.py"
  - "packages/application/ff_warehouse_documents.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/factory_order_supply.py"
  - "packages/application/fbs_fulfillment_order.py"
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
  - "sheet_vitrina_v1_ff_pool_overhead_payment_evidence"
  - "sheet_vitrina_v1_wb_supply_ff_origin_assignments"
  - "sheet_vitrina_v1_wb_supplies_fbs_order_observations"
  - "sheet_vitrina_v1_wb_supplies_fbs_collector_state"
  - "sheet_vitrina_v1_wb_supplies_fbs_status_current"
  - "sheet_vitrina_v1_wb_supplies_fbs_status_transitions"
  - "sheet_vitrina_v1_wb_supplies_fbs_poll_runs"
  - "sheet_vitrina_v1_wb_fbs_warehouse_registry_runs"
  - "sheet_vitrina_v1_wb_fbs_warehouse_registry_rows"
  - "sheet_vitrina_v1_wb_fbs_stock_snapshot_runs"
  - "sheet_vitrina_v1_wb_fbs_stock_snapshot_rows"
  - "sheet_vitrina_v1_wb_fbs_binding_requests"
  - "sheet_vitrina_v1_wb_fbs_binding_confirmations"
  - "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
  - "sheet_vitrina_v1_ff_pool_fbs_drain_state"
  - "sheet_vitrina_v1_ff_pool_fbs_identity_pending"
  - "sheet_vitrina_v1_ff_pool_fbs_identity_pending_resolutions"
  - "sheet_vitrina_v1_ff_pool_fbs_mapping_extensions"
  - "sheet_vitrina_v1_ff_pool_fbs_mapping_extension_allocations"
  - "sheet_vitrina_v1_ff_pool_fbs_forward_generations"
  - "sheet_vitrina_v1_ff_pool_fbs_forward_state"
  - "sheet_vitrina_v1_ff_pool_fbs_backlog_recovery_runs"
  - "sheet_vitrina_v1_ff_pool_fbs_backlog_recovery_targets"
  - "sheet_vitrina_v1_ff_pool_fbs_applicability_events"
  - "sheet_vitrina_v1_ff_pool_fbs_dense_intents"
  - "sheet_vitrina_v1_ff_pool_fbs_dense_intent_events"
  - "sheet_vitrina_v1_warehouse_fbs_material_intents"
  - "sheet_vitrina_v1_warehouse_fbs_material_intent_events"
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
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/documents/overhead/preview"
  - "GET|POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-supply-origins[/{supply_ref}]"
  - "GET /v1/sheet-vitrina-v1/supply/fbs-fulfillment-order/status"
  - "POST /v1/sheet-vitrina-v1/supply/fbs-fulfillment-order/calculate"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/fbs-orders[/{order_id}]"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-warehouses"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-warehouses/binding/preview"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-warehouses/binding/{request_id}/confirm"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/facilities/preview"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/facilities/onboarding/{request_id}/confirm"
related_runners:
  - "apps/warehouse_cost_unified_recovery.py"
  - "apps/ff_stock_targeted_reconciliation.py"
  - "apps/ff_stock_targeted_reconciliation_smoke.py"
  - "apps/ff_stock_targeted_reconciliation_runner_smoke.py"
  - "apps/ff_stock_ledger_smoke.py"
  - "apps/ff_pool_documents_smoke.py"
  - "apps/ff_pool_zero_physical_production.py"
  - "apps/ff_pool_zero_physical_production_smoke.py"
  - "apps/ff_fbs_mapping_extension_production.py"
  - "apps/ff_fbs_mapping_extension_production_smoke.py"
  - "apps/ff_pool_fbs_forward_recovery.py"
  - "apps/ff_pool_fbs_forward_recovery_smoke.py"
  - "apps/ff_pool_dense_fbs.py"
  - "apps/ff_pool_dense_fbs_smoke.py"
  - "apps/warehouse_fbs_material_rematerialization_smoke.py"
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
  - "apps/ff_wb_supply_origins_smoke.py"
  - "apps/ff_wb_supply_origins_http_smoke.py"
  - "apps/ff_wb_supply_origins_browser_smoke.py"
  - "apps/wb_fbs_orders_collector_smoke.py"
  - "apps/wb_fbs_orders_http_smoke.py"
  - "apps/wb_fbs_warehouse_registry.py"
  - "apps/wb_fbs_warehouse_registry_smoke.py"
  - "apps/wb_fbs_shadow.py"
  - "apps/wb_fbs_shadow_polling_smoke.py"
  - "apps/ff_stage_7a_production.py"
  - "apps/ff_stage_7a_production_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "`Остатки ФФ` use one append-only physical ledger plus separate reservation/order journals. Migration 162 adds manifest-driven dense zero repair and one bounded historical material lane; both are owner-gated and inert on deploy, while missing remains distinct from zero."
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

The independent own-FBS fulfillment-order planner reads the facility × FBS read model directly for one selected active facility. Its available operand is the signed value `physical − reserved`; it never reads legacy aggregate `current_stock_ff`, WB stock, FBO pools or WB overlays. Global FBS readiness may remain fail-closed when another active facility is incomplete, but that does not hide or block a selected facility whose every active-SKU physical row is exact. Conversely, any missing selected-facility physical row is unavailable rather than zero and blocks that facility. In the national-demand MVP only FF Москва is executable; FF Оренбург remains visible and explicitly blocked until both its exact ledger and a later demand-allocation stage are approved.

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

### Stage 6 cutover preparation (default-off, unapplied)

Migration 138 adds a query-only manifest planner, immutable checkpoint/recovery
evidence and a SQLite-local warehouse-domain write epoch for a later exact
aggregate-FF opening into facility × `FBS|FBO`. Deployment creates no facility,
feature epoch, opening, reservation, movement or historical FBS debit. The
planner requires externally selected UTC `T`, exact active functional revision,
per-SKU quantity/capital parity, exact mappings, complete pre/post-`T`
classification and explicit active-FBW origin. `supplierStatus=complete` is
explicitly forbidden as a debit trigger; Stage 6 defines no debit trigger.
Classification is accepted only against append-only official-status shadow
evidence with the exact order revision, evidence digest and positive quantity;
Stage 5 identity observations alone cannot make a plan ready. Signed canonical
quantity and exact Decimal capital, including fractional kopecks, are preserved.

The shipped CLI is read-only and has no apply action. The transactional path is
fixture-only and requires a marker absent from operational schema. Production
opening remains a separate exact-SHA/human-gated mutation.

Migration 141 adds only a faster official FBS observation contour below this
ledger.  Immutable same-order transition evidence and query-only readiness may
inform a later owner-gated design, but create no reservation, debit, movement,
opening or pool balance.  `supplierStatus=complete` remains prohibited and no
`wbStatus` value is selected as a live trigger by this module.

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
exact integer quantities plus canonical RUB minor-unit capital against the
caller-owned aggregate FF readback; the reader also requires the same current
aggregate revision and unchanged detail fingerprint. Any quantity mismatch,
canonical-kopek mismatch, unattributed/cross-boundary residual or drift is
fail-closed for the future detail reader and never edits or invalidates the
aggregate ledger. Raw sub-kopeck differences with identical canonical values
remain append-only diagnostic evidence. Current FF writers,
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

Migration 136 adds only append-only FBW supply origin evidence: an existing real
WB supply may point to one existing active FF facility in fixed pool `FBO`.
Assignment/correction is guarded by the same absent-by-default writer epoch,
idempotent request identity, current-assignment CAS, supply-role, write barrier
and same-origin CSRF. It does not invoke the existing WB auto-writeoff,
reservation or lifecycle paths, and creates no operation, document, movement or
balance. No producer consumes this evidence in Stage 4.

### Stage 7A default-off operator continuation

Migration 139 adds `Настройки → Склады`, an additive facility profile (`city`
plus reserved future fields), and guided China → FF acceptance. Facility
identity/code remain immutable, city is not unique and no address or seed is
introduced. Deactivation rejects unfinished requests and non-zero pool
balances. The fixed FBS/FBO pools remain system-owned.

The guided document replaces routine acceptance-date editing. Its workbook
records immutable expected quantity, actual accepted quantity, exact
nmId/barcode/SKU, FBS/FBO allocation, proportional pool-scoped expenses and
discrepancy evidence. A related immutable discrepancy child is posted with the
root. Template generation uses a non-persisting exact supplier-cost preview.
Fractional-kopeck supplier capital crosses the document boundary by rounding
the aggregate header once (`ROUND_HALF_UP`) and distributing its kopecks by
largest fractional remainder then `nmId`; exact/canonical totals, per-SKU
residuals and residual owners are immutable evidence. Per-row independent
rounding, synthetic zero and double capital are forbidden. Finalization pins
the source revision and materializes the supplier FF cost layer with both
actual accepted quantities and the same normalized per-SKU capital used by the
pool movements. The minor-unit boundary applies to each new immutable movement,
not to the pre-existing pool balance: opening/cutover capital remains its
authoritative exact Decimal and apply adds the signed kopeck delta without
rounding or rewriting that prior value. The inverse storno therefore subtracts
the same kopeck delta and restores the exact prior capital. Guided aggregate
apply and aggregate/detail arithmetic use the same bounded 160-digit Decimal
context as the pool writer and ordinary functional publisher. Operational
parity then applies the centralized `rub_minor_unit_round_half_up_v1` policy:
quantities remain exact INTEGER, while capital must match in canonical kopecks
per SKU and in total. Raw Decimal values, per-SKU residuals and their conserved
total remain diagnostic/audit evidence; a canonical mismatch, boundary-crossing
total or failed attribution stays fail-closed. The ordinary publisher keeps
both the facility/pool fold and final aggregate serialization inside the exact
context, so it never rewrites or silently discards those raw tails.
A zero-quantity
close with a non-zero fractional residual remains fail-closed. The service is
the only future owner of the factual date, existing
aggregate receipt, detail movements and targeted replay, and rejects a prior
receipt/date. Confirm requires both the writer epoch and applied opening;
without them preview is durable but all business posting remains zero.
Before a guided preview exposes `confirm_allowed=true`, it query-only builds the
full posting plan, rechecks the exact supplier source revision and durably pins
the immutable `ff_guided_acceptance_business_effect_v1`: request/source/file,
epoch, business date, document identities, every line/movement, exact
quantity/capital normalization, pool allocation and expenses. Pool, aggregate
and dependent before-state digests remain explicit diagnostic evidence, but
are not the owner decision identity because ordinary FBS reservations,
releases and debits may legitimately advance them after preview. Readiness also
recomputes global current-epoch parity between the complete active functional
`ff` revision and every facility × pool row. Exact quantity and centralized
canonical-money mismatches are blocked, while an attributed sub-kopeck raw tail
with identical canonical totals is diagnostic only. Confirm acquires the shared
warehouse writer lock,
rebuilds a current parity-passing plan, requires the same stored business-effect
digest, then creates the recovery T1 from that current before-state and repeats
the plan under `BEGIN IMMEDIATE`. Functional publication, pool documents and
the normal FBS lifecycle drain cannot overlap this bounded plan → T1 → commit
window. A legitimately advanced before-state is rebased; changed receipt
identity/date/allocation/quantity/capital/expense/source/epoch still fails
closed. The lock is released before cost-layer replay/final readback, so the
continuous collector is never disabled and delays only its bounded lifecycle
drain. Once ordinary publication restores parity, an identical request blocked
only by this parity/plan guard may be reprocessed in place, without a duplicate
request or business effect. A pre-upgrade identical ready request is refreshed
in place; it never
creates a duplicate request or business effect. The request-level source
revision is deliberately the hash of the raw supplier revision plus the exact
workbook SHA-256, while the workbook manifest retains the raw supplier
revision. Readiness and confirm compare current raw supplier truth only with
that manifest value and independently recompute the request-level binding; the
two revision layers are never compared as though they were the same value. An
identical request incorrectly blocked by the former raw-versus-combined check
may be reprocessed in place only when the caller reproduces the stored combined
revision and it recomputes from the immutable manifest plus workbook digest.
Real source drift remains blocked on the live raw recheck.

An accepted SKU does not have to exist in the current aggregate `ff` snapshot:
new inbound inventory is frozen as an absent semantic-zero before-state and its
first positive row is materialized only by confirm. Quantity, canonical capital
and `cost_covered_quantity` advance together; no synthetic zero or capital-only
row is allowed. The immutable plan explicitly names every semantic-zero SKU.

The exact guided replay and its recovery are append-only. A storno is admitted
only when supplier factual state, every affected pool/aggregate row and the
current cost layer still equal the immutable original after-state. It appends a
negative legacy receipt, reverses the pool document, restores factual status/date
and the prior cost-layer state, returns aggregate FF to the frozen before-state
and queues affected-SKU replay. Any downstream reservation/debit, source/cost
revision or affected balance drift blocks compensation; delete/ad-hoc SQL/blind
restore is not a recovery path. Previously present aggregate rows are restored
field-for-field. A row first created by the acceptance remains as an audited
zero row after storno (zero quantity/capital/cost coverage and null WAC), which
is the canonical no-delete equivalent of its frozen absent state. A recovered
shipment can be accepted again only through a fresh source revision and a new
immutable request.

Routine facility/pool inventory uses a full selected-scope workbook and stores
immutable `pool_inventory` absolute-target lines. If an included FBS target is
explicitly zero while the exact facility × FBS × SKU balance row is absent,
confirmation materializes an audited row with `quantity=0`, `capital_rub=0`,
`wac_rub=NULL` and the root inventory document as source watermark. The
zero-to-zero result creates no movement, quantity/capital delta or synthetic
cost. Its absent before-image is part of the same T1 balance-key closure and a
row appearing between plan and commit fails closed. Existing rows, unselected
pools and every SKU outside the uploaded full scope remain unchanged. This is a
general operator-document rule, not a SKU/facility allowlist or hidden
backfill; missing rows remain unknown until an audited inventory document is
confirmed. Migration 149 reuses this same rule through one separately
owner-authorized, exact-manifest production runner for 41 already confirmed
`FF Москва × FBS` zeros. That runner may insert only absent zero rows, while an
existing target row, facility/catalog drift or any non-target effect fails
closed.

Migration 140 separately activates only the facility registry and FBS shadow:
`FF Москва` is active, `FF Оренбург` is inactive, and the fixed system pools
remain unchanged. The repo-owned production runner does not acquire a writer
epoch, apply an opening, post a guided acceptance, touch aggregate FF
quantity/capital or create a ledger/reservation/movement. Exact target/env
before-images and post-apply invariants make this activation evidence-bearing,
but it is not permission to run the later physical opening/cutover stage.

### Official seller warehouses, stock readback and generic onboarding

`wb-core-fbs-warehouse-registry.timer` runs every 15 minutes independently of
the five-minute order collector. It reads the official seller warehouse and
office registries, then uses official read-only `POST /api/v3/stocks/{id}` for
timestamped `seller warehouse × chrtId` stock evidence. A registry or stock
failure is append-only status evidence and never blocks order ingestion,
lifecycle or Finance. Missing returned `chrtId` is partial coverage, not zero.
The exact `chrtId → nmId` scope currently comes from observed orders and active
exact identity mappings; even when it covers every active known `nmId`, it is
reported as `observed_identity_scope_only` and `complete=false` because those
sources do not prove the full official WB size/chrt catalog.

The latest official warehouse remains visible by stable positive WB ID, name,
office ID/name/city and evidence digest even when unbound; orders likewise
remain visible. There is no fuzzy/name/city auto-link and no user-facing
virtual-warehouse entity. The generic operator flow supports both exact
directions over current entities: official unbound WB warehouse to an existing
or newly created internal facility, and an internal facility in
`Ожидает привязки к WB` state to a later discovered unbound warehouse. New
confirmed bindings enforce one active official warehouse to one internal FBS
facility without rewriting historical/legacy mappings. Each preview pins the
latest official evidence, facility revision and exact IDs; confirm appends one
mapping plus audit only. Its recovery scope names that warehouse's unresolved
identities and never automatically starts a global backlog replay.

Creating an internal facility is also audited preview/confirm. An explicitly
inactive facility remains an empty retained registry subject with no mapping or
WB mutation. If active publication is requested, Migration 161 first stages the
facility inactive, posts and reads back the complete active-SKU `pool_inventory`
roster, and only then publishes active. Activation, binding and transfer remain
distinct decisions. A facility can therefore never become an active
applicable-missing operand during ordinary onboarding.

WB-declared stock is reconciliation-only. The UI shows internal physical,
official declared, delta, timestamp, completeness and source digest. For an
unbound warehouse internal quantity/capital is unavailable and excluded from
physical/capital totals. The readback never creates receipt, movement,
inventory, quantity, capital, WAC or implicit zero, and stale/unavailable
evidence is non-blocking. Synthetic API/browser/mobile coverage proves both
binding directions, inactive facility onboarding and zero physical/capital
effect; production creation/binding/transfer requires a later explicit
operator preview and confirm.

### Applicability-gated dense FBS

Migration 161 makes current FBS applicability explicit without adding another
physical ledger. Every active facility × active/non-hidden positive-`nmId` SKU
is applicable by default. The only override is a dated append-only
`inapplicable` event with reason, actor and provenance; reinstatement is another
dated event and requires the retained physical row. Applicability is not
materialized as one default row per pair.

Facility activation and SKU activation/reactivation follow
`staged -> materializing -> [resumable] -> materialized -> active`. The
canonical onboarding entrypoints are the nomenclature atomic-save path calling
`activate_staged_skus` and the facility create/update surface calling
`activate_facility`; neither path may publish its subject active first. The
immutable intent pins the roster, writer epoch, applicability, materialized
balance before-images, compact existing-coverage proof, subject CAS and plan
fingerprint. Materialization reuses canonical `pool_inventory`
request/document/absolute-target/readback evidence: an absent applicable row is
inserted as `quantity=0`, `capital_rub=0`, `wac_rub=NULL`; an existing row is
retained exactly. A retained canonical zero receives the same immutable dense-T0
receipt without being rewritten. Neither case emits a movement or changes
quantity/capital/WAC.
Facility activation covers the full applicable SKU roster. SKU activation covers
only the staged SKU across every active facility after proving all pre-existing
pairs complete through a streamed receipt; it persists neither the existing
cross-product nor default-applicability events and cannot opportunistically
repair an older gap.
The registry subject stays inactive until completed coverage is rechecked in the
final publication transaction. Shared warehouse locking, idempotent request
identity and canonical status readback handle concurrency and ambiguous
transport without active-then-catch-up or blind retry.

Retirement through delete, inactive/hidden save or `nmId` replacement holds the
same lock and fails before publication for any non-canonical-zero FBS row,
missing applicable coverage at an active facility, active reservation or
unfinished lifecycle/order dependency. Canonical-zero
archive/reactivation retains the balance, documents and history. Current reads
use the canonical EKT business-date helper, not process-local date.
Facility deactivation holds the same shared lock and requires canonical-zero
FBS physical/capital/WAC plus no pending pool request, active FBS reservation,
open reconciliation, unresolved mapped identity or unfinished mapped FBS order.
The existing quantity guard for other pools remains unchanged; these new FBS
dependency rules do not extend FBO semantics.

Receipts, writeoffs, transfers, reservations and FBS order lifecycle writers
must find an existing applicable physical row. They cannot create it implicitly.
The server-owned component contract is `exact | exact_zero | missing |
inapplicable` with reason and provenance. Missing never means zero. Archive and
reactivation retain all rows/documents and cannot reset a balance. FBO and WB are
outside dense initialization.

The first explicit zero is valid only from its proven dense `T0`. Its immutable
inventory line plus request manifest remains the coverage receipt after later
movements advance the current balance watermark. A pre-T0 business event is
routed to explicit reconciliation/forward recovery rather than copying current
zero into history. The generic repair adapter accepts a reviewed current-state
v3 manifest containing the owner-approved missing identities, complete
stock-managed roster and exact existing-row partition; no production identity
is compiled into the generic adapter. Historical captures, missing/NULL
presentation rows, fixed capture/date counts and cross-lineage semantic equality
are immutable audit evidence only and are absent from admission and CAS. The
current-state planner instead proves exact target identity and absence, exact
seller-warehouse mapping, roster/allocation, current target/non-target rows and
no Orenburg-scoped current material lifecycle, reservation, reconciliation,
unresolved identity or unprocessed handoff. Closed history and Moscow operations
do not block; an actual current Orenburg conflict does. The plan pins active
target, StoreRegistry generation and all current material CAS. Explicit apply
additionally requires the exact
plan fingerprint, actor and approval reference, repeats qualification before
and under the shared writer lock, persists one `repair` intent and submits one
deterministic `pool_inventory` request containing only `0 / 0 / NULL` inserts.
Repeat and ambiguous transport use canonical request readback; there is no blind
second submit. Query-only readback proves roster coverage, one document and its
absolute-target lines, zero movements, forward `T0`, history-write count zero
and non-target preservation. Deployment performs no production repair and the
adapter is inert until a separately approved manifest is applied.

The 26-August material-version addendum closes a separate post-T publication
gap. Lifecycle debits, guided receipt/recovery and pool overhead no longer edit
an accepted functional `ff` row in place. Inside the same physical-ledger
transaction and shared warehouse lock they derive exact FF
quantity/capital/WAC/coverage from facility × pool rows, materialize a complete
successor functional version plus its reservation/snapshot/audit/read-model and
business-projection closure, and switch active last under CAS. A canonical
version missing its same-date WB snapshot fails closed; legacy in-place
compatibility is limited to the reviewed pre-T opening or a test contour with no
canonical version row. Thus no active reader can observe a `1952` quantity with
`1953` cost coverage or location evidence.

The post-cutover lifecycle commit also creates the canonical targeted queue row
bound to its immutable lifecycle event and successor version. Existing version
binding hides the prior ready material, so restart resumes economics from that
durable identity without repeating the handoff. Pool overhead keeps its existing
same-transaction queue; guided receipt/recovery keeps its durable posted/replay
continuation. There is no active-then-best-effort recalculation window.

Source-effect time and current material-publication time are separate exact
facts. A lifecycle operation keeps the canonical business date derived from its
immutable status observation, while its current aggregate successor and targeted
queue bind the exact active functional version's business date. The latter must
also equal the canonical business date of `published_at`; a stale or future
active version fails closed. A late source observation may therefore catch the
current aggregate up without rewriting a closed ready snapshot or pretending
that the source event occurred on the current date. The source and material
dates are both present in the successor fingerprint/provenance. The frozen
cutover-manifest date is never reused as the business date of an ordinary
post-cutover debit.

The bounded internal recovery planner accepts only one proven active-date
facility × FBS × SKU mismatch, binds the current balance watermark to an
immutable lifecycle event or pool-document operation, recomputes own cost and
the dependent Proxy 3/4 TOTAL closure, and pins source/target/roster/provenance/
ready CAS. Its append-only intent states are `repairable`, `repairing`,
`repaired`, `retry_exhausted`, `historical_recovery_required` and
`unsafe_ambiguous`; the complete bounded plan is persisted so process restart
resumes the same identity rather than rebuilding or blindly retrying it. The
active-date lane is unchanged. A separate owner-gated historical lane accepts
only one manifest-bound `business_date × accepted good version × facility × FBS
× SKU × immutable handoff_debit`. WBC0013 selects the accepted
`2026-08-26 × 428853741 × whfv_cb0657c384d5adebae01e585 ×
ffbf_87cea959c9d600da99caa1ab68ef` identity directly and never enumerates a
broad mismatch set. It validates event source, status and
evidence/full-row digests plus separately typed version-plan, full-version-row,
accepted-target-row and provenance bindings. One explicitly bound StoreRegistry
generation supplies every query-only dependency; qualification performs no DDL
or hidden re-resolution. It rebuilds the accepted target from the exact three
facility/pool locations plus that event, debits only Orenburg and preserves both
other locations, and
requires the positive-order, blank-own-cost and exact six-missing-TOTAL incident
shape before recomputing only the target SKU and Proxy 3/4 TOTAL dependencies.
It publishes a new immutable good historical version and
same-date business projection without switching the current active/sync pointer
and without reading current pool rows or the terminal A zeros as candidate
operands. Candidate timestamps derive from the accepted event, so two JIT
witnesses are materially identical despite different wall-clock time. A positive finite
legacy long WAC is accepted only while its exact row/provenance digests remain
unchanged; the historical source text is never rewritten and the candidate uses
the canonical precision-38 ratio. Durable intent is created only under the
shared lock after under-lock CAS. The intent,
bounded retry, restart and query-only ambiguous readback contracts are shared.
No adapter is invoked by deploy, no timer or automatic apply exists, and the
typed evidence adds no UI or health policy.

### Stage 7C exact opening and FBS lifecycle

Migration 145 exposes the applied lifecycle through query-only planning and
order UI surfaces. For every active FBS facility the public read model shows
`physical`, active `reserved` and signed `available = physical - reserved`;
negative available is retained, while an active facility without physical
ledger evidence makes current FBS total unavailable instead of synthesizing
zero. Inactive facilities leave the current FBS
total without rewriting history. Official seller-warehouse stock is a
timestamped reconciliation/readback only and reaches a facility through exact
`sellerWarehouseId` mapping; multiple active target facilities are ambiguous
and therefore excluded rather than guessed. The new exact binding workflow
prevents creating that ambiguity while preserving older mappings as audit. It
is never a second physical operand.

The FBS order list/detail surface is server-paginated and filterable by date,
status, SKU and facility. It exposes safe order identity, status/lifecycle,
mapping, reservation, debit/close and transition digest evidence, but no PII,
raw payload, token or manual accounting/WB action.

Migration 142 supplies the trusted production writer that Stage 6 intentionally
lacked. It allocates the exact current aggregate `ff` rows into active
facility/pool detail with signed SQLite `INTEGER` quantities and exact Decimal
text capital/WAC, posts immutable opening evidence, then applies the pre-T FBS
checkpoint. Complete/sorted historical handoff orders debit physical stock once;
active orders reserve only; pre-handoff cancellation is a no-op. Available may
be negative while physical/capital remain exact. Post-T fulfillment uses the
same frozen opening WAC and idempotent order/event identity; later sold/closed
cannot debit again, and post-handoff cancellation/return is reconciliation
evidence rather than an implicit receipt.

The processor is hard-off without an applied manifest, approved complete/sorted
policy and writer epoch. It consumes only local immutable official observations
after the five-minute collector commits them and performs no WB mutation. An
explicit clean `excluded_pending_receipt` stays outside opening/backfill and can
later enter both aggregate and pools exactly once through guided acceptance.
Aggregate/detail parity is recomputed after every physical lifecycle or guided
document movement. After the owner-gated pool cutover, ordinary functional
publication takes current physical `ff` quantity/capital from the exact sum of
facility × `FBS|FBO` balances. The append-only legacy FF ledger remains the
historical and outbound-WAC evidence source, but can no longer resurrect an FBS
lifecycle debit in the public aggregate. Epoch, cutover manifest and every pool
row participate in the coherent local-source digest and are rechecked while the
functional apply holds its immediate write lock.

Migration 143 makes the opening checkpoint replayable without a moving owner
gate. The immutable manifest owns local observation boundary `T`, three
independent append-only watermarks and their complete frozen-row digests.
Status rows above those watermarks are processed in exact status-sequence order;
drain progress commits atomically with reservations/debits/reconciliation. A
retry cannot repeat a physical delta, and rows appended after the opening lock
are caught by the ordinary collector lifecycle pass. Runtime initialization
also recognizes only the exact four-value legacy order-classification CHECK and
atomically widens it to the canonical six values under an immediate SQLite
transaction. The rebuild copies named columns exactly, preserves the
opening-reservation FK definition and fails closed on unknown schema objects,
pre-existing target FK violations or copy drift; deployment itself still posts
no business rows and invalidates earlier owner gates through the changed
deployed SHA.

The ordinary collector's post-poll lifecycle pass uses the same shared
warehouse writer lock as functional publication and guided confirm. It attempts
that lock non-blocking: collection/audit rows may continue, while a busy confirm
returns a visible `held` drain with no checkpoint movement. The next timer pass
resumes from the unchanged `last_status_observation_sequence`; event identity
and atomic progress make the suffix exactly-once. Thus normal orders after a
validated preview neither make the receipt owner gate a moving target nor get
lost/double-counted around its short commit window.

An otherwise valid post-T status whose exact order revision still has
unmatched or drifted identity evidence cannot pin that global cursor. The same
rule covers `order_sku_unmapped`: an exact matched order for a known facility
whose nmId/chrtId is absent from the immutable cutover SKU manifest is not a
batch-fatal exception. The same transaction appends its exact status sequence to
`sheet_vitrina_v1_ff_pool_fbs_identity_pending`, advances only the source
cursor, applies no reservation/debit/capital delta for that order and continues
later matched statuses in sequence. Every later pass retries a bounded pending
prefix independently of the new suffix. Resolution is append-only and is
allowed only when immutable matched evidence proves the same order and exact
warehouse/nm/chrt tuple; the processor then replays the original pending status
and records one `...identity_pending_resolutions` row atomically with its normal
idempotent lifecycle event. No facility/SKU mapping is guessed or changed, an
unresolved row stays visible as `caught_up_identity_pending`, and an exact retry
cannot duplicate a physical delta or WB action.

The read-only FBS-orders contract reports the drain independently of collector
poll health: latest source status sequence/time, durable cursor sequence/time,
lag and unresolved identity-pending count. The generic pending envelope keeps
its existing append-only reason contract; the exact operator reason is derived
from immutable order/mapping/manifest evidence and remains
`sku_mapping_missing_or_ambiguous` for a missing known-facility SKU. Collector
success cannot make a lagging lifecycle cursor green.

### Bounded dual-lane backlog recovery

Migration 157 provides the versioned repo-owned runner
`apps/ff_pool_fbs_forward_recovery.py`. Deploy is inert and creates no business
boundary. The normal runtime initializer installs only empty
generation/recovery tables; before a generation exists, a missing-SKU row
retains the legacy fail-closed suffix rollback and cannot consume the backlog
ahead of its owner gate. After exact-SHA deploy the default query-only T0
dry-run opens the operational store as `mode=ro` with SQLite `query_only=ON`.
It does not clone the operational database. One explicit coherent read
transaction streams the target dependency graph into a private mode-`0600`
file-backed SQLite scratch with the exact production table/index/trigger
schema, foreign-key readback, lifecycle AUTOINCREMENT seed and canonical
warehouse-operation `rowid` seed. That makes consecutive same-SKU handoff
evidence identical to live apply without loading the multi-gigabyte store into
RAM. Row, payload, target-manifest, disk-capacity and scratch-size ceilings fail
closed; the scratch and sidecars are removed after preview. Unrelated
operational history is not materialized. T0 pins active storage
generation/schema, Stage 7C cutover, old lifecycle cursor, source
status maximum `C`, exact stable business identities in `(cursor,C]`, target
WAC/after-images, past fulfilled evidence, backup/recovery and one fingerprint.
Dynamic `generated_at`/refresh/poll timestamps and global maxima above `C` are
excluded; exact target revision/status/mapping/WAC drift remains blocking.
Target and past-fulfilled digests are canonical length-delimited streams and
therefore do not depend on fetch chunk size. Planner evidence exposes copied
table/row/payload/scratch counts and confirms `whole_database_backup=false`.

A separately owner-gated explicit apply atomically appends one immutable
generation at `C`, initializes the forward cursor at `C` and processes only the
pinned `<=C` rows through this same lifecycle implementation. Ordinary
processing then starts at `C+1`; the recovery call updates neither the old nor
forward cursor. Thus continuous ingress never joins the recovery manifest and
does not wait for historical identity quarantine. Missing mapping/SKU evidence
stays pending with no debit/capital/WAC/fallback, while later valid forward rows
continue. Target results and non-target changes inside the short writer
transaction are reconciled exactly; past fulfilled/frozen events stay
immutable. Exact Decimal scale is normalized, but facility/SKU/status,
quantity, WAC/capital, debit identity and invariant drift is never tolerated.
Heavy coherent preview revalidation runs outside the writer lock and
transaction. Any remaining canonical after-image mismatch fsyncs a private
privacy-safe field-level diff before rollback. The production-scale smoke uses
40,000 pinned statuses and a TEMP identity table rather than a platform-limited
SQL parameter list. After the single authorized apply, query-only `verify-noop` binds
the reviewed plan to completed durable readback and proves a repeat would write
nothing without issuing another submit. Ambiguous post-commit transport
requires query-only readback before any retry. See migration 157 for the exact
schema, gate and smoke contract.

Migration 150 adds one supported post-cutover mapping-extension path without
rewriting the immutable Stage 7C manifest/checkpoint. The canonical warehouse
mapping table remains the sole routing source and gains exact official office
evidence. One append-only extension envelope binds that mapping to the applied
cutover, exact facility, official warehouse/office identities, deployed SHA,
reviewed manifest, accepted transfer receipt and compound frozen `W`; its
per-SKU allocation rows freeze receipt-backed positive WAC. The lifecycle uses
the extension only when warehouse, office, mapping row, matched identity row
and allocation all agree exactly. Name/fuzzy routing and inferred SKU ownership
remain forbidden.

The extension runner is query-only by default. Its reviewed source contains
complete frozen-row digests for all three streams plus the exact target backlog
partition. Ordinary append-only rows above `W` do not stale the gate and enter
the normal exactly-once suffix. Apply holds the shared warehouse writer lock,
creates a central T2 domain checkpoint and private `0600` target before-image,
appends only reviewed mappings/evidence, and invokes the unchanged lifecycle
drain. `new|eligible` reserves, only `complete+sorted` debits physical once,
complete alone remains forbidden, late/terminal evidence stays audited, and
unproven identities stay pending. Readback proves the source receipt unchanged,
the original Moscow mapping unchanged, frozen backlog resolution, no duplicate
event/operation, live pool/aggregate parity, collector continuation and zero WB
writes.

Migration 151 makes `pool_overhead` in the existing facility/pool document
workflow the only operator path for new FF overhead. The operator must select
one active facility, `FBS|FBO|both` and one stable expense category; the server
derives the current business date from the selected facility timezone and does
not allow backdating. Manual entry requires a positive RUB amount. An optional
payment-order PDF is parsed by the already versioned Russian payment-order
parser, but never chooses facility, pool or category. Only executed,
posting-eligible RUB documents may reach ready; unsupported, damaged,
OCR-only, ambiguous, needs-review and non-executed evidence is retained as a
durable blocked request and creates no cost document.

The full positive physical facility/pool quantity is the denominator;
reservations stay excluded. Every positive physical SKU must also have a
positive known capital basis. One missing/zero cost basis blocks the whole
preview instead of redistributing over a subset. Preview freezes the exact
quantities, capitals, evidence, feature epoch and dedup state, and confirm
rebuilds the plan both before and inside the writer transaction. Posting keeps
quantity unchanged, adds the exact conserved kopecks to capital and recalculates
facility-local WAC. Storno retains the category, manual/PDF origin and payment
evidence link while reversing the exact original capital effect.

The posting transaction also publishes a successor immutable functional
version whose exact affected aggregate `ff` operands come from the posted pool
rows, without changing quantity, and atomically inserts
one `pool_overhead:<document_id>` targeted queue revision. HTTP ends at the
durable `posted/queued` readback; Warehouse, Proxy/economics and Finance
continue outside the interactive request. The queue persists separate
Warehouse, economics and Finance states, and Finance completion stores its
exact stale-plan CAS fingerprint. `Себестоимость опубликована` is possible
only after all three stages read back complete; durable error never asks the
operator to repeat the business document.

For a post-cutover FBS order the lifecycle debit freezes the positive current
WAC of its exact `facility_id + FBS + nmId` inside the serialized debit
transaction, publishes the successor aggregate version atomically and records
the source operation order/revision. A debit committed
before overhead retains the previous WAC; one committed after it receives the
new WAC. Fulfilled history is immutable. Missing mapping, balance, positive
capital or exact WAC remains unavailable with a reason instead of using an
opening/cutover value, another facility/SKU, an average or zero. Storno fails
closed if a later immutable debit already froze the overhead-derived WAC.

For PDF mode the canonical request stores the original authenticated source
file, normalized parser result, parser/fingerprint versions, file SHA-256 and
content payment fingerprint. The fingerprint has a unique append-only binding
to the canonical request, so renamed or regenerated equivalent PDFs and new
client request IDs read back the existing request/document rather than post a
second expense. Parsed amount is authoritative while the file is attached;
another amount requires removing the file and using manual mode. The old
aggregate FF overhead documents and reversals remain historical compatibility
evidence, but their UI creation form is retired and links to `Документы
фулфилмента → Накладные расходы FBS/FBO`; no second allocator or ledger is
introduced.

The five-minute collector consumes at most 10,000 new lifecycle observations
per warehouse-functional transaction (the domain primitive remains capped at
100,000), so a measured production suffix catches up in bounded passes without
either a 500-row moving tail or one unbounded lock hold.

Every post-T lifecycle capital product, pool fold and aggregate fold uses the
same 160-digit Decimal arithmetic boundary. Operational parity is centralized:
integer quantity is exact, while detail and aggregate capital must match per SKU
and in total after deterministic `ROUND_HALF_UP` normalization to kopecks. Raw
Decimal tails are retained in source fingerprints and parity diagnostics. An
equal-kopeck tail does not block; a kopeck mismatch, a total residual that
crosses the boundary or failed deterministic attribution remains fail-closed.

Canonical warehouse publication keeps the 160-digit boundary while adding the
exact pool aggregate to its stage bucket and while serializing the final
warehouse line. A later hourly/manual publication therefore preserves raw
audit quality; lifecycle safety itself does not depend on byte-equal
sub-kopeck tails once the centralized minor-unit and residual-conservation gate
passes.

The bounded five-document corrective runner uses that same parity boundary.
It records the full selected `175206.50 RUB` as document evidence separately
from the actual aggregate rewrite. An aggregate already equal to facility
detail at the canonical kopeck boundary has rewrite count/capital delta zero;
raw Decimal tails remain fingerprinted. Only missing deterministic
`pool_overhead:<document_id>` queue identities and incomplete Warehouse,
economics or Finance acknowledgements may then advance. Fully complete stages,
quantities, business documents and fulfilled lifecycle debits remain untouched,
and a new dry-run after reconciliation is a proven no-op.
