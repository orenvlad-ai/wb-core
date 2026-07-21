"""Stable contracts for the server-native WB autoanswers contour.

The frozen AI bundle remains the semantic owner for prompts, schemas, routing,
draft guards and fallbacks.  This module owns only server orchestration states,
permissions and versioned Python <-> Node envelopes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


AUTOANSWERS_CONTRACT_VERSION: Final = "wb_autoanswers_server_v2"
NODE_BOUNDARY_VERSION: Final = "wb_autoanswers_node_boundary_v1"
PROMPT_BUNDLE_VERSION: Final = "1.4.2"
EVALUATION_SIGNATURE: Final = "sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da"
BACKFILL_FROM_DATE: Final = "2026-01-01"

MODE_DRAFT_ONLY: Final = "draft_only"
MODE_MANUAL: Final = "manual"
MODE_AUTO_SAFE: Final = "auto_safe"
MODE_AUTO_ALL: Final = "auto_all"
AUTOANSWER_MODES: Final = (MODE_MANUAL, MODE_DRAFT_ONLY, MODE_AUTO_SAFE, MODE_AUTO_ALL)

MASTER_OFF: Final = "off"
MASTER_ON: Final = "on"

PERMISSION_VIEW: Final = "feedbacks"
PERMISSION_AI_REVIEW: Final = "feedbacks.ai_review"
PERMISSION_ADMIN: Final = "feedbacks.autoanswers_admin"

AUTO_SAFE_ROUTES: Final = frozenset({"public_only", "wb_return", "wb_support", "rating_only_template"})
REVIEW_ONLY_ROUTES: Final = frozenset({"seller_chat"})
ROUTE_RATING_ONLY_TEMPLATE: Final = "rating_only_template"
PROCESSING_KIND_FROZEN_AI: Final = "frozen_ai"
PROCESSING_KIND_RATING_ONLY_TEMPLATE: Final = "rating_only_template"

STATE_DISCOVERED: Final = "discovered"
STATE_SYNCED: Final = "synced"
STATE_QUEUED: Final = "queued"
STATE_PROCESSING: Final = "processing"
STATE_GENERATED: Final = "generated"
STATE_NEEDS_REVIEW: Final = "needs_review"
STATE_APPROVED: Final = "approved"
STATE_PUBLISHING: Final = "publishing"
STATE_PUBLISH_PENDING_READBACK: Final = "publish_pending_readback"
STATE_PUBLISHED: Final = "published"
STATE_SKIPPED: Final = "skipped"
STATE_RETRYABLE_ERROR: Final = "retryable_error"
STATE_TERMINAL_ERROR: Final = "terminal_error"

PROCESSING_STATES: Final = (
    STATE_DISCOVERED,
    STATE_SYNCED,
    STATE_QUEUED,
    STATE_PROCESSING,
    STATE_GENERATED,
    STATE_NEEDS_REVIEW,
    STATE_APPROVED,
    STATE_PUBLISHING,
    STATE_PUBLISH_PENDING_READBACK,
    STATE_PUBLISHED,
    STATE_SKIPPED,
    STATE_RETRYABLE_ERROR,
    STATE_TERMINAL_ERROR,
)

TERMINAL_STATES: Final = frozenset({STATE_PUBLISHED, STATE_SKIPPED, STATE_TERMINAL_ERROR})

ALLOWED_TRANSITIONS: Final = {
    STATE_DISCOVERED: frozenset({STATE_SYNCED, STATE_RETRYABLE_ERROR, STATE_TERMINAL_ERROR}),
    STATE_SYNCED: frozenset({STATE_QUEUED, STATE_SKIPPED, STATE_RETRYABLE_ERROR, STATE_TERMINAL_ERROR}),
    STATE_QUEUED: frozenset({STATE_PROCESSING, STATE_NEEDS_REVIEW, STATE_SKIPPED, STATE_RETRYABLE_ERROR}),
    STATE_PROCESSING: frozenset(
        {STATE_GENERATED, STATE_NEEDS_REVIEW, STATE_SKIPPED, STATE_RETRYABLE_ERROR, STATE_TERMINAL_ERROR}
    ),
    STATE_GENERATED: frozenset({STATE_NEEDS_REVIEW, STATE_APPROVED, STATE_TERMINAL_ERROR}),
    STATE_NEEDS_REVIEW: frozenset({STATE_APPROVED, STATE_SKIPPED, STATE_TERMINAL_ERROR}),
    STATE_APPROVED: frozenset({STATE_PUBLISHING, STATE_NEEDS_REVIEW, STATE_SKIPPED}),
    STATE_PUBLISHING: frozenset({STATE_PUBLISH_PENDING_READBACK, STATE_RETRYABLE_ERROR, STATE_TERMINAL_ERROR}),
    STATE_PUBLISH_PENDING_READBACK: frozenset(
        {STATE_PUBLISHED, STATE_RETRYABLE_ERROR, STATE_NEEDS_REVIEW, STATE_TERMINAL_ERROR}
    ),
    STATE_RETRYABLE_ERROR: frozenset(
        {
            STATE_SYNCED,
            STATE_QUEUED,
            STATE_PROCESSING,
            STATE_APPROVED,
            STATE_PUBLISH_PENDING_READBACK,
            STATE_TERMINAL_ERROR,
        }
    ),
    STATE_PUBLISHED: frozenset(),
    STATE_SKIPPED: frozenset(),
    STATE_TERMINAL_ERROR: frozenset(),
}


@dataclass(frozen=True)
class AutoanswersSettings:
    master_enabled: bool
    force_off: bool
    effective_enabled: bool
    mode: str
    enable_epoch: int
    policy_epoch: int
    enabled_at: str | None
    daily_cap_usd: float
    monthly_cap_usd: float
    hourly_cap_usd: float
    max_paid_reviews_per_hour: int
    global_paid_review_concurrency: int
    max_inflight_role_calls: int
    max_materialized_processing_jobs: int
    warning_ratio: float
    max_reservation_per_review_usd: float
    policy_version: str
    updated_at: str


def validate_mode(mode: str) -> str:
    normalized = str(mode or "").strip()
    if normalized not in AUTOANSWER_MODES:
        raise ValueError(f"unsupported autoanswers mode: {normalized or '<empty>'}")
    return normalized


def assert_transition(previous: str, next_state: str) -> None:
    if previous not in PROCESSING_STATES:
        raise ValueError(f"unknown previous state: {previous}")
    if next_state not in ALLOWED_TRANSITIONS[previous]:
        raise ValueError(f"invalid autoanswers transition: {previous} -> {next_state}")


def processing_key(feedback_id: str, content_version: int) -> str:
    normalized_id = str(feedback_id or "").strip()
    if not normalized_id or int(content_version) < 1:
        raise ValueError("feedback_id and positive content_version are required")
    return f"{normalized_id}|{int(content_version)}|{PROMPT_BUNDLE_VERSION}"


def publication_key(feedback_id: str, content_version: int, final_reply_sha256: str) -> str:
    digest = str(final_reply_sha256 or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise ValueError("final_reply_sha256 must be a lowercase SHA-256 hex digest")
    return f"{feedback_id}|{int(content_version)}|{digest}|create-answer-v1"
