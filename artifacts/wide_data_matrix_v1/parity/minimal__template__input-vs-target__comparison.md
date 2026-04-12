# Parity Для Wide Data Matrix V1: minimal-case

- Input bundle намеренно выбирает только disabled SKU `210185771`.
- Target корректно остаётся wide-shaped, но возвращает `kind = "empty"`.
- В empty-case сохраняются:
  - колонки `A = label`, `B = key`, `C = date`
  - block registry `TOTAL / GROUP / SKU`
- Все block counts равны `0`, rows отсутствуют.
- Этот сценарий подтверждает, что wide fixture не симулирует строки при отсутствии enabled SKU.
