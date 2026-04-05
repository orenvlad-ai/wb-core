# Normal Comparison

- `date_from` / `date_to`: совпадают по смыслу и значениям (`2026-04-04` / `2026-04-04`).
- `count`: совпадает, значение `35` сохранено.
- `items`: состав не потерян, количество и содержимое items сохранены.
- `nm_id`, `views_current`, `ctr_current`, `orders_current`, `position_avg`: item-level поля сохранены без semantic drift.
- Различие только в верхнем shape: target добавляет envelope `result` и discriminator `kind: "success"`.

Вывод: parity по смыслу для normal-case достигнута. Изменение касается только оболочки ответа, данные не потеряны.
