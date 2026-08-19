# Codex Execution Protocol

## Назначение

Корневой [`AGENTS.md`](../../AGENTS.md) — самодостаточный operational
entrypoint. Этот документ раскрывает тот же единственный действующий flow и не
создаёт второй набор правил.

Change-задача без отдельной пользовательской границы проходит:

`implementation → targeted checks → semantic review → PR → baseline → release:ready → GitHub Release Train → technical handoff`

План, незакоммиченный diff, только local checks, open PR либо merge без
применимого deploy/verify не являются completion.

## Действующий последовательный flow

1. Пользователь согласует с куратором цель, bounded scope, acceptance и closure.
2. Куратор через supported task/thread creation surface создаёт ровно одного
   прямого видимого user-owned исполнителя, фиксирует его thread ID, linked
   title/pin и destination repo/worktree/host и завершает executor prompt
   обязательным terminal-handoff указанием из раздела
   [Prompt Contract](#prompt-contract-и-technical-path-revalidation).
3. До fetch/branch/write исполнитель выполняет non-mutating
   `CAPABILITY_ROUTING_CANARY`. `CANARY_QUALIFIED` продолжает ту же visible task:
   fresh `origin/main`, отдельная branch/worktree, executor autonomy preflight,
   task-local `autonomy_ready`, bounded change, necessary docs, targeted checks
   и полный semantic diff. `CANARY_RESTRICTED` substantive work не начинает.
4. Исполнитель открывает один open non-draft same-repository PR в `main` с
   `task:standard` и ровно одной `scope:*` label.
5. После successful `baseline` на current exact head исполнитель добавляет
   `release:ready`.
6. Existing GitHub Release Train сериализует работу, повторно проверяет exact
   head и labels, синхронизирует branch с current `main`, запускает fresh
   baseline и выполняет применимые merge и exact-SHA deploy/verify.
7. Terminal state — `release:done` для `scope:repo-only` либо
   `release:production` для `scope:live-runtime`. Production mutation закрывает
   только отдельный human-gated exact-evidence contract.
8. Исполнитель возвращает куратору один финальный technical handoff. Куратор
   без повторной технической проверки тезисно передаёт его владельцу; только
   владелец принимает задачу.

`WB_CORE_ORCHESTRATION_REQUIRED=false`. Legacy Global Watcher, orchestration
registry, Task Passport, acceptance envelope, logical release lane,
orchestration admission, shepherd/takeover, persistent arbiter и обязательные
heartbeat/chat callbacks не входят ни в один шаг. Их нельзя запускать,
регистрировать, восстанавливать или заменять новым control plane. Архивный
контур находится только в
[`12_codex_global_orchestration.md`](12_codex_global_orchestration.md).

Retained compatibility handlers и historical labels не являются instruction
для нового executor. Настоящий документ не меняет Release Train state machine;
он однозначно определяет, какие его поверхности использует current ordinary
flow.

## Видимый Жизненный Цикл Codex-Задач

Это единственный authoritative WBC contract для видимых имён кураторских и
исполнительских задач, их закрепления, technical handoff и owner acceptance.
UI metadata не меняет task class, branch/PR identity, execution contour или
Release Train.

### Имена И Связь Ролей

Канонические шаблоны:

- куратор: `WBC · <короткая тема> · К<n>`;
- исполнитель: `WBC · <та же короткая тема> · И<n>`.

Первый куратор новой цепочки в первом рабочем turn без напоминания пользователя
выбирает короткую полностью видимую тему, задаёт имя
`WBC · <короткая тема> · К1` и закрепляет задачу. После согласования цели он
создаёт ровно одного прямого исполнителя без nested curator, fork или
collaboration `spawn_agent`/subagent, задаёт имя
`WBC · <та же короткая тема> · И1` и закрепляет его задачу. Видимая task
identity включает thread ID и destination repo/worktree/host; владелец может
открыть карточку и видеть task-local status.

Exact topic одной незавершённой цепочки не меняется. Счётчики ролей независимы:
для каждого нового поколения роли `n = 1 + max` уже использованных номеров этой
роли. Номера не переиспользуются.

Имя использует prefix `WBC`, разделитель ` · `, тему без `·`, role marker `К`
или `И` и positive decimal `n` без leading zero. Варианты `К1+`, `И1+`, suffix
`fix`/`retry` и произвольные несвязанные названия запрещены.

### Pin, Handoff И Owner Acceptance

Title и pin назначаются агентом один раз при получении роли без напоминания.
Если владелец вручную открепил задачу, агент не закрепляет повторно. Отсутствие
supported title/pin capability честно отмечается, но не заменяется
repository/runtime automation и не влияет на PR eligibility.

После `COMPLETE` либо доказанного `BLOCKED` исполнитель передаёт в исходную
кураторскую задачу один финальный technical handoff. Он содержит итоговый
статус; что сделано; что не сделано или осталось вне scope; PR и final SHA;
проверки; merge/release/deploy/production state; visible executor task/thread
ID; effective routing profile и app/CLI/runner versions; platform approval
count; сложности, риски и blockers.

Получив handoff, куратор без повторной технической проверки тезисно сообщает
владельцу статус, сделанное, не сделанное или исключённое, выполненные проверки
и достигнутый production/terminal state, а также сложности, риски или blocker,
после чего просит владельца ответить ровно: `Задача принята`.

Merge, `release:done`, `release:production` и handoff не являются owner
acceptance. Куратор, исполнитель и другие агенты не синтезируют
`Задача принята` от имени владельца. Только владелец вручную открепляет задачи;
агенты не unpin/archive/delete их автоматически.

Project/bootstrap instructions только направляют к root `AGENTS.md` и этому
разделу. Они не дублируют naming/pinning/unpinning/acceptance правила и не
создают Gateway, Agent Orchestrator, reviewer, arbiter, watcher, scheduler или
другой enforcement runtime.

## Куратор и отдельный исполнитель

Discussion-задача остаётся curator surface. Когда пользователь согласовал цель
и просит начать реализацию, куратор создаёт отдельную user-owned Codex-задачу
через supported thread/task creation capability. Он фиксирует thread ID,
связанный title, pin и destination repo/worktree/host. Владелец может открыть
эту карточку и видеть status. Внутренний subagent, fork, nested curator, hidden
agent и реализация change в discussion-задаче не заменяют прямого исполнителя.

Куратор передаёт исполнителю цель, ожидаемый результат, included/excluded scope,
constraints, acceptance/closure, правило technical-path revalidation и
обязательный terminal-handoff contract. Он не создаёт registry entry, passport,
monitor, heartbeat automation или callback контур. Для delegation, анализа,
implementation, review, monitoring, recovery, takeover и executor-роли он не
вызывает collaboration `spawn_agent`/subagent. Acceptance требует zero curator
`spawn_agent` calls.

Если task-create surface не умеет pin и report effective permission profile,
первый turn новой visible task является capability-only canary. Qualified task
сама продолжает как substantive executor; restricted task ничего не реализует,
а куратор после callback один раз reroute-ит работу в qualified turn/pinned
runner либо фиксирует tooling blocker.

После успешного dispatch куратор немедленно завершает текущий turn. `Ждёт`
означает quiet wait: отсутствие активных model/tool calls, а не `wait`/poll
loop. Куратор не инициирует wait/read/list/status опросы исполнителя,
GitHub/CI/runtime/production audit его работы, follow-up prompts, промежуточные
сводки, параллельную реализацию, независимую перепроверку handoff, heartbeat,
automation или любой другой мониторинговый контур.

Куратор пробуждается только по финальному handoff, direct
`CANARY_RESTRICTED`/routing-defect callback, direct strict human-only
pre-terminal callback исполнителя либо новому явному указанию владельца.
Обычный progress, включая `CANARY_QUALIFIED` и `autonomy_ready`, не является
поводом просыпаться. Вся technical verification, evidence и terminal closure
до handoff — ответственность исполнителя; куратор не создаёт второй audit
contour.

## Permanent Permission Routing И Executor Autonomy Preflight

### Bounded Incident Basis

Проверенные WBC runs показали оба режима при неизменных saved config и owner
authority: qualified executors с `never`/`danger-full-access`/network и zero
prompts завершали unattended work, а позднее routed executors с
`on-request`/`workspace-write`/disabled network упирались в prompts для обычных
Git/network/host действий. Проведённая 2026-08-16 programmatically created
visible canary снова получила qualified profile. Следовательно, restricted
cascade был routing defect, а prompt/owner authority не могли расширить
effective profile.

Отдельный curator audit обнаружил повторные collaboration `spawn_agent` calls,
включая hidden executor-like analysis/implementation/recovery, тогда как другие
recent WBC curator tasks завершали dispatch с zero spawn calls. Hidden
subagents поэтому являются повторяемым dispatch defect, а не технической
неизбежностью. Эти findings не меняют repo-owned runtime/worker concepts и не
создают межрепозиторную runtime dependency.

### Capability Truth И Routing Record

Capability truth — effective machine-reported context текущего turn или
managed runner. Saved user/project config, prior turn, prompt, owner
authorization, model assertion и неизменившаяся app version являются intent
или historical evidence, но не доказывают и не расширяют approval policy,
sandbox, network, writable roots либо destination access. Owner authority
по-прежнему ограничивает task scope/risk.

До fetch/branch/write, model-backed substantive analysis, remote operation или
production mutation repo-backed executor выполняет bounded non-mutating
`CAPABILITY_ROUTING_CANARY`. Routing record включает:

| Field | Machine-backed evidence |
| --- | --- |
| Task/runner identity | Current task/thread/turn ID либо immutable runner receipt |
| Destination surface | Codex task, CLI invocation, task-create surface или managed runner |
| Versions | Machine-read app и exact CLI/runner versions |
| Effective profile | Approval policy, sandbox, network и writable roots |
| Capability inventory | Exact repo, shared Git metadata, GitHub/network, loopback, SSH, service-manager, filesystem и другие реально нужные capabilities |
| Destinations | Exact repository/remotes и применимые host/service/runtime/data targets |
| Approval count | `platform_approval_count=0` с сохранением до terminal handoff |

Missing, ambiguous, inherited-only или model-asserted field fail closed. Canary
не делает fetch/branch, test-marker write, GitHub mutation, service/data action
или production gate. Разрешены только bounded reads, необходимые для проверки
identity, versions, auth и reachability.

### Qualified И Restricted Lanes

Для unattended workspace/network/host работы обычный local lane доказывает:

- effective `approval_policy=never`;
- effective `sandbox=danger-full-access`;
- enabled network, когда она нужна;
- owner-bounded repo/remote/host/operation destinations; и
- zero platform approval prompts.

Эквивалентен pinned managed runner с immutable receipt для approval, sandbox,
network, writable roots и destinations. Более узкий профиль допустим только
для read-only/workspace-contained задачи, если полный capability inventory
покрыт без interactive platform prompt.

Если task-create не pin/report-ит нужный профиль, через него создаётся только
capability-only visible user-owned task. `CANARY_QUALIFIED` продолжает в той же
task как единственный substantive executor. `CANARY_RESTRICTED` не делает
repo/host mutations и не запрашивает command approval: direct routing-defect
callback будит куратора, который ровно один раз выбирает already-qualified
turn/pinned runner либо фиксирует tooling blocker. Каскад restricted executors
запрещён.

Первый unexpected platform permission prompt — routing defect, не Human Gate.
Executor останавливается на последней safe point, фиксирует exact missing
capability/destination, не request/forward-ит approval, не повторяет команду
другой формой/API и отправляет direct routing-defect callback. Acceptance:
`platform_approval_count=0` от canary до terminal handoff.

### Repository Autonomy После Qualified Canary

Только после `CANARY_QUALIFIED` новый repo-backed executor выполняет
`EXECUTOR_AUTONOMY_PREFLIGHT`:

1. чтение/запись собственного worktree и shared Git metadata;
2. status, remotes, GitHub auth, обязательный `git fetch --prune origin` и
   отдельная branch от current `origin/main`;
3. GitHub connector и доступный fallback;
4. необходимые local dependencies/runtime paths;
5. отсутствие pending platform prompt и сохранение approval count zero.

После успеха исполнитель фиксирует в своей visible task `autonomy_ready`: exact
starting main SHA, branch и `platform_approval_count=0`. Это task-local
progress, не callback куратору и не новая durable state machine.

Routing canary повторяется при смене app/CLI/runner, app relaunch, turn/task,
task-create или execution surface, remote-exec/SSH implementation, effective
profile/network/writable roots, host/session/destination либо capability
inventory. Qualification не кэшируется через эти boundaries.

### Duplicate-Executor Guard После Wake Signal

Restricted profile, `waitingOnApproval` и platform prompt не являются
разрешением создать дубль. Только после допустимого wake signal,
`CANARY_RESTRICTED`/routing-defect callback либо обнаруженного curator dispatch
defect и до решения «перезапустить в новом executor» куратор делает один
bounded read-only check:

- terminal/unavailable state исходной задачи;
- worktree status и branch;
- uncommitted diff;
- commits/push;
- open PR.

При branch/diff/commit/push/PR или возможности resume в qualified lane
продолжается тот же visible executor либо фиксируется exact blocker. Новый
executor допустим только если исходный доказанно terminal/unrecoverable и
незавершённого implementation state нет: clean untouched worktree, no branch,
no commit, no push и no PR должны быть явно доказаны. Automatic
reset/clean/delete чужого state, параллельный takeover, duplicate
implementation и monitoring contour запрещены.

Первый curator collaboration `spawn_agent` — dispatch defect. Hidden agent
останавливается на safe point до дальнейших mutations; затем guard выше
сохраняет ровно одну visible user-owned task без потери или дублирования state.
Fork, nested curator, hidden executor, monitor/reporter/reviewer subagent и
implementation в discussion-task не считаются допустимым executor routing.

## Prompt Contract И Technical Path Revalidation

Task prompt описывает результат: цель, необходимые данные, read-only/mutation
boundaries, итоговый artifact/answer и acceptance/closure.
Он не называет WebCore Data MCP. Он не назначает
connector/server/runtime/storage/SSH alias и не запрещает canonical server-side
read, если сам пользователь отдельно и явно не установил такое ограничение.

Каждый prompt содержит provenance-правило:

`Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

Curator dispatch создаёт prompt только для одной отдельной visible user-owned
Codex-задачи через supported task/thread creation surface. Dispatch record
содержит executor thread ID, linked title/pin, destination repo/worktree/host и
zero curator `spawn_agent` calls. Collaboration subagent, fork, nested curator
или hidden monitoring/review/recovery agent не являются executor surface.

Каждый новый repo-backed executor prompt до terminal-handoff указания явно
включает contract из предыдущего раздела: немедленный non-mutating
`CAPABILITY_ROUTING_CANARY`, полный routing record,
`platform_approval_count=0`, продолжение той же task только после
`CANARY_QUALIFIED`, `EXECUTOR_AUTONOMY_PREFLIGHT`, task-local `autonomy_ready`,
direct routing-defect либо strict human-only callback, safe stop без
request/forward command approval и duplicate-executor guard после wake signal.
В prompt также однозначно сказано, что restricted profile и
`waitingOnApproval` не разрешают automatic restart/duplicate executor, а
`autonomy_ready` не создаёт callback/heartbeat/monitoring contour. Если
task-create не pin/report-ит profile, prompt остаётся capability-only до
результата canary.
Для production-mutation prompt также явно разделяет source owner
business/risk decision и GitHub transport. Exact authorization из visible source
task передаётся executor-у дословно вместе с source task/thread ID;
summary, paraphrase или hidden-memory claim не являются authorization. Prompt
требует от qualified executor-а самому relay-ить доказанный payload через
non-interactive GitHub identity с association `OWNER`/`MEMBER` или fail
closed с direct callback. Владелец не обязан вручную открывать PR,
публиковать GitHub comment, запускать command или выполнять GitHub action.

Каждый executor task prompt обязательно заканчивается следующим по смыслу и
составу полей указанием, после которого нет иных task directives:

`Исполнитель самостоятельно доводит задачу до применимого terminal state. После COMPLETE либо доказанного BLOCKED отправь в исходную кураторскую задачу один финальный technical handoff: итоговый статус; что сделано; что не сделано или осталось вне scope; PR и final SHA; проверки; merge/release/deploy/production state; visible executor task/thread ID; effective routing profile и app/CLI/runner versions; platform approval count; сложности, риски и blockers.`

До terminal state исполнитель не отправляет обычный progress как callback.
Pre-terminal исключения — direct `CANARY_RESTRICTED`/routing-defect callback и
direct strict human-only callback по описанному выше составу, без которого
безопасное продолжение невозможно.

До выполнения Codex повторно проверяет proposed technical path по current
`origin/main`, root `AGENTS.md`, релевантным authoritative docs и code truth.
Tool/source/path из prompt остаётся гипотезой автора. Старый prompt с
обязательным MCP или запретом canonical server-side access без отдельного
пользовательского требования в этой части не действует и не создаёт blocker.

Если нужны production evidence/data:

1. определить current target, runtime и concrete stores/documents по code/docs;
2. выполнить фактический `PRODUCTION_READ_PREFLIGHT`, включая штатный SSH к
   canonical production target и exact source access;
3. читать server-owned stores query-only (`mode=ro` и
   `PRAGMA query_only=ON` для SQLite либо эквивалент) и bounded server-owned
   documents;
4. не менять services, schedules, runtime files/data, upstream systems или
   production config и не раскрывать secrets/raw dumps;
5. объявлять blocker только после exact canonical SSH/store/document error либо
   доказанного отсутствия необходимых данных.

Архивный WebCore Data MCP не является normal path, prerequisite или fallback;
его отсутствие non-blocking.

## Task Mode И Контур

Пользователь не выбирает служебный class и не начинает prompt специальной
строкой:

- `ДИАГНОСТИКА` — строго read-only: code/docs/GitHub/log/production evidence
  можно читать, mutations запрещены;
- `СТАНДАРТ` — ordinary implementation и полный применимый current flow;
- legacy LOOP не запускается новым executor из current protocol. Retained LOOP
  machine behavior остаётся fail-closed compatibility, а не активным
  orchestration/callback требованием.

Неоднозначный case выбирает `СТАНДАРТ`, не расширяя scope или authority.

## Шесть Execution-Контуров

### `read-only`

Анализ, диагностика или review без code, GitHub и production mutations. Итог —
подтверждённый анализ либо exact external blocker.

### `user-artifact`

Создание или изменение запрошенного XLSX, CSV, DOCX, PDF, TXT либо аналогичного
файла является mutation и не является `ДИАГНОСТИКОЙ`. Если это единственная
mutation и файл находится вне repository:

- branch, worktree, commit и PR не создаются;
- GitHub Release Train и GitHub state не изменяются;
- label `scope:user-artifact` не существует и не создаётся;
- repo files, production и business data не изменяются;
- разрешены read-only sources, временные files вне repo и итоговый файл.

Изменение Git-tracked docs/code/tests/helper — обычный
`СТАНДАРТ + scope:repo-only`, даже если change посвящён artifacts.

Для нового обычного XLSX основной path — active Spreadsheets skill и
`@oai/artifact-tool`. Runtime discovery предоставляет
`CODEX_PRIMARY_RUNTIME_ROOT`, `CODEX_PRIMARY_RUNTIME_NODE`,
`CODEX_PRIMARY_RUNTIME_NODE_MODULES`, `CODEX_PRIMARY_RUNTIME_PYTHON`.
Builder работает во временной директории вне repo.
Отсутствие `load_workspace_dependencies` само по себе не blocker.
После bounded recovery допустимы уже установленные `openpyxl`, затем `xlsxwriter`, затем
dependency-free ZIP/XML `OOXML`; source data между попытками не собирается
заново, новые network dependencies не устанавливаются.

### `repo-only`

Code/docs/tests change без live/runtime эффекта. Deploy не применяется;
terminal state — `release:done`.

### `live/runtime`

Public route, service/process, operator UI, runtime behavior, deploy wiring или
другой live effect. Требует full repository closure, canonical repo-owned
deploy exact merge SHA и live/service/public verify; terminal state —
`release:production`.

### `production data mutation/backfill`

Label `scope:production-mutation` применяется только к PR/runner, который
фактически выполняет bounded apply. Обязательны explicit cohort/effect,
read-only preflight, dry-run, verified
backup/reversibility, audit, idempotency/resumability, human gate, canonical
runner, reconciliation и non-target invariants.

### `archived GAS guard`

Только явно заданный bounded archive guard change. Он не возвращает Google
Sheets/GAS в active runtime и требует targeted guard checks и bounded verify.

## Unattended Execution Boundary

Unattended выполнение начинается только в `CANARY_QUALIFIED` lane с exact
capability match и `platform_approval_count=0`. Широкое предварительное
разрешение пользователя покрывает последовательный запуск заранее
согласованных ordinary repo/live stages и устранение non-material технических
blockers внутри scope, но не расширяет machine profile. Оно не заменяет
source owner business/risk decision для exact production-mutation gate,
credential/login/2FA/captcha без разрешённого
non-interactive path, owner decision при proven irreversible data risk,
security change, new external destination или material product/risk choice.
После данного exact decision его GitHub transport и остальной mechanical
closure принадлежат qualified executor-у, а не владельцу.

Отсутствие владельца не ослабляет `baseline`, GitHub Release Train, exact-SHA
deploy/verify и production-mutation evidence. Для новой unattended цепочки
куратор создаёт одну visible executor task; она проходит routing canary и
`autonomy_ready`. Restricted task не начинает implementation, не просит
platform approval и не порождает параллельные/вложенные executors. Quiet curator
после dispatch остаётся quiet до terminal, routing-defect, true human-only или
owner wake signal.

## Phase-Local Preflight И Production Safety

Preflight не является глобальным барьером. Dependency order:

1. `CAPABILITY_ROUTING_CANARY` — non-mutating proof текущей execution lane.
2. `REPOSITORY_PREFLIGHT` — repository/worktree, AGENTS/docs/code, local
   dependencies, tests и GitHub baseline.
3. Repository implementation/validation, repo-owned runner на fixtures/mocks,
   branch/PR, CI и review.
4. `PRODUCTION_READ_PREFLIGHT` — только перед конкретным production read.
5. `PRODUCTION_MUTATION_PREFLIGHT` — непосредственно перед apply: exact scope,
   dry-run/coverage, manifest/digests, backup/restore, expected records,
   non-target invariants, authorization, deployed runner/version и
   reconciliation.
6. `PRODUCTION_UI_PREFLIGHT` — только перед production UI verify.

Отсутствующая будущая capability не блокирует независимые repository phases.
Phase-local wait допустим только на непосредственной boundary после actual
preflight, исчерпанной repo-owned remediation и завершения всей безопасной
работы. `current_phase`, `blocked_phase`, `safe_phases_remaining`,
`required_capability`, `capability_evidence` и `next_executable_action` могут
фиксировать dependency context, но не образуют второй release state machine.

Будущий production-mutation runner до gate реализуется и тестируется на
fixtures/mocks. Contract: dry-run default, отдельный explicit apply, bounded
scope, machine-readable manifest, pre-change digest, backup/evidence, expected
affected records, non-target invariants, idempotency либо documented recovery,
post-apply readback и reconciliation. Ad-hoc SQL, случайные local/server-only
scripts и mutation через archived MCP запрещены.

Большая live SQLite база проверяется через bounded writer/WAL ownership,
coherent backup и full integrity/foreign-key gates на immutable copy, а не
долгим `integrity_check` на writer-owned file. Любая source identity, capacity,
backup, writer или rollback ambiguity оставляет mutation fail closed.

### Owner Decision И GitHub Transport

Production-mutation safety требует exact human source decision, но не
ручного GitHub transport. Relay разрешён, только если qualified executor
может доказать один exact authorization payload в visible source task,
получил его дословно с source task/thread ID и имеет non-interactive
GitHub identity с association `OWNER` или `MEMBER`. Release-gate payload обязан
сам содержать exact PR/head и merge/deploy semantics; apply-gate payload — exact
PR, deployed SHA, manifest и apply semantics. Executor не добавляет и не
расширяет authority: он не invent/synthesize/broaden-ит authorization.

Relay comment фиксирует source и executor task/thread IDs, включает
authorization payload дословно и явно отмечает transport-only semantics.
Executor после записи повторно читает comment metadata/body и использует
его exact UTF-8 digest. Missing/ambiguous payload, paraphrase, недоказанный
source binding, wrong association или head/semantic/deployed-SHA/manifest drift fail
closed и приводят к direct callback за новым decision. Владелец не обязан вручную
открывать PR, публиковать GitHub comment, запускать command или выполнять
GitHub action.

Human-authorized terminalization использует exact command:

`/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> release-gate <RELEASE_GATE_COMMENT_ID> release-gate-digest sha256:<RELEASE_GATE_COMMENT_HASH> apply-gate <APPLY_GATE_COMMENT_ID> apply-gate-digest sha256:<APPLY_GATE_COMMENT_HASH> manifest sha256:<MANIFEST_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>`

Trusted-main Actions отдельно проверяет pre-merge OWNER/MEMBER release gate на
exact head и merge/deploy semantics, затем post-merge OWNER/MEMBER apply gate
на exact PR, deployed SHA, manifest fingerprint и production-apply semantics.
Reconciliation обязана следовать после apply gate. Exact baseline/merge,
deployed ancestry, все identities/digests и canonical read-only deploy evidence
также обязательны. Только Actions-owned proof ставит `release:production`.
Ручной label, local token, одно-gate command, stale SHA/comment/digest, нарушенный
порядок или missing evidence fail closed.
После valid exact owner authorization factual post-apply reconciliation comment,
terminalization command и прочие mechanical closure actions публикует executor;
они не создают нового business/risk decision.

Finance/storage mutations дополнительно подчиняются active lease, snapshot,
capacity, backup/restore, writer/timer, SHA and reconciliation contracts из
[`10_hosted_runtime_deploy_contract.md`](10_hosted_runtime_deploy_contract.md)
и [`11_github_release_train.md`](11_github_release_train.md).

## Repository Implementation И GitHub Closure

До длительного repo analysis/implementation `CANARY_QUALIFIED` routing record и
следующий за ним `EXECUTOR_AUTONOMY_PREFLIGHT` подтверждают:

- доступ к worktree и shared Git metadata;
- `git status --short`, branch, remotes и GitHub auth;
- выполнить `git fetch --prune origin`;
- сравнить `HEAD` с current `origin/main`;
- создать separate branch/worktree от current `origin/main`;
- проверить GitHub connector/fallback и необходимые local dependencies/runtime
  paths;
- effective profile покрывает exact capability inventory/destinations без
  platform prompt;
- проверить open PR и не включать unmerged foreign changes;
- не reset/clean и не изменять чужой dirty state.

Успешный preflight завершается task-local `autonomy_ready` с exact starting
main SHA/branch и `platform_approval_count=0`. Unexpected platform prompt
обрабатывается как routing defect с safe stop и direct callback, но без
command approval request. Только истинный owner business/risk gate использует
strict human-only callback.

Executor выполняет:

1. bounded implementation и necessary docs sync;
2. targeted checks;
3. полный semantic diff review;
4. fixes и повторные checks/review;
5. explicit staging intended files, commit и push;
6. один open non-draft PR в `main` с `task:standard` и одной scope label;
7. wait/readback successful `baseline` на current exact head;
8. добавить `release:ready`;
9. ждать и проверять Release Train до terminal state;
10. проверить comments/reviews/unresolved threads, final merge/deploy SHA и
    current `origin/main`.

Release Train может синхронизировать branch с `main`; он обязан запустить fresh
baseline и снова проверить exact final head. Executor не снимает release labels
и не мутирует foreign PR/gates. Active foreign release — normal waiting.

Явная граница пользователя (`только ветка`, `до commit`, `до draft PR`, `без
merge`, `без deploy`, `без production mutations`) останавливает работу ровно на
ней и не расширяет authority.

## Проверка И Semantic Review

Для каждого change проверь:

- scope и diff hygiene;
- отсутствие secrets, production data, generated dumps и unrelated edits;
- соответствие code и authoritative docs;
- targeted tests/checks по риску;
- полный semantic diff;
- исправление findings и повторные checks/review;
- visible executor task/thread ID, zero curator `spawn_agent` calls и
  `platform_approval_count=0`;
- correct task/scope labels и successful baseline exact head;
- terminal GitHub/deploy/data state выбранного contour.

Review списка файлов не считается semantic review.

## Production UI Verification

HTTP `200`, `curl`, наличие HTML и public probe не доказывают UI render.
Минимальное evidence:

1. requested URL, final URL и redirect/document chain;
2. отсутствие `5xx` navigation/resources;
3. `DOMContentLoaded`, видимый render и непустые title/body;
4. собранные `pageerror`, fatal surface и существенные console errors;
5. screenshot final surface и его визуальная проверка.

По умолчанию используй local Playwright с fresh isolated non-persistent
Chrome/Chromium context без user profile/cookies/credentials. Не выполняй click,
input или business mutation вне explicit UI Flow. Browser/auth проверяется в
`PRODUCTION_UI_PREFLIGHT`, поэтому будущая UI capability не блокирует
repository work.

## Documentation Sync И Executor Evidence

Authoritative docs: `README.md`, `docs/architecture/*`, `docs/modules/*`,
`migration/*`. Contract/runtime/module status change обновляет их в той же
задаче.

Исполнитель не подменяет собственную verification отчётами других агентов.
Перед финальным handoff он сам проверяет routing record и approval count,
GitHub state, branch/final SHA, semantic diff, tests/checks, review threads,
docs и, если применимо, deployed SHA, live/service/UI evidence, mutation
dry-run, backup/reversibility, audit, reconciliation и non-target invariants.
Эти executor checks и механические Release Train gates являются техническим
proof; куратор после handoff не повторяет их как независимый audit.

## Human-Only Boundary

User action требуется только для owner business/risk decision, exact
production-mutation gate, credential/login/2FA/captcha без разрешённого
non-interactive path, proven irreversible data risk, security change, new
external data destination или material scope/risk change. Такой gate сообщает
direct pre-terminal callback с exact resource/effect и minimal owner decision в
visible task; hidden UI
flag не заменяет callback. `waitingOnApproval`, missing platform capability,
permission prompt и platform hard stop — routing/tooling defects, не Human
Gates. Обычные Git, GitHub, checks, review, merge, queue wait, deploy/verify и
доступная UI automation не перекладываются на пользователя. GitHub comment
transport, reconciliation и terminalization после exact owner decision также
остаются executor closure, а не новым human-only gate.

Blocker final содержит exact error, выполненную безопасную работу и один
минимальный human-only step.

## Формат Итогового Ответа

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
