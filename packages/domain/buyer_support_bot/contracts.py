"""Serializable contracts. No network, database, application or production imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

POLICY_VERSION = "wbc0115.chat.2026-10-04.v1"
TOPICS = ("general", "fracture", "bubbles", "dust", "edge", "tab", "film", "size", "supplies", "missing_glass", "privacy", "privacy_dark", "matte", "marks", "touch", "camera", "faceid", "alignment", "frame", "case", "delivery", "payment", "giveaway", "compensation", "product", "instruction", "wrong_item", "opened_used", "scratch", "earpiece", "dangerous_edge", "injury", "display", "other")
FACT_VALUES = {
    "topic": TOPICS,
    "stage": ("before_use", "installation", "initial_inspection", "in_use", "unknown"),
    "advice_given": ("bubbles", "dust", "edge", "supplies", "brightness", "wipe", "touch_clean", "camera_clean", "faceid_clean", "without_case", "display_clean", "earpiece_check"),
    "correction": ("stage", "phone_model", "privacy_effect", "fit_kind"),
    "advice_status": ("not_tried", "tried_failed", "refused", "tried_success", "unknown"),
    "phone_model": None,
    "bubble_type": ("air", "air_small", "air_large", "dust", "unknown"),
    "missing_sticker": ("true", "false", "unknown"),
    "cleaning_option": ("microfibre", "wet_dry_wipes", "own_soft_cloth", "unknown"),
    "privacy_effect": ("partial", "absent_from_start", "unknown"),
    "marks_kind": ("wipeable", "persistent_from_start", "late_wear", "unknown"),
    "coating_kind": ("light_grain", "uneven", "absent", "unknown"),
    "display_kind": ("persistent_artifact", "subjective_discomfort", "unknown"),
    "edge_kind": ("dangerous", "subjective", "unknown"),
    "detail_request_kind": ("problem_detail", "fracture_stage", "phone_model", "selection_condition", "onset", "remaining_supplies", "privacy_manifestation", "coating_manifestation", "marks_manifestation", "bubble_kind"),
    "fit_kind": ("normal_gap", "real_mismatch", "content_overlap", "subjective_frame", "unknown"),
    "pristine": ("true", "false", "unknown"),
    "return_requested": ("true", "false", "unknown"),
    "claim_reported": ("true", "false", "unknown"),
    "claim_number": None,
    "resolved": ("true", "false", "unknown"),
    "cannot_photo": ("true", "false", "unknown"),
    "photo_requested": ("true",),
    "detail_requested": ("true",),
    "contradiction_asked": ("true",),
    "claim_number_requested": ("true",),
    "claim_location_explained": ("true",),
    "refusal_explained": ("true",),
    "review_requested": ("true",),
    "greeted": ("true",),
    "manager_requested": ("true", "false"),
    "direct_insult": ("true", "false"),
    "substantive": ("true", "false"),
    "compensation_materials": ("true", "false", "unknown"),
    "historical_obligation": ("replacement_glass", "compensation", "unknown"),
    "installation_age": ("under_day", "over_day", "unknown"),
}
HISTORICAL_KEYS = frozenset({"photo_requested", "detail_requested", "contradiction_asked", "claim_number_requested", "claim_location_explained", "refusal_explained", "review_requested", "greeted", "historical_obligation", "advice_given", "detail_request_kind"})
COUNTER_KEYS = {"photo_requested": "photo_requests", "detail_requested": "detail_requests", "contradiction_asked": "contradiction_requests", "refusal_explained": "refusal_replies"}


@dataclass(frozen=True)
class Evidence:
    event_id: str
    quote: str


@dataclass(frozen=True)
class Fact:
    issue_id: str
    key: str
    value: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class Event:
    event_id: str
    role: str
    text: str
    at: str = ""
    attachments: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PhotoObservation:
    attachment_id: str
    issue_id: str
    task: str
    result: str  # suitable / irrelevant / contradiction / unassessable / unavailable
    evidence_event_id: str
    checked: bool = False  # supplied by an image evaluator, never attachment presence


@dataclass(frozen=True)
class ClaimSnapshot:
    availability: str = "unknown"  # unknown / absent / error / present
    claim_id: str = ""
    status: str = "unknown"  # pending / approved / rejected / unknown
    linked: bool = False
    fresh: bool = False
    source: str = "unknown"  # authoritative_api / simulation / unknown
    actions: tuple[str, ...] = ()
    return_method: str = "unknown"  # keep_goods / return_goods / unknown
    rejection_reason: str = ""
    deadline_at: str = ""
    deadline_verified: bool = False
    safety_margin_seconds: int | None = None


@dataclass(frozen=True)
class ReviewSnapshot:
    linked: bool = False
    fresh: bool = False
    negative: bool | None = None
    source: str = "unknown"


@dataclass(frozen=True)
class Context:
    claim: ClaimSnapshot = field(default_factory=ClaimSnapshot)
    review: ReviewSnapshot = field(default_factory=ReviewSnapshot)
    chat_available: bool = True
    return_discussed: bool = False
    compatibility: str = "unknown"  # verified_match / verified_mismatch / unknown
    received_matches_order: bool | None = None
    product_line: str = "unknown"  # privacy / matte / clear / unknown
    product_verified: bool = False
    now: str = ""
    timer_event: bool = False


@dataclass
class IssueState:
    facts: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    advice_given: set[str] = field(default_factory=set)
    waiting_since: str = ""
    last_action: str = ""


@dataclass
class OperationIntent:
    operation_id: str
    claim_id: str
    action: str
    state: str = "prepared"
    simulated: bool = True
    expected_claim_status: str = "pending"
    case_revision: int = 0


@dataclass
class CaseState:
    case_id: str
    policy_version: str = POLICY_VERSION
    revision: int = 0
    issues: dict[str, IssueState] = field(default_factory=dict)
    processed_events: dict[str, str] = field(default_factory=dict)
    received_materials: dict[str, dict[str, Any]] = field(default_factory=dict)
    observations: list[PhotoObservation] = field(default_factory=list)
    operations: dict[str, OperationIntent] = field(default_factory=dict)
    greeted: bool = False
    review_requested: bool = False
    claim_number_requested: bool = False
    claim_location_explained: bool = False
    insult_count: int = 0
    last_substantive: bool = True
    phase: str = "received"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for issue in value["issues"].values():
            issue["advice_given"] = sorted(issue["advice_given"])
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CaseState:
        data = dict(value)
        data["issues"] = {key: IssueState(**{**item, "advice_given": set(item["advice_given"])}) for key, item in data.get("issues", {}).items()}
        data["observations"] = [PhotoObservation(**item) for item in data.get("observations", [])]
        data["operations"] = {key: OperationIntent(**item) for key, item in data.get("operations", {}).items()}
        if data.get("policy_version", POLICY_VERSION) != POLICY_VERSION:
            raise ValueError("state policy version differs; explicit migration required")
        return cls(**data)


@dataclass(frozen=True)
class Decision:
    action: str
    rule: str
    issue_id: str = ""
    method: str = "unknown"
    missing: tuple[str, ...] = ()
    template: str = ""
    operation: str = ""
    unavailable: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION
    facts_used: tuple[str, ...] = ()
    secondary_unavailable: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parsed_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone required")
    return result
