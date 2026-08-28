# Рабочий протокол `wb-core`

Этот файл — единственный mandatory operational entrypoint. Канонические
детали разделены по владельцам:

- execution lifecycle, actor routing и corrections —
  [`docs/architecture/07_codex_execution_protocol.md`](docs/architecture/07_codex_execution_protocol.md);
- workspace/UI, owner-facing surface и handoff —
  [`docs/architecture/13_codex_curator_workspace.md`](docs/architecture/13_codex_curator_workspace.md);
- authorization router state machine —
  [`docs/architecture/15_codex_authorization_router.md`](docs/architecture/15_codex_authorization_router.md);
- exact PR Gate/Release Runner flow —
  [`docs/architecture/11_github_release_train.md`](docs/architecture/11_github_release_train.md);
- domain/runtime invariants — релевантные `docs/architecture/*`,
  `docs/modules/*` и `migration/*`.

Исторические чаты, ветки, labels и Git history не являются текущими
инструкциями.

## Источники истины и preflight

1. `origin/main` и Git-tracked code задают code truth.
2. Authoritative docs задают действующие contracts.
3. GitHub задаёт PR, exact checks, merge и immutable release receipts.
4. Canonical production host и server-owned stores задают runtime/data truth.

Перед техническим выводом или изменением прочитай этот файл, только относящиеся
к задаче authoritative docs/code и fresh GitHub state. Перед записью проверь
status/remotes, выполни `git fetch --prune origin`, создай отдельную clean
branch/worktree от current `origin/main` и не смешивай чужой dirty state.

Выбор инструментов и источников не является требованием пользователя и всегда перепроверяется по актуальному протоколу, если пользователь отдельно явно не зафиксировал обратное.
Prompt не называет WebCore Data MCP prerequisite или fallback: archived
compatibility не является blocker, current canonical source acquisition
остаётся server-side.

## Mandatory task invariants

- Main task владеет accepted goal/decisions, коротким passport, scope,
  acceptance и единственной owner-facing surface. Новая WBC-задача получает
  atomic номер, title и pin по execution/workspace contracts.
- Ясное `сделай`/`исправь`/`реализуй`, а после design —
  `запускай`/`принимаю`/`доведи до конца`, разрешает autonomous implementation
  до `COMPLETE`. Явные `design-only`, `branch-only`, `до PR`, `до merge` и
  `до deploy` stop-lines сильнее default completion. Разговорное усиление вроде
  `одним проходом` или `сразу` не создаёт выдуманный числовой/lifecycle limit;
  literal quantitative limit пользователя обязателен по router contract.
- `Read-only` задаёт mutation boundary, не actor. Новое substantive technical
  evidence или long wait выполняет один fresh visible internal subagent;
  curator-control read ограничен exact immutable receipt/status readback без
  нового domain evidence или inference. Exact routing определяет doc07.
- Один bounded implementation block владеет одной branch и, без stop-line,
  одним non-draft PR. Same-scope correction продолжает тот же block/PR; новый
  scope или PR требует terminal предыдущего block. Internal execution создаётся
  только через `collaboration.spawn_agent`, не через user-owned sidebar task.
- После двух terminal pre-submit failures или двух sequential correction
  releases одной family третий narrow retry запрещён. Применяется consolidated
  terminal query-only/no-submit rehearsal и следующий consolidated correction
  PR строго по doc07; ordinary tasks и post-submit same-operation reconciliation
  не получают этот overhead.
- Evidence read обязан разрешать acceptance predicate, blocker или current
  failure hypothesis. Full logs/manifests/receipts остаются durable; active
  context и handoff используют bounded conclusion и exact pointers/digests.
- Пользователь не подтверждает повторно accepted goal, business meaning, exact
  plan или routine technical decisions. Любой owner-facing gate проходит
  closed router из doc15; discretionary permission questions запрещены.
- Видимая внутренняя работа subagent-а по умолчанию русская. После handoff только
  owning main task публикует owner outcome/допустимый business question и
  dispatch-ит continuation; compact handoff и plain-Russian пересказ определяет
  doc13.
- Post-task
  [`docs/architecture/14_codex_task_audit_checklist.md`](docs/architecture/14_codex_task_audit_checklist.md)
  не является execution checklist. Его читаёт только curator, отдельно
  оптимизирующий production protocol; ordinary main/domain curators и technical
  subagents его не применяют.

## Authorization и safety invariants

Router имеет только `AUTO_CONTINUE`, `EVIDENCE_BLOCKED`, `HUMAN_REQUIRED` с
closed reason codes и byte-stable receipt. Classification зависит от exact
semantic/final delta, не от storage medium или слов `material`, `risky`,
`production DB write`. Missing identity/evidence запускает automatic diagnosis,
а не human gate. Полный state machine и owner-gate deduplication задаёт doc15.

Независимо от authorization всегда сохраняются:

- exact CAS/readiness и target/destination binding;
- backup/recovery, rollback, one-submit, no-blind-retry и query-only
  readback/reconciliation;
- exact-head PR Gate и trusted Release Runner; unmerged PR не deploy-ится;
- no secrets, production dumps или unrelated edits в Git;
- exact destructive targets; broad recursive deletion запрещено.

Production read сначала определяет exact current target/source, затем использует
штатный SSH и query-only server-owned access (`mode=ro` и
`PRAGMA query_only=ON` для SQLite) либо эквивалент. Production probe/deploy
сначала разрешает canonical target и передаёт его runner-у явно; вызов
legacy/default target «для проверки guard» не является preflight evidence.

Для changing live resource curator/executor сам выбирает cheapest safe strategy:
semantic revalidation, selective quiet window, snapshot/generation плюс
tail/catch-up либо immutable CAS. Blanket stop cron/timers/services запрещён.
Unknown writer/job/FD даёт `EVIDENCE_BLOCKED`; selective pause начинается late
и требует exact prior-state restore, catch-up, health и query-only readback.
Эти правила не добавляют требований ordinary repo/user-artifact/query-only work
без consistent-boundary claim. Полный contract находится в doc07 и релевантных
domain docs.

Production mutation остаётся dry-run-by-default и использует bounded manifest,
pre-change digest, backup/recovery evidence, expected affected records,
non-target invariants, explicit apply и post-apply reconciliation. Apply submit
выполняется не более одного раза; ambiguous transport разрешает только exact
same-operation query-only readback. Ad-hoc SQL и local/server-only mutation
запрещены.

## Repository и release flow

Без explicit stop-line implementation subagent создаёт один non-draft
same-repository PR в `main`; labels не выбирают tests или release kind.
`ci/test_planner.py` строит byte-stable plan из exact PR base/head, registry
union, dependencies и changed paths. Unknown/unresolved mapping выбирает full
regression. Genuine exact-head PR workflow публикует aggregate `pr-gate` без
secrets.

После successful gate trusted-main Release Runner проверяет open non-draft PR,
exact base/head/plan/jobs/mergeability, делает один expected-head squash merge и
exact readback. `repo_only` завершается receipt `done`; `live_runtime` deploy-ит
exact merge SHA через canonical adapter; `production_mutation` только выпускает
`awaiting_apply` до separate Apply Runner. Stable terminal states и recovery
contours определяет doc11; queue, polling, blind resubmit и branch auto-sync не
используются.

Requested `XLSX/CSV/DOCX/PDF/TXT` вне Git — `user-artifact`; он не является `ДИАГНОСТИКОЙ`.
Branch/worktree/PR не создаются. Используй
`load_workspace_dependencies`: runtime предоставляет
`CODEX_PRIMARY_RUNTIME_NODE` и `CODEX_PRIMARY_RUNTIME_NODE_MODULES`.
Отсутствие `load_workspace_dependencies` само по себе не blocker; fallback — `openpyxl`,
`xlsxwriter`, dependency-free OOXML. Полный artifact contract определяет doc07.

## Проверка и terminal handoff

Перед завершением прочитай semantic diff целиком, выполни risk-proportionate
checks, исправь findings, повтори checks и проверь exact
PR/head/plan/check/receipt/merge/deploy state и отсутствие unresolved review
threads. Terminal technical handoff и owner-facing result формируются один раз
по doc13. Technical completion не синтезирует owner acceptance и не
archive/unpin пользовательскую task.
