# Module 48 — Складские остатки и стоимость

## Каноническая модель

Складской контур хранит шесть взаимоисключающих стадий движения товара, а также
количество, WAC и капитал по SKU. Идентичность SKU и склада берётся из
действующих реестров, а не из подписи интерфейса.

## Правила

- каждое движение имеет источник, рабочую дату и устойчивый идентификатор;
- одна единица одновременно относится только к одной стадии;
- сумма quantity и capital сохраняется при внутреннем перемещении;
- стоимость не переносится между SKU и не заменяется средним без отдельного
  подтверждённого правила;
- snapshot WB и FBS не подменяет ledger, а входит как внешний факт;
- отсутствующее доказательство остаётся отсутствующим.

## История и публикация

Закрытый день меняется только более точным фактом с происхождением. Обычное
обновление не перезаписывает историю текущим остатком. Большой пересчёт строит
кандидат отдельно, проверяет totals/non-target и переключает публикацию атомарно.

## Проверка

Минимальная проверка охватывает affected SKU/dates, общие количества и капитал,
границы стадий, качество источника и потребляющие финансовые показатели.

## Legacy-привязки FBS без official evidence

Старая активная `seller warehouse → FF` строка остаётся immutable. Если она была
создана до появления `official_office_id` и official evidence, её нельзя
перезаписывать SQL-запросом или создавать рядом вторую active mapping. Канонический
путь добавляет одну версию в
`sheet_vitrina_v1_wb_fbs_mapping_official_evidence_versions` и сохраняет:

- точные `mapping_id`, `seller_warehouse_id`, `facility_id` и mapping digest;
- before-image и его SHA-256;
- свежий immutable registry run, два совпавших чтения официальных WB warehouse и
  office endpoints, stable office ID и evidence digests;
- operation ID, candidate digest, actor и время единственной записи.

Preview блокируется, если mapping не имеет точную legacy-форму, seller warehouse
или facility не one-to-one, facility неактивен, официальный office ID изменился,
registry evidence старше 30 минут или persisted/live evidence расходятся. Apply
повторяет official read непосредственно перед транзакцией, сверяет reviewed
prestate/candidate и делает только один append. Raw mapping, facility, остатки,
движения, стоимость, lifecycle и WB не меняются.

Full-catalog FBS snapshot читает последнюю append-only evidence version как
effective official metadata поверх исходной mapping. Восстановление — отдельная
авторизуемая append-only версия из сохранённого before-image; UPDATE/DELETE не
используются. Production-вызов идёт через adapter
`wb_fbs_mapping_evidence_v1` общего one-submit launcher и завершается query-only
readback той же operation identity.
