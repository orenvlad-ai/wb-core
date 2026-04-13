# Compact V3 Seed Bootstrap: input-vs-sheets

- Источником bounded bootstrap служит compact v3 seed, встроенный в repo в `input/` и зеркалированный в `gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs`.
- `prepareRegistryUploadOperatorSheets()` materialize-ит те же `config_v2`, `metrics_v2`, `formulas_v2` в листы `CONFIG`, `METRICS`, `FORMULAS`.
- `CONFIG` сохраняет основной компактный блок в колонках `A:E`, а service/control block остаётся в `H:I`.
- Пустые колонки `F:G` не входят в bundle и остаются только визуальным разделителем между compact seed и service-zone.
- Для bounded совместимости с текущим live upload path seed сужен до runtime-compatible subset: `9` SKU, `10` metrics, `2` formulas.
- Нормализации bounded шага:
  - `display_order` в compact subset перенумерован плотно;
  - `ads_ctr` нормализован к runtime key в `calc_ref` вместо legacy ratio-string;
  - `F_proxy_profit_rub` нормализован в `proxy_profit_rub`, чтобы formula metric совпадал с текущим runtime contract.
