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

## Independent safety guards

Authorization router не заменяет target/destination binding, CAS/readiness,
one-submit, no-blind-retry, backup/recovery, rollback, query-only readback,
reconciliation и no-secrets. Они остаются machine guards. Missing guard
evidence блокирует действие и запускает correction; оно не становится новым
permission question.

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
