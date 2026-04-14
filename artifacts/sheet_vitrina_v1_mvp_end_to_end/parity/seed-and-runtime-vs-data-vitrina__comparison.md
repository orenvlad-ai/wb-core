# seed-and-runtime-vs-data-vitrina

- `CONFIG / METRICS / FORMULAS` поднимаются как current operator seed `33 / 19 / 2`.
- Upload path materialize-ит current registry state в runtime DB.
- `DATA_VITRINA` теперь materialize-ит все `19` authoritative metric rows из current runtime truth и больше не режется до `7` displayed metrics.
- Numeric live fill при этом остаётся backed only для current `7` public readback metrics; остальные authoritative rows пока пишутся blank вместо выпадения из плана.
- `DATA_VITRINA` грузится обратно не из local fixture, а из live public server-side sources `seller_funnel_snapshot` и `web_source_snapshot` поверх current registry state.
