# Ads Compact Normal-Case Comparison

- `snapshot_date` совпадает: `2026-04-05`.
- Состав `nmId` совпадает: `210183919`, `210184534`.
- Базовые поля сохранены после aggregation по `snapshot_date + nmId`:
  - `210183919 -> views=1500.0 clicks=75.0 atbs=15.0 orders=8.0 sum=1125.0 sum_price=6000.0`
  - `210184534 -> views=800.0 clicks=32.0 atbs=8.0 orders=4.0 sum=640.0 sum_price=3200.0`
- Производные apply-level метрики восстановлены корректно:
  - `210183919 -> ads_cpc=15.0 ads_ctr=0.05 ads_cr=0.10666666666666667`
  - `210184534 -> ads_cpc=20.0 ads_ctr=0.04 ads_cr=0.125`
- Различие только в target envelope: `result.kind = "success"` и item shape с уже вычисленными производными метриками.
