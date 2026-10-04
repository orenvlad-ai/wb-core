"""Official analytics item-rating reader; no independent schedule or history fetch."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import threading
import time
from urllib import error, request as urllib_request

from packages.adapters.official_api_runtime import DEFAULT_WB_API_TOKEN_ENV, load_runtime_config
from packages.business_time import current_business_date_iso


class HttpBackedCardRatingSource:
    # Shared by in-process full/group refresh instances. Runtime admission owns
    # cross-process refresh exclusivity, as for the other API collectors.
    _request_lock = threading.Lock()
    _last_request_at = None

    def __init__(self, *, now_factory=None, page_limit=1000):
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        if not 1 <= page_limit <= 1000:
            raise ValueError("item-rating limit must be 1..1000")
        self.page_limit = page_limit

    def fetch(self, request):
        now = self.now_factory()
        today = current_business_date_iso(now)
        if request.snapshot_date != today:
            raise ValueError("item-rating is current-only; historical backfill forbidden")
        nm_ids = sorted(set(request.nm_ids))
        if any(type(nm_id) is not int or nm_id <= 0 for nm_id in nm_ids):
            raise ValueError("nmIds must be positive integers")
        runtime = load_runtime_config(
            token_env_var=DEFAULT_WB_API_TOKEN_ENV,
            default_base_url="https://seller-analytics-api.wildberries.ru",
            base_url_env_var="WB_SELLER_ANALYTICS_API_BASE_URL",
            default_timeout_seconds=30.0,
        )
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        items = []
        # Empty requested scope means no request, never nmIds:[] (all WB cards).
        for start in range(0, len(nm_ids), 50):
            batch = nm_ids[start:start + 50]
            offset = 0
            seen = set()
            while True:
                payload = self._post(runtime, {
                    "currentPeriod": {"start": yesterday, "end": yesterday},
                    "nmIds": batch,
                    "orderBy": {"field": "feedbackCount", "mode": "desc"},
                    "isNotIncludeNmsWithoutSales": False,
                    "onlyShadowedNms": False,
                    "limit": self.page_limit,
                    "offset": offset,
                })
                data = payload.get("data")
                page = data.get("items") if isinstance(data, dict) else None
                if not isinstance(page, list):
                    raise ValueError("item-rating response requires data.items")
                for item in page:
                    nm_id = item.get("nmId") if isinstance(item, dict) else None
                    if type(nm_id) is not int or nm_id not in batch or nm_id in seen:
                        raise ValueError("item-rating response has unexpected or duplicate nmId")
                    seen.add(nm_id)
                    items.append(item)
                if len(page) < self.page_limit:
                    break
                offset += len(page)
        if current_business_date_iso(self.now_factory()) != today:
            raise RuntimeError("item-rating crossed business-day boundary; candidate discarded")
        return {"snapshot_date": today, "observed_at": now.isoformat(),
                "requested_nm_ids": nm_ids, "data": {"items": items}}

    def _post(self, runtime, body):
        req = urllib_request.Request(
            runtime.base_url + "/api/analytics/v2/item-rating",
            data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Authorization": runtime.token, "Content-Type": "application/json"},
        )
        with self._request_lock:
            last = HttpBackedCardRatingSource._last_request_at
            if last is not None:
                delay = 20.0 - (time.monotonic() - last)
                if delay > 0:
                    time.sleep(delay)
            HttpBackedCardRatingSource._last_request_at = time.monotonic()
            try:
                with urllib_request.urlopen(req, timeout=runtime.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"), parse_float=Decimal)
            except error.HTTPError as exc:
                # No automatic retries; the common refresh acceptance handles failure.
                raise RuntimeError(f"WB item-rating HTTP {exc.code}") from exc
            except error.URLError as exc:
                raise RuntimeError("WB item-rating transport failed") from exc
