# Ads Bids Normal-Case Comparison

- `snapshot_date` совпадает: `2026-04-05`.
- Состав `nmId` совпадает: `210183919`, `210184534`.
- `ads_bid_search` сохранён без потери:
  - `210183919 -> 4000.0`
  - `210184534 -> 4000.0`
- `ads_bid_recommendations` сохранён без потери:
  - `210183919 -> 0.0`
  - `210184534 -> 0.0`
- Различие только в target envelope: `result.kind = "success"` и агрегированные item fields.
