---
title: "Модуль: supplier_shipments_block"
doc_id: "WB-CORE-MODULE-34-SUPPLIER-SHIPMENTS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для блока `Поставки -> От поставщика` и `Поставки -> Реестр поставок`: Реестр заказов, supplier invoice parser, server-side nomenclature matching, persisted invoice price conformity, financial documents by supplier order, read-only shipment registry matrix, runtime storage, protected API and supplier-only UI."
scope: "Server-owned invoice order registry under current WebCore runtime: XLSX parse with openpyxl, deterministic type/model matching through server-side nomenclature, persisted per-line invoice price conformity against current purchase_price_yuan snapshots, editable shipment card, filesystem-backed original invoice and financial-document storage under runtime dir, SQLite metadata/lines/nomenclature/financial documents/expense lines, read-only shipment comparison matrix, operator embedded UI, settings surface and supplier-only role boundary."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
related_modules:
  - "packages/contracts/supplier_shipments.py"
  - "packages/contracts/supplier_financial_documents.py"
  - "packages/application/supplier_invoice_parser.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/supplier_financial_documents.py"
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
  - "sheet_vitrina_v1_trade_documents"
  - "sheet_vitrina_v1_invoice_contract_links"
  - "sheet_vitrina_v1_supplier_financial_documents"
  - "sheet_vitrina_v1_supplier_financial_expense_lines"
related_endpoints:
  - "GET /sheet-vitrina-v1/supplier"
  - "GET /sheet-vitrina-v1/settings"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/registry"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/registry/compare-quote"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/parse"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "DELETE /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/rematch"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/price-check"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/invoice"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract"
  - "PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents"
  - "POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}"
  - "PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}"
  - "DELETE /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}"
  - "GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}/file"
  - "GET /v1/sheet-vitrina-v1/settings/nomenclature"
  - "POST /v1/sheet-vitrina-v1/settings/nomenclature"
  - "GET /v1/sheet-vitrina-v1/settings/nomenclature/export.xlsx"
  - "POST /v1/sheet-vitrina-v1/settings/nomenclature/import.xlsx"
  - "PATCH /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}"
  - "DELETE /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}"
  - "GET /v1/sheet-vitrina-v1/settings/documents"
  - "POST /v1/sheet-vitrina-v1/settings/documents"
  - "PATCH /v1/sheet-vitrina-v1/settings/documents/{document_id}"
  - "DELETE /v1/sheet-vitrina-v1/settings/documents/{document_id}"
  - "GET /v1/sheet-vitrina-v1/settings/documents/{document_id}/file"
  - "PATCH /v1/sheet-vitrina-v1/settings/documents/{invoice_document_id}/contract"
  - "DELETE /v1/sheet-vitrina-v1/settings/documents/{invoice_document_id}/contract"
related_runners:
  - "apps/supplier_invoice_parser_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/supplier_financial_documents_smoke.py"
  - "apps/supplier_financial_documents_real_pdf_browser_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_price_conformity_backfill.py"
  - "apps/registry_upload_http_entrypoint_auth_smoke.py"
  - "apps/registry_upload_http_entrypoint_supplier_auth_smoke.py"
  - "apps/sheet_vitrina_v1_trade_documents_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py"
related_docs:
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Supplier-facing order registry uses trilingual Chinese/English/Russian labels, shows fixed supplier in the registry only, hides Supplier/Customer and order SKU fields from the card, defaults supplier metadata to HanShang Technology, persists operator-owned order_status on shipment headers, reads invoice contract no/date from cells or drawing XML text, keeps nmId/nomenclature visible, preserves deterministic nomenclature matching, and persists per-line invoice price conformity snapshots/statuses against current nomenclature purchase_price_yuan. Manual price recheck remains operator-only and UI-scoped to the operator embedded `Поставки -> От поставщика` frame. Operator supply navigation also exposes read-only `Реестр поставок`, a grouped matrix with shipments as columns and financial/physical/document metrics as rows, built from existing shipment, financial-document and expense-line runtime truth. Operator settings are split into inner tabs `Номенклатура`, `Договоры` and `Инвойсы`; contract and invoice document rows are rendered in separate subsections over the same server-owned registry. The order card now has inner tabs `Состав поставки` and operator-only `Финансовые документы`; the financial contour stores supplier-order PDF originals, deterministic parsed financial document rows and expense lines for logistics quotes, logistics invoices and customs declarations, computes compact logistics/customs/FX/efficiency/per-unit summary, and keeps quote-vs-invoice matching reviewable instead of silently allocating ambiguous costs. The document registry owns PDF/JPG/PNG/XLSX contract/invoice files, canonical default supplier fallback, XLSX invoice metadata parsing, bounded contract number/date parsing for XLSX, text-layer PDF and image-only first-page OCR when runtime tools are installed, idempotent document metadata/supplier backfill, invoice->contract links, shipment-linked contract download, and idempotent legacy shipment invoice backfill. Standalone supplier role can read/download only shipment-linked invoice/contract files through supplier shipment routes and cannot access settings document CRUD, financial-document routes, shipment-registry matrix or arbitrary document ids. No Google Sheets/GAS contour, no browser-local truth; image-only PDF/JPG/PNG contract parsing returns safe diagnostics when OCR tools or patterns are missing."
---

# 1. Contract

- Operator surface: in `Поставки`, inner tab `Расчёты` keeps the existing factory/WB supply calculators; inner tab `Реестр поставок` renders the read-only comparison matrix; inner tab `От поставщика` embeds the supplier registry without an extra operator card/title wrapper.
- Supplier-only surface: `GET /sheet-vitrina-v1/supplier` renders only the supplier shipment registry without full operator navigation and without the manual price recheck button, even when opened from an operator session.
- Supplier-facing labels in the order registry/card use `中文 / English / Русский` business wording. The main title is `订单登记表 / Order registry / Реестр заказов`; duplicated registry headings and the old subtitle are not rendered. In framed operator view the title row actions are `新增订单 / Add order / Добавить заказ`, `退出 / Logout / Выйти`, then `Открыть отдельно`; the standalone-open link points to the existing `GET /sheet-vitrina-v1/supplier` UI route and does not introduce a new API route.
- Registry list UI separates `loading`, `loaded_empty`, `loaded_with_rows` and `error`: the initial DOM and in-flight list fetch show a compact loading state, while `暂无订单 / No orders yet / Заказов пока нет.` is rendered only after the list API has returned an empty shipment list.
- Supplier registry table headers remain sticky and use a subtly stronger dark/violet surface plus visible lower border so header cells do not blend into order rows; horizontal scroll and row action semantics are unchanged.
- Visible order card/form UI does not render editable `Supplier` or `Customer`. Supplier metadata is fixed server-side to `HanShang Technology`; the registry shows this fixed value in `供应商 / Supplier / Поставщик` with fallback for legacy rows. Customer is kept backward-compatible in stored/API payloads but is not required by create/save and is not shown.
- Visible order product rows do not render `our_sku`/`SKU` fields. Matching still may keep legacy `internal_sku` in storage for backward compatibility, but the supplier/order UI shows only `平台ID / nmId / nmId` and `我方品名 / Our item name / Номенклатура`.
- Registry matching column is `匹配 / Matching / Матчинг`; compact registry values are `OK` when every product line is matched and `Check` otherwise.
- Operator settings surface: `GET /sheet-vitrina-v1/settings` is a service page reachable from the top-right `Настройки` button, not a top-level WebCore tab. Inside the page, inner settings tabs switch between `Номенклатура`, `Договоры` and `Инвойсы` without reload; `Номенклатура` is the default active subsection and any browser-local tab state is UI preference only, not runtime truth.
- Runtime truth is server-owned:
  - original XLSX files live under `<runtime_dir>/supplier_invoices/files/<shipment_id>/<safe_filename>`;
  - staged uploads live under `<runtime_dir>/supplier_invoices/uploads/<upload_id>/<safe_filename>`;
  - settings-uploaded trade document files live under `<runtime_dir>/trade_documents/files/<document_type>/<document_id>/<safe_filename>`;
  - SQLite stores upload metadata, shipment headers, editable line details, persisted price conformity fields, nomenclature rows, trade document rows and invoice-contract links.
- `shipment_date` is the only required manual field after parse. It is rendered as `出货日期 / Shipment date / Дата отгрузки`, is required on create/save, and is validated server-side even though the UI disables save until it is present.
- Shipment headers persist `order_status` in `sheet_vitrina_v1_supplier_shipments` with non-destructive default `production`. Canonical values are `production` (`На производстве`), `in_transit` (`В пути`) and `accepted_ff` (`Принято на ФФ`). List and detail API responses expose `order_status`; legacy rows without the column/value fall back to `production`.
- Factory-order supplier-registry inbound uses `order_status` server-side: only `production` and `in_transit` shipments can become `Товары в пути от фабрики`; `accepted_ff` shipments are excluded because their goods are already accepted on FF and must enter calculation through FF stock, not as factory inbound.
- In the operator embedded registry table, `Currency / Валюта` is hidden from the list, compact `Документы` replaces the old invoice-only file column, `Статус заказа` is shown after `Документы` and before `Actions`, and status changes use a narrow status-only PATCH so shipment lines, invoice metadata, source file pointers and matching state are not rebuilt or erased. The status selector is not rendered in the standalone supplier-only view.
- Orders can be deleted by operator role only. Delete controls use UI-level confirmation: the first click opens an inline confirmation with cancel, and backend DELETE is called only by the explicit confirm action. While confirmation is open, row clicks do not open the order card. Confirmed delete removes the DB order/lines, archives the invoice document link when present, and makes shipment-scoped invoice download unavailable without deleting the physical runtime invoice file.
- Saved order cards expose `关闭 / Close / Закрыть`; the visible `重新匹配 / Re-match / Пересопоставить` action is not rendered in the supplier/order card. The rematch API remains available for internal compatibility and applies current nomenclature without overwriting manual overrides unless explicitly requested by API payload.
- Saved order cards have an inner tab switcher. `Состав поставки` owns the existing editable order composition and remains the default view. `Финансовые документы` is rendered only in operator embedded mode and is hidden from supplier-only view.

# 1.1 Trade Documents Registry

- `Настройки -> Договоры` renders only active `contract` documents. The table omits a type column and shows number, date, supplier, linked invoice count, source, file, updated time and actions. Operator inline edit is limited to `number`, `document_date` and `supplier_name`; it never mutates file metadata, archive status or invoice links, and blank supplier edits fall back to canonical default supplier on the backend.
- `Настройки -> Инвойсы` renders only active `invoice` documents. The table omits a type column and shows number, date, supplier, `Сумма invoice`, linked contract state, source, file, updated time and actions. Unlinked invoices show `Не привязан`; the link/change selector is built only from active contracts. Linked invoices expose an idempotent operator-only unlink action that removes only the invoice->contract row.
- Supported files are `.pdf`, `.jpg`, `.jpeg`, `.png`, `.xlsx`. PDF/JPG/PNG are stored as files; contract metadata extraction is best-effort and bounded.
- Contract uploads attempt to fill `number` and `document_date` automatically. XLSX files are read through `openpyxl`; PDF files first use `pdftotext`/embedded text when available and then bounded first-page OCR if runtime tools are installed. Runtime OCR dependencies are `poppler-utils`, `tesseract-ocr`, `tesseract-ocr-eng`, `tesseract-ocr-chi-sim` and optionally `tesseract-ocr-rus`. JPG/PNG files use the same Tesseract language selection over the original image. Missing OCR tools remain non-fatal and produce parser warnings.
- OCR for image-only PDFs is first-page only. It renders bounded strategies through `pdftoppm` (page-size tolerant low DPI first, then grayscale/top-crop/higher-DPI fallbacks) and tries Tesseract `psm` values `6`, `11`, `4`, `3` with the available subset of `eng+chi_sim+rus`. The parser stops when both number and date are found; it does not OCR all contract pages.
- Contract number follows the MVP rule: the first non-empty document line is authoritative input, with labels such as `Contract No`, `Contract No.`, `Contract Number`, `No.`, `№`, `Контракт №`, `Договор №`, `合同编号` and `合同号` stripped when present. If OCR produces a bilingual heading such as `KOHTPAKT Nb ... CONTRACT J ...`, the parser extracts the repeated number candidate instead of storing the whole heading. If the first line is only a generic heading (`Contract`, `Договор`, `合同`), the parser searches the nearest top header lines for the actual number instead of storing the generic heading. Contract date is searched only in a bounded header fragment and normalized to `YYYY-MM-DD` for formats such as `YYYY-MM-DD`, `YYYY.MM.DD`, `YYYY/MM/DD`, `DD.MM.YYYY`, `MM/DD/YYYY`, `2026年5月13日`, `May 13, 2026`, `13 May 2026` and Russian month names.
- Contract parser metadata stores safe diagnostics under `parsed_metadata_json.diagnostics`: OCR availability, engine, selected languages, strategy used, attempt count, whether OCR text was non-empty, and whether number/date were found. It does not store or expose full OCR text beyond the existing first non-empty line metadata used for bounded debugging.
- Manual `number`/`document_date` values on upload remain authoritative. If parser values are different, the manual values are stored on the document row while parsed values and warnings remain in `parsed_metadata_json`/`warnings_json`. Upload API responses expose `parsed_number`, `parsed_document_date`, `parser_warnings` and `parser_errors`.
- New and updated trade documents never persist an empty supplier when no manual or parsed supplier is available: they use canonical `DEFAULT_SUPPLIER_NAME = HanShang Technology`. Manual supplier input wins over parsed/default values, and parsed supplier wins over default when available.
- `apps/sheet_vitrina_v1_trade_documents_backfill.py --runtime-dir <runtime_dir> --apply` backfills existing active documents idempotently: empty `supplier_name` gets the canonical default supplier, contract rows with empty `number` or `document_date` are reparsed from their stored runtime file, and non-empty/manual values are preserved. Repeated runs do not rewrite already filled rows.
- XLSX invoice uploads through settings are parsed with the existing supplier invoice parser when applicable. Parsed metadata may fill invoice no/date, contract no/date, supplier, currency, totals, warnings and errors. Settings invoice upload creates only a document record; it does not create a supplier shipment/order card.
- Settings upload deduplicates active settings documents by `document_type + file_sha256` and returns the existing document instead of creating another row for the same file.
- Supplier shipment create from the existing invoice parser flow creates or finds an `invoice` document record, writes `invoice_document_id` to `sheet_vitrina_v1_supplier_shipments`, and keeps the existing `GET .../{shipment_id}/invoice` download backward-compatible.
- Legacy shipments with `source_file_path` and no `invoice_document_id` are backfilled idempotently into `invoice` document records. The record references the existing runtime invoice file path; physical legacy invoice files are not moved or deleted by the backfill.
- `sheet_vitrina_v1_invoice_contract_links` allows one primary contract per invoice and many invoices per contract. Exact active contract candidates are found by contract number and date. If supplier shipment creation/backfill finds exactly one active contract candidate, it auto-links the invoice to that contract; zero or multiple candidates are returned as non-destructive candidate/status data.
- The order card has a compact `Документы` block: invoice download, linked contract no/date and download, or operator-only select/upload controls for missing contract. Supplier standalone view can see/download linked documents but cannot link/upload/archive.
- Contract archive is rejected while active invoice links point to that contract. Invoice archive removes the invoice-contract link. File rows are archived in DB; physical files are not removed by document archive.

# 1.2 Supplier Order Financial Documents

- Financial documents are scoped to one saved supplier shipment/order (`supplier_order_id` = shipment id). Runtime truth is server-owned:
  - PDF originals live under `<runtime_dir>/supplier_financial_documents/files/<supplier_order_id>/<document_id>/<safe_filename>`;
  - SQLite table `sheet_vitrina_v1_supplier_financial_documents` stores document metadata, parser/rate status and raw/normalized parse JSON;
  - SQLite table `sheet_vitrina_v1_supplier_financial_expense_lines` stores normalized expense lines by document/order.
- MVP document types are `logistics_quote`, `logistics_invoice` and `customs_declaration`. Factory invoices, factory payments, RUB->CNY conversion, SKU cost allocation and WB mutations are out of this module scope.
- Financial upload accepts PDF only and first attempts text-layer extraction. Runtime prefers `pdftotext` when available, then `pypdf`; OCR is not an MVP dependency for this parser and missing text/OCR returns controlled `parse_error`/warnings instead of silent failure.
- For customs declarations, if the primary `pdftotext` extraction recognizes the document but misses gross weight or customs value, parsing retries against the same stored PDF through `pypdf` and accepts the fallback only when those critical fields are recovered.
- Parser is deterministic/rule-based and returns normalized fields, expense lines, parser version (`supplier_financial_document_parser_v2`), warnings and confidence/status. It detects Transitplus logistics quotes, logistics invoices like World-Logistik invoices 103/113, and aggregate customs declarations (`ДТ`) with customs fee/duty/VAT totals, customs value and aggregate gross/net item weights. Transitplus quote parsing is table-layout aware for both `pypdf` and production `pdftotext -layout` shapes: when service labels are separated from the `Общая стоимость` column, or rows are extracted as `1 Стоимость доставки 14360` without `USD`, the parser maps known service labels/trailing amounts plus numbered cost-column rows and a total-sum sanity check to `delivery_cost`, `customs_payments_and_fees`, `ecological_fee`, `brokerage_services`, `company_commission` and `insurance`. A total-validated numbered block wins over percent-like label values such as `Страховая ставка, % 1,0%`.
- Expense lines store category/stage/description/amount/currency/amount_rub/VAT and inclusion flags. Logistics quote lines keep customs payments separate from logistics quote components, and possible/not-included export-document costs are marked reviewable rather than included in totals.
- Transitplus quotes require non-empty positive `delivery_cost` and `customs_payments_and_fees` when the quote total is positive. If those required amounts are missing or zero, the financial document status is `needs_review`; the UI shows a warning and quote-vs-invoice rate calculations are not produced from a partial USD base.
- Server-side USD/RUB CBR rate provider is a seam. Runtime may request official CBR XML by document date and, if an exact date is unavailable, stores the last official effective date before the requested date. Tests and local smokes use `StaticUsdRateProvider`. Rate failure does not reject upload; document rows persist with `rate_pending`/`fx_rate_missing`.
- CBR is a benchmark only. For quote-vs-invoice comparison the summary uses:
  - `implied_rate = invoice_amount_rub / linked_quote_usd_component`;
  - `spread_pct = implied_rate / cbr_usd_rate_on_invoice_date`;
  - `estimated_bank_rate_on_quote_date = cbr_usd_rate_on_quote_date * spread_pct`;
  - `quote_rub_equivalent = linked_quote_usd_component * estimated_bank_rate_on_quote_date`.
  UI labels this as `расчётный курс по правилу КП` / `оценочный банковский курс`, not as a VTB truth.
- MVP auto-match candidate links logistics invoices to logistics-related quote components (`delivery_cost`, `brokerage_services`, `company_commission`, `insurance`) and excludes customs payments/federal customs totals. The summary status is `needs_review` and the UI shows reviewable warnings because exact line-level evidence is intentionally not assumed. If the linked quote USD component is incomplete, zero or missing, implied rate, estimated bank rate and delta are hidden instead of showing mathematically unsafe values.
- `Финансовые документы` UI shows single- or multi-file PDF upload, per-file upload status, document table, recognized fields, expense lines, original PDF download link when stored, status/warnings, and grouped compact summary: quote totals/logistics/customs USD, invoice fact/VAT RUB, customs fee/duty/VAT/total RUB, `расчётный курс по правилу КП`, absolute/relative deviation from CBR and `Счета минус КП-логистика`; `На штуку` metrics for quote delivery+customs RUB equivalent per supplier-order unit and fact delivery+customs per supplier-order unit; `На кг` metrics for logistics/customs/delivery+customs by quote gross weight and by actual customs-declaration gross weight; `% от стоимости` metrics for quote logistics/customs/total vs quote estimated cargo value and fact logistics/customs without VAT/customs with VAT/delivery+customs vs customs value.
- Quote-weight metrics use only `logistics_quote.gross_weight_kg`; customs-weight metrics use only aggregate gross weight parsed from the customs declaration. If actual customs weight is missing, the UI/API expose unavailable values instead of falling back to quote weight and surface `Нет фактического веса из ДТ` only when a customs declaration document exists but the field was not parsed.
- Quote percent metrics use `quote_estimated_cargo_value_usd`; fact percent metrics use `total_customs_value_rub` from the customs declaration. Missing or zero denominators render unavailable values and short source warnings (`Нет стоимости груза по КП`, `Нет таможенной стоимости из ДТ`) rather than `0`, `NaN` or `Infinity`.
- On read, saved customs declaration documents from older parser payloads are refreshed from their already-stored runtime PDF when the normalized parse lacks gross weight, customs value or the current parser version. `excluded` documents are not refreshed, and an operator `confirmed` status is preserved.
- Protected routes are operator-only and follow the existing supplier shipment path:
  - list/upload collection: `/v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents`;
  - detail/patch status/delete: `/v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}`;
  - original file download: `/v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}/file`.
- Operator delete removes exactly one financial-document record, its expense lines and the stored PDF only when the file path belongs to that document's runtime storage directory. Missing documents return controlled JSON 404; bulk delete is not part of the contract.

# 1.3 Supplier Shipment Registry Matrix

- `Поставки -> Реестр поставок` is an operator-only read-side matrix for comparing official supplier shipments. It does not create a parallel storage layer and does not call Google Sheets/GAS.
- Protected read-only route: `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/registry`. Response contract is `sheet_vitrina_v1_supplier_shipment_registry` with `columns[]` for shipments, grouped `sections[]`, row ids, labels and per-shipment cells shaped as `{value, display}`.
- Protected temporary comparison route: `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/registry/compare-quote` accepts multipart `file` + `shipment_id`, parses the PDF with the existing logistics quote parser, and returns `sheet_vitrina_v1_supplier_shipment_registry_quote_comparison`. The uploaded quote is not saved as a financial document, supplier shipment or runtime source of truth.
- The matrix is built from existing runtime truth: supplier shipment headers/lines, supplier financial documents, financial expense lines and `build_financial_summary(..., shipment=...)`. Missing financial documents or denominators produce `value = null` and `display = "—"` rather than `0`, `NaN` or `Infinity`.
- Shipments are sorted by invoice date, then shipment/order date or upload/created date; newer shipments are rightmost. Column headers expose the invoice/order number and date so later sorting changes do not hide date evidence.
- Row groups are: passport, cargo physics, cargo value, logistics quote, fact expenses, normalized fact metrics, lead times and documents. The UI renders these groups with horizontal scroll and sticky left row labels.
- Registry quote RUB/unit uses `quote_total_usd * quote_total_rate / total_units`, where `quote_total_rate` is the same quote-vs-invoice CBR spread estimate when invoices are available and falls back to the quote-date CBR rate when only КП is available. Fact RUB/unit uses `(logistics_invoice_total_rub + customs_total_payments_rub) / total_units`.
- The comparison UI lets an operator choose one temporary КП PDF, select exactly one shipment column, and render grouped rows with `КП`, `Поставка факт`, `Разница` and simple status (`лучше`, `хуже`, `примерно равно`). Numeric differences are `КП - Поставка`, percent differences use the shipment value as baseline when available, and missing baselines remain `—`.

# 2. Parser

- Parser uses `openpyxl` and searches for flexible invoice table headers including `NO.`, `MODELS / （型号）`, `NAME & SPECIFICATION / （品名规格）`, `QTY (PCS) / （数量）`, `U.PRICE / （单价）` and `AMOUNT / （总价）`.
- Financial-document parser aggregates customs declaration item weights from goods blocks (`35 Вес брутто`, `38 Вес нетто`) when the text layer exposes per-item rows. For current GTD `10131010/100626/5187132`, expected parser aggregates are `customs_gross_weight_kg ~= 9784.60`, `customs_net_weight_kg ~= 8806.18`, `total_goods_count = 28` and `total_places = 465`.
- `RMB`/`CNY`/`¥` invoice currency is normalized to `RMB`; declared invoice totals may be read from post-table `Total`/`总值` rows when the value is not available in pre-table metadata.
- Contract metadata is extracted from ordinary cells, merged-cell values and bounded workbook drawing XML text. Supported labels include `Contract No`, `Contract No.`, `Contract Number`, `Contract Date`, `Date of Contract`, `合同号`, `合同编号`, `合同日期`, `下单日期`; date formats such as `2026.5.13`, `2026-05-13`, `13.05.2026` and `5/13/2026` normalize to `YYYY-MM-DD`.
- The document-registry contract parser is separate from invoice table parsing: it reads only bounded header text, stores `parser_version=contract_metadata_parser_v2`, and returns non-fatal warnings/errors instead of blocking contract file storage when metadata cannot be extracted.
- Merged-cell/fill-down blocks are handled for product type markers:
  - `高清膜` / `smk` -> `clear`
  - `防窥膜` / `(Anti-Spy)` -> `anti_spy`
  - `磨砂膜` / `(Matte)` -> `matte`
- Compatible model aliases such as `iPhone 15 / 16` stay as one invoice row and one legacy normalized `match_key`; parser does not split quantity into separate SKU rows.
- Extras such as OPP packets, labels and cards are stored as `line_type=extra`, not product SKU rows. Product-row comments may mention OPP/labels/cards as packaging instructions without turning the product line into an extra.
- Unknown aliases remain persisted and visible with `match_status=unmatched`; low-confidence fuzzy matching is not performed.

# 3. Nomenclature And Matching

- Server-side nomenclature lives in `sheet_vitrina_v1_nomenclature_items` and is edited through `Настройки -> Номенклатура` in the `Справочник номенклатуры` subsection.
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

- Operator role can access the full WebCore shell, `Настройки`, nomenclature CRUD/export/import API, the read-only supplier shipment registry matrix and supplier shipment APIs including delete/rematch, manual price check and order-status update.
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
- Supplier registry conversion uses only saved shipments in `production` or `in_transit` status with valid `shipment_date`. Legacy shipments without `order_status` keep the existing fallback as `production`. Current bounded default acceptance date is `shipment_date + 30 days` (`SUPPLIER_REGISTRY_FACTORY_TO_FF_ACCEPTANCE_DAYS = 30`); this is intentionally a backend constant with a TODO to move into operator settings later.
- Shipments in `accepted_ff` / `Принято на ФФ` are excluded from the factory inbound summary and usable rows. Status/calculate diagnostics expose excluded accepted-FF shipment/product-line/quantity counts and a warning, so this exclusion is visible and does not silently hide source data.
- Only product lines deterministically resolved to `nmId` (`matched` or `matched_by_compatibility`) and positive quantity become inbound rows. Unmatched, ambiguous, missing-shipment-date and invalid-quantity product lines stay visible in diagnostics/warnings and do not silently reduce the factory-order recommendation.
- The factory-order UI shows a read-only supplier registry summary under the manual factory inbound block: invoice number/date, total product quantity, supplier shipment date, calculated acceptance date, matched/unmatched/ambiguous/missing-date diagnostics and usable quantity.
- The factory-order supplier-registry summary has a compact `Обновить` action. It re-requests `GET /v1/sheet-vitrina-v1/supply/factory-order/status` without a full page reload and re-renders the shipment table, totals, warnings and excluded `accepted_ff` counters after order-status changes or newly saved supplier orders. In the embedded operator supplier registry, a successful order-status PATCH also notifies the parent operator page to refresh this status surface.
- When supplier registry source is selected and usable matched rows inside the current factory-order inbound window are zero, calculation still succeeds with zero factory inbound coverage and a truthful warning.
