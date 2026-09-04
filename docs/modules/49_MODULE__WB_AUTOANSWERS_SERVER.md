# Module 49 — WB Autoanswers

## Назначение

Модуль синхронизирует отзывы Wildberries, готовит ответы через замороженный AI
bundle и публикует разрешённые ответы с обязательным readback.

Действующая бизнес-политика: [`../policies/WB_AUTOANSWERS_POLICY.md`](../policies/WB_AUTOANSWERS_POLICY.md).

## Поток

```text
WB GET → версии отзывов и медиа → processing job → bundle 1.4.2
→ server policy → publication job → один WB POST → обязательный WB GET
```

Служебная SQLite база хранит отзывы, неизменяемые версии, jobs, leases,
публикации, попытки, бюджеты, режимы и аудит. Идентичное содержимое не создаёт
новую версию. Ответ WB и служебное состояние наблюдения не входят в semantic
content hash.

## Код

- `packages/contracts/wb_autoanswers.py` — состояния и публичные форматы;
- `packages/application/wb_autoanswers_runtime.py` — хранилище и очередь;
- `packages/application/wb_autoanswers_sync.py` — синхронизация;
- `packages/application/wb_autoanswers_worker.py` — подготовка;
- `packages/application/wb_autoanswers_publication.py` — публикация и readback;
- `packages/application/wb_autoanswers_owner_policy.py` — server-owned guard;
- `packages/adapters/wb_autoanswers.py` — WB API;
- `packages/node/wb_autoanswers_v1_4_2/make_mvp/` — замороженный AI bundle;
- `apps/wb_autoanswers_readonly.py` и `apps/wb_autoanswers_lifecycle.py` —
  безопасные entrypoint.

## Инварианты

- `WB_AUTOANSWERS_FORCE_OFF=true` блокирует новые model calls и WB POST;
- режим Autoanswers принадлежит самому модулю, а не общему планировщику;
- paid call требует доступного атомарного резерва бюджета;
- один feedback version имеет не более одной publication aggregate;
- после начавшегося POST возможен только readback, не повторный POST;
- внешний ответ, устаревшая версия, неясное медиа и небезопасный текст блокируют
  автоматическую публикацию;
- тесты и recovery не выполняют реальных provider/WB-записей.

Активация новой политики для существующей очереди выполняется отдельной
dry-run/apply/readback операцией при остановленном worker. Она меняет только ещё
не начатые публикации и сохраняет историю и стоимость начатых операций.
