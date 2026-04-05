# Empty Comparison

- `snapshot_date`: совпадает по смыслу и значению (`2026-04-05`).
- `listGoods = []` на legacy-стороне преобразуется в controlled empty-case, а не в hard failure.
- `count`: корректно становится `0`.
- `items`: корректно становится пустым списком.
- Target явно фиксирует discriminator `kind: "empty"` и `detail`.

Вывод: parity по смыслу для empty-case достигнута.
