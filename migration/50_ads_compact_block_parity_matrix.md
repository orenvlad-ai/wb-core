# Parity Matrix Блока Ads Compact

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| `snapshot_date` | `result.snapshot_date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `ads_views` | `result.items[].ads_views` | required |
| `ads_clicks` | `result.items[].ads_clicks` | required |
| `ads_atbs` | `result.items[].ads_atbs` | required |
| `ads_orders` | `result.items[].ads_orders` | required |
| `ads_sum` | `result.items[].ads_sum` | required |
| `ads_sum_price` | `result.items[].ads_sum_price` | required |
| `ads_sum / ads_clicks` | `result.items[].ads_cpc` | required |
| `ads_clicks / ads_views` | `result.items[].ads_ctr` | required |
| `ads_orders / ads_clicks` | `result.items[].ads_cr` | required |

## Empty-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| no compact rows for requested `nmId` set on `snapshot_date` | `result.kind = "empty"` | required |
| `items = []` | `result.items` | required |
| `count = 0` | `result.count` | required |

## Checkpoint Scope

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- natural empty-case для `nmId`, отсутствующего в filtered compact rows;
- RAW aggregation semantics на уровне `snapshot_date + nmId`;
- derivation `ads_cpc`, `ads_ctr`, `ads_cr`.
