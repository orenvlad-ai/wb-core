"""Targeted smoke-check for provisional current vs accepted closed-day retry semantics."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
FIRST_AS_OF_DATE = "2026-04-12"
SECOND_AS_OF_DATE = "2026-04-13"
FIRST_CURRENT_DATE = "2026-04-13"
SECOND_CURRENT_DATE = "2026-04-14"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    with TemporaryDirectory(prefix="sheet-vitrina-closure-retry-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {result}")

        state = _TemporalState(requested_nm_ids=requested_nm_ids)
        plan_block = _build_live_plan(runtime=runtime, state=state, current_date=FIRST_CURRENT_DATE)
        first_plan = plan_block.build_plan(as_of_date=FIRST_AS_OF_DATE)
        first_status_rows = _status_rows(first_plan)
        if first_status_rows["web_source_snapshot[yesterday_closed]"][1] != "closure_retrying":
            raise AssertionError("first refresh must immediately enter closed-day retry state for missing web source")
        if first_status_rows["seller_funnel_snapshot[yesterday_closed]"][1] != "closure_retrying":
            raise AssertionError("first refresh must immediately enter closed-day retry state for missing seller funnel")
        if first_status_rows["stocks[yesterday_closed]"][1] != "closure_retrying":
            raise AssertionError("first refresh must immediately enter closed-day retry state for missing historical stocks")
        if first_status_rows["web_source_snapshot[today_current]"][1] != "success":
            raise AssertionError("current-day provisional snapshot must materialize as success")

        plan_block = _build_live_plan(runtime=runtime, state=state, current_date=SECOND_CURRENT_DATE, current_hour=8)
        second_plan = plan_block.build_plan(as_of_date=SECOND_AS_OF_DATE)
        second_status_rows = _status_rows(second_plan)
        for key in ("web_source_snapshot[yesterday_closed]", "seller_funnel_snapshot[yesterday_closed]"):
            if second_status_rows[key][1] != "closure_retrying":
                raise AssertionError(f"{key} must not inherit provisional current data into closed slot")
            if "closure_state=closure_retrying" not in str(second_status_rows[key][10]):
                raise AssertionError(f"{key} must expose retry state in note")
        if second_status_rows["stocks[yesterday_closed]"][1] != "success":
            raise AssertionError("stocks yesterday_closed must promote the exact-date cache instead of inventing a fresh closed slot")
        if "exact_date_stocks_history_runtime_cache" not in str(second_status_rows["stocks[yesterday_closed]"][10]):
            raise AssertionError("stocks yesterday_closed must disclose exact-date runtime cache resolution")
        second_data_rows = _data_rows(second_plan)
        if second_data_rows[f"SKU:{probe_nm_id}|views_current"][2:] != ["", 200.0]:
            raise AssertionError("search metric must stay blank for closed slot while closure is retrying")
        if second_data_rows[f"SKU:{probe_nm_id}|view_count"][2:] != ["", 400.0]:
            raise AssertionError("seller metric must stay blank for closed slot while closure is retrying")
        if second_data_rows[f"SKU:{probe_nm_id}|stock_total"][2:] != [17.0, 18.0]:
            raise AssertionError("stocks must reuse exact-date cache for yesterday_closed and keep today_current")

        state.enable_acceptance(SECOND_AS_OF_DATE)
        plan_block = _build_live_plan(runtime=runtime, state=state, current_date=SECOND_CURRENT_DATE, current_hour=10)
        third_plan = plan_block.build_plan(as_of_date=SECOND_AS_OF_DATE)
        third_status_rows = _status_rows(third_plan)
        for key in ("web_source_snapshot[yesterday_closed]", "seller_funnel_snapshot[yesterday_closed]", "stocks[yesterday_closed]"):
            if third_status_rows[key][1] != "success":
                raise AssertionError(f"{key} must materialize accepted closed-day truth after retry")
            if "accepted_closed" not in str(third_status_rows[key][10]):
                raise AssertionError(f"{key} must disclose accepted closed-day resolution")
        third_data_rows = _data_rows(third_plan)
        if third_data_rows[f"SKU:{probe_nm_id}|views_current"][2:] != [100.0, 200.0]:
            raise AssertionError("search metric must expose accepted yesterday + current today values")
        if third_data_rows[f"SKU:{probe_nm_id}|view_count"][2:] != [300.0, 400.0]:
            raise AssertionError("seller metric must expose accepted yesterday + current today values")
        if third_data_rows[f"SKU:{probe_nm_id}|stock_total"][2:] != [17.0, 18.0]:
            raise AssertionError("stocks must expose accepted yesterday + current today values after retry")

        state.invalidate_after_acceptance(SECOND_AS_OF_DATE)
        preserved_plan = plan_block.build_plan(as_of_date=SECOND_AS_OF_DATE)
        preserved_status_rows = _status_rows(preserved_plan)
        for key in ("web_source_snapshot[yesterday_closed]", "seller_funnel_snapshot[yesterday_closed]", "stocks[yesterday_closed]"):
            if preserved_status_rows[key][1] != "success":
                raise AssertionError(f"{key} must preserve the accepted closed-day snapshot after a later invalid attempt")
            if "accepted_closed_preserved_after_invalid_attempt" not in str(preserved_status_rows[key][10]):
                raise AssertionError(f"{key} must explain that the accepted closed-day snapshot was preserved")
        preserved_data_rows = _data_rows(preserved_plan)
        if preserved_data_rows[f"SKU:{probe_nm_id}|views_current"][2:] != [100.0, 200.0]:
            raise AssertionError("later invalid closed-day search attempt must not overwrite the accepted yesterday snapshot")
        if preserved_data_rows[f"SKU:{probe_nm_id}|view_count"][2:] != [300.0, 400.0]:
            raise AssertionError("later invalid closed-day seller attempt must not overwrite the accepted yesterday snapshot")
        if preserved_data_rows[f"SKU:{probe_nm_id}|stock_total"][2:] != [17.0, 18.0]:
            raise AssertionError("later invalid closed-day stocks attempt must not overwrite the accepted yesterday snapshot")

        print(f"first_refresh: ok -> {first_plan.snapshot_id}")
        print(f"closure_retrying: ok -> {second_status_rows['web_source_snapshot[yesterday_closed]'][10]}")
        print(f"closure_accepted: ok -> {third_status_rows['web_source_snapshot[yesterday_closed]'][10]}")
        print(f"closure_preserved: ok -> {preserved_status_rows['web_source_snapshot[yesterday_closed]'][10]}")
        print("smoke-check passed")


def _build_live_plan(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    state: "_TemporalState",
    current_date: str,
    current_hour: int = 8,
) -> SheetVitrinaV1LivePlanBlock:
    state.current_date = current_date
    return SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        web_source_block=_SyntheticWebSourceBlock(state),
        seller_funnel_block=_SyntheticSellerFunnelBlock(state),
        sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
        prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
        sf_period_block=_SyntheticSuccessBlock("sf_period"),
        spp_block=_SyntheticSuccessBlock("spp"),
        ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
        stocks_block=_TemporalStocksBlock(state),
        ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
        fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
        current_web_source_sync=_NoopCurrentSync(),
        closed_day_web_source_sync=_SyntheticClosedDaySync(state),
        now_factory=lambda current_date=current_date, current_hour=current_hour: datetime.fromisoformat(
            f"{current_date}T{current_hour:02d}:00:00+00:00"
        ),
    )


def _status_rows(plan) -> dict[str, list[object]]:
    status_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {row[0]: row for row in status_sheet.rows}


def _data_rows(plan) -> dict[str, list[object]]:
    data_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
    return {row[1]: row for row in data_sheet.rows}


class _TemporalState:
    def __init__(self, *, requested_nm_ids: list[int]) -> None:
        self.requested_nm_ids = requested_nm_ids
        self.current_date = FIRST_CURRENT_DATE
        self.accepted_dates: set[str] = set()
        self.invalid_after_acceptance_dates: set[str] = set()

    def enable_acceptance(self, snapshot_date: str) -> None:
        self.accepted_dates.add(snapshot_date)

    def invalidate_after_acceptance(self, snapshot_date: str) -> None:
        self.invalid_after_acceptance_dates.add(snapshot_date)


class _NoopCurrentSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return


class _SyntheticClosedDaySync:
    def __init__(self, state: _TemporalState) -> None:
        self.state = state

    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        if snapshot_date in self.state.accepted_dates:
            return


class _SyntheticWebSourceBlock:
    def __init__(self, state: _TemporalState) -> None:
        self.state = state

    def execute(self, request: object) -> SimpleNamespace:
        requested_date = str(getattr(request, "date_to"))
        if requested_date in self.state.invalid_after_acceptance_dates:
            return SimpleNamespace(result=SimpleNamespace(kind="not_found", detail="explicit invalid after acceptance"))
        if requested_date == self.state.current_date or requested_date in self.state.accepted_dates:
            return SimpleNamespace(
                result=SimpleNamespace(
                    kind="success",
                    date_from=requested_date,
                    date_to=requested_date,
                    count=len(self.state.requested_nm_ids),
                    items=[
                        SimpleNamespace(
                            nm_id=nm_id,
                            views_current=float(_web_views_value(requested_date, index)),
                            ctr_current=float(20 + index),
                            orders_current=float(5 + index),
                            position_avg=float(10 + index),
                        )
                        for index, nm_id in enumerate(self.state.requested_nm_ids)
                    ],
                    detail="synthetic web source success",
                )
            )
        return SimpleNamespace(result=SimpleNamespace(kind="not_found", detail="explicit not found"))


class _SyntheticSellerFunnelBlock:
    def __init__(self, state: _TemporalState) -> None:
        self.state = state

    def execute(self, request: object) -> SimpleNamespace:
        requested_date = str(getattr(request, "date"))
        if requested_date in self.state.invalid_after_acceptance_dates:
            return SimpleNamespace(result=SimpleNamespace(kind="not_found", detail="explicit invalid after acceptance"))
        if requested_date == self.state.current_date or requested_date in self.state.accepted_dates:
            return SimpleNamespace(
                result=SimpleNamespace(
                    kind="success",
                    date=requested_date,
                    count=len(self.state.requested_nm_ids),
                    items=[
                        SimpleNamespace(
                            nm_id=nm_id,
                            name=f"NM {nm_id}",
                            vendor_code=f"VC-{nm_id}",
                            view_count=float(_seller_view_count(requested_date, index)),
                            open_card_count=float(30 + index),
                            ctr=float(40 + index),
                        )
                        for index, nm_id in enumerate(self.state.requested_nm_ids)
                    ],
                    detail="synthetic seller funnel success",
                )
            )
        return SimpleNamespace(result=SimpleNamespace(kind="not_found", detail="explicit not found"))


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = ""
        for field in ("snapshot_date", "date", "date_to"):
            value = getattr(request, field, None)
            if isinstance(value, str) and value:
                request_date = value
                break
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[],
                snapshot_date=request_date,
                date=request_date,
                date_from=request_date,
                date_to=request_date,
                detail=f"{self.source_key} synthetic success for {request_date}",
                storage_total=None,
            )
        )


class _TemporalStocksBlock:
    def __init__(self, state: _TemporalState) -> None:
        self.state = state

    def execute(self, request: object) -> SimpleNamespace:
        requested_date = str(getattr(request, "snapshot_date"))
        if requested_date in self.state.invalid_after_acceptance_dates:
            return SimpleNamespace(result=SimpleNamespace(kind="not_found", detail="explicit invalid after acceptance"))
        if requested_date == self.state.current_date or requested_date in self.state.accepted_dates:
            return SimpleNamespace(
                result=SimpleNamespace(
                    kind="success",
                    snapshot_date=requested_date,
                    items=[
                        SimpleNamespace(
                            nm_id=nm_id,
                            stock_total=float(_stocks_total_value(requested_date, index)),
                            stock_ru_central=float(_stocks_total_value(requested_date, index)),
                            stock_ru_northwest=0.0,
                            stock_ru_volga=0.0,
                            stock_ru_south_caucasus=0.0,
                            stock_ru_ural=0.0,
                            stock_ru_far_siberia=0.0,
                        )
                        for index, nm_id in enumerate(self.state.requested_nm_ids)
                    ],
                    detail="synthetic stocks success",
                )
            )
        return SimpleNamespace(result=SimpleNamespace(kind="not_found", detail="explicit not found"))


def _web_views_value(snapshot_date: str, index: int) -> int:
    return (100 if snapshot_date == FIRST_CURRENT_DATE else 200) + index


def _seller_view_count(snapshot_date: str, index: int) -> int:
    return (300 if snapshot_date == FIRST_CURRENT_DATE else 400) + index


def _stocks_total_value(snapshot_date: str, index: int) -> int:
    return (17 if snapshot_date == FIRST_CURRENT_DATE else 18) + index


if __name__ == "__main__":
    main()
