# Parity Matrix Блока Sales Funnel History

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| `date` | `result.items[].date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `metric` | `result.items[].metric` | required |
| latest `value` | `result.items[].value` | required |
| percent metric normalized to fraction | `result.items[].value` | required |

## Empty-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| no history rows for requested `nmId` set | `result.kind = "empty"` | required |
| `items = []` | `result.items` | required |
| `count = 0` | `result.count` | required |

## Checkpoint Scope

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- natural empty-case на synthetic `nmId` с пустым `history`;
- latest `fetched_at` semantics;
- percent normalization semantics.
