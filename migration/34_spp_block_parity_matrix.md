# Parity Matrix Блока Spp

## Зачем Нужна Matrix

Эта matrix фиксирует минимальную parity surface для `spp_block` между current legacy semantics и target block.

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| request date | `result.snapshot_date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `spp_avg` | `result.items[].spp` | required |
| количество найденных `nmId` | `result.count` | required |

## Empty-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| нет sales rows по запрошенным `nmId` на дату | `result.kind = "empty"` | required |
| request date | `result.snapshot_date` | required |
| пустой набор | `result.items = []` | required |
| count zero | `result.count = 0` | required |

## Что Проверяется На Первом Checkpoint

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- empty-case на artificial `nmId`, который не попадает в sales rows за дату;
- корректная нормализация `spp` в долю;
- корректное среднее по всем sales rows на дату.
