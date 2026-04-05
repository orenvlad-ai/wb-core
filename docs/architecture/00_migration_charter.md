# Migration Charter

## Цель

Создать новый target-core рядом с legacy и затем переносить возможности по одной, под явным контролем.

Целевое состояние:
- server-first modular monolith;
- domain-логика живёт вне operator-table;
- таблица становится thin operator shell;
- legacy-контуры остаются maintenance-only, пока каждая замена не доказана.

Новая разработка идёт только в `wb-core`.

В legacy допускаются только maintenance, bugfix и reconcile-изменения.

## Scope Текущего Этапа

В scope Phase 0/1 входит:
- зафиксировать migration model и ограничения;
- зафиксировать target architecture и границы репозитория;
- зафиксировать source-of-truth policy и anti-drift rules;
- зафиксировать backlog foundation-этапа, cutover rules и правила работы Codex;
- собрать inventory legacy-контрактов, которые нельзя потерять при последующих переносах.

## Что Считается Success

Этап успешен, если:
- в `wb-core` есть reviewable foundation package;
- target-boundaries описаны достаточно жёстко, чтобы не повторить drift между table/server/web;
- следующий модульный перенос можно начинать без повторного спора о migration model;
- ни один business-модуль не перенесён преждевременно.

## Что Не Входит В Этап

Сейчас не входит в scope:
- перенос business-модулей;
- создание новой таблицы;
- production API, ingestion, jobs или web-source код;
- миграции БД;
- CI/CD и deploy automation;
- cutover любых live-workflow;
- замена legacy-репозиториев.

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
