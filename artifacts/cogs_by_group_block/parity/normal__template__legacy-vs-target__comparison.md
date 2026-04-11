# Cogs By Group Normal Comparison

- `date_from/date_to` сохранены без изменений.
- В target сохранён полный `date + nmId` row set из legacy sample.
- `cost_price_rub` совпадает для всех 8 row-level observations.
- Historical rule selection совпадает с latest `effective_from <= date`.

Вывод: normal-case parity подтверждена на bootstrap sample set.
