# wb-core

`wb-core` — новый target-core репозиторий для controlled greenfield sidecar migration.

Legacy-репозитории остаются рабочими и на текущем этапе не заменяются:
- `wb-table-audit`
- `wb-ai-research`
- `wb-web-bot`

В legacy допускаются только maintenance, bugfix и reconcile-изменения.

Новая разработка идёт только в `wb-core`.

Этот репозиторий создаётся для foundation-этапа и target-core, а не для in-place refactor legacy.

Phase 0/1 содержит только архитектурные, migration- и control-документы. Здесь нет business-логики, production ingestion/jobs/api реализаций и новой operator-table.
