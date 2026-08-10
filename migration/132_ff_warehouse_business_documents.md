# Migration 132: единый пилот документов склада FF

## Цель

Пилот завершает business-document contour страницы `Остатки → Склады и себестоимость → Склад FF` без второго баланса или ledger. Canonical physical/reservation ledgers и versioned functional warehouse state остаются источниками истины; новый реестр является только lazy read projection.

## Инвентаризация

UI download строит полный XLSX на business date по каждой active/non-hidden stable `nmId`. Upload требует ровно одну строку на весь этот catalog, включая явные нули. Duplicate, unknown, missing/ambiguous identity, negative target, future/missing/non-positive cost basis и stale fingerprint блокируют confirm.

Stored preview и parent `Инвентаризация склада FF` сохраняют original bytes/SHA, actor/date, exact target/readback, manifest/fingerprint и child operation identities. Parent has zero movement. Positive/negative differences become `Оприходование излишков` / `Списание недостач` with frozen same-SKU cost hierarchy already owned by `ff_inventory_reconciliation`. Exact repeat is T0; rollback appends exact inverse-cost documents and preserves all source/audit rows.

## Накладные расходы

`sheet_vitrina_v1_ff_overhead_documents` stores one immutable positive RUB amount/reason/date/actor, exact physical-source revision, denominator and per-SKU Decimal allocations. Only positive physical FF quantity on the selected date participates. Reservation, `FF → WB`, zero/negative quantities and later arrivals are excluded.

Allocation rounds down to kopecks and assigns the deterministic remainder by largest fractional part then `nmId`; allocations exactly conserve the header amount. Ledger child lines have zero quantity and frozen `cost_adjustment`. Targeted functional replay validates the frozen quantity basis, changes only capital/WAC and republishes affected economics. Exact reversal appends the negative original allocations. Supply-specific Fulfillment services/storage/transit/paid acceptance are never duplicated.

## Единый read model

`packages/application/ff_warehouse_documents.py` projects canonical FF operations, inventory parent, reservations, legacy opening and functional technical records into one stable business view. It localizes receipt, WB shipment, inventory/children, return, manual documents, overhead, reservation lifecycle, opening and storno. Technical cutover/sync/repair/archive is hidden by default and version-qualified when explicitly included; repeated functional sync cannot clone one canonical business movement.

Canonical receipt/shipment/return rows also expose a warehouse-neutral `warehouse-transfer:{source_type}:{source_object_id}` identity and explicit source-object fields. A later read-only projection on another warehouse can therefore reuse the same transfer identity without creating another movement; this pilot does not roll that UI out beyond FF.

Server applies `effect`, `reason`, inclusive business-date range, bounded number/source/supply/invoice/nmId/SKU/barcode search and `include_technical` before pagination. Lines/source XLSX load only on detail. Header exposes total quantity/capital/expense, never a synthetic multi-SKU unit cost.

Legacy `Поставки → ФФ → Операции остатков ФФ` remains compatible and continues reading the same ledger, but is not the primary business registry.

## Safety and rollout

All endpoints retain the supply-operator auth boundary. Page open, template download, registry/filter/detail reads are mutation-free. Preview and confirm are separate routes. Inventory/overhead apply and storno enqueue one exact targeted warehouse replay; active functional runtime attempts the bounded functional/economics publication immediately, while an unavailable cutover leaves a truthful durable queued state. No production business-data fixture is required for deployment verification.

## Проверки

- `python3 apps/ff_inventory_reconciliation_smoke.py`;
- `python3 apps/ff_overhead_allocation_smoke.py`;
- `python3 apps/ff_warehouse_documents_smoke.py`;
- `python3 apps/ff_stock_ledger_http_smoke.py`;
- `python3 apps/warehouse_functional_smoke.py`;
- `python3 apps/warehouse_stocks_browser_smoke.py`.
