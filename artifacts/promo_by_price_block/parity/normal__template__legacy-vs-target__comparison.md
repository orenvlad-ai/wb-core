# Promo By Price Normal Comparison

- `date_from/date_to` сохранены без изменений.
- В target сохранён полный `date + nmId` row set из legacy sample.
- `promo_count_by_price` совпадает для всех 8 row-level observations.
- `promo_entry_price_best` теперь намеренно расходится с legacy на non-eligible rows: target возвращает truthful empty `0.0` вместо `max(plan_price)` по всем active rules.
- `promo_participation` совпадает как бинарный derived flag от `promo_count_by_price`.

Вывод: row set сохранён, а divergence по `promo_entry_price_best` на non-eligible rows является ожидаемой canonical business override.
