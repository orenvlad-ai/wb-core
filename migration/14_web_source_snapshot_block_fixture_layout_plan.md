# Fixture Layout Plan Блока Web-Source Snapshot

## Зачем Нужен Layout Plan

Этот план нужен, чтобы заранее отсечь хаос в fixtures, parity artifacts и evidence.

Он предотвращает:
- смешение legacy и target данных;
- потерю привязки к сценарию и дате;
- нечитабельные comparison/evidence артефакты.

## Какие Типы Артефактов Будут Храниться

Минимально будут храниться:
- legacy reference fixtures;
- target fixtures или target outputs;
- parity comparison artifacts;
- evidence artifacts.

## Какая Нужна Структура Каталогов

Skeleton только для этого блока:

```text
artifacts/web_source_snapshot_block/
  legacy/
  target/
  parity/
  evidence/
```

Если понадобится дополнительная разбивка, она должна идти внутрь этих четырёх зон, а не смешивать их между собой.

## Какой Должен Быть Принцип Именования Файлов

Имя файла должно сразу показывать:
- сценарий;
- дату или period case;
- сторону (`legacy` или `target`);
- тип артефакта (`fixture`, `comparison`, `evidence`).

Базовый шаблон:

```text
<scenario>__<date-or-period>__<side>__<artifact-type>.<ext>
```

Примеры шаблонов:
- `normal__2026-03-20__legacy__fixture.json`
- `normal__2026-03-20__target__fixture.json`
- `normal__2026-03-20__legacy-vs-target__comparison.md`
- `not-found__2026-03-21__evidence.md`

## Что Обязательно Должно Быть Разделено

Обязательно разделять:
- legacy fixtures и target fixtures;
- raw fixture artifacts и comparison artifacts;
- comparison artifacts и final evidence notes;
- normal-case и failure-case сценарии.

Нельзя смешивать:
- разные даты в одном безымянном артефакте;
- legacy и target payload в одном fixture-файле;
- evidence summary и payload sample в одном файле.

## Что Не Входит В Этот План

Не входит:
- создание самих каталогов;
- создание самих fixtures;
- выбор финальных расширений для всех будущих файлов;
- loader/utilities/test harness;
- реализация модуля.

## Следующий Шаг После Этого Документа

Следующий шаг:
- создать в `wb-core` короткий plan минимального начального набора артефактов для этого layout, то есть перечислить какие первые 5–7 файлов должны появиться самыми первыми без их фактического создания.
