# Wide Data Matrix Delivery Bundle V1

## Что Делает Этот Шаг

`wide_data_matrix_delivery_bundle_v1_block` фиксирует первый handoff-ready artifact между `wb-core` и будущей Google Sheets-витриной.

Он делает три вещи:
- берёт готовую wide matrix из `wide_data_matrix_v1`;
- раскладывает её в sheet-ready `DATA_VITRINA`;
- отдельно раскладывает `source_statuses` в sheet-ready `STATUS`.

## Чем Он Отличается От Fixture

`wide_data_matrix_v1_fixture_block` ещё описывает внутреннюю wide-shape матрицу.

`wide_data_matrix_delivery_bundle_v1_block` уже описывает delivery layer:
- с `delivery_contract_version`;
- со `snapshot_id`;
- с `as_of_date`;
- с двумя секциями `sheet_name + header + rows`.

## Почему Это Уже Handoff-Ready Artifact

Этот bundle уже можно использовать как вход для будущего sheet-side importer, потому что:
- `DATA_VITRINA` готова к range-write;
- `STATUS` готов к range-write;
- metadata верхнего уровня задают идентичность снапшота;
- ни Google Sheet, ни Apps Script для проверки shape не нужны.

## Что Ещё Остаётся До Первого Sheet-Side Каркаса

После этого шага ещё не сделаны:
- реальный Google Sheet;
- Apps Script importer;
- запись bundle в live ranges;
- orchestration доставки по расписанию.
