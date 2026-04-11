# Evidence Summary: table_projection_bundle_block

- Upstream/source basis зафиксирован как уже перенесённые module outputs `wb-core` плюс `migration/65_new_table_minimum_data_contract.md`.
- Для этого блока используется `reference source`, а не классический legacy RAW-path.
- Projection остаётся минимальным: один table-facing bundle, status/freshness/coverage слой и linked history summary.
- Artifact-backed input bundle подтверждает стабильную и reviewable projection shape.
- Bundle-composition path подтверждает, что этот projection честно собирается из уже существующих merged fixtures без новых fetch/API клиентов.

Вывод: `table_projection_bundle_block` даёт первый реальный server-side projection для новой витрины без раннего перепроектирования таблицы или registry-слоя.
