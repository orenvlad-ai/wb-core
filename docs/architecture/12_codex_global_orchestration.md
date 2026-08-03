# Global Codex Orchestration v1

## Назначение и граница

Global Codex Orchestration v1 связывает кураторские обсуждения, отдельные user-owned Codex-задачи, один локальный Luna Watcher, временные Sol-арбитры и GitHub Release Train. Система применяется только к `orenvlad-ai/wb-core` и не становится внешним управляющим сервисом.

Durable state распределён по владельцам:

- локальный `~/.wb-core/orchestrator/v1/registry.sqlite3` — logical task/thread/PR mapping, user-level acceptance envelopes, durable attention outbox, executor succession, Task Passport revision, retry fingerprints, incidents, resource locks и Watcher generations;
- локальный append-only `events.jsonl` — переносимый audit событий registry;
- GitHub — branch/commit/PR/check/review/release truth и logical release lane;
- Codex threads — исполняемые и наблюдаемые поверхности, но не база состояния;
- localhost dashboard — read-only представление registry, не control API.

Mac и Codex Desktop должны быть включены, а локальный проект — доступен. Внешнего control plane, Entire dependency и Telegram в v1 нет. Native app notifications остаются штатным push-каналом. Entire может появиться позже только как необязательная observability surface после устойчивого базового контура.

## Проверенный Desktop capability contour

Desktop v1 фактически предоставляет callable операции create/list/read/wait/send для Codex threads, archive, pin, rename и recurring heartbeat automation. Heartbeat привязывается к exact target thread и использует его модель, поэтому глобальный Watcher создаётся отдельной user-owned Codex task на `gpt-5.6-luna`, а затем получает одну 10-минутную heartbeat automation. Arbiter создаётся отдельной временной task на `gpt-5.6-sol`.

Automation не является внешним daemon: она выполняется локальным Desktop. Thread listing не используется как durable state и не считается доказательством repository identity само по себе. Fallback discovery принимает только pinned local Codex thread, чей текущий `cwd` является Git checkout; поддержанный GitHub HTTPS/SSH origin нормализуется в slug, который обязан быть exact `orenvlad-ai/wb-core`. Malformed, missing, non-GitHub, projectless, ChatGPT-only и non-repository tasks исключаются.

## Dispatch и Task Passport

Post-plan команда запуска из `discussion-only` остаётся `DISPATCH_REQUEST`. Initiating curator не реализует change сам и не создаёт subagent вместо user-owned task. Launch operation не откладывается:

1. сформировать Task Passport `wb-core-task-passport/v1` по [`codex_task_passport_v1.schema.json`](../../packages/contracts/codex_task_passport_v1.schema.json);
2. создать exact executor task и выполнить bounded `wait_threads(timeoutMs: 0)` readback;
3. дать короткие titles `<тема> · C1` curator и `<тема> · C2` executor, закрепить оба и получить exact pin/readback evidence; missing pin/readback останавливает launch fail closed;
4. только после readback атомарно зарегистрировать exact task/curator/executor/host identities, assignment-time pin digests, passport digest, initial resources и acceptance envelope через `apps/codex_task_orchestrator.py register-task`; обычная задача создаёт собственный root, а corrective child явно присоединяется к существующему незавершённому user-level envelope и атомарно переоткрывает его;
5. проверить registry readback и ровно одно active Watcher generation;
6. только после этого сообщить успешный dispatch. Если target создан, но local Watcher временно недоступен, execution продолжается с явным `MONITORING_CAPABILITY_LIMITATION`; неподтверждённый target create остаётся fail closed.

Passport фиксирует цель, expected result, execution contour, included/excluded scope, constraints, acceptance, closure, autonomy envelope, strict HumanGate allowlist, initial resource set и exact source identities. Registry принимает только canonical repository и не регистрирует сторонние задачи. Pin выполняется один раз при назначении curator/current-executor role; Watcher heartbeat не делает постоянный re-pin, поэтому поздний ручной unpin владельца сохраняется.

## Registry и локальные интерфейсы

Основной CLI: `python3 apps/codex_task_orchestrator.py`. Default home — `~/.wb-core/orchestrator/v1`, права каталога/SQLite/JSONL ограничены текущим пользователем, SQLite работает с WAL/FULL sync. Записи состояния используют `BEGIN IMMEDIATE`, optimistic task revision и machine validation. JSONL flush сериализован file lock и не дублирует concurrent sequence.

Основные операции:

- `init`, `register-task`, `add-thread`, `confirm-role-pin`, `bind-acceptance`, `reconcile-acceptance`, `link-pr`, `update-task`;
- `checkpoint-progress`, `progress-state`, `apply-progress`;
- `enqueue-attention`, `reserve-attention`, `mark-attention-sent`, `retry-attention`, `attention`, `ack-attention`;
- `prepare-owner-handoff`, `confirm-owner-notification`, `accept-curator`;
- `register-executor-succession`, `pending-executor-archives`, `confirm-executor-archive`;
- `record-failure`, `resolve-failure`;
- `open-incident`, `claim-incident`, `attach-arbiter`, `decide`, `deliver`, `verify`, `close-incident`;
- `prepare-watcher`, `confirm-watcher-readback`, `smoke-watcher`, `activate-watcher`, `pending-watcher-retirements`, `confirm-watcher-retirement`, `begin-run`, `end-run`;
- `heartbeat-plan`, `heartbeat-record-target`, `heartbeat-actuate`, `heartbeat-record-followup`, `heartbeat-finish`;
- `list`, `snapshot`, `report`, `heartbeat-response`, `integrity`, `serve`.

`serve` допускает только loopback bind и не имеет mutation endpoints. Registry не вызывает Codex или GitHub самостоятельно: действия исполняет Watcher через поддержанные Desktop/GitHub interfaces.

## Один Luna Watcher

Машинный config — [`codex_watcher_v1.json`](../../packages/contracts/codex_watcher_v1.json), durable prompt — [`codex_watcher_prompt_v1.md`](../policies/codex_watcher_prompt_v1.md). Каждые 10 минут Watcher:

1. получает generation-bound lease и уникальный `run_id`; overlap и old generation завершаются no-op;
2. читает registry snapshot/integrity и read-only `python3 apps/github_release_train.py queue-status`, затем создаёт durable `heartbeat-plan` с полным ordered phase list и exact target set;
3. покрывает каждый target отдельным `wait_threads(timeoutMs: 0)` readback и фиксирует binding через `heartbeat-record-target`; ранний wake одного target не меняет pending-state остальных;
4. active target только наблюдает; idle non-terminal target получает ровно один bounded follow-up с transport receipt;
5. только после полного coverage `heartbeat-actuate` детерминированно обрабатывает progress/checkpoint, objective evidence, failure/incident и terminal evidence для каждого task revision; один task получает не больше одного meaningful transition на run;
6. обслуживает revision-bound attention outbox и подтверждает delivery receipts;
7. завершает run единственным stdout `heartbeat-finish`; команда fail closed проверяет coverage/follow-up/attention, освобождает lease и возвращает canonical `NOTIFY/DONT_NOTIFY` wrapper.

`watcher_run_plans` и `watcher_run_targets` являются локальным durable audit обязательных действий, а не новым control service. Desktop readback/transport остаются агентными, но их результат принимается только как exact machine observation `wb-core-watcher-target-observation/v1`, связанный с task/revision, executor thread/generation/host, turn/final item, timestamp и digests. Completed target без такого binding не превращается в terminal task по одному слову из чата.

Локальный `queue-status` использует `GITHUB_TOKEN`, если он передан явно, а при его отсутствии читает уже существующую авторизацию через `gh auth token`. Токен не печатается и не сохраняется в registry/JSONL. Этот fallback ограничен read-only командой Watcher: в GitHub Actions отсутствие `GITHUB_TOKEN` остаётся ошибкой, mutation-команды не получают локальный fallback.

## Evidence-backed progress

`register-task` выполняется только после доказанного `TARGET_CREATE_READBACK`, поэтому новый active task materialize-ится сразу как `executor-started = 5%`. Значение `0%` остаётся только pre-executor понятием и не является нормальным зарегистрированным состоянием. Единственный mapper находится в `apps/codex_task_orchestrator_spec.py`; executor, Watcher prompt и renderer не хранят собственные magic numbers:

| Доказанный этап | Прогресс |
|---|---:|
| executor запущен | 5% |
| preflight/анализ завершён | 15% |
| реализация начата | 25% |
| основной diff готов | 40% |
| первичные проверки прошли | 55% |
| полные проверки и semantic review прошли | 65% |
| PR создан | 72% |
| CI/admission/release stage доказаны | 80% |
| merge/release выполняются | 88% |
| deploy выполнен, идёт финальная проверка | 95% |
| применимое техническое завершение доказано | 100% |

Executor вызывает `checkpoint-progress` только на meaningful раннем milestone: передаёт stage, evidence digest, реалистичный ETA range, свежий русский delta и следующий шаг. Команда пишет существующий append-only `events` audit и не меняет task revision/percentage. Отдельной checkpoint table и нового control plane нет. Повтор того же payload идемпотентен; устаревший task revision и downgrade fail closed.

Watcher читает `progress-state` и bounded thread readback. Если checkpoint валиден и новее materialized task state, он может быть применён. Если checkpoint отсутствует, но readback доказывает свежие file/test facts, Watcher передаёт только минимальный observed early-stage floor с evidence timestamp/digest и свежими delta/current; elapsed time и heartbeat count доказательством не являются. GitHub/Release Train truth сначала связывается через `link-pr`: `open/draft → PR`, `ci-green/staged/ready/admitted → admission`, `running/merged → release`, `deployed/awaiting-ui/production → final verification`. Только такой linked objective state может подтвердить поздний milestone и он сильнее self-report.

Revision-bound `apply-progress` выбирает сильнейшее свежее evidence и materialize-ит task fields. Один `run_owner` может сделать не больше одного meaningful progress transition для task за heartbeat. Процент может остаться прежним, когда новые delta/current описывают содержательную работу. Нижележащий test finding сам по себе не уменьшает ранее доказанный progress. Если objective truth опроверг прежний milestone, explicit invalidation допускает ровно один предыдущий уровень, переводит task в recovery и требует русское объяснение; последующий доказанный stage возвращает обычное движение. Corrective/new task всегда регистрируется отдельно и начинает свой member progress с 5%.

`100%` не принимает generic `update-task` или `apply-progress`. Только `enqueue-attention TECHNICAL_COMPLETION` materialize-ит terminal progress после contour-aware proof: `diagnostic-complete` для read-only, `artifact-verified` для user artifact, linked `release:done` для repo-only и linked `release:production` для live/runtime, LOOP и production mutation. Repo-only не имеет 95% deploy stage. Terminal percentage не заменяет durable attention delivery, curator acknowledgement, acceptance-envelope completion или фразу владельца `Задача принята`.

Visible report не строится Watcher-ом из raw `snapshot`. Repo-owned renderer сначала делит каждый незавершённый acceptance envelope на user-visible workstreams: root и каждый параллельный `required-child` являются отдельными workstream anchors, а `corrective`/replacement generations наследуют anchor исходной workstream. По умолчанию corrective наследует root; коррекция конкретного `required-child` регистрируется с exact `--acceptance-workstream-task <ANCHOR_TASK>`. Каждый workstream даёт отдельный block со своим title, минимальным доказанным progress и собственным critical-path ETA/delta/current. Независимые envelopes одного curator также остаются отдельными blocks. Acceptance при этом остаётся единой на envelope и ждёт terminal+acked состояние всех required workstreams. Технические task IDs, UUID, revisions, digests, enums, registry/queue/lease/follow-up/batch facts остаются только в audit/evidence. Статусы и тексты user layer — короткие русские; GitHub, Watcher, PR и C1/C2/C3 разрешены, когда полезны владельцу.

Platform heartbeat response тоже repo-owned. `heartbeat-finish` проверяет complete run receipts, вызывает canonical renderer и сериализует ровно один `<heartbeat>` wrapper; standalone `heartbeat-response` остаётся только низкоуровневым renderer/smoke interface. При одном или нескольких active workstreams `decision=NOTIFY` обязателен на каждом scheduled run, а parsed XML `message` равен canonical report stdout. Поэтому unchanged fingerprint остаётся видимым periodic update: renderer заменяет delta на краткое `Изменений нет; работа продолжается: ...`, но wrapper не превращает его в `DONT_NOTIFY`. Несколько workstreams/envelopes сохраняются отдельными русскими blocks в одном message. Никакого plain report, summary или diagnostics после wrapper Watcher не добавляет. XML escaping относится только к transport serialization; parsed message text остаётся исходным report.

`DONT_NOTIFY` возвращается только при одновременном отсутствии active report blocks, due/sent owner-relevant attention и active incident. Quiet message — короткое `Нет активных задач.`. Pending owner delivery fail-safe даёт `NOTIFY`; обычно такой state всё равно имеет active envelope. Решение вычисляется из durable registry при каждом run, поэтому chat context compaction и generation rotation не меняют поведение.

Report format не расширяется произвольными секциями:

```text
Статус: ...
Задача: ...
Прогресс: ≈...% · Осталось: ≈...
С прошлого отчёта: ...
Сейчас: ...
Блокер: ...
```

`Блокер` присутствует только при доказанном strict human-only состоянии. Progress не растёт от времени, числа heartbeat или повторных ожиданий. Если fingerprint user-level envelope не изменился, renderer явно пишет, что изменений нет и работа продолжается, не повторяя служебную диагностику как пользовательскую дельту.

## Attention Outbox И User-Level Acceptance

Attention kinds: техническое завершение, terminal failure, strict HumanGate и доказанная серьёзная остановка. Immutable payload содержит стабильные `event_id/event_digest`, task/revision/kind, exact curator, acceptance envelope и bounded evidence. Delivery state проходит `PENDING → LEASED → SENT/RETRY → ACKED`; stale revision даёт `STALE`. Lease, attempts, timestamps, transport receipt и curator acknowledgement сохраняются в SQLite/JSONL. Rotation/restart не создают новый logical event.

Desktop thread transport предоставляет at-least-once, но не доказанное exactly-once. Watcher резервирует максимум восемь due events, отправляет каждое в exact `curator_thread_id` через supported Desktop capability и после call readback фиксирует `mark-attention-sent`; ошибка получает bounded `retry-attention`. Crash после send до confirm может дать повтор того же `event_id`. Curator `ack-attention` идемпотентен, поэтому повтор не создаёт вторую user-level приёмку.

Техническое завершение атомарно создаёт `DONE_PENDING_HANDOFF`; только exact curator acknowledgement переводит member в `DONE_AWAITING_ACCEPTANCE`. Аналогично pending states защищают terminal failure и strict HumanGate. User-level envelope имеет `OPEN → DONE_PENDING_HANDOFF → AWAITING_ACCEPTANCE → ACCEPTED`: он ждёт terminal+acked state всех required parent/corrective members. Добавление нового required corrective member к ещё не принятому envelope атомарно возвращает его в `OPEN`, повышает revision и инвалидирует прежние owner notification/handoff evidence; старый summary больше не разрешает acceptance. Один C1 может содержать несколько technical tasks одного envelope, но получает один owner summary и одну просьбу `Задача принята` после последнего required ack.

Curator вызывает `prepare-owner-handoff`, который материализует digest-bound короткий текст: `Статус: Завершена — требуется приёмка`, одна-две строки смысла в `Сделано`, `Проверено`, только реальное `Ограничения` и `Ответьте ровно: «Задача принята»`. Самый надёжный Desktop sequencing: показать exact текст владельцу в commentary, затем выполнить `confirm-owner-notification` с его digest и повторить тот же текст на final surface. Повтор с тем же digest не создаёт вторую logical notification, зато финальный ответ остаётся самодостаточным. После подтверждения renderer пишет `Ожидается приёмка владельца.`. `accept-curator` без task name допустим только при ровно одном awaiting envelope, current notification revision и exact curator. При нескольких независимых envelopes операция fail closed. После ACCEPTED все member tasks исключаются из следующего report; unpin/archive current curator/current executor не выполняются.

## Executor Succession И Searchable Archive

Curator и current active executor никогда автоматически не unpin/archive. Corrective executor может supersede predecessor только внутри того же acceptance envelope после доказанных `TARGET_CREATE_READBACK`, prompt delivery, registry link, переданного checkpoint/context и active successor readback. `register-executor-succession` атомарно сохраняет predecessor/successor thread+generation, reason, evidence digests и переводит predecessor в inactive legacy. Missing/wrong/ambiguous identity fail closed.

Только после этого Watcher/curator вызывает supported `set_thread_archived` для exact predecessor, проверяет searchable exact readback и фиксирует digest через `confirm-executor-archive`. Archive не удаляет thread, GitHub или audit. Terminal executor без successor остаётся current; final acceptance не архивирует и не снимает pin у current curator/current executor.

Generation rotation готовит новый Luna Watcher из trusted `origin/main`, даёт ему title, закрепляет и получает exact title/pin/enabled-automation readbacks до `prepare-watcher`. Smoke fail closed без любого readback и включает deterministic run plan, early-wake remaining coverage, два checkpoint transitions в одном run без потери второго target, idempotent bound terminalization, initial 5%, terminal-only 100%, один-envelope parallel workstreams, corrective folding, два independent envelopes, unchanged `NOTIFY`, quiet `DONT_NOTIFY` и отсутствие raw machine jargon. Пока successor готовится, старая automation непрерывно работает. После `smoke-watcher` atomic activation делает старое поколение stale/no-op, но не pause его automation. Successor сначала обязан доказать первый post-cutover active `begin-run`, полный successful `heartbeat-finish` и собственный enabled-automation readback через `confirm-watcher-liveness`. Только после этого старая automation ставится на паузу, старый Watcher searchable-архивируется с readback, а оба evidence фиксируются через `confirm-watcher-retirement`. Эта последовательность не ждёт idle tasks: весь user-level state остаётся в registry/JSONL, а objective evidence перечитывается из current GitHub и trusted repo.

## Retry, replacement и Sol-арбитр

Failure identity — task + phase + deterministic evidence fingerprint. Counts живут в SQLite, поэтому rotation/restart чата их не обнуляет:

- первая пустая system error — один same-executor retry;
- вторая одинаковая пустая system error — unclaimed incident и один replacement executor из последнего проверенного checkpoint; успешный replacement переводит этот case в `STALE` через `resolve-failure`;
- третья одинаковая error fingerprint — claim существующего incident и свежий temporary Sol arbiter;
- четвёртый blind retry запрещён.

Один active incident на task обеспечивается partial unique index. `task:<id>` всегда входит в resources; конфликтующие release решения дополнительно держат `wb-core:release`. Watcher сначала резервирует case и locks, затем создаёт arbiter и атомарно attaches exact thread identity. Arbiter получает только Task Passport, exact task revision, bounded state/evidence, incident key/digest и resources по [`codex_arbiter_prompt_v1.md`](../policies/codex_arbiter_prompt_v1.md); полный чат не передаётся.

Decision содержит bounded action, scope, expected transition и evidence digest. Перед delivery Watcher повторно читает task revision; stale decision становится `STALE` и locks освобождаются. После delivery ожидаемый transition независимо проверяется и фиксируется `verify --verification-evidence-digest sha256:<DIGEST>`, case становится `VERIFIED`, arbiter архивируется, и только затем `close-incident --archive-evidence-digest sha256:<DIGEST>` фиксирует archive readback и освобождает locks. Decision/audit остаётся в SQLite/JSONL.

## Strict HumanGate

`AWAITING_HUMAN` допустим только при непустом exact blocker, allowlisted reason, `repo_owned_remediation_available=false` и `remediation_exhausted=true`. Allowlist:

- missing credential;
- interactive login/2FA/captcha;
- proven irreversible data risk;
- security/permission change;
- new external data destination;
- material scope/risk change;
- platform hard stop.

Git/GitHub/checks/review/merge/deploy/reconciliation, bounded retries, reversible technical decisions, queue waiting и доступная UI automation не являются HumanGate.

## Release admission и logical lane

Feature flag `WB_CORE_ORCHESTRATION_REQUIRED` по умолчанию выключен до доказанного local pilot. При включённом enforcement:

- STANDARD executor после checks/review/docs оставляет `release:staged`;
- LOOP сначала получает существующий repo-owned exact new/recovery enrollment;
- Watcher публикует `/wb-core orchestration admit <PR> head <HEAD_SHA> task <TASK_ID> revision <N> passport sha256:<DIGEST>`;
- trusted-main Actions проверяет actor association, exact PR/head/baseline/passport/task revision, получает либо проверяет `release:lane-owner`, создаёт admission proof и только для STANDARD переводит `staged → ready`;
- selection и merge требуют current exact-head admission, совпадающую logical task lane; прямой `release:ready` не обходит enforcement;
- если trusted `main` sync изменил STANDARD head, PR безопасно возвращается в `release:staged`, а не `blocked`, и требует re-admission нового exact head;
- same-task staged PR регистрируется admission proof, но ждёт завершения текущего same-task PR; LOOP recovery того же root допускается через active UI gate;
- lane остаётся на anchor PR через несколько PR, merge/deploy/UI/recovery и освобождается exact `release-lane` command только после task-level closure.

`outcome=parked` допустим только для unmerged `staged/blocked` state. Merged ambiguity, `running`, `awaiting-ui`, `halted`, conflicting owner/proof или незавершённый same-task PR оставляют lane fail closed. Release Train остаётся механическим исполнителем sync/checks/merge/deploy/verify.

## Legacy retirement

Исторические merged blocked PR из [`release_train_legacy_retirement_20260803.json`](../../migration/release_train_legacy_retirement_20260803.json) не очищаются ручным label edit. После merge trusted main owner публикует exact command:

`/wb-core orchestration retire-legacy <PR> head <HEAD_SHA> manifest sha256:<MANIFEST_DIGEST>`

Actions сверяет manifest digest, exact PR/head/merge, merged blocked state и отсутствие terminal label, создаёт bot-owned proof и атомарно ставит `release:retired`. `retired` означает только подтверждённый вывод исторической записи из active monitoring и не заявляет отсутствующее production evidence.

## Watcher rotation

`packages/contracts/codex_watcher_v1.json` централизованно задаёт `max_runs=48`: при cadence 10 минут planned rotation наступает примерно каждые восемь часов. Boundary точный: run 47 ещё не требует rotation, run 48 уже возвращает `rotation_due=true`. Дополнительный ранний trigger использует только доказанный supported Desktop readback: `read_thread` возвращает typed turn item `items[].type=contextCompaction`. Active Watcher bounded-читает собственные последние turns и при таком exact item под текущей lease вызывает `record-watcher-context-compaction` с item ID и digest полного bounded readback. Размер чата, число токенов, elapsed time и визуальная длина не считаются signal. Если exact typed item не предоставлен automation surface, ранняя detection недоступна и безопасный deterministic fallback — только hard 48-run cap.

Rotation выполняет одну recoverable последовательность, не ожидая пустой очереди или отсутствия active tasks:

`prepare new Luna thread + title/pin/enabled 10-minute automation readbacks while old remains enabled → read-only canary/smoke → record smoke digest → atomic activate → old begin-run returns stale-generation → successor active begin-run/full heartbeat/end-run + liveness readback → pause old automation → searchable archive old thread → confirm retirement evidence`

Activate без smoke и title/pin/enabled-automation readback запрещён. Enabled evidence состоит из supported Desktop `automation_update mode=view` и exact local `automation.toml` readback с `status=ACTIVE`; его digest фиксирует `confirm-watcher-automation-enabled`. Unique active Watcher index и generation-bound lease исключают dual mutation authority; временно две enabled scheduled automations допустимы. Empty report, `DONT_NOTIFY`, rotation due и preactivation никогда не меняют automation state. Chat context не переносится; новый Watcher восстанавливается только из registry, JSONL, current GitHub и current repo contracts. Failure до activation оставляет старое generation active и не требует rollback. Если первый successor run после activation не завершился, old authority не восстанавливается и old automation не pause: `record-watcher-liveness-failure` оставляет successor единственным active generation и задаёт bounded retry той же automation. Это минимальный Desktop-compatible handover: platform не предоставляет транзакцию, объединяющую SQLite generation switch и внешний automation scheduler, поэтому exact no-gap доказывается enabled readbacks до/после cutover и lease isolation, а не заявлением невозможной внешней атомарности. Незавершённые liveness/pause/archive/retirement steps остаются resumable. Active Watcher остаётся pinned до безопасной rotation; old generation и arbiter threads можно архивировать автоматически только после successor liveness, но curator/executor unpin никогда не автоматизируется.

## Rollout и rollback

Rollout v1:

1. merge code/docs through current Release Train with enforcement still false;
2. initialize local registry from trusted `origin/main`;
3. create/title/pin one Luna Watcher, capture exact title/pin/enabled-automation readbacks, attach one 10-minute heartbeat, prepare/smoke/activate generation and prove its first active heartbeat liveness;
4. на двух независимых fixture/pilot envelopes проверить initial 5%, свежий bounded early floor, linked objective late stage, отсутствие time-based начисления, same-stage delta/current, terminal contour и раздельные русские report blocks;
5. pause legacy per-chat heartbeat automations only after the global Watcher owns both periodic reporting and durable attention delivery; corrective predecessor archive требует отдельного succession/readback proof и не выводится из самого запуска Watcher;
6. retire manifest PRs through trusted-main exact commands;
7. enable `WB_CORE_ORCHESTRATION_REQUIRED` only after successful pilot and empty/conflict-free release lane readback.

Rollback pauses the global heartbeat and leaves enforcement false (or returns it to false before admitting new work). Registry/audit are preserved. GitHub labels/proofs are never manually erased. A proven platform limitation keeps enforcement false and is reported with exact evidence and the minimal next step; repository completion is not misrepresented as end-to-end rollout.
