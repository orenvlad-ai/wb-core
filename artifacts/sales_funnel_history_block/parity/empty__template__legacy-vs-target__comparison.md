# Sales Funnel History Empty-Case Comparison

- Для `nmId = 999000001` upstream вернул item c пустым `history`.
- Legacy rows после нормализации равны `[]`.
- Target корректно возвращает `result.kind = "empty"` и `items = []`.
