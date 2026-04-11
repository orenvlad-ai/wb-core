# Pilot Registry Bundle Для Первой Витрины

## Зачем Нужен Этот Bundle

`registry/pilot_bundle/` — первый bounded implementation artifact для registry-layer новой витрины.

Это уже не просто схема:
- bundle лежит в Git как versioned source of truth;
- все четыре реестра и bridge-export существуют в machine-readable виде;
- есть минимальный smoke, который проверяет связи между display-layer и runtime semantics.

## Что Вошло В Pilot Scope

В bundle вошли:
- `5` SKU из тонкого `CONFIG_V2`;
- `12` метрик;
- `2` формулы;
- все три типа метрик:
  - `direct`
  - `formula`
  - `ratio`

Малый набор выбран специально:
- его можно проверить руками;
- он уже покрывает split между `METRICS_V2` и `metric runtime registry`;
- он не требует полной миграции legacy-листов или отдельной БД.

## Какие SKU И Метрики Взяты

SKU:
- `210183919`
- `210184534`
- `245720334`
- `259460529`
- `210185771`

Метрики:
- `stock_total`
- `price_seller_discounted`
- `spp`
- `ads_views`
- `ads_clicks`
- `ads_orders`
- `ads_sum_price`
- `fin_buyout_rub`
- `fin_commission`
- `ads_ctr`
- `proxy_profit_rub`
- `proxy_stock_value_rub`

## Как Bundle Должен Использоваться Дальше

Этот bundle нужен как pilot base для следующего bounded implementation шага:
- server-side код должен читать именно его, а не legacy `CONFIG/METRICS/FORMULAS` целиком;
- bridge-export показывает, как тонкий tabular subset переводится в нормализованные V2-реестры;
- runtime registry отдельно фиксирует вычислительную семантику и не смешивается обратно с display-слоем.

Следующий bounded шаг после этого bundle:
- подключить к нему первый локальный registry-consumer без БД и без UI.
