# Migration 127: immutable supply calculation registry

## Причина

Factory-order хранил только один перезаписываемый latest result. WB regional
дополнительно имел bounded metadata-only audit, но тот намеренно не содержал
выбранные supply ids, полный result payload или выгрузку. Поэтому оператор не
мог доказанно открыть и скачать точный прошлый расчёт.

## Additive schema

Runtime schema initialization создаёт
`sheet_vitrina_v1_supply_calculation_registry`:

- `record_id` / `calculation_id` и `calculation_type`;
- `completeness = complete | legacy_metadata`;
- `calculated_at`, `report_date`, `status`;
- canonical `payload_json`, bounded `metadata_json` и `payload_sha256`;
- optional exact historical `export_blob`, filename/content type и SHA-256;
- source/source identity и audit `created_at`.

Индексы задают immutable complete identity и стабильные list/filter reads по
`calculated_at DESC, record_id DESC` и
`calculation_type + report_date + calculated_at + record_id`.

Существующие latest tables и regional metadata audit не удаляются и не
переписываются.

## Write contract

Успешный factory-order или WB regional calculation в одном SQLite transaction:

1. валидирует безопасный `calculation_id` и canonical payload;
2. до transaction успешно формирует exact XLSX/ZIP, затем добавляет immutable
   complete registry row с exact evidence/export bytes;
3. обновляет соответствующий single-slot latest result;
4. применяет bounded retention.

Ошибка export-build не начинает write, а любая transaction error откатывает
и history, и latest. Точный повтор с совпадающими
canonical payload, evidence, calculated timestamp и export digest
идемпотентен. Та же identity с любым отличием fail-closed до latest update.

Retention хранит не более `200` complete rows globally. Оба record ids,
на которые указывают текущие factory/regional latest slots, защищены от
удаления даже при backdated timestamp. Legacy metadata rows не расходуют этот
лимит. List API использует default `limit=25`, maximum `100` и offset
pagination. Один complete row дополнительно fail-closed ограничен:
canonical payload `32 MiB`, metadata/evidence `8 MiB`, export `64 MiB`.

## Stored evidence boundary

Complete row хранит только bounded calculation truth:

- exact settings и выбранные WB supply ids;
- incident policy revision/status/digest;
- source/dataset states и canonical fingerprints;
- fingerprints фактически использованных sales samples/regional demand
  estimates, чтобы редкий конкурентный refresh source window не подменял
  evidence входов расчёта;
- warnings, summary и полный per-SKU/per-district result payload;
- exact XLSX/ZIP результата, сформированный при расчёте.

Secrets, credentials, browser/session state и лишние raw WB responses не
копируются. Historical download читает saved blob и не обращается к текущим
latest/result sources.

## Legacy compatibility

При schema initialization прежние
`sheet_vitrina_v1_wb_regional_supply_calculation_audit` rows идемпотентно
проецируются в unified registry с identity
`legacy-regional-audit:<audit id>` и `completeness=legacy_metadata`.
Сохраняется только существовавшая metadata; payload/export/выбранные ids не
изобретаются. UI/API отмечают такие rows как non-reproducible.

Никакой factory history не создаётся задним числом.

## Rollback

Application rollback безопасно игнорирует новую additive table; старые latest
и regional audit остаются совместимыми. Physical drop таблицы не является
частью runtime rollback и допустим только отдельной обслуживающей операцией
после доказанного отсутствия нужной history.

## Проверка

- `python3 apps/supply_calculation_registry_smoke.py`;
- `python3 apps/sheet_vitrina_v1_factory_order_http_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_regional_supply_http_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_auth_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`;
- `python3 apps/sheet_vitrina_v1_operator_ui_persistence_smoke.py`.
