---
title: "Модуль: web_vitrina_page_composition_block"
doc_id: "WB-CORE-MODULE-31-WEB-VITRINA-PAGE-COMPOSITION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded phase-4 слою `web_vitrina_page_composition_block`."
scope: "Real page composition для `GET /sheet-vitrina-v1/vitrina`: separate sibling page shell, split page-refresh/data-freshness summary, server-driven full-width table `Загрузка данных`, secondary block `Лог`, semantic green/red truth taxonomy for today/yesterday source status, bounded `Обновить` vs `Загрузить и обновить` action semantics, filters area, table container, truthful loading/empty/error states и minimal inline client island поверх stable server seams `web_vitrina_contract -> web_vitrina_view_model -> web_vitrina_gravity_table_adapter` без SPA/platform redesign."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
related_modules:
  - "packages/contracts/web_vitrina_contract.py"
  - "packages/contracts/web_vitrina_view_model.py"
  - "packages/contracts/web_vitrina_gravity_table_adapter.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/web_vitrina_view_model.py"
  - "packages/application/web_vitrina_gravity_table_adapter.py"
  - "packages/application/web_vitrina_page_composition.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_tables:
  - "DATA_VITRINA"
related_endpoints:
  - "GET /sheet-vitrina-v1/vitrina"
  - "GET /v1/sheet-vitrina-v1/web-vitrina"
  - "GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&include_source_status=1"
related_runners:
  - "apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_reason_sanitization_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Phase 4 live page composition остаётся server-driven, current status semantics stay source-aware instead of naive two-slot worst-case, а bot-backed auth barrier теперь humanize-ится explicitly: invalid seller-portal browser session surfaces as short Russian reason about required re-login instead of raw Playwright timeout/traceback."
---

# 1. Идентификатор и статус

- `module_id`: `web_vitrina_page_composition_block`
- `family`: `web-vitrina`
- `status_transfer`: phase-4 live page composition перенесён в `wb-core`
- `status_verification`: targeted composition smoke, browser smoke и hosted/public probe подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - stable `web_vitrina_contract` v1
  - stable `web_vitrina_view_model` v1
  - stable `web_vitrina_gravity_table_adapter` v1
  - existing sibling routes `GET /sheet-vitrina-v1/vitrina` + `GET /v1/sheet-vitrina-v1/web-vitrina`
- Семантика блока: не делать новый frontend platform contour и не переносить business truth в браузер, а materialize-ить repo-owned page-only layer над уже готовыми seams.

# 3. Target contract и смысл результата

- `GET /sheet-vitrina-v1/vitrina` теперь является реальной usable web-vitrina page:
  - canonical entrypoint for the unified `sheet_vitrina_v1` UI; first visible tab is `Витрина`, alongside `Расчет поставок` and `Отчеты`
  - `GET /sheet-vitrina-v1/operator` remains a compatibility entry and renders the same unified shell instead of the former narrow operator-only page; embedded operator-only panels are reserved for the unified tabs and internal compatibility probes
  - compact top panel inside `Витрина`: `Загрузить и обновить` is the single primary manual action, `JSON Connect` and the old cheap `Обновить` button are not rendered, and no permanent top status badge duplicates the summary cards
  - while `Загрузить и обновить` is running, the top panel shows a minimal stage-based progress bar driven by the existing async job/log polling (`start/queued`, source fetch, prepare/materialize, load/update table, finish); after completion the progress bar disappears and the final semantic status stays in the summary/log surfaces
  - compact summary with separate `Последнее обновление страницы` and `Свежесть данных`
  - primary table immediately follows the summary cards; filters/settings, historical controls and `Действия и состояния` render after the table
  - main table display headers are Russian (`Раздел`, `Метрика`, `Обновлено`, etc.); backend/API keys stay stable, while `Обновлено` surfaces per-row last successful update timestamp from snapshot metadata
  - `Загрузить и обновить` = canonical server-side refresh from external sources + page reread, without Google Sheet write path
  - two server-driven action-adjacent information blocks:
    - `Загрузка данных` = lazy detailed source-status surface. Initial page composition renders only a calm `not_loaded` state plus the explicit button `Загрузить`; it must not auto-render source group shells or the misleading row `Источники группы пока не представлены в status payload.` before the operator asks for details. On click, the page makes a read-only request to the same web-vitrina read route with `surface=page_composition&include_source_status=1` and then renders the grouped compact table derived from per-source upload/fetch truth: stable groups `WB API`, `Seller Portal / бот`, `Прочие источники`; each group has a compact date control, `Обновить группу`, group-level last update timestamp, and rows with server/business today and yesterday statuses, reason text, Russian metric labels and secondary technical endpoint. Every metric visible in the main table belongs to exactly one loading group; residual calculated/formula metrics such as proxy profit and proxy margin live in `Прочие источники`.
    - `Seller Portal / бот` group additionally renders bounded session status on the left side of the group header and session controls (`Проверить сессию`, `Восстановить сессию`, `Скачать лаунчер`) over the existing seller-session/recovery seams
    - `Лог` = compact fixed-height tail below the loading table plus `Скачать лог` via existing job/log contour; if exact transient job for the visible snapshot is unavailable, block must show persisted semantic fallback instead of stale green success
    - former `Обновление данных` is not rendered as a page-composition activity block; persisted `STATUS` rows remain internal truth for status/read contracts
  - filters area
  - table container
  - truthful `loading / empty / error` states
  - `Расчет поставок` and `Отчеты` reuse the existing operator template/actions in embedded mode, preserving factory/WB supply blocks and the internal report subsection selector (`Ежедневные отчёты`, `Отчёт по остаткам`, `Выполнение плана`) without changing business routes; embedded height is measured from the actual `.page` content rather than iframe viewport/body `100vh`, and edge wheel gestures are relayed to the parent shell so these tabs do not create large empty scroll tails or swallow the first trackpad scroll
- Existing `GET /v1/sheet-vitrina-v1/web-vitrina` keeps the default public contract unchanged:
  - default/no-surface path still returns `web_vitrina_contract` v1
  - optional `as_of_date` keeps one-day historical read on the same route
  - optional `date_from/date_to` now materializes a bounded ready-snapshot period window on the same route
  - optional `surface=page_composition` now returns a server-driven page payload for the live page shell; by default it keeps source-status details unloaded
  - optional `include_source_status=1` on that page-composition surface returns the detailed grouped loading table without triggering refresh/upstream fetch
- `page_composition` is server-owned and assembled only from:
  - `web_vitrina_contract`
  - `web_vitrina_view_model`
  - `web_vitrina_gravity_table_adapter`
- Browser role is intentionally narrow:
  - render the received page payload
  - keep only local filter/search/sort state
  - keep only browser-owned page reread timestamp for `Последнее обновление страницы`
  - keep only session-local cell highlighting for the last refresh result: `updated` cells render as soft green, `latest_confirmed`/fallback cells render as soft yellow, full refresh highlights every refreshed temporal date column (`yesterday_closed` and `today_current` when both are in scope), group refresh highlights only the selected group/date, and the highlight disappears on browser reload
  - keep only session-local source-status load state for `Загрузка данных`: `not_loaded`, `loading`, `loaded`, `empty`, `error`; this state controls visibility of the detailed table and retry button but never becomes source truth
  - keep zero ownership over job/log/status truth for `Лог` or `Загрузка данных`
  - never assemble canonical truth
  - never compute business metrics
  - never replace the server contract/view-model/adapter owner

## 3.1 Minimal bundled/client path

- Chosen path:
  - repo-owned inline client island inside `packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html`
  - same existing read route with optional `surface=page_composition`
- Why this path is minimal:
  - no new endpoint family
  - no Node/React/Vite/Webpack contour
  - no client router
  - no static asset publish campaign
  - no duplication of business-truth assembly in browser
- Gravity-specific logic stays isolated:
  - adapter semantics still live in `packages/application/web_vitrina_gravity_table_adapter.py`
  - page shell only consumes adapter payload and renders sticky/basic table behavior above it

## 3.2 Browser-state boundary

- Browser-side state now exists only as ephemeral page-local filter/search/sort state.
- Historical access state stays query-string-owned on the same route:
  - no query = current cheap daily mode
  - `as_of_date` = one-day historical mode
  - `date_from/date_to` = bounded period mode assembled server-side from materialized ready snapshots
- Namespace is explicit: `wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1`.
- Persistence mode stays `none`; no localStorage/user-profile/server preference path is introduced.
- This state is not canonical for truth and can be dropped without changing server semantics.

# 4. Артефакты и wiring по модулю

- page-composition builder:
  - `packages/application/web_vitrina_page_composition.py`
- HTTP wiring:
  - `packages/application/registry_upload_http_entrypoint.py`
  - `packages/adapters/registry_upload_http_entrypoint.py`
- live page shell:
  - `packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html`
- upstream seams:
  - `packages/application/sheet_vitrina_v1_web_vitrina.py`
  - `packages/application/web_vitrina_view_model.py`
  - `packages/application/web_vitrina_gravity_table_adapter.py`

# 5. Кодовые части

- targeted composition smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py`
- local/live browser smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- HTTP integration smoke:
  - `apps/sheet_vitrina_v1_web_vitrina_http_smoke.py`
- hosted deploy/probe contract:
  - `apps/registry_upload_http_entrypoint_hosted_runtime.py`

# 6. Какой smoke подтверждён

- `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py`
  - confirms `composition_name/version`, source chain, state namespace, filter surface, timestamp-format hint for `Свежесть данных` and human-readable activity payload fields
- `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
  - confirms real page render, visible table, lazy source-status initial state, explicit `Загрузить` details flow, filter controls, Russian activity labels/reasons, unified readable freshness timestamp without raw ISO artefacts, empty state on no-match search, reset recovery, period selector UX (`calendar + presets + date_from/date_to + save/reset`) and truthful error state when the ready snapshot is absent
- `apps/sheet_vitrina_v1_web_vitrina_http_smoke.py`
  - confirms default `web_vitrina_contract` path stays stable, optional `date_from/date_to` works as bounded period window and optional `surface=page_composition` works on the same route with severity-sorted human activity items
- `apps/sheet_vitrina_v1_web_vitrina_highlight_ui_smoke.py`
  - confirms full refresh session highlighting covers both touched temporal dates, keeps green for changed cells, yellow for latest-confirmed cells and clears on browser reload
- `apps/sheet_vitrina_v1_web_vitrina_source_status_smoke.py`
  - confirms source-aware loading-table reduction for accepted-current rollover, latest-confirmed/runtime-cache fallback, `stocks[today_current]` non-required slots, promo fallback and `fin_report_daily[yesterday_closed]` accepted truth
- `apps/sheet_vitrina_v1_web_vitrina_group_action_ui_smoke.py`
  - confirms initial `Загрузка данных` does not auto-render source group details, explicit empty/error details payload does not create fake group shells, and group refresh controls still work after details are loaded
- `apps/sheet_vitrina_v1_web_vitrina_reason_sanitization_smoke.py`
  - confirms warning/error `reason_ru` is derived as a short human summary and visible card text no longer leaks raw JSON, traceback, request ids, `resolution_rule=...` or `accepted_at=...`
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`
  - now probes both the live HTML page and the page-composition JSON surface

# 7. Что уже доказано по модулю

- `/sheet-vitrina-v1/vitrina` is no longer a placeholder shell.
- Existing stable seams are now used end-to-end in a live read-only surface.
- The chosen client path stays intentionally minimal and repo-owned.
- Web-vitrina completion is verified through the server/public web surface (`/v1/sheet-vitrina-v1/web-vitrina`, optional `surface=page_composition`, and `/sheet-vitrina-v1/vitrina`); Google Sheets / GAS / `clasp` are not active verification targets for this surface.
- Historical ready snapshots may be used as bounded, audited reconciliation input for the shared accepted temporal source layer when the read model already contains daily SKU facts that reports lack; this is done only by repo-owned dry-run/apply tooling and does not make the browser UI or Google Sheets/GAS a source of truth.
- `Свежесть данных` stays server-owned and comes from the current read-side snapshot metadata (`refreshed_at / snapshot_id / as_of_date`), while `Последнее обновление страницы` is only the browser reread marker and is intentionally separate.
- Embedded `Расчет поставок` / `Отчеты` remain UI-only composition inside `/sheet-vitrina-v1/vitrina`; iframe sizing/scroll relay changes do not introduce browser-side business truth or server-side profile persistence.
- user-facing timestamp render is unified:
  - `Свежесть данных` now reuses the same client formatter as `Последнее обновление страницы`
  - raw ISO artefacts like `T` / `Z` stay machine-only and no longer leak into the visible page text
- summary/card and `Свежесть данных` tone now follow semantic snapshot truth:
  - green = confirmed normal result;
  - yellow = empty/zero/unchanged/stale/not_refreshed/preserved/retrying/not_verified;
  - red = hard source/materialization failure;
  - snapshot row existence alone is never enough for green.
- source-aware temporal policy is now part of that read-side truth:
  - `stocks` stays green when only non-required `today_current` is blank/`not_available`;
  - `spp` / `fin_report_daily` stay green when `yesterday_closed` is confirmed and intraday `today_current` only produced tolerated non-final current-day output;
  - `prices_snapshot` and `ads_bids` stay green for accepted-current rollover, same-day accepted preservation and latest confirmed filled values; missing required current without accepted fallback remains not OK;
  - `promo_by_price` stays green when accepted/runtime-cached latest confirmed values fill the visible cells, while invalid attempts without fallback remain not OK;
  - `seller_funnel_snapshot` / `web_source_snapshot` remain strict two-slot sources and keep the summary cards degraded on broken `today_current`.
- `Загрузить и обновить` on the vitrina now reuse-ит the canonical refresh contour and no longer depends on `/load` or Google Sheet auth to refresh the web-vitrina itself.
- `Обновить группу` on the vitrina starts date-scoped `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` with payload `{async: true, source_group_id, as_of_date}` for one source group and one selected date. The action immediately surfaces a group-local launch state and appends a visible client action-log line; if the POST fails before a backend job is created, the page shows a group-local error and a visible log line such as `Не удалось запустить обновление группы WB API за 2026-04-24: HTTP 404 route not found`.
- Once the POST reaches the backend, the action reuses the existing refresh/status/job/log seams, creates a `refresh_group` job before source fetch, fetches/prepares only the selected group, loads only cells for the selected date into the target ready snapshot, updates row-level `Обновлено` and group-level `last_updated_at` only for affected rows/groups, and logs stage-aware success/failure (`source_fetch`, `prepare_materialize`, `load_group_to_vitrina`) with the selected `as_of_date`.
- Refresh and group-refresh job results may include `updated_cells` entries `{row_id, metric_key, as_of_date, source_group_id, status}`. The field is result metadata for the current UI session only: it drives transient green/yellow cell highlighting and log counters, but it is not persisted as permanent table styling. Full refresh emits metadata across all refreshed `date_columns`; group refresh remains bounded to the selected `source_group_id + as_of_date`.
- `Загрузка данных` and `Лог` stay server-driven:
  - source-status details are lazy-loaded: initial page-open keeps a `not_loaded` state and the `Загрузить` button; no grouped source rows, no group action controls and no session controls are rendered until the explicit read-only details request succeeds
  - if the explicit details request returns empty/incomplete payload, the UI shows an explicit empty/error message and retry button instead of normal-looking group shells with `Источники группы пока не представлены...`
  - loading table is derived from the last relevant refresh/group-refresh job log and persisted source fallback; if exact job association is unavailable, page shows truthful non-OK status rather than unrelated stale run
  - after explicit details load succeeds, absence of transient in-memory refresh-log must not hide source-group headers/actions when persisted source summary and backend capabilities are available; before that click, controls stay intentionally unloaded
  - loading table rows are nested under stable source-group headers while preserving source truth and canonical source labels; coverage must include all visible main-table metrics, with residual calculated/formula metrics assigned to `Прочие источники`
  - loading table uses server/business `Сегодня: <YYYY-MM-DD>` and `Вчера: <YYYY-MM-DD>` dates, short OK/not-OK cells, reason columns, Russian metric labels from the existing metric registry and secondary technical endpoint text
  - loading table OK/not-OK reduction is source-aware: latest confirmed/runtime-cache/accepted fallback with filled visible cells is OK, non-required source slots are OK/non-degrading, and red is reserved for required source failures without accepted fallback or required visible value
  - log preview and `Скачать лог` reuse the existing in-memory job/log contour and render below the loading table
  - former `Обновление данных` is not an active page-composition activity block; persisted `STATUS` rows remain the underlying read-side truth for status contracts and fallback source outcomes
  - warning/error reasons are strictly summarized on the backend: raw STATUS/job note, JSON blobs, traceback text, request ids and similar technical payload stay only in the existing log/download surface
  - for bot-backed families an invalidated seller session is surfaced as short human reason (`сессия seller portal больше не действует; требуется повторный вход`) instead of generic `Template request ... was not captured`
- Historical period UX is intentionally thin:
  - calendar/preset panel lives in the same server template
  - `Сохранить` only rewrites query string and re-reads server payload
  - `Сбросить` only removes `as_of_date/date_from/date_to` and returns to cheap daily mode
- period mode aggregates semantic status across the selected ready-snapshot window and must not force green merely because the window exists.
- The page composition layer knows only page/layout/render state and does not become a second truth owner.
- User-facing `ЕБД` / `единая база данных` means the shared server-side accepted truth/runtime layer behind web-vitrina and reports; the browser page, Google Sheets/GAS and localStorage are not data-truth owners.

# 8. Что пока не является частью финальной production-сборки

- real bundled `@gravity-ui/table` package/runtime integration
- export layer
- grid virtualization / advanced resizing UX
- legacy Google Sheets/export contour migration
- broad parity campaign with every operator/report/supply surface
- any browser-side business truth assembly
