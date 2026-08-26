---
title: "Модуль: sku_inventory_balance"
doc_id: "WB-CORE-MODULE-53-SKU-INVENTORY-BALANCE"
doc_type: "module"
status: "active"
purpose: "Зафиксировать server-owned подраздел `Управление SKU → Баланс запасов`: immutable расчёты темпа и рекламных целей, раздельные ручные overrides, расширенный XLSX и durable dry-run apply jobs."
scope: "Одна строка на active nmID поверх canonical SKU Management evidence; conservative stock pacing, old CPM/new CPC campaign groups, exact target recommendations and dry-run-only resumable application protocol."
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
related_tables:
  - "sheet_vitrina_v1_inventory_balance_calculations"
  - "sheet_vitrina_v1_inventory_balance_overrides"
  - "sheet_vitrina_v1_inventory_balance_apply_jobs"
  - "sheet_vitrina_v1_inventory_balance_apply_items"
  - "sheet_vitrina_v1_inventory_balance_outcomes"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/sku-management/inventory-balance"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/settings"
  - "POST /v1/sheet-vitrina-v1/sku-management/inventory-balance/calculate"
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
source_of_truth_level: "module_canonical"
update_note: "Formula v2 adds a Balance-only exact aggregate-WB opening fallback with immutable provenance; warehouse/regional incident semantics and live WB boundaries remain unchanged."
---

# 1. Product surface and ownership

`Управление SKU` содержит два first-level подраздела:

- `Общее` — прежний SKU Management surface без изменения его расчётной и write-логики;
- `Баланс запасов` — отдельный calculation/decision surface с одной строкой на active `nmID`.

Оба подраздела используют authorization section `sku_management` и текущую WebCore session. Отдельной identity/role model нет. Row universe, товарная identity, operational stock timeline и campaign identity приходят только из действующих SKU Management и Ads Operator contracts. Browser не создаёт stock, sales или campaign truth.

`sheet_vitrina_v1_user_configs` с key `sku_inventory_balance` хранит calculation settings и table preferences: видимость, порядок, ширины колонок, search/status filters и preset. Обязательные колонки `Выбор` и `Название / nmID` всегда остаются первыми. `localStorage` не является источником настройки.

# 2. Immutable calculation registry

Каждый явный `Новый расчёт` создаёт новый `calculation_id`. Строка в `sheet_vitrina_v1_inventory_balance_calculations` содержит versioned formula, source digest, settings, полный payload, actor/time и `previous_calculation_id`. SQLite triggers запрещают update/delete calculation rows. Новый source snapshot никогда не переписывает прежнее решение. Bounded registry endpoint и экранный реестр показывают последние расчёты, lineage, versioned protocols и связанные apply jobs/status/manifest digest.

Расчёт возвращает lineage к предыдущему calculation и future observation fields. Наблюдения имеют отдельную append-only таблицу `sheet_vitrina_v1_inventory_balance_outcomes`; исходный calculation и применённые значения не переписываются результатом наблюдения.

Автоматическое ML, обучение на outcomes, causal lift и automatic target tuning отсутствуют. Поле `automatic_ml_or_training=false` является частью публичного contract.

# 3. Conservative stock pacing

Формула `sku_inventory_balance_conservative_pace_v2` использует отдельный SKU Management evidence contract. Текущий opening по всем фронтам равен `stock_ff + wb_confidence_coefficient × stock_wb`; server-owned user setting имеет default `0.5`, диапазон `0..1`, не nullable и применяется только к WB (`0` полностью исключает WB, `1` учитывает его полностью). Каждый immutable calculation сохраняет выбранный коэффициент в settings и в per-SKU WB evidence lineage. Оба stock-поля должны присутствовать явно: missing не реконструируется из forecast timeline и не превращается в zero. Текущий FF/FBS входит в opening сейчас и никогда не показывается как будущая поставка.

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

Calculated target bid использует bounded factor `0.25..2.0`, подтверждённый WB minimum как нижнюю границу и relative group CPO evidence. При росте темпа изменение направляется в eligible group с меньшим CPO; при снижении сначала режется group с большим CPO. Остальные группы удерживаются. Если сравнимая CPO statistics неполна, multi-group target fail closed в консервативный hold с `recommendation_quality=insufficient_stats`. Single eligible group может масштабироваться с явным `single_group_no_relative_comparison`. Поля `current_bid_rub`, `calculated_target_bid_rub` и `final_target_bid_rub` существуют одновременно.

Inline manual override записывается только в `sheet_vitrina_v1_inventory_balance_overrides`. Immutable calculated target сохраняется; final target выбирает manual value при его наличии. Очистка manual value возвращает final target к calculated value. При unknown inventory pacing manual override недоступен и не может превратить строку без supply evidence в actionable. Apply selection принимает только exact valid target с current/final value и реальным изменением.

# 5. Selection, confirmation and durable apply

Оператор выбирает строки либо `Выбрать все доступные`. Одна SKU-строка разворачивается в список exact campaign targets. До job browser показывает confirmation summary: число SKU/targets, повышения/понижения и явную границу `dry-run`.

`sheet_vitrina_v1_inventory_balance_apply_jobs` и `..._apply_items` — durable server-backed state machine. Item states: `pending → running → succeeded|failed`; stale `running` после process interruption terminalize-ится как `ambiguous` и не повторяется blind retry. Job progress, animated progress bar/spinner, final success/error state и aggregate per-SKU terminal state читаются с сервера, поэтому reload не теряет состояние.

Каждый job сохраняет canonical apply manifest и digest: calculation/mode плюс sorted exact target identity, current/calculated/manual/final bid и override timestamp. Same exact manifest дедуплицируется; manual override или иное изменение target создаёт новый manifest и новый связанный job, даже при той же selection.

Current HTTP contract принимает только `mode=dry_run`. Default adapter возвращает `wb_patch_called=false` и не вызывает WB source. `mode=live_wb` возвращает fail-closed 403.

Код содержит отдельную future live adapter boundary, но она не подключена к runtime. Даже прямой вызов запрещён default-false capability provider. Если отдельная будущая задача откроет capability, adapter обязан переиспользовать действующий exact single-target `SkuManagementBlock.preview_bid → commit_bid → matching readback`; отдельный bulk PATCH, обход preview, inferred success и blind retry запрещены.

# 6. Workbook contract

Download строится из выбранного immutable calculation плюс отдельного текущего override readback. Первая и primary sheet всегда `Решения`. Расширенный workbook также содержит:

- `Расчёт` — stock pace inputs/bounds и formula version;
- `Кампании` — exact identities, CPO и current/calculated/manual/final bids плюс outcome placeholders;
- `Поставки` — все exact milestones: date/quantity, available-before/cumulative-after, source identity, next/subsequent role и ETA evidence/quality;
- `Источники` — calculation/source digest, lineage, settings и no-training boundary;
- `История расчётов` — immutable registry chain.

`Решения` содержит отдельные колонки `Следующая поставка`, `Последующая поставка` и `WB источник`. `Расчёт` отдельно показывает raw WB, коэффициент, учтённый WB и evidence mode; `Источники` сохраняет полный per-SKU WB evidence lineage. Aggregate mode в UI/XLSX всегда подписан как aggregate-only без складской раскладки. Перед response workbook повторно открывается через XLSX reader и проверяет primary/campaign/inbound sheets. XLSX остаётся export artifact и не становится calculation/source truth.

# 7. Verification and exclusions

`apps/sku_inventory_balance_smoke.py` проверяет all-fronts opening (`coefficient × WB + FF`), coefficient boundaries `0/1`, complete aggregate-only WB fallback with milestones, fail-closed partial/missing/malformed aggregate evidence, unchanged non-Balance strict stock field, before-arrival constraints, last-exact-supply horizon, no-supply unknown, 7/14-day demand evidence, empirical/fallback ETA contract, inbound dedupe, zero-sales launch boundary, CPO routing, exact iPhone Air glass exclusions, immutable schema/trigger, separated override, manifest-aware idempotency, registry/workbook provenance readback, durable progress and disabled live adapter without preview/commit calls.

`apps/sku_inventory_balance_browser_smoke.py` проверяет subtabs, presets, server-owned settings, columns, grouped CPC/CPM recommendations, inline override, select-all, confirmation, server-backed terminal progress and отсутствие browser `PATCH`.

Explicit exclusion policy `sku_inventory_balance_exclusions_v1` удаляет из calculation rows/UI/XLSX только exact nmID `497413772`, `497415593`, `497416931` с reason `iPhone Air glass is outside inventory-balance scope`; name substring matching запрещён. Policy version, полный configured list и matched rows сохраняются в lineage и `Источники`.

В scope не входят production backfill, production-data mutation, live WB write enablement, automatic campaign management, ML/training, autonomous observation collection и изменение существующей логики `Общее`.
