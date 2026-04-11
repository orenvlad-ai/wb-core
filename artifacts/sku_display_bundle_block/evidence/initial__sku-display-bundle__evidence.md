# Evidence Summary: sku_display_bundle_block

- Legacy-source зафиксирован как минимальный subset листа `CONFIG`.
- Для первой витрины честно ограничены только поля `nmId`, `comment`, `group`, `active` и stable row order.
- Полный перенос `CONFIG`, `METRICS`, `FORMULAS`, `DAILY RUN` сознательно оставлен вне этого checkpoint.
- Artifact-backed parity подтверждена для `normal-case` и `empty-case`.
- Safe CONFIG-fixture path подтверждает, что bundle можно собрать без live spreadsheet/runtime и без нового registry-слоя.

Вывод: `sku_display_bundle_block` даёт рабочий bounded checkpoint для главного ближайшего gap первой новой витрины.
