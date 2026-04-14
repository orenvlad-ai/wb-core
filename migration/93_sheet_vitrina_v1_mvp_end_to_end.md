# Sheet Vitrina V1 MVP End-to-End

## 1. Что Это Такое

`sheet_vitrina_v1_mvp_end_to_end_block` — следующий bounded implementation step поверх уже смёрженного `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`.

Этот шаг не строит новый server-side контур, а замыкает уже существующую линию в один операторский MVP-flow:
- `prepare` поднимает uploaded compact seed;
- `upload` продолжает писать в уже существующий HTTP entrypoint;
- explicit `refresh` materialize-ит ready snapshot в repo-owned runtime;
- `load` читает только готовый server-side snapshot обратно в `DATA_VITRINA`.

## 2. Что Именно Покрывает Блок

Блок покрывает один bounded end-to-end MVP:
- materialize-ить `CONFIG / METRICS / FORMULAS` уже заполненным uploaded compact seed;
- сохранить service/status block и не сломать existing upload trigger;
- использовать уже существующий `POST /v1/registry-upload/bundle`;
- добавить второй operator-facing trigger `Загрузить таблицу`;
- materialize-ить repo-owned ready snapshot для `DATA_VITRINA` и `STATUS`;
- разделить heavy refresh и cheap read:
  - `POST /v1/sheet-vitrina-v1/refresh`
  - `GET /v1/sheet-vitrina-v1/plan`

## 3. Expanded Operator Seed

Current main-confirmed operator seed этого шага фиксируется так:
- `config_v2 = 33`
- `metrics_v2 = 102`
- `formulas_v2 = 7`
- `enabled + show_in_data = 95`
- server-side plan = `95` metric keys / `1631` flat source rows при одном дне
- operator-facing `DATA_VITRINA` = server-driven `date_matrix` `1698` rendered rows / `95` metric keys / `34` blocks при одном дне

Bounded допущение шага:
- в seed materialize-ится не full legacy dump;
- sheet-side `METRICS` поднимает полный uploaded compact dictionary для upload/runtime;
- server-side current truth и ready snapshot остаются authoritative и держат все `95` enabled+show_in_data metrics;
- `DATA_VITRINA` не делает локальный subset/fallback и использует incoming full row dump только как source для thin `date_matrix` presentation;
- metrics без live HTTP adapters остаются явным server-side/status surface, а не превращаются в local sheet fallback.

## 4. Readback Семантика

Первый reverse-load path bounded шага фиксируется так:
- sheet-side trigger читает `CONFIG!I2` как base upload URL;
- от него derives `GET /v1/sheet-vitrina-v1/plan`;
- `GET /v1/sheet-vitrina-v1/plan` читает только готовый ready snapshot для текущего `bundle_version` из `RegistryUploadDbBackedRuntime`;
- если ready snapshot для current bundle / requested `as_of_date` отсутствует, read path возвращает явную ошибку `ready snapshot missing` и не делает live fetch;
- heavy build вынесен в отдельный explicit action `POST /v1/sheet-vitrina-v1/refresh`;
- `POST /v1/sheet-vitrina-v1/refresh` читает current registry truth из `RegistryUploadDbBackedRuntime`;
- heavy refresh подтягивает live data из already materialized public source blocks:
  - `web_source_snapshot_block`
  - `seller_funnel_snapshot_block`
  - `sales_funnel_history_block`
  - `prices_snapshot_block`
  - `sf_period_block`
  - `spp_block`
  - `ads_bids_block`
  - `stocks_block`
  - `ads_compact_block`
  - `fin_report_daily_block`
- refresh сохраняет полученный `SheetVitrinaV1Envelope` как persisted ready snapshot в repo-owned SQLite runtime table `sheet_vitrina_v1_ready_snapshots`;
- cheap read возвращает этот уже сохранённый plan;
- итоговый плоский plan по-прежнему пишется через existing sheet write bridge в `DATA_VITRINA` и `STATUS`;
- sheet-side bridge перестраивает incoming `DATA_VITRINA` в operator-facing `date_matrix` без локального subset path;
- presentation pass только форматирует уже materialized matrix rows и не меняет source truth.

Явные решения этого шага:
- `openCount` и `open_card_count` не объединяются;
- uploaded `total_*` / `avg_*` rows сохраняются;
- uploaded `section` dictionary остаётся authoritative;
- `CONFIG!H:I` service block сохраняется;
- `promo_by_price` и `cogs_by_group` пока дают blocked status и blank values, потому что live HTTP adapters ещё не materialized.

Этот шаг не утверждает:
- full legacy parity 1:1;
- official-api coverage для всех historical metrics;
- deploy, auth-hardening и stable hosted runtime URL.

## 5. Чем Это Отличается От Предыдущих Шагов

`sheet_vitrina_v1_registry_seed_v3_bootstrap_block` доказывал:
- compact v3 bootstrap в operator sheets;
- сохранение control block;
- совместимость с existing upload path.

`sheet_vitrina_v1_mvp_end_to_end_block` добавляет:
- uploaded compact seed;
- второй operator-facing trigger `Загрузить таблицу`;
- explicit server-side refresh action для materialized ready snapshot;
- cheap read path из ready snapshot обратно в `DATA_VITRINA`;
- thin presentation pass поверх existing server contour с history-right `date_matrix`, без локального subset path;
- end-to-end smoke `prepare -> upload -> refresh -> load`.

## 6. Что Остаётся После Этого Блока

После bounded MVP всё ещё остаются вне scope:
- full legacy parity по всем metric sections и registry rows;
- numeric live fill для promo/cogs-backed metrics до появления HTTP adapters;
- stable hosted runtime URL вместо временного/локального live routing;
- deploy/auth-hardening;
- daily orchestration;
- большой UI/UX redesign таблицы;
- production storage binding beyond current bounded runtime contour.
