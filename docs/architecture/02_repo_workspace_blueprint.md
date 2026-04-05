# Схема Репозитория И Workspace

## Целевая Форма Репозитория

Планируемая верхнеуровневая структура:
- `apps/`
- `packages/`
- `infra/`
- `docs/`
- `migration/`
- `tests/`

Сейчас эти директории уже существуют в репозитории как пустые placeholders.

## Роли Директорий

### `apps/`

Здесь живут thin entrypoint-ы:
- будущий operator shell;
- будущие internal service entrypoint-ы;
- будущие admin или maintenance surfaces.

Domain-логика отсюда начинаться не должна.

### `packages/`

Здесь живёт модульный core-код:
- domain rules;
- application services;
- contracts и schemas;
- adapters/integrations;
- shared utilities с жёсткой ownership-моделью.

### `infra/`

Здесь живут repo-owned operational artifacts:
- local environment descriptors;
- non-secret runtime manifests;
- позже observability/bootstrap wiring.

Здесь не должно быть секретов и host-specific runtime snapshots.

### `docs/`

Здесь живут устойчивые architecture docs, ADR и operating policies.

### `migration/`

Здесь живут migration backlog, contract inventory, parity rules и staged module notes.

### `tests/`

Здесь живут contract, adapter, application и parity tests.

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
