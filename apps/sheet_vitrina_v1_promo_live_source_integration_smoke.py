"""Bounded integration smoke for live-wired promo source inside refresh/runtime/read-side contour."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-19"
CURRENT_DATE = "2026-04-20"
ACTIVATED_AT = "2026-04-20T06:00:00Z"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    if not requested_nm_ids:
        raise AssertionError("fixture bundle must contain enabled nm_ids")

    with TemporaryDirectory(prefix="sheet-vitrina-promo-live-source-integration-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: ACTIVATED_AT,
            refreshed_at_factory=lambda: "2026-04-20T06:05:00Z",
            now_factory=lambda: datetime.fromisoformat("2026-04-20T06:00:00+05:00"),
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=lambda: datetime.fromisoformat("2026-04-20T06:00:00+05:00"),
            promo_live_source_block=PromoLiveSourceBlock(
                runtime_dir=runtime_dir,
                max_candidates=8,
                max_downloads=5,
            ),
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

        refresh = entrypoint._run_sheet_refresh(as_of_date=AS_OF_DATE, log=None)
        if refresh["status"] != "success":
            raise AssertionError(f"promo live-wired refresh must succeed, got {refresh}")

        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        status_rows = _status_rows(plan)
        promo_status = status_rows["promo_by_price[today_current]"]
        if promo_status[1] != "success":
            raise AssertionError(f"promo source must materialize success in STATUS, got {promo_status}")
        note = str(promo_status[10])
        if "trace_run_dir=" not in note or "current_promos_downloaded=" not in note:
            raise AssertionError(f"promo STATUS note must expose collector trace and download count, got {promo_status}")
        trace_run_dir = _extract_note_value(note, "trace_run_dir")
        if not trace_run_dir:
            raise AssertionError(f"failed to extract trace_run_dir from promo status note: {promo_status}")
        trace_path = Path(trace_run_dir)
        if not trace_path.exists():
            raise AssertionError(f"promo collector trace_run_dir missing on disk: {trace_run_dir}")

        rows = _data_rows(plan)
        row_keys = [key for key in rows if key.endswith("|promo_count_by_price")]
        if not row_keys:
            raise AssertionError("DATA_VITRINA must contain promo_count_by_price rows")
        if not any(_to_float(rows[key][3]) > 0 for key in row_keys):
            raise AssertionError("today_current promo_count_by_price must contain at least one positive value")

        downloaded_folders = sorted(path.parent for path in trace_path.glob("promos/*/workbook.xlsx"))
        if not downloaded_folders:
            raise AssertionError(f"trace run must contain at least one downloaded workbook, got {trace_run_dir}")
        first_metadata = downloaded_folders[0] / "metadata.json"
        if not first_metadata.exists():
            raise AssertionError(f"downloaded promo folder missing metadata.json: {downloaded_folders[0]}")

        print(f"trace_run_dir: {trace_run_dir}")
        print(f"downloaded_promos: {len(downloaded_folders)}")
        print(f"first_downloaded_folder: {downloaded_folders[0]}")
        print("integration-smoke passed")


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


def _extract_note_value(note: str, key: str) -> str | None:
    match = re.search(rf"{re.escape(key)}=([^;]+)", note)
    return match.group(1).strip() if match else None


def _to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


if __name__ == "__main__":
    main()
