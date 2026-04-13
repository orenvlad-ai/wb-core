# Input Vs Storage Comparison

- Входом `registry_upload_file_backed_service_block` выступает уже собранный bundle из `artifacts/registry_upload_file_backed_service/input/registry_upload_bundle__fixture.json`.
- При успешном upload accepted version materialize-ится как точная копия bundle под versioned filename в `accepted/`.
- Filename storage-слоя нормализует `bundle_version` только для имени файла: `:` заменяется на `-`, но само значение `bundle_version` внутри JSON не меняется.
- Upload result materialize-ится отдельно в `results/` и повторяет каноническую форму из `migration/86_registry_upload_contract.md`.
- Current marker живёт в `current/registry_upload_current.json` и указывает на текущий active bundle и его upload result относительными путями внутри storage root.
- Повторная попытка загрузить уже принятый `bundle_version` возвращает `rejected` result и не перезаписывает accepted/current state.
