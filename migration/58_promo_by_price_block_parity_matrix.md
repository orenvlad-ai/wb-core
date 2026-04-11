# Parity Matrix Блока Promo By Price

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| `date` | `result.items[].date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `promo_count_by_price` | `result.items[].promo_count_by_price` | required |
| `promo_entry_price_best` | `result.items[].promo_entry_price_best` | required |
| `promo_participation` | `result.items[].promo_participation` | required |

## Empty-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| no applicable promo rows for requested `nmId` in range | `result.kind = "empty"` | required |
| `items = []` | `result.items` | required |
| `count = 0` | `result.count` | required |

## Checkpoint Scope

На первом checkpoint проверяется:
- success-case на bootstrap `nmId` set;
- empty/no-rule case для requested `nmId`, отсутствующего в promo rules;
- `promo_count_by_price` относительно текущей discounted price;
- `promo_entry_price_best` как максимум среди активных `plan_price`;
- `promo_participation` как бинарный derived flag.
