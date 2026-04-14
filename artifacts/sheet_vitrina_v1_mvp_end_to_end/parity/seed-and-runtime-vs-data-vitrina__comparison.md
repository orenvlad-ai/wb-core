# seed-and-runtime-vs-data-vitrina

- `CONFIG / METRICS / FORMULAS` поднимаются как current operator seed `33 / 19 / 2`.
- Upload path materialize-ит current registry state в runtime DB.
- `DATA_VITRINA` при этом остаётся bounded to `7` supported live readback metrics и не расширяется автоматически до всех `19` metric rows.
- `DATA_VITRINA` грузится обратно не из local fixture, а из live public server-side sources `seller_funnel_snapshot` и `web_source_snapshot` поверх current registry state.
