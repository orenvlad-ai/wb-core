"""Targeted smoke-check for the repo-only curator cockpit MVP skeleton."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.curator_cockpit_mvp import (  # noqa: E402
    DEFAULT_FORBIDDEN_ACTIONS,
    DEFAULT_FORBIDDEN_PATHS,
    CuratorCockpitValidationError,
    SprintPlan,
    SprintStep,
    TaskSpec,
    build_codex_prompt,
    freeze_task_spec,
    validate_sprint_plan,
    validate_task_spec,
)


def main() -> None:
    draft_spec = _build_task_spec(status="draft")
    step = _build_sprint_step()

    _expect_validation_error(lambda: build_codex_prompt(draft_spec, step), "draft spec must not generate prompt")

    frozen_spec = freeze_task_spec(draft_spec, frozen_at="2026-05-01T00:00:00Z")
    prompt = build_codex_prompt(frozen_spec, step)

    for token in ("Класс задачи:", "Причина классификации:", "Режим выполнения:"):
        if token not in prompt:
            raise AssertionError(f"prompt missing classification header token: {token}")
    for token in ("=== ДЛЯ КУРАТОРА ===", "=== СЖАТАЯ ПРОВЕРКА ==="):
        if token not in prompt:
            raise AssertionError(f"prompt missing final block: {token}")

    for path in DEFAULT_FORBIDDEN_PATHS:
        if path not in frozen_spec.forbidden_paths:
            raise AssertionError(f"default forbidden path missing from spec: {path}")
        if path not in prompt:
            raise AssertionError(f"default forbidden path missing from prompt: {path}")

    for action in ("live", "deploy", "SSH", "root"):
        if action not in DEFAULT_FORBIDDEN_ACTIONS:
            raise AssertionError(f"default forbidden action constant missing: {action}")
        if action not in frozen_spec.forbidden_actions:
            raise AssertionError(f"default forbidden action missing from spec: {action}")
        if action not in prompt:
            raise AssertionError(f"default forbidden action missing from prompt: {action}")

    l3_without_gate = _build_task_spec(status="frozen", task_class="L3", human_gates=(), explicit_policy_note=None)
    _expect_validation_error(lambda: validate_task_spec(l3_without_gate, require_frozen=True), "L3 without gate rejected")

    l3_with_policy_note = _build_task_spec(
        status="frozen",
        task_class="L3",
        human_gates=(),
        explicit_policy_note="Explicit repo-only policy exception: no live/deploy lane in this step.",
    )
    validate_task_spec(l3_with_policy_note, require_frozen=True)

    plan = SprintPlan(
        id="plan-curator-cockpit-mvp",
        task_spec_id=frozen_spec.id,
        status="planned",
        steps=(step,),
    )
    validate_sprint_plan(plan, frozen_spec)
    if step.scope != ("packages/application/curator_cockpit_mvp.py", "apps/curator_cockpit_mvp_smoke.py"):
        raise AssertionError(f"sprint step scope drifted: {step.scope}")
    if step.required_smokes != ("python3 apps/curator_cockpit_mvp_smoke.py",):
        raise AssertionError(f"sprint step required smokes drifted: {step.required_smokes}")

    print("curator-cockpit-mvp-smoke passed")


def _build_task_spec(
    *,
    status: str,
    task_class: str = "L2",
    human_gates: tuple[str, ...] = (),
    explicit_policy_note: str | None = None,
) -> TaskSpec:
    return TaskSpec(
        id="task-curator-cockpit-mvp",
        version="v1",
        status=status,
        title="Curator cockpit MVP skeleton",
        goal="Materialize a repo-only contract skeleton for frozen specs, sprint plans and Codex prompts.",
        scope=(
            "packages/application/curator_cockpit_mvp.py",
            "apps/curator_cockpit_mvp_smoke.py",
        ),
        not_in_scope=(
            "runtime service",
            "API endpoints",
            "UI implementation",
            "Codex worker execution",
            "live deploy",
        ),
        task_class=task_class,
        class_reason="Bounded repo-only implementation skeleton with smoke coverage; no live/runtime contour change.",
        risks=("Prompt contract drift could bypass governance footer.",),
        acceptance_criteria=(
            "draft task spec cannot generate Codex prompt",
            "frozen task spec can generate Codex prompt",
            "prompt contains classification header and curator footer blocks",
        ),
        required_smokes=("python3 apps/curator_cockpit_mvp_smoke.py",),
        allowed_paths=(
            "packages/application/curator_cockpit_mvp.py",
            "apps/curator_cockpit_mvp_smoke.py",
            "docs/architecture/11_server_curator_cockpit_mvp.md",
        ),
        forbidden_paths=(),
        allowed_actions=("repo_edit", "local_smoke", "git_diff_check"),
        forbidden_actions=(),
        human_gates=human_gates,
        frozen_at="2026-05-01T00:00:00Z" if status == "frozen" else None,
        explicit_policy_note=explicit_policy_note,
    )


def _build_sprint_step() -> SprintStep:
    return SprintStep(
        id="step-curator-cockpit-mvp-contract",
        sequence=1,
        title="Create repo-only curator cockpit contract skeleton",
        goal="Add frozen task spec, sprint plan validation and prompt builder without execution.",
        task_class="L2",
        scope=(
            "packages/application/curator_cockpit_mvp.py",
            "apps/curator_cockpit_mvp_smoke.py",
        ),
        acceptance_criteria=(
            "TaskSpec validation enforces frozen prompt generation",
            "Codex prompt contains mandatory governance sections",
            "MVP default forbidden paths/actions are enforced",
        ),
        required_smokes=("python3 apps/curator_cockpit_mvp_smoke.py",),
        stop_conditions=(
            "runtime/deploy/API/UI work is required",
            "forbidden paths need modification",
        ),
    )


def _expect_validation_error(callback, label: str) -> None:
    try:
        callback()
    except CuratorCockpitValidationError:
        return
    raise AssertionError(label)


if __name__ == "__main__":
    main()
