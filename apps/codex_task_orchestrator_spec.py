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
ENVELOPE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
ATTENTION_EVENT_ID_RE = re.compile(r"^evt-[0-9a-f]{24}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
CANONICAL_REPOSITORY = "orenvlad-ai/wb-core"
TASK_PASSPORT_SCHEMA = "wb-core-task-passport/v1"
WATCHER_CONFIG_SCHEMA = "wb-core-codex-watcher/v1"
WATCHER_RUN_PLAN_SCHEMA = "wb-core-watcher-run-plan/v1"
WATCHER_TARGET_OBSERVATION_SCHEMA = "wb-core-watcher-target-observation/v1"
WATCHER_MECHANICAL_PREFLIGHT_SCHEMA = "wb-core-watcher-mechanical-preflight/v1"
CURATOR_LIFECYCLE_OBSERVATION_SCHEMA = "wb-core-curator-lifecycle-observation/v1"
WATCHER_RUN_PHASES = (
    "snapshot-integrity-queue",
    "target-coverage",
    "progress-state",
    "objective-evidence",
    "apply-progress",
    "failure-incident",
    "terminal-evidence",
    "release-lane-closure",
    "attention-delivery",
    "rotation-operation",
    "end-run",
    "heartbeat-response",
)
RELEASE_LANE_CLOSURE_STATES = frozenset(
    {"PENDING", "DISPATCHED", "RETRY", "CONFIRMED", "EXHAUSTED", "STALE"}
)
RELEASE_LANE_CLOSURE_MAX_ATTEMPTS = 3
WATCHER_TARGET_READBACK_STATUSES = frozenset(
    {"active", "idle", "completed", "failed"}
)
WATCHER_PREFLIGHT_DECISIONS = frozenset(
    {"FULL", "QUIET", "OWNER_WAITING", "OWNER_REMINDER"}
)
WATCHER_BASELINE_MODEL_FACING_STEPS = (
    "begin-run",
    "protocol-doc-readback",
    "watcher-thread-readback",
    "queue-status",
    "heartbeat-plan",
    "heartbeat-actuate",
    "attention-reservation",
    "pending-executor-archives",
    "heartbeat-finish",
)
WATCHER_FAST_MODEL_FACING_STEPS = (
    "begin-run",
    "mechanical-preflight",
    "heartbeat-fast-finish",
)
CURATOR_WAKE_SOURCES = frozenset(
    {"dispatch-complete", "user-message", "watcher-attention"}
)

WATCHER_ROTATION_STATES = frozenset(
    {
        "REQUIRED",
        "RETRY_PENDING",
        "ATTENTION_REQUIRED",
        "SUCCESSOR_PREPARED",
        "SUCCESSOR_SMOKED",
        "ACTIVATED",
        "LIVENESS_PROVEN",
        "COMPLETED",
    }
)
WATCHER_ROTATION_TERMINAL_STATES = frozenset({"COMPLETED"})
WATCHER_ROTATION_REMEDIATION_STATES = frozenset(
    {"REQUIRED", "RETRY_PENDING", "ATTENTION_REQUIRED"}
)
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


class ProgressStage(str, Enum):
    PRE_EXECUTOR = "pre-executor"
    EXECUTOR_STARTED = "executor-started"
    PREFLIGHT_COMPLETE = "preflight-complete"
    IMPLEMENTATION_STARTED = "implementation-started"
    MAIN_DIFF_READY = "main-diff-ready"
    PRIMARY_CHECKS_PASSED = "primary-checks-passed"
    FULL_CHECKS_PASSED = "full-checks-passed"
    PR_CREATED = "pr-created"
    RELEASE_ADMITTED = "release-admitted"
    RELEASE_RUNNING = "release-running"
    DEPLOYED_VERIFYING = "deployed-verifying"
    TECHNICAL_COMPLETE = "technical-complete"


PROGRESS_PERCENT_BY_STAGE = {
    ProgressStage.PRE_EXECUTOR: 0,
    ProgressStage.EXECUTOR_STARTED: 5,
    ProgressStage.PREFLIGHT_COMPLETE: 15,
    ProgressStage.IMPLEMENTATION_STARTED: 25,
    ProgressStage.MAIN_DIFF_READY: 40,
    ProgressStage.PRIMARY_CHECKS_PASSED: 55,
    ProgressStage.FULL_CHECKS_PASSED: 65,
    ProgressStage.PR_CREATED: 72,
    ProgressStage.RELEASE_ADMITTED: 80,
    ProgressStage.RELEASE_RUNNING: 88,
    ProgressStage.DEPLOYED_VERIFYING: 95,
    ProgressStage.TECHNICAL_COMPLETE: 100,
}
PROGRESS_STAGES = tuple(PROGRESS_PERCENT_BY_STAGE)
EXECUTOR_CHECKPOINT_STAGES = frozenset(
    {
        ProgressStage.PREFLIGHT_COMPLETE,
        ProgressStage.IMPLEMENTATION_STARTED,
        ProgressStage.MAIN_DIFF_READY,
        ProgressStage.PRIMARY_CHECKS_PASSED,
        ProgressStage.FULL_CHECKS_PASSED,
    }
)
OBSERVED_EARLY_STAGES = EXECUTOR_CHECKPOINT_STAGES
OBJECTIVE_PROGRESS_STAGES = frozenset(
    {
        ProgressStage.PR_CREATED,
        ProgressStage.RELEASE_ADMITTED,
        ProgressStage.RELEASE_RUNNING,
        ProgressStage.DEPLOYED_VERIFYING,
    }
)
OBJECTIVE_PR_STATE_STAGE = {
    "open": ProgressStage.PR_CREATED,
    "draft": ProgressStage.PR_CREATED,
    "checks-pending": ProgressStage.PR_CREATED,
    "ci-green": ProgressStage.RELEASE_ADMITTED,
    "staged": ProgressStage.RELEASE_ADMITTED,
    "ready": ProgressStage.RELEASE_ADMITTED,
    "admitted": ProgressStage.RELEASE_ADMITTED,
    "awaiting-agent": ProgressStage.RELEASE_ADMITTED,
    "blocked": ProgressStage.RELEASE_ADMITTED,
    "running": ProgressStage.RELEASE_RUNNING,
    "release-running": ProgressStage.RELEASE_RUNNING,
    "merged": ProgressStage.RELEASE_RUNNING,
    "deployed": ProgressStage.DEPLOYED_VERIFYING,
    "awaiting-ui": ProgressStage.DEPLOYED_VERIFYING,
    "verifying": ProgressStage.DEPLOYED_VERIFYING,
    "done": ProgressStage.RELEASE_RUNNING,
    "production": ProgressStage.DEPLOYED_VERIFYING,
}
TERMINAL_EVIDENCE_BY_CONTOUR = {
    "read-only": "diagnostic-complete",
    "user-artifact": "artifact-verified",
    "repo-only": "release:done",
    "live-runtime": "release:production",
    "production-mutation": "release:production",
    "archived-gas-guard": "release:done",
}


def progress_percent(stage: ProgressStage) -> int:
    return PROGRESS_PERCENT_BY_STAGE[stage]


def progress_stage_for_percent(percent: int) -> ProgressStage:
    for stage, mapped_percent in PROGRESS_PERCENT_BY_STAGE.items():
        if mapped_percent == percent:
            return stage
    raise ValueError("progress must equal a centralized evidence-backed milestone")


def previous_progress_stage(stage: ProgressStage) -> ProgressStage:
    index = PROGRESS_STAGES.index(stage)
    if index <= 1:
        raise ValueError("executor-started progress cannot be invalidated")
    return PROGRESS_STAGES[index - 1]


def objective_stage_from_pr_states(states: Iterable[str]) -> ProgressStage | None:
    normalized = [
        state.strip().casefold().removeprefix("release:") for state in states
    ]
    stages = [
        OBJECTIVE_PR_STATE_STAGE[state]
        for state in normalized
        if state in OBJECTIVE_PR_STATE_STAGE
    ]
    if not stages:
        return None
    return max(stages, key=progress_percent)


def validate_progress_stage_for_contour(
    stage: ProgressStage, execution_contour: str
) -> ProgressStage:
    if execution_contour not in EXECUTION_CONTOURS:
        raise ValueError("unknown execution contour")
    if stage == ProgressStage.PRE_EXECUTOR:
        raise ValueError("registered tasks already have a started executor")
    if (
        execution_contour in {"read-only", "user-artifact"}
        and stage in OBJECTIVE_PROGRESS_STAGES
    ):
        raise ValueError("this execution contour has no GitHub/release progress stages")
    if execution_contour == "repo-only" and stage == ProgressStage.DEPLOYED_VERIFYING:
        raise ValueError("repo-only tasks do not have a deploy verification stage")
    return stage


def validate_terminal_evidence(
    execution_contour: str, evidence_class: str
) -> str:
    expected = TERMINAL_EVIDENCE_BY_CONTOUR.get(execution_contour)
    if expected is None or evidence_class.strip() != expected:
        raise ValueError(
            "technical completion evidence does not match the execution contour"
        )
    return expected


class TaskStatus(str, Enum):
    DISCUSSION = "DISCUSSION"
    DISPATCHING = "DISPATCHING"
    WORKING = "WORKING"
    READY_FOR_RELEASE = "READY_FOR_RELEASE"
    RELEASE_OWNED = "RELEASE_OWNED"
    VERIFYING = "VERIFYING"
    RECOVERING = "RECOVERING"
    DONE_PENDING_HANDOFF = "DONE_PENDING_HANDOFF"
    TERMINAL_FAILURE_PENDING_HANDOFF = "TERMINAL_FAILURE_PENDING_HANDOFF"
    AWAITING_HUMAN_PENDING_HANDOFF = "AWAITING_HUMAN_PENDING_HANDOFF"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    DONE_AWAITING_ACCEPTANCE = "DONE_AWAITING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


class AttentionKind(str, Enum):
    TECHNICAL_COMPLETION = "TECHNICAL_COMPLETION"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    STRICT_HUMAN_GATE = "STRICT_HUMAN_GATE"
    SERIOUS_STALL = "SERIOUS_STALL"


class AttentionStatus(str, Enum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    SENT = "SENT"
    RETRY = "RETRY"
    ACKED = "ACKED"
    STALE = "STALE"


class AcceptanceStatus(str, Enum):
    OPEN = "OPEN"
    DONE_PENDING_HANDOFF = "DONE_PENDING_HANDOFF"
    AWAITING_ACCEPTANCE = "AWAITING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"


class SuccessionStatus(str, Enum):
    READY_TO_ARCHIVE = "READY_TO_ARCHIVE"
    ARCHIVED = "ARCHIVED"


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
        {
            TaskStatus.WORKING,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
        }
    ),
    TaskStatus.WORKING: frozenset(
        {
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            TaskStatus.DONE_PENDING_HANDOFF,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
    ),
    TaskStatus.READY_FOR_RELEASE: frozenset(
        {
            TaskStatus.RELEASE_OWNED,
            TaskStatus.WORKING,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            TaskStatus.DONE_PENDING_HANDOFF,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
    ),
    TaskStatus.RELEASE_OWNED: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            TaskStatus.DONE_PENDING_HANDOFF,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RECOVERING,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            TaskStatus.DONE_PENDING_HANDOFF,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
    ),
    TaskStatus.RECOVERING: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RELEASE_OWNED,
            TaskStatus.VERIFYING,
            TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF,
            TaskStatus.DONE_PENDING_HANDOFF,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
    ),
    TaskStatus.DONE_PENDING_HANDOFF: frozenset(
        {TaskStatus.DONE_AWAITING_ACCEPTANCE}
    ),
    TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF: frozenset(
        {TaskStatus.TERMINAL_FAILURE}
    ),
    TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF: frozenset(
        {TaskStatus.AWAITING_HUMAN}
    ),
    TaskStatus.AWAITING_HUMAN: frozenset(
        {
            TaskStatus.WORKING,
            TaskStatus.READY_FOR_RELEASE,
            TaskStatus.RECOVERING,
            TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF,
        }
    ),
    TaskStatus.DONE_AWAITING_ACCEPTANCE: frozenset(
        {TaskStatus.ACCEPTED}
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
    TaskStatus.DONE_PENDING_HANDOFF: "Завершена — передаётся куратору",
    TaskStatus.TERMINAL_FAILURE_PENDING_HANDOFF: "Остановлена — передаётся куратору",
    TaskStatus.AWAITING_HUMAN_PENDING_HANDOFF: "Требует владельца — передаётся куратору",
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


def validate_envelope_id(envelope_id: str) -> str:
    normalized = envelope_id.strip().lower()
    if not ENVELOPE_ID_RE.fullmatch(normalized):
        raise ValueError(
            "acceptance envelope id must contain 8-64 lowercase letters, digits or hyphens"
        )
    return normalized


def validate_attention_event_id(event_id: str) -> str:
    normalized = event_id.strip().lower()
    if not ATTENTION_EVENT_ID_RE.fullmatch(normalized):
        raise ValueError("attention event id must use evt- followed by 24 lowercase hex characters")
    return normalized


VISIBLE_INTERNAL_TOKENS = frozenset(
    {
        "registry",
        "integrity",
        "queue",
        "lease",
        "follow-up",
        "bounded",
        "exact",
        "wait_threads",
        "task_id",
        "revision",
        "thread uuid",
        "done_awaiting_acceptance",
        "done_pending_handoff",
        "working",
        "release_owned",
        "verifying",
    }
    | {item.value.casefold() for item in TaskStatus}
    | {item.value.casefold() for item in AttentionKind}
    | {item.value.casefold() for item in AttentionStatus}
    | {item.value.casefold() for item in AcceptanceStatus}
    | {item.value.casefold() for item in SuccessionStatus}
)


def validate_visible_text(text: str, *, field: str, task_id: str = "") -> str:
    """Fail closed instead of leaking machine diagnostics into the Watcher report."""

    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{field} must be a non-empty user-facing string")
    normalized = text.strip()
    lowered = normalized.casefold()
    if UUID_RE.search(normalized):
        raise ValueError(f"{field} must not expose a thread UUID")
    if re.search(r"\b(?:evt-[0-9a-f]{24}|succ-[0-9a-f]{20})\b", lowered):
        raise ValueError(f"{field} must not expose an internal event identity")
    if "sha256:" in lowered or re.search(r"\brevision\s*\d+\b", lowered):
        raise ValueError(f"{field} must not expose a digest or revision")
    if task_id and task_id.casefold() in lowered:
        raise ValueError(f"{field} must not expose task_id")
    leaked = sorted(
        token
        for token in VISIBLE_INTERNAL_TOKENS
        if re.search(
            rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])",
            lowered,
        )
    )
    if leaked:
        raise ValueError(f"{field} contains internal Watcher terms: {', '.join(leaked)}")
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


def validate_curator_lifecycle_observation(
    observation: Mapping[str, object],
) -> dict[str, object]:
    required = {
        "schema",
        "phase",
        "source_surface",
        "source_project",
        "executor_project",
        "service_prompt_present",
        "dispatch_summary_count",
        "turn_completed",
        "curator_idle",
        "curator_progress_poll_calls",
        "curator_heartbeat_count",
        "wake_source",
        "bounded_action_count",
        "exact_attention_binding",
        "executor_is_only_watcher_target",
        "active_watcher_count",
        "per_task_automation_count",
        "manual_sleep_correction",
    }
    if set(observation) != required:
        raise ValueError("curator lifecycle observation fields do not match the contract")
    if observation["schema"] != CURATOR_LIFECYCLE_OBSERVATION_SCHEMA:
        raise ValueError("unknown curator lifecycle observation schema")
    phase = str(observation["phase"])
    if phase not in {"post-dispatch", "post-wake"}:
        raise ValueError("unknown curator lifecycle phase")
    source_surface = str(observation["source_surface"])
    if source_surface not in {
        "ordinary-chatgpt-project-chat",
        "backend-load-fixture",
    }:
        raise ValueError("unknown curator source surface")
    expected_source_project = (
        "wb_core_3"
        if source_surface == "ordinary-chatgpt-project-chat"
        else "backend-load-fixture"
    )
    if observation["source_project"] != expected_source_project:
        raise ValueError("curator lifecycle source project does not match its surface")
    if observation["executor_project"] != "wb-core - codex":
        raise ValueError("curator lifecycle requires the saved executor project")
    integer_fields = {
        "dispatch_summary_count",
        "curator_progress_poll_calls",
        "curator_heartbeat_count",
        "bounded_action_count",
        "active_watcher_count",
        "per_task_automation_count",
    }
    boolean_fields = {
        "service_prompt_present",
        "turn_completed",
        "curator_idle",
        "exact_attention_binding",
        "executor_is_only_watcher_target",
        "manual_sleep_correction",
    }
    for field in integer_fields:
        value = observation[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"curator lifecycle {field} must be a non-negative integer")
    for field in boolean_fields:
        if not isinstance(observation[field], bool):
            raise ValueError(f"curator lifecycle {field} must be boolean")
    wake_source = str(observation["wake_source"])
    if wake_source not in CURATOR_WAKE_SOURCES:
        raise ValueError("unknown curator wake source")
    if not observation["turn_completed"] or not observation["curator_idle"]:
        raise ValueError("curator must complete its bounded turn and become idle")
    if int(observation["curator_progress_poll_calls"]) != 0:
        raise ValueError("curator progress polling is forbidden")
    if int(observation["curator_heartbeat_count"]) != 0:
        raise ValueError("curator heartbeat automation is forbidden")
    if int(observation["active_watcher_count"]) != 1:
        raise ValueError("curator lifecycle requires exactly one active Global Watcher")
    if int(observation["per_task_automation_count"]) != 0:
        raise ValueError("per-task heartbeat automation is forbidden")
    if not observation["executor_is_only_watcher_target"]:
        raise ValueError("only the executor may enter the Watcher target contour")
    if observation["manual_sleep_correction"]:
        raise ValueError("manual sleep correction is not lifecycle evidence")
    if phase == "post-dispatch":
        if int(observation["dispatch_summary_count"]) != 1:
            raise ValueError("dispatch must emit exactly one short summary")
        if wake_source != "dispatch-complete" or int(
            observation["bounded_action_count"]
        ) != 1:
            raise ValueError("dispatch lifecycle requires one bounded launch action")
        if observation["exact_attention_binding"]:
            raise ValueError("post-dispatch lifecycle cannot claim attention binding")
    else:
        if int(observation["dispatch_summary_count"]) != 0:
            raise ValueError("post-wake action cannot emit another dispatch summary")
        if wake_source not in {"user-message", "watcher-attention"}:
            raise ValueError("post-wake lifecycle requires a normal wake source")
        if int(observation["bounded_action_count"]) != 1:
            raise ValueError("post-wake lifecycle requires one bounded curator action")
        if (wake_source == "watcher-attention") != bool(
            observation["exact_attention_binding"]
        ):
            raise ValueError("Watcher wake requires exact attention binding")
    if (
        source_surface == "ordinary-chatgpt-project-chat"
        and observation["service_prompt_present"]
    ):
        raise ValueError("front-door canary cannot use a service prompt")
    validated = dict(observation)
    validated["front_door_surface_eligible"] = bool(
        source_surface == "ordinary-chatgpt-project-chat"
        and not observation["service_prompt_present"]
    )
    return validated


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
