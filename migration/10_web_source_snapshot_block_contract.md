# Контракт Блока Web-Source Snapshot

## Что Это За Блок

`web-source snapshot block` — это bounded migration unit, который отвечает за получение и выдачу server-side snapshot-данных из web-source контура.

Он нужен новой системе как первый изолированный блок, который:
- лежит на границе между browser/web acquisition и server-first core;
- уже имеет видимый legacy-контракт;
- не требует раннего переноса operator-table или business-модулей.

С него начинаем, потому что это самый ясный кандидат для strangler-step без big-bang migration.

## Границы Блока

В этот migration unit входит:
- contract получения snapshot-данных из web-source acquisition слоя;
- contract server-side snapshot output для downstream-потребителей;
- semantics snapshot identity, payload shape и failure-mode.

В этот migration unit не входит:
- browser automation как таковая;
- реализация acquisition runtime;
- API-ручки, jobs и ingestion;
- table apply-логика;
- перенос downstream business-модулей.

Пока остаётся в legacy:
- текущий browser/web capture;
- текущие Apps Script consumers;
- текущий server/runtime path, который фактически производит snapshot.

## Что Блок Должен Принимать

Блок должен принимать:
- запрос на получение snapshot определённого типа;
- snapshot scope и период, если они обязательны для конкретного snapshot;
- обязательный identity набора данных.

Минимально обязательные сущности и идентификаторы:
- `snapshot_type`
- `snapshot_date` или `date_from/date_to`
- `nm_id` как основной SKU identity внутри items
- version/metadata snapshot-а

Upstream-источники:
- web-source acquisition слой;
- browser/session-backed capture path или его будущий server-side replacement

Inference:
- точная форма внутреннего acquisition input может измениться.
- внешний downstream contract по snapshot semantics меняться самовольно не должен.

## Что Блок Должен Отдавать

Блок должен отдавать server-side snapshot contract, пригодный для downstream-слоёв.

Минимально:
- snapshot metadata;
- список `items`;
- устойчивую payload-shape;
- предсказуемый failure-mode.

Для search-analytics snapshot минимально значимые поля по legacy evidence:
- `date_from`
- `date_to`
- `items[]`
- внутри item: `nm_id`, `views_current`, `ctr_current`, `orders_current`, `position_avg`

Для downstream важно сохранить:
- day/slice semantics snapshot-а;
- item-level привязку к `nm_id`;
- числовые поля без потери смысла;
- различие между `snapshot not found` и hard failure.

## Минимальная Parity Surface

Legacy и новый блок сравниваются по:
- наличию и форме snapshot metadata;
- составу item-level данных;
- semantics полей `nm_id`, дат и числовых значений;
- поведению на отсутствии snapshot;
- стабильности payload shape для downstream consumer.

По смыслу должны совпадать:
- какой срез данных представляет snapshot;
- какие item-данные входят в выдачу;
- как интерпретируются даты и `nm_id`;
- что считается not found, а что считается ошибкой.

Нельзя потерять:
- `nm_id` identity;
- `date_from/date_to` semantics для search snapshot;
- meaning числовых полей;
- contract о non-fatal обработке `404/not found`.

## Required Evidence

Чтобы считать блок корректным, нужны:
- зафиксированный target contract;
- legacy contract sample или recorded fixture;
- parity matrix по полям и failure-mode;
- явное описание semantic transforms, если они есть;
- перечень downstream consumers, для которых этот contract критичен.

До следующего этапа должны быть проверены:
- какие snapshot variants входят в первый scope;
- какой legacy producer path считается reference для сравнения;
- какие поля обязательны, а какие допустимо отложить.

## Что Не Делаем В Рамках Этого Блока

Не делаем:
- реализацию browser capture;
- реализацию API;
- реализацию jobs;
- перенос apply-логики в таблицу;
- перенос `AI_EXPORT`, `CONFIG`, `METRICS`;
- новую таблицу;
- cutover downstream-модулей.

## Следующий Шаг После Этого Документа

Следующий шаг:
- создать в `wb-core` короткую parity matrix для `web-source snapshot block`, где по полям и failure-mode будет сопоставлен legacy contract и target contract.
