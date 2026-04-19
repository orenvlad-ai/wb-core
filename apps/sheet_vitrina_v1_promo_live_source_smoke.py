"""Targeted smoke-check for live-wired promo source semantics."""

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

from packages.application.promo_xlsx_collector_block import parse_period_text
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import (
    EXECUTION_MODE_AUTO_DAILY,
    TEMPORAL_SLOT_TODAY_CURRENT,
    SheetVitrinaV1LivePlanBlock,
)
from packages.contracts.promo_live_source import (
    PromoLiveSourceEnvelope,
    PromoLiveSourceIncomplete,
    PromoLiveSourceItem,
    PromoLiveSourceRequest,
    PromoLiveSourceSuccess,
)


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-19"
CURRENT_DATE = "2026-04-20"
ACTIVATED_AT = "2026-04-20T06:00:00Z"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]][:3]
    if len(requested_nm_ids) < 3:
        raise AssertionError("fixture bundle must expose at least three enabled nm_ids")

    _assert_cross_year_parse_rule()
    _assert_promo_source_runtime_mapping(bundle, requested_nm_ids)
    preserved_note = _assert_accepted_current_preserved_after_invalid_attempt(bundle, requested_nm_ids)

    print("cross_year_parse_rule: ok -> low-confidence period keeps null exact dates")
    print("promo_source_runtime_mapping: ok -> promo metrics reach STATUS and DATA_VITRINA via runtime source")
    print(f"accepted_current_preservation: ok -> {preserved_note}")
    print("smoke-check passed")


def _assert_cross_year_parse_rule() -> None:
    start_at, end_at, confidence = parse_period_text(
        "28 декабря 02:00 → 04 января 01:59",
        reference_year=2026,
    )
    if (start_at, end_at, confidence) != (None, None, "low"):
        raise AssertionError(
            "cross-year short labels must not invent exact promo_start_at/promo_end_at"
        )


def _assert_promo_source_runtime_mapping(bundle: dict[str, Any], requested_nm_ids: list[int]) -> None:
    yesterday_items = [
        PromoLiveSourceItem(snapshot_date=AS_OF_DATE, nm_id=requested_nm_ids[0], promo_count_by_price=1.0, promo_entry_price_best=500.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=AS_OF_DATE, nm_id=requested_nm_ids[1], promo_count_by_price=0.0, promo_entry_price_best=450.0, promo_participation=0.0),
        PromoLiveSourceItem(snapshot_date=AS_OF_DATE, nm_id=requested_nm_ids[2], promo_count_by_price=0.0, promo_entry_price_best=0.0, promo_participation=0.0),
    ]
    today_items = [
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[0], promo_count_by_price=2.0, promo_entry_price_best=600.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[1], promo_count_by_price=1.0, promo_entry_price_best=550.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[2], promo_count_by_price=0.0, promo_entry_price_best=0.0, promo_participation=0.0),
    ]
    with TemporaryDirectory(prefix="sheet-vitrina-promo-source-map-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        runtime.save_temporal_source_snapshot(
            source_key="promo_by_price",
            snapshot_date=AS_OF_DATE,
            captured_at="2026-04-19T18:00:00Z",
            payload=PromoLiveSourceSuccess(
                kind="success",
                snapshot_date=AS_OF_DATE,
                date_from=AS_OF_DATE,
                date_to=AS_OF_DATE,
                requested_count=len(requested_nm_ids),
                covered_count=len(requested_nm_ids),
                items=yesterday_items,
                detail="trace_run_dir=/tmp/promo-yesterday; current_promos=2; current_promos_downloaded=2",
                trace_run_dir="/tmp/promo-yesterday",
                current_promos=2,
                current_promos_downloaded=2,
                current_promos_blocked=0,
                future_promos=1,
                skipped_past_promos=4,
                ambiguous_promos=0,
                current_download_export_kinds=["eligible_items_report"],
            ),
        )

        entrypoint = _build_entrypoint(
            runtime=runtime,
            promo_source_block=_SyntheticPromoSourceBlock(mode="success", snapshot_items=today_items),
            now_factory=_MutableNowFactory("2026-04-20T08:00:00+00:00"),
        )
        refresh_payload = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError(f"refresh must succeed, got {refresh_payload}")

        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        status_rows = _status_rows(plan)
        yesterday_status = status_rows["promo_by_price[yesterday_closed]"]
        today_status = status_rows["promo_by_price[today_current]"]
        if yesterday_status[1] != "success":
            raise AssertionError(f"promo yesterday_closed must materialize from prior current cache, got {yesterday_status}")
        if "accepted_closed_from_prior_current_cache" not in str(yesterday_status[10]):
            raise AssertionError(f"promo yesterday_closed note must expose cache-based closed semantics, got {yesterday_status}")
        if today_status[1] != "success":
            raise AssertionError(f"promo today_current must be success, got {today_status}")
        if "trace_run_dir=/tmp/promo-today" not in str(today_status[10]):
            raise AssertionError(f"promo STATUS note must expose collector trace run dir, got {today_status}")
        if "current_promos_downloaded=2" not in str(today_status[10]):
            raise AssertionError(f"promo STATUS note must expose current download counts, got {today_status}")

        rows = _data_rows(plan)
        if _today_value(rows[f"SKU:{requested_nm_ids[0]}|promo_count_by_price"]) != 2.0:
            raise AssertionError("promo_count_by_price today_current must materialize from promo source")
        if _today_value(rows[f"SKU:{requested_nm_ids[1]}|promo_participation"]) != 1.0:
            raise AssertionError("promo_participation today_current must materialize from promo source")
        if _yesterday_value(rows[f"SKU:{requested_nm_ids[1]}|promo_entry_price_best"]) != 450.0:
            raise AssertionError("promo_entry_price_best yesterday_closed must materialize from runtime cache")


def _assert_accepted_current_preserved_after_invalid_attempt(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
) -> str:
    valid_items = [
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[0], promo_count_by_price=2.0, promo_entry_price_best=600.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[1], promo_count_by_price=1.0, promo_entry_price_best=550.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[2], promo_count_by_price=0.0, promo_entry_price_best=0.0, promo_participation=0.0),
    ]
    with TemporaryDirectory(prefix="sheet-vitrina-promo-source-preserve-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        promo_source = _SyntheticPromoSourceBlock(mode="success", snapshot_items=valid_items)
        now_factory = _MutableNowFactory("2026-04-20T08:00:00+00:00")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            promo_source_block=promo_source,
            now_factory=now_factory,
        )
        first = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if first["status"] != "success":
            raise AssertionError("first promo refresh must succeed")
        morning_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        morning_rows = _data_rows(morning_plan)
        baseline = _today_value(morning_rows[f"SKU:{requested_nm_ids[0]}|promo_count_by_price"])
        if baseline != 2.0:
            raise AssertionError(f"expected baseline promo_count_by_price=2.0, got {baseline}")

        promo_source.mode = "incomplete"
        promo_source.detail = "trace_run_dir=/tmp/promo-invalid; current_promos=2; current_promos_downloaded=1; current_promos_blocked=1"
        now_factory.value = "2026-04-20T14:00:00+00:00"
        second = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if second["status"] != "success":
            raise AssertionError("later invalid promo refresh must still persist a ready snapshot")
        evening_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        evening_status = _status_rows(evening_plan)["promo_by_price[today_current]"]
        if evening_status[1] != "success":
            raise AssertionError(f"invalid later promo attempt must preserve accepted current snapshot, got {evening_status}")
        note = str(evening_status[10])
        if "accepted_current_preserved_after_invalid_attempt" not in note:
            raise AssertionError(f"preserved promo current snapshot must explain accepted preservation, got {evening_status}")
        evening_rows = _data_rows(evening_plan)
        if _today_value(evening_rows[f"SKU:{requested_nm_ids[0]}|promo_count_by_price"]) != baseline:
            raise AssertionError("invalid later promo attempt must not overwrite accepted current values")
        closure_state = runtime.load_temporal_source_closure_state(
            source_key="promo_by_price",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is None or closure_state.state != "success":
            raise AssertionError(f"accepted promo current snapshot must keep success closure state, got {closure_state}")
        return note


def _build_entrypoint(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    promo_source_block: object,
    now_factory: "_MutableNowFactory",
) -> RegistryUploadHttpEntrypoint:
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime.runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: ACTIVATED_AT,
        refreshed_at_factory=lambda: "2026-04-20T08:05:00Z",
        now_factory=now_factory,
    )
    entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        now_factory=now_factory,
        promo_live_source_block=promo_source_block,
        current_web_source_sync=_NoopCurrentWebSourceSync(),
        seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot"),
        web_source_block=_SyntheticSuccessBlock("web_source_snapshot"),
        sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
        prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
        sf_period_block=_SyntheticSuccessBlock("sf_period"),
        spp_block=_SyntheticSuccessBlock("spp"),
        ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
        stocks_block=_SyntheticSuccessBlock("stocks"),
        ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
        fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
    )
    return entrypoint


class _NoopCurrentWebSourceSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
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


class _SyntheticPromoSourceBlock:
    def __init__(self, *, mode: str, snapshot_items: list[PromoLiveSourceItem]) -> None:
        self.mode = mode
        self.snapshot_items = snapshot_items
        self.detail = "trace_run_dir=/tmp/promo-today; current_promos=2; current_promos_downloaded=2; current_promos_blocked=0"

    def execute(self, request: PromoLiveSourceRequest) -> PromoLiveSourceEnvelope:
        if self.mode == "success":
            return PromoLiveSourceEnvelope(
                result=PromoLiveSourceSuccess(
                    kind="success",
                    snapshot_date=request.snapshot_date,
                    date_from=request.snapshot_date,
                    date_to=request.snapshot_date,
                    requested_count=len(request.nm_ids),
                    covered_count=len(request.nm_ids),
                    items=self.snapshot_items,
                    detail=self.detail,
                    trace_run_dir="/tmp/promo-today",
                    current_promos=2,
                    current_promos_downloaded=2,
                    current_promos_blocked=0,
                    future_promos=1,
                    skipped_past_promos=4,
                    ambiguous_promos=0,
                    current_download_export_kinds=["eligible_items_report"],
                )
            )
        return PromoLiveSourceEnvelope(
            result=PromoLiveSourceIncomplete(
                kind="incomplete",
                snapshot_date=request.snapshot_date,
                date_from=request.snapshot_date,
                date_to=request.snapshot_date,
                requested_count=len(request.nm_ids),
                covered_count=0,
                items=[],
                detail=self.detail,
                trace_run_dir="/tmp/promo-invalid",
                current_promos=2,
                current_promos_downloaded=1,
                current_promos_blocked=1,
                future_promos=1,
                skipped_past_promos=4,
                ambiguous_promos=0,
                missing_nm_ids=list(request.nm_ids),
                current_download_export_kinds=["eligible_items_report"],
            )
        )


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic request must carry a date field")


def _status_rows(plan) -> dict[str, list[Any]]:
    status_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {row[0]: row for row in status_sheet.rows}


def _data_rows(plan) -> dict[str, list[Any]]:
    data_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
    return {row[1]: row for row in data_sheet.rows}


def _yesterday_value(row: list[Any]) -> Any:
    return row[2]


def _today_value(row: list[Any]) -> Any:
    return row[3]


if __name__ == "__main__":
    main()
