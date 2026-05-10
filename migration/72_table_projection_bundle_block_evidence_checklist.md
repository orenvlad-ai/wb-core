# Evidence Checklist Для Table Projection Bundle

- [x] Upstream/source basis зафиксирован как набор уже перенесённых модулей и `migration/65_new_table_minimum_data_contract.md`.
- [x] Явно отмечено, что для этого блока используется `reference`, а не классический legacy RAW-path.
- [x] Подготовлен input-bundle normal-case sample.
- [x] Подготовлен minimal-case sample.
- [x] Подготовлены target samples для обоих режимов.
- [x] Заполнена parity matrix.
- [x] Реализован artifact-backed input-bundle adapter path.
- [x] Реализован bundle-composition adapter path поверх существующих module fixtures.
- [x] Подтверждён artifact-backed smoke.
- [x] Подтверждён bundle-composition smoke.
- [x] Выполнен doc-sync через `docs/modules/14_MODULE__TABLE_PROJECTION_BUNDLE_BLOCK.md` и индекс.
