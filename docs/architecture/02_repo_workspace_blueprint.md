# Схема Репозитория И Workspace

## Статус Документа

Этот документ описывает target workspace blueprint и должен явно отделяться от фактического дерева `main`.

## Current Main-Confirmed Tree

На текущем `main` подтверждены верхнеуровневые директории и контуры:
- `apps/`
- `artifacts/`
- `docs/`
- `gas/`
- `migration/`
- `packages/adapters/`
- `packages/application/`
- `packages/contracts/`
- `registry/`

Дополнительно в корне присутствует `.clasp.json` для bound Apps Script wiring новой витрины.

На текущем `main` не найдены как материализованные каталоги:
- `packages/domain/`
- `infra/`
- `tests/`
- `api/`
- `jobs/`
- `db/`

Эти слои нельзя описывать как уже существующие placeholders в текущем дереве repo.

## Целевая Форма Репозитория

Планируемая верхнеуровневая структура:
- `apps/`
- `packages/`
- `infra/`
- `docs/`
- `migration/`
- `tests/`

Это target/future blueprint, а не буквальное описание текущего `main`.

## Роли Директорий

### `apps/`

Здесь живут thin entrypoint-ы:
- будущий operator shell;
- будущие internal service entrypoint-ы;
- будущие admin или maintenance surfaces.

Domain-логика отсюда начинаться не должна.

### `packages/`

Здесь живёт модульный core-код:
- application services;
- contracts и schemas;
- adapters/integrations;
- shared utilities с жёсткой ownership-моделью.

Текущее состояние `main`:
- `packages/contracts`, `packages/application` и `packages/adapters` уже есть;
- `packages/domain` пока не материализован.

### `infra/`

Здесь живут repo-owned operational artifacts:
- local environment descriptors;
- non-secret runtime manifests;
- позже observability/bootstrap wiring.

Здесь не должно быть секретов и host-specific runtime snapshots.

Текущее состояние `main`:
- каталог `infra/` ещё не материализован.

### `docs/`

Здесь живут устойчивые architecture docs, ADR и operating policies.

### `migration/`

Здесь живут migration backlog, contract inventory, parity rules и staged module notes.

### `tests/`

Здесь живут contract, adapter, application и parity tests.

Текущее состояние `main`:
- отдельный каталог `tests/` ещё не материализован;
- текущие проверки живут в `apps/*_smoke.py` и в artifact-backed parity/evidence слоях.

## Что Должно Жить В Репозитории

В репозитории должны жить:
- source code;
- contracts и schemas;
- архитектурные решения;
- migration policy;
- deterministic fixtures и tests;
- local/dev environment templates без секретов.

## Что Не Должно Жить В Репозитории

В репозитории не должны жить:
- секреты;
- ad hoc runtime snapshots;
- browser session state;
- host-local DB dumps, если они явно не санированы и не одобрены;
- manual production-only patches, не отражённые в Git;
- скопированный legacy-код без redesign и ownership.

Факты из reference:
- `wb-web-bot` использует локальный `storage_state.json`;
- `wb-ai-research` ожидает `/opt/wb-ai/.env` и `/opt/wb-ai/gcp-sa.json`;
- reconcile summary в reference-репозиториях показывают, почему runtime-only state нельзя считать source truth для core.

## Как Будет Сохраняться Модульность

Модульность сохраняется так:
- contract-first interfaces между модулями;
- без cross-module доступа к internal files;
- adapters зависят внутрь на contracts, а не наоборот;
- tests проверяют public contracts, а не incidental internals;
- каждый перенесённый capability входит как bounded module, а не как legacy dump.
