# Initial Artifacts Plan Блока Web-Source Snapshot

## Зачем Нужен Initial Artifacts Plan

Этот план нужен, чтобы не начать техническую подготовку с лишнего и не утонуть в артефактах раньше времени.

Он предотвращает:
- преждевременное разрастание fixture-набора;
- смешение обязательного минимума с поздними edge-cases;
- старт без базового reference и базового comparison.

## Какие Артефакты Должны Появиться Самыми Первыми

| Артефакт | Категория | Зачем нужен | Почему входит в минимальный стартовый набор |
| --- | --- | --- | --- |
| `normal__<date>__legacy__fixture.json` | legacy | Базовый reference normal-case snapshot | Без него нет исходной точки для parity |
| `normal__<date>__target__fixture.json` | target | Базовый target output для того же сценария | Без него нечего сравнивать с legacy |
| `normal__<date>__legacy-vs-target__comparison.md` | parity | Поле-в-поле сравнение normal-case | Это первый обязательный comparison artifact |
| `not-found__<date>__legacy__fixture.json` | legacy | Reference для failure-case `not found` | `not found` обязан быть отдельным проверяемым режимом |
| `not-found__<date>__target__fixture.json` | target | Target output для того же failure-case | Без него нельзя проверить сохранение failure semantics |
| `not-found__<date>__legacy-vs-target__comparison.md` | parity | Сравнение legacy/target для `not found` | Это второй обязательный comparison artifact |
| `initial__web-source-snapshot__evidence.md` | evidence | Краткое summary, что именно уже доказано по стартовому набору | Нужен единый evidence entrypoint до дальнейшего расширения |

## Что В Этот Стартовый Набор Специально Не Входит

Не входит:
- fixture на все возможные даты;
- расширенный набор edge-cases;
- дополнительные optional fields;
- performance artifacts;
- production observability artifacts;
- test outputs;
- любые артефакты для других migration units.

## Как Понять, Что Стартовый Набор Собран

Стартовый набор собран, если:
- есть normal-case пара `legacy/target`;
- есть `not found` пара `legacy/target`;
- на обе пары есть comparison artifacts;
- есть один общий evidence summary;
- из этих артефактов уже видно, что проверены shape, period semantics, `nm_id` и failure-mode.

## Следующий Шаг После Этого Документа

Следующий шаг:
- создать в `wb-core` короткий plan минимальной skeleton-структуры директорий под будущие artifacts этого блока, без создания самих artifact-файлов и без реализации модуля.
