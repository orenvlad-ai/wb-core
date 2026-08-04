# Canonical Codex Curator Workspace v1

## Назначение и source-of-truth boundary

`workspaces/WB Core · Кураторы/` — канонический local-project front door для новых C1 проекта `orenvlad-ai/wb-core`. Общий source of truth не переносится в Desktop project: им остаются current `origin/main:AGENTS.md`, authoritative docs и code/GitHub truth по обычному приоритету. Saved project хранит только primary cwd и применяет checked-in role-specific delta.

Instruction chain обязана быть `root AGENTS.md → workspaces/WB Core · Кураторы/AGENTS.override.md`. Nested override не повторяет execution contours, Release Train, Watcher или domain contracts и применим только к C1, запущенному с этим primary cwd. C2 всегда создаётся отдельной user-owned task в обычном `wb-core` project/worktree и потому не видит curator-only delta.

Machine contract: [`codex_curator_workspace_v1.json`](../../packages/contracts/codex_curator_workspace_v1.json). Checked-in validator и deterministic project-readback verifier: `python3 apps/codex_curator_workspace.py validate` и `python3 apps/codex_curator_workspace.py plan --repository <TRUSTED_MAIN_CHECKOUT> --source-ref origin/main`.

## Desktop discovery и model defaults

Codex Desktop primary folder определяет новый chat cwd, Git default и automatic discovery `AGENTS.md`/skills/`.codex/config.toml`; secondary folders доступны для файлов, но не становятся instruction sources. Поэтому rollout создаёт отдельный clean checkout `/Users/ovlmacbook/Projects/wb-core-curators` из trusted `origin/main`, а saved project получает exact label `WB Core · Кураторы` и primary path `workspaces/WB Core · Кураторы` внутри него. `list_projects` должен вернуть этот путь без нормализации к Git root, `hostId=local` и `isGitRepository=true`; mismatch fail closed.

Nested `.codex/config.toml` выбирает `gpt-5.6-sol`, `model_reasoning_effort=max` и bounded `project_doc_max_bytes=65536`. Последний параметр обязателен: фактический root `AGENTS.md` больше standard 32 KiB, поэтому без scoped лимита chain обрывается до role delta. Smoke проверяет, что суммарный размер root+nested guidance остаётся ниже лимита. Curator canary запускается в `local` environment сохранённого primary folder, а не в автоматически созданном implementation worktree. Project trust обязателен для project-scoped config; это обычная локальная настройка, а не новый credential или permission contour.

Checkout служит bootstrap, но C1 не предполагает, что его `HEAD` всегда свежий: каждый before-action preflight fetch-ит canonical origin и bounded-читает exact root и role sources из `origin/main`. Если поддержанный Desktop readback докажет нормализацию primary cwd к Git root, nested rollout не считается успешным. Тогда применяется небольшой standalone primary folder, который содержит тот же C1 delta и project config, а current `origin/main` checkout используется как implementation/source repository. Standalone folder не содержит копии общего протокола; before-action preflight всё равно перечитывает exact repository origin и `origin/main:AGENTS.md`. Конкретный fallback создаётся только после такого evidence, без удаления nested artifacts и legacy state.

## Deterministic C1 lifecycle

1. C1 обсуждает/проектирует и выполняет curator preflight.
2. Обычная команда запуска классифицируется как `DISPATCH_REQUEST`.
3. C1 выполняет единую launch operation из root contract: exact C2 create/readback, title/pin, registry registration/envelope и readback единственного active Watcher.
4. C1 публикует один короткий dispatch summary и завершает turn.
5. Steady state C1 — idle без model turn. Он не вызывает `wait_threads`/`read_thread`, не опрашивает GitHub и не создаёт heartbeat.
6. C1 просыпается только от нового пользовательского сообщения либо exact Watcher attention. После одного bounded verify/ack/handoff action снова завершает turn.

Watcher identity всегда читается из active registry generation; никакой thread/generation не hardcode-ится. Owner acceptance не выполняют ни C1 canary, ни C2, ни Watcher: exact curator только подготавливает handoff, а владелец отвечает `Задача принята`.

## Natural canary и closure evidence

Canary создаётся как новая локальная C1 в saved project без служебного prompt footer. Пользовательский текст короткий и естественный:

`Запусти отдельную короткую диагностику: проверь, что новый кураторский контур следует актуальному протоколу и не создаёт второй Watcher.`

Успех требует exact project/path readback, фактического root+nested instruction discovery, отдельного read-only C2, `TARGET_CREATE_READBACK`, title/pin/registry evidence, одного dispatch summary и завершённого C1 turn. Следующий global heartbeat обязан увидеть canary C2 как отдельную зарегистрированную задачу; C1 не poll-ит его. Уже работающие C1/C2/Watcher и automations не меняются, не дублируются и не архивируются.

## Миграция с `wb_core_3`

Новые задачи могут переходить на `WB Core · Кураторы` только после successful saved-project и natural-canary readbacks. `wb_core_3` остаётся доступным, пока выполняется хотя бы одно условие:

- существует active pre-migration C1/C2 task;
- initiating curator этой миграции ещё не получила terminal attention и owner acceptance;
- registry integrity или single-active-Watcher evidence не подтверждены;
- новый front door не прошёл естественный canary.

Архивация `wb_core_3` не входит в rollout v1 и не выполняется автоматически. После исчезновения всех условий она может быть отдельным reversible Desktop action с новым readback; история чатов не удаляется и не становится state.
