# seed-and-runtime-vs-data-vitrina

- `CONFIG / METRICS / FORMULAS` поднимаются как MVP-safe compact v3 seed `33 / 7 / 7`.
- Upload path materialize-ит current registry state в runtime DB.
- `DATA_VITRINA` грузится обратно не из local fixture, а из live public server-side sources `seller_funnel_snapshot` и `web_source_snapshot` поверх current registry state.
