"""Targeted smoke-check для persisted ready snapshot sheet_vitrina_v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import DB_FILENAME, RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
REFRESHED_AT = "2026-04-13T12:10:00Z"
AS_OF_DATE = "2026-04-12"
TODAY_CURRENT_DATE = "2026-04-13"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)

    with TemporaryDirectory(prefix="sheet-vitrina-ready-snapshot-runtime-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted.status}")

        current_state = runtime.load_current_state()
        plan = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            web_source_block=_SyntheticSuccessBlock("web_source_snapshot"),
            seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot"),
            sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
            prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
            sf_period_block=_SyntheticSuccessBlock("sf_period"),
            spp_block=_SyntheticSuccessBlock("spp"),
            ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
            stocks_block=_SyntheticSuccessBlock("stocks"),
            ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
            fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
            now_factory=lambda: datetime(2026, 4, 13, 9, 0, tzinfo=timezone.utc),
        ).build_plan(as_of_date=AS_OF_DATE)
        refresh_result = runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=REFRESHED_AT,
            plan=plan,
        )
        if refresh_result.bundle_version != current_state.bundle_version:
            raise AssertionError("refresh result bundle_version mismatch")
        if refresh_result.snapshot_id != plan.snapshot_id:
            raise AssertionError("refresh result snapshot_id mismatch")
        if refresh_result.date_columns != [AS_OF_DATE, TODAY_CURRENT_DATE]:
            raise AssertionError("refresh result must persist both date columns")
        if [slot.slot_key for slot in refresh_result.temporal_slots] != [
            "yesterday_closed",
            "today_current",
        ]:
            raise AssertionError("refresh result temporal_slots mismatch")

        exact_snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=plan.as_of_date)
        latest_snapshot = runtime.load_sheet_vitrina_ready_snapshot()
        if exact_snapshot.snapshot_id != plan.snapshot_id:
            raise AssertionError("exact ready snapshot mismatch")
        if latest_snapshot.snapshot_id != plan.snapshot_id:
            raise AssertionError("latest ready snapshot mismatch")
        if exact_snapshot.date_columns != [AS_OF_DATE, TODAY_CURRENT_DATE]:
            raise AssertionError("persisted ready snapshot must keep both date columns")

        next_bundle = dict(bundle)
        next_bundle["bundle_version"] = "sheet_vitrina_v1_snapshot_runtime__2026-04-13T12:20:00Z"
        next_bundle["uploaded_at"] = "2026-04-13T12:20:00Z"
        second_accept = runtime.ingest_bundle(next_bundle, activated_at="2026-04-13T12:20:00Z")
        if second_accept.status != "accepted":
            raise AssertionError("second bundle must also be accepted")
        try:
            runtime.load_sheet_vitrina_ready_snapshot()
        except ValueError as exc:
            if "ready snapshot missing" not in str(exc):
                raise AssertionError(f"unexpected ready-snapshot error: {exc}") from exc
        else:
            raise AssertionError("ready snapshot must not silently reuse stale snapshot after current bundle changes")

        print(f"runtime_db: ok -> {runtime_dir / DB_FILENAME}")
        print(f"refresh_result: ok -> {refresh_result.snapshot_id}")
        print(f"latest_read: ok -> {latest_snapshot.date_columns}")
        print("stale_snapshot_guard: ok -> current bundle requires explicit refresh")
        print("smoke-check passed")


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        payload = SimpleNamespace(
            kind="success",
            items=[],
            snapshot_date=request_date,
            date=request_date,
            date_from=request_date,
            date_to=request_date,
            detail=f"{self.source_key} synthetic success for {request_date}",
            storage_total=None,
        )
        return SimpleNamespace(result=payload)


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic source request must carry a date field")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
