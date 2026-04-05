# Legacy Source Для Ads Bids

## Что Считается Legacy-Source

Для `ads_bids_block` legacy-source фиксируется так:
- current official-source chain `GET /adv/v1/promotion/count` + `GET /api/advert/v2/adverts?ids=...&statuses=9`;
- current RAW normalization semantics из `34_raw_ads_bids.js`;
- current APPLY semantics из `74_plugins_ads_bids.js`.

Именно эта связка сейчас задаёт downstream смысл `ads_bids` в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для `ads_bids` сейчас не подтверждён;
- raw path уже строит canonical `RAW_ADS_BIDS`;
- apply path однозначно фиксирует итоговые метрики на уровне `nmId` и даты.

## Какая Semantics Зафиксирована

Зафиксировано:
- из `promotion/count` извлекаются advert IDs;
- затем запрашиваются active adverts через `statuses=9`;
- raw path пишет строки:
  - `fetched_at`
  - `snapshot_date`
  - `nmId`
  - `advertId`
  - `bid_type`
  - `placement`
  - `bid_kopecks`
- apply выбирает latest `fetched_at`;
- по каждой паре `(date,nmId,placement)` берётся `max(bid_kopecks)`;
- итоговые метрики пишутся в рублях:
  - `ads_bid_search`
  - `ads_bid_recommendations`.

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Этот набор безопасно подтверждён repository evidence:
- `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`;
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.

## Какие Первые Два Sample Нужны

Первые два sample:
- normal-case sample на bootstrap `nmId` set;
- empty-case sample, где active campaigns не содержат запрошенный `nmId`.
