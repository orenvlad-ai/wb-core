# Normal Comparison

- `snapshot_date`: совпадает по смыслу и значению (`2026-04-05`).
- `count`: совпадает, агрегированы оба `nmId`.
- `210183919.price_seller`: `min(1499, 1399) = 1399`, значение сохранено.
- `210183919.price_seller_discounted`: `min(1049, 999) = 999`, значение сохранено.
- `210184534.price_seller`: `min(1890, 1790) = 1790`, значение сохранено.
- `210184534.price_seller_discounted`: `min(1490, 1390) = 1390`, значение сохранено.
- Различие только в верхнем shape: target добавляет envelope `result` и discriminator `kind: "success"`.

Вывод: parity по смыслу для normal-case достигнута.
