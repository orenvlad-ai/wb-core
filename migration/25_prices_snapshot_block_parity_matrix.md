# Parity Matrix Блока Prices Snapshot

## Цель Parity-Проверки

Parity нужна, чтобы доказать: новый блок воспроизводит текущую legacy semantics prices RAW/APPLY path по смыслу до начала дальнейшего расширения.

## Что Сравниваем

Сравниваем:
- legacy-source как `POST /api/v2/list/goods/filter` + current normalization/apply semantics;
- target block output в `wb-core`.

Уровень сравнения:
- contract level;
- payload level;
- semantic level;
- empty-case level.

## Обязательные Точки Сравнения

| Точка | Что должно совпадать |
| --- | --- |
| `snapshot_date` | Day semantics snapshot-а сохраняется |
| Item identity | `nm_id` остаётся главным ключом |
| `price_seller` | Равен `min(price)` по всем sizes на `nmId` |
| `price_seller_discounted` | Равен `min(discountedPrice)` по всем sizes на `nmId` |
| `count` | Количество агрегированных `nmId` сохраняет смысл |
| Payload shape | Обязательные поля присутствуют и стабильны |
| Empty-case | Пустой ответ не превращается в hard failure |

## Что Считается Успехом

Успех:
- `snapshot_date`, `count` и items совпадают по смыслу;
- на каждом `nmId` сохраняются `price_seller` и `price_seller_discounted`;
- empty-case остаётся отдельным controlled режимом;
- различия сведены к target envelope и discriminator `kind`.

Неприемлемое расхождение:
- потеря агрегации `min(...)`;
- drift между raw prices и aggregated item;
- смешение empty-case с общей ошибкой;
- потеря `nmId`.

## Какие Evidence Нужны

Нужны:
- legacy normal-case sample;
- legacy empty-case sample;
- target normal-case sample;
- target empty-case sample;
- comparison по обоим режимам;
- evidence summary.

## Что Не Входит В Parity Этого Блока

Не входит:
- полнота active catalog;
- `CONFIG/METRICS` parity;
- performance;
- network hardening;
- downstream business parity.
