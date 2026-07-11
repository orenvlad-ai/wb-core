# Рабочий контекст wb-core

## Источники истины

- Git-tracked code и current `origin/main` задают code truth. Изменения в рабочей ветке считаются предложением до review и merge.
- Authoritative docs: `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*`.
- GitHub задаёт факты о ветках, commit, PR, checks, review и merge.
- WebCore Data MCP используется только read-only для production-состояния, диагностики и бизнес-метрик. Он не заменяет Git и не выполняет mutations.
- Production server — canonical deploy/runtime boundary. Запрещены server-only правки, ручной drift, ad-hoc SQL, обход repo-owned deploy/runbook и секреты в Git, docs, logs или PR.
- Legacy-артефакты используются только как migration evidence и do-not-lose constraints; они не являются normal implementation path.

Если меняется code, contract, runtime boundary, module status или другой зафиксированный truth, синхронизируй затронутые authoritative docs в той же задаче.

## Выполнение change-задач

Работай автономно до проверяемого результата:

1. implementation;
2. targeted tests/checks;
3. review итогового diff;
4. fixes и повторная проверка;
5. полный closure либо точный внешний blocker.

Не останавливайся на плане, гипотезе, локальном diff или открытом PR. Отдельная служебная строка о режиме выполнения не требуется.

Практические контуры:

- `repo-only`: targeted checks, review и GitHub closure; deploy не выполняется.
- `live/runtime`: после merge обязательны canonical deploy и live/public verify.
- `production data mutation/backfill`: заранее зафиксируй scope; выполни read-only preflight и dry-run; обеспечь backup/reversibility, idempotency и audit; пройди необходимые human gates; используй только repo-owned path, без ad-hoc server/SQL.
- `archived GAS guard`: только явно заданный bounded scope; publish и guard verify обязательны, но это не normal path.

## Git и GitHub

- Сначала проверь status/branch/remotes/auth, обнови `origin` и создай отдельную ветку от current `origin/main`; не смешивай и не теряй чужой dirty state.
- Для задач с GitHub closure Codex владеет всей рутиной: commit → push → PR → checks/review → fixes/recheck → merge → удаление ветки → подтверждение результата в `origin/main`.
- Останавливайся только на конкретном внешнем ограничении прав, approval или недоступной системе; сообщай точную причину и один минимальный ручной шаг.
