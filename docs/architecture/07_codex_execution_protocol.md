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
2. Куратор создаёт ровно одного прямого видимого user-owned исполнителя и
   завершает executor prompt обязательным terminal-handoff указанием из раздела
   [Prompt Contract](#prompt-contract-и-technical-path-revalidation).
3. Исполнитель начинает от fresh `origin/main` в отдельной branch/worktree,
   немедленно выполняет executor autonomy preflight, фиксирует task-local
   `autonomy_ready` и только затем реализует bounded change, синхронизирует
   необходимые authoritative docs, запускает targeted checks и читает полный
   semantic diff.
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
создаёт ровно одного прямого исполнителя без nested curator или subagent,
задаёт имя `WBC · <та же короткая тема> · И1` и закрепляет его задачу.

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
проверки; merge/release/deploy/production state; сложности, риски и blockers.

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
через supported thread/task creation capability. Внутренний subagent, fork без
видимой task identity и реализация change в discussion-задаче не заменяют
прямого исполнителя.

Куратор передаёт исполнителю цель, ожидаемый результат, included/excluded scope,
constraints, acceptance/closure, правило technical-path revalidation и
обязательный terminal-handoff contract. Он не создаёт registry entry, passport,
monitor, heartbeat automation или callback контур.

После успешного dispatch куратор немедленно завершает текущий turn. `Ждёт`
означает quiet wait: отсутствие активных model/tool calls, а не `wait`/poll
loop. Куратор не инициирует wait/read/list/status опросы исполнителя,
GitHub/CI/runtime/production audit его работы, follow-up prompts, промежуточные
сводки, параллельную реализацию, независимую перепроверку handoff, heartbeat,
automation или любой другой мониторинговый контур.

Куратор пробуждается только по финальному handoff, direct strict human-only
pre-terminal callback исполнителя либо новому явному указанию владельца.
Обычный progress исполнителя, включая `autonomy_ready`, не является поводом
просыпаться. Вся technical verification, evidence и terminal closure до
handoff — ответственность исполнителя; куратор не создаёт второй audit
contour.

## Executor Autonomy Preflight И Silent Approval Prevention

Новый repo-backed executor выполняет `EXECUTOR_AUTONOMY_PREFLIGHT` немедленно
после dispatch и до длительного domain analysis/implementation. Он фактически
проверяет:

1. чтение/запись собственного worktree и shared Git metadata;
2. status, remotes, GitHub auth, обязательный `git fetch --prune origin` и
   возможность создать собственную branch от current `origin/main`;
3. GitHub connector и доступный fallback;
4. необходимые local dependencies/runtime paths;
5. ожидаемые permission/credential gates.

Все предсказуемые approval/permission requests front-load-ятся, пока владелец
ещё онлайн. После успешного preflight исполнитель кратко фиксирует в своей
видимой задаче `autonomy_ready`: exact starting main SHA, branch и отсутствие
ожидающего approval. Эта запись — task-local progress, не callback куратору и
не новая durable state machine; она не запускает heartbeat, automation,
periodic monitor или registry.

`waitingOnApproval`, missing permission/credential, interactive
login/2FA/captcha и platform hard stop — strict human-only boundary. Для
предсказуемого gate исполнитель до approval-gated call отправляет один короткий
pre-terminal callback в исходную кураторскую задачу. Если gate стал известен
только после platform/tool call, callback отправляется сразу после получения
возможности продолжить model turn: hidden executor UI flag не может оставаться
единственным сигналом.

Callback содержит только тип gate, точное действие/ресурс, почему безопасного
non-gated path нет, ожидаемый mutation/read effect и одно минимальное действие
владельца. Обычный progress и технический audit в него не входят. После
callback исполнитель остаётся на безопасной точке, не симулирует approval и не
обходит permission/security policy.

### Duplicate-Executor Guard После Wake Signal

`waitingOnApproval` не является разрешением создать дубль. Только после одного
из допустимых wake signals и до решения «перезапустить в новом executor»
куратор делает один bounded read-only check:

- terminal/unavailable state исходной задачи;
- worktree status и branch;
- uncommitted diff;
- commits/push;
- open PR.

При branch/diff/commit/push/PR или возможности resume продолжается тот же
executor либо фиксируется human-only blocker. Новый executor допустим только
если исходный доказанно terminal/unrecoverable и незавершённого implementation
state нет: clean untouched worktree, no branch, no commit, no push и no PR
должны быть явно доказаны. Automatic reset/clean/delete чужого state,
параллельный takeover, duplicate implementation и monitoring contour
запрещены.

## Prompt Contract И Technical Path Revalidation

Task prompt описывает результат: цель, необходимые данные, read-only/mutation
boundaries, итоговый artifact/answer и acceptance/closure.
Он не называет WebCore Data MCP. Он не назначает
connector/server/runtime/storage/SSH alias и не запрещает canonical server-side
read, если сам пользователь отдельно и явно не установил такое ограничение.

Каждый prompt содержит provenance-правило:

`Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.`

Каждый новый repo-backed executor prompt до terminal-handoff указания явно
включает contract из предыдущего раздела: немедленный
`EXECUTOR_AUTONOMY_PREFLIGHT`, task-local `autonomy_ready`, front-loaded
approval/permission requests, direct strict human-only callback, safe stop без
симуляции approval и duplicate-executor guard после wake signal. В prompt также
однозначно сказано, что `waitingOnApproval` не разрешает автоматический restart
или duplicate executor, а `autonomy_ready` не создаёт callback/heartbeat/
monitoring contour.

Каждый executor task prompt обязательно заканчивается следующим по смыслу и
составу полей указанием, после которого нет иных task directives:

`Исполнитель самостоятельно доводит задачу до применимого terminal state. После COMPLETE либо доказанного BLOCKED отправь в исходную кураторскую задачу один финальный technical handoff: итоговый статус; что сделано; что не сделано или осталось вне scope; PR и final SHA; проверки; merge/release/deploy/production state; сложности, риски и blockers.`

До terminal state исполнитель не отправляет обычный progress как callback.
Единственное pre-terminal исключение — direct strict human-only callback по
описанному выше составу, без которого безопасное продолжение невозможно.

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

Широкое предварительное разрешение пользователя покрывает последовательный
запуск заранее согласованных ordinary repo/live stages и устранение
non-material технических blockers внутри scope. Оно не делегирует exact
production-mutation gate, missing credentials/permission, login/2FA/captcha,
approval при proven irreversible data risk, security change, new external data
destination или material product/risk choice.

Отсутствие владельца не ослабляет `baseline`, GitHub Release Train, exact-SHA
deploy/verify и production-mutation evidence. Для новой unattended цепочки
curator/executor заранее пытаются довести ближайшего исполнителя до
`autonomy_ready`, но не создают зависимые implementation tasks параллельно
только ради раннего permission prompt. Quiet curator после dispatch остаётся
quiet до одного из трёх допустимых wake signals.

## Phase-Local Preflight И Production Safety

Preflight не является глобальным барьером. Dependency order:

1. `REPOSITORY_PREFLIGHT` — repository/worktree, AGENTS/docs/code, local
   dependencies, tests и GitHub baseline.
2. Repository implementation/validation, repo-owned runner на fixtures/mocks,
   branch/PR, CI и review.
3. `PRODUCTION_READ_PREFLIGHT` — только перед конкретным production read.
4. `PRODUCTION_MUTATION_PREFLIGHT` — непосредственно перед apply: exact scope,
   dry-run/coverage, manifest/digests, backup/restore, expected records,
   non-target invariants, authorization, deployed runner/version и
   reconciliation.
5. `PRODUCTION_UI_PREFLIGHT` — только перед production UI verify.

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

Human-gated terminalization использует exact command:

`/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> gate <GATE_COMMENT_ID> gate-digest sha256:<GATE_COMMENT_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>`

Trusted-main Actions проверяет owner/member gate, exact head/baseline/merge,
deployed ancestry, reconciliation identities/digests и canonical read-only
deploy evidence. Только Actions-owned proof ставит `release:production`.
Ручной label, local token, stale SHA/comment/digest или missing evidence fail
closed.

Finance/storage mutations дополнительно подчиняются active lease, snapshot,
capacity, backup/restore, writer/timer, SHA and reconciliation contracts из
[`10_hosted_runtime_deploy_contract.md`](10_hosted_runtime_deploy_contract.md)
и [`11_github_release_train.md`](11_github_release_train.md).

## Repository Implementation И GitHub Closure

До длительного repo analysis/implementation `EXECUTOR_AUTONOMY_PREFLIGHT`
подтверждает:

- доступ к worktree и shared Git metadata;
- `git status --short`, branch, remotes и GitHub auth;
- выполнить `git fetch --prune origin`;
- сравнить `HEAD` с current `origin/main`;
- создать separate branch/worktree от current `origin/main`;
- проверить GitHub connector/fallback и необходимые local dependencies/runtime
  paths;
- front-load-ить предсказуемые permission/credential gates;
- проверить open PR и не включать unmerged foreign changes;
- не reset/clean и не изменять чужой dirty state.

Успешный preflight завершается task-local `autonomy_ready` с exact starting
main SHA/branch и подтверждением отсутствия pending approval. Gate вместо
success обрабатывается через direct strict human-only callback; скрытый
`waitingOnApproval` не считается достаточным уведомлением.

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
Перед финальным handoff он сам проверяет GitHub state, branch/final SHA,
semantic diff, tests/checks, review threads, docs и, если применимо, deployed
SHA, live/service/UI evidence, mutation dry-run, backup/reversibility, audit,
reconciliation и non-target invariants. Эти executor checks и механические
Release Train gates являются техническим proof; куратор после handoff не
повторяет их как независимый audit.

## Human-Only Boundary

User action требуется только для `waitingOnApproval`, missing
credential/permission/approval, interactive login/2FA/captcha, proven
irreversible data risk, security change, new external data destination,
material scope/risk change или platform hard stop. Такой gate сообщает direct
pre-terminal callback с exact resource/effect/minimal action; hidden executor
UI flag не заменяет callback. Обычные Git, GitHub, checks, review, merge, queue
wait, deploy/verify и доступная UI automation не перекладываются на
пользователя.

Blocker final содержит exact error, выполненную безопасную работу и один
минимальный human-only step.

## Формат Итогового Ответа

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
