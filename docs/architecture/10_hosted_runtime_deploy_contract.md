# Hosted Runtime Deploy Contract

## Purpose

Этот документ фиксирует минимальный repo-owned deploy/publish contract для already materialized hosted runtime family вокруг `registry_upload_http_entrypoint_block` и `sheet_vitrina_v1`.

Цель bounded шага:
- убрать hidden operational knowledge о target routes и проверках;
- дать один canonical runner для `deploy -> loopback probe -> public probe`;
- не коммитить secrets и materialize-ить repo-owned runtime/timer wiring без ручного host drift.

## Canonical Scope

Contract покрывает hosted contour на `api.selleros.pro` для routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `POST /v1/sheet-vitrina-v1/load`
- `GET /v1/sheet-vitrina-v1/daily-report`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/job`
- `GET /sheet-vitrina-v1/operator`
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
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx`

Contract не меняет public HTTP schema этих routes и не переносит truth logic в Apps Script.

## Repo-Owned Execution Entrypoint

Canonical runner:
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`

Canonical target template:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json`

Canonical live target for the current selleros host:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json`

Canonical repo-owned systemd artifacts for this contour:
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.timer`

Runner работает от current checked-out worktree и поэтому применим к незамёрженному branch/PR without merge-before-verify, если доступны safe deploy rights.

Supported commands:
- `print-plan`
- `deploy`
- `loopback-probe`
- `public-probe`
- `deploy-and-verify`

## Canonical Target Definition

Checked-in target template фиксирует field names, которые больше не нужно угадывать руками:
- `target_id`
- `public_base_url`
- `loopback_base_url`
- `ssh_destination`
- `target_dir`
- `service_name`
- `restart_command`
- `status_command`
- `environment_file`
- `systemd_unit_directory`
- `systemd_units_source_dir`
- `managed_systemd_units`
- `runtime_env`

Known selleros target values теперь зафиксированы repo-owned:
- `public_base_url = https://api.selleros.pro`
- `loopback_base_url = http://127.0.0.1:8765`
- `ssh_destination = selleros-root`
- `target_dir = /opt/wb-core-runtime/app`
- `service_name = wb-core-registry-http.service`
- `restart_command = systemctl restart wb-core-registry-http.service`
- `status_command = systemctl status --no-pager --full wb-core-registry-http.service`
- `environment_file = /opt/wb-ai/.env`
- `runtime_env.REGISTRY_UPLOAD_RUNTIME_DIR = /opt/wb-core-runtime/state`
- `systemd_unit_directory = /etc/systemd/system`
- `systemd_units_source_dir = artifacts/registry_upload_http_entrypoint/systemd`
- `managed_systemd_units = refresh.service + refresh.timer + closure-retry.service + closure-retry.timer`
- route paths inside `runtime_env` follow current entrypoint defaults

Secrets and mutable credentials по-прежнему не хранятся в Git. Repo stores only non-secret target wiring and unit artifacts.

## Canonical Runtime Env Contract

Hosted service должна предоставлять current repo entrypoint env names:
- `REGISTRY_UPLOAD_HTTP_HOST`
- `REGISTRY_UPLOAD_HTTP_PORT`
- `REGISTRY_UPLOAD_RUNTIME_DIR`
- `REGISTRY_UPLOAD_HTTP_PATH`
- `COST_PRICE_UPLOAD_HTTP_PATH`
- `SHEET_VITRINA_HTTP_PATH`
- `SHEET_VITRINA_REFRESH_HTTP_PATH`
- `SHEET_VITRINA_STATUS_HTTP_PATH`
- `SHEET_VITRINA_OPERATOR_UI_PATH`

Current required upstream secret contract stays:
- `WB_API_TOKEN`

Optional runtime overrides remain the same as in current official-api boundary:
- `WB_OFFICIAL_API_BASE_URL`
- `WB_ADVERT_API_BASE_URL`
- `WB_SELLER_ANALYTICS_API_BASE_URL`
- `WB_STATISTICS_API_BASE_URL`
- `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`

Current promo live-wiring note:
- if hosted runtime uses the repo-owned `promo_by_price` live seam, service env must expose a valid seller session state path for the bounded browser collector;
- canonical selleros host default = `/opt/wb-web-bot/storage_state.json`, but runtime may override it explicitly via `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`.
- hosted deploy contract must also materialize the bounded workbook parser dependency on the remote system python:
  - current canonical package = `openpyxl==3.1.5`
  - deploy runner installs it on host before restart if it is still missing.

Secrets stay outside Git. Repo stores only env names and target shape.

## Canonical Completion Sequence

For live/public tasks affecting this contour `repo-only` does not count as complete. The canonical sequence is:
1. repo fix and local validation;
2. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py print-plan`;
3. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy`;
4. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe`;
5. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe`;
6. verify the installed repo-owned systemd units via `systemctl cat` / `systemctl list-timers` when the task depends on scheduler truth;
7. if the task changes bound Apps Script or live sheet behavior, finish the corresponding `clasp`/sheet verify path.

`deploy-and-verify` may be used as one combined step when access is already safe and available.

Current deploy contract note:
- `deploy` does more than `rsync + restart`:
  - sync current checkout;
  - ensure required hosted runtime python packages are present (`openpyxl==3.1.5`);
  - install/update repo-owned systemd units when configured;
  - restart runtime;
  - only after that run loopback/public verification.

If deploy / publish / restart / probe / `clasp` / verify steps are safe and available, Codex обязана выполнить их в том же bounded execution.
If any of these steps are unavailable or unsafe, execution must return incomplete with an exact blocker instead of a vague ops-gap.

## Probe Norm

Loopback/runtime probe validates the hosted process behind the reverse proxy or equivalent publish layer.

Public probe validates:
- `GET /sheet-vitrina-v1/operator` returns `200` + `text/html` and still contains the compact operator tokens for the three top-level sections, separated refresh/load, reports subsection-switch, server/time block and both bounded supply subsections (`Обновление данных`, `Расчёт поставок`, `Отчёты`, `Загрузить данные`, `Отправить данные`, `Ежедневные отчёты`, `Отчёт по остаткам`, `Total Order Sum`, `Негативные факторы`, `Позитивные факторы`, `Скачать лог`, `Лог`, `Сервер и расписание`, `Часовой пояс`, `Автообновление`, `Последний автозапуск`, `Статус последнего автозапуска`, `Последнее успешное автообновление`, `Общий вход для двух расчётов`, `Заказ на фабрике`, `Поставка на Wildberries`, `Цикл заказов`, `Цикл поставок`)
- `GET /v1/sheet-vitrina-v1/daily-report` returns `200` + JSON for both states:
  - `status=available` when ready snapshots for `default_business_as_of_date(now)` and `default_business_as_of_date(now)-1 day` are present and their `yesterday_closed` slots are comparable;
  - `status=unavailable` with truthful `reason` when one of those ready snapshots is missing or structurally unusable;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/stock-report` returns `200` + JSON for both states:
  - `status=available` when the current `ready snapshot` for `default_business_as_of_date(now)` contains a valid `today_current` slot for the current business day;
  - `status=unavailable` with truthful `reason` when the current ready snapshot or `today_current` slot is missing/stale;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/status` returns JSON with either success shape including `server_context` or truthful `422 {"error": ..., "server_context": ...}`
- `GET /v1/sheet-vitrina-v1/plan` returns JSON with either success shape or truthful `422 {"error": ...}`
- after the historical stocks checkpoint switch, both `stocks[yesterday_closed]` and `stocks[today_current]` must resolve through exact-date runtime snapshots sourced from Seller Analytics CSV `STOCK_HISTORY_DAILY_CSV`
- when strict bot/web-source closed-day acceptance is active, `STATUS` / `plan` / job surfaces must disclose truthful closure states (`closure_pending`, `closure_retrying`, `closure_rate_limited`, `closure_exhausted`, `success`) instead of silently reusing provisional same-day values in `yesterday_closed`
- when promo live wiring is active, `STATUS` / `plan` surfaces must disclose truthful `promo_by_price[*]` source facts, including `success/incomplete/missing`, collector trace note and accepted-current preservation instead of keeping promo rows as a permanent blocked gap
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status` returns JSON with dataset states, active SKU count and recommendation path
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status` returns JSON with active SKU count, methodology note, shared dataset state and optional last result
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/*.xlsx` returns `200` + XLSX content type for all operator templates with Russian headers
- `GET /v1/sheet-vitrina-v1/supply/factory-order/uploaded/*` returns the exact currently stored operator workbook when the dataset is uploaded, or truthful `422 {"error": ...}` when it is absent
- `DELETE /v1/sheet-vitrina-v1/supply/factory-order/upload/*` returns a truthful deleted/absent state and is reflected back through `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx` returns either `200` + XLSX after calculation or truthful `422 {"error": ...}` before the first successful calculation
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx` returns either `200` + XLSX after regional calculation or truthful `422 {"error": ...}` before the first successful calculation
- `POST /v1/sheet-vitrina-v1/refresh` returns JSON with either success shape including `server_context` or truthful `422 {"error": ...}`
- `POST /v1/sheet-vitrina-v1/load` and `GET /v1/sheet-vitrina-v1/job` are operator-facing live/write routes and therefore are verified as part of task-level GAS/sheet closure, not by default public probe

If the task changes operator upload/calculate write paths inside this contour, live closure additionally requires one controlled end-to-end HTTP scenario on the hosted runtime:
- download the relevant operator templates;
- upload bounded test data through the published write routes, including auto-upload-after-file-pick UX when the operator page contract changed;
- verify current uploaded file download/delete lifecycle if the task changes upload state handling;
- run the server-side calculation or equivalent write action;
- verify the published result surface (`status`, operator HTML, downloadable XLSX, summary JSON) without inventing sheet/GAS steps that are outside the actual change scope.

Timeout, non-JSON body, wrong content type, `404`, stale HTML error surface or missing operator route tokens are treated as stale deploy/publish symptoms.

If the task introduces or changes temporal closed-day retry behavior for `sheet_vitrina_v1`, live closure additionally requires:
- verify the repo-owned retry runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py` on the hosted target;
- verify the repo-owned timer/service artifacts are installed on host as `wb-core-sheet-vitrina-closure-retry.service` / `.timer`;
- verify at least one affected `as_of_date` where a strict closed-day-capable source either transitions to `success` after retry or stays in a truthful retry/exhausted/blocker state without fake closed values in the visible slot.

If the local machine cannot validate the current selleros certificate chain, public probe may reuse the existing bounded diagnostic fallback:
- `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe ...`

This fallback is only for local diagnostic reachability. It is not a statement that live runtime should run insecurely.

## Human-Only Boundary

One minimal human-only step remains allowed only when repo-owned contract still cannot execute due missing access:
- grant deploy access for `selleros-root` / `api.selleros.pro`

Without that step a live/public/GAS task stays `live-complete = blocked`; reporting only `repo-complete` is insufficient.
The blocker must name the concrete missing access/value and must not be phrased as unspecified operational uncertainty.

For server/operator-only changes that do not touch bound Apps Script or live sheet writes, `Sheet verify result` must stay `not in scope` rather than being filled with fake closure activity.
