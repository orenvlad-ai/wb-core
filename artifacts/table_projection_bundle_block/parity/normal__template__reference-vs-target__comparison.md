# Parity: normal reference vs target

- `sku_display_bundle_block` сохранён как базовый row-set на `3` SKU и определяет `display_order`.
- Все upstream source statuses честно перенесены в `source_statuses` с `freshness` и `coverage`.
- Для `210183919` и `210184534` projection сохраняет table-facing summaries из web-source и official API блоков.
- Для `210185771` projection честно показывает `kind = "missing"` во всех upstream summaries, не притягивая synthetic данные.
- `sales_funnel_history_block` свёрнут до linked `history_summary`, а не раскрыт в full inline history.
