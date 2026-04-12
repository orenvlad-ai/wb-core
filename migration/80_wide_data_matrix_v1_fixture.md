# Wide Data Matrix V1 Fixture

## Что Делает Этот Шаг

`wide_data_matrix_v1_fixture_block` — первый bounded implementation step для новой wide-by-date витрины.

Он делает три вещи:
- фиксирует machine-readable input bundle;
- фиксирует первый target fixture широкой матрицы;
- добавляет минимальный smoke, который проверяет форму и layout wide matrix.

## Какие Блоки Реально Наполнены

В V1 честно наполнены:
- `SKU` как главный рабочий блок;
- `TOTAL` как минимальный safe subset;
- `GROUP` как минимальный safe subset по уже известным `group`.

Внутри `TOTAL` и `GROUP` сейчас intentionally limited only:
- `stock_total`
- `ads_views`

Внутри `SKU` используется больший display-subset:
- `stock_total`
- `price_seller_discounted`
- `spp`
- `ads_views`
- `ads_ctr`
- `proxy_profit_rub`

## Что Остаётся Неполным

В V1 ещё не делается:
- полный набор строк `TOTAL`;
- полный набор строк `GROUP`;
- full legacy `DATA` parity;
- live Google Sheet / Apps Script wiring.

## Почему Это Честный Первый Implementation Step

Этот шаг уже не является только схемой, потому что:
- есть bounded input fixture;
- есть target fixture с wide-by-date формой `A=label`, `B=key`, `C..=dates`;
- есть проверяемые key-patterns `TOTAL|...`, `GROUP:...|...`, `SKU:...|...`;
- есть smoke, который подтверждает порядок строк и layout без внешних систем.
