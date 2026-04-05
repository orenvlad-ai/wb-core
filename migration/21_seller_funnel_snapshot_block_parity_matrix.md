# Parity Matrix Блока Seller Funnel Snapshot

## Цель Parity-Проверки

Parity-проверка нужна, чтобы доказать: новый блок воспроизводит legacy daily contract по смыслу до начала дальнейшего расширения.

## Что Сравниваем

Сравниваем:
- legacy `GET /v1/sales-funnel/daily` как reference contract;
- target block output в `wb-core`.

Уровень сравнения:
- contract level;
- payload level;
- semantic level;
- failure-mode level.

## Обязательные Точки Сравнения

| Точка | Что должно совпадать |
| --- | --- |
| Daily date | `date` остаётся тем же snapshot day |
| `count` | Количество items сохраняет смысл |
| `items` | Состав items сохраняется по смыслу |
| Item identity | `nm_id` остаётся главным item identity |
| Item fields | `name`, `vendor_code`, `view_count`, `open_card_count`, `ctr` не теряют смысл |
| Payload shape | Обязательные поля присутствуют и стабильны |
| `not_found` | `404/not found` остаётся отдельным non-fatal режимом |

## Что Считается Успехом

Успех:
- `date`, `count`, `items` и item-поля совпадают по смыслу;
- `detail` в `not_found` не теряет смысл;
- различия сведены только к target envelope и discriminator `kind`.

Неприемлемое расхождение:
- потеря item-полей;
- смена смысла `date`;
- смешение `not_found` с общей ошибкой;
- скрытая semantic drift в payload.

## Какие Evidence Нужны

Нужны:
- legacy samples для normal-case и `not_found`;
- target samples для normal-case и `not_found`;
- comparison по обоим режимам;
- evidence summary с итоговым выводом.

## Что Не Входит В Parity Этого Блока

Не входит:
- producer-side parity;
- performance;
- hardening;
- parity downstream business-логики.

