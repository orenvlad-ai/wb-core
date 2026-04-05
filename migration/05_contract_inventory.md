# Legacy Contract Inventory

Этот inventory собирает контракты, которые видны в reference-репозиториях. Там, где подтверждения не хватает, это отмечено явно.

## 1. `AI_EXPORT`

Подтверждено по `wb-table-audit/apps-script/src/81_bench_ai_export.js`:
- имя листа: `AI_EXPORT`;
- header: `date_key | scope | entity_id | metric | value | valueRef`;
- строится из `DATA`;
- окно экспорта в текущей реализации: последние 120 date-columns;
- exporter пишет scopes: `TOTAL`, `GROUP`, `SKU`;
- текущие шаблоны `valueRef`:
  - `TOTAL|<metric>`
  - `GROUP:<group>|<metric>`
  - `<nmId>|<metric>`

Подтверждено по `wb-ai-research/wb-ai/ingest.py`:
- reader ожидает ровно эти первые шесть колонок;
- текущий ingest принимает только `scope == SKU`;
- upsert key: `(date_key, valueref)`;
- `date_key` парсится как дата;
- `entity_id` ingest-ится как строка.

Риск:
- target migration не должна молча ломать non-SKU semantics экспорта, даже если текущий ingest их пропускает.

## 2. `CONFIG`

Подтверждено по `wb-table-audit/apps-script/src/10_config.js`:
- фактические колонки сейчас: `sku(nmId) | active | comment | group`;
- активный набор SKU фильтруется по boolean-like `active`;
- основной operator key — числовой `nmId`;
- `group` и `comment` — operator-visible attributes.

Подтверждено по `wb-ai-research/wb-ai/sync_registry.py`:
- sync допускает header variants `nmId` / `nm_id`;
- может использоваться optional gate `active` или `enabled`;
- server-side registry хранит `nm_id`, `group_name`, `human_name`.

Inference:
- `comment` сейчас фактически используется как human-readable SKU name в нескольких downstream-контекстах.

## 3. `METRICS`

Подтверждено по `wb-table-audit/apps-script/src/20_metrics.js`:
- обязательные колонки: `metric`, `type`, `scope`, `enabled`;
- сейчас используются optional columns: `source`, `formula`, `label_ru`, `show_in_data`, `ui_parent`, `ui_collapse`, `ui_format`, `agg_scope`;
- в Apps Script validation используются scope values `SKU`, `TOTAL`;
- enabled rows формируют `skuEnabled` и `totalEnabled`;
- дублирующийся `metric` недопустим.

Подтверждено по `wb-ai-research/wb-ai/sync_registry.py` и `wb-ai/sql/metrics_registry_semantics.sql`:
- server registry также моделирует `metric_kind`, `value_unit`, `value_scale`, `missing_policy`, `period_agg`, `ratio_num_key`, `ratio_den_key`;
- check для `value_scale` сейчас допускает `raw`, `percent_0_100`, `ratio_0_1`;
- check для `missing_policy` сейчас допускает `null`, `zero`;
- check для `period_agg` сейчас допускает `sum`, `mean`, `last`, `ratio_of_sums`, `custom`.

Gap:
- ни один текущий источник не доказывает полную canonical `METRICS` schema в production.

## 4. Semantics Для Metric Keys / `nmId` / `date_key`

Подтверждено:
- `nmId` — главный SKU identity во всех контурах Apps Script, AI registry sync, export и snapshot payloads;
- `date_key` в `AI_EXPORT` — это day-granularity ключ в ISO-like формате, производный от date-columns в `DATA`;
- raw snapshot modules используют day-based semantics вроде `snapshot_date`, `snapshot_date_from`, `snapshot_date_to` или `date`;
- часть метрик требует scale transform на переходе между границами.

Конкретный пример:
- `wb-table-audit/apps-script/src/80_plugins_search_analytics_snapshot.js` фиксирует, что `ctr_current` приходит из RAW в шкале `0..100`, а в `DATA` пишется как `0..1` ради percent-format листа.

Следствие:
- одного identity metric-key недостаточно; вместе с контрактом должна ехать и scale semantics.

Gap:
- канонический словарь metric keys нельзя полностью восстановить только из reference-репозиториев, потому что live `METRICS` sheet отсутствует.

## 5. Snapshot API Contracts

Подтверждено в текущем legacy:

### Search Analytics Snapshot

Evidence по producer/consumer:
- `wb-table-audit/apps-script/src/44_raw_search_analytics_snapshot.js`
- `wb-web-bot/bot/fetch_report.py`
- `wb-ai-research/wb-ai/web_sources/client.py`
- `wb-ai-research/RECONCILE_SUMMARY.md`

Наблюдаемый контракт на стороне table-consumer:
- `GET /v1/search-analytics/snapshot`
- ожидаемый payload:
  - `date_from`
  - `date_to`
  - `items[]`
  - каждый item включает `nm_id`, `views_current`, `ctr_current`, `orders_current`, `position_avg`
- `404` означает snapshot not found и не должен ронять запись в sheet.

Inference:
- текущий server endpoint, вероятно, оборачивает browser/web-source capture или близкий server-side acquisition path.

### Sales Funnel Daily Snapshot

Подтверждено по `wb-table-audit/apps-script/src/45_raw_sales_funnel_daily_server.js`:
- `GET /v1/sales-funnel/daily`
- ожидаемый payload:
  - `date`
  - `count`
  - `items[]`
  - каждый item включает `nm_id`, `name`, `vendor_code`, `view_count`, `open_card_count`, `ctr`
- `404` означает snapshot not found и обрабатывается как non-fatal.

### Supplies Snapshot

Подтверждено по `wb-ai-research/wb-ai/api.py`:
- `GET /v1/supplies/snapshot`
- payload включает:
  - top-level `snapshot_date`
  - `count`
  - `items[]`
  - поля item-а, такие как `supply_key`, `supply_id`, `preorder_id`, `status_id`, `status_name`, `nm_id`, `vendor_code`, `barcode`, `quantity`, `accepted_quantity`, `server_updated_at`

## 6. Operator-Visible Outputs

Подтверждено в legacy:
- лист `DATA` как главная operator-facing history surface;
- листы `RAW_*` как transient raw inspection layers;
- `CONFIG` и `METRICS` как operator-managed structure inputs;
- pilot/setup/report tabs в Apps Script, которые читают `DATA` и `CONFIG`;
- `AI_EXPORT` как downstream machine-readable export.

Inference:
- точный минимальный operator-visible surface для target cutover пока не зафиксирован.

## 7. Неподтверждённые Или Частично Подтверждённые Пункты

Явные gaps:
- live production content листа `METRICS`;
- полный authoritative список metric keys;
- существуют ли сегодня потребители `AI_EXPORT`, кроме `wb-ai-research/ingest.py`;
- backed ли `GET /v1/search-analytics/snapshot` от `wb-web-bot`, `wb-ai-research` или другого runtime path в текущем production;
- останутся ли group-level outputs first-class semantics в target-core.
