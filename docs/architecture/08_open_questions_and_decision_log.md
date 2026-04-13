# Open Questions And Decision Log

## Открытые Вопросы

| ID | Тема | Статус | Примечание |
| --- | --- | --- | --- |
| Q-01 | Какой именно будет первый функциональный перенос после foundation? | Закрыт | Исторически первым functional migration шагом стал `web_source_snapshot_block`; текущий `main` уже находится существенно дальше этой точки. |
| Q-02 | Какова каноническая target storage model для facts, registries и snapshots? | Открыт | В `main` уже есть локальный SQLite-backed runtime шаг для registry upload, но это не фиксирует окончательное production storage решение. |
| Q-03 | Какова полная authoritative schema `METRICS` и живой словарь metric keys? | Открыт | В текущей линии уже есть `migration/75`, `migration/76`, `migration/78`, `migration/86`, `migration/87`, `migration/88`, `migration/89`, `migration/90`, `migration/91`, `migration/92`, `migration/93`; bounded MVP-safe subset и первый reverse-load уже materialize-ятся в `main`, но окончательный authoritative metric dictionary и full parity beyond MVP-safe subset ещё не зафиксированы. |
| Q-04 | Какие operator inputs останутся table-native в target-state? | Открыт | `CONFIG` и часть manual rules, вероятно, останутся, но final boundary ещё не зафиксирован. |
| Q-05 | Должен ли `AI_EXPORT` остаться compatibility contract или его заменит прямой server contract? | Открыт | Текущий ingest всё ещё зависит от него. |
| Q-06 | Кто является authoritative current producer для `GET /v1/search-analytics/snapshot`? | Открыт | Reference-репозитории показывают consumers и adjacent capture code, но не один окончательный producer path. |
| Q-07 | Какие operator-visible outputs обязательны для первых cutover кроме raw parity? | Открыт | Сейчас существуют `DATA`, отчёты и machine-readable export. |
| Q-08 | Каким будет первый server-side ingest/runtime step для registry upload path после `registry_upload_bundle_v1_block`? | Закрыт | Bounded chain уже дошёл до `registry_upload_file_backed_service_block`, `registry_upload_db_backed_runtime_block`, `registry_upload_http_entrypoint_block`, `sheet_vitrina_v1_registry_upload_trigger_block`, `sheet_vitrina_v1_registry_seed_v3_bootstrap_block` и `sheet_vitrina_v1_mvp_end_to_end_block`; следующий отдельный gap уже лежит не на стороне ingest/runtime, а на стороне full parity, repo-owned stable hosted runtime и production hardening. |

## Незакрытые Решения

| ID | Решение | Состояние | Примечание |
| --- | --- | --- | --- |
| D-01 | Greenfield sidecar migration в `wb-core` | Принято | Зафиксировано в ADR-0001. |
| D-02 | Target-state — server-first modular monolith | Принято | Зафиксировано текущей архитектурой. |
| D-03 | Таблица становится thin operator shell | Принято | Новая таблица отложена, но принцип зафиксирован. |
| D-04 | Legacy остаётся maintenance-only | Принято | Никакой in-place cleanup campaign в legacy не планируется. |
| D-05 | `web_source_snapshot_block` был первым functional migration шагом | Реализовано | Исторический вопрос закрыт: `main` уже содержит и более поздние migration-линии. |

## Provisional Assumptions

- `CONFIG.comment` сейчас фактически работает как human-readable SKU name.
- Group-level outputs пока важны, потому что `AI_EXPORT` и Apps Script оба кодируют group semantics.
- Postgres — наиболее вероятная начальная persistence target, потому что `wb-ai-research` уже использует его для facts, registry и supplies.
- Browser/web-source capture должен жить как adapter за server-owned snapshot contract.

Каждый такой assumption должен быть либо подтверждён, либо снят до соответствующего module cutover.
