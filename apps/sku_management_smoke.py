"""Deterministic SKU management forecast/write/event smoke; no live WB mutations."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_ads import AdsSafetyConfig, SheetVitrinaV1AdsBlock
from packages.application.sku_management import (
    PRICE_PARAMETER,
    ForecastInbound,
    ForecastSettings,
    SkuManagementBlock,
    SkuManagementError,
    calculate_depletion_forecast,
    choose_target_price_configuration,
    select_nearest_supplier_inbound,
    _select_observed_buyer_price,
)
from packages.application.wb_prices_management import WbPricesManagementBlock, WbPricesSafetyConfig
from packages.business_time import current_business_date_iso
from packages.contracts.stocks_block import StocksWarehouseRow


BUNDLE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
NM_ID = 210183919
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class FakePrices:
    def __init__(self) -> None:
        self.price = 1000
        self.discount = 10
        self.pending: list[dict[str, object]] = []
        self.ignore_upload = False
        self.alternate_matching_tuple = False
        self.quarantined = False
        self.quarantine_error = False
        self.upload_calls = 0
        self.goods_by_nm_calls = 0
        self.delay_seconds = 0.0

    def _good(self):
        discounted = round(self.price * (100 - self.discount) / 100, 2)
        return {"nmID": NM_ID, "vendorCode": "SKU-1", "sizes": [{"sizeID": 1, "price": self.price, "discountedPrice": discounted, "clubDiscountedPrice": discounted, "techSizeName": "0"}], "discount": self.discount, "clubDiscount": 0, "editableSizePrice": False}

    def fetch_goods(self, *, limit, offset, filter_nm_id=None):
        rows = [self._good()] if filter_nm_id in (None, NM_ID) else []
        return {"data": {"listGoods": rows}}

    def fetch_goods_by_nm_ids(self, nm_ids):
        self.goods_by_nm_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return {"data": {"listGoods": [self._good()] if NM_ID in [int(item) for item in nm_ids] else []}}

    def upload_task(self, goods):
        self.upload_calls += 1
        self.pending = [dict(item) for item in goods]
        row = self.pending[0]
        if not self.ignore_upload:
            self.price = int(row["price"])
            self.discount = int(row["discount"])
        if self.alternate_matching_tuple:
            self.price = int(round(float(row["price"]) * (100 - int(row["discount"])) / 100))
            self.discount = 0
        return {"data": {"id": 101, "alreadyExists": False}}

    def fetch_upload_status(self, upload_id):
        return {"data": {"uploadID": upload_id, "status": 3, "overAllGoodsNumber": 1, "successGoodsNumber": 1}}

    def fetch_upload_goods(self, *, upload_id, limit, offset):
        return {"data": {"historyGoods": []}}

    def fetch_quarantine_goods(self, *, limit, offset):
        if self.quarantine_error:
            raise RuntimeError("fake quarantine source unavailable")
        rows = [{"nmID": NM_ID, "newPrice": self.price, "oldPrice": self.price}] if self.quarantined else []
        return {"data": {"quarantineGoods": rows}}


class FakeAds:
    def __init__(self) -> None:
        self.bid = 1500
        self.min_bid = 1000
        self.ignore_patch = False
        self.calls = {
            "count": 0,
            "adverts": 0,
            "min": 0,
            "recommendations": 0,
            "fullstats": 0,
            "patch": 0,
        }
        self.delay_seconds = 0.0

    def fetch_campaign_count(self):
        self.calls["count"] += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return {"adverts": [{"status": 9, "advert_list": [{"advertId": 77}]}]}

    def fetch_adverts(self, advert_ids, *, statuses=None, payment_type=""):
        self.calls["adverts"] += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return {"adverts": [{"id": 77, "status": 9, "bid_type": "manual", "settings": {"name": "SKU campaign", "payment_type": "cpm", "placements": {"search": True}}, "nm_settings": [{"nm_id": NM_ID, "bids_kopecks": {"search": self.bid}}]}]}

    def fetch_min_bids(self, *, advert_id, nm_ids, payment_type, placement_types):
        self.calls["min"] += 1
        if self.min_bid is None:
            return {"bids": []}
        return {"bids": [{"nm_id": NM_ID, "bids": [{"type": "search", "value": self.min_bid}]}]}

    def fetch_recommendations(self, *, advert_id, nm_id):
        self.calls["recommendations"] += 1
        return {"base": {"competitiveBid": {"bidKopecks": 1600}}}

    def fetch_fullstats(self, advert_ids, *, begin_date, end_date):
        self.calls["fullstats"] += 1
        return []

    def patch_bids(self, payload):
        self.calls["patch"] += 1
        if not self.ignore_patch:
            self.bid = int(payload["bids"][0]["nm_bids"][0]["bid_kopecks"])
        return {"result": "ok"}


class FakeBuyer:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        return {"snapshot_date": request.snapshot_date, "data": {"items": [{"nmId": NM_ID, "public_buyer_price": 777.0}]}, "diagnostics": {"fresh": True}}


class FakeStocksBlock:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        item = SimpleNamespace(
            nm_id=NM_ID,
            stock_total=180.0,
            stock_ru_central=20.0,
            stock_ru_northwest=30.0,
            stock_ru_volga=40.0,
            stock_ru_ural=30.0,
            stock_ru_south_caucasus=40.0,
            stock_ru_far_siberia=20.0,
        )
        warehouse_rows = [
            StocksWarehouseRow(
                nm_id=NM_ID, warehouse_id=101, warehouse_name="Альфа",
                region_name="Центральный", quantity=20,
                planning_zone_key="central_north", classification_status="mapped",
                classification_source="fixture",
            ),
            StocksWarehouseRow(
                nm_id=NM_ID, warehouse_id=102, warehouse_name="Бета",
                region_name="Северо-Западный", quantity=30,
                planning_zone_key="northwest", classification_status="mapped",
                classification_source="fixture",
            ),
            StocksWarehouseRow(
                nm_id=NM_ID, warehouse_id=103, warehouse_name="Гамма",
                region_name="Приволжский", quantity=40,
                planning_zone_key="volga", classification_status="mapped",
                classification_source="fixture",
            ),
        ]
        return SimpleNamespace(result=SimpleNamespace(
            kind="success",
            items=[item],
            warehouse_rows=warehouse_rows,
            snapshot_date=request.snapshot_date,
            fetched_at="2026-07-13T08:00:00Z",
            pagination_complete=True,
            raw_rows_digest="sha256:sku-management-fixture",
        ))


class FakeSalesHistory:
    def __init__(self) -> None:
        self.calls = 0

    def load_order_count_samples_by_date(self, **kwargs):
        self.calls += 1
        return {NM_ID: [((date(2026, 7, 12) - __import__("datetime").timedelta(days=index)).isoformat(), 10.0) for index in range(30)]}


class EmptySalesHistory:
    def load_order_count_samples_by_date(self, **kwargs):
        return {NM_ID: []}


class IncompleteStocksBlock:
    def execute(self, request):
        return SimpleNamespace(result=SimpleNamespace(kind="incomplete", items=[]))


def main() -> None:
    _forecast_checks()
    _metric_date_policy_checks()
    _supplier_inbound_projection_checks()
    _price_configuration_checks()
    _wb_supply_double_count_check()
    with TemporaryDirectory(prefix="sku-management-smoke-") as tmp:
        runtime_dir = Path(tmp)
        runtime = _seed_runtime(runtime_dir)
        prices_source = FakePrices()
        ads_source = FakeAds()
        prices = WbPricesManagementBlock(runtime=runtime, runtime_dir=runtime_dir, source=prices_source, now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", safety_config=WbPricesSafetyConfig(True, 300))
        ads = SheetVitrinaV1AdsBlock(runtime=runtime, runtime_dir=runtime_dir, source=ads_source, now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", cache_ttl_seconds=0, safety_config=AdsSafetyConfig(True, 100000, __import__("decimal").Decimal("100"), 100000, 300))
        buyer_source = FakeBuyer()
        block = SkuManagementBlock(runtime=runtime, runtime_dir=runtime_dir, prices_block=prices, ads_block=ads, stocks_block=FakeStocksBlock(), sales_history=FakeSalesHistory(), buyer_price_source=buyer_source, now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", sleep=lambda _: None, readback_attempts=2, readback_delay_seconds=0)
        _settings_and_table(block)
        _quick_detail_narrow_call_graph(block, prices_source, ads_source)
        missing_sales = SkuManagementBlock(runtime=runtime, runtime_dir=runtime_dir, prices_block=prices, ads_block=ads, stocks_block=FakeStocksBlock(), sales_history=EmptySalesHistory(), buyer_price_source=FakeBuyer(), now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", sleep=lambda _: None, readback_attempts=2, readback_delay_seconds=0)
        missing_sales_row = next(item for item in missing_sales.build_table(user_key="operator")["rows"] if item["nm_id"] == NM_ID)
        if missing_sales_row["risk"] != "unknown" or missing_sales_row["daily_demand"] is not None:
            raise AssertionError("absent sales samples must remain unknown rather than optimistic zero demand")
        _price_write(block, runtime, prices_source, buyer_source)
        _bid_write_and_stabilization(block, runtime, ads_source)
        _daily_projection(runtime)
    with TemporaryDirectory(prefix="sku-management-default-gates-") as tmp:
        runtime_dir = Path(tmp)
        runtime = _seed_runtime(runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime)
        if not entrypoint.sku_management_block.prices_block.safety.write_enabled or not entrypoint.sku_management_block.ads_block.safety.write_enabled:
            raise AssertionError("SKU management write flow must not depend on disabled-by-default legacy flags")
    print("sku_management_smoke: OK")


def _metric_date_policy_checks() -> None:
    class MetricRuntime:
        def load_sheet_vitrina_ready_snapshot_covering_date_any_bundle(self, *, column_date):
            return SimpleNamespace(
                date_columns=["2026-07-23", "2026-07-24", "2026-07-25"],
                sheets=[
                    SimpleNamespace(
                        sheet_name="DATA_VITRINA",
                        rows=[
                            ["", f"SKU:{NM_ID}|orderSum", 10, 20, 999],
                            ["", f"SKU:{NM_ID}|proxy_profit_rub", 1, None, 999],
                        ],
                    )
                ],
            )

        def list_temporal_source_snapshot_dates(self, *, source_key):
            return ["2026-07-23", "2026-07-24", "2026-07-25"]

        def load_temporal_source_snapshot(self, *, source_key, snapshot_date):
            if snapshot_date == "2026-07-25":
                return SimpleNamespace(items=[]), "2026-07-25T08:00:00Z"
            if source_key == "spp_proxy" and snapshot_date == "2026-07-24":
                return SimpleNamespace(items=[SimpleNamespace(
                    nm_id=NM_ID, public_buyer_price=777.0, spp_proxy=0.22
                )]), "2026-07-24T08:30:00Z"
            if source_key == "promo_by_price" and snapshot_date == "2026-07-24":
                return SimpleNamespace(items=[SimpleNamespace(
                    nm_id=NM_ID, promo_participation=1.0, promo_count_by_price=2.0
                )]), "2026-07-24T09:00:00Z"
            if source_key == "ads_compact" and snapshot_date == "2026-07-24":
                return SimpleNamespace(result=SimpleNamespace(items=[SimpleNamespace(
                    nm_id=NM_ID, ads_sum=321.0
                )])), "2026-07-24T09:15:00Z"
            return SimpleNamespace(items=[]), f"{snapshot_date}T08:00:00Z"

    block = SkuManagementBlock(
        runtime=MetricRuntime(),
        runtime_dir=Path("/tmp"),
        prices_block=object(),
        ads_block=object(),
        now_factory=lambda: datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc),
    )
    cumulative = block._commercial_projection([NM_ID], target_date="2026-07-24")[NM_ID]
    if cumulative.get("orderSum") != 20 or cumulative.get("orderSum__date") != "2026-07-24":
        raise AssertionError("cumulative metrics must read exact business D-2")
    if "proxy_profit_rub" in cumulative:
        raise AssertionError("missing exact D-2 value must not fall back to another day")
    if cumulative.get("ads_sum") != 321 or cumulative.get("ads_sum__date") != "2026-07-24":
        raise AssertionError("exact D-2 advertising spend must use ads_compact evidence")
    snapshots = block._snapshot_projection([NM_ID])[NM_ID]
    if snapshots.get("buyer_price_rub") != 777 or snapshots.get("buyer_price_rub__date") != "2026-07-24":
        raise AssertionError("snapshot metrics must use the latest successful non-empty observation")
    if snapshots.get("promo_participation") != 1 or snapshots.get("promo_participation__date") != "2026-07-24":
        raise AssertionError("empty refresh attempt must not replace the latest successful promo fact")


def _forecast_checks() -> None:
    if current_business_date_iso(datetime(2026, 7, 13, 19, 30, tzinfo=timezone.utc)) != "2026-07-14":
        raise AssertionError("forecast business-day boundary must use Asia/Yekaterinburg")
    settings = ForecastSettings(forecast_horizon_days=80, future_order_period_days=14, production_lead_days=7, factory_to_ff_lead_days=5, ff_to_wb_lead_days=3, safety_stock_days=5, order_batch_qty=10)
    result = calculate_depletion_forecast(
        as_of_date="2026-07-13", stock_wb=50, stock_ff=20, daily_demand=10, settings=settings,
        real_inbounds=[ForecastInbound("2026-07-16", 30, "supplier_shipment", "s1"), ForecastInbound("2026-07-20", 40, "wb_supply", "w1")],
        districts={"central": {"stock": 12, "daily_demand": 3}, "volga": {"stock": 80, "daily_demand": 2}},
    )
    if result["deficit_date"] != "2026-07-13":
        raise AssertionError(f"sequential safety depletion mismatch: {result['deficit_date']}")
    ff_day = next(item for item in result["timeline"] if item["date"] == "2026-07-16")
    if "current_ff_stock" not in ff_day["inbound_sources"]:
        raise AssertionError("current FF stock must respect FF -> WB lead time")
    if not result["synthetic_orders"] or any(not item["calculation_only"] for item in result["synthetic_orders"]):
        raise AssertionError("forecast must transition to calculation-only synthetic orders")
    if result["first_problem_district"] != "central":
        raise AssertionError(f"district risk mismatch: {result}")
    deduped = calculate_depletion_forecast(as_of_date="2026-07-13", stock_wb=100, stock_ff=0, daily_demand=1, settings=settings, real_inbounds=[ForecastInbound("2026-07-14", 10, "wb_supply", "same"), ForecastInbound("2026-07-14", 10, "wb_supply", "same")])
    day = next(item for item in deduped["timeline"] if item["date"] == "2026-07-14")
    if day["inbound_qty"] != 10:
        raise AssertionError("duplicate inbound evidence must not double count")
    same_day = calculate_depletion_forecast(
        as_of_date="2026-07-13",
        stock_wb=100,
        stock_ff=0,
        daily_demand=1,
        settings=settings,
        real_inbounds=[
            ForecastInbound("2026-07-14", 10, "wb_supply", "one"),
            ForecastInbound("2026-07-14", 15, "wb_supply", "two"),
        ],
    )
    same_day_row = next(item for item in same_day["timeline"] if item["date"] == "2026-07-14")
    if same_day_row["inbound_qty"] != 25:
        raise AssertionError("independent same-day inbounds must be accumulated")
    zero_sales = calculate_depletion_forecast(
        as_of_date="2026-07-13", stock_wb=10, stock_ff=0, daily_demand=0, settings=settings
    )
    if zero_sales["risk"] != "low" or zero_sales["deficit_date"] is not None:
        raise AssertionError("authoritative zero sales must not become unknown demand")
    missing_ff = calculate_depletion_forecast(
        as_of_date="2026-07-13", stock_wb=10, stock_ff=None, daily_demand=1, settings=settings
    )
    if missing_ff["risk"] != "unknown" or missing_ff["first_problem_district"] != "unknown":
        raise AssertionError("missing FF evidence must fail closed")
    regional_unknown = calculate_depletion_forecast(
        as_of_date="2026-07-13", stock_wb=10, stock_ff=0, daily_demand=1, settings=settings
    )
    if regional_unknown["first_problem_district"] != "unknown" or regional_unknown["regional_status"] != "unknown":
        raise AssertionError("missing regional evidence must remain explicit unknown")
    regional_zero = calculate_depletion_forecast(
        as_of_date="2026-07-13",
        stock_wb=10,
        stock_ff=0,
        daily_demand=0,
        settings=settings,
        districts={"central": {"stock": 10, "daily_demand": 0}},
    )
    if regional_zero["regional_status"] != "available" or regional_zero["first_problem_district"] is not None:
        raise AssertionError("authoritative zero regional demand is known evidence, not unknown")
    missing_regional_stock = calculate_depletion_forecast(
        as_of_date="2026-07-13",
        stock_wb=10,
        stock_ff=0,
        daily_demand=1,
        settings=settings,
        districts={"central": {"daily_demand": 1}},
    )
    if missing_regional_stock["regional_status"] != "unknown":
        raise AssertionError("missing district stock must not be converted to optimistic zero")
    missing_date = calculate_depletion_forecast(
        as_of_date="2026-07-13",
        stock_wb=10,
        stock_ff=5,
        daily_demand=1,
        settings=settings,
        real_inbounds=[ForecastInbound("", 5, "wb_supply", "missing-date", consumes_initial_ff=True)],
    )
    if not any("usable даты" in item for item in missing_date["quality_warnings"]):
        raise AssertionError("undated inbound must remain explicit partial evidence")
    missing_date_ff_day = next(item for item in missing_date["timeline"] if item["date"] == "2026-07-16")
    if missing_date_ff_day["inbound_qty"] != 5 or "current_ff_stock" not in missing_date_ff_day["inbound_sources"]:
        raise AssertionError("an excluded undated transfer must not reserve authoritative current FF stock")
    if len(missing_date["timeline"]) != settings.forecast_horizon_days:
        raise AssertionError("forecast horizon must have exact configured calendar-day length")
    partial = calculate_depletion_forecast(
        as_of_date="2026-07-13",
        stock_wb=0,
        stock_ff=10,
        daily_demand=1,
        settings=settings,
        real_inbounds=[ForecastInbound("2026-07-14", 15, "wb_supply", "partial", consumes_initial_ff=True)],
    )
    partial_day = next(item for item in partial["timeline"] if item["date"] == "2026-07-14")
    if partial_day["inbound_qty"] != 10 or not any("partially" in item for item in partial["quality_warnings"]):
        raise AssertionError("FF-backed WB supply must be capped to partial authoritative quantity")
    overdue = calculate_depletion_forecast(
        as_of_date="2026-07-13",
        stock_wb=-2,
        stock_ff=0,
        daily_demand=1,
        settings=settings,
        real_inbounds=[ForecastInbound("2026-07-12", 20, "supplier_shipment", "overdue")],
    )
    if overdue["minimum_stock"] >= 0 or not any("Просроченный" in item for item in overdue["quality_warnings"]):
        raise AssertionError("negative stock and overdue plans must not be hidden by optimistic clamps")


def _price_configuration_checks() -> None:
    pair = choose_target_price_configuration(target_seller_price=850, current_price=1000, current_discount=10)
    if pair["price"] * (100 - pair["discount"]) / 100 != 850:
        raise AssertionError(f"target price pair mismatch: {pair}")
    freshest = _select_observed_buyer_price(
        event_buyer={"value": 700, "quality": "observed", "freshness": "2026-07-12", "source": "public_wb_card"},
        metrics={"buyer_price_rub": 777, "buyer_price_rub__date": "2026-07-13"},
    )
    if freshest["value"] != 777 or freshest["source"] != "web_vitrina_spp_proxy_projection":
        raise AssertionError("table buyer price must choose the freshest factual public-card observation")
    refreshed_same_day = _select_observed_buyer_price(
        event_buyer={"value": 777, "quality": "observed", "freshness": "2026-07-13", "observed_at": "2026-07-13T09:00:00Z"},
        metrics={"buyer_price_rub": 776, "buyer_price_rub__date": "2026-07-13"},
        current_buyer={"value": 778, "quality": "observed", "freshness": "2026-07-13", "observed_at": "2026-07-13T10:00:00Z", "source": "spp_proxy_temporal_snapshot"},
    )
    if refreshed_same_day["value"] != 778:
        raise AssertionError("a later same-day public-card refresh must supersede an older mutation readback")
    missing = _select_observed_buyer_price(
        event_buyer={"value": 701, "quality": "estimated", "freshness": "2026-07-14"},
        metrics={},
    )
    if missing["value"] is not None or missing["quality"] != "missing":
        raise AssertionError("calculated buyer price must never substitute missing public confirmation")


def _supplier_inbound_projection_checks() -> None:
    settings = ForecastSettings(factory_to_ff_lead_days=5, ff_to_wb_lead_days=3)
    details = {
        "later": {
            "header": {
                "invoice_no": "INV-200",
                "order_status": "in_transit",
                "actual_shipment_date": "2026-07-15",
                "shipment_date": "2026-07-10",
            },
            "lines": [
                {"line_type": "product", "match_status": "matched_by_barcode", "internal_nm_id": NM_ID, "qty": 20},
                {"line_type": "product", "match_status": "matched_by_compatibility", "internal_nm_id": NM_ID, "qty": 5},
                {"line_type": "extra", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 999},
            ],
        },
        "first-b": {
            "header": {"invoice_no": "INV-B", "order_status": "production", "actual_shipment_date": "invalid", "shipment_date": "2026-07-14"},
            "lines": [{"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 12}],
        },
        "first-a": {
            "header": {"invoice_no": "INV-A", "order_status": "production", "shipment_date": "2026-07-14"},
            "lines": [{"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 11}],
        },
        "untrusted": {
            "header": {"invoice_no": "INV-UNTRUSTED", "order_status": "production", "shipment_date": "2026-07-13"},
            "lines": [
                {"line_type": "product", "match_status": "unmatched", "internal_nm_id": NM_ID, "qty": 100},
                {"line_type": "product", "match_status": "ambiguous", "internal_nm_id": NM_ID, "qty": 100},
                {"line_type": "product", "match_status": "matched", "manual_override": True, "internal_nm_id": NM_ID, "qty": 100},
                {"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 0},
                {"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": -5},
            ],
        },
        "accepted": {
            "header": {
                "invoice_no": "INV-ACCEPTED",
                "order_status": "in_transit",
                "shipment_date": "2026-07-13",
                "actual_ff_acceptance_date": "2026-07-14",
            },
            "lines": [{"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 500}],
        },
        "cancelled": {
            "header": {"invoice_no": "INV-CANCELLED", "order_status": "cancelled", "shipment_date": "2026-07-13"},
            "lines": [{"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 500}],
        },
        "overdue": {
            "header": {"invoice_no": "INV-OVERDUE", "order_status": "production", "shipment_date": "2026-06-01"},
            "lines": [{"line_type": "product", "match_status": "matched", "internal_nm_id": NM_ID, "qty": 30}],
        },
    }
    runtime = SimpleNamespace(
        list_supplier_shipments=lambda: [{"shipment_id": shipment_id} for shipment_id in details],
        load_supplier_shipment=lambda shipment_id: details[shipment_id],
    )
    block = SkuManagementBlock(
        runtime=runtime,
        runtime_dir=Path("."),
        prices_block=object(),
        ads_block=object(),
        now_factory=lambda: NOW,
    )
    result = {NM_ID: {"real_inbounds": [], "supplier_inbounds": [], "warnings": []}}
    block._append_supplier_inbounds(result, settings=settings)
    supplier = result[NM_ID]["supplier_inbounds"]
    later = next(item for item in supplier if item["invoice_no"] == "INV-200")
    if later["quantity"] != 25 or later["arrival_date"] != "2026-07-23" or later["date_source"] != "actual_shipment_date":
        raise AssertionError(f"supplier lines/date-source projection mismatch: {later}")
    timeline_later = next(
        item for item in result[NM_ID]["real_inbounds"] if item.source_id == "later:" + str(NM_ID)
    )
    if timeline_later.arrival_date != later["arrival_date"] or timeline_later.quantity != later["quantity"]:
        raise AssertionError("nearest-inbound projection must reuse the exact forecast timeline evidence")
    nearest = select_nearest_supplier_inbound(supplier, as_of_date="2026-07-13")
    if nearest is None or nearest["invoice_no"] != "INV-A" or nearest["arrival_date"] != "2026-07-22":
        raise AssertionError(f"nearest invoice/tie-break mismatch: {nearest}")
    planned_fallback = next(item for item in supplier if item["invoice_no"] == "INV-B")
    if planned_fallback["date_source"] != "planned_shipment_date" or planned_fallback["arrival_date"] != "2026-07-22":
        raise AssertionError("invalid/non-applicable actual date must fall back to the valid planned forecast date")
    admitted = {item["invoice_no"] for item in supplier}
    forbidden = {"INV-UNTRUSTED", "INV-ACCEPTED", "INV-CANCELLED"}
    if admitted & forbidden:
        raise AssertionError(f"untrusted/accepted/cancelled rows leaked into projection: {admitted & forbidden}")
    overdue_only = select_nearest_supplier_inbound(
        [item for item in supplier if item["invoice_no"] == "INV-OVERDUE"],
        as_of_date="2026-07-13",
    )
    if overdue_only is not None:
        raise AssertionError("overdue inbound must not be presented as the nearest future shipment")
    if select_nearest_supplier_inbound([], as_of_date="2026-07-13") is not None:
        raise AssertionError("absence of a registered future invoice must remain empty")


def _wb_supply_double_count_check() -> None:
    record = {
        "supply_id": "supply-1",
        "cache_key": "supply:supply-1",
        "normalized": {"status_id": 3, "supply_date": "2026-07-20"},
        "raw_goods": [
            {"nmID": NM_ID, "quantity": 10, "acceptedQuantity": 4},
            {"nmID": NM_ID, "quantity": 15, "acceptedQuantity": 6},
        ],
    }
    runtime = SimpleNamespace(
        list_wb_supplies_cache_records=lambda: [record],
        load_ff_stock_operation_by_source_key=lambda source_key: None,
    )
    block = SkuManagementBlock(
        runtime=runtime,
        runtime_dir=Path("."),
        prices_block=object(),
        ads_block=object(),
        now_factory=lambda: NOW,
    )
    result = {NM_ID: {"real_inbounds": [], "warnings": []}}
    block._append_wb_supply_inbounds(result, settings=ForecastSettings())
    transfer = result[NM_ID]["real_inbounds"][0]
    if len(result[NM_ID]["real_inbounds"]) != 1 or not transfer.consumes_initial_ff:
        raise AssertionError("WB supply still present in FF balance must be represented as a transfer")
    if transfer.quantity != 15 or transfer.initial_ff_reservation_qty != 25:
        raise AssertionError("duplicate SKU lines must aggregate before partial acceptance and FF reservation")
    runtime.load_ff_stock_operation_by_source_key = lambda source_key: {"operation_id": "writeoff-1"}
    result = {NM_ID: {"real_inbounds": [], "warnings": []}}
    block._append_wb_supply_inbounds(result, settings=ForecastSettings())
    if len(result[NM_ID]["real_inbounds"]) != 1 or result[NM_ID]["real_inbounds"][0].quantity != 15:
        raise AssertionError("WB supply deducted from FF ledger must return only its not-yet-progressed quantity")


def _settings_and_table(block) -> None:
    settings = block.get_settings(user_key="operator")
    if settings["forecast"]["sales_avg_period_days"] != 14 or settings["canonical_store"] != "server_runtime_user_config":
        raise AssertionError(settings)
    saved = block.save_settings(user_key="operator", payload={"base_revision": 0, "forecast": {**settings["forecast"], "sales_avg_period_days": 30}, "table": {"visible_columns": ["risk", "first_problem_district"], "column_order": ["first_problem_district", "risk", "product"], "column_widths": {"product": 190, "first_problem_district": 160}, "filters": {"search": "SKU", "risk": "high", "coverage_min": 100}, "sort": [{"key": "first_problem_district", "direction": "asc"}, {"key": "deficit_date", "direction": "asc"}]}})
    if saved["forecast"]["sales_avg_period_days"] != 30 or saved["table"]["column_widths"]["product"] != 190:
        raise AssertionError(saved)
    if saved["table"]["filters"] != {"search": "SKU"}:
        raise AssertionError("retired filters must be removed from persisted active state")
    if saved["table"]["visible_columns"] != ["product", "risk"]:
        raise AssertionError("mandatory product must survive migration while retired columns are removed")
    if saved["table"]["column_order"][0] != "product":
        raise AssertionError("mandatory product must remain the first persisted column")
    serialized_table = json.dumps(saved["table"], sort_keys=True)
    if "first_problem_district" in serialized_table:
        raise AssertionError("retired presentation column must be removed from preferences")
    table = block.build_table(user_key="operator")
    if not table["rows"] or table["meta"]["writes_enabled"] is not True:
        raise AssertionError(table)
    row = next(item for item in table["rows"] if item["nm_id"] == NM_ID)
    if row["seller_price"] != 900 or row["campaign_count"] != 1 or not row["ad_options"]:
        raise AssertionError(row)
    warehouse_settings = block.save_warehouse_exclusion_settings(
        user_key="operator",
        payload={
            "base_revision": 0,
            "active": True,
            "excluded_wb_warehouse_ids": [101, 102],
            "reason": "fixture incident",
            "effective_from": "2026-07-13",
            "effective_to": "",
            "status": "active",
        },
    )
    if warehouse_settings["excluded_wb_warehouse_ids"] != [101, 102]:
        raise AssertionError("warehouse exclusions must use one server-owned config")
    rematerialization = warehouse_settings.get("rematerialization") or {}
    if (
        rematerialization.get("status") != "no_ready_dates"
        or rematerialization.get("snapshot_count") != 0
    ):
        raise AssertionError(
            "policy Apply must invoke the bounded idempotent Vitrina "
            f"rematerialization path: {rematerialization}"
        )
    entrypoint = object.__new__(RegistryUploadHttpEntrypoint)
    entrypoint.sku_management_block = block
    canonical_payload = entrypoint._with_canonical_warehouse_exclusions(
        {"excluded_wb_warehouse_ids": [999]},
        user_key="operator",
    )
    if canonical_payload["excluded_wb_warehouse_ids"] != [101, 102]:
        raise AssertionError("supply calculations must override browser copies with canonical config")
    evidence = block._collect_forecast_evidence(
        active=block._active_skus(),
        settings=ForecastSettings(),
    )[NM_ID]
    if evidence["stock_wb"] != 130:
        raise AssertionError(f"warehouse exclusions must change total SKU stock: {evidence}")
    if evidence["districts"]["central"]["stock"] != 0 or evidence["districts"]["northwest"]["stock"] != 0:
        raise AssertionError(f"warehouse exclusions must change regional SKU stock: {evidence}")
    complete_stocks = block.stocks_block
    block.stocks_block = IncompleteStocksBlock()
    incomplete = block._collect_forecast_evidence(
        active=block._active_skus(),
        settings=ForecastSettings(),
    )[NM_ID]
    block.stocks_block = complete_stocks
    if incomplete["stock_wb"] is not None or not any(
        "incomplete" in warning for warning in incomplete["warnings"]
    ):
        raise AssertionError("incomplete warehouse evidence must fail closed")
    prior_policy_record = block.runtime.load_latest_wb_incident_policy(
        seller_id="canonical"
    )
    original_rematerialize = block._rematerialize_incident_policy_ready_dates
    block._rematerialize_incident_policy_ready_dates = lambda **_: {
        "status": "failed",
        "date_from": "2026-07-13",
        "date_to": "2026-07-13",
        "error": "fixture dependent replay failure",
    }
    failed_apply = block.save_warehouse_exclusion_settings(
        user_key="operator",
        payload={
            "base_revision": warehouse_settings["revision"],
            "active": True,
            "warehouse_entries": [
                {"warehouse_id": 101, "effective_from": "2026-07-13"},
                {"warehouse_id": 102, "effective_from": "2026-07-13"},
                {"warehouse_id": 103, "effective_from": "2026-07-13"},
            ],
            "reason": "fixture incident changed",
            "effective_to": "",
            "status": "active",
            "change_effective_from": "2026-07-13",
        },
    )
    block._rematerialize_incident_policy_ready_dates = original_rematerialize
    if (
        failed_apply.get("policy_apply_status")
        != "failed_reverted_to_last_good"
        or failed_apply.get("excluded_wb_warehouse_ids") != [101, 102]
    ):
        raise AssertionError(
            "failed dependent replay must append an exact last-good recovery revision"
        )
    restored_policy_record = block.runtime.load_latest_wb_incident_policy(
        seller_id="canonical"
    )
    if (
        restored_policy_record.get("warehouse_entries")
        != prior_policy_record.get("warehouse_entries")
        or restored_policy_record.get("source")
        != "incident_policy_v2_last_good_recovery"
    ):
        raise AssertionError("last-good recovery must preserve the exact prior interval history")
    block.save_warehouse_exclusion_settings(
        user_key="operator",
        payload={
            "base_revision": failed_apply["revision"],
            "active": False,
            "excluded_wb_warehouse_ids": [101, 102],
            "reason": "fixture incident resolved",
            "effective_from": "2026-07-14",
            "effective_to": "",
            "status": "resolved",
        },
    )


def _quick_detail_narrow_call_graph(block, prices_source, ads_source) -> None:
    price_calls_before = prices_source.goods_by_nm_calls
    ads_calls_before = dict(ads_source.calls)
    stocks_calls_before = block.stocks_block.calls
    sales_calls_before = block.sales_history.calls
    prices_source.delay_seconds = 0.03
    ads_source.delay_seconds = 0.03
    started = time.monotonic()
    try:
        detail = block.build_sku_detail(NM_ID, user_key="operator")
    finally:
        prices_source.delay_seconds = 0.0
        ads_source.delay_seconds = 0.0
    elapsed_ms = (time.monotonic() - started) * 1000
    diagnostics = detail["meta"]["diagnostics"]
    price_delta = prices_source.goods_by_nm_calls - price_calls_before
    ads_delta = {
        key: ads_source.calls[key] - ads_calls_before[key]
        for key in ads_calls_before
    }
    if price_delta != 1:
        raise AssertionError(f"quick detail must perform one exact price read: {price_delta}")
    if ads_delta != {
        "count": 1,
        "adverts": 1,
        "min": 0,
        "recommendations": 0,
        "fullstats": 0,
        "patch": 0,
    }:
        raise AssertionError(f"quick detail advertising call graph widened: {ads_delta}")
    if block.stocks_block.calls != stocks_calls_before or block.sales_history.calls != sales_calls_before:
        raise AssertionError("quick detail must not invoke stocks, sales forecast or supply work")
    if detail["meta"]["read_scope"] != "quick_exact_sku_mutation":
        raise AssertionError(detail["meta"])
    if diagnostics["remote_call_counts"] != {
        "wb_prices_exact_goods": 1,
        "wb_ads_campaign_count": 1,
        "wb_ads_adverts_batch": 1,
        "wb_ads_fullstats": 0,
        "wb_ads_min_bid": 0,
        "wb_ads_recommendations": 0,
        "wb_stocks": 0,
        "forecast_or_supply": 0,
    }:
        raise AssertionError(diagnostics)
    if elapsed_ms >= 500 or diagnostics["total_ms"] >= 500:
        raise AssertionError(
            f"artificial-delay quick detail must avoid sequential full-table latency: "
            f"wall={elapsed_ms:.2f} diagnostics={diagnostics}"
        )
    row = detail["row"]
    if row["nm_id"] != NM_ID or row["name"] != "SKU management fixture" or len(row["ad_options"]) != 1:
        raise AssertionError(row)


def _price_write(block, runtime, prices_source, buyer_source) -> None:
    prices_source.quarantined = True
    try:
        block.preview_price({"nm_id": NM_ID, "target_seller_price": 850}, actor="operator")
    except SkuManagementError as exc:
        if exc.http_status != 409 or exc.payload.get("safety_status") != "current_quarantine":
            raise
    else:
        raise AssertionError("current quarantine must block price preview")
    prices_source.quarantined = False
    prices_source.quarantine_error = True
    try:
        block.preview_price({"nm_id": NM_ID, "target_seller_price": 850}, actor="operator")
    except SkuManagementError as exc:
        if exc.http_status != 503 or exc.payload.get("safety_status") != "quarantine_evidence_unavailable":
            raise
    else:
        raise AssertionError("unavailable quarantine evidence must fail closed")
    prices_source.quarantine_error = False

    original_snapshot_projection = block._snapshot_projection
    original_table = block.prices_block.build_goods_table
    block.prices_block.build_goods_table = lambda params=None: {
        "rows": [{"nmID": NM_ID, "promoLabel": "0 / 5", "promoEligibleCount": 0, "promoCurrentCount": 5, "promoReason": "source=promo_by_price date=2026-07-13"}]
    }
    block._snapshot_projection = lambda nm_ids: {NM_ID: {"promo_participation": 0.0, "promo_participation__date": "2026-07-13", "promo_count_by_price": 0.0, "promo_count_by_price__date": "2026-07-13"}}
    no_participation = block.preview_price({"nm_id": NM_ID, "target_seller_price": 860}, actor="operator")["preview"]
    if "active_promo_participation" in no_participation["warnings"]:
        raise AssertionError("global current promo count must not be mistaken for this SKU's participation")
    block._snapshot_projection = lambda nm_ids: {NM_ID: {"promo_participation": 1.0, "promo_participation__date": "2026-07-13", "promo_count_by_price": 1.0, "promo_count_by_price__date": "2026-07-13"}}
    active_participation = block.preview_price({"nm_id": NM_ID, "target_seller_price": 860}, actor="operator")["preview"]
    if "active_promo_participation" not in active_participation["override_required_warnings"]:
        raise AssertionError("canonical per-SKU promo participation must require explicit price override")
    block._snapshot_projection = original_snapshot_projection
    block.prices_block.build_goods_table = original_table

    price_reads_before = prices_source.goods_by_nm_calls
    preview = block.preview_price({"nm_id": NM_ID, "target_seller_price": 850}, actor="operator")
    if prices_source.goods_by_nm_calls - price_reads_before != 1:
        raise AssertionError("price preview must reuse its single exact current-price read")
    facts = preview["preview"]
    if (
        facts["new"]["discountedPrice"] != 850
        or facts["current_buyer_price"] != 777
        or facts["product_name"] != "SKU management fixture"
    ):
        raise AssertionError(facts)
    commit_price_reads_before = prices_source.goods_by_nm_calls
    committed = block.commit_price(
        {"preview_id": facts["preview_id"], "confirm": True, "override_warnings": True},
        actor="operator",
    )
    if prices_source.goods_by_nm_calls - commit_price_reads_before != 2:
        raise AssertionError("price commit must use one current-tuple read and one matching readback")
    if buyer_source.calls:
        raise AssertionError("optional buyer-price enrichment must not block preview or commit")
    if (
        committed["status"] != "success"
        or committed["confirmed_value"] != 850
        or committed["old_value"] != 900
        or committed["requested_value"] != 850
        or committed["product_name"] != "SKU management fixture"
        or committed["nm_id"] != NM_ID
    ):
        raise AssertionError(committed)
    if committed["buyer_price"].get("post_commit_refresh") != "not_awaited":
        raise AssertionError("buyer-price enrichment must be explicitly deferred")
    latest = runtime.latest_sku_action_events_by_nm([NM_ID])[NM_ID][PRICE_PARAMETER]
    if latest["confirmed_value"] != 850 or not latest["confirmed_at"]:
        raise AssertionError(latest)
    try:
        block.commit_price({"preview_id": facts["preview_id"], "confirm": True}, actor="operator")
    except SkuManagementError as exc:
        if exc.http_status != 409:
            raise
    else:
        raise AssertionError("one preview must not produce a second WB action")

    stale = block.preview_price({"nm_id": NM_ID, "target_seller_price": 840}, actor="operator")["preview"]
    calls_before = prices_source.upload_calls
    prices_source.discount -= 1
    try:
        block.commit_price(
            {"preview_id": stale["preview_id"], "confirm": True, "override_stabilization": True},
            actor="operator",
        )
    except SkuManagementError as exc:
        if exc.http_status != 409 or prices_source.upload_calls != calls_before:
            raise
    else:
        raise AssertionError("stale current WB price must block before upload")
    prices_source.discount += 1

    promo_stale = block.preview_price({"nm_id": NM_ID, "target_seller_price": 840}, actor="operator")["preview"]
    original_table = block.prices_block.build_goods_table
    block.prices_block.build_goods_table = lambda params=None: {
        "rows": [{"nmID": NM_ID, "promoLabel": "1 / 1", "promoEligibleCount": 1, "promoCurrentCount": 1}]
    }
    calls_before = prices_source.upload_calls
    try:
        block.commit_price(
            {"preview_id": promo_stale["preview_id"], "confirm": True, "override_warnings": True},
            actor="operator",
        )
    except SkuManagementError as exc:
        if exc.http_status != 409 or exc.payload.get("safety_status") != "promo_evidence_changed" or prices_source.upload_calls != calls_before:
            raise
    else:
        raise AssertionError("changed promo evidence must block before WB upload")
    finally:
        block.prices_block.build_goods_table = original_table

    mismatch = block.preview_price({"nm_id": NM_ID, "target_seller_price": 830}, actor="operator")["preview"]
    prices_source.alternate_matching_tuple = True
    try:
        block.commit_price(
            {"preview_id": mismatch["preview_id"], "confirm": True, "override_stabilization": True, "override_warnings": True},
            actor="operator",
        )
    except SkuManagementError as exc:
        observed = exc.payload.get("readback") or {}
        if exc.http_status != 409 or observed.get("seller_price") != 830 or observed.get("price") == mismatch["new"]["price"]:
            raise
    else:
        raise AssertionError("seller-price-only readback must not hide a price/discount tuple mismatch")
    finally:
        prices_source.alternate_matching_tuple = False
    latest_error = runtime.list_sku_action_events(status="error", limit=1)["rows"][0]
    if latest_error["confirmed_value"] is not None or latest_error["commit_status"] != "error":
        raise AssertionError("readback mismatch must persist a controlled failure event")


def _bid_write_and_stabilization(block, runtime, ads_source) -> None:
    block.ads_block.source.min_bid = None
    try:
        block.preview_bid({"nm_id": NM_ID, "advert_id": 77, "placement": "search", "requested_bid_rub": 18}, actor="operator")
    except SkuManagementError as exc:
        if exc.http_status != 503 or exc.payload.get("safety_status") != "min_bid_unavailable":
            raise
    else:
        raise AssertionError("bid preview must fail closed when the WB minimum is unavailable")
    block.ads_block.source.min_bid = 1000

    changed_minimum = block.preview_bid({"nm_id": NM_ID, "advert_id": 77, "placement": "search", "requested_bid_rub": 18}, actor="operator")["preview"]
    block.ads_block.source.min_bid = 1900
    try:
        block.commit_bid(
            {"preview_id": changed_minimum["preview_id"], "confirm": True, "override_stabilization": True},
            actor="operator",
        )
    except Exception as exc:
        if getattr(exc, "http_status", None) != 409:
            raise
    else:
        raise AssertionError("a higher current WB minimum must block bid commit before PATCH")
    block.ads_block.source.min_bid = 1000

    call_counts_before = dict(ads_source.calls)
    cross = block.preview_bid({"nm_id": NM_ID, "advert_id": 77, "placement": "search", "requested_bid_rub": 18}, actor="operator")["preview"]
    if "cross_parameter_stabilization" not in cross["warnings"]:
        raise AssertionError(cross)
    try:
        block.commit_bid({"preview_id": cross["preview_id"], "confirm": True}, actor="operator")
    except SkuManagementError as exc:
        if exc.http_status != 409:
            raise
    else:
        raise AssertionError("stabilization warning must require explicit override")
    committed = block.commit_bid({"preview_id": cross["preview_id"], "confirm": True, "override_stabilization": True}, actor="operator")
    call_delta = {
        key: ads_source.calls[key] - call_counts_before[key]
        for key in call_counts_before
    }
    if call_delta != {
        "count": 0,
        "adverts": 3,
        "min": 2,
        "recommendations": 0,
        "fullstats": 0,
        "patch": 1,
    }:
        raise AssertionError(f"bid preview/commit/readback call graph widened: {call_delta}")
    if (
        committed["confirmed_value"] != 18
        or committed["old_value"] != 15
        or committed["requested_value"] != 18
        or committed["product_name"] != "SKU management fixture"
        or committed["nm_id"] != NM_ID
        or committed["advert_id"] != 77
        or committed["campaign_name"] != "SKU campaign"
        or committed["placement"] != "search"
        or committed["event"]["stabilization_override"] is not True
    ):
        raise AssertionError(committed)
    mismatch = block.preview_bid(
        {
            "nm_id": NM_ID,
            "advert_id": 77,
            "placement": "search",
            "requested_bid_rub": 19,
        },
        actor="operator",
    )["preview"]
    ads_source.ignore_patch = True
    try:
        block.commit_bid(
            {
                "preview_id": mismatch["preview_id"],
                "confirm": True,
                "override_stabilization": True,
            },
            actor="operator",
        )
    except SkuManagementError as exc:
        readback = exc.payload.get("readback") or {}
        if (
            exc.http_status != 409
            or exc.payload.get("readback_status") != "mismatch"
            or readback.get("advert_id") != 77
            or readback.get("placement") != "search"
            or readback.get("current_bid_rub") != 18
        ):
            raise
    else:
        raise AssertionError("exact bid readback mismatch must fail closed")
    finally:
        ads_source.ignore_patch = False
    mismatch_error = next(
        item
        for item in runtime.list_sku_action_events(
            parameter="advertising_bid",
            status="error",
            limit=50,
        )["rows"]
        if item["preview_id"] == mismatch["preview_id"]
    )
    if (
        mismatch_error["parameter"] != "advertising_bid"
        or mismatch_error["confirmed_value"] is not None
        or mismatch_error["commit_status"] != "error"
        or mismatch_error["readback_status"] != "mismatch"
    ):
        raise AssertionError(
            f"bid mismatch must persist a controlled failure event: {mismatch_error}"
        )
    same = block.preview_bid({"nm_id": NM_ID, "advert_id": 77, "placement": "search", "requested_bid_rub": 19}, actor="operator")["preview"]
    if "same_parameter_stabilization" not in same["warnings"]:
        raise AssertionError(same)
    current = block.get_settings(user_key="operator")
    disabled = {**current["forecast"], "price_stabilization_days": 0, "bid_stabilization_days": 0}
    saved = block.save_settings(user_key="operator", payload={"base_revision": current["revision"], "forecast": disabled, "table": current["table"]})
    if block._stabilization_warnings(nm_id=NM_ID, parameter=PRICE_PARAMETER, actor="operator"):
        raise AssertionError("zero-day stabilization must disable same and cross warnings")
    restored = {**saved["forecast"], "price_stabilization_days": 3, "bid_stabilization_days": 3}
    block.save_settings(user_key="operator", payload={"base_revision": saved["revision"], "forecast": restored, "table": saved["table"]})


def _daily_projection(runtime) -> None:
    runtime.create_sku_action_event({"event_id": "extra_price", "nm_id": NM_ID, "parameter": PRICE_PARAMETER, "old_value": 850, "requested_value": 840, "confirmed_value": 840, "delta": -10, "requested_at": "2026-07-13T09:00:00Z", "confirmed_at": "2026-07-13T09:01:00Z", "actor": "operator", "source": "sku_management", "commit_status": "confirmed"})
    runtime.create_sku_action_event({"event_id": "nonmatching_defense", "nm_id": NM_ID, "parameter": PRICE_PARAMETER, "old_value": 840, "requested_value": 940, "confirmed_value": 940, "delta": 100, "requested_at": "2026-07-13T09:02:00Z", "confirmed_at": "2026-07-13T09:03:00Z", "actor": "operator", "source": "sku_management", "commit_status": "confirmed", "readback_status": "mismatch"})
    lookup = runtime.load_sku_action_daily_metric_lookup("2026-07-13")[NM_ID]
    if lookup["seller_price_change_rub"] != -60 or lookup["advertising_bid_change_rub"] != 3:
        raise AssertionError(f"daily aggregation mismatch: {lookup}")
    latest = runtime.latest_sku_action_events_by_nm([NM_ID])[NM_ID][PRICE_PARAMETER]
    if latest["event_id"] == "nonmatching_defense":
        raise AssertionError("non-matching readback must not update stabilization/last-confirmed truth")
    if runtime.load_sku_action_daily_metric_lookup("2026-07-12"):
        raise AssertionError("no-change day must stay empty")
    for event_id, old_value, confirmed_value in (("canary_change", 840, 839), ("canary_restore", 839, 840)):
        runtime.create_sku_action_event({"event_id": event_id, "nm_id": NM_ID, "parameter": PRICE_PARAMETER, "old_value": old_value, "requested_value": confirmed_value, "confirmed_value": confirmed_value, "delta": confirmed_value - old_value, "requested_at": "2026-07-14T09:00:00Z", "confirmed_at": "2026-07-14T09:01:00Z", "actor": "operator", "source": "sku_management", "commit_status": "confirmed", "readback_status": "matching"})
    restored_lookup = runtime.load_sku_action_daily_metric_lookup("2026-07-14")[NM_ID]
    if restored_lookup["seller_price_change_rub"] != 0:
        raise AssertionError("same-day mutation plus exact restore must aggregate to a truthful zero daily delta")
    events = runtime.list_sku_action_events(nm_id=NM_ID, parameter=PRICE_PARAMETER, limit=100)["rows"]
    if not {"canary_change", "canary_restore"}.issubset({item["event_id"] for item in events}):
        raise AssertionError("zero daily delta must retain both underlying action events")


def _seed_runtime(runtime_dir: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    result = runtime.ingest_bundle(json.loads(BUNDLE.read_text(encoding="utf-8")), activated_at="2026-07-13T08:00:00Z")
    if result.status != "accepted":
        raise AssertionError(result)
    runtime.save_nomenclature_item({"item_id": "sku-management-primary", "is_active": True, "our_sku": "SKU-1", "nm_id": NM_ID, "barcode": "4600000000001", "nomenclature_name": "SKU management fixture", "product_type": "case", "match_key": "sku-management", "created_at": "2026-07-13T08:00:00Z", "updated_at": "2026-07-13T08:00:00Z"})
    runtime.create_ff_stock_operation(operation_id="ff_open", operation_type="manual_receipt", source_type="manual_excel", source_key="ff_open", source_object_id="ff_open", source_object_label="FF opening", created_at="2026-07-13T08:00:00Z", created_by="smoke", lines=[{"nm_id": NM_ID, "quantity_delta": 40}])
    runtime.save_temporal_source_snapshot(
        source_key="spp_proxy",
        snapshot_date="2026-07-13",
        captured_at="2026-07-13T08:00:00Z",
        payload={
            "items": [
                {
                    "nm_id": NM_ID,
                    "public_buyer_price": 777.0,
                    "spp_proxy": 0.22,
                }
            ]
        },
    )
    runtime.save_temporal_source_snapshot(
        source_key="promo_by_price",
        snapshot_date="2026-07-13",
        captured_at="2026-07-13T08:00:00Z",
        payload={
            "items": [
                {
                    "nm_id": NM_ID,
                    "promo_participation": 0.0,
                    "promo_count_by_price": 0.0,
                }
            ]
        },
    )
    return runtime


if __name__ == "__main__":
    main()
