"""Machine-readable contract for the local Codex task control plane.

The control plane is deliberately small.  GitHub remains the durable release
actuator; this module only defines logical task, watcher and incident policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CANONICAL_REPOSITORY = "orenvlad-ai/wb-core"
TASK_PASSPORT_SCHEMA = "wb-core-task-passport/v1"
WATCHER_CONFIG_SCHEMA = "wb-core-codex-watcher/v1"
ARBITER_BRIEF_SCHEMA = "wb-core-arbiter-brief/v1"
ARBITER_DECISION_SCHEMA = "wb-core-arbiter-decision/v1"
ARBITER_ACTIONS = frozenset(
    {
        "retry",
        "replace-executor",
        "continue-waiting",
        "recover-release",
        "await-human",
        "terminal-failure",
    }
)
EXECUTION_CONTOURS = frozenset(
    {
        "read-only",
        "user-artifact",
        "repo-only",
        "live-runtime",
        "production-mutation",
        "archived-gas-guard",
    }
)


class TaskStatus(str, Enum):
    DISCUSSION = "DISCUSSION"
    DISPATCHING = "DISPATCHING"
    WORKING = "WORKING"
    READY_FOR_RELEASE = "READY_FOR_RELEASE"
    RELEASE_OWNED = "RELEASE_OWNED"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    DONE_AWAITING_ACCEPTANCE = "DONE_AWAITING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    WAITING_RESOURCE = "WAITING_RESOURCE"
    CLAIMED = "CLAIMED"
    DECIDED = "DECIDED"
    DELIVERED = "DELIVERED"
    VERIFIED = "VERIFIED"
    CLOSED = "CLOSED"
    STALE = "STALE"


class IncidentDisposition(str, Enum):
    RETRY = "RETRY"
    REPLACE_EXECUTOR = "REPLACE_EXECUTOR"
    OPEN_ARBITER = "OPEN_ARBITER"
    AWAIT_HUMAN = "AWAIT_HUMAN"


class HumanReason(str, Enum):
    MISSING_CREDENTIAL = "missing-credential"
    INTERACTIVE_AUTH = "interactive-auth"
    IRREVERSIBLE_DATA_RISK = "irreversible-data-risk"
    SECURITY_PERMISSION = "security-or-permission-change"
    NEW_EXTERNAL_DESTINATION = "new-external-destination"
    MATERIAL_SCOPE_CHANGE = "material-scope-or-risk-change"
    PLATFORM_HARD_STOP = "platform-hard-stop"


STRICT_HUMAN_REASONS = frozenset(reason.value for reason in HumanReason)


TASK_TRANSITIONS = {
    TaskStatus.DISCUSSION: frozenset({TaskStatus.DISPATCHING}),
    TaskStatus.DISPATCHING: frozenset(
        {TaskStatus.WORKING, TaskStatus.RECOVERING, TaskStatus.AWAITING_HUMAN}
    ),
    TaskStatus.WORKING: frozenset(
        {
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.DONE_AWAITING_ACCEPTANCE,
            TaskStatus.TERMINAL_FAILURE,
        }
    ),
    TaskStatus.READY_FOR_RELEASE: frozenset(
        {
            TaskStatus.RELEASE_OWNED,
            TaskStatus.WORKING,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN,
        }
    ),
    TaskStatus.RELEASE_OWNED: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.DONE_AWAITING_ACCEPTANCE,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.DONE_AWAITING_ACCEPTANCE,
        }
    ),
    TaskStatus.RECOVERING: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RELEASE_OWNED,
            TaskStatus.VERIFYING,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.TERMINAL_FAILURE,
        }
    ),
    TaskStatus.AWAITING_HUMAN: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RECOVERING,
            TaskStatus.TERMINAL_FAILURE,
        }
    ),
    TaskStatus.DONE_AWAITING_ACCEPTANCE: frozenset(
        {TaskStatus.WORKING, TaskStatus.ACCEPTED}
    ),
    TaskStatus.ACCEPTED: frozenset(),
    TaskStatus.TERMINAL_FAILURE: frozenset({TaskStatus.ACCEPTED}),
}


REPORT_STATUS = {
    TaskStatus.DISCUSSION: "В работе",
    TaskStatus.DISPATCHING: "В работе",
    TaskStatus.WORKING: "В работе",
    TaskStatus.READY_FOR_RELEASE: "Выпуск и проверка",
    TaskStatus.RELEASE_OWNED: "Выпуск и проверка",
    TaskStatus.VERIFYING: "Выпуск и проверка",
    TaskStatus.RECOVERING: "Техническое восстановление",
    TaskStatus.AWAITING_HUMAN: "Требует владельца",
    TaskStatus.DONE_AWAITING_ACCEPTANCE: "Завершена — ждёт приёмки",
    TaskStatus.TERMINAL_FAILURE: "Остановлена",
}


@dataclass(frozen=True)
class RetryObservation:
    error_class: str
    identical_fingerprint_count: int
    transient: bool = False
    empty_system_error: bool = False
    repo_owned_remediation_available: bool = True
    remediation_exhausted: bool = False
    human_reason: str = ""


def validate_task_id(task_id: str) -> str:
    normalized = task_id.strip().lower()
    if not TASK_ID_RE.fullmatch(normalized):
        raise ValueError("task_id must contain 8-64 lowercase letters, digits or hyphens")
    return normalized


def validate_digest(digest: str) -> str:
    normalized = digest.strip().lower()
    if not DIGEST_RE.fullmatch(normalized):
        raise ValueError("digest must use sha256:<64 lowercase hex characters>")
    return normalized


def canonical_digest(payload: Mapping[str, object]) -> str:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(rendered).hexdigest()


def _exact_keys(value: Mapping[str, object], *, field: str, keys: set[str]) -> None:
    actual = set(value)
    if actual != keys:
        raise ValueError(
            f"{field} fields must equal {sorted(keys)!r}; got {sorted(actual)!r}"
        )


def _string_list(
    value: object,
    *,
    field: str,
    required: bool = False,
    unique: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a JSON array")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must contain only strings")
    normalized = [item.strip() for item in value]
    if any(not item for item in normalized):
        raise ValueError(f"{field} must contain only non-empty strings")
    if required and not normalized:
        raise ValueError(f"{field} must not be empty")
    if unique and len(set(normalized)) != len(normalized):
        raise ValueError(f"{field} must contain unique strings")
    return normalized


def validate_task_passport(
    passport: Mapping[str, object],
    *,
    task_id: str,
    title: str,
    objective: str,
    curator_thread_id: str,
    executor_thread_id: str,
) -> dict[str, Any]:
    """Validate the versioned dispatch envelope before it becomes registry state."""

    identity = validate_task_id(task_id)
    payload = json.loads(json.dumps(passport, ensure_ascii=False))
    if not isinstance(payload, dict):
        raise ValueError("passport must be a JSON object")
    _exact_keys(
        payload,
        field="passport",
        keys={
            "schema",
            "title",
            "objective",
            "expected_result",
            "scope",
            "constraints",
            "acceptance",
            "closure",
            "autonomy",
            "initial_resources",
            "source",
        },
    )
    if payload.get("schema") != TASK_PASSPORT_SCHEMA:
        raise ValueError(f"passport schema must be {TASK_PASSPORT_SCHEMA!r}")
    for field, expected in (("title", title), ("objective", objective)):
        if not isinstance(payload.get(field), str):
            raise ValueError(f"passport {field} must be a string")
        actual = payload[field].strip()
        if not actual or actual != expected.strip():
            raise ValueError(f"passport {field} must match task registration")
        payload[field] = actual
    if not isinstance(payload.get("expected_result"), str):
        raise ValueError("passport expected_result must be a string")
    expected_result = payload["expected_result"].strip()
    if not expected_result:
        raise ValueError("passport expected_result is required")
    payload["expected_result"] = expected_result

    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise ValueError("passport scope must be a JSON object")
    _exact_keys(
        scope,
        field="passport scope",
        keys={"execution_contour", "included", "excluded"},
    )
    if not isinstance(scope.get("execution_contour"), str):
        raise ValueError("passport scope.execution_contour must be a string")
    contour = scope["execution_contour"].strip()
    if contour not in EXECUTION_CONTOURS:
        raise ValueError("passport scope.execution_contour is invalid")
    scope["execution_contour"] = contour
    scope["included"] = _string_list(scope.get("included"), field="scope.included", required=True)
    scope["excluded"] = _string_list(scope.get("excluded"), field="scope.excluded")

    payload["constraints"] = _string_list(
        payload.get("constraints"), field="constraints"
    )
    payload["acceptance"] = _string_list(
        payload.get("acceptance"), field="acceptance", required=True
    )
    payload["closure"] = _string_list(
        payload.get("closure"), field="closure", required=True
    )
    resources = _string_list(
        payload.get("initial_resources"),
        field="initial_resources",
        required=True,
        unique=True,
    )
    if f"task:{identity}" not in resources:
        raise ValueError("passport initial_resources must include the exact task resource")
    payload["initial_resources"] = sorted(resources)

    autonomy = payload.get("autonomy")
    if not isinstance(autonomy, dict):
        raise ValueError("passport autonomy must be a JSON object")
    _exact_keys(
        autonomy,
        field="passport autonomy",
        keys={
            "reversible_technical_actions",
            "production_data_mutation",
            "human_only_reasons",
        },
    )
    if autonomy.get("reversible_technical_actions") != "authorized":
        raise ValueError("passport must authorize reversible in-scope technical actions")
    if autonomy.get("production_data_mutation") not in {"forbidden", "human-gated"}:
        raise ValueError("passport production_data_mutation must be forbidden or human-gated")
    reasons = set(
        _string_list(
            autonomy.get("human_only_reasons"),
            field="autonomy.human_only_reasons",
            required=True,
            unique=True,
        )
    )
    if reasons != STRICT_HUMAN_REASONS:
        raise ValueError("passport human_only_reasons must equal the strict v1 allowlist")
    autonomy["human_only_reasons"] = sorted(reasons)

    source = payload.get("source")
    if not isinstance(source, dict):
        raise ValueError("passport source must be a JSON object")
    _exact_keys(
        source,
        field="passport source",
        keys={"curator_thread_id", "executor_thread_id"},
    )
    expected_threads = {
        "curator_thread_id": curator_thread_id.strip(),
        "executor_thread_id": executor_thread_id.strip(),
    }
    if any(
        not isinstance(source.get(key), str) or source[key].strip() != value
        for key, value in expected_threads.items()
    ):
        raise ValueError("passport source must bind the exact curator and executor threads")
    source.update(expected_threads)
    return payload


def validate_arbiter_decision(
    decision: Mapping[str, object],
    *,
    task_id: str,
    task_revision: int,
    incident_key_value: str,
    allowed_resources: Iterable[str],
    expected_transition: str,
    evidence_digest: str,
) -> dict[str, Any]:
    payload = json.loads(json.dumps(decision, ensure_ascii=False))
    if not isinstance(payload, dict):
        raise ValueError("arbiter decision must be a JSON object")
    _exact_keys(
        payload,
        field="arbiter decision",
        keys={
            "schema",
            "task_id",
            "task_revision",
            "incident_key",
            "action",
            "scope",
            "expected_transition",
            "evidence_digest",
            "reason",
            "human_reason",
        },
    )
    if payload["schema"] != ARBITER_DECISION_SCHEMA:
        raise ValueError(f"arbiter decision schema must be {ARBITER_DECISION_SCHEMA!r}")
    if payload["task_id"] != validate_task_id(task_id):
        raise ValueError("arbiter decision task_id does not match the incident")
    if payload["task_revision"] != task_revision:
        raise ValueError("arbiter decision task_revision is stale")
    if payload["incident_key"] != validate_digest(incident_key_value):
        raise ValueError("arbiter decision incident_key does not match the incident")
    action = payload["action"]
    if action not in ARBITER_ACTIONS:
        raise ValueError("arbiter decision action is invalid")
    scope = _string_list(payload["scope"], field="arbiter decision scope", required=True, unique=True)
    resources = set(allowed_resources)
    if not set(scope).issubset(resources):
        raise ValueError("arbiter decision expands beyond the incident resource set")
    transition = payload["expected_transition"]
    if not isinstance(transition, str) or transition.strip() != expected_transition.strip() or not transition.strip():
        raise ValueError("arbiter decision expected_transition does not match delivery")
    if payload["evidence_digest"] != validate_digest(evidence_digest):
        raise ValueError("arbiter decision evidence_digest does not match delivery")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("arbiter decision requires a reason")
    human_reason = payload["human_reason"]
    if not isinstance(human_reason, str):
        raise ValueError("arbiter decision human_reason must be a string")
    human_reason = human_reason.strip()
    if action == "await-human":
        if human_reason not in STRICT_HUMAN_REASONS:
            raise ValueError("await-human requires a strict v1 human reason")
    elif human_reason:
        raise ValueError("human_reason is allowed only for await-human")
    payload["scope"] = scope
    payload["expected_transition"] = transition.strip()
    payload["reason"] = reason.strip()
    payload["human_reason"] = human_reason
    return payload


def incident_key(
    *,
    task_id: str,
    task_revision: int,
    phase: str,
    error_class: str,
    evidence_fingerprint: str,
    resources: Iterable[str],
) -> str:
    if task_revision <= 0:
        raise ValueError("task_revision must be positive")
    payload = {
        "task_id": validate_task_id(task_id),
        "task_revision": task_revision,
        "phase": phase.strip(),
        "error_class": error_class.strip(),
        "evidence_fingerprint": evidence_fingerprint.strip(),
        "resources": sorted({item.strip() for item in resources if item.strip()}),
    }
    if not all(payload[key] for key in ("phase", "error_class", "evidence_fingerprint")):
        raise ValueError("incident identity fields must be non-empty")
    return canonical_digest(payload)


def transition_allowed(current: TaskStatus, target: TaskStatus) -> bool:
    return current == target or target in TASK_TRANSITIONS[current]


def classify_incident(observation: RetryObservation) -> IncidentDisposition:
    """Keep human parking narrow and prevent infinite blind restarts."""

    if observation.identical_fingerprint_count <= 0:
        raise ValueError("identical_fingerprint_count must be positive")

    if observation.human_reason:
        if observation.human_reason not in STRICT_HUMAN_REASONS:
            return IncidentDisposition.OPEN_ARBITER
        if observation.repo_owned_remediation_available or not observation.remediation_exhausted:
            return IncidentDisposition.OPEN_ARBITER
        return IncidentDisposition.AWAIT_HUMAN
    if observation.empty_system_error:
        if observation.identical_fingerprint_count == 1:
            return IncidentDisposition.RETRY
        if observation.identical_fingerprint_count == 2:
            return IncidentDisposition.REPLACE_EXECUTOR
        return IncidentDisposition.OPEN_ARBITER
    if observation.identical_fingerprint_count >= 3:
        return IncidentDisposition.OPEN_ARBITER
    return IncidentDisposition.RETRY


def report_status(status: TaskStatus) -> str:
    if status == TaskStatus.ACCEPTED:
        raise ValueError("accepted tasks are excluded from active reports")
    return REPORT_STATUS[status]
