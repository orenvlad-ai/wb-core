"""Machine-readable contract shared by the wb-core Release Train surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable
from urllib.parse import quote_plus


class TaskClass(str, Enum):
    STANDARD = "standard"
    LOOP = "loop"
    DIAGNOSTIC = "diagnostic"


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
CANONICAL_PRODUCTION_TARGET_ID = "wb_core_eu_hosted_runtime_active"


@dataclass(frozen=True)
class TaskIntent:
    read_only: bool = False
    deploy: bool = False
    production_ui: bool = False
    iterative: bool = False
    ambiguous: bool = False


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
