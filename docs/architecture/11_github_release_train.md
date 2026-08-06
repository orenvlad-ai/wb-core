# GitHub Release Train

## Назначение

Release Train — единственный GitHub-native механизм merge/deploy для `wb-core`.
Он хранит durable queue/terminal state на PR, проверяет exact head, сериализует
main sync, CI, merge, deploy и reconciliation. Он не зависит от Global Watcher,
external registry, Task Passport, acceptance envelope, logical lane owner или
активной Codex chat session.

## Repo-Owned Артефакты

- `.github/workflows/baseline-ci.yml`
- `.github/workflows/release-train.yml`
- `apps/github_release_train.py`
- `apps/github_release_train_spec.py`
- `apps/github_release_train_smoke.py`
- `apps/github_release_train_wait.py`
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`

Workflow использует один concurrency group `wb-core-production-release`.
Standalone `user-artifact` не входит в GitHub Release Train и не имеет label
`scope:user-artifact`.
Архивный read-only MCP не является source/acquisition path. Release evidence
берётся из canonical server-side GitHub/runtime readback; отсутствие MCP не
blocker.

## Labels

Новые PR используют:

- task: `task:standard`;
- scope: ровно один из `scope:repo-only`, `scope:live-runtime`,
  `scope:production-mutation`;
- active: `release:ready`, `release:running`, `release:blocked`,
  `release:halted`;
- terminal: `release:done`, `release:production`.

`release:superseded` и `release:retired` сохраняются только для historical PR.
Старые `release:staged`, `release:awaiting-agent`, `release:awaiting-ui`,
`release:needs-resume`, `release:lane-owner`, `task:loop` и `loop:*` не создаются
current flow и не дают eligibility.

## Trusted Exact-Head Enqueue

После successful `baseline` OWNER/MEMBER оставляет comment:

`/wb-core release enqueue <PR> head <HEAD_SHA>`

Issue-comment workflow checkout’ит trusted `main` и проверяет:

1. Actions-owned handler и non-bot OWNER/MEMBER actor;
2. command относится к current PR;
3. open non-draft PR targeting `main`;
4. same-repository head branch;
5. exact 40-character current head SHA;
6. `task:standard` и scope `repo-only`/`live-runtime`;
7. successful `baseline` на этом exact SHA;
8. PR не running/terminal.

После этого bot публикует marker `wb-core-release-enqueue-proof`, ставит
`release:ready` и dispatch’ит queue. Selector/prepare/merge каждый раз требуют
этот marker для текущего head. Ручной label, copied marker от пользователя,
stale SHA, direct push, failed check, draft/fork head или untrusted comment не
допускают PR.

## Selection И Serialization

Scheduled/push/workflow-dispatch run сначала проверяет global halt и Finance
deploy lease, затем выбирает oldest open `release:ready` PR с current enqueue
proof. Пустая очередь завершается успешным idle без polling.

Finance migration deploy lease остаётся отдельным fail-closed product safety
guard. Пока exact active lease не terminalized, обычные releases не проходят;
разрешён только proof-bound recovery PR владельца lease.

## Prepare

Train переводит selected PR в `release:running`, сверяет base/head/scope,
синхронизирует с current `main`, dispatch’ит fresh `baseline` и ждёт success.
Перед merge повторно читаются PR head, labels, scope, mergeability и enqueue
proof.

Если sync изменил head, прежний proof становится stale. PR fail-closed
переходит в `release:blocked`; после successful baseline trusted actor повторяет
enqueue для нового exact head. Blind retry или label edit proof не создаёт.

## Repo-Only

Для `scope:repo-only` Train merge’ит exact checked head, публикует
Actions-owned completion proof, ставит `release:done`, удаляет feature branch и
dispatch’ит следующий queue run.

## Live Runtime

Для `scope:live-runtime` перед merge проверяется production secret binding и
SSH. После exact merge workflow checkout’ит merge SHA, запускает canonical
`deploy-and-verify`, сверяет deployed SHA/runtime и только затем публикует
completion proof и `release:production`.

Live verification не открывает acknowledgement/UI gate и не ждёт активную Codex
session. Требуемая задачей UI-проверка выполняется как обычная independent
runtime verification и прикладывается к handoff.

Ошибка до merge ставит `release:blocked`. Ошибка deploy/verify после merge
публикует exact halt proof и ставит `release:halted`; вся последующая mutation
останавливается.

## Halt И Reconciliation

Reconciliation запускается только для merged `release:halted` PR и связывает:

- original PR/head/merge SHA;
- canonical deployed SHA;
- failed stage;
- settling/readback evidence digest.

`resume-halted` принимает только repo-owned exact evidence. Успешный verify
ставит `release:production`; несоответствие остаётся halted. Legacy authority
или ручное снятие label не являются rollback.

## Retry

Исправленный pre-merge PR проходит новый exact-head `baseline`, затем ту же
trusted enqueue command. Это одновременно является retry и новой admission;
старый proof не переносится. Bounded reconcile/retry сохраняют durable comment
evidence и не создают бесконечный loop.

## Production Mutation HumanGate

`scope:production-mutation` не входит в ordinary enqueue. Apply terminalization
использует trusted-main command:

`/wb-core production-mutation complete <PR> head <HEAD_SHA> merge <MERGE_SHA> deployed <DEPLOYED_SHA> gate <GATE_COMMENT_ID> gate-digest sha256:<GATE_COMMENT_HASH> reconciliation <RECONCILIATION_COMMENT_ID> reconciliation-digest sha256:<RECONCILIATION_COMMENT_HASH> evidence sha256:<EVIDENCE_HASH>`

Handler проверяет non-bot OWNER/MEMBER, exact PR/head/merge/deployed identities,
fresh explicit HumanGate comment, immutable comment digests, apply evidence,
reconciliation и canonical deployed readback. Только Actions-owned terminal
marker разрешает `release:production`.

## Finance Migration Deploy Lease

Global Finance lease остаётся business safety contract. Commands
`/wb-core finance-lease ...` проходят trusted-main preflight, bind task/lease/
revision/window/head/deployed/reconciliation evidence и используют production
environment только когда требуется readback. Lease acquisition, recovery,
release/abort и idempotency реализованы в `apps/github_release_train.py` и
`packages/application/finance_migration_deploy_lease.py`; обычный enqueue не
может их обойти.

## Security Boundary

- Workflow command code всегда checkout’ится с trusted `main`, не из PR head.
- `GITHUB_TOKEN` имеет только declared workflow permissions.
- Production secrets доступны только production jobs и не печатаются.
- Exact head проверяется до/после sync, CI и непосредственно перед merge.
- Public branch ruleset требует PR + `baseline`, запрещает deletion/force-push.
- Direct label и direct push не заменяют Actions-owned proof.

## Terminal Report И Owner Acceptance

Bot terminal comment фиксирует только technical evidence: PR, merge/deploy SHA,
contour и result. Исполнитель передаёт короткий handoff куратору. Owner
acceptance остаётся ручным отдельным состоянием и не синтезируется Release
Train, GitHub label или model output.
