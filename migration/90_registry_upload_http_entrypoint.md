# Registry Upload HTTP Entrypoint

## 1. Что Это Такое

`registry_upload_http_entrypoint_block` — следующий bounded implementation step после `registry_upload_db_backed_runtime_block`.

Этот шаг добавляет первый живой inbound HTTP/API boundary для registry upload без Apps Script UI, deploy и production framework rollout.

## 2. Что Именно Покрывает Блок

Блок покрывает минимальный live flow:
- принять уже собранный bundle по HTTP;
- делегировать ingest в существующий `RegistryUploadDbBackedRuntime`;
- поставить `activated_at` на server-side;
- вернуть наружу канонический `RegistryUploadResult`.

## 3. HTTP Entrypoint Shape

Для bounded шага HTTP boundary фиксируется так:
- `POST /v1/registry-upload/bundle`
- request body = канонический upload bundle V1
- success response = канонический `RegistryUploadResult`
- duplicate response = канонический rejected `RegistryUploadResult` с HTTP `409`

Допущение bounded шага:
- для первого live boundary используется стандартный `http.server` как минимальный прозрачный inbound слой;
- это не фиксирует финальный production framework choice;
- auth, deploy и operator-facing trigger остаются вне scope этого блока.

## 4. Чем Это Отличается От DB-Backed Runtime

`registry_upload_db_backed_runtime_block` доказывал:
- persistent current truth в runtime DB;
- version history;
- upload result и current state внутри server-side runtime.

`registry_upload_http_entrypoint_block` добавляет:
- первый внешний живой вызов в этот runtime;
- thin request -> runtime -> response wiring;
- env-backed runtime path/config для live runner;
- integration smoke, который реально стучится в HTTP entrypoint.

## 5. Что Остаётся Следующим Шагом

Следующий bounded step после этого блока:
- добавить operator-facing trigger для отправки bundle из `VB-Core Витрина V1` в уже materialized HTTP entrypoint, не смешивая это с deploy, auth-hardening и production storage redesign.
