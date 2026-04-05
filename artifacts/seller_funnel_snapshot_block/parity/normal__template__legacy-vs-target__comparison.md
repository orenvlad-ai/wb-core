# Normal-Case Comparison

- `date` совпадает по смыслу и значению.
- `count` совпадает.
- `items` сохранены без потери состава и item-полей: `nm_id`, `name`, `vendor_code`, `view_count`, `open_card_count`, `ctr`.
- Различие есть только в верхнем target shape: `result` + `kind: "success"`.

Вывод: parity по смыслу для normal-case достигнута.

