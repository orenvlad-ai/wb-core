# Sheet Vitrina V1 MVP End-to-End

## 1. Что Это Такое

`sheet_vitrina_v1_mvp_end_to_end_block` — следующий bounded implementation step поверх уже смёрженного `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`.

Этот шаг не строит новый server-side контур, а впервые замыкает уже существующую линию в один операторский MVP-flow:
- `prepare` поднимает не пилотный мини-набор, а expanded MVP-safe seed;
- `upload` продолжает писать в уже существующий HTTP entrypoint;
- `load` возвращает живые server-side данные обратно в `DATA_VITRINA`.

## 2. Что Именно Покрывает Блок

Блок покрывает один bounded end-to-end MVP:
- materialize-ить `CONFIG / METRICS / FORMULAS` уже заполненным MVP-safe compact seed;
- сохранить service/status block и не сломать existing upload trigger;
- использовать уже существующий `POST /v1/registry-upload/bundle`;
- добавить второй operator-facing trigger `Загрузить таблицу`;
- materialize-ить `DATA_VITRINA` и `STATUS` из живого server-side contour через новый lightweight plan endpoint.

## 3. MVP-Safe Расширение Seed

Expanded compact v3 seed этого шага фиксируется так:
- `config_v2 = 33`
- `metrics_v2 = 7`
- `formulas_v2 = 7`

Bounded допущение шага:
- в MVP materialize-ится не full legacy dump;
- в seed остаётся только тот объём registry rows и metric keys, который current upload/runtime/readback contour реально переваривает без server-side redesign;
- проблемный хвост legacy-модели не блокирует весь MVP и остаётся явно вне scope.

## 4. Readback Семантика

Первый reverse-load path bounded шага фиксируется так:
- sheet-side trigger читает `CONFIG!I2` как base upload URL;
- от него derives `GET /v1/sheet-vitrina-v1/plan`;
- live HTTP entrypoint делегирует сбор плана в существующий application-layer;
- application-layer читает current registry truth из `RegistryUploadDbBackedRuntime`;
- live data подтягиваются из уже materialized public source blocks:
  - `web_source_snapshot_block`
  - `seller_funnel_snapshot_block`
- итоговый план пишется через existing sheet write bridge в `DATA_VITRINA` и `STATUS`.

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
- expanded MVP-safe seed;
- второй operator-facing trigger `Загрузить таблицу`;
- первый живой обратный контур из server-side truth в `DATA_VITRINA`;
- end-to-end smoke `prepare -> upload -> load`.

## 6. Что Остаётся После Этого Блока

После bounded MVP всё ещё остаются вне scope:
- full legacy parity по всем metric sections и registry rows;
- stable hosted runtime URL вместо временного/локального live routing;
- deploy/auth-hardening;
- daily orchestration;
- большой UI/UX redesign таблицы;
- production storage binding beyond current bounded runtime contour.
