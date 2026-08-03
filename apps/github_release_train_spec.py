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


class ExecutionContour(str, Enum):
    """Technical boundary; only PR-backed contours receive GitHub scope labels."""

    READ_ONLY = "read-only"
    USER_ARTIFACT = "user-artifact"
    REPO_ONLY = "repo-only"
    LIVE_RUNTIME = "live/runtime"
    PRODUCTION_DATA_MUTATION = "production data mutation/backfill"
    ARCHIVED_GAS_GUARD = "archived GAS guard"


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
    CONTINUE_SAFE_PHASES = "CONTINUE_SAFE_PHASES"
    AWAIT_PHASE_CAPABILITY = "AWAIT_PHASE_CAPABILITY"
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
    SAFE_PHASES_REMAIN = "safe-phases-remain"
    PHASE_ACTION_READY = "phase-action-ready"
    PHASE_CAPABILITY_PREFLIGHT_REQUIRED = "phase-capability-preflight-required"
    PHASE_CAPABILITY_REMEDIATION_AVAILABLE = "phase-capability-remediation-available"
    PHASE_CAPABILITY_AWAITED = "phase-capability-awaited"
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
    PRODUCTION_MUTATION_TERMINALIZATION_AVAILABLE = (
        "production-mutation-terminalization-available"
    )
    EXTERNAL_AUTHORITY_REQUIRED = "external-authority-required"
    PROTOCOL_IRRECOVERABLE = "protocol-irrecoverable"


class GoalPhase(str, Enum):
    """Dependency-ordered Goal phases; production gates apply only to their phase."""

    REPOSITORY_PREFLIGHT = "REPOSITORY_PREFLIGHT"
    REPOSITORY_IMPLEMENTATION = "REPOSITORY_IMPLEMENTATION"
    REPOSITORY_VALIDATION = "REPOSITORY_VALIDATION"
    REPOSITORY_RUNNER_PREPARATION = "REPOSITORY_RUNNER_PREPARATION"
    PULL_REQUEST = "PULL_REQUEST"
    GITHUB_RELEASE = "GITHUB_RELEASE"
    RUNNER_DEPLOYMENT = "RUNNER_DEPLOYMENT"
    PRODUCTION_READ_PREFLIGHT = "PRODUCTION_READ_PREFLIGHT"
    PRODUCTION_MUTATION_PREFLIGHT = "PRODUCTION_MUTATION_PREFLIGHT"
    PRODUCTION_APPLY = "PRODUCTION_APPLY"
    PRODUCTION_RECONCILIATION = "PRODUCTION_RECONCILIATION"
    PRODUCTION_UI_PREFLIGHT = "PRODUCTION_UI_PREFLIGHT"
    PRODUCTION_UI_ACCEPTANCE = "PRODUCTION_UI_ACCEPTANCE"
    COMPLETE = "COMPLETE"


GOAL_PHASE_ORDER = tuple(GoalPhase)
class GoalCapability(str, Enum):
    """Capabilities are checked for a concrete phase, never as global availability."""

    REPOSITORY = "repository"
    GITHUB = "github"
    PRODUCTION_READ = "production-read"
    PRODUCTION_CREDENTIALS = "production-credentials"
    PRODUCTION_DATABASE = "production-database"
    PRODUCTION_MANIFEST = "production-manifest"
    PRODUCTION_DIGEST = "production-digest"
    PRODUCTION_BACKUP = "production-backup"
    PRODUCTION_MUTATION = "production-mutation"
    PRODUCTION_FILESYSTEM = "production-filesystem"
    ARBITRARY_SQL = "arbitrary-sql"
    RAW_EXPORT = "raw-export"
    BACKFILL = "backfill"
    LOCAL_PLAYWRIGHT = "local-playwright"
    PRODUCTION_UI_AUTH = "production-ui-auth"
    WEBCORE_DATA_MCP_READ = "webcore-data-mcp-read"


MCP_NEVER_PROVIDES = frozenset(
    {
        GoalCapability.PRODUCTION_MUTATION.value,
        GoalCapability.PRODUCTION_FILESYSTEM.value,
        GoalCapability.ARBITRARY_SQL.value,
        GoalCapability.RAW_EXPORT.value,
        GoalCapability.PRODUCTION_BACKUP.value,
        GoalCapability.BACKFILL.value,
    }
)

PRODUCTION_MUTATION_RUNNER_REQUIREMENTS = frozenset(
    {
        "repo_owned_runner",
        "dry_run_default",
        "explicit_apply_flag",
        "bounded_scope",
        "machine_readable_manifest",
        "pre_change_digest",
        "backup_evidence_contract",
        "expected_affected_records",
        "non_target_invariants",
        "idempotency_or_documented_recovery",
        "post_apply_readback",
        "reconciliation",
    }
)


def production_mutation_runner_contract(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Return machine-readable readiness for a canonical production-data runner."""

    missing = sorted(
        requirement
        for requirement in PRODUCTION_MUTATION_RUNNER_REQUIREMENTS
        if manifest.get(requirement) is not True
    )
    return {
        "valid": not missing,
        "missing_requirements": missing,
        "apply_allowed": not missing,
    }


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
    current_phase: GoalPhase = GoalPhase.GITHUB_RELEASE
    blocked_phase: GoalPhase | None = None
    safe_phases_remaining: tuple[GoalPhase, ...] = ()
    required_capability: str = ""
    capability_evidence: tuple[Mapping[str, Any], ...] = ()
    next_executable_action: str = ""

    def __post_init__(self) -> None:
        if self.own_pr <= 0 or self.action_pr <= 0:
            raise ValueError("Goal decision requires positive own_pr and action_pr")
        if not self.reason_code:
            raise ValueError("Goal decision requires a canonical reason code")
        if not self.canonical_github_state:
            raise ValueError("Goal decision requires canonical GitHub state")
        if self.next_executable_action != self.allowed_next_action:
            raise ValueError("next_executable_action must equal allowed_next_action")
        if self.blocked_phase is not None and self.blocked_phase != self.current_phase:
            raise ValueError("blocked_phase must be the immediate current phase")
        if self.disposition == GoalDisposition.CONTINUE_SAFE_PHASES:
            if not self.safe_phases_remaining or self.blocked_phase is not None:
                raise ValueError(
                    "CONTINUE_SAFE_PHASES requires executable safe phases and no current blocker"
                )
        if self.disposition == GoalDisposition.AWAIT_PHASE_CAPABILITY:
            if not (
                self.blocked_phase == self.current_phase
                and not self.safe_phases_remaining
                and self.required_capability
                and self.capability_evidence
                and self.user_intervention_required
                and self.remediation_exhausted
                and self.allowed_next_action
            ):
                raise ValueError(
                    "AWAIT_PHASE_CAPABILITY requires an immediate evidenced phase gate, "
                    "exhausted remediation and a human-only action"
                )
            if not phase_capability_evidence_sufficient(self.capability_evidence):
                raise ValueError(
                    "AWAIT_PHASE_CAPABILITY requires actual failed preflight attempts"
                )
            if any(
                bool(item.get("repo_owned_action_available"))
                for item in self.capability_evidence
            ):
                raise ValueError(
                    "AWAIT_PHASE_CAPABILITY is forbidden while repo-owned remediation is available"
                )
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
            if self.safe_phases_remaining:
                raise ValueError(
                    "EXTERNAL_BLOCKER is forbidden while safe executable phases remain"
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
        elif (
            self.disposition != GoalDisposition.AWAIT_PHASE_CAPABILITY
            and (self.user_intervention_required or self.remediation_exhausted)
        ):
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
            "current_phase": self.current_phase.value,
            "blocked_phase": self.blocked_phase.value if self.blocked_phase else None,
            "safe_phases_remaining": [phase.value for phase in self.safe_phases_remaining],
            "required_capability": self.required_capability,
            "capability_evidence": [dict(item) for item in self.capability_evidence],
            "next_executable_action": self.next_executable_action,
        }


@dataclass(frozen=True)
class GoalPhaseContext:
    """Current dependency state supplied to the canonical Goal shepherd."""

    current_phase: GoalPhase
    safe_phases_remaining: tuple[GoalPhase, ...] = ()
    required_capability: str = ""
    capability_available: bool = True
    capability_evidence: tuple[Mapping[str, Any], ...] = ()
    repo_owned_remediation_available: bool = False
    remediation_exhausted: bool = False
    user_intervention_required: bool = False
    next_executable_action: str = ""
    minimal_user_action: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "GoalPhaseContext":
        raw_safe = payload.get("safe_phases_remaining") or []
        if not isinstance(raw_safe, list):
            raise ValueError("safe_phases_remaining must be a JSON array")
        raw_evidence = payload.get("capability_evidence") or []
        if not isinstance(raw_evidence, list) or any(
            not isinstance(item, Mapping) for item in raw_evidence
        ):
            raise ValueError("capability_evidence must be an array of JSON objects")
        return cls(
            current_phase=GoalPhase(str(payload.get("current_phase") or "")),
            safe_phases_remaining=order_goal_phases(
                GoalPhase(str(item)) for item in raw_safe
            ),
            required_capability=str(payload.get("required_capability") or ""),
            capability_available=payload.get("capability_available") is True,
            capability_evidence=tuple(dict(item) for item in raw_evidence),
            repo_owned_remediation_available=(
                payload.get("repo_owned_remediation_available") is True
            ),
            remediation_exhausted=payload.get("remediation_exhausted") is True,
            user_intervention_required=(
                payload.get("user_intervention_required") is True
            ),
            next_executable_action=str(payload.get("next_executable_action") or ""),
            minimal_user_action=str(payload.get("minimal_user_action") or ""),
        )


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


def order_goal_phases(phases: Iterable[GoalPhase]) -> tuple[GoalPhase, ...]:
    """Return unique phases in dependency order, independent of prompt ordering."""

    requested = set(phases)
    return tuple(phase for phase in GOAL_PHASE_ORDER if phase in requested)


def mcp_capability_sufficient(
    required_capability: GoalCapability | str,
    allowlist: Iterable[GoalCapability | str],
) -> bool:
    """Check the concrete read capability; MCP is never generic production access."""

    required = (
        required_capability.value
        if isinstance(required_capability, GoalCapability)
        else str(required_capability)
    )
    allowed = {
        item.value if isinstance(item, GoalCapability) else str(item) for item in allowlist
    }
    return required not in MCP_NEVER_PROVIDES and required in allowed


def production_evidence_route(
    required_capability: GoalCapability | str,
    *,
    mcp_allowlist: Iterable[GoalCapability | str],
) -> str:
    """Select the type-appropriate evidence path without assuming MCP sufficiency."""

    return (
        "webcore-data-mcp"
        if mcp_capability_sufficient(required_capability, mcp_allowlist)
        else "repo-owned-runner"
    )


def phase_capability_evidence_sufficient(
    evidence: Iterable[Mapping[str, Any]],
) -> bool:
    """Require an actual failed attempt, not a capability word or assumption."""

    items = tuple(evidence)
    return bool(items) and not any(
        item.get("repo_owned_action_available") is True for item in items
    ) and any(
        isinstance(item.get("attempts"), list)
        and bool(item.get("attempts"))
        and item.get("repo_owned_action_available") is False
        for item in items
    )


def phase_goal_decision(
    context: GoalPhaseContext,
    *,
    own_pr: int,
    action_pr: int,
    canonical_github_state: Mapping[str, Any],
) -> GoalDecision | None:
    """Classify the current phase before Release Train state is interpreted.

    ``None`` delegates to the Release Train classifier because the current phase
    is itself the GitHub release phase (or the declared Goal is complete).
    """

    if context.safe_phases_remaining:
        action = context.next_executable_action or (
            f"execute {context.safe_phases_remaining[0].value} before any production gate"
        )
        return GoalDecision(
            disposition=GoalDisposition.CONTINUE_SAFE_PHASES,
            own_pr=own_pr,
            action_pr=action_pr,
            canonical_github_state=canonical_github_state,
            reason_code=GoalReasonCode.SAFE_PHASES_REMAIN.value,
            allowed_next_action=action,
            user_intervention_required=False,
            evidence=(
                {
                    "kind": "phase-dependency-plan",
                    "current_phase": context.current_phase.value,
                    "future_capability_available": context.capability_available,
                    "repo_owned_action_available": True,
                },
            ),
            remediation_exhausted=False,
            current_phase=context.current_phase,
            blocked_phase=None,
            safe_phases_remaining=context.safe_phases_remaining,
            required_capability=context.required_capability,
            capability_evidence=context.capability_evidence,
            next_executable_action=action,
        )

    if context.current_phase in {GoalPhase.GITHUB_RELEASE, GoalPhase.COMPLETE} and not (
        context.required_capability and not context.capability_available
    ):
        return None

    if not context.required_capability or context.capability_available:
        action = context.next_executable_action or f"execute {context.current_phase.value}"
        return GoalDecision(
            disposition=GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=action_pr,
            canonical_github_state=canonical_github_state,
            reason_code=GoalReasonCode.PHASE_ACTION_READY.value,
            allowed_next_action=action,
            user_intervention_required=False,
            evidence=(
                {
                    "kind": "phase-capability",
                    "current_phase": context.current_phase.value,
                    "required_capability": context.required_capability,
                    "capability_available": context.capability_available,
                    "repo_owned_action_available": True,
                },
            ),
            remediation_exhausted=False,
            current_phase=context.current_phase,
            blocked_phase=None,
            safe_phases_remaining=(),
            required_capability=context.required_capability,
            capability_evidence=context.capability_evidence,
            next_executable_action=action,
        )

    if context.repo_owned_remediation_available or not (
        phase_capability_evidence_sufficient(context.capability_evidence)
        and context.remediation_exhausted
    ):
        reason = (
            GoalReasonCode.PHASE_CAPABILITY_REMEDIATION_AVAILABLE
            if context.repo_owned_remediation_available
            else GoalReasonCode.PHASE_CAPABILITY_PREFLIGHT_REQUIRED
        )
        action = context.next_executable_action or (
            f"run repo-owned {context.current_phase.value} capability preflight/remediation"
        )
        return GoalDecision(
            disposition=GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=action_pr,
            canonical_github_state=canonical_github_state,
            reason_code=reason.value,
            allowed_next_action=action,
            user_intervention_required=False,
            evidence=(
                {
                    "kind": "phase-capability",
                    "current_phase": context.current_phase.value,
                    "required_capability": context.required_capability,
                    "capability_available": False,
                    "repo_owned_action_available": True,
                },
                *context.capability_evidence,
            ),
            remediation_exhausted=False,
            current_phase=context.current_phase,
            blocked_phase=None,
            safe_phases_remaining=(),
            required_capability=context.required_capability,
            capability_evidence=context.capability_evidence,
            next_executable_action=action,
        )

    if not context.user_intervention_required or not context.minimal_user_action:
        action = context.next_executable_action or (
            f"record the exact {context.required_capability} capability boundary and owner"
        )
        return GoalDecision(
            disposition=GoalDisposition.OWN_ACTION,
            own_pr=own_pr,
            action_pr=action_pr,
            canonical_github_state=canonical_github_state,
            reason_code=GoalReasonCode.PHASE_CAPABILITY_PREFLIGHT_REQUIRED.value,
            allowed_next_action=action,
            user_intervention_required=False,
            evidence=context.capability_evidence,
            remediation_exhausted=False,
            current_phase=context.current_phase,
            blocked_phase=None,
            safe_phases_remaining=(),
            required_capability=context.required_capability,
            capability_evidence=context.capability_evidence,
            next_executable_action=action,
        )

    return GoalDecision(
        disposition=GoalDisposition.AWAIT_PHASE_CAPABILITY,
        own_pr=own_pr,
        action_pr=action_pr,
        canonical_github_state=canonical_github_state,
        reason_code=GoalReasonCode.PHASE_CAPABILITY_AWAITED.value,
        allowed_next_action=context.minimal_user_action,
        user_intervention_required=True,
        evidence=context.capability_evidence,
        remediation_exhausted=True,
        current_phase=context.current_phase,
        blocked_phase=context.current_phase,
        safe_phases_remaining=(),
        required_capability=context.required_capability,
        capability_evidence=context.capability_evidence,
        next_executable_action=context.minimal_user_action,
    )


EXPLICIT_TASK_PROMPTS = {
    "КЛАСС ЗАДАЧИ: СТАНДАРТ": TaskClass.STANDARD,
    "КЛАСС ЗАДАЧИ: LOOP": TaskClass.LOOP,
    "КЛАСС ЗАДАЧИ: ДИАГНОСТИКА": TaskClass.DIAGNOSTIC,
}

READY_LABEL = "release:ready"
STAGED_LABEL = "release:staged"
RUNNING_LABEL = "release:running"
AWAITING_AGENT_LABEL = "release:awaiting-agent"
AWAITING_UI_LABEL = "release:awaiting-ui"
NEEDS_RESUME_LABEL = "release:needs-resume"
BLOCKED_LABEL = "release:blocked"
HALTED_LABEL = "release:halted"
DONE_LABEL = "release:done"
PRODUCTION_LABEL = "release:production"
SUPERSEDED_LABEL = "release:superseded"
RETIRED_LABEL = "release:retired"
RELEASE_LANE_OWNER_LABEL = "release:lane-owner"

ACTIVE_PRIMARY_LABELS = frozenset(
    {
        READY_LABEL,
        STAGED_LABEL,
        RUNNING_LABEL,
        AWAITING_AGENT_LABEL,
        AWAITING_UI_LABEL,
        BLOCKED_LABEL,
        HALTED_LABEL,
    }
)
OVERLAY_LABELS = frozenset({NEEDS_RESUME_LABEL})
TERMINAL_LABELS = frozenset(
    {DONE_LABEL, PRODUCTION_LABEL, SUPERSEDED_LABEL, RETIRED_LABEL}
)
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
    "release:none": frozenset({READY_LABEL, STAGED_LABEL}),
    STAGED_LABEL: frozenset({READY_LABEL, BLOCKED_LABEL, RETIRED_LABEL}),
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
    BLOCKED_LABEL: frozenset(
        {READY_LABEL, STAGED_LABEL, PRODUCTION_LABEL, SUPERSEDED_LABEL, RETIRED_LABEL}
    ),
    HALTED_LABEL: frozenset({AWAITING_UI_LABEL, PRODUCTION_LABEL, SUPERSEDED_LABEL}),
    DONE_LABEL: frozenset(),
    PRODUCTION_LABEL: frozenset(),
    SUPERSEDED_LABEL: frozenset(),
    RETIRED_LABEL: frozenset(),
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
        (BLOCKED_LABEL, PRODUCTION_LABEL),
        (BLOCKED_LABEL, RETIRED_LABEL),
    }
)

CANONICAL_MONITOR_QUERY = (
    'is:pr -label:release:superseded '
    'label:"release:staged,release:ready,release:running,release:awaiting-agent,release:awaiting-ui,'
    'release:needs-resume,release:blocked,release:halted,'
    'release:lane-owner,finance:migration-deploy-lease" sort:created-asc'
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
PRODUCTION_MUTATION_COMPLETION_PROOF_MARKER = (
    "wb-core-production-mutation-completion-proof"
)
FINANCE_DEPLOY_LEASE_BINDING_PROOF_MARKER = (
    "wb-core-finance-migration-deploy-lease-binding"
)
FINANCE_DEPLOY_LEASE_RECOVERY_PROOF_MARKER = (
    "wb-core-finance-migration-deploy-lease-recovery"
)
FINANCE_DEPLOY_LEASE_TERMINAL_PROOF_MARKER = (
    "wb-core-finance-migration-deploy-lease-terminal"
)
HALT_PROOF_MARKER = "wb-core-release-halt-proof"
RETRY_PROOF_MARKER = "wb-core-release-retry-proof"
NEW_ROOT_PROOF_MARKER = "wb-core-loop-new-root-proof"
RECOVERY_PROOF_MARKER = "wb-core-loop-recovery-proof"
CLASSIFICATION_BLOCKER_MARKER = "wb-core-loop-classification-blocker"
IDENTITY_CORRECTION_PROOF_MARKER = "wb-core-loop-identity-correction-proof"
ORCHESTRATION_ADMISSION_PROOF_MARKER = "wb-core-orchestration-admission-proof"
RELEASE_LANE_PROOF_MARKER = "wb-core-release-lane-proof"
LEGACY_RETIREMENT_PROOF_MARKER = "wb-core-legacy-retirement-proof"
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
    user_artifact: bool = False
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
    if intent.user_artifact:
        return TaskClass.STANDARD
    if intent.read_only and not any(
        (intent.deploy, intent.production_ui, intent.iterative, intent.ambiguous)
    ):
        return TaskClass.DIAGNOSTIC
    if intent.deploy and intent.production_ui and intent.iterative and not intent.ambiguous:
        return TaskClass.LOOP
    return TaskClass.STANDARD


def github_closure_required(task_class: TaskClass, contour: ExecutionContour) -> bool:
    """Return whether this task must create a PR and enter the Release Train."""

    if contour == ExecutionContour.USER_ARTIFACT:
        if task_class != TaskClass.STANDARD:
            raise ValueError("user-artifact contour requires STANDARD task class")
        return False
    if task_class == TaskClass.DIAGNOSTIC:
        if contour != ExecutionContour.READ_ONLY:
            raise ValueError("DIAGNOSTIC task class requires read-only contour")
        return False
    return True


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
