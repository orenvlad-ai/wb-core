# Контракт Загрузки V2-Реестров В Серверный Контур

`registry_upload_contract_block` фиксирует source-of-truth контракт для загрузки V2-реестров из новой таблицы в серверный контур `wb-core`.

Документ опирается на уже зафиксированную registry-линейку:
- `migration/75_registry_v2_minimal_schema.md`
- `migration/76_metric_runtime_registry_minimal_schema.md`
- `migration/77_registry_implementation_path.md`
- `migration/78_pilot_registry_bundle.md`

## 1. Назначение Upload Flow

Отдельный `registry upload path` нужен затем, чтобы:
- отделить редактирование реестров от их серверной активации;
- дать один контролируемый вход для валидации, версионирования и audit-trace;
- не смешивать table-side editing и server-side runtime consumption в один неявный шаг.

На переходном этапе таблица остаётся только временным `editor-layer`, а не source of truth, потому что:
- табличный слой нужен для удобного редактирования операторских V2-реестров;
- server-side контур должен владеть активной версией registry bundle;
- только сервер может дать одну reviewable и versioned точку правды для downstream-consumers.

Сервер должен принимать bundle целиком, а не три разрозненных потока, потому что:
- `CONFIG_V2`, `METRICS_V2` и `FORMULAS_V2` образуют один связанный registry-snapshot;
- ссылки `METRICS_V2.calc_ref` нельзя честно валидировать вне общего контекста bundle;
- активация должна быть атомарной: либо включается вся согласованная версия, либо не включается ничего.

## 2. Какие Листы Участвуют

В upload flow участвуют только три служебных листа:
- `CONFIG_V2`:
  - временный табличный редактор SKU/display registry;
  - задаёт `nm_id`, `enabled`, `display_name`, `group`, `display_order`.
- `METRICS_V2`:
  - временный табличный редактор display-registry метрик;
  - задаёт `metric_key`, `scope`, `label_ru`, `calc_type`, `calc_ref`, `format`, `section`, `display_order`.
- `FORMULAS_V2`:
  - временный табличный редактор словаря формул;
  - задаёт `formula_id`, `expression`, `description`.

Никакие другие листы не считаются частью V1 upload contract.

## 3. Канонический Upload Bundle V1

Для V1 фиксируется один канонический формат загрузки:

```json
{
  "bundle_version": "registry_v2_2026-04-13T12:00:00Z",
  "uploaded_at": "2026-04-13T12:00:00Z",
  "config_v2": [],
  "metrics_v2": [],
  "formulas_v2": []
}
```

### Обязательные Поля Верхнего Уровня

- `bundle_version`
  - непустой строковый идентификатор загружаемой версии;
  - в пределах server-side registry storage должен быть уникальным.
- `uploaded_at`
  - timestamp загрузочного snapshot-а в ISO 8601 UTC;
  - фиксирует момент, к которому относится bundle.
- `config_v2`
  - массив нормализованных записей `CONFIG_V2`.
- `metrics_v2`
  - массив нормализованных записей `METRICS_V2`.
- `formulas_v2`
  - массив нормализованных записей `FORMULAS_V2`.

Для V1 верхний уровень сознательно остаётся минимальным.

### Нормализация `calc_type`

Внутри upload contract канонический словарь `calc_type` фиксируется как:
- `metric`
- `formula`
- `ratio`

Значение `projection` не является частью канонического upload contract V1 и в серверный upload path не принимается.

### Правило Резолва `calc_ref`

- если `calc_type = metric`, `calc_ref` должен резолвиться в server-side runtime metric key;
- если `calc_type = ratio`, `calc_ref` должен резолвиться в server-side runtime metric key с `metric_kind = ratio`;
- если `calc_type = formula`, `calc_ref` должен совпадать с `formulas_v2.formula_id` внутри того же upload bundle.

## 4. Что Должно Валидироваться На Сервере

Минимально сервер обязан валидировать:
- базовую структурную валидность bundle:
  - присутствуют все top-level поля;
  - поля имеют ожидаемые типы;
  - `config_v2`, `metrics_v2`, `formulas_v2` являются массивами объектов;
  - `bundle_version` непустой и ещё не занят;
  - `uploaded_at` имеет корректный timestamp-формат.
- уникальность `nm_id` внутри `config_v2`;
- уникальность `display_order` внутри `config_v2`;
- уникальность `metric_key` внутри `metrics_v2`;
- уникальность `display_order` внутри `metrics_v2`;
- корректность `scope`:
  - допустимы только `SKU`, `GROUP`, `TOTAL`;
- корректность `calc_type`:
  - допустимы только `metric`, `formula`, `ratio`;
- резолв `calc_ref`:
  - для `formula` ссылка должна находить запись в `formulas_v2`;
  - для `metric` и `ratio` ссылка должна находить допустимый server-side runtime metric key;
- существование `formula_id`:
  - у каждой записи `formulas_v2` должен быть непустой `formula_id`;
  - `formula_id` внутри `formulas_v2` не должны дублироваться;
  - все формульные ссылки из `metrics_v2` должны указывать на существующий `formula_id`.

Этого минимума достаточно, чтобы сервер не активировал структурно битый или внутренне противоречивый bundle.

## 5. Что Сервер Должен Делать После Приёма

После поступления upload bundle сервер должен:
1. принять bundle как один атомарный вход;
2. провалидировать его целиком;
3. при успешной валидации сохранить bundle как новую immutable-версию registry bundle;
4. пометить эту версию как `active/current`;
5. вернуть нормализованный результат загрузки.

В рамках этого контракта не фиксируется конкретная БД, таблица хранения, endpoint или runtime orchestration.

## 6. Какой Ответ Должен Возвращаться

Минимальный upload result фиксируется так:

```json
{
  "status": "accepted",
  "bundle_version": "registry_v2_2026-04-13T12:00:00Z",
  "accepted_counts": {
    "config_v2": 5,
    "metrics_v2": 12,
    "formulas_v2": 2
  },
  "validation_errors": [],
  "activated_at": "2026-04-13T12:00:02Z"
}
```

### Минимальный Смысл Полей

- `status`
  - минимум: `accepted` или `rejected`.
- `bundle_version`
  - версия, которую сервер принял или отклонил.
- `accepted_counts`
  - количество реально принятых записей по каждому V2-реестру.
- `validation_errors`
  - список ошибок валидации;
  - при success может быть пустым массивом.
- `activated_at`
  - timestamp активации принятой версии;
  - при rejected-ответе может быть `null` или отсутствовать, если это будет так отдельно зафиксировано в runtime implementation.

## 7. Что Считается Каноникой

Каноника фиксируется жёстко:
- до загрузки таблица является только `editor-layer`;
- после успешной загрузки каноникой становится server-side registry bundle, который сохранён как новая версия и помечен `active/current`;
- таблица сама по себе не считается production truth;
- сервер и таблица не могут считаться двумя равноправными источниками истины одновременно.

Практическое следствие:
- изменение в `CONFIG_V2`, `METRICS_V2` или `FORMULAS_V2` не меняет server truth, пока не прошло через успешный upload и activation;
- после activation downstream-контур читает только server-side bundle, а не листы напрямую.

## 8. Что Сознательно Не Входит

В этот контракт сознательно не входят:
- отдельный UI вне таблицы;
- двусторонняя синхронизация;
- merge конфликтов между сервером и таблицей;
- partial upload;
- live DB schema;
- API реализация;
- Apps Script реализация кнопки.

## 9. Следующий Практический Шаг

Следующий bounded step после этого документа:
- добавить один artifact-backed пример `registry upload bundle` и один локальный smoke-validator, который проверяет shape bundle, uniqueness и `calc_ref` resolution без endpoint, без БД и без UI.

Это уже ведёт к первой реализации `registry upload path`, но не требует прыжка сразу в большой runtime.
