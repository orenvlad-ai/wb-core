"""WB review-report observations, distinct from content/seller ratings."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CardRatingRequest:
    snapshot_date: str
    nm_ids: list[int]


@dataclass(frozen=True)
class CardRatingItem:
    nm_id: int
    card_rating: float | None
    # Keep the exact JSON decimal spelling as well as the API's float64 value.
    card_rating_raw: str | None
    observed_at: str = ""


@dataclass(frozen=True)
class CardRatingSnapshot:
    kind: str
    snapshot_date: str
    items: list[CardRatingItem]
    requested_count: int
    covered_count: int
    missing_nm_ids: list[int]
    observed_at: str
    request_period: dict[str, str] | None = None
    request_period_policy: str | None = None
    source_endpoint: str | None = None
    source_field: str | None = None
    detail: str = "WB item-rating report feedbackRating.current; dated report observation"


@dataclass(frozen=True)
class CardRatingEnvelope:
    result: CardRatingSnapshot
