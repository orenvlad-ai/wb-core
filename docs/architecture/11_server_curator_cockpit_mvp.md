# Server Curator Cockpit MVP

## Summary

Этот документ фиксирует первый authoritative architecture contract для будущего server-side curator cockpit / autonomous sprint orchestrator.

Новый контур проектируется как параллельный experimental/night-run contour. Он не заменяет текущий ChatGPT Project workflow, не становится source of truth и не получает права выполнять unfrozen discussion как задачу.

Current MVP-0 checkpoint materializes only a repo-only contract skeleton: data contracts, deterministic validation, bounded Codex prompt generation and local smoke. MVP-0.1 adds local contract tooling on top of that skeleton: JSON example task spec, validate, freeze and generate-prompt CLI flow, plus CLI smoke. MVP-0.2 adds a local-only internal cockpit prototype: stdlib server, simple HTML page, JSON state dir, discussion/task-spec/freeze/prompt flow and server smoke. MVP-0.3 adds a repo-only execution loop prototype: runner CLI, fake executor, isolated worktree/run artifacts, deterministic verifier and runner smoke. MVP-0.4 wires the local cockpit to that fake-run loop: prepare fake run, run fake executor, view prompt/handoff/verifier result and cleanup owned run worktree. В этом checkpoint нет production runtime service, production API endpoints, OpenAI API integration, real Codex execution through UI or smoke, deploy или live/public mutation.

## Current Norm

До отдельного cutover canonical рабочий контур остаётся таким:

`operator/user -> ChatGPT Project curator -> manual Codex prompt -> Codex execution -> handoff -> user/curator decision`

Текущие governance/source-of-truth нормы продолжают действовать:
- authoritative docs/code остаются в `README.md`, `docs/architecture/*`, `docs/modules/*`, `migration/*` и repo code;
- `wb_core_docs_master/` остаётся derived secondary retrieval pack;
- ordinary task-flow не обновляет `wb_core_docs_master/**` и manifest;
- live/public/runtime closure регулируется hosted runtime deploy contract, а не новым cockpit.

## Transitional Dual-Contour Strategy

Переходная стратегия:
- днём работа продолжается через текущий ChatGPT Project contour;
- новый server curator cockpit используется только как experimental/night-run contour или на безопасных bounded задачах;
- forced cutover запрещён;
- новый контур не заменяет source-of-truth policy;
- результаты нового контура возвращаются в обычный review/handoff flow;
- cutover возможен только после отдельного explicit decision и серии успешных repo-only runs;
- до cutover текущий ChatGPT Project pack/governance продолжает существовать;
- retrieval/indexer нового контура может читать repo docs, task ledger, handoff ledger и project pack, но не отменяет canonical repo docs.

## Target Contour

Целевой будущий контур:

`operator -> cockpit/intake chat -> curator service -> frozen task spec -> sprint plan -> Codex worker -> verifier -> nightly report / blocker / human gate`

Новый сервис не является свободным AI-агентом. Это deterministic orchestration service со встроенным LLM-curator. LLM помогает формулировать и объяснять, но process ownership принадлежит state machine, policy engine, verifier, task ledger и immutable audit/state snapshots.

## Control-Plane Isolation

Curator cockpit is a development/control-plane surface, not product UI.

Isolation rules:
- MVP remains local-only/internal-only;
- cockpit must not be published under `api.selleros.pro`, `selleros.pro`, `/sheet-vitrina-v1/vitrina` or `/sheet-vitrina-v1/operator`;
- future hosted cockpit requires separate host/domain/access layer, auth, state and secrets;
- SellerOS/product-plane outage must not make the development control center unavailable;
- no production SellerOS/operator tab is introduced before an explicit control-plane hosting decision.

## Role-Based Architecture

### Operator

Operator:
- задаёт intent;
- обсуждает задачу;
- редактирует task spec до freeze;
- принимает только human-only decisions: risk acceptance, login, GitHub approval, branch protection, manual UI check, explicit live/deploy gate.

### Curator Service

Curator Service:
- backend orchestration service;
- управляет discussion, task spec, sprint plan и run status;
- вызывает LLM Curator;
- ведёт state machine и audit snapshots;
- не пишет код сам и не владеет repo truth.

### LLM Curator

LLM Curator:
- ведёт intake discussion;
- превращает discussion в task spec;
- предлагает L1/L2/L3 class;
- предлагает sprint plan;
- генерирует bounded Codex prompt;
- интерпретирует handoff/blocker;
- кратко объясняет operator-у состояние.

LLM Curator не принимает irreversible process decisions и не обходит policy engine.

### Planner Role

Planner Role:
- в MVP может быть частью LLM Curator;
- позже может стать отдельным role/agent;
- отвечает за decomposition into bounded sprint steps;
- не запускает execution и не bypass-ит freeze.

### Codex Worker / Executor

Codex Worker / Executor:
- исполняет один bounded sprint step;
- работает в isolated git worktree;
- использует отдельную branch;
- работает только в разрешённом sandbox/container profile;
- пишет logs, artifacts и final handoff;
- не владеет process decision, merge decision или live/deploy decision.

### Reviewer Role

Reviewer Role:
- в MVP не обязателен;
- позже может стать read-only LLM reviewer;
- проверяет diff, scope, docs, tests и handoff на reasoning level;
- не пишет код и не исправляет результат.

### Verifier

Verifier:
- deterministic checker;
- не доверяет Codex handoff на слово;
- проверяет diff scope, forbidden paths, `git diff --check`, required smokes, handoff schema и policy evidence;
- выдаёт structured check_result.

### Policy Engine / Gatekeeper

Policy Engine / Gatekeeper:
- deterministic policy, не LLM;
- решает `allowed`, `blocked`, `human_gate_required`;
- запрещает live/deploy/secret/root actions без explicit gate;
- enforce-ит class L1/L2/L3 obligations, source-of-truth rules, forbidden paths и allowed actions.

### Task Ledger / Audit Layer

Task Ledger / Audit Layer хранит:
- discussion и discussion messages;
- frozen specs;
- sprint plans;
- runs;
- blockers;
- human decisions;
- handoffs;
- retrieved context snapshots;
- immutable state snapshots с hash/version.

## Multi-Agent Readiness

MVP не является multi-agent execution.

Правильная граница:
- сначала роли разделяются протоколом и data contracts;
- затем роли, где есть доказанная польза, могут быть вынесены в отдельные agents;
- Planner и Reviewer являются первыми кандидатами на выделение;
- Codex Worker остаётся executor-ом bounded step, а не owner-ом процесса;
- deterministic process ownership остаётся в Curator Service, Policy Engine, Verifier и Ledger.

## Workflow

### Discuss

Operator обсуждает intent с LLM Curator. Curator может читать project context через retrieval, но execution из сырого чата запрещён.

### Task Spec

Curator превращает discussion в structured spec:
- goal;
- scope;
- not_in_scope;
- class `L1/L2/L3`;
- risks;
- acceptance_criteria;
- required_smokes;
- allowed_paths;
- forbidden_paths;
- allowed_actions;
- forbidden_actions;
- human_gates.

### Freeze

Без freeze запуск запрещён.

Frozen spec получает immutable `version`, `hash`, `created_at`, `created_by` и ссылку на context_snapshot. Все дальнейшие runs привязаны к этой версии.

### Sprint Plan

Из frozen spec формируется список bounded steps. Broad task must be split. Каждый step должен быть small/reviewable и иметь acceptance, smokes и stop conditions.

### Run

Один sprint step = один Codex worker run. Первые MVP используют repo-only sandbox, separate worktree/branch, no live deploy, no SSH/root и no auto-merge by default.

### Verify

Verifier независимо проверяет результат. Handoff Codex является evidence input, но не итоговой truth.

### Human Gate

Если нужен человек, workflow останавливается. Возвращается exact blocker и один next manual step.

### Continue / Stop

Если verifier passed и policy allows, Curator Service может перейти к следующему sprint step. Если есть blocker, human gate или failure, workflow stops.

## UI Contract

Будущий operator UI состоит из пяти surfaces:

- `Discuss`: chat intake с куратором; execution из raw chat запрещён.
- `Task Spec`: structured карточка задачи; editable до freeze; immutable после freeze.
- `Sprint Plan`: bounded steps с class, goal, scope, acceptance, smokes и stop conditions.
- `Run`: status Codex worker-а, diff/log/handoff/checks/PR links.
- `Human Gates`: только реальные manual actions: login, GitHub approval, branch protection, risk decision, manual UI check. Один blocker = один следующий ручной шаг.

## Data Contracts

### discussion

Минимальные поля: `id`, `operator_id`, `status`, `created_at`, `updated_at`, `active_task_spec_id`.

### discussion_message

Минимальные поля: `id`, `discussion_id`, `role`, `content`, `created_at`, `source_refs`, `redaction_status`.

### context_snapshot

Минимальные поля: `id`, `created_at`, `retrieval_query`, `source_refs`, `source_hashes`, `repo_revision`, `pack_revision`, `notes`.

### task_spec

Минимальные fields for the repo-only MVP-0 skeleton: `id`, `version`, `status`, `title`, `goal`, `scope`, `not_in_scope`, `task_class`, `class_reason`, `risks`, `acceptance_criteria`, `required_smokes`, `allowed_paths`, `forbidden_paths`, `allowed_actions`, `forbidden_actions`, `human_gates`, `frozen_at`, `spec_hash`, `explicit_policy_note`.

### sprint_plan

Минимальные поля: `id`, `task_spec_id`, `version`, `hash`, `status`, `steps`, `created_at`.

### sprint_step

Минимальные fields for the repo-only MVP-0 skeleton: `id`, `sequence`, `title`, `goal`, `task_class`, `scope`, `acceptance_criteria`, `required_smokes`, `stop_conditions`.

### run

Минимальные поля: `id`, `sprint_step_id`, `worktree`, `branch`, `sandbox_profile`, `status`, `started_at`, `finished_at`, `logs_ref`, `artifacts_ref`, `handoff_id`.

MVP-0.3 repo-only runner materializes local `RunRequest` and `RunResult` contracts with `executor_mode`, `repo_root`, `state_dir`, `base_ref`, `branch_name`, `prompt_path`, `handoff_path`, `log_path`, `changed_files`, `check_results`, `blocker_reason` and `next_manual_step`.

### check_result

Минимальные поля: `id`, `run_id`, `checker`, `status`, `summary`, `evidence_refs`, `failed_rules`, `created_at`.

### blocker

Минимальные поля: `id`, `run_id`, `kind`, `reason`, `exact_next_manual_step`, `owner`, `created_at`, `resolved_at`.

### decision

Минимальные поля: `id`, `subject_id`, `decision_type`, `chosen_value`, `rationale`, `operator_id`, `created_at`.

### handoff

Минимальные поля: `id`, `run_id`, `status`, `changed_files`, `checks_run`, `repo_state`, `live_deploy_state`, `public_verify_result`, `sheet_verify_result`, `upload_ready_source_state`, `commit_hash`, `push`, `pr`, `pr_url`, `manual_only_remainder`.

## Policy Compiler / Gates

Policy compiler turns frozen task spec + sprint step into deterministic execution policy.

Required gates:
- `allowed_paths`: paths Codex may edit;
- `forbidden_paths`: paths that fail verification if changed;
- `allowed_actions`: shell/git/GitHub/runtime actions allowed for this step;
- `forbidden_actions`: actions blocked regardless of prompt text;
- `network_policy`: default repo-only for MVP-1; external calls require explicit policy;
- `git_policy`: branch/worktree required; auto-merge disabled by default in MVP-1;
- `live_deploy_policy`: no live deploy in MVP-0/MVP-1; future live lane requires explicit gate and hosted runtime contract;
- `derived_sync_policy`: `wb_core_docs_master/**` and manifest blocked unless task is explicit derived-sync flow;
- `human_gates`: L3/live/secret/root/risk actions require explicit decision.

Global safety defaults:
- no unrestricted shell/root;
- no production runtime mutation;
- no SSH/root in MVP-0/MVP-1;
- no live deploy in MVP-0/MVP-1;
- no old selleros write path;
- no Google Sheets/GAS legacy active completion path;
- no secrets in prompts/logs/handoffs;
- no execution from unfrozen discussion.

## Verifier Contract

Verifier must run after each Codex worker run and before continue/stop decision.

Minimum deterministic checks:
- changed files are within allowed_paths;
- no changes under forbidden_paths;
- no changes under `wb_core_docs_master/**` or `99_MANIFEST__DOCSET_VERSION.md` unless explicit derived-sync policy allows it;
- no runtime/code changes for docs-only tasks;
- `git diff --check` passes;
- required smokes/checks from sprint_step ran or blocker names exact reason;
- handoff includes required fields from Codex execution protocol;
- no secrets are present in prompts, logs or handoff artifacts;
- live/deploy/public operations did not run unless policy allowed them.

## Source Of Truth And Retrieval Rules

Authoritative repo docs/code remain canonical:
- `README.md`;
- `docs/architecture/*`;
- `docs/modules/*`;
- `migration/*`;
- repo code and versioned contracts.

`wb_core_docs_master/` is derived secondary project pack. Retrieval, vector index, task ledger and handoff ledger are evidence/cache/state, not canonical truth.

Discrepancy between current ChatGPT Project contour and new contour is resolved by authoritative repo docs plus curator/human review.

New retrieval/indexer may read repo docs, task ledger, handoff ledger and project pack, but it must not redefine source-of-truth policy or make derived pack a completion blocker for ordinary task-flow.

## Completion Semantics

The cockpit must report exact state:
- `discussion_only`: only intake discussion exists; no execution allowed.
- `frozen_spec`: immutable task_spec exists; no sprint step has run.
- `sprint_planned`: bounded sprint_plan exists.
- `run_prepared`: Codex prompt/policy/worktree plan prepared, worker not complete.
- `repo_prepared`: repo files changed and local repo checks completed or blocker recorded.
- `verifier_passed`: deterministic verifier passed for the run.
- `pr_ready`: GitHub PR is ready or exact GitHub blocker is recorded.
- `blocked`: workflow stopped on deterministic failure.
- `human_gate_required`: workflow stopped on one manual decision/action.
- `live_complete`: future/gated only; never default in MVP-0/MVP-1.

For the ADR-only step MVP-0 target completion was `repo-prepared / docs-only`. The original MVP-0 skeleton completion was `repo-prepared / repo-only`: contract code and local smoke existed without execution runner, runtime service, UI, API endpoints or live/deploy lanes. MVP-0.3 remains repo-only: local runner artifacts and fake execution are allowed, while production runtime, OpenAI-backed execution, auto-merge and live/public/deploy lanes remain out of scope.

## MVP Boundaries

### MVP-0

- repo-only contract skeleton only;
- data contracts for frozen task spec, sprint plan and sprint step;
- deterministic policy validation;
- bounded Codex prompt builder;
- targeted local smoke;
- no runtime/production code;
- no execution;
- no Codex runner;
- no UI implementation;
- no API endpoints;
- no live deploy;
- no Telegram as UI;
- no vector DB requirement.

### MVP-0.1 Contract Tooling

- local CLI only: `validate-task-spec`, `freeze-task-spec`, `generate-codex-prompt`;
- checked-in example draft task spec under `artifacts/curator_cockpit_mvp/input/`;
- deterministic freeze writes `frozen_at` and stable `spec_hash`;
- prompt generation remains forbidden for draft specs;
- CLI smoke covers validate/freeze/prompt flow;
- still no backend service, UI, OpenAI integration, Codex execution runner, live deploy or public route mutation.

### MVP-0.2 Local Cockpit Prototype

- local-only stdlib server entrypoint under `apps/`;
- default bind is `127.0.0.1`;
- simple HTML cockpit covers Discuss, Task Spec, Sprint Plan, Prompt and Human Gates surfaces;
- local JSON state dir stores discussions, messages, task specs and prompt artifacts;
- server uses existing contract helpers for validation, freeze and prompt generation;
- server smoke covers root HTML, state API, discussion/message flow, draft rejection, freeze and prompt generation;
- no OpenAI API integration yet;
- no Codex runner yet;
- no live/public/deploy contour or production route wiring;
- current ChatGPT Project workflow remains active and canonical until explicit cutover.

### MVP-0.3 Repo-Only Execution Loop

- local runner CLI only: `prepare-run`, `run-step`, `verify-run`, `cleanup-run`;
- fake executor writes deterministic handoff with mandatory curator and compact-check blocks;
- run artifacts live under local `state_dir`, with prompt, handoff, log, metadata and verifier output;
- `run-step` creates an isolated git worktree/branch under local state before executor activity;
- command executor exists only behind explicit `--allow-real-executor`, `--executor-command` and `repo_only_executor` policy;
- deterministic verifier checks frozen spec, prompt, handoff blocks, forbidden path hits and `git diff --check`;
- runner smoke uses fake executor only and does not require Codex CLI/auth;
- no OpenAI API integration, no real Codex execution in smoke, no auto-merge, no live/public/deploy contour;
- no local cockpit fake-run endpoint is introduced in this checkpoint;
- current ChatGPT Project workflow remains active and canonical until explicit cutover.

### MVP-0.4 Local Cockpit Fake-Run Integration

- local cockpit exposes fake-run-only endpoints for `prepare-run`, `run-fake`, `verify` and `cleanup`;
- UI can go from frozen task spec to prompt, prepared run, fake executor handoff and verifier result;
- run prompt, handoff, run metadata and verifier status are visible in the local cockpit;
- cleanup is limited to the worktree/branch owned by that runner artifact;
- still no real Codex execution through UI;
- still no command executor through UI;
- still no OpenAI API integration;
- still no live/public/deploy contour;
- still no SellerOS product-plane route or operator tab;
- current ChatGPT Project workflow remains active and canonical until explicit cutover.

### MVP-1

- repo-only task spec/prompt schema;
- one Codex worker;
- deterministic verifier;
- no live deploy;
- no SSH/root;
- no auto-merge by default;
- no multi-agent execution;
- reviewer role may remain conceptual.

### MVP-2

- optional read-only reviewer role/agent;
- PR/checks/handoff hardening;
- human gates screen;
- notification layer;
- still no default auto-live-deploy.

### MVP-3

- gated live/runtime lane only after stable repo-only runs;
- explicit policy for live/deploy;
- public probe integration;
- exact blocker on missing SSH/env/target.

## Known Gaps

- Persistent storage technology for task ledger is not selected.
- Exact schema serialization format is not selected.
- Codex worker isolation/container profile is not specified beyond contract-level requirements.
- GitHub closure behavior must reuse existing Codex execution protocol and may need dedicated cockpit UX.
- Retrieval freshness and snapshot retention policies are not fixed.
- Reviewer role value must be proven before extraction into a separate agent.

## Not In Scope

This document does not implement:
- production runtime service;
- production cockpit UI;
- production API endpoints;
- OpenAI-backed Codex execution runner;
- vector DB;
- multi-agent execution;
- live deploy lane;
- auto-merge;
- secret management;
- Telegram UI;
- replacement of current ChatGPT Project workflow;
- update of `wb_core_docs_master/**` or `99_MANIFEST__DOCSET_VERSION.md`.

## Blockers / Future Decisions

Future decisions before implementation:
- choose task ledger storage and immutable snapshot format;
- define exact JSON schema for task_spec, sprint_plan, run and handoff;
- define sandbox/container profiles for Codex worker;
- define policy compiler representation;
- define verifier command set and evidence storage;
- decide when planner/reviewer become separate agents;
- decide cutover criteria after successful repo-only runs;
- define gated live/runtime lane only after MVP-1/MVP-2 evidence.
