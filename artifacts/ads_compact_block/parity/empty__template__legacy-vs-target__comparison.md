# Ads Compact Empty-Case Comparison

- `snapshot_date` совпадает: `2026-04-05`.
- В legacy отсутствуют compact rows для запрошенного `nmId`.
- В target это честно отображается как:
  - `result.kind = "empty"`
  - `result.count = 0`
  - `result.items = []`
- Потери данных нет: empty-case является естественным результатом фильтрации по `nmId` и дате.
