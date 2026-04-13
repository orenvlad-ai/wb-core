# Initial Evidence: sheet_vitrina_v1_registry_seed_v3_bootstrap_block

- Источник bounded seed взят из локального пакета `wbcore_registry_seed_v3_json.zip` и приведён к runtime-compatible compact subset внутри repo.
- `prepareRegistryUploadOperatorSheets()` теперь не только materialize-ит листы `CONFIG / METRICS / FORMULAS`, но и заполняет их compact v3 seed значениями.
- Service/control block `CONFIG!H:I` сохраняется при повторной подготовке листов и не теряет `endpoint_url` и `last_*` значения.
- Новый smoke подтверждает связку:
  - `prepare -> compact v3 seed -> build bundle from sheets`;
  - `prepare -> preserve control block -> upload via existing HTTP entrypoint`;
  - `upload -> DB-backed current truth`.
- Подтверждённые команды:
  - `python3 apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py`
  - `python3 apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py`
  - `python3 apps/registry_upload_http_entrypoint_smoke.py`
