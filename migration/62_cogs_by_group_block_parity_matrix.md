# Parity Matrix Блока Cogs By Group

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| `date` | `result.items[].date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `cost_price_rub` | `result.items[].cost_price_rub` | required |

## Empty-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| no applicable cost rows for requested `nmId` in range | `result.kind = "empty"` | required |
| `items = []` | `result.items` | required |
| `count = 0` | `result.count` | required |

## Checkpoint Scope

На первом checkpoint проверяется:
- success-case на bootstrap `nmId` set;
- empty/no-row case для requested `nmId`, отсутствующего в historical group linkage;
- group linkage из bootstrap active SKU set;
- rule resolution по latest `effective_from <= date`.
