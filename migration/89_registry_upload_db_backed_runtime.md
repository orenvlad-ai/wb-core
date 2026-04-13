# Registry Upload DB-Backed Runtime

## 1. Что Это Такое

`registry_upload_db_backed_runtime_block` — следующий bounded implementation step после `registry_upload_file_backed_service_block`.

Этот шаг добавляет локальный DB-backed runtime слой, который принимает upload bundle, валидирует его и materialize-ит current server-side truth уже не в JSON-store, а в persistent runtime DB.

## 2. Что Именно Покрывает Блок

Блок покрывает полный локальный runtime flow:
- принять уже собранный bundle;
- переиспользовать validator из `registry_upload_bundle_v1_block`;
- сохранить accepted version в DB-backed runtime storage;
- выставить current/active state для server-side truth;
- сохранить и вернуть upload result в канонической форме.

## 3. Runtime Storage Shape

Для bounded шага storage shape фиксируется так:
- локальный SQLite-файл `registry_upload_runtime.sqlite3` внутри runtime root;
- version table для принятых upload bundle;
- result table для materialized upload result;
- current-state pointer table;
- versioned item tables для `config_v2`, `metrics_v2`, `formulas_v2`.

Допущение bounded шага:
- SQLite используется только как минимальный локальный DB-backed analog server-side runtime;
- это не фиксирует production storage model и не отменяет открытый вопрос про final Postgres-backed target.

## 4. Чем Это Отличается От File-Backed Service

`registry_upload_file_backed_service_block` доказывал:
- file-backed accept/store/activate flow;
- current marker в JSON;
- upload result в JSON-store.

`registry_upload_db_backed_runtime_block` добавляет:
- persistent DB-backed runtime storage;
- current state как server-side truth, читаемый из DB;
- version history в runtime DB;
- runtime ingest слой, который становится прямой базой под будущий thin API/entrypoint.

## 5. Что Остаётся Следующим Шагом

Следующий bounded step после этого блока:
- добавить тонкий live entrypoint поверх уже доказанного DB-backed runtime слоя, не прыгая в Apps Script UI, operator workflow и deploy orchestration.
