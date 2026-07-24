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
- `.github/workflows/release-train.yml` — один repository-wide queue worker и GitHub-native LOOP command handler;
- `apps/github_release_train.py` — GitHub API/state-machine runner;
- `apps/github_release_train_wait.py` — bounded CLI waiter и канонический Goal queue shepherd для Codex;
- `apps/github_release_train_smoke.py` — deterministic state-machine smoke;
- `.github/pull_request_template.md` — PR closure checklist.

## Eligibility И Labels

Queue eligibility требует одновременно:

- open non-draft PR в `main`;
- same-repository head branch;
- `release:ready`;
- ровно одну известную `task:*` label;
- ровно одну известную `scope:*` label;
- отсутствие `release:blocked`, `release:halted` и `release:superseded`.

LOOP дополнительно требует exact-head repo-owned registration proof. Один `loop:root-*` или `release:ready`, добавленный вручную, eligibility не доказывает.

Основные state labels:

- `release:ready` — task owner закончил pre-release proof и явно поставил PR в очередь;
- `release:running` — worker выполняет sync/baseline/release;
- `release:awaiting-agent` — LOOP прошёл sync/baseline и ждёт exact-head acknowledgement активной Codex-сессии;
- `release:needs-resume` — non-terminal overlay на активном LOOP `ready/running/awaiting-agent/awaiting-ui`: owner heartbeat истёк, но primary state и gate не изменяются;
- `release:awaiting-ui` — LOOP merge задеплоен и ждёт production UI Flow/acceptance;
- `release:blocked` — PR-specific failure до merge;
- `release:done` — terminal success STANDARD `repo-only` без deploy;
- `release:production` — terminal success STANDARD live/runtime или принятой LOOP-цепочки;
- `release:halted` — failure после merge; вся очередь остановлена.
- `release:superseded` — terminal audit state незамёрженной LOOP-итерации, однозначно заменённой завершённой production recovery-chain; root/task/scope/history сохраняются, активные queue/failure labels снимаются.

Active states: `release:ready`, `release:running`, `release:awaiting-agent`, `release:awaiting-ui`, `release:needs-resume`, `release:blocked`, `release:halted`. Terminal states: `release:done`, `release:production`, `release:superseded`. Terminal state является жёсткой identity boundary и не имеет перехода обратно в очередь.

Каноническая машинная спецификация живёт в `apps/github_release_train_spec.py`: task class, continuity, active/overlay/terminal sets, transition matrix, critical transitions, monitor query, marker names и Goal disposition contract. Runtime, waiter/shepherd и smoke импортируют её, а AGENTS/docs проверяются regression assertions. Primary states взаимоисключающие, кроме временной `ready+running`; `needs-resume` — только overlay. State/identity registration заменяет полный label set одним GitHub API call, поэтому не оставляет между add/remove временного conflicting state. Ручно добавленный label не является proof: LOOP registration/recovery, ack, terminal completion, deployed UI gate, acceptance и halted recovery требуют repo-owned marker и exact PR/head/gate/merge/root/evidence.

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

## STANDARD Flow

STANDARD PR проходит существующую последовательность без agent acknowledgement:

1. worker выбирает старейший eligible PR;
2. синхронизирует branch с current `main`;
3. явно dispatch-ит `baseline-ci.yml` и ждёт новый successful `baseline` на final head SHA;
4. повторно проверяет exact head/base/task/scope/mergeability;
5. squash-merges только проверенный head;
6. `scope:repo-only` получает `release:done` без deploy;
7. `scope:live-runtime` checkout-ит exact merge SHA, вызывает canonical `deploy-and-verify` и получает `release:production`;
8. worker best-effort удаляет feature branch и dispatch-ит следующий queue run.

`scope:production-mutation` никогда не выпускается автоматически и до merge получает `release:blocked` с требованием отдельного human-gated production-mutation protocol.

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

- STANDARD ждёт `release:done` для `scope:repo-only` или `release:production` для `scope:live-runtime`;
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

Shepherd читает own PR и global gate, выводит machine-readable Goal disposition и не принимает UI без evidence. `--phase-state <JSON>` передаёт `current_phase`/capability evidence в тот же classifier; `--once` нужен для bounded pre-handoff проверки. Exit codes: `0` = `TERMINAL_SUCCESS`; `2` = доказанный `EXTERNAL_BLOCKER`; `3` = `RECOVER_OWN_CHAIN`; `4` = `TAKEOVER_PREDECESSOR`/ownership resumed next action; `5` = `OWN_ACTION`; `6` = одно наблюдение `CONTINUE_WAITING`; `7` = доказанный `TERMINAL_FAILURE`; `8` = `CONTINUE_SAFE_PHASES`; `9` = `AWAIT_PHASE_CAPABILITY`; `130` = interrupt. Timeout, unchanged state и коды `3/4/5/6/8/9` не terminal. После кода `4` выполняется exact resume/action predecessor, затем та же команда с `OWN_PR` возвращает наблюдение к исходной очереди.

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
- repeated enqueue/correction events не дублируют proof и не меняют другие roots.

Исправленный own технический pre-merge blocker повторно входит в очередь только через trusted comment `/wb-core loop retry-blocked <PR> head <HEAD_SHA>`; underlying runner остаётся `retry-blocked --pr <PR> --expected-head-sha <HEAD_SHA>`, но task owner не запускает его локальным user token. New/recovery enrollment не может снять technical blocker. Command требует open non-draft PR, exact head, `OWNER`/`MEMBER`/`COLLABORATOR` association и successful `baseline`, сохраняет task class/scope/root и не удаляет LOOP labels. Если fix изменил LOOP head, command выпускает новый exact-head marker только при наличии prior repo-owned proof той же identity; для recovery дополнительно остаются обязательны тот же active gate/root и отсутствие terminal member. Classification provenance остаётся unresolved через любое число последующих head changes, поэтому generic retry отклоняется, пока более поздний trusted new/recovery/correction proof явно не разрешит identity. Codex waiter не выполняет classification mutations: он только сообщает mismatch и завершается fail-closed, оставляя durable transition trusted workflow.

Ошибочная stale-terminal recovery identity исправляется только `/wb-core loop correct-to-new <PR> head <HEAD_SHA> old-root <ROOT>`. Command требует `OWNER`/`MEMBER` authorization, open/unmerged exact PR/head, successful baseline, exact classification-blocker proof, repo-owned terminal proof old root и отсутствие его active gate; затем одним label replacement назначает own root/ready и оставляет идемпотентный correction/new-root audit proof. Без любого evidence command fail-closed. Эта операция не применяется автоматически и не изменяет старый root или другие chains.

SSH exit `255` или unexpected disconnect после merge классифицируется как `transport-indeterminate`. Repo-owned reconciler bounded-переподключается и сопоставляет canonical `target_id`, expected merge SHA, deploy metadata SHA, runtime SHA marker, systemd active/MainPID и обязательные loopback probes. Wrong/mixed SHA, inactive unit или failed probes сохраняют `release:halted`. Повторяются только `daemon-reload`, restart, probes и readback. Отдельный production-environment workflow `resume-halted` снимает halted только после healthy exact PR/head/merge/target JSON evidence; ручное снятие label не считается reconciliation.

## Канонический Мониторинг

[Основной мониторинг исполняемых/ожидающих PR](https://github.com/orenvlad-ai/wb-core/pulls?q=is%3Apr+-label%3Arelease%3Asuperseded+label%3A%22release%3Aready%2Crelease%3Arunning%2Crelease%3Aawaiting-agent%2Crelease%3Aawaiting-ui%2Crelease%3Aneeds-resume%2Crelease%3Ablocked%2Crelease%3Ahalted%22+sort%3Acreated-asc) намеренно не использует `is:open`: merged PR имеет GitHub state `closed`, но LOOP с `release:awaiting-ui` остаётся active global gate и обязан быть видимым. Comma-OR qualifier включает `release:ready`, `release:running`, `release:awaiting-agent`, `release:awaiting-ui`, `release:needs-resume`, `release:blocked` и `release:halted`; `-label:release:superseded` исключает доказанно заменённые итерации. Terminal `release:production` и `release:done` не включаются, а `sort:created-asc` сохраняет queue order. PR-specific evidence по-прежнему исследуется по точной ссылке, comments и workflow runs.

## Baseline И Security Boundary

`baseline-ci.yml` выполняет `compileall`, `git diff --check` и `apps/github_release_train_smoke.py`. Task owner дополнительно выполняет применимые targeted checks и перечисляет их в PR.

`pull_request_target` и `issue_comment` всегда checkout-ят trusted `main`; PR code до merge не исполняется этим trigger. LOOP commands проходят exact parsing и association checks. Production SSH material доступен только job с GitHub Environment `production`; required secrets остаются `WB_CORE_DEPLOY_SSH_KEY` и `WB_CORE_DEPLOY_KNOWN_HOSTS`. Live deploy выполняется только canonical repo-owned runner из clean exact merge SHA. Release Train не выполняет WB writes, backfill или production business mutation.

## Проверенный LOOP Canary

[PR #616](https://github.com/orenvlad-ai/wb-core/pull/616) остаётся проверенным reference flow post-registration стадий LOOP: exact-head acknowledgement, merge, canonical deploy, `release:awaiting-ui`, read-only CLI Playwright/Chrome verification, exact `accept-ui`, terminal `release:production` и post-accept empty-queue dispatch. Он исторически предшествует отдельным new/recovery enrollment proofs и не является примером ручного назначения identity. GitHub PR, comments, labels и workflow runs остаются durable evidence; временный repository marker после этого доказательства не нужен.

Новые canary/LOOP задачи сначала проходят `enqueue-new` либо `enqueue-recovery`, затем повторяют post-registration контракт: waiter останавливается кодом `3` на `release:awaiting-ui`, Codex выполняет production UI verification и оставляет exact `accept-ui` только при фактическом UI success. HTTP-only evidence не открывает gate.
