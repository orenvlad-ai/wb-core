---
title: "Модуль: registry_upload_db_backed_runtime_block"
doc_id: "WB-CORE-MODULE-22-REGISTRY-UPLOAD-DB-BACKED-RUNTIME-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_db_backed_runtime_block`."
scope: "Локальный SQLite-backed runtime ingest для V2-реестров: persistent current state, version history, upload result, exact-date temporal source snapshots, role-aware temporal slot truth (`provisional_current / closed_day_candidate / accepted_closed`), persisted closure-retry state и supplier invoice shipment registry state без Apps Script UI и внешнего API."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_db_backed_runtime/evidence/initial__registry-upload-db-backed-runtime__evidence.md"
related_modules:
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/application/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/application/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/factory_order_sales_history.py"
  - "packages/application/supplier_shipments.py"
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
related_endpoints: []
related_runners:
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
  - "apps/factory_order_sales_history_smoke.py"
  - "apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py"
  - "apps/sheet_vitrina_v1_trade_documents_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/88_registry_upload_file_backed_service.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "docs/modules/21_MODULE__REGISTRY_UPLOAD_FILE_BACKED_SERVICE_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под current temporal closure seam, plan-report baseline, supplier shipments and trade document registry: SQLite-backed runtime теперь materialize-ит current registry state/version history, role-aware temporal slot snapshots, persisted closure retry state, operator-side factory-order dataset/result state, supplier invoice upload/header/line state including legacy planned `shipment_date`, nullable fact dates `actual_shipment_date` / `actual_ff_acceptance_date` and nullable manual `approx_yuan_rate`, trade document rows/links including parsed contract metadata/warnings/default supplier backfill, and a separate manual monthly baseline table used only by the plan-report."
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
    - shipment headers/totals/status/file references, legacy planned `shipment_date`, nullable `actual_shipment_date`, nullable `actual_ff_acceptance_date`, nullable `approx_yuan_rate` and nullable `invoice_document_id` in `sheet_vitrina_v1_supplier_shipments`;
    - editable product/extra line details and persisted invoice price conformity snapshots/statuses in `sheet_vitrina_v1_supplier_shipment_lines`;
    - server-owned trade document registry in `sheet_vitrina_v1_trade_documents` for `contract` and `invoice` files;
    - one-primary-contract-per-invoice links in `sheet_vitrina_v1_invoice_contract_links`.
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
