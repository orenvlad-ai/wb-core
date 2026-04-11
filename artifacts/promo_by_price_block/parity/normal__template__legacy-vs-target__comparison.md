# Promo By Price Normal Comparison

- `date_from/date_to` сохранены без изменений.
- В target сохранён полный `date + nmId` row set из legacy sample.
- `promo_count_by_price` совпадает для всех 8 row-level observations.
- `promo_entry_price_best` совпадает как `max(plan_price)` среди active rules.
- `promo_participation` совпадает как бинарный derived flag от `promo_count_by_price`.

Вывод: normal-case parity подтверждена на bootstrap sample set.
