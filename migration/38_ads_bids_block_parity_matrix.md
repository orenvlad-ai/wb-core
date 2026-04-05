# Parity Matrix Блока Ads Bids

## Зачем Нужна Matrix

Эта matrix фиксирует минимальную parity surface для `ads_bids_block` между current legacy semantics и target block.

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| request date | `result.snapshot_date` | required |
| `nmId` | `result.items[].nm_id` | required |
| `max(search bid_kopecks) / 100` | `result.items[].ads_bid_search` | required |
| `max(recommendations bid_kopecks) / 100` | `result.items[].ads_bid_recommendations` | required |
| количество найденных `nmId` | `result.count` | required |

## Empty-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| нет active bid rows по запрошенным `nmId` | `result.kind = "empty"` | required |
| request date | `result.snapshot_date` | required |
| пустой набор | `result.items = []` | required |
| count zero | `result.count = 0` | required |

## Что Проверяется На Первом Checkpoint

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- empty-case на artificial `nmId`, который не присутствует в active campaigns;
- корректный `max` по placement;
- корректный перевод из копеек в рубли.
