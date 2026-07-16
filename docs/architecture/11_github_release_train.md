# GitHub Release Train

## Назначение

GitHub Release Train — repo-owned очередь для параллельно подготовленных change-задач. Она не создаёт отдельную базу задач: durable state хранится в GitHub PR, labels, checks, comments и workflow runs.

Цель контура:

- разрешить независимым Codex-задачам работать параллельно в отдельных branch/worktree;
- сделать каждый незавершённый change видимым как PR;
- сериализовать только критическую секцию `sync -> CI -> merge -> deploy -> verify`;
- не забывать готовые PR и не группировать их в непрозрачный общий merge;
- остановить дальнейшие production releases после первого неуспешного deploy/verify.

## Repo-Owned Артефакты

- `.github/workflows/baseline-ci.yml` — обязательный машинный baseline check с именем `baseline`;
- `.github/workflows/release-train.yml` — один сериализованный queue worker;
- `apps/github_release_train.py` — GitHub API/state-machine runner;
- `apps/github_release_train_smoke.py` — deterministic contract smoke;
- `.github/pull_request_template.md` — минимальный PR closure checklist.

Release Train не заменяет targeted checks и semantic review конкретной задачи. Метка `release:ready` означает, что task owner уже выполнил применимые targeted checks, прочитал полный semantic diff, исправил findings, синхронизировал authoritative docs и готов отвечать за acceptance criteria.

## Явная Постановка В Очередь

Ни один существующий или новый PR не обнаруживается как готовый неявно. Queue eligibility требует одновременно:

- open PR в `main`;
- same-repository head branch;
- PR не draft;
- метку `release:ready`;
- ровно одну execution-contour метку:
  - `scope:repo-only`;
  - `scope:live-runtime`;
  - `scope:production-mutation`.

`scope:production-mutation` никогда не выполняется автоматически: runner переводит такой PR в `release:blocked` и требует отдельный exact human gate по production-mutation protocol.

PR с `release:blocked` не выбирается. `release:halted` на любом PR, включая уже merged PR, является глобальным production stop и блокирует выбор следующего change до bounded recovery и явного удаления halt state.

## State Model

- `release:ready` — проверенный PR ожидает queue worker;
- `release:running` — worker выполняет финальную синхронизацию/выпуск; `release:ready` сохраняется до terminal outcome, чтобы interrupted run можно было безопасно повторить;
- `release:blocked` — PR-specific conflict, missing check/secret или другой blocker до merge;
- `release:done` — `repo-only` PR смёржен, deploy не применялся;
- `release:production` — exact merge SHA задеплоен и production verify успешен;
- `release:halted` — PR смёржен, но deploy/verify не доказан; вся очередь остановлена.

Workflow сам создаёт и поддерживает эти labels и три `scope:*` labels через GitHub API. GitHub Project может отображать те же labels как board, но не является дополнительным source of truth и не требуется для работы очереди.

## Последовательность Выпуска

Workflow имеет repository-wide concurrency group `wb-core-production-release` с pending queue. Каждый run выбирает старейший eligible PR и обрабатывает ровно один change:

1. читает state только из GitHub;
2. проверяет draft/base/head/scope gates;
3. для `scope:live-runtime` до merge проверяет наличие production secrets;
4. синхронизирует branch с текущим `main`, если PR отстал;
5. явно dispatch-ит `baseline-ci.yml` на финальной head branch и ожидает успешный check `baseline` на финальном head SHA; это не зависит от implicit workflow recursion после update-branch через `GITHUB_TOKEN`;
6. до live merge доказывает SSH connectivity к canonical EU target;
7. повторно проверяет, что `main` не ушёл вперёд, и squash-merges ровно проверенный head SHA;
8. checkout-ит exact merge SHA;
9. для `scope:live-runtime` запускает canonical `deploy-and-verify`;
10. записывает terminal label/comment, best-effort удаляет merged feature branch и явно dispatch-ит следующий queue run.

PR-specific failure до merge не изменяет `main` или production и переводит только этот PR в `release:blocked`. Failure после merge переводит PR в `release:halted`; последующие queued runs fail closed до recovery. Release Train не выполняет semantic conflict resolution: конфликт возвращается task owner исходного PR.

Явный dispatch следующего run обязателен: merge, выполненный стандартным `GITHUB_TOKEN`, не создаёт новый push-triggered workflow. `workflow_dispatch` является поддерживаемым исключением и безопасно ставит следующий run в ту же concurrency queue.

## Baseline CI Boundary

`baseline-ci.yml` запускается на каждом PR в `main` и выполняет:

- `python3 -m compileall -q apps packages`;
- `git diff --check` относительно base branch;
- `python3 apps/github_release_train_smoke.py`.

Это минимальный постоянный merge gate, а не полный универсальный test suite. Task owner обязан дополнительно выполнить релевантные smoke/browser/live checks по изменённому модулю и перечислить фактически выполненные команды в PR.

## Security Boundary

- `pull_request_target` используется только как label event; workflow всегда checkout-ит trusted `main` и не запускает PR code до merge.
- Production SSH material доступен только job с GitHub Environment `production`.
- Required secrets: `WB_CORE_DEPLOY_SSH_KEY` и `WB_CORE_DEPLOY_KNOWN_HOSTS`.
- SSH использует exact active EU target metadata, `BatchMode`, `IdentitiesOnly` и strict known-host verification.
- Secret values не печатаются, не попадают в Git, PR, comments или artifacts и удаляются с runner после job.
- Перед merge live PR проверяет SSH connectivity; deploy выполняется только после merge из clean exact merge SHA.
- Workflow не выполняет WB writes, data backfill или production business mutation. Deploy сам по себе не расширяет application mutation authority.

## Activation И Operator Flow

После merge этих артефактов push-to-main run создаёт labels. Для реального live release администратор один раз добавляет два secrets в GitHub Environment `production` и проверяет первый bounded canary PR.

Первый activation canary должен быть business-no-op change без production data mutation. Его terminal `release:production` подтверждает environment secret binding, strict SSH, exact merge, canonical deploy и production verify до постановки рабочих live PR в очередь.

Обычный task owner:

1. создаёт отдельную branch/worktree от актуального `origin/main`;
2. реализует change, делает targeted checks/review/docs sync и открывает PR;
3. добавляет ровно одну `scope:*` label;
4. после полного pre-release proof добавляет `release:ready`;
5. наблюдает workflow до `release:done`, `release:production`, `release:blocked` или `release:halted`;
6. при blocker исправляет тот же PR, снимает `release:blocked` и только затем снова ставит `release:ready`.

Открытый PR или только `release:ready` не являются closure. Task остаётся незавершённой до terminal state применимого execution-контура.
