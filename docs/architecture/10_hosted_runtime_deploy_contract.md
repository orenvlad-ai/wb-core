# Hosted Runtime Deploy Contract

## Purpose

Этот документ фиксирует минимальный repo-owned deploy/publish contract для already materialized hosted runtime family вокруг `registry_upload_http_entrypoint_block` и `sheet_vitrina_v1`.

Цель bounded шага:
- убрать hidden operational knowledge о target routes и проверках;
- дать один canonical runner для `deploy -> loopback probe -> public probe`;
- не коммитить secrets и materialize-ить repo-owned runtime/timer wiring без ручного host drift.

## Canonical Scope

Contract покрывает active EU hosted contour на `https://api.selleros.pro` через EU host `89.191.226.88` для routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `POST /v1/sheet-vitrina-v1/load`
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
- `GET /sheet-vitrina-v1/operator`
- `GET /sheet-vitrina-v1/vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
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

Canonical active target for the current EU hosted runtime:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json`
- `target_status = active`
- `target_role = primary_live`
- `target_lifecycle = current_live`
- `mutation_policy = routine_writes_allowed`
- `host_ip = 89.191.226.88`
- `public_domain = api.selleros.pro`
- `ssh_destination = wb-core-eu-root`
- `public_base_url = https://api.selleros.pro`
- current live DNS name = `api.selleros.pro`
- `runtime_env.REGISTRY_UPLOAD_RUNTIME_DIR = /opt/wb-core-runtime/state`
- `service_name = wb-core-registry-http.service`
- nginx `server_names = 89.191.226.88 api.selleros.pro`
- nginx managed TLS = `/etc/letsencrypt/live/api.selleros.pro/fullchain.pem` + `/etc/letsencrypt/live/api.selleros.pro/privkey.pem`
- This production domain/TLS publication is a hard current-live invariant. For targets marked `primary_live` or `current_live`, `deploy`, `deploy-and-verify` and `apply-nginx-routes` must fail locally before SSH/rsync/nginx/systemd mutation if the target regresses to IP-only HTTP, drops `api.selleros.pro` from `server_names`, or drops managed `443 ssl` TLS.

Archived legacy target:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json`
- `target_status = archived`
- `target_role = rollback_only`
- `target_lifecycle = deprecated_live_target`
- `mutation_policy = do_not_deploy_without_emergency_rollback_override`
- `legacy_host_ip = 178.72.152.177`
- `public_domain = api.selleros.pro`
- `ssh_destination = selleros-root`
- `public_base_url = https://api.selleros.pro`
- This target is rollback/read-only migration evidence only. Routine deploy, apply-nginx, restart, update, audit, GC or hosted runtime write tasks must use the EU target. The runner fail-fast rejects archived/legacy target hosts for mutating actions unless an explicit emergency rollback override is present.
- The domain string in this archived JSON is historical metadata, not old-VPS identity. Old VPS identity is `selleros-root` / `178.72.152.177`; `api.selleros.pro` may be a current live DNS name for the EU target.
- Recommended provider-side label for the old VPS: `ROLLBACK-ONLY_DO-NOT-DEPLOY_wb-core-old-selleros`.

Canonical repo-owned systemd artifacts for this contour:
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-registry-http.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.timer`

Canonical repo-owned public route allowlist:
- `artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json`

Runner работает от current checked-out worktree и поэтому применим к незамёрженному branch/PR without merge-before-verify, если доступны safe deploy rights.

Supported commands:
- `print-plan`
- `deploy`
- `loopback-probe`
- `public-probe`
- `deploy-and-verify`

Read-only commands may inspect rollback-only target metadata (`print-plan`, `deploy --dry-run`, `apply-nginx-routes --dry-run`, bounded probes when explicitly needed), but routine writes must not target selleros.

## Canonical Target Definition

Checked-in target template фиксирует field names, которые больше не нужно угадывать руками:
- `target_id`
- `target_status`
- `target_role`
- `target_lifecycle`
- `mutation_policy`
- `host_ip`
- `legacy_host_ip`
- `public_domain`
- `provider_side_label_recommendation`
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
- `nginx_public_routes`
  - optional `server_names` array may pin concrete nginx hostnames/IP names for the server block; when omitted, the runner derives the single name from `public_base_url`
  - optional `tls` object may render a managed TLS block into that same server block:
    - `listen` = explicit nginx listen directives
    - `certificate_path` = public certificate chain path
    - `certificate_key_path` = private key path reference only; deploy output must not print key content
- `runtime_env`

Known active EU target values теперь зафиксированы repo-owned:
- `target_status = active`
- `target_role = primary_live`
- `target_lifecycle = current_live`
- `mutation_policy = routine_writes_allowed`
- `host_ip = 89.191.226.88`
- `public_domain = api.selleros.pro`
- `public_base_url = https://api.selleros.pro`
- current live DNS name = `api.selleros.pro`
- `loopback_base_url = http://127.0.0.1:8765`
- `ssh_destination = wb-core-eu-root`
- `target_dir = /opt/wb-core-runtime/app`
- `service_name = wb-core-registry-http.service`
- `restart_command = systemctl restart wb-core-registry-http.service`
- `status_command = systemctl status --no-pager --full wb-core-registry-http.service`
- `environment_file = /opt/wb-ai/.env`
- `runtime_env.REGISTRY_UPLOAD_RUNTIME_DIR = /opt/wb-core-runtime/state`
- `systemd_unit_directory = /etc/systemd/system`
- `systemd_units_source_dir = artifacts/registry_upload_http_entrypoint/systemd`
- `managed_systemd_units = wb-ai-api.service + refresh.service + refresh.timer + closure-retry.service + closure-retry.timer`
- `nginx_public_routes.server_config_path = /etc/nginx/sites-enabled/wb-ai`
- `nginx_public_routes.manifest_path = artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json`
- `nginx_public_routes.test_command = nginx -t`
- `nginx_public_routes.reload_command = systemctl reload nginx`
- `nginx_public_routes.server_names = ["89.191.226.88", "api.selleros.pro"]`
- `nginx_public_routes.tls.listen = ["443 ssl"]`
- `nginx_public_routes.tls.certificate_path = /etc/letsencrypt/live/api.selleros.pro/fullchain.pem`
- `nginx_public_routes.tls.certificate_key_path = /etc/letsencrypt/live/api.selleros.pro/privkey.pem`
- route paths inside `runtime_env` follow current entrypoint defaults
- losing `api.selleros.pro` or `listen 443 ssl` is production outage drift, not an acceptable deploy variant; repo-owned validation treats it as a blocker before live mutation.

Archived selleros target note:
- `selleros-root` and host `178.72.152.177` are not active runtime targets after the EU VPS cutover.
- `api.selleros.pro` is not by itself old-VPS identity; target safety is determined from repo target metadata plus `ssh_destination`, target dir, runtime dir, service name and the old IP guard.
- Selleros is `rollback_only` / `deprecated_live_target` / `do_not_deploy_without_emergency_rollback_override`; it is not a routine deploy, apply-nginx, restart, update, GC or hosted runtime mutation target.
- If `WB_CORE_HOSTED_RUNTIME_TARGET_FILE` points to archived selleros JSON or any target with `ssh_destination=selleros-root`, mutating commands must fail fast before SSH/rsync/nginx/systemd writes instead of silently touching the old VPS.
- Emergency rollback writes require the exact explicit override `WB_CORE_ALLOW_ROLLBACK_TARGET_WRITE=I_UNDERSTAND_SELLEROS_IS_ROLLBACK_ONLY`; the runner prints a warning and still does not print secrets.
- `print-plan` and dry-run command planning may remain available for rollback evidence because they do not mutate the old VPS.
- DNS/TLS publication for `api.selleros.pro` is part of the current EU target contract; future DNS/TLS changes still require an explicit target-contract update before deploy. The current invariant is exact: `public_base_url=https://api.selleros.pro`, `nginx_public_routes.server_names=["89.191.226.88","api.selleros.pro"]`, and managed TLS with `listen=["443 ssl"]` plus the LetsEncrypt paths for `api.selleros.pro`.

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
- `OPENAI_API_KEY`

Optional runtime overrides remain the same as in current official-api boundary:
- `WB_OFFICIAL_API_BASE_URL`
- `WB_ADVERT_API_BASE_URL`
- `WB_SELLER_ANALYTICS_API_BASE_URL`
- `WB_STATISTICS_API_BASE_URL`
- `WB_FEEDBACKS_API_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_API_BASE_URL`
- `OPENAI_TIMEOUT_SECONDS`
- `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`
- `SELLER_PORTAL_CANONICAL_SUPPLIER_ID`
- `SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL`
- `SELLER_PORTAL_RELOGIN_SSH_DESTINATION`
- `SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_API_BASE_URL`
- `SHEET_VITRINA_WEB_SOURCE_SNAPSHOT_BASE_URL`
- `SHEET_VITRINA_SELLER_FUNNEL_SNAPSHOT_BASE_URL`

Current promo live-wiring note:
- if hosted runtime uses the repo-owned `promo_by_price` live seam, service env must expose a valid seller session state path for the bounded browser collector;
- canonical selleros host default = `/opt/wb-web-bot/storage_state.json`, but runtime may override it explicitly via `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`.
- when the hosted operator contour exposes permanent seller-session recovery, the same env also defines canonical organization truth and reusable launcher metadata:
  - `SELLER_PORTAL_CANONICAL_SUPPLIER_ID` = authoritative supplier that the saved seller session must target before recovery is considered successful;
  - `SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL` = operator-facing org label for the same supplier;
  - `SELLER_PORTAL_RELOGIN_SSH_DESTINATION` = SSH host alias baked into the downloadable macOS launcher for localhost-only noVNC tunneling.
- hosted deploy contract must materialize the bounded workbook/parser/browser dependency on the remote system python:
  - current canonical packages = `openpyxl==3.1.5`, `playwright==1.58.0`
  - deploy runner installs them on host before restart if they are still missing;
  - deploy runner also verifies or installs Playwright Chromium with host browser dependencies before restart.
- current seller-portal relogin recovery on the EU hosted runtime is repo-owned dependency setup, not a manual one-off host state:
  - `/opt/wb-web-bot/venv/bin/python` must exist and carry `playwright==1.58.0` plus `psycopg2-binary==2.9.11` for seller-session probes and owner capture DB writes;
  - `/opt/wb-web-bot/storage_state.json` remains runtime data and is never created, printed or deleted by deploy;
  - the deploy runner creates/repairs `/opt/wb-web-bot/venv` with `python3 -m venv`, installs pinned packages there and ensures Chromium can launch from both the hosted runtime system python and the wb-web-bot venv.
- steady-state Seller Portal bot-backed capture on EU is a separate owner runtime contour, not a public nginx route:
  - non-secret owner code lives under `/opt/wb-web-bot/bot` and `/opt/wb-ai`;
  - `/opt/wb-ai/venv/bin/python` must carry `fastapi==0.129.1`, `uvicorn==0.41.0`, `psycopg2-binary==2.9.11` and `requests==2.32.5`;
  - `wb-ai-api.service` is repo-owned systemd wiring and binds `/opt/wb-ai/api.py` to `127.0.0.1:8000`;
  - web-vitrina materialization adapters default to that local owner API for `GET /v1/search-analytics/snapshot` and `GET /v1/sales-funnel/daily`, with env overrides only for an explicit alternate owner runtime;
  - local PostgreSQL is the EU handoff store for source raw tables and local read-side tables; credentials remain in host env files and must not be printed. DB/schema initialization is operational runtime setup, while deploy verifies packages, venvs, code/import contract and the localhost API systemd unit.
- current seller-portal relogin recovery also expects host OS packages that deploy now verifies/installs:
  - `python3-pip`
  - `python3-venv`
  - `xvfb`
  - `x11vnc`
  - `novnc`
  - `websockify`
  - `openbox`
- these packages are used only by the repo-owned seller-session recovery contour `apps/seller_portal_relogin_session.py`; it binds noVNC to `127.0.0.1` on the host, issues a per-run `run_id`, must materialize a real visible headed Chromium window before surfacing `awaiting_login`, writes updated `storage_state.json` only after validated auth plus canonical supplier confirmation/safe switch, and is intended for temporary auth recovery rather than for the steady-state ingest path.
- current steady operator path over that tool is bounded and HTTP-owned:
  - `GET /v1/sheet-vitrina-v1/seller-portal-session/check`
  - `POST /v1/sheet-vitrina-v1/seller-portal-recovery/start`
  - `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
  - `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status`
  - `POST /v1/sheet-vitrina-v1/seller-portal-recovery/stop`
  - `GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status` must remain a 200-shape status surface even when the EU host is missing `/opt/wb-web-bot/venv/bin/python`; that state is surfaced as a seller-session probe error, not as a public 500.
- the downloadable launcher stays Mac-only and does not expose noVNC publicly: `launcher.zip` is a zip only while the current recovery run is `awaiting_login`; outside that state it returns truthful `409` JSON, not public `500`. The launcher binds to the current recovery `run_id`, opens a SSH tunnel to the localhost-only host port, waits for local HTTP-ready, launches `http://127.0.0.1:<port>/vnc.html?...path=websockify&reconnect=1` locally, polls `GET /seller-portal-recovery/status?run_id=...` and always prints a final completion marker (`completed / not_needed / stopped / timeout / error`) before exiting.

Secrets stay outside Git. Repo stores only env names and target shape.

## Canonical Completion Sequence

For live/public tasks affecting this contour `repo-only` does not count as complete. The canonical sequence is:
1. repo fix and local validation;
2. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py print-plan`;
3. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy`;
4. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe`;
5. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe`;
6. verify the installed repo-owned systemd units via `systemctl cat` / `systemctl list-timers` when the task depends on scheduler truth;
7. if the task changes archived Apps Script guard code, finish `clasp push` plus guard-only verify.

For current web-vitrina work, final verification is the server/public web surface:
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`
- `GET /sheet-vitrina-v1/vitrina`

Promo current correctness guard:
- run `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` after hosted deploys or live/public verification tasks where current promo correctness must be proven, and after any change touching `promo_by_price` materialization, promo archive/artifact validation, promo collector diagnostics/status handling, expected `ended_without_download` / non-materializable campaign handling, `sheet_vitrina_v1` refresh orchestration, promo temporal acceptance/fallback, promo source-status reduction, or web-vitrina read/page-composition code that can affect promo metric row visibility.
- The smoke is read-only: it reads public `status`, `web-vitrina` and `plan` surfaces and does not call `/v1/sheet-vitrina-v1/load`, Google Sheets/GAS, browser `localStorage`, or a refresh endpoint.
- It validates that `metadata.refresh_diagnostics.source_slots[]` contains `promo_by_price[today_current]`, source status/origin and `requested_count / covered_count` are coherent, `fatal_missing_artifact_count == 0` and `true_artifact_loss_count == 0` when exposed, expected ended/no-download artifacts remain diagnostic-only with `workbook_required=false` instead of fatal, current promo metric rows are present and not all blank, and truthful zero rows for ineligible SKU remain valid.
- Preferred command: `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`.
- If the local machine cannot validate the selleros certificate chain, the accepted diagnostic-only fallback is `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`. This is only a local CA verification fallback; route timeouts, non-200 responses or bad payloads are real blockers.

Feedbacks tab/route guard:
- run `python3 apps/sheet_vitrina_v1_feedbacks_http_smoke.py`, `python3 apps/sheet_vitrina_v1_feedbacks_ai_smoke.py` and `python3 apps/sheet_vitrina_v1_feedbacks_browser_smoke.py` after changes touching the `Отзывы` tab, `GET /v1/sheet-vitrina-v1/feedbacks`, `feedbacks/ai-prompt`, `feedbacks/ai-analyze`, official feedbacks adapter/token path, OpenAI adapter path, server-side prompt storage or feedbacks date/filter/table UI.
- Live/public closure must read `/sheet-vitrina-v1/vitrina`, one bounded `GET /v1/sheet-vitrina-v1/feedbacks?...`, `GET/POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt` and one bounded small `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` on the hosted runtime when AI feedback analysis changes. This verifies route wiring, `WB_API_TOKEN` permission for feedbacks, `OPENAI_API_KEY` visibility to the service without printing the key, friendly upstream error surfacing and normalized JSON shape without `/load`, Google Sheets/GAS, Seller Portal bot, complaint submission or accepted-truth persistence.

Google Sheets, GAS, `clasp`, `/v1/sheet-vitrina-v1/load` and `invalid_grant` are not active blockers for web-vitrina completion. If a task explicitly changes archived Apps Script guard code, verify blocked/archived behavior only.

`deploy-and-verify` may be used as one combined step when access is already safe and available.

Current deploy contract note:
- `deploy` does more than `rsync + restart`:
  - sync current checkout;
  - ensure host OS dependencies for SellerPortalBot recovery are present (`python3-pip`, `python3-venv`, `xvfb`, `x11vnc`, `novnc`, `websockify`, `openbox`);
  - ensure host OS dependencies for SellerPortalBot owner runtime are present (`postgresql`, `postgresql-client`);
  - ensure required hosted runtime python packages are present (`openpyxl==3.1.5`, `playwright==1.58.0`);
  - create/repair `/opt/wb-web-bot/venv`, install `playwright==1.58.0` and `psycopg2-binary==2.9.11` into it and ensure Playwright Chromium can launch from both Python contexts;
  - create/repair `/opt/wb-ai/venv`, install the pinned local API/handoff packages and verify `/opt/wb-web-bot/bot` plus `/opt/wb-ai/run_web_source_handoff.py` imports;
  - install/update repo-owned systemd units when configured;
  - render the repo-owned nginx public route allowlist into the configured server block, create a timestamped backup before changing the file, validate with `nginx -t`, and reload nginx only after validation succeeds;
  - restart runtime;
  - only after that run loopback/public verification.
- nginx public route publishing is idempotent: the runner removes prior `WB-CORE MANAGED PUBLIC ROUTES` block, prior `WB-CORE MANAGED TLS` block and matching legacy/manual locations from the configured server config, rewrites the target `server_name` directive to the target's explicit `nginx_public_routes.server_names` when provided, then inserts generated TLS and route blocks from target/manifest truth. New public routes for this contour must be added to that manifest and verified through the deploy runner; manual live nginx edits are not the completion path.
- The allowlist intentionally uses exact locations plus narrow route-family prefixes such as `/v1/sheet-vitrina-v1/supply/factory-order/`, `/v1/sheet-vitrina-v1/supply/wb-regional/` and `/v1/sheet-vitrina-v1/research/`; broad catch-all publication is not part of the current contract.

If deploy / publish / restart / probe / required verify steps are safe and available, Codex обязана выполнить их в том же bounded execution. `clasp` is part of this list only for archived Apps Script guard changes.
If any of these steps are unavailable or unsafe, execution must return incomplete with an exact blocker instead of a vague ops-gap.

## Probe Norm

Loopback/runtime probe validates the hosted process behind the reverse proxy or equivalent publish layer.

Public probe validates:
- `GET /sheet-vitrina-v1/operator` returns `200` + `text/html` for the unified shell; public probe also checks `GET /sheet-vitrina-v1/operator?embedded_tab=reports` for the embedded report panel. Together they must contain compact operator tokens for the top-level sections, server refresh, truthful manual-vs-auto blocks, bounded seller-session block, report subsections, plan-report baseline controls, feedbacks tab and both bounded supply subsections (`Обновление данных`, `Ручная загрузка данных`, `Проверка и восстановление Seller-сессии`, `Проверить сессию`, `Восстановить сессию`, `Скачать launcher для Mac`, `Остановить восстановление`, `Текущий запуск`, `Финал запуска`, `Статус сессии`, `Расчёт поставок`, `Отчёты`, `Отзывы`, `Загрузить отзывы`, `Загрузить данные`, `Legacy Google Sheets`, `Ежедневные отчёты`, `Отчёт по остаткам`, `Выполнение плана`, `Исторические данные для отчёта`, `planReportApplyButton`, `planReportBaselineTemplateButton`, `planReportBaselineFileInput`, `Total Order Sum`, `Негативные факторы`, `Позитивные факторы`, `Скачать лог`, `Лог`, `Автообновления`, `Часовой пояс`, `Автоцепочка`, `Последний автозапуск`, `Статус последнего автозапуска`, `Последнее успешное автообновление`, `Общий вход для двух расчётов`, `Заказ на фабрике`, `Поставка на Wildberries`, `Цикл заказов`, `Цикл поставок`)
- `GET /v1/sheet-vitrina-v1/seller-portal-session/check` returns `200` + JSON with one truthful status from `session_valid_canonical / session_valid_wrong_org / session_invalid / session_missing / session_probe_error`
- `GET /sheet-vitrina-v1/vitrina` returns `200` + `text/html` as a real operator-grade web-vitrina page shell: page must contain `Web-витрина`, `Операторский сайт`, primary `Загрузить и обновить`, top-level tab `Отзывы`, canonical JSON route token `/v1/sheet-vitrina-v1/web-vitrina`, feedbacks route token `/v1/sheet-vitrina-v1/feedbacks`, explicit `surface=page_composition` wiring, bottom `Действия и состояния`, grouped date-scoped `Обновить группу` controls and Seller Portal session controls; `JSON Connect`, the old cheap top-panel `Обновить` button and the permanent top status badge are not rendered
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition` returns `200` + bounded JSON `web_vitrina_page_composition` v1 with `meta`, `summary_cards`, `filter_surface`, `table_surface`, `status_summary`, `capabilities`; route stays read-only, defers heavy `table_surface.rows` unless `include_table_data=1` is explicit, and must not trigger refresh/upstream fetch from the public read path
  - summary/card tone must follow semantic source truth of the visible snapshot or selected period, not mere snapshot existence
  - main table must render before filters/history/actions, use Russian visible headers and expose per-row `Обновлено` timestamp without renaming backend/API field keys
  - `Загрузка данных` must render in the bottom actions block as a grouped compact table with source-group headers `WB API`, `Seller Portal / бот`, `Прочие источники`, one compact date input and one `Обновить группу` action per group, group-level last update timestamp, server/business `Сегодня: <YYYY-MM-DD>` and `Вчера: <YYYY-MM-DD>` status columns, reason columns, Russian metric labels and a secondary technical endpoint column; it must not fabricate stale-job success when exact transient log association is unavailable. The three groups must cover every visible main-table metric exactly once, with residual calculated/formula metrics assigned to `Прочие источники`.
  - `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` must be publicly routed to the hosted runtime. A POST without `source_group_id` is the safe publish probe and must return app-level `400 {"error":"source_group_id is required"}`, not proxy/fallback `404 {"detail":"Not Found"}`. With supported `source_group_id` and `as_of_date`, it must return an async job payload and the job/log must report selected date plus stage-aware source fetch / prepare-materialize / load-to-vitrina outcome, including `updated_cells`/`latest_confirmed_cells` counters. The page may use returned `updated_cells` for session-only green/yellow highlighting, but no permanent styling state is stored.
  - `Лог` must render below that table as the secondary block and keep the existing job/log download contour
  - the former sibling block `Обновление данных` is no longer rendered or exposed as an active page-composition activity block; persisted STATUS/read-side fields remain internal truth for other status contracts
  - user-facing `Свежесть данных` value must stay separate from browser-owned `Последнее обновление страницы`, but use the same readable timestamp style without leaking raw ISO `T/Z`
- `GET /v1/sheet-vitrina-v1/web-vitrina` returns either:
  - `200` + JSON `web_vitrina_contract` v1 when a ready snapshot is present, with root fields `contract_name`, `contract_version`, `page_route`, `read_route`, `meta`, `status_summary`, `schema`, `rows`, `capabilities`
  - truthful `422 {"error": ...}` when the ready snapshot is absent
  - route remains read-only, optional `as_of_date` override stays on the same boundary and must not trigger refresh/upstream fetch
- `GET /v1/sheet-vitrina-v1/feedbacks` returns `200` + JSON `sheet_vitrina_v1_feedbacks` v1 for a bounded valid query (`date_from`, `date_to`, optional `stars`, `is_answered`). It is read-only over official WB `GET /api/v1/feedbacks` with canonical `WB_API_TOKEN`; it must not trigger refresh, `/load`, Google Sheets/GAS, complaint submission or runtime persistence. If the hosted token lacks feedbacks permission, 401/403 is a real live blocker for the `Отзывы` feature rather than a deploy-script success.
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt` and `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt` manage server-side operational prompt config in the hosted runtime dir. This prompt is not ЕБД, accepted truth, ready snapshot truth or browser-local truth.
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` runs a bounded OpenAI Responses API structured-output call over loaded feedback rows. The browser processes the current visible/filtered operator set as a bounded sequential queue and sends exactly one feedback row per request; large visible sets must be rejected client-side with a clear narrowing message. The route still enforces a hard cap of 3 rows per request as a safety guard. Results and per-row failures remain transient for the current UI session and must not persist AI labels, submit complaints, call Seller Portal or write Google Sheets/GAS.
- `GET /v1/sheet-vitrina-v1/daily-report` returns `200` + JSON for both states:
  - `status=available` when ready snapshots for `default_business_as_of_date(now)` and `default_business_as_of_date(now)-1 day` are present and their `yesterday_closed` slots are comparable;
  - `status=unavailable` with truthful `reason` when one of those ready snapshots is missing or structurally unusable;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/stock-report` returns `200` + JSON for both states:
  - `status=available` when the current `ready snapshot` for `default_business_as_of_date(now)` contains a valid `yesterday_closed` slot for the requested/default closed business day;
  - `status=unavailable` with truthful `reason` when the current ready snapshot or `yesterday_closed` slot is missing/stale;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/plan-report` returns `200` + JSON for valid primary query params `period`, `h1_buyout_plan_rub`, `h2_buyout_plan_rub`, `plan_drr_pct` and optional `as_of_date`; legacy complete `q1_buyout_plan_rub`..`q4_buyout_plan_rub` may be accepted only as transitional fallback:
  - response contains `selected_period`, `month_to_date`, `quarter_to_date`, `year_to_date` blocks;
  - each block has independent `available / partial / unavailable` status, coverage details, reason, source mix and metrics; an unavailable YTD block must not hide an available selected period;
  - daily fact source is persisted accepted closed-day snapshots `fin_report_daily.fin_buyout_rub` + `ads_compact.ads_sum` for current active `config_v2` SKU;
  - buyout and ads daily facts use the same accepted temporal source slot layer but keep source-specific coverage; missing one source for a date keeps the block partial instead of dropping the other source's available fact;
  - manual monthly source `manual_monthly_plan_report_baseline` may contribute only full months inside this plan-report route; if daily precision for a baseline month is incomplete, the monthly aggregate covers the month and overlapping daily rows are excluded from the block to avoid double-count;
  - buyout plan is distributed by calendar day, with the daily amount derived from the H1/H2 plan for each date and independent from fact coverage;
  - DRR fact is `ads_sum / fin_buyout_rub * 100`, ads plan is full-calendar period buyout plan multiplied by planned DRR;
  - route stays read-only, never triggers refresh/upstream fetch, and returns truthful `available / partial / unavailable` coverage semantics instead of fabricating zero facts
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx` returns `200` + XLSX content type with compact Russian headers for monthly baseline upload
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-status` returns `200` + JSON baseline status/totals/months/upload metadata
- `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload` accepts a controlled XLSX upload with months `YYYY-MM` and non-negative numeric facts, rejects empty/invalid/negative rows, stores aggregates idempotently in runtime SQLite and does not write Google Sheets/GAS or accepted daily snapshots
- Historical web-vitrina/report consistency repair is performed only through the repo-owned one-off CLI `apps/sheet_vitrina_v1_ready_fact_reconcile.py`: dry-run first, apply only for bounded windows/metrics, no overwrite of existing accepted diffs, no fake zeros from blank ready cells, and no recurring Google Sheets/GAS dependency.
- `GET /v1/sheet-vitrina-v1/status` returns JSON with either success shape including `server_context` + `manual_context` or truthful `422 {"error": ..., "server_context": ..., "manual_context": ...}`
  - on `200`, root `status` is semantic snapshot outcome (`success / warning / error`), while technical completion stays separated in `technical_status`/derived fields
  - `server_context` / `manual_context` must keep persisted latest semantic result summaries, so restart/reload does not erase warning/error truth
- `GET /v1/sheet-vitrina-v1/plan` returns JSON with either success shape or truthful `422 {"error": ...}`
- after the current source-aware temporal-policy switch, `stocks[yesterday_closed]` must resolve through exact-date runtime snapshots sourced from Seller Analytics CSV `STOCK_HISTORY_DAILY_CSV`, while `stocks[today_current]` may truthfully stay `not_available`/blank and must not degrade source or aggregate semantic status by itself
- when strict bot/web-source closed-day acceptance is active, `STATUS` / `plan` / job surfaces must disclose truthful closure states (`closure_pending`, `closure_retrying`, `closure_rate_limited`, `closure_exhausted`, `success`) instead of silently reusing provisional same-day values in `yesterday_closed`; if exact closed-day capture is currently blocked but an accepted current snapshot for that same date already exists, the visible closed-day cell may be restored only as `latest_confirmed` fallback (`resolution_rule=accepted_current_from_prior_closed_day_latest_confirmed`) without creating accepted closed truth
- full refresh and date-scoped group refresh must keep prior confirmed visible cells when a selected source/date status is failed or unavailable, while still updating source STATUS/job diagnostics with the exact failure reason; failed bot/web-source materialization must not silently turn previous values into dashes
- when promo live wiring is active, `STATUS` / `plan` surfaces must disclose truthful `promo_by_price[*]` source facts, including `success/incomplete/missing`, collector trace note and accepted-current preservation instead of keeping promo rows as a permanent blocked gap
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status` returns JSON with dataset states, active SKU count and recommendation path
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status` returns JSON with active SKU count, methodology note, shared dataset state and optional last result
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/*.xlsx` returns `200` + XLSX content type for all operator templates with Russian headers
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-*` accepts zero-quantity rows, drops them from normalized runtime payload and coverage, and still accepts a workbook that becomes an empty inbound dataset after zero-row filtering
- `GET /v1/sheet-vitrina-v1/supply/factory-order/uploaded/*` returns the exact currently stored operator workbook when the dataset is uploaded, or truthful `422 {"error": ...}` when it is absent
- `DELETE /v1/sheet-vitrina-v1/supply/factory-order/upload/*` returns a truthful deleted/absent state and is reflected back through `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx` returns either `200` + XLSX after calculation or truthful `422 {"error": ...}` before the first successful calculation
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx` returns either `200` + XLSX after regional calculation or truthful `422 {"error": ...}` before the first successful calculation
- `POST /v1/sheet-vitrina-v1/refresh` returns JSON with either success shape including `server_context` or truthful `422 {"error": ...}`
  - refresh completion must separate `ready snapshot persisted` from semantic source health via explicit semantic fields
  - after ready snapshot persistence and promo normalized archive sync, refresh runs bounded `promo_refresh_light_gc_v1`; it scans only promo artifact roots, protects the current collector run and replay-critical archive files, and surfaces `refresh_diagnostics.promo_artifact_gc` plus operator log summary. GC warnings stay warnings and must not convert a successful data refresh into an error.
- `POST /v1/sheet-vitrina-v1/load` is archived and must return blocked/archived behavior; `GET /v1/sheet-vitrina-v1/job` remains a current operator log route for refresh/supply jobs

If the task changes operator upload/calculate write paths inside this contour, live closure additionally requires one controlled end-to-end HTTP scenario on the hosted runtime:
- download the relevant operator templates;
- upload bounded test data through the published write routes, including mixed positive/zero inbound rows and zero-only inbound files when the changed contract touches inbound acceptance;
- verify current uploaded file download/delete lifecycle if the task changes upload state handling;
- run the server-side calculation or equivalent write action;
- verify the published result surface (`status`, operator HTML, downloadable XLSX, summary JSON), including truthful `row_count=0` for accepted empty inbound datasets, without inventing sheet/GAS steps that are outside the actual change scope.

Timeout, non-JSON body, wrong content type, `404`, stale HTML error surface or missing operator route tokens are treated as stale deploy/publish symptoms.

If the task introduces or changes temporal closed-day retry behavior for `sheet_vitrina_v1`, live closure additionally requires:
- verify the repo-owned retry runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py` on the hosted target;
- verify the repo-owned timer/service artifacts are installed on host as `wb-core-sheet-vitrina-closure-retry.service` / `.timer`;
- verify at least one affected `as_of_date` where a strict closed-day-capable source either transitions to `success` after retry or stays in a truthful retry/exhausted/blocker state without fake closed values in the visible slot.

The current active public probe target is `https://api.selleros.pro`. Live/public closure for website/operator tasks must verify the HTTPS production domain routes, including `GET /sheet-vitrina-v1/vitrina`, `GET /sheet-vitrina-v1/operator`, `GET /v1/sheet-vitrina-v1/status`, `GET /v1/sheet-vitrina-v1/web-vitrina`, and `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`. `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1` remains a diagnostic-only legacy TLS escape hatch for historical checks and is not part of the active EU target closure.

## Human-Only Boundary

One minimal human-only step remains allowed only when repo-owned contract still cannot execute due missing access:
- grant deploy access for `wb-core-eu-root` / `89.191.226.88`

Without that step a live/public task stays `live-complete = blocked`; reporting only `repo-complete` is insufficient. For GAS/sheet-only scope the blocker is tracked as `sheet-complete = blocked`.
The blocker must name the concrete missing access/value and must not be phrased as unspecified operational uncertainty.

For server/operator-only changes that do not touch archived bound Apps Script guard code, `Sheet verify result` must stay `not in scope` rather than being filled with fake closure activity.
