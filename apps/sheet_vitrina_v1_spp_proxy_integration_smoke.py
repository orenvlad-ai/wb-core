"""Integration smoke for SPP proxy live-plan/web-vitrina wiring."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    EXECUTION_MODE_AUTO_DAILY,
    EXECUTION_MODE_MANUAL_OPERATOR,
    TEMPORAL_ROLE_ACCEPTED_CURRENT,
)
from packages.application.spp_proxy_block import SppProxyBlock  # noqa: E402
from packages.contracts.spp_proxy_block import SppProxyRequest  # noqa: E402


AS_OF_DATE = "2026-05-06"
CURRENT_DATE = "2026-05-07"
ROLLOVER_AS_OF_DATE = CURRENT_DATE
NEXT_CURRENT_DATE = "2026-05-08"
ACTIVATED_AT = "2026-05-07T00:00:00Z"
REQUESTED_NM_IDS = [210183919, 210184534]


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-spp-proxy-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(_bundle(), activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        scenario = _SppProxyScenario(mode="valid")
        now_factory = _MutableNowFactory("2026-05-07T08:00:00+00:00")
        entrypoint = _build_entrypoint(runtime=runtime, now_factory=now_factory, scenario=scenario)

        first_refresh = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if first_refresh["status"] != "success":
            raise AssertionError(f"first refresh must succeed, got {first_refresh}")
        accepted_proxy, accepted_at = runtime.load_temporal_source_slot_snapshot(
            source_key="spp_proxy",
            snapshot_date=CURRENT_DATE,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
        )
        if accepted_proxy is None or accepted_at != "2026-05-07T08:00:00Z":
            raise AssertionError(f"valid SPP proxy current snapshot must be accepted, got {accepted_proxy} at {accepted_at}")
        if _first_item_value(accepted_proxy, "spp_proxy") != 0.23:
            raise AssertionError(f"accepted SPP proxy must be 0.23, got {accepted_proxy}")

        now_factory.value = "2026-05-08T08:00:00+00:00"
        rollover_refresh = entrypoint._run_sheet_refresh(
            as_of_date=ROLLOVER_AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if rollover_refresh["status"] != "success":
            raise AssertionError(f"rollover refresh must succeed, got {rollover_refresh}")
        rollover_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=ROLLOVER_AS_OF_DATE)
        rows = _data_rows(rollover_plan)
        probe_key = f"SKU:{REQUESTED_NM_IDS[0]}|spp_proxy"
        spp_key = f"SKU:{REQUESTED_NM_IDS[0]}|spp"
        if _yesterday_value(rows[probe_key]) != 0.23:
            raise AssertionError(f"day-D accepted SPP proxy must materialize into yesterday_closed, got {rows[probe_key]}")
        if _today_value(rows[probe_key]) != 0.2:
            raise AssertionError(f"day D+1 SPP proxy must materialize as 0.2, got {rows[probe_key]}")
        if _today_value(rows[spp_key]) != 0.12:
            raise AssertionError(f"existing SPP metric must remain unchanged, got {rows[spp_key]}")

        contract_payload = entrypoint.handle_sheet_web_vitrina_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date=ROLLOVER_AS_OF_DATE,
        )
        contract_rows = {row["row_id"]: row for row in contract_payload["rows"]}
        if probe_key not in contract_rows:
            raise AssertionError(f"web-vitrina contract must expose SPP proxy row, got {contract_rows.keys()}")
        if contract_rows[probe_key]["metric_label"] != "SPP-прокси":
            raise AssertionError(f"SPP proxy label mismatch, got {contract_rows[probe_key]}")
        if contract_rows[probe_key]["values_by_date"][NEXT_CURRENT_DATE] != 0.2:
            raise AssertionError(f"web-vitrina SPP proxy value mismatch, got {contract_rows[probe_key]}")

        scenario.mode = "invalid"
        now_factory.value = "2026-05-08T14:00:00+00:00"
        invalid_refresh = entrypoint._run_sheet_refresh(
            as_of_date=ROLLOVER_AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        )
        if invalid_refresh["status"] != "success":
            raise AssertionError(f"invalid later refresh must preserve accepted values, got {invalid_refresh}")
        invalid_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=ROLLOVER_AS_OF_DATE)
        invalid_rows = _data_rows(invalid_plan)
        invalid_status = _status_rows(invalid_plan)["spp_proxy[today_current]"]
        if _today_value(invalid_rows[probe_key]) != 0.2:
            raise AssertionError(f"invalid public-card attempt must preserve SPP proxy current value, got {invalid_rows[probe_key]}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(invalid_status[10]):
            raise AssertionError(f"SPP proxy STATUS must explain preserved accepted current, got {invalid_status}")
        if _today_value(invalid_rows[spp_key]) != 0.12:
            raise AssertionError("invalid SPP proxy attempt must not change existing SPP")

        composition_payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            as_of_date=ROLLOVER_AS_OF_DATE,
            include_source_status=True,
        )
        loading_rows = {
            row["source_key"]: row
            for row in composition_payload["activity_surface"]["loading_table"]["rows"]
        }
        proxy_row = loading_rows.get("spp_proxy")
        if not proxy_row:
            raise AssertionError(f"loading table must expose spp_proxy source, got {loading_rows.keys()}")
        if proxy_row["source_group_id"] != "wb_public_card_bot":
            raise AssertionError(f"SPP proxy must live in WB public card group, got {proxy_row}")
        if "SPP-прокси" not in proxy_row["metric_labels"]:
            raise AssertionError(f"SPP proxy source row must expose metric label, got {proxy_row}")
        if not proxy_row["today"]["ok"]:
            raise AssertionError(f"preserved SPP proxy current value must be latest-confirmed OK, got {proxy_row}")
        if not loading_rows["spp"]["today"]["ok"] or not loading_rows["prices_snapshot"]["today"]["ok"]:
            raise AssertionError("SPP proxy source-status must not degrade unrelated SPP/prices groups")

        scenario.mode = "group_valid_changed"
        group_refresh = entrypoint._run_sheet_source_group_refresh(
            source_group_id="wb_public_card_bot",
            selected_as_of_date=NEXT_CURRENT_DATE,
            target_snapshot_as_of_date=ROLLOVER_AS_OF_DATE,
            log=None,
        )
        if group_refresh["status"] != "success":
            raise AssertionError(f"SPP proxy group refresh must succeed, got {group_refresh}")
        group_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=ROLLOVER_AS_OF_DATE)
        group_rows = _data_rows(group_plan)
        if _today_value(group_rows[probe_key]) != 0.181818:
            raise AssertionError(f"group refresh must update selected SPP proxy date, got {group_rows[probe_key]}")
        if _today_value(group_rows[spp_key]) != 0.12:
            raise AssertionError("SPP proxy group refresh must not change existing SPP row")

    print("sheet_vitrina_v1_spp_proxy_integration: ok")


def _bundle() -> dict[str, Any]:
    return {
        "bundle_version": "spp_proxy_fixture_v1",
        "uploaded_at": ACTIVATED_AT,
        "config_v2": [
            {
                "nm_id": nm_id,
                "enabled": True,
                "display_name": f"SKU {index}",
                "group": "fixture",
                "display_order": index,
            }
            for index, nm_id in enumerate(REQUESTED_NM_IDS, start=1)
        ],
        "metrics_v2": [
            {
                "metric_key": "spp",
                "enabled": True,
                "scope": "SKU",
                "label_ru": "СПП",
                "calc_type": "metric",
                "calc_ref": "spp",
                "show_in_data": True,
                "format": "percent",
                "display_order": 30,
                "section": "Цены",
            },
            {
                "metric_key": "spp_proxy",
                "enabled": True,
                "scope": "SKU",
                "label_ru": "SPP-прокси",
                "calc_type": "metric",
                "calc_ref": "spp_proxy",
                "show_in_data": True,
                "format": "percent",
                "display_order": 31,
                "section": "Цены",
            },
            *_hidden_dependency_metrics(),
        ],
        "formulas_v2": [],
    }


def _hidden_dependency_metrics() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, (metric_key, fmt) in enumerate(
        (
            ("orderSum", "rub"),
            ("orderCount", "integer"),
            ("ads_sum", "rub"),
        ),
        start=1000,
    ):
        rows.append(
            {
                "metric_key": metric_key,
                "enabled": True,
                "scope": "SKU",
                "label_ru": metric_key,
                "calc_type": "metric",
                "calc_ref": metric_key,
                "show_in_data": False,
                "format": fmt,
                "display_order": index,
                "section": "Hidden",
            }
        )
    rows.append(
        {
            "metric_key": "total_orderSum",
            "enabled": True,
            "scope": "TOTAL",
            "label_ru": "total_orderSum",
            "calc_type": "metric",
            "calc_ref": "orderSum",
            "show_in_data": False,
            "format": "rub",
            "display_order": 1003,
            "section": "Hidden",
        }
    )
    return rows


def _build_entrypoint(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    now_factory: "_MutableNowFactory",
    scenario: "_SppProxyScenario",
) -> RegistryUploadHttpEntrypoint:
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime.runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: ACTIVATED_AT,
        refreshed_at_factory=_SequenceTimestampFactory(
            [
                "2026-05-07T08:05:00Z",
                "2026-05-08T08:05:00Z",
                "2026-05-08T14:05:00Z",
                "2026-05-08T15:05:00Z",
            ]
        ),
        now_factory=now_factory,
    )
    entrypoint.sheet_plan_block = _build_live_plan(runtime=runtime, now_factory=now_factory, scenario=scenario)
    return entrypoint


def _build_live_plan(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    now_factory: "_MutableNowFactory",
    scenario: "_SppProxyScenario",
):
    from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock

    return SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        now_factory=now_factory,
        current_web_source_sync=_NoopCurrentWebSourceSync(),
        seller_funnel_block=_GenericSuccessBlock("seller_funnel_snapshot"),
        web_source_block=_GenericSuccessBlock("web_source_snapshot"),
        sales_funnel_history_block=_GenericSuccessBlock("sales_funnel_history"),
        prices_snapshot_block=_SyntheticPricesBlock(),
        sf_period_block=_GenericSuccessBlock("sf_period"),
        spp_block=_SyntheticSppBlock(),
        spp_proxy_block=SppProxyBlock(_SyntheticPublicBuyerPriceSource(scenario)),
        ads_bids_block=_SyntheticAdsBidsBlock(),
        stocks_block=_GenericSuccessBlock("stocks"),
        onec_stocks_block=_SyntheticOnecStocksBlock(),
        ads_compact_block=_GenericSuccessBlock("ads_compact"),
        fin_report_daily_block=_GenericSuccessBlock("fin_report_daily"),
        promo_live_source_block=_GenericSuccessBlock("promo_by_price"),
    )


class _SppProxyScenario:
    def __init__(self, *, mode: str) -> None:
        self.mode = mode


class _SyntheticPublicBuyerPriceSource:
    def __init__(self, scenario: _SppProxyScenario) -> None:
        self.scenario = scenario

    def fetch(self, request: SppProxyRequest) -> Mapping[str, Any]:
        if self.scenario.mode == "invalid":
            items: list[dict[str, Any]] = []
        else:
            price_by_date = {
                CURRENT_DATE: 770.0,
                NEXT_CURRENT_DATE: 880.0,
            }
            if self.scenario.mode == "group_valid_changed":
                price_by_date[NEXT_CURRENT_DATE] = 900.0
            base_price = price_by_date[str(request.snapshot_date)]
            items = [
                {
                    "nmId": nm_id,
                    "public_buyer_price": base_price + index * 10,
                    "parse_method": "fixture",
                }
                for index, nm_id in enumerate(request.nm_ids)
            ]
        return {
            "snapshot_date": request.snapshot_date,
            "requested_nm_ids": request.nm_ids,
            "source": {
                "mode": "fixture_public_card",
                "endpoint": "fixture",
                "temporal_capability": "current_only",
                "auth_context": "anonymous",
            },
            "data": {"items": items},
        }


class _SyntheticPricesBlock:
    def execute(self, request: object) -> SimpleNamespace:
        snapshot_date = str(getattr(request, "snapshot_date"))
        discounted_by_date = {
            CURRENT_DATE: 1000.0,
            NEXT_CURRENT_DATE: 1100.0,
        }
        base_discounted = discounted_by_date.get(snapshot_date, 1000.0)
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=snapshot_date,
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        price_seller=base_discounted + 100 + index,
                        price_seller_discounted=base_discounted + index * 10,
                    )
                    for index, nm_id in enumerate(REQUESTED_NM_IDS)
                ],
            )
        )


class _SyntheticSppBlock:
    def execute(self, request: object) -> SimpleNamespace:
        snapshot_date = str(getattr(request, "snapshot_date"))
        base = 0.11 if snapshot_date == CURRENT_DATE else 0.12
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=snapshot_date,
                count=len(REQUESTED_NM_IDS),
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        spp=round(base + index / 1000, 6),
                    )
                    for index, nm_id in enumerate(REQUESTED_NM_IDS)
                ],
            )
        )


class _SyntheticAdsBidsBlock:
    def execute(self, request: object) -> SimpleNamespace:
        snapshot_date = str(getattr(request, "snapshot_date"))
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=snapshot_date,
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        ads_bid_search=10.0 + index,
                        ads_bid_recommendations=8.0 + index,
                    )
                    for index, nm_id in enumerate(REQUESTED_NM_IDS)
                ],
            )
        )


class _SyntheticOnecStocksBlock:
    def execute(self, request: object) -> SimpleNamespace:
        request_date = str(getattr(request, "date"))
        items = []
        for nm_id in REQUESTED_NM_IDS:
            for stage in ("CHINA_TO_FF", "FF_STOCK", "FF_TO_WB", "WB_STOCK"):
                items.append(
                    SimpleNamespace(
                        nm_id=nm_id,
                        stage_name=stage,
                        canonical_stage_code=stage,
                        qty=1.0,
                        unit_cost_rub=100.0,
                        cost_total_rub=100.0,
                    )
                )
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                meta=SimpleNamespace(date=request_date),
                item_count=len(REQUESTED_NM_IDS),
                stage_count=len(items),
                dynamic_stage_names=["CHINA_TO_FF", "FF_STOCK", "FF_TO_WB", "WB_STOCK"],
                items=items,
                detail="fixture 1C success",
            )
        )


class _GenericSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        if self.source_key == "sales_funnel_history":
            return SimpleNamespace(
                result=SimpleNamespace(
                    kind="success",
                    snapshot_date=request_date,
                    date=request_date,
                    date_from=request_date,
                    date_to=request_date,
                    items=[
                        SimpleNamespace(nm_id=nm_id, metric=metric, date=request_date, value=value)
                        for nm_id in REQUESTED_NM_IDS
                        for metric, value in (("orderSum", 1000.0), ("orderCount", 2.0))
                    ],
                    detail=f"{self.source_key} fixture success",
                    storage_total=None,
                )
            )
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=request_date,
                date=request_date,
                date_from=request_date,
                date_to=request_date,
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        view_count=100.0,
                        open_card_count=20.0,
                        views_current=200.0,
                        ctr_current=5.0,
                        orders_current=10.0,
                        ads_sum=50.0,
                        ads_sum_price=100.0,
                    )
                    for nm_id in REQUESTED_NM_IDS
                ],
                detail=f"{self.source_key} fixture success",
                storage_total=SimpleNamespace(fin_storage_fee_total=0.0)
                if self.source_key == "fin_report_daily"
                else None,
            )
        )


class _NoopCurrentWebSourceSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return

    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        return


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)


class _SequenceTimestampFactory:
    def __init__(self, values: list[str]) -> None:
        self.values = values
        self.index = 0

    def __call__(self) -> str:
        if self.index >= len(self.values):
            return self.values[-1]
        value = self.values[self.index]
        self.index += 1
        return value


def _request_date(request: object) -> str:
    for attr in ("snapshot_date", "date", "date_to"):
        value = getattr(request, attr, None)
        if value:
            return str(value)
    return CURRENT_DATE


def _data_rows(plan: object) -> dict[str, list[Any]]:
    data = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
    return {str(row[1]): list(row) for row in data.rows}


def _status_rows(plan: object) -> dict[str, list[Any]]:
    status = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {str(row[0]): list(row) for row in status.rows}


def _yesterday_value(row: list[Any]) -> Any:
    return row[2]


def _today_value(row: list[Any]) -> Any:
    return row[3]


def _first_item_value(payload: object, attr: str) -> Any:
    items = list(getattr(payload, "items", []) or [])
    if not items:
        return None
    return getattr(items[0], attr, None)


if __name__ == "__main__":
    main()
