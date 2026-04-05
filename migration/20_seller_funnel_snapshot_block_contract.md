# Контракт Блока Seller Funnel Snapshot

## Что Это За Блок

`seller_funnel_snapshot block` — bounded migration unit, который отвечает за получение и выдачу server-side daily snapshot по seller funnel.

Он нужен новой системе как второй snapshot-like блок с уже видимым consumer-facing contract.

С него можно идти почти конвейером, потому что:
- contract уже вынесен на server-side;
- downstream shape уже стабилен;
- блок не требует раннего переноса table/runtime внутренностей.

## Границы Блока

В этот migration unit входит:
- consumer-facing contract `GET /v1/sales-funnel/daily`;
- semantics daily snapshot-а;
- item-level payload shape;
- режим `404/not found`.

В этот migration unit не входит:
- producer-side внутренний path;
- API/jobs вокруг блока;
- table apply-логика;
- перенос downstream business-модулей.

Пока остаётся в legacy:
- текущий producer/runtime path;
- текущие consumers этого daily snapshot;
- любые внутренние server-side детали формирования snapshot-а.

## Что Блок Должен Принимать

Блок должен принимать:
- запрос на daily snapshot;
- дату snapshot-а;
- явный сценарий `normal` или `not_found` для controlled checks.

Минимально обязательные сущности:
- `snapshot_type`
- `date`
- `scenario`

## Что Блок Должен Отдавать

Блок должен отдавать target envelope поверх seller funnel daily snapshot.

Минимально:
- `date`
- `count`
- `items`
- внутри item: `nm_id`, `name`, `vendor_code`, `view_count`, `open_card_count`, `ctr`
- отдельный `not_found` режим с `detail`

## Минимальная Parity Surface

Legacy и target сравниваются по:
- daily semantics поля `date`;
- `count`;
- составу `items`;
- semantics item-полей;
- отдельному режиму `not_found`;
- устойчивости downstream payload shape.

Нельзя потерять:
- `nm_id` как item identity;
- `name` и `vendor_code`;
- смысл `view_count`, `open_card_count`, `ctr`;
- различие между `not_found` и hard failure.

## Required Evidence

Чтобы считать блок корректным, нужны:
- legacy normal-case sample;
- legacy `not_found` sample;
- target samples для обоих режимов;
- parity comparison по normal-case и `not_found`;
- короткий evidence summary.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- producer-side refactor;
- network hardening;
- API/jobs;
- test framework;
- cutover downstream-модулей.

