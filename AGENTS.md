# Рабочий протокол `wb-core`

Этот файл — самодостаточный entrypoint для Codex CLI, Codex в приложении ChatGPT для Mac, локального проекта Codex и обычного ChatGPT, читающего репозиторий. Доменные детали не дублируются здесь: они живут в authoritative docs.

## Источники истины

Приоритет источников:

1. Git-tracked code и актуальный `origin/main` задают code truth. Рабочая ветка — только proposed change до review и merge.
2. Authoritative docs: `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*`.
3. GitHub задаёт факты о branch, commit, PR, checks, review и merge.
4. WebCore Data MCP используется только read-only для production evidence, диагностики и бизнес-метрик. Он не заменяет Git и не выполняет mutations.
5. Production server — canonical deploy/runtime boundary.
6. Legacy-артефакты, старые чаты, вложения и прежние project instructions — только migration evidence и do-not-lose constraints, но не current truth и не normal implementation path.

Подробности: [source-of-truth policy](docs/architecture/03_source_of_truth_policy.md), [execution protocol](docs/architecture/07_codex_execution_protocol.md), [hosted runtime contract](docs/architecture/10_hosted_runtime_deploy_contract.md), [GitHub Release Train](docs/architecture/11_github_release_train.md).

## Кураторский preflight

Перед техническим выводом, постановкой задачи, реализацией или проверкой результата другого агента изучи:

- актуальный GitHub state;
- этот `AGENTS.md`;
- только релевантные authoritative docs;
- фактический code truth, если вывод касается текущей реализации.

Если репозиторий или необходимый источник недоступен, не утверждай уверенно текущее состояние: верни точный blocker. Если меняется code, contract, runtime boundary, module status или другой зафиксированный truth, синхронизируй затронутые authoritative docs в той же задаче.

## GOAL mode

Change-задачу формулируй через проверяемый конечный результат. Зафиксируй цель, ожидаемый проверяемый итог, bounded scope, существенные ограничения, acceptance criteria, closure criteria и применимый execution-контур. Routine-шаги из этого файла и authoritative docs не нужно копировать в каждый prompt.

## Execution-контуры

- `read-only`: анализ, диагностика или review без code, GitHub и production mutations. Итог — подтверждённый анализ либо точный внешний blocker.
- `repo-only`: code/docs change без live/runtime эффекта. По умолчанию включает implementation, targeted checks, semantic review, fixes/recheck, docs sync и полный GitHub closure. Deploy не применяется.
- `live/runtime`: изменение public route, service/process, operator UI, runtime behavior, deploy wiring или другого live-контура. Включает полный GitHub closure, canonical repo-owned deploy и live/service/public verify.
- `production data mutation/backfill`: только с explicit bounded scope, read-only preflight, dry-run/plan, verified backup или доказанной reversibility, idempotency/resumability, audit, необходимыми human gates, canonical repo-owned runner, post-run reconciliation и non-target invariants. Ad-hoc SQL, произвольные SSH-команды, server-only scripts и обход safety gates запрещены.
- `archived GAS guard`: только явно заданное bounded изменение archive guard. Оно не восстанавливает Google Sheets/GAS как current runtime; обязательны targeted checks, bounded publish и guard verify.

Production changes доставляются только repo-owned deploy/runbook path. Запрещены server-only drift, секреты в Git/docs/logs/PR и production mutations вне safety-контура.

## Выполнение и closure

Если пользователь явно не ограничил closure, Codex самостоятельно ведёт задачу до полного применимого результата:

- `repo-only`: implementation → checks → semantic review → fixes/recheck → docs sync → commit → push → PR → checks/review → merge → удаление feature-ветки → подтверждение результата в актуальном `origin/main`;
- `live/runtime`: весь `repo-only` closure → canonical deploy → live/service/public verify;
- `production data mutation/backfill`: применимый GitHub/runtime closure плюс весь обязательный safety-контур и human gates.

Явная граница пользователя имеет приоритет: например, «только ветка», «до commit», «до draft PR», «без merge», «без deploy» или «без production mutations». Остановись ровно на ней; отсутствие следующих стадий тогда не является ошибкой. Без такой границы не считай завершением план, гипотезу, незакоммиченный diff, только локальные проверки или открытый PR.

Перед изменениями проверь status/branch/remotes/auth, выполни `git fetch --prune origin` и создай отдельную ветку от актуального `origin/main`. Не смешивай, не очищай и не теряй чужой dirty state; при необходимости используй отдельный worktree.

Независимые change-задачи могут выполняться параллельно только в отдельных branch/worktree и отдельных PR. Для PR, явно поставленного в GitHub Release Train меткой `release:ready`, task owner добавляет ровно одну `scope:*` метку и продолжает наблюдать применимый terminal state: `release:done`, `release:production`, `release:blocked` или `release:halted`. Queue владеет только сериализованной секцией sync/checks/merge/deploy/verify; semantic conflict возвращается исходной задаче. `release:ready` и открытый PR не являются closure. `scope:production-mutation` автоматически не выпускается.

## Независимая проверка

Отчёт Codex или другого агента не является доказательством сам по себе. Перед подтверждением результата проверь применимое:

- фактический GitHub state, commit и branch;
- semantic diff, а не только список файлов;
- targeted tests/checks и исправление review findings;
- отсутствие unresolved review threads;
- согласованность с authoritative docs;
- для live/runtime — deploy commit и live/service/public результат;
- для production mutation — dry-run, backup/reversibility, audit, reconciliation и non-target invariants.

Пользователь нужен только для действительно human-only действия: login, отсутствующего permission/approval, ненадёжно автоматизируемого manual UI-check, materially different risk decision, production mutation approval или недоступного внешнего источника. Доступные Git, GitHub, test, review, merge и deploy действия в scope выполняет Codex.

## Итоговый ответ

Сообщай только применимое:

1. итоговый статус;
2. что реально изменено;
3. что реально проверено;
4. что намеренно осталось вне scope;
5. blocker и один минимальный следующий шаг — только если blocker есть.
