# GitHub Release Train

## Назначение

GitHub Release Train — repo-owned serialized queue для независимо подготовленных
change-задач. Durable truth хранится в GitHub PR, labels, checks, comments и
workflow runs. Queue владеет critical sequence:

`sync current main → fresh exact-head baseline → merge → applicable exact-SHA deploy → verify → terminal label`

Она не заменяет executor-level implementation, targeted checks, semantic
review, review-thread resolution и authoritative docs sync.

`WB_CORE_ORCHESTRATION_REQUIRED=false`. Watcher admission, external registry,
passports, logical task lane, shepherd/takeover и chat callbacks не являются
eligibility, release или closure requirements. Current executor не запускает и
не восстанавливает их. Исторический contour находится только в
[`12_codex_global_orchestration.md`](12_codex_global_orchestration.md).

Retained compatibility handlers/states остаются в code truth и не меняются
этим documentation contract, но не являются current agent instructions.

Non-PR `user-artifact`, где единственная mutation — пользовательский файл вне
репозитория, в Release Train не входит. Изменение Git-tracked protocol/docs/code
является обычным `scope:repo-only` PR.

## Repo-Owned Artifacts

- `.github/workflows/baseline-ci.yml` — required check `baseline`;
- `.github/workflows/release-train.yml` — repository-wide queue worker,
  production-mutation terminalizer и Finance migration deploy lease handler;
- `apps/github_release_train.py` — GitHub API/state-machine runner;
- `apps/github_release_train_spec.py` — canonical labels/transitions/proofs;
- `apps/github_release_train_wait.py` — retained bounded status/readback helper;
- `apps/github_release_train_smoke.py` — deterministic model-free regression;
- `.github/pull_request_template.md` — PR checklist.

Runtime code и workflow являются code truth. Этот документ описывает их
действующий ordinary path и product-safety boundaries без активации retired
orchestration surfaces.

## Ordinary PR Eligibility

Новый ordinary change использует:

- open non-draft PR в `main`;
- same-repository head branch;
- `task:standard`;
- ровно одну label: `scope:repo-only`, `scope:live-runtime` или
  `scope:production-mutation`;
- successful required `baseline` на current exact head;
- `release:ready` только после этого baseline readback;
- отсутствие `release:blocked`, `release:halted` и несовместимых terminal state.

Executor добавляет `release:ready` на собственный PR после fresh readback exact
head и successful baseline. Он не использует промежуточную orchestration
admission, не получает logical lane и не меняет foreign PR/labels.

Manual merge, direct push в `main`, draft/fork PR, missing/failed baseline,
stale head или label на чужом PR не являются ordinary closure.

## Active Ordinary States

- `release:ready` — executor доказал pre-release checklist и поставил exact PR
  в serialized queue;
- `release:running` — worker выполняет sync/check/release;
- `release:blocked` — exact PR остановлен до исправления конкретного pre-merge
  либо human-gated mutation condition;
- `release:done` — terminal success для STANDARD `scope:repo-only`;
- `release:production` — terminal success для STANDARD live/runtime либо
  Actions-terminalized production mutation;
- `release:halted` — post-merge deploy/verify ambiguity or failure; queue
  удерживается fail closed до exact-SHA reconciliation.

Primary/terminal invariants и compatibility states определены в
`apps/github_release_train_spec.py`. Ручной terminal label не доказывает merge,
deploy или reconciliation: critical transitions требуют repo-owned evidence.
Terminal state является identity boundary; новый defect после terminal closure
получает новую branch, PR и task.

## STANDARD Flow

После `release:ready` worker выполняет:

1. выбирает oldest eligible PR, если нет global halted/Finance safety gate;
2. атомарно переводит его в processing state;
3. сравнивает head с current `main`;
4. при необходимости синхронизирует same-repository branch;
5. dispatch-ит новый `baseline-ci.yml` на final head и ждёт success;
6. повторно читает PR и проверяет exact head/base/task/scope/state/mergeability;
7. squash-merges только проверенный exact head;
8. для `scope:repo-only` создаёт Actions-owned completion proof и
   `release:done` без deploy;
9. для `scope:live-runtime` checkout-ит clean exact merge SHA, запускает
   canonical deploy-and-verify и ставит `release:production` только после exact
   deploy/readback/probes;
10. best-effort удаляет feature branch и dispatch-ит next queue observation.

Если branch sync изменил head, old baseline не переносится: Release Train
обязательно запускает fresh check и повторяет exact-head validation. Head/scope
drift во время checks fail closed. Другой ready/running PR — normal serialized
waiting и не разрешает manual intervention.

`scope:production-mutation` никогда не merge/deploy/apply автоматически из
`release:ready`. Worker оставляет его `release:blocked` до separate human-gated
contract.

## Exact-SHA Deploy И Reconciliation

Live deploy выполняется только canonical repo-owned runner из clean merge SHA.
Deployment success требует exact target identity, deploy metadata SHA, runtime
SHA marker, `deployment_complete=true`, expected auth binding, active systemd
unit/MainPID и mandatory loopback/public probes.

SSH exit `255`, disconnect или incomplete metadata после merge означает
`transport-indeterminate`, а не success. Repo-owned reconciler boundedly читает
canonical evidence. Wrong/mixed SHA, `deployment_complete=false`, inactive unit
или failed probes сохраняют `release:halted`; ответ старого процесса не заменяет
exact-SHA proof.

Разрешены только bounded reconnect, exact readback и документированные
idempotent service/probe repairs. `resume-halted` работает через production
environment и снимает halt только после healthy evidence, связанным с exact
PR/head/merge/target. Ручное снятие label или повтор business mutation не
являются reconciliation.

## Production-Mutation Terminalization

Human merge/deploy/apply не выполняется queue worker и не возникает из
`release:ready`. После отдельного exact human gate, exact-head merge, canonical
deploy/apply и bounded reconciliation
`task:standard + scope:production-mutation` закрывается только comment:

```text
/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> gate <GATE_COMMENT_ID> gate-digest sha256:<GATE_COMMENT_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>
```

Two-stage workflow разделяет authority:

1. Trusted-main preflight без production environment требует actor
   `OWNER`/`MEMBER`, current merged PR, exact retained pre-merge head,
   `task:standard + scope:production-mutation`, successful `baseline` exact
   head, exact merge SHA и fail-closed state. Human-gate comment обязан быть на
   том же PR, предшествовать merge, содержать exact head и authorization
   semantics; command фиксирует exact UTF-8 body digest.
2. Reconciliation comment обязан отличаться от gate, следовать после merge,
   иметь authorized owner/member identity, exact deployed SHA, completion
   semantics, evidence fingerprint и exact body digest. GitHub compare
   доказывает merge/descendant relation deployed SHA.
3. Только successful immutable-evidence preflight открывает production
   environment. Hosted reconciler запускается `--read-only` и сверяет canonical
   target, metadata/runtime SHA, `deployment_complete`, auth binding, service
   MainPID и probes. Он не выполняет deploy, restart, repair или business apply.
4. Только GitHub Actions создаёт completion proof, заменяет stale active/failure
   state на `release:production` и dispatch-ит queue observation.

Повтор exact proven command идемпотентен. Stale SHA/comment digest, missing
gate/deploy/reconciliation/evidence, unauthorized actor, wrong task/scope,
non-ancestor deployed SHA, local invocation или ручной terminal label fail
closed.

## Global Finance Migration Deploy Lease

Finance raw/operational migration использует отдельный GitHub-owned lease до
любого snapshot plan, coherent snapshot, capacity/fingerprint,
candidate/backfill, live-tail, cutover или rollback action. Durable authority
остаётся в PR labels и Actions-owned comments; private readback JSON — только
fresh переносимое evidence для hosted runner.

Acquire выполняется на proven terminal deployed anchor PR:

```text
/wb-core finance-lease acquire <ANCHOR_PR> head <HEAD_SHA> deployed <DEPLOYED_SHA> task <TASK_ID> lease <LEASE_ID> window <WINDOW_ID> phase <PHASE> ttl-minutes <30..4320>
```

Repository-wide `wb-core-production-release` concurrency сериализует command с
Release Train. Trusted-main handler требует `OWNER`/`MEMBER`, exact head,
canonical deployed SHA readback, merge/descendant relation, bounded TTL и
отсутствие active or failed release gate. Затем Actions-owned binding создаёт
audit guard и global hold. Partial/missing/conflicting proof остаётся ambiguous
и удерживает queue fail closed. Повтор exact command завершает тот же acquire и
не создаёт второй lease.

Lease не auto-releases по времени. Expiry делает его stale и запрещает Finance
mutation, но global hold остаётся. Fresh private readback:

```bash
python3 apps/github_release_train.py finance-lease-status \
  --require-active --output /private/path/finance-deploy-lease.json
```

Readback содержит exact task/anchor/head/deployed SHA,
lease/window/phase/revision, timestamps, recovery policy и
`baseline_invalidation_epoch`; file хранится вне Git и принимается hosted
runner только boundedly fresh. Remote runner повторно сверяет его с canonical
`.wb-core-runtime-sha`. Любой later SHA/revision/expiry/lost-owner drift
инвалидирует прежние baseline/snapshot/plan/fingerprint evidence до Finance
destination bytes.

Code recovery допускает только один exact authorized
`task:standard + scope:live-runtime` PR с successful baseline. Пока lease
активен, unrelated ready PR остаются held. После recovery deploy требуется exact
rebind на новый deployed SHA и новую revision; stale plan/fingerprint не
переносится.

Lease release/abort возможен только после structured reconciliation с exact
task/lease/revision/deployed/evidence и proof, что manual barrier released,
writers/timers/policy restored, non-target unchanged и SHA readback exact.
Production environment снова независимо читает canonical deployed SHA; только
Actions-owned terminal marker снимает global hold. Partial terminalization
остаётся fail closed, но не повторяет migration mutation.

Полный Finance storage snapshot/cutover/rollback contract находится в
[`10_hosted_runtime_deploy_contract.md`](10_hosted_runtime_deploy_contract.md).

## Retained Compatibility Boundary

State machine сохраняет historical LOOP and retired-orchestration parsing,
proof validation и fail-closed guards, чтобы не ослабить существующее product
safety и audit. Current ordinary protocol:

- не создаёт новые legacy orchestration identities;
- не назначает historical task/lane ownership;
- не запускает agent heartbeat, callback, takeover или UI-acceptance session;
- не снимает и не переклассифицирует historical labels вручную;
- не использует compatibility code как источник новых agent actions.

Если в GitHub уже существует non-terminal compatibility state, executor только
фиксирует его как foreign machine gate и не вмешивается без отдельной явно
ограниченной migration/recovery задачи. Исторические команды и contracts
доступны через Git history anchor, указанный в
[`12_codex_global_orchestration.md`](12_codex_global_orchestration.md).

## Failures И Idempotency

- invalid/missing task class or scope блокирует PR до merge;
- failed/missing fresh baseline, update conflict или mergeability drift
  блокируют exact PR;
- deploy/verify ambiguity after merge переводит exact PR в `release:halted` и
  удерживает queue;
- repeated label/push/schedule events не повторяют terminal merge/deploy;
- repeated production-mutation completion сохраняет один Actions-owned proof;
- Finance lease acquire/recovery/rebind/terminal commands идемпотентны, а
  stale/partial state удерживает unrelated releases;
- direct/manual terminal labels, stale SHA и forged markers не являются proof.

Exact blocker исправляется на own branch/head, после чего запускаются fresh
checks и documented repo-owned retry/reconciliation. Labels других PR и current
live release вручную не снимаются.

## Phase-Local Production Gates

`REPOSITORY_PREFLIGHT`, `PRODUCTION_READ_PREFLIGHT`,
`PRODUCTION_MUTATION_PREFLIGHT` и `PRODUCTION_UI_PREFLIGHT` независимы.
Production credentials/database/browser/manifests/digest/backup не требуются для
repository development, fixtures/mocks, tests, PR, CI или review.

Production read использует actual canonical server-side path: current target,
штатный SSH, query-only store access (`mode=ro`, `PRAGMA query_only=ON` для
SQLite) и bounded server-owned document reads. Архивный WebCore Data MCP не
является prerequisite/fallback; его отсутствие не образует blocker.

Production mutation runner до gate реализуется на fixtures/mocks и имеет
dry-run default, explicit apply, bounded scope, machine-readable manifest,
pre-change digest, backup/evidence, expected records, non-target invariants,
idempotency/recovery и post-apply reconciliation. Ad-hoc local/server-only
scripts production mutation не выполняют.

## Baseline И Security Boundary

`baseline-ci.yml` выполняет `compileall`, `git diff --check`,
`apps/github_release_train_smoke.py` и остальные repository regression smokes.
Executor дополнительно выполняет targeted checks и перечисляет их в PR.

`pull_request_target` и `issue_comment` checkout-ят trusted `main`; unmerged PR
code этими triggers не исполняется. Exact command parsers проверяют actor
association и immutable identities. Production secrets получает только job с
GitHub Environment `production` после successful preflight. Live deploy
исполняет canonical repo-owned runner из clean exact merge SHA. Production
mutation terminalizer выполняет только `--read-only` deploy evidence readback и
GitHub terminal transition; Release Train не выполняет business mutation.

## Executor Monitoring И Closure

Executor boundedly читает свой PR/check/workflow state и ждёт terminal label.
Периодический AI monitor, shepherd, takeover и callback для этого не создаются.
Unchanged queue state нормально; elapsed time не разрешает обход safety gate.

Technical closure требует:

- exact terminal state и Actions-owned evidence;
- merge SHA и current `origin/main` readback;
- no unresolved review threads;
- для live/runtime — canonical deployed SHA и verify evidence;
- для production mutation — gate, backup/reversibility, apply audit,
  reconciliation и non-target invariants.

Technical closure не является owner acceptance. Executor сообщает куратору
факты, а только владелец отвечает `Задача принята` и вручную открепляет задачи.
