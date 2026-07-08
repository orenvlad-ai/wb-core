---
title: "Модуль: ff_stock_ledger_block"
doc_id: "WB-CORE-MODULE-43-FF-STOCK-LEDGER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для server-owned `ФФ -> Остатки ФФ`: количественный ledger, Excel preview/confirm ручных документов, автооприходование supplier shipments, автосписание WB supplies и расчётный источник `Остатки ФФ`."
scope: "Operator supply contour for current ФФ quantity balances only: runtime SQLite operation headers/lines/previews, original manual Excel storage, protected HTTP routes, operator UI registry/journal, idempotent supplier/WB auto movements, and factory-order/WB regional stock_ff source. FIFO, партии, себестоимость, бухгалтерский склад, 1C writes, WB mutations and Google Sheets/GAS are out of scope."
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
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks"
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks/export.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/ff-stocks/preview"
  - "POST /v1/sheet-vitrina-v1/supply/ff-stocks/confirm"
  - "GET /v1/sheet-vitrina-v1/supply/ff-stocks/operations/{operation_id}/file"
related_runners:
  - "apps/ff_stock_ledger_smoke.py"
  - "apps/ff_stock_ledger_http_smoke.py"
  - "apps/factory_order_supply_smoke.py"
  - "apps/wb_regional_supply_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "`Остатки ФФ` are now computed from an append-only quantity ledger. Manual Excel documents require preview then explicit confirm, auto supplier/WB movements are idempotent by source key, negative balances are allowed with warnings, and calculations can choose `stock_ff_source=ff_stock_ledger` without removing manual Excel or `1С / Фулфилмент` sources."
---

# 1. Contract

`Поставки` exposes top-level section `ФФ` with two inner subsections:
- `Услуги ФФ` for the existing fulfillment service upload/payment-validation contour.
- `Остатки ФФ` for current quantity balances by SKU.

`Остатки ФФ` is not an editable snapshot table. Current balance is computed from ledger lines:
- manual receipt documents add quantity;
- manual writeoff documents subtract quantity;
- supplier shipment acceptance on ФФ adds quantity;
- eligible WB supplies subtract quantity.

Negative balances are valid runtime state and must be shown as `Отрицательный остаток ФФ`; calculations must not fail only because the ledger balance is negative.

# 2. Operator UI

The `Остатки ФФ` subsection shows:
- current balance registry for active nomenclature SKU;
- operation journal;
- current balances XLSX export;
- manual `Оприходовать`;
- manual `Списать`.

The registry row fields are barcode when available, `nmId`, SKU/name/comment from active nomenclature, SKU group when available, current ФФ balance and negative-balance warning.

The operation journal shows operation datetime, operation type, source type, linked source object label/id, actor when available, SKU count, total quantity, warnings and source-file link for manual Excel documents. Auto operations link to their source object by label/id and do not have a file.

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

There is no cell-level balance editing. Corrections are represented by new reverse manual documents.

# 4. Runtime Persistence

Runtime SQLite tables:
- `sheet_vitrina_v1_ff_stock_operation_previews` stores pending manual preview payload, summary, warnings/errors and original Excel blob until confirm/cancel.
- `sheet_vitrina_v1_ff_stock_operations` stores durable operation header: operation id, operation type, source type, idempotency/source key, source object id/label, created time, actor, SKU/quantity totals, warnings/diagnostics and optional source file metadata/blob.
- `sheet_vitrina_v1_ff_stock_operation_lines` stores signed quantity deltas by `nmId`, plus barcode/SKU/group display fields and raw row/source metadata.

Balance is read as `SUM(quantity_delta)` grouped by `nmId` over durable lines. There is no separate balance snapshot source of truth.

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

# 7. Calculation Source

`Поставки -> Расчёты` supports three mutually exclusive `stock_ff_source` values:
- `manual_excel` — existing manual Excel `Остатки ФФ`;
- `onec_ff_stock` — existing read-only `1С / Фулфилмент`;
- `ff_stock_ledger` — new server ledger source labeled `Остатки ФФ`.

Factory-order and WB regional calculations resolve `ff_stock_ledger` into the same row contract as the other sources. Negative balances are passed through with warnings instead of being treated as missing or fatal.

For calculation-only `Учесть WB-поставки`, statuses `3/4/6` still add future inbound/projection evidence, but selected WB supplies do not reduce `stock_ff` again when `stock_ff_source=ff_stock_ledger`: the ledger balance is already current after WB auto writeoffs. Manual Excel and `1С / Фулфилмент` keep the older transfer behavior where selected WB supplies reduce available ФФ stock and add the same quantity to inbound/projection. Ledger auto writeoff remains broader than calculation overlay and still records statuses `3/4/5/6`, while statuses `1/2` and `Допринято` are skipped.

The existing manual Excel and `1С / Фулфилмент` sources remain valid.

# 8. Smokes

Targeted smoke:
- `python3 apps/ff_stock_ledger_smoke.py`
- `python3 apps/ff_stock_ledger_http_smoke.py`

The smoke covers manual receipt/writeoff preview-confirm-balance, Excel export/import roundtrip, negative-balance warning, supplier auto receipt idempotency, WB status writeoff idempotency, statuses `1/2` skip, `Допринято` skip, factory-order ledger source without duplicate selected-WB deduction, selected-WB inbound/projection for ledger source and WB regional ledger source.

# 9. Explicit Non-Scope

This module does not implement:
- FIFO or lot accounting;
- бухгалтерский склад;
- final товарная себестоимость;
- 1C stock writes;
- WB create/update/delete mutations;
- deletion of operations as a correction mechanism;
- Google Sheets/GAS;
- legacy `wb_core_docs_master` or old project pack updates.
