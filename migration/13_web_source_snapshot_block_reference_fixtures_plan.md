# Reference Fixtures Plan Блока Web-Source Snapshot

## Зачем Нужен Reference-Fixtures Plan

Этот план нужен, чтобы до начала реализации было ясно, какие эталонные примеры данных мы обязаны собрать первыми.

Он должен дать:
- минимальный набор reference fixtures для старта;
- понятную опору для parity и evidence checks;
- защиту от реализации "вслепую".

## Какие Типы Reference Fixtures Нужны

Минимально нужны:
- нормальный успешный snapshot;
- snapshot на другой дате;
- пример `not found` или пустого результата;
- пример с несколькими `items`;
- пример с ключевыми числовыми полями;
- пример, пригодный для downstream-проверки shape.

## Какие Свойства У Каждого Fixture Должны Быть Зафиксированы

Для каждого fixture должны быть зафиксированы:
- дата или `date_from/date_to`;
- ожидаемый top-level shape;
- ожидаемый item-level shape;
- ключевые поля;
- ожидаемый смысл snapshot-а;
- зачем этот fixture нужен для проверки.

Минимально значимые поля для фиксации:
- `date_from` / `date_to` или `snapshot_date`;
- `items[]`;
- `nm_id`;
- `views_current`;
- `ctr_current`;
- `orders_current`;
- `position_avg`;
- режим `not found`, если fixture относится к failure-case.

## Какие Fixtures Обязательны До Начала Реализации

До начала реализации обязательны:
- один normal-case fixture;
- один fixture на другой дате или другом period slice;
- один fixture с несколькими `items`;
- один fixture или sample для `not found`;
- один fixture, пригодный для downstream shape-check.

Позже можно добавить:
- дополнительные date-cases;
- дополнительные edge-cases по числовым полям;
- более широкий coverage по optional fields.

## Что Не Входит В Этот План

Не входит:
- создание самих fixture-файлов;
- сбор данных из сети;
- реализация fixture loader;
- реализация модуля;
- performance и hardening scenarios;
- полный production fixture catalog.

## Следующий Шаг После Этого Документа

Следующий шаг:
- создать в `wb-core` короткий skeleton-план структуры каталогов и имён файлов для будущих reference fixtures и parity artifacts этого блока, без создания самих fixtures.
