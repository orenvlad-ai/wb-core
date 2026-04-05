# Official API Secret Boundary

## Какие Секреты Нужны Official-API Family

Для official-API family минимально нужны:
- API tokens;
- при необходимости отдельные upstream-specific base URLs или host overrides, если они считаются environment-specific runtime config.

В текущем evidence для `prices_snapshot_block` required secret — `WB_TOKEN`.

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

Модуль не должен знать:
- из `.env`, shell env, secret manager или process supervisor пришёл секрет;
- как именно secret provisioned в server-side среде;
- какие operator steps использованы для его доставки.

Следствие: module adapter получает secret только через runtime boundary, а не строит собственную secret-loading схему.
