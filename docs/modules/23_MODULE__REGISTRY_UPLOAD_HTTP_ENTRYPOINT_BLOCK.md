---
title: "Модуль: registry_upload_http_entrypoint_block"
doc_id: "WB-CORE-MODULE-23-REGISTRY-UPLOAD-HTTP-ENTRYPOINT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_http_entrypoint_block`."
scope: "Первый live inbound HTTP entrypoint для V2-реестров, separate `COST_PRICE` upload contour и narrow operator surface для `sheet_vitrina_v1`: canonical bundle request, sibling cost-price request, thin request -> runtime -> response wiring, server-side `activated_at`, existing refresh/read split, date-aware plan/status read и simple repo-owned HTML page без SPA/build pipeline."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_http_entrypoint/evidence/initial__registry-upload-http-entrypoint__evidence.md"
related_modules:
  - "packages/contracts/cost_price_upload.py"
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_db_backed_runtime.py"
  - "packages/contracts/registry_upload_http_entrypoint.py"
  - "packages/application/cost_price_upload.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
  - "POST /v1/cost-price/upload"
  - "POST /v1/sheet-vitrina-v1/refresh"
  - "GET /v1/sheet-vitrina-v1/plan"
  - "GET /v1/sheet-vitrina-v1/status"
  - "GET /sheet-vitrina-v1/operator"
related_runners:
  - "apps/registry_upload_http_entrypoint_live.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/cost_price_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_read_side_smoke.py"
  - "apps/sheet_vitrina_v1_business_time_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под separate `COST_PRICE` contour, EKT-aligned date-aware `sheet_vitrina_v1` read model, bounded current-day web-source sync и live daily refresh timer: HTTP entrypoint принимает фактические registry list lengths, держит sibling cost-price dataset отдельно от compact bundle, использует его в existing refresh/plan/status read-side через server-side effective-date overlay без нового public route, считает default `as_of_date` / `today_current` по `Asia/Yekaterinburg`, а refresh contour при missing `today_current` web-source snapshot может bounded-trigger'ить server-local producer/handoff seam."
---

# 1. Идентификатор и статус

- `module_id`: `registry_upload_http_entrypoint_block`
- `family`: `registry`
- `status_transfer`: live HTTP entrypoint перенесён в `wb-core`
- `status_verification`: integration smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_bundle_v1_block`
  - `registry_upload_file_backed_service_block`
  - `registry_upload_db_backed_runtime_block`
  - `migration/86_registry_upload_contract.md`
  - `migration/90_registry_upload_http_entrypoint.md`
- Семантика блока: принять bundle по HTTP, делегировать ingest/refresh/status-read в существующий DB-backed runtime и отдать наружу канонические JSON/results плюс один narrow operator HTML page на том же server contour.

# 3. Target contract и смысл результата

- Канонический input:
  - `POST /v1/registry-upload/bundle`
  - request body = `bundle_version + uploaded_at + config_v2 + metrics_v2 + formulas_v2`
- Канонический sibling input:
  - `POST /v1/cost-price/upload`
  - request body = `dataset_version + uploaded_at + cost_price_rows`
  - `cost_price_rows[]` = `group + cost_price_rub + effective_from`
- Канонический output:
  - JSON `RegistryUploadResult`
  - `status`
  - `bundle_version`
  - `accepted_counts`
  - `validation_errors`
  - `activated_at`
- HTTP boundary не должен навязывать fixed row-count presets для `config_v2 / metrics_v2 / formulas_v2`.
- `accepted_counts` обязаны отражать фактические длины списков из request body после successful ingest.
- COST_PRICE contour не подмешивается в main compact registry bundle и хранится в runtime как отдельный authoritative dataset/current-state seam.
- Existing `POST /v1/cost-price/upload` остаётся единственным write path для себестоимостей, а existing `POST /v1/sheet-vitrina-v1/refresh` / `GET /v1/sheet-vitrina-v1/plan` / `GET /v1/sheet-vitrina-v1/status` используют этот current state только server-side:
  - match по `group`;
  - choose latest `effective_from <= slot_date`;
  - surface coverage/diagnostics в `STATUS.cost_price[*]` без отдельного public read route для dataset rows.
- Для `POST /v1/cost-price/upload` server-side validator обязан:
  - требовать `group`, `cost_price_rub`, `effective_from` на каждой row;
  - canonicalize `effective_from` к `YYYY-MM-DD`, если sheet/client прислал `DD.MM.YYYY` или ISO datetime;
  - отвергать duplicate `(group, effective_from)` внутри одного dataset вместо неявного last-row-wins.
- HTTP семантика bounded шага:
  - `200` для `accepted`
  - `409` для duplicate `bundle_version`
  - `422` для contract-level rejection после parse
- Для COST_PRICE действует тот же bounded status mapping:
  - `200` для `accepted`
  - `409` для duplicate `dataset_version`
  - `422` для contract-level rejection после parse/validation
- Для `sheet_vitrina_v1` тот же entrypoint обслуживает ещё четыре узких surface:
  - `POST /v1/sheet-vitrina-v1/refresh` = existing heavy server-side action
  - `GET /v1/sheet-vitrina-v1/plan` = existing cheap date-aware ready-snapshot read
  - `GET /v1/sheet-vitrina-v1/status` = cheap metadata read для последнего persisted refresh result
  - `GET /sheet-vitrina-v1/operator` = simple repo-owned page с одной primary action `Загрузить данные`
- Внутри existing `POST /v1/sheet-vitrina-v1/refresh` live contour теперь допускает bounded server-local sync для `seller_funnel_snapshot` и `web_source_snapshot`:
  - сначала refresh проверяет, materialized ли exact-date `today_current` snapshot в local `wb-ai` read-side;
  - если exact-date snapshot отсутствует, refresh может вызвать server-local owner path `/opt/wb-web-bot` (`bot.runner_day`, `bot.runner_sales_funnel_day`) и затем `/opt/wb-ai/run_web_source_handoff.py`;
  - после этого refresh повторно валидирует exact-date local API availability и только потом читает live sources;
  - contour не открывает новый public producer route, не backfill-ит yesterday в today и остаётся bounded orchestration boundary поверх existing owner path.
- Operator page не invent-ит новый heavy route: UI вызывает существующий `POST /v1/sheet-vitrina-v1/refresh` и читает только cheap status surface.
- Для current checkpoint `plan/status` обязаны surface-ить temporal metadata, достаточную для thin operators:
  - `date_columns`
  - `temporal_slots`
  - `source_temporal_policies`
- Это нужно, чтобы public/runtime/operator contour не маскировал `today_current` values под surrogate `as_of_date`.
- Canonical business timezone для server-side default-date semantics = `Asia/Yekaterinburg`:
  - default `as_of_date` = previous business day in `Asia/Yekaterinburg`;
  - `today_current` = factual current business day in `Asia/Yekaterinburg`;
  - operator/status surface не должен зависеть от host-local timezone и не должен отставать на UTC boundary `19:00–23:59`.

## 3.1 Допущение bounded шага

- Для первого live inbound boundary используется стандартный `http.server`.
- Это не является решением, что production target обязан жить на этом же framework-path.
- Deploy/auth-hardening остаются отдельными шагами вне исходного module proof, но task-level execution handoff для public route change всё равно требует live publish verification.

## 3.2 Completion semantics для public route changes

- Repo router code и local smoke дают только `repo-complete`, но не закрывают execution handoff для public/live task.
- Если задача добавляет или меняет public route этого entrypoint, completion считается достигнутым только после live publish verification:
  - нужный runtime/process реально обновлён;
  - expected route существует в live app;
  - expected route опубликован снаружи через nginx/proxy или equivalent contour;
  - public probe подтверждает expected response shape.
- Для этого блока есть два отдельных operational failure mode, которые нельзя путать с отсутствием repo code:
  - stale runtime deploy: live process обслуживает старую версию entrypoint;
  - incomplete nginx publish: live app route уже есть, но public contour не публикует его наружу.
- Поэтому для нового public route недостаточно проверить только file-level router code в repo: нужен explicit probe live/public route existence.

# 4. Артефакты и wiring по модулю

- input artifact:
  - `artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json`
- target artifacts:
  - `artifacts/registry_upload_http_entrypoint/target/http_result__accepted__fixture.json`
  - `artifacts/registry_upload_http_entrypoint/target/http_result__duplicate_bundle_version__fixture.json`
  - `artifacts/registry_upload_http_entrypoint/target/current_state__fixture.json`
- parity:
  - `artifacts/registry_upload_http_entrypoint/parity/request-vs-runtime__comparison.md`
- evidence:
  - `artifacts/registry_upload_http_entrypoint/evidence/initial__registry-upload-http-entrypoint__evidence.md`

# 5. Кодовые части

- contracts:
  - `packages/contracts/registry_upload_http_entrypoint.py`
- application:
  - `packages/application/registry_upload_http_entrypoint.py`
  - `packages/business_time.py`
- reused runtime:
  - `packages/application/registry_upload_db_backed_runtime.py`
- adapter:
  - `packages/adapters/registry_upload_http_entrypoint.py`
- live runner:
  - `apps/registry_upload_http_entrypoint_live.py`
- smoke:
  - `apps/registry_upload_http_entrypoint_smoke.py`
  - `apps/sheet_vitrina_v1_business_time_smoke.py`
  - `apps/sheet_vitrina_v1_web_source_current_sync_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный integration smoke через `apps/registry_upload_http_entrypoint_smoke.py`.
- Подтверждён targeted boundary/default-date smoke через `apps/sheet_vitrina_v1_business_time_smoke.py`.
- Подтверждён targeted current-day web-source sync smoke через `apps/sheet_vitrina_v1_web_source_current_sync_smoke.py`.
- Smoke проверяет:
  - что HTTP entrypoint реально поднимается и принимает `POST`;
  - что request body попадает в существующий DB-backed runtime, а не в дублирующую ingest-логику;
  - что accepted response возвращается в канонической форме;
  - что current server-side truth обновляется через runtime DB;
  - что operator page `GET /sheet-vitrina-v1/operator` отдается тем же contour и публикует правильные refresh/status paths;
  - что `GET /v1/sheet-vitrina-v1/status` до refresh честно возвращает `ready snapshot missing`;
  - что duplicate `bundle_version` возвращает rejected result и HTTP `409`;
  - что accepted HTTP response сохраняет фактические request counts;
  - что synthetic oversized bundle проходит тот же HTTP boundary без hardcoded row-count caps.
  - что empty/default refresh request считает `as_of_date` и `today_current` по `Asia/Yekaterinburg`, а не по UTC.

# 7. Что уже доказано по модулю

- upload line больше не заканчивается на локальном runtime: в repo появился первый внешний вызываемый boundary.
- Separate COST_PRICE contour переиспользует тот же app/service boundary и runtime DB, но остаётся отдельным dataset seam без смешивания с `config_v2 / metrics_v2 / formulas_v2`.
- New read-side integration не открывает новый public route: authoritative `COST_PRICE` current state читается внутри existing refresh/read contour и materialize-ит operator-facing metrics/diagnostics в already existing `DATA_VITRINA` / `STATUS`.
- Repo-owned operator page для explicit refresh теперь живёт на том же thin HTTP entrypoint и убирает ручной `curl` из нормального operator path.
- Live service остаётся тонкой loopback HTTP boundary (`wb-core-registry-http.service` -> `127.0.0.1:8765`) и не переносит heavy truth в Apps Script.
- Existing live daily refresh scheduler materialize-ится как external systemd timer `wb-core-sheet-vitrina-refresh.timer`, который вызывает тот же existing `POST /v1/sheet-vitrina-v1/refresh` ежедневно в `11:00 Asia/Yekaterinburg` (`06:00 UTC` на current host).
- Если bot/web-source family не успела materialize-ить current-day snapshot по daily cron/handoff policy, same refresh route теперь может bounded-trigger'ить server-local same-day capture + handoff и закрыть `today_current` без возврата heavy producer logic в Apps Script.
- HTTP boundary выровнен с current upload semantics: валидируется содержимое registry rows, а не их заранее зашитое количество.

# 8. Что пока не является частью финальной production-сборки

- Apps Script upload button redesign;
- Google Sheets UI redesign;
- operator workflow в таблице;
- generic scheduler framework beyond current one timer / one route wiring;
- auth/hardening;
- большой background-jobs subsystem;
- production Postgres redesign и внешняя инфраструктура.
