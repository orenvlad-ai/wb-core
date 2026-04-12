# Контракт доставки wide matrix в новую Google Sheets-витрину

## 1. Назначение delivery contract

Отдельный `delivery contract` нужен, чтобы жёстко развести:
- что именно формирует `wb-core`;
- что именно получает новая Google Sheets-витрина;
- где заканчивается server-side truth и начинается sheet-side отображение.

`wide_data_matrix_v1` fixture сама по себе ещё не равна рабочей витрине, потому что fixture:
- фиксирует shape и layout;
- подтверждает первый implementation artifact;
- но ещё не задаёт канонический handoff между сервером и Google Sheets.

## 2. Минимальный состав sheet-side

Для V1 достаточно двух листов:
- `DATA_VITRINA`
- `STATUS`

`DATA_VITRINA` нужен как основной read-side wide sheet.

`STATUS` нужен как отдельный технический слой для freshness, coverage и source-status.

Отдельный hidden/service sheet для V1 не обязателен.

Если позже понадобится техническая память импорта, её можно вынести в hidden service-layer отдельно, но в этот контракт она не входит.

## 3. Что именно должен отдавать сервер

Сервер должен отдавать:
- `wide matrix payload`;
- `status/freshness payload`;
- `delivery_contract_version`;
- `snapshot_id`;
- `as_of_date`;
- coverage и missing indicators.

Минимально обязательные server-side сущности:
- идентичность снапшота;
- sheet-ready matrix data;
- отдельный status sidecar;
- метаданные покрытия.

## 4. В каком виде wide matrix должна приходить

Для V1 каноническим вариантом фиксируется:
- `bundle из header + rows`

а не:
- opaque whole-sheet blob;
- и не live sheet-side pull of cell-by-cell values.

Канонический V1 format:
- один file-based delivery bundle в JSON;
- внутри два section-пакета:
  - `data_vitrina`
  - `status`

Минимальная форма:

```json
{
  "delivery_contract_version": "wide_matrix_delivery_v1",
  "snapshot_id": "2026-04-12__wide_matrix__v1",
  "as_of_date": "2026-04-05",
  "data_vitrina": {
    "sheet_name": "DATA_VITRINA",
    "header": ["label", "key", "2026-04-03", "2026-04-04", "2026-04-05"],
    "rows": [["...", "...", 1, 2, 3]]
  },
  "status": {
    "sheet_name": "STATUS",
    "header": ["source_key", "kind", "freshness", "snapshot_date", "requested_count", "covered_count", "missing_nm_ids"],
    "rows": [["stocks", "success", "2026-04-05", "2026-04-05", 3, 2, "210185771"]]
  }
}
```

Этот вариант выбран, потому что:
- он напрямую соответствует sheet-range записи;
- не требует раннего live API;
- не требует раннего Apps Script runtime;
- уже согласуется с текущим artifact-backed состоянием проекта.

## 5. Граница между server-side и sheet-side

На сервере формируется:
- wide matrix shape;
- порядок строк;
- key-patterns;
- значения по датам;
- блоки `TOTAL / GROUP / SKU`;
- status/freshness/coverage;
- snapshot identity и delivery version.

Табличный слой делает только:
- приём готового payload;
- запись `header + rows` в нужные листы;
- простое отображение результата.

Таблица просто отображает:
- готовую wide matrix;
- готовый status sidecar.

Пока не считается частью sheet-side:
- вычисление метрик;
- formula/ratio resolution;
- aggregation logic;
- registry resolution;
- orchestration;
- любые runtime decisions.

## 6. Что должно попадать в DATA_VITRINA

В `DATA_VITRINA` должен попадать:
- wide matrix как read-side view;
- колонка `A = label`;
- колонка `B = key`;
- колонки `C.. = dates`;
- блоки `TOTAL / GROUP / SKU`.

Для V1 допустимы:
- строки `SKU` как основной рабочий блок;
- `TOTAL` только для safe subset;
- `GROUP` только для safe subset;
- числовые значения;
- пустые ячейки там, где значение честно отсутствует.

Для V1 пока допустимо ограничение:
- неполный `TOTAL`;
- неполный `GROUP`;
- ограниченный набор метрик по сравнению с будущей полной витриной.

## 7. Что должно попадать в STATUS

В `STATUS` должны попадать:
- `source_key`;
- `kind/status`;
- freshness;
- `snapshot_date` и/или `date_from/date_to`;
- `requested_count`;
- `covered_count`;
- `missing_nm_ids`;
- при необходимости короткий `note`.

`STATUS` не должен смешиваться с `DATA_VITRINA`, потому что:
- это operational sidecar;
- он нужен для контроля качества доставки;
- он не должен ломать читаемость основной wide matrix.

## 8. Способ доставки

Для V1 фиксируется:
- `file-based / artifact-based handoff`

То есть канонический путь такой:
- `wb-core` формирует versioned delivery bundle;
- bundle хранит `DATA_VITRINA` и `STATUS` в sheet-ready формате;
- будущий тонкий sheet-side importer читает именно этот bundle.

Почему выбран именно этот вариант:
- это минимально рискованный путь при текущем состоянии проекта;
- он не требует раннего server API или Apps Script runtime;
- он продолжает уже принятую artifact-backed фазу;
- он keeps source-of-truth на стороне `wb-core`, а не в Google Sheet.

Для V1 не выбираются:
- live pull из Sheets в server API;
- server push в live sheet;
- cell-by-cell bridge.

## 9. Что сознательно НЕ входит

В этот контракт не входят:
- Apps Script реализация;
- реальный Google Sheet;
- перенос supply/report расчётов;
- orchestration;
- live registry UI;
- full legacy sheet reproduction.

## 10. Следующий практический шаг

Следующий bounded step:
- зафиксировать artifact-backed `wide_data_matrix_delivery_v1` bundle с двумя sheet-ready payload sections (`DATA_VITRINA` и `STATUS`) и добавить один локальный smoke, который проверяет `header + rows` contract без Apps Script и без live Google Sheet.
