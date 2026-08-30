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
- `wb_prices_management_block` / operator section `Цены`
- `wb_feedbacks` / `sheet_vitrina_v1_feedbacks` read-only route
- `wb_content` / `Настройки -> Номенклатура` read-only SKU/card sync from WB Content cards

`web_source_snapshot_block` и `seller_funnel_snapshot_block` не используют direct WB token path: они ходят в repo-owned hosted runtime contour. Active hosted target is `wb-core-eu-root` / `89.191.226.88`, with `api.selleros.pro` allowed as the current live DNS name; archived `selleros-root` / `178.72.152.177` is rollback/read-only evidence only, and mutating deploy/apply-nginx/restart/update/GC paths must fail fast unless the explicit emergency rollback override is set.

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

Для feedbacks MVP это означает: `GET /v1/sheet-vitrina-v1/feedbacks` использует тот же `WB_API_TOKEN` через adapter boundary `packages/adapters/wb_feedbacks.py`; отсутствие прав WB token на категорию feedbacks должно surface-иться как явная 401/403 upstream error, а не как fallback на другой secret name.

Для `Цены` / WB Prices and Discounts management это означает: `GET/POST /v1/sheet-vitrina-v1/prices/...` использует тот же `WB_API_TOKEN` через `packages/adapters/wb_prices_management.py` к `https://discounts-prices-api.wildberries.ru`. Optional base URL override is `WB_PRICES_API_BASE_URL`. Ordinary live price write is a separate server-side safety flag, `WB_PRICES_WRITE_ENABLED`, not a token source; when it is absent/false, preview and readback may work but `POST /v1/sheet-vitrina-v1/prices/upload-task` must fail closed before any WB `POST /api/v2/upload/task`. `Цены -> Проверка СПП` has an additional non-secret guard `WB_SPP_TEST_ENABLED`; its live start/restore writes require both `WB_SPP_TEST_ENABLED=true` and `WB_PRICES_WRITE_ENABLED=true` plus explicit operator confirmations. Tests/smokes must use fake upstreams and must not perform live price mutations.

Отдельная покупательская сессия `Цены -> Проверка СПП` является server-side secret material, но не official API token и не Seller Portal state. Она хранится только под `/opt/wb-core-runtime/state/wb_buyer_session`: directory `0700`, files `0600`, atomic replace, independent lock. Код не читает и не копирует `/opt/wb-web-bot/storage_state.json`. Первый валидный buyer account закрепляется только через HMAC-SHA256 fingerprint с runtime-random key; raw phone/email/name/account id не сохраняются и не возвращаются. API/UI/audit/log surfaces expose only bounded status, safe reason, irreversible fingerprint and sanitized price/context fields; cookies, authorization headers, raw browser storage, recovery connection material and internal paths are forbidden. Buyer-session loss fails closed before the next seller write, with no anonymous-price fallback; already required seller restore still uses WB seller tuple truth independently.

Для nomenclature SKU sync это означает: `POST /v1/sheet-vitrina-v1/settings/nomenclature/barcode-sync` использует тот же `WB_API_TOKEN` через read-only adapter boundary `packages/adapters/wb_content.py` к WB Content `POST /content/v2/get/cards/list`, читает карточки cursor pagination и синхронизирует только локальные reference-поля (`nm_id`, non-manual `barcode/barcodes`, `vendor_code`, WB title/subject/updatedAt and sync evidence). `POST /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}/barcode-sync` остаётся совместимым per-row barcode reference route. Optional base URL override is `WB_CONTENT_API_BASE_URL`; отсутствие token, Content permission, rate-limit или upstream transport failure surface-ится как controlled `token_missing`/`sync_error` diagnostics without printing token material and does not reject saving nomenclature rows. WB Content sync is read-only and must not create/update/delete WB cards.

Для WB regional supply planning это означает: `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options` использует тот же `WB_API_TOKEN` через read-only `packages/adapters/wb_supplies.py` boundary к WB FBW `POST /api/v1/acceptance/options`, `GET /api/v1/warehouses`, `GET /api/v1/transit-tariffs` and Common/Tariffs `GET /api/tariffs/v1/acceptance/coefficients` / `GET /api/v1/tariffs/box`. `acceptance/options` вызывается только как SKU/quantity availability query; никаких create/slot booking/price/stock mutations нет. HTTP diagnostics сохраняют request ID, counts and sanitized shape, but never token or full barcode lists. Optional base URL overrides stay `WB_SUPPLIES_API_BASE_URL`, `WB_MARKETPLACE_API_BASE_URL` and `WB_TARIFFS_API_BASE_URL`. Missing token, permission errors, rate limits and non-JSON upstream failures surface as controlled fail-closed blockers/warnings.

Для `Управление SKU` seller-price и exact placement-bid writes выполняются только через существующие backend WB adapters и canonical `WB_API_TOKEN`; browser payload never receives the token or a WB write URL. Normal runtime этого section write-capable без дополнительного disabled-by-default flag, но каждый action ограничен section authorization, one target, stored preview, explicit confirmation, stale/safety/quarantine validation, single-use commit claim, sanitized audit и matching post-write readback. Это не ослабляет secret/logging boundary и не разрешает bulk/automatic mutations.

`Управление SKU → Баланс запасов` не получает собственный token path или WB write URL. Его versioned `live_wb` boundary использует incumbent guarded Ads source только server-side, canonical seller binding, `InternalWriterRegistry`, exact immutable manifest, owner confirmation, fresh campaign/current/minimum preflight, canary, bounded documented bid PATCH и matching readback. Тот же durable apply job может содержать отдельные typed campaign-state items: official Promotion `start` разрешён только из status `4|11`, reversible `pause` — только из `9`; до единственного call обязательны exact-one nmID, raw-status/state CAS, payment/placement evidence, а после — exact state readback. Seller-side increase/ceiling thresholds могут быть только human-readable warnings внутри typed owner-confirmed Balance policy; standalone Ads/SKU остаётся strict, а identity/CAS/WB minimum/status/payment/placement и transport-uncertainty guards не ослабляются. Browser не получает token/URL и не вызывает WB PATCH/start/pause; ambiguous transport разрешается только query-only reconciliation без blind resubmit. Deploy/activation не создаёт apply job и не выполняет seller mutation.
