# Initial Evidence

- Legacy-source зафиксирован как `RAW_COGS_RULES + CONFIG.group + 77_plugins_cogs_by_group.js`.
- Источник `nmId` и group linkage зафиксирован через bootstrap active SKU set.
- Собраны legacy samples для `normal-case` и `empty/no-row`.
- Собраны target samples для `normal-case` и `empty`.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty/no-row`.
- Artifact-backed transformation проверяется локальным smoke-check.
- Fixture-backed rule-source semantics дополнительно проверяется отдельным rule-smoke без live/deploy path.

Вывод: `cogs_by_group_block` подтверждён как рабочий bounded checkpoint rule/apply-слоя без зависимости от browser/web-source контура.
