---
title: "Модуль: supplier_shipments_block"
doc_id: "WB-CORE-MODULE-34-SUPPLIER-SHIPMENTS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для блока `Поставки -> От поставщика`: supplier invoice registry, XLSX parser, runtime storage, protected API and supplier-only UI."
scope: "Server-owned invoice registry for supplier shipments under current WebCore runtime: XLSX parse with openpyxl, deterministic type/model alias matching, editable shipment card, filesystem-backed original invoice storage under runtime dir, SQLite metadata/lines, operator embedded UI and supplier-only role boundary."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
related_modules:
  - "packages/contracts/supplier_shipments.py"
  - "packages/application/supplier_invoice_parser.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_supplier.html"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
related_tables:
  - "sheet_vitrina_v1_supplier_shipment_uploads"
  - "sheet_vitrina_v1_supplier_shipments"
  - "sheet_vitrina_v1_supplier_shipment_lines"
related_endpoints:
  - "GET /sheet-vitrina-v1/supplier"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/parse"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/invoice"
related_runners:
  - "apps/supplier_invoice_parser_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/registry_upload_http_entrypoint_supplier_auth_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py"
related_docs:
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Initial supplier shipment registry checkpoint: no Google Sheets/GAS contour, no fake SKU matching, no browser-local truth."
---

# 1. Contract

- Operator surface: in `Поставки`, inner tab `Расчёты` keeps the existing factory/WB supply calculators; inner tab `От поставщика` embeds the supplier shipment registry.
- Supplier-only surface: `GET /sheet-vitrina-v1/supplier` renders only the supplier shipment registry without full operator navigation.
- Runtime truth is server-owned:
  - original XLSX files live under `<runtime_dir>/supplier_invoices/files/<shipment_id>/<safe_filename>`;
  - staged uploads live under `<runtime_dir>/supplier_invoices/uploads/<upload_id>/<safe_filename>`;
  - SQLite stores upload metadata, shipment headers and editable line details.
- `shipment_date` is required on create/save and is validated server-side even though the UI disables save until it is present.

# 2. Parser

- Parser uses `openpyxl` and searches for table headers `NO.`, `MODELS`, `QTY`, `U.PRICE`, `AMOUNT`.
- Merged-cell/fill-down blocks are handled for product type markers:
  - `高清膜` / `smk` -> `clear`
  - `防窥膜` / `(Anti-Spy)` -> `anti_spy`
  - `磨砂膜` / `(Matte)` -> `matte`
- Compatible model aliases such as `iPhone 15 / 16` stay as one normalized alias (`iphone_15_16`); parser does not split them into separate SKU rows.
- Extras such as OPP packets, labels and cards are stored as `line_type=extra`, not product SKU rows.
- Unknown aliases remain persisted and visible with `match_status=unmatched`; low-confidence fuzzy matching is not performed.

# 3. Matching

- Seed alias config is `artifacts/supplier_shipments/factory_invoice_aliases.json`.
- Active alias rows are deterministic only: `factory_type + normalized_model -> match_key -> internal sku/nmId/name/group`.
- Empty or inactive alias config is valid; the UI/API must still preserve unmatched rows for manual operator correction.

# 4. Auth Boundary

- Operator role can access the full WebCore shell and supplier shipment APIs.
- Supplier role is optional and configured only through runtime env:
  - `WB_CORE_SUPPLIER_AUTH_USERNAME`
  - `WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH`
  - `WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME`
- Supplier role can access only `/sheet-vitrina-v1/supplier`, supplier shipment APIs, invoice downloads, login/logout and needed static/browser assets. It cannot access `/sheet-vitrina-v1/vitrina`, `/sheet-vitrina-v1/operator` or unrelated `/v1/sheet-vitrina-v1/...` APIs.

