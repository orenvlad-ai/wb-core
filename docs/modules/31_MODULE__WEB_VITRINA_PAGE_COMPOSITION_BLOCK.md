---
title: "Модуль: web_vitrina_page_composition_block"
doc_id: "WB-CORE-MODULE-31-WEB-VITRINA-PAGE-COMPOSITION-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded phase-4 слою `web_vitrina_page_composition_block`."
scope: "Real page composition для `GET /sheet-vitrina-v1/vitrina`: separate sibling page shell, split page-refresh/data-freshness summary, server-driven full-width table `Загрузка данных`, secondary block `Лог`, semantic green/red truth taxonomy for today/yesterday source status, bounded `Обновить` vs `Загрузить и обновить` action semantics, compact table toolbar for period/search/filters/columns/sort, вкладка `Отзывы` с manual read-only WB API feedbacks load/filter table плюс transient AI-assisted разбора отзывов через server-side prompt/OpenAI route, вкладка `Исследования` с read-only SKU group comparison MVP, promo candidate chips, compact research date-range controls, scrollable table/grid result, table container, truthful loading/empty/error states и minimal inline client island поверх stable server seams `web_vitrina_contract -> web_vitrina_view_model -> web_vitrina_gravity_table_adapter` без SPA/platform redesign."
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
  - "POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start"
  - "GET /v1/sheet-vitrina-v1/feedbacks"
  - "POST /v1/sheet-vitrina-v1/feedbacks/export.xlsx"
  - "GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt"
  - "POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt"
  - "POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze"
  - "GET /v1/sheet-vitrina-v1/feedbacks/complaints"
  - "POST /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status"
  - "GET /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job?run_id=..."
  - "POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected"
  - "GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job?run_id=..."
  - "GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options"
  - "POST /v1/sheet-vitrina-v1/research/sku-group-comparison/calculate"
related_runners:
  - "apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/sheet_vitrina_v1_popup_outside_click_browser_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_reason_sanitization_smoke.py"
  - "apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_http_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_ai_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_browser_smoke.py"
  - "apps/seller_portal_feedbacks_complaints_scout.py"
  - "apps/seller_portal_feedbacks_complaints_scout_smoke.py"
  - "apps/seller_portal_feedbacks_matching_replay.py"
  - "apps/seller_portal_feedbacks_matching_replay_smoke.py"
  - "apps/seller_portal_feedbacks_filter_dom_scout.py"
  - "apps/seller_portal_feedbacks_filter_dom_scout_smoke.py"
  - "apps/seller_portal_feedbacks_actionable_resolver.py"
  - "apps/seller_portal_feedbacks_complaint_dry_run_plan.py"
  - "apps/seller_portal_feedbacks_complaint_dry_run_plan_smoke.py"
  - "apps/seller_portal_feedbacks_complaint_submit.py"
  - "apps/seller_portal_feedbacks_complaint_submit_smoke.py"
  - "apps/seller_portal_feedbacks_complaints_status_sync.py"
  - "apps/seller_portal_feedbacks_complaints_status_sync_smoke.py"
  - "apps/seller_portal_feedbacks_complaint_confirmation.py"
  - "apps/seller_portal_feedbacks_complaint_confirmation_smoke.py"
  - "apps/seller_portal_feedbacks_complaints_detail_probe.py"
  - "apps/seller_portal_feedbacks_complaints_detail_probe_smoke.py"
  - "apps/sheet_vitrina_v1_feedbacks_complaints_smoke.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Phase 4 live page composition остаётся server-driven; вкладка `Отзывы` теперь содержит подразделы `Отзывы`, `AI-промпт разбора` и `Жалобы`, snapshot-independent bounded feedback date picker, strict server-side feedback period/star/answered load diagnostics, official WB review tags/chips (`review_tags` from fields such as `bables`), Excel export of the current table, resizable feedback columns, full-width prompt editor, discovered AI model selector and transient OpenAI-backed analysis via saved server-side prompt/model. `Жалобы` читает operational runtime-журнал и запускает async read-only status sync job из WB `Мои жалобы`: public POST быстро возвращает `run_id`, UI poll-ит job route и перечитывает журнал после финала. Operator-selected complaint submit is now available only inside the auth-protected operator UI as an async `submit-selected` backend job over selected non-journaled feedback rows after AI analysis; it reuses the existing guarded Seller Portal submit runner/actionable resolver, hard-caps `max_submit<=5`, records bounded event log/report state and keeps exact/actionable/description gates. Post-submit uncertainty is handled by read-only confirmation and detail/network probe runners that may update only the runtime journal after direct-id or strict strong-composite proof."
---

# 1. Идентификатор и статус

- `module_id`: `web_vitrina_page_composition_block`
- `family`: `web-vitrina`
- `status_transfer`: phase-4 live page composition перенесён в `wb-core`
- `status_verification`: targeted composition smoke, browser smoke и hosted/public probe подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`
- `status_live_publication`: current production verify target is `https://api.selleros.pro`; IP-only HTTP or missing `443 ssl` for this page is production publication drift, not an acceptable web-vitrina variant

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - stable `web_vitrina_contract` v1
  - stable `web_vitrina_view_model` v1
  - stable `web_vitrina_gravity_table_adapter` v1
  - existing sibling routes `GET /sheet-vitrina-v1/vitrina` + `GET /v1/sheet-vitrina-v1/web-vitrina`
- Семантика блока: не делать новый frontend platform contour и не переносить business truth в браузер, а materialize-ить repo-owned page-only layer над уже готовыми seams.

# 3. Target contract и смысл результата

- `GET /sheet-vitrina-v1/vitrina` теперь является реальной usable web-vitrina page:
  - canonical entrypoint for the unified `sheet_vitrina_v1` UI; first visible tab is `Витрина`, alongside `Расчет поставок`, `Отчеты`, `Отзывы` and `Исследования`
  - `GET /sheet-vitrina-v1/operator` remains a compatibility entry and renders the same unified shell instead of the former narrow operator-only page; embedded operator-only panels are reserved for the unified tabs and internal compatibility probes
  - compact top panel inside `Витрина`: `Загрузить и обновить` is the single primary manual action, `JSON Connect` and the old cheap `Обновить` button are not rendered, and no permanent top status badge duplicates the summary cards
  - while `Загрузить и обновить` is running, the top panel shows a minimal stage-based progress bar driven by the existing async job/log polling (`start/queued`, source fetch, prepare/materialize, load/update table, finish); after completion the progress bar disappears and the final semantic status stays in the summary/log surfaces
  - compact summary with separate `Последнее обновление страницы` and `Свежесть данных`
  - compact historical period control now sits in the primary table toolbar, between summary/action context and the table; the old always-expanded `История` block is not rendered by default, so the operator sees the table immediately after a narrow date-range strip
  - table controls are one compact toolbar above the table, not a separate `Фильтры и настройки` section: `Диапазон`, `Поиск`, `Секции`, `Группа`, `Scope`, `Метрики`, `Столбцы`, `Сортировка` share the same line/wrapping strip and reuse the existing local filter/search/sort/column-visibility state
  - custom browser floating controls in the unified shell close on outside click and `Escape`: historical period popover, column multiselect, feedbacks date-range picker, research SKU/metric multiselects, research date-range pickers and embedded operator stock-report SKU selector. Inside clicks for checkbox multiselects/date-range selection stay inside the control, and the rule is browser-only UX state, not backend/data truth.
  - main table display headers are Russian (`Раздел`, `Метрика`, `Обновлено`, etc.); backend/API keys stay stable, while `Обновлено` surfaces per-row last successful update timestamp from snapshot metadata
  - `Загрузить и обновить` = canonical server-side refresh from external sources + page reread, without Google Sheet write path
  - two server-driven action-adjacent information blocks:
    - `Загрузка данных` = lazy detailed source-status surface. Initial page composition renders only a calm `not_loaded` state plus the explicit button `Загрузить`; it must not auto-render source group shells or the misleading row `Источники группы пока не представлены в status payload.` before the operator asks for details. On click, the page makes a read-only request to the same web-vitrina read route with `surface=page_composition&include_source_status=1` and the visible `snapshot_as_of_date` from the current page payload; the browser must not infer the business date from its own clock or use the `today_current` column as a ready-snapshot key. The loaded surface then renders the grouped compact table derived from per-source upload/fetch truth: stable groups `WB API`, `Seller Portal / бот`, `Прочие источники`; each group has a compact date control, `Обновить группу`, group-level last update timestamp, and rows with server/business today and yesterday statuses, reason text, Russian metric labels and secondary technical endpoint. Every metric visible in the main table belongs to exactly one loading group; residual calculated/formula metrics such as proxy profit and proxy margin live in `Прочие источники`.
    - `Seller Portal / бот` group additionally renders bounded session status on the left side of the group header and session controls (`Проверить сессию`, `Восстановить сессию`, `Скачать лаунчер`) over the existing seller-session/recovery seams; the hosted deploy contract owns the required EU runtime dependencies for that contour, including `/opt/wb-web-bot/venv/bin/python`, pinned Playwright/psycopg2 in that venv, localhost-only noVNC/Xvfb tooling and Chromium launchability. The same group-refresh path reads materialized bot-backed source truth from the localhost-only owner API on `127.0.0.1:8000` (`wb-ai-api.service`) after `/opt/wb-web-bot/bot` capture and `/opt/wb-ai/run_web_source_handoff.py` handoff, not from a public nginx route.
    - `Лог` = compact fixed-height tail below the loading table plus `Скачать лог` via existing job/log contour; if exact transient job for the visible snapshot is unavailable, block must show persisted semantic fallback instead of stale green success
    - former `Обновление данных` is not rendered as a page-composition activity block; persisted `STATUS` rows remain internal truth for status/read contracts
  - compact table toolbar
  - table container
  - truthful `loading / empty / error` states
  - `Расчет поставок` and `Отчеты` reuse the existing operator template/actions in embedded mode, preserving factory/WB supply blocks and the internal report subsection selector (`Ежедневные отчёты`, `Отчёт по остаткам`, `Выполнение плана`) without changing business routes; embedded height is measured from the actual `.page` content rather than iframe viewport/body `100vh`, and edge wheel gestures are relayed to the parent shell so these tabs do not create large empty scroll tails or swallow the first trackpad scroll
  - `Отзывы` is a same-shell manual read-only tab over `GET /v1/sheet-vitrina-v1/feedbacks`: the operator chooses a bounded feedback date range with the same compact calendar/popover style, selects stars and answered/unanswered filter, then explicitly loads a normalized table from official WB API feedbacks. The normalized row includes official WB review chips as `review_tags` with `tag_source=official_wb_api` when fields such as `bables` are present; response meta exposes `rows_with_tags_count`, `tag_source` and `tag_enrichment_status`. The feedback date picker is independent from web-vitrina ready-snapshot availability: its max date comes from server/business time (`server_now_business_tz` / `generated_at`) rather than `snapshot_as_of_date` or available ready dates. The backend owns the exact filter semantics: it accepts a bounded 62-day window, chunks the requested period, paginates WB `take/skip`, dedupes, applies final strict `date_from/date_to/stars/is_answered` filters and returns diagnostic counts (`raw_fetched_count`, `deduped_count`, `final_filtered_count`, page/chunk counters, truncation state and earliest/latest final dates). Browser reloads clear stale feedback rows/AI state before a new request, and the visible summary shows requested filters, loaded count, actual date span and truncation warnings instead of silently accepting old rows or hidden caps.
  - The feedback table is internally scrollable around the operator-visible row window, keeps long feedback/pros/cons/answer text bounded, renders the `Теги` column as compact chips/text (`—` when absent), supports manual column resizing with namespaced localStorage (`wb_core_feedbacks_column_widths_v1`) and a reset action, includes `Скачать Excel по текущему фильтру` for the current visible rows including `Теги отзыва`, includes a `Жалоба` status column joined from the runtime complaint journal by `feedback_id`, and includes AI columns (`Подходит для жалобы`, `Категория`, `Причина`, `Уверенность`) that show `Не разобрано` before analysis. The left selection checkbox is disabled before AI analysis completes, while a submit job is running, or when the row already exists in the complaint journal; select-all selects only enabled rows.
  - The same tab has a nested `AI-промпт разбора` subsection backed by `GET/POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt`. The UI shows a full-width editable starter prompt when no saved prompt exists, exposes the current/available AI model selector from server-side OpenAI model discovery, and saves prompt+model server-side; unavailable preferred models are not selectable, and discovery fallback is explicit. Only a saved server-side prompt enables real analysis. The `AI-разбор отзывов` action processes the current visible/filtered table as a bounded sequential queue, sends one row per `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` including `review_tags`/`tag_source`, updates matching rows progressively, supports stop/retry for unresolved rows and refuses oversized visible queues with a clear filter-narrowing message. The response fills the existing feedback table, adds AI filter (`Все / Подходит для жалобы / На проверку / Не подходит / Не разобрано`) and sorts analyzed rows as `Да`, `Проверить`, `Нет`, `Не разобрано` while preserving review date desc inside each group. AI category schema is restricted to real WB complaint categories (`Отзыв оставили конкуренты`, `Другое`, `Отзыв не относится к товару`, `Спам-реклама в тексте`, `Нецензурная лексика`, `Отзыв с политическим контекстом`, `Угрозы, оскорбления`, `Фото или видео не имеет отношения к товару`, `Нецензурное содержимое в фото или видео`, `Спам-реклама на фото или видео`); former internal labels such as `Недостаточно данных`, `Претензия к товару`, `Доставка, ПВЗ или логистика WB` and `Другой товар или медиа` are not valid output categories. The starter prompt and server-side validator treat non-empty `review_tags` as review content: AI must not say "отзыв без текста"/"нет описания" when tags exist, and product tags such as `Плохое качество` normally lead to `complaint_fit=no` unless another formal complaint ground is present. The `Причина` column now means text for the WB modal field `Опишите ситуацию` for `Да/Проверить`, while `Нет` rows use it only as a short "Жалобу не подавать: ..." explanation.
  - Feedbacks AI output is transient browser/session display over a server-side OpenAI call; it does not persist AI labels, does not write accepted truth/ready snapshots/ЕБД, does not submit complaints, does not call Seller Portal and does not use Google Sheets/GAS.
  - `apps/seller_portal_feedbacks_complaints_scout.py` is a bounded read-only Seller Portal scout for future complaint workflow feasibility. It reuses the existing `/opt/wb-web-bot/storage_state.json` session contour and Playwright conventions, can inspect `Отзывы и вопросы`, visible feedback rows, row-level `...` menus, the `Пожаловаться на отзыв` complaint category modal and `Мои жалобы`, and writes sanitized JSON/Markdown diagnostics outside committed source. It must not click final complaint submit/save buttons, edit feedback answers, persist AI/operator labels, write accepted truth, call Google Sheets/GAS or expose a public HTTP route.
  - `apps/seller_portal_feedbacks_matching_replay.py` is a bounded no-submit replay for future complaint matching feasibility. It loads canonical feedback rows through `SheetVitrinaV1FeedbacksBlock`, reads Seller Portal `Отзывы` rows with the same storage_state contour, extracts Seller Portal cursor/DOM review tags from `goodReasons`/`badReasons` and visible `Плюсы`/`Минусы` chip blocks, compares text/tags + exact minute/date + rating + nmId/WB article/supplier article + product/media signals, and emits `exact` / `high` / `ambiguous` / `not_found` with per-row reasons and `safe_for_future_submit=true` only for `exact`. It must not open complaint submit paths, click final complaint buttons, change Seller Portal state, persist status truth or expose a public route.
  - `apps/seller_portal_feedbacks_filter_dom_scout.py` is a focused read-only live DOM scout for the real Seller Portal `Фильтры -> Оценка отзыва` popup. It opens the feedback filters, activates the rating section, saves a sanitized popup DOM summary plus screenshot, records stable selector candidates for the popup root, `1★` row/custom checkbox and `Применить`, and never submits complaints or writes complaint journal state.
  - `apps/seller_portal_feedbacks_actionable_resolver.py` is the shared no-submit Seller Portal DOM materialization/actionability helper used by both `target_row_probe` and the guarded submit runner. It owns the proven path for status tab selection, candidate date filter, real `Оценка отзыва` star filter, bounded DOM collection, exact row scoring, row-menu inspection and optional complaint-modal dry-run, and always reports `submit_clicked=false` unless the caller later enters the separately guarded real-submit branch.
  - `apps/seller_portal_feedbacks_target_row_probe.py` is a narrower read-only diagnostic runner for one explicit official-WB date/star slice. It loads canonical feedback rows without AI selection, applies the corresponding Seller Portal UI status/date/star filters, uses the shared robust rating-popup controller that can handle custom checkbox rows under `Оценка отзыва`, records selected stars before/after and apply/list-update diagnostics, collects DOM rows plus the read-only Seller Portal cursor rows, compares API vs UI counts including false/true/all `is_answered` API breakdown, tries to exact-match API rows to DOM rows, delegates row materialization/menu/modal checks to the shared actionable resolver, and may open the matched row menu plus complaint modal only to read categories and close it. It must keep `submit_clicked=false`, avoid journal writes and never expose a public route.
  - `apps/seller_portal_feedbacks_complaint_dry_run_plan.py` is a bounded no-submit dry-run runner for the future complaint draft chain. It loads canonical feedback rows, runs transient saved-prompt AI analysis, selects only `complaint_fit=yes/review`, hard-denylists the historical uncertain `feedback_id=GPe9vrq0kctlSfobrgq2` and the already-successful investigation id `feedback_id=fdQpHhNXTosEkArTHAZF`, records API/UI tag diagnostics, blocks modal drafting when AI reason says text is absent while tags exist, and requires the filter-aware actionable-row resolver to prove an exact actionable Seller Portal DOM row before modal work. A preliminary unfiltered `high`/`ambiguous`/`not_found` match is not submit proof; it only defers the candidate to the resolver, which applies answered/unanswered feedback subtabs, candidate date range filter, `Оценка отзыва` star filter, safe WB article/nmId search and bounded virtualized scroll. The runner may open the scoped row complaint modal only after the row menu exposes `Пожаловаться на отзыв`, select `Другое` by default or the AI WB category label when explicitly allowed, wait for the scoped WB `Опишите ситуацию` textbox after category selection, fill it from AI `reason` without legacy prefixes, blur it and re-read the field value. Dry-run `draft_prepared` requires `value_match=true`. It must not click final complaint submit/save buttons, create durable complaint state, persist statuses/AI labels, change prompt logic, call Google Sheets/GAS or expose a public route.
  - `Жалобы` is the third nested subsection inside `Отзывы`. It renders a table over the runtime complaint journal from `GET /v1/sheet-vitrina-v1/feedbacks/complaints`, reuses the feedback table visual pattern, has localStorage-only complaint column visibility (`wb_core_feedbacks_complaints_visible_columns_v1`), and exposes `Обновить статусы` as an async read-only status job: `POST /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status` quickly returns `sheet_vitrina_v1_feedbacks_complaints_status_sync_job` with `run_id`, `status`, `poll_url` and `complaints_url`, while `GET /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job?run_id=...` returns current queued/running/success/error state, safe report paths and summary counters. The UI disables the button while queued/running, shows `run_id`, started/finished times and final counters, polls every few seconds, then refreshes `GET /feedbacks/complaints` after success/error. It must not auto-sync on page load.
  - Protected operator-selected complaint submit lives on `POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected` plus `GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job?run_id=...`. The `Отзывы` table button `Подать выбранные жалобы` sends selected `feedback_ids` only after AI analysis, hard-caps `max_submit<=5`, rejects more than 20 selected ids, allows one active job at a time and renders a scrollable `Лог подачи жалоб` with bounded event entries and final counts. The job skips journaled/denylisted rows, reuses the existing guarded submit runner/actionable resolver and records only safe report paths/events without secrets.
  - `apps/seller_portal_feedbacks_complaint_submit.py` is the bounded instrumented controlled submit runner used by CLI and by the protected submit-selected job. Real submit still requires the explicit guarded runner branch, `--dry-run 0` plus `--i-understand-this-submits-complaints` in CLI mode; `--max-submit` is hard-capped at 5 final clicks for small controlled batches. The runner also supports `--target-feedback-id` for an explicit no-submit diagnostic over one loaded API row. It always hard-denylists the historical uncertain `feedback_id=GPe9vrq0kctlSfobrgq2` and the already-successful empty-description investigation id `feedback_id=fdQpHhNXTosEkArTHAZF`, loads canonical feedback rows, runs saved-prompt transient AI analysis, selects fresh `yes` candidates first and then `review` candidates for bounded actionability checks, skips existing runtime complaint records by `feedback_id`, blocks `reason_contradicts_review_tags` when AI says the review has no text while API/UI tags exist, and reuses the same shared filter-aware actionable-row resolver before opening a complaint modal. If one candidate is exact in cursor but cannot be materialized as an actionable DOM row, the runner records that blocker and proceeds to the next eligible candidate. The runner may continue after a `confirmed_success` until the explicit cap is reached or candidates are exhausted, but it stops immediately on validation/network/unconfirmed submit results or uncertain modal/page state. Preliminary matching may be non-exact for historical/answered slices, but it never authorizes submit; final submit remains blocked unless the resolver materializes an exact actionable Seller Portal DOM row with `Пожаловаться на отзыв`, the modal opens, the AI WB category label or `Другое` is selected, the `Опишите ситуацию` textbox is ready after category selection, AI `reason` is filled without legacy prefixes, blurred, and the re-read field value matches the intended text. Around each possible final click it records pre-click modal state, selected category/text, API/UI tag diagnostics, submit button label/enabled state, validation hints, sanitized network response/request-payload description summaries, toast/snackbar/modal messages, post-click modal state and row/menu state. The journal status is evidence-based: `confirmed_success` becomes `Ждёт ответа`; `confirmed_validation_error`, `confirmed_network_error` and `unconfirmed_after_click` become `Ошибка`. A click alone is not success.
  - `apps/seller_portal_feedbacks_complaints_status_sync.py` is a read-only Seller Portal sync runner over `Мои жалобы`. The public route wraps this runner in bounded runtime JSON job state under the runtime dir, allows only one active sync job at a time and returns an already-running `run_id` instead of starting duplicate browser automation. The runner reads `Ждут ответа` and `Есть ответ`, maps statuses to `Ждёт ответа` / `Удовлетворена` / `Отклонена` / `Ошибка`, accepts only direct id or strict strong-composite matches, reports direct/strong/weak counters, rejects weak matches, and updates only the operational complaint journal, not accepted truth/ЕБД/Google Sheets/GAS.
  - `apps/seller_portal_feedbacks_complaint_confirmation.py` is a bounded read-only confirmation runner for a single previous submit attempt. It checks the original Seller Portal feedback row and `Мои жалобы`, accepts only direct `feedback_id` proof or strong composite proof, updates the runtime journal only when confirmed, otherwise keeps `Ошибка` with a precise `last_error`, and must report `submit_clicked_during_runner=0`.
  - `apps/seller_portal_feedbacks_complaints_detail_probe.py` is a bounded read-only detail/network probe for `Мои жалобы`. It can inspect visible complaint rows, open read-only detail panels/cards, capture sanitized complaint-related network response shapes/ids without headers/cookies/tokens, accept only direct linked `feedback_id`/review id or strict strong composite proof, and must not open complaint creation or final submit paths.
  - `Исследования` is a same-shell read-only tab, not an iframe and not a new truth contour; MVP block `Сравнение групп SKU` reads active SKU from current `config_v2`, selectable non-financial SKU metrics from current registry/view truth, and calculates retrospective group dynamics over persisted ready snapshots only
  - research SKU selectors include independent compact `Товар в акции` chips; the options route derives the candidate-only promo filter from latest closed-day ready snapshot promo metrics and returns unavailable metadata instead of fabricating a filtered list when promo truth is absent
  - research period controls are compact date-range pickers in the browser, while the calculate contract remains explicit `date_from/date_to`; result rendering uses the same `table-shell / table-scroll / vitrina-table` page pattern with horizontal scroll rather than a card layout
- Existing `GET /v1/sheet-vitrina-v1/web-vitrina` keeps the default public contract unchanged:
  - default/no-surface path still returns `web_vitrina_contract` v1
  - optional `as_of_date` keeps one-day historical read on the same route
  - optional `date_from/date_to` now materializes a bounded ready-snapshot period window on the same route
  - optional `surface=page_composition` now returns a bounded server-driven page payload for the live page shell; by default it keeps source-status details unloaded and defers heavy `table_surface.rows`
  - optional `include_table_data=1` on that page-composition surface returns the full table rows/groupings for the browser lazy table load without changing ready-snapshot truth or triggering refresh/upstream fetch
  - optional `include_source_status=1` on that page-composition surface returns the detailed grouped loading table without triggering refresh/upstream fetch and does not imply full table rows
  - page-composition `meta` exposes the explicit server-owned time model: `business_timezone`, `snapshot_as_of_date`, `yesterday_closed_date`, `today_current_date`, `visible_date_columns`, `server_now_business_tz` and `generated_at`. Business dates remain backend-owned; browser timezone is used only for readable timestamp display.
  - page-composition `meta.page_composition_diagnostics` exposes lightweight read-side diagnostics: server build duration, compact payload bytes, `include_source_status`, `include_table_data`, total/returned row count and returned cell count. This field is observability-only and does not create a new source-status endpoint or browser-owned truth layer.
  - if a web-vitrina read/model/page-composition change can affect promo metric row visibility, live/public closure must include `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` (or local-CA-only fallback `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`) to prove current promo rows remain present and expected ended/no-download artifacts are not fatal.
- `page_composition` is server-owned and assembled only from:
  - `web_vitrina_contract`
  - `web_vitrina_view_model`
  - `web_vitrina_gravity_table_adapter`
- Browser role is intentionally narrow:
  - render the received page payload
  - keep only local filter/search/sort state
  - keep only browser-owned page reread timestamp for `Последнее обновление страницы`
  - keep only transient browser-owned popup open/closed state; outside-click/`Escape` close behavior never creates server-side user state and never changes metric/source truth
  - keep only session-local cell highlighting for the last refresh result: `updated` cells render as soft green, `latest_confirmed`/fallback cells render as soft yellow, full refresh highlights every refreshed temporal date column (`yesterday_closed` and `today_current` when both are in scope), group refresh highlights only the selected group/date, and the highlight disappears on browser reload
  - failed source/date materialization must not replace prior confirmed visible values with dashes; the table preserves the prior cell when available, while the bottom loading table/status surfaces the fresh failure reason. Strict Seller Portal bot closed-day fallback from same-date `accepted_current_snapshot` is displayed only as `latest_confirmed`, not as final accepted closed truth.
  - never derive the full-refresh `as_of_date` from `date_from/date_to` or the rightmost `today_current` column; period selection is a read-side window, while `Загрузить и обновить` lets the backend resolve the current closed-day snapshot key
  - keep only session-local source-status load state for `Загрузка данных`: `not_loaded`, `loading`, `loaded`, `empty`, `error`; this state controls visibility of the detailed table and retry button but never becomes source truth
  - keep only session-local feedbacks filters/result payload for the current manual load; feedback rows are read-through WB API output and are not accepted truth, ready snapshot facts or browser-local source of truth
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
- feedbacks route/UI smokes:
  - `apps/sheet_vitrina_v1_feedbacks_http_smoke.py`
  - `apps/sheet_vitrina_v1_feedbacks_browser_smoke.py`
- read-only Seller Portal complaint scout:
  - `apps/seller_portal_feedbacks_complaints_scout.py`
  - `apps/seller_portal_feedbacks_complaints_scout_smoke.py`
- no-submit Seller Portal matching replay:
  - `apps/seller_portal_feedbacks_matching_replay.py`
  - `apps/seller_portal_feedbacks_matching_replay_smoke.py`
- read-only Seller Portal filter DOM scout and target-row probe:
  - `apps/seller_portal_feedbacks_filter_dom_scout.py`
  - `apps/seller_portal_feedbacks_filter_dom_scout_smoke.py`
  - `apps/seller_portal_feedbacks_actionable_resolver.py`
  - `apps/seller_portal_feedbacks_target_row_probe.py`
  - `apps/seller_portal_feedbacks_target_row_probe_smoke.py`
- no-submit Seller Portal complaint dry-run:
  - `apps/seller_portal_feedbacks_complaint_dry_run_plan.py`
  - `apps/seller_portal_feedbacks_complaint_dry_run_plan_smoke.py`
- controlled Seller Portal complaint submit/status contour:
  - `packages/application/sheet_vitrina_v1_feedbacks_complaints.py`
  - `apps/sheet_vitrina_v1_feedbacks_complaints_smoke.py`
  - `apps/seller_portal_feedbacks_complaint_submit.py`
  - `apps/seller_portal_feedbacks_complaint_submit_smoke.py`
  - `apps/seller_portal_feedbacks_complaints_status_sync.py`
  - `apps/seller_portal_feedbacks_complaints_status_sync_smoke.py`
  - `apps/seller_portal_feedbacks_complaint_confirmation.py`
  - `apps/seller_portal_feedbacks_complaint_confirmation_smoke.py`
  - `apps/seller_portal_feedbacks_complaints_detail_probe.py`
  - `apps/seller_portal_feedbacks_complaints_detail_probe_smoke.py`
- hosted deploy/probe contract:
  - `apps/registry_upload_http_entrypoint_hosted_runtime.py`
- auth/route publication smokes:
  - `apps/registry_upload_http_entrypoint_auth_smoke.py`
  - `apps/registry_upload_http_entrypoint_public_routes_smoke.py`

# 6. Какой smoke подтверждён

- `apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py`
  - confirms `composition_name/version`, source chain, state namespace, filter surface, timestamp-format hint for `Свежесть данных` and human-readable activity payload fields
- `apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
  - confirms real page render, visible table, lazy source-status initial state, explicit `Загрузить` details flow, filter controls, Russian activity labels/reasons, unified readable freshness timestamp without raw ISO artefacts, empty state on no-match search, reset recovery, period selector UX (`calendar + presets + date_from/date_to + save/reset`) and truthful error state when the ready snapshot is absent
- `apps/sheet_vitrina_v1_popup_outside_click_browser_smoke.py`
  - confirms outside-click/`Escape` close behavior for custom browser floating controls across `Витрина`, `Отзывы`, `Отчеты` embedded stock-report selector and `Исследования`, while checkbox multiselects and date-range first-click selection remain usable
- `apps/sheet_vitrina_v1_web_vitrina_http_smoke.py`
  - confirms default `web_vitrina_contract` path stays stable, optional `date_from/date_to` works as bounded period window and optional `surface=page_composition` works on the same route with severity-sorted human activity items
- `apps/sheet_vitrina_v1_feedbacks_http_smoke.py`
  - confirms `GET /v1/sheet-vitrina-v1/feedbacks` returns normalized `sheet_vitrina_v1_feedbacks` JSON including `review_tags` from official WB fields such as `bables`, default `is_answered=all` reads both required WB streams, bounded windows greater than 31 days are accepted, chunked/paginated fake upstream can exceed 500 rows without silent cap, period A/B star-filter counts differ truthfully when older rows exist, final rows stay inside requested range/stars, diagnostics meta including tag counters/source is present, export helper/route creates a valid XLSX with `Теги отзыва`, and the unified HTML shell exposes the `Отзывы` tab/route wiring including export and AI prompt/analyze endpoints
- `apps/sheet_vitrina_v1_feedbacks_ai_smoke.py`
  - confirms server-side prompt+model get/save validation, OpenAI model discovery metadata, safe fallback when model discovery fails, invalid/unavailable model rejection, fake-provider selected-model propagation including `review_tags`/`tag_source`, AI analyze shape, invalid provider output surfacing, exact WB complaint category enum/labels, removal of old internal categories, actionable `reason` semantics for WB `Опишите ситуацию`, review-tag prompt guidance, reason/tag contradiction rejection and HTTP prompt/analyze routes without live OpenAI calls
  - live EU prompt/analyze evidence on `https://api.selleros.pro` confirms the active saved prompt was updated at `2026-05-01T20:26:17.715541Z` with model `gpt-5.5`, contains the WB `Опишите ситуацию` reason semantics, does not contain old internal category ids, and a one-row live analyze returned `category=other`, `category_label=Другое`, `complaint_fit=review`, model `gpt-5.5` and an actionable `reason` without forbidden category labels.
- `apps/sheet_vitrina_v1_feedbacks_browser_smoke.py`
  - confirms the `Отзывы` tab opens in the unified shell, nested feedbacks/prompt/complaints subsections render, the compact feedbacks range picker closes on outside click, non-future feedback dates after a stale ready-snapshot date remain selectable, valid >31-day ranges save without hover-triggered loading/disable, star filter changes the route query and clears stale rows/export state, the feedback table is internally scrollable even for 650 fake rows, `Теги` render in the table, Excel export sends current visible rows including `review_tags`, discovered model selector/full-width prompt render, column resize handles persist widths through localStorage, saved prompt enables the filtered-set AI queue, every AI request contains one row including tags, row-level AI failures are visible and retryable, AI filter works, oversized visible queues fail before OpenAI requests, positive complaint-fit rows sort first, WB category labels are rendered instead of stale internal labels, the `Жалобы` subsection loads the runtime journal, column visibility works, `Обновить статусы` calls the async sync route only on explicit click, shows running state, polls the job route, refreshes the complaint table after fake success, exposes no submit controls, and `/load` is not used
- `apps/seller_portal_feedbacks_complaints_scout_smoke.py`
  - confirms the read-only scout parsers extract visible feedback rows including `Минусы: Плохое качество` tags/chips, scoped row-menu items (`Запросить возврат`, `Пожаловаться на отзыв`), complaint modal categories, `Мои жалобы` rows, match-score statuses and the missing-session blocker, and that submit-like labels such as `Отправить` / `Подать жалобу` are refused while the safe `Пожаловаться на отзыв` modal-open label remains allowed for scout mode
  - live EU scout evidence (`/opt/wb-core-runtime/state/feedbacks_complaints_scout/20260501T124236Z/`) confirms the Seller Portal route `Товары и цены -> Коммуникации -> Отзывы и вопросы -> Отзывы`, row-level `...` menu opening, scoped detection of `Пожаловаться на отзыв`, safe complaint modal open/close without submit, visible category samples, stronger feedback row fields (`product_title`, supplier article, WB/nmId, rating, exact date/time, pros/cons/comment/media), and `Мои жалобы` pending/answered status extraction
- `apps/seller_portal_feedbacks_matching_replay_smoke.py`
  - local smoke also confirms Seller Portal cursor `badReasons`/tag-like text normalizes into `review_tags`, matching reports include API/UI tag diagnostics, and tag-only reviews can participate in text support without creating a submit path.
  - confirms API fixture rows can be matched to UI fixture rows as `exact`, `high`, `ambiguous` short-text duplicate and `not_found`, validates WB UI datetime parsing (`01.05.2026 в 17:03`), text/article/nmId normalization, duplicate penalty, Seller Portal cursor payload parsing, coverage metrics/not_found reason split, report JSON/Markdown shape and no-submit guards (`complaint_submit_clicked=false`, no complaint modal path called)
  - live EU no-submit replay evidence (`/opt/wb-core-runtime/state/feedbacks_matching_replay/20260501T130828Z/`) confirms canonical API feedback rows load for `2026-05-01`, Seller Portal session/navigation stay valid on `/feedbacks/feedbacks-tab/not-answered`, UI row extraction exposes product/title/article/nmId/rating/date/text fields without hidden `feedback_id`, matching produces `exact/high/ambiguous/not_found` with per-row reasons, and no complaint modal or submit path is called. Bounded sample result: 30 API rows tested from 49 available, 5 UI rows collected, 4 exact, 0 high, 1 ambiguous, 25 not_found; readiness remains `not_ready` until UI date/star filter alignment or deeper list collection covers more API rows.
  - live EU matching-improvement evidence (`/opt/wb-core-runtime/state/feedbacks_matching_replay/20260501T133817Z/`, `/opt/wb-core-runtime/state/feedbacks_matching_replay/20260501T133856Z/`) diagnoses the previous 5-row coverage as Seller Portal DOM rendering only the first cursor page (`limit=5`) with no scroll delta; the replay now captures Seller Portal request headers in memory, pages the same read-only `api/v2/feedbacks` cursor endpoint, filters requested date/stars client-side, keeps DOM hidden `feedback_id=false` while reporting Seller Portal network `feedback_id=true`, and produced no-submit exact-only matches: scenario A 30/30 exact with 53 UI cursor rows, scenario B 5/5 exact for `stars=1`.
- `apps/seller_portal_feedbacks_filter_dom_scout_smoke.py`
  - confirms the real-filter helper shape on a local custom-checkbox fixture: `Фильтры` opens the popup, `Оценка отзыва` is activated, the `1★` row and `Применить` button are parsed, selecting `1★` changes the checked state after reread, report artifacts are written, and no submit/journal path exists.
- `apps/seller_portal_feedbacks_target_row_probe_smoke.py`
  - confirms the read-only target probe count comparison, exact DOM matching by direct `feedback_id` or fallback text/date/rating/article fields, row-menu parser detection of `Пожаловаться на отзыв`, complaint modal category parsing, JSON/Markdown report shape, API false/true/all breakdown reporting, filter selected-star before/after diagnostics and no-submit/no-journal-write guards.
- `apps/seller_portal_feedbacks_complaint_dry_run_plan_smoke.py`
  - confirms dry-run candidate selection prefers `complaint_fit=yes` before `review`, skips `no`, respects the historical hard denylist before modal planning, exact-only guard blocks `high/ambiguous/not_found` from direct modal authorization while deferring selected preliminary non-exact candidates to the filter-aware resolver, unique DOM row targeting can use an exact Seller Portal cursor `feedback_id` plus exact datetime/article/text support when DOM rating is absent, answered feedbacks prefer `Есть ответ`, unanswered feedbacks prefer `Ждут ответа`, fallback tries both subtabs, forced `Другое` category selection stays explicit, `force_category_other=0` can choose an available AI WB category label and falls back to `Другое` otherwise, draft text uses AI `reason` as the ready WB description without legacy prefix duplication, the `Опишите ситуацию` field is detected/focused/filled/blurred and `value_match=true` is required, aggregate/report shape records `submit_clicked_count=0`, and no-submit guards keep final complaint submission disabled.
  - live EU no-submit dry-run evidence (`/opt/wb-core-runtime/state/feedbacks_complaint_dry_run_plan/20260501T195341Z/`) confirms the primitive complaint draft chain for `2026-05-01`, `stars=1`, `is_answered=false`: canonical API loaded 8 rows, saved-prompt AI analyzed 8 (`yes=1`, `review=4`, `no=3`), one `yes` candidate exact-matched by Seller Portal cursor `feedback_id`, targeted WB-article search found the actionable DOM row, row menu exposed `Запросить возврат` and `Пожаловаться на отзыв`, the complaint modal opened, category `Другое` was selected, a short AI-derived description was filled, submit label `Отправить` was observed but not clicked, the modal closed via icon close, and post-close status stayed `unknown`/not submitted (`submit_clicked_count=0`, durable submitted state `false`).
  - live EU no-submit dry-run evidence after WB-category prompt alignment (`/opt/wb-core-runtime/state/feedbacks_complaint_dry_run_plan/20260501T202938Z/`) confirms `force_category_other=0` selected the AI WB category label `Другое` from the modal list, filled the description field with AI `reason` exactly, observed submit label `Отправить` without clicking it, closed the modal, and kept post-close complaint status `unknown` (`submit_clicked_count=0`, durable submitted state `false`).
- `apps/sheet_vitrina_v1_feedbacks_complaints_smoke.py`
  - confirms runtime complaint journal create/dedupe by `feedback_id`, status update labels, post-submit evidence metadata persistence, async status-sync job start/read, queued -> running -> success/error transitions, duplicate running-job guard, safe error persistence without journal corruption, HTTP POST/GET job routes, table schema and no auto-sync on page load.
- `apps/seller_portal_feedbacks_complaint_submit_smoke.py`
  - confirms submit candidate selection (`yes` before `review`, no `complaint_fit=no`), preliminary non-exact matches defer to the filter-aware resolver rather than authorizing submit, hard denylist skip for `GPe9vrq0kctlSfobrgq2`, duplicate skip, ready complaint reason validation, explicit real-submit flag requirement, hard max-submit cap 5, click-alone-is-not-success classification, validation/network/success classification, network sanitizer and journal record shape.
- `apps/seller_portal_feedbacks_complaints_status_sync_smoke.py`
  - confirms read-only status sync maps `Ждут ответа` to `Ждёт ответа`, accepted/approved answered rows to `Удовлетворена`, rejected rows to `Отклонена`, direct `feedback_id` overrides text mismatch, weak matches are rejected/diagnosed, and unmatched rows are reported.
- `apps/seller_portal_feedbacks_complaint_confirmation_smoke.py`
  - confirms single-feedback read-only confirmation maps direct pending/accepted/rejected proof, accepts strong composite proof, rejects weak text-only matches, does not create duplicate journal records and can update only the runtime journal with no submit path.
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
- For full refresh, an explicit current business date is normalized to the previous closed-day snapshot key; this prevents invalid current-day ready snapshots with duplicate `today_current`/`yesterday_closed` columns and keeps highlight metadata on both temporal slots.
- `Загрузка данных` and `Лог` stay server-driven:
  - source-status details are lazy-loaded: initial page-open keeps a `not_loaded` state and the `Загрузить` button; no grouped source rows, no group action controls and no session controls are rendered until the explicit read-only details request succeeds
  - if the explicit details request asks for a ready snapshot that is not materialized, the loading table returns `source_status_state=missing_snapshot` with an actionable Russian message to run `Загрузить и обновить`; it must not render fake group shells or generic `upload summary unavailable` as the primary operator message
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
  - the collapsed control shows `DD.MM.YYYY - DD.MM.YYYY` plus a calendar icon above the table
  - first open / hard refresh without explicit query uses the latest four server-readable business dates inclusive, ending on backend-owned `today_current_date` when that date is present in the current visible/readable context
  - opened state is a compact one-month picker, not the former expanded `История` section: header has previous/next month arrows, the calendar renders only one month, and technical mode/default/query-state explanations are not user-facing
- Table control UX is intentionally thin:
  - the former expanded `Фильтры и настройки` card is not rendered as a separate section
  - search, section/group/scope/metric filters, column visibility and sorting live in the compact toolbar next to `Диапазон`
  - controls stay browser-local and only filter/sort the already received page payload; they do not trigger source refresh, data writes or new truth semantics
  - presets (`Неделя`, `2 недели`, `Месяц`, `Квартал`, `Год`), manual `date_from/date_to` fields and `Сбросить`/`Сохранить` live below that one-month calendar in the same small popover
  - `Сохранить` rewrites query string and re-reads server payload through the existing `date_from/date_to` ready-snapshot window path
  - `Сбросить` removes `as_of_date/date_from/date_to` and returns to the same latest-four-days default
  - source-status lazy load uses `source_status_snapshot_as_of_date` from the server contract, so a period ending in `today_current` does not accidentally use the current-day column as the ready-snapshot key
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
