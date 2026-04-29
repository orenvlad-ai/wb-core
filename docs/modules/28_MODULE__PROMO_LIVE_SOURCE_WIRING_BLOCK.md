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
  - "apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py"
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
  - "apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py"
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
- `status_verification`: targeted smoke, bounded live integration smoke и live/public invariant smoke подтверждены
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
  - archived workbook задаёт SKU + `Плановая цена для акции`, and archive sync also persists normalized `campaign_rows.jsonl` + `campaign_rows_manifest.json`
  - interval replay may read normalized campaign rows when raw `workbook.xlsx` is absent, but only if manifest fingerprint/row-count/column-signature checks pass
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
- artifact validation writes `artifact_validation_schema_version=promo_artifact_validation_v1`, `artifact_state_counts`, `artifact_validation_summary`, compact `missing_campaign_artifacts` examples for all problematic covering campaigns, plus separated `fatal_missing_artifacts` and `expected_non_materializable_artifacts`;
- materializer counters distinguish collector-reported reuse (`collector_reuse_count` / legacy `workbook_reuse_count`) from archive artifacts validated as usable (`validated_workbook_usable_count` / `materializer_usable_count` / `materializable_campaigns`);
- status-aware validation also surfaces `ui_status_counts`, `download_action_state_counts`, `ended_without_download_count`, `metadata_only_true_artifact_loss_count`, `true_artifact_loss_count`, `fatal_missing_artifact_count`, `excluded_non_materializable_campaign_count`, `covering_campaigns`, `usable_campaigns`, `requested_count`, `covered_count` and `non_materializable_expected_count`;
- collector preflight diagnostics additionally surface `opened_drawer_count`, `shallow_status_checked_count`, `deep_workbook_flow_count`, `early_ended_no_download_count`, `early_non_materializable_count`, `unknown_status_full_flow_count`, `active_downloadable_full_flow_count`, `download_attempt_count`, `generate_screen_attempt_count`, `heavy_flow_avoided_count`, `estimated_heavy_flow_avoided_count`, `early_preflight_duration_ms`, `deep_flow_duration_ms`, timeline counters (`timeline_card_seen_count`, `timeline_status_classified_count`, `drawer_open_avoided_count`, `drawer_open_required_count`, `timeline_unknown_full_flow_count`, `timeline_non_materializable_count`, `timeline_shallow_duration_ms`, `drawer_open_duration_ms`) and compact `collector_preflight_campaigns`;
- campaign manifest diagnostics additionally surface `manifest_campaign_seen_count`, `manifest_timeline_match_count`, `manifest_match_low_confidence_count`, `manifest_missing_for_card_count`, `manifest_status_classified_count`, `manifest_downloadability_classified_count`, `manifest_drawer_avoid_count`, `manifest_drawer_required_count`, `manifest_unknown_full_flow_count`, `manifest_low_confidence_full_flow_count`, `manifest_load_duration_ms`, `manifest_parse_duration_ms`, `manifest_match_duration_ms`, and per-campaign manifest fields in `collector_preflight_campaigns`;
- high-confidence timeline-ended cards may avoid drawer opening when sanitized timeline evidence includes ended status, title evidence and period evidence; this is path control only, not metric/data truth;
- high-confidence campaign manifest ended/non-downloadable cards may avoid drawer opening when the visible timeline card matches manifest title+period, the manifest lifecycle is high-confidence `ended`, and manifest downloadability/materializability is high-confidence `not_available`; this is path control only, not metric/data truth;
- high-confidence drawer-level `ended` + absent/disabled download campaigns may still avoid the deep workbook generate/download path after opening the card/drawer;
- active/downloadable, future/pending archive-policy, missing/no manifest, low-confidence manifest match, unknown manifest status, UI-not-loaded, identity-mismatch and unclear download states retain conservative drawer/full-flow behavior;
- эти diagnostics не являются data truth, не меняют source fetch policy, acceptance/fallback semantics, temporal policy, retry behavior, Google Sheets/GAS archive boundary или browser/localStorage truth;
- browser collector now emits aggregate preflight/deep-flow timing, but selector-level browser timings stay an explicit observability gap until a separate adapter refactor.

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
- workbook file physically exists, is non-empty, has mtime/size and fingerprint evidence, and can be opened for plan-price sheet inspection during materialization; or normalized `campaign_rows.jsonl` + `campaign_rows_manifest.json` exist with matching workbook/metadata fingerprints, row count and schema version;
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

If collector metadata has high-confidence ended evidence from loaded drawer/card evidence (campaign/title match plus absent/disabled download action), from high-confidence timeline evidence (`timeline_non_materializable_expected`, `drawer_opened=false`, ended label/title/period evidence), or from high-confidence read-only campaign manifest evidence (`manifest_decision=drawer_avoid_manifest_non_materializable`, timeline title+period match, ended lifecycle, not-available downloadability/materializability), the materializer may classify a metadata-only campaign as `ended_without_download` with `workbook_required=false` and reason `metadata_only_ended_without_download`. This remains non-materializable and must not be treated as fresh upstream success. It is excluded from fatal missing-artifact gating, remains visible in diagnostics, and does not materialize rows; usable complete campaigns still materialize rows for the same requested date. If all covering campaigns are expected non-materializable and no complete workbook can materialize rows, the source returns safe `incomplete`/blank rather than fake zero-success. Unknown/low-confidence UI or manifest status keeps the conservative fatal missing-artifact path, with true metadata-only loss surfaced as `metadata_only_true_artifact_loss`.

Invalid or incomplete current artifacts still produce `PromoLiveSourceIncomplete`; temporal policy continues to preserve accepted current/closed truth rather than writing fake zeros/blanks or accepting low-confidence dates. `apps/promo_campaign_archive_integrity_smoke.py` is a dry-run audit fixture: it counts archive artifact states and examples without deleting, repairing, downloading, or changing runtime accepted truth.

Retention / GC guard:
- `apps/promo_campaign_archive_gc.py audit` is read-only and reports runtime/archive/run sizes.
- `apps/promo_campaign_archive_gc.py dry-run` builds a deletion plan without deleting.
- `apps/promo_campaign_archive_gc.py apply --confirm` deletes only guarded candidates from the structured plan; it never removes archive records, metadata, normalized rows/manifests, exact-date runtime snapshots or unknown/incomplete parse artifacts.
- Safe workbook deletion is limited to duplicate workbook copies after hash proof and normalized row archive proof. Old successful HAR/screenshots/request logs may be planned after TTL; failed traces use a longer TTL and keep compact summaries.

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
- live/public invariant smoke:
  - `apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`

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
- Live/public invariant smoke подтверждает на already refreshed public payload:
  - public `status`, `web-vitrina` и `plan` routes доступны read-only;
  - `metadata.refresh_diagnostics.source_slots[]` содержит `promo_by_price[today_current]` без fatal expected non-materializable artifacts;
  - `fatal_missing_artifact_count=0` и `true_artifact_loss_count=0`, когда эти counters представлены;
  - ended/no-download artifacts, including campaign `2242` when present, остаются `workbook_required=false` diagnostics и не входят в `fatal_missing_artifacts`;
  - current promo metric rows are present in public `web-vitrina` and are not all blank, while truthful zero rows for ineligible SKU remain valid.
- Required checklist rule: run `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` after changes touching `promo_by_price` materialization, promo archive/artifact validation, promo collector diagnostics/status handling, `ended_without_download` / expected non-materializable campaign handling, `sheet_vitrina_v1` refresh orchestration, temporal source acceptance/fallback around promo, promo source-status reduction, web-vitrina read/page-composition paths that affect promo row visibility, or hosted deploys where current promo correctness must be verified.
- Retention changes must additionally pass `python3 apps/promo_campaign_archive_gc_smoke.py` and a GC `dry-run` on the intended runtime before any live `apply`.
- If local CA verification blocks the live read while the route is otherwise reachable, the accepted local-only fallback is `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`; timeout, non-200 route status or bad payload remains a real blocker.
- This smoke is read-only and must not be replaced by `/v1/sheet-vitrina-v1/load`, Google Sheets/GAS, browser `localStorage` truth or a runtime refresh unless a separate task explicitly requires a controlled refresh.
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
