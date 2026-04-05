# Legacy Source Для Stocks

## Что Считается Legacy-Source

Для `stocks_block` legacy-source фиксируется так:
- current official-source path `POST /api/v2/stocks-report/products/sizes`;
- current RAW normalization semantics из `31_raw_stocks.js`;
- current APPLY semantics из `72_plugins_stocks.js`.

Именно эта связка сейчас задаёт downstream смысл stocks в табличном legacy-контуре.

## Почему Выбран Именно Он

Он выбран потому, что:
- отдельный consumer-facing HTTP contract для stocks сейчас не подтверждён;
- raw path уже фиксирует canonical row semantics;
- apply path однозначно задаёт `stock_total`, региональные `stock_*` и coverage guard.

## Какая Semantics Зафиксирована

Зафиксировано:
- upstream вызывается по одному `nmID` за раз;
- raw path парсит offices/sizes/products variants в единый row shape;
- latest `snapshot_ts` per `(date,nmId)` считается authoritative;
- `stock_total` = сумма `stockCount` по всем offices latest snapshot;
- region mapping:
  - `Центральный -> stock_ru_central`
  - `Северо-Западный -> stock_ru_northwest`
  - `Приволжский -> stock_ru_volga`
  - `Уральский -> stock_ru_ural`
  - `Южный + Северо-Кавказский -> stock_ru_south_caucasus`
  - `Дальневосточный + Сибирский -> stock_ru_far_siberia`
- apply отменяется, если coverage по `nmId` неполный.

## Как В Bounded Checkpoint Сохранён Guard

В `wb-core` cursor/staging не переносится буквально.

Вместо этого сохраняется эквивалентный bounded guard:
- snapshot считается publishable только если для всего requested `nmId` set есть coverage;
- если covered `nmId` меньше requested `nmId`, block возвращает `kind: "incomplete"` и не считается success checkpoint.

Это сохраняет downstream смысл целостного snapshot без копирования Apps Script orchestration 1:1.

## Откуда Берётся Bootstrap `nmId`

В `wb-core` нет live `CONFIG`, поэтому checkpoint использует bootstrap sample set из уже известных проекту SKU:
- `210183919`
- `210184534`

Этот набор безопасно подтверждён repository evidence:
- `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`;
- `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.
