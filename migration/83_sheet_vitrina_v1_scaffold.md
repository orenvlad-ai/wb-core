# Sheet Vitrina V1 Scaffold

## Что Это За Шаг

`sheet_vitrina_v1_scaffold_block` фиксирует первый минимальный технический каркас новой Google Sheets-витрины.

Он не создаёт реальную таблицу и не пишет Apps Script. Его задача:
- закрепить состав листов V1;
- закрепить layout этих листов;
- закрепить первый write plan для sheet-side bridge.

## Состав Листов V1

В V1 фиксируются только:
- `DATA_VITRINA`
- `STATUS`

`DATA_VITRINA` получает основную wide-by-date матрицу.

`STATUS` получает freshness, coverage и source-status sidecar.

## Что Должен Писать Sheet-Side Bridge

Sheet-side bridge должен:
- брать готовый delivery bundle из `wb-core`;
- писать `DATA_VITRINA` целиком как `header + rows`;
- писать `STATUS` целиком как `header + rows`;
- считать обе записи полным overwrite без partial update semantics.

## Что Пока Сознательно Не Входит

В этот scaffold пока не входят:
- реальный Google Sheet;
- Apps Script runtime;
- supply/report скрипты;
- операторские листы;
- orchestration доставки;
- partial update logic.
