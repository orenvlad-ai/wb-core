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
from typing import Iterable, Mapping


TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


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
    TaskStatus.TERMINAL_FAILURE: frozenset(),
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

    if observation.human_reason:
        if observation.human_reason not in STRICT_HUMAN_REASONS:
            return IncidentDisposition.OPEN_ARBITER
        if observation.repo_owned_remediation_available or not observation.remediation_exhausted:
            return IncidentDisposition.OPEN_ARBITER
        return IncidentDisposition.AWAIT_HUMAN
    if observation.empty_system_error:
        return (
            IncidentDisposition.RETRY
            if observation.identical_fingerprint_count < 2
            else IncidentDisposition.REPLACE_EXECUTOR
        )
    if observation.identical_fingerprint_count >= 3:
        return IncidentDisposition.OPEN_ARBITER
    if observation.transient:
        return IncidentDisposition.RETRY
    return IncidentDisposition.OPEN_ARBITER


def report_status(status: TaskStatus) -> str:
    if status == TaskStatus.ACCEPTED:
        raise ValueError("accepted tasks are excluded from active reports")
    return REPORT_STATUS[status]
