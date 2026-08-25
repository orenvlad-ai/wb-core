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
- `baseline-ci.yml` — compatibility only for genuine PR Gate rollback.

## Deterministic test plan

Planner получает exact PR/base/head и делает `git diff --name-status` между
ними. Registry читается как из base, так и из head; canonical union сохраняет
оба набора commands/rules/dependencies, поэтому branch не может удалить свою
coverage. Любой group conflict, invalid registry или unresolved dependency
fail closed.

Path rules поддерживают exact include/exclude mapping, выбирают suites и
transitive dependencies. Orchestration docs/config и bounded
Runner/Apply/release tooling выбирают targeted `release_safety` и
`repo_only`; test-selection/execution semantics (`ci/**`, `pr-gate.yml` и
shared smoke framework) выбирают full regression с `repo_only`. Blanket
`apps/**` fallback отсутствует и не может перекрыть specific release tooling:
неизвестный code path остаётся `live_runtime` и автоматически получает full
regression.

Каждый suite command, указывающий на repo smoke/script, проходит deterministic
self-coverage invariant: изменение его собственного path обязано выбрать suite,
который фактически исполняет тот же exact command. Исключение допустимо только
как явно зарегистрированный `core_only_commands` entry с непустым
justification; сейчас это planner/Release Runner/Apply Runner smokes, которые
всегда выполняет Fast core. Registry validation требует для каждого такого
entry существующий repo script path и ровно одну exact command line в
unconditional `core` job `pr-gate.yml`; декларация без реального исполнения
невалидна. Duplicate exact commands across selected suites deduplicate-ятся в
canonical plan и исполняются ровно один раз.

Inventory history/history backfill/planning имеют отдельные `history` и, где
нужен Chromium, `history-browser` groups. Они фактически исполняют exact
history/backfill/planning regressions и не тянут Finance. Shared business-data
barrier/maintenance smokes также имеют собственный suite, поэтому их own-path
selection больше не зависит от Finance.

Unknown path автоматически расширяет выбор до full regression. Invalid head
registry или unresolved dependency при наличии valid counterpart строит
executable full-regression fallback, но помечает release plan invalid: selected
groups выполняются для evidence, aggregate gate обязательно падает и merge
невозможен. Если ни один registry input не даёт exact executable commands,
planner останавливается exact fail-closed error. Labels и user input tests не
выбирают.

`ci/replay_test_selection.py` сравнивает baseline/candidate selector минимум на
100 merged PR и отдельно фиксирует `replayed_current_unmapped_paths`, deleted
historical paths и targeted-to-full regressions. Эта replay-метрика относится
только к изменённым путям попавшей в выборку сотни PR и не является аудитом
всего current tree. Отдельный `current_tree_mapping_audit` проверяет все tracked
paths под `apps/`, `packages/`, `gas/`, применяя candidate registry и к
baseline tree, и к current tree. Bounded legacy residual остаётся явно
посчитанным; mapping или удаление старого residual разрешено, но любой новый
current residual path относительно baseline residual set делает replay nonzero,
даже если другой path в том же изменении был mapped или удалён. Наличие
residual не ослабляет fail-closed fallback: изменение любого
unmatched path выбирает full regression, а unmatched code остаётся как минимум
`live_runtime`. Current path в replay или новый unexplained targeted-to-full
transition делает replay nonzero; удалённый legacy path
остаётся отдельным historical evidence и не маскирует current coverage.

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
`max-parallel=4`; browser runtime устанавливается для exact groups, где хотя
бы один selected suite declares `requires_browser=true`. Один aggregate
job/check называется ровно `pr-gate`.

Planner публикует отдельно `execution_valid` и `release_valid`. Executable
invalid-registry fallback запускает full selected matrix, но `pr-gate` требует
оба значения `true`; тем самым failure evidence не превращается в admission.

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

Перед production probe/deploy exact canonical target file и `target_id`
разрешаются первыми и передаются adapter-у явно как global
`--target-file <canonical-target>` до subcommand. Вызов legacy/default target
«для проверки guard» не является target discovery или acceptance evidence.

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

`baseline` запускается только для prefix `codex/pr-gate-rollback-` и только
когда сломан сам required PR Gate. Obsolete cutover branch trigger удалён.
Ordinary task и Release Runner recovery не получают compatibility check и не
дублируют full `pr-gate` через baseline.

Два recovery contour различаются до создания branch:

1. **PR Gate healthy, Release Runner broken.** Создаётся ordinary bounded
   `repo_only` recovery PR без `codex/pr-gate-rollback-*`. Он проходит normal
   genuine exact-head `pr-gate`; baseline остаётся skipped. Поскольку trusted
   Runner ещё сломан, после successful gate допускается только документированный
   bounded expected-head squash merge этого recovery PR через GitHub с exact
   head readback — один bootstrap merge, без deploy/production mutation и без
   ослабления ruleset. Затем исходный PR обязан получить fresh head, current
   base и новый genuine exact `pr-gate`; старый plan/run не переиспользуется.
2. **Сломан сам PR Gate.** Используется exact
   `codex/pr-gate-rollback-*` compatibility contour. `baseline` проверяет
   rollback head; ruleset остаётся enabled. Любое genuinely necessary изменение
   required context/ruleset/security требует exact owner authorization и
   bounded readback. Bypass actors, отключение ruleset и возврат старого Release
   Train запрещены.

Если recovery требует второй PR, текущий implementation subagent возвращает
terminal handoff; следующий последовательный `SSS` начинается только после его
terminal state.

## Independent safety

Canonical live deploy остаётся exact merge SHA и сохраняет transport
reconciliation, auth, target/service and production probe guards из
[`10_hosted_runtime_deploy_contract.md`](10_hosted_runtime_deploy_contract.md).
Finance/storage lease, snapshot, writer/timer/barrier, restore, manifest и
non-target invariants остаются plan/apply guards, но не queue ownership.

`user-artifact` не входит в GitHub PR/release flow. Historical Actions logs,
comments и branches сохраняются как audit evidence.
