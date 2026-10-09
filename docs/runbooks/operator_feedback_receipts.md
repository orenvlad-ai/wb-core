# Квитанции операторских ответов, жалоб и buyer pilot

Общий журнал — read-only представление сохранённых native записей. Он не
создаёт новые задания, не вызывает WB и не открывает источник через writable
repository. Доступ к каждому источнику, actor и cabinet/account ограничивается
до подсчёта, поиска, страницы и точного detail. Справочник SKU сохраняет свой
существующий actor filter.

## Что означает результат

- `feedback_reply` с `reply_draft_saved`: черновик сохранён; в WB не опубликован.
  Exact processing key/version и manual revision/hash принадлежат native AI store.
- Принятая публикация остаётся processing до сохранённого native audit readback,
  совпадающего по feedback ID, content version/hash и нормализованному answer hash.
- `feedback_complaint`: native submit job содержит selected IDs, actor, account,
  request key и digest выбранных operands. Это фиксируется под существующим
  native lock до запуска одного existing worker. Каждый подтверждённый target
  требует exact run ID и `row_submit_confirmed_success` в native attempts.
  Частичный batch не становится общим completed. Старый job без manifest не
  получает зелёную квитанцию; отсутствие доказательства не равно отсутствию записи.
- `buyer_support`: существующие allowlist, capabilities, stale guards и отдельная
  native SQLite остаются authority. Chat completion требует saved unique event
  correlation; return completion — exact archived action/status. Unknown outcomes
  дают только чтение той же операции; draft/propose не означает отправку.

## Потерянный ответ

Actual manual reply / complaint forms сохраняют browser attempt fence до POST.
После неоднозначного ответа они читают `/v1/sheet-vitrina-v1/operations/feedback`
с тем же domain и native ID. Reload не снимает fence. Полученный receipt обязан
совпасть с native ID и ожидаемым reply hash/publication/media revision. Чужая,
отсутствующая или несовпадающая запись не даёт подтверждения. Buyer pilot сохраняет
свой существующий request-ID recovery; общий компонент только показывает его proof.

Жалобы с одним request key и другим actor/account/payload получают конфликт.
Worker-owned job восстанавливается чтением. Busy без собственного durable ID
означает «не принято»; busy чужого задания не раскрывает его ID. Keyed manifests
не удаляются с legacy tail100. Native store явно блокирует новые записи при
5000 jobs / 8 MiB; он не заменяет повреждённый inventory пустым списком.
Для растущего operational store нужен отдельный owner-approved retention путь;
общая проекция не архивирует и не переподаёт внешние операции.

## Локальная проверка

`apps/operator_feedback_operations_smoke.py` использует actual repositories,
worker и fake WB transports. `apps/operator_feedback_forms_browser_smoke.py`
выполняет actual UI functions → native HTTP → source commit и намеренно теряет
POST responses. Реальные WB/portal работы не запускаются. Дополнительно сохранить
native publication / buyer HTTP/browser / complaints и common journal/component
regressions. CI declaration и release принадлежат root задачи WBC0137.
