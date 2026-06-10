---
title: "Модуль: supplier_shipments_block"
doc_id: "WB-CORE-MODULE-34-SUPPLIER-SHIPMENTS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для блока `Поставки -> От поставщика`: Реестр заказов, supplier invoice parser, server-side nomenclature matching, persisted invoice price conformity, runtime storage, protected API and supplier-only UI."
scope: "Server-owned invoice order registry under current WebCore runtime: XLSX parse with openpyxl, deterministic type/model matching through server-side nomenclature, persisted per-line invoice price conformity against current purchase_price_yuan snapshots, editable shipment card, filesystem-backed original invoice storage under runtime dir, SQLite metadata/lines/nomenclature, operator embedded UI, settings surface and supplier-only role boundary."
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
  - "sheet_vitrina_v1_nomenclature_items"
related_endpoints:
  - "GET /sheet-vitrina-v1/supplier"
  - "GET /sheet-vitrina-v1/settings"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/parse"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "DELETE /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/rematch"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/price-check"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/invoice"
  - "GET /v1/sheet-vitrina-v1/settings/nomenclature"
  - "POST /v1/sheet-vitrina-v1/settings/nomenclature"
  - "GET /v1/sheet-vitrina-v1/settings/nomenclature/export.xlsx"
  - "POST /v1/sheet-vitrina-v1/settings/nomenclature/import.xlsx"
  - "PATCH /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}"
  - "DELETE /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}"
related_runners:
  - "apps/supplier_invoice_parser_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_price_conformity_backfill.py"
  - "apps/registry_upload_http_entrypoint_auth_smoke.py"
  - "apps/registry_upload_http_entrypoint_supplier_auth_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py"
related_docs:
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Supplier-facing order registry uses trilingual Chinese/English/Russian labels, shows fixed supplier in the registry only, hides Supplier/Customer and order SKU fields from the card, defaults supplier metadata to HanShang Technology, persists operator-owned order_status on shipment headers, reads contract no/date from cells or drawing XML text, keeps nmId/nomenclature visible, preserves deterministic nomenclature matching, and persists per-line invoice price conformity snapshots/statuses against current nomenclature purchase_price_yuan. Manual price recheck remains operator-only and UI-scoped to the operator embedded `Поставки -> От поставщика` frame: standalone `/sheet-vitrina-v1/supplier` and supplier/factory UI show saved statuses but do not render the recheck button. Operator nomenclature settings hide legacy SKU/alias/comment fields by default, add nullable purchase_price_yuan, and support operator-only XLSX export/import with dry-run validation; no Google Sheets/GAS contour, no browser-local truth, no low-confidence fuzzy matching."
---

# 1. Contract

- Operator surface: in `Поставки`, inner tab `Расчёты` keeps the existing factory/WB supply calculators; inner tab `От поставщика` embeds the supplier registry without an extra operator card/title wrapper.
- Supplier-only surface: `GET /sheet-vitrina-v1/supplier` renders only the supplier shipment registry without full operator navigation and without the manual price recheck button, even when opened from an operator session.
- Supplier-facing labels in the order registry/card use `中文 / English / Русский` business wording. The main title is `订单登记表 / Order registry / Реестр заказов`; duplicated registry headings and the old subtitle are not rendered. In framed operator view the title row actions are `新增订单 / Add order / Добавить заказ`, `退出 / Logout / Выйти`, then `Открыть отдельно`; the standalone-open link points to the existing `GET /sheet-vitrina-v1/supplier` UI route and does not introduce a new API route.
- Registry list UI separates `loading`, `loaded_empty`, `loaded_with_rows` and `error`: the initial DOM and in-flight list fetch show a compact loading state, while `暂无订单 / No orders yet / Заказов пока нет.` is rendered only after the list API has returned an empty shipment list.
- Supplier registry table headers remain sticky and use a subtly stronger dark/violet surface plus visible lower border so header cells do not blend into order rows; horizontal scroll and row action semantics are unchanged.
- Visible order card/form UI does not render editable `Supplier` or `Customer`. Supplier metadata is fixed server-side to `HanShang Technology`; the registry shows this fixed value in `供应商 / Supplier / Поставщик` with fallback for legacy rows. Customer is kept backward-compatible in stored/API payloads but is not required by create/save and is not shown.
- Visible order product rows do not render `our_sku`/`SKU` fields. Matching still may keep legacy `internal_sku` in storage for backward compatibility, but the supplier/order UI shows only `平台ID / nmId / nmId` and `我方品名 / Our item name / Номенклатура`.
- Registry matching column is `匹配 / Matching / Матчинг`; compact registry values are `OK` when every product line is matched and `Check` otherwise.
- Operator settings surface: `GET /sheet-vitrina-v1/settings` is a service page reachable from the top-right `Настройки` button, not a top-level WebCore tab.
- Runtime truth is server-owned:
  - original XLSX files live under `<runtime_dir>/supplier_invoices/files/<shipment_id>/<safe_filename>`;
  - staged uploads live under `<runtime_dir>/supplier_invoices/uploads/<upload_id>/<safe_filename>`;
  - SQLite stores upload metadata, shipment headers, editable line details, persisted price conformity fields and nomenclature rows.
- `shipment_date` is the only required manual field after parse. It is rendered as `出货日期 / Shipment date / Дата отгрузки`, is required on create/save, and is validated server-side even though the UI disables save until it is present.
- Shipment headers persist `order_status` in `sheet_vitrina_v1_supplier_shipments` with non-destructive default `production`. Canonical values are `production` (`На производстве`), `in_transit` (`В пути`) and `accepted_ff` (`Принято на ФФ`). List and detail API responses expose `order_status`; legacy rows without the column/value fall back to `production`.
- In the operator embedded registry table, `Currency / Валюта` is hidden from the list, `Статус заказа` is shown after `Invoice file` and before `Actions`, and status changes use a narrow status-only PATCH so shipment lines, invoice metadata, source file pointers and matching state are not rebuilt or erased. The status selector is not rendered in the standalone supplier-only view.
- Orders can be deleted by operator role only. Delete controls use UI-level confirmation: the first click opens an inline confirmation with cancel, and backend DELETE is called only by the explicit confirm action. While confirmation is open, row clicks do not open the order card. Confirmed delete removes the DB order/lines and makes the original invoice download unavailable.
- Saved order cards expose `关闭 / Close / Закрыть`; the visible `重新匹配 / Re-match / Пересопоставить` action is not rendered in the supplier/order card. The rematch API remains available for internal compatibility and applies current nomenclature without overwriting manual overrides unless explicitly requested by API payload.

# 2. Parser

- Parser uses `openpyxl` and searches for flexible invoice table headers including `NO.`, `MODELS / （型号）`, `NAME & SPECIFICATION / （品名规格）`, `QTY (PCS) / （数量）`, `U.PRICE / （单价）` and `AMOUNT / （总价）`.
- `RMB`/`CNY`/`¥` invoice currency is normalized to `RMB`; declared invoice totals may be read from post-table `Total`/`总值` rows when the value is not available in pre-table metadata.
- Contract metadata is extracted from ordinary cells, merged-cell values and bounded workbook drawing XML text. Supported labels include `Contract No`, `Contract No.`, `Contract Number`, `Contract Date`, `Date of Contract`, `合同号`, `合同编号`, `合同日期`, `下单日期`; date formats such as `2026.5.13`, `2026-05-13`, `13.05.2026` and `5/13/2026` normalize to `YYYY-MM-DD`.
- Merged-cell/fill-down blocks are handled for product type markers:
  - `高清膜` / `smk` -> `clear`
  - `防窥膜` / `(Anti-Spy)` -> `anti_spy`
  - `磨砂膜` / `(Matte)` -> `matte`
- Compatible model aliases such as `iPhone 15 / 16` stay as one invoice row and one legacy normalized `match_key`; parser does not split quantity into separate SKU rows.
- Extras such as OPP packets, labels and cards are stored as `line_type=extra`, not product SKU rows. Product-row comments may mention OPP/labels/cards as packaging instructions without turning the product line into an extra.
- Unknown aliases remain persisted and visible with `match_status=unmatched`; low-confidence fuzzy matching is not performed.

# 3. Nomenclature And Matching

- Server-side nomenclature lives in `sheet_vitrina_v1_nomenclature_items` and is edited through `Настройки -> Справочник номенклатуры`.
- Nomenclature rows include `item_id`, `is_active`, `our_sku`, `nm_id`, `nomenclature_name`, `product_type`, `match_key`, nullable `purchase_price_yuan`, `aliases`, `compatible_models_text`, normalized `compatible_model_keys`, `comment`, `created_at`, `updated_at`.
- Default operator settings UI shows only `Вкл.`, `nmId`, `Номенклатура`, `Тип`, `Match key`, `Цена закупки, ¥`, `Совместимые модели`, `Обновлено` and compact row actions. Legacy backend fields `our_sku`, `aliases` and `comment` remain stored/API-compatible but are not shown in the default table/export.
- `product_type` canonical values remain `clear`, `anti_spy`, `matte`, `extra`, `other`; the settings UI displays Russian labels and sends canonical values in JSON payloads.
- `purchase_price_yuan` is an optional fixed factory purchase price in CNY for the nomenclature dictionary. It accepts blank/null or a numeric value `>= 0` with decimal dot/comma normalization; it is used only by supplier shipment price conformity checks and is not used by shipment totals, matching, factory-order calculations, reports or web-vitrina metrics.
- `GET /v1/sheet-vitrina-v1/settings/nomenclature/export.xlsx` returns the current dictionary as XLSX with Russian headers and without default legacy `Наш SKU`/`Aliases`/`Комментарий` columns. `POST /v1/sheet-vitrina-v1/settings/nomenclature/import.xlsx` accepts `.xlsx`, supports `?dry_run=1`, returns row-level validation errors/counts, rejects invalid rows atomically with no partial mutation, preserves hidden legacy fields when columns are absent, and keeps DELETE/import disable semantics as soft-disable (`is_active = 0`).
- If the nomenclature table is empty and current `registry config_v2` is available, active SKU rows seed deterministic entries from `display_name`/`group`/`nm_id`; no SKU is invented beyond current config truth.
- Active duplicate `match_key` values are rejected. Active product rows require non-empty `match_key` and `nomenclature_name`.
- Matching is deterministic only. Resolution order is exact active `match_key`, exact alias/normalized alias, then compatibility by `product_type + intersection(invoice_model_keys, compatible_model_keys)`.
- Compatibility extraction recognizes iPhone tokens such as `iPhone 17e / 16e /14 / 13 / 13Pro` as `iphone_17e`, `iphone_16e`, `iphone_14`, `iphone_13`, `iphone_13_pro`. It is used only for matching; it never splits invoice quantity into separate rows.
- A compatibility candidate is auto-selected only when one best candidate is deterministic. Equal top candidates stay unfilled with `match_status=ambiguous`.
- `Match status` in the order card is read-only UI state rendered under `匹配 / Matching / Матчинг`: `已匹配 / Matched / Сопоставлено`, `按兼容型号匹配 / Matched by compatibility / Сопоставлено по совместимости`, `不明确 / Ambiguous / Неоднозначно`, `未匹配 / Unmatched / Не сопоставлено`, or `手动修改 / Manual override / Ручная правка`.
- Empty or inactive nomenclature is valid; the UI/API must still preserve unmatched rows for manual operator correction.

# 4. Invoice Price Conformity

- Initial invoice parse/create computes and persists per-line price conformity against the current active nomenclature `purchase_price_yuan`. Browser/localStorage is not a source of truth.
- Saved line fields include `invoice_price_yuan_snapshot`, `reference_purchase_price_yuan_snapshot`, `price_conformity_status`, `price_conformity_checked_at`, `price_conformity_check_mode`, `price_conformity_reason`, `price_conformity_actor` and `price_conformity_context`.
- Canonical statuses are `matched`, `mismatched`, `sku_not_found`, `reference_price_missing`, `invoice_price_missing` and `not_checked`. `not_checked` is used for non-product rows and conservative ambiguity such as missing/non-yuan invoice currency.
- Money comparison is Decimal-based after normalization of strings, spaces, currency symbols and comma/dot decimals. Equality is not checked through float comparison.
- A confirmed comparison is made only when the invoice currency is recognizably yuan (`RMB`, `CNY`, `CNH`, `YUAN`, `¥`, `元`). Other or missing currencies keep the row unconfirmed with machine-readable reason `currency_not_yuan` or `currency_missing`.
- Opening an existing saved order through `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}` does not recalculate price conformity; it returns persisted snapshots/statuses.
- Operator-only `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/price-check` manually rechecks all lines against current nomenclature, updates persisted price fields, writes `check_mode=manual_recheck` and stores the current web actor/context when available. Supplier/factory role can read saved statuses but cannot call the route. The manual recheck button is rendered only inside the operator embedded supplier frame, not on standalone `/sheet-vitrina-v1/supplier`.
- The order card product table includes `价格匹配 / Price check / Соответствие цены`: `matched` renders a green check, every problem or unconfirmed status renders a red cross with a safe tooltip reason/snapshots.
- One-time backfill for legacy saved orders is `apps/sheet_vitrina_v1_supplier_price_conformity_backfill.py --runtime-dir <runtime_dir> --apply`. It fills only lines without existing `price_conformity_checked_at`, uses `check_mode=migration_backfill`, is idempotent and preserves unrelated shipment/line fields.

# 5. Auth Boundary

- Operator role can access the full WebCore shell, `Настройки`, nomenclature CRUD/export/import API and supplier shipment APIs including delete/rematch, manual price check and order-status update.
- Supplier role is optional and configured only through runtime env:
  - `WB_CORE_SUPPLIER_AUTH_USERNAME`
  - `WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH`
  - `WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME`
- Supplier role can access only `/sheet-vitrina-v1/supplier`, read/create/edit supplier shipment APIs, invoice downloads, login/logout and needed static/browser assets. It cannot access `/sheet-vitrina-v1/vitrina`, `/sheet-vitrina-v1/operator`, `/sheet-vitrina-v1/settings`, nomenclature CRUD/export/import APIs, supplier delete/rematch/manual price-check, order-status mutation or unrelated `/v1/sheet-vitrina-v1/...` APIs.
- Supplier credentials are never committed as plaintext, hashes, cookies or tokens. A live supplier account may use machine-safe username `hanshang` and display label `HanShang Technology` / `Ханшанг`, but the password and PBKDF2-HMAC hash remain runtime-only values outside Git/log output.

# 6. Factory-Order Inbound Source

- The supplier shipment registry is an optional calculation input source for `Поставки -> Расчёты -> Заказ на фабрике`, not a new ЕБД/accepted truth replacement for supplier orders.
- Manual Excel input for `Товары в пути от фабрики` remains available and is the default factory inbound source (`manual_excel`).
- Operator may choose `supplier_registry` as the mutually exclusive factory inbound source. The selection is sent explicitly in the calculate request and may be persisted in browser/operator UI state plus the last server result payload.
- Supplier registry conversion uses only saved shipments with valid `shipment_date`. Current bounded default acceptance date is `shipment_date + 30 days` (`SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS = 30`); this is intentionally a backend constant with a TODO to move into operator settings later.
- Only product lines deterministically resolved to `nmId` (`matched` or `matched_by_compatibility`) and positive quantity become inbound rows. Unmatched, ambiguous, missing-shipment-date and invalid-quantity product lines stay visible in diagnostics/warnings and do not silently reduce the factory-order recommendation.
- The factory-order UI shows a read-only supplier registry summary under the manual factory inbound block: invoice number/date, total product quantity, supplier shipment date, calculated acceptance date, matched/unmatched/ambiguous/missing-date diagnostics and usable quantity.
- When supplier registry source is selected and usable matched rows inside the current factory-order inbound window are zero, calculation still succeeds with zero factory inbound coverage and a truthful warning.
