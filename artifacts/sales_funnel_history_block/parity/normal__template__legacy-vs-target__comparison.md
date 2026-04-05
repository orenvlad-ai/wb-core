# Sales Funnel History Normal-Case Comparison

- `date`, `nmId`, `metric` состав совпадает.
- Latest `fetched_at` semantics сохранена.
- Percent metrics нормализованы в долю:
  - `buyoutPercent 100 -> 1.0`
  - `addToCartConversion 19 -> 0.19`
  - `cartToOrderConversion 71 -> 0.71`
- Остальные метрики перенесены без преобразования значения.
