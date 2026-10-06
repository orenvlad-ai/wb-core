# Module 26 — Web Vitrina end to end

## Назначение

Модуль связывает server-owned источники и подготовленные снимки с основной Web
Vitrina. Архивный обратный путь записи в Google Sheets не является рабочим
контуром.

## Поток

```text
источники → нормализация → registry/runtime storage → ready snapshot
→ read API → Web Vitrina и пользовательские выгрузки
```

## Инварианты

- интерфейс читает один опубликованный ready snapshot;
- кандидат строится до переключения указателя на него;
- неготовый кандидат не заменяет last-good;
- дата и качество показателя сохраняются до ячейки;
- `missing`, `partial`, `stale`, `unconfirmed` и точный ноль различаются;
- ручное обновление не создаёт отдельную версию бизнес-логики;
- старый Google Sheets write bridge не используется как fallback.

## Кодовые границы

- `packages/application/sheet_vitrina_v1_*` — сборка и публикация;
- `packages/contracts/sheet_vitrina_v1_*` — форматы;
- `apps/registry_upload_http_entrypoint*` — server entrypoint и read API;
- `packages/adapters/templates/sheet_vitrina_v1_operator.html` — основной UI.

Предметные строки и формулы описаны в документах конкретных модулей. Этот
документ не дублирует их.

## Сбор источников и локальный расчёт

`SheetVitrinaCycleSources(block)` предоставляет `collect_sources(...)` и
`derive_collected(handle)` для того же instance канонического блока. Adapter
использует его существующие `_collect_only`/`_collection` швы; обычный
`SheetVitrinaV1LivePlanBlock.build_plan(...)` и весь pinned evaluator сохраняются
побайтно прежними. Adapter пока не подключён к расписанию или HTTP-пути.

Сбор принимает те же аргументы, один раз сохраняет выбранные источники и
метрики, выполняет прежние внешние чтения и принятие исходных снимков. Он также
сохраняет существующие эффекты mature buyout, Proxy V4 rollover, web sync и
кэшей. Это не read-only операция. Сбор не строит строки Data Vitrina и не
публикует ready snapshot.

Возвращаемый `CollectedLivePlanSources` — непрозрачный объект одного процесса,
связанный с тем же экземпляром блока, runtime и путями хранилищ. В нём нет
доступных изменяемых исходных результатов. Приватная weak-key привязка хранит
запрос и копии принятых результатов только пока вызывающая сторона хранит
handle; отдельный close или постоянный реестр не требуется.

`derive_collected(handle)` не принимает новые параметры запроса, не вызывает
загрузчики источников и не повторяет эффекты сбора. Повторный локальный расчёт
допустим: каждый раз он читает свежие складские/учётные данные и ready pins.
Смена runtime, authority, bundle, бизнес-даты, состава SKU или выбранной области,
либо изменение уже потреблённого источника отклоняет расчёт. Копии исходных
результатов не меняются от правки возвращённого плана.

Существующий предел трёх локальных попыток при смене material/history/ready и
ошибки публикации сохраняются. План несёт `publication_inputs`,
`local_derive_attempt` и `local_derive_expected_ready_fingerprint`; перед обычной
публикацией по-прежнему нужен `bind_local_derive_publication`. Handle не является
форматом сериализации, готовым планом или объектом для передачи другому блоку.
Сроки freshness и атомарность FBS book + ready остаются прежними.

Offline-проверки контракта и publication races:
`python3 apps/sheet_vitrina_v1_local_derive_smoke.py`.

## Неактивный последовательный цикл

`RegistryUploadHttpEntrypoint._start_sheet_cycle_job` — внутренний шов того же
HTTP instance. Он не подключён к HTTP route, CLI или расписанию. Текущие сроки
freshness и обычные действия сохраняются. До активации нужны отдельные shared
heavy admission для конфликтующих producers/backup и утверждённый schedule
profile; локальный single-flight цикла не доказывает общую сериализацию.

Фиксированный порядок: полный прежний `auto_daily collect_sources` без selectors;
канонические daily/weekly attempts; новый полный официальный FBS generation;
один owned warehouse handler с существующим journal, economics и weekly cost
tail; source-free visible14 daily repair; derive из того же handle и обычный
publication-only tail; reviewed rolling14 history. Повторный полный API сбор,
warehouse apply и weekly cost tail не выполняются. Last-good после неудачного
нового FBS attempt не считается новым generation. Допустимые partial/retained,
archive-only и unsupported temporal roles отражаются как degraded proofs;
неподтверждённые обязательные operands останавливают цикл.

Canonical daily proofs сохраняют исходный status, отдельно projection status и
наличие attempt. `loaded_preliminary` — `accepted_provisional`; `waiting` без
принятого raw — `official_waiting`. Failed latest attempt/backoff с точным ранее
принятым dated batch — `accepted_retained_after_failed_attempt`, с batch ID/hash;
ошибка без такого operand останавливает цикл. Readback связывается с тем же
pointer/immutable batch (date/hash/count/terminal204) и admitted projection,
поэтому одинаковый row count не разрешает подменить версию. Split raw store и
обычный canonical backlog вне visible14 сохраняются. Raw payloads в proof нет.

В `sheet-vitrina-cycles` сохраняются малые receipts: этап до effects, IDs,
digests, версии, предупреждения и коды ошибок. Raw payloads и handle не
сериализуются. Один request key или UTC slot возвращает тот же receipt; смена
контекста отклоняется. Потерянный owner даёт `interrupted`/uncertain, чтение
ничего не исправляет и не повторяет source effects. Новая попытка с новым slot
не является восстановлением предыдущей. Linux process identity и существующий
CPython no-start predicate определяют startup proof: при возможном native spawn
сохраняются thread reference, lease, API marker и single-flight до worker finally.

SH business admission удерживается весь фактический worker lifetime, включая
ошибку. История освобождает только свой точный API marker из admission probe;
сам marker остаётся видим другим builders. Остальные API jobs, systemd units,
daily/warehouse locks, storage/formula guards, finished-builder и candidate
single-flight сохраняются. Будущий service launcher должен завершать dispatch
после durable acceptance: service, ожидающий HTTP job до конца, блокирует history
своего же цикла. Здесь нет общего `ignore_units`.

Rolling14 выполняется в одной ограниченной порции. Pending/deadline, смена final
vector/ready receipt или dirty window означают неуспех; автоматического resume
нет. Runtime contract привязывает formula epoch к хешам канонического кода,
включая `live_plan`. Adapter содержит только короткую orchestration fresh pins/
три CAS попытки; формулы и source execution остаются в pinned evaluator. Его
байты, formula epoch, contract и archive identity не меняются. Точный guard
сохраняется.

Offline-проверка порядка, durable faults, startup cancellation, source policy,
реального FBS generation/warehouse journal, source-free repair и exact publication:
`python3 apps/sheet_vitrina_v1_cycle_smoke.py`.
