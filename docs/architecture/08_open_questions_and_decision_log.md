# Open Questions And Decision Log

## Открытые Вопросы

| ID | Тема | Статус | Примечание |
| --- | --- | --- | --- |
| Q-01 | Какой именно будет первый функциональный перенос после foundation? | Открыт | Предварительный кандидат: web-source snapshot block. |
| Q-02 | Какова каноническая target storage model для facts, registries и snapshots? | Открыт | Reference-репозитории указывают на Postgres, но `wb-core` это ещё не зафиксировал. |
| Q-03 | Какова полная authoritative schema `METRICS` и живой словарь metric keys? | Открыт | Reference-code показывает только часть схемы. |
| Q-04 | Какие operator inputs останутся table-native в target-state? | Открыт | `CONFIG` и часть manual rules, вероятно, останутся, но final boundary ещё не зафиксирован. |
| Q-05 | Должен ли `AI_EXPORT` остаться compatibility contract или его заменит прямой server contract? | Открыт | Текущий ingest всё ещё зависит от него. |
| Q-06 | Кто является authoritative current producer для `GET /v1/search-analytics/snapshot`? | Открыт | Reference-репозитории показывают consumers и adjacent capture code, но не один окончательный producer path. |
| Q-07 | Какие operator-visible outputs обязательны для первых cutover кроме raw parity? | Открыт | Сейчас существуют `DATA`, отчёты и machine-readable export. |

## Незакрытые Решения

| ID | Решение | Состояние | Примечание |
| --- | --- | --- | --- |
| D-01 | Greenfield sidecar migration в `wb-core` | Принято | Зафиксировано в ADR-0001. |
| D-02 | Target-state — server-first modular monolith | Принято | Зафиксировано текущей архитектурой. |
| D-03 | Таблица становится thin operator shell | Принято | Новая таблица отложена, но принцип зафиксирован. |
| D-04 | Legacy остаётся maintenance-only | Принято | Никакой in-place cleanup campaign в legacy не планируется. |
| D-05 | Первый кандидат на cutover — web-source snapshot block | Предварительно | Сильный кандидат, но не финальное обязательство. |

## Provisional Assumptions

- `CONFIG.comment` сейчас фактически работает как human-readable SKU name.
- Group-level outputs пока важны, потому что `AI_EXPORT` и Apps Script оба кодируют group semantics.
- Postgres — наиболее вероятная начальная persistence target, потому что `wb-ai-research` уже использует его для facts, registry и supplies.
- Browser/web-source capture должен жить как adapter за server-owned snapshot contract.

Каждый такой assumption должен быть либо подтверждён, либо снят до соответствующего module cutover.
