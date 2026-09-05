---
title: "Модуль: sku_inventory_balance"
doc_id: "WB-CORE-MODULE-53-SKU-INVENTORY-BALANCE"
doc_type: "module"
status: "active"
purpose: "Зафиксировать server-owned подраздел `Управление SKU → Баланс запасов`: immutable расчёты темпа и ручные рекламные решения, раздельные ручные overrides, расширенный XLSX и durable подтверждаемое применение ставок и состояний кампаний."
scope: "Одна строка на active nmID поверх canonical SKU Management evidence; conservative stock pacing, old CPM/new CPC campaign groups, manual target decisions and reload-safe live WB bid/campaign-state application protocol."
source_basis:
  - "packages/application/sku_inventory_balance.py"
  - "packages/application/sku_management.py"
  - "packages/application/sheet_vitrina_v1_ads.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_modules:
  - "46_MODULE__SKU_MANAGEMENT_BLOCK.md"
  - "37_MODULE__SHEET_VITRINA_V1_ADS_OPERATOR_BLOCK.md"
  - "52_MODULE__WEB_VITRINA_INVENTORY_HISTORY.md"
  - "58_MODULE__CHANGE_REGISTRY_INTERNAL_WRITERS.md"
related_tables:
  - "sheet_vitrina_v1_inventory_balance_operations"
  - "sheet_vitrina_v1_inventory_balance_calculations"
  - "sheet_vitrina_v1_inventory_balance_overrides"
  - "sheet_vitrina_v1_inventory_balance_apply_jobs"
  - "sheet_vitrina_v1_inventory_balance_apply_items"
  - "sheet_vitrina_v1_inventory_balance_outcomes"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/settings"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/calculate"
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance/operations/{operation_id}"
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance/calculations?limit=20"
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance/calculations/{calculation_id}"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/calculations/{calculation_id}/override"
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance/calculations/{calculation_id}/xlsx"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/apply-jobs"
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance/apply-jobs/{job_id}"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/apply-jobs/{job_id}/resume"
related_runners:
  - "apps/sku_inventory_balance_smoke.py"
  - "apps/sku_inventory_balance_browser_smoke.py"
  - "apps/sku_inventory_balance_live_apply_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Compact inline cells отдают основное место полноценной ставке, скрывают campaign detail за последней info-кнопкой и используют fact-preserving play/pause menu; manual-only policy исключает автоматические рекламные цели; остатки FBS читаются из полного официального снимка без lifecycle."
---

# 1. Product surface and ownership

`Управление SKU` содержит два first-level подраздела:

- `Общее` — прежний SKU Management surface без изменения его расчётной и write-логики;
- `Баланс запасов` — отдельный calculation/decision surface с одной строкой на active `nmID`.

Оба подраздела используют authorization section `sku_management` и текущую WebCore session. Отдельной identity/role model нет. Row universe, товарная identity, operational stock timeline и campaign identity приходят только из действующих SKU Management и Ads Operator contracts. Browser не создаёт stock, sales или campaign truth.

`sheet_vitrina_v1_user_configs` с key `sku_inventory_balance` хранит calculation settings и table preferences: видимость, порядок, ширины колонок, search/status filters и preset. Обязательные колонки `Выбор` и `Название / nmID` всегда остаются первыми. `localStorage` не является источником настройки.

Колонки `CPC · ставка / состояние` и `CPM · ставка / состояние` симметричны: default width `360px`, принимаемый диапазон server/client normalization `350..360px`. Ранее сохранённая чрезмерная ширина bounded clamp-ится при чтении/следующем сохранении только для этих двух presentation columns; остальные preferences и business data не меняются. Внутренняя группа controls не использует растягивающий `1fr` и остаётся прижатой к началу ячейки, поэтому более широкая legacy table allocation не создаёт пустоту между current/input/status/info.

Экран содержит ровно одну компактную строку на SKU высотой не более примерно двух видимых текстовых строк в каждой ячейке. Горизонтальный table shell, server-owned порядок/видимость/ширины колонок и sticky `Выбор` + `Название / nmID` сохраняются. Обычная работа не создаёт subordinate detail row, spoiler или отдельный служебный блок под SKU.

Важная human-readable причина отображается компактным warning badge/icon в той же строке с accessible tooltip. Raw quality payload, service tokens, WB/source lineage, shipment identities, digests и provenance не выводятся в рабочую строку; immutable provenance остаётся в calculation/XLSX/backend contracts.

# 2. Immutable calculation registry

Каждый явный `Новый расчёт` сначала создаёт durable row в `sheet_vitrina_v1_inventory_balance_operations`. Browser до POST генерирует stable `operation_id` и отдельный `idempotency_key`, сохраняет только эту recovery identity, а server атомарно принимает exact sanitized settings и возвращает быстрый `202` с byte-stable acceptance receipt. Повтор того же `user + idempotency_key` с тем же digest возвращает те же bytes и не запускает второй worker; divergent payload или занятая identity fail closed. После transport uncertainty browser выполняет только `GET .../operations/{operation_id}` и не делает blind resubmit.

Operation contract `sheet_vitrina_v1_sku_inventory_balance_operation/v1` имеет server-backed states `accepted → running → succeeded|failed`, versioned phase/progress, controlled error, durable outcome и единственный nullable result. Один process держит максимум один calculation worker thread и не имеет очереди; HTTP request завершается до тяжёлого source/Ads расчёта, поэтому current single-thread `HTTPServer` продолжает обслуживать соседние GET/health. Новая independent operation при занятом worker получает controlled `409`; same-key readback остаётся доступен. После process interruption незавершённая operation terminalize-ится как explicit `failed/no_calculation_created` и retry разрешён только новой operation identity.

Только terminal `succeeded` в одной SQLite transaction вставляет ровно одну строку calculation с unique `operation_id` и записывает этот же `calculation_id` в operation outcome. Failure до insert остаётся explicit и не выводится как success по косвенным признакам. Bounded observability логирует и отдаёт через status только sanitized operation id, phase, duration и durable outcome без payload, user identity или secrets.

Каждая successful operation создаёт новый `calculation_id`. Строка в `sheet_vitrina_v1_inventory_balance_calculations` содержит versioned formula, source digest, settings, полный payload, actor/time, `operation_id` и `previous_calculation_id`. SQLite triggers запрещают update/delete calculation rows. Новый source snapshot никогда не переписывает прежнее решение. Bounded registry endpoint сохраняется для recovery/XLSX/provenance consumers, но полный реестр расчётов не отображается на экране `Баланс запасов` и не дублирует sibling `Реестр изменений`.

Каждое открытие/reopen подраздела делает lightweight server read и показывает последний immutable calculation, а не сохранённое SPA-состояние. Рядом показываются фактические `created_at`, возраст, `as_of_date`, source generated time и Ads window, если они доступны; UI не придумывает semantic green/stale verdict. Единственное действие пересчёта называется `Новый расчёт`; отдельной кнопки `Обновить` нет. Terminal operation сначала атомарно публикует `operation.result` в таблице и только затем допускает независимые secondary refresh; initial `GET latest=500`, delayed/error Registry endpoint или пустые persisted `column_order=[]`/`visible_columns=[]` не блокируют результат и не требуют reload. Older in-flight latest/override responses не могут перезаписать новый `calculation_id`; drafts, timers и selection также scoped точным calculation id.

Расчёт возвращает lineage к предыдущему calculation и future observation fields. Наблюдения имеют отдельную append-only таблицу `sheet_vitrina_v1_inventory_balance_outcomes`; исходный calculation и применённые значения не переписываются результатом наблюдения.

Автоматическое ML, обучение на outcomes, causal lift и automatic target tuning отсутствуют. Поле `automatic_ml_or_training=false` является частью публичного contract.

# 3. Conservative stock pacing

Формула `sku_inventory_balance_official_fbs_manual_v3` использует отдельный SKU Management evidence contract. Текущий opening по всем фронтам равен `stock_ff + wb_confidence_coefficient × stock_wb`; server-owned user setting имеет default `0.5`, диапазон `0..1`, не nullable и применяется только к WB (`0` полностью исключает WB, `1` учитывает его полностью). Каждый immutable calculation сохраняет выбранный коэффициент в settings и в per-SKU WB evidence lineage. Оба stock-поля должны присутствовать явно: missing не реконструируется из forecast timeline и не превращается в zero. Текущий FBS входит в opening сейчас и никогда не показывается как будущая поставка. Поле `stock_ff` сохранено для совместимости, но в новых Balance evidence содержит сумму официально заявленных FBS по точным активным facilities (`current_official_fbs_facilities`). Полный snapshot должен покрывать universe, соответствовать текущему business day и быть не старше 30 минут. Source generation/digest, facility/run identity и quantities сохраняются в immutable lineage/XLSX. Старый FF ledger, lifecycle и общая сборка `build_table` не вызываются; старый FF не прибавляется к FBS. Неполный, устаревший снимок или facility вне complete scope оставляет остаток unknown без fallback/refresh. Отдельный физический запас вне официального FBS этим источником не доказывается. `Общее` не меняется.

Обычный источник `stock_wb` остаётся strict warehouse-granular incident projection SKU Management. Единственное Balance-specific исключение: если complete official current snapshot покрывает exact requested nmID universe, имеет `pagination_complete=true`, exact snapshot date, raw-row SHA-256 digest и конечное неотрицательное numeric `stock_total` для каждого SKU, но `warehouse_granularity_complete=false`, Balance может использовать этот per-SKU aggregate total как opening WB evidence. Calculation row и lineage сохраняют source contract, snapshot/fetched time, digest, quantity, `mode=aggregate_per_sku_total`, `incident_projection_applied=false`, выбранный коэффициент и explicit quality warning. Агрегат не становится warehouse, region или incident row и не распределяется по складам. Partial coverage, duplicate/missing SKU identity, missing/malformed quantity/date/digest или incomplete pagination оставляют `stock_wb` unknown; zero допустим только как exact value внутри принятого complete snapshot.

Demand пересчитывается для самого balance calculation по выбранному `sales_period_days=7|14|30|60` через shared availability-adjusted sales history. Lineage хранит exact lookup window, requested valid-day period и demand mode; период Ads statistics совпадает с этим окном, но не подменяет sales evidence.

Future milestones — только exact supplier-registry rows `production`/`in_transit` с положительной `matched_by_barcode` nmID quantity. Identity дедуплицируется по `source + source_id + date + district`; дубликат исключается с quality warning. `in_transit` ETA считается как `actual_shipment_date + lead`, `production` — `planned_shipment_date + lead`. Lead — среднее по последним четырём (минимум трём) завершённым shipments от actual shipment до actual FF acceptance; exact samples/mean/applied days записываются в lineage. При меньшем числе samples используется configured factory-to-FF lead с `partial` quality. Synthetic orders, current FF transfer, WB-only FF→WB movement, compatibility match и name-based guesses не являются milestone.

Горизонт заканчивается на последней eligible exact поставке, даже если она позже incumbent SKU forecast. При отсутствии такой поставки target/status fail closed в `Недостаточно данных`; фиксированный synthetic 180-day horizon не используется.

Для каждого milestone вычисляются две границы:

- hard pace = minimum `available before this arrival / days to arrival`;
- reserve pace = minimum `available before this arrival / (days to arrival + safety_stock_days)`.

Товар самой поставки добавляется только после constraint её даты и участвует в следующем milestone. При нулевых наблюдаемых продажах и положительном overstock target используется bounded launch ratio `bid_scale_max` с warning об отсутствии наблюдаемой эластичности; ложный `Баланс` не возвращается.

Текущий темп выше hard pace даёт `Дефицит` и target=hard pace. Текущий темп ниже reserve pace даёт `Переизбыток` и target=reserve pace. Значение между границами даёт `Баланс` и сохраняет текущий темп. Строка показывает известный запас, текущий/целевой темп, изменение, days cover, limiting date, ближайший и последующий ненулевой inbound. Это decision projection, а не гарантия будущих продаж или автоматическое изменение рекламы.

# 4. Campaign recommendations and overrides

Campaign rows сохраняют exact `nm_id + advert_id + placement`. `payment_type=cpc` отображается в группе `Новые CPC`, `payment_type=cpm` — в `Старые CPM`; неизвестный тип не становится одной из этих групп. CPO считается только при положительных campaign/SKU orders: `spend / orders`; missing orders оставляют CPO пустым.

Автоматические рекламные рекомендации отключены: `calculated_target_bid_rub=null`, `recommendation_quality=not_generated`, `allocation_action=null`. Текущие ставки, состояния, CPO и расчёт запасов сохраняются. Пустая цель означает отсутствие решения, не hold. Поля коэффициентов старого алгоритма не запускают его. Новые расчёты явно сохраняют `automatic_advertising_recommendations=false`.

Inline CPC/CPM cells показывают для каждого exact campaign target одну плотную логичную группу: полноценный numeric target input с видимым caret и многозначным значением, читаемую единицу, компактную фактическую current bid, status control и последнюю маленькую accessible info-кнопку. Постоянного текста/кнопки `Кампания` нет. CPC подписан `₽/клик`, CPM — `₽/1000 показов`. Info click открывает compact popover с полным campaign name, exact `advert_id`, placement, фактическим current state, warnings и identity detail; guessed portal deep-link отсутствует. Поле новой ставки изначально пустое. Ручной ввод доступен при точной campaign identity и известной текущей ставке независимо от полноты прогноза запасов. Старые расчётные рекомендации не подставляются даже при открытии исторического расчёта. Несколько targets одного типа не смешиваются: каждый имеет отдельные bid/state controls в той же SKU row, без subordinate row.

Inline manual override записывается только в `sheet_vitrina_v1_inventory_balance_overrides`. Исторический immutable calculated target сохраняется как история; final target всегда равен только manual value. Очистка возвращает пустую цель, а не старую рекомендацию. Unknown inventory pacing не блокирует ручной ввод; exact identity/current/minimum/status/payment/placement/CAS guards остаются. Apply selection принимает только явный exact valid target с реальным изменением.

Editable bid control находится только inline в CPC/CPM cell и подписан единицами. Значение durable сохраняется по debounced input и немедленно по change/Enter; корректность не зависит от неочевидного blur и отдельной кнопки Save. Любой successful durable manual override, который действительно отличается от current bid, автоматически отмечает SKU без второго click; failed/stale save этого не делает. Если первый click выбора пришёл во время pending override save, он остаётся explicit selection intent и применяется только после successful response. UI различает `сохраняется`, `готово к выбору`, controlled save error и точную business-причину блокировки; stale response не меняет более новый draft/calculation. При доступной auto target control показывает effective final target; возврат к calculated value очищает manual override. Blank, invalid, equal-current и unchanged target не входят в atomic apply selection.

# 5. Selection, confirmation and durable apply

Для каждой exact campaign рядом со ставкой показывается фактический status: зелёный play для `9/active`, красный pause для `4/ready` или `11/paused`; unknown/unsupported state остаётся neutral non-actionable с точным raw-status/identity reason. Click по action-capable status открывает compact menu только с одним уместным действием: `остановить` через reversible pause для active либо `запустить/возобновить` через start для ready/paused. Отсутствие pending action и есть `не менять`, поэтому постоянного select нет. Выбор меняет только browser pending state, добавляет короткий badge `Ост.`/`Возоб.` с cancel и никогда не вызывает WB до explicit owner confirmation. Фактическая иконка не меняется на optimistic state до confirmed apply. Campaign state и numeric bid остаются разными typed payload fields; action text не попадает в numeric input.

Selection ownership детерминирован: explicit checkbox, additive `Выбрать все доступные` и auto-owned user change хранятся раздельно. Successful manual bid save добавляет auto reason только для exact target; pending stop/resume добавляет отдельный state reason сразу. Очистка/cancel последнего user-authored reason снимает только auto-owned selection; explicit checkbox и select-all ownership сохраняются. Manual uncheck снимает все текущие ownership reasons строки, но новый последующий user change снова auto-checks. `Выбрать все доступные` только добавляет подготовленные ручные bid/state changes и никогда не создаёт цели и не работает как toggle/reset.

Selected count и точная причина disabled Apply остаются рядом с действием. Кнопка и modal называются `Применить изменения`. Exact preview строится только из итогового selected set: отдельно маркирует `Ручная ставка` и `Ожидающее действие`, показывает selected SKU, atomic bid current→target с единицами, exact campaign state transitions, число повышений/понижений, исключённые targets и предупреждения. Modal прямо сообщает, что только после отдельного подтверждения exact изменения действительно будут отправлены в WB.

`sheet_vitrina_v1_inventory_balance_apply_jobs` и `..._apply_items` — durable server-backed state machine с worker token/lease. Browser только создаёт job и читает status; закрытие/reload страницы не останавливает работу. Item phases различают `pending/preflighting/ready/submitting/submitted/verifying/delayed` и terminal `succeeded/failed/skipped/ambiguous`. Worker после restart не повторяет target на границе возможного submit: он переводит его в query-only reconciliation. Stalled worker виден явно и может быть возобновлён оператором без повторной отправки ambiguous target.

Каждый job сохраняет canonical apply manifest и digest: calculation/mode плюс sorted exact targets. Bid item хранит current/calculated/manual/final bid и override timestamp; campaign-state item — exact-one `nmID + advert_id`, current raw status/canonical state, requested canonical state, typed action и placement/payment evidence. Same exact manifest дедуплицируется; manual override или иное изменение target/state создаёт новый manifest и новый связанный job, даже при той же selection. Это расширение существующих `sheet_vitrina_v1_inventory_balance_apply_jobs/items`, не новая очередь/ledger/allocator.

Production runtime принимает `mode=live_wb` только при canonical seller binding, включённом guarded Ads writer и доступном `InternalWriterRegistry`. До первого write выполняется fresh batched advert read и exact `advert_id → ровно один nmID` identity gate. Bid item отдельно проверяет current-bid CAS и WB minimum; state item отдельно проверяет raw status/canonical state CAS, payment и placement evidence и разрешимость official transition. Ноль/несколько nmID, stale current, неизвестный placement/status/payment model, invalid/non-positive bid, WB bounds violation, unavailable minimum, bid ниже minimum или недоступное state action fail closed до submit. Рубли переводятся в integer minor units ровно один раз в immutable apply manifest.

Seller-side пороги `MAX_ABSOLUTE_INCREASE_RUB`, `MAX_PERCENT_INCREASE` и `MAX_BID_RUB` не являются hard blocker только для typed `inventory_balance_owner_confirmed_bid_thresholds/v1`: preview показывает current→target, delta RUB/% и соответствующий порог, а immutable owner-confirmed exact manifest разрешает прямой submit целевого значения без staircase. Warning policy, thresholds, exact warnings и confirmation scope входят в manifest/digest и Registry-linked provenance. Без `confirmed=true` job не создаётся и PATCH невозможен. Standalone Ads/SKU preview/commit сохраняет прежний strict threshold guard. Identity/CAS/WB minimum/status/payment/placement/staleness/digest/rate-limit/canary/readback guards никогда не переводятся в warnings.

Первый exact target любого типа отправляется как canary и обязан получить matching readback. Затем remaining bid targets отправляются configurable micro-batches (default 10, WB envelope не более 50), а campaign-state items — по одному official start/pause request; каждый target имеет отдельный durable item/Registry change item. Один delayed query-only readback проверяет submitted group после documented WB sync window. Explicit `429` до submit fail closed для state action и сохраняет bounded existing bid retry policy; transport/5xx uncertainty после вызова любого writer никогда не вызывает blind resubmit и разрешается только query-only readback каждого target. Job может завершиться полностью, частично или ошибочно.

Новые manual decision IDs имеют префиксы `ibmd_` (bid) и `ibms_` (state) и typed contracts `sku_inventory_balance_manual_*_decision/v1`. Совместимое имя колонки `recommendation_item_id` является технической ссылкой на решение, не тегом алгоритма. Registry provenance новых IDs содержит `decision_source=manual_operator` и `automatic_recommendation_status=not_generated`; прежние durable jobs/IDs не переименовываются и не получают ложную новую provenance. Связь с исходным `calculation_id` остаётся неизменной после появления следующих расчётов, а stock/pace context берётся из него.

Перед каждым WB writer call для каждого target Registry durable создаёт operation/change item/attempt=`created` с `calculation_id + apply_job_id + recommendation_item_id`. Bid использует canonical `bid_minor`, state transition — campaign target с canonical `campaign_state`. После доказанного submit добавляется `submitted`; только exact matching readback создаёт confirmation/fact/link. Transport uncertainty хранится как `ambiguous`, а совпавший последующий readback разрешает тот же attempt без duplicate fact. Native SKU bid history получает событие только для bid и только после того же matching readback; campaign-state не подделывает native bid history.

Preview modal после подтверждения не исчезает: та же modal переходит `preview → running → terminal result`. UI показывает real animated progress, живые числа `применено / проверяется / ожидает / не применено / требует проверки`, per-target current→target/status и очевидный полный/частичный/нулевой итог. Human-readable ошибка находится в строке, technical identity/detail — только под optional disclosure. Reopen страницы восстанавливает active/latest durable job и polling; после намеренного сворачивания остаётся persistent page indicator/reopen action. Последний apply result также виден compact badge у соответствующей SKU row.

Отдельное действие `Изменить вручную на портале` не является live apply-протоколом и не вызывает WB. Для каждой выбранного exact ручного bid-решения payload содержит стабильный `recommendation_item_id`, typed target `seller/account/nmID/advert_id/placement/bid_minor`, наблюдаемое current и ручной target в копейках. После отдельного подтверждения Registry создаёт только append-only `manual_pending` на 24 часа. Эта manual-pending дорожка остаётся bid-only; live campaign-state action использует доказанный writer lifecycle выше и не создаёт manual pending.

Один exact target имеет максимум один active pending. Новое immutable решение supersede-ит прежнюю через CAS pointer; replay того же `recommendation_item_id` idempotent, а divergent bytes fail closed. `Проверить изменения сейчас` переиспользует существующий authenticated observer manual scan; scheduled двухчасовой observer остаётся без изменений.

Default dry-run adapter остаётся доступен только как internal/test boundary и всегда возвращает `wb_patch_called=false`. Production operator surface использует Balance-owned batch transport поверх того же guarded Ads source; live capability не появляется без Registry binding и canonical seller identity.

# 6. Workbook contract

Download строится из выбранного immutable calculation плюс отдельного текущего override readback. Первая и primary sheet всегда `Решения`. Расширенный workbook также содержит:

- `Расчёт` — stock pace inputs/bounds и formula version;
- `Кампании` — exact identities, CPO и current/calculated/manual/final bids плюс outcome placeholders;
- `Поставки` — все exact milestones: date/quantity, available-before/cumulative-after, source identity, next/subsequent role и ETA evidence/quality;
- `Источники` — operation/calculation identity, source digest, lineage, settings и no-training boundary;
- `История расчётов` — immutable registry chain.

`Решения` содержит отдельные колонки `Следующая поставка`, `Последующая поставка` и `WB источник`. `Расчёт` отдельно показывает raw WB, коэффициент, учтённый WB и evidence mode; `Источники` сохраняет полный per-SKU WB evidence lineage. Aggregate mode в UI/XLSX всегда подписан как aggregate-only без складской раскладки. Перед response workbook повторно открывается через XLSX reader и проверяет primary/campaign/inbound sheets. XLSX остаётся export artifact и не становится calculation/source truth.

# 7. Verification and exclusions

`apps/sku_inventory_balance_smoke.py` проверяет all-fronts opening (`coefficient × WB + FF`), coefficient boundaries `0/1`, complete aggregate-only WB fallback with milestones, fail-closed partial/missing/malformed aggregate evidence, unchanged non-Balance strict stock field, before-arrival constraints, last-exact-supply horizon, no-supply unknown, 7/14-day demand evidence, empirical/fallback ETA contract, inbound dedupe, zero-sales launch boundary, manual-only no-prefill, official FBS strict source/no legacy reads, exact iPhone Air glass exclusions, immutable schema/trigger, separated override, manifest-aware idempotency and registry/workbook provenance readback. Тот же smoke разрывает клиентский socket до response, удерживает тяжёлый worker, доказывает быстрый соседний GET, byte-stable same-key `202`, later operation GET recovery и exact one operation/one calculation.

`apps/sku_inventory_balance_live_apply_smoke.py` проверяет mixed CPC/CPM targets, integer minor-unit contract, one-nmID identity/stale/minimum gates, canary + bid batches, exact one successful submit per target, typed state-only/mixed pause+resume jobs, status/payment/placement CAS, state exact readback, transport ambiguity without resubmit, durable restart recovery, per-target Registry provenance и facts/links без дублей. Отдельные fixtures доказывают: `>100 ₽`, `>50%`, target `>1000 ₽` и исторический `requested_bid_rub exceeds absolute increase threshold` strict-reject-ятся вне Balance policy, проходят direct submit после immutable Balance confirmation, сохраняют warning/provenance и не создают job/WB call без confirmation.

`apps/sku_inventory_balance_browser_smoke.py` проверяет latest auto-load/reopen freshness, initial latest `500 → terminal operation.result → visible rows`, default columns для empty arrays и stale latest/override isolation; реальные layout measurements на `1366px` и narrow `1100px` для symmetric `350..360px` CPC/CPM columns, usable `92px` input/caret, bounded padding/gap/no page overflow и last-position info; отсутствие постоянного `Кампания`, info popover, factual play/pause/neutral controls, menu, pending badge/cancel и zero immediate business request; auto-check после successful manual/state change, manual uncheck/rechange, failed/stale save, ownership-safe cancel/clear, additive select-all и exact manual/state preview/payload; modal preview→running→full/partial/zero/stalled result, reload reconnect, human safety warning и last result; browser-side WB writer call отсутствует.

Explicit exclusion policy `sku_inventory_balance_exclusions_v1` удаляет из calculation rows/UI/XLSX только exact nmID `497413772`, `497415593`, `497416931` с reason `iPhone Air glass is outside inventory-balance scope`; name substring matching запрещён. Policy version, полный configured list и matched rows сохраняются в lineage и `Источники`.

В scope не входят production backfill, deployment-time WB mutation, automatic execution без owner confirmation, terminal campaign stop/delete, campaign creation, prices, budgets/topups, guessed exact campaign portal URL, ML/training, recommender changes, autonomous observation collection и изменение существующей логики `Общее`.
