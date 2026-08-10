# Migration 132: единый пилот документов склада FF

## Цель

Пилот завершает business-document contour страницы `Остатки → Склады и себестоимость → Склад FF` без второго баланса или ledger. Canonical physical/reservation ledgers и versioned functional warehouse state остаются источниками истины; новый реестр является только lazy read projection.

## Инвентаризация

UI download строит полный XLSX на business date по каждой active/non-hidden stable `nmId` и записывает canonical primary `Штрихкод` как text, не теряя leading zero/long identifiers. Новый upload profile принимает identity по unique `nmId`, только по exact primary/additional barcode из `barcode + barcodes_json` или по согласованной паре; прежний exact четырёхколоночный `nmId` workbook остаётся допустимым. Upload требует ровно одну resolved SKU-строку на весь catalog, включая явные нули. Duplicate после resolution, empty/unknown/ambiguous/conflicting identity, numeric/formula/scientific/fractional barcode representation, negative target, future/missing/non-positive cost basis и stale fingerprint блокируют confirm.

Stored preview и parent `Инвентаризация склада FF` сохраняют original bytes/SHA, actor/date, exact target/readback, manifest/fingerprint и child operation identities. `request_id` создаётся до upload; accepted/processing/blocked/ready status остаётся в operational SQLite и возобновляется после disconnect/reload/server restart. Быстрый POST фиксирует bytes/identity, а тяжёлый plan выполняется вне interactive HTTP. Exact повтор, включая новый request id для того же source/date identity, возвращает тот же preview T0. Parent has zero movement. Positive/negative differences become `Оприходование излишков` / `Списание недостач` with frozen same-SKU cost hierarchy already owned by `ff_inventory_reconciliation`. Confirm atomically appends reconciliation/children and the one canonical targeted queue row. Rollback appends exact inverse-cost documents and preserves all source/audit rows.

XLSX row blockers keep structured server `code/details`. Одинаковый
`business_date_mismatch` возвращается как одна русская сводка с обеими датами,
диапазонами и количеством строк; остальные ошибки expose bounded localized
examples. Preview/blocked state never enables confirm and never clears the
selected file/form values in the current tab.

## Накладные расходы

`sheet_vitrina_v1_ff_overhead_documents` stores one immutable positive RUB amount/reason/date/actor, exact physical-source revision, denominator and per-SKU Decimal allocations. Only positive physical FF quantity on the selected date participates. Reservation, `FF → WB`, zero/negative quantities and later arrivals are excluded.

Allocation rounds down to kopecks and assigns the deterministic remainder by largest fractional part then `nmId`; allocations exactly conserve the header amount. Ledger child lines have zero quantity and frozen `cost_adjustment`. Targeted functional replay validates the frozen quantity basis, changes only capital/WAC and republishes affected economics. Exact reversal appends the negative original allocations. Supply-specific Fulfillment services/storage/transit/paid acceptance are never duplicated.

Overhead preview uses the same durable request/job/status contour. Confirm
returns immediately after the immutable document and its existing atomic queue
write read back; functional/economics publication is owned by the normal
warehouse worker. A response lost after commit is recovered by preview/request
or document id, and an exact retry creates neither a second header nor a second
queue item.

## Единый read model

`packages/application/ff_warehouse_documents.py` projects canonical FF operations, inventory parent, reservations, legacy opening and functional technical records into one stable business view. It localizes receipt, WB shipment, inventory/children, return, manual documents, overhead, reservation lifecycle, opening and storno. Technical cutover/sync/repair/archive is hidden by default and version-qualified when explicitly included; repeated functional sync cannot clone one canonical business movement.

Canonical receipt/shipment/return rows also expose a warehouse-neutral `warehouse-transfer:{source_type}:{source_object_id}` identity and explicit source-object fields. A later read-only projection on another warehouse can therefore reuse the same transfer identity without creating another movement; this pilot does not roll that UI out beyond FF.

Server applies `effect`, `reason`, inclusive business-date range, bounded number/source/supply/invoice/nmId/SKU/barcode search and `include_technical` before pagination. Lines/source XLSX load only on detail. Header exposes total quantity/capital/expense, never a synthetic multi-SKU unit cost.

Legacy `Поставки → ФФ → Операции остатков ФФ` remains compatible and continues reading the same ledger, but is not the primary business registry.

## Safety and rollout

All endpoints retain the supply-operator auth boundary. Page open, template download, registry/filter/detail/status reads are mutation-free. Preview and confirm are separate routes. `ff_document_workflow_v1` exposes five durable stages from server acceptance through document and economics completion. UI yellow/partial/red/final-green semantics are derived only from that readback; local storage contains identities, not business truth. Inventory/overhead apply and storno enqueue one exact targeted warehouse replay. Primary confirms do not run heavy replay in HTTP; the canonical hourly/manual worker records functional queue and separate economics completion on the same row. No production business-data fixture is required for deployment verification.

Functional economics full plan/digest revalidation runs outside a SQLite
transaction under a `PRAGMA data_version` mutation guard. Only guarded target
updates/readback/undo manifest run inside bounded `BEGIN IMMEDIATE`; concurrent
background or interactive writes cause fast stale-plan retry instead of a
multi-minute writer/read-lock starvation window. Atomic publication and
last-good active version remain unchanged.

## Проверки

- `python3 apps/ff_inventory_reconciliation_smoke.py`;
- `python3 apps/ff_overhead_allocation_smoke.py`;
- `python3 apps/ff_warehouse_documents_smoke.py`;
- `python3 apps/ff_stock_ledger_http_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_auth_smoke.py`;
- `python3 apps/warehouse_functional_smoke.py`;
- `python3 apps/warehouse_stocks_browser_smoke.py`.
