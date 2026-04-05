# Spp Normal-Case Comparison

- `snapshot_date` совпадает: `2026-04-04`.
- Состав `nmId` совпадает: `210183919`, `210184534`.
- `spp_avg` сохранён без потери:
  - `210183919 -> 0.24621052631578932`
  - `210184534 -> 0.24250000000000005`
- Различие только в target envelope: `result.kind = "success"` и item shape без internal `spp_count`.
