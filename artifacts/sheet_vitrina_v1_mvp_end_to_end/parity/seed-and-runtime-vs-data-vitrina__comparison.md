# seed-and-runtime-vs-data-vitrina

- `CONFIG / METRICS / FORMULAS` поднимаются как current operator seed `33 / 102 / 7`.
- Upload path materialize-ит current registry state в runtime DB.
- `DATA_VITRINA` materialize-ит все `95` enabled+show_in_data metrics из current authoritative package и даёт `1631` data rows.
- `DATA_VITRINA` грузится обратно не из local fixture, а из live public server-side sources поверх current registry state.
- Promo/cogs-backed rows не срезаются: они остаются в выдаче, а blocker фиксируется через `STATUS`.
