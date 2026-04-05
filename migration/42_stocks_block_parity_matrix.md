# Parity Matrix Блока Stocks

## Зачем Нужна Matrix

Эта matrix фиксирует минимальную parity surface для `stocks_block` между current legacy semantics и target block.

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| request date | `result.snapshot_date` | required |
| `nmId` | `result.items[].nm_id` | required |
| sum latest `stockCount` across offices | `result.items[].stock_total` | required |
| regionName=`Центральный` | `result.items[].stock_ru_central` | required |
| regionName=`Северо-Западный` | `result.items[].stock_ru_northwest` | required |
| regionName=`Приволжский` | `result.items[].stock_ru_volga` | required |
| regionName=`Уральский` | `result.items[].stock_ru_ural` | required |
| regionName=`Южный + Северо-Кавказский` | `result.items[].stock_ru_south_caucasus` | required |
| regionName=`Дальневосточный + Сибирский` | `result.items[].stock_ru_far_siberia` | required |

## Partial-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| unique covered `nmId` < requested `nmId` | `result.kind = "incomplete"` | required |
| requested coverage size | `result.requested_count` | required |
| actual covered size | `result.covered_count` | required |
| missing requested ids | `result.missing_nm_ids` | required |

## Что Проверяется На Первом Checkpoint

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- partial-case как coverage-risk sample;
- корректный `stock_total`;
- корректная региональная раскладка;
- сохранение publish guard в bounded форме.
