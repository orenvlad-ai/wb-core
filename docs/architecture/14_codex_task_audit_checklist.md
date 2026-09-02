# Post-task audit checklist for the production protocol curator

## Статус и единственный потребитель

Это внутреннее read-only service guidance исключительно для одного куратора,
ответственного за оптимизацию WBC production protocol и process documentation.
Это не authoritative execution checklist.

Обычные main/domain curators и technical execution subagents **MUST NOT** читать или
вызывать этот документ ради исполнения задачи и **MUST NOT** менять из-за него
своё поведение. Их единственный operational entrypoint — root
[`AGENTS.md`](../../AGENTS.md) плюс релевантные authoritative domain docs.

Checklist только классифицирует уже существующее evidence. Он **MUST NOT** сам
создавать execution gate, approval, test, task, PR, deploy, apply, runtime/data
mutation или settings/ruleset change. Его вывод ничего не меняет автоматически.

## Миссия

Audit одновременно защищает от unsafe looseness и бюрократического роста. Для
каждого finding он проверяет в таком порядке:

1. можно ли сократить end-to-end wall-clock time;
2. можно ли убрать idle waits и ненужные human gates;
3. можно ли сократить uncached context/output, повторные проверки и tool calls;
4. пропорциональна ли verification реальному риску;
5. сохранена ли safety, пропорциональная доказанному harm, reversibility и blast
   radius.

Один harmless deviation по умолчанию не меняет протокол и не ведёт к
tightening. Любое замедление допустимо только для proven material risk, который
нельзя закрыть более дешёвым guard-ом.

## Детерминированный проход

Для каждой завершившейся задачи выполни один read-only проход:

1. **Eligibility.** Убедись, что audit выполняет назначенный production-
   protocol/documentation curator. Иначе остановись без finding и без действий.
2. **Evidence.** Используй только уже существующие task passport, meaningful
   transitions, terminal handoff, exact GitHub checks/receipts и durable
   operation evidence. Не создавай новый test или mutation ради доказательства.
   Отсутствие UI/status evidence не превращай в failure или success.
3. **Actor/context.** Проверь purpose и ownership evidence. Substantive
   technical read-only работа внутри main curator является actor/context
   finding, если owner-facing conclusion потребовал нового evidence из
   repository/code, logs, server, database, external API либо длительного
   ожидания. Curator-control reads, pure conceptual/clarification/design ответ
   и вывод из уже существующего exact handoff отклонением не являются. Finding
   проходит обычный cost-first decision order ниже; он не означает
   автоматический `TIGHTEN`, новый task gate или отдельное human confirmation.
4. **State split.** Отдельно зафиксируй terminal state technical execution block и
   business outcome main task. `Done` subagent-а не означает автоматически
   завершённый business outcome.
5. **Contour.** Отнеси наблюдение ровно к одному execution contour из таблицы
   ниже. Если наблюдений несколько, раздели их на независимые findings.
6. **Wait review.** Нормой является ровно один outstanding bounded
   terminal/event wait, покрывающий весь active set subagents и сохраняющий main
   turn активным, пока хотя бы один из них non-terminal. Чистый tool timeout
   разрешает немедленный silent re-arm того же wait как renewal lease/
   subscription; это не polling и не evidence прогресса. Любой status read
   (`list_agents`, worktree/Git/CI/status`) либо heartbeat/user-facing «ещё идёт»
   на timeout остаётся нарушением. После meaningful callback wait можно
   повторить на оставшийся active set; кроме silent timeout re-arm, иных
   повторов без нового event быть не должно.
7. **Classification.** Пройди decision order и назначь finding ровно один code.
8. **Cost test.** Для любой предлагаемой protocol/documentation change заполни
   все delta fields. Если данных недостаточно, оставь `NO_CHANGE`; не добавляй
   guard «на всякий случай».
9. **Stop.** Сохрани только read-only audit conclusion. Не открывай follow-up
   task/PR и не исправляй найденный domain/platform defect из этого audit.

## Execution contours

| Contour | Process treatment |
| --- | --- |
| Diagnostic/read-only technical execution | Audit отличает отдельные fresh-subagent blocks с неповторяющимися bounded evidence questions от curator-control reads. Независимые read-only blocks могут идти параллельно; main-owned substantive сбор, mutating capability внутри diagnostic block или duplicate question является actor/context finding и проходит обычный cost-first decision order, а не автоматически `TIGHTEN`. |
| Standard OS maintenance | Использовать штатную OS-процедуру в accepted scope с proportional target/recovery/readback evidence. Сам факт production host не создаёт blanket PR requirement. |
| Bounded reversible one-off infrastructure maintenance | Допустимо выполнить быстро без PR, только если заранее exact scope/identity, recovery, один mutation submit, non-target protection, post-action readback и durable receipt. Local `/tmp` допустим как working evidence, но никогда не как единственное terminal evidence destructive/production operation. |
| Recurring automation | Implementation должна быть repo-owned и проходить обычный repository/release flow. Audit не проектирует и не запускает automation. |
| Business-data mutation/change | Implementation должна быть repo-owned и следовать действующим dry-run/apply/readback/reconciliation contracts. Audit не создаёт manifest и не выполняет mutation. |

Отсутствие одного из guards для one-off maintenance не доказывает, что нужен
blanket repo-only путь: finding оценивает конкретный risk и ищет самый дешёвый
безопасный guard. Promo GC, journal retention, disk guard, DCP и любая другая
domain logic не исправляются здесь.

## Decision order и classification codes

Применяй первый подходящий пункт:

1. Defect относится к domain code/policy/runbook, а не к общему protocol:
   `ROUTE_DOMAIN` специализированному curator-у без исправления в этом audit.
2. Причина находится в Codex UI/status, orchestration platform или другом
   внешнем tooling, не управляемом protocol docs: `PLATFORM` без обходного
   protocol tightening.
3. Нет доказанного causal process gap, evidence недостаточно либо deviation был
   единичным и harmless: `NO_CHANGE`.
4. Существующий requirement/guard сохраняется, но другой mechanism или sequence
   даёт ту же safety быстрее, с меньшим context/output или меньшим числом
   checks/tool calls: `OPTIMIZE`.
5. Существующее правило доказанно шире риска и его можно безопасно ослабить или
   убрать: `RELAX`.
6. Риск/стоимость вызваны двусмысленной формулировкой при достаточной текущей
   защите: `CLARIFY`.
7. Доказан material harm path, текущего guard недостаточно и более дешёвого
   закрытия нет: `TIGHTEN`.

`ROUTE_DOMAIN` и `PLATFORM` — terminal routing conclusions этого audit, не
разрешение исправлять defect. Один finding не получает несколько codes.

## Mandatory change-cost record

Для каждого `RELAX`, `CLARIFY`, `TIGHTEN` или `OPTIMIZE` заполни:

```text
classification: <code>
evidence: <exact observed task/receipt/check facts>
current_rule: <exact protocol text or gap>
proposed_change: <one bounded process/documentation change>
delta_wall_time: <signed minutes or bounded percentage per task>
delta_human_gates: <signed integer per task>
delta_uncached_context_output: <signed tokens or bounded percentage per task>
delta_checks_tool_calls: <signed integer per task>
material_risk_closed: <concrete harm, reversibility and blast radius; or none>
cheaper_closure_considered: <alternatives and why insufficient, or n/a>
confidence: <high|medium|low>
```

Signed delta is proposed minus current: negative is faster/cheaper, zero is no
change, positive is slower/costlier. Диапазон допустим, когда exact telemetry
нет; пустое поле недопустимо. Если любое delta положительно, proposal проходит
только при concrete `material_risk_closed` и доказанно недостаточном
`cheaper_closure_considered`. Иначе classification возвращается в `NO_CHANGE`
или меняется на более дешёвый `OPTIMIZE`/`CLARIFY`.

## Terminal audit record

Один audit record содержит:

```text
task/block: <identity>
technical_execution_terminal: <state + exact evidence>
business_outcome: <state + exact evidence or unknown>
actor_context: <curator-control only | fresh subagent | deviation + exact evidence>
contour: <one contour>
finding: <one concise observation>
classification: <one code>
change_cost: <mandatory block or n/a>
routing_target: <specialized curator/platform owner or n/a>
audit_effect: read_only_no_action
```

Audit заканчивается этим record. Он не является execution instruction,
acceptance, authorization или основанием для автоматического follow-up.
Owner-facing представление record следует
[`13_codex_curator_workspace.md`](13_codex_curator_workspace.md#owner-facing-result);
его exact machine schema не ограничивается summary body limit.
