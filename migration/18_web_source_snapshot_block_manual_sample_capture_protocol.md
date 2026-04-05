# Manual Sample Capture Protocol Блока Web-Source Snapshot

## Зачем Нужен Capture Protocol

Этот protocol нужен, чтобы не собрать первые samples в разном формате и не потерять contract shape уже на первом ручном шаге.

## Какие Два Sample Собираем Первыми

Собираем:
- normal-case sample;
- `not found` sample.

Оба относятся к `GET /v1/search-analytics/snapshot`.

## Какой Ручной Способ Сбора Используем

Используем ручной сбор на уровне готового HTTP-ответа snapshot contract.

Сохраняем:
- полный response body;
- HTTP status;
- дату или period context, если он виден из ответа.

Сохраняем в виде:
- сырого JSON payload для response body;
- краткой пометки рядом, какой это сценарий: `normal` или `not-found`.

Полностью должны быть сохранены:
- top-level shape;
- `date_from` / `date_to`, если они есть;
- `items[]`;
- все поля item-ов, которые пришли в response;
- признак `404/not found`, если это failure-case.

## Куда Потом Кладём Каждый Sample

Соответствие слотов:
- normal-case legacy sample -> `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`
- `not found` legacy sample -> `artifacts/web_source_snapshot_block/legacy/not-found__template__legacy__fixture.json`

На этом шаге target, parity и evidence файлы не заполняются.

## Какие Минимальные Правила Обязательны При Ручном Сборе

Обязательно:
- не редактировать payload руками;
- не смешивать разные даты и сценарии;
- не выкидывать поля из response body;
- явно помечать сценарий как `normal` или `not-found`;
- сохранять sample так, чтобы исходный contract shape не был потерян.

## Что Не Входит В Этот Protocol

Не входит:
- автоматизация сбора;
- browser-capture logic;
- target sample capture;
- parity comparison;
- evidence summary;
- любая реализация нового модуля.

## Следующий Шаг После Этого Документа

Следующий шаг:
- вручную собрать и зафиксировать первый legacy normal-case sample в `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`.
