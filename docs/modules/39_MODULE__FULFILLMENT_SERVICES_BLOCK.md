---
title: "Модуль: fulfillment_services_block"
doc_id: "WB-CORE-MODULE-39-FULFILLMENT-SERVICES-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для `ФФ -> Услуги ФФ`: XLSX template/download/upload, server-owned runtime validation, PDF-виза на оплату, accepted-only documents table, delete flow and overlay расходов ФФ в `Поставки -> Wildberries`."
scope: "Operator supply contour for Fulfillment service expenses only: PNG-derived XLSX template, protected HTTP routes, openpyxl parser, STORAGE row handling with storage allocation, SQLite upload/line persistence with soft-delete, PDF payment-validation artifact, accepted-only UI list and approved-only WB supplies overlay. Final product cost, 1C cost truth, ЕБД metric truth and global cost-source switching are out of scope."
source_basis:
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "Local PNG visual source: ~/Downloads/IMG_20260706_011652.png"
related_modules:
  - "packages/application/fulfillment_services.py"
  - "packages/application/wb_supplies.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
related_tables:
  - "sheet_vitrina_v1_fulfillment_service_uploads"
  - "sheet_vitrina_v1_fulfillment_service_lines"
  - "sheet_vitrina_v1_wb_supplies"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/template.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}"
  - "DELETE /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}/payment-validation.pdf"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies"
related_runners:
  - "apps/sheet_vitrina_v1_fulfillment_services_smoke.py"
  - "apps/sheet_vitrina_v1_fulfillment_services_browser_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py"
related_docs:
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Fulfillment uploads are server-owned runtime truth for uploaded service-expense files and payment validation only. They are not official WB evidence, not 1C cost truth, not ЕБД metric truth and not final товарная себестоимость. The operator UI top-level supply section is `ФФ`; this module owns the inner subsection `Услуги ФФ`, while `43_MODULE__FF_STOCK_LEDGER_BLOCK.md` owns inner subsection `Остатки ФФ`. `Услуги ФФ` shows only accepted uploads in `Загруженные документы`, keeps failed uploads out of the accepted list/overlay, supports `STORAGE` rows in `Номер поставки` for storage allocation across ordinary matched rows, and soft-deletes accepted uploads so their PDF and WB supplies overlay amounts become unavailable."
---

# 1. Contract

`Поставки` exposes top-level section `ФФ` with inner subsections `Услуги ФФ` and `Остатки ФФ`.

This module owns only `ФФ -> Услуги ФФ`. The `Остатки ФФ` quantity ledger/current balances contour is documented in `43_MODULE__FF_STOCK_LEDGER_BLOCK.md`.

The section is server-owned/runtime-backed and contains:
- `Скачать шаблон`;
- `Загрузить заполненный файл`;
- one accepted-documents block `Загруженные документы`;
- an accepted-only table with `Дата загрузки`, source filename, row counts, totals, PDF link and delete action;
- compact row-level validation errors only after a failed upload;
- PDF-виза link only for fully valid, non-deleted uploads.

Browser `localStorage`, Google Sheets/GAS and legacy project packs are not source of truth for this contour.

Accepted, non-deleted Fulfillment uploads are also read by the management proxy WB cost model as explicit `ff_services_per_unit_rub` and allocated `ff_storage_per_unit_rub` components. Failed, deleted, unmatched and duplicate uploads remain excluded; missing upload evidence is surfaced as component status, not as silent confirmed zero.

# 2. Template

The XLSX template is generated server-side from the local visual source `~/Downloads/IMG_20260706_011652.png`.

Headers:
1. `Номер поставки`;
2. `Склад`;
3. `Кол-во коробов`;
4. `Цена`;
5. `Кол-во паллет`;
6. `Цена`;
7. blank subtotal/amount column as present in the visual form;
8. `Выезд`;
9. `Итого`;
10. `НДС 5%`.

The first column is the only added system column. Duplicate headers such as `Цена` and the blank subtotal column are preserved because the template is intended to stay familiar for managers/Fulfillment.

The parser remains backward-compatible with the old second-column header `Стоимость услуг`, but newly downloaded templates must use `Склад`. Operators use `Склад` for warehouse/route/description text; storage rows usually use `Хранение`.

Route:
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/template.xlsx`

The route is protected by the same supply-operator boundary as operator supply routes and does not write production DB data.

# 3. Upload Parser And Validation

Route:
- `POST /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads`

Parser rules:
- use `openpyxl`;
- read the first sheet;
- detect the header row by `Номер поставки`, `Итого` and `НДС 5%`;
- save the original XLSX under runtime storage;
- persist SHA256 of the original file;
- parse every ordinary detail row as exactly one WB supply;
- recognize `STORAGE` in `Номер поставки` case-insensitively as a service storage row, not as a WB supply;
- skip fully empty rows and footer/total rows without `Номер поставки`;
- parse numeric values with spaces, NBSP, comma decimal separator, ruble suffixes and empty optional cells;
- preserve raw row JSON and useful line fields even when MVP overlay uses only totals.

Validation OK requires:
- at least one ordinary WB supply detail row;
- every ordinary detail row has non-empty `Номер поставки`;
- every ordinary supply number matches an existing cached row in `Поставки -> Wildberries`;
- no duplicate supply id inside the upload;
- `Итого` is numeric and `>= 0`;
- `НДС 5%` is numeric and `>= 0`;
- `amount_with_vat = Итого + НДС 5%`;
- all ordinary lines have `match_status=ok`.

`STORAGE` rows:
- do not require WB match and do not appear as separate WB supplies;
- require numeric non-negative `Итого` and `НДС 5%`;
- may appear once or multiple times; their `Итого + НДС 5%` is summed;
- cannot be the only accepted content in an upload.

When storage exists, `storage_total_with_vat` is allocated across ordinary matched rows in the same upload. Allocation uses `boxes_qty` first, then matched WB supply quantity. If neither source is available for an ordinary row, the upload fails with a controlled validation error. Rounding is adjusted so allocated storage totals equal the source storage total.

On any failure:
- upload status is `failed`;
- row-level errors are returned and persisted;
- PDF is not generated;
- failed lines do not enter the WB supplies overlay.

# 4. Runtime Persistence

Tables:
- `sheet_vitrina_v1_fulfillment_service_uploads`;
- `sheet_vitrina_v1_fulfillment_service_lines`.

Upload rows store:
- `upload_id`;
- source filename, stored file path and SHA256;
- uploaded/created/updated timestamps;
- validation status and summary;
- total/matched row counts;
- totals for `Итого`, `НДС 5%`, `К оплате`;
- stable `payment_validation_id`;
- generated PDF path.

Line rows store:
- upload id and source row index;
- input supply id and matched WB cache identity;
- match status;
- service/route text;
- boxes/pallets/departure fields;
- `amount_without_vat`, `vat_amount`, `amount_with_vat`;
- `is_storage_line`;
- storage source totals for STORAGE rows;
- allocated storage amount/per-unit for ordinary rows;
- service amount with and without allocated storage;
- raw row JSON;
- row error and warnings.

Original XLSX files and generated PDFs live under the current runtime directory contract, not in Git and not in Google Sheets.

# 5. PDF Payment Validation

For fully valid uploads the backend generates one protected PDF:

`Виза на оплату услуг ФФ`

The PDF includes:
- title and status `Проверено системой / OK`;
- `generated_at`;
- `upload_id`;
- stable `payment_validation_id` in the `FF-XXXXXXXX` format;
- source filename and short hash;
- rows total/matched;
- totals for `Итого`, `НДС 5%`, `К оплате = Итого + НДС 5%`;
- storage summary line `Хранение`;
- matched supplies table with ordinary lines and allocated storage;
- note that the PDF is valid only for the uploaded file with the shown upload id and hash.

Route:
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}/payment-validation.pdf`

Downloading an already generated PDF must not recreate `payment_validation_id`.

# 6. List And Detail API

Routes:
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads`;
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}`.

List returns non-deleted accepted uploads only (`validation_status=ok`) with totals, row counts, PDF availability and timestamps. Failed uploads may remain in DB for diagnostics, but they are not accepted documents and are not returned to the operator accepted-documents table.

Detail returns upload metadata, parsed lines, row errors, validation status, totals and PDF link/status for non-deleted uploads.

Delete route:
- `DELETE /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}`.

Delete is protected by the same supply-operator auth boundary. The current implementation soft-deletes the upload by setting `deleted_at`, `deleted_by` and `delete_reason`, clears/removes the generated PDF path, and keeps source XLSX/line evidence in runtime storage for diagnostics. Deleted uploads are excluded from accepted list/detail/PDF download and from WB supplies overlay. The delete action does not delete WB supplies cache, official WB evidence, Seller Portal transit enrichment, transit costs, 1C/ЕБД data or final cost data.

# 7. WB Supplies Overlay

`Поставки -> Wildberries` remains read-only WB source evidence. Fulfillment upload data is a server-owned operator overlay, not official WB evidence.

Table changes:
- old column `Стоимость` is renamed to `Транзит`;
- new column `Услуги ФФ` is added.

`Транзит` shows the current transit/effective cost amount plus `₽/шт`. It does not place source labels such as `Seller Portal` under the amount; those labels stay backend provenance fields.

`Услуги ФФ` shows approved service amount with allocated storage included plus `₽/шт`; supplies without approved matched active Fulfillment lines show `—`. When storage allocation exists, the cell also shows `в т.ч. хранение: X ₽/шт`.

Denominator priority for per-unit display:
1. accepted quantity / accepted goods total;
2. `quantity_for_size_filter` / known supply quantity;
3. planned/added quantity with preliminary marker;
4. missing or zero denominator -> `₽/шт —`.

Only active uploads with `validation_status=ok`, `deleted_at IS NULL` and ordinary matched lines enter the overlay. STORAGE rows enter only through their allocated amount on ordinary lines. Failed, unmatched, duplicate and deleted uploads are excluded.

# 8. Smokes

Targeted smokes:
- `python3 apps/sheet_vitrina_v1_fulfillment_services_smoke.py`;
- `python3 apps/sheet_vitrina_v1_fulfillment_services_browser_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_supplies_http_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`.

The browser smoke uses an isolated temporary runtime SQLite DB, seeds bounded WB supplies cache rows, downloads the template through UI, uploads a real local `~/Downloads/fulfillment_services_filled_*.xlsx` workbook containing `STORAGE`, verifies PDF text, verifies storage allocation in the WB supplies overlay, and verifies delete/failed flows.

Current smoke expectations also cover:
- accepted uploads appear in `Загруженные документы`;
- failed uploads do not appear in the accepted table;
- delete requires UI confirmation;
- cancel keeps the accepted document;
- confirm soft-deletes the accepted document, makes its PDF unavailable and removes its Fulfillment/storage amounts/per-unit values from `Поставки -> Wildberries`.

# 9. Explicit Non-Scope

This module does not implement:
- final товарная себестоимость;
- 1C cost truth changes;
- ЕБД metric truth changes;
- global cost truth switch;
- WB official evidence mutation;
- Seller Portal transit source boundary changes;
- Google Sheets/GAS.

# 9. Paid-capital consumer boundary

Approved Fulfillment uploads remain service/payment-validation evidence, not capital merely because a file was uploaded or approved. Module 45 may include transit, FF service and storage components only after factual payment evidence with an effective payment date is recognized, and only once for the applicable WB movement layer. Missing payment/completeness evidence produces an estimated/unconfirmed bucket and server-derived yellow reason rather than confirmed zero or a future expense backdated into history.
## Canonical cost consumer boundary (2026-07-01)

Accepted service/storage documents are financial evidence for `packages/application/canonical_cost_engine.py`. Recognized amount uses the factual document/effective date and supply/SKU allocation provenance; paid projection remains zero until factual payment evidence exists. Quote, `К оплате`, upload time or unaccepted document cannot create paid capital. The module never owns physical FF/WB quantity and cannot replace the exact FF debit snapshot.
