# Normal Case Parity

- `DATA_VITRINA` берёт `sheet_name`, `header` и `rows` напрямую из delivery bundle и добавляет только scaffold metadata для full overwrite.
- `STATUS` берёт `sheet_name`, `header` и `rows` напрямую из delivery bundle и добавляет только scaffold metadata для full overwrite.
- План записи фиксирует две независимые sheet-зоны:
  - `DATA_VITRINA` -> `A1:E19`
  - `STATUS` -> `A1:K12`
- Частичных обновлений нет: обе секции считаются полным overwrite текущего payload range.
