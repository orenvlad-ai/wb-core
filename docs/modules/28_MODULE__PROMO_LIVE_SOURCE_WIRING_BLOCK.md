---
title: "Модуль: promo_live_source_wiring_block"
doc_id: "WB-CORE-MODULE-28-PROMO-LIVE-SOURCE-WIRING-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded live-wired checkpoint блока `promo_live_source_wiring_block`."
scope: "Repo-owned wiring `promo_xlsx_collector_block` обратно в current `sheet_vitrina_v1` refresh/runtime/read-side contour: archive-first source contract, interval-based historical replay into exact-date runtime seam, accepted snapshot semantics, STATUS/read-side exposure и bounded live integration smoke."
source_basis:
  - "packages/contracts/promo_live_source.py"
  - "packages/application/promo_campaign_archive.py"
  - "packages/application/promo_live_source.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "apps/promo_campaign_archive_integrity_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py"
related_modules:
  - "packages/contracts/promo_live_source.py"
  - "packages/application/promo_campaign_archive.py"
  - "packages/application/promo_live_source.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/registry_upload_http_entrypoint.py"
related_tables:
  - "temporal_source_snapshots"
  - "temporal_source_slot_snapshots"
related_endpoints:
  - "POST /v1/sheet-vitrina-v1/refresh"
  - "GET /v1/sheet-vitrina-v1/status"
  - "GET /v1/sheet-vitrina-v1/plan"
related_runners:
  - "apps/promo_campaign_archive_integrity_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под archive-first / interval-based promo semantics and canonical candidate-vs-eligible metric split: participation/count считаются по eligible rows, а promo_entry_price_best считается как max plan price по active candidate rows."
---

# 1. Идентификатор и статус

- `module_id`: `promo_live_source_wiring_block`
- `family`: `browser-capture -> live source wiring`
- `status_transfer`: repo-owned wiring materialized
- `status_verification`: targeted smoke и bounded live integration smoke подтверждены
- `status_checkpoint`: текущий `main` уже wire-ит `promo_by_price` в refresh/runtime/read-side contour
- `status_main`: модуль смёржен в `main`

# 2. Что именно wire-ится

- Existing repo-owned precursor `promo_xlsx_collector_block` остаётся thin browser adapter boundary.
- Новый bounded live seam materialize-ит server-owned source `promo_by_price` через:
  - `packages/contracts/promo_live_source.py`
  - `packages/application/promo_live_source.py`
  - live injection into `RegistryUploadHttpEntrypoint -> SheetVitrinaV1LivePlanBlock`
- Collector sidecar/workbook truth теперь преобразуется обратно в current runtime rows:
  - `promo_participation`
  - `promo_count_by_price`
  - `promo_entry_price_best`
- Historical truth generation теперь тоже server-owned:
  - campaign metadata задаёт authoritative interval `promo_start_at..promo_end_at`
  - archived workbook задаёт SKU + `Плановая цена для акции`
  - daily metric truth задаёт `price_seller_discounted` через runtime `prices_snapshot[accepted_current_snapshot]` для exact requested date
  - interval replay materialize-ит exact-date payload в existing runtime seam `temporal_source_snapshots[source_key=promo_by_price]`

# 3. Target contract и смысл результата

- `promo_by_price[today_current]`:
  - trigger-ит bounded repo-owned seller-portal collector run;
  - collector сначала пытается reuse-ить unchanged archived campaign artifacts и скачивает только missing/changed campaigns;
  - after archive sync materialize-ит exact-date current payload из covering archived campaign artifacts;
  - future promo не попадают в current numeric fill.
- `promo_by_price[yesterday_closed]`:
  - corrective refresh сначала обязан попытаться server-side interval replay из archived campaign artifacts для exact requested date и overwrite-ить stale accepted closed snapshot, если replay дал exact `success`;
  - только если interval replay не дал exact `success`, read-side может fallback-нуться к already accepted/runtime-cached promo truth;
  - invalid later attempt не может destructively overwrite accepted closed/current truth.

`STATUS` для promo source обязан surface-ить:
- `success`
- `incomplete`
- `missing`

Refresh diagnostics для `promo_by_price` дополнительно surface-ятся как observability-only metadata внутри already existing ready snapshot path:
- `metadata.refresh_diagnostics.source_slots[].promo_diagnostics` для source slot `promo_by_price[*]`;
- для `today_current` этот block содержит internal `phase_summary` по promo chain (`collector_total`, archive lookup/sync, workbook inspection, price truth lookup/join, source payload build, acceptance/fallback policy markers), lightweight counters, fingerprints, fallback/invalid reason fields и dry-run-only skip opportunity marker;
- artifact validation writes `artifact_validation_schema_version=promo_artifact_validation_v1`, `artifact_state_counts`, `artifact_validation_summary`, and compact `missing_campaign_artifacts` examples for problematic covering campaigns;
- materializer counters distinguish collector-reported reuse (`collector_reuse_count` / legacy `workbook_reuse_count`) from archive artifacts validated as usable (`validated_workbook_usable_count` / `materializer_usable_count`);
- status-aware validation also surfaces `ui_status_counts`, `download_action_state_counts`, `ended_without_download_count`, `metadata_only_true_artifact_loss_count`, and `non_materializable_expected_count`;
- эти diagnostics не являются data truth, не меняют source fetch policy, acceptance/fallback semantics, temporal policy, retry behavior, Google Sheets/GAS archive boundary или browser/localStorage truth;
- browser collector currently emits only total collector runtime plus summary counters; per-candidate browser/download timings stay an explicit observability gap until a separate adapter refactor.

`STATUS.note` обязан нести minimum debug facts:
- `trace_run_dir`
- `collector_status`
- `current_promos`
- `current_promos_downloaded`
- `current_promos_blocked`
- `future_promos`
- `skipped_past_promos`
- `ambiguous_promos`

# 4. Явно принятые temporal semantics

- `promo_by_price` теперь относится к `dual_day_capable` группе read-side, но с asymmetric capture semantics:
  - `today_current` = archive-first live collector attempt for the current business day, followed by archive-based exact-date materialization
  - `yesterday_closed` = corrective interval replay first, then accepted/runtime cache only as bounded fallback when replay is unavailable or non-exact
- exact promo dates materialize-ятся только при reliable parse and only inside authoritative campaign interval.
- Для cross-year short labels:
  - `promo_period_text` остаётся authoritative raw field
  - `promo_start_at = null`
  - `promo_end_at = null`
  - `period_parse_confidence = low`
- Exact-date current success может быть accepted и сохранён в runtime snapshot seam.
- Later invalid current attempt:
  - не может очищать already accepted same-day promo truth;
  - surface-ится как `resolution_rule=accepted_current_preserved_after_invalid_attempt`.

# 5. Source -> runtime mapping

- Numeric mapping живёт server-side и не переносится в Apps Script:
  - сначала строится `candidate set` из covering campaign rows для `SKU + date`, где SKU есть в archived workbook, дата попадает в `promo_start_at..promo_end_at`, а `Плановая цена для акции` валидна;
  - campaign interval / identity / `Плановая цена для акции` берутся из archived promo workbook + metadata;
  - `price_seller_discounted` берётся как already materialized daily metric truth для exact date из runtime `prices_snapshot`;
  - `eligible set` = candidate rows, где `price_seller_discounted < Плановая цена для акции`;
  - `promo_participation` = `1` when eligible set is non-empty, else `0`;
  - `promo_count_by_price` = count of eligible rows;
  - `promo_entry_price_best` = max(`Плановая цена для акции`) по candidate rows, not eligible rows; при пустом candidate set остаётся truthful empty `0`;
  - если candidate set есть, но `price_seller_discounted` отсутствует, source may remain `incomplete`, participation/count stay non-positive, но `promo_entry_price_best` продолжает surface-ить max candidate plan price.
- overlap rule is deterministic and additive across covering campaigns for the same SKU/date.
- Workbook alone не считается sufficient:
  - promo title / period / promo status / promo_id / period_id идут из sidecar/card truth
  - workbook inspection нужен для export-kind reporting и artifact debugging, но не для вычисления seller discounted price
  - collector workbook reuse remains a fetch-side observation and is not treated as materializer truth until archive artifact validation marks the metadata+workbook unit as `complete`

# 5.1. Promo archive artifact validation

Materializer-level validation checks the atomic campaign artifact unit before row materialization:
- `metadata.json` sidecar exists and carries parseable campaign coverage fields;
- `archive_record.json` workbook path points to the canonical archive workbook path;
- workbook file physically exists, is non-empty, has mtime/size and fingerprint evidence, and can be opened for plan-price sheet inspection during materialization;
- `period_parse_confidence=high` and requested exact date is inside `promo_start_at..promo_end_at`;
- workbook inspection JSON is parsed when present, but raw workbook/upstream payloads, cookies, tokens, browser state and localStorage-derived data are not persisted in diagnostics.

Artifact states are:
- `complete`
- `incomplete`
- `stale`
- `corrupted`
- `missing_workbook`
- `metadata_only`
- `workbook_without_metadata`
- `ambiguous_date`
- `unusable`
- `ended_without_download`

If collector metadata has high-confidence ended UI evidence, loaded drawer/card, campaign/title match, and absent/disabled download action, the materializer may classify a metadata-only campaign as `ended_without_download` with `workbook_required=false` and reason `metadata_only_ended_without_download`. This remains non-materializable and must not be treated as fresh upstream success. Unknown/low-confidence UI status keeps the conservative missing-artifact path, with true metadata-only loss surfaced as `metadata_only_true_artifact_loss`.

Invalid or incomplete current artifacts still produce `PromoLiveSourceIncomplete`; temporal policy continues to preserve accepted current/closed truth rather than writing fake zeros/blanks or accepting low-confidence dates. `apps/promo_campaign_archive_integrity_smoke.py` is a dry-run audit fixture: it counts archive artifact states and examples without deleting, repairing, downloading, or changing runtime accepted truth.

# 6. Кодовые части

- contracts:
  - `packages/contracts/promo_live_source.py`
- application:
  - `packages/application/promo_live_source.py`
  - `packages/application/sheet_vitrina_v1_live_plan.py`
  - `packages/application/registry_upload_http_entrypoint.py`
- targeted smoke:
  - `apps/sheet_vitrina_v1_promo_live_source_smoke.py`
- bounded integration smoke:
  - `apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py`

# 7. Какой smoke подтверждён

- Targeted smoke подтверждает:
  - promo source -> runtime mapping
  - low-confidence cross-year no-date invention
  - accepted current preservation after invalid later attempt
  - historical interval replay fills exact-date runtime seam on promo cache miss
  - STATUS/read-side source reporting
- Integration smoke подтверждает:
  - existing refresh/runtime/read-side contour реально materialize-ит `promo_by_price[today_current]` как `success`
  - `DATA_VITRINA` получает non-zero promo-backed values
  - collector trace и downloaded promo folders truthfully surface-ятся в runtime note/output
- Hosted live closure additionally depends on one bounded runtime dependency seam:
  - remote system python must have `openpyxl==3.1.5` and `playwright==1.58.0`;
  - canonical repo-owned hosted deploy contract now installs these python packages before service restart instead of leaving them as hidden host drift;
  - browser binaries stay owned by the already existing seller-site contour on the live host and are not re-installed by `wb-core`.

# 8. Что уже доказано по модулю

- `promo_by_price` больше не является permanent blocked source внутри current live contour.
- Existing refresh path остаётся canonical orchestration boundary: отдельный shadow contour не появляется.
- Promo truth живёт в том же runtime snapshot family, что и остальные live sources.
- Blank/absence остаются truthful signal:
  - failed collector attempt не превращается в fake zero-success;
  - accepted current/closed promo truth не стирается invalid later attempt.

# 9. Что пока не является частью финальной production-сборки

- broad operator UX redesign вокруг promo source;
- отдельный public promo collector route;
- Apps Script-side heavy promo logic;
- какой-либо stale-value fallback, который подменяет source failure fake success path.
