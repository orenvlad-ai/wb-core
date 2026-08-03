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
- `enqueue-attention`, `reserve-attention`, `mark-attention-sent`, `retry-attention`, `attention`, `ack-attention`;
- `prepare-owner-handoff`, `confirm-owner-notification`, `accept-curator`;
- `register-executor-succession`, `pending-executor-archives`, `confirm-executor-archive`;
- `record-failure`, `resolve-failure`;
- `open-incident`, `claim-incident`, `attach-arbiter`, `decide`, `deliver`, `verify`, `close-incident`;
- `prepare-watcher`, `confirm-watcher-readback`, `smoke-watcher`, `activate-watcher`, `pending-watcher-retirements`, `confirm-watcher-retirement`, `begin-run`, `end-run`;
- `list`, `snapshot`, `report`, `integrity`, `serve`.

`serve` допускает только loopback bind и не имеет mutation endpoints. Registry не вызывает Codex или GitHub самостоятельно: действия исполняет Watcher через поддержанные Desktop/GitHub interfaces.

## Один Luna Watcher

Машинный config — [`codex_watcher_v1.json`](../../packages/contracts/codex_watcher_v1.json), durable prompt — [`codex_watcher_prompt_v1.md`](../policies/codex_watcher_prompt_v1.md). Каждые 10 минут Watcher:

1. получает generation-bound lease; overlap и old generation завершаются no-op;
2. читает registry snapshot/integrity и read-only `python3 apps/github_release_train.py queue-status`;
3. читает exact registered targets пакетами не больше восьми через `wait_threads(timeoutMs: 0)`;
4. active target только наблюдает; idle non-terminal target получает ровно один bounded follow-up;
5. сохраняет evidence-backed progress/failure/task revision и обслуживает revision-bound attention outbox;
6. печатает без изменений stdout единственного repo-owned renderer `python3 apps/codex_task_orchestrator.py report`, один короткий блок на active acceptance envelope;
7. освобождает run lease.

Локальный `queue-status` использует `GITHUB_TOKEN`, если он передан явно, а при его отсутствии читает уже существующую авторизацию через `gh auth token`. Токен не печатается и не сохраняется в registry/JSONL. Этот fallback ограничен read-only командой Watcher: в GitHub Actions отсутствие `GITHUB_TOKEN` остаётся ошибкой, mutation-команды не получают локальный fallback.

Visible report не строится Watcher-ом из raw `snapshot`. Единственная repo-owned функция агрегирует обязательных members acceptance envelope без двойного учёта: user-level progress равен минимальному доказанному progress required-member, ETA/delta/current берутся из его текущего critical path, а terminal root ждёт всех required corrective children. Независимые envelopes дают отдельные blocks. Технические task IDs, UUID, revisions, digests, enums, registry/queue/lease/follow-up/batch facts остаются только в audit/evidence. Статусы и тексты user layer — короткие русские; GitHub, Watcher, PR и C1/C2/C3 разрешены, когда полезны владельцу.

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

Generation rotation готовит новый Luna Watcher из trusted `origin/main`, даёт ему title, закрепляет и получает exact title/pin/automation readbacks до `prepare-watcher`. Smoke fail closed без любого readback и включает outbox capability и канонический report: один русский block на текущий acceptance envelope, доказанный meaningful progress и отсутствие raw enum/task ID/UUID/revision/digest/internal jargon. После `smoke-watcher` atomic activation делает старое поколение stale/no-op; только затем его heartbeat ставится на паузу, старый Watcher searchable-архивируется с readback, а оба evidence фиксируются через `confirm-watcher-retirement`.

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

Каждое generation имеет `max_runs` (v1 default 720). Rotation выполняет одну recoverable последовательность:

`prepare new Luna thread/automation → immediate readback smoke → record smoke digest → atomic activate → old begin-run returns stale-generation → archive old thread`

Activate без smoke и title/pin/automation readback запрещён. Unique active Watcher index и generation-bound lease исключают overlap. Chat context не переносится; новый Watcher восстанавливается только из registry, JSONL, current GitHub и current repo contracts. Active Watcher остаётся pinned до безопасной rotation; old generation и arbiter threads можно архивировать автоматически только после successor activation/readback, но curator/executor unpin никогда не автоматизируется.

## Rollout и rollback

Rollout v1:

1. merge code/docs through current Release Train with enforcement still false;
2. initialize local registry from trusted `origin/main`;
3. create/title/pin one Luna Watcher, capture exact title/pin/automation readbacks, attach one 10-minute heartbeat, prepare/smoke/activate generation;
4. register a bounded pilot task and verify exact readback, fallback repository filtering, report format, retry counts, incident/arbiter lifecycle, rotation no-op and acceptance exclusion;
5. pause legacy per-chat heartbeat automations only after the global Watcher owns both periodic reporting and durable attention delivery; corrective predecessor archive требует отдельного succession/readback proof и не выводится из самого запуска Watcher;
6. retire manifest PRs through trusted-main exact commands;
7. enable `WB_CORE_ORCHESTRATION_REQUIRED` only after successful pilot and empty/conflict-free release lane readback.

Rollback pauses the global heartbeat and leaves enforcement false (or returns it to false before admitting new work). Registry/audit are preserved. GitHub labels/proofs are never manually erased. A proven platform limitation keeps enforcement false and is reported with exact evidence and the minimal next step; repository completion is not misrepresented as end-to-end rollout.
