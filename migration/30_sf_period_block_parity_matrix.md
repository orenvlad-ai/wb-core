# Parity Matrix Блока Sf Period

## Зачем Нужна Matrix

Эта matrix фиксирует минимальную parity surface для `sf_period_block` между current legacy semantics и target block.

## Success-Case

| Legacy semantics | Target field | Status |
| --- | --- | --- |
| request date | `result.snapshot_date` | required |
| `product.nmId` | `result.items[].nm_id` | required |
| `statistic.selected.localizationPercent` | `result.items[].localization_percent` | required |
| `product.feedbackRating` | `result.items[].feedback_rating` | required |
| количество вернувшихся карточек | `result.count` | required |

## Что Проверяется На Первом Checkpoint

На первом checkpoint проверяется:
- success-case на bootstrap sample set;
- корректная сборка item shape по `nmId`;
- отсутствие потери `localizationPercent`;
- отсутствие потери `feedbackRating`.

## Что Осознанно Не Покрывается Здесь

В этом checkpoint не покрывается:
- полнота каталога beyond bootstrap sample set;
- pagination beyond текущий bounded sample;
- sort/filter semantics beyond `nmIds`;
- отдельный domain-level empty-case, пока он не подтверждён безопасным sample;
- table write path.
