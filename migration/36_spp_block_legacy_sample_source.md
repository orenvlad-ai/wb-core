# Legacy Source Для Spp

## Что Считается Legacy-Source

Для `spp_block` legacy-source фиксируется так:
- current official-source path `GET /api/v1/supplier/sales?dateFrom=...`;
- current RAW normalization semantics из `40_raw_spp.js`;
- current APPLY semantics из `77_plugins_spp.js`.

Именно эта связка сейчас задаёт downstream смысл `spp` в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для `spp` сейчас не подтверждён;
- raw path уже строит canonical `RAW_SPP`;
- apply path пишет в `DATA` только метрику `spp`.

## Какая Semantics Зафиксирована

Зафиксировано:
- raw path берёт yesterday date в spreadsheet timezone;
- upstream вызов идёт в `GET /api/v1/supplier/sales?dateFrom={snapshot_date}`;
- из полученного массива остаются только sales rows с `saleDate == snapshot_date`;
- `spp` нормализуется в долю: `>1 => /100`, иначе как есть;
- по каждому `nmId` считается:
  - `spp_avg = sum(normalized_spp) / count`
  - `spp_count = count`
- apply пишет в `DATA` только `spp = spp_avg` для пары `date|nmId`.

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
- empty-case sample, где за дату нет sales rows по запрошенному `nmId`.
