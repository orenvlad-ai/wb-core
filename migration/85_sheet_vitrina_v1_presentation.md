# Sheet Vitrina V1 Presentation

## Что Это За Шаг

`sheet_vitrina_v1_presentation_block` фиксирует первый presentation/UI pass для новой Google Sheets-витрины.

Он меняет только визуальную подачу уже записанных листов и не меняет:
- server-side bundle;
- write bridge;
- payload данных;
- состав листов.

## Что Считается Обязательным Для V1

Presentation pass V1 обязан:
- делать `DATA_VITRINA` читаемым без изменения данных;
- делать `STATUS` читаемым как технический sidecar без изменения данных;
- оставаться поверх уже существующего full-overwrite data path;
- допускать delivery через direct Google Sheets API без deploy и без изменения bound write bridge;
- не вводить новую расчётную или orchestration-логику.

## Обязательные Визуальные Правила Для DATA_VITRINA

Для `DATA_VITRINA` обязательны:
- freeze колонок `A:B`;
- базовая ширина колонок `A` и `B`;
- читаемый header row;
- визуально выделенные date-columns `C..`;
- базовые number formats для:
  - процентов;
  - рублей;
  - целых и обычных чисел.

## Обязательные Визуальные Правила Для STATUS

Для `STATUS` обязательны:
- freeze header row;
- базовая ширина колонок;
- читаемый header row;
- базовое форматирование:
  - статусов;
  - date-like колонок;
  - coverage/count колонок.

## Что Сознательно Не Входит

В presentation pass V1 сознательно не входят:
- изменение данных;
- изменение bridge-логики;
- deploy Apps Script;
- новые листы;
- formulas;
- filter views;
- conditional formatting complexity;
- partial update semantics;
- перенос расчётных скриптов.
