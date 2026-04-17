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
built_from_commit: "2e6bfd43a88e693a30b130516f5f8ce66889b801"
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
python3 apps/sheet_vitrina_v1_web_source_current_sync_smoke.py
python3 apps/sheet_vitrina_v1_stocks_refresh_smoke.py
python3 apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py
python3 apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py
git diff --check
```

## Live local runner

```bash
python3 apps/registry_upload_http_entrypoint_live.py
```

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

Current canonical WB secret path for official adapters:
- `WB_API_TOKEN`
- keep live service/env aligned to one canonical WB token path before calling a live task complete

Current canonical business timezone for server-side `sheet_vitrina_v1` date math:
- `Asia/Yekaterinburg`
- default `as_of_date` = previous business day in `Asia/Yekaterinburg`
- `today_current` / current-only freshness = current business day in `Asia/Yekaterinburg`

Expected routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `POST /v1/sheet-vitrina-v1/load`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/job`
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
- если auth валиден, `gh` доступен и execution context имеет repo write/merge access, обычные `ready`, `retarget`, `merge`, `delete branch` являются Codex-owned routine;
- это одинаково относится и к stacked PR sequence, где merge идёт не в `main`, а в промежуточную base branch;
- auto-merge optional и не заменяет обычный merge для такого sequence;
- manual merge допустим только как fallback-blocker case: нет `gh`, нет auth, недостаточные scopes/permissions, GitHub вернул write blocker или branch protection требует human approval.

## Post-change closure

### Repo-only closure

- проверить scope diff и `git diff --check`;
- прогнать targeted local smoke / integration smoke по затронутому bounded path;
- не объявлять задачу complete, если для неё по смыслу нужен live/public/GAS closure.

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
  - schedule = `11:00 Asia/Yekaterinburg` = `06:00 UTC` in current systemd host timezone
- route change не считается complete, пока public probe не подтвердил expected content type / response shape.
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
- operator page показывает только narrow status/log surface: separate actions `Загрузить данные` / `Отправить данные`, compact Russian chrome для status/live-log и row-count labels плюс один compact server-driven block `Сервер и расписание`; raw log/error text и technical values при этом могут оставаться canonical;
- `POST /v1/sheet-vitrina-v1/refresh` обновляет date-aware ready snapshot в repo-owned SQLite runtime contour;
- `POST /v1/sheet-vitrina-v1/load` пишет в live sheet только already prepared snapshot и truthfully падает при missing ready snapshot / bridge blocker;
- empty/default refresh request must resolve `as_of_date` by `Asia/Yekaterinburg`, not by UTC/host-local clock;
- `GET /v1/sheet-vitrina-v1/status` читает последний persisted refresh result, не триггерит heavy source fetch и показывает `date_columns` / `temporal_slots` plus `server_context`;
- при missing ready snapshot тот же `GET /v1/sheet-vitrina-v1/status` остаётся truthful `422`, но всё равно отдаёт `server_context`, чтобы operator page показывала текущие timezone/scheduler facts уже в empty state;
- around UTC boundary `19:00–23:59`, `today_current` must already point to next `Asia/Yekaterinburg` business day;
- `CONFIG!H:I` preserves `endpoint_url`, `last_bundle_version`, `last_status`, `last_http_status`;
- current truth / ready snapshot keep `95` enabled+show_in_data metrics;
- `DATA_VITRINA` keeps the same server-driven truth as operator-facing two-day `date_matrix`: `1631` source rows, `34` blocks, `33` separators, `1698` rendered rows и `95` unique metric keys при `yesterday_closed + today_current`;
- `STATUS` names live sources per temporal slot, such as `seller_funnel_snapshot[yesterday_closed]`, `seller_funnel_snapshot[today_current]`, `stocks[today_current]`, `cost_price[yesterday_closed]`, `cost_price[today_current]`, plus blocked `promo_by_price`;
- current-only sources (`stocks`, `prices_snapshot`, `ads_bids`) are expected to show `not_available` for `yesterday_closed` instead of copying `today_current` into a closed-day column;
- `seller_funnel_snapshot` and `web_source_snapshot` use bounded `explicit-date -> latest-if-date-matches`; if requested yesterday date is no longer available upstream but was captured earlier as exact-date current snapshot, `STATUS.*[yesterday_closed].note` may show `resolution_rule=exact_date_runtime_cache`;
- if exact-date `today_current` snapshot is still missing for `seller_funnel_snapshot` / `web_source_snapshot`, refresh may bounded-trigger server-local `/opt/wb-web-bot` same-day runners plus `/opt/wb-ai/run_web_source_handoff.py` before final read-side fetch;
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
| на `/sheet-vitrina-v1/operator` пустой/неактуальный block `Сервер и расписание` | stale deploy, stale operator template или `GET /v1/sheet-vitrina-v1/status` не несёт expected `server_context` |
| `sheet vitrina endpoint returned non-JSON response` | wrong publish/upstream route or HTML error surface instead of expected JSON |
| `today_current` values оказались под yesterday date column | live runtime или GAS publish stale; current contour всё ещё использует single-date surrogate вместо two-slot ready snapshot |
| default refresh without `as_of_date` materialize-ит `UTC yesterday` / `UTC today` вместо EKT dates | stale deploy or stale business-time helper; current runtime still uses UTC-bound default-date semantics instead of `Asia/Yekaterinburg` |
| `required env WB_API_TOKEN is not set` | live/runtime secret boundary is not aligned with the canonical WB token path |
| `official stocks request failed with status 429` in `STATUS.stocks[today_current].note` | live runtime still hits WB inventory limiter; confirm batched `stocks` path is deployed, no stale runtime remains, and upstream wait headers are being honored |
| `STATUS.stocks[today_current] = error` with blank stock rows after refresh | bounded refresh stayed honest about stocks failure; investigate upstream inventory rate-limit / token scope instead of treating blanks as fresh stock values |
| `STATUS.stocks[yesterday_closed] = not_available` | expected bounded semantics: current-only stocks are not backfilled into yesterday EOD without dedicated historical path |
| `STATUS.web_source_snapshot[yesterday_closed] = not_found` or `STATUS.seller_funnel_snapshot[yesterday_closed] = not_found` with `resolution_rule=explicit_or_latest_date_match` | upstream latest payload no longer matches requested day and exact-date runtime cache for that date is still missing |
| `STATUS.web_source_snapshot[yesterday_closed].note` or `STATUS.seller_funnel_snapshot[yesterday_closed].note` contains `resolution_rule=exact_date_runtime_cache` | expected bounded semantics: previous exact-date current snapshot was truthfully reused server-side for the matching closed-day slot |
| `STATUS.web_source_snapshot[today_current].note` or `STATUS.seller_funnel_snapshot[today_current].note` starts with `current_day_web_source_sync_failed=` | bounded refresh tried server-local same-day capture/handoff and failed before exact-date local snapshot became available; investigate `/opt/wb-web-bot` runners, `/opt/wb-ai/run_web_source_handoff.py`, env and host-local owner paths |
| `STATUS.stocks[today_current].note` starts with `unmapped stocks quantity outside configured district map=` | raw payload contains quantity outside the current RU district mapping; `stock_total` keeps it, district rows stay source-backed, and the residual is surfaced explicitly instead of being dropped |
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
