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
- `GET /v1/sheet-vitrina-v1/wb-finance-report`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx`
- `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-status`
- `GET /v1/sheet-vitrina-v1/feedbacks`
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints`
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job`
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job`
- `GET /v1/sheet-vitrina-v1/prices/goods`
- `POST /v1/sheet-vitrina-v1/prices/preview`
- `POST /v1/sheet-vitrina-v1/prices/upload-task`
- `GET /v1/sheet-vitrina-v1/prices/upload-task/{upload_id}`
- `GET /v1/sheet-vitrina-v1/prices/upload-task/{upload_id}/goods`
- `GET /v1/sheet-vitrina-v1/prices/quarantine`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/baseline?nmID=...`
- `POST /v1/sheet-vitrina-v1/prices/spp-test/plan`
- `POST /v1/sheet-vitrina-v1/prices/spp-test/start`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/status`
- `POST /v1/sheet-vitrina-v1/prices/spp-test/restore`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/history?limit=...&cursor=...`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/history/{job_id}`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/schedule`
- `POST /v1/sheet-vitrina-v1/prices/spp-test/schedule`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/product-capital/status`
- `POST /v1/sheet-vitrina-v1/product-capital/recalculate`
- `GET /v1/sheet-vitrina-v1/job`
- `GET /sheet-vitrina-v1/operator`
- `GET /sheet-vitrina-v1/vitrina`
- `GET /sheet-vitrina-v1/instructions`
- `GET /login`
- `POST /login`
- `GET /logout`
- `POST /logout`
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/warehouses`
- `GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}`
- `GET /v1/sheet-vitrina-v1/seller-portal-session/check`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status`
- `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip`
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec-check`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/calculate`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status`
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/calculate`
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options`
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync`
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/template.xlsx`
- `POST /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}`
- `DELETE /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}/payment-validation.pdf`
- `GET /sheet-vitrina-v1/supplier`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/registry`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/registry/compare-quote`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/parse`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}`
- `PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}`
- `DELETE /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/rematch`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/price-check`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/invoice`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract`
- `PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}`
- `PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}/file`
- `GET /sheet-vitrina-v1/settings`
- `GET /v1/sheet-vitrina-v1/settings/nomenclature`
- `POST /v1/sheet-vitrina-v1/settings/nomenclature`
- `POST /v1/sheet-vitrina-v1/settings/nomenclature/barcode-sync`
- `PATCH /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}`
- `POST /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}/barcode-sync`
- `DELETE /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}`
- `GET /v1/sheet-vitrina-v1/settings/sku-groups`
- `POST /v1/sheet-vitrina-v1/settings/sku-groups`
- `PATCH /v1/sheet-vitrina-v1/settings/sku-groups/{group_key}`
- `DELETE /v1/sheet-vitrina-v1/settings/sku-groups/{group_key}`
- `GET /v1/sheet-vitrina-v1/settings/users`
- `POST /v1/sheet-vitrina-v1/settings/users`
- `PATCH /v1/sheet-vitrina-v1/settings/users/{user_id}`
- `DELETE /v1/sheet-vitrina-v1/settings/users/{user_id}`

Contract keeps runtime truth inside hosted WebCore and does not move supplier/order/nomenclature logic into Apps Script.

## Repo-Owned Execution Entrypoint

Canonical runner:
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`

The same runner owns the only production path for the active functional cutover after deploy:
- `warehouse-functional-dry-run --output <absolute local plan.json>` captures coherent primary sources and fresh complete official WB stock without derived writes;
- `warehouse-functional-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>` pins the active EU target/runtime, creates a coherent `0600` integrity-checked backup and atomically applies six-stage balances, frozen historical cost projection and initial calculation parameters;
- `warehouse-functional-readback` proves stage/source/capital reconciliation and the exact cutover identity;
- repeated exact apply must return idempotent/no-op and create no second movement;
- `warehouse-functional-economics-dry-run --output <absolute plan.json>` and `warehouse-functional-economics-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>` are the only bounded `2026-07-01+` ready-snapshot WB cost/Proxy 3 publication path; apply preserves a separate `0600` backup and proves non-target digest invariance and idempotent readback;
- `warehouse-functional-sync` is the bounded manual/hourly WB pipeline: official supply refresh, supply-specific downstream component materialization, complete official stock capture, one canonical warehouse/cost publication and targeted Proxy publication; it does not invoke legacy daily cost/product-capital rebuild or the global vitrina refresh;
- `warehouse-functional-enable-hourly` enables the timer only after successful cutover/readback;
- `warehouse-functional-rollback --fingerprint <exact stored sha256:...>` removes only functional derived state after another backup;
- `warehouse-ui-flow --evidence-dir <absolute path outside repo>` uses a fresh isolated Playwright context and reconciles navigation, six warehouses, WB contour, settings/reference, Proxy 3, supplier cost/bank fee fields, consumers, legacy redirects and sync freshness with protected readback. Evidence stays outside Git.

Ad-hoc SQL, arbitrary remote commands and server-only scripts are not valid initialization paths. `warehouse_opening_v1` remains immutable audit under migration 102; active sources/non-target invariants are fixed in module 48 and `migration/103_warehouse_functional_cutover.md`.

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
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-wb-finance-weekly.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-wb-finance-weekly.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-spp-tester-schedule-tick.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-spp-tester-schedule-tick.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-data-mcp.service` is the separate read-only MCP boundary. It is a managed, enabled and restarted unit on the active EU target, listening only on `127.0.0.1:8766`. Its checked-in defaults cap HTTP workers at `16`, tool workers at `8`, each tool at `12s` and structured result bytes at `524288`. The nginx allowlist may publish only exact OAuth/MCP locations; owner-only OAuth 2.1 + PKCE remains the ChatGPT auth path. MCP lifecycle logs and health summaries must contain only request/correlation id, tool, safe identity hash, status, duration and result size—never args, credentials, paths or business payloads.

`wb-core-sheet-vitrina-refresh.timer` is a due-check ticker, not the business-time source of truth: it runs every 10 minutes and starts `apps/sheet_vitrina_v1_auto_refresh_tick.py`; the runner reads runtime JSON schedules (`11:00`/`20:00 Asia/Yekaterinburg` by default, editable through the web-vitrina auto-schedules API), builds an in-memory WebCore session cookie from hosted env, and then calls the protected refresh route with `auto_refresh=true`. The backend auto-refresh cycle first refreshes the web-vitrina ready snapshot and then runs a nonfatal WB supplies official incremental sync; the result payload/logs expose `wb_supplies_auto_sync_status` and `wb_supplies_auto_sync` diagnostics, while WB supplies failure or Seller Portal transit-cost preflight failure is warning metadata rather than a critical web-vitrina snapshot failure. The timer itself is non-persistent; catch-up is owned by the runner's schedule state so a deploy/restart does not immediately fire a stale systemd event while the app process is restarting.

`wb-core-spp-tester-schedule-tick.timer` is likewise a non-persistent one-minute due ticker for `apps/wb_spp_tester_schedule_tick.py`. The single daily SPP schedule, consent, next due time, last business-date claim and automatic result stay under `sheet_vitrina_v1_prices/spp_tests/schedule.json`; the timer does not own business time. The runner atomically claims one schedule/business date, uses the same cross-process SPP execution lock and existing `WbSppTesterBlock`, captures a fresh baseline immediately before a due run, enforces mandatory restore and records scheduled starts/skips in the existing job/audit files. Catch-up is limited to 15 minutes; later missed runs are visible skips and advance to the next day without a price write. The oneshot has `TimeoutStartSec=3h`, long enough for the bounded safe-slow probe and final restore instead of inheriting systemd's short default start timeout.

Canonical repo-owned public route allowlist:
- `artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json`

The managed nginx block renders `client_max_body_size 32m` for the public WebCore routes so real supplier invoice XLSX uploads reach the app instead of failing at nginx with an HTML `413 Request Entity Too Large` page.

Runner работает от current checked-out worktree и поэтому применим к незамёрженному branch/PR without merge-before-verify, если доступны safe deploy rights.

Supported commands:
- `print-plan`
- `deploy`
- `loopback-probe`
- `public-probe`
- `deploy-and-verify`

## GitHub Release Train Binding

Repo-owned [GitHub Release Train](11_github_release_train.md) является optional explicit queue binding поверх этого canonical runner, а не вторым deploy implementation. Для STANDARD PR с `task:standard + scope:live-runtime + release:ready` workflow обязан до merge доказать required `baseline` на final head SHA и SSH connectivity к active EU target, затем squash-merge exact head, checkout exact merge SHA и вызвать только:

`python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy-and-verify`

LOOP PR использует тот же единственный deploy command, но после final sync/baseline сначала обязан остановиться на `release:awaiting-agent`. Exact-head acknowledgement одноразово потребляется перед merge. После успешного deploy/verify LOOP получает `release:awaiting-ui`, а не terminal success; несвязанные releases ждут UI acceptance либо exact-linked recovery. Ни agent handshake, ни UI gate не меняют canonical deploy implementation или target.

Production failure после merge ставит global `release:halted` и блокирует следующие releases. `scope:repo-only` не вызывает deploy. `scope:production-mutation` автоматически не выполняется. GitHub Environment secrets `WB_CORE_DEPLOY_SSH_KEY` и `WB_CORE_DEPLOY_KNOWN_HOSTS` остаются вне Git; отсутствие любого из них блокирует live PR до merge.

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
- `managed_systemd_units = wb-ai-api.service + refresh.service + refresh.timer + closure-retry.service + closure-retry.timer + feedbacks-auto-complaints-tick.service + feedbacks-auto-complaints-tick.timer + spp-tester-schedule-tick.service + spp-tester-schedule-tick.timer + wb-finance-weekly.service + wb-finance-weekly.timer + wb-core-data-mcp.service`
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
- `WB_CORE_WEB_AUTH_USERNAME`
- `WB_CORE_WEB_AUTH_PASSWORD_HASH`
- `WB_CORE_WEB_AUTH_SESSION_SECRET`
- `WB_CORE_SUPPLIER_AUTH_USERNAME` (optional supplier-only account)
- `WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH` (optional supplier-only account)
- `WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME` (optional)
- `WB_PRICES_WRITE_ENABLED` (optional safety gate; default false)
- `WB_SPP_TEST_ENABLED` (optional SPP tester safety gate; default false)
- `WB_SPP_TEST_SCHEDULE_LATE_WINDOW_MINUTES` (optional bounded catch-up override; default `15`)
- `WB_BUYER_RECOVERY_LOCK_WAIT_SEC` (optional bounded supervisor wait for an in-flight buyer-session preflight; default `90`)
- `WB_BUYER_SESSION_VALIDATION_NM_ID` (optional positive read-only product used for persistent-profile restart proof; default `497416931`)

Production WebCore auth is app-level session auth, not nginx basic auth. The password hash uses the entrypoint PBKDF2-HMAC format `pbkdf2_sha256$iterations$salt_b64$digest_b64`; plaintext credentials must stay outside Git/docs/logs and are handed to the owner separately. `WB_CORE_WEB_AUTH_REQUIRED=1` may be set to fail closed when auth env is incomplete. The env web principal is the bootstrap/fallback `admin`; runtime users are stored server-side in SQLite `sheet_vitrina_v1_users` and may have legacy technical roles `admin`, `operator`, `supply_operator` or `supplier`, but shell/API authorization is section-based through `allowed_sections` plus internal `manage_users`. The internal `GET /sheet-vitrina-v1/instructions` route is protected by the independent `instructions` section: admin has it through administrative semantics, while non-admin users require an explicit setting through the existing user-management API; nginx publication never makes it public. Supplier env credentials are optional and remain backward-compatible supplier-only; when absent, supplier login is unavailable, but users with the `supply` section can access `Поставки -> От поставщика` through the shell.

The authenticated `supplier` principal is a distinct data-security boundary inside the supplier-shipment route family. Its list/detail/parse/create/update HTTP responses are server-side allowlisted before serialization and its HTML is a dedicated supplier-safe template; browser flags, query parameters and iframe state cannot upgrade this projection. Stored shipment invoice/contract, order-document archives/logistics packages and financial-document list/detail/file routes return consistent `403` to supplier even for known valid URLs. Internal `admin`, `operator` and `supply_operator` principals with `supply` access keep the full financial/document read model and download workflow.

WebCore Data MCP is a separate read-only data/diagnostics gateway and must not expose browser session cookies as its MCP auth boundary. Its repo-owned runner is `apps/webcore_data_mcp_server.py`, defaulting to loopback `127.0.0.1:8766` and `POST /mcp`. The public nginx allowlist can route only exact OAuth/MCP paths to that loopback upstream: `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`, `/oauth/authorize`, `/oauth/token` and `/mcp`; no prefix/static/runtime-file exposure is part of the MCP route contract. Relevant env names are:
- `WEBCORE_DATA_MCP_HOST`
- `WEBCORE_DATA_MCP_PORT`
- `WEBCORE_DATA_MCP_PATH`
- `WEBCORE_DATA_MCP_HEALTH_PATH`
- `WEBCORE_DATA_MCP_AUTH_MODE`
- `WEBCORE_DATA_MCP_BEARER_TOKEN` or `WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256`
- `WEBCORE_DATA_MCP_DB_PATH` (optional override; default is `REGISTRY_UPLOAD_RUNTIME_DIR/registry_upload_runtime.sqlite3`)
- `WEBCORE_DATA_MCP_AUDIT_LOG_PATH`
- `WEBCORE_DATA_MCP_RESOURCE_URL`
- `WEBCORE_DATA_MCP_RESOURCE_DOCUMENTATION_URL`
- `WEBCORE_DATA_MCP_AUTHORIZATION_SERVERS`
- `WEBCORE_DATA_MCP_SCOPES`
- `WEBCORE_DATA_MCP_OAUTH_ISSUER`
- `WEBCORE_DATA_MCP_OAUTH_SIGNING_SECRET`
- `WEBCORE_DATA_MCP_OAUTH_OWNER_USERNAME` or `WB_CORE_WEB_AUTH_USERNAME`
- `WEBCORE_DATA_MCP_OAUTH_OWNER_PASSWORD_HASH` or `WB_CORE_WEB_AUTH_PASSWORD_HASH`
- `WEBCORE_DATA_MCP_OAUTH_SESSION_SECRET` or `WB_CORE_WEB_AUTH_SESSION_SECRET` (optional session-cookie auto-consent)
- `WEBCORE_DATA_MCP_OAUTH_CODE_STORE_PATH`
- `WEBCORE_DATA_MCP_OAUTH_ALLOWED_REDIRECT_PREFIXES`
- `WEBCORE_DATA_MCP_OAUTH_ALLOWED_CLIENT_ID_PREFIXES`
- `WEBCORE_DATA_MCP_OAUTH_CODE_TTL_SECONDS`
- `WEBCORE_DATA_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS`
- `WEBCORE_DATA_MCP_MAX_HTTP_WORKERS`
- `WEBCORE_DATA_MCP_MAX_TOOL_WORKERS`
- `WEBCORE_DATA_MCP_TOOL_DEADLINE_SECONDS`
- `WEBCORE_DATA_MCP_MAX_TOOL_RESULT_BYTES`

Default MCP OAuth scopes:
- `webcore.analytics.read`
- `webcore.supply.read`
- `webcore.finance.read`
- `webcore.ops.read`

Active EU private MCP live state:
- `wb-core-data-mcp.service` is installed and enabled as a loopback-only private service;
- the generated bearer secret is stored outside Git in root-only `/etc/wb-core-data-mcp.env`;
- `/etc/wb-core-data-mcp.env` is consumed by the MCP unit and must not be printed, committed, copied into docs or placed in `/opt/wb-ai/.env`;
- when `https://api.selleros.pro/mcp` is published, unauthenticated access must be auth-blocked with no business data; ChatGPT connector access uses owner-only OAuth 2.1 auth-code + PKCE S256. Bearer auth may remain enabled only as a server/admin diagnostic path and must not be documented as the final ChatGPT auth mode.

MCP publication gate:
- unauthenticated `POST /mcp` must return `401` with no business data;
- `tools/list` must expose the compact primary profile (currently 16 names) while known legacy names remain callable compatibility-only;
- every exposed tool must have exact input/output schemas and full read-only/non-destructive/idempotent/closed-world annotations;
- every exposed tool must carry an OAuth `securitySchemes` scope in the `webcore.*.read` namespace;
- ops diagnostics tools must carry only `webcore.ops.read`, accept only enum allowlists/bounded date-log args, and return sanitized summaries for fixed units/logs/refresh-load state/snapshots/deploy labels;
- the DB read path must use SQLite `mode=ro` and `PRAGMA query_only=ON`;
- no MCP tool may expose arbitrary SQL, shell/SSH, arbitrary filesystem browsing, upstream sync/backfill/refresh/load, restart, supplier write/upload/rematch/price-check, runtime file download, secrets, storage-state content, raw env or raw payload dumps;
- OAuth authorization codes are one-time, short-lived and stored outside Git under runtime state; access tokens are short-lived, HMAC-signed, audience-bound to `WEBCORE_DATA_MCP_RESOURCE_URL`, scope-bound and never logged or printed.
- the canonical deploy writes `.wb-core-deploy.json` only through the repo-owned runner after rsync; `deploy_state` must read that safe file and expose the active 40-character commit plus deploy timestamp even though `.git` is excluded from production;
- the canonical deploy must install/enable/restart `wb-core-data-mcp.service`, then live verification must cover authenticated initialize/list/direct business/ops calls, concurrent latency and the commit equality check;
- after model-visible metadata changes, connector refresh in the ChatGPT UI may remain the single human-only post-deploy step.

Current required upstream secret contract stays:
- `WB_API_TOKEN`
- `OPENAI_API_KEY`

Optional runtime overrides remain the same as in current official-api boundary:
- `WB_OFFICIAL_API_BASE_URL`
- `WB_ADVERT_API_BASE_URL`
- `WB_SELLER_ANALYTICS_API_BASE_URL`
- `WB_STATISTICS_API_BASE_URL`
- `WB_FEEDBACKS_API_BASE_URL`
- `WB_SUPPLIES_API_BASE_URL`
- `WB_PRICES_API_BASE_URL`
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
  - current canonical packages = `openpyxl==3.1.5`, `xlrd==2.0.1`, `playwright==1.58.0`, `pypdf==6.4.1`, `reportlab==4.4.5`
  - deploy runner installs them on host before restart if they are still missing;
  - deploy runner also verifies or installs Playwright Chromium with host browser dependencies before restart.
- current seller-portal relogin recovery on the EU hosted runtime is repo-owned dependency setup, not a manual one-off host state:
  - `/opt/wb-web-bot/venv/bin/python` must exist and carry `playwright==1.58.0` plus `psycopg2-binary==2.9.11` for seller-session probes and owner capture DB writes;
  - `/opt/wb-web-bot/storage_state.json` remains runtime data and is never created, printed or deleted by deploy;
  - the deploy runner creates/repairs `/opt/wb-web-bot/venv` with `python3 -m venv`, installs pinned packages there and ensures Chromium can launch from both the hosted runtime system python and the wb-web-bot venv.
- live Seller Portal automation uses one shared single-flight lock at `/opt/wb-core-runtime/state/seller_portal_automation.lock.json`. Status sync, complaint submit/batch, auto-complaints tick/run-now, target/matching/filter scouts, dry-run, confirmation/detail probes, relogin/recovery and future complaint parser/export browser jobs must acquire this lock before opening Playwright; public status/submit/auto launchers return controlled `seller_portal_automation_busy` metadata instead of starting a second browser context. Stale lock cleanup requires process/heartbeat evidence, and reports may include only safe lock fields (`owner`, `purpose`, `run_id`, `started_at`, `pid`, `host`, `expected_max_seconds`, `heartbeat_at`) without cookies/tokens/headers/storage-state content.
- EU live Seller Portal jobs use canonical bot session state `/opt/wb-web-bot/storage_state.json` by default, or an explicit `SELLER_PORTAL_STORAGE_STATE_PATH` pointing to that live contour. They must not implicitly fall back to local Mac paths such as `/Users/.../storage_state.json`; an invalid live path is a storage-state policy blocker, not a hidden fallback. The dedicated bot account/recovery process materializes only this canonical EU runtime file and never prints its contents.
- Seller Portal browser tasks validate the route capabilities they need rather than treating a generic cabinet page as enough. Capability names include `seller_portal_base`, `feedbacks_list`, `complaints_pending`, `complaints_answered`, `analytics_supplier_context` and `canonical_supplier_context`; status sync requires both complaints status tabs, parser/export requires `complaints_answered`, and submit/batch requires feedbacks list plus the guarded row/modal gates. Auth redirects become precise blockers such as `session_invalid_for_route: complaints_answered`.
- Navigation policy is human-stable and bounded, not stealth/evasion: warm up Seller Portal base, wait for canonical supplier/app shell, enter the relevant module, then open deep status URLs or tabs; after major navigation, tab/filter changes, pagination/detail drawer transitions and submit/status-changing actions, runners use deterministic short settle waits and stop on auth redirect rather than looping reloads.
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
- these packages are used only by the repo-owned seller-session recovery contour `apps/seller_portal_relogin_session.py`; it binds noVNC to `127.0.0.1` on the host, issues a per-run `run_id`, must materialize a real visible headed Chromium window before surfacing `awaiting_login`, and is intended for temporary auth recovery rather than for the steady-state ingest path. Recovery writes updated `storage_state.json` only after validated auth plus canonical supplier confirmation/safe switch, stages the validated candidate, atomically replaces the canonical file, then runs a fresh post-save probe and restores the previous backup if that probe fails. After saving a validated session, its post-login refresh trigger calls the protected WebCore refresh route with an in-memory app-session cookie derived from the hosted env file; cookie material, password hashes, session secrets and storage-state contents must not be printed.
- current steady operator path over that tool is bounded and HTTP-owned:
  - `GET /v1/sheet-vitrina-v1/seller-portal-session/check`
  - `POST /v1/sheet-vitrina-v1/seller-portal-recovery/start`
  - `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
  - `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status`
  - `POST /v1/sheet-vitrina-v1/seller-portal-recovery/stop`
  - `GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status` must remain a 200-shape status surface even when the EU host is missing `/opt/wb-web-bot/venv/bin/python`; that state is surfaced as a seller-session probe error, not as a public 500. Recovery payloads expose machine-readable lifecycle gates: `run_id`, `run_status`, `running`, `launcher_ready`, `can_download_launcher`, `can_open_login_window`, `launcher_url`/`launcher_download_path`, `reason`, `summary`, `storage_state_path`, `final_marker` and the separate `session_status`. Default reads may perform the current session probe; `probe=0` is the cheap run-state read for UI refreshes that must not synchronously probe Seller Portal.
- the downloadable launcher stays Mac-only and does not expose noVNC publicly: `launcher.zip` is a zip only while the current recovery run is `awaiting_login` and `can_download_launcher=true`; outside that state it returns truthful `409` JSON, not public `500`. The 409 body classifies known states (`no_active_run`, `run_starting`, `launcher_artifact_missing`, `run_replaced`, `run_final`, `launcher_not_ready`) so the UI can keep `starting` as normal "окно готовится" state instead of showing a fatal download failure. The launcher binds to the current recovery `run_id`, opens a SSH tunnel to the localhost-only host port, waits for local HTTP-ready, launches `http://127.0.0.1:<port>/vnc.html?...path=websockify&reconnect=1` locally, polls `GET /seller-portal-recovery/status?run_id=...` and always prints a final completion marker (`completed / not_needed / stopped / timeout / error`) before exiting.
- the buyer-session recovery for `Цены -> Проверка СПП` reuses only the installed headed-browser OS/runtime dependencies, never Seller Portal storage or lock. Its canonical state is the protected `0700` persistent Chromium profile `/opt/wb-core-runtime/state/wb_buyer_session/chromium_user_data` plus HMAC-only account metadata; the old `storage_state.json` remains untouched and may be read once only for best-effort cookies/localStorage migration. Session check, recovery, saved-account auto-login and authenticated SPP price reads all use `chromium.launch_persistent_context` with that same `user_data_dir`. Buyer use of `browser.new_context(storage_state=...)`, candidate storage state, `context.storage_state(...)`, IndexedDB snapshot capture and validation in a browser importing a snapshot is forbidden.
- buyer recovery has one single-flight start lock and one supervisor-owned automation lock, localhost-only VNC/web ports and protected routes `.../prices/spp-test/buyer-session/check`, `.../recovery/status`, `.../recovery/start`, `.../recovery/stop`, `.../recovery/launcher.zip`. Opening the SPP subsection first rejoins an existing exact `run_id`; concurrent UI/status/price requests report/join that run and cannot launch browser contexts. The supervisor starts headed persistent Chromium on Xvfb, checks `/lk`, clicks exactly one safe saved-account action, and starts VNC/websockify only for a real SMS/OTP/phone/CAPTCHA/security/multiple-account challenge. After login it requires `/lk` plus a successful read-only authenticated price response, closes the first persistent context completely, launches a second Chromium process with the same profile and repeats both proofs before `completed` is published after lock release. A stable account id from an authenticated response is stored only as HMAC and mismatches are blocked; missing account-id data is not logout and does not block read-only SPP after real browser proof.
- buyer recovery supervisor cleanup is bound to exact `run_id + PID + process group + process-start` evidence. Launcher lifetime polls the exact terminal recovery status, closes the localhost noVNC tab and SSH tunnel, and does not infer completion only from a disappearing port.
- the buyer recovery supervisor is an isolated process-group leader; Xvfb/openbox/x11vnc/websockify and its browser stay in that same group. `stop` terminates the complete group and removes supervisor identity, while normal completion closes both persistent contexts, terminates every display/noVNC child and releases lock-owner metadata. No terminal path deletes the persistent profile or legacy `storage_state.json`.
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` for `source_group_id=seller_portal_bot` must preflight the same canonical Seller Portal session before heavy source fetch. Invalid, missing, wrong-supplier or probe-failed sessions return an action-required job result with `failed_stage=session_preflight`, session status/reason and operator guidance; valid canonical sessions continue into the normal bot-dependent source fetch over the same `/opt/wb-web-bot/storage_state.json` contract.

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
- Live/public closure must first prove unauthenticated operator/product routes are blocked by login/401 and then authenticate through the app-level login cookie before reading `/sheet-vitrina-v1/vitrina`, one bounded `GET /v1/sheet-vitrina-v1/feedbacks?...`, `GET/POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt` and one bounded small `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` on the hosted runtime when AI feedback analysis changes. This verifies route wiring, auth cookie compatibility for same-origin fetches, `WB_API_TOKEN` permission for feedbacks, `OPENAI_API_KEY` visibility to the service without printing the key, friendly upstream error surfacing and normalized JSON shape without `/load`, Google Sheets/GAS, bypassing Seller Portal safety gates or accepted-truth persistence.

Google Sheets, GAS, `clasp`, `/v1/sheet-vitrina-v1/load` and `invalid_grant` are not active blockers for web-vitrina completion. If a task explicitly changes archived Apps Script guard code, verify blocked/archived behavior only.

`deploy-and-verify` may be used as one combined step when access is already safe and available.

Current deploy contract note:
- `deploy` does more than `rsync + restart`:
  - sync current checkout;
  - ensure host OS dependencies for SellerPortalBot recovery are present (`python3-pip`, `python3-venv`, `xvfb`, `x11vnc`, `novnc`, `websockify`, `openbox`);
  - use the same installed headed-browser dependencies for the independent WB buyer-session recovery while preserving separate state, lock and ports;
  - ensure host OS dependencies for SellerPortalBot owner runtime are present (`postgresql`, `postgresql-client`);
  - ensure required hosted runtime python packages are present (`openpyxl==3.1.5`, `xlrd==2.0.1`, `playwright==1.58.0`, `pypdf==6.4.1`, `reportlab==4.4.5`);
  - create/repair `/opt/wb-web-bot/venv`, install `playwright==1.58.0` and `psycopg2-binary==2.9.11` into it and ensure Playwright Chromium can launch from both Python contexts;
  - create/repair `/opt/wb-ai/venv`, install the pinned local API/handoff packages and verify `/opt/wb-web-bot/bot` plus `/opt/wb-ai/run_web_source_handoff.py` imports;
  - install/update repo-owned systemd units when configured;
  - render the repo-owned nginx public route allowlist into the configured server block, create a timestamped backup before changing the file, validate with `nginx -t`, and reload nginx only after validation succeeds;
  - restart runtime;
  - only after that run loopback/public verification.
- nginx public route publishing is idempotent: the runner removes prior `WB-CORE MANAGED PUBLIC ROUTES` block, prior `WB-CORE MANAGED TLS` block and matching legacy/manual locations from the configured server config, rewrites the target `server_name` directive to the target's explicit `nginx_public_routes.server_names` when provided, then inserts generated TLS and route blocks from target/manifest truth. New public routes for this contour must be added to that manifest and verified through the deploy runner; manual live nginx edits are not the completion path.
- The allowlist intentionally uses exact locations plus narrow route-family prefixes such as `/v1/sheet-vitrina-v1/warehouses/`, `/v1/sheet-vitrina-v1/supply/factory-order/`, `/v1/sheet-vitrina-v1/supply/wb-regional/` and `/v1/sheet-vitrina-v1/research/`; broad catch-all publication is not part of the current contract.

If deploy / publish / restart / probe / required verify steps are safe and available, Codex обязана выполнить их в том же bounded execution. `clasp` is part of this list only for archived Apps Script guard changes.
If any of these steps are unavailable or unsafe, execution must return incomplete with an exact blocker instead of a vague ops-gap.

## Probe Norm

Loopback/runtime probe validates the hosted process behind the reverse proxy or equivalent publish layer.

HTTP probe boundary: canonical `loopback-probe`, `public-probe` and `deploy-and-verify` доказывают transport, auth-aware route/content shape и service health, но HTTP `200`, `curl` или наличие HTML не являются полноценной production UI-проверкой. Когда acceptance требует UI evidence, дополнительно применяется browser contract из [`07_codex_execution_protocol.md`](07_codex_execution_protocol.md): фактический Playwright/Browser render, DOM/final URL, отсутствие `5xx`/`pageerror`/fatal surface, классификация существенных console errors и визуально проверенный screenshot. Этот UI Flow по умолчанию использует изолированный context без пользовательского profile/cookies/credentials и не выполняет clicks, input или business mutations вне explicit scope.

Canonical `loopback-probe`, `public-probe` and `deploy-and-verify` are auth-aware and fast by default:
- when production WebCore app-level auth is configured, the runner may create a short-lived same-origin session cookie from the hosted runtime env file and use it only in memory for probe requests; the cookie, password hash and session secret must not be printed in JSON, logs, PR text or handoff;
- unauthenticated browser/curl reads may return login redirect or auth error after auth hardening, but the canonical runner must verify the authenticated operator surface with sanitized auth metadata only;
- bounded JSON reads must continue across short socket chunks until EOF or the configured byte limit plus one byte; a short transport read is not EOF and must not turn a valid large response into a false `expected JSON object response` failure;
- full `POST /v1/sheet-vitrina-v1/refresh` is a heavy mutating/deep check and is not part of ordinary health probes; it runs only when `--include-refresh` is passed, while `--skip-refresh` remains a compatibility force-skip flag;
- deploy closure must use canonical probes for service health and may run the explicit deep refresh probe only when the task scope actually changes refresh semantics.

Public probe validates:
- `GET /sheet-vitrina-v1/operator` returns `200` + `text/html` for the unified shell; public probe also checks `GET /sheet-vitrina-v1/operator?embedded_tab=reports` for the embedded report panel and `GET /sheet-vitrina-v1/operator?embedded_tab=factory-order` for the embedded supply panel. Together they must contain compact operator tokens for the top-level sections, server refresh, truthful manual-vs-auto blocks, report subsections, plan-report baseline controls, feedbacks tab, right-side settings action and both bounded supply subsections (`Ручная загрузка данных`, `Проверка и восстановление Seller-сессии`, `Текущий запуск`, `Финал запуска`, `Статус сессии`, `Поставки`, `Отчёты`, `Отзывы`, `Настройки`, `Выйти`, `Загрузить отзывы`, `Загрузить данные`, `Legacy Google Sheets`, `Ежедневные отчёты`, `Отчёт по остаткам`, `Выполнение плана`, `Равномерный годовой план`, `Прогноз к концу договорного периода при текущем темпе`, `Исторические данные для отчёта`, `planReportApplyButton`, `planReportAnnualEvenCheckbox`, `planReportProjectionTable`, `planReportBaselineTemplateButton`, `planReportBaselineFileInput`, `Total Order Sum`, `Негативные факторы`, `Позитивные факторы`, `Скачать лог`, `Лог`, `Автообновления`, `Часовой пояс`, `Следующий`, `Последний запуск`, `Последний успех`, `Статус`, `Общий вход для двух расчётов`, `Заказ на фабрике`, `Поставка на Wildberries`, `Цикл заказов`, `Цикл поставок`, `Скачать все рекомендации`). The ordinary plan-report UI must not expose legacy contract-start checkbox/date controls; it sends canonical hidden contract-start params and sends `annual_plan_evenly_distributed=false|true` from the optional annual-even checkbox. Plan-report results render the contract-period projection card before selected/MTD/QTD/YTD cards, and long per-metric diagnostics such as ads-plan-base explanations are exposed through compact `?` tooltips rather than inline metric subtitles. The read-only `сессия |` indicator and `Проверить сессию` / `Установить сессию` contract is owned by the `/sheet-vitrina-v1/vitrina` web-vitrina page.
- The embedded supply probe also requires the `Счёт CNY` / `Конвертации RUB → CNY` delete wiring, including the existing CNY documents route token and the explicit balance/rate/ledger replay warning. Runtime UI verification must confirm that eligible conversion rows expose `Удалить`, source-owned rows do not expose an active direct delete, and no real production document is deleted during deploy verification.
- `GET /v1/sheet-vitrina-v1/seller-portal-session/check` returns `200` + JSON with one truthful status from `session_valid_canonical / session_valid_wrong_org / session_invalid / session_missing / session_probe_error` plus secret-free probe reason when available (`login_redirect`, `validate_401`, `security_challenge`, `access_denied`, `login_page`, `probe_failed`, etc.)
- `GET /sheet-vitrina-v1/vitrina` returns `200` + `text/html` as a real operator-grade web-vitrina page shell: page must contain `Web-витрина`, `Операторский сайт`, primary `Загрузить и обновить`, top Seller session indicator rendered as lowercase word `сессия` + neutral grey separator `|`, without a leading dot/bullet, main tabs `Витрина`, `Поставки`, `Отчёты`, `Отзывы`, `Исследования`, right-side system actions `Инструкции`, `Настройки` and `Выйти`, block `Автообновления`, canonical JSON route token `/v1/sheet-vitrina-v1/web-vitrina`, auto-schedules route token `/v1/sheet-vitrina-v1/web-vitrina/auto-schedules`, feedbacks route token `/v1/sheet-vitrina-v1/feedbacks`, settings users route token `/v1/sheet-vitrina-v1/settings/users`, explicit `surface=page_composition` wiring, bottom `Действия и состояния`, grouped date-scoped `Обновить группу` controls and exactly two Seller Portal session action controls: `Проверить сессию` and `Установить сессию`. Only the word `сессия` takes the green/red session tone; the container, separator, update timestamp and neighboring text stay neutral. The top session indicator is read-only, non-clickable and derived from existing last-run runtime/source-status/result-state; page open/table reread must not call `seller-portal-session/check` only for the indicator. `Проверить сессию` is manual-only and may update the UI with active/expired/check-error. `Установить сессию` starts the existing recovery launcher flow and downloads the launcher as soon as it is ready, without rendering extra `Скачать launcher`, `Открыть launcher`, repeated recovery buttons, fallback links or stepper controls. `JSON Connect`, the old cheap top-panel `Обновить` button and the permanent top status badge are not rendered. Page open must not trigger hidden full refresh or hidden heavy group/source fetch.
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition` returns `200` + bounded JSON `web_vitrina_page_composition` v1 with `meta`, `summary_cards`, `filter_surface`, `table_surface`, `status_summary`, `capabilities`; route stays read-only, defers heavy `table_surface.rows` unless `include_table_data=1` is explicit, and must not trigger refresh/upstream fetch from the public read path
  - summary/card tone must follow semantic source truth of the visible snapshot or selected period, not mere snapshot existence
  - main table must render before filters/history/actions, use Russian visible headers and expose per-row `Обновлено` timestamp without renaming backend/API field keys
  - `Загрузка данных` must render in the bottom actions block as a grouped compact table with source-group headers `WB API`, `Seller Portal / бот`, `Прочие источники`, one compact date input and one `Обновить группу` action per group, group-level last update timestamp, server/business `Сегодня: <YYYY-MM-DD>` and `Вчера: <YYYY-MM-DD>` status columns, reason columns, Russian metric labels and a secondary technical endpoint column; it must not fabricate stale-job success when exact transient log association is unavailable. The three groups must cover every visible main-table metric exactly once, with residual calculated/formula metrics assigned to `Прочие источники`.
  - `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` must be publicly routed to the hosted runtime. A POST without `source_group_id` is the safe publish probe and must return app-level `400 {"error":"source_group_id is required"}`, not proxy/fallback `404 {"detail":"Not Found"}`. With supported `source_group_id` and `as_of_date`, it must return an async job payload and the job/log must report selected date plus stage-aware source fetch / prepare-materialize / load-to-vitrina outcome, including `updated_cells`/`latest_confirmed_cells` counters. The page may use returned `updated_cells` for session-only green/yellow highlighting, but no permanent styling state is stored.
  - `Лог` must render below that table as the secondary block and keep the existing job/log download contour
  - the former sibling block `Обновление данных` is no longer rendered or exposed as an active page-composition activity block; persisted STATUS/read-side fields remain internal truth for other status contracts
  - top summary must be compact (`Обновлено`, `Статус`, `Период`); the old bulky `Свежесть данных`/`Строки` cards are not separate page blocks. Auto freshness must be visible in `Автообновления` through last run, last success, next run and last status/error.
- `GET/POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules` returns/persists runtime-managed web-vitrina refresh schedules. Default rows are `11:00` and `20:00 Asia/Yekaterinburg`, but business cadence is editable in runtime JSON; response exposes timezone, mutability, `next_auto_run_at`, `last_auto_run_at`, `last_auto_success_at`, `last_auto_error_summary` and per-schedule next/last/status fields.
- `POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules/run-now` launches the existing async full-refresh job with auto-schedule trigger metadata. The route must return a job payload quickly and must not call archived Google Sheets/GAS load.
- `GET /v1/sheet-vitrina-v1/prices/spp-test/history`, bounded by `limit<=50` and an opaque cursor, reads existing and new `sheet_vitrina_v1_prices/spp_tests/jobs/*.json` newest-first; `GET /v1/sheet-vitrina-v1/prices/spp-test/history/{job_id}` accepts only a safe job id and returns sanitized lifecycle detail without headers, credentials or internal paths. Legacy jobs keep `trigger_source=null`; new jobs record `manual` or `schedule`.
- `GET/POST /v1/sheet-vitrina-v1/prices/spp-test/schedule` reads and saves the single server-owned daily schedule. An enabled save requires explicit consent for future temporary price changes, computes a strictly future `next_run_at` in `Asia/Yekaterinburg` and never starts a job inline. The due ticker atomically claims one business date, refuses an active or not-proven-restored predecessor through the shared execution lock, captures a fresh baseline and always runs with restore enabled. A missed due time is eligible for at most 15 minutes; later ticks persist a visible `skipped` history result and advance to the next business date without a price write.
- the SPP tester performs the dedicated buyer-session preflight before manual/scheduled start and immediately before every measurement write. The initial preflight joins/starts automatic recovery; successful saved-account recovery continues, while SMS/other human requirements return `action_required` before seller writes. Invalid/missing/wrong-account session prevents writes; missing fingerprint material alone does not, because current persistent-profile `/lk` plus authenticated price proof is authoritative. Mid-run loss stops further measurements without anonymous fallback and proceeds to mandatory seller restore. Authenticated price is primary, anonymous public price is an independently stable control retargeted by a per-read adapter to the authenticated integer `dest`, and both contexts are persisted only as sanitized measurement evidence. The global module 35 destination/source is not mutated. Invalid or mismatched destination context blocks comparison/start.
- `GET /v1/sheet-vitrina-v1/prices/spp-test/status` reconciles an orphan only after the cross-process execution lock proves that no runner is alive. It then requires a fresh exact WB tuple (`price`, `discount`, `discountedPrice`) and no quarantine before terminalizing the job as `interrupted_restored`; TTL expiry alone is never restore proof. Authenticated/anonymous buyer availability is diagnostic and cannot invalidate exact seller restore. A seller mismatch or unsafe seller readback becomes `manual_restore_required` and blocks both manual and scheduled starts until guarded restore succeeds.
- `GET /v1/sheet-vitrina-v1/web-vitrina` returns either:
  - `200` + JSON `web_vitrina_contract` v1 when a ready snapshot is present, with root fields `contract_name`, `contract_version`, `page_route`, `read_route`, `meta`, `status_summary`, `schema`, `rows`, `capabilities`
  - truthful `422 {"error": ...}` when the ready snapshot is absent
  - route remains read-only, optional `as_of_date` override stays on the same boundary and must not trigger refresh/upstream fetch
- `GET /v1/sheet-vitrina-v1/feedbacks` returns `200` + JSON `sheet_vitrina_v1_feedbacks` v1 for a bounded valid query (`date_from`, `date_to`, optional `stars`, `is_answered`). It is read-only over official WB `GET /api/v1/feedbacks` with canonical `WB_API_TOKEN`; it must not trigger refresh, `/load`, Google Sheets/GAS, complaint submission or runtime persistence. If the hosted token lacks feedbacks permission, 401/403 is a real live blocker for the `Отзывы` feature rather than a deploy-script success.
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt` and `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt` manage server-side operational prompt config in the hosted runtime dir. This prompt is not ЕБД, accepted truth, ready snapshot truth or browser-local truth.
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` runs a bounded OpenAI Responses API structured-output call over loaded feedback rows. The browser processes the current visible/filtered operator set as a bounded sequential queue and sends exactly one feedback row per request; large visible sets must be rejected client-side with a clear narrowing message. The route still enforces a hard cap of 3 rows per request as a safety guard. Results and per-row failures remain transient for the current UI session and must not persist AI labels, submit complaints, call Seller Portal or write Google Sheets/GAS.
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected` is an auth-protected operator route for selected feedback rows. It must return quickly with a submit job `run_id`, reject `feedback_ids>20` and `max_submit>5`, allow only one active job, skip existing complaint-journal ids, and reuse the guarded Seller Portal submit runner/actionable resolver. It is not a public bypass around exact/actionable/description gates.
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job?run_id=...` returns bounded safe job state/events/counters/report paths for that route without secrets, headers, cookies, bearer tokens or storage state.
- `GET/POST /v1/sheet-vitrina-v1/feedbacks/automation/schedules`, `POST /v1/sheet-vitrina-v1/feedbacks/automation/run-now`, `GET /v1/sheet-vitrina-v1/feedbacks/automation/runs`, `GET /v1/sheet-vitrina-v1/feedbacks/automation/run?run_id=...` and `POST /v1/sheet-vitrina-v1/feedbacks/automation/tick` are auth-protected auto-complaints surfaces. Business schedules live in runtime JSON and default to `Asia/Yekaterinburg`; save returns canonical persisted rows, `run-now` accepts only persisted `schedule_id`, and stale ids return structured `schedule_not_found`. Schedule lifecycle fields (`created_at`, `enabled_since_at`, `last_run_at`, `last_success_at`, `last_due_at`, `last_status`, `last_run_id`, `last_stats`) are server-owned and are not accepted from browser save payloads. `run-now` also returns observable `run_id/status/summary` plus refreshed schedules/recent runs; the operator UI polls the sanitized run detail route and `Посмотреть лог` must show either the run report, explicit still-running guidance, or an explicit empty-log state. First runs process only the last 24 hours with no overlap fetch; recurring runs use server-owned `last_success_at..window_to` with a 24h overlap fetch. The systemd timer only invokes the idempotent due-check CLI every 10 minutes. Auto runs use the same feedback loader, saved AI analyzer, complaint journal idempotency, hard cap `5`, Seller Portal lock/storage-state policy and guarded selected-submit runner as manual complaint submission.
- `GET /v1/sheet-vitrina-v1/daily-report` returns `200` + JSON for both states:
  - `status=available` when two latest persisted ready snapshots `<= default_business_as_of_date(now)` are present and their `yesterday_closed` slots are comparable;
  - `status=unavailable` with truthful `reason` when fewer than two eligible ready snapshots exist or either selected snapshot is structurally unusable;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/stock-report` returns `200` + JSON for both states:
  - without explicit `as_of_date`, `status=available` when the latest persisted ready snapshot `<= default_business_as_of_date(now)` contains a valid `yesterday_closed` slot;
  - with explicit `as_of_date`, the route stays strict exact-read and returns `status=unavailable` when that exact ready snapshot or `yesterday_closed` slot is missing/stale;
  - optional `sales_avg_period_days=<positive integer>` controls the availability-adjusted `orderCount` averaging period; missing/empty uses the supply default `14`, non-integer returns controlled JSON `422`, and non-positive values follow the existing supply default semantics;
  - `rows[]` is the full active `config_v2` SKU table, not a legacy low-stock `<50` subset. Rows expose raw numeric sort fields: `supplier_production_qty`, `supplier_in_transit_qty`, `stock_ff`, `wb_supplies_inbound_qty`, `stock_wb`, backward-compatible `stock_total`, `zero_district_count`, `avg_sales_per_day`, `days_left_total`, and per-district `stock`, `avg_daily_burn`, `days_left`;
  - operator visible columns immediately after `Акция` are `на произв.`, `в пути Китай`, `ост. ФФ`, `поставки ВБ`, `ост. ВБ`. `на произв.` and `в пути Китай` are aggregated by active SKU `internal_nm_id` from existing supplier shipment product lines in statuses `production` (`На производстве`) and `in_transit` (`В пути`). `ост. ФФ` reads current balances from server-owned `ff_stock_ledger`. `поставки ВБ` is aggregated by `nmId` from current `sheet_vitrina_v1_wb_supplies` cache `raw_goods` quantity and excludes only status ids `1/2/5` (`Не запланировано`/`Запланировано`/`Принято`); all other status ids/labels are counted when goods composition has positive active-SKU quantity. `ост. ВБ` is the previous `stock_total` WB stock value from persisted ready snapshot, with data semantics unchanged;
  - payload also exposes `summary_row` for the top `Итого` row. The operator table keeps this row above detail rows during sorting; stock/supply values are summed and days-left values use aggregate stock / aggregate demand or burn rather than a simple row average;
  - operator header sorting stays local and must preserve the horizontal `scrollLeft` of the stock-report wrapper when the table DOM is rebuilt, for repeated ascending/descending clicks at the left edge, right edge and intermediate positions;
  - `promotion_participation` is read from canonical `promo_participation` in the persisted ready snapshot: numeric `>0` = `Да`, numeric `0` = `Нет`, missing = `н/д`/`null`;
  - district days-left uses persisted ready-snapshot depletion only: positive decreases between consecutive `yesterday_closed` district stock snapshots are averaged; missing, gap, restock/increase and zero-depletion days are diagnostics and are not fabricated as district расход;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/plan-report` returns `200` + JSON for valid primary query params `period`, `h1_buyout_plan_rub`, `h2_buyout_plan_rub`, `plan_drr_pct`, optional `as_of_date`, optional boolean `annual_plan_evenly_distributed`, and optional contract-start params; legacy complete `q1_buyout_plan_rub`..`q4_buyout_plan_rub` may be accepted only as transitional fallback:
  - response contains `selected_period`, `month_to_date`, `quarter_to_date`, `year_to_date` blocks plus `contract_period_projection`;
  - each block has independent `available / partial / unavailable` status, coverage details, reason, source mix and metrics; an unavailable YTD block must not hide an available selected period;
  - daily fact source is persisted accepted closed-day snapshots `fin_report_daily.fin_buyout_rub` + `ads_compact.ads_sum` for current active `config_v2` SKU;
  - buyout and ads daily facts use the same accepted temporal source slot layer but keep source-specific coverage; missing one source for a date keeps the block partial instead of dropping the other source's available fact;
  - manual monthly source `manual_monthly_plan_report_baseline` may contribute only full months inside this plan-report route; if daily precision for a baseline month is incomplete, the monthly aggregate covers the month and overlapping daily rows are excluded from the block to avoid double-count;
  - ordinary operator UI defaults, when namespaced browser state is missing or invalid, are `h1_buyout_plan_rub=155379879`, `h2_buyout_plan_rub=294620121`, `plan_drr_pct=6`, `use_contract_start_date=true`, `contract_start_date=2026-02-01`, `annual_plan_evenly_distributed=false`; default period follows the current WB/VB target half-year (`first_half` through 2026-06-30, then `second_half`); H2 is the annual remainder to `450000000`, while Q3+Q4 source figures sum to `294620120`, so the 1 rub discrepancy is explicit;
  - default buyout plan is distributed by calendar day, with the daily amount derived from the H1/H2 plan for each date and independent from fact coverage;
  - fixed target periods (`first_quarter`, `second_quarter`, `third_quarter`, `fourth_quarter`, `first_half`, `second_half`, and selected `current_year`) use the full target-period buyout plan for the main `plan`/completion, clipped by contract start when enabled; their facts and coverage use only the closed fact window up to `min(as_of_date, target_date_to)`, so future target dates are not missing coverage;
  - with `annual_plan_evenly_distributed=true`, plan values use `h1_buyout_plan_rub + h2_buyout_plan_rub` evenly across calendar days of the year or contract-start calculation window, without mutating persisted H1/H2 inputs; ordinary UI exposes this as an optional strategic pace view and defaults it to unchecked/`false`;
  - optional `use_contract_start_date=true&contract_start_date=YYYY-MM-DD` trims selected/MTD/QTD/YTD fact windows and fixed target windows before facts, plan and coverage are calculated; ordinary UI always sends `use_contract_start_date=true&contract_start_date=2026-02-01`; annual-even mode then uses the remaining annual calculation window from contract start through year end;
  - metrics expose `completion_pct = fact / plan * 100` for buyout and ads; legacy `delta_pct = (fact - plan) / plan * 100` remains diagnostic;
  - DRR fact is `ads_sum / fin_buyout_rub * 100`; `plan_drr_pct` is the contractual minimum, so a fact at or above it is `ok`/positive margin and only a value below it is a minimum violation; ads plan follows WB/VB execution semantics: `max(buyout_plan, buyout_fact) * plan_drr_pct / 100`, with `ads_plan_base_rub`/`ads_plan_base_mode` disclosing whether the base was plan turnover or overperformance fact turnover; `ads_sum_rub` is ok when `fact_ads >= ads_plan`, not when spend is below a cost limit;
  - `contract_period_projection` is independent from the selected period and uses facts from `2026-02-01..min(as_of_date, 2026-12-31)` with the same accepted snapshot/monthly baseline source rules; projected buyout/ads = elapsed fact divided by elapsed days and multiplied by total contract days, annual ads plan = `(H1+H2) * plan_drr_pct / 100`, and future contract days are not missing coverage;
  - contract-period turnover truth is buyouts only: both elapsed fact and forecast use `fin_buyout_rub`; orders and `orderSum` are not substitutes;
  - `contract_period_projection` always exposes `annual_buyout_plan_rub`, fixed `usn_upper_limit_rub=490500000`, `projected_buyout_rub`, `projected_buyout_pct_of_annual_plan`, `projected_buyout_pct_of_usn_upper_limit`, `projected_buyout_remaining_to_usn_upper_limit_rub`, `projected_buyout_exceeds_usn_upper_limit`, `drr_minimum_pct`, `drr_requirement_type=minimum`, `projected_drr_pct`, `projected_drr_margin_to_minimum_pp` and `projected_drr_minimum_met`; fixed guardrails remain present for `partial/unavailable`, while derivatives are `null` when the required buyout or DRR projection is unavailable, and USN remaining may be negative on exceedance;
  - `usn_upper_limit_rub=490500000` is the 2026 management reference `450000000 × 1.090`, where `1.090` is the deflator coefficient established by Ministry of Economic Development of Russia order dated 06.11.2025 №734; it is a buyout-forecast reference and does not replace the tax-accounting income register;
  - operator projection UI renders the annual buyout plan `450 млн ₽`, `Верхний порог УСН` at `490,5 млн ₽` and `Минимальный DRR по договору` at `6%`, with accessible management/tax-register and contractual-minimum explanations; DRR above `6%` is rendered as margin, not an alert;
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
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status` returns JSON with dataset states, active SKU count, recommendation path, selected `stock_ff_source` and 1C FF_STOCK summary
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status` returns JSON with active SKU count, methodology note, shared dataset state and optional last result
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options` returns a protected read-only planning payload for one selected district of the latest regional result: barcode blockers when nomenclature is incomplete, or ranked WB acceptance/options variants enriched with warehouse/district mapping and tariff/coefficient evidence. It must not create/draft FBW/FBS supplies, run Seller Portal automation, mutate WB, write Google Sheets/GAS or persist selected variants as fact.
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies` returns protected cached WB supplies JSON only, supports `sort_key=supply_date&sort_dir=asc|desc`, and sorts all filtered rows before pagination.
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options` returns protected server-validated selector options for calculation-only WB supply overlays, including eligibility, disabled reasons, dates, active-SKU usable quantity and warehouse district mapping diagnostics.
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/nomenclature...` remains the protected operator-only server-owned nomenclature surface. Default list visibility is `visible`; `visibility=hidden|all` exposes hidden rows. `POST /v1/sheet-vitrina-v1/settings/nomenclature/barcode-sync` is the read-only WB Content card sync: it calls only `POST /content/v2/get/cards/list`, matches local rows by `nm_id`, then barcode, then `vendor_code`, includes hidden rows in matching, does not auto-match by fuzzy WB title and does not unhide hidden rows. Existing rows may update only WB-owned/reference fields and never overwrite nomenclature name, SKU group, purchase price, match key, compatible models, operational `is_active`, hidden status or manual barcode overrides.
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/sku-groups...` is the protected operator-only server-owned SKU group dictionary. It stores group labels and aliases/patterns used for vendorCode auto-detection, seeds Clean/Anti-spy/Matte/No Frame groups plus `extra` and `other`, accepts legacy `clear` rows without destructive migration, and blocks disabling a group while active nomenclature rows still use it.
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/users...` is a `settings + manage_users` runtime user admin surface. It returns `available_sections`, returns/accepts server-owned `allowed_sections`, preserves legacy `role` for compatibility, never returns plaintext passwords or password hashes, rejects duplicate usernames against env/runtime principals, rejects reserved service/debug/test identities (`codex_*`, `smoke_*`, `test_*`) through ordinary user-facing create, validates section ids and must not allow the final active `settings + manage_users` path to be removed. The default list is user-facing only: server-side classification hides Codex/smoke/test service rows from `users[]`, reports `hidden_service_users_count`, and only admin diagnostic `include_service=1` returns them separately as `service_users[]`. The settings users UI presents `allowed_sections` and `manage_users` through compact access pickers; collapsed summaries are presentation only. Env bootstrap/supplier principals are listed as read-only env rows with compact summaries; their secrets are not mutated from this UI.
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/registry` returns protected read-only supplier shipment matrix JSON for `Поставки -> Реестр поставок`; it is built from existing supplier shipment and financial-document runtime truth and must return grouped rows with null-equivalent missing values instead of `NaN`/`Infinity`. КП rows with known absence reasons return short displays such as `нет КП`, `нет в КП`, `ошибка парсинга КП`, `нет стоимости груза в КП`, `курс не подтверждён` or `ждём счета`; plain `—` remains for unknown/not-applicable cells.
- Supplier shipment list/detail/registry live reads must expose the server-derived factual-date status contract, including `order_status_display`. `GET .../supplier-shipments/{shipment_id}/factual-date-correction` returns the persisted reload-safe correction state. A live factual-date mutation uses only `apps/supplier_shipment_factual_date_correction.py`: read-only constrained preflight/dry-run first, exact human-approved fingerprint, verified `0600` backup, bounded apply and post-run zero-change proof. Direct SQL, server-only header edits and legacy movement rewrites are forbidden; authenticated UI/API readback must confirm the factual date and status badge after deploy/apply.
- If a verified correction candidate cannot be built because immutable historical SQLite backups exhaust the target filesystem, only `apps/sqlite_backup_archive.py` may losslessly transcode an existing file below a `backups/` directory. It is dry-run by default, rejects the live runtime DB and cross-directory output, requires the exact source-stat/SHA/integrity fingerprint for apply, tests the zstd frame and streamed decompressed size/SHA before deleting the uncompressed source, writes a `0600` archive plus manifest, and never deletes or rewrites business/runtime rows.
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/registry/compare-quote` is the protected temporary КП comparison route for the shipment registry. It accepts multipart `file` + `shipment_id`, reuses the logistics quote parser, returns grouped `КП` vs `Поставка факт` comparison rows, and must not persist the uploaded PDF as a supplier shipment or financial document.
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync` is the ordinary protected latest-window refresh: it fetches `offset=0`, additionally fetches bounded active `statusIDs=[1,2,3,4]` and recent historical `statusIDs=[5,6]` slices when no explicit status filter is requested, compares raw hashes/`updatedDate`/status, upserts new/changed rows, forces detail/goods refresh for up to 12 prioritized active/recent historical rows that changed, failed enrichment or have newer raw evidence, retries old critical-missing rows only when explicitly requested with `enrich=missing_critical`, exposes counters such as `forced_status_refresh_rows`, `refreshed_recent_historical_rows` and `accepted_qty_changed_rows`, and returns controlled JSON errors with sanitized upstream status/content-type/body prefix.
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill` starts a protected background full-history backfill and returns `202` with `run_id`; the job walks WB list pagination by `limit/offset`, saves resumable progress after each page, keeps old rows, and records partial/blocker state on 429/timeout/non-JSON/upstream failures.
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status` returns protected JSON run progress and sync state for WB supplies incremental/backfill jobs.
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/*.xlsx` returns `200` + XLSX content type for all operator templates with Russian headers
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec-check` returns controlled JSON summary for the existing materialized 1C `FF_STOCK` source, including ready/partial/missing/error and coverage counts
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec.xlsx` returns `200` + XLSX with the same headers as the manual `Остатки ФФ` template when 1C coverage is ready, or truthful `422 {"error": ...}` when it is not ready
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-*` accepts zero-quantity rows, drops them from normalized runtime payload and coverage, and still accepts a workbook that becomes an empty inbound dataset after zero-row filtering
- `GET /v1/sheet-vitrina-v1/supply/factory-order/uploaded/*` returns the exact currently stored operator workbook when the dataset is uploaded, or truthful `422 {"error": ...}` when it is absent
- `DELETE /v1/sheet-vitrina-v1/supply/factory-order/upload/*` returns a truthful deleted/absent state and is reflected back through `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx` returns either `200` + XLSX after calculation or truthful `422 {"error": ...}` before the first successful calculation
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx` returns either `200` + XLSX after regional calculation or truthful `422 {"error": ...}` before the first successful calculation or when the requested district was excluded from the latest calculation methodology; content-disposition filename is stable ASCII translit such as `wb_regional_central_fo.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip` returns either `200` + `application/zip` after regional calculation or truthful `422 {"error": ...}` before the first successful calculation; the archive filename is `wb_regional_recommendations_<report_date>.zip`, contains one district XLSX per included district only and uses the same ASCII translit member names as individual downloads
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options` returns `200` with status `ready`, `blocked`, `empty`, `no_last_calculation`, `no_options` or `upstream_error`; missing barcode must be a controlled blocker before any WB `acceptance/options` call, while partial enrichment failures stay warnings on otherwise visible options
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

The current active public probe target is `https://api.selleros.pro`. Live/public closure for website/operator tasks must verify the HTTPS production domain routes, including `GET /sheet-vitrina-v1/vitrina`, authorized `GET /sheet-vitrina-v1/instructions`, `GET /sheet-vitrina-v1/operator`, `GET /v1/sheet-vitrina-v1/status`, `GET /v1/sheet-vitrina-v1/web-vitrina`, `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`, `GET /v1/sheet-vitrina-v1/warehouses`, and one `GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}`. `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1` remains a diagnostic-only legacy TLS escape hatch for historical checks and is not part of the active EU target closure.

Live/public verify that creates temporary runtime users must prefer temp/local runtime state. If a hosted verify must create a live runtime user, it must use an unmistakable service/test prefix or marker, run cleanup in a finally-style path, and verify that the default admin users list does not expose those rows. Archived/inactive service rows such as `codex_live_*`, `codex_debug_*`, `smoke_*` or `test_*` are not user-facing users and must be hidden by the default users API/UI even when cleanup cannot hard-delete an historical row; any bounded live cleanup for those prefixes must not touch env principals or real manual users.

## Human-Only Boundary

One minimal human-only step remains allowed only when repo-owned contract still cannot execute due missing access:
- grant deploy access for `wb-core-eu-root` / `89.191.226.88`

Without that step a live/public task stays `live-complete = blocked`; reporting only `repo-complete` is insufficient. For GAS/sheet-only scope the blocker is tracked as `sheet-complete = blocked`.
The blocker must name the concrete missing access/value and must not be phrased as unspecified operational uncertainty.

For server/operator-only changes that do not touch archived bound Apps Script guard code, `Sheet verify result` must stay `not in scope` rather than being filled with fake closure activity.

## SKU management runtime/write contract

The authenticated public route family is exact GET `/v1/sheet-vitrina-v1/sku-management` plus narrow GET/POST prefix `/v1/sheet-vitrina-v1/sku-management/`; app session and `sku_management` section authorization remain authoritative. Dedicated price and exact-placement bid blocks are part of normal runtime construction and require no post-deploy feature-flag enablement. `WB_PRICES_WRITE_ENABLED` and `SHEET_VITRINA_ADS_WRITE_ENABLED` continue to gate their legacy standalone tabs but do not disable this separately authorized workflow. Its sufficient mandatory gates are one stored target/preview, explicit confirmation, stale/min/quarantine validation, backend-only WB call, audit and exact readback. Deploy itself performs no WB mutation.
