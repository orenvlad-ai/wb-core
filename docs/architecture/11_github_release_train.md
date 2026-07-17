# GitHub Release Train

## Назначение

GitHub Release Train — repo-owned сериализованная очередь для независимо подготовленных STANDARD и LOOP change-задач. Durable state хранится только в GitHub PR, labels, checks, comments и workflow runs. Очередь владеет критической секцией `sync -> baseline -> merge -> deploy -> verify`, но не заменяет task-level targeted checks, semantic review и docs sync.

Класс задачи и execution contour независимы. PR обязан иметь ровно одну task label и ровно одну scope label:

- `task:standard` или `task:loop`;
- `scope:repo-only`, `scope:live-runtime` или `scope:production-mutation`.

`task:loop` совместим только с `scope:live-runtime`. Диагностические задачи строго read-only и в Release Train не входят.

## Repo-Owned Артефакты

- `.github/workflows/baseline-ci.yml` — обязательный check `baseline`;
- `.github/workflows/release-train.yml` — один repository-wide queue worker и GitHub-native LOOP command handler;
- `apps/github_release_train.py` — GitHub API/state-machine runner;
- `apps/github_release_train_wait.py` — bounded CLI waiter для Codex;
- `apps/github_release_train_smoke.py` — deterministic state-machine smoke;
- `.github/pull_request_template.md` — PR closure checklist.

## Eligibility И Labels

Queue eligibility требует одновременно:

- open non-draft PR в `main`;
- same-repository head branch;
- `release:ready`;
- ровно одну известную `task:*` label;
- ровно одну известную `scope:*` label;
- отсутствие `release:blocked` и `release:halted`.

Основные state labels:

- `release:ready` — task owner закончил pre-release proof и явно поставил PR в очередь;
- `release:running` — worker выполняет sync/baseline/release;
- `release:awaiting-agent` — LOOP прошёл sync/baseline и ждёт exact-head acknowledgement активной Codex-сессии;
- `release:awaiting-ui` — LOOP merge задеплоен и ждёт production UI Flow/acceptance;
- `release:blocked` — PR-specific failure до merge;
- `release:done` — terminal success STANDARD `repo-only` без deploy;
- `release:production` — terminal success STANDARD live/runtime или принятой LOOP-цепочки;
- `release:halted` — failure после merge; вся очередь остановлена.

Промежуточные `release:ready`, `release:running`, `release:awaiting-agent` и `release:awaiting-ui` не являются closure.

## STANDARD Flow

STANDARD PR проходит существующую последовательность без agent acknowledgement:

1. worker выбирает старейший eligible PR;
2. синхронизирует branch с current `main`;
3. явно dispatch-ит `baseline-ci.yml` и ждёт новый successful `baseline` на final head SHA;
4. повторно проверяет exact head/base/task/scope/mergeability;
5. squash-merges только проверенный head;
6. `scope:repo-only` получает `release:done` без deploy;
7. `scope:live-runtime` checkout-ит exact merge SHA, вызывает canonical `deploy-and-verify` и получает `release:production`;
8. worker best-effort удаляет feature branch и dispatch-ит следующий queue run.

`scope:production-mutation` никогда не выпускается автоматически и до merge получает `release:blocked` с требованием отдельного human-gated production-mutation protocol.

## LOOP Pre-Deploy Handshake

LOOP нельзя merge/deploy автоматически только потому, что он стал первым в очереди. Первый worker pass выполняет sync и baseline, затем ставит `release:awaiting-agent` и прекращает release. Это состояние является глобальным fail-closed gate: пока активная сессия не подтвердит готовность, остальные PR ждут и production не меняется.

Repo-owned waiter:

```bash
python3 apps/github_release_train_wait.py <PR>
```

Увидев `release:awaiting-agent`, waiter публикует на этом PR единственную bounded GitHub mutation — точный comment:

```text
/wb-core loop ack-agent <PR> head <EXACT_40_CHAR_HEAD_SHA>
```

Workflow принимает command только от `OWNER`, `MEMBER` или `COLLABORATOR`, проверяет номер PR, open/non-draft state, `task:loop + scope:live-runtime`, recovery linkage и текущее exact head. Принятый ack кодируется одноразовой label `loop:ack-<HEAD_SHA>`, возвращает PR в `release:ready` и dispatch-ит worker.

На втором pass baseline снова доказывается для того же head. Ack удаляется непосредственно перед merge API call. Изменение head на любом этапе делает старую label невалидной; worker снова ставит `release:awaiting-agent`. Каждый recovery PR имеет другой PR/head identity и требует собственного acknowledgement.

`--no-ack-agent` оставляет waiter полностью read-only. STANDARD waiter никогда не публикует comments.

## Exclusive Production UI Gate

После успешного LOOP merge, canonical deploy и production verify worker не ставит terminal success и не dispatch-ит следующий release. Он создаёт deterministic chain label `loop:root-<ROOT_PR>`, ставит текущей итерации `release:awaiting-ui` и завершает job. Push-triggered или повторный queue run видит gate и не выбирает несвязанный PR.

Если production UI Flow не принят, исчезновение Codex не открывает очередь: `release:awaiting-ui` остаётся durable fail-closed state.

При успешном UI Flow активная Codex-сессия оставляет точную GitHub-native command на текущей итерации:

```bash
gh pr comment <ACTIVE_LOOP_PR> --body "/wb-core loop accept-ui <ACTIVE_LOOP_PR>"
```

Handler проверяет write association, активный gate и deterministic chain. Он идемпотентно переводит все merged PR этой chain в `release:production` и dispatch-ит следующий queue run. Повторная acceptance не меняет terminal state и не вызывает повторный merge/deploy.

## Recovery PR

Во время `release:awaiting-ui` разрешён только recovery текущей LOOP-цепочки. Связь не извлекается из title/body/free text: recovery PR обязан иметь exact dynamic label `loop:root-<ROOT_PR>`, уже созданную worker для активной chain, а также `task:loop + scope:live-runtime + release:ready`.

Пример постановки recovery в очередь:

```bash
gh pr edit <RECOVERY_PR> \
  --add-label task:loop \
  --add-label scope:live-runtime \
  --add-label loop:root-<ROOT_PR> \
  --add-label release:ready
python3 apps/github_release_train_wait.py <RECOVERY_PR>
```

Несвязанные STANDARD, LOOP и production-mutation PR сохраняют `release:ready`, но не выбираются. Recovery проходит новый baseline и новый exact-head acknowledgement. После его deploy `release:awaiting-ui` снимается с прежней итерации и ставится recovery PR; root label не меняется. Повторный transfer command лечит допустимый duplicate-gate partial state в пользу новой итерации, а неоднозначные roots оставляют очередь fail-closed.

## CLI Waiter Contract

`apps/github_release_train_wait.py` получает номер PR, выводит только изменения `class/scope/state/head` и использует GitHub CLI auth/repository context, если env не задан.

- STANDARD ждёт `release:done` для `scope:repo-only` или `release:production` для `scope:live-runtime`;
- LOOP автоматически выполняет exact-head ack на `release:awaiting-agent` и продолжает polling;
- LOOP возвращает код `3` на `release:awaiting-ui`, чтобы Codex выполнил UI Flow;
- повторный запуск после acceptance ждёт `release:production`;
- `release:blocked`/`release:halted` возвращают код `2`;
- timeout возвращает `124`, `Ctrl-C` — `130`;
- `--timeout-seconds` и `--poll-seconds` задают bounded polling; polling не содержит AI-цикла.

## Failures И Idempotency

- invalid/missing task class или scope — `release:blocked` до merge;
- semantic/update conflict, failed baseline, missing production secret или SSH preflight failure — `release:blocked`;
- deploy/verify/UI-gate publication failure после merge — `release:halted`;
- любой существующий `release:halted` глобально блокирует выбор следующего PR;
- `release:awaiting-agent` блокирует всю очередь до exact ack;
- `release:awaiting-ui` допускает только exact-linked recovery;
- repeated label/push/dispatch events не выбирают PR без `release:ready` и не повторяют terminal merge/deploy;
- repeated ack проверяет тот же PR/head, а consumed/stale ack не может разрешить новый merge;
- repeated UI acceptance сохраняет terminal labels и лишь безопасно пере-dispatch-ит serialized worker.

## Baseline И Security Boundary

`baseline-ci.yml` выполняет `compileall`, `git diff --check` и `apps/github_release_train_smoke.py`. Task owner дополнительно выполняет применимые targeted checks и перечисляет их в PR.

`pull_request_target` и `issue_comment` всегда checkout-ят trusted `main`; PR code до merge не исполняется этим trigger. LOOP commands проходят exact parsing и association checks. Production SSH material доступен только job с GitHub Environment `production`; required secrets остаются `WB_CORE_DEPLOY_SSH_KEY` и `WB_CORE_DEPLOY_KNOWN_HOSTS`. Live deploy выполняется только canonical repo-owned runner из clean exact merge SHA. Release Train не выполняет WB writes, backfill или production business mutation.

## Bounded LOOP Canary

Первый canary после rollout должен быть отдельным business-no-op live/runtime PR без production data mutation. После targeted checks, semantic review и docs sync безопасный запуск выполняется так:

```bash
gh pr edit <CANARY_PR> --add-label task:loop --add-label scope:live-runtime --add-label release:ready && \
python3 apps/github_release_train_wait.py <CANARY_PR>
```

Команда остановится кодом `3` только после verified deploy и `release:awaiting-ui`; затем Codex выполняет заранее заданный UI Flow. Canary закрывается exact `accept-ui` command только при фактическом UI success. В рамках repo-only изменения Release Train сам canary не запускается.
