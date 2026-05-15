"""Smoke-check 1C stocks wiring into sheet_vitrina_v1 data/status/web surfaces."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.onec_stocks_block import ArtifactBackedOnecStocksSource
from packages.application.onec_stocks_block import OnecStocksBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.application.sheet_vitrina_v1_onec_stocks import (
    DEFAULT_ONEC_STAGE_MAPPING,
    ONEC_STOCKS_SOURCE_GROUP_ID,
    ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
    ONEC_STOCKS_SOURCE_KEY,
    ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
    onec_stage_metric_key,
)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

ARTIFACTS = ROOT / "artifacts" / "onec_stocks_block"
NM_ID = 428855306
AS_OF_DATE = "2026-05-14"
TODAY_DATE = "2026-05-15"
REFRESHED_AT = "2026-05-15T12:05:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-onec-stocks-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-15T11:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"minimal registry bundle must be accepted, got {accepted}")

        onec_block = OnecStocksBlock(
            ArtifactBackedOnecStocksSource(ARTIFACTS),
            stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
        )
        sheet_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=onec_block,
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        metric_keys = [
            ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
            ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
            onec_stage_metric_key("CHINA_TO_FF", "qty"),
            onec_stage_metric_key("CHINA_TO_FF", "unit_cost_rub"),
            onec_stage_metric_key("CHINA_TO_FF", "cost_total_rub"),
            onec_stage_metric_key("FF_STOCK", "qty"),
            onec_stage_metric_key("FF_TO_WB", "qty"),
            onec_stage_metric_key("WB_STOCK", "qty"),
        ]
        plan = sheet_block.build_plan(
            as_of_date=AS_OF_DATE,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=metric_keys,
        )
        data_rows = {str(row[1]): row for row in _sheet_rows(plan, "DATA_VITRINA")}
        assert_close(data_rows["TOTAL|total_onec_total_qty"][3], 12540.0, "total 1C qty")
        assert_close(data_rows["TOTAL|total_onec_total_cost_rub"][3], 1190938.16, "total 1C cost")
        assert_close(
            data_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"][3],
            4782.0,
            "CHINA_TO_FF qty",
        )
        assert_close(
            data_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_unit_cost_rub"][3],
            77.82,
            "CHINA_TO_FF unit cost",
        )
        assert_close(
            data_rows[f"SKU:{NM_ID}|onec_FF_STOCK_qty"][3],
            1250.0,
            "FF_STOCK qty",
        )
        if data_rows[f"SKU:{NM_ID}|onec_FF_TO_WB_qty"][3] != "":
            raise AssertionError(f"missing FF_TO_WB stage must stay blank, got {data_rows}")

        status_rows = {str(row[0]): row for row in _sheet_rows(plan, "STATUS")}
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[today_current]"][1] != "success":
            raise AssertionError(f"1C today status must be success, got {status_rows}")
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"][1] != "missing":
            raise AssertionError(f"1C first-run yesterday rollover must be explicit missing, got {status_rows}")

        current_state = runtime.load_current_state()
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=REFRESHED_AT,
            plan=plan,
        )
        web_contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date=AS_OF_DATE,
        )
        web_rows = {row.row_id: row for row in web_contract.rows}
        if web_rows["TOTAL|total_onec_total_cost_rub"].section != ONEC_STOCKS_SOURCE_GROUP_LABEL_RU:
            raise AssertionError(f"web contract must expose 1C section labels, got {web_rows}")

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=Path(tmp),
            runtime=runtime,
            activated_at_factory=lambda: REFRESHED_AT,
            refreshed_at_factory=lambda: REFRESHED_AT,
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        page_payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            as_of_date=AS_OF_DATE,
            include_source_status=True,
        )
        loading_table = page_payload["activity_surface"]["loading_table"]
        groups = {item["group_id"]: item for item in loading_table["groups"]}
        if groups[ONEC_STOCKS_SOURCE_GROUP_ID]["label"] != ONEC_STOCKS_SOURCE_GROUP_LABEL_RU:
            raise AssertionError(f"1C source group must be visible in status surface, got {groups}")
        onec_rows = [
            row for row in loading_table["rows"]
            if row["source_key"] == ONEC_STOCKS_SOURCE_KEY
        ]
        if not onec_rows or onec_rows[0]["source_group_id"] != ONEC_STOCKS_SOURCE_GROUP_ID:
            raise AssertionError(f"1C loading row must carry source group id, got {loading_table}")
        if "1С: товарный капитал всего, руб" not in onec_rows[0]["metric_labels"]:
            raise AssertionError(f"1C loading row must expose metric labels, got {onec_rows[0]}")

        captured: dict[str, object] = {}

        def build_partial_plan(**kwargs: object) -> SheetVitrinaV1Envelope:
            captured["source_keys"] = list(kwargs.get("source_keys") or [])
            captured["metric_keys"] = list(kwargs.get("metric_keys") or [])
            return plan

        entrypoint.sheet_plan_block.build_plan = build_partial_plan  # type: ignore[method-assign]
        job = entrypoint.start_sheet_source_group_refresh_job(
            source_group_id=ONEC_STOCKS_SOURCE_GROUP_ID,
            as_of_date=AS_OF_DATE,
        )
        job_snapshot = _wait_job(entrypoint, str(job["job_id"]))
        if job_snapshot["status"] != "success":
            raise AssertionError(f"1C group refresh must finish successfully, got {job_snapshot}")
        if captured.get("source_keys") != [ONEC_STOCKS_SOURCE_KEY]:
            raise AssertionError(f"1C group refresh must select only 1C source, got {captured}")
        captured_metric_keys = set(captured.get("metric_keys") or [])
        for expected in (
            ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
            ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
            onec_stage_metric_key("CHINA_TO_FF", "qty"),
            onec_stage_metric_key("WB_STOCK", "cost_total_rub"),
        ):
            if expected not in captured_metric_keys:
                raise AssertionError(f"1C group refresh must select virtual metric {expected}, got {captured}")

        print("sheet_vitrina_onec_stocks_metrics: ok -> total_qty=12540 total_cost=1190938.16")
        print("sheet_vitrina_onec_stocks_status_group: ok ->", ONEC_STOCKS_SOURCE_GROUP_ID)
        print("sheet_vitrina_onec_stocks_group_refresh: ok ->", len(captured_metric_keys))


def _build_bundle() -> dict[str, object]:
    return {
        "bundle_version": "sheet_vitrina_onec_stocks_wiring_smoke",
        "uploaded_at": "2026-05-15T11:55:00Z",
        "config_v2": [
            {
                "nm_id": NM_ID,
                "enabled": True,
                "display_name": "1C smoke SKU",
                "group": "1C smoke",
                "display_order": 1,
            }
        ],
        "metrics_v2": [
            {
                "metric_key": "stock_total",
                "enabled": False,
                "scope": "SKU",
                "label_ru": "Остаток всего",
                "calc_type": "metric",
                "calc_ref": "stock_total",
                "show_in_data": False,
                "format": "integer",
                "display_order": 10,
                "section": "Запасы",
            }
        ],
        "formulas_v2": [],
    }


def _sheet_rows(plan: SheetVitrinaV1Envelope, sheet_name: str) -> list[list[object]]:
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return sheet.rows
    raise AssertionError(f"plan missing sheet {sheet_name}")


def _wait_job(entrypoint: RegistryUploadHttpEntrypoint, job_id: str) -> dict[str, object]:
    deadline = time.time() + 10.0
    while time.time() < deadline:
        snapshot = entrypoint.handle_sheet_operator_job_request(job_id)
        if str(snapshot.get("status")) in {"success", "error"}:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def assert_close(actual: object, expected: float, label: str) -> None:
    if not isinstance(actual, (int, float)) or abs(float(actual) - expected) > 0.01:
        raise AssertionError(f"{label} mismatch: expected {expected}, got {actual!r}")


if __name__ == "__main__":
    main()
