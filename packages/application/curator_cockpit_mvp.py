"""Repo-only contract skeleton for the server curator cockpit MVP."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal, Sequence

TaskSpecStatus = Literal["draft", "frozen"]
TaskClass = Literal["L1", "L2", "L3"]
SprintPlanStatus = Literal["draft", "planned"]

TASK_SPEC_STATUSES = {"draft", "frozen"}
TASK_CLASSES = {"L1", "L2", "L3"}
SPRINT_PLAN_STATUSES = {"draft", "planned"}

DEFAULT_FORBIDDEN_PATHS = (
    "wb_core_docs_master/**",
    "99_MANIFEST__DOCSET_VERSION.md",
)
DEFAULT_FORBIDDEN_ACTIONS = (
    "live",
    "deploy",
    "SSH",
    "root",
    "production_runtime_mutation",
    "execution_from_discussion",
    "codex_worker_run",
    "api_endpoints",
    "ui_implementation",
)
DEFAULT_EXECUTION_MODE = "repo-only, no live/deploy, no API endpoints, no UI, no Codex worker run"


class CuratorCockpitValidationError(ValueError):
    """Raised when a curator cockpit contract violates deterministic policy."""


@dataclass(frozen=True)
class TaskSpec:
    id: str
    version: str
    status: TaskSpecStatus
    title: str
    goal: str
    scope: Sequence[str]
    not_in_scope: Sequence[str]
    task_class: TaskClass
    class_reason: str
    risks: Sequence[str]
    acceptance_criteria: Sequence[str]
    required_smokes: Sequence[str]
    allowed_paths: Sequence[str]
    forbidden_paths: Sequence[str] = field(default_factory=lambda: DEFAULT_FORBIDDEN_PATHS)
    allowed_actions: Sequence[str] = field(default_factory=tuple)
    forbidden_actions: Sequence[str] = field(default_factory=lambda: DEFAULT_FORBIDDEN_ACTIONS)
    human_gates: Sequence[str] = field(default_factory=tuple)
    frozen_at: str | None = None
    spec_hash: str | None = None
    explicit_policy_note: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _to_tuple(self.scope))
        object.__setattr__(self, "not_in_scope", _to_tuple(self.not_in_scope))
        object.__setattr__(self, "risks", _to_tuple(self.risks))
        object.__setattr__(self, "acceptance_criteria", _to_tuple(self.acceptance_criteria))
        object.__setattr__(self, "required_smokes", _to_tuple(self.required_smokes))
        object.__setattr__(self, "allowed_paths", _to_tuple(self.allowed_paths))
        object.__setattr__(self, "allowed_actions", _to_tuple(self.allowed_actions))
        object.__setattr__(self, "human_gates", _to_tuple(self.human_gates))
        object.__setattr__(
            self,
            "forbidden_paths",
            _merge_defaults(self.forbidden_paths, DEFAULT_FORBIDDEN_PATHS),
        )
        object.__setattr__(
            self,
            "forbidden_actions",
            _merge_defaults(self.forbidden_actions, DEFAULT_FORBIDDEN_ACTIONS),
        )


@dataclass(frozen=True)
class SprintStep:
    id: str
    sequence: int
    title: str
    goal: str
    task_class: TaskClass
    scope: Sequence[str]
    acceptance_criteria: Sequence[str]
    required_smokes: Sequence[str]
    stop_conditions: Sequence[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _to_tuple(self.scope))
        object.__setattr__(self, "acceptance_criteria", _to_tuple(self.acceptance_criteria))
        object.__setattr__(self, "required_smokes", _to_tuple(self.required_smokes))
        object.__setattr__(self, "stop_conditions", _to_tuple(self.stop_conditions))


@dataclass(frozen=True)
class SprintPlan:
    id: str
    task_spec_id: str
    status: SprintPlanStatus
    steps: Sequence[SprintStep]

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))


def freeze_task_spec(task_spec: TaskSpec, frozen_at: str | None = None) -> TaskSpec:
    validate_task_spec(task_spec, require_frozen=False)
    frozen_timestamp = frozen_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    frozen = replace(task_spec, status="frozen", frozen_at=frozen_timestamp, spec_hash=None)
    return replace(frozen, spec_hash=_compute_task_spec_hash(frozen))


def validate_task_spec(task_spec: TaskSpec, require_frozen: bool = False) -> None:
    _require_non_empty("id", task_spec.id)
    _require_non_empty("version", task_spec.version)
    _require_status("status", task_spec.status, TASK_SPEC_STATUSES)
    _require_non_empty("title", task_spec.title)
    _require_non_empty("goal", task_spec.goal)
    _require_non_empty_sequence("scope", task_spec.scope)
    _require_non_empty_sequence("acceptance_criteria", task_spec.acceptance_criteria)
    _require_task_class(task_spec.task_class)
    _require_non_empty("class_reason", task_spec.class_reason)
    _require_non_empty_sequence("allowed_paths", task_spec.allowed_paths)
    _require_non_empty_sequence("forbidden_paths", task_spec.forbidden_paths)
    _require_required_defaults("forbidden_paths", task_spec.forbidden_paths, DEFAULT_FORBIDDEN_PATHS)
    _require_required_defaults("forbidden_actions", task_spec.forbidden_actions, DEFAULT_FORBIDDEN_ACTIONS)
    _reject_action_overlap(task_spec.allowed_actions, task_spec.forbidden_actions)
    _reject_path_overlap(task_spec.allowed_paths, task_spec.forbidden_paths)

    if require_frozen and task_spec.status != "frozen":
        raise CuratorCockpitValidationError("run/prompt generation requires a frozen task spec")
    if task_spec.task_class == "L3" and not task_spec.human_gates and not _is_present(task_spec.explicit_policy_note):
        raise CuratorCockpitValidationError("L3 task spec requires a human gate or explicit policy note")
    if "execution_from_discussion" not in task_spec.forbidden_actions:
        raise CuratorCockpitValidationError("execution_from_discussion must stay forbidden")


def validate_sprint_step(step: SprintStep) -> None:
    _require_non_empty("sprint_step.id", step.id)
    if step.sequence < 1:
        raise CuratorCockpitValidationError("sprint_step.sequence must be >= 1")
    _require_non_empty("sprint_step.title", step.title)
    _require_non_empty("sprint_step.goal", step.goal)
    _require_task_class(step.task_class)
    _require_non_empty_sequence("sprint_step.scope", step.scope)
    _require_non_empty_sequence("sprint_step.acceptance_criteria", step.acceptance_criteria)
    _require_non_empty_sequence("sprint_step.required_smokes", step.required_smokes)
    _require_non_empty_sequence("sprint_step.stop_conditions", step.stop_conditions)


def validate_sprint_plan(plan: SprintPlan, task_spec: TaskSpec) -> None:
    _require_non_empty("sprint_plan.id", plan.id)
    _require_non_empty("sprint_plan.task_spec_id", plan.task_spec_id)
    _require_status("sprint_plan.status", plan.status, SPRINT_PLAN_STATUSES)
    if plan.task_spec_id != task_spec.id:
        raise CuratorCockpitValidationError("sprint_plan.task_spec_id must match task_spec.id")
    if not plan.steps:
        raise CuratorCockpitValidationError("sprint_plan.steps must not be empty")

    seen_sequences: set[int] = set()
    for step in plan.steps:
        validate_sprint_step(step)
        if step.sequence in seen_sequences:
            raise CuratorCockpitValidationError(f"duplicate sprint_step.sequence: {step.sequence}")
        seen_sequences.add(step.sequence)


def build_codex_prompt(
    task_spec: TaskSpec,
    sprint_step: SprintStep,
    execution_mode: str = DEFAULT_EXECUTION_MODE,
) -> str:
    validate_task_spec(task_spec, require_frozen=True)
    validate_sprint_step(sprint_step)

    lines = [
        f"Класс задачи: {sprint_step.task_class}",
        f"Причина классификации: {task_spec.class_reason}",
        f"Режим выполнения: {execution_mode}",
        "",
        "# Task",
        f"Task spec: {task_spec.id}@{task_spec.version}",
        f"Sprint step: {sprint_step.sequence}. {sprint_step.title}",
        f"Spec hash: {task_spec.spec_hash or 'not provided'}",
        "",
        "## Goal",
        task_spec.goal,
        "",
        "## Step Goal",
        sprint_step.goal,
        "",
        "## Scope",
        *_format_items(task_spec.scope),
        "",
        "## Step Scope",
        *_format_items(sprint_step.scope),
        "",
        "## Not In Scope",
        *_format_items(task_spec.not_in_scope or ("not specified",)),
        "",
        "## Acceptance Criteria",
        *_format_items(sprint_step.acceptance_criteria),
        "",
        "## Required Smokes / Checks",
        *_format_items((*task_spec.required_smokes, *sprint_step.required_smokes)),
        "",
        "## Forbidden Scope",
        "Forbidden paths:",
        *_format_items(task_spec.forbidden_paths),
        "Forbidden actions:",
        *_format_items(task_spec.forbidden_actions),
        "",
        "## Stop Conditions",
        *_format_items(sprint_step.stop_conditions),
        "",
        "=== ДЛЯ КУРАТОРА ===",
        "",
        "Статус:",
        "Что сделано:",
        "Изменённые/созданные файлы:",
        "Ключевой результат:",
        "Что НЕ тронуто / что осталось вне scope:",
        "Следующий шаг:",
        "Если есть блокер — точная причина:",
        "Repo state:",
        "Live deploy state:",
        "Public verify result:",
        "Sheet verify result:",
        "Upload-ready source state:",
        "Manual-only remainder:",
        "Commit hash:",
        "Push:",
        "PR:",
        "Ссылка на PR:",
        "",
        "=== СЖАТАЯ ПРОВЕРКА ===",
        "",
        "-",
        "-",
        "-",
        "Главный вывод:",
    ]
    return "\n".join(lines)


def _to_tuple(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        return (values,)
    return tuple(str(value) for value in values)


def _merge_defaults(values: Sequence[str], defaults: Sequence[str]) -> tuple[str, ...]:
    values_tuple = _to_tuple(values)
    merged: list[str] = []
    for value in (*defaults, *values_tuple):
        item = str(value)
        if item not in merged:
            merged.append(item)
    return tuple(merged)


def _compute_task_spec_hash(task_spec: TaskSpec) -> str:
    payload = asdict(task_spec)
    payload["spec_hash"] = None
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_non_empty(label: str, value: str | None) -> None:
    if not _is_present(value):
        raise CuratorCockpitValidationError(f"{label} is required")


def _require_non_empty_sequence(label: str, values: Sequence[str]) -> None:
    if not values or not any(_is_present(value) for value in values):
        raise CuratorCockpitValidationError(f"{label} must not be empty")


def _require_status(label: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        raise CuratorCockpitValidationError(f"{label} must be one of {sorted(allowed)}")


def _require_task_class(value: str) -> None:
    if value not in TASK_CLASSES:
        raise CuratorCockpitValidationError("task_class must be one of L1, L2, L3")


def _require_required_defaults(label: str, values: Sequence[str], defaults: Sequence[str]) -> None:
    missing = [item for item in defaults if item not in values]
    if missing:
        raise CuratorCockpitValidationError(f"{label} missing required defaults: {missing}")


def _reject_action_overlap(allowed_actions: Sequence[str], forbidden_actions: Sequence[str]) -> None:
    overlap = sorted(set(allowed_actions) & set(forbidden_actions))
    if overlap:
        raise CuratorCockpitValidationError(f"allowed_actions overlap forbidden_actions: {overlap}")


def _reject_path_overlap(allowed_paths: Sequence[str], forbidden_paths: Sequence[str]) -> None:
    overlap = sorted(set(allowed_paths) & set(forbidden_paths))
    if overlap:
        raise CuratorCockpitValidationError(f"allowed_paths overlap forbidden_paths: {overlap}")


def _is_present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _format_items(values: Sequence[str]) -> list[str]:
    return [f"- {value}" for value in values]
