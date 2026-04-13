# Input Vs Runtime Comparison

- Входом `registry_upload_db_backed_runtime_block` выступает уже собранный bundle из `artifacts/registry_upload_db_backed_runtime/input/registry_upload_bundle__fixture.json`.
- Runtime storage materialize-ится как локальный SQLite-файл `registry_upload_runtime.sqlite3` внутри runtime root.
- Accepted upload не пишет versioned JSON как source of truth: каноникой bounded runtime шага становится current state, реконструируемый из DB-backed storage.
- Upload result сохраняется в таблице `registry_upload_results` и читается обратно в той же канонической форме `migration/86_registry_upload_contract.md`.
- Current truth читается через pointer `registry_upload_current_state`, а bundle-данные реконструируются из versioned таблиц `config_v2`, `metrics_v2`, `formulas_v2`.
- Повторная попытка ingest уже принятого `bundle_version` возвращает `rejected` result и не двигает DB-backed current state.
