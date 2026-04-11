# Initial Evidence

- Legacy-source зафиксирован как `RAW_PROMO_RULES + DATA.price_seller_discounted + 77_plugins_promo_by_price.js`.
- Источник `nmId` зафиксирован как bootstrap active SKU set из уже известных проекту `nmId`.
- Собраны legacy samples для `normal-case` и `empty/no-rule`.
- Собраны target samples для `normal-case` и `empty`.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty/no-rule`.
- Artifact-backed transformation проверяется локальным smoke-check.
- Fixture-backed rule-source semantics дополнительно проверяется отдельным rule-smoke без live/deploy path.

Вывод: `promo_by_price_block` подтверждён как рабочий bounded checkpoint rule/apply-слоя без зависимости от browser/web-source контура.
