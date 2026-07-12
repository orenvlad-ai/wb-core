"""Deterministic SKU management forecast/write/event smoke; no live WB mutations."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_ads import AdsSafetyConfig, SheetVitrinaV1AdsBlock
from packages.application.sku_management import (
    BID_PARAMETER,
    PRICE_PARAMETER,
    ForecastInbound,
    ForecastSettings,
    SkuManagementBlock,
    SkuManagementError,
    calculate_depletion_forecast,
    choose_target_price_configuration,
)
from packages.application.wb_prices_management import WbPricesManagementBlock, WbPricesSafetyConfig


BUNDLE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
NM_ID = 210183919
NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)


class FakePrices:
    def __init__(self) -> None:
        self.price = 1000
        self.discount = 10
        self.pending: list[dict[str, object]] = []
        self.ignore_upload = False
        self.upload_calls = 0

    def _good(self):
        discounted = round(self.price * (100 - self.discount) / 100, 2)
        return {"nmID": NM_ID, "vendorCode": "SKU-1", "sizes": [{"sizeID": 1, "price": self.price, "discountedPrice": discounted, "clubDiscountedPrice": discounted, "techSizeName": "0"}], "discount": self.discount, "clubDiscount": 0, "editableSizePrice": False}

    def fetch_goods(self, *, limit, offset, filter_nm_id=None):
        rows = [self._good()] if filter_nm_id in (None, NM_ID) else []
        return {"data": {"listGoods": rows}}

    def fetch_goods_by_nm_ids(self, nm_ids):
        return {"data": {"listGoods": [self._good()] if NM_ID in [int(item) for item in nm_ids] else []}}

    def upload_task(self, goods):
        self.upload_calls += 1
        self.pending = [dict(item) for item in goods]
        row = self.pending[0]
        if not self.ignore_upload:
            self.price = int(row["price"])
            self.discount = int(row["discount"])
        return {"data": {"id": 101, "alreadyExists": False}}

    def fetch_upload_status(self, upload_id):
        return {"data": {"uploadID": upload_id, "status": 3, "overAllGoodsNumber": 1, "successGoodsNumber": 1}}

    def fetch_upload_goods(self, *, upload_id, limit, offset):
        return {"data": {"historyGoods": []}}

    def fetch_quarantine_goods(self, *, limit, offset):
        return {"data": {"quarantineGoods": []}}


class FakeAds:
    def __init__(self) -> None:
        self.bid = 1500

    def fetch_campaign_count(self):
        return {"adverts": [{"status": 9, "advert_list": [{"advertId": 77}]}]}

    def fetch_adverts(self, advert_ids, *, statuses=None, payment_type=""):
        return {"adverts": [{"id": 77, "status": 9, "bid_type": "manual", "settings": {"name": "SKU campaign", "payment_type": "cpm", "placements": {"search": True}}, "nm_settings": [{"nm_id": NM_ID, "bids_kopecks": {"search": self.bid}}]}]}

    def fetch_min_bids(self, *, advert_id, nm_ids, payment_type, placement_types):
        return {"bids": [{"nm_id": NM_ID, "bids": [{"type": "search", "value": 1000}]}]}

    def fetch_recommendations(self, *, advert_id, nm_id):
        return {"base": {"competitiveBid": {"bidKopecks": 1600}}}

    def fetch_fullstats(self, advert_ids, *, begin_date, end_date):
        return []

    def patch_bids(self, payload):
        self.bid = int(payload["bids"][0]["nm_bids"][0]["bid_kopecks"])
        return {"result": "ok"}


class FakeBuyer:
    def fetch(self, request):
        return {"snapshot_date": request.snapshot_date, "data": {"items": [{"nmId": NM_ID, "public_buyer_price": 777.0}]}, "diagnostics": {"fresh": True}}


class FakeStocksBlock:
    def execute(self, request):
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
        return SimpleNamespace(result=SimpleNamespace(kind="success", items=[item]))


class FakeSalesHistory:
    def load_order_count_samples_by_date(self, **kwargs):
        return {NM_ID: [((date(2026, 7, 12) - __import__("datetime").timedelta(days=index)).isoformat(), 10.0) for index in range(30)]}


def main() -> None:
    _forecast_checks()
    _price_configuration_checks()
    _wb_supply_double_count_check()
    with TemporaryDirectory(prefix="sku-management-smoke-") as tmp:
        runtime_dir = Path(tmp)
        runtime = _seed_runtime(runtime_dir)
        prices_source = FakePrices()
        ads_source = FakeAds()
        prices = WbPricesManagementBlock(runtime=runtime, runtime_dir=runtime_dir, source=prices_source, now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", safety_config=WbPricesSafetyConfig(True, 300))
        ads = SheetVitrinaV1AdsBlock(runtime=runtime, runtime_dir=runtime_dir, source=ads_source, now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", cache_ttl_seconds=0, safety_config=AdsSafetyConfig(True, 100000, __import__("decimal").Decimal("100"), 100000, 300))
        block = SkuManagementBlock(runtime=runtime, runtime_dir=runtime_dir, prices_block=prices, ads_block=ads, stocks_block=FakeStocksBlock(), sales_history=FakeSalesHistory(), buyer_price_source=FakeBuyer(), now_factory=lambda: NOW, timestamp_factory=lambda: "2026-07-13T08:00:00Z", sleep=lambda _: None, readback_attempts=2, readback_delay_seconds=0)
        _settings_and_table(block)
        _price_write(block, runtime, prices_source)
        _bid_write_and_stabilization(block, runtime)
        _daily_projection(runtime)
    with TemporaryDirectory(prefix="sku-management-default-gates-") as tmp:
        runtime_dir = Path(tmp)
        runtime = _seed_runtime(runtime_dir)
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir, runtime=runtime)
        if not entrypoint.sku_management_block.prices_block.safety.write_enabled or not entrypoint.sku_management_block.ads_block.safety.write_enabled:
            raise AssertionError("SKU management write flow must not depend on disabled-by-default legacy flags")
    print("sku_management_smoke: OK")


def _forecast_checks() -> None:
    settings = ForecastSettings(forecast_horizon_days=80, future_order_period_days=14, production_lead_days=7, factory_to_ff_lead_days=5, ff_to_wb_lead_days=3, safety_stock_days=5, order_batch_qty=10)
    result = calculate_depletion_forecast(
        as_of_date="2026-07-13", stock_wb=50, stock_ff=20, daily_demand=10, settings=settings,
        real_inbounds=[ForecastInbound("2026-07-16", 30, "supplier_shipment", "s1"), ForecastInbound("2026-07-20", 40, "wb_supply", "w1")],
        districts={"central": {"stock": 12, "daily_demand": 3}, "volga": {"stock": 80, "daily_demand": 2}},
    )
    if result["deficit_date"] != "2026-07-15":
        raise AssertionError(f"sequential safety depletion mismatch: {result['deficit_date']}")
    if not result["synthetic_orders"] or any(not item["calculation_only"] for item in result["synthetic_orders"]):
        raise AssertionError("forecast must transition to calculation-only synthetic orders")
    if result["first_problem_district"] != "central":
        raise AssertionError(f"district risk mismatch: {result}")
    deduped = calculate_depletion_forecast(as_of_date="2026-07-13", stock_wb=100, stock_ff=0, daily_demand=1, settings=settings, real_inbounds=[ForecastInbound("2026-07-14", 10, "wb_supply", "same"), ForecastInbound("2026-07-14", 10, "wb_supply", "same")])
    day = next(item for item in deduped["timeline"] if item["date"] == "2026-07-14")
    if day["inbound_qty"] != 10:
        raise AssertionError("duplicate inbound evidence must not double count")


def _price_configuration_checks() -> None:
    pair = choose_target_price_configuration(target_seller_price=850, current_price=1000, current_discount=10)
    if pair["price"] * (100 - pair["discount"]) / 100 != 850:
        raise AssertionError(f"target price pair mismatch: {pair}")


def _wb_supply_double_count_check() -> None:
    record = {
        "supply_id": "supply-1",
        "cache_key": "supply:supply-1",
        "normalized": {"status_id": 3, "supply_date": "2026-07-20"},
        "raw_goods": [{"nmID": NM_ID, "quantity": 25}],
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
    if result[NM_ID]["real_inbounds"] or not any("not added twice" in item for item in result[NM_ID]["warnings"]):
        raise AssertionError("WB supply still present in FF balance must not be added a second time")
    runtime.load_ff_stock_operation_by_source_key = lambda source_key: {"operation_id": "writeoff-1"}
    result = {NM_ID: {"real_inbounds": [], "warnings": []}}
    block._append_wb_supply_inbounds(result, settings=ForecastSettings())
    if len(result[NM_ID]["real_inbounds"]) != 1 or result[NM_ID]["real_inbounds"][0].quantity != 25:
        raise AssertionError("WB supply deducted from FF ledger must return as one dated inbound")


def _settings_and_table(block) -> None:
    settings = block.get_settings(user_key="operator")
    if settings["forecast"]["sales_avg_period_days"] != 14 or settings["canonical_store"] != "server_runtime_user_config":
        raise AssertionError(settings)
    saved = block.save_settings(user_key="operator", payload={"base_revision": 0, "forecast": {**settings["forecast"], "sales_avg_period_days": 30}, "table": {"visible_columns": ["product", "risk"], "column_order": ["risk", "product"], "column_widths": {"product": 190}, "filters": {"risk": "high"}, "sort": [{"key": "deficit_date", "direction": "asc"}]}})
    if saved["forecast"]["sales_avg_period_days"] != 30 or saved["table"]["column_widths"]["product"] != 190:
        raise AssertionError(saved)
    table = block.build_table(user_key="operator")
    if not table["rows"] or table["meta"]["writes_enabled"] is not True:
        raise AssertionError(table)
    row = next(item for item in table["rows"] if item["nm_id"] == NM_ID)
    if row["seller_price"] != 900 or row["campaign_count"] != 1 or not row["ad_options"]:
        raise AssertionError(row)


def _price_write(block, runtime, prices_source) -> None:
    preview = block.preview_price({"nm_id": NM_ID, "target_seller_price": 850}, actor="operator")
    facts = preview["preview"]
    if facts["new"]["discountedPrice"] != 850 or facts["current_buyer_price"] != 777:
        raise AssertionError(facts)
    committed = block.commit_price({"preview_id": facts["preview_id"], "confirm": True}, actor="operator")
    if committed["status"] != "success" or committed["confirmed_value"] != 850:
        raise AssertionError(committed)
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

    mismatch = block.preview_price({"nm_id": NM_ID, "target_seller_price": 830}, actor="operator")["preview"]
    prices_source.ignore_upload = True
    try:
        block.commit_price(
            {"preview_id": mismatch["preview_id"], "confirm": True, "override_stabilization": True},
            actor="operator",
        )
    except SkuManagementError as exc:
        if exc.http_status != 409 or (exc.payload.get("readback") or {}).get("seller_price") != 850:
            raise
    else:
        raise AssertionError("price readback mismatch must not produce optimistic success")
    finally:
        prices_source.ignore_upload = False
    latest_error = runtime.list_sku_action_events(status="error", limit=1)["rows"][0]
    if latest_error["confirmed_value"] is not None or latest_error["commit_status"] != "error":
        raise AssertionError("readback mismatch must persist a controlled failure event")


def _bid_write_and_stabilization(block, runtime) -> None:
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
    if committed["confirmed_value"] != 18 or committed["event"]["stabilization_override"] is not True:
        raise AssertionError(committed)
    same = block.preview_bid({"nm_id": NM_ID, "advert_id": 77, "placement": "search", "requested_bid_rub": 19}, actor="operator")["preview"]
    if "same_parameter_stabilization" not in same["warnings"]:
        raise AssertionError(same)


def _daily_projection(runtime) -> None:
    runtime.create_sku_action_event({"event_id": "extra_price", "nm_id": NM_ID, "parameter": PRICE_PARAMETER, "old_value": 850, "requested_value": 840, "confirmed_value": 840, "delta": -10, "requested_at": "2026-07-13T09:00:00Z", "confirmed_at": "2026-07-13T09:01:00Z", "actor": "operator", "source": "sku_management", "commit_status": "confirmed"})
    lookup = runtime.load_sku_action_daily_metric_lookup("2026-07-13")[NM_ID]
    if lookup["seller_price_change_rub"] != -60 or lookup["advertising_bid_change_rub"] != 3:
        raise AssertionError(f"daily aggregation mismatch: {lookup}")
    if runtime.load_sku_action_daily_metric_lookup("2026-07-12"):
        raise AssertionError("no-change day must stay empty")


def _seed_runtime(runtime_dir: Path) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    result = runtime.ingest_bundle(json.loads(BUNDLE.read_text(encoding="utf-8")), activated_at="2026-07-13T08:00:00Z")
    if result.status != "accepted":
        raise AssertionError(result)
    runtime.save_nomenclature_item({"item_id": "sku-management-primary", "is_active": True, "our_sku": "SKU-1", "nm_id": NM_ID, "barcode": "4600000000001", "nomenclature_name": "SKU management fixture", "product_type": "case", "match_key": "sku-management", "created_at": "2026-07-13T08:00:00Z", "updated_at": "2026-07-13T08:00:00Z"})
    runtime.create_ff_stock_operation(operation_id="ff_open", operation_type="manual_receipt", source_type="manual_excel", source_key="ff_open", source_object_id="ff_open", source_object_label="FF opening", created_at="2026-07-13T08:00:00Z", created_by="smoke", lines=[{"nm_id": NM_ID, "quantity_delta": 40}])
    return runtime


if __name__ == "__main__":
    main()
