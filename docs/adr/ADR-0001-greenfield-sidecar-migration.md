# ADR-0001: Greenfield Sidecar Migration

## Context

Текущее legacy-поведение разрезано на три репозитория:
- `wb-table-audit` для Apps Script table/operator logic и `AI_EXPORT`;
- `wb-ai-research` для ingest, registry sync, analysis и server API;
- `wb-web-bot` для browser-based web-source capture.

Reference-репозитории также показывают документированный Git/runtime drift и смешанную ownership-модель между operator, server и web-source concerns.

## Decision

Строить новый target-core в `wb-core` как controlled greenfield sidecar migration.

Legacy in-place не рефакторить.

Сначала зафиксировать foundation documents и control rules. Функциональные модули переносить позже, по одной bounded capability за раз, с parity evidence и strangler-style cutover.

## Consequences

Плюсы:
- чище границы;
- ниже риск перетащить legacy-chaos в target-state;
- явные source-of-truth и anti-drift rules;
- безопаснее staged cutover.

Цена:
- legacy дольше остаётся в работе;
- краткосрочно дублируются репозитории и понятия;
- migration требует осознанного contract inventory и parity work вместо быстрого copy-paste.
