# Official API Secret Boundary

## Какие Секреты Нужны Official-API Family

Для official-API family минимально нужны:
- API tokens;
- при необходимости отдельные upstream-specific base URLs или host overrides, если они считаются environment-specific runtime config.

В текущем repo norm для official-API family required secret — `WB_API_TOKEN`.
Он является canonical runtime path для current WB adapters в `sheet_vitrina_v1` refresh contour:
- `prices_snapshot_block`
- `sf_period_block`
- `spp_block`
- `ads_bids_block`
- `stocks_block`
- `sales_funnel_history_block`
- `ads_compact_block`
- `fin_report_daily_block`

`web_source_snapshot_block` и `seller_funnel_snapshot_block` не используют direct WB token path: они ходят в repo-owned `api.selleros.pro` contour.

## Что Хранится Только Вне Git

Вне Git хранятся только:
- secret values;
- environment-specific runtime values, если они дают доступ к private upstream path.

В Git допустимо хранить только:
- env variable names;
- required/optional shape;
- default non-secret timeout values;
- documented runtime boundary.

## Чем Отличаются Local И Server-Side Secret Layers

`local secret layer`:
- используется для developer preflight и bounded smoke;
- может быть неполным;
- не считается доказательством production reachability.

`server-side secret layer`:
- используется для authoritative live-source execution;
- должна управляться вне репозитория;
- должна предоставлять тот же secret interface, что и local layer.

## Что Должен И Чего Не Должен Знать Модуль

Модуль должен знать только:
- какие секреты ему нужны по имени runtime contract;
- какие runtime параметры обязательны для запроса;
- какие ошибки вернуть, если runtime boundary не собран.

Для current official-API contour это значит:
- default token env key в repo code должен быть один: `WB_API_TOKEN`;
- legacy names вроде `WB_TOKEN` / `WB_AUTH_TOKEN` / `WB_SUPPLIES_API_TOKEN` не должны оставаться hidden runtime fallback inside adapters;
- если какой-то endpoint реально требует другой token type/category и не работает от canonical token, это должно быть отдельным documented exception, а не silent branch в runtime loading.

Модуль не должен знать:
- из `.env`, shell env, secret manager или process supervisor пришёл секрет;
- как именно secret provisioned в server-side среде;
- какие operator steps использованы для его доставки.

Следствие: module adapter получает secret только через runtime boundary, а не строит собственную secret-loading схему.
