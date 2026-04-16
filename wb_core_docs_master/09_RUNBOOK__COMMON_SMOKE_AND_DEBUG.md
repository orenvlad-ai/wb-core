---
title: "Runbook: common smoke and debug"
doc_id: "WB-CORE-PROJECT-09-RUNBOOK"
doc_type: "runbook"
status: "active"
purpose: "Дать компактный набор частых smoke/debug команд для `wb-core` без погружения во все artifacts и module docs."
scope: "Registry upload chain, sheet-side MVP flow, live GAS checks, common failure signatures и минимальные debug entrypoints."
source_basis:
  - "README.md"
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
  - "apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py"
  - "apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_read_split_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
source_of_truth_level: "secondary_project_pack"
related_paths:
  - "apps/"
  - "gas/sheet_vitrina_v1/"
  - "artifacts/"
update_triggers:
  - "изменение smoke runner"
  - "изменение live operator flow"
  - "изменение common failure signature"
built_from_commit: "5db3548de01b2299c4f003ad43074f367d3050c8"
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
python3 apps/cost_price_upload_http_entrypoint_smoke.py
python3 apps/official_api_token_path_smoke.py
python3 apps/stocks_block_smoke.py
python3 apps/stocks_block_region_mapping_smoke.py
python3 apps/stocks_block_batching_smoke.py
python3 apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py
python3 apps/sheet_vitrina_v1_cost_price_upload_smoke.py
python3 apps/sheet_vitrina_v1_cost_price_read_side_smoke.py
python3 apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py
python3 apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py
python3 apps/sheet_vitrina_v1_refresh_read_split_smoke.py
python3 apps/sheet_vitrina_v1_stocks_refresh_smoke.py
python3 apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py
python3 apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py
git diff --check
```

## Live local runner

```bash
python3 apps/registry_upload_http_entrypoint_live.py
```

Current canonical WB secret path for official adapters:
- `WB_API_TOKEN`
- keep live service/env aligned to one canonical WB token path before calling a live task complete

Expected routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /sheet-vitrina-v1/operator`

## Live GAS checks

```bash
clasp push
clasp run prepareRegistryUploadOperatorSheets
clasp run uploadRegistryUploadBundle
clasp run prepareCostPriceSheet
clasp run uploadCostPriceSheet
# open the narrow repo-owned operator page for explicit refresh
python3 -m webbrowser http://127.0.0.1:8765/sheet-vitrina-v1/operator
# curl remains a fallback if browser/UI surface is unavailable
curl -X POST http://127.0.0.1:8765/v1/sheet-vitrina-v1/refresh \
  -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-04-12"}'
clasp run loadSheetVitrinaTable
clasp run getSheetVitrinaV1State
clasp run getSheetVitrinaV1PresentationSnapshot
```

## Post-change closure

### Repo-only closure

- проверить scope diff и `git diff --check`;
- прогнать targeted local smoke / integration smoke по затронутому bounded path;
- не объявлять задачу complete, если для неё по смыслу нужен live/public/GAS closure.

### Live route/runtime closure

- если change затрагивает public HTTP route, runtime/service wiring или nginx/proxy publish, после repo update нужно закрыть и live contour;
- минимальная норма:
  - обновить existing live runtime;
  - перезапустить/reload нужный process/service;
  - проверить route на loopback/runtime contour;
  - проверить route снаружи через public URL;
- route change не считается complete, пока public probe не подтвердил expected content type / response shape.

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
  - в финальном handoff напомнить один human-only шаг: после merge загрузить актуальный pack во внешний Project.

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
- operator page показывает только narrow status/log surface: `idle / loading / success / error`, `as_of_date`, `date_columns`, `refreshed_at`, `DATA_VITRINA` / `STATUS` row counts и текст ошибки;
- `POST /v1/sheet-vitrina-v1/refresh` обновляет date-aware ready snapshot в repo-owned SQLite runtime contour;
- `GET /v1/sheet-vitrina-v1/status` читает последний persisted refresh result, не триггерит heavy source fetch и показывает `date_columns` / `temporal_slots`;
- `CONFIG!H:I` preserves `endpoint_url`, `last_bundle_version`, `last_status`, `last_http_status`;
- current truth / ready snapshot keep `95` enabled+show_in_data metrics;
- `DATA_VITRINA` keeps the same server-driven truth as operator-facing two-day `date_matrix`: `1631` source rows, `34` blocks, `33` separators, `1698` rendered rows и `95` unique metric keys при `yesterday_closed + today_current`;
- `STATUS` names live sources per temporal slot, such as `seller_funnel_snapshot[yesterday_closed]`, `seller_funnel_snapshot[today_current]`, `stocks[today_current]`, `cost_price[yesterday_closed]`, `cost_price[today_current]`, plus blocked `promo_by_price`;
- current-only sources (`stocks`, `prices_snapshot`, `ads_bids`) are expected to show `not_available` for `yesterday_closed` instead of copying `today_current` into a closed-day column;
- blank values для promo-backed metrics и unmatched/missing `COST_PRICE` coverage трактуются как truthful current-truth/status signal, а не как повод переносить heavy fallback logic в Apps Script.

## Common failure signatures

| Signal | Meaning |
| --- | --- |
| `CONFIG!I2 должен содержать URL registry upload endpoint` | sheet-side endpoint URL is missing |
| `COST_PRICE!F2 должен содержать URL cost price upload endpoint или должен быть заполнен CONFIG!I2` | COST_PRICE upload path has no explicit URL and cannot derive origin from registry upload control block |
| `STATUS.cost_price[*] = missing` or `incomplete` | authoritative COST_PRICE dataset is empty, not materialized, or does not cover every enabled group for the requested slot date |
| public `404` JSON / `{"detail":"Not Found"}` на ожидаемом public route | route есть в repo intent, но live deploy или publish wiring stale/incomplete |
| `sheet_vitrina_v1 ready snapshot missing` после upload | load path is cheap-read only; explicit refresh has not materialized snapshot for the current bundle / date yet |
| `Ready snapshot пока не materialized.` на `/sheet-vitrina-v1/operator` | operator page честно сообщает, что explicit refresh ещё не запускался для current bundle / date |
| `sheet vitrina endpoint returned non-JSON response` | wrong publish/upstream route or HTML error surface instead of expected JSON |
| `today_current` values оказались под yesterday date column | live runtime или GAS publish stale; current contour всё ещё использует single-date surrogate вместо two-slot ready snapshot |
| `required env WB_API_TOKEN is not set` | live/runtime secret boundary is not aligned with the canonical WB token path |
| `official stocks request failed with status 429` in `STATUS.stocks[today_current].note` | live runtime still hits WB inventory limiter; confirm batched `stocks` path is deployed, no stale runtime remains, and upstream wait headers are being honored |
| `STATUS.stocks[today_current] = error` with blank stock rows after refresh | bounded refresh stayed honest about stocks failure; investigate upstream inventory rate-limit / token scope instead of treating blanks as fresh stock values |
| `STATUS.stocks[yesterday_closed] = not_available` | expected bounded semantics: current-only stocks are not backfilled into yesterday EOD without dedicated historical path |
| `STATUS.stocks[today_current].note` starts with `unmapped stocks quantity outside configured district map=` | raw payload contains quantity outside the current RU district mapping; `stock_total` keeps it, district rows stay source-backed, and the residual is surfaced explicitly instead of being dropped |
| `ReferenceError: URL is not defined` | Apps Script runtime bug in sheet-side URL derivation |
| `registry upload bundle must contain 5-64 metrics_v2 entries` | live runtime still serves stale validator / stale deploy and is not aligned with current repo semantics |
| `ACCESS_TOKEN_SCOPE_INSUFFICIENT` for `clasp` | local GAS OAuth scopes are insufficient for content read/write |

# Known gaps

- This runbook is compact and does not replace module-specific evidence.
- It intentionally omits full deploy/platform operations.

# Not in scope

- Full SRE runbook.
- Full legacy debug cookbook.
- Secrets or host-specific credential instructions.
