# Registry Upload File-Backed Service

## 1. Что Это Такое

`registry_upload_file_backed_service_block` — следующий bounded implementation step после `registry_upload_bundle_v1_block`.

Этот шаг добавляет локальный file-backed аналог server-side приёмника V2-реестров без раннего API, live DB storage и operator-side UI.

## 2. Что Именно Покрывает Блок

Блок покрывает полный локальный flow:
- принять уже собранный bundle;
- переиспользовать validator из `registry_upload_bundle_v1_block`;
- materialize-ить принятую version artifact;
- выставить current/active marker;
- вернуть upload result в канонической форме.

## 3. File-Backed Store Shape

Канонический store layout фиксируется так:
- `accepted/<bundle_version_filename>.json`
- `results/<bundle_version_filename>.json`
- `current/registry_upload_current.json`

Где:
- accepted artifact — точная копия принятого bundle;
- result artifact — upload result по форме `migration/86_registry_upload_contract.md`;
- current marker — указатель на текущую активную bundle-версию и её result file.

Допущение этого bounded шага:
- для имени файла `bundle_version` нормализуется только на файловом уровне: `:` заменяется на `-`;
- внутри JSON само значение `bundle_version` остаётся каноническим и неизменным.

## 4. Чем Это Отличается От Bundle V1

`registry_upload_bundle_v1_block` доказывал только:
- сборку bundle;
- локальную contract-validation;
- artifact-backed smoke без ingest.

`registry_upload_file_backed_service_block` добавляет:
- приём bundle как payload;
- file-backed accept/store/activate semantics;
- materialized upload result;
- local current-marker слой как прямой bridge к будущему server ingest/runtime.

## 5. Что Остаётся Следующим Шагом

Следующий bounded step после этого блока:
- добавить live server-side ingest entrypoint и DB-backed/runtime storage вокруг уже доказанного file-backed upload flow, не прыгая сразу в Apps Script UI и operator orchestration.
