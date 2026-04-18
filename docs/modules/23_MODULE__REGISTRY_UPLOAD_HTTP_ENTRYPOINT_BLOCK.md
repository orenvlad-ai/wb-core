---
title: "Модуль: registry_upload_http_entrypoint_block"
doc_id: "WB-CORE-MODULE-23-REGISTRY-UPLOAD-HTTP-ENTRYPOINT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `registry_upload_http_entrypoint_block`."
scope: "Первый live inbound HTTP entrypoint для V2-реестров, separate `COST_PRICE` upload contour и narrow operator surface для `sheet_vitrina_v1`: canonical bundle request, sibling cost-price request, thin request -> runtime -> response wiring, server-side `activated_at`, separated refresh/load actions, date-aware plan/status read, repo-owned operator page с двумя top-level tabs и bounded server-side factory-order supply contour без SPA/build pipeline."
source_basis:
  - "migration/86_registry_upload_contract.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json"
  - "artifacts/registry_upload_http_entrypoint/evidence/initial__registry-upload-http-entrypoint__evidence.md"
related_modules:
  - "packages/contracts/cost_price_upload.py"
  - "packages/contracts/factory_order_supply.py"
  - "packages/contracts/registry_upload_bundle_v1.py"
  - "packages/contracts/registry_upload_file_backed_service.py"
  - "packages/contracts/registry_upload_db_backed_runtime.py"
  - "packages/contracts/registry_upload_http_entrypoint.py"
  - "packages/application/cost_price_upload.py"
  - "packages/application/factory_order_sales_history.py"
  - "packages/application/factory_order_supply.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/simple_xlsx.py"
  - "packages/application/sheet_vitrina_v1_load_bridge.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "CONFIG_V2"
  - "METRICS_V2"
  - "FORMULAS_V2"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
  - "POST /v1/cost-price/upload"
  - "POST /v1/sheet-vitrina-v1/refresh"
  - "POST /v1/sheet-vitrina-v1/load"
  - "GET /v1/sheet-vitrina-v1/plan"
  - "GET /v1/sheet-vitrina-v1/status"
  - "GET /v1/sheet-vitrina-v1/job"
  - "GET /sheet-vitrina-v1/operator"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/status"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/calculate"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
related_runners:
  - "apps/registry_upload_http_entrypoint_live.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py"
  - "apps/factory_order_sales_history_smoke.py"
  - "apps/factory_order_sales_history_reconcile.py"
  - "apps/factory_order_supply_smoke.py"
  - "apps/sheet_vitrina_v1_factory_order_http_smoke.py"
  - "apps/sheet_vitrina_v1_operator_load_smoke.py"
  - "apps/cost_price_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_read_side_smoke.py"
  - "apps/sheet_vitrina_v1_business_time_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
related_docs:
  - "migration/86_registry_upload_contract.md"
  - "migration/89_registry_upload_db_backed_runtime.md"
  - "migration/90_registry_upload_http_entrypoint.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под current factory-order historical seam и current-day seller-funnel repair semantics: HTTP/operator contour по-прежнему остаётся server-owned и narrow, authoritative `orderCount` history читается из exact-date runtime cache `temporal_source_snapshots[source_key=sales_funnel_history]`, а zero-filled exact-date `seller_funnel_snapshot` больше не считается готовым current-day snapshot и не materialize-ится как truthful zero rows в `DATA_VITRINA`."
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
- Для `sheet_vitrina_v1` тот же entrypoint обслуживает narrow operator surface в двух блоках:
  - `POST /v1/sheet-vitrina-v1/refresh` = existing heavy server-side action
  - `POST /v1/sheet-vitrina-v1/load` = thin operator action, который пишет уже готовый snapshot в live sheet через existing bound Apps Script bridge
  - `GET /v1/sheet-vitrina-v1/plan` = existing cheap date-aware ready-snapshot read
  - `GET /v1/sheet-vitrina-v1/status` = cheap metadata read для последнего persisted refresh result
  - `GET /v1/sheet-vitrina-v1/job` = cheap poll/read surface для live operator log и async action state
  - `GET /sheet-vitrina-v1/operator` = simple repo-owned page с top-level tabs `Обновление данных витрины` / `Расчёт поставок`
  - `GET /v1/sheet-vitrina-v1/supply/factory-order/status` = cheap JSON status surface для bounded factory-order flow
  - `GET /v1/sheet-vitrina-v1/supply/factory-order/template/*.xlsx` = operator template downloads с русскими headers
  - `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/*` = server-side XLSX parse/validation/upload
  - `GET /v1/sheet-vitrina-v1/supply/factory-order/uploaded/*` = download exactly the current uploaded operator workbook for the selected dataset
  - `DELETE /v1/sheet-vitrina-v1/supply/factory-order/upload/*` = delete the current uploaded dataset/file for the selected dataset
  - `POST /v1/sheet-vitrina-v1/supply/factory-order/calculate` = server-side factory-order calculation
  - `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx` = operator-facing recommendation download
  - `GET /v1/sheet-vitrina-v1/supply/wb-regional/status` = cheap JSON status surface для bounded WB regional supply flow
  - `POST /v1/sheet-vitrina-v1/supply/wb-regional/calculate` = server-side district allocation calculation
  - `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx` = отдельный operator-facing XLSX download по федеральному округу
- Внутри existing `POST /v1/sheet-vitrina-v1/refresh` live contour теперь допускает bounded server-local sync для `seller_funnel_snapshot` и `web_source_snapshot`:
  - сначала refresh проверяет, materialized ли exact-date `today_current` snapshot в local `wb-ai` read-side;
  - если exact-date snapshot отсутствует, refresh может вызвать server-local owner path `/opt/wb-web-bot` (`bot.runner_day`, `bot.runner_sales_funnel_day`) и затем `/opt/wb-ai/run_web_source_handoff.py`;
  - для `seller_funnel_snapshot` и `web_source_snapshot` refresh additionally rejects zero-filled exact-date current-day snapshots: такой payload не считается готовым `today_current` state и bounded-trigger'ит retry вместо залипания ложных нулей;
  - после этого refresh повторно валидирует exact-date local API availability и только потом читает live sources;
  - contour не открывает новый public producer route, не backfill-ит yesterday в today и остаётся bounded orchestration boundary поверх existing owner path.
- Для strict closed-day acceptance тех же bot/web-source families existing refresh contour теперь различает три уровня truth:
  - `provisional_current_snapshot` для открытого `today_current`;
  - `closed_day_candidate_snapshot` для явной попытки закрытия уже завершённого дня;
  - `accepted_closed_day_snapshot` как единственный допустимый truth для `yesterday_closed`.
- Для `seller_funnel_snapshot` и `web_source_snapshot` `yesterday_closed` теперь не может silently наследоваться из provisional snapshot:
  - closed slot сначала ищет уже сохранённый `accepted_closed_day_snapshot`;
  - если его нет, contour запускает explicit closure attempt через тот же owner path;
  - invalid candidate не принимается и не оставляет в closed slot старое provisional same-day значение;
  - вместо этого source/date уходит в persisted retry state `closure_pending / closure_retrying / closure_rate_limited / closure_exhausted`.
- Source-aware invalid signatures для strict closed-day policy:
  - `seller_funnel_snapshot`: zero-filled payload plus freshness gate `source_fetched_at >= next business day start in Asia/Yekaterinburg`;
  - `web_source_snapshot`: zero-filled payload plus freshness gate `search_analytics_raw.fetched_at >= next business day start in Asia/Yekaterinburg`;
  - `prices_snapshot`, `ads_bids`, `stocks` не попадают под strict closed-day policy и сохраняют current-only semantics `not_available` для `yesterday_closed`.
- Repo-owned bounded retry cycle теперь materialize-ится отдельным runner’ом:
  - `apps/sheet_vitrina_v1_temporal_closure_retry_live.py`
  - runner вызывает existing runtime path `run_sheet_temporal_closure_retry_cycle(...)`, выбирает due source/date pairs из persisted closure state и безопасно reuses existing refresh/load contour вместо нового parallel app.
- Operator page не invent-ит новый heavy route: UI запускает existing heavy `POST /v1/sheet-vitrina-v1/refresh` отдельно от narrow `POST /v1/sheet-vitrina-v1/load`, а live progress читает только через cheap poll surface `GET /v1/sheet-vitrina-v1/job`.
- Во второй tab `Расчёт поставок` current bounded scope materialize-ит два sibling block внутри одного narrow operator page:
  - shared block `Остатки ФФ` остаётся один для обоих расчётов и хранится в одном server-owned dataset state;
  - block `Заказ на фабрике` сохраняет existing behavior, но vocabulary/settings now include explicit `cycle_order_days` (`Цикл заказов`) as an additive day tail in `target_qty`;
  - sibling selector/button label для второго блока сокращён до `Поставка на Wildberries`, while the block itself still publishes district-level outputs;
  - block `Поставка на Wildberries` использует тот же shared `stock_ff`, свои settings (`sales_avg_period_days`, `cycle_supply_days`, `lead_time_to_region_days`, `safety_days`, `order_batch_qty`, `report_date_override`) и отдельный result/download surface;
  - regional block не materialize-ит upload contract `Товары в пути от ФФ на Wildberries`: этот input остаётся вне текущего bounded scope;
  - settings fields остаются server-owned и валидируются на backend;
  - operator-facing vocabulary around supply inputs is unified as `Период усреднения продаж` / lead times / safety / `Кратность штук в коробке` / `Цикл`;
  - current operator defaults on page load:
    - factory = `30 / 30 / 15 / 15 / 15 / 14 / 250 / 14` for `prod / factory->ff / ff->wb / safety_mp / safety_ff / cycle_order / batch / sales_avg`
    - regional = `14 / 7 / 15 / 15 / 250` for `sales_avg / cycle_supply / lead_time_to_region / safety / batch`
  - authoritative `orderCount` history для расчёта живёт только server-side в `temporal_source_snapshots[source_key=sales_funnel_history]`;
  - UI больше не hard-cap'ит `sales_avg_period_days`; operator может ввести любой положительный период, а backend считает любой полностью покрытый window и truthfully возвращает blocker только если exact runtime coverage недостаточно;
  - live `DATA_VITRINA` может использоваться лишь как one-time migration input для bounded historical reconcile/replacement window `2026-03-01..2026-04-18`; после этого sheet не становится новым permanent source of truth;
  - existing `POST /v1/sheet-vitrina-v1/refresh` current flow продолжает materialize-ить future exact-date `sales_funnel_history` snapshots в тот же runtime seam, поэтому historical bootstrap не должен повторяться вручную для следующих дней;
  - все operator XLSX templates используют русские headers, а backend держит stable mapping `operator label -> internal field id`;
  - XLSX generation hardening обязана отдавать файлы, которые нормально открываются стандартными XLSX readers/Excel без recovery prompt;
  - `Остатки ФФ` требуют ровно одну строку на активный SKU и truthfully reject duplicate `nmId`;
  - `Товары в пути от фабрики` и `Товары в пути от ФФ на Wildberries` трактуют одну строку как один отдельный inbound event;
  - duplicate `nmId` в inbound templates допустимы и expected, если это разные ожидаемые поставки;
  - inbound datasets optional: если `Товары в пути от фабрики` и/или `Товары в пути от ФФ на Wildberries` не загружены либо удалены, расчёт не блокируется, а соответствующие coverage terms truthfully считаются как `0`;
  - upload UX simplified: после выбора XLSX upload starts immediately, separate `Загрузить ...` buttons are removed, а текущий uploaded file по-прежнему surface-ится как download link + subtle delete icon;
  - status surface для каждого upload block показывает current uploaded filename, download link и delete action, если файл действительно хранится в current server-owned state;
  - upper `sheet_vitrina_v1` label в operator panels is a truthful clickable link to the current bound live spreadsheet target resolved from the current Apps Script bridge config;
  - planning horizon coverage суммирует только inbound events, чья `planned_arrival_date` попадает внутрь текущего расчётного горизонта;
  - итоговые summary/result values (`Общее количество`, `Расчётный вес`, `Расчётный объём`, recommendation XLSX) остаются server-driven и не вычисляются в browser или sheet.
- Для regional supply block result contract теперь также server-driven:
  - top summary surface = `Статус`, `Дата отчёта`, `Цикл поставок, дней`, `Активных SKU`, `Общее количество`, `Расчётный вес`, `Расчётный объём`;
  - compact district table = `Федеральный округ / Общее количество в поставке / Дефицит`;
  - server хранит и публикует отдельный XLSX на каждый округ, а не один общий recommendation workbook;
  - district XLSX содержит district identification + compact operator rows `nmId / SKU / Количество к поставке` именно по фактически аллоцированному количеству после ограничения `stock_ff`.
- Current repo state не имел другого authoritative source для legacy parity term `FO_INBOUND_FF_TO_WB`, поэтому entrypoint получил narrow explicit upload contract `Товары в пути от ФФ на Wildberries`; silent drop этого члена формулы считается некорректным.
- Operator page keeps narrow Russian chrome for operator-visible labels (`Загрузить данные`, `Отправить данные`, compact `Статус` / `Лог`, row-count labels, `Скачать лог`) without explanatory subtitle/subcopy про refresh/date defaults/temporal slots под заголовком или кнопкой.
- Operator page добавляет один compact server-driven info block `Сервер и расписание`:
  - `Часовой пояс`
  - `Текущее время сервера`
  - `Автообновление`
  - `Последний автозапуск`
  - `Статус последнего автозапуска`
  - `Последнее успешное автообновление`
  - `Технический триггер`
- Этот block не hardcode-ится в UI: page читает его только из existing `GET /v1/sheet-vitrina-v1/status` / `POST /v1/sheet-vitrina-v1/refresh` response field `server_context`.
- `Автообновление` в этом block обязано быть truthful server-driven описанием полного daily auto path, а не только временем:
  - canonical current wording = `Ежедневно в 11:00 Asia/Yekaterinburg: загрузка данных + отправка данных в таблицу`
  - manual operator semantics при этом не меняются: explicit UI buttons всё ещё разделяют `refresh` и `load`
- Operator log surface обязан показывать построчный start / key steps / finish / error для обеих operator actions:
  - `refresh` = build/persist ready snapshot only
  - `load` = write already prepared snapshot only
- Один и тот же `GET /v1/sheet-vitrina-v1/job` route остаётся canonical operator job surface, но теперь поддерживает два bounded режима:
  - default JSON poll = current async state + detailed per-run log lines
  - `format=text&download=1` = plain-text export именно текущего requested `job_id` без отдельного historical dump route
- Live log lines должны быть machine-useful и server-driven:
  - route/cycle identity (`refresh` vs `load`)
  - source/module/adapter/endpoint step markers
  - source result semantics (`success`, `incomplete`, `missing`, `blocked`, `error`, `not_available`) с requested/covered/missing counts и raw note/detail
  - metric-batch summaries с явным разделением `non_zero` / `zero` / `blank` и truthful blocked cases
  - bridge/write result lines for load (`bridge_start`, per-sheet write/state summary, final result)
- Raw log entries, raw backend errors и canonical technical identifiers/values на operator page не локализуются и не переписываются.
- Для current checkpoint `plan/status` обязаны surface-ить temporal metadata, достаточную для thin operators:
  - `date_columns`
  - `temporal_slots`
  - `source_temporal_policies`
- Для operator-facing time/scheduler chrome existing `status/refresh` surface теперь дополнительно обязаны отдавать `server_context`:
  - `business_timezone`
  - `business_now`
  - `default_as_of_date`
  - `today_current_date`
  - `daily_refresh_business_time`
  - `daily_refresh_systemd_time`
  - `daily_refresh_systemd_oncalendar`
  - `daily_auto_action`
  - `daily_auto_description`
  - `daily_auto_trigger_name`
  - `daily_auto_trigger_description`
  - `last_auto_run_status`
  - `last_auto_run_status_label`
  - `last_auto_run_time`
  - `last_auto_run_finished_at`
  - `last_successful_auto_update_at`
  - `last_auto_run_error`
- Если ready snapshot ещё не materialized, `GET /v1/sheet-vitrina-v1/status` сохраняет truthful `422`, но error payload всё равно обязан нести `server_context`, чтобы operator page могла показать актуальные server/timezone/scheduler facts уже на empty state.
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

## 3.3 Hosted deploy contract для live closure

- Hosted runtime contract теперь materialized в repo:
  - `apps/registry_upload_http_entrypoint_hosted_runtime.py`
  - `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json`
  - `docs/architecture/10_hosted_runtime_deploy_contract.md`
- Этот contract фиксирует:
  - canonical public base URL `https://api.selleros.pro`;
  - canonical loopback base URL `http://127.0.0.1:8765`;
  - canonical route paths через existing entrypoint env names;
  - required target fields `ssh_destination / target_dir / service_name / restart_command / environment_file`;
  - one repo-owned sequence `deploy -> loopback-probe -> public-probe`.
- Unknown host-specific values не invent-ятся в repo:
  - actual `target_dir`
  - actual `service_name`
  - actual `restart_command`
  - actual `status_command`
  - actual `environment_file`
- Пока эти values или deploy rights не даны, live task обязана завершаться точным blocker-ом, а не vague ссылкой на “external operational knowledge”.

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
  - `packages/application/factory_order_sales_history.py`
- reused runtime:
  - `packages/application/registry_upload_db_backed_runtime.py`
- adapter:
  - `packages/adapters/registry_upload_http_entrypoint.py`
- live runner:
  - `apps/registry_upload_http_entrypoint_live.py`
- hosted deploy runner:
  - `apps/registry_upload_http_entrypoint_hosted_runtime.py`
- smoke:
  - `apps/registry_upload_http_entrypoint_smoke.py`
  - `apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`
  - `apps/factory_order_sales_history_smoke.py`
  - `apps/factory_order_sales_history_reconcile.py`
  - `apps/sheet_vitrina_v1_business_time_smoke.py`
  - `apps/sheet_vitrina_v1_web_source_current_sync_smoke.py`
  - `apps/web_source_current_sync_zero_snapshot_smoke.py`
  - `apps/web_source_current_sync_closed_day_freshness_smoke.py`
  - `apps/sheet_vitrina_v1_temporal_closure_retry_smoke.py`
  - `apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный integration smoke через `apps/registry_upload_http_entrypoint_smoke.py`.
- Подтверждён targeted boundary/default-date smoke через `apps/sheet_vitrina_v1_business_time_smoke.py`.
- Подтверждён targeted current-day web-source sync smoke через `apps/sheet_vitrina_v1_web_source_current_sync_smoke.py`.
- Подтверждён targeted zero-filled seller-funnel retry smoke через `apps/web_source_current_sync_zero_snapshot_smoke.py`.
- Подтверждён targeted closed-day source freshness smoke через `apps/web_source_current_sync_closed_day_freshness_smoke.py`.
- Подтверждён targeted truthful temporal closure retry smoke через `apps/sheet_vitrina_v1_temporal_closure_retry_smoke.py`.
- Подтверждён integration smoke для strict web-source temporal refresh через `apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py`.
- Smoke проверяет:
  - что HTTP entrypoint реально поднимается и принимает `POST`;
  - что request body попадает в существующий DB-backed runtime, а не в дублирующую ingest-логику;
  - что accepted response возвращается в канонической форме;
  - что current server-side truth обновляется через runtime DB;
  - что operator page `GET /sheet-vitrina-v1/operator` отдается тем же contour и публикует правильные refresh/load/status/job paths;
  - что operator page рендерит compact `Сервер и расписание` block с русскими labels, а не hardcoded timezone text;
  - что `GET /v1/sheet-vitrina-v1/status` до refresh честно возвращает `ready snapshot missing`;
  - что `GET /v1/sheet-vitrina-v1/status` до refresh всё равно несёт `server_context` с `Asia/Yekaterinburg` и current scheduler trigger metadata;
  - что async operator `refresh` / `load` live-log surface отдаёт построчные шаги и не смешивает `refresh` с `load`;
  - что duplicate `bundle_version` возвращает rejected result и HTTP `409`;
  - что accepted HTTP response сохраняет фактические request counts;
  - что synthetic oversized bundle проходит тот же HTTP boundary без hardcoded row-count caps.
  - что empty/default refresh request считает `as_of_date` и `today_current` по `Asia/Yekaterinburg`, а не по UTC.

# 7. Что уже доказано по модулю

- upload line больше не заканчивается на локальном runtime: в repo появился первый внешний вызываемый boundary.
- Separate COST_PRICE contour переиспользует тот же app/service boundary и runtime DB, но остаётся отдельным dataset seam без смешивания с `config_v2 / metrics_v2 / formulas_v2`.
- New read-side integration не открывает новый public route: authoritative `COST_PRICE` current state читается внутри existing refresh/read contour и materialize-ит operator-facing metrics/diagnostics в already existing `DATA_VITRINA` / `STATUS`.
- Repo-owned operator page для explicit refresh теперь живёт на том же thin HTTP entrypoint и убирает ручной `curl` из нормального operator path.
- Existing operator page/status surface теперь server-driven показывает текущую EKT business-time truth и current timer trigger metadata без отдельного meta route и без hardcoded UI copy.
- Live service остаётся тонкой loopback HTTP boundary (`wb-core-registry-http.service` -> `127.0.0.1:8765`) и не переносит heavy truth в Apps Script.
- Existing live daily refresh scheduler materialize-ится как external systemd timer `wb-core-sheet-vitrina-refresh.timer`, который вызывает тот же existing `POST /v1/sheet-vitrina-v1/refresh` ежедневно в `11:00 Asia/Yekaterinburg` (`06:00 UTC` на current host), но теперь делает это в `auto_load=true` режиме:
  - server contour сначала materialize-ит ready snapshot тем же heavy refresh path;
  - затем в том же auto cycle вызывает existing load bridge и доводит результат до live sheet;
  - итоговый auto result persist-ится в runtime/status surface как last auto run status / timestamps, чтобы operator page не маскировала refresh-only под sheet-complete.
- Existing live contour также допускает отдельный bounded retry timer `wb-core-sheet-vitrina-closure-retry.timer`, который вызывает repo-owned runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py`:
  - timer не делает tight loop и может запускаться чаще, чем real retry cadence, потому что due/backoff decision already lives in persisted runtime state;
  - один и тот же source/date не должен принимать competing closed-day truth параллельно: retry runner читает only due states from runtime and materialize-ит acceptance через existing refresh path.
- Если bot/web-source family не успела materialize-ить current-day snapshot по daily cron/handoff policy, same refresh route теперь может bounded-trigger'ить server-local same-day capture + handoff и закрыть `today_current` без возврата heavy producer logic в Apps Script.
- HTTP boundary выровнен с current upload semantics: валидируется содержимое registry rows, а не их заранее зашитое количество.
- Live closure для этого блока теперь тоже имеет repo-owned contract: Codex больше не должна угадывать target route/service steps руками, а должна идти через canonical hosted runner.

# 8. Что пока не является частью финальной production-сборки

- Apps Script upload button redesign;
- Google Sheets UI redesign;
- operator workflow в таблице;
- actual deploy rights и внешняя доступность entrypoint;
- final auth/hardening;
- generic scheduler framework beyond current one timer / one route wiring;
- большой background-jobs subsystem;
- production Postgres redesign и внешняя инфраструктура.
