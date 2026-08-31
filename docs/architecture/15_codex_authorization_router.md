# Codex Authorization Router v1

## Назначение и activation

Это authoritative contract для решения, можно ли autonomous curator-у
продолжить accepted goal или нужен один owner-facing callback. Исполняемый pure
validator — [`apps/codex_authorization_gate.py`](../../apps/codex_authorization_gate.py),
regression contract — `apps/codex_authorization_gate_smoke.py`.

Пользователь работает в одном режиме: explicit implementation intent
(`сделай`, `исправь`, `реализуй`, после design — `запускай`, `принимаю`,
`доведи до конца`) автоматически активирует goal-to-`COMPLETE`. Intake не
предлагает trust tier, approval mode или standing technical profile.
`approval_policy=never` и full technical execution остаются routine platform
profile, а не пользовательским выбором.
Activation начинает autonomous process, но не отменяет mandatory
pre-dispatch resolution перед implementation block.

Обычные owner phrases вроде `одним проходом`, `сразу` или `доведи до конца`
задают goal и отсутствие лишних пауз, но сами по себе не создают `max 1 PR`,
`max 1 retry`, `max 1 attempt/minute` либо другую числовую или lifecycle
границу. Router не выводит quantitative limit из разговорного усиления:
ограничение действует только когда пользователь задал его буквально. Явная
stop-line и literal quantitative limit имеют приоритет над default completion и
сохраняются в envelope без расширительного толкования.

Contract применяется только к technical blocks, начатым после merge этой
редакции. Он не будит, не меняет, не reclassify и не трактует задним числом
`wbc 0008` или `wbc 0010`.

## Canonical artifacts

Все artifacts — strict-schema UTF-8 JSON: sorted keys, без insignificant
whitespace, float/timestamp/random fields. Unordered collections
нормализуются сортировкой, duplicate/unknown fields fail closed. Digest имеет
вид `sha256:<hex>` и считается над canonical bytes. Ни один artifact не хранит
secret, credential value, token, cookie или raw protected data.

### Accepted goal и immutable envelope

`wb-core.codex-accepted-goal/v1` — внутреннее deterministic представление уже
принятого goal, а не новая анкета пользователя. Оно содержит единственное
`implementation_intent=IMPLEMENT_TO_COMPLETE`, goal statement, owner surface,
included final targets/destinations, allowed final и auxiliary final deltas,
allowed temporary dependency actions, forbidden effects, accepted-extension
bindings, terminal decision digests и optional superseded goal id.

`compile-envelope` выпускает
`wb-core.codex-authorization-envelope/v1`:

- `goal_id`, digest statement и exact `owner_surface_id`;
- included final targets и destinations;
- descriptors allowed final/auxiliary deltas;
- descriptors bounded temporary dependency actions;
- forbidden effect codes;
- answered accepted-extension decision/delta digests и terminal decision
  digests;
- fixed validity `COMPLETE_OR_SUPERSEDED` и envelope digest.

Delta descriptor связывает `target`, `destination`, `semantic_kind`,
`operation`, closed `effects` и `reversible`. Semantic kinds закрыты:
`technical`, `operational_control_metadata`, `business_semantic`,
`protected_business_fact`. Operational control metadata не становится
protected business fact из-за SQLite/server/production storage.

### Action manifest

`wb-core.codex-action-manifest/v1` содержит:

- exact goal/action/proposer surface identities;
- exact resources с audit-only `storage_medium` и role;
- final и auxiliary deltas с before/after digests;
- bounded temporary dependency actions с identity, preservation и readback;
- dependency status/evidence;
- submit state `not_started|submitted|ambiguous|reconciled`, intent, current,
  submitted и terminal operation identities;
- rollback/readback predicates;
- target/unrelated/stale warnings с evidence digests.

Manifest не может декларировать произвольные `risky`, `material` или mode
fields. Closed effects: `business`, `financial`, `external`, `publication`,
`security_access`, `destination`, `credential_capability`, `protected_data`,
`irreversible`. Unknown schema/effect/semantic/reason/state или malformed
identity/evidence fail closed.

### Owner-gate registry и receipt

На goal существует одна owner-facing surface. Optional
`wb-core.codex-owner-gate-registry/v1` связывает exact owner, pending gate и
answered `accepted_extension|rejected` records. Non-owner никогда не становится
publisher-ом.

`wb-core.codex-authorization-receipt/v1` содержит ровно один outcome, closed
reason codes, exact delta evidence, warning handling, explicit publication
disposition, stable `decision_digest` и `receipt_digest`. Receipt validator
обязателен перед owner-facing gate. Unknown/malformed receipt fail closed; он
не может молча превратиться в human question.

## Closed decision state machine

Допустимы только:

1. `AUTO_CONTINUE` — действие покрыто accepted goal или closed automatic
   correction/reconciliation rule;
2. `EVIDENCE_BLOCKED` — identity/evidence/readiness/authorization proof
   недостаточен; automatic diagnostic/correction required, вопрос запрещён;
3. `HUMAN_REQUIRED` — exact unmatched final/effect delta доказал один closed
   human predicate.

`HUMAN_REQUIRED` имеет только эти grounds:

- `NEW_BUSINESS_SEMANTIC`;
- `NEW_FINAL_TARGET`;
- `NEW_DESTINATION`;
- `NEW_EXTERNAL_EFFECT`;
- `NEW_PUBLICATION_EFFECT`;
- `NEW_FINANCIAL_EFFECT`;
- `NEW_SECURITY_ACCESS_EFFECT`;
- `CREDENTIAL_CAPABILITY_REQUIRED` для login/2FA/captcha или exact
  human-only credential capability;
- `NEW_PROTECTED_DATA_FINAL_DELTA`;
- `NEW_IRREVERSIBLE_FINAL_DELTA` вне allowlist.

Каждый ground требует exact item/delta digest, target/destination/effects и
machine reason evidence. Generic `material scope expansion`, `business-data
mutation`, `production DB write`, `risky` или prose `irreversible` reason code
не являются. Если перечисленные predicates не доказаны, permission question
protocol-invalid. Когда curator имеет одну dominant technical recommendation
и ему не нужна уникальная business preference пользователя, discretionary
question запрещён.

## Automatic и evidence-blocked rules

### Pre-dispatch

Owning main применяет те же closed outcomes до implementation spawn, используя
существующий compact task passport и exact available evidence, а не новый
artifact или gate. Однозначный accepted outcome/acceptance/boundary и dominant
technical path дают `AUTO_CONTINUE`. Недостающее substantive technical
evidence даёт `EVIDENCE_BLOCKED` и отдельный automatic bounded diagnostic/
read-only block на каждый независимый compact evidence question без human gate;
один question параллельно не дублируется, а diagnostic dispatch сам эту
проверку не требует.

`HUMAN_REQUIRED` возникает только когда exact evidence оставляет два или
более различных допустимых business outcomes без dominant technical choice,
а exact difference доказан existing unmatched final/effect delta и ground из
closed list. `Ambiguity` не является новым outcome/reason code. Owner получает
один concrete business question, различие и рекомендацию; technical permission
question запрещён. Required technical dependency остаётся `AUTO_CONTINUE`,
если final target, business meaning, destination и effects accepted goal не меняются.

### Pre-submit

Same-goal code/runtime defect до submit даёт `AUTO_CONTINUE` с новой
sequential operation/readiness identity, fresh checks и тем же envelope.
Terminal operation identity не переиспользуется. Missing identity/evidence,
unknown schema/effect или target warning без diagnosis даёт
`EVIDENCE_BLOCKED`, никогда `HUMAN_REQUIRED`.

Sequential identity не требует пустого code PR, если capability contract уже
задаёт bounded same-release attempt sequence. Для WBC0008 exact-six это только
contiguous readiness-v2 `a01`..`a03`, привязанные к одному deployed SHA,
authorization comment и derived goal operation; каждый attempt terminal и
immutable, а exhaustion не создаёт queue/retry. Новый PR нужен только для
реального code/runtime delta, не как identity nonce.

Для WBC0027 terminal blocked pre-submit attempt из PR #1128 остаётся
неизменяемым predecessor evidence. Реальная correction release использует
новые PR/comment/release bindings и fresh derived goal/phase identities; её
OWNER/MEMBER passport точно связывает старые release/passport/run/receipt и обе
старые phase identities только как superseded zero-submit evidence. Старый
passport, marker, operation или private manifest никогда не даёт
`already_terminal` новой release и не переиспользуется для submit. До следующего
отдельного Production Apply допустима deployed query-only/no-create
qualification с двумя witnesses и реальным non-blocking shared-lock preflight,
который останавливается до T1 и требует свободный recovery namespace.

Unrelated warning записывается, stale warning refresh-ится. Они не создают
gate и не расширяют scope.

### Dependencies

Required dependency нельзя пропустить. Failed dependency допускает automatic
remediation только если manifest содержит exact allowlisted bounded temporary
action или auxiliary final transition, preservation/readback predicates и не
создаёт undeclared business/financial/external/publication/security/
destination effect. Иначе это `EVIDENCE_BLOCKED` либо, только при точном новом
closed effect, `HUMAN_REQUIRED`.

Canonical Autoanswers example: exact dependency task
`processing -> terminal_error`, clear exact lease, append audit, preserve
settlement, zero provider/WB/publication/financial/business deltas —
`AUTO_CONTINUE`, даже если operational control metadata хранится в production
SQLite.

### Submit ambiguity

После `submitted` или `ambiguous` mutation разрешён только query-only readback/
reconciliation exact same operation identity, с нулём new final/temporary
actions. Blind retry, новый submit и guessed success запрещены. Reconciled или
terminal old identity не переиспользуется для следующей correction; создаётся
fresh sequential identity.

Отложенное terminal receipt reconciliation той же уже submitted operation —
`AUTO_CONTINUE`, если exact immutable source receipt имеет единственный
allowlisted post-submit readback blocker, новый trusted code release repo-only,
remote contour query-only и production mutation count равен нулю. Такая
supersession завершает evidence/receipt того же operation id; она не является
новой correction, readiness, submit, job или разрешением повторить mutation.
Любая новая final/temporary action, другой blocker/scope/digest или ambiguous
preexisting supersession marker остаётся fail-closed и требует обычной fresh
route decision.

WBC0027 after-COMMIT false quarantine — частный allowlisted случай этой нормы.
Durable `after_digest`/`committed_pending_reconciliation` означает submit=1,
`database_written=true` и `applied_pending_reconciliation`, даже если последующий
retain/readback выбросил exception. Automatic continuation может вызвать только
deployed `finalize-only` contour, exact-bound к source run `33345644125`, artifact
`9741910399`, receipt/blocked marker/authorization и original T1 journal. Contour
query-only, mutation0/replay0 и сохраняет quarantined row immutable. Исторический
source transaction доказывается независимо от current: source raw scope и три
T1 before/planned-after rows после удаления target slices обязаны совпасть,
тогда как поздняя ordinary non-target evolution допускается и записывается
typed source/current raw digests и current semantic components. Это receipt
evidence, а не разрешение target
изменения; current target after-image drift всегда блокирует. Единственный
новый effect — uploaded receipt и compact supersession marker. Любой mismatch
остаётся `EVIDENCE_BLOCKED`, не разрешением на повтор product/economics Apply.

Allowlist относится только к immutable source PR `1129`, run `33345644125`,
artifact `9741910399`, receipt
`843d1eb81d92ac16a51bc21fb92256916e4c9c3a353d3221ebc1a82df80bf9f5`,
blocked marker `5472359912`, predecessor OWNER passport `5472278622`, source
SHA `876f5f307a2053d66544dd1c8950f94f77f92ddb`, goal
`production-goal-v1-5024719a64fa9707b72d938ebf8a2127` и private manifest
`675fcb98fdcc74ce2d30c4e907c9c5330f7878fee929027c536b5a6f03ec47c4`.
Legacy adapter не изобретает отсутствующие source semantic components: source
truth — raw 221-row aggregate плюс три exact target-removed T1 equality и
undo/order proof. Эта exception grammar недоступна future Apply manifest-ам.

Для этого WBC0027 contour caller-provided reconciliation PR/operation всегда
bind exact `live_runtime/done` deployed finalize-only code. Trusted current-main
workflow SHA выводится отдельно из GitHub dispatch/PR/Gate/Release evidence: он
либо равен deployed SHA, либо является его descendant с exact `repo_only/done`
receipt и byte-identical closed reconciliation source binding. Workflow-only
bridge не становится новым production release; изменение Apply/finalize/
warehouse-policy/receipt-validator blob без нового live-runtime release даёт
`EVIDENCE_BLOCKED`. Оба binding сохраняются раздельно в query-only receipt.

Для WBC0008 terminal-receipt contour legacy reconciliation `a01` также является
immutable terminal evidence. После exact validation его run/artifact/receipt/
marker, того же source operation/job, zero mutation и единственного timer-
predicate blocker разрешён ровно один derived `a02`, bound к a01 artifact/
marker digests и новому merged `repo_only/done` release. Exact replay `a02`
является `already_terminal`; blocked `a02` исчерпывает sequence. Foreign,
duplicate, different-digest и `a03` fail closed, не создавая human gate, queue
или retry.

Blocked legacy `a02` остаётся exhausted terminal generation и никогда не
становится `a03` или retry. Доказанный в его immutable artifact дефект самого
duplicated reconciliation classifier допускает только real code-delta
generation `v2`: она exact-bind original submitted operation/source receipt,
оба terminal `a01/a02` run/artifact archive/receipt/marker digests и новый
merged `repo_only/done` release. В этой generation существует ровно один
`v2-a01`; exact existing terminal marker даёт `already_terminal` до SSH/comment,
а `v2-a02`, `v2-a03`, queue и identity-only PR запрещены. Remote contour
по-прежнему имеет один query-only SSH probe и production mutation count zero;
это same-operation receipt evidence correction `AUTO_CONTINUE`, а не новая
readiness, operation или owner gate.

## Owner publication deduplication

Human decision material состоит из goal, exact owner, sorted reason codes и
exact unmatched delta digests. Поэтому одинаковый gate на двух curator
surfaces получает одинаковый `decision_digest`.

- owner surface + новый exact decision: `PUBLISH_ON_OWNER`;
- non-owner surface: `ROUTE_TO_OWNER`, только structured evidence;
- existing pending same decision: `SUPPRESS_DUPLICATE`;
- answered accepted extension или любой её delta subset: `AUTO_CONTINUE`;
- rejected answered gate: `EVIDENCE_BLOCKED`, без повторного вопроса.

Таким образом один goal не публикует два owner questions, а answered decision
не теряется при routine correction или смене technical actor.
Кто после technical handoff публикует owner-facing outcome и dispatch-ит
continuation, определяет workspace contract
[`13_codex_curator_workspace.md`](13_codex_curator_workspace.md); router
определяет только authorization outcome и publication disposition.

## Independent safety guards

Authorization router не заменяет target/destination binding, CAS/readiness,
one-submit, no-blind-retry, backup/recovery, rollback, query-only readback,
reconciliation и no-secrets. Они остаются machine guards. Missing guard
evidence блокирует действие и запускает correction; оно не становится новым
permission question.

Для WBC0027 accepted goal-to-COMPLETE разрешает consolidated technical
correction, merge/deploy и query-only deployed qualification, но не подменяет
отдельный immutable Production Apply passport. Release block обязан завершить
`live_runtime/done` и может подготовить fresh OWNER/MEMBER scope-goal comment;
сам Production Apply dispatch и его business-data submit остаются отдельным
следующим действием. Исторические manifest/comment/operation bindings не
расширяют эту авторизацию и fail closed.

Для general FBS mapping correction release также разрешает только inert
capability и deployed query-only/no-submit rehearsal. Отдельный
`fbs-identity-mapping-v2` OWNER/MEMBER passport exact-bind digest incident
passport, target, operation, один insert и один submit. Strict
`fbs_identity_mapping_manifest/v2` переносит runtime, четыре раздельных поля
StoreRegistry/schema, cutover/forward generation, arbitrary exact tuple,
external/owner/warehouse/facility-admission evidence и material CAS. Orders,
statuses, groups и dates в mapping manifest запрещены.

Перед любым FBS qualification/Apply router требует две разные complete
release lineage: source `production_mutation/awaiting_apply` и correction
`live_runtime/done`. Проверяются exact PR base/head/merge, Gate, Release
Runner, comment, downloaded artifact archive/file digest и source manifest;
correction base обычно обязан совпасть с source merge. Если `main` успел
продвинуться, допускается только bounded exact linear ancestry: каждый
промежуточный merge имеет downloaded/hash-verified `repo_only/done` receipt и
меняет исключительно `docs/**` или executable `*_smoke.py`; workflow/runtime/
registry/migration/manifest/business-data path блокирует lineage. Exact
commit/PR/Gate/Release/artifact/path proof включается в correction binding.
Один parsed goal может иметь
ровно один OWNER/MEMBER comment. Equivalent duplicate — `EVIDENCE_BLOCKED`, а
не выбор первого комментария.

Terminal mapping readback открывает только подготовку fresh
`fbs_lifecycle_impact_manifest/v2`; он сам не является recovery authorization.
Отдельный `fbs-lifecycle-recovery-v2` passport exact-bind incident passport,
mapping operation, terminal mapping readback, impact digest, recovery digest и
один submit.
Mapping Apply может сделать только один canonical mapping insert; recovery
Apply не может писать mapping или WB. Release, rehearsal и подготовка обоих
passport body не являются authorization или dispatch обоих Apply.

Default-off modes `fbs-mapping-qualification`, `fbs-impact-generation` и
`fbs-recovery-qualification` завершаются `qualified_no_submit` до remote apply
command. Реальные submits доступны только через `fbs-mapping-apply` и
`fbs-recovery-apply`; generic `scope-goal` FBS passports не принимает. Terminal
marker публикуется только после upload/download/hash canonical artifact.
`already_terminal` возможен лишь после exact marker+artifact validation и
означает SSH/comment/dispatch count `0`.

Query-only WBC0027 same-operation finalization не является новым Production
Apply business submit и не требует нового OWNER passport: она использует exact
digest существующего accepted passport как immutable predecessor authorization.
Release block может выполнить только deployed no-submit qualification и передать
точные inputs mode `wbc0027-receipt-reconciliation`; dispatch этого default-off
mode остаётся отдельным terminal handoff.

Validator pure: он не пишет GitHub, runtime, database или owner surface и не
выполняет proposed action. Repository test registry исполняет smoke contract;
owner-facing actor использует только validated receipt и publication
disposition.

## CLI

```text
python3 apps/codex_authorization_gate.py compile-envelope \
  --goal accepted-goal.json --output envelope.json

python3 apps/codex_authorization_gate.py decide \
  --envelope envelope.json --manifest action-manifest.json \
  --gate-registry owner-gates.json --output receipt.json

python3 apps/codex_authorization_gate.py validate-receipt \
  --receipt receipt.json
```

CLI output для envelope/receipt всегда canonical bytes с одной terminal newline.
Malformed decide input возвращает `EVIDENCE_BLOCKED/INVALID_INPUT`; он не
публикует и не предлагает human question.
