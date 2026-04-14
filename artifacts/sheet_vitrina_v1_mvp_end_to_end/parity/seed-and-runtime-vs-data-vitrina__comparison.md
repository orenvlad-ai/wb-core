# seed-and-runtime-vs-data-vitrina

- `CONFIG / METRICS / FORMULAS` поднимаются как current operator seed `33 / 102 / 7`.
- Upload path materialize-ит current registry state в runtime DB.
- Current truth и server-side plan по-прежнему держат все `95` enabled+show_in_data metrics из authoritative package.
- Operator-facing `DATA_VITRINA` поверх этого plan materialize-ится как тот же server-driven flat readback и даёт `1631` data rows / `95` unique metric keys.
- `DATA_VITRINA` грузится обратно не из local fixture, а из live public server-side sources поверх current registry state.
- Promo/cogs-backed rows не теряются на стороне current truth / `STATUS`; `DATA_VITRINA` не invent-ит локальный fallback и остаётся thin presentation layer.
