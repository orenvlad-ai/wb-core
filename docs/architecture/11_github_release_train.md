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
ними. Для genuine PR canonical artifact всегда исполняет planner bytes из
exact PR base checkout; exact head доступен только как read-only Git object,
полученный через canonical base-repository `refs/pull/<PR>/head` с SHA
readback. Ни head branch name, ни mutable `main`, ни head `PYTHONPATH` не
используются. Registry читается как из base, так и из head; canonical union
сохраняет оба набора commands/rules/dependencies, поэтому branch не может
удалить свою coverage. Любой group conflict, invalid registry или unresolved
dependency fail closed. Head schema, которую base planner не понимает, даёт
явный `head-registry-schema-incompatible-staged-migration`: unmerged planner
не исполняется как trusted substitute, а schema change разбивается на staged
migration.

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
- exact-base planner path/execution SHA/blob digest;
- exact-base selected-group harness path/execution SHA/blob digest и exact-head
  candidate working-tree binding;
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

Required workflow запускается genuine `pull_request` event. Fast core на exact
proposed head всегда выполняет syntax, diff hygiene, candidate
planner/Runner/Apply smokes и workflow YAML parse. Отдельный plan job остаётся
на exact base checkout, materialize-ит exact pull-ref head objects без checkout
или import head code и исполняет base planner в isolated Python. Selected suites
исполняются на exact head по immutable plan artifact; после pull-ref fetch и
SHA readback checkout token удаляется из local/recursive Git config до первого
candidate command. Selected job materialize-ит отдельный credential-free
exact-base worktree и запускает оттуда `ci/run_test_group.py`/plan verifier с
cwd exact head: orchestration/commands остаются base-owned, а сами commands
исполняют candidate tree. Head harness проверяется Fast core и активируется
только после merge. Matrix работает с
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
5. требует exact workflow name/path/event, first attempt и byte-identical
   base↔head `.github/workflows/pr-gate.yml`; head workflow никогда не
   исполняется Runner-ом как trusted substitute;
6. требует ровно Fast core, plan, canonical `Selected group · <group>` для
   каждого group из artifact и aggregate `pr-gate`, все unique/completed/
   successful; skip или extra/missing job fail closed;
7. требует trusted checkout SHA равным exact PR base, fetch-ит canonical
   `refs/pull/<PR>/head` с exact head readback и recompute-ит plan тем же
   isolated exact-base planner-ом;
8. требует byte-identical artifact/recomputed plan и protocol-v2 cutover epoch;
9. проверяет, что epoch является ancestor base;
10. проверяет durable operation receipt ровно один раз.

Любое будущее изменение самого trusted `pr-gate.yml` получает явный
`pr-gate-workflow-change-requires-staged-bootstrap` и не проходит ordinary
Runner admission. Оно использует отдельный reviewed staged/bootstrap contour;
это исключение не распространяется на обычные изменения planner/registry,
которые продолжают автоматически завершаться normal one-shot Runner-ом.

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

The active target's versioned root-storage policy is part of that same
canonical `live_runtime` adapter, not a manual post-release cleanup. Its current
mode is the block-004 corrective removal of the exact block-003 journald
drop-in. The deploy privately materializes a fresh full journal-root inventory,
removes that one exact file and submits one journald restart; later exact
deploys are readback no-ops. Ambiguous transport runs only
`journald-corrective-readback` and never repeats removal or restart. The exact
non-target reconciliation contract is migration 158; no journal, other file or
production-data mutation is admitted by this deploy binding.

## Default-off Apply Runner

Apply workflow имеет только manual `workflow_dispatch` и production
environment. Он поддерживает два fail-closed authorization mode.

Legacy `exact-manifest` inputs bind PR, merge/deployed SHA, manifest SHA-256,
durable operation id и exact authorization comment id. Authorization comment
обязан быть immutable `OWNER`/`MEMBER` body:

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

Task-scoped `scope-goal` mode не принимает merge/deployed/manifest hashes от
пользователя. Inputs содержат только merged PR, exact Release Runner operation
id и durable authorization comment id. Runner сам выводит exact merge/deployed
SHA из единственного trusted `live_runtime/done` receipt. Immutable
`OWNER`/`MEMBER` comment является scope-level passport, например:

```text
/wb-core authorize-goal-v1 task WBC0006 profile inventory-history-backfill target wb_core_eu_hosted_runtime_active dates 2026-03-01..2026-08-24 captures 177 components 18054 finalizations 177 full-days 172 partial-days 5
```

Для bounded file-lifecycle WBC0008 block 006 поддерживается отдельный exact
profile без manifest hash в owner passport:

```text
/wb-core authorize-goal-v1 task WBC0008 profile root-warm-archive-six target wb_core_eu_hosted_runtime_active sources 6 archives 6 manifests 6 unlinks 6 reclaimed-allocated-bytes <exact-bytes> root-minimum-bytes 26843545600 backup-floor-bytes <finance-next-replacement-plus-8GiB>
```

Этот profile создаёт два одинаковых JIT material-CAS witness, затем ровно один
caller-known detached sanitation job. После submit разрешён только query-only
job/archive readback; ambiguous transport не запускает submit повторно. Exact
six archive/restore/unlink и capacity/non-target/service reconciliation описаны
в `migration/159_root_storage_warm_archive_wbc0008_006.md`.

После WBC0008 block-007 этому profile обязательно предшествует отдельный
`warm-archive-readiness` mode того же default-off workflow. Он не принимает
owner authorization comment, не выводит production-goal operation id и не
имеет submit/mutation primitive: exact merged PR и единственный
`live_runtime/done` Release receipt определяют canonical deployed SHA, после
чего repo-owned runner выполняет один полный query-only compression/material
projection и требует три consecutive clean lightweight activity/material-CAS
witness внутри максимум 60 секунд. Один transient sample не terminalizes
будущий apply. Persistent write-capable/unknown FD opener, kernel lock,
sidecar, hold или material drift публикует один structured readiness callback;
scope-goal operation после такого receipt не допускается.

Ready receipt cryptographically binds the private projection path/SHA,
material digest, exact six source identities/SHA and conservative capacity
guard. Subsequent JIT witnesses and mutation-start qualification reuse that
projection only while fresh stat/sidecar/FD/lock/hold/provenance/material CAS,
capacity and non-target checks remain exact. They do not repeat compression
measurement, full SQLite integrity or full source hashing merely to obtain two
equivalent witnesses. Actual archive/independent full restore/SQLite proof and
one exact full pre-unlink source hash remain mandatory inside mutation.

Passport фиксирует business task, canonical target, profile, bounded dates,
exact expected insertions/quality и one-submit boundary; manifest hash в нём
не является human gate. Для supported profile Runner checkout-ит exact merge,
проверяет canonical current-live target и запускает только deployed repo-owned
backfill. Manifest создаётся JIT в operation-specific private `0600` evidence
directory на canonical host и остаётся immutable audit/recovery artifact вне
Git.

Перед mutation требуются два consecutive полных query-only dry-run с одним
material qualification digest. При material drift предыдущий candidate
superseded и выполняется bounded regeneration, максимум три, до первого submit;
это не mutation retry. Scope/count/quality/deployed-SHA escape fail closed.
После квалификации вызывается ровно один `--apply`; его nonzero exit или
ambiguous SSH transport никогда не повторяется. Отдельный deployed
`--readback` открывает canonical store query-only, reconciles exact applies row,
added capture/component/finalization counts, bounded-date visibility/quality,
material source CAS и non-target invariants. Durable v3 receipt сохраняет все
candidate hashes, exact applied manifest, command/output digests,
`apply_count=0|1` и query-only result.

Terminal receipt publication использует PR timeline endpoint и явные
`issues: write` плюс `pull-requests: write`: workflow не полагается на
`issues: write` как достаточный permission для closed/merged PR. Если apply уже
завершён и reconciled, но publication упала после записи immutable artifact,
режим `receipt-recovery` принимает exact merged PR, failed source run, его
детерминированное artifact name, SHA-256 exact
`production-apply-receipt.json`, operation id и исходный authorization comment.
Отдельный recovery job имеет только GitHub read/comment permissions: production
environment, SSH secrets, dependency install, dry-run, qualification, apply,
readback и любые production commands в нём отсутствуют.

Recovery требует один completed failed `workflow_dispatch` Production Apply run
на `main`, один unexpired artifact с exact run/head provenance, canonical bytes,
receipt `state=done`, derived goal/operation/authorization/release/merge/deployed
bindings, `apply_count=1`, successful query-only reconciled readback, один exact
apply-ledger receipt и preserved non-target digest. Wrong run/artifact/PR/
operation/digest, non-done receipt, incomplete evidence, чужой marker или
несколько marker comments fail closed. При нуле marker comments Actions bot
публикует original receipt payload и делает exact comment readback; один уже
существующий Actions-bot comment с byte-semantically тем же receipt считается
idempotent `already_terminal`, а второй comment не создаётся.

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
