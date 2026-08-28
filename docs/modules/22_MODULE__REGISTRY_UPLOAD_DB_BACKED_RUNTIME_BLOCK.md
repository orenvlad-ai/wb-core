---
title: "Модуль: registry_upload_db_backed_runtime_block"
doc_id: "WB-CORE-MODULE-22-REGISTRY-UPLOAD-DB-BACKED-RUNTIME-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_db_backed_runtime_block`."
scope: "Локальный SQLite-backed runtime ingest для V2-реестров: persistent current state, version history, upload result, exact-date temporal source snapshots, role-aware temporal slot truth (`provisional_current / closed_day_candidate / accepted_closed`), persisted closure-retry state, supplier invoice shipment registry state and CNY account ledger state без Apps Script UI и внешнего API."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_db_backed_runtime/evidence/initial__registry-upload-db-backed-runtime__evidence.md"
related_modules:
  - "packages/application/change_registry.py"
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/application/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/application/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/factory_order_sales_history.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/cny_ledger.py"
  - "packages/application/ff_stock_ledger.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
  - "temporal_source_snapshots"
  - "temporal_source_slot_snapshots"
  - "temporal_source_closure_state"
  - "sheet_vitrina_v1_plan_report_monthly_baseline"
  - "sheet_vitrina_v1_factory_order_dataset_state"
  - "sheet_vitrina_v1_factory_order_result_state"
  - "sheet_vitrina_v1_supplier_shipment_uploads"
  - "sheet_vitrina_v1_supplier_shipments"
  - "sheet_vitrina_v1_supplier_shipment_lines"
  - "sheet_vitrina_v1_trade_documents"
  - "sheet_vitrina_v1_invoice_contract_links"
  - "sheet_vitrina_v1_cny_documents"
  - "sheet_vitrina_v1_cny_ledger_operations"
  - "sheet_vitrina_v1_cny_ledger_replay_state"
  - "sheet_vitrina_v1_ff_stock_operation_previews"
  - "sheet_vitrina_v1_ff_stock_operations"
  - "sheet_vitrina_v1_ff_stock_operation_lines"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs"
  - "sheet_vitrina_v1_source_health_status"
related_endpoints: []
related_runners:
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
  - "apps/factory_order_sales_history_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_trade_documents_smoke.py"
  - "apps/cny_ledger_smoke.py"
  - "apps/ff_stock_ledger_smoke.py"
  - "apps/ff_stock_ledger_http_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "docs/modules/21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под current temporal closure seam, plan-report baseline, supplier shipments, trade document registry, CNY account ledger and ФФ stock ledger: SQLite-backed runtime теперь materialize-ит current registry state/version history, role-aware temporal slot snapshots, persisted closure retry state, operator-side factory-order dataset/result state, supplier invoice upload/header/line state including legacy planned `shipment_date`, nullable fact dates `actual_shipment_date` / `actual_ff_acceptance_date`, nullable manual `approx_yuan_rate` and ledger-derived CNY calculation fields, trade document rows/links including parsed contract metadata/warnings/default supplier backfill, CNY currency documents/ledger operations/replay state, ФФ quantity operation previews/headers/lines/source Excel blobs, and a separate manual monthly baseline table used only by the plan-report."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_db_backed_runtime_block`
- `family`: `registry`
- `status_transfer`: DB-backed runtime ingest перенесён в `wb-core`
- `status_verification`: full runtime smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_bundle_v1_block`
  - `registry_upload_file_backed_service_block`
  - `migration/86_registry_upload_contract.md`
  - `migration/89_registry_upload_db_backed_runtime.md`
- Семантика блока: принять уже собранный bundle, переиспользовать текущий validator и materialize-ить current server-side truth в DB-backed runtime storage до любого live API entrypoint.

# 3. Target contract и смысл результата

- Канонический input:
  - `bundle_version`
  - `uploaded_at`
  - `config_v2`
  - `metrics_v2`
  - `formulas_v2`
- Канонический output result:
  - `status`
  - `bundle_version`
  - `accepted_counts`
  - `validation_errors`
  - `activated_at`
- Runtime DB materialize-ит:
  - version history для принятых bundle;
  - persisted upload result;
  - current-state pointer;
  - versioned rows `config_v2`, `metrics_v2`, `formulas_v2`.
- Runtime DB также materialize-ит:
  - exact-date temporal source cache в `temporal_source_snapshots`;
  - role-aware temporal slot cache в `temporal_source_slot_snapshots`:
    - `provisional_current_snapshot`
    - `closed_day_candidate_snapshot`
    - `accepted_closed_day_snapshot`
  - persisted closure retry state в `temporal_source_closure_state` с `source_key / target_date / slot_kind / attempt_count / next_retry_at / state / last_reason / accepted_at`;
  - bounded one-off reconciliation helper `apps/sheet_vitrina_v1_ready_fact_reconcile.py`, который может materialize-ить missing accepted slots для `fin_report_daily.fin_buyout_rub` и `ads_compact.ads_sum` из уже persisted server-side `sheet_vitrina_v1_ready_snapshots`:
    - default bounded window = `2026-03-01..2026-04-24`;
    - dry-run показывает insert/skip/diff по source/date/metric;
    - apply пишет только отсутствующие `accepted_closed_day_snapshot` slots и closure success metadata с `source_kind=web_vitrina_ready_snapshot_to_temporal_accepted_fact_reconcile_v1`;
    - existing accepted snapshots с diff не перезаписываются, blank ready values не превращаются в нули;
  - separate plan-report manual monthly baseline в `sheet_vitrina_v1_plan_report_monthly_baseline`:
    - key = `month` в формате `YYYY-MM`;
    - fact fields = `fin_buyout_rub`, `ads_sum`;
    - metadata = `uploaded_at`, `source_kind=manual_monthly_plan_report_baseline`, uploaded filename/content-type, workbook checksum, optional note;
    - baseline не подменяет `accepted_closed_day_snapshot` и используется только расчётом `GET /v1/sheet-vitrina-v1/plan-report`;
  - operator-side uploaded workbook state для factory-order datasets;
  - last successful factory-order result state.
  - supplier invoice registry state:
    - staged upload metadata in `sheet_vitrina_v1_supplier_shipment_uploads`;
    - shipment headers/totals/status/file references, legacy planned `shipment_date`, nullable `actual_shipment_date`, nullable `actual_ff_acceptance_date`, nullable manual `approx_yuan_rate`, ledger-derived nullable `cny_ledger_effective_rate` / `cny_payment_currency_rub_cost` / `cny_paid_amount` / `cny_bank_fee_rub` / `cny_calculation_status` / `cny_calculation_error` / `cny_calculated_at`, and nullable `invoice_document_id` in `sheet_vitrina_v1_supplier_shipments`;
    - editable product/extra line details and persisted invoice price conformity snapshots/statuses in `sheet_vitrina_v1_supplier_shipment_lines`;
    - server-owned trade document registry in `sheet_vitrina_v1_trade_documents` for `contract` and `invoice` files;
    - one-primary-contract-per-invoice links in `sheet_vitrina_v1_invoice_contract_links`.
    - supplier financial documents/expense lines in `sheet_vitrina_v1_supplier_financial_documents` and `sheet_vitrina_v1_supplier_financial_expense_lines`, including confirmed `bank_fee_statement` parent documents, matched bank-fee expense lines and `packing_list` workbook documents with parsed carton/quantity/weight/volume summary. A bank statement source is content-addressed once by SHA-256; the generic upload token lives in `supplier_confirmation_runtime.sqlite3` and the bank-specific selection preview is a private fsynced sidecar. Neither creates an active parent document or expense before explicit confirm. Main-DB confirmation atomically writes the parent, selected expenses, globally unique semantic atomic-operation assignments and any derived CNY bank-fee documents; exact repeat is a no-op and cross-shipment reuse is a controlled conflict. Exact-cost fields (`exact_*`) are derived read-side from shipment header CNY fields and financial summary, not stored as separate shipment columns in this checkpoint.
  - server-owned CNY account ledger state:
    - canonical currency documents in `sheet_vitrina_v1_cny_documents` with document type, source/order context, file metadata/hash, operation date/datetime, parse payload, status and natural key;
    - deterministic replay rows in `sheet_vitrina_v1_cny_ledger_operations` with operation type, document/order links, sequence key, CNY/RUB deltas, balances, effective/average rates and diagnostic status. Confirmed CNY-account bank statement fees create canonical `bank_fee` documents and `transfer_fee` operations through the existing ledger; RUB-account statement fees stay only in supplier financial expense lines;
    - last replay state in `sheet_vitrina_v1_cny_ledger_replay_state` with balance, average rate, counts and diagnostics.
  - server-owned ФФ stock ledger state:
    - manual operation previews in `sheet_vitrina_v1_ff_stock_operation_previews` with parsed lines, summary, warnings/errors and original Excel blob until explicit confirm/cancel;
    - durable operation headers in `sheet_vitrina_v1_ff_stock_operations` with operation/source type, idempotency `source_key`, source object id/label, actor, warnings/diagnostics, SKU/quantity totals and optional source Excel metadata/blob;
    - signed quantity lines in `sheet_vitrina_v1_ff_stock_operation_lines` keyed by operation and `nm_id`;
    - current ФФ balance is read as grouped `SUM(quantity_delta)` over durable lines and is not persisted as an editable snapshot.
  - server-owned WB supply transit-cost state:
    - canonical last successful value and current recalculation state in `sheet_vitrina_v1_wb_supply_transit_cost_enrichment`;
    - append-only attempt accounting and classified errors in `sheet_vitrina_v1_wb_supply_transit_cost_enrichment_attempts` without destroying the last successful value;
    - single-flight batch-run lifecycle in `sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs`, including bounded totals and latest run error;
    - sanitized cached source/capability observations in `sheet_vitrina_v1_source_health_status`; this cache accelerates the Settings monitoring surface but does not replace Seller Portal or WB Buyer session storage and is not independent authentication truth.
  - trade document files:
    - settings-uploaded files live under `<runtime_dir>/trade_documents/files/<document_type>/<document_id>/<safe_filename>`;
    - supplier shipment invoice documents may reference existing `<runtime_dir>/supplier_invoices/files/...` paths to preserve backward-compatible invoice downloads;
    - contract parser metadata is stored in existing document registry fields: normalized `number`/`document_date` when available, plus `parser_version`, `parsed_metadata_json`, `warnings_json` and `errors_json`;
    - `contract_metadata_parser_v2` adds bounded first-page OCR diagnostics under `parsed_metadata_json.diagnostics` for image-only PDF/JPG/PNG inputs: OCR availability, engine, selected languages, strategy used, attempt count, non-empty text flag, and number/date found flags. Runtime OCR tools are external host packages (`poppler-utils`, `tesseract-ocr`, language packs), not files committed to the repo;
    - empty document `supplier_name` values can be filled idempotently with canonical default supplier `HanShang Technology`, and empty contract `number`/`document_date` values can be reparsed from the stored runtime file without overwriting non-empty values;
    - scanned/image-only contracts without available OCR remain valid file records and carry parser warnings instead of blocking storage; with OCR available, backfill can populate missing number/date from the stored first-page image text;
    - legacy shipment backfill is idempotent and does not move/delete physical invoice files.
- Для current factory-order seam `temporal_source_snapshots[source_key=sales_funnel_history]` является authoritative server-side storage contract для persisted `orderCount` history:
  - bounded historical window может truthfully replace-иться целиком;
  - future exact-date snapshots продолжают дописываться existing live flow без возврата truth logic в sheet.
- Для current `sheet_vitrina_v1` stocks seam `temporal_source_snapshots[source_key=stocks]` теперь является authoritative exact-date closed-day storage contract:
  - bounded historical window `2026-03-01..2026-04-18` может truthfully persist-иться в тот же runtime layer;
  - future `stocks[yesterday_closed]` reads reuse exact-date runtime snapshots instead of current intraday `wb-warehouses` values.

## 3.1 Допущение bounded шага

- Внутри `wb-core` этот шаг использует локальный SQLite-файл как минимальный DB-backed analog server-side runtime.
- Это не является решением, что production target storage обязан быть SQLite.
- Финальная Postgres/storage model остаётся отдельным архитектурным вопросом вне scope этого блока.

# 4. Артефакты и wiring по модулю

- input artifact:
  - `artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json`
- target artifacts:
  - `artifacts/registry_upload_db_backed_runtime/target/upload_result__accepted__fixture.json`
  - `artifacts/registry_upload_db_backed_runtime/target/upload_result__duplicate_bundle_version__fixture.json`
  - `artifacts/registry_upload_db_backed_runtime/target/current_state__fixture.json`
  - `artifacts/registry_upload_db_backed_runtime/target/version_index__fixture.json`
- parity:
  - `artifacts/registry_upload_db_backed_runtime/parity/input-vs-runtime__comparison.md`
- evidence:
  - `artifacts/registry_upload_db_backed_runtime/evidence/initial__registry-upload-db-backed-runtime__evidence.md`

# 5. Кодовые части

- contracts:
  - `packages/contracts/registry_upload_db_backed_runtime.py`
- application:
  - `packages/application/registry_upload_db_backed_runtime.py`
- reused validator:
  - `packages/application/registry_upload_bundle_v1.py`
- reused result contract:
  - `packages/contracts/registry_upload_file_backed_service.py`
- smoke:
  - `apps/registry_upload_db_backed_runtime_smoke.py`
  - `apps/factory_order_sales_history_smoke.py`
  - `apps/sheet_vitrina_v1_temporal_closure_retry_smoke.py`
  - `apps/ff_stock_ledger_smoke.py`
  - `apps/ff_stock_ledger_http_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный full runtime smoke через `apps/registry_upload_db_backed_runtime_smoke.py`.
- Smoke проверяет:
  - что bundle ingest-ится в runtime DB;
  - что accepted upload result persist-ится и читается обратно из DB;
  - что current server-side truth реконструируется из DB в канонической форме;
  - что version index materialize-ится в DB-backed storage;
  - что duplicate `bundle_version` отвергается и не двигает current state.
- Additional smoke проверяет:
  - что polluted temporal `sales_funnel_history` window может быть truthfully replace-нут exact-date slices from `DATA_VITRINA`-shaped input;
  - что runtime после replacement даёт zero diff against expected window;
  - что missing recent exact-date snapshot может bounded-refetch-иться и persist-иться обратно в runtime coverage.

# 7. Что уже доказано по модулю

- upload line больше не заканчивается на file-backed simulation: есть локальный DB-backed runtime слой.
- Current truth уже materialize-ится как server-side runtime state, а не только как JSON-marker.
- Новый слой является прямой технической базой под будущий тонкий API/entrypoint для загрузки реестров из `VB-Core Витрина V1`.
- Тот же runtime слой уже достаточно выразителен для authoritative factory-order history seam:
  - `sales_funnel_history` exact-date slices живут server-side;
  - bounded historical replacement не требует append-only merge с polluted rows;
  - one-time migration input из live `DATA_VITRINA` не превращает sheet в постоянный source of truth.
- Тот же runtime слой теперь достаточно выразителен и для strict temporal truth в `sheet_vitrina_v1`:
  - `today_current` может жить как provisional slot snapshot;
  - `yesterday_closed` не обязан reuse-ить provisional current payload;
  - accepted closed-day truth materialize-ится только после отдельного acceptance step и сохраняется отдельно от provisional/candidate state;
  - retry/backoff lifecycle живёт server-side и не переносится в Apps Script.
- Тот же runtime слой теперь достаточно выразителен и для historical stocks truth:
  - `stocks` exact-date success payload хранится в общем `temporal_source_snapshots` без нового отдельного storage contour;
  - live refresh может читать этот cache runtime-first и не refetch-ить historical CSV при уже materialized snapshot.
- Тот же runtime слой теперь содержит bounded operator-owned manual monthly baseline для `Выполнение плана`:
  - агрегаты Jan/Feb и последующих недостающих полных месяцев могут быть загружены XLSX-файлом в отдельную таблицу;
  - daily accepted snapshots остаются приоритетным source по дням;
  - baseline не становится general-purpose historical backfill и не доступен другим отчётам.

# 8. Что пока не является частью финальной production-сборки

- Apps Script upload button;
- Google Sheets UI;
- live operator-facing API endpoint;
- deploy и orchestration;
- production Postgres schema и внешняя инфраструктура.

## SKU action event extension

The same SQLite runtime owns `sheet_vitrina_v1_sku_action_events`. It is append-only audit evidence for SKU-management price and exact campaign/placement bid attempts. Only rows with confirmed commit and matching successful readback participate in last-change and daily-delta projections. Business-day grouping uses `Asia/Yekaterinburg`; multiple confirmed deltas are summed, while a day with no event has no lookup value (`null`, not `0`). This table is also the only source for the operator history panel; no parallel experiment journal is created.

## WB incident policy extension

The runtime owns append-only `sheet_vitrina_v1_wb_incident_policy_revisions` by canonical seller and revision. Each row stores activity, stable warehouse IDs plus exact historical name identities, reason, effective interval, status, actor/source, created timestamp and preserved legacy per-user payload evidence. Date resolution selects the latest revision that owns the exact date; inactive/end-state revisions stop current/future adjustment without rewriting prior published dates.

`sheet_vitrina_v1_wb_incident_projection_cache` is derived-only materialization keyed by seller, exact snapshot/cache digest, policy revision and snapshot date. Confirmed entries use the upstream raw digest. The Vitrina-only provisional namespace uses `vitrina-accepted-payload:sha256:...`, a deterministic digest of the actually accepted items/warehouse rows; quality metadata explicitly records that this identity is not proof of completeness. It stores deterministic fact/incident/effective projections and cannot mutate canonical stock snapshots, warehouse balances, WAC or capital events.

`sheet_vitrina_v1_incident_rematerialization_audit` owns the bounded ready-snapshot publication audit. One operation/date row pins operation and plan fingerprint, approval reference/actor, bundle/snapshot identity, target dates, semantic before/after plan digests, non-target digest, changed-cell count, compact before/after incident manifests and apply timestamp. Apply updates only the matching ready snapshot `plan_json` inside `BEGIN IMMEDIATE`, accepts only the reviewed before/after digest, preserves raw temporal stocks and unrelated fields, and requires transactional plus second-plan idempotent readback.

`sheet_vitrina_v1_finance_daily_recovery_audit` is the operation receipt for one
exact daily Finance recovery; it is not a Finance row ledger. The row pins
operation/fingerprint/approval/actor/deployed SHA, exact date and ready-snapshot
identity, terminal page/cursor/source digest, before/after/non-target plan
digests, changed-cell count and compact 171-cell before/after manifests. One
operation also retains the exact pre-change general/accepted temporal payloads
and closure row plus their digest. One
`BEGIN IMMEDIATE` CAS publishes the reviewed ready plan, the existing
`temporal_source_snapshots` and `accepted_closed` payload plus closure success,
then inserts this audit. Raw seller-report rows are never persisted here.
Same-identity repeat is zero-write/no-op; conflicting or auditless after-image
fails closed. Readback opens the selected operational generation in SQLite
`mode=ro`, sets `query_only=ON`, and verifies 171/171, 33/33, terminal 204,
source/plan/non-target digests, duplicate absence and closure state.
After an audit exists, ordinary full/group/auto refresh publication fails
closed if it would regress any protected exact Finance cell; a producer with
the same 171 values remains admissible. This prevents a plan built before the
recovery from overwriting the bounded result without stopping global timers.

## Global SQLite contention contract

All shared runtime writers open SQLite through `packages/application/sqlite_contention.py`. Interactive requests have a 30-second bounded budget and shorter jittered backoff; background processes have a 10-second budget and yield longer between attempts. Each individual SQLite attempt uses a 250 ms busy timeout. Only `SQLITE_BUSY`/`SQLITE_LOCKED` is retried, so business validation and unrelated database errors are never repeated. The warehouse functional process retains its explicit process-local 120-second override.

The runtime records only sanitized endpoint/operation/phase, priority, owner process, actual wait, retry count and write-transaction duration. SQL text, paths, document contents, bank details, cookies and secrets are excluded. Schema installation is cached per process and database inode after successful verification; ordinary requests do not replay the large DDL script. External requests, parsing, workbook/PDF generation and heavy calculations stay outside write transactions; commit-critical sections contain only validation against current revisions plus bounded persistence.

If the interactive budget is exhausted before a business commit, the HTTP adapter returns `503`, `Retry-After` and contract `wb_core_sqlite_contention_v1` with a Russian retry/resume message. It never exposes the raw SQLite exception. Transactions roll back before that response, and callers use their existing idempotency/revision contracts on retry. Bank-fee confirm is the explicit post-commit exception: its parent/expense/assignment/CNY-document unit is already complete, so contention in the subsequent derived replay returns Russian `202 pending`, `operation_applied=true` and safe retry; the repeat resumes replay without duplicating business rows.

## Change registry operational store

The same StoreRegistry-selected operational SQLite generation contains the
additive `change_registry_*` foundation defined by
`docs/modules/54_MODULE__CHANGE_REGISTRY_FOUNDATION.md` plus the observer-owned
job/source-summary/health/lease tables from module 57. Runtime schema ensure
installs only tables/indexes/triggers. Read overview/status never ensures the
schema: it uses StoreRegistry `mode=ro` with `PRAGMA query_only=ON` and reports
missing schema without a hidden write. Scheduled observation and exact-SHA
deploy activation belong to separate read-only Prices+Ads worker units; no
existing Prices/Ads/SKU writer is instrumented and no historical evidence is
imported.
