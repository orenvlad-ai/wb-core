---
title: "Runbook: common smoke and debug"
doc_id: "WB-CORE-PROJECT-09-RUNBOOK"
doc_type: "runbook"
status: "active"
purpose: "Дать компактный набор частых smoke/debug команд для `wb-core` без погружения во все artifacts и module docs."
scope: "Registry upload chain, current server/web-vitrina flow, legacy sheet/export checks, common failure signatures и минимальные debug entrypoints."
source_basis:
  - "README.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
  - "apps/sheet_vitrina_v1_business_time_smoke.py"
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
  - "apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py"
  - "apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_read_split_smoke.py"
  - "apps/sheet_vitrina_v1_operator_load_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_group_refresh_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_source_status_smoke.py"
  - "apps/sheet_vitrina_v1_popup_outside_click_browser_smoke.py"
  - "apps/sheet_vitrina_v1_plan_report_smoke.py"
  - "apps/sheet_vitrina_v1_plan_report_http_smoke.py"
  - "apps/sheet_vitrina_v1_ready_fact_reconcile_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_http_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_ai_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_browser_smoke.py"
  - "apps/sheet_vitrina_v1_research_sku_group_comparison_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
  - "apps/promo_xlsx_collector_contract_smoke.py"
  - "apps/promo_xlsx_collector_integration_smoke.py"
  - "apps/promo_campaign_archive_integrity_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py"
  - "apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py"
source_of_truth_level: "derived_secondary_project_pack"
related_paths:
  - "apps/"
  - "gas/sheet_vitrina_v1/"
  - "artifacts/"
update_triggers:
  - "изменение smoke runner"
  - "изменение live operator flow"
  - "изменение common failure signature"
built_from_commit: "863184041a619b3a940f94c38d60e0dfce6bc6d9"
---

# Summary

Этот runbook нужен для быстрого ответа на вопросы:
- broken ли registry upload chain;
- broken ли current server/web-vitrina flow;
- broken ли archived guard для legacy sheet/export wiring, если scope действительно затрагивает bound Apps Script guard;
- где искать first useful signal.

# Current norm

## Core local smokes

```bash
python3 apps/registry_upload_bundle_v1_smoke.py
python3 apps/registry_upload_file_backed_service_smoke.py
python3 apps/registry_upload_db_backed_runtime_smoke.py
python3 apps/registry_upload_http_entrypoint_smoke.py
python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py
python3 apps/cost_price_upload_http_entrypoint_smoke.py
python3 apps/official_api_token_path_smoke.py
python3 apps/sales_funnel_history_block_batching_smoke.py
python3 apps/factory_order_sales_history_smoke.py
python3 apps/sheet_vitrina_v1_business_time_smoke.py
python3 apps/stocks_block_smoke.py
python3 apps/stocks_block_region_mapping_smoke.py
python3 apps/stocks_block_batching_smoke.py
python3 apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py
python3 apps/sheet_vitrina_v1_cost_price_upload_smoke.py
python3 apps/sheet_vitrina_v1_cost_price_read_side_smoke.py
python3 apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py
python3 apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py
python3 apps/sheet_vitrina_v1_refresh_read_split_smoke.py
python3 apps/sheet_vitrina_v1_operator_load_smoke.py
python3 apps/factory_order_supply_smoke.py
python3 apps/sheet_vitrina_v1_factory_order_http_smoke.py
python3 apps/web_source_current_sync_zero_snapshot_smoke.py
python3 apps/web_source_current_sync_closed_day_freshness_smoke.py
python3 apps/sheet_vitrina_v1_seller_funnel_zero_payload_smoke.py
python3 apps/sheet_vitrina_v1_web_source_current_sync_smoke.py
python3 apps/sheet_vitrina_v1_current_snapshot_acceptance_smoke.py
python3 apps/sheet_vitrina_v1_temporal_closure_retry_smoke.py
python3 apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py
python3 apps/sheet_vitrina_v1_stocks_refresh_smoke.py
python3 apps/sheet_vitrina_v1_auto_update_smoke.py
python3 apps/sheet_vitrina_v1_daily_report_smoke.py
python3 apps/sheet_vitrina_v1_daily_report_http_smoke.py
python3 apps/sheet_vitrina_v1_stock_report_smoke.py
python3 apps/sheet_vitrina_v1_plan_report_smoke.py
python3 apps/sheet_vitrina_v1_plan_report_http_smoke.py
python3 apps/sheet_vitrina_v1_reports_ui_smoke.py
python3 apps/sheet_vitrina_v1_ready_fact_reconcile_smoke.py
python3 apps/sheet_vitrina_v1_feedbacks_http_smoke.py
python3 apps/sheet_vitrina_v1_feedbacks_ai_smoke.py
python3 apps/sheet_vitrina_v1_feedbacks_browser_smoke.py
python3 apps/sheet_vitrina_v1_research_sku_group_comparison_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_http_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py
python3 apps/sheet_vitrina_v1_popup_outside_click_browser_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_current_tail_browser_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_source_status_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_group_coverage_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_group_refresh_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_group_action_ui_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_highlight_ui_smoke.py
python3 apps/sheet_vitrina_v1_operator_ui_persistence_smoke.py
python3 apps/sheet_vitrina_v1_seller_funnel_relevant_filter_smoke.py
python3 apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py
python3 apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py
python3 apps/promo_xlsx_collector_contract_smoke.py
python3 apps/promo_xlsx_collector_integration_smoke.py
python3 apps/promo_campaign_archive_integrity_smoke.py
python3 apps/promo_campaign_archive_gc_smoke.py
python3 apps/sheet_vitrina_v1_refresh_promo_artifact_gc_smoke.py
python3 apps/sheet_vitrina_v1_promo_live_source_smoke.py
python3 apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py
python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py
git diff --check
```

Current promo smoke intent:
- `apps/sheet_vitrina_v1_promo_live_source_smoke.py` now additionally proves historical interval replay fills the exact-date promo seam on `yesterday_closed` cache miss.
- `apps/promo_campaign_archive_integrity_smoke.py` proves normalized promo replay still works after raw workbook is hidden in fixture.
- `apps/promo_campaign_archive_gc_smoke.py` covers guarded audit/dry-run/apply fixture behavior, duplicate/debug cleanup candidates and unknown/incomplete artifact skips.
- `apps/sheet_vitrina_v1_refresh_promo_artifact_gc_smoke.py` proves refresh-integrated light GC runs after normalized archive/ready snapshot persistence and surfaces warnings without turning successful data refresh into data loss.
- `apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` is the read-only live/public guard for `promo_by_price[today_current]`, expected ended/no-download diagnostics and non-blank current promo rows.

Current web-vitrina phase-2 smoke intent:
- `apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py` keeps the mapper library-agnostic and checks canonical `columns / rows / groups / sections / formatters / filters / sorts / state_model`.
- `apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py` proves the typed Phase 1 contract builder still feeds the same view-model seam without changing public routes or deploy requirements.

Current web-vitrina phase-3 smoke intent:
- `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py` keeps Gravity-specific config/data/render hints isolated in the adapter layer and checks sticky/sizing/sort/filter/useTable/state invariants without changing `view_model`.
- `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py` proves the full seam `contract -> view_model -> gravity adapter` and keeps per-cell renderer bindings authoritative for mixed temporal columns.

Current web-vitrina phase-4 smoke intent:
- `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py` keeps page composition server-driven and checks source-chain metadata, compact filter/toolbar surface, row counts and ready/error state behavior without changing the default read contract.
- `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py` proves the real sibling page on `/sheet-vitrina-v1/vitrina`: table render, compact toolbar/history selector, lazy source-status details flow, empty state, reset recovery and truthful error state all run through the same HTTP contour.
- `apps/sheet_vitrina_v1_web_vitrina_source_status_smoke.py` proves source-aware loading-table reduction for accepted-current rollover, runtime-cache/latest-confirmed fallback, non-required slots and promo fallback.
- `apps/sheet_vitrina_v1_web_vitrina_group_coverage_smoke.py`, `apps/sheet_vitrina_v1_web_vitrina_group_refresh_smoke.py` and `apps/sheet_vitrina_v1_web_vitrina_group_action_ui_smoke.py` prove grouped loading-table coverage, lazy details empty/error behavior, date-scoped `group-refresh` payload semantics and visible launch failure handling.
- `apps/sheet_vitrina_v1_web_vitrina_highlight_ui_smoke.py` keeps `updated_cells` highlighting browser-session-only across full refresh and group refresh.
- `apps/sheet_vitrina_v1_popup_outside_click_browser_smoke.py` keeps custom floating controls closeable by outside-click/`Escape` without breaking checkbox/date-range first-click behavior.
- `apps/sheet_vitrina_v1_feedbacks_http_smoke.py`, `apps/sheet_vitrina_v1_feedbacks_ai_smoke.py` and `apps/sheet_vitrina_v1_feedbacks_browser_smoke.py` cover the read-only `Отзывы` route/table plus transient server-side prompt/analyze flow.
- `apps/sheet_vitrina_v1_research_sku_group_comparison_smoke.py` covers read-only research options/calculate semantics, non-financial metric filtering, promo candidate chip metadata and no zero-fill coverage behavior.

Current reports smoke intent:
- `apps/sheet_vitrina_v1_stock_report_smoke.py` checks previous-closed stock report semantics and active SKU filtering.
- `apps/sheet_vitrina_v1_plan_report_smoke.py` and `apps/sheet_vitrina_v1_plan_report_http_smoke.py` check read-only plan execution calculations, H1/H2 plan params, per-block coverage, contract-start clipping, manual monthly baseline and public route wiring.
- `apps/sheet_vitrina_v1_reports_ui_smoke.py` checks the reports tab, stock selector persistence, baseline controls and plan-report controls.
- `apps/sheet_vitrina_v1_ready_fact_reconcile_smoke.py` checks bounded one-off report fact reconcile from persisted ready snapshots into missing accepted temporal slots without overwrites or fake zeros.

Targeted expectation for `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`:
- same-day incoming blank cell in server-owned `DATA_VITRINA` plan must clear the live-sheet cell instead of preserving a stale historical value or stale zero.

## Factory-order historical reconcile helpers

```bash
python3 apps/factory_order_sales_history_reconcile.py \
  extract-live-data-vitrina-window \
  --output /tmp/factory_order_sales_history_window.json

python3 apps/factory_order_sales_history_reconcile.py \
  diff-runtime-window \
  --runtime-dir <runtime_dir> \
  --input /tmp/factory_order_sales_history_window.json

python3 apps/factory_order_sales_history_reconcile.py \
  reconcile-runtime-window \
  --runtime-dir <runtime_dir> \
  --input /tmp/factory_order_sales_history_window.json
```

Norm:
- `DATA_VITRINA` is only a one-time migration input for the bounded history window, not a new permanent source of truth.
- authoritative target seam for factory-order history = `temporal_source_snapshots[source_key=sales_funnel_history]`.
- use `diff-runtime-window` before a live replacement when runtime access is available; use `reconcile-runtime-window` only after divergence is understood.

## Plan-report ready-fact reconcile helper

```bash
python3 apps/sheet_vitrina_v1_ready_fact_reconcile.py dry-run \
  --runtime-dir <runtime_dir> \
  --date-from 2026-03-01 \
  --date-to 2026-04-24

python3 apps/sheet_vitrina_v1_ready_fact_reconcile.py apply \
  --runtime-dir <runtime_dir> \
  --date-from 2026-03-01 \
  --date-to 2026-04-24
```

Norm:
- this is a bounded one-off repair path for report facts, not a recurring source;
- input truth must already be server-side persisted `sheet_vitrina_v1_ready_snapshots`;
- target truth is only missing `accepted_closed_day_snapshot` slots for `fin_report_daily.fin_buyout_rub` and `ads_compact.ads_sum`;
- existing accepted snapshots with diffs are not overwritten;
- blank ready values are not converted to zeros;
- run dry-run first and apply only after insert/skip/diff output is understood.

## Live local runner

```bash
python3 apps/registry_upload_http_entrypoint_live.py
python3 apps/promo_xlsx_collector_live.py --max-candidates 5
```

Current promo collector norm:
- use the local runner only as bounded local contour against existing session reuse path;
- canonical hydration entry = direct open `dp-promo-calendar` -> wait/click `Принимаю` -> wait hydrated DOM -> optional auto-promo modal close;
- canonical inter-promo reset = click `#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-close-button-button-ghost"]` -> wait until `#Portal-drawer [data-testid="pages/main-page/promo-action-wizard/drawer-drawer-overlay"]` disappears -> only then next promo click;
- `metadata.json` is mandatory for all promo because workbook alone does not carry promo-level truth.

Current promo live-source wiring norm:
- authoritative live source key = `promo_by_price`
- `today_current` runs bounded repo-owned promo collector server-side inside refresh contour
- `yesterday_closed` attempts corrective interval replay first and falls back to accepted/runtime-cached promo truth only when replay is unavailable or non-exact
- invalid later attempt must not overwrite accepted current/closed promo truth
- promo candidate/eligible metric split is canonical: `promo_participation` and `promo_count_by_price` are computed from eligible rows where runtime `price_seller_discounted < Плановая цена для акции`; `promo_entry_price_best` is computed as max plan price across active candidate rows, so ineligible SKUs can truthfully have participation/count `0` and entry price `>0`
- missing runtime seller price must not fake-positive participation/count, but must not hide candidate-derived `promo_entry_price_best` when active candidate plan prices exist
- collector timeline/manifest/drawer preflight and artifact validation diagnostics are observability-only; high-confidence ended/no-download campaigns may be non-materializable without becoming fatal missing artifacts.

## Hosted runtime contract

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py print-plan
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy --dry-run
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe --as-of-date AUTO_YESTERDAY
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe --as-of-date AUTO_YESTERDAY
SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 \
  python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe --as-of-date AUTO_YESTERDAY
```

Norm:
- `public-probe` for current live uses the HTTPS production URL `https://api.selleros.pro`;
- the runner should use secure verification first, including system-CA fallback for local trust-store gaps;
- `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1` is diagnostic-only and must not redefine the production contour as insecure.

Required local env for the runner itself:
- `WB_CORE_HOSTED_RUNTIME_TARGET_FILE`
- optional `WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE`
- optional `WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS`
- optional `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1` only when local trust store cannot verify current selleros certificate chain

Canonical target template:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json`
- current EU live target:
  - `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json`
  - `ssh_destination = wb-core-eu-root`
  - `host_ip = 89.191.226.88`
  - `public_domain = api.selleros.pro`
  - `public_base_url = https://api.selleros.pro`
  - `server_names = ["89.191.226.88", "api.selleros.pro"]`
  - TLS must be enabled with LetsEncrypt cert/key paths under `/etc/letsencrypt/live/api.selleros.pro/`
  - `runtime_dir = /opt/wb-core-runtime/state`
  - `service_name = wb-core-registry-http.service`
- rollback-only old selleros target:
  - `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json`
  - `ssh_destination = selleros-root`
  - `legacy_host_ip = 178.72.152.177`
  - routine mutating actions fail fast unless `WB_CORE_ALLOW_ROLLBACK_TARGET_WRITE=I_UNDERSTAND_SELLEROS_IS_ROLLBACK_ONLY`
- repo-owned systemd artifacts:
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.service`
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.timer`
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.service`
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.timer`
- repo-owned nginx public route allowlist:
  - `artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json`

Current-live nginx invariant:
- rendered nginx must include `server_name 89.191.226.88 api.selleros.pro;`;
- rendered nginx must include `listen 443 ssl`;
- losing `api.selleros.pro` or `443 ssl` is a production outage, not acceptable drift;
- `deploy`, `deploy-and-verify` and `apply-nginx-routes` must fail locally before SSH/rsync/nginx/systemd mutation if this invariant is broken.

Current canonical WB secret path for official adapters:
- `WB_API_TOKEN`
- keep live service/env aligned to one canonical WB token path before calling a live task complete

Current feedbacks AI secret/env path:
- `OPENAI_API_KEY`
- optional `OPENAI_MODEL`, `OPENAI_API_BASE_URL`, `OPENAI_TIMEOUT_SECONDS`
- AI feedback labels are transient operator output, not accepted truth or ЕБД persistence

Current promo runtime env override when hosted runtime needs explicit seller session path:
- `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`
- canonical selleros value = `/opt/wb-web-bot/storage_state.json`

Current promo archive/runtime norm:
- promo collector is archive-first: unchanged campaigns reuse archived workbook artifacts instead of redownloading every Excel
- historical `promo_by_price[yesterday_closed]` may be truthfully filled from campaign interval replay into exact-date runtime seam when archive coverage exists
- uncovered dates must stay blank/incomplete; no fake sheet-side backfill is allowed
- campaign rows are normalized into `campaign_rows.jsonl` plus manifest/fingerprint/source metadata so replay does not require indefinite raw workbook retention
- refresh automatically runs bounded `promo_refresh_light_gc_v1` after normalized archive + ready snapshot persistence; unknown/current/replay-critical files are skipped, not deleted

Current hosted runtime dependency note for promo live wiring:
- hosted `deploy` now also ensures `openpyxl==3.1.5` and `playwright==1.58.0` on the remote system python before restarting `wb-core-registry-http.service`;
- browser binaries remain an existing external host contour expectation and are not installed by `wb-core` deploy;
- if deploy still fails before HTTP probes, first inspect `journalctl -u wb-core-registry-http.service` for import-time dependency drift instead of treating it as an unspecified runtime outage.

Current canonical business timezone for server-side `sheet_vitrina_v1` date math:
- `Asia/Yekaterinburg`
- default `as_of_date` = previous business day in `Asia/Yekaterinburg`
- `today_current` / current-only freshness = current business day in `Asia/Yekaterinburg`

Expected routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `POST /v1/sheet-vitrina-v1/load` (archived/blocked)
- `GET /v1/sheet-vitrina-v1/daily-report`
- `GET /v1/sheet-vitrina-v1/stock-report`
- `GET /v1/sheet-vitrina-v1/plan-report`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx`
- `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-status`
- `GET /v1/sheet-vitrina-v1/feedbacks`
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/job`
- `GET /v1/sheet-vitrina-v1/seller-portal-session/check`
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh`
- `GET /sheet-vitrina-v1/operator`
- `GET /sheet-vitrina-v1/vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&include_source_status=1`
- `GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options`
- `POST /v1/sheet-vitrina-v1/research/sku-group-comparison/calculate`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status`
- `POST /v1/sheet-vitrina-v1/seller-portal-recovery/start`
- `POST /v1/sheet-vitrina-v1/seller-portal-recovery/stop`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/calculate`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status`
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/calculate`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district}.xlsx`

Temporal closure retry runner:

```bash
python3 apps/sheet_vitrina_v1_temporal_closure_retry_live.py --date 2026-04-17 --date 2026-04-18
```

Norm:
- use the retry runner for due `yesterday_closed` states across the full historical/date-period matrix (`seller_funnel_snapshot`, `web_source_snapshot`, `sales_funnel_history`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`) and for same-day current-only capture retries only within the current business day;
- `today_current` may remain incomplete and blank, but `yesterday_closed` must not silently inherit provisional same-day values;
- group A bot/web-source families accept closed truth only after exact-date sync + source freshness validation (`source_fetched_at` / `fetched_at` after next business-day start in `Asia/Yekaterinburg`);
- group C current-snapshot-only sources (`prices_snapshot`, `ads_bids`) still capture upstream truth only as current snapshot, but an already accepted snapshot for closed business day D must materialize as `yesterday_closed=D` on D+1; later invalid/blank/zero auto or manual attempts must preserve both prior-day accepted truth and any already accepted same-day truth;
- `stocks` is now `yesterday_closed_only` inside `sheet_vitrina_v1`: required slot = `yesterday_closed` from exact-date runtime cache `temporal_source_snapshots[source_key=stocks]`, while `today_current` may truthfully stay `not_available`/blank and must not degrade source/aggregate status by itself;
- `spp` and `fin_report_daily` stay requestable on `today_current`, but intraday empty/zero/invalid/no-result/429/timeout/runtime-cache current fallback is tolerated when `yesterday_closed` is confirmed;
- manual operator refresh keeps short retries inside the run, but must not leak persisted due retry states into this runner path.

One-off stocks backfill runner:

```bash
python3 apps/sheet_vitrina_v1_stocks_historical_backfill.py \
  --date-from 2026-03-01 \
  --date-to 2026-04-18
```

## Archived Google Sheets/GAS contour

Legacy Google Sheets/GAS contour is `ARCHIVED / DO NOT USE`.

Do not use Google Sheets, GAS, `clasp`, `/v1/sheet-vitrina-v1/load`, `loadSheetVitrinaTable`, sheet readback, or `invalid_grant` as active completion blockers for current website/operator/web-vitrina work.

Allowed references:
- archive/migration boundary;
- explicit decommission guard verification;
- historical evidence.

Current verification targets:
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`
- `GET /sheet-vitrina-v1/vitrina`
- `GET /sheet-vitrina-v1/operator`
- server-side refresh/status/read probes.

For current hosted/public website tasks, these checks use `https://api.selleros.pro`; IP-only HTTP probes are not sufficient production verification.

If a task explicitly changes archived GAS guard code, closure is `clasp push` plus a guard-only check that archived functions fail fast. Do not run former prepare/upload/load flows as success criteria.

## GitHub PR closure

```bash
gh auth status -h github.com
gh pr ready <pr_number>
gh pr edit <pr_number> --base <base_branch>
gh pr merge <pr_number> --merge --delete-branch
```

Operational rule:
- сначала проверять `gh auth status -h github.com`;
- если requested outcome по смыслу включает Git fixation или GitHub closure и пользователь явно не запретил Git/GitHub actions, эти шаги входят в тот же bounded execution;
- если auth валиден, `gh` доступен и execution context имеет repo write/merge access, обычные `commit`, `push`, `ready`, `retarget`, `merge`, `delete branch` являются Codex-owned routine;
- это одинаково относится и к stacked PR sequence, где merge идёт не в `main`, а в промежуточную base branch;
- auto-merge optional и не заменяет обычный merge для такого sequence;
- при working auth/access Codex обязана довести ordinary GitHub closure до merge + delete-branch;
- manual merge допустим только как fallback-blocker case: нет `gh`, нет auth, недостаточные scopes/permissions, GitHub вернул write blocker или branch protection требует human approval.

## Post-change closure

### Repo-only closure for repo-only scope

- проверить scope diff и `git diff --check`;
- прогнать targeted local smoke / integration smoke по затронутому bounded path;
- использовать этот closure только там, где scope реально repo-only;
- не объявлять задачу complete, если для неё по смыслу нужен live/public closure. Former sheet success closure is archived; GAS closure now means archive-guard push/verify only.

### Docs-governance closure

- если change ограничен governance/docs/pack rules, не придумывать fake deploy / `clasp` / sheet verify steps;
- обновить затронутые authoritative docs, если truth изменился;
- не обновлять `wb_core_docs_master/**` и manifest по умолчанию в ordinary task-flow;
- обновлять `wb_core_docs_master/**` и manifest только если task явно является derived-sync flow или transitional pack rebuild;
- проверить scope diff и `git diff --check`;
- закрыть GitHub closure до merge + delete-branch, если access работает;
- для explicit derived-sync/transitional pack rebuild после merge привести `~/Projects/wb-core` к current `origin/main` и проверить `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md` как upload-ready source;
- для explicit derived-sync/transitional pack rebuild оставить один human-only remainder: внешний upload актуального pack.

### Live route/runtime closure

- если change затрагивает public HTTP route, runtime/service wiring или nginx/proxy publish, после repo update нужно закрыть и live contour;
- минимальная норма:
  - обновить existing live runtime через canonical runner `deploy` или equivalent bounded path;
  - перезапустить/reload нужный process/service через canonical `restart_command` или live-owned equivalent;
  - если change затрагивает daily refresh semantics, обновить и timer wiring;
  - проверить route на loopback/runtime contour через `loopback-probe` или equivalent probe;
  - проверить route снаружи через public URL через `public-probe` или equivalent probe;
- для текущей web-витрины final verify = server/public web surface:
  - `GET /v1/sheet-vitrina-v1/web-vitrina`
  - `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`
  - `GET /sheet-vitrina-v1/vitrina`
  - `GET /sheet-vitrina-v1/operator`
  - public URL must be `https://api.selleros.pro`, not IP-only HTTP;
  - content-level verify по affected SKU/date rows в payload/table surface;
  - Google Sheets / GAS / `clasp` / `invalid_grant` не входят в active completion path.
  - current live `sheet_vitrina_v1` contour:
  - service = `wb-core-registry-http.service`
  - timer = `wb-core-sheet-vitrina-refresh.timer`
  - schedule = `11:00, 20:00 Asia/Yekaterinburg` = `06:00 UTC, 15:00 UTC` in current systemd host timezone
  - daily timer target = `POST /v1/sheet-vitrina-v1/refresh` with `{"auto_refresh": true}`; it builds server-side ready snapshots only and never auto-loads Google Sheets
- current bounded `factory-order` supply contour is server/operator-only:
  - live closure still requires deploy + loopback/public probe + one controlled download/upload/calculate/download scenario if those routes changed;
  - the same closure rule now covers the sibling regional block under `Расчёт поставок`: shared `stock_ff` upload lifecycle, regional calculate, summary table and per-district XLSX routes are part of the same operator/public contour;
  - sheet/GAS write verify stays out of scope; archived GAS changes require only guard push/verify.
  - if the task changes upload state handling, closure additionally verifies auto-upload-after-file-pick plus `upload -> current uploaded file download -> delete -> absent state`;
  - for inbound upload contract changes, controlled live scenario must cover both mixed positive/zero rows and zero-only files, and status must truthfully expose accepted empty datasets as `row_count = 0`;
  - current UI may accept any positive `sales_avg_period_days`; backend now uses persisted runtime coverage instead of fixed `<= 7`, so covered windows such as `10 / 14 / 21` must succeed after truthful reconcile/backfill.
  - if the task changes operator defaults or supply vocabulary, probe/operator smokes must also confirm the current field values and labels (`Цикл заказов`, `Цикл поставок`, batch label, lead-time labels) on page load;
  - bounded historical reconcile may use live `DATA_VITRINA` only as migration input for window `2026-03-01..2026-04-18`; ongoing truth remains server-side in `temporal_source_snapshots[source_key=sales_funnel_history]`.
  - out-of-range windows must fail with the exact requested range plus earliest/latest available runtime coverage, not with a fake upstream-depth surrogate.
  - XLSX fixes are not considered complete until generated/publicly downloaded files pass bounded integrity checks and open as standard XLSX workbooks without a recovery path.
- route change не считается complete, пока public probe не подтвердил expected content type / response shape.
- для regional supply verify минимум включает:
  - shared `Остатки ФФ` status/download/delete reuse between factory and district block;
  - truthfully blocked `422`, если shared `stock_ff` отсутствует;
  - district summary totals = sum of generated district XLSX rows;
  - public `GET /v1/sheet-vitrina-v1/supply/wb-regional/status` и `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district}.xlsx` surface expected JSON/XLSX shape.
- если change затрагивает archived bound Apps Script guard, closure дополнительно требует `clasp push` и guard-only verify. Former `POST /v1/sheet-vitrina-v1/load` / Apps Script menu write flow must return archived/blocked and must not be treated as a success path.
- если runner уже materialized, но `ssh_destination / target_dir / service_name / restart_command / environment_file` или access отсутствуют, это фиксируется как точный blocker, а не как vague ops-gap.

### GAS/sheet archive closure

- если change затрагивает archived bound Apps Script files, default closure включает `clasp push`, если он безопасен и доступен;
- после `clasp push` verify должен подтверждать archive guard:
  - `getLegacyGoogleSheetsArchiveStatus` returns inactive/disabled status;
  - former write/upload/load functions fail fast with archive message;
  - no sheet write/readback is required or allowed as completion proof.

### Derived-pack closure

- применять только когда task явно является derived-sync flow или transitional pack rebuild;
- перед pack rebuild authoritative docs должны уже отражать current truth;
- пересобрать затронутый `wb_core_docs_master` как compact derived retrieval pack;
- обновить manifest как build-metadata only, без operational upload-state;
- проверить governance smoke и contamination smoke;
- после merge привести `~/Projects/wb-core` к current `origin/main`;
- проверить readiness по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`;
- в финальном handoff оставить один human-only шаг: после merge загрузить актуальный pack во внешний Project.

## What to verify in current web/operator contour

Use this section for current website/operator/public verification. Legacy Google Sheets sections are archived and not a completion checklist.

- `POST /v1/sheet-vitrina-v1/refresh` builds server-side ready snapshots without `auto_load`;
- `POST /v1/sheet-vitrina-v1/refresh` with `auto_load=true` is rejected as archived legacy Google Sheets target;
- `POST /v1/sheet-vitrina-v1/load` returns archived/blocked and does not write Google Sheets;
- `GET /sheet-vitrina-v1/operator` does not expose Google Sheets send as an active action;
- `GET /v1/sheet-vitrina-v1/status` exposes archived `legacy_google_sheets_contour` in load context;
- current COST_PRICE checkpoint проверяется по accepted/rejected server upload result, separate runtime current state и server-side refresh/read integration;
- applicable себестоимость резолвится server-side по `group + latest effective_from <= slot_date`;
- operator-facing derived rows используют canonical keys `total_proxy_profit_rub` и `proxy_margin_pct_total`;
- `GET /sheet-vitrina-v1/vitrina` поднимает primary unified web/operator page без SPA/build pipeline: first/default tab `Витрина`, sibling tabs `Расчет поставок`, `Отчеты`, `Отзывы` and `Исследования`;
- `GET /sheet-vitrina-v1/operator` остаётся compatibility entry и рендерит тот же unified shell, а не отдельный truth owner;
- `GET /v1/sheet-vitrina-v1/web-vitrina` остаётся cheap read-only JSON path: default v1 shape = `contract_name / contract_version / page_route / read_route / meta / status_summary / schema / rows / capabilities`, optional `as_of_date` stays on том же route и не имеет права trigger-ить refresh/upstream fetch;
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition` now adds the page-only payload for `/sheet-vitrina-v1/vitrina`: `composition_name / composition_version / meta / summary_cards / filter_surface / table_surface / status_summary / capabilities`; default page-composition keeps source-status details unloaded, route still stays read-only and must not trigger refresh/upstream fetch;
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&include_source_status=1` returns the detailed grouped `Загрузка данных` payload for an explicit details request; it must use server-owned `snapshot_as_of_date`, expose `source_status_state`, and must not infer date from browser-local today or the rightmost `today_current` column;
- vitrina page shows primary action `Загрузить и обновить`; old top-panel `Обновить`, `JSON Connect` and permanent top status badge are not active current UI.
- bottom `Действия и состояния` contains server-driven lazy `Загрузка данных`: initial `not_loaded` + `Загрузить`, then grouped table (`WB API`, `Seller Portal / бот`, `Прочие источники`) after explicit details load, plus secondary `Лог`; former sibling block `Обновление данных` is not active page-composition UI.
- compact toolbar above the table owns `Диапазон`, `Поиск`, `Секции`, `Группа`, `Scope`, `Метрики`, `Столбцы`, `Сортировка`; the old always-expanded `История` and separate `Фильтры и настройки` card are not default page sections.
- no-query page-composition opens latest four server-readable business dates inclusive, ending on backend-owned `today_current_date` when available; explicit `as_of_date` and `date_from/date_to` remain read-only ready-snapshot reads.
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` must reach app-level validation. A request without `source_group_id` returns app-level `400 {"error":"source_group_id is required"}`, not proxy `404`.
- valid group-refresh payload `{async: true, source_group_id, as_of_date}` creates a group/date-scoped job and must not clear, overwrite or timestamp unrelated groups/date cells; `updated_cells` metadata drives only transient browser-session highlighting.
- `GET /v1/sheet-vitrina-v1/daily-report` остаётся cheap read-only JSON path: route сравнивает только два последних closed business day через persisted ready snapshots `default_business_as_of_date(now)` и `default_business_as_of_date(now)-1 day` и не имеет права trigger-ить refresh/upstream fetch;
- `GET /v1/sheet-vitrina-v1/stock-report` остаётся cheap read-only JSON path: route по умолчанию читает previous closed business day only from persisted ready snapshot `DATA_VITRINA[yesterday_closed]`, принимает optional explicit `as_of_date` override на том же read path, не trigger-ит refresh/upstream fetch и включает только SKU с district stock `< 50`;
- subsection `Отчёт по остаткам` now adds a compact SKU selector: full active SKU list comes from current authoritative `config_v2` truth on the operator page itself, defaults to all selected, applies only after `Рассчитать`, rejects empty selection with `Выберите хотя бы один SKU` and must show an empty result instead of stale rows when the selected subset has no breaches;
- `GET /v1/sheet-vitrina-v1/plan-report` остаётся cheap read-only JSON path: primary valid query includes `period`, `h1_buyout_plan_rub`, `h2_buyout_plan_rub`, planned DRR percent and optional `as_of_date` / contract-start params; complete Q1-Q4 params are transitional fallback only;
- plan-report response contains independent selected/MTD/QTD/YTD blocks with `available / partial / unavailable`, source mix and per-source missing dates; an unavailable YTD block must not hide an available selected period;
- plan-report may use `manual_monthly_plan_report_baseline` only for full-month aggregates inside the route; baseline is uploaded/read via `baseline-template.xlsx`, `baseline-upload`, `baseline-status` and does not replace accepted daily snapshots or any other report source;
- `GET /v1/sheet-vitrina-v1/feedbacks` is read-only over official WB feedbacks and supports bounded `date_from/date_to`, `stars` and `is_answered`; hosted 401/403 from WB token permission is a real blocker for the `Отзывы` feature, not a silent fallback to another token name;
- feedbacks AI prompt/analyze routes manage operational prompt config and transient structured output only; they must not write accepted truth, submit complaints, call Seller Portal or use Google Sheets/GAS;
- `GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options` and `POST .../calculate` are read-only over active SKU/config, non-financial metric options and persisted ready snapshots; missing dates/values surface partial/unavailable coverage and are not zero-filled;
- one-off `apps/sheet_vitrina_v1_ready_fact_reconcile.py dry-run|apply` can repair missing accepted report facts from persisted ready snapshots, but must not overwrite existing accepted diffs or fabricate blank ready values as zero;
- operator page state is browser-owned only: current top-level tab, active subsection under `Отчёты` / `Расчёт поставок` and stock-report SKU selection persist in namespaced `localStorage`; reload must restore the last valid state, while empty/broken storage or obsolete `nmId` values must fall back safely to current defaults/current active SKU truth;
- daily-report factor lists are now full valid sets sorted by `matched_sku_count desc` and aggregate strength; factor rows surface label + arrow + `N SKU` + truthful aggregate summary instead of plain `вверх/вниз` text;
- daily-report response now includes `metric_ranking_diagnostics`, so a short decline list can be diagnosed from the payload itself instead of being treated as a UI cap bug;
- в block `Автообновления` `Автоцепочка` должна быть backend-driven description current daily chain, а не legacy sheet write; current truthful wording = `Ежедневно в 11:00, 20:00 Asia/Yekaterinburg: server-side refresh ready snapshot for website/operator web-vitrina`;
- тот же block должен surface-ить `Последний автозапуск`, `Статус последнего автозапуска` и `Последнее успешное автообновление` из backend/status contract;
- `POST /v1/sheet-vitrina-v1/refresh` обновляет date-aware ready snapshot в repo-owned SQLite runtime contour;
- `POST /v1/sheet-vitrina-v1/load` is archived/blocked in current default runtime and must not be used as success proof for web-vitrina completion;
- empty/default refresh request must resolve `as_of_date` by `Asia/Yekaterinburg`, not by UTC/host-local clock;
- `GET /v1/sheet-vitrina-v1/status` читает последний persisted refresh result, не триггерит heavy source fetch и показывает `date_columns` / `temporal_slots` plus `server_context`;
- при missing ready snapshot тот же `GET /v1/sheet-vitrina-v1/status` остаётся truthful `422`, но всё равно отдаёт `server_context`, чтобы operator page показывала текущие timezone/scheduler facts уже в empty state;
- around UTC boundary `19:00–23:59`, `today_current` must already point to next `Asia/Yekaterinburg` business day;
- `CONFIG!H:I` preserves `endpoint_url`, `last_bundle_version`, `last_status`, `last_http_status`;
- current truth / ready snapshot keep `95` enabled+show_in_data metrics;
- `DATA_VITRINA` keeps the same server-driven truth as operator-facing two-day `date_matrix`: `1631` source rows, `34` blocks, `33` separators, `1698` rendered rows и `95` unique metric keys при `yesterday_closed + today_current`;
- `STATUS` names live sources per temporal slot, such as `seller_funnel_snapshot[yesterday_closed]`, `seller_funnel_snapshot[today_current]`, `stocks[yesterday_closed]`, `stocks[today_current]`, `cost_price[yesterday_closed]`, `cost_price[today_current]`, `promo_by_price[yesterday_closed]`, `promo_by_price[today_current]`;
- current-snapshot-only sources (`prices_snapshot`, `ads_bids`) are expected to read `yesterday_closed` from the already accepted current snapshot of the previous business day instead of historical refetching or blanking the closed-day column;
- `stocks[yesterday_closed]` is expected to materialize as success from exact-date runtime cache / historical CSV; `stocks[today_current]` is expected to stay truthful `not_available`/blank under the current `yesterday_closed_only` policy;
- `seller_funnel_snapshot` and `web_source_snapshot` use bounded `explicit-date -> latest-if-date-matches` only for current-day read resolution; `yesterday_closed` accepts only an explicit accepted closed-day snapshot and must not silently reuse provisional same-day values;
- if exact-date `today_current` snapshot is still missing for `seller_funnel_snapshot` / `web_source_snapshot`, refresh may bounded-trigger server-local `/opt/wb-web-bot` same-day runners plus `/opt/wb-ai/run_web_source_handoff.py` before final read-side fetch;
- zero-filled exact-date `seller_funnel_snapshot` is not treated as truthful success anymore: refresh retries current-day capture/handoff, and if the payload still stays all-zero it surfaces as source error/blank instead of `view_count=0` / `open_card_count=0` rows;
- later invalid same-day attempt for current-only sources must preserve the last accepted snapshot and surface the failure only in status/note/log;
- manual operator refresh keeps short retries only inside that run and must not create persisted retry debt for the background runner;
- blank values для promo-backed metrics и unmatched/missing `COST_PRICE` coverage трактуются как truthful current-truth/status signal, а не как повод переносить heavy fallback logic в Apps Script.

## Common failure signatures

| Signal | Meaning |
| --- | --- |
| `clasp` / Google auth `invalid_grant` | legacy sheet/GAS auth failure only; not a blocker for current web-vitrina unless the task scope explicitly changes archived Apps Script guard code |
| `CONFIG!I2 должен содержать URL registry upload endpoint` | sheet-side endpoint URL is missing |
| `COST_PRICE!F2 должен содержать URL cost price upload endpoint или должен быть заполнен CONFIG!I2` | COST_PRICE upload path has no explicit URL and cannot derive origin from registry upload control block |
| `STATUS.cost_price[*] = missing` or `incomplete` | authoritative COST_PRICE dataset is empty, not materialized, or does not cover every enabled group for the requested slot date |
| public `404` JSON / `{"detail":"Not Found"}` на ожидаемом public route | route есть в repo intent, но live deploy или publish wiring stale/incomplete |
| `sheet_vitrina_v1 ready snapshot missing` после upload | load path is cheap-read only; explicit refresh has not materialized snapshot for the current bundle / date yet |
| `Снимок пока не подготовлен.` на `/sheet-vitrina-v1/operator` | operator page честно сообщает, что explicit refresh ещё не запускался для current bundle / date |
| на `/sheet-vitrina-v1/operator` пустой/неактуальный block `Автообновления` | stale deploy, stale operator template или `GET /v1/sheet-vitrina-v1/status` не несёт expected `server_context` |
| `Ежедневный отчёт пока недоступен` при ожидаемо готовых closed-day snapshots | missing/stale deploy, broken `GET /v1/sheet-vitrina-v1/daily-report` route, либо один из required ready snapshots (`default_business_as_of_date(now)` / `minus 1 day`) не materialized |
| daily-report block сравнивает `today_current` вместо двух closed days | broken server-side comparison rule или stale operator JS/template |
| в ranked decline list daily-report показывается только `3` позиции | truthful data shape for the current comparable pair; repo-owned diagnostic smoke currently keeps `raw_candidate_count=10`, `present_after_none_filter_count=9`, `negative_count=3`, `positive_count=6`, with `avg_ads_bid_search` excluded because both closed-day values are missing |
| `Отчёт по остаткам пока недоступен` при ожидаемо готовом closed-day snapshot | missing/stale deploy, broken `GET /v1/sheet-vitrina-v1/stock-report` route, либо ready snapshot `default_business_as_of_date(now)` не materialized или его `yesterday_closed` slot не совпадает с requested/default closed business day |
| `sheet vitrina endpoint returned non-JSON response` | wrong publish/upstream route or HTML error surface instead of expected JSON |
| `today_current` values оказались под yesterday date column | live runtime stale; current contour всё ещё использует single-date surrogate вместо two-slot ready snapshot. GAS publish относится только к legacy sheet/export scope |
| default refresh without `as_of_date` materialize-ит `UTC yesterday` / `UTC today` вместо EKT dates | stale deploy or stale business-time helper; current runtime still uses UTC-bound default-date semantics instead of `Asia/Yekaterinburg` |
| `required env WB_API_TOKEN is not set` | live/runtime secret boundary is not aligned with the canonical WB token path |
| `GET /v1/sheet-vitrina-v1/feedbacks` returns 401/403 from upstream | hosted `WB_API_TOKEN` lacks feedbacks permission or is invalid for that WB category; this blocks the `Отзывы` feature until the canonical token is fixed |
| `required env OPENAI_API_KEY is not set` or OpenAI 401/403 on feedbacks AI | feedbacks AI prompt/table may still render, but AI analyze is blocked by the canonical OpenAI runtime secret/model access |
| promo current invariant smoke reports fatal expected ended/no-download artifacts | regression in promo artifact classification; high-confidence non-materializable campaigns must stay diagnostic-only and not enter fatal missing-artifact gating |
| research options route has `promo_filter_available=false` | latest closed ready snapshot or promo truth is unavailable; chip must be disabled/unavailable and calculation must not fabricate a promo-filtered candidate list |
| `historical stocks report .* did not finish within bounded polling window` or `... was not listed` | Seller Analytics CSV historical report did not become downloadable in bounded time; inspect `STOCK_HISTORY_DAILY_CSV` report queue / token scope / upstream availability |
| `STATUS.stocks[yesterday_closed] = error` with note from historical CSV fetch | closed-day stocks path failed before exact-date runtime cache was materialized; inspect Seller Analytics CSV create/poll/download chain and runtime backfill state |
| `STATUS.stocks[yesterday_closed] = not_available` | stale deploy or stale ready snapshot: after the historical stocks checkpoint switch, this source should no longer stay current-only in `sheet_vitrina_v1` |
| `STATUS.stocks[today_current] = not_available` | truthful result under the current `yesterday_closed_only` policy; investigate only if source/card/aggregate status is still degraded because of this non-required slot |
| `STATUS.web_source_snapshot[yesterday_closed] = not_found` or `STATUS.seller_funnel_snapshot[yesterday_closed] = not_found` with `resolution_rule=explicit_or_latest_date_match` | upstream latest payload no longer matches requested day and exact-date runtime cache for that date is still missing |
| `STATUS.web_source_snapshot[yesterday_closed]` or `STATUS.seller_funnel_snapshot[yesterday_closed]` is `closure_retrying` / `closure_rate_limited` / `closure_exhausted` | strict closed-day acceptance has not confirmed final truth yet; closed slot must stay blank/error instead of silently inheriting provisional same-day values |
| `STATUS.web_source_snapshot[today_current].note` or `STATUS.seller_funnel_snapshot[today_current].note` starts with `current_day_web_source_sync_failed=` | bounded refresh tried server-local same-day capture/handoff and failed before exact-date local snapshot became available; investigate `/opt/wb-web-bot` runners, `/opt/wb-ai/run_web_source_handoff.py`, env and host-local owner paths |
| the same note contains `seller_portal_session_invalid` or UI reason says `сессия seller portal больше не действует; требуется повторный вход` | current `/opt/wb-web-bot/storage_state.json` no longer authorizes seller portal; prepare localhost-only relogin surface via `cd /opt/wb-core-runtime/app && python3 apps/seller_portal_relogin_session.py start`, then use the returned SSH/noVNC step to log in; the tool auto-saves `storage_state.json` after validated auth and auto-runs refresh |
| operator block `Проверка и восстановление Seller-сессии` shows `starting` | recovery run уже создан и получил свой `run_id`, но временное окно входа ещё готовится; дождитесь `awaiting_login`, затем скачайте Mac launcher |
| operator block `Проверка и восстановление Seller-сессии` shows `awaiting_login` | используйте тот же page block, скачайте launcher и войдите через SSH-tunneled localhost-only noVNC browser; recovery не complete until status leaves `awaiting_login` and the tool confirms save/validation/canonical supplier/refresh |
| operator block shows final `not_needed` | конкретный recovery run завершился сразу: на момент старта seller session already valid + canonical, поэтому noVNC/login не требовались |
| operator block shows final `error` with summary about wrong cabinet or refresh | смотрите `summary`/`Финал запуска`: final run marker уже materialized и больше не зависит от общего session-check; wrong cabinet => inspect `SELLER_PORTAL_CANONICAL_SUPPLIER_ID` / `SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL`, refresh error => debug downstream refresh/log contour |
| `STATUS.seller_funnel_snapshot[*] = error` with `invalid_exact_snapshot=zero_filled_seller_funnel_snapshot` | exact-date seller-funnel payload existed but every `view_count/open_card_count` was zero; runtime rejected it as invalid instead of materializing false zero metrics, so inspect `/opt/wb-web-bot` capture freshness and rerun handoff |
| `STATUS.web_source_snapshot[yesterday_closed].note` contains `closed_day_source_freshness_not_accepted` | exact-date search snapshot was fetched before the business day was actually closed; rerun authoritative closure path instead of accepting a provisional payload as final truth |
| `STATUS.stocks[yesterday_closed].note` starts with `unmapped stocks quantity outside configured district map=` | historical stocks payload contains quantity outside the current RU district mapping; `stock_total` keeps it, district rows stay source-backed, and the residual is surfaced explicitly instead of being dropped |
| later invalid auto/manual attempt clears current-only values that were accepted earlier the same day | regression in same-day accepted snapshot contract; inspect `accepted_current_snapshot` persistence and current-only invalid candidate handling |
| manual refresh leaves `closure_retrying` / `closure_pending` state for a source that only failed in the manual run | regression in execution-mode separation; manual path must not create persisted retry debt |
| `ReferenceError: URL is not defined` | Apps Script runtime bug in sheet-side URL derivation |
| `registry upload bundle must contain 5-64 metrics_v2 entries` | live runtime still serves stale validator / stale deploy and is not aligned with current repo semantics |
| `ACCESS_TOKEN_SCOPE_INSUFFICIENT` for `clasp` | local GAS OAuth scopes are insufficient for archived GAS guard publish; not a current web-vitrina blocker |
| `gh: command not found` or `gh auth status -h github.com` shows no active auth | current execution context cannot own ordinary GitHub PR closure; return exact blocker and one minimal manual next step |
| `gh pr merge` returns permission / protection error | ordinary merge is blocked by missing write rights or branch protection; keep merge as human-only fallback only for this blocker case |

# Known gaps

- This runbook is compact and does not replace module-specific evidence.
- It intentionally omits full SRE hardening beyond the canonical hosted deploy/probe contract.

# Not in scope

- Full SRE runbook.
- Full legacy debug cookbook.
- Secrets or host-specific credential instructions.
