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
  - "apps/sheet_vitrina_v1_promo_live_source_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/27_MODULE__PROMO_XLSX_COLLECTOR_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под archive-first / interval-based promo semantics: collector reuse-ит unchanged campaign artifacts, historical closed-day truth materialize-ится из campaign interval replay в exact-date runtime seam, а existing refresh/load contour продолжает читать только already prepared server-owned snapshots."
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
  - archived workbook задаёт SKU + price fields
  - interval replay materialize-ит exact-date payload в existing runtime seam `temporal_source_snapshots[source_key=promo_by_price]`

# 3. Target contract и смысл результата

- `promo_by_price[today_current]`:
  - trigger-ит bounded repo-owned seller-portal collector run;
  - collector сначала пытается reuse-ить unchanged archived campaign artifacts и скачивает только missing/changed campaigns;
  - after archive sync materialize-ит exact-date current payload из covering archived campaign artifacts;
  - future promo не попадают в current numeric fill.
- `promo_by_price[yesterday_closed]`:
  - read-side по-прежнему читает только accepted/runtime-cached promo truth;
  - но cache miss теперь может truthfully закрываться server-side interval replay из archived campaign artifacts для exact requested date;
  - invalid later attempt не может destructively overwrite accepted closed/current truth.

`STATUS` для promo source обязан surface-ить:
- `success`
- `incomplete`
- `missing`

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
  - `yesterday_closed` = accepted/runtime-cached exact-date truth, which may be initially populated by interval replay on cache miss
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
  - сначала строится один общий eligible set из covering campaign participations для `SKU + date`
  - row считается eligible, если row-level цена продавца со скидкой из этой строки `< Плановая цена для акции`
  - если workbook даёт `Текущая розничная цена` + row-level discount columns, server-side truth path derive-ит discounted seller price из этих же row fields
  - `promo_entry_price_best` = max(`Плановая цена для акции`) среди eligible rows; при пустом eligible set остаётся truthful empty
  - `promo_count_by_price` = count of eligible rows
  - `promo_participation` = `1` when `promo_count_by_price > 0`, else `0`
- overlap rule is deterministic and additive across covering campaigns for the same SKU/date.
- Workbook alone не считается sufficient:
  - promo title / period / promo status / promo_id / period_id идут из sidecar/card truth
  - workbook inspection нужен для row-level numeric fill и export-kind reporting
  - workbook reuse is preferred over redundant repeated downloads when metadata/content did not change

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
