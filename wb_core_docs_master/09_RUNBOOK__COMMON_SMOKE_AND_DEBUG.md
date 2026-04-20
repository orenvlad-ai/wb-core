---
title: "Runbook: common smoke and debug"
doc_id: "WB-CORE-PROJECT-09-RUNBOOK"
doc_type: "runbook"
status: "active"
purpose: "Дать компактный набор частых smoke/debug команд для `wb-core` без погружения во все artifacts и module docs."
scope: "Registry upload chain, sheet-side MVP flow, live GAS checks, common failure signatures и минимальные debug entrypoints."
source_basis:
  - "README.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_business_time_smoke.py"
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
  - "apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py"
  - "apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_read_split_smoke.py"
  - "apps/sheet_vitrina_v1_operator_load_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
  - "apps/promo_xlsx_collector_contract_smoke.py"
  - "apps/promo_xlsx_collector_integration_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_smoke.py"
  - "apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py"
source_of_truth_level: "secondary_project_pack"
related_paths:
  - "apps/"
  - "gas/sheet_vitrina_v1/"
  - "artifacts/"
update_triggers:
  - "изменение smoke runner"
  - "изменение live operator flow"
  - "изменение common failure signature"
built_from_commit: "847121253d61497cba0f1541121ed35f060705ed"
---

# Summary

Этот runbook нужен для быстрого ответа на вопросы:
- broken ли registry upload chain;
- broken ли sheet-side MVP flow;
- broken ли live GAS wiring;
- где искать first useful signal.

# Current norm

## Core local smokes

```bash
python3 apps/registry_upload_bundle_v1_smoke.py
python3 apps/registry_upload_file_backed_service_smoke.py
python3 apps/registry_upload_db_backed_runtime_smoke.py
python3 apps/registry_upload_http_entrypoint_smoke.py
python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
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
python3 apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_http_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py
python3 apps/sheet_vitrina_v1_operator_ui_persistence_smoke.py
python3 apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py
python3 apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py
python3 apps/promo_xlsx_collector_contract_smoke.py
python3 apps/promo_xlsx_collector_integration_smoke.py
python3 apps/sheet_vitrina_v1_promo_live_source_smoke.py
python3 apps/sheet_vitrina_v1_promo_live_source_integration_smoke.py
git diff --check
```

Current promo smoke intent:
- `apps/sheet_vitrina_v1_promo_live_source_smoke.py` now additionally proves historical interval replay fills the exact-date promo seam on `yesterday_closed` cache miss.

Current web-vitrina phase-2 smoke intent:
- `apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py` keeps the mapper library-agnostic and checks canonical `columns / rows / groups / sections / formatters / filters / sorts / state_model`.
- `apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py` proves the typed Phase 1 contract builder still feeds the same view-model seam without changing public routes or deploy requirements.

Current web-vitrina phase-3 smoke intent:
- `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py` keeps Gravity-specific config/data/render hints isolated in the adapter layer and checks sticky/sizing/sort/filter/useTable/state invariants without changing `view_model`.
- `apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py` proves the full seam `contract -> view_model -> gravity adapter` and keeps per-cell renderer bindings authoritative for mixed temporal columns.

Current web-vitrina phase-4 smoke intent:
- `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py` keeps page composition server-driven and checks source-chain metadata, filter surface, row counts and ready/error state behavior without changing the default read contract.
- `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py` proves the real sibling page on `/sheet-vitrina-v1/vitrina`: table render, filter controls, empty state, reset recovery and truthful error state all run through the same HTTP contour.

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
- `yesterday_closed` reads only accepted/runtime-cached promo truth
- invalid later attempt must not overwrite accepted current/closed promo truth

## Hosted runtime contract

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py print-plan
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy --dry-run
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe --as-of-date AUTO_YESTERDAY
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe --as-of-date AUTO_YESTERDAY
SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 \
  python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe --as-of-date AUTO_YESTERDAY
```

Required local env for the runner itself:
- `WB_CORE_HOSTED_RUNTIME_TARGET_FILE`
- optional `WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE`
- optional `WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS`
- optional `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1` only when local trust store cannot verify current selleros certificate chain

Canonical target template:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json`
- current selleros live target:
  - `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json`
- repo-owned systemd artifacts:
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.service`
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.timer`
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.service`
  - `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.timer`

Current canonical WB secret path for official adapters:
- `WB_API_TOKEN`
- keep live service/env aligned to one canonical WB token path before calling a live task complete

Current promo runtime env override when hosted runtime needs explicit seller session path:
- `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`
- canonical selleros value = `/opt/wb-web-bot/storage_state.json`

Current promo archive/runtime norm:
- promo collector is archive-first: unchanged campaigns reuse archived workbook artifacts instead of redownloading every Excel
- historical `promo_by_price[yesterday_closed]` may be truthfully filled from campaign interval replay into exact-date runtime seam when archive coverage exists
- uncovered dates must stay blank/incomplete; no fake sheet-side backfill is allowed

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
- `POST /v1/sheet-vitrina-v1/load`
- `GET /v1/sheet-vitrina-v1/daily-report`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/job`
- `GET /sheet-vitrina-v1/operator`
- `GET /sheet-vitrina-v1/vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/calculate`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx`

Temporal closure retry runner:

```bash
python3 apps/sheet_vitrina_v1_temporal_closure_retry_live.py --date 2026-04-17 --date 2026-04-18
```

Norm:
- use the retry runner for due `yesterday_closed` states across the full historical/date-period matrix (`seller_funnel_snapshot`, `web_source_snapshot`, `sales_funnel_history`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`) and for same-day current-only capture retries only within the current business day;
- `today_current` may remain incomplete and blank, but `yesterday_closed` must not silently inherit provisional same-day values;
- group A bot/web-source families accept closed truth only after exact-date sync + source freshness validation (`source_fetched_at` / `fetched_at` after next business-day start in `Asia/Yekaterinburg`);
- group C current-snapshot-only sources (`prices_snapshot`, `ads_bids`) still capture upstream truth only as current snapshot, but an already accepted snapshot for closed business day D must materialize as `yesterday_closed=D` on D+1; later invalid/blank/zero auto or manual attempts must preserve both prior-day accepted truth and any already accepted same-day truth;
- `stocks` is no longer current-only inside `sheet_vitrina_v1`: both `yesterday_closed` and `today_current` must resolve through exact-date runtime cache `temporal_source_snapshots[source_key=stocks]` built from Seller Analytics CSV `STOCK_HISTORY_DAILY_CSV`;
- manual operator refresh keeps short retries inside the run, but must not leak persisted due retry states into this runner path.

One-off stocks backfill runner:

```bash
python3 apps/sheet_vitrina_v1_stocks_historical_backfill.py \
  --date-from 2026-03-01 \
  --date-to 2026-04-18
```

## Live GAS checks

```bash
clasp push
clasp run prepareRegistryUploadOperatorSheets
clasp run uploadRegistryUploadBundle
clasp run prepareCostPriceSheet
clasp run uploadCostPriceSheet
# open the narrow repo-owned operator page for explicit refresh
python3 -m webbrowser http://127.0.0.1:8765/sheet-vitrina-v1/operator
# open the sibling phase-1 web-vitrina shell if the task changes its route/contract
python3 -m webbrowser http://127.0.0.1:8765/sheet-vitrina-v1/vitrina
# curl remains a fallback if browser/UI surface is unavailable
curl -X POST http://127.0.0.1:8765/v1/sheet-vitrina-v1/refresh \
  -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-04-12"}'
curl -X POST http://127.0.0.1:8765/v1/sheet-vitrina-v1/load \
  -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-04-12"}'
clasp run loadSheetVitrinaTable
clasp run getSheetVitrinaV1State
clasp run getSheetVitrinaV1PresentationSnapshot
```

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
- не объявлять задачу complete, если для неё по смыслу нужен live/public/GAS closure.

### Docs-governance closure

- если change ограничен governance/docs/pack rules, не придумывать fake deploy / `clasp` / sheet verify steps;
- обновить primary docs;
- обновить затронутый `wb_core_docs_master`;
- обновить manifest;
- проверить scope diff и `git diff --check`;
- закрыть GitHub closure до merge + delete-branch, если access работает;
- после merge привести `~/Projects/wb-core` к current `origin/main` и проверить `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md` как upload-ready source;
- оставить один human-only remainder: внешний upload актуального pack.

### Live route/runtime closure

- если change затрагивает public HTTP route, runtime/service wiring или nginx/proxy publish, после repo update нужно закрыть и live contour;
- минимальная норма:
  - обновить existing live runtime через canonical runner `deploy` или equivalent bounded path;
  - перезапустить/reload нужный process/service через canonical `restart_command` или live-owned equivalent;
  - если change затрагивает daily refresh semantics, обновить и timer wiring;
  - проверить route на loopback/runtime contour через `loopback-probe` или equivalent probe;
  - проверить route снаружи через public URL через `public-probe` или equivalent probe;
- current live `sheet_vitrina_v1` contour:
  - service = `wb-core-registry-http.service`
  - timer = `wb-core-sheet-vitrina-refresh.timer`
  - schedule = `11:00, 20:00 Asia/Yekaterinburg` = `06:00 UTC, 15:00 UTC` in current systemd host timezone
  - daily timer target = `POST /v1/sheet-vitrina-v1/refresh` with payload flag `auto_load=true`, so the automatic cycle truthfully finishes as `refresh + load to live sheet`
- current bounded `factory-order` supply contour is server/operator-only:
  - live closure still requires deploy + loopback/public probe + one controlled download/upload/calculate/download scenario if those routes changed;
  - the same closure rule now covers the sibling regional block under `Расчёт поставок`: shared `stock_ff` upload lifecycle, regional calculate, summary table and per-district XLSX routes are part of the same operator/public contour;
  - sheet/GAS verify stays `not in scope`, пока change не затрагивает bound Apps Script или live sheet write path.
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
- если change затрагивает operator `load` или live sheet write path, closure дополнительно требует `clasp push` и sheet verify по `POST /v1/sheet-vitrina-v1/load` или equivalent existing Apps Script menu flow.
- если runner уже materialized, но `ssh_destination / target_dir / service_name / restart_command / environment_file` или access отсутствуют, это фиксируется как точный blocker, а не как vague ops-gap.

### GAS/sheet closure

- если change затрагивает bound Apps Script, sheet-side flow или live sheet behavior, default closure включает `clasp push`, если он безопасен и доступен;
- после `clasp push` нужно сделать хотя бы минимальный live verify по затронутому flow:
  - `prepare`
  - `upload`
  - `refresh`
  - `load`
  - или более узкий subset, если именно он соответствует change scope;
- если upload меняет current bundle/readiness semantics, после upload недостаточно local smoke: нужно подтвердить `refresh/load` path для current bundle/date.

### Docs-pack closure

- если change меняет contract/status/checkpoint/runbook/policy wording, нужно:
  - обновить primary docs;
  - обновить затронутый `wb_core_docs_master`;
  - обновить manifest;
  - после merge привести `~/Projects/wb-core` к current `origin/main`;
  - проверить readiness по `~/Projects/wb-core/wb_core_docs_master/99_MANIFEST__DOCSET_VERSION.md`;
  - в финальном handoff оставить один human-only шаг: после merge загрузить актуальный pack во внешний Project.

## What to verify in sheet

- `CONFIG / METRICS / FORMULAS` have expected headers and non-empty rows;
- `prepareRegistryUploadOperatorSheets` currently materializes `33 / 102 / 7`;
- `uploadRegistryUploadBundle` accepts and persists factual registry sheet lengths; на текущем contour это `33 / 102 / 7`, но проверка не должна зависеть от hardcoded row caps;
- `COST_PRICE` has exact headers `group / cost_price_rub / effective_from`;
- `prepareCostPriceSheet` materializes only `COST_PRICE` and its local control block, не меняя existing registry/upload actions;
- `uploadCostPriceSheet` sends `dataset_version + uploaded_at + cost_price_rows` в separate `POST /v1/cost-price/upload`, а не подмешивает rows в `config_v2 / metrics_v2 / formulas_v2`;
- current COST_PRICE checkpoint проверяется по accepted/rejected upload result, separate runtime current state и server-side refresh/read integration;
- applicable себестоимость резолвится server-side по `group + latest effective_from <= slot_date`;
- operator-facing derived rows используют canonical keys `total_proxy_profit_rub` и `proxy_margin_pct_total`;
- `GET /sheet-vitrina-v1/operator` поднимает simple operator page без SPA/build pipeline;
- `GET /sheet-vitrina-v1/vitrina` поднимает отдельную live read-only sibling page и не встраивается в existing `/sheet-vitrina-v1/operator`;
- `GET /v1/sheet-vitrina-v1/web-vitrina` остаётся cheap read-only JSON path: default v1 shape = `contract_name / contract_version / page_route / read_route / meta / status_summary / schema / rows / capabilities`, optional `as_of_date` stays on том же route и не имеет права trigger-ить refresh/upstream fetch;
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition` now adds the page-only payload for `/sheet-vitrina-v1/vitrina`: `composition_name / composition_version / meta / summary_cards / filter_surface / table_surface / status_summary / capabilities`; route still stays read-only and must not trigger refresh/upstream fetch;
- operator page показывает narrow server-driven surface: top-level sections `Обновление данных` / `Расчёт поставок` / `Отчёты`, compact manual block `Ручная загрузка данных` with embedded actions `Загрузить данные` / `Отправить данные`, one compact reports subsection-switch `Ежедневные отчёты` / `Отчёт по остаткам` inside `Отчёты`, separate compact auto block `Автообновления` и отдельный `Лог`; manual block показывает только persisted manual-success timestamps `Последняя удачная загрузка` / `Последняя удачная отправка` из `manual_context`, reload страницы не считается proof последней manual `Отправить данные`, а completed run можно скачать через `Скачать лог`;
- `GET /v1/sheet-vitrina-v1/daily-report` остаётся cheap read-only JSON path: route сравнивает только два последних closed business day через persisted ready snapshots `default_business_as_of_date(now)` и `default_business_as_of_date(now)-1 day` и не имеет права trigger-ить refresh/upstream fetch;
- `GET /v1/sheet-vitrina-v1/stock-report` остаётся cheap read-only JSON path: route по умолчанию читает previous closed business day only from persisted ready snapshot `DATA_VITRINA[yesterday_closed]`, принимает optional explicit `as_of_date` override на том же read path, не trigger-ит refresh/upstream fetch и включает только SKU с district stock `< 50`;
- subsection `Отчёт по остаткам` now adds a compact SKU selector: full active SKU list comes from current authoritative `config_v2` truth on the operator page itself, defaults to all selected, applies only after `Рассчитать`, rejects empty selection with `Выберите хотя бы один SKU` and must show an empty result instead of stale rows when the selected subset has no breaches;
- operator page state is browser-owned only: current top-level tab, active subsection under `Отчёты` / `Расчёт поставок` and stock-report SKU selection persist in namespaced `localStorage`; reload must restore the last valid state, while empty/broken storage or obsolete `nmId` values must fall back safely to current defaults/current active SKU truth;
- daily-report factor lists are now full valid sets sorted by `matched_sku_count desc` and aggregate strength; factor rows surface label + arrow + `N SKU` + truthful aggregate summary instead of plain `вверх/вниз` text;
- daily-report response now includes `metric_ranking_diagnostics`, so a short decline list can be diagnosed from the payload itself instead of being treated as a UI cap bug;
- в block `Автообновления` `Автоцепочка` должна быть backend-driven description full daily chain, а не только scheduler time; current truthful wording = `Ежедневно в 11:00, 20:00 Asia/Yekaterinburg: загрузка данных + отправка данных в таблицу`;
- тот же block должен surface-ить `Последний автозапуск`, `Статус последнего автозапуска` и `Последнее успешное автообновление` из backend/status contract;
- `POST /v1/sheet-vitrina-v1/refresh` обновляет date-aware ready snapshot в repo-owned SQLite runtime contour;
- `POST /v1/sheet-vitrina-v1/load` пишет в live sheet только already prepared snapshot и truthfully падает при missing ready snapshot / bridge blocker;
- empty/default refresh request must resolve `as_of_date` by `Asia/Yekaterinburg`, not by UTC/host-local clock;
- `GET /v1/sheet-vitrina-v1/status` читает последний persisted refresh result, не триггерит heavy source fetch и показывает `date_columns` / `temporal_slots` plus `server_context`;
- при missing ready snapshot тот же `GET /v1/sheet-vitrina-v1/status` остаётся truthful `422`, но всё равно отдаёт `server_context`, чтобы operator page показывала текущие timezone/scheduler facts уже в empty state;
- around UTC boundary `19:00–23:59`, `today_current` must already point to next `Asia/Yekaterinburg` business day;
- `CONFIG!H:I` preserves `endpoint_url`, `last_bundle_version`, `last_status`, `last_http_status`;
- current truth / ready snapshot keep `95` enabled+show_in_data metrics;
- `DATA_VITRINA` keeps the same server-driven truth as operator-facing two-day `date_matrix`: `1631` source rows, `34` blocks, `33` separators, `1698` rendered rows и `95` unique metric keys при `yesterday_closed + today_current`;
- `STATUS` names live sources per temporal slot, such as `seller_funnel_snapshot[yesterday_closed]`, `seller_funnel_snapshot[today_current]`, `stocks[yesterday_closed]`, `stocks[today_current]`, `cost_price[yesterday_closed]`, `cost_price[today_current]`, `promo_by_price[yesterday_closed]`, `promo_by_price[today_current]`;
- current-snapshot-only sources (`prices_snapshot`, `ads_bids`) are expected to read `yesterday_closed` from the already accepted current snapshot of the previous business day instead of historical refetching or blanking the closed-day column;
- `stocks[yesterday_closed]` and `stocks[today_current]` are expected to materialize as success from exact-date runtime cache / historical CSV after the classifier switch;
- `seller_funnel_snapshot` and `web_source_snapshot` use bounded `explicit-date -> latest-if-date-matches` only for current-day read resolution; `yesterday_closed` accepts only an explicit accepted closed-day snapshot and must not silently reuse provisional same-day values;
- if exact-date `today_current` snapshot is still missing for `seller_funnel_snapshot` / `web_source_snapshot`, refresh may bounded-trigger server-local `/opt/wb-web-bot` same-day runners plus `/opt/wb-ai/run_web_source_handoff.py` before final read-side fetch;
- zero-filled exact-date `seller_funnel_snapshot` is not treated as truthful success anymore: refresh retries current-day capture/handoff, and if the payload still stays all-zero it surfaces as source error/blank instead of `view_count=0` / `open_card_count=0` rows;
- later invalid same-day attempt for current-only sources must preserve the last accepted snapshot and surface the failure only in status/note/log;
- manual operator refresh keeps short retries only inside that run and must not create persisted retry debt for the background runner;
- blank values для promo-backed metrics и unmatched/missing `COST_PRICE` coverage трактуются как truthful current-truth/status signal, а не как повод переносить heavy fallback logic в Apps Script.

## Common failure signatures

| Signal | Meaning |
| --- | --- |
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
| `today_current` values оказались под yesterday date column | live runtime или GAS publish stale; current contour всё ещё использует single-date surrogate вместо two-slot ready snapshot |
| default refresh without `as_of_date` materialize-ит `UTC yesterday` / `UTC today` вместо EKT dates | stale deploy or stale business-time helper; current runtime still uses UTC-bound default-date semantics instead of `Asia/Yekaterinburg` |
| `required env WB_API_TOKEN is not set` | live/runtime secret boundary is not aligned with the canonical WB token path |
| `historical stocks report .* did not finish within bounded polling window` or `... was not listed` | Seller Analytics CSV historical report did not become downloadable in bounded time; inspect `STOCK_HISTORY_DAILY_CSV` report queue / token scope / upstream availability |
| `STATUS.stocks[yesterday_closed] = error` with note from historical CSV fetch | closed-day stocks path failed before exact-date runtime cache was materialized; inspect Seller Analytics CSV create/poll/download chain and runtime backfill state |
| `STATUS.stocks[yesterday_closed] = not_available` | stale deploy or stale ready snapshot: after the historical stocks checkpoint switch, this source should no longer stay current-only in `sheet_vitrina_v1` |
| `STATUS.stocks[today_current] = not_available` | stale deploy or stale ready snapshot: after the final classifier switch this source should materialize same-day exact-date historical stocks instead of staying blank |
| `STATUS.web_source_snapshot[yesterday_closed] = not_found` or `STATUS.seller_funnel_snapshot[yesterday_closed] = not_found` with `resolution_rule=explicit_or_latest_date_match` | upstream latest payload no longer matches requested day and exact-date runtime cache for that date is still missing |
| `STATUS.web_source_snapshot[yesterday_closed]` or `STATUS.seller_funnel_snapshot[yesterday_closed]` is `closure_retrying` / `closure_rate_limited` / `closure_exhausted` | strict closed-day acceptance has not confirmed final truth yet; closed slot must stay blank/error instead of silently inheriting provisional same-day values |
| `STATUS.web_source_snapshot[today_current].note` or `STATUS.seller_funnel_snapshot[today_current].note` starts with `current_day_web_source_sync_failed=` | bounded refresh tried server-local same-day capture/handoff and failed before exact-date local snapshot became available; investigate `/opt/wb-web-bot` runners, `/opt/wb-ai/run_web_source_handoff.py`, env and host-local owner paths |
| `STATUS.seller_funnel_snapshot[*] = error` with `invalid_exact_snapshot=zero_filled_seller_funnel_snapshot` | exact-date seller-funnel payload existed but every `view_count/open_card_count` was zero; runtime rejected it as invalid instead of materializing false zero metrics, so inspect `/opt/wb-web-bot` capture freshness and rerun handoff |
| `STATUS.web_source_snapshot[yesterday_closed].note` contains `closed_day_source_freshness_not_accepted` | exact-date search snapshot was fetched before the business day was actually closed; rerun authoritative closure path instead of accepting a provisional payload as final truth |
| `STATUS.stocks[yesterday_closed].note` starts with `unmapped stocks quantity outside configured district map=` | historical stocks payload contains quantity outside the current RU district mapping; `stock_total` keeps it, district rows stay source-backed, and the residual is surfaced explicitly instead of being dropped |
| later invalid auto/manual attempt clears current-only values that were accepted earlier the same day | regression in same-day accepted snapshot contract; inspect `accepted_current_snapshot` persistence and current-only invalid candidate handling |
| manual refresh leaves `closure_retrying` / `closure_pending` state for a source that only failed in the manual run | regression in execution-mode separation; manual path must not create persisted retry debt |
| `ReferenceError: URL is not defined` | Apps Script runtime bug in sheet-side URL derivation |
| `registry upload bundle must contain 5-64 metrics_v2 entries` | live runtime still serves stale validator / stale deploy and is not aligned with current repo semantics |
| `ACCESS_TOKEN_SCOPE_INSUFFICIENT` for `clasp` | local GAS OAuth scopes are insufficient for content read/write |
| `gh: command not found` or `gh auth status -h github.com` shows no active auth | current execution context cannot own ordinary GitHub PR closure; return exact blocker and one minimal manual next step |
| `gh pr merge` returns permission / protection error | ordinary merge is blocked by missing write rights or branch protection; keep merge as human-only fallback only for this blocker case |

# Known gaps

- This runbook is compact and does not replace module-specific evidence.
- It intentionally omits full SRE hardening beyond the canonical hosted deploy/probe contract.

# Not in scope

- Full SRE runbook.
- Full legacy debug cookbook.
- Secrets or host-specific credential instructions.
