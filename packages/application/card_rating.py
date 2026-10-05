"""Normalize only feedbackRating.current, preserving all WB float64 precision."""
from decimal import Decimal
import math

from packages.contracts.card_rating import CardRatingEnvelope, CardRatingItem, CardRatingSnapshot
from packages.application.sheet_vitrina_v1_card_rating import normalize_request_period


class CardRatingBlock:
    def __init__(self, source):
        self.source = source

    def execute(self, request):
        payload = self.source.fetch(request)
        items = []
        requested = set(request.nm_ids)
        seen = set()
        for item in payload["data"]["items"]:
            nm_id = item["nmId"]
            if type(nm_id) is not int or nm_id not in requested or nm_id in seen:
                raise ValueError("unexpected or duplicate item-rating nmId")
            seen.add(nm_id)
            rating = item.get("feedbackRating") or {}
            raw = rating.get("current")
            value = None
            raw_text = None
            if raw is not None:
                if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal)):
                    raise ValueError("feedbackRating.current must be numeric or null")
                number = float(raw)
                if not math.isfinite(number) or not 0 <= number <= 5:
                    raise ValueError("feedbackRating.current must be finite within 0..5")
                raw_text = str(raw)
                # Zero means no usable rating in this report, even for rated cards.
                value = number if number > 0 else None
            items.append(CardRatingItem(nm_id, value, raw_text, payload["observed_at"]))
        available = {item.nm_id for item in items if item.card_rating is not None}
        return CardRatingEnvelope(CardRatingSnapshot(
            kind="success", snapshot_date=payload["snapshot_date"], items=items,
            requested_count=len(requested), covered_count=len(available),
            missing_nm_ids=sorted(requested - available), observed_at=payload["observed_at"],
            request_period=normalize_request_period(payload.get("request_period")),
            request_period_policy=payload.get("request_period_policy"),
            source_endpoint=payload.get("source_endpoint"), source_field=payload.get("source_field"),
        ))
