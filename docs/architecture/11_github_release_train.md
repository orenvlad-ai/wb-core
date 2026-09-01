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

Web-vitrina page-composition acceptance использует тот же canonical read route
с `surface=page_composition&probe_shape=1`. Сервер сначала строит обычную full
composition, оставляя её публичный контракт без изменений, а затем возвращает
bounded closed-schema proof с identity/as-of, table state/counts, наличием
loading table, отсутствием update summary, logical full bytes/SHA-256 и
component digests. Probe принимает только HTTP 200, exact `application/json`,
один совпадающий `Content-Length`, observed EOF, strict UTF-8/JSON без duplicate
или unknown fields и body не более 64 KiB. HTML, malformed/truncated response,
digest drift или overflow fail closed; JSON-prefix extraction для этого route
запрещён. Поэтому корректный full payload больше прежнего 768 KiB transport cap
не является release failure.

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
environment. Он поддерживает fail-closed scope-goal, exact-manifest,
warm-archive-mount-probe, warm-archive-readiness, receipt-recovery и
warm-archive-receipt-reconciliation modes. Source-specific
`wbc0027-receipt-reconciliation` is the mutation-incapable finalize-only mode
for the one already committed WBC0027 economics operation.

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

Перед исполнением exact-manifest commands Runner материализует canonical hosted
SSH identity и strict known-hosts options из production environment secrets во
временный mode-0600 contour. Один и тот же contour используется dry-run, apply,
readback и reconcile, затем временные файлы и process environment удаляются.

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

WBC0013 имеет отдельный exact двухфазный profile:

```text
/wb-core authorize-goal-v1 task WBC0013 profile dense-fbs-historical-recovery target wb_core_eu_hosted_runtime_active roster 71 existing 21 owner-approved-missing 50 zero-inserts 50 historical-date 2026-08-26 historical-nm 428853741 historical-version whfv_cb0657c384d5adebae01e585 historical-event ffbf_87cea959c9d600da99caa1ab68ef historical-repairs 1
```

Он принимает только текущую каноническую форму `71 = 21 + 50`; 50 —
owner-approved identity list (исходные 12 плюс WB Content 38). Исторические
capture/date/lineage и `missing / NULL` остаются audit-only и не входят в
admission или CAS A. Profile создаёт bounded private-0600 JIT планы только в
зарегистрированном backup destination `production_apply_evidence` внутри
exact mode-0700 operation directory. План сначала fsync-ится во временный файл
того же каталога, затем публикуется atomically без overwrite и остаётся durable
до submit/readback; qualification receipt связывает размер, mode, path,
file/directory fsync и полный root-storage admission result. Profile требует
два consecutive одинаковых material witness для A и B, максимум
с тремя регенерациями до submit. Runner делает ровно один A submit, только
query-only A reconciliation, затем fresh B plan, ровно один B submit и
query-only B reconciliation. B выбирает только exact date/nm/version/event из
passport и не строит broad mismatch set; timestamp кандидата детерминирован
accepted event. Потерянный ответ ведёт к same-operation readback,
а не к повторному submit. Profile default-off, deploy его не вызывает; обычные
service/timer не останавливаются и не меняются.
Remote command закрепляет deployed path, явный `PYTHONPATH`, target/generation
и mode-0700 evidence directory. Любой terminal failure сохраняется в receipt
как bounded typed `phase/stage/code/message/predicate/expected_cardinality/
observed_cardinality/candidate_digest/details_digest`; stderr digest не
заменяет доменную причину. В частности, storage admission сохраняет полный
`RootStoragePolicyError: <reason>`, а не обобщённую ошибку persistence.

WBC0027 использует отдельный consolidated JIT profile и не принимает
исторический exact-manifest/comment/phase identity из PR #1126:

```text
/wb-core authorize-goal-v1 task WBC0027 profile product-capital-qualified-economics target wb_core_eu_hosted_runtime_active product-rows 1152 product-cells 24192 product-mismatches 9446 primary-rows 936 primary-cells 19656 primary-mismatches 7655 secondary-rows 216 secondary-mismatches 1791 special-date 2026-08-21 special-nm 497413772 special-cells 16 blocked-date 2026-08-15 hard-non-target-from 2026-08-30 economics-logical 298 economics-persisted 472 economics-blocked 12 protected-nm 428853741 protected-unit-cost-rub 117.537167 submits 2 predecessor-pr 1128 predecessor-release-operation release-v2-52c958d066816e6e7b2fec7b419fc530 predecessor-release-comment 5471998411 predecessor-authorization-comment 5472023099 predecessor-apply-run 33343193199 predecessor-apply-comment 5472070488 predecessor-receipt sha256:2e65b37d7a44027928143d0f8b4ab71c43638450f659c4875faf3b0d80f7b9d5 predecessor-operation production-goal-v1-89bfdc5e4e4bffcbc9f6f6aea677e389 predecessor-product-phase recovery_303ece915dfb8e89b615a84dc8f14d70 predecessor-economics-phase recovery_8fe6bf612bde74c0dec9cb3b441944b2
```

Новая correction-authority точно связывает terminal blocked predecessor: trusted
release/comment, OWNER passport, Apply run/comment/artifact/receipt, zero submit
и обе non-reusable phase identity. Runner проверяет immutable GitHub/Actions
evidence до SSH. Predecessor остаётся только superseded audit evidence: его
goal/phase ids и private manifests не могут дать `already_terminal`, стать
новым namespace или участвовать в submit. Новый PR и новый comment id входят в
derivation свежего goal operation; старый passport без predecessor binding
больше не является допустимой WBC0027 grammar.

Runner выводит deployed SHA только из единственного trusted
`live_runtime/done`, создаёт две consecutive нормализованные material
witness для product, допускает максимум три регенерации только до первого
product submit и после submit делает только same-operation query-only
readback. Лишь retained/exact product readback разрешает fresh economics
qualification с отдельной phase identity и одним submit. Итоговый максимум —
два submit, по одному на фазу. Сбой до submit имеет typed `not_applied`;
transport/terminal uncertainty — `ambiguous`; exact retained readback —
`applied`. Existing terminal Apply receipt возвращает `already_terminal` до
SSH, private directory или нового файла.

Economics non-target CAS теперь имеет один versioned contract
`wbc0027_economics_semantic_non_target_digest/v1` со scope
`ready_snapshot_target_slices_removed_v1`. Planner, consecutive witness,
writer-lock rebuild, T1 pre-submit, in-transaction post-submit, retain и
query-only readback используют один и тот же all-ready-row snapshot: row count,
target-row count и component digests identities/semantic payloads/rows.
Допустима только ordinary semantic rebase до submit; exact target before-images
остаются CAS, а реальное изменение semantic non-target fail closed.

Эта строгость сохраняется для реального Apply между T1 before-image,
pre-submit, post-submit и retain. Отложенный `finalize-only` — иной temporal
boundary: он не пишет business data и не требует равенства позднего current
non-target историческому source digest. Receipt отдельно сохраняет
source/current row counts, source/current raw digests, current identity/payload/
row component digests и derivable changed identities/hashes. Недоступные source
semantic components не синтезируются. Поздняя ordinary evolution — только evidence, не
approval target changes; current target after-images остаются exact.

Факт business write сохраняется внутри той же SQLite transaction непосредственно
перед COMMIT как `committed_pending_reconciliation` с exact after/non-target
digests. Поэтому exception после COMMIT никогда не превращается в `submit=0` или
`database_written=false`: receipt возвращает `applied_pending_reconciliation`,
один submit и только same-operation readback/finalization.

Каждый фактический candidate хранится только в mode-0700 goal directory и
mode-0600 no-overwrite файле через `O_EXCL`, file fsync, atomic publish и
directory fsync; storage admission входит в receipt. Обычная публикация вне
accepted target slice не меняет material witness: apply заново строит current
candidate под warehouse writer lock и проверяет exact target before image.
Изменение target, deployed SHA, StoreRegistry generation или schema до submit
fail closed. Events/outbox/timestamps и широкие ready envelopes audit-only.
Workflow остаётся default-off в `production` environment; release/deploy его
не запускает, timers/services не останавливаются.

StoreRegistry binding использует canonical typed fields `generation_id`,
`manifest_sha256` и `schema_revision`. `generation_id` и `schema_revision` —
exact non-empty opaque strings: Runner не выводит из них prefix, число или
порядок и не выполняет integer coercion. Два consecutive witness обязаны иметь
одинаковые normalized material digest, phase identity и весь StoreRegistry
binding; private manifest, writer-lock rebuild, submit и readback проверяют его
exact equality. Empty, non-string или изменившийся revision блокирует phase до
submit с нулём business mutation.

Перед следующим Production Apply release можно квалифицировать deployed profile
только query-only/no-create: два consecutive product witness должны совпасть по
material/phase/generation/deployed binding, после чего `preflight --phase
product` входит в реальный `warehouse_sync_lock(runtime_dir, blocking=False)`,
повторяет JIT candidate под lock и останавливается перед T1. Preflight требует
`recovery_lifecycle=missing`, не создаёт private directory/manifest и возвращает
`production_mutation_submit_count=0`. Это release qualification, не Apply
dispatch и не authorization marker.

Для уже committed economics operation
`recovery_ae66a56f72d90b469b75d8adb893c51f`, ошибочно quarantined после source
Apply run `33345644125`, существует отдельный default-off mode
`wbc0027-receipt-reconciliation`. Он exact-bind source PR/run/artifact/receipt/
blocked marker/OWNER passport, исходные T1 before/after rows и quarantine reason,
а также exact `live_runtime/done` release фактически deployed reconciliation
code. Caller inputs `reconciliation_pr` и
`reconciliation_release_operation_id` всегда относятся только к этому release;
текущий trusted workflow checkout не подменяет deployed binding.

Workflow bridge выводится без нового dispatch input. `GITHUB_SHA` обязан быть
exact first-attempt `workflow_dispatch` checkout ветки `main`, связанным с одним
merged same-repository PR, exact successful PR Gate и trusted Release receipt.
При прямом совпадении с deployed SHA используется тот же `live_runtime/done`
receipt. Более новый SHA допускается только как descendant deployed SHA с exact
`repo_only/done` receipt и byte-identical closed source binding
`wbc0027_reconciliation_runtime_source_binding/v1`. В binding входят Apply
Runner, WBC0027 finalize-only runtime, warehouse recovery/storage/lock owners и
imported release receipt validators. Любое изменение одного из этих Git blobs
требует новый live-runtime reconciliation release; workflow/tests/docs-only
bridge может остаться repo-only. Workflow blob проверяется отдельно на current
main. Missing/ambiguous PR, Gate или receipt, divergent ancestry и source drift
fail closed. Receipt сохраняет deployed reconciliation release и workflow bridge
раздельно, а uploaded artifact provenance связан с bridge SHA.

Fixed remote command способен вызвать только `finalize-only`: он открывает
production SQLite query-only, не создаёт manifest или recovery row, не пишет
product/economics/outbox и не повторяет ни один submit. Full canonical receipt
загружается до единственного compact supersession marker; exact repeat
валидирует этот artifact и возвращает `already_terminal` до SSH. Missing,
foreign, duplicated или drifted evidence fail closed.

После failure run `33363863580` continuation обязана передать его только через
существующий `prior_reconciliation_run_id`; остальные prior artifact/comment/a02
поля остаются zero/empty. Runner source-specifically проверяет exact main SHA,
workflow/event/attempt, job `99400411103`, failed collect, skipped upload/publish,
artifacts=0, marker chronology, исходные logged source inputs и preflight
query-only/mutation0. Collect после успешного preflight всегда материализует
canonical `done` или `blocked` receipt. Transport failure, remote nonzero,
invalid JSON и validator failure имеют разные closed reason codes, bounded
redacted output плюс digests/parse error и named predicate failures. Workflow
upload использует `always()` и предшествует marker; blocked artifact никогда не
публикует done marker. Это exact WBC0027 continuation, не generic failed-run
bypass; 25-input surface и warm-mode prior semantics не меняются.

Source manifest PR #1129 имеет ровно legacy shape: raw `non_target_digest`,
три `functional_economics.patches` и три `material.semantic_patches`; полей
`functional_economics.semantic_non_target` и
`material.semantic_non_target_contract` в нём нет. Только для exact source
PR/run/artifact/receipt/marker/passport/deployed SHA/goal/manifest/generation/
phase adapter
`wbc0027_source_economics_transaction_legacy_adapter/v1` принимает это
отсутствие. Он связывает allowlisted cardinality `224 ready = 221 raw + 3
target`, raw digest, три target-removed before/planned-after equality, undo
artifact, write set `3/472` и source-code order CAS → after-readback → semantic
equality → COMMIT → retain/quarantine. Исторические per-row semantic component
digests не реконструируются и не декларируются. Current canonical semantic
builder остаётся versioned и strict для любого будущего Apply; поздняя current
non-target evolution сохраняется только typed receipt evidence, тогда как
current target after-images остаются exact equality gate.

У этой же immutable legacy source recovery row поле `after_digest` исторически
пустое. Wrapper принимает пустую строку только после exact allowlist binding и
полного `legacy_adapter/v1` transaction proof; current target digest и три row
hashes всё равно обязаны точно совпасть. Для любого другого source пустое поле,
а для exact legacy source любое непустое неравное значение, fail closed.

После terminal blocked run `33370422066` эта exception проверяется только pure
stdlib-модулем `apps/wbc0027_capital_recovery_source_binding.py`. Apply Runner
не импортирует full recovery runtime и не зависит от `openpyxl` либо другой
business-runtime dependency для receipt validation. Пустой `after_digest`
допускается лишь при одновременном exact source allowlist и полном legacy proof:
raw `221`, три exact target rows с removed-target equality, write set `3/472`,
undo/order и source-code binding. Модуль входит в собственный closed runtime
source set, поэтому его drift требует нового `live_runtime` release.

Следующая reconciliation generation exact-bind оба terminal predecessor:
artifact-less failed run `33363863580` и artifact-bearing blocked run
`33370422066` / artifact `9749833454` / receipt
`sha256:518fc39f3c7a17e84a247075f540ef393aed0110b827d276d322075de1000951` /
evidence
`sha256:87017b579f91e8c49de9111a38098cfef5e02f401467ba1726fb15ed736f9e3b`.
Receipt и compact summary используют generation v3 с одним consolidated
`wbc0027_reconciliation_terminal_predecessors/v1`; старые artifacts и markers
не переписываются. Input surface workflow не расширяется: первый run остаётся
caller-bound через `prior_reconciliation_run_id`, второй immutable predecessor
выводится и проверяется server-side. Новый release по-прежнему не dispatch-ит
`finalize-only`; отдельный default-off query-only continuation сохраняет
mutation/replay count zero.

Отдельная новая presentation-only операция WBC0013 не переиспользует terminal
A/B identity и имеет собственный exact profile:

```text
/wb-core authorize-goal-v1 task WBC0013 profile historical-analytical-cost-carry-forward target wb_core_eu_hosted_runtime_active business-date 2026-08-26 nm 428853741 unit-cost-rub 117.537167 accepted-versions 1 ready-snapshots 1
```

Typed owner-fixed lane принимает literal `117.537167 RUB` как historical
analytical estimate, связывает его с digest exact OWNER/MEMBER authorization и
не выводит из lifecycle/warehouse WAC. Обычный trusted carry-forward lane
сохраняет прежнюю event admission без ослабления. Два одинаковых JIT material
witness допускают максимум один submit. Candidate создаётся штатной формулой,
но write set ограничен одной accepted analytical version и CAS одного ready
snapshot; warehouse/source truth не меняется. После submit разрешён только
same-operation query-only readback, включая exact target/non-target digests и
все двенадцать target+TOTAL dependency cells. Deploy profile не запускает.

WBC0008 profile создаёт два одинаковых JIT material-CAS witness, затем ровно один
caller-known detached sanitation job. После submit разрешён только query-only
job/archive readback; ambiguous transport не запускает submit повторно. Exact
six archive/restore/unlink и capacity/non-target/service reconciliation описаны
в `migration/159_root_storage_warm_archive_wbc0008_006.md`.

После WBC0008 block-007 этому profile обязательно предшествует отдельный
`warm-archive-readiness` mode того же default-off workflow. Он принимает exact
authorization comment только как immutable scope/goal binding, выводит тот же
production-goal operation id, но не имеет submit/mutation primitive. Exact
merged PR, единственный `live_runtime/done` Release receipt, authorization
comment и derived goal определяют canonical deployed SHA и readiness-v2 base
identity. Для одной такой binding разрешены только contiguous attempts
`a01`..`a03`; каждый выполняет один полный query-only compression/material
projection и требует три consecutive clean lightweight activity/material-CAS
witness внутри максимум 60 секунд. Каждый attempt terminal и immutable;
blocked не переписывается, ready может быть только последним, duplicate/gap/
foreign/out-of-range fails closed. Следующий attempt под тем же deployed code и
goal устраняет необходимость пустого PR только ради свежего readiness id, но
не создаёт queue, automatic retry или unbounded loop. Один transient sample не
terminalizes будущий apply. Persistent write-capable/unknown FD opener, kernel
lock, sidecar, hold или material drift публикует structured readiness callback;
scope-goal operation допускается только после единственного final ready
attempt.

Ready receipt cryptographically binds the private projection path/SHA,
material digest, exact six source identities/SHA and conservative capacity
guard. После WBC0008 block 017 material partition `immutable_safety_v1`
separately binds source/sidecar/hold/provenance, destination/mount,
StoreRegistry/policy/ownership, protected non-target and canonical stable
topology. Mutable Finance/capacity, service PID/timing, source-activity and
ordinary canonical/protected size/mtime observations are re-evaluated at JIT
and under mutation-start locks by semantic health/capacity/activity predicates,
not by whole-snapshot byte equality. Только явные
root-policy resolver bindings для current Finance raw/operational и
Autoanswers относятся ко второй группе: их same-inode content/size/mtime
эволюция сохраняется как evidence, но не меняет material CAS; path/device/
mount/inode/type/symlink/owner/classification/StoreRegistry или service-access
relationship drift блокирует. После block 013 relationship задаётся versioned
explicit access-role matrix: каждый FD обязан совпасть с exact device/inode и
exact healthy declared systemd MainPID, а его mode — с reader/writer policy;
PID ambiguity, pathname/process fallback и unknown mode fail closed. Эта matrix
входит в stable topology digest. Subsequent JIT witnesses and mutation-start qualification reuse that
projection only while fresh stat/sidecar/FD/lock/hold/provenance/material CAS,
capacity and non-target checks remain exact. They do not repeat compression
measurement, full SQLite integrity or full source hashing merely to obtain two
equivalent witnesses. Actual archive/independent full restore/SQLite proof and
one exact full pre-unlink source hash remain mandatory inside mutation.

Source activity is a bounded observation stream, not a one-row-per-target
table. Its predicate requires semantic coverage of all six literal target
keys/paths and validates every observation against the matching exact target
identity and clear sidecar/FD/lock/hold/provenance evidence. Multiple clean
samples for one target are valid; a missing/foreign/malformed target, an unsafe
sample or a duplicate with identity drift blocks. Capacity and lifecycle-lock
collections likewise prove exact identity coverage, so equal raw row counts
cannot hide a duplicated row and a missing target/lock.

After WBC0008 block 022, stable filesystem CAS is semantic across the host and
the detached systemd worker mount namespaces. Before any readiness/apply for a
fresh release, the default-off workflow runs one
`warm-archive-mount-probe` through the exact deployed
`wb-core-storage-recovery-sanitation@.service` contour. The caller-known job has
no archive, unlink, service-restart, timer-change or business-data primitive.
Its immutable server result and Actions artifact bind the deployed SHA, repo
unit-template SHA, unit instance, mount namespace identity, canonical target
and family-anchor stat/realpath identities, and all sorted raw maximum-depth
mountinfo records including exact raw lines. The subsequent v4 readiness
receipt binds that exact probe job/evidence/artifact/comment and accepts only a
newer exact OWNER scope comment; a pre-probe or reused binding cannot authorize
the fresh readiness identity.

The v2 semantic selector independently proves every maximum-depth candidate's
exact `st_dev` and major/minor, source/`st_rdev`, UUID, filesystem type,
declared root/backup/generation role, policy owner, path/family-anchor placement,
normalized mount-root-to-target backing subpath, writable state and stable
integrity/write options. Multiple records collapse only when there is one
distinct semantic identity; its distinct-identity count/digest enters stable
CAS. Raw candidate count/digest/records and per-candidate proofs remain
observation evidence, so record order and namespace-local mount/parent ids do
not manufacture drift. Optional propagation fields, atime observations and
extra role-allowed restrictive `nosuid|nodev|noexec` flags are observation-only.
Loss of a declared generation restriction remains blocking. `ro`, unknown or
partial candidate evidence, different device/source/UUID/type/stable option,
divergent normalized path/root/anchor/role binding, destination on
root/generation, symlink escape, StoreRegistry, owner/classification or
access-role drift still fail closed with exact candidate/component evidence.

Any immutable or semantic-predicate mismatch before submit or mutation journal
durably writes a private exclusive-create/fsynced component-diff artifact bound
to readiness/operation/job/deployed SHA. It includes exact changed JSON paths,
classification, before/after component digests and bounded safe stat/identity
evidence. The first failure is never replaced by a later matching snapshot; no
destination/archive/unlink primitive follows it.

Before that projection the readiness receipt persists the complete 27-literal
systemd unit snapshot plus all 12 derived timer/owning-service classifications
defined by migration 159, including timer last/next trigger evidence. Waiting
plus successful inactive one-shot and running plus successful active/activating
one-shot are the only healthy pair predicates. A possible sequential-snapshot
edge receives one bounded paired resample contour whose exact samples remain in
the receipt; timeout, unknown state or failed Result/ExecMainStatus remains
blocked. A failed service gate publishes the same exact final rows, pair
evidence and failing predicates in its durable callback; the trusted workflow
never substitutes a generic service-health message or creates a production-
goal operation from that blocked receipt.

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
material source CAS и non-target invariants. Durable v4 receipt сохраняет все
candidate hashes, exact applied manifest, command/output digests,
`apply_count=0|1` и query-only result.
Для exact-six readback дополнительно обязательны раздельная immutable/mutable
reconciliation, before/after ordinary mutable observations и exact mutation-
scope ledger с нулём non-target unlink/move/write.

Terminal receipt publication использует PR timeline endpoint и явные
`issues: write` плюс `pull-requests: write`: workflow не полагается на
`issues: write` как достаточный permission для closed/merged PR. Полный
canonical receipt всегда сначала записывается в immutable private artifact;
PR comment — deterministic compact summary менее 65,536 bytes со state,
operation/apply count, job/error/component-diff summary и exact artifact name,
size/SHA-256. Поэтому oversized evidence не получает HTTP 422; любой 422 или
другой publication failure оставляет artifact доступным и никогда не повторяет
readiness, qualification, submit или mutation. Если apply уже
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

### Existing WBC0008 receipt reconciliation

`warm-archive-receipt-reconciliation` — отдельный repo-only contour только для
уже submitted exact-six WBC0008 operation, у которой source Apply run завершён
success, detached job имеет `succeeded/attempt=1`, а immutable source receipt и
единственный Actions-bot marker имеют ровно `state=blocked`, `apply_count=1` и
reason `post-submit-readback-not-reconciled`. Legacy reconciliation `a01`
остаётся immutable: run `33069817619`, artifact id `9645283377`, artifact
`root-warm-archive-reconciliation-pr-1075-run-33069817619`, receipt
`sha256:1b99b7a01127f963af31b0cafb2a764e928eb839662af665b1afa4646b9c4847`
и marker `5438726868` обязаны подтвердить `blocked/query-only-reconciliation-
not-proven`, zero mutation и единственный exact blocker старого timer predicate
для `wb-core-sheet-vitrina-refresh.timer`. Inputs обязаны exact-bind source
PR/run/artifact name/artifact SHA-256, owner authorization comment, blocked
marker, release/readiness/operation/job/manifest identities, deployed SHA,
legacy `a01` marker/artifact/digest и отдельный merged `repo_only/done` Release
receipt новой trusted-main reconciliation code. Любой другой state/reason/scope
или digest fails closed.

После exact legacy `a01` допускается только детерминированный `a02`. Его
sequence id привязан к тому же operation/job, a01 marker/artifact digest, а
attempt binding — также к новому repo-only merge/release SHA. `a01` не
переписывается и не redispatch-ится. Exact existing `a02` возвращает
`already_terminal` без SSH и comment; duplicate/foreign/different binding и
любой `a03` отклоняются. Blocked `a02` исчерпывает sequence и не создаёт queue,
retry или новый reconciliation attempt.

PR #1077 legacy `a02` terminalized именно так: run `33073151214`, artifact id
`9646668764`, receipt
`sha256:ce87472b71d1545cb8383ec417b1d83cba1c5f46568beb6249b9e66368d4030a`
и marker `5439297992` имеют `blocked`, zero mutation и exhausted legacy
generation. Они не переписываются и не становятся `a03`. Их artifact доказал
реальный code defect: duplicated probe classifier требовал у timer
`MainPID/ExecMainStatus`, отвергал допустимый `disabled` owner oneshot и не
следовал canonical paired classifier.

Только исправляющий этот defect новый merged `repo_only/done` release создаёт
отдельную generation `v2` с единственным attempt `v2-a01`; workflow передаёт
этот closed literal и не принимает attempt как dispatch input. Generation exact-
bind original source receipt/operation/job, legacy a01 и a02 run/artifact
archive/receipt/marker digests и exact code-delta release. `v2-a02`, `v2-a03`,
queue, retry и PR-identity nonce отсутствуют; exact existing `v2-a01` проверяет
artifact и возвращает `already_terminal` до SSH/comment.

После GitHub-only preflight workflow выполняет не более одного SSH process с
`PYTHONDONTWRITEBYTECODE=1`. Переданный через stdin probe имеет только direct
read и allowlisted `systemctl show`/`systemd-analyze cat-config` primitives. В
нём отсутствуют readiness, submit/apply/job creation, archive worker,
`readback_batch`, full restore/decompression-to-file, temp/lock acquisition,
service start/restart, timer change, SQL/file write и unlink. Probe сверяет
immutable job request/status/result и complete journal, ровно один submit,
exact-six source absence, exact 12 destination objects без foreign/temp/
partial/pending, текущие archive/manifest hashes и сохранённые stream/full-
restore/SQLite proof digests, six unlink intents/completions и reclaimed bytes.
Он также требует отсутствие active sanitation jobs/held locks, три стабильных
capacity sample выше root/Finance floors, свежий natural monitor `normal`, все
27 units/12 pairs, unchanged journald, direct non-target/StoreRegistry
identities и zero Promo/business/non-target mutation. Generation v2 сначала
exact-verify deployed SHA
`7d83c5d0ddf6bf86d6359409ef0f9a7bb4ad4747` и deployed
`apps/root_storage_warm_archive.py`, затем импортирует только canonical
query-only `SERVICE_NAMES`, 27-unit snapshot, unit-row и bounded paired
classifier symbols. Reconciliation не содержит собственной service
classification policy. Timer `MainPID/ExecMainStatus`, `Triggers` и next-
trigger не становятся обязательными полями, если canonical classifier их не
требует; realtime и monotonic next-trigger сохраняются как raw observation.
`static`/`disabled` owner oneshot оценивается по canonical state/result/PID
semantics. Canonical failed/unknown/masked/missing/nonzero и impossible pair
relation fail closed; только canonical transition получает максимум три
resample за пять секунд. Полные initial rows, final units/pairs и bounded
resamples остаются в artifact.

Полный canonical terminal receipt публикуется immutable Actions artifact до
любого нового PR comment. Затем на original operation PR добавляется один
отдельный compact supersession marker, который не изменяет старый receipt или
blocked marker и binds source artifact/comment, reconciliation release,
artifact SHA-256/evidence digest и terminal disposition
`done/reconciled_existing_operation`. Повтор exact inputs сначала скачивает и
проверяет уже bound artifact: exact same digest даёт `already_terminal` без SSH
и без второго comment; duplicate/foreign marker или любой different artifact/
receipt digest fails closed. Probe failure может опубликовать только один
immutable `blocked` reconciliation receipt/marker и никогда не публикует
`done`. Production mutation count этого contour всегда равен нулю.
Authoritative migration-159 terminal addendum находится в
[`159_root_storage_warm_archive_wbc0008_006_receipt_reconciliation.md`](../../migration/159_root_storage_warm_archive_wbc0008_006_receipt_reconciliation.md).

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

### WBC0027 general FBS mapping, impact and recovery

Release Runner deploys the generic manifest-driven capability as ordinary
`live_runtime` and performs no business-data mutation. Incident SKU, facility,
group, order/status count and date values live only in a checked-in incident
passport or a fresh private manifest.

The default-off mapping grammar is:

```text
/wb-core authorize-goal-v2 task WBC0027 profile fbs-identity-mapping-v2 target <target-id> incident-passport sha256:<incident-passport> operation <operation-id> inserts 1 submits 1
```

The runner parses `fbs_identity_mapping_manifest/v2`, obtains two matching
query-only stable-only material-CAS witnesses and requires a successful hypothetical
mapping/readback plus global impact/recovery rehearsal.  Mapping Apply permits
at most one canonical INSERT under the shared warehouse writer lock, writes a
private before-image, cannot touch lifecycle/history/WB state and is never
retried after an ambiguous transport result.  Its terminal query-only readback
digest is a mandatory input to impact generation.

Both real FBS Apply entrypoints require two separately validated release
lineages: the source `production_mutation/awaiting_apply` receipt and the
correction `live_runtime/done` receipt.  Each lineage binds exact PR
base/head/merge, Gate run/plan, Release Runner, comment, downloaded artifact,
archive/file digests and (for the source) incident manifest path/digest/
operation.  The correction base normally equals the source merge.  A moving
`main` is accepted only as a bounded exact descendant: every intervening merge
must form a linear main chain, have a fully downloaded and hash-verified
`repo_only/done` Release receipt, and touch only `docs/**` or executable
`*_smoke.py` test files.  Any workflow, runtime, registry, migration, manifest
or business-data surface makes the lineage `EVIDENCE_BLOCKED`.  The complete
commit/PR/Gate/Release/artifact/path proof and its digest become part of the
correction binding. There is one closed migration exception for the already
released faulty phase-identity runtime: PR 1145, head
`068446766a144348578cd8460d8f22f267460681`, merge/deployed
`5cdd45b5a499e630bed5277d46bd7047ac6624e2`, release operation
`release-v2-76858aebf78533adc107428d99a7aa33`, artifact `9774197000` and exact
changed-file proof digest
`sha256:2ca8871159a4ca9d79f3c0f9bb948e95d56b75634a202d6ca263cf4b04ba741b`.
It may occur only as `superseded_fbs_runtime` in that exact linear ancestry;
all PR/Gate/Release/comment/artifact/archive/file/path fields are equality
checked. It is never current correction evidence, terminal phase evidence or
Apply authorization. No other intervening runtime release is admitted.
FBS passports are accepted only through the explicit `fbs-mapping-qualification`,
`fbs-impact-generation`, `fbs-recovery-qualification`, `fbs-mapping-apply` and
`fbs-recovery-apply` modes; the old generic `scope-goal` route rejects them.

Qualification modes are default-off and terminate as
`qualified_no_submit` after the native non-blocking shared-lock boundary.
They perform zero submit, mapping, recovery, history and WB writes.  Mapping
plans use a new immutable per-attempt candidate path, so A/B witnesses never
overwrite each other.

Five workflow modes form one closed ordered lifecycle:

`mapping_qualification -> mapping_apply -> impact_generation -> recovery_qualification -> recovery_apply`.

The parsed passport still produces one immutable root goal operation; it is not
duplicated per mode. Each mode derives a distinct phase operation through
`wb-core.fbs-phase-binding/v1`. The derivation includes the phase, exact source
and correction release binding digests, incident-passport and authorization
body digests, plus the exact predecessor marker/artifact descriptor when the
phase has a predecessor. `blocked_comment_id` is reused as the exact FBS
predecessor marker input because the workflow dispatch surface is already at
GitHub's 25-input limit; for first `mapping_qualification` it must remain zero.

Before a later phase can reach checkout/SSH, Runner validates the selected
predecessor comment, requires it to be the only marker for that phase operation,
downloads and hashes the named artifact, validates its closed receipt and exact
terminal state, and checks common root/release/passport bindings. Mapping Apply
accepts only terminal mapping qualification; impact only terminal mapping Apply
and its readback digest; recovery qualification only terminal impact plus the
recovery passport; recovery Apply only terminal recovery qualification. A
missing, duplicate, foreign, cross-mode, skipped, reordered or drifted
predecessor fails closed.

`fbs_lifecycle_impact_manifest/v2` scans the complete fresh unresolved set and
derives every affected facility × SKU, facility total, global SKU and global
total plus FBS/capital/WAC/economics/history evidence.  It is reviewed data, not
a mutation submit.

The separate recovery grammar is:

```text
/wb-core authorize-goal-v2 task WBC0027 profile fbs-lifecycle-recovery-v2 target <target-id> incident-passport sha256:<incident-passport> mapping-operation production-goal-v2-<32hex> mapping-readback sha256:<terminal-mapping-readback> impact sha256:<impact-manifest> recovery sha256:<recovery-manifest> submits 1
```

Two matching query-only witnesses must retain the exact runtime, four distinct
StoreRegistry/schema fields, cutover/forward generation, complete target row
coverage, predicted effects, history bases and non-target/WB digests.  Recovery
Apply has its own one-submit boundary and terminal query-only readback; it cannot
write mappings or WB state.  History cells classified
`remain_missing_no_same_date_evidence` stay missing and do not block exact
recovery of cells classified `recoverable_exact`.

Only one OWNER/MEMBER comment may parse to an equivalent goal.  Duplicate
passports fail closed.  Every terminal marker is a closed exact JSON schema;
before publication the workflow uploads, downloads and hashes the canonical
receipt artifact.  Replay returns `already_terminal` only after the marker and
artifact validate exactly, and then performs no SSH, comment or workflow
dispatch. Terminal markers are phase-scoped: an earlier phase marker is
predecessor evidence, never terminal evidence for the next phase. Qualification
and impact phases have submit count zero; mapping and recovery Apply each have
their own one-submit budget. Ambiguity after either submit permits only
query-only readback under that same phase operation and never opens the next
phase.
