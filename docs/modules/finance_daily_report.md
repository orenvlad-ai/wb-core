# Дневной финансовый отчёт витрины

`fin_report_daily` принимает полный транзакционный отчёт продавца из
`POST /api/finance/v1/sales-reports/detailed`, `period=daily`, точная дата
`dateFrom=dateTo`. Отсутствие операции по SKU полного проверенного отчёта
позволяет нули только для аддитивных полей этого отчёта. Первый 204 без строк,
незавершённая пагинация, неизвестная identity, неверная дата или нечисловые
обязательные суммы оставляют источник unknown. Они не публикуют нули.

Pure proof `finance_daily_report_v1` связывает исходный digest, наблюдение,
пагинацию, rrDate, счётчики операций, roster, observed/no_activity и суммы.
Нормализованный payload проверяется повторно при live/cache admission,
в plan-report и внутри publication adapter. Legacy accepted payload остаётся
читаемым по прежнему договору, без переписывания старого дня. Plan-report
сохраняет собственный manual roster: только подтверждённое подмножество
полного Finance proof, без расширения его бизнес-области.

## Операции без nmId

Услуги продавца не распределяются по произвольным SKU. Поддержаны ровно
`nmId=0`/`"0"` и два значения `sellerOperName`:

- `Возмещение за выдачу и возврат товаров на ПВЗ`: все десять исходных полей
  текущего Finance расчёта нулевые; ppvzReward/vw/vwNds сохраняются отдельно.
- `Удержание`: из этих полей ненулевым допускается только deduction;
  ppvzReward/vw/vwNds должны быть нулевыми.

Для обоих обязательны конечные денежные значения, quantity/retailAmount/
forPay/retailPrice=0, пустые vendorCode/sku/title/brandName/subjectName/techSize,
положительный уникальный rrdId и пригодная точная rrDate. Транзакционные
srid/shkId/orderId могут присутствовать у услуги ПВЗ. Список rrdId и все суммы
сохраняются в `seller_operation_groups`, входят в source-facts proof и не
добавляются к SKU/TOTAL выкупа. Неизвестные виды операций остаются unknown.
Explicit source-bound basis для seller storage и date fallback не расширяется.

Предметные основания: [удержания WB](https://seller.wildberries.ru/instructions/ru/ru/material/retentions),
[услуги WB](https://seller.wildberries.ru/instructions/en/ru/material/service-terms).
Удержания не привязаны к конкретному заказу; услуги выдачи/возврата — отдельные
операции. Эти определения не служат разрешением распределять их по SKU.

## Публикация и переход

`finance_daily_publication_v1` в общем Production Apply принимает один
неизменяемый локальный source artifact, одну дату и существующий ready этой
даты (`as_of_date=date`, yesterday_closed). Read-only preview фиксирует roster
из этого ready, source/hash, ключи пяти SKU и шести TOTAL, STATUS и соседние
значения. Writer заново рассчитывает source proof/keys/суммы и сверяет CAS,
точный runtime, storage authority и controls. Он сохраняет before-image,
атомарно пишет ready, `accepted_closed_day_snapshot` и Finance closure success.
Чужие source slots и остальные даты не меняются. Existing accepted closed и
отличающиеся подтверждённые ячейки сохраняются; для их пересмотра нужен отдельный
договор. Readback идёт по той же operation identity; повторный submit запрещён.
Rollback допустим только при совпадении всех точных after-images.

Старый `finance_daily_historical_recovery` остаётся legacy-механизмом прежних
дат. Его `accepted_closed` и лимит171 не используются новым adapter.

Автоматическое reopening exhausted Finance включено только для source dates
с **12.09.2026** — граница перехода на новый договор. Selector возвращает их
в существующий next-business-day retry; другие exhausted sources не добавлены.
Даты старого инцидента08–11 восстанавливаются отдельно через проверяемые
Finance candidates. Это необходимо потому, что штатный retry caller обновляет
целую дату и не должен автоматически запускать общий ремонт старой истории.
Обычные pending/retrying и регулярное обновление продолжают прежний цикл.

Дневной Finance не является источником weekly/cost coverage, mature buyout
когорт, рекламных Proxy или нового финансового модуля.
