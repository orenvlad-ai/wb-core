# GitHub PR Gate And One-Shot Release Runner

## Status

Это current repository release truth. Исторический serialized Release Train,
queue/recovery carousel, scheduled polling и label-driven state удалены.
Термин `Release Train` в имени этого файла сохраняется только ради стабильной
ссылки из старых domain docs; действующий actor называется Release Runner.

Components:

- `ci/test_registry.json` — protocol-v2 suites, dependencies и path impacts;
- `ci/test_planner.py` — canonical exact base/head planner;
- `.github/workflows/pr-gate.yml` — required PR workflow;
- `apps/github_release_runner.py` и `release-runner.yml` — trusted-main
  one-shot admission/merge/deploy/receipt;
- `apps/production_apply_runner.py` и `production-apply.yml` — separate
  default-off exact production apply;
- `baseline-ci.yml` — temporary cutover/rollback compatibility only.

## Deterministic test plan

Planner получает exact PR/base/head и делает `git diff --name-status` между
ними. Registry читается как из base, так и из head; canonical union сохраняет
оба набора commands/rules/dependencies, поэтому branch не может удалить свою
coverage. Любой group conflict, invalid registry или unresolved dependency
fail closed.

Path rules выбирают suites и transitive dependencies. Unknown path,
registry/workflow/planner/runner/core-framework change или unresolved mapping
автоматически расширяет выбор до full regression. Labels и user input tests не
выбирают.

Canonical `test-plan.json` содержит:

- schema/protocol/cutover epoch;
- PR, exact base/head;
- base/head/union registry digests;
- normalized changed records и changed-path digest;
- unknown paths, reason codes, selected suites и bounded parallel groups;
- exact suite commands from base+head union;
- derived release plan (`repo_only`, `live_runtime`, `production_mutation`);
- optional exact production manifest path/digest;
- plan hash over every preceding field.

JSON использует sorted keys, UTF-8 и no insignificant whitespace. В plan нет
timestamp/random field; одинаковый input даёт одинаковые bytes/hash.

## PR Gate

Required workflow запускается genuine `pull_request` event. Fast core всегда
выполняет syntax, diff hygiene, planner/Runner/Apply smokes и workflow YAML
parse. Selected suites исполняются по immutable plan artifact в matrix с
`max-parallel=4`. Один aggregate job/check называется ровно `pr-gate`.

`workflow_dispatch` предназначен только для diagnostics; aggregate context
называется `pr-gate-diagnostic` и не удовлетворяет ruleset. Workflow имеет
только `contents:read`; PR jobs не получают secrets.

## One-shot admission

`workflow_run` допускает privileged action только после successful `PR Gate`
с event `pull_request`. Trusted-main runner:

1. читает exact workflow run и единственный immutable plan artifact;
2. связывает ровно один PR;
3. требует open, non-draft, same-repository PR в `main`;
4. требует current main base, exact workflow/head/plan bindings и
   `mergeable=true` в одном snapshot;
5. fetch-ит PR object without checkout и recompute-ит plan trusted planner-ом;
6. требует byte-identical artifact/recomputed plan и protocol-v2 cutover epoch;
7. проверяет, что epoch является ancestor base;
8. проверяет durable operation receipt ровно один раз.

Runner не исполняет unmerged PR code, tests или compatibility baseline. Он не
poll-ит, не sync-ит branch, не enqueue-ит и не resubmit-ит.

## One action and one receipt

При admission success Runner вызывает GitHub squash merge с expected head SHA,
затем один раз читает back exact merged head/merge SHA.

- `repo_only`: receipt `done` после exact merge readback.
- `live_runtime`: checkout clean exact merge SHA, canonical
  `registry_upload_http_entrypoint_hosted_runtime.py deploy-and-verify`, exact
  deployed-SHA binding, receipt `done`.
- `production_mutation`: тот же merge/deploy when applicable, затем query-only
  read exact manifest bytes/digest и receipt `awaiting_apply`. Business apply
  не выполняется.

Receipt — один PR comment и immutable Actions artifact с operation id,
workflow run, PR/base/head/plan hash, release kind, merge/deployed SHA,
manifest binding и stable reason codes. States:

- `done` — applicable merge/deploy complete;
- `awaiting_apply` — exact production manifest ждёт separate owner gate;
- `blocked` — one-shot admission/action could not safely complete;
- `superseded` — exact head/base/provenance was replaced;
- `already_terminal` — same durable operation already has a receipt.

Existing exact receipt запрещает duplicate action. Ambiguous merge transport
получает один readback; отсутствие exact proof не разрешает второй merge/deploy.

## Default-off Apply Runner

Apply workflow имеет только manual `workflow_dispatch` и production
environment. Inputs bind PR, merge/deployed SHA, manifest SHA-256, durable
operation id и exact authorization comment id.

Authorization comment обязан быть immutable `OWNER`/`MEMBER` body:

```text
/wb-core apply-v2 pr <PR> merge <MERGE_SHA> deployed <DEPLOYED_SHA> manifest sha256:<MANIFEST_SHA256> operation <OPERATION_ID>
```

Runner требует единственный earlier `awaiting_apply` receipt, exact merged PR,
exact manifest bytes/schema and operation id. Manifest carries canonical target,
`exact-merge-sha` deployed binding, dry-run default, bounded scope, exact
pre-change SHA-256, immutable backup evidence id, expected affected record
count, named non-target invariants, idempotency/bounded recovery contract,
query-only manifest readback и four explicit commands: dry-run, apply,
readback, reconcile.

Dry-run success precedes mutation. Apply command запускается максимум один раз.
Readback и reconciliation запускаются по одному разу даже после nonzero apply,
чтобы зафиксировать ambiguity без blind retry. Durable apply receipt binds
authorization body digest, command/stdout/stderr digests, return codes и
`apply_count` (0 или 1).

## Compatibility and rollback

`baseline` существует только для exact branch
`codex/process-cutover-pr-gate` и prefix `codex/pr-gate-rollback-`. Ordinary
future PR не получает compatibility check. Cutover merge выполняется manually
с expected head только после successful exact-head `baseline` и `pr-gate`.

Если post-merge `pr-gate` неработоспособен, ruleset возвращается к exact
`baseline` context и используется только bounded rollback branch. Ruleset не
выключается, bypass actors не добавляются, старый scheduled actor автоматически
не включается.

## Independent safety

Canonical live deploy остаётся exact merge SHA и сохраняет transport
reconciliation, auth, target/service and production probe guards из
[`10_hosted_runtime_deploy_contract.md`](10_hosted_runtime_deploy_contract.md).
Finance/storage lease, snapshot, writer/timer/barrier, restore, manifest и
non-target invariants остаются plan/apply guards, но не queue ownership.

`user-artifact` не входит в GitHub PR/release flow. Historical Actions logs,
comments и branches сохраняются как audit evidence.
