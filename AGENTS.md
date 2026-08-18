# Рабочий протокол `wb-core`

Этот файл — самодостаточный operational entrypoint для Codex и ChatGPT,
работающих с репозиторием. Доменная архитектура живёт в
`docs/architecture/*`, `docs/modules/*` и `migration/*`; здесь находится ровно
один действующий execution flow.

## Действующий flow

Обычная change-задача проходит один последовательный контур:

1. Новый куратор без напоминания задаёт себе короткое полностью видимое имя
   `WBC · <короткая тема> · К<n>` и закрепляет задачу.
2. Пользователь обсуждает с куратором цель, bounded scope, acceptance и closure.
3. После согласования куратор создаёт ровно одного прямого user-owned
   исполнителя как отдельную видимую Codex-задачу через supported task/thread
   creation surface, фиксирует её thread ID, destination repo/worktree/host,
   задаёт ей связанное имя
   `WBC · <та же короткая тема> · И<n>` и закрепляет задачу. Куратор не
   реализует change сам и не вызывает collaboration `spawn_agent`/subagent для
   delegation, анализа, implementation, review, monitoring, recovery,
   takeover или executor-роли. Executor prompt завершается обязательным
   указанием самостоятельно дойти до применимого terminal state и вернуть в
   исходную кураторскую задачу один финальный technical handoff после
   `COMPLETE` либо доказанного `BLOCKED`.
4. До fetch/branch/write и substantive work исполнитель выполняет bounded
   non-mutating `CAPABILITY_ROUTING_CANARY`. Только `CANARY_QUALIFIED`
   продолжает тот же visible executor в `EXECUTOR_AUTONOMY_PREFLIGHT`,
   отдельной branch/worktree от актуального `origin/main` и task-local
   `autonomy_ready`. `CANARY_RESTRICTED` не начинает работу и не просит
   покомандный platform approval.
5. Исполнитель обновляет только необходимые code/docs/tests и выполняет
   targeted checks и semantic self-review.
6. Исполнитель открывает один open non-draft PR в `main` из same-repository
   branch, ставит `task:standard` и ровно одну label:
   `scope:repo-only`, `scope:live-runtime` или, только для фактического apply,
   `scope:production-mutation`.
7. После successful required check `baseline` на current exact head исполнитель
   добавляет `release:ready`. До этого label не ставится.
8. GitHub Release Train повторно проверяет current head, labels, baseline,
   mergeability и safety gates, при необходимости синхронизирует branch с
   current `main`, запускает fresh baseline и сериализует merge и применимый
   exact-SHA deploy/verify.
   Исключение — exact DCP branch `ao/wb-core-<positive>/root`: versioned
   `wb-core.dcp-release-handoff/v1` (legacy repo-only) и
   `wb-core.dcp-release-handoff/v2` (repo-only/live-runtime) запрещают
   auto-sync. Actions публикует typed readmission marker, а fresh head/review/
   admission создаёт только DCP; Release Train остаётся единственным merge и
   deploy actor.
9. `scope:repo-only` завершается только на `release:done`;
   `scope:live-runtime` — только на `release:production` после canonical
   deploy/verify. `scope:production-mutation` использует отдельный human-gated
   terminalization contract и автоматически не выпускается.
10. Исполнитель передаёт куратору один финальный technical handoff. Куратор без
   повторной технической проверки тезисно пересказывает его владельцу.
   Техническое завершение, merge и release label не являются owner acceptance:
   только владелец пишет `Задача принята` и вручную открепляет задачи.

Ветви, PR и release labels других задач не изменяй. Чужая активная release
операция — штатное ожидание; она не разрешает снимать labels, обходить очередь
или вмешиваться в live release.

## Quiet curator после dispatch

Каждый executor task prompt заканчивается обязательным указанием со следующей
не сокращаемой семантикой:

`Исполнитель самостоятельно доводит задачу до применимого terminal state. После COMPLETE либо доказанного BLOCKED отправь в исходную кураторскую задачу один финальный technical handoff: итоговый статус; что сделано; что не сделано или осталось вне scope; PR и final SHA; проверки; merge/release/deploy/production state; visible executor task/thread ID; effective routing profile и app/CLI/runner versions; platform approval count; сложности, риски и blockers.`

После успешного dispatch куратор немедленно завершает свой текущий turn.
`Ждёт` означает quiet wait: отсутствие активных model/tool calls, а не
`wait`/poll loop. До пробуждения куратор не инициирует wait/read/list/status
опросы исполнителя, GitHub/CI/runtime/production audit его работы, follow-up
prompts, промежуточные сводки, параллельную реализацию, независимую перепроверку
handoff, heartbeat, automation или любой другой мониторинговый контур.

Куратора пробуждает только финальный handoff исполнителя, direct
`CANARY_RESTRICTED`/routing-defect callback, прямой strict human-only
pre-terminal callback либо новое явное указание владельца. Обычный progress,
включая `CANARY_QUALIFIED` и `autonomy_ready`, не является сигналом. Вся
техническая проверка, evidence и terminal closure до handoff принадлежат
исполнителю. После финального handoff куратор только тезисно сообщает владельцу
статус, сделанное, не сделанное или исключённое, выполненные проверки и
достигнутый production/terminal state, а также сложности, риски или blocker;
второй технический audit он не выполняет.

## Permanent permission routing и executor autonomy preflight

Capability truth — effective machine-reported context текущего turn/runner.
Saved config, prior turn, prompt, broad owner authorization и версия app сами
по себе не доказывают и не расширяют approval policy, sandbox, network,
writable roots или destination access. Owner authority ограничивает scope и
risk, но не меняет machine-enforced profile.

Каждый новый repo-backed executor до fetch/branch/write, длительного анализа и
implementation выполняет bounded non-mutating `CAPABILITY_ROUTING_CANARY` и
фиксирует:

- current task/turn либо immutable runner identity и destination surface;
- machine-read app/CLI/runner versions;
- effective approval policy, sandbox, network и writable roots;
- exact capability inventory и destination identities: repo, shared Git
  metadata, GitHub/network и применимые host/service/runtime/data targets;
- `platform_approval_count=0`.

Canary не выполняет fetch, branch, test write, GitHub mutation, service/data
action или production gate. Для unattended workspace/network/host работы
обычный local lane обязан доказать `approval_policy=never`,
`sandbox=danger-full-access`, требуемую сеть и owner-bounded destinations;
допустим также эквивалентный pinned managed runner с immutable capability
receipt. Более узкий профиль допустим только когда он полностью покрывает
read-only или workspace-contained задачу без interactive platform prompt.

Если task-create не умеет pin и report требуемый профиль, через него создаётся
только capability-only отдельная видимая user-owned задача. После
`CANARY_QUALIFIED` эта же задача продолжает как единственный substantive
executor. `CANARY_RESTRICTED` не делает repo/host mutations и не просит owner
подтверждать команды: куратор после direct routing-defect callback ровно один
раз направляет работу в уже qualified turn/pinned runner либо фиксирует tooling
blocker. Цепочка restricted executors запрещена.

Первый unexpected platform permission prompt — routing defect, а не Human
Gate. Исполнитель останавливается на последней safe point, записывает exact
missing capability/destination, не request/forward-ит command approval, не
повторяет команду другой формой и отправляет куратору один direct
routing-defect callback. Platform approval acceptance от canary до terminal
handoff равен нулю.

После `CANARY_QUALIFIED` исполнитель выполняет
`EXECUTOR_AUTONOMY_PREFLIGHT`: проверяет доступ к собственному worktree и shared
Git metadata, status, remotes, GitHub auth, обязательный
`git fetch --prune origin`, отдельную branch от current `origin/main`, GitHub
connector/fallback и необходимые local dependencies/runtime paths. После
успеха он кратко пишет в своей видимой задаче `autonomy_ready` с exact starting
main SHA, branch и подтверждением `platform_approval_count=0`. Это task-local
progress, не callback куратору и не новая durable state machine.

Re-canary обязателен при смене app/CLI/runner, turn/task-create или execution
surface, effective profile/network/writable roots, host/session/destination
либо required capability inventory. Предыдущее qualification через такую
границу не наследуется.

Истинный strict Human Gate остаётся только для owner business/risk decision,
exact production-mutation gate, credentials/login/2FA/captcha, которые нельзя
предоставить разрешённым non-interactive контуром, proven irreversible risk,
security change, new external destination или material scope/risk change.
Такой gate получает один direct pre-terminal callback с exact resource/effect
и одним минимальным owner action; hidden UI state не заменяет callback.

## Duplicate-executor guard

Только после допустимого wake signal, `CANARY_RESTRICTED`/routing-defect
callback либо обнаруженного curator dispatch defect и перед решением
«перезапустить в новом executor» куратор выполняет один bounded read-only
check: terminal/unavailable state исходной задачи, worktree status, branch,
uncommitted diff, commits/push и open PR. Restricted profile,
`waitingOnApproval` и platform prompt не являются разрешением создать дубль.
Если существует branch/diff/commit/push/PR либо исходный executor можно resume
в qualified lane, продолжается тот же visible executor или фиксируется exact
blocker.

Новый executor допустим только когда исходный доказанно terminal/unrecoverable
и незавершённого implementation state нет. Для этого должны быть явно доказаны
clean untouched worktree, no branch, no commit, no push и no PR. Куратор не
выполняет automatic reset/clean/delete чужого state и не запускает takeover,
параллельного исполнителя или новый monitoring contour.

Куратор не использует collaboration `spawn_agent`/subagent для delegation,
анализа, implementation, review, monitoring, recovery, takeover или
executor-роли. Fork, nested curator, hidden agent, monitor/reporter/reviewer
subagent и implementation внутри discussion-task не заменяют отдельную
видимую user-owned executor task. Первый curator `spawn_agent` — dispatch
defect: скрытый агент останавливается на safe point до дальнейших mutations,
после чего guard выше сохраняет ровно одну visible task без потери или
дублирования state. Acceptance требует zero curator `spawn_agent` calls,
видимый executor task/thread ID и zero platform approval prompts.

## Выключенная legacy-оркестрация

`WB_CORE_ORCHESTRATION_REQUIRED=false`. Global Watcher, external orchestration
registry, Task Passport, acceptance envelope, curator workspace automation,
logical release lane, orchestration admission, shepherd/takeover, persistent
arbiter и обязательные heartbeat/chat callback механизмы выведены из active
flow.

Не запускай, не регистрируй и не восстанавливай эти механизмы; не создавай им
замену в виде scheduler, reviewer, reporter, arbiter или control plane. Они не
нужны для dispatch, PR eligibility, `release:ready`, merge, deploy, closure или
owner acceptance. Исторический contract доступен только через
[`docs/architecture/12_codex_global_orchestration.md`](docs/architecture/12_codex_global_orchestration.md).
Retained compatibility code и historical GitHub labels не являются agent
instructions и не разрешают начинать новый legacy-контур.

GitHub Release Train core, Finance/storage safety, exact-SHA deploy/verify,
production-mutation gates и manual owner acceptance остаются действующими без
ослабления.

## Видимый lifecycle ролей

Единственный подробный contract имён, нумерации, pin/unpin и owner acceptance
находится в разделе [«Видимый жизненный цикл Codex-задач»](docs/architecture/07_codex_execution_protocol.md#видимый-жизненный-цикл-codex-задач).

Короткие правила:

- exact topic у куратора и исполнителя одной цепочки совпадает;
- счётчики `К<n>` и `И<n>` независимы и не переиспользуются;
- title и pin назначаются один раз при получении роли без напоминания владельца;
- агент не закрепляет повторно вручную откреплённую владельцем задачу;
- агент не синтезирует `Задача принята`, не открепляет и не архивирует текущие
  задачи автоматически;
- project/bootstrap instructions только направляют к этому файлу и execution
  protocol, но не дублируют lifecycle как второй source of truth.

## Источники истины и preflight

Приоритет источников:

1. Git-tracked code и актуальный `origin/main` задают code truth. Рабочая ветка
   остаётся proposed change до review и merge.
2. `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*` задают
   authoritative documentation truth.
3. GitHub задаёт branch, commit, PR, checks, review, merge и release truth.
4. Production server и его current server-owned stores/documents задают
   canonical deploy/runtime и production-data boundary.
5. WebCore Data MCP — архивный read-only compatibility contour, не normal
   source/acquisition path и не обязательная capability.
6. Legacy artifacts, старые чаты, вложения и прежние project instructions —
   только migration evidence и do-not-lose constraints.

Перед техническим выводом, постановкой задачи, реализацией или проверкой
результата другого агента изучи:

- актуальный GitHub state;
- этот `AGENTS.md`;
- только релевантные authoritative docs;
- фактический code truth, если вывод касается реализации.

Перед изменениями проверь status/branch/remotes/auth, выполни
`git fetch --prune origin`, создай отдельную branch/worktree от актуального
`origin/main` и проверь открытые PR. Не смешивай, не очищай и не теряй чужой
dirty state.

Если меняется code, contract, runtime boundary, module status или другой
зафиксированный truth, синхронизируй затронутые authoritative docs в той же
задаче.

## Prompt и technical path

Любой connector, server, runtime, storage, SSH alias, путь или технический
запрет из prompt повторно проверяется по current `AGENTS.md`, authoritative docs
и code truth. Это гипотеза автора prompt, если пользователь отдельно и явно не
зафиксировал её как своё ограничение.

Новый task prompt не называет WebCore Data MCP и не hardcode-ит access path. Он
фиксирует цель, необходимые данные, read-only/mutation boundaries, ожидаемый
результат и acceptance/closure criteria и содержит правило:

`Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

Curator dispatch prompt создаётся только для одной отдельной видимой user-owned
Codex-задачи через supported task/thread creation surface; он фиксирует thread
ID, linked title/pin и destination repo/worktree/host. Куратор не использует
collaboration `spawn_agent`/subagent, fork, nested curator или hidden
monitor/reviewer/recovery agent как замену executor.

Каждый новый repo-backed executor prompt до обязательной финальной
terminal-handoff фразы явно требует: немедленный non-mutating
`CAPABILITY_ROUTING_CANARY`; routing record и
`platform_approval_count=0`; продолжение этой же visible task только после
`CANARY_QUALIFIED`; `EXECUTOR_AUTONOMY_PREFLIGHT` и task-local
`autonomy_ready`; direct routing-defect либо strict human-only callback по
описанному выше контракту; safe stop без request/forward command approval;
запрет считать restricted profile или `waitingOnApproval` разрешением на
duplicate executor; bounded read-only duplicate guard после wake signal.
Если task-create не pin/report-ит profile, prompt capability-only до результата
canary. `autonomy_ready` не превращается в обычный callback, heartbeat или
периодический monitor. Обязательная terminal-handoff фраза остаётся последней
директивой prompt.

Если нужны production evidence/data, используй canonical server-side path:
сначала определи current target/runtime и конкретный source по code/docs, затем
выполни фактический
`PRODUCTION_READ_PREFLIGHT`: штатный SSH к canonical server, query-only чтение
server-owned stores (`mode=ro` и `PRAGMA query_only=ON` для SQLite либо
эквивалент) и bounded read server-owned documents. Этот read path не разрешает
deploy, service changes, upstream sync, production writes, ad-hoc mutation или
раскрытие secrets. Недоступность архивного MCP не является blocker.

## Режимы и execution-контуры

Пользователь не выбирает служебный класс специальной строкой. Codex определяет
его по результату:

- исключительно read-only анализ — `ДИАГНОСТИКА`, без files/GitHub/production
  mutations;
- обычная реализация, live change или неоднозначный случай — `СТАНДАРТ` и
  действующий flow выше;
- новый legacy `LOOP` из текущего protocol не запускается. Сохранившиеся LOOP
  states/handlers относятся к compatibility и product fail-closed behavior, а
  не к обязательному callback или отдельной orchestration-сессии.

Execution contours:

- `read-only` — подтверждённый анализ без mutations;
- `user-artifact` — единственная mutation: запрошенный XLSX/CSV/DOCX/PDF/TXT
  вне репозитория. Такая запись не является `ДИАГНОСТИКОЙ`; branch, worktree,
  Release Train и label `scope:user-artifact` не создаются;
- `repo-only` — code/docs/tests change без live/runtime эффекта;
- `live/runtime` — public route, service/process, operator UI, runtime behavior
  или deploy wiring, с canonical deploy и verify;
- `production data mutation/backfill` — только PR/runner, который фактически
  выполняет bounded apply;
- `archived GAS guard` — только явно заданный bounded archive guard scope.

Для нового XLSX основной путь — active Spreadsheets skill и
`@oai/artifact-tool`. Runtime discovery предоставляет
`CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`,
`CODEX_PRIMARY_RUNTIME_NODE_MODULES` и `CODEX_PRIMARY_RUNTIME_PYTHON`.
Отсутствие `load_workspace_dependencies` само по себе не blocker. После bounded
recovery допустимы уже установленные `openpyxl`, затем `xlsxwriter`, затем
dependency-free ZIP/XML `OOXML`; новые зависимости из сети не устанавливаются.

## Unattended execution boundary

Unattended выполнение начинается только в `CANARY_QUALIFIED` lane с полным
capability match и `platform_approval_count=0`. Широкое предварительное
разрешение пользователя позволяет последовательно запускать заранее
согласованные ordinary repo/live stages и устранять non-material технические
blockers внутри scope, но не расширяет machine profile. Оно не заменяет exact
production-mutation gate, credentials/login/2FA/captcha без разрешённого
non-interactive path, owner decision при proven irreversible risk, security
change, new external destination или material product/risk choice.

Отсутствие владельца не разрешает обходить `baseline`, GitHub Release Train,
exact-SHA deploy/verify или production-mutation evidence. Для новой unattended
цепочки куратор создаёт одну visible executor task, а она проходит routing
canary и `autonomy_ready`; restricted task не начинает implementation, не
просит platform approval и не порождает параллельные/вложенные executors.

## Phase-local production safety

Production gates не являются одним глобальным барьером:

1. `REPOSITORY_PREFLIGHT` покрывает repo/worktree/docs/code/tests/GitHub.
2. Repository implementation, fixtures/mocks, review, PR и CI выполняются до
   максимально возможного безопасного состояния.
3. `PRODUCTION_READ_PREFLIGHT` выполняется только перед необходимым read-only
   evidence.
4. `PRODUCTION_MUTATION_PREFLIGHT` выполняется непосредственно перед apply.
5. `PRODUCTION_UI_PREFLIGHT` выполняется только перед production UI verify.

Отсутствие будущих credentials/database/browser/manifests/digest/backup не
блокирует независимые repository phases. Blocker допустим только на
непосредственной boundary после фактического preflight, исчерпанной repo-owned
remediation и отсутствия оставшейся безопасной работы.

Production mutation runner обязан иметь dry-run по умолчанию, отдельный
explicit apply, bounded scope, machine-readable manifest, pre-change digest,
backup/evidence contract, expected affected records, non-target invariants,
idempotency либо документированный recovery, post-apply readback и
reconciliation. Ad-hoc SQL, случайные local/server-only scripts и mutation
через архивный read-only MCP запрещены.

Human-gated `scope:production-mutation` закрывается только trusted-main exact
command после pre-merge release gate, merge, separately authorized exact
post-merge apply и reconciliation:

`/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> release-gate <RELEASE_GATE_COMMENT_ID> release-gate-digest sha256:<RELEASE_GATE_COMMENT_HASH> apply-gate <APPLY_GATE_COMMENT_ID> apply-gate-digest sha256:<APPLY_GATE_COMMENT_HASH> manifest sha256:<MANIFEST_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>`

Release gate и apply gate — разные immutable OWNER/MEMBER comments. Первый
предшествует merge, содержит exact head и разрешает merge/deploy. Второй
следует после merge, содержит exact PR, deployed SHA, manifest fingerprint и
production-apply authorization. Reconciliation следует после apply gate.
Append-only source suffix либо сама reconciliation не могут подменить apply
gate; редактирование любого из трёх comments инвалидирует exact digest.

Finance/storage migrations дополнительно сохраняют все lease, snapshot,
backup, restore, writer/timer, exact-SHA и non-target contracts из
[`docs/architecture/10_hosted_runtime_deploy_contract.md`](docs/architecture/10_hosted_runtime_deploy_contract.md)
и [GitHub Release Train](docs/architecture/11_github_release_train.md).

## Проверка и closure

Перед `release:ready` проверь:

- изменены только requested и прямо необходимые support files;
- semantic diff прочитан полностью, а не только список файлов;
- targeted checks соответствуют риску;
- findings исправлены, checks и semantic review повторены;
- authoritative docs синхронизированы;
- нет secrets, production data, generated dumps и unrelated edits;
- visible executor task/thread ID зафиксирован, curator `spawn_agent` calls=0
  и platform approval prompts=0;
- PR open, non-draft, same-repository, направлен в `main`, current exact head
  имеет successful `baseline`, выставлены `task:standard` и одна `scope:*`.

Если пользователь не задал более раннюю границу, executor самостоятельно ведёт
repo-backed задачу через commit, push, PR, checks/review, `release:ready` и
Release Train до `release:done` либо `release:production`, затем fetch-ит
`origin/main` и подтверждает merged result. Open PR, только local checks или
merge без требуемого deploy/verify не являются completion.

Production UI evidence не заменяется HTTP `200`. Оно включает requested/final
URL и redirects, отсутствие `5xx`, `DOMContentLoaded`, видимый render,
непустые title/body, `pageerror`/fatal/существенные console errors и визуально
проверенный screenshot. По умолчанию используется новый isolated non-persistent
Playwright context без пользовательского profile/cookies/credentials и без
business mutations вне explicit scope.

Исполнитель не подменяет собственную technical verification отчётами других
агентов. Перед финальным handoff он сам проверяет GitHub state, final SHA,
semantic diff, checks/reviews, unresolved threads, docs и, если применимо,
canonical deploy/live/data evidence. Эта обязанность не поручает куратору
повторный audit после handoff.

Пользователь нужен только для strict human-only действия: owner business/risk
decision, exact production-mutation gate, credential/login/2FA/captcha без
разрешённого non-interactive path, доказанный необратимый data risk, security
change, новая внешняя data destination или material scope/risk change. До
terminal handoff такой gate сообщает direct pre-terminal callback с одним
минимальным owner action. `waitingOnApproval`, missing platform capability,
permission prompt и platform hard stop являются routing/tooling defects, не
Human Gates и не основаниями просить покомандное подтверждение.

## Итоговый ответ

Сообщай только применимое:

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
