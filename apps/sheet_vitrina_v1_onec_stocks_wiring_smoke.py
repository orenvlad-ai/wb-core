"""Smoke-check 1C stocks wiring into sheet_vitrina_v1 data/status/web surfaces."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
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
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock, STATUS_HEADER
from packages.application.sheet_vitrina_v1_onec_stocks import (
    DEFAULT_ONEC_STAGE_MAPPING,
    ONEC_STOCKS_STAGE_FIELDS,
    ONEC_STOCKS_STAGE_KEYS,
    ONEC_STOCKS_SOURCE_GROUP_ID,
    ONEC_STOCKS_SOURCE_GROUP_LABEL_RU,
    ONEC_STOCKS_SOURCE_KEY,
    ONEC_STOCKS_TOTAL_STAGE_METRIC_KEYS,
    ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
    onec_stage_metric_key,
    onec_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.contracts.onec_stocks_block import (
    ONEC_STOCKS_PARTIAL_FETCH_META_KEY,
    OnecStocksRequest,
)
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

ARTIFACTS = ROOT / "artifacts" / "onec_stocks_block"
NM_ID = 428855306
MISSING_NM_ID = 210183919
AS_OF_DATE = "2026-05-14"
TODAY_DATE = "2026-05-15"
PERIOD_OLD_DATE = "2026-05-13"
REFRESHED_AT = "2026-05-15T12:05:00Z"


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-onec-stocks-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-15T11:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"minimal registry bundle must be accepted, got {accepted}")

        source = _RequestDateArtifactOnecStocksSource(ARTIFACTS)
        onec_block = OnecStocksBlock(
            source,
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
            *ONEC_STOCKS_TOTAL_STAGE_METRIC_KEYS,
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
        partial_source = _PartialOnecStocksSource(ARTIFACTS)
        partial_onec_block = OnecStocksBlock(
            partial_source,
            stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
        )
        partial_sheet_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=partial_onec_block,
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        partial_plan = partial_sheet_block.build_plan(
            as_of_date=AS_OF_DATE,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=metric_keys,
        )
        data_rows = {str(row[1]): row for row in _sheet_rows(plan, "DATA_VITRINA")}
        assert_close(data_rows["TOTAL|total_onec_total_qty"][3], 12540.0, "total 1C qty")
        assert_close(data_rows["TOTAL|total_onec_total_cost_rub"][3], 1190938.16, "total 1C cost")
        for stage_key in ONEC_STOCKS_STAGE_KEYS:
            for field in ONEC_STOCKS_STAGE_FIELDS:
                row_id = f"TOTAL|{onec_stage_total_metric_key(stage_key, field)}"
                if row_id not in data_rows:
                    raise AssertionError(f"summary/totals must expose 1C stage metric {row_id}")
        assert_close(
            data_rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'qty')}"][3],
            4782.0,
            "total CHINA_TO_FF qty",
        )
        assert_close(
            data_rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'cost_total_rub')}"][3],
            372123.77,
            "total CHINA_TO_FF cost",
        )
        assert_close(
            data_rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'unit_cost_rub')}"][3],
            372123.77 / 4782.0,
            "weighted CHINA_TO_FF unit cost",
        )
        assert_close(
            data_rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'qty')}"][2],
            4782.0,
            "historical total CHINA_TO_FF qty",
        )
        assert_close(
            data_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"][2],
            4782.0,
            "historical SKU CHINA_TO_FF qty",
        )
        assert_close(
            data_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"][3],
            4782.0,
            "CHINA_TO_FF qty",
        )
        assert_close(
            data_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_unit_cost_rub"][3],
            372123.77 / 4782.0,
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
        yesterday_status = status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"]
        if yesterday_status[1] != "success":
            raise AssertionError(f"1C yesterday status must load the requested historical date, got {status_rows}")
        if yesterday_status[3] != AS_OF_DATE:
            raise AssertionError(f"1C yesterday snapshot lineage must match requested date, got {yesterday_status}")
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[today_current]"][3] != TODAY_DATE:
            raise AssertionError(f"1C today snapshot lineage must match requested date, got {status_rows}")
        if sorted(source.request_dates) != [AS_OF_DATE, TODAY_DATE]:
            raise AssertionError(f"1C live plan must request each slot date separately, got {source.request_dates}")

        partial_data_rows = {str(row[1]): row for row in _sheet_rows(partial_plan, "DATA_VITRINA")}
        assert_close(
            partial_data_rows["TOTAL|total_onec_total_qty"][3],
            12540.0,
            "partial total 1C qty",
        )
        assert_close(
            partial_data_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"][3],
            4782.0,
            "partial SKU 1C qty",
        )
        partial_status_rows = {str(row[0]): row for row in _sheet_rows(partial_plan, "STATUS")}
        partial_today_status = partial_status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[today_current]"]
        if partial_today_status[1] != "incomplete":
            raise AssertionError(f"partial 1C today status must be incomplete, got {partial_today_status}")
        if partial_today_status[8] != 1 or str(MISSING_NM_ID) not in str(partial_today_status[9]):
            raise AssertionError(f"partial 1C status must expose covered/missing counts, got {partial_today_status}")
        if sorted(partial_source.request_dates) != [AS_OF_DATE, TODAY_DATE]:
            raise AssertionError(f"partial 1C plan must request each slot date separately, got {partial_source.request_dates}")

        current_state = runtime.load_current_state()
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-05-13T12:05:00Z",
            plan=_build_legacy_period_snapshot(),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=REFRESHED_AT,
            plan=_with_unrelated_source_error(plan),
        )
        web_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        web_contract = web_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date=AS_OF_DATE,
        )
        web_rows = {row.row_id: row for row in web_contract.rows}
        if web_rows["TOTAL|total_onec_total_cost_rub"].section != ONEC_STOCKS_SOURCE_GROUP_LABEL_RU:
            raise AssertionError(f"web contract must expose 1C section labels, got {web_rows}")
        assert_nonblank(
            web_rows["TOTAL|total_onec_total_cost_rub"].values_by_date.get(TODAY_DATE),
            "web explicit as_of_date total 1C cost",
        )
        assert_nonblank(
            web_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"].values_by_date.get(TODAY_DATE),
            "web explicit as_of_date SKU 1C qty",
        )

        period_contract = web_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from=PERIOD_OLD_DATE,
            date_to=TODAY_DATE,
        )
        period_rows = {row.row_id: row for row in period_contract.rows}
        if "TOTAL|total_onec_total_cost_rub" not in period_rows:
            raise AssertionError(f"period web contract must keep 1C total rows visible, got {period_rows}")
        if f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty" not in period_rows:
            raise AssertionError(f"period web contract must keep 1C SKU rows visible, got {period_rows}")
        assert_nonblank(
            period_rows["TOTAL|total_onec_total_cost_rub"].values_by_date.get(TODAY_DATE),
            "period total 1C cost",
        )
        assert_nonblank(
            period_rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"].values_by_date.get(TODAY_DATE),
            "period SKU 1C qty",
        )

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
        period_page_payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            date_from=PERIOD_OLD_DATE,
            date_to=TODAY_DATE,
            include_table_data=True,
        )
        metric_control = next(
            (
                control
                for control in (period_page_payload.get("filter_surface") or {}).get("controls", [])
                if control.get("control_id") == "metric"
            ),
            None,
        )
        metric_option_values = {
            str(option.get("value") or "")
            for option in ((metric_control or {}).get("options") or [])
        }
        for expected_metric_key in (
            ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
            onec_stage_total_metric_key("CHINA_TO_FF", "unit_cost_rub"),
            onec_stage_metric_key("CHINA_TO_FF", "qty"),
        ):
            if expected_metric_key not in metric_option_values:
                raise AssertionError(
                    f"period filter/options must expose 1C metric {expected_metric_key}, got {metric_option_values}"
                )

        captured: dict[str, object] = {}

        def build_partial_plan(**kwargs: object) -> SheetVitrinaV1Envelope:
            captured["source_keys"] = list(kwargs.get("source_keys") or [])
            captured["metric_keys"] = list(kwargs.get("metric_keys") or [])
            return partial_plan

        entrypoint.sheet_plan_block.build_plan = build_partial_plan  # type: ignore[method-assign]
        job = entrypoint.start_sheet_source_group_refresh_job(
            source_group_id=ONEC_STOCKS_SOURCE_GROUP_ID,
            as_of_date=TODAY_DATE,
        )
        job_snapshot = _wait_job(entrypoint, str(job["job_id"]))
        if job_snapshot["status"] != "success":
            raise AssertionError(f"1C group refresh must finish successfully, got {job_snapshot}")
        if captured.get("source_keys") != [ONEC_STOCKS_SOURCE_KEY]:
            raise AssertionError(f"1C group refresh must select only 1C source, got {captured}")
        job_result = dict(job_snapshot.get("result") or {})
        if int(job_result.get("updated_cell_count") or 0) <= 0:
            raise AssertionError(f"partial 1C group refresh must update cells, got {job_result}")
        if job_result.get("snapshot_semantic_status") != "error":
            raise AssertionError(f"group refresh must preserve overall snapshot semantic status, got {job_result}")
        if job_result.get("semantic_status") == "error" or job_result.get("status_label") == "Ошибка":
            raise AssertionError(f"1C group refresh must not surface unrelated source errors as group blockers, got {job_result}")
        captured_metric_keys = set(captured.get("metric_keys") or [])
        for expected in (
            ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
            ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
            onec_stage_total_metric_key("CHINA_TO_FF", "qty"),
            onec_stage_total_metric_key("CHINA_TO_FF", "unit_cost_rub"),
            onec_stage_metric_key("CHINA_TO_FF", "qty"),
            onec_stage_metric_key("WB_STOCK", "cost_total_rub"),
        ):
            if expected not in captured_metric_keys:
                raise AssertionError(f"1C group refresh must select virtual metric {expected}, got {captured}")

        captured.clear()
        yesterday_job = entrypoint.start_sheet_source_group_refresh_job(
            source_group_id=ONEC_STOCKS_SOURCE_GROUP_ID,
            as_of_date=AS_OF_DATE,
        )
        yesterday_job_snapshot = _wait_job(entrypoint, str(yesterday_job["job_id"]))
        if yesterday_job_snapshot["status"] != "success":
            raise AssertionError(f"1C yesterday group refresh must finish successfully, got {yesterday_job_snapshot}")
        yesterday_result = dict(yesterday_job_snapshot.get("result") or {})
        yesterday_cells = {
            (str(cell.get("row_id") or ""), str(cell.get("as_of_date") or ""), str(cell.get("status") or ""))
            for cell in (yesterday_result.get("updated_cells") or [])
            if isinstance(cell, dict)
        }
        if (
            f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'qty')}",
            AS_OF_DATE,
            "updated",
        ) not in yesterday_cells:
            raise AssertionError(f"1C historical group refresh must update requested-date cells, got {yesterday_cells}")
        if int(yesterday_result.get("updated_cell_count") or 0) <= 0:
            raise AssertionError(f"1C historical group refresh must mark cells updated, got {yesterday_result}")
        yesterday_merge_summary = dict(yesterday_result.get("merge_summary") or {})
        if int(yesterday_merge_summary.get("status_rows_updated") or 0) <= 0:
            raise AssertionError(f"1C historical group refresh must update STATUS truth, got {yesterday_result}")

        _assert_onec_mismatched_historical_payload_not_reused()
        _assert_onec_single_metric_historical_action()
        _assert_onec_date_specific_snapshot_lineage()
        _assert_onec_date_specific_period_snapshots()
        _assert_weighted_unit_cost_semantics()

        print("sheet_vitrina_onec_stocks_metrics: ok -> summary_and_sku_values_present")
        print("sheet_vitrina_onec_stocks_partial_acceptance: ok -> covered=1 missing=1")
        print("sheet_vitrina_onec_stocks_period_visibility: ok -> filter_and_rows_present")
        print("sheet_vitrina_onec_stocks_status_group: ok ->", ONEC_STOCKS_SOURCE_GROUP_ID)
        print("sheet_vitrina_onec_stocks_group_refresh: ok ->", len(captured_metric_keys))
        print("sheet_vitrina_onec_stocks_mismatch_rejection: ok ->", AS_OF_DATE)
        print("sheet_vitrina_onec_stocks_single_metric_action: ok ->", AS_OF_DATE)
        print("sheet_vitrina_onec_stocks_date_specific_lineage: ok -> 2026-05-15/2026-05-16/2026-05-17")
        print("sheet_vitrina_onec_stocks_period_snapshots: ok -> 2026-05-01..2026-05-17")
        print("sheet_vitrina_onec_stocks_weighted_unit_cost: ok -> weighted_avg")


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
            },
            {
                "nm_id": MISSING_NM_ID,
                "enabled": True,
                "display_name": "1C missing SKU",
                "group": "1C smoke",
                "display_order": 2,
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


def _assert_onec_mismatched_historical_payload_not_reused() -> None:
    metric_key = onec_stage_metric_key("CHINA_TO_FF", "qty")
    with TemporaryDirectory(prefix="sheet-vitrina-onec-mismatch-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-15T08:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"mismatch registry bundle must be accepted, got {accepted}")
        source = _MismatchedHistoricalOnecStocksSource()
        block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=OnecStocksBlock(
                source,
                stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
            ),
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        plan = block.build_plan(
            as_of_date=AS_OF_DATE,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=[metric_key],
        )
        rows = {str(row[1]): row for row in _sheet_rows(plan, "DATA_VITRINA")}
        row = rows[f"SKU:{NM_ID}|{metric_key}"]
        if row[2] != "":
            raise AssertionError(f"mismatched historical 1C payload must not populate {AS_OF_DATE}, got {row}")
        assert_close(row[3], 15.0, "mismatch smoke current 1C qty")
        status_rows = {str(row[0]): row for row in _sheet_rows(plan, "STATUS")}
        historical_status = status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"]
        if historical_status[1] == "success":
            raise AssertionError(f"mismatched historical 1C payload must not be accepted, got {historical_status}")
        if historical_status[3] == AS_OF_DATE:
            raise AssertionError(f"mismatched historical 1C status must not claim requested-date lineage, got {historical_status}")
        if sorted(set(source.request_dates)) != [AS_OF_DATE, TODAY_DATE]:
            raise AssertionError(f"mismatch smoke must still request each 1C date separately, got {source.request_dates}")


def _assert_onec_single_metric_historical_action() -> None:
    metric_key = onec_stage_metric_key("CHINA_TO_FF", "qty")
    with TemporaryDirectory(prefix="sheet-vitrina-onec-single-metric-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-15T08:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"single metric registry bundle must be accepted, got {accepted}")
        source = _RequestDateDatedOnecStocksSource()
        block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=OnecStocksBlock(
                source,
                stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
            ),
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        plan = block.build_plan(
            as_of_date=AS_OF_DATE,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=[metric_key],
        )
        rows = {str(row[1]): row for row in _sheet_rows(plan, "DATA_VITRINA")}
        row = rows[f"SKU:{NM_ID}|{metric_key}"]
        assert_close(row[2], 14.0, "single metric historical 1C qty")
        assert_close(row[3], 15.0, "single metric current 1C qty")
        status_rows = {str(row[0]): row for row in _sheet_rows(plan, "STATUS")}
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"][3] != AS_OF_DATE:
            raise AssertionError(f"single metric historical lineage must match requested date, got {status_rows}")
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[today_current]"][3] != TODAY_DATE:
            raise AssertionError(f"single metric current lineage must match requested date, got {status_rows}")
        if sorted(set(source.request_dates)) != [AS_OF_DATE, TODAY_DATE]:
            raise AssertionError(f"single metric action must request each 1C date separately, got {source.request_dates}")


def _assert_onec_date_specific_snapshot_lineage() -> None:
    closed_date = "2026-05-15"
    next_closed_date = "2026-05-16"
    current_date = "2026-05-17"
    metric_keys = [
        ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
        onec_stage_metric_key("CHINA_TO_FF", "qty"),
    ]
    with TemporaryDirectory(prefix="sheet-vitrina-onec-date-lineage-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-15T08:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"date lineage registry bundle must be accepted, got {accepted}")

        source = _RequestDateDatedOnecStocksSource()
        first_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=OnecStocksBlock(
                source,
                stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
            ),
            now_factory=lambda: datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        )
        first_plan = first_block.build_plan(
            as_of_date=closed_date,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=metric_keys,
        )
        current_state = runtime.load_current_state()
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-05-16T12:05:00Z",
            plan=first_plan,
        )

        second_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=OnecStocksBlock(
                source,
                stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
            ),
            now_factory=lambda: datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
        )
        second_plan = second_block.build_plan(
            as_of_date=next_closed_date,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=metric_keys,
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-05-17T12:05:00Z",
            plan=second_plan,
        )

        rows = {str(row[1]): row for row in _sheet_rows(second_plan, "DATA_VITRINA")}
        row = rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"]
        assert_close(row[2], 16.0, "2026-05-16 historical qty")
        assert_close(row[3], 17.0, "2026-05-17 current qty")
        if row[2] == row[3]:
            raise AssertionError(f"1C 2026-05-16 and 2026-05-17 values must be date-specific, got {row}")
        status_rows = {str(row[0]): row for row in _sheet_rows(second_plan, "STATUS")}
        yesterday_status = status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"]
        if yesterday_status[1] != "success":
            raise AssertionError(f"1C closed date must use a matching historical source payload, got {status_rows}")
        if yesterday_status[3] != next_closed_date:
            raise AssertionError(f"1C closed date lineage must match requested date, got {yesterday_status}")
        if "accepted_closed_from_prior_current_snapshot" in str(yesterday_status[10]):
            raise AssertionError(f"1C closed date must not use current-snapshot rollover, got {yesterday_status}")

        expected_dates = [closed_date, next_closed_date, current_date]
        if sorted(set(source.request_dates)) != expected_dates:
            raise AssertionError(f"1C must request each lineage date separately, got {source.request_dates}")
        _assert_runtime_onec_snapshots_have_lineage(runtime, expected_dates)

        web_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
        )
        period_contract = web_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from=closed_date,
            date_to=current_date,
        )
        web_row = {
            item.row_id: item
            for item in period_contract.rows
        }[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"]
        for expected_date in expected_dates:
            assert_close(web_row.values_by_date.get(expected_date), float(expected_date[-2:]), f"{expected_date} web qty")


def _assert_onec_date_specific_period_snapshots() -> None:
    start = date(2026, 5, 1)
    end = date(2026, 5, 17)
    metric_keys = [
        ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
        onec_stage_metric_key("CHINA_TO_FF", "qty"),
    ]
    with TemporaryDirectory(prefix="sheet-vitrina-onec-period-history-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-01T08:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"period history registry bundle must be accepted, got {accepted}")

        source = _RequestDateDatedOnecStocksSource()
        current_state = runtime.load_current_state()
        closed_day = start
        while closed_day < end:
            current_day = closed_day + timedelta(days=1)
            block = SheetVitrinaV1LivePlanBlock(
                runtime=runtime,
                onec_stocks_block=OnecStocksBlock(
                    source,
                    stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
                ),
                now_factory=lambda current_day=current_day: datetime(
                    current_day.year,
                    current_day.month,
                    current_day.day,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
            )
            plan = block.build_plan(
                as_of_date=closed_day.isoformat(),
                execution_mode="manual_operator",
                source_keys=[ONEC_STOCKS_SOURCE_KEY],
                metric_keys=metric_keys,
            )
            row = {
                str(item[1]): item
                for item in _sheet_rows(plan, "DATA_VITRINA")
            }[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"]
            assert_close(row[2], float(closed_day.day), f"{closed_day.isoformat()} closed qty")
            assert_close(row[3], float(current_day.day), f"{current_day.isoformat()} current qty")
            status_rows = {str(item[0]): item for item in _sheet_rows(plan, "STATUS")}
            if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"][3] != closed_day.isoformat():
                raise AssertionError(f"1C closed lineage must match {closed_day.isoformat()}, got {status_rows}")
            if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[today_current]"][3] != current_day.isoformat():
                raise AssertionError(f"1C current lineage must match {current_day.isoformat()}, got {status_rows}")
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{current_day.isoformat()}T12:05:00Z",
                plan=plan,
            )
            closed_day = current_day

        expected_dates = [
            (start + timedelta(days=offset)).isoformat()
            for offset in range((end - start).days + 1)
        ]
        if sorted(set(source.request_dates)) != expected_dates:
            raise AssertionError(f"period 1C must request every date separately, got {source.request_dates}")
        _assert_runtime_onec_snapshots_have_lineage(runtime, expected_dates)

        web_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc),
        )
        period_contract = web_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from=start.isoformat(),
            date_to=end.isoformat(),
        )
        row = {
            item.row_id: item
            for item in period_contract.rows
        }[f"SKU:{NM_ID}|onec_CHINA_TO_FF_qty"]
        actual_values = [
            row.values_by_date.get(snapshot_date)
            for snapshot_date in expected_dates
        ]
        for offset, actual in enumerate(actual_values, start=1):
            assert_close(actual, float(offset), f"period {expected_dates[offset - 1]} qty")
        for snapshot_date in expected_dates[:14]:
            assert_nonblank(row.values_by_date.get(snapshot_date), f"period {snapshot_date} 1C value")
        if len({float(value) for value in actual_values if isinstance(value, (int, float))}) != len(expected_dates):
            raise AssertionError(f"period 1C values must stay date-specific, got {actual_values}")


def _assert_weighted_unit_cost_semantics() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-onec-weighted-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_weighted_bundle(), activated_at="2026-05-15T11:55:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"weighted registry bundle must be accepted, got {accepted}")
        block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=OnecStocksBlock(
                _WeightedOnecStocksSource(),
                stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
            ),
            now_factory=lambda: datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc),
        )
        plan = block.build_plan(
            as_of_date=AS_OF_DATE,
            execution_mode="manual_operator",
            source_keys=[ONEC_STOCKS_SOURCE_KEY],
            metric_keys=[
                onec_stage_total_metric_key("CHINA_TO_FF", "qty"),
                onec_stage_total_metric_key("CHINA_TO_FF", "cost_total_rub"),
                onec_stage_total_metric_key("CHINA_TO_FF", "unit_cost_rub"),
                onec_stage_metric_key("CHINA_TO_FF", "unit_cost_rub"),
            ],
        )
        rows = {str(row[1]): row for row in _sheet_rows(plan, "DATA_VITRINA")}
        assert_close(
            rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'qty')}"][3],
            4.0,
            "weighted total qty",
        )
        assert_close(
            rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'cost_total_rub')}"][3],
            100.0,
            "weighted total cost",
        )
        assert_close(
            rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'unit_cost_rub')}"][3],
            25.0,
            "weighted total unit cost",
        )
        assert_close(
            rows[f"TOTAL|{onec_stage_total_metric_key('CHINA_TO_FF', 'unit_cost_rub')}"][2],
            25.0,
            "weighted historical total unit cost",
        )
        if rows[f"SKU:{NM_ID}|onec_CHINA_TO_FF_unit_cost_rub"][3] == rows[f"SKU:{MISSING_NM_ID}|onec_CHINA_TO_FF_unit_cost_rub"][3]:
            raise AssertionError("weighted fixture must keep distinct SKU unit costs")
        status_rows = {str(row[0]): row for row in _sheet_rows(plan, "STATUS")}
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"][1] != "success":
            raise AssertionError(f"weighted 1C historical status must be success, got {status_rows}")
        if status_rows[f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"][3] != AS_OF_DATE:
            raise AssertionError(f"weighted 1C historical lineage must match requested date, got {status_rows}")


def _build_weighted_bundle() -> dict[str, object]:
    return {
        "bundle_version": "sheet_vitrina_onec_weighted_unit_cost_smoke",
        "uploaded_at": "2026-05-15T11:55:00Z",
        "config_v2": [
            {
                "nm_id": NM_ID,
                "enabled": True,
                "display_name": "1C weighted SKU A",
                "group": "1C weighted",
                "display_order": 1,
            },
            {
                "nm_id": MISSING_NM_ID,
                "enabled": True,
                "display_name": "1C weighted SKU B",
                "group": "1C weighted",
                "display_order": 2,
            },
        ],
        "metrics_v2": [],
        "formulas_v2": [],
    }


def _build_legacy_period_snapshot() -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="sheet_vitrina_onec_stocks_wiring_smoke__legacy_period",
        snapshot_id=f"{PERIOD_OLD_DATE}__legacy_without_onec_rows__ready",
        as_of_date=PERIOD_OLD_DATE,
        date_columns=[PERIOD_OLD_DATE],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="snapshot",
                slot_label=PERIOD_OLD_DATE,
                column_date=PERIOD_OLD_DATE,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:C2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", PERIOD_OLD_DATE],
                rows=[
                    ["Legacy smoke row", f"SKU:{NM_ID}|legacy_smoke_metric", 1],
                ],
                row_count=1,
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "legacy_smoke_source[snapshot]",
                        "success",
                        PERIOD_OLD_DATE,
                        PERIOD_OLD_DATE,
                        PERIOD_OLD_DATE,
                        "",
                        "",
                        1,
                        1,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _with_unrelated_source_error(plan: SheetVitrinaV1Envelope) -> SheetVitrinaV1Envelope:
    sheets: list[SheetVitrinaWriteTarget] = []
    for sheet in plan.sheets:
        if sheet.sheet_name != "STATUS":
            sheets.append(sheet)
            continue
        rows = [list(row) for row in sheet.rows]
        rows.append(
            [
                "prices_snapshot[today_current]",
                "error",
                "blocked",
                TODAY_DATE,
                "",
                "",
                "",
                0,
                0,
                "",
                "synthetic unrelated source error for group status smoke",
            ]
        )
        sheets.append(
            replace(
                sheet,
                rows=rows,
                row_count=len(rows),
                column_count=len(sheet.header),
            )
        )
    return replace(plan, sheets=sheets)


class _PartialOnecStocksSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._source = _RequestDateArtifactOnecStocksSource(artifacts_root)

    @property
    def request_dates(self) -> list[str]:
        return self._source.request_dates

    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        payload = dict(self._source.fetch(request))
        payload[ONEC_STOCKS_PARTIAL_FETCH_META_KEY] = {
            "requested_count": 2,
            "requested_nm_ids": [NM_ID, MISSING_NM_ID],
            "successful_request_count": 1,
            "failure_count": 1,
            "missing_nm_ids": [MISSING_NM_ID],
            "status_codes": {"401": 1},
            "error_kinds": {"http": 1},
        }
        return payload


class _RequestDateArtifactOnecStocksSource:
    def __init__(self, artifacts_root: Path) -> None:
        self._source = ArtifactBackedOnecStocksSource(artifacts_root)
        self.request_dates: list[str] = []

    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        request_date = _request_date(request)
        self.request_dates.append(request_date)
        payload = deepcopy(self._source.fetch(request))
        meta = dict(payload.get("meta") or {})
        meta["date"] = request_date
        meta["generated_at"] = f"{request_date}T11:30:37"
        payload["meta"] = meta
        return payload


class _WeightedOnecStocksSource:
    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        request_date = _request_date(request)
        return {
            "meta": {
                "version": "1.0",
                "marketplace": "WB",
                "account_id": request.account_id,
                "date": request_date,
                "generated_at": f"{request_date}T11:30:37",
                "currency": "RUB",
            },
            "items": [
                {
                    "nmId": str(NM_ID),
                    "product_1c_id": "weighted-a",
                    "vendor_code": "weighted-a",
                    "name": "weighted-a",
                    "stages": {
                        "В_пути": {
                            "qty": 1.0,
                            "unit_cost_rub": 10.0,
                            "cost_total_rub": 10.0,
                        },
                    },
                },
                {
                    "nmId": str(MISSING_NM_ID),
                    "product_1c_id": "weighted-b",
                    "vendor_code": "weighted-b",
                    "name": "weighted-b",
                    "stages": {
                        "В_пути": {
                            "qty": 3.0,
                            "unit_cost_rub": 30.0,
                            "cost_total_rub": 90.0,
                        },
                    },
                },
            ],
        }


class _RequestDateDatedOnecStocksSource:
    def __init__(self) -> None:
        self.request_dates: list[str] = []

    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        request_date = _request_date(request)
        self.request_dates.append(request_date)
        qty = float(date.fromisoformat(request_date).day)
        return {
            "meta": {
                "version": "1.0",
                "marketplace": "WB",
                "account_id": request.account_id,
                "date": request_date,
                "generated_at": f"{request_date}T11:30:37",
                "currency": "RUB",
            },
            "items": [
                _build_dated_onec_item(NM_ID, qty, "dated-a"),
                _build_dated_onec_item(MISSING_NM_ID, qty + 1.0, "dated-b"),
            ],
        }


class _MismatchedHistoricalOnecStocksSource:
    def __init__(self) -> None:
        self.request_dates: list[str] = []

    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        request_date = _request_date(request)
        self.request_dates.append(request_date)
        payload_date = TODAY_DATE if request_date != TODAY_DATE else request_date
        qty = float(date.fromisoformat(payload_date).day)
        return {
            "meta": {
                "version": "1.0",
                "marketplace": "WB",
                "account_id": request.account_id,
                "date": payload_date,
                "generated_at": f"{payload_date}T11:30:37",
                "currency": "RUB",
            },
            "items": [
                _build_dated_onec_item(NM_ID, qty, "mismatch-a"),
            ],
        }


def _build_dated_onec_item(nm_id: int, qty: float, suffix: str) -> dict[str, object]:
    return {
        "nmId": str(nm_id),
        "product_1c_id": suffix,
        "vendor_code": suffix,
        "name": suffix,
        "stages": {
            "В_пути": {
                "qty": qty,
                "unit_cost_rub": 10.0,
                "cost_total_rub": qty * 10.0,
            },
        },
    }


def _request_date(request: OnecStocksRequest) -> str:
    request_date = str(request.date or "").strip()
    if not request_date:
        raise AssertionError("1C smoke source must be called with request.date")
    return request_date


def _assert_runtime_onec_snapshots_have_lineage(
    runtime: RegistryUploadDbBackedRuntime,
    expected_dates: list[str],
) -> None:
    snapshot_dates = runtime.list_temporal_source_snapshot_dates(source_key=ONEC_STOCKS_SOURCE_KEY)
    if snapshot_dates != expected_dates:
        raise AssertionError(f"runtime must persist one 1C snapshot per requested date, got {snapshot_dates}")
    for expected_date in expected_dates:
        payload, captured_at = runtime.load_temporal_source_snapshot(
            source_key=ONEC_STOCKS_SOURCE_KEY,
            snapshot_date=expected_date,
        )
        if not captured_at:
            raise AssertionError(f"runtime 1C snapshot {expected_date} must preserve captured_at")
        payload_date = _payload_meta_date(payload)
        if payload_date != expected_date:
            raise AssertionError(
                f"runtime 1C snapshot {expected_date} must preserve meta.date lineage, got {payload_date!r}"
            )


def _payload_meta_date(payload: object) -> str:
    meta = getattr(payload, "meta", None)
    if isinstance(payload, dict):
        meta = payload.get("meta")
    if isinstance(meta, dict):
        return str(meta.get("date") or "")
    return str(getattr(meta, "date", "") or "")


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


def assert_nonblank(actual: object, label: str) -> None:
    if actual is None or str(actual).strip() == "":
        raise AssertionError(f"{label} must be nonblank, got {actual!r}")


def assert_blank(actual: object, label: str) -> None:
    if actual is not None and str(actual).strip() != "":
        raise AssertionError(f"{label} must stay blank, got {actual!r}")


if __name__ == "__main__":
    main()
