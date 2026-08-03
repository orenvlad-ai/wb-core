# GitHub Release Train

## Назначение

GitHub Release Train — repo-owned сериализованная очередь для независимо подготовленных STANDARD и LOOP change-задач. Durable state хранится только в GitHub PR, labels, checks, comments и workflow runs. Очередь владеет критической секцией `sync -> baseline -> merge -> deploy -> verify`, но не заменяет task-level targeted checks, semantic review и docs sync.

Non-PR `user-artifact`, где единственная mutation — пользовательский файл вне репозитория, в Release Train не входит и не получает отдельный scope label. Если меняются Git-tracked protocol/docs/helper, это уже обычный `scope:repo-only` PR и данный Release Train contract применяется полностью.

Класс задачи и execution contour независимы. PR обязан иметь ровно одну task label и ровно одну scope label:

- `task:standard` или `task:loop`;
- `scope:repo-only`, `scope:live-runtime` или `scope:production-mutation`.

`task:loop` совместим только с `scope:live-runtime`. Диагностические задачи строго read-only и в Release Train не входят.

Task class и task continuity независимы. `TaskContinuity` в `apps/github_release_train_spec.py` различает `NEW_TASK`, `ACTIVE_ADDITION`, `ACTIVE_LOOP_RECOVERY`, `TERMINAL_STALE_REFERENCE`. Только явное дополнение к active task наследует её branch/PR; только defect текущего незавершённого UI acceptance может стать same-root recovery.

Одинаковый чат, экран или функциональная область continuity не доказывают. Фразы «новая/отдельная/самостоятельная задача» и «новый LOOP» принудительно создают новую identity; неоднозначность также даёт `NEW_TASK`. После `release:done`, `release:production` или `release:superseded` запрещено наследовать branch, PR, task identity, LOOP root, acknowledgement, owner heartbeat и recovery identity. Новый дефект после terminal closure всегда получает новый PR/root.

## Repo-Owned Артефакты

- `.github/workflows/baseline-ci.yml` — обязательный check `baseline`;
- `.github/workflows/release-train.yml` — один repository-wide queue worker, GitHub-native LOOP handler, Actions-owned global Finance migration deploy lease и two-stage trusted-main production-mutation terminalizer;
- `apps/github_release_train.py` — GitHub API/state-machine runner;
- `apps/github_release_train_wait.py` — bounded CLI waiter и канонический Goal queue shepherd для Codex;
- `apps/github_release_train_smoke.py` — deterministic state-machine smoke;
- `apps/codex_task_orchestrator.py` — local registry, Watcher lease, incidents, resource locks, reports и dashboard;
- [`12_codex_global_orchestration.md`](12_codex_global_orchestration.md) — authoritative admission/lane и Desktop orchestration contract;
- `.github/pull_request_template.md` — PR closure checklist.

## Eligibility И Labels

Queue eligibility требует одновременно:

- open non-draft PR в `main`;
- same-repository head branch;
- `release:ready`;
- ровно одну известную `task:*` label;
- ровно одну известную `scope:*` label;
- отсутствие `release:blocked`, `release:halted` и `release:superseded`.

При `WB_CORE_ORCHESTRATION_REQUIRED=true` любой STANDARD и LOOP дополнительно требуют exact-head Actions-owned orchestration admission proof и совпадающий logical task на active `release:lane-owner`. LOOP также требует exact-head repo-owned new/recovery registration proof. Ручные `loop:root-*`, `release:ready`, admission marker или lane label eligibility не доказывают. Feature flag по умолчанию выключен до end-to-end Desktop пилота.

Основные state/lease labels:

- `release:staged` — STANDARD executor закончил pre-release proof и ждёт exact orchestration admission;
- `release:ready` — trusted-main command доказал admission/lane (и для LOOP registration) и поставил PR в очередь;
- `release:lane-owner` — отдельный logical-task lease на anchor PR; он удерживает critical lane через несколько PR/deploy/UI/recovery;
- `release:running` — worker выполняет sync/baseline/release;
- `release:awaiting-agent` — LOOP прошёл sync/baseline и ждёт exact-head acknowledgement активной Codex-сессии;
- `release:needs-resume` — non-terminal overlay на активном LOOP `ready/running/awaiting-agent/awaiting-ui`: owner heartbeat истёк, но primary state и gate не изменяются;
- `release:awaiting-ui` — LOOP merge задеплоен и ждёт production UI Flow/acceptance;
- `release:blocked` — PR-specific fail-closed state; обычно pre-merge, а human-gated production mutation может сохранять его после merge/apply до exact terminalization;
- `release:done` — terminal success STANDARD `repo-only` без deploy;
- `release:production` — terminal success STANDARD live/runtime, Actions-terminalized human-gated production mutation или принятой LOOP-цепочки;
- `release:halted` — failure после merge; вся очередь остановлена.
- `release:superseded` — terminal audit state незамёрженной LOOP-итерации, однозначно заменённой завершённой production recovery-chain; root/task/scope/history сохраняются, активные queue/failure labels снимаются.
- `release:retired` — terminal exact-evidence state для перечисленного legacy manifest; он не создаёт новую release identity и не снимает labels вручную.

Active states: `release:staged`, `release:ready`, `release:running`, `release:awaiting-agent`, `release:awaiting-ui`, `release:needs-resume`, `release:blocked`, `release:halted`. Terminal states: `release:done`, `release:production`, `release:superseded`, `release:retired`. `release:lane-owner` — не state, а lease overlay, который может оставаться на terminal anchor до closure всей логической задачи. Terminal state является жёсткой identity boundary и не имеет перехода обратно в очередь.

Каноническая машинная спецификация живёт в `apps/github_release_train_spec.py`: task class, continuity, active/overlay/terminal sets, transition matrix, critical transitions, monitor query, marker names и Goal disposition contract. Runtime, waiter/shepherd и smoke импортируют её, а AGENTS/docs проверяются regression assertions. Primary states взаимоисключающие, кроме временной `ready+running`; `needs-resume` — только overlay. State/identity registration заменяет полный label set одним GitHub API call, поэтому не оставляет между add/remove временного conflicting state. Ручно добавленный label не является proof: LOOP registration/recovery, ack, terminal completion, deployed UI gate, acceptance, halted recovery и production-mutation completion требуют repo-owned marker и exact PR/head/gate/merge/deployed/evidence.

`finance:migration-deploy-lease` — отдельный global fail-closed lease label на
уже terminal production anchor PR. Он не меняет terminal release identity, но
пока присутствует, блокирует selection/merge/deploy всех unrelated PR. Ручной
label без contiguous Actions-owned binding history считается ambiguous и
также блокирует очередь; он не разрешает ни одной Finance migration action.
`finance:migration-lease-recovery` допускается только на одном exact
owner-bound STANDARD live-runtime recovery PR и действует лишь вместе с
bot-owned proof текущих anchor/task/lease/revision/head.

Goal disposition является отдельной интерпретацией durable state, а не новым transition graph:

- `TERMINAL_SUCCESS` — применимый terminal state подтверждён repo-owned exact-SHA proof;
- `CONTINUE_WAITING` — штатное ожидание own/foreign queue state;
- `CONTINUE_SAFE_PHASES` — будущая production capability недоступна, но dependency plan ещё содержит безопасную исполнимую repository work;
- `AWAIT_PHASE_CAPABILITY` — все независимые safe phases завершены, а непосредственный production/UI step ждёт доказанную внешнюю capability;
- `OWN_ACTION` — доступно действие над собственным PR или canonical reconciliation;
- `TAKEOVER_PREDECESSOR` — чужой predecessor имеет доказанный lost-owner overlay и безопасный resume path;
- `RECOVER_OWN_CHAIN` — нужно завершить UI/recovery собственной LOOP-chain;
- `EXTERNAL_BLOCKER` — требуется human/external authority, repo-owned actions отсутствуют и remediation исчерпана;
- `TERMINAL_FAILURE` — evidence доказывает невосстановимую ошибку протокола после исчерпания remediation.

Каждый результат содержит `disposition`, `own_pr`, `action_pr`, `canonical_github_state`, `reason_code`, `allowed_next_action`, `user_intervention_required`, `evidence`, `remediation_exhausted`, `current_phase`, `blocked_phase`, `safe_phases_remaining`, `required_capability`, `capability_evidence`, `next_executable_action`. `EXTERNAL_BLOCKER` конструктивно запрещён, если evidence содержит доступную repo-owned команду или `safe_phases_remaining` непуст. `AWAIT_PHASE_CAPABILITY` не является terminal failure всей цели: он допустим только на непосредственной phase boundary с фактическим capability preflight, исчерпанным repo-owned remediation и минимальным human-only действием.

## Phase-Local Production Gates

Одна machine specification также задаёт dependency order и четыре независимых preflight boundary:

- `REPOSITORY_PREFLIGHT` читает repository/worktree, `AGENTS.md`, architecture/runners, local dependencies, tests и при необходимости GitHub baseline; production credentials/database, MCP, browser, manifests, digest и backup ему не нужны;
- `PRODUCTION_READ_PREFLIGHT` запускается только перед конкретным read-only production evidence и проверяет лишь требуемую capability/source;
- `PRODUCTION_MUTATION_PREFLIGHT` запускается непосредственно перед apply и доказывает bounded scope, dry-run/coverage, manifest/digests, backup/restore, expected records, non-target invariants, authorization, exact deployed runner/version и reconciliation;
- `PRODUCTION_UI_PREFLIGHT` запускается только перед UI acceptance и проверяет local Playwright/Chromium плюс authorization, реально нужную этой navigation/operation.

Prompt order не является dependency order. Для production-data flow каноническая последовательность: `repository development → fixtures/tests → repo-owned runner → PR/CI/review → deploy runner → production read/dry-run → backup/manifests/digests/evidence → explicit apply → readback/reconciliation → UI acceptance`. Невозможность выполнить поздние steps не блокирует ранние. До production gate runner всё равно реализуется и тестируется на fixtures/mocks; он имеет dry-run default, отдельный apply flag, bounded scope, machine-readable manifest, pre-change digest, backup/evidence contract, expected affected records, non-target invariants, idempotency/documented recovery и post-apply reconciliation. Ad-hoc/local/server-only scripts production mutation не выполняют.

Production read evidence по умолчанию собирается через current canonical server-side path: actual target/SSH preflight, query-only store access и bounded server-owned document reads по current repo/docs truth. Архивный WebCore Data MCP не выбирается shepherd как capability, prerequisite или fallback; его отсутствие никогда не образует blocker. `EXTERNAL_BLOCKER` для production read допустим только после exact canonical SSH/store/document error либо доказанного отсутствия данных.

## LOOP Registration И Root Invariants

Новый LOOP и recovery ставятся в очередь разными trusted-main `issue_comment` operations. Перед command уже должны существовать open non-draft PR, `task:loop + scope:live-runtime`, exact head и successful `baseline`.

Новый самостоятельный LOOP:

```bash
gh pr comment <PR> --body "/wb-core loop enqueue-new <PR> head <HEAD_SHA>"
```

Handler создаёт `loop:root-<PR>`, machine new-root proof и атомарно выставляет `release:ready`. Такой root может ждать за чужим active UI gate; это normal waiting.

Recovery текущего active UI Flow:

```bash
gh pr comment <RECOVERY_PR> --body "/wb-core loop enqueue-recovery <RECOVERY_PR> head <HEAD_SHA> gate <ACTIVE_GATE_PR> root <ROOT>"
```

Handler доказывает active merged `release:awaiting-ui` gate, его exact deploy/root proof, отсутствие terminal member и exact root, затем создаёт recovery proof и одним label replacement выставляет root/ready. Инварианты: `root == PR` — new chain; `root < PR` — recovery exact active gate; `root > PR` — invalid. Исчезнувший gate, terminal root, manual label или mismatching proof являются classification error; merge/deploy запрещены, status comment содержит точный code/reason, другие PR/roots не изменяются.

Repeated enrollment events идемпотентны, включая отложенную повторную доставку после перехода PR в `running`, `awaiting-agent` или `blocked`: доказанная exact identity остаётся неизменной, state не откатывается в `ready`, workflow повторно не dispatch-ится. Underlying runner operations называются `enqueue-loop-new` и `enqueue-loop-recovery`, но durable proof создаёт trusted-main command handler; agents не назначают root/ready вручную.

При включённом `WB_CORE_ORCHESTRATION_REQUIRED` repo-owned LOOP registration создаёт identity/ready, но selection и merge дополнительно требуют exact orchestration admission и совпадающий logical lane. Для same-root recovery активный predecessor `release:awaiting-ui` разрешает той же logical task сохранить lane; другая task ждёт.

## STANDARD Flow

STANDARD executor после targeted checks, semantic review, fixes/recheck и docs sync ставит `release:staged`, а не `release:ready`. Глобальный Watcher читает exact Task Passport/revision/head и публикует trusted-main command:

```text
/wb-core orchestration admit <PR> head <HEAD_SHA> task <TASK_ID> revision <REVISION> passport sha256:<PASSPORT_DIGEST>
```

Handler доказывает current PR/head, successful baseline, exact registration и available logical lane, создаёт Actions-owned admission marker, приобретает/проверяет `release:lane-owner` и атомарно переводит PR в `release:ready`. Если lane принадлежит другой задаче, PR остаётся staged в normal waiting. Если lane принадлежит той же задаче, следующий PR получает admission proof, но ждёт terminal/releasable predecessor без потери mapping.

После адмиссии STANDARD PR проходит существующую последовательность без agent acknowledgement:

1. worker выбирает старейший eligible PR;
2. синхронизирует branch с current `main`;
3. явно dispatch-ит `baseline-ci.yml` и ждёт новый successful `baseline` на final head SHA;
4. повторно проверяет exact head/base/task/scope/mergeability и admission/lane;
5. squash-merges только проверенный head;
6. `scope:repo-only` получает `release:done` без deploy;
7. `scope:live-runtime` checkout-ит exact merge SHA, вызывает canonical `deploy-and-verify` и получает `release:production`;
8. worker best-effort удаляет feature branch и dispatch-ит следующий queue run.

`scope:production-mutation` никогда не выпускается автоматически и до merge получает `release:blocked` с требованием отдельного human-gated production-mutation protocol.

Если trusted-main sync изменил STANDARD head, старый admission больше не exact. Worker без blocker возвращает PR в `release:staged`, оставляет audit comment и ждёт re-admission нового head. Непосредственно перед merge exact admission/lane проверяются снова.

Logical lane освобождает только trusted-main command после terminal closure всей задачи либо доказанно safe parking:

```text
/wb-core orchestration release-lane <ANCHOR_PR> task <TASK_ID> outcome <completed|parked> evidence sha256:<EVIDENCE_HASH>
```

Parking запрещён при merged ambiguity, `release:running`, `release:awaiting-ui` или `release:halted`. Закрытый/merged legacy backlog из versioned manifest переводится в `release:retired` только `/wb-core orchestration retire-legacy ...` после exact head/merge/manifest/digest proof из trusted `main`; ручное снятие legacy labels запрещено.

## Production-Mutation Terminalization

Human merge/deploy/apply не выполняется queue worker и не возникает из `release:ready`. После отдельного exact human gate, exact-head merge, canonical deploy/apply и bounded reconciliation `task:standard + scope:production-mutation` закрывается только PR comment:

```text
/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> gate <GATE_COMMENT_ID> gate-digest sha256:<GATE_COMMENT_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>
```

Two-stage workflow разделяет authority:

1. `production_mutation_command` checkout-ит trusted `main` без production environment и требует command actor `OWNER`/`MEMBER`, current PR, `task:standard + scope:production-mutation`, merged GitHub state, exact retained pre-merge head, successful `baseline` на exact head, exact merge SHA и fail-closed `blocked/halted` state. Gate comment обязан принадлежать тому же PR, иметь `OWNER`/`MEMBER`, предшествовать merge, содержать exact head и human-authorization semantics; его exact UTF-8 body SHA-256 совпадает с command. Reconciliation comment отличается от gate, следует после merge, принадлежит `OWNER`/`MEMBER`, содержит exact deployed SHA, completion semantics и exact 64-hex payload command evidence fingerprint (исторический comment может не иметь текстового `sha256:` prefix); его body SHA-256 также совпадает. GitHub compare доказывает, что deployed SHA равен merge либо является его потомком.
2. Только успешный preflight открывает `terminalize_production_mutation` с environment `production`. Existing hosted-runtime reconciler запускается с обязательным `--read-only` и сверяет canonical target id, deploy metadata SHA, runtime SHA, `deployment_complete=true`, auth binding, active unit/MainPID и loopback probes с exact deployed SHA. В этом режиме он не выполняет deploy, daemon-reload, restart, repair probes, business mutation или reconciliation apply.
3. Trusted-main handler повторно проверяет immutable GitHub evidence, связывает canonical deploy evidence digest с PR/head/merge/deployed SHA, gate identity/actor/association/digest, reconciliation identity/actor/association/digest и evidence fingerprint. Только GitHub Actions создаёт `wb-core-production-mutation-completion-proof`, атомарно заменяет stale active/failure/overlay state на `release:production` и dispatch-ит queue observation.

Повтор exact command после proven terminal state возвращает `already-completed` без новых comments/labels и безопасно re-dispatch-ит queue observation. Stale head/SHA/comment digest, missing gate/deploy/reconciliation/evidence, unauthorized actor, wrong PR/task/scope, non-ancestor deployed SHA, forged owner marker или local invocation fail closed. Terminal proof readback повторно проверяет current source-comment digests и bot-owned marker; ручной `release:production` не даёт `TERMINAL_SUCCESS`.

## Global Finance Migration Deploy Lease

Finance raw/operational migration использует отдельный GitHub-owned lease до
любого нового snapshot plan, coherent snapshot, capacity/fingerprint,
candidate/backfill, live-tail, cutover или rollback action. Durable authority
остаётся в PR labels и Actions-owned comments; private readback JSON является
только свежим переносимым доказательством для hosted runner.

Acquire выполняется comment на terminal deployed anchor PR:

```text
/wb-core finance-lease acquire <ANCHOR_PR> head <HEAD_SHA> deployed <DEPLOYED_SHA> task <TASK_ID> lease <LEASE_ID> window <WINDOW_ID> phase <PHASE> ttl-minutes <30..4320>
```

Тот же repository-wide `wb-core-production-release` concurrency сериализует
command с Release Train. Trusted-main handler требует `OWNER`/`MEMBER`,
proven terminal anchor, exact head, current canonical deployed SHA readback,
anchor merge/descendant relation, bounded ttl и полное отсутствие
`release:running`, `release:awaiting-agent`, `release:awaiting-ui` и
`release:halted`. Сначала ставятся audit guard
`finance:migration-deploy-lease-audit` и active global hold
`finance:migration-deploy-lease`, затем создаётся bot-owned binding proof.
Если transition прервался после labels, lease readback остаётся `ambiguous`, а
очередь уже удерживается fail-closed; если labels не были созданы, GitHub state
не изменился. Потеря только hold либо только audit label при нетерминальном
binding также остаётся `ambiguous`. Повтор той же command механически завершает
тот же acquire и не создаёт второй lease.

Lease не auto-releases при истечении ttl. После `expires_at` status становится
`stale`, `allows_finance_migration=false`, но global label и deploy hold
остаются. Любой missing revision, duplicate anchor, conflicting proof, lost
owner или invalid terminal anchor также даёт `ambiguous` без silent-open.
Fresh private status:

```bash
python3 apps/github_release_train.py finance-lease-status \
  --require-active --output /private/path/finance-deploy-lease.json
```

Readback имеет contract
`wb_core_finance_migration_deploy_lease_readback_v1`, exact
task/anchor/head/deployed SHA, lease/window/phase/revision, acquired/expiry
timestamps, recovery policy и `baseline_invalidation_epoch`. Hosted Finance
commands требуют этот файл вне Git, не старше пяти минут; remote
`apps/finance_storage_split.py` повторно сверяет его с canonical
`.wb-core-runtime-sha`. Поэтому любой pre-acquire deploy, later SHA drift,
revision change или expired/lost lease инвалидирует старые
baseline/snapshot/plan/fingerprint evidence до записи Finance destination
bytes.

При необходимости code recovery сначала авторизуется единственный exact PR:

```text
/wb-core finance-lease authorize-recovery <ANCHOR_PR> task <TASK_ID> lease <LEASE_ID> revision <REVISION> recovery-pr <RECOVERY_PR> head <RECOVERY_HEAD_SHA>
```

Требуются open non-draft `task:standard + scope:live-runtime` и successful
exact-head `baseline`. Пока lease активен, selection, prepare и merge повторно
разрешают только этот PR; все unrelated ready PR остаются held. После recovery
deploy старый lease SHA больше не разрешает migration. Точное rebind
обязательно:

```text
/wb-core finance-lease rebind <ANCHOR_PR> deployed <RECOVERY_MERGE_SHA> task <TASK_ID> lease <LEASE_ID> revision <CURRENT_REVISION> window <NEW_WINDOW_ID> phase <PHASE> recovery-pr <RECOVERY_PR> ttl-minutes <30..4320>
```

Lost/stale owner без deploy использует тот же fail-closed revalidation через
`resume` без `recovery-pr`; он создаёт следующую revision даже при том же SHA.
Каждый rebind/resume меняет `baseline_invalidation_epoch`, поэтому никакой
старый plan/fingerprint не переносится через recovery/re-dispatch.

```text
/wb-core finance-lease resume <ANCHOR_PR> deployed <CURRENT_DEPLOYED_SHA> task <TASK_ID> lease <LEASE_ID> revision <CURRENT_REVISION> window <NEW_WINDOW_ID> phase <PHASE> ttl-minutes <30..4320>
```

Lease terminalization никогда не выводится из elapsed time или отсутствия
owner. После exact migration abort либо post-cutover reconciliation owner
оставляет отдельный structured reconciliation comment с exact
`task/lease/revision/deployed/evidence` и всеми machine tokens:
`manual_barrier=released`, `writers=restored`, `timers=restored`,
`policy=restored`, `non_target=unchanged`, `sha_readback=exact`; abort
дополнительно требует `migration_abort=complete canonical_source=monolith`, а
normal release —
`post_cutover_reconciliation=complete canonical_source=split`. Затем:

```text
/wb-core finance-lease <abort|release> <ANCHOR_PR> task <TASK_ID> lease <LEASE_ID> revision <REVISION> deployed <DEPLOYED_SHA> reconciliation <COMMENT_ID> reconciliation-digest sha256:<COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>
```

Production-environment job снова независимо читает canonical deployed SHA,
проверяет exact comment identity/digest и только Actions-owned terminal marker
может снять active global label и audit guard. Bot-owned terminal marker
остаётся на PR как история закрытого lease.
Marker-before-label-removal и repeated exact command делают
disconnect/re-dispatch recoverable: partial terminalization остаётся blocked,
но не требует повторять migration mutation.

## LOOP Pre-Deploy Handshake

LOOP нельзя merge/deploy автоматически только потому, что он стал первым в очереди. Первый worker pass выполняет sync и baseline, затем ставит `release:awaiting-agent` и прекращает release. Это состояние является глобальным fail-closed gate: пока активная сессия не подтвердит готовность, остальные PR ждут и production не меняется.

Repo-owned waiter:

```bash
python3 apps/github_release_train_wait.py <PR>
```

Увидев `release:awaiting-agent`, waiter публикует на этом PR единственную bounded GitHub mutation — точный comment:

```text
/wb-core loop ack-agent <PR> head <EXACT_40_CHAR_HEAD_SHA>
```

Workflow принимает command только от `OWNER`, `MEMBER` или `COLLABORATOR`, проверяет номер PR, open/non-draft state, `task:loop + scope:live-runtime`, recovery linkage и текущее exact head. Принятый ack кодируется одноразовой label `loop:ack-<HEAD_SHA>`, возвращает PR в `release:ready` и dispatch-ит worker.

На втором pass baseline снова доказывается для того же head. Ack удаляется непосредственно перед merge API call. Изменение head на любом этапе делает старую label невалидной; worker снова ставит `release:awaiting-agent`. Каждый recovery PR имеет другой PR/head identity и требует собственного acknowledgement.

Waiter ведёт на каждом активном PR ровно один marker-based status comment: task title, class, stage, queue reason/position, loop root, last action, intervention и exact resume command. Heartbeat обновляет этот comment, а дубли удаляются. `--no-ack-agent` запрещает ack; status heartbeat остаётся единственной idempotent ownership mutation.

Чужой exclusive gate означает только waiting. Ни количество одинаковых polls/goal-turns, ни длительность, ни отсутствие GitHub changes не переводят его в `release:blocked` и не разрешают снимать, обходить или перехватывать gate. Task owner продолжает waiter/heartbeat до своей очереди; при исчерпании текущего goal-turn создаётся следующий bounded turn на продолжение того же Goal, а не terminal handoff открытого PR.

Workflow запускает queue observation каждые пять минут. Если LOOP status heartbeat на `ready/running/awaiting-agent/awaiting-ui` старше `WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES` (default `30`), worker идемпотентно добавляет overlay `release:needs-resume` и обновляет status comment командой `python3 apps/github_release_train_wait.py <PR> --resume-owner --no-ack-agent`. Это доступный takeover-path, не blocker. Resume comment-command привязан к PR, exact head и root; он снимает overlay и обновляет owner heartbeat, но не выполняет acknowledgement или acceptance. Повторный resume безопасен и возвращает промежуточный код `4`.

Shepherd выдаёт `TAKEOVER_PREDECESSOR` только при одновременных machine evidence: `release:needs-resume`, exact status `owner=unowned`, отсутствие подтверждённого живого owner, проверенный exact head/root, для UI gate — exact deployed SHA, repo-owned resume command и сохранение root isolation. Takeover без overlay запрещён. После resume агент восстанавливает predecessor context из PR/status/diff/docs, завершает его точный stage, выполняет UI Flow при `awaiting-ui`, принимает только exact deployed SHA, ждёт terminal predecessor и повторно продолжает shepherd собственного PR. UI defect создаёт same-root recovery либо сохраняет gate fail-closed. Resume/takeover никогда автоматически не выполняет ack-agent или accept-ui.

## Глобальный Watcher И Canonical Monitoring

Post-plan `DISPATCH_REQUEST` создаёт отдельную user-owned Codex task и подтверждает её через `TARGET_CREATE_READBACK`. Та же launch operation формирует versioned Task Passport, pin/title curator и executor, регистрирует exact task/thread/PR resources в локальном registry и проверяет одно active generation глобального Watcher. Per-task heartbeat automation больше не является частью контракта.

Один Luna Watcher каждые 10 минут читает local registry, exact Codex thread snapshots и read-only `python3 apps/github_release_train.py queue-status`. Он не хранит release truth в chat history и не подменяет Release Train. Watcher может выполнять только deterministic registration, bounded retry/replacement, incident arbitration, exact orchestration admission/lane release и bounded idle follow-up; merge/deploy/ack/UI acceptance выполняют существующие repo-owned paths и task owner по exact evidence.

Для локального Watcher `queue-status` принимает явный `GITHUB_TOKEN` либо, только вне GitHub Actions, безопасно читает credential из уже авторизованного `gh`. Credential не выводится и не становится local-registry state. Actions без `GITHUB_TOKEN` и все mutation-команды по-прежнему fail closed.

Каждый run получает generation-bound lease. Exact threads читаются пакетами не более восьми, active turns только наблюдаются. Registration является основным acquisition path; fallback-discovery разрешён только для pinned tasks с доказанным project/repository `orenvlad-ai/wb-core`. Сторонние, projectless, личные и медицинские chats исключены.

Отчёт по active task имеет только поля `Статус`, `Задача`, `Прогресс · Осталось`, `С прошлого отчёта`, `Сейчас`; `Блокер` появляется только при доказанной strict human-only причине. После фразы владельца «Задача принята» task становится `ACCEPTED` и пропадает из следующего отчёта; Watcher не выполняет unpin.

Повтор failure fingerprint хранится в registry: bounded retry → unclaimed incident и replacement для второй пустой system error → claim/Sol arbiter на третьей; успешный replacement stales неclaim-нутый case. Одинаковая содержательная ошибка открывает incident на третьем наблюдении. Один active incident на task, resource locks и stale revision/evidence checks исключают конкурирующие решения. Арбитр получает Task Passport и fresh bounded evidence, а не chat history; после доказанного transition Watcher сначала архивирует arbiter thread и затем закрывает incident.

Cadence намеренно различается: GitHub Actions наблюдает durable queue каждые пять минут, CLI waiter обновляет PR ownership heartbeat по своему contract, а Desktop Watcher наблюдает Codex tasks каждые 10 минут. Watcher не меняет `WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES`, не доказывает живого LOOP owner без exact-head status heartbeat и не обходит `release:needs-resume`.

Ротация Watcher выполняется `prepare → smoke → atomic activate → old generation no-op → pause old automation → archive old task`. Registry/JSONL/dashboard живут локально на Mac; Mac и Codex Desktop должны быть включены. Внешний управляющий сервис, Entire и Telegram не являются v1 dependency. Полный контракт: [`12_codex_global_orchestration.md`](12_codex_global_orchestration.md).
## Exclusive Production UI Gate

После успешного LOOP merge, canonical deploy и production verify worker не ставит terminal success и не dispatch-ит следующий release. Он повторно проверяет зарегистрированный root/proof, ставит текущей итерации `release:awaiting-ui` и завершает job. Push-triggered или повторный queue run видит gate и не выбирает несвязанный PR.

Если production UI Flow не принят, исчезновение Codex не открывает очередь: `release:awaiting-ui` остаётся durable fail-closed state.

UI Flow следует production UI contract из [`07_codex_execution_protocol.md`](07_codex_execution_protocol.md). HTTP `200`, `curl`, наличие HTML или только canonical public probe недостаточны: требуется фактический browser render с DOM/final URL, отсутствием `5xx`/`pageerror`/fatal surface, классификацией существенных console errors и визуально проверенным screenshot. В Codex CLI сразу используется Playwright с новым изолированным Chrome/Chromium context; встроенный Browser в CLI недоступен и не требуется. В ChatGPT web/desktop встроенный Browser допустим, если доступен. Пользовательский profile/cookies/credentials и любые clicks/input/business mutations запрещены по умолчанию. Если UI Flow не проходит, gate остаётся fail-closed.

CLI preflight: `python3 apps/github_release_train_wait.py <ACTION_PR> --playwright-preflight`. Helper фактически импортирует local Playwright и запускает fresh isolated non-persistent Chromium context. Browser session не нужна для repository development и проверяется только в `PRODUCTION_UI_PREFLIGHT`; будущая UI gate не останавливает code/tests/PR. Успех продолжает UI Flow независимо от embedded Browser. Публичная/неавторизованная проверка выполняется, если достаточна текущему этапу. Ошибка сначала означает repo-owned repair action; browser `EXTERNAL_BLOCKER` допустим только после зафиксированных import/launch errors, исчерпанного восстановления, `repo_owned_action_available=false`, `remediation_exhausted=true` и нового human permission/authority. Auth blocker также требует фактической navigation/auth evidence и относится только к конкретной требующей auth операции.

При успешном UI Flow активная Codex-сессия оставляет точную GitHub-native command на текущей итерации:

```bash
gh pr comment <ACTIVE_LOOP_PR> --body "/wb-core loop accept-ui <ACTIVE_LOOP_PR> deployed <MERGE_SHA> evidence sha256:<EVIDENCE_HASH>"
```

Handler проверяет write association, active latest gate, exact deployed merge SHA, repo-owned deploy proof и evidence fingerprint. Он идемпотентно оставляет `release:production` только terminal PR, нормализует chain и dispatch-ит следующий queue run. Acceptance более старой итерации после recovery отклоняется.

Terminal cleanup механический, root-bounded и idempotent. До первой state mutation он проверяет repo-owned new/recovery proof каждого участника exact root; ручной same-root label делает membership неоднозначным и fail-closed. Только последний принятый PR/exact deployed SHA остаётся `release:production`. Предыдущие merged members того же exact root теряют active/failure/overlay и ложные terminal labels, сохраняют task/scope/root и получают один audit comment с terminal PR/SHA. Доказанно заменённые unmerged predecessors получают `release:superseded`, теряют active/failure labels, получают audit comment и закрываются not planned. Более новый или неоднозначный member запрещает auto-cleanup; другие roots не мутируются.

## Recovery PR

Во время `release:awaiting-ui` продолжить gated chain может только recovery с exact repo-owned recovery proof. Связь не извлекается из title/body/free text и не доказывается ручной label: proof связывает recovery PR/head с конкретными gate PR и root и действителен только пока этот gate активен, а root не terminal.

Одновременно могут существовать несколько LOOP roots, но global workflow concurrency `wb-core-production-release` допускает ровно один merge/deploy/reconcile. Чужой awaiting-ui держит остальные roots в normal waiting; только same-root recovery может продолжить chain. После terminal acceptance worker dispatch-ит следующий oldest ready root.

Несвязанные STANDARD, независимые LOOP roots и production-mutation PR сохраняют `release:ready`, но не выбираются. Recovery проходит новый baseline и новый exact-head acknowledgement. После его deploy `release:awaiting-ui` снимается с прежней итерации и ставится recovery PR; root label не меняется. Повторный transfer command лечит допустимый duplicate-gate partial state в пользу новой итерации, а неоднозначные roots оставляют очередь fail-closed.

## CLI Waiter Contract

`apps/github_release_train_wait.py` получает номер PR, выводит только изменения `class/scope/state/head/queue/gate` и использует GitHub CLI auth/repository context, если env не задан.

- STANDARD ждёт `release:done` для `scope:repo-only` или proven `release:production` для `scope:live-runtime`/terminalized `scope:production-mutation`;
- чужой exclusive gate выводится как normal `wait-foreign-gate`; waiter продолжает polling без terminal timeout и никогда не называет это blocked;
- LOOP заново читает actual head, автоматически выполняет exact-head ack только на собственном `release:awaiting-agent` и продолжает polling через merge/deploy;
- до heartbeat/resume/ack LOOP waiter проверяет new/recovery registration proof и terminal boundary;
- LOOP возвращает код `3` на `release:awaiting-ui`, чтобы Codex выполнил UI Flow;
- повторный запуск после acceptance ждёт `release:production`;
- legacy waiter возвращает код `2` на собственные `release:blocked`/`release:halted` и conflicting durable gates; Goal не использует этот код без последующего canonical shepherd, который сначала ищет retry/reconciliation/takeover;
- `Ctrl-C` возвращает `130`;
- `--poll-seconds` задаёт bounded polling interval, `--status-seconds` и backward-compatible `--timeout-seconds` — только heartbeat; elapsed time не является terminal condition, polling не содержит AI-цикла.

Goal/shepherd command:

```bash
python3 apps/github_release_train_wait.py <OWN_PR> --shepherd
```

Shepherd читает own PR и global gate, выводит machine-readable Goal disposition и не принимает UI без evidence. `--phase-state <JSON>` передаёт `current_phase`/capability evidence в тот же classifier; `--once` нужен для bounded pre-handoff проверки. Exit codes: `0` = `TERMINAL_SUCCESS`; `2` = доказанный `EXTERNAL_BLOCKER`; `3` = `RECOVER_OWN_CHAIN`; `4` = `TAKEOVER_PREDECESSOR`/ownership resumed next action; `5` = `OWN_ACTION`; `6` = одно наблюдение `CONTINUE_WAITING`; `7` = доказанный `TERMINAL_FAILURE`; `8` = `CONTINUE_SAFE_PHASES`; `9` = `AWAIT_PHASE_CAPABILITY`; `130` = interrupt. Timeout, unchanged state и коды `3/4/5/6/8/9` не terminal. Merged production-mutation `blocked/halted` получает `OWN_ACTION / production-mutation-terminalization-available`, пока repo-owned exact command может проверить evidence; старое отсутствие `complete-standard --contour production-verified` больше не является blocker. `AWAIT_PHASE_CAPABILITY` остаётся phase-local и не является terminal failure. После кода `4` выполняется exact resume/action predecessor, затем та же команда с `OWN_PR` возвращает наблюдение к исходной очереди.

Минимальный phase-state для будущей недоступной production capability:

```json
{
  "current_phase": "REPOSITORY_IMPLEMENTATION",
  "safe_phases_remaining": ["REPOSITORY_IMPLEMENTATION", "REPOSITORY_VALIDATION", "PULL_REQUEST"],
  "required_capability": "production-credentials",
  "capability_available": false,
  "capability_evidence": [],
  "repo_owned_remediation_available": false,
  "remediation_exhausted": false,
  "user_intervention_required": false,
  "next_executable_action": "finish implementation and fixture-backed validation",
  "minimal_user_action": ""
}
```

Classifier сам выводит `blocked_phase`: при непустом `safe_phases_remaining` он остаётся `null` и возвращается `CONTINUE_SAFE_PHASES`; выставить его можно только для immediate evidenced capability gate.

Перед blocked handoff обязателен `--shepherd --once` с актуальным `--phase-state`, если задача имеет последующие production/UI phases. Handoff всей цели разрешён только для `EXTERNAL_BLOCKER` или `TERMINAL_FAILURE` вместе с canonical reason, evidence, выполненными recovery attempts и `remediation_exhausted=true`. При `CONTINUE_WAITING`, `CONTINUE_SAFE_PHASES`, `AWAIT_PHASE_CAPABILITY`, `OWN_ACTION`, `TAKEOVER_PREDECESSOR`, `RECOVER_OWN_CHAIN` общий blocked handoff запрещён. `EXTERNAL_BLOCKER` запрещён, если доступна repo-owned команда или осталась независимая safe phase.

## Failures И Idempotency

- invalid/missing task class или scope — `release:blocked` до merge;
- semantic/update conflict, failed baseline, missing production secret или SSH preflight failure — `release:blocked`;
- deploy/verify/UI-gate publication failure после merge — `release:halted`;
- любой существующий `release:halted` глобально блокирует выбор следующего PR;
- `release:awaiting-agent` блокирует всю очередь до exact ack; `release:needs-resume` только делает потерю владельца видимой и ничего не разрешает;
- `release:awaiting-ui` допускает только exact-linked recovery;
- успешно принятая recovery-chain оставляет `release:production` только terminal PR, нормализует merged predecessors и закрывает доказанно superseded unmerged PR того же root;
- repeated label/push/dispatch events не выбирают PR без `release:ready` и не повторяют terminal merge/deploy;
- repeated ack проверяет тот же PR/head, а consumed/stale ack не может разрешить новый merge;
- repeated UI acceptance сохраняет terminal labels и лишь безопасно пере-dispatch-ит serialized worker.
- repeated production-mutation completion сохраняет один Actions-owned exact-evidence marker и terminal label без новых comments/labels и безопасно re-dispatch-ит queue observation; partial label/marker state лечится только повторной полной проверкой canonical deploy evidence.
- Finance deploy lease никогда не auto-opens: repeated acquire/recovery/rebind/terminal commands идемпотентны; stale/duplicate/partial state блокирует unrelated release, а exact recovery deploy требует следующей SHA-bound revision до продолжения migration;
- repeated enqueue/correction events не дублируют proof и не меняют другие roots.

Исправленный own технический pre-merge blocker повторно входит в очередь только через trusted comment `/wb-core loop retry-blocked <PR> head <HEAD_SHA>`; underlying runner остаётся `retry-blocked --pr <PR> --expected-head-sha <HEAD_SHA>`, но task owner не запускает его локальным user token. New/recovery enrollment не может снять technical blocker. Command требует open non-draft PR, exact head, `OWNER`/`MEMBER`/`COLLABORATOR` association и successful `baseline`, сохраняет task class/scope/root и не удаляет LOOP labels. Если fix изменил LOOP head, command выпускает новый exact-head marker только при наличии prior repo-owned proof той же identity; для recovery дополнительно остаются обязательны тот же active gate/root и отсутствие terminal member. Classification provenance остаётся unresolved через любое число последующих head changes, поэтому generic retry отклоняется, пока более поздний trusted new/recovery/correction proof явно не разрешит identity. Codex waiter не выполняет classification mutations: он только сообщает mismatch и завершается fail-closed, оставляя durable transition trusted workflow.

Ошибочная stale-terminal recovery identity исправляется только `/wb-core loop correct-to-new <PR> head <HEAD_SHA> old-root <ROOT>`. Command требует `OWNER`/`MEMBER` authorization, open/unmerged exact PR/head, successful baseline, exact classification-blocker proof, repo-owned terminal proof old root и отсутствие его active gate; затем одним label replacement назначает own root/ready и оставляет идемпотентный correction/new-root audit proof. Без любого evidence command fail-closed. Эта операция не применяется автоматически и не изменяет старый root или другие chains.

SSH exit `255` или unexpected disconnect после merge классифицируется как
`transport-indeterminate`. Repo-owned reconciler bounded-переподключается и
сопоставляет canonical `target_id`, expected merge SHA, deploy metadata SHA,
runtime SHA marker, atomic `deployment_complete=true`, systemd active/MainPID
и обязательные loopback probes. Ранние exact-SHA markers с
`deployment_complete=false` доказывают только начатый rollout и не могут снять
`release:halted`, даже если старый/перезапущенный процесс отвечает на probes.
Wrong/mixed SHA, incomplete deploy, inactive unit или failed probes сохраняют
`release:halted`. Повторяются только `daemon-reload`, restart, probes и
readback. Отдельный production-environment workflow `resume-halted` снимает
halted только после healthy exact PR/head/merge/target JSON evidence; ручное
снятие label не считается reconciliation.

Если transport оборвался на финальном `metadata-complete`, внутренний runner может признать settling успешным только после bounded readback с `deployment_complete=true` для exact SHA и всеми probes; в этой фазе repairs запрещены. Workflow не запускает общий reconciliation после произвольной deploy/public-probe ошибки и никогда не маскирует её базовым exact-SHA health readback.

## Канонический Мониторинг

[Основной мониторинг исполняемых/ожидающих PR](https://github.com/orenvlad-ai/wb-core/pulls?q=is%3Apr+-label%3Arelease%3Asuperseded+label%3A%22release%3Astaged%2Crelease%3Aready%2Crelease%3Arunning%2Crelease%3Aawaiting-agent%2Crelease%3Aawaiting-ui%2Crelease%3Aneeds-resume%2Crelease%3Ablocked%2Crelease%3Ahalted%2Crelease%3Alane-owner%2Cfinance%3Amigration-deploy-lease%22+sort%3Acreated-asc) намеренно не использует `is:open`: staged work, merged LOOP с `release:awaiting-ui`, logical `release:lane-owner` и terminal anchor с `finance:migration-deploy-lease` остаются active и обязаны быть видимыми. Comma-OR qualifier включает active release labels и leases; `-label:release:superseded` исключает доказанно заменённые итерации. Terminal `release:production`, `release:done` и `release:retired` без отдельного global lease не включаются; `sort:created-asc` сохраняет queue order. PR-specific evidence по-прежнему исследуется по exact ссылке, comments и workflow runs; machine snapshot для Watcher даёт `python3 apps/github_release_train.py queue-status`.

## Baseline И Security Boundary

`baseline-ci.yml` выполняет `compileall`, `git diff --check`, `apps/codex_task_orchestrator_smoke.py` и `apps/github_release_train_smoke.py`, затем остальные repository regression smokes. Task owner дополнительно выполняет применимые targeted checks и перечисляет их в PR.

`pull_request_target` и `issue_comment` всегда checkout-ят trusted `main`; PR code до merge не исполняется этим trigger. LOOP, Finance lease и production-mutation commands проходят exact parsing и association checks. Finance lease workflow использует production secrets только для `--read-only` exact deployed-SHA readback; acquire/rebind/release меняют лишь GitHub durable state и не запускают Finance runner. Production-mutation command preflight работает без production secrets; SSH material получает только следующий job с GitHub Environment `production` после успешного immutable-evidence preflight. Required secrets остаются `WB_CORE_DEPLOY_SSH_KEY` и `WB_CORE_DEPLOY_KNOWN_HOSTS`. Live deploy выполняется только canonical repo-owned runner из clean exact merge SHA. Production-mutation terminalizer выполняет только `--read-only` deploy readback и GitHub terminal state transition; Release Train не выполняет WB writes, backfill или production business mutation.

## Проверенный LOOP Canary

[PR #616](https://github.com/orenvlad-ai/wb-core/pull/616) остаётся проверенным reference flow post-registration стадий LOOP: exact-head acknowledgement, merge, canonical deploy, `release:awaiting-ui`, read-only CLI Playwright/Chrome verification, exact `accept-ui`, terminal `release:production` и post-accept empty-queue dispatch. Он исторически предшествует отдельным new/recovery enrollment proofs и не является примером ручного назначения identity. GitHub PR, comments, labels и workflow runs остаются durable evidence; временный repository marker после этого доказательства не нужен.

Новые canary/LOOP задачи сначала проходят `enqueue-new` либо `enqueue-recovery`, затем повторяют post-registration контракт: waiter останавливается кодом `3` на `release:awaiting-ui`, Codex выполняет production UI verification и оставляет exact `accept-ui` только при фактическом UI success. HTTP-only evidence не открывает gate.
