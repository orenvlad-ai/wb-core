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
- отсутствие `release:blocked`, `release:halted` и `release:superseded`.

Основные state labels:

- `release:ready` — task owner закончил pre-release proof и явно поставил PR в очередь;
- `release:running` — worker выполняет sync/baseline/release;
- `release:awaiting-agent` — LOOP прошёл sync/baseline и ждёт exact-head acknowledgement активной Codex-сессии;
- `release:needs-resume` — non-exclusive overlay на `release:awaiting-agent`: сохранённого владельца нужно возобновить, но gate остаётся fail-closed;
- `release:awaiting-ui` — LOOP merge задеплоен и ждёт production UI Flow/acceptance;
- `release:blocked` — PR-specific failure до merge;
- `release:done` — terminal success STANDARD `repo-only` без deploy;
- `release:production` — terminal success STANDARD live/runtime или принятой LOOP-цепочки;
- `release:halted` — failure после merge; вся очередь остановлена.
- `release:superseded` — terminal audit state незамёрженной LOOP-итерации, однозначно заменённой завершённой production recovery-chain; root/task/scope/history сохраняются, активные queue/failure labels снимаются.

Промежуточные `release:ready`, `release:running`, `release:awaiting-agent`, `release:needs-resume` и `release:awaiting-ui` не являются closure. `release:superseded` не является success исходной итерации, но исключает доказанно заменённый PR из активной очереди.

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

Чужой exclusive gate означает только waiting. Ни количество одинаковых polls/goal-turns, ни длительность не переводят его в `release:blocked` и не разрешают снимать, обходить или перехватывать gate. Task owner продолжает waiter до своей очереди; при исчерпании текущего goal-turn создаётся следующий goal на продолжение ожидания, а не terminal handoff открытого PR.

Workflow запускает queue observation каждые пять минут. Если `release:awaiting-agent` остаётся без ack дольше `WB_CORE_RELEASE_NEEDS_RESUME_AFTER_MINUTES` (default `30`), worker идемпотентно добавляет `release:needs-resume` и ровно один marker-comment для актуальной пары PR/exact head. Comment фиксирует, что ack/skip не выполнялись, и даёт команды `codex resume` и `python3 apps/github_release_train_wait.py <PR>`. Следующая задача не выбирается. Exact-head ack возобновлённого владельца автоматически снимает `release:needs-resume`; любое дальнейшее release transition также очищает overlay. Изменившийся head требует нового exact marker/ack и не наследует старое разрешение.

## Exclusive Production UI Gate

После успешного LOOP merge, canonical deploy и production verify worker не ставит terminal success и не dispatch-ит следующий release. Он создаёт deterministic chain label `loop:root-<ROOT_PR>`, ставит текущей итерации `release:awaiting-ui` и завершает job. Push-triggered или повторный queue run видит gate и не выбирает несвязанный PR.

Если production UI Flow не принят, исчезновение Codex не открывает очередь: `release:awaiting-ui` остаётся durable fail-closed state.

UI Flow следует production UI contract из [`07_codex_execution_protocol.md`](07_codex_execution_protocol.md). HTTP `200`, `curl`, наличие HTML или только canonical public probe недостаточны: требуется фактический browser render с DOM/final URL, отсутствием `5xx`/`pageerror`/fatal surface, классификацией существенных console errors и визуально проверенным screenshot. В Codex CLI сразу используется Playwright с новым изолированным Chrome/Chromium context; встроенный Browser в CLI недоступен. В ChatGPT web/desktop встроенный Browser допустим, если доступен. Пользовательский profile/cookies/credentials и любые clicks/input/business mutations запрещены по умолчанию. Если Playwright/Chromium или необходимая авторизация недоступны, gate остаётся fail-closed.

При успешном UI Flow активная Codex-сессия оставляет точную GitHub-native command на текущей итерации:

```bash
gh pr comment <ACTIVE_LOOP_PR> --body "/wb-core loop accept-ui <ACTIVE_LOOP_PR>"
```

Handler проверяет write association, активный gate и deterministic chain. Он идемпотентно переводит все merged PR этой chain в `release:production`, нормализует доказанно заменённые unmerged predecessors и dispatch-ит следующий queue run. Повторная acceptance не меняет terminal state и не вызывает повторный merge/deploy.

Superseded proof механический и root-bounded: кандидат обязан иметь тот же exact `loop:root-<ROOT_PR>`, `task:loop + scope:live-runtime`, оставаться unmerged и иметь PR number меньше принятой production-итерации. Тогда handler ставит `release:superseded`, снимает `release:ready/running/awaiting-*/blocked/halted`, `release:needs-resume` и stale `loop:ack-*`, сохраняет root/task/scope и оставляет audit comment. PR другой root или более новая/неоднозначная запись не меняется. Автоматическое закрытие не выполняется: label normalization достаточно и сохраняет историю.

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

`apps/github_release_train_wait.py` получает номер PR, выводит только изменения `class/scope/state/head/queue/gate` и использует GitHub CLI auth/repository context, если env не задан.

- STANDARD ждёт `release:done` для `scope:repo-only` или `release:production` для `scope:live-runtime`;
- чужой exclusive gate выводится как normal `wait-foreign-gate`; waiter продолжает polling без terminal timeout и никогда не называет это blocked;
- LOOP заново читает actual head, автоматически выполняет exact-head ack только на собственном `release:awaiting-agent` и продолжает polling через merge/deploy;
- LOOP возвращает код `3` на `release:awaiting-ui`, чтобы Codex выполнил UI Flow;
- повторный запуск после acceptance ждёт `release:production`;
- собственный `release:blocked`, global `release:halted` и conflicting durable gates возвращают код `2` с точной причиной;
- `Ctrl-C` возвращает `130`;
- `--poll-seconds` задаёт bounded polling interval, `--status-seconds` и backward-compatible `--timeout-seconds` — только heartbeat; elapsed time не является terminal condition, polling не содержит AI-цикла.

## Failures И Idempotency

- invalid/missing task class или scope — `release:blocked` до merge;
- semantic/update conflict, failed baseline, missing production secret или SSH preflight failure — `release:blocked`;
- deploy/verify/UI-gate publication failure после merge — `release:halted`;
- любой существующий `release:halted` глобально блокирует выбор следующего PR;
- `release:awaiting-agent` блокирует всю очередь до exact ack; `release:needs-resume` только делает потерю владельца видимой и ничего не разрешает;
- `release:awaiting-ui` допускает только exact-linked recovery;
- успешно принятая recovery-chain переводит merged members в `release:production`, а только доказанные unmerged predecessors того же root — в `release:superseded`; другой root не мутируется;
- repeated label/push/dispatch events не выбирают PR без `release:ready` и не повторяют terminal merge/deploy;
- repeated ack проверяет тот же PR/head, а consumed/stale ack не может разрешить новый merge;
- repeated UI acceptance сохраняет terminal labels и лишь безопасно пере-dispatch-ит serialized worker.

## Канонический Мониторинг

[Основной мониторинг исполняемых/ожидающих PR](https://github.com/orenvlad-ai/wb-core/pulls?q=is%3Apr+is%3Aopen+-label%3Arelease%3Asuperseded+label%3A%22release%3Aready%2Crelease%3Arunning%2Crelease%3Aawaiting-agent%2Crelease%3Aneeds-resume%22) использует `is:open`, явно исключает `release:superseded` и через comma-OR label qualifier показывает только открытую активную очередь плюс отдельный resume overlay. Уже merged `release:awaiting-ui` gate не может входить в `is:open` view и наблюдается на текущем chain PR/workflow. PR-specific failures исследуются по их точной ссылке/comment и после доказанной замены не возвращаются в этот view.

## Baseline И Security Boundary

`baseline-ci.yml` выполняет `compileall`, `git diff --check` и `apps/github_release_train_smoke.py`. Task owner дополнительно выполняет применимые targeted checks и перечисляет их в PR.

`pull_request_target` и `issue_comment` всегда checkout-ят trusted `main`; PR code до merge не исполняется этим trigger. LOOP commands проходят exact parsing и association checks. Production SSH material доступен только job с GitHub Environment `production`; required secrets остаются `WB_CORE_DEPLOY_SSH_KEY` и `WB_CORE_DEPLOY_KNOWN_HOSTS`. Live deploy выполняется только canonical repo-owned runner из clean exact merge SHA. Release Train не выполняет WB writes, backfill или production business mutation.

## Проверенный LOOP Canary

[PR #616](https://github.com/orenvlad-ai/wb-core/pull/616) является проверенным reference flow новой LOOP-инфраструктуры: отдельный business-no-op documentation PR прошёл `task:loop + scope:live-runtime + release:ready`, exact-head acknowledgement, merge, canonical deploy, `release:awaiting-ui`, read-only CLI Playwright/Chrome verification, exact `accept-ui`, terminal `release:production` и post-accept empty-queue dispatch. GitHub PR, comments, labels и workflow runs остаются durable evidence; временный repository marker после этого доказательства не нужен.

Новые canary/LOOP задачи повторяют тот же контракт: waiter останавливается кодом `3` на `release:awaiting-ui`, Codex выполняет production UI verification и оставляет exact `accept-ui` только при фактическом UI success. HTTP-only evidence не открывает gate.
