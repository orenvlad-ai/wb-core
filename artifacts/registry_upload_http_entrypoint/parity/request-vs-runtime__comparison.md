# Request Vs Runtime Comparison

- Входом `registry_upload_http_entrypoint_block` выступает тот же канонический upload bundle, но уже как HTTP request body на `POST /v1/registry-upload/bundle`.
- HTTP entrypoint не дублирует ingest-логику: он только принимает JSON payload, делегирует его в `RegistryUploadDbBackedRuntime` и сериализует `RegistryUploadResult` обратно в HTTP response.
- Accepted request materialize-ит current server-side truth в runtime DB через уже существующий DB-backed runtime слой.
- Duplicate `bundle_version` возвращается как canonical rejected result c HTTP `409`, не двигая current state.
- Этот шаг добавляет первый внешний вызываемый boundary, но не добавляет Apps Script UI, deploy и production storage binding.
