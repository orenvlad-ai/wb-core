"""External response fixtures for native full-cycle execution.

Only transport inputs and time are injected here. Finance ingestion, official
FBS generation, supply reconciliation, stock validation and cycle proofs remain
the production implementations. An empty supply response is explicit evidence
of no supplies in this synthetic fixture, not an unavailable source coerced to 0.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import sqlite3
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from .__main__ import ReplayClock
from .http import HttpTape, Response
from .sources import encoded_size


class FullHttpTape(HttpTape):
    def open(self, request, *, timeout=None):
        url = urlsplit(request.full_url)
        query = parse_qs(url.query)
        body = json.loads(request.data) if request.data else None
        payload = None
        key = ""
        if (url.scheme == "https" and url.netloc == "advert-api.wildberries.ru"
                and url.path == "/api/advert/v2/adverts" and request.get_method() == "GET"
                and query == {"ids": ["601"], "statuses": ["4,9,11"]} and body is None):
            # Native management/catalog observation requests all live states;
            # the one scenario campaign is active (9), exactly as its count
            # response declares. Reuse the same dated bid source values.
            record = next(item for item in self.replay.scenario["http"] if item["source"] == "ads_bids")
            payload = deepcopy(record.get("responses_by_day", {}).get(self.replay.current_day, record["response"]))
            for advert in payload["adverts"]:
                advert["status"] = 9
            self.replay.observe("ads_management_adverts", payload, len(payload["adverts"]), pages=1,
                boundary="http_response", request_bytes=0)
            return Response(payload)
        if url.scheme == "https" and url.netloc == "supplies-api.wildberries.ru":
            if (url.path == "/api/v1/warehouses" and request.get_method() == "GET"
                    and not query and body is None):
                key, payload = "canonical_supply_warehouses", [
                    {"ID": 507, "name": "Тестовый склад WB", "address": "Москва"}]
            elif (url.path == "/api/v1/supplies" and request.get_method() == "POST"
                    and query == {"limit": ["1000"], "offset": ["0"]}
                    and isinstance(body, dict) and set(body) <= {"dates", "statusIDs"}
                    and body.get("dates") == []
                    and body.get("statusIDs", []) in ([], [1, 2, 3, 4], [5, 6])):
                key, payload = "canonical_supplies", []
        elif (url.scheme == "https" and url.netloc == "common-api.wildberries.ru"
                and url.path == "/api/v1/tariffs/box" and request.get_method() == "GET"
                and set(query) == {"date"} and len(query["date"]) == 1 and body is None):
            # Native mapping helper uses wall-clock date rather than entry clock;
            # record that requested date, keeping the offline empty tariff set.
            datetime.strptime(query["date"][0], "%Y-%m-%d")
            key, payload = "canonical_box_tariffs", {"warehouseList": []}
        elif (url.scheme == "https" and url.netloc == "marketplace-api.wildberries.ru"
                and url.path == "/api/v3/orders" and request.get_method() == "GET"
                and body is None and set(query) == {"limit", "next", "dateFrom", "dateTo"}
                and query["limit"] == ["1000"] and query["next"] == ["0"]
                and all(len(query[k]) == 1 and query[k][0].isdigit() for k in ("dateFrom", "dateTo"))
                and 0 <= int(query["dateTo"][0]) - int(query["dateFrom"][0]) <= 30 * 86400):
            key, payload = "canonical_fbs_orders", {"orders": [], "next": 0}
        elif (url.scheme == "https" and url.netloc == "seller-analytics-api.wildberries.ru"
                and url.path == "/api/analytics/v1/stocks-report/wb-warehouses"
                and request.get_method() == "POST" and not query
                and body == {"nmIds": [101], "chrtIds": [], "limit": 250000, "offset": 0}):
            key, payload = "canonical_warehouse_stocks", {"data": {"items": [
                {"nmId": 101, "chrtId": 9001, "warehouseId": 507,
                 "warehouseName": "Тестовый склад WB", "regionName": "Центральный",
                 "quantity": 12, "inWayToClient": 0, "inWayFromClient": 0}]}}
        if key:
            self.replay.observe(key, payload, len(payload) if isinstance(payload, list) else
                len(payload.get("data", {}).get("items", [])), pages=1,
                boundary="http_response", request_bytes=len(request.data or b""))
            return Response(payload)
        return super().open(request, timeout=timeout)


class CanonicalFinanceTransport:
    def __init__(self, replay):
        self.replay = replay

    def __call__(self, payload):
        from packages.adapters.wb_finance_api import FinanceHttpResult
        previous_week = [(date.fromisoformat(day) - timedelta(days=7)).isoformat()
                         for day in self.replay.scenario["finance_week"]]
        if (payload["period"] == "weekly" and [payload["dateFrom"], payload["dateTo"]] == previous_week
                and payload["rrdId"] == 0 and payload["limit"] > 0):
            # The native second tick checks the other recent closed week. This
            # fixture explicitly has no report there; preserve native waiting.
            self.replay.observe("finance_weekly", [], 0, pages=1,
                boundary="finance_transport", request_bytes=encoded_size(payload))
            return FinanceHttpResult(204, [], {})
        # Preserve exact paging/cursor validation of the earlier-stage tape.
        response = self.replay.finance_request(payload)
        rows = deepcopy(response.rows)
        for row in rows:
            row.update(reportId=("LAB-" + payload["period"] + "-" + payload["dateTo"]),
                       reportType=1, dateFrom=payload["dateFrom"], dateTo=payload["dateTo"],
                       createDate=payload["dateTo"], quantity=1,
                       warehouseName="Маркетплейс" if row["nmId"] == 101 else "Тестовый склад WB",
                       supplierArticle="LAB-101" if row["nmId"] == 101 else "LAB-999",
                       barcode="0000000000101" if row["nmId"] == 101 else "0000000000999")
        # Update byte evidence to the actual canonical source payload.
        delta = encoded_size(rows) - encoded_size(response.rows)
        self.replay.totals["finance_" + payload["period"]]["payload_json_bytes"] += delta
        self.replay.phase_totals[self.replay.phase]["payload_json_bytes"] += delta
        return FinanceHttpResult(response.status, rows, response.headers)


def configure_sources(entry, replay, now=None):
    """Configure existing native blocks; return a context required during cycle.

    Call inside the lab writer lifetime, after entry construction and before
    finance/FBS/warehouse execution. Use ``with configure_sources(...) as io``.
    ``io.finance_client`` is also usable by build_sources for vitrina inputs.
    Configuration writes only the synthetic daily-history policy (Nov 22).
    """
    return FullSourceContext(entry, replay, now or entry.now_factory())


class FullSourceContext:
    def __init__(self, entry, replay, now):
        from packages.adapters.wb_finance_api import WbFinanceApiClient
        from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
        from packages.adapters.wb_content import HttpBackedWbContentSource
        from packages.adapters.wb_supplies import HttpBackedWbSuppliesSource
        from packages.adapters.stocks_block import HttpBackedStocksSource
        from packages.application.stocks_block import StocksBlock
        self.entry, self.replay, self.now = entry, replay, now
        self.tape = FullHttpTape(replay)
        self.clock = ReplayClock(now)
        self.finance_transport = CanonicalFinanceTransport(replay)
        self._real_finance_client = WbFinanceApiClient
        self.finance_client = self._finance_factory("lab-only", rate_gate_root=entry.runtime.runtime_dir)
        for block in (entry.wb_finance_daily_block, entry.wb_finance_weekly_block):
            block.now_factory = entry.now_factory
        with sqlite3.connect(entry.runtime.db_path) as conn:
            conn.execute("UPDATE wb_finance_daily_config SET initial_day=? WHERE seller_id=?",
                         (replay.scenario["bootstrap_days"][0], entry.wb_finance_daily_block.seller_id))
        registry = entry.wb_fbs_warehouse_registry
        registry.source = HttpBackedWbFbsOrdersSource(opener=self.tape.open, sleep_fn=self.clock.advance)
        registry.catalog_source = HttpBackedWbContentSource(opener=self.tape.open)
        entry.wb_fbs_orders.source = registry.source
        entry.wb_fbs_orders.enabled = True
        entry.wb_fbs_orders.unix_time_factory = lambda: now.timestamp()
        entry.wb_supplies_block.source = HttpBackedWbSuppliesSource(opener=self.tape.open)
        entry.wb_regional_supply_planning_block.source = entry.wb_supplies_block.source
        source = HttpBackedStocksSource(opener=self.tape.open, reuse_ttl_seconds=0,
            sleep_fn=self.clock.advance, monotonic_fn=self.clock.time, time_fn=self.clock.time,
            now_factory=entry.now_factory)
        # WarehouseFunctionalBlock captures through its opening block.
        entry.warehouse_functional_block.opening.stocks_block = StocksBlock(source)
        entry.warehouse_stocks_block.stocks_block = StocksBlock(source)

    def _finance_factory(self, token, **kwargs):
        if token != "lab-only":
            raise ValueError("Full-cycle fixture requires the synthetic lab token")
        return self._real_finance_client(token, **{**kwargs, "request": self.finance_transport,
            "wall_time": self.clock.time, "monotonic": self.clock.time, "sleep": self.clock.advance})

    def __enter__(self):
        from contextlib import ExitStack
        self._stack = ExitStack()
        self._stack.enter_context(patch("urllib.request.urlopen", self.tape.open))
        self._stack.enter_context(patch("packages.adapters.wb_finance_api.WbFinanceApiClient", self._finance_factory))
        from .__main__ import build_sources
        self.entry.sheet_plan_block = build_sources(self.entry.runtime, self.replay, self.now, self.finance_client)
        return self

    def __exit__(self, *args):
        return self._stack.__exit__(*args)
