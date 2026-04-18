# Hosted Runtime Deploy Contract

## Purpose

Этот документ фиксирует минимальный repo-owned deploy/publish contract для already materialized hosted runtime family вокруг `registry_upload_http_entrypoint_block` и `sheet_vitrina_v1`.

Цель bounded шага:
- убрать hidden operational knowledge о target routes и проверках;
- дать один canonical runner для `deploy -> loopback probe -> public probe`;
- не коммитить secrets и не invent-ить неизвестные host-specific значения.

## Canonical Scope

Contract покрывает hosted contour на `api.selleros.pro` для routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `POST /v1/sheet-vitrina-v1/load`
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

Contract не меняет public HTTP schema этих routes и не переносит truth logic в Apps Script.

## Repo-Owned Execution Entrypoint

Canonical runner:
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`

Canonical target template:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json`

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
- `runtime_env`

Known repo-backed facts уже можно фиксировать directly:
- `public_base_url = https://api.selleros.pro`
- `ssh_destination = api.selleros.pro`
- `loopback_base_url = http://127.0.0.1:8765`
- route paths inside `runtime_env` follow current entrypoint defaults

Unknown host-specific values не invent-ятся:
- `target_dir`
- `service_name`
- `restart_command`
- `status_command`
- `environment_file`
- live `REGISTRY_UPLOAD_RUNTIME_DIR`

Пока эти значения не заполнены и deploy access не выдан, hosted task не считается `live-complete`.

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

Secrets stay outside Git. Repo stores only env names and target shape.

## Canonical Completion Sequence

For live/public tasks affecting this contour `repo-only` does not count as complete. The canonical sequence is:
1. repo fix and local validation;
2. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py print-plan`;
3. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy`;
4. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe`;
5. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe`;
6. if the task changes bound Apps Script or live sheet behavior, finish the corresponding `clasp`/sheet verify path.

`deploy-and-verify` may be used as one combined step when access is already safe and available.

If deploy / publish / restart / probe / `clasp` / verify steps are safe and available, Codex обязана выполнить их в том же bounded execution.
If any of these steps are unavailable or unsafe, execution must return incomplete with an exact blocker instead of a vague ops-gap.

## Probe Norm

Loopback/runtime probe validates the hosted process behind the reverse proxy or equivalent publish layer.

Public probe validates:
- `GET /sheet-vitrina-v1/operator` returns `200` + `text/html` and still contains the compact operator tokens for separated refresh/load plus server/time block (`Загрузить данные`, `Отправить данные`, `Скачать лог`, `Лог`, `Сервер и расписание`, `Часовой пояс`, `Автообновление`, `Последний автозапуск`, `Статус последнего автозапуска`, `Последнее успешное автообновление`)
- `GET /v1/sheet-vitrina-v1/status` returns JSON with either success shape including `server_context` or truthful `422 {"error": ..., "server_context": ...}`
- `GET /v1/sheet-vitrina-v1/plan` returns JSON with either success shape or truthful `422 {"error": ...}`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status` returns JSON with dataset states, active SKU count and recommendation path
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/*.xlsx` returns `200` + XLSX content type for all operator templates with Russian headers
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx` returns either `200` + XLSX after calculation or truthful `422 {"error": ...}` before the first successful calculation
- `POST /v1/sheet-vitrina-v1/refresh` returns JSON with either success shape including `server_context` or truthful `422 {"error": ...}`
- `POST /v1/sheet-vitrina-v1/load` and `GET /v1/sheet-vitrina-v1/job` are operator-facing live/write routes and therefore are verified as part of task-level GAS/sheet closure, not by default public probe

If the task changes operator upload/calculate write paths inside this contour, live closure additionally requires one controlled end-to-end HTTP scenario on the hosted runtime:
- download the relevant operator templates;
- upload bounded test data through the published write routes;
- run the server-side calculation or equivalent write action;
- verify the published result surface (`status`, operator HTML, downloadable XLSX, summary JSON) without inventing sheet/GAS steps that are outside the actual change scope.

Timeout, non-JSON body, wrong content type, `404`, stale HTML error surface or missing operator route tokens are treated as stale deploy/publish symptoms.

If the local machine cannot validate the current selleros certificate chain, public probe may reuse the existing bounded diagnostic fallback:
- `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe ...`

This fallback is only for local diagnostic reachability. It is not a statement that live runtime should run insecurely.

## Human-Only Boundary

One minimal human-only step remains allowed only when repo-owned contract still cannot execute due missing access:
- fill actual hosted target values and grant deploy access for `api.selleros.pro`

Without that step a live/public/GAS task stays `live-complete = blocked`; reporting only `repo-complete` is insufficient.
The blocker must name the concrete missing access/value and must not be phrased as unspecified operational uncertainty.

For server/operator-only changes that do not touch bound Apps Script or live sheet writes, `Sheet verify result` must stay `not in scope` rather than being filled with fake closure activity.
