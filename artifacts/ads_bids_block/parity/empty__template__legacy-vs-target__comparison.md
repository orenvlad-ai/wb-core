# Ads Bids Empty-Case Comparison

- `snapshot_date` совпадает: `2026-04-05`.
- Для запрошенного `nmId = 999000001` active bid rows отсутствуют.
- Legacy `data.rows = []` преобразуется в target `result.kind = "empty"`.
- `count = 0` и `items = []` сохранены без потери.
