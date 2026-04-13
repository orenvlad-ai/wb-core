# Migration Charter

## Статус Документа

Этот документ фиксирует исходный charter запуска `wb-core`.

Foundation-этап уже пройден. Текущее состояние `main` шире стартового charter:
- в `main` уже есть смёрженные bounded modules для `web-source`, `official-api`, `rule-based`, `table-facing`, `registry`, `wide-matrix` и `sheet-side` линии;
- подтверждён working contour `sku_display -> table_projection -> registry_pilot -> wide_matrix -> delivery -> sheet_scaffold`;
- live write и presentation новой витрины уже присутствуют в `main` как code + evidence;
- registry upload path уже дошёл до artifact-backed bundle, local validator, file-backed service, DB-backed runtime, live HTTP entrypoint, sheet-side operator trigger и compact v3 bootstrap: `migration/86`, `migration/87`, `migration/88`, `migration/89`, `migration/90`, `migration/91`, `migration/92` вместе с соответствующими модулями уже materialize-ятся в текущей линии;
- на текущей PR-ветке дополнительно materialize-ится `migration/93` как первый bounded end-to-end MVP `prepare -> upload -> load DATA_VITRINA`.

## Цель

Создать новый target-core рядом с legacy и затем переносить возможности по одной, под явным контролем.

Целевое состояние:
- server-first modular monolith;
- domain-логика живёт вне operator-table;
- таблица становится thin operator shell;
- legacy-контуры остаются maintenance-only, пока каждая замена не доказана.

Новая разработка идёт только в `wb-core`.

В legacy допускаются только maintenance, bugfix и reconcile-изменения.

## Исторический Scope Стартового Этапа

В стартовый scope Phase 0/1 входило:
- зафиксировать migration model и ограничения;
- зафиксировать target architecture и границы репозитория;
- зафиксировать source-of-truth policy и anti-drift rules;
- зафиксировать backlog foundation-этапа, cutover rules и правила работы Codex;
- собрать inventory legacy-контрактов, которые нельзя потерять при последующих переносах.

## Что Считалось Success Для Стартового Этапа

Стартовый этап считался успешным, если:
- в `wb-core` есть reviewable foundation package;
- target-boundaries описаны достаточно жёстко, чтобы не повторить drift между table/server/web;
- следующий модульный перенос можно начинать без повторного спора о migration model;
- ни один business-модуль не перенесён преждевременно.

Эти критерии уже закрыты и не описывают текущий checkpoint `main`.

## Что Не Входило В Стартовый Этап

В стартовый scope не входило:
- перенос business-модулей;
- создание новой таблицы;
- production API, ingestion, jobs или web-source код;
- миграции БД;
- CI/CD и deploy automation;
- cutover любых live-workflow;
- замена legacy-репозиториев.

## Текущий Checkpoint Main

На текущем `main` уже собраны:
- bounded source blocks и official-api blocks;
- rule-based модули;
- table-facing и projection слой для новой витрины;
- registry pilot line;
- wide matrix, delivery bundle и sheet scaffold;
- live write bridge и presentation pass для новой Google Sheets-витрины;
- compact v3 bootstrap для `CONFIG / METRICS / FORMULAS`.

На текущем `main` ещё не собраны:
- controlled reverse-load server-side current truth обратно в `DATA_VITRINA`.
- deploy/auth-hardening и production storage binding вокруг registry upload.

На текущей PR-ветке дополнительно закрывается:
- первый bounded reverse-load server-side truth обратно в `DATA_VITRINA` через lightweight plan endpoint и уже существующие live public source blocks.

После этого на линии остаются:
- full legacy parity по всем metric sections и registry rows;
- stable hosted runtime URL, deploy/auth-hardening и production storage binding.

## Почему Выбран Greenfield Sidecar Migration

Greenfield sidecar migration выбран потому, что текущий legacy уже разрезан на несколько контуров:
- Apps Script table/operator слой в `wb-table-audit`;
- server/data слой в `wb-ai-research`;
- browser-driven web-source capture в `wb-web-bot`.

Факты из reference-репозиториев:
- operator-логика, raw ingestion, apply-логика и export-логика смешаны в Apps Script codebase;
- server-only контракты и runtime-state уже живут вне Git-управляемого table-кода;
- reconcile summary в `wb-ai-research` и `wb-web-bot` прямо фиксируют drift между Git и runtime.

In-place refactor сохранил бы текущее переплетение. Sidecar foundation даёт контролируемый путь с явными контрактами, staged parity checks и без big-bang rewrite.
