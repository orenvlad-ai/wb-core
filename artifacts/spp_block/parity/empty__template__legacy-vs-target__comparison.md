# Spp Empty-Case Comparison

- `snapshot_date` совпадает: `2026-04-04`.
- Для запрошенного `nmId = 999000001` sales rows отсутствуют.
- Legacy `data.items = []` преобразуется в target `result.kind = "empty"`.
- `count = 0` и `items = []` сохранены без потери.
