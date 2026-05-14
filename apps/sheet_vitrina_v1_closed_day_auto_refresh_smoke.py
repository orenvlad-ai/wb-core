"""Regression smoke for scheduled closed-day web-vitrina refresh freshness."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    EXECUTION_MODE_AUTO_DAILY,
    EXECUTION_MODE_MANUAL_OPERATOR,
    TEMPORAL_ROLE_ACCEPTED_CLOSED,
    SheetVitrinaV1LivePlanBlock,
)


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-05-13T09:00:00Z"
PREVIOUS_CLOSED_DATE = "2026-05-12"
CLOSED_DATE = "2026-05-13"
NEXT_CURRENT_DATE = "2026-05-14"
EVENING_D_2000_EKT = "2026-05-13T15:00:00+00:00"
MORNING_D1_1100_EKT = "2026-05-14T06:00:00+00:00"
STALE_CACHE_CAPTURED_AT = "2026-05-13T15:00:00Z"
FRESH_CACHE_CAPTURED_AT = "2026-05-13T19:30:00Z"
SCHEDULE_ID = "daily_11_00_ekt"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    _assert_scheduled_reloads_stale_evening_cache(bundle, requested_nm_ids, probe_nm_id)
    _assert_stale_cache_without_fresh_source_is_not_success(bundle, requested_nm_ids, probe_nm_id)
    _assert_post_midnight_ekt_cache_is_closed_day_fresh(bundle, requested_nm_ids, probe_nm_id)

    print("scheduled_reloads_stale_evening_cache: ok")
    print("stale_cache_without_fresh_source_not_success: ok")
    print("closed_day_timezone_cutoff: ok -> 20:00 EKT stale, 00:30 EKT fresh")
    print("smoke-check passed")


def _assert_scheduled_reloads_stale_evening_cache(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
    probe_nm_id: int,
) -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-closed-day-scheduled-") as tmp:
        runtime, entrypoint, now_factory, history = _build_entrypoint(bundle, requested_nm_ids, tmp)

        now_factory.value = EVENING_D_2000_EKT
        history.values_by_date[CLOSED_DATE] = _HistoryValues(order_count=21.0, order_sum=2100.0)
        entrypoint._run_sheet_refresh(
            as_of_date=PREVIOUS_CLOSED_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        cached_payload, cached_at = runtime.load_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=CLOSED_DATE,
        )
        if cached_payload is None or cached_at != STALE_CACHE_CAPTURED_AT:
            raise AssertionError(f"evening run must seed stale exact-date cache, got captured_at={cached_at}")

        now_factory.value = MORNING_D1_1100_EKT
        history.calls.clear()
        history.values_by_date[CLOSED_DATE] = _HistoryValues(order_count=32.0, order_sum=3200.0)
        scheduled_payload = entrypoint._run_sheet_scheduled_auto_update(
            schedule_id=SCHEDULE_ID,
            due_at="2026-05-14T06:00:00Z",
            trigger_source="scheduled",
            log=None,
        )
        if scheduled_payload["as_of_date"] != CLOSED_DATE:
            raise AssertionError(f"scheduled D+1 11:00 EKT must target D-1, got {scheduled_payload['as_of_date']}")
        if CLOSED_DATE not in history.calls:
            raise AssertionError(f"scheduled refresh must re-fetch closed date despite stale cache, calls={history.calls}")

        scheduled_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=CLOSED_DATE)
        scheduled_rows = _data_rows(scheduled_plan)
        scheduled_order_count = _yesterday_value(scheduled_rows[f"SKU:{probe_nm_id}|orderCount"])
        if scheduled_order_count != 32.0:
            raise AssertionError(f"scheduled refresh must use final closed-day value, got {scheduled_order_count}")
        scheduled_status = _status_rows(scheduled_plan)["sales_funnel_history[yesterday_closed]"]
        if scheduled_status[1] != "success" or "accepted_closed_current_attempt" not in str(scheduled_status[10]):
            raise AssertionError(f"scheduled refresh must accept fresh closed-day attempt, got {scheduled_status}")

        history.calls.clear()
        manual_payload = entrypoint._run_sheet_refresh(
            as_of_date=CLOSED_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        )
        manual_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=CLOSED_DATE)
        manual_order_count = _yesterday_value(_data_rows(manual_plan)[f"SKU:{probe_nm_id}|orderCount"])
        if manual_payload["as_of_date"] != scheduled_payload["as_of_date"]:
            raise AssertionError("manual and scheduled refresh must resolve the same as_of_date")
        if manual_order_count != scheduled_order_count:
            raise AssertionError(
                f"manual and scheduled closed-day values must match, got manual={manual_order_count}, scheduled={scheduled_order_count}"
            )


def _assert_stale_cache_without_fresh_source_is_not_success(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
    probe_nm_id: int,
) -> None:
    del probe_nm_id
    with TemporaryDirectory(prefix="sheet-vitrina-closed-day-stale-cache-") as tmp:
        runtime, entrypoint, now_factory, history = _build_entrypoint(bundle, requested_nm_ids, tmp)
        now_factory.value = MORNING_D1_1100_EKT
        history.values_by_date[CLOSED_DATE] = _HistoryValues(order_count=21.0, order_sum=2100.0)
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=CLOSED_DATE,
            captured_at=STALE_CACHE_CAPTURED_AT,
            payload=history.success_payload(CLOSED_DATE),
        )

        history.calls.clear()
        history.not_found_dates.add(CLOSED_DATE)
        payload = entrypoint._run_sheet_scheduled_auto_update(
            schedule_id=SCHEDULE_ID,
            due_at="2026-05-14T06:00:00Z",
            trigger_source="scheduled",
            log=None,
        )
        if CLOSED_DATE not in history.calls:
            raise AssertionError(f"stale cache must not skip upstream closed-day verification, calls={history.calls}")
        if payload["semantic_status"] == "success":
            raise AssertionError(f"stale closed-day cache without fresh source must not yield success: {payload}")
        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=CLOSED_DATE)
        status_row = _status_rows(plan)["sales_funnel_history[yesterday_closed]"]
        if status_row[1] == "success":
            raise AssertionError(f"stale cache without fresh source must not become success, got {status_row}")
        accepted_payload, accepted_at = runtime.load_temporal_source_slot_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=CLOSED_DATE,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        )
        if accepted_payload is not None or accepted_at is not None:
            raise AssertionError("stale exact-date cache must not be promoted into accepted closed-day slot")


def _assert_post_midnight_ekt_cache_is_closed_day_fresh(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
    probe_nm_id: int,
) -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-closed-day-fresh-cache-") as tmp:
        runtime, entrypoint, now_factory, history = _build_entrypoint(bundle, requested_nm_ids, tmp)
        now_factory.value = MORNING_D1_1100_EKT
        history.values_by_date[CLOSED_DATE] = _HistoryValues(order_count=41.0, order_sum=4100.0)
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=CLOSED_DATE,
            captured_at=FRESH_CACHE_CAPTURED_AT,
            payload=history.success_payload(CLOSED_DATE),
        )

        history.calls.clear()
        history.not_found_dates.add(CLOSED_DATE)
        entrypoint._run_sheet_refresh(
            as_of_date=CLOSED_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if CLOSED_DATE in history.calls:
            raise AssertionError(f"post-midnight EKT cache is already closed-day fresh and should not be refetched: {history.calls}")
        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=CLOSED_DATE)
        order_count = _yesterday_value(_data_rows(plan)[f"SKU:{probe_nm_id}|orderCount"])
        if order_count != 41.0:
            raise AssertionError(f"fresh closed-day cache must remain usable, got {order_count}")
        status_row = _status_rows(plan)["sales_funnel_history[yesterday_closed]"]
        if status_row[1] != "success" or "accepted_closed_current_attempt" not in str(status_row[10]):
            raise AssertionError(f"fresh cache should be promoted to accepted closed-day snapshot, got {status_row}")


def _build_entrypoint(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
    tmp: str,
) -> tuple[RegistryUploadDbBackedRuntime, RegistryUploadHttpEntrypoint, "_MutableNowFactory", "_HistoryBlock"]:
    runtime_dir = Path(tmp) / "runtime"
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
    if result.status != "accepted":
        raise AssertionError(f"fixture bundle must be accepted, got {result}")
    _ingest_cost_prices(runtime, bundle)

    now_factory = _MutableNowFactory(EVENING_D_2000_EKT)
    history = _HistoryBlock(requested_nm_ids)
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime_dir,
        runtime=runtime,
        activated_at_factory=now_factory.iso_z,
        refreshed_at_factory=now_factory.iso_z,
        now_factory=now_factory,
    )
    entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        seller_funnel_block=_SyntheticSourceBlock("seller_funnel_snapshot", requested_nm_ids),
        sales_funnel_history_block=history,
        web_source_block=_SyntheticSourceBlock("web_source_snapshot", requested_nm_ids),
        prices_snapshot_block=_SyntheticSourceBlock("prices_snapshot", requested_nm_ids),
        sf_period_block=_SyntheticSourceBlock("sf_period", requested_nm_ids),
        spp_block=_SyntheticSourceBlock("spp", requested_nm_ids),
        ads_bids_block=_SyntheticSourceBlock("ads_bids", requested_nm_ids),
        stocks_block=_SyntheticSourceBlock("stocks", requested_nm_ids),
        ads_compact_block=_SyntheticSourceBlock("ads_compact", requested_nm_ids),
        fin_report_daily_block=_SyntheticSourceBlock("fin_report_daily", requested_nm_ids),
        current_web_source_sync=_NoopCurrentSync(),
        closed_day_web_source_sync=_NoopClosedDaySync(),
        now_factory=now_factory,
    )
    return runtime, entrypoint, now_factory, history


def _ingest_cost_prices(runtime: RegistryUploadDbBackedRuntime, bundle: dict[str, Any]) -> None:
    groups = sorted({str(item["group"]) for item in bundle["config_v2"] if item.get("enabled")})
    result = runtime.ingest_cost_price_payload(
        {
            "dataset_version": "closed-day-auto-refresh-smoke-costs",
            "uploaded_at": ACTIVATED_AT,
            "cost_price_rows": [
                {"group": group, "cost_price_rub": 100.0, "effective_from": "2026-01-01"}
                for group in groups
            ],
        },
        activated_at=ACTIVATED_AT,
    )
    if result.status != "accepted":
        raise AssertionError(f"cost price fixture must be accepted, got {result}")


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)

    def iso_z(self) -> str:
        return self().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class _NoopCurrentSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return


class _NoopClosedDaySync:
    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        return


class _HistoryValues:
    def __init__(self, *, order_count: float, order_sum: float) -> None:
        self.order_count = order_count
        self.order_sum = order_sum


class _HistoryBlock:
    def __init__(self, requested_nm_ids: list[int]) -> None:
        self.requested_nm_ids = requested_nm_ids
        self.values_by_date: dict[str, _HistoryValues] = {}
        self.not_found_dates: set[str] = set()
        self.calls: list[str] = []

    def execute(self, request: object) -> SimpleNamespace:
        request_date = str(getattr(request, "date_to"))
        self.calls.append(request_date)
        if request_date in self.not_found_dates:
            return SimpleNamespace(
                result=SimpleNamespace(
                    kind="not_found",
                    date_from=request_date,
                    date_to=request_date,
                    count=0,
                    items=[],
                    detail="synthetic final closed-day source not materialized",
                )
            )
        return SimpleNamespace(result=self.success_payload(request_date))

    def success_payload(self, request_date: str) -> SimpleNamespace:
        values = self.values_by_date.get(request_date) or _HistoryValues(order_count=10.0, order_sum=1000.0)
        items = []
        for index, nm_id in enumerate(self.requested_nm_ids):
            order_count = values.order_count + index
            order_sum = values.order_sum + index * 10.0
            for metric, value in {
                "orderCount": order_count,
                "orderSum": order_sum,
                "openCount": order_count * 2.0,
                "cartCount": order_count,
                "addToCartConversion": 50.0,
                "cartToOrderConversion": 100.0,
                "addToWishlistCount": 1.0,
            }.items():
                items.append(SimpleNamespace(date=request_date, nm_id=nm_id, metric=metric, value=float(value)))
        return SimpleNamespace(
            kind="success",
            date_from=request_date,
            date_to=request_date,
            count=len(items),
            items=items,
        )


class _SyntheticSourceBlock:
    def __init__(self, source_key: str, requested_nm_ids: list[int]) -> None:
        self.source_key = source_key
        self.requested_nm_ids = requested_nm_ids

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        items = [
            SimpleNamespace(nm_id=nm_id, **_item_fields(self.source_key, index))
            for index, nm_id in enumerate(self.requested_nm_ids)
        ]
        payload = {
            "kind": "success",
            "items": items,
            "count": len(items),
            "snapshot_date": request_date,
            "date": request_date,
            "date_from": request_date,
            "date_to": request_date,
            "detail": f"{self.source_key} synthetic success for {request_date}",
            "storage_total": SimpleNamespace(fin_storage_fee_total=3.0),
        }
        return SimpleNamespace(result=SimpleNamespace(**payload))


def _item_fields(source_key: str, index: int) -> dict[str, float | str]:
    base = float(index + 1)
    if source_key == "seller_funnel_snapshot":
        return {"name": "SKU", "vendor_code": "VC", "view_count": 10.0 + base, "open_card_count": 5.0 + base, "ctr": 12.0}
    if source_key == "web_source_snapshot":
        return {"views_current": 20.0 + base, "ctr_current": 7.0, "orders_current": 3.0 + base, "position_avg": 11.0}
    if source_key == "prices_snapshot":
        return {"price_seller": 250.0, "price_seller_discounted": 200.0}
    if source_key == "sf_period":
        return {"localization_percent": 80.0, "feedback_rating": 4.8}
    if source_key == "spp":
        return {"spp": 15.0}
    if source_key == "ads_bids":
        return {"ads_bid_search": 12.0, "ads_bid_recommendations": 9.0}
    if source_key == "stocks":
        return {
            "stock_total": 100.0 + base,
            "stock_ru_central": 10.0,
            "stock_ru_northwest": 10.0,
            "stock_ru_volga": 10.0,
            "stock_ru_south_caucasus": 10.0,
            "stock_ru_ural": 10.0,
            "stock_ru_far_siberia": 10.0,
        }
    if source_key == "ads_compact":
        return {
            "ads_views": 100.0,
            "ads_clicks": 10.0,
            "ads_atbs": 4.0,
            "ads_orders": 2.0,
            "ads_sum": 50.0,
            "ads_sum_price": 400.0,
            "ads_cpc": 5.0,
            "ads_ctr": 10.0,
            "ads_cr": 20.0,
        }
    if source_key == "fin_report_daily":
        return {
            "fin_buyout_rub": 500.0,
            "fin_delivery_rub": 20.0,
            "fin_commission_wb_portal": 30.0,
            "fin_acquiring_fee": 10.0,
            "fin_loyalty_rub": 5.0,
        }
    return {}


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError(f"request does not carry a date field: {request!r}")


def _status_rows(plan: object) -> dict[str, list[object]]:
    status_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {str(row[0]): row for row in status_sheet.rows}


def _data_rows(plan: object) -> dict[str, list[object]]:
    data_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
    return {str(row[1]): row for row in data_sheet.rows}


def _yesterday_value(row: list[object]) -> object:
    return row[2]


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
