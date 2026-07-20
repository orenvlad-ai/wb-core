"""Machine-readable contract shared by the wb-core Release Train surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping
from urllib.parse import quote_plus


class TaskClass(str, Enum):
    STANDARD = "standard"
    LOOP = "loop"
    DIAGNOSTIC = "diagnostic"


class TaskContinuity(str, Enum):
    """Identity relationship between a prompt and an already known task."""

    NEW_TASK = "NEW_TASK"
    ACTIVE_ADDITION = "ACTIVE_ADDITION"
    ACTIVE_LOOP_RECOVERY = "ACTIVE_LOOP_RECOVERY"
    TERMINAL_STALE_REFERENCE = "TERMINAL_STALE_REFERENCE"


class GoalDisposition(str, Enum):
    """Canonical Goal Mode interpretation of a Release Train observation."""

    TERMINAL_SUCCESS = "TERMINAL_SUCCESS"
    CONTINUE_WAITING = "CONTINUE_WAITING"
    OWN_ACTION = "OWN_ACTION"
    TAKEOVER_PREDECESSOR = "TAKEOVER_PREDECESSOR"
    RECOVER_OWN_CHAIN = "RECOVER_OWN_CHAIN"
    EXTERNAL_BLOCKER = "EXTERNAL_BLOCKER"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"


class GoalReasonCode(str, Enum):
    """Stable machine reason codes; prose is never used as a state transition."""

    TERMINAL_PROOF_VERIFIED = "terminal-proof-verified"
    TERMINAL_PROOF_MISSING = "terminal-proof-missing"
    NORMAL_QUEUE_WAITING = "normal-queue-waiting"
    FOREIGN_OWNER_ACTIVE = "foreign-owner-active"
    FOREIGN_GATE_NEEDS_TAKEOVER = "foreign-gate-needs-takeover"
    LOST_OWNER_EVIDENCE_INCOMPLETE = "lost-owner-evidence-incomplete"
    OWN_RELEASE_RESUME_REQUIRED = "own-release-resume-required"
    OWN_AGENT_ACK_REQUIRED = "own-agent-ack-required"
    OWN_UI_FLOW_REQUIRED = "own-ui-flow-required"
    OWN_RELEASE_REMEDIATION_AVAILABLE = "own-release-remediation-available"
    OWN_RELEASE_ENQUEUE_REQUIRED = "own-release-enqueue-required"
    QUEUE_RECONCILIATION_AVAILABLE = "queue-reconciliation-available"
    HALTED_RECONCILIATION_AVAILABLE = "halted-reconciliation-available"
    EXTERNAL_AUTHORITY_REQUIRED = "external-authority-required"
    PROTOCOL_IRRECOVERABLE = "protocol-irrecoverable"


class UiRuntime(str, Enum):
    """UI runtime selected by the execution surface."""

    LOCAL_PLAYWRIGHT = "LOCAL_PLAYWRIGHT"
    CHATGPT_EMBEDDED_BROWSER = "CHATGPT_EMBEDDED_BROWSER"


@dataclass(frozen=True)
class GoalDecision:
    """Machine-readable Goal handoff contract shared by CLI and regressions."""

    disposition: GoalDisposition
    own_pr: int
    action_pr: int
    canonical_github_state: Mapping[str, Any]
    reason_code: str
    allowed_next_action: str
    user_intervention_required: bool
    evidence: tuple[Mapping[str, Any], ...]
    remediation_exhausted: bool

    def __post_init__(self) -> None:
        if self.own_pr <= 0 or self.action_pr <= 0:
            raise ValueError("Goal decision requires positive own_pr and action_pr")
        if not self.reason_code:
            raise ValueError("Goal decision requires a canonical reason code")
        if not self.canonical_github_state:
            raise ValueError("Goal decision requires canonical GitHub state")
        if self.disposition == GoalDisposition.EXTERNAL_BLOCKER:
            if not (
                self.user_intervention_required
                and self.remediation_exhausted
                and self.evidence
                and self.allowed_next_action
            ):
                raise ValueError(
                    "EXTERNAL_BLOCKER requires evidence, exhausted remediation and a human-only action"
                )
            if any(bool(item.get("repo_owned_action_available")) for item in self.evidence):
                raise ValueError(
                    "EXTERNAL_BLOCKER is forbidden while repo-owned remediation is available"
                )
        elif self.disposition == GoalDisposition.TERMINAL_FAILURE:
            if self.user_intervention_required or not self.remediation_exhausted or not self.evidence:
                raise ValueError(
                    "TERMINAL_FAILURE requires evidence and exhausted remediation without handoff"
                )
        elif self.user_intervention_required or self.remediation_exhausted:
            raise ValueError(
                "non-blocking Goal dispositions cannot require user intervention or exhausted remediation"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "own_pr": self.own_pr,
            "action_pr": self.action_pr,
            "canonical_github_state": dict(self.canonical_github_state),
            "reason_code": self.reason_code,
            "allowed_next_action": self.allowed_next_action,
            "user_intervention_required": self.user_intervention_required,
            "evidence": [dict(item) for item in self.evidence],
            "remediation_exhausted": self.remediation_exhausted,
        }


@dataclass(frozen=True)
class UiRuntimeDecision:
    runtime: UiRuntime
    continue_ui_flow: bool
    external_blocker_eligible: bool
    reason_code: str
    evidence: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "runtime": self.runtime.value,
            "continue_ui_flow": self.continue_ui_flow,
            "external_blocker_eligible": self.external_blocker_eligible,
            "reason_code": self.reason_code,
            "evidence": [dict(item) for item in self.evidence],
        }


EXPLICIT_TASK_PROMPTS = {
    "КЛАСС ЗАДАЧИ: СТАНДАРТ": TaskClass.STANDARD,
    "КЛАСС ЗАДАЧИ: LOOP": TaskClass.LOOP,
    "КЛАСС ЗАДАЧИ: ДИАГНОСТИКА": TaskClass.DIAGNOSTIC,
}

READY_LABEL = "release:ready"
RUNNING_LABEL = "release:running"
AWAITING_AGENT_LABEL = "release:awaiting-agent"
AWAITING_UI_LABEL = "release:awaiting-ui"
NEEDS_RESUME_LABEL = "release:needs-resume"
BLOCKED_LABEL = "release:blocked"
HALTED_LABEL = "release:halted"
DONE_LABEL = "release:done"
PRODUCTION_LABEL = "release:production"
SUPERSEDED_LABEL = "release:superseded"

ACTIVE_PRIMARY_LABELS = frozenset(
    {
        READY_LABEL,
        RUNNING_LABEL,
        AWAITING_AGENT_LABEL,
        AWAITING_UI_LABEL,
        BLOCKED_LABEL,
        HALTED_LABEL,
    }
)
OVERLAY_LABELS = frozenset({NEEDS_RESUME_LABEL})
TERMINAL_LABELS = frozenset({DONE_LABEL, PRODUCTION_LABEL, SUPERSEDED_LABEL})
ACTIVE_STATE_LABELS = ACTIVE_PRIMARY_LABELS | OVERLAY_LABELS
RESUMABLE_OWNER_LABELS = frozenset(
    {READY_LABEL, RUNNING_LABEL, AWAITING_AGENT_LABEL, AWAITING_UI_LABEL}
)
PRIMARY_STATE_LABELS = ACTIVE_PRIMARY_LABELS | TERMINAL_LABELS
MONITORED_RELEASE_LABELS = ACTIVE_PRIMARY_LABELS | OVERLAY_LABELS

# The only intentional two-label primary representation is running+ready: ready preserves
# queue membership while the serialized worker owns the PR.
ALLOWED_PRIMARY_COMBINATIONS = frozenset(
    {frozenset(), frozenset({READY_LABEL, RUNNING_LABEL})}
    | {frozenset({label}) for label in PRIMARY_STATE_LABELS}
)

TRANSITION_MATRIX = {
    "release:none": frozenset({READY_LABEL}),
    READY_LABEL: frozenset({RUNNING_LABEL, BLOCKED_LABEL, SUPERSEDED_LABEL}),
    RUNNING_LABEL: frozenset(
        {
            READY_LABEL,
            AWAITING_AGENT_LABEL,
            AWAITING_UI_LABEL,
            BLOCKED_LABEL,
            HALTED_LABEL,
            DONE_LABEL,
            PRODUCTION_LABEL,
            SUPERSEDED_LABEL,
        }
    ),
    AWAITING_AGENT_LABEL: frozenset({READY_LABEL, BLOCKED_LABEL, SUPERSEDED_LABEL}),
    AWAITING_UI_LABEL: frozenset({PRODUCTION_LABEL, HALTED_LABEL, SUPERSEDED_LABEL}),
    BLOCKED_LABEL: frozenset({READY_LABEL, SUPERSEDED_LABEL}),
    HALTED_LABEL: frozenset({AWAITING_UI_LABEL, PRODUCTION_LABEL, SUPERSEDED_LABEL}),
    DONE_LABEL: frozenset(),
    PRODUCTION_LABEL: frozenset(),
    SUPERSEDED_LABEL: frozenset(),
}

CRITICAL_TRANSITIONS = frozenset(
    {
        (AWAITING_AGENT_LABEL, READY_LABEL),
        (RUNNING_LABEL, DONE_LABEL),
        (RUNNING_LABEL, PRODUCTION_LABEL),
        (RUNNING_LABEL, AWAITING_UI_LABEL),
        (AWAITING_UI_LABEL, PRODUCTION_LABEL),
        (HALTED_LABEL, AWAITING_UI_LABEL),
        (HALTED_LABEL, PRODUCTION_LABEL),
    }
)

CANONICAL_MONITOR_QUERY = (
    'is:pr -label:release:superseded '
    'label:"release:ready,release:running,release:awaiting-agent,release:awaiting-ui,'
    'release:needs-resume,release:blocked,release:halted" sort:created-asc'
)
CANONICAL_MONITOR_URL = (
    "https://github.com/orenvlad-ai/wb-core/pulls?q=" + quote_plus(CANONICAL_MONITOR_QUERY)
)

STATUS_COMMENT_MARKER = "wb-core-release-status"
ACK_PROOF_MARKER = "wb-core-loop-ack-proof"
DEPLOY_PROOF_MARKER = "wb-core-loop-deploy-proof"
CHAIN_AUDIT_MARKER = "wb-core-loop-chain-audit"
RECONCILE_PROOF_MARKER = "wb-core-release-reconcile-proof"
COMPLETION_PROOF_MARKER = "wb-core-release-completion-proof"
HALT_PROOF_MARKER = "wb-core-release-halt-proof"
RETRY_PROOF_MARKER = "wb-core-release-retry-proof"
NEW_ROOT_PROOF_MARKER = "wb-core-loop-new-root-proof"
RECOVERY_PROOF_MARKER = "wb-core-loop-recovery-proof"
CLASSIFICATION_BLOCKER_MARKER = "wb-core-loop-classification-blocker"
IDENTITY_CORRECTION_PROOF_MARKER = "wb-core-loop-identity-correction-proof"
CANONICAL_PRODUCTION_TARGET_ID = "wb_core_eu_hosted_runtime_active"

TERMINAL_FORBIDDEN_INHERITANCE = frozenset(
    {
        "branch",
        "pr",
        "task_identity",
        "loop_root",
        "acknowledgement",
        "owner_heartbeat",
        "recovery_identity",
    }
)

EXPLICIT_NEW_TASK_PHRASES = frozenset(
    {
        "новая задача",
        "отдельная задача",
        "самостоятельная задача",
        "новый loop",
        "новая loop-задача",
        "отдельная loop-задача",
        "самостоятельная loop-задача",
    }
)


@dataclass(frozen=True)
class TaskIntent:
    read_only: bool = False
    deploy: bool = False
    production_ui: bool = False
    iterative: bool = False
    ambiguous: bool = False


@dataclass(frozen=True)
class ContinuityIntent:
    """Evidence used independently from task-class selection."""

    prompt: str = ""
    explicit_addition: bool = False
    explicit_recovery: bool = False
    referenced_release_state: str = ""
    defect_found_during_active_ui: bool = False
    same_chat: bool = False
    same_functional_area: bool = False


def select_ui_runtime(
    *,
    execution_surface: str,
    playwright_available: bool,
    chromium_launchable: bool,
    repo_owned_recovery_available: bool,
    remediation_exhausted: bool = False,
    user_authority_required: bool = False,
) -> UiRuntimeDecision:
    """Select the UI runtime without treating an absent embedded Browser as evidence."""

    normalized_surface = execution_surface.strip().casefold()
    local_evidence: tuple[Mapping[str, Any], ...] = (
        {
            "kind": "ui-runtime-preflight",
            "execution_surface": normalized_surface,
            "playwright_available": playwright_available,
            "chromium_launchable": chromium_launchable,
            "repo_owned_recovery_available": repo_owned_recovery_available,
            "remediation_exhausted": remediation_exhausted,
            "user_authority_required": user_authority_required,
        },
    )
    if normalized_surface in {"codex-cli", "cli"}:
        if playwright_available and chromium_launchable:
            return UiRuntimeDecision(
                runtime=UiRuntime.LOCAL_PLAYWRIGHT,
                continue_ui_flow=True,
                external_blocker_eligible=False,
                reason_code="local-playwright-ready",
                evidence=local_evidence,
            )
        external_eligible = (
            not repo_owned_recovery_available
            and remediation_exhausted
            and user_authority_required
        )
        return UiRuntimeDecision(
            runtime=UiRuntime.LOCAL_PLAYWRIGHT,
            continue_ui_flow=False,
            external_blocker_eligible=external_eligible,
            reason_code=(
                "local-playwright-external-authority-required"
                if external_eligible
                else "local-playwright-recovery-required"
            ),
            evidence=local_evidence,
        )
    if normalized_surface in {"chatgpt-web", "chatgpt-desktop"}:
        return UiRuntimeDecision(
            runtime=UiRuntime.CHATGPT_EMBEDDED_BROWSER,
            continue_ui_flow=True,
            external_blocker_eligible=False,
            reason_code="chatgpt-embedded-browser-selected",
            evidence=local_evidence,
        )
    raise ValueError(f"unsupported execution surface: {execution_surface}")


def explicitly_requests_new_task(prompt: str) -> bool:
    normalized = " ".join(prompt.casefold().replace("ё", "е").split())
    return any(phrase in normalized for phrase in EXPLICIT_NEW_TASK_PHRASES)


def classify_continuity(
    intent: ContinuityIntent,
    *,
    task_class: TaskClass | None = None,
) -> TaskContinuity:
    """Classify identity continuity; ambiguity and terminal additions start fresh."""

    state = intent.referenced_release_state
    if explicitly_requests_new_task(intent.prompt):
        return TaskContinuity.NEW_TASK
    if intent.explicit_recovery:
        if state in TERMINAL_LABELS:
            return TaskContinuity.TERMINAL_STALE_REFERENCE
        if (
            task_class == TaskClass.LOOP
            and state == AWAITING_UI_LABEL
            and intent.defect_found_during_active_ui
        ):
            return TaskContinuity.ACTIVE_LOOP_RECOVERY
        return TaskContinuity.NEW_TASK
    if intent.explicit_addition and state in ACTIVE_STATE_LABELS:
        return TaskContinuity.ACTIVE_ADDITION
    return TaskContinuity.NEW_TASK


def explicit_task_class(prompt: str) -> TaskClass | None:
    first_line = prompt.splitlines()[0].strip() if prompt.strip() else ""
    return EXPLICIT_TASK_PROMPTS.get(first_line)


def classify_task(
    intent: TaskIntent,
    *,
    explicit: TaskClass | None = None,
    inherited: TaskClass | None = None,
) -> TaskClass:
    """Classify before work; explicit wins, additions inherit, ambiguity is STANDARD."""

    if explicit is not None:
        return explicit
    if inherited is not None:
        return inherited
    if intent.read_only and not any(
        (intent.deploy, intent.production_ui, intent.iterative, intent.ambiguous)
    ):
        return TaskClass.DIAGNOSTIC
    if intent.deploy and intent.production_ui and intent.iterative and not intent.ambiguous:
        return TaskClass.LOOP
    return TaskClass.STANDARD


def primary_states(labels: Iterable[str]) -> frozenset[str]:
    return frozenset(set(labels) & PRIMARY_STATE_LABELS)


def assert_state_invariants(labels: Iterable[str]) -> None:
    values = set(labels)
    primary = primary_states(values)
    if primary not in ALLOWED_PRIMARY_COMBINATIONS:
        raise ValueError("conflicting primary release states: " + ", ".join(sorted(primary)))
    if NEEDS_RESUME_LABEL in values and not primary & RESUMABLE_OWNER_LABELS:
        raise ValueError("release:needs-resume requires a resumable LOOP owner state")
    if NEEDS_RESUME_LABEL in values and "task:loop" not in values:
        raise ValueError("release:needs-resume requires task:loop")


def transition_allowed(current: str, target: str) -> bool:
    return current == target or target in TRANSITION_MATRIX.get(current, frozenset())
