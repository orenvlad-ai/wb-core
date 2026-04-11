# Parity Matrix Для Sku Display Bundle

| Legacy source | Target field | Status |
| --- | --- | --- |
| `CONFIG.sku(nmId)` | `result.items[].nm_id` | required |
| `CONFIG.comment` | `result.items[].display_name` | required |
| `CONFIG.group` | `result.items[].group` | required |
| `CONFIG.active` | `result.items[].enabled` | required |
| row order inside `CONFIG` sample | `result.items[].display_order` | required |
| no rows in minimal sample | `result.kind = "empty"` | required |

## Комментарии

- Target bundle остаётся тонким и не тянет остальные поля `CONFIG`.
- `display_order` не читается из отдельной колонки: это безопасная фиксация row order внутри legacy sample.
- `enabled` сохраняет boolean-like semantics `CONFIG.active`, а не придумывает новый registry status.
