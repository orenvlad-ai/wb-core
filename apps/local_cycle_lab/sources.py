"""Deterministic provider inputs for the offline WBC bench. No HTTP fallback."""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
import json

from packages.adapters.wb_finance_api import FinanceHttpResult
from packages.contracts.promo_xlsx_collector_block import CollectorRunSummary


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def render(value, day, start=None, end=None):
    """Expand fixture dates only in string values, never coerce missing values."""
    if isinstance(value, dict):
        return {key: render(item, day, start, end) for key, item in value.items()}
    if isinstance(value, list):
        return [render(item, day, start, end) for item in value]
    if isinstance(value, str) and "$" in value:
        d = date.fromisoformat(day)
        return (value.replace("$day", day).replace("$start", start or day)
                .replace("$end", end or day).replace("$rating_start", (d - timedelta(days=7)).isoformat())
                .replace("$yesterday", (d - timedelta(days=1)).isoformat()))
    return value


class Replay:
    def __init__(self, scenario, variant):
        self.scenario = scenario
        self.variant = variant
        self.current_day = scenario["today"]
        self.nm_ids = [item["nm_id"] for item in scenario["catalog"] if item["enabled"]]
        self.totals = {}
        self.boundaries = {}
        self.phase = "setup"
        self.phase_totals = {}
        self.unexpected = []

    @contextmanager
    def on_day(self, day):
        previous = self.current_day
        self.current_day = day
        try:
            yield
        finally:
            self.current_day = previous

    def reject(self, detail):
        self.unexpected.append(detail)
        raise ValueError(f"Запрос вне тестового сценария: {detail}")

    def observe(self, key, payload, rows, *, pages=0, boundary="application_input", request_bytes=0):
        counter = self.totals.setdefault(key, Counter())
        values = dict(calls=1, pages=pages, rows=rows, payload_json_bytes=encoded_size(payload),
                      request_body_bytes=request_bytes)
        counter.update(values)
        self.phase_totals.setdefault(self.phase, Counter()).update(values)
        self.boundaries[key] = boundary
        return payload

    def source(self, key):
        return InputSource(self, key)

    def check_day(self, day):
        if day not in self.scenario["days"] + self.scenario["bootstrap_days"]:
            self.reject(f"date={day}")

    def ensure_snapshot(self, snapshot_date):
        self.check_day(snapshot_date)

    def ensure_closed_day_snapshot(self, *, source_key, snapshot_date):
        self.check_day(snapshot_date)
        if source_key not in {"web_source_snapshot", "seller_funnel_snapshot"}:
            self.reject(source_key)

    def execute(self, request):
        """Stand in for the portal browser, keep the native promo archive/materializer."""
        campaigns = self.scenario["promo_campaigns"]
        if campaigns:
            self.reject("promo XLSX fixture required for non-empty campaigns")
        self.observe("promo_by_price", campaigns, 0, boundary="collector_result")
        return CollectorRunSummary(run_dir=request.output_root, status="success",
                                   started_at=self.scenario["today"] + "T08:00:00Z")

    def finance_request(self, payload):
        period = payload["period"]
        start, end = payload["dateFrom"], payload["dateTo"]
        if period == "daily" and start == end:
            self.check_day(start)
        elif period != "weekly" or [start, end] != self.scenario["finance_week"]:
            self.reject(f"finance {period} {start}..{end}")
        cursor = payload["rrdId"]
        records = self.scenario["finance_rows"]
        cursors = [0, *(row["rrdId"] for row in records)]
        if cursor not in cursors or payload["limit"] < 1:
            self.reject(f"finance cursor={cursor}")
        index = cursors.index(cursor)
        page = render(records[index:index + 1], end)
        status = 200 if page else 204
        self.observe("finance_" + period, page, len(page), pages=1,
                     boundary="finance_transport", request_bytes=encoded_size(payload))
        return FinanceHttpResult(status, page, {})


class InputSource:
    """Input to a real application block, keyed by requested period and catalog."""
    def __init__(self, replay, key):
        self.replay = replay
        self.key = key

    def fetch(self, request):
        replay = self.replay
        scenario = replay.scenario
        start = (getattr(request, "date_from", None) or getattr(request, "snapshot_date", None)
                 or getattr(request, "date", None))
        end = getattr(request, "date_to", None) or start
        if start == end:
            replay.check_day(start)
        elif self.key != "sales_funnel_history" or [start, end] not in scenario["history_ranges"]:
            replay.reject(f"{self.key} {start}..{end}")
        if hasattr(request, "nm_ids") and sorted(request.nm_ids) != sorted(replay.nm_ids):
            replay.reject(f"{self.key} catalog={list(request.nm_ids)}")
        record = scenario["inputs"][self.key]
        template = record.get("responses_by_day", {}).get(end, record["response"])
        payload = render(template, end, start, end)
        if self.key == "sales_funnel_history" and start != end:
            day = date.fromisoformat(start)
            history_rows = []
            while day <= date.fromisoformat(end):
                history_rows.extend(render(template["data"]["rows"], day.isoformat()))
                day += timedelta(days=1)
            payload["data"]["rows"] = history_rows
        rows = payload
        for part in record["rows_path"].split("."):
            rows = rows[part]
        return replay.observe(self.key, payload, len(rows))
