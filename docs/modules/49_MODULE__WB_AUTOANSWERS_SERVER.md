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


## Восстановление после пауз и технических сбоев

Полный официальный unanswered inventory читается без даты начала, по одной
странице: первый tick новой версии recovery, смена policy epoch, перерыв worker
более трёх минут, ручная sync-команда и затем каждые 12 worker ticks. Только
complete/count-matched список возрастом до 15 минут разрешает автоматическое
восстановление. Кандидаты ограничены 25 на tick и глубиной очереди. Наличие
локального `answer_text=''` само по себе не разрешает новый ответ.

Текущий автоматический режим допускает отзывы, прочитанные во время OFF/manual,
и перепривязывает ещё не завершённую автоматическую работу к текущему epoch.
Старые известные технические terminal failures и подтверждённые оборванные
provider boundaries переходят на существующий `safe_public_template` с тем же
processing key. Попытки, аудит и расходы сохраняются. Ручные jobs, semantic /
owner-policy отказы, неопределённое медиа и publication aggregates автоматически
не переоткрываются. Старый sweep другого epoch не ограничивает сегодняшнюю
steady-очередь.

429, 5xx и ошибки сети/ответа провайдера получают общий persistent cooldown,
экспоненциальный backoff 60–960 секунд и максимум две paid attempts для ключа;
после этого действует существующая безопасная политика. Исходный Node runner
не экспортирует provider Retry-After: Python boundary классифицирует его
существующие ошибки, а повтор регулирует общий bounded cooldown. Quota failures имеют
cooldown минимум 900 секунд. Исторический terminal quota восстанавливается
только после явного нового ON epoch или доказанного более позднего AI success.
Auth/config ошибки остаются видимыми блокерами. Cooldown блокирует paid claims,
но сохраняет WB GET, readback и бесплатную подготовку/публикацию.

Подтверждённый HTTP 429 и невозможность запуска Node (`ENOENT`) не считаются
неизвестным расходом. Реально неизвестная граница получает conservative capped
hold, а не нулевой расход. После учёта всех таких границ снимается только latch
`budget_state_unknown`; holds продолжают уменьшать доступные бюджеты. Неучтённая
граница вне свежего списка, ручной задачи или очередной ограниченной пачки
сохраняет блокировку paid work.

Перед каждым первым WB POST worker делает fresh detail GET, проверяет identity,
внешний ответ, `wbRu` и semantic content hash. Сбой GET допускает повтор GET.
После durable write marker повторный POST запрещён; выполняется только readback.
Старый локальный unanswered tail, отсутствующий в свежем inventory, сверяется
по два detail GET на tick: только подтверждённый ответ/`wbRu` изменяет наблюдение.
Rating-only `wbRu` не попадает в этот tail. UI отдельно показывает текущую
сверенную очередь WB, её свежесть и unresolved technical/policy/manual work;
исторический знаменатель старого запуска не заменяет этот счётчик.

## Зарегистрированное exact-cohort recovery

`wb_autoanswers_recovery_v1` работает через штатный
`apps/production_apply_launcher.py`. Общий launcher не изменён. Транспорт
ограничен active primary target `wb-core-eu-root`, canonical state directory и
точным SHA из trusted main, подтверждённым runtime/deploy metadata. Trusted SSH
transport явно включает `WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true` только для
процесса зарегистрированного adapter; allowlist env loader не расширяется.

Read-only capture request: `{"capture_only":true}`. Запускать только preview;
полный `wb_autoanswers_t0_manifest_v1` находится в `receipt.scope.manifest`.
Далее создать отдельную операцию с exact request:

```json
{
  "manifest": "<полный объект wb_autoanswers_t0_manifest_v1 из capture>",
  "approval_reference": "<точная ссылка на разрешение владельца для этого cohort>",
  "recovery_reference": "/opt/wb-core-runtime/state/backups/private-evidence/<task>/before.sqlite3"
}
```

`manifest` — объект, а не строка. Review preview, затем передать его
`prestate_sha256` и `candidate_sha256` в apply без изменения request. Apply
повторно проверяет полный стабильный WB inventory, details, backup, mode,
неопределённые бюджеты и fingerprint под общим worker control lock. Durable
exclusive operation marker записывается до первого domain mutation. Повтор
этой операции разрешает только readback; частичный/неясный результат не
отправляет apply заново.

Readback подтверждает exact atomic domain enqueue record, а не глобальный WB
zero. Обычный worker отдельно публикует и подтверждает ответ. Explicit approved
recovery для `owner_policy_unsafe_public_reply` выбирает safe template даже при
наличии старого audited ready result: забракованный текст не переутверждается.
Original job evidence архивируется. Автоматический recovery сохраняет этот
policy exclusion. Сам adapter не вызывает provider и не выполняет WB POST.

Safe-public замена старого seller-chat решения сохраняет provenance в
`server_policy_recovery`, отдельно от `server_policy_transform` настоящего
чат-приглашения. Текст остаётся точным детерминированным public template без
case code; общий chat guard не изменён. Для старой zero-write публикации с
legacy safe-public metadata новый exact-cohort preview показывает действие
`repair_safe_public_provenance`. Оно проверяет исходную archived seller-chat
revision, source hash, текущий template ID/rating, точный reply/hash, result gates
и отсутствие write marker/attempts. Apply сохраняет прежний result в аудите,
исправляет только provenance и rebinds те же processing/publication keys.
Требуется новая одобренная операция зарегистрированного adapter; предыдущая
применённая операция не отправляется повторно. Поддельное доказательство,
реальные chat/case-code сведения и возможный прошлый POST не переоткрываются.

## Общий вопрос о возврате без причины

Unfrozen orchestration policy `generic-return-question-2026-10-08-v1`
проверяет всё `text + pros + cons` до штатного case-code allocation. Общие
«Как оформить возврат?», «Как вернуть деньги?» и вежливые вариации направляются
в `seller_chat`: сначала продавец уточняет ситуацию и пробует помочь.
Само слово «деньги» не доказывает уже оформленный возврат. Факты об одобренном
возврате/задержке выплаты сохраняют `wb_support`; описанный дефект сохраняет
действующие товарные guards. Неизвестное добавленное содержание, медиа и tags
не поглощаются этим правилом. Frozen bundle и manifest не изменены.

Публичный ответ содержит только приглашение в чат и единственный штатный код,
без обещания возврата/денег или требования публичных материалов. Существующий
`promote_chat_invitation` и его проверка на каждой публикационной границе
сохраняются. CTA guard различает действие «Оформите/заполните заявку на возврат»
и существительное «Оформление возврата»/«статус заявки на возврат».

Для ошибочно обработанного старой политикой `needs_review / fallback_used /
WB_REFUND_STATUS` разрешена отдельная exact-cohort операция зарегистрированного
adapter с дополнительным полем request:
`"replacement_policy": "generic_return_question_v1"`. Поле входит в fingerprints
и durable operation claim. Без него semantic fallback не принимается.
Preview показывает `replace_generic_return_question`, полный новый текст,
case code, source/result/content hashes и нулевые новые provider calls.

Replacement повторно под control lock проверяет свежий WB content, отсутствие
ответа, manual/error/lease/media blockers и любых publication generations для
feedback. Canonical Node allocator учитывает занятые коды и стабильный key;
штатные draft/chat-promotion guards проверяют новый детерминированный текст.
Исходный результат и стоимость архивируются. Processing key, attempts,
settled costs и usage сохраняются; enqueue создаёт обычную публикацию без POST.
Повтор retained operation допускает только readback. Возможный начавшийся POST
никогда не переоткрывается. Это замена по явному новому правилу владельца,
а не одобрение исходного fallback или массовое разрешение fallback-ответов.
