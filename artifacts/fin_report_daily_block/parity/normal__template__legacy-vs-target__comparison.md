# Fin Report Daily Normal-Case Comparison

- `snapshot_date` совпадает: `2026-04-05`.
- Состав `nmId` совпадает: `210183919`, `210184534`.
- По каждому `nmId` сохранены все финансовые поля:
  - `210183919 -> delivery=120.0 storage=40.0 deduction=10.0 commission=55.0 penalty=0.0 additional=5.0 buyout=890.0 commission_wb_portal=88.0 acquiring=12.0 loyalty=7.0`
  - `210184534 -> delivery=80.0 storage=20.0 deduction=0.0 commission=33.0 penalty=2.0 additional=0.0 buyout=540.0 commission_wb_portal=54.0 acquiring=8.0 loyalty=3.0`
- Total storage row вынесен в отдельное поле `storage_total`.
- Различие только в target envelope: `result.kind = "success"` и явное отделение `nmId=0` от обычных item.
