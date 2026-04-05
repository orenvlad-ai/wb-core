# Directory Skeleton Plan Блока Web-Source Snapshot

## Зачем Нужен Directory Skeleton Plan

Этот план нужен, чтобы не начать собирать артефакты в случайных местах.

Он предотвращает:
- смешение категорий данных;
- хаос в первых артефактах;
- лишнюю глубину структуры до реальной необходимости.

## Какие Минимальные Директории Должны Появиться Первыми

| Путь | Категория | Зачем нужна | Что в ней будет лежать |
| --- | --- | --- | --- |
| `artifacts/web_source_snapshot_block/legacy/` | legacy | Хранить reference fixtures legacy-стороны | legacy sample payloads для normal-case и `not found` |
| `artifacts/web_source_snapshot_block/target/` | target | Хранить target fixtures или target outputs | target sample payloads для тех же сценариев |
| `artifacts/web_source_snapshot_block/parity/` | parity | Хранить comparison artifacts | old/new comparison по normal-case и `not found` |
| `artifacts/web_source_snapshot_block/evidence/` | evidence | Хранить короткие evidence summaries | итоговые notes по тому, что уже доказано стартовым набором |

Это и есть минимальный skeleton. Дополнительная глубина на этом шаге не нужна.

## Какие Разделения Обязательны

Сразу должны быть разведены:
- legacy и target;
- parity и evidence;
- raw payload artifacts и summary artifacts.

Нельзя держать в одной папке:
- legacy и target payloads;
- comparison markdown и payload json;
- failure-case и summary notes без явной категории.

## Что Не Входит В Этот Skeleton

Не входит:
- создание самих директорий;
- поддиректории по датам;
- поддиректории по сценариям;
- loader/test/utilities;
- структура под другие migration units;
- структура под production runtime artifacts.

## Как Понять, Что Skeleton Достаточен

Skeleton достаточен, если:
- в нём есть отдельные зоны для `legacy`, `target`, `parity`, `evidence`;
- первый стартовый набор артефактов можно разложить без смешения типов;
- не требуется дополнительная вложенность для normal-case и `not found`.

## Следующий Шаг После Этого Документа

Следующий шаг:
- создать в `wb-core` короткий naming-checklist для первых artifact-файлов этого блока, чтобы перед фактическим созданием файлов был зафиксирован единый шаблон имён для normal-case и `not found`.
