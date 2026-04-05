# Stocks Partial-Case Comparison

- `snapshot_date` совпадает: `2026-04-05`.
- Requested `nmId` set содержит `210183919` и `210184534`.
- В legacy sample покрыт только `210183919`, coverage = `1/2`.
- Target корректно возвращает `result.kind = "incomplete"` и `missing_nm_ids = [210184534]`.
- Это сохраняет apply-level guard против публикации неполного snapshot.
