"""Regression smoke for partial FF_STOCK blanks in the 1C stock-capital group."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.onec_stocks_block import OnecStocksBlock, normalize_onec_stocks_payload
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.application.sheet_vitrina_v1_onec_stocks import (
    DEFAULT_ONEC_STAGE_MAPPING,
    ONEC_STOCKS_SOURCE_GROUP_ID,
    ONEC_STOCKS_SOURCE_KEY,
    ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
    ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
    onec_stage_metric_key,
    onec_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.contracts.onec_stocks_block import OnecStocksRequest
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

SKU_A = 910001
SKU_B = 910002
FILLED_DATE = "2026-05-18"
MISSING_DATE = "2026-05-19"
CURRENT_DATE = "2026-05-20"
FF_TOTAL_QTY = onec_stage_total_metric_key("FF_STOCK", "qty")
FF_TOTAL_UNIT_COST = onec_stage_total_metric_key("FF_STOCK", "unit_cost_rub")
FF_TOTAL_COST = onec_stage_total_metric_key("FF_STOCK", "cost_total_rub")
CHINA_TOTAL_QTY = onec_stage_total_metric_key("CHINA_TO_FF", "qty")
WB_TOTAL_COST = onec_stage_total_metric_key("WB_STOCK", "cost_total_rub")
UNRELATED_ROW_ID = "TOTAL|unrelated_smoke_metric"


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-onec-ff-stock-regression-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-18T08:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"regression bundle must be accepted, got {accepted}")

        source = _FfStockRegressionSource()
        first_plan = _build_plan(runtime, source, as_of_date=FILLED_DATE, current_date=MISSING_DATE)
        _assert_plan_ff_stock_filled(first_plan, FILLED_DATE)
        _assert_plan_ff_stock_zero(first_plan, MISSING_DATE)
        _assert_ff_stock_status_has_zero_stock(first_plan, MISSING_DATE)
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=runtime.load_current_state(),
            refreshed_at="2026-05-19T08:05:00Z",
            plan=first_plan,
        )
        runtime.save_temporal_source_slot_snapshot(
            source_key=ONEC_STOCKS_SOURCE_KEY,
            snapshot_date=MISSING_DATE,
            snapshot_role="accepted_current_snapshot",
            captured_at="2026-05-19T08:10:00Z",
            payload=_accepted_onec_payload(MISSING_DATE),
        )

        second_plan = _build_plan(runtime, source, as_of_date=MISSING_DATE, current_date=CURRENT_DATE)
        _assert_plan_ff_stock_zero(second_plan, MISSING_DATE)
        _assert_plan_ff_stock_zero(second_plan, CURRENT_DATE)
        _assert_ff_stock_status_has_zero_stock(second_plan, MISSING_DATE)
        _assert_ff_stock_status_has_zero_stock(second_plan, CURRENT_DATE)
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=runtime.load_current_state(),
            refreshed_at="2026-05-20T08:05:00Z",
            plan=second_plan,
        )

        period_contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 5, 20, 8, 10, tzinfo=timezone.utc),
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from=FILLED_DATE,
            date_to=CURRENT_DATE,
        )
        period_rows = {row.row_id: row for row in period_contract.rows}
        _assert_web_ff_stock_filled(period_rows, FILLED_DATE)
        _assert_web_ff_stock_zero(period_rows, MISSING_DATE)
        _assert_web_ff_stock_zero(period_rows, CURRENT_DATE)
        assert_close(
            period_rows[f"TOTAL|{ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY}"].values_by_date.get(MISSING_DATE),
            1560.0,
            "missing-date total 1C cost must include available source buckets plus structural zero FF_STOCK",
        )

        stale_plan = _with_stale_ff_stock_cells_and_unrelated_row(second_plan)
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=runtime.load_current_state(),
            refreshed_at="2026-05-20T08:20:00Z",
            plan=stale_plan,
        )

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=Path(tmp),
            runtime=runtime,
            activated_at_factory=lambda: "2026-05-20T08:30:00Z",
            refreshed_at_factory=lambda: "2026-05-20T08:30:00Z",
            now_factory=lambda: datetime(2026, 5, 20, 8, 30, tzinfo=timezone.utc),
        )
        entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            onec_stocks_block=OnecStocksBlock(source, stage_mapping=DEFAULT_ONEC_STAGE_MAPPING),
            now_factory=lambda: datetime(2026, 5, 20, 8, 30, tzinfo=timezone.utc),
        )
        job = entrypoint.start_sheet_source_group_refresh_job(
            source_group_id=ONEC_STOCKS_SOURCE_GROUP_ID,
            as_of_date=MISSING_DATE,
        )
        job_snapshot = _wait_job(entrypoint, str(job["job_id"]))
        if job_snapshot["status"] != "success":
            raise AssertionError(f"1C FF_STOCK group refresh must finish, got {job_snapshot}")
        job_result = dict(job_snapshot.get("result") or {})
        if job_result.get("semantic_status") != "success":
            raise AssertionError(f"zero-stock FF_STOCK group refresh must be a success, got {job_result}")

        repaired_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=MISSING_DATE)
        repaired_rows = _data_rows(repaired_plan)
        selected_idx = _date_index(repaired_plan, MISSING_DATE)
        current_idx = _date_index(repaired_plan, CURRENT_DATE)
        assert_close(repaired_rows[f"TOTAL|{FF_TOTAL_QTY}"][selected_idx], 0.0, "selected FF_STOCK qty after group refresh")
        assert_close(
            repaired_rows[f"TOTAL|{FF_TOTAL_UNIT_COST}"][selected_idx],
            0.0,
            "selected FF_STOCK unit cost after group refresh",
        )
        assert_close(
            repaired_rows[f"TOTAL|{FF_TOTAL_COST}"][selected_idx],
            0.0,
            "selected FF_STOCK cost after group refresh",
        )
        for row_id in _ff_total_row_ids():
            assert_close(repaired_rows[row_id][current_idx], 777.0, f"{row_id} non-selected date must be preserved")
        unrelated = repaired_rows[UNRELATED_ROW_ID]
        if unrelated[_date_index(repaired_plan, MISSING_DATE)] != "keep-selected":
            raise AssertionError(f"group refresh must preserve unrelated selected-date cells, got {unrelated}")
        _assert_ff_stock_status_has_zero_stock(repaired_plan, MISSING_DATE)
        page_payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            as_of_date=MISSING_DATE,
            include_source_status=True,
        )
        _assert_loading_table_explains_zero_stock(page_payload)

        repaired_contract = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 5, 20, 8, 35, tzinfo=timezone.utc),
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from=FILLED_DATE,
            date_to=CURRENT_DATE,
        )
        repaired_web_rows = {row.row_id: row for row in repaired_contract.rows}
        _assert_web_ff_stock_filled(repaired_web_rows, FILLED_DATE)
        _assert_web_ff_stock_zero(repaired_web_rows, MISSING_DATE)

    print("sheet_vitrina_onec_ff_stock_partial_regression: ok -> fresh_empty_bucket_materialized_as_zero")
    print("sheet_vitrina_onec_ff_stock_group_refresh: ok -> zero_stock_written_and_unrelated_preserved")


def _build_plan(
    runtime: RegistryUploadDbBackedRuntime,
    source: "_FfStockRegressionSource",
    *,
    as_of_date: str,
    current_date: str,
) -> SheetVitrinaV1Envelope:
    block = SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        onec_stocks_block=OnecStocksBlock(source, stage_mapping=DEFAULT_ONEC_STAGE_MAPPING),
        now_factory=lambda: datetime.fromisoformat(f"{current_date}T08:00:00+00:00"),
    )
    return block.build_plan(
        as_of_date=as_of_date,
        execution_mode="manual_operator",
        source_keys=[ONEC_STOCKS_SOURCE_KEY],
        metric_keys=[
            ONEC_STOCKS_TOTAL_QTY_METRIC_KEY,
            ONEC_STOCKS_TOTAL_COST_RUB_METRIC_KEY,
            CHINA_TOTAL_QTY,
            FF_TOTAL_QTY,
            FF_TOTAL_UNIT_COST,
            FF_TOTAL_COST,
            WB_TOTAL_COST,
            onec_stage_metric_key("FF_STOCK", "qty"),
            onec_stage_metric_key("FF_STOCK", "unit_cost_rub"),
            onec_stage_metric_key("FF_STOCK", "cost_total_rub"),
        ],
    )


def _accepted_onec_payload(snapshot_date: str) -> Any:
    return normalize_onec_stocks_payload(
        {
            "meta": {
                "version": "1.0",
                "marketplace": "WB",
                "account_id": "000000001",
                "date": snapshot_date,
                "generated_at": f"{snapshot_date}T08:10:00",
                "currency": "RUB",
            },
            "items": [
                _build_onec_item(SKU_A, "sku-a", include_ff_stock=True),
                _build_onec_item(SKU_B, "sku-b", include_ff_stock=True),
            ],
        },
        stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
    ).result


def _assert_plan_ff_stock_filled(plan: SheetVitrinaV1Envelope, column_date: str) -> None:
    rows = _data_rows(plan)
    date_idx = _date_index(plan, column_date)
    _assert_exact_labels(rows)
    assert_close(rows[f"TOTAL|{FF_TOTAL_QTY}"][date_idx], 12.0, "FF_STOCK total qty")
    assert_close(rows[f"TOTAL|{FF_TOTAL_COST}"][date_idx], 1490.0, "FF_STOCK total cost")
    assert_close(rows[f"TOTAL|{FF_TOTAL_UNIT_COST}"][date_idx], 1490.0 / 12.0, "FF_STOCK weighted unit cost")


def _assert_plan_ff_stock_zero(plan: SheetVitrinaV1Envelope, column_date: str) -> None:
    rows = _data_rows(plan)
    date_idx = _date_index(plan, column_date)
    assert_close(rows[f"TOTAL|{FF_TOTAL_QTY}"][date_idx], 0.0, f"FF_STOCK total qty {column_date}")
    assert_close(rows[f"TOTAL|{FF_TOTAL_COST}"][date_idx], 0.0, f"FF_STOCK total cost {column_date}")
    assert_close(rows[f"TOTAL|{FF_TOTAL_UNIT_COST}"][date_idx], 0.0, f"FF_STOCK weighted unit cost {column_date}")
    assert_close(rows[f"TOTAL|{CHINA_TOTAL_QTY}"][date_idx], 5.0, "neighbor CHINA_TO_FF qty")
    assert_close(rows[f"TOTAL|{WB_TOTAL_COST}"][date_idx], 900.0, "neighbor WB_STOCK cost")


def _assert_web_ff_stock_filled(rows: dict[str, Any], column_date: str) -> None:
    assert_close(rows[f"TOTAL|{FF_TOTAL_QTY}"].values_by_date.get(column_date), 12.0, "web FF_STOCK total qty")
    assert_close(rows[f"TOTAL|{FF_TOTAL_COST}"].values_by_date.get(column_date), 1490.0, "web FF_STOCK total cost")
    assert_close(
        rows[f"TOTAL|{FF_TOTAL_UNIT_COST}"].values_by_date.get(column_date),
        1490.0 / 12.0,
        "web FF_STOCK unit cost",
    )


def _assert_web_ff_stock_zero(rows: dict[str, Any], column_date: str) -> None:
    assert_close(rows[f"TOTAL|{FF_TOTAL_QTY}"].values_by_date.get(column_date), 0.0, "web FF_STOCK total qty")
    assert_close(rows[f"TOTAL|{FF_TOTAL_COST}"].values_by_date.get(column_date), 0.0, "web FF_STOCK total cost")
    assert_close(
        rows[f"TOTAL|{FF_TOTAL_UNIT_COST}"].values_by_date.get(column_date),
        0.0,
        "web FF_STOCK unit cost",
    )
    assert_close(
        rows[f"TOTAL|{CHINA_TOTAL_QTY}"].values_by_date.get(column_date),
        5.0,
        "web neighbor CHINA_TO_FF qty",
    )


def _find_onec_status_row(plan: SheetVitrinaV1Envelope, column_date: str) -> list[Any]:
    rows = {str(row[0]): row for row in _sheet_rows(plan, "STATUS")}
    matching_rows = [
        row for row in rows.values()
        if row[0] == f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]" and row[3] == column_date
    ]
    if not matching_rows:
        matching_rows = [
            row for row in rows.values()
            if row[0] == f"{ONEC_STOCKS_SOURCE_KEY}[today_current]" and row[3] == column_date
        ]
    if not matching_rows:
        raise AssertionError(f"missing FF_STOCK status row for {column_date}: {rows}")
    return matching_rows[0]


def _assert_ff_stock_status_has_zero_stock(plan: SheetVitrinaV1Envelope, column_date: str) -> None:
    status_row = _find_onec_status_row(plan, column_date)
    if status_row[1] != "success":
        raise AssertionError(f"empty FF_STOCK bucket must be a successful zero-stock source status, got {status_row}")
    note = str(status_row[10] if len(status_row) > 10 else "")
    if "zero_stock_stage_buckets=FF_STOCK" not in note:
        raise AssertionError(f"zero-stock FF_STOCK status must name the bucket, got {status_row}")


def _assert_missing_ff_stock_status(plan: SheetVitrinaV1Envelope, column_date: str) -> None:
    status_row = _find_onec_status_row(plan, column_date)
    if status_row[1] != "incomplete":
        raise AssertionError(f"missing FF_STOCK must surface incomplete source status, got {status_row}")
    note = str(status_row[10] if len(status_row) > 10 else "")
    if "missing_stage_buckets=FF_STOCK" not in note:
        raise AssertionError(f"missing FF_STOCK status must name the missing bucket, got {status_row}")


def _assert_ff_stock_status_has_accepted_fallback(plan: SheetVitrinaV1Envelope, column_date: str) -> None:
    _assert_missing_ff_stock_status(plan, column_date)
    status_row = _find_onec_status_row(plan, column_date)
    note = str(status_row[10] if len(status_row) > 10 else "")
    if "accepted_fallback_stage_buckets=FF_STOCK" not in note:
        raise AssertionError(f"accepted fallback status must name FF_STOCK fallback, got {status_row}")


def _assert_loading_table_explains_zero_stock(page_payload: dict[str, Any]) -> None:
    loading_table = ((page_payload.get("activity_surface") or {}).get("loading_table") or {})
    onec_rows = [
        row for row in (loading_table.get("rows") or [])
        if row.get("source_key") == ONEC_STOCKS_SOURCE_KEY
    ]
    if not onec_rows:
        raise AssertionError(f"source-status loading table must expose 1C row, got {loading_table}")
    reason_text = " ".join(
        str(onec_rows[0].get(key) or "")
        for key in ("today_reason", "yesterday_reason")
    )
    if "stage bucket: FF_STOCK" not in reason_text or "нулевой остаток" not in reason_text:
        raise AssertionError(f"source-status reason must explain zero-stock FF_STOCK, got {onec_rows[0]}")


def _assert_exact_labels(rows: dict[str, list[Any]]) -> None:
    expected = {
        f"TOTAL|{FF_TOTAL_QTY}": "1С ФФ: всего кол-во",
        f"TOTAL|{FF_TOTAL_UNIT_COST}": "1С ФФ: средневзвешенная себестоимость за ед., руб",
        f"TOTAL|{FF_TOTAL_COST}": "1С ФФ: всего капитал, руб",
    }
    for row_id, label in expected.items():
        if rows[row_id][0] not in {label, f"Итого: {label}"}:
            raise AssertionError(f"{row_id} label mismatch: expected {label!r}, got {rows[row_id][0]!r}")


def _with_stale_ff_stock_cells_and_unrelated_row(plan: SheetVitrinaV1Envelope) -> SheetVitrinaV1Envelope:
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name != "DATA_VITRINA":
            sheets.append(sheet)
            continue
        selected_idx = _header_date_index(sheet.header, MISSING_DATE)
        current_idx = _header_date_index(sheet.header, CURRENT_DATE)
        rows = [list(row) for row in sheet.rows]
        for row in rows:
            if str(row[1] if len(row) > 1 else "") in _ff_total_row_ids():
                row[selected_idx] = 999.0
                row[current_idx] = 777.0
        rows.append(["Unrelated smoke metric", UNRELATED_ROW_ID, "keep-selected", "keep-current"])
        sheets.append(replace(sheet, rows=rows, row_count=len(rows)))
    return replace(plan, sheets=sheets)


class _FfStockRegressionSource:
    def __init__(self) -> None:
        self.request_dates: list[str] = []

    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        request_date = str(request.date or "").strip()
        if not request_date:
            raise AssertionError("FF_STOCK regression source requires request.date")
        self.request_dates.append(request_date)
        include_ff_stock = request_date == FILLED_DATE
        return {
            "meta": {
                "version": "1.0",
                "marketplace": "WB",
                "account_id": request.account_id,
                "date": request_date,
                "generated_at": f"{request_date}T07:55:00",
                "currency": "RUB",
            },
            "items": [
                _build_onec_item(SKU_A, "sku-a", include_ff_stock=include_ff_stock),
                _build_onec_item(SKU_B, "sku-b", include_ff_stock=include_ff_stock),
            ],
        }


def _build_onec_item(nm_id: int, suffix: str, *, include_ff_stock: bool) -> dict[str, object]:
    stages: dict[str, dict[str, float]] = {
        "CHINA_TO_FF": {"qty": 2.0, "unit_cost_rub": 60.0, "cost_total_rub": 120.0},
        "FF_TO_WB": {"qty": 1.0, "unit_cost_rub": 120.0, "cost_total_rub": 120.0},
        "WB_STOCK": {"qty": 3.0, "unit_cost_rub": 150.0, "cost_total_rub": 450.0},
    }
    if nm_id == SKU_B:
        stages["CHINA_TO_FF"] = {"qty": 3.0, "unit_cost_rub": 60.0, "cost_total_rub": 180.0}
        stages["FF_TO_WB"] = {"qty": 2.0, "unit_cost_rub": 120.0, "cost_total_rub": 240.0}
        stages["WB_STOCK"] = {"qty": 3.0, "unit_cost_rub": 150.0, "cost_total_rub": 450.0}
    if include_ff_stock:
        stages["FF_STOCK"] = (
            {"qty": 5.0, "unit_cost_rub": 130.0, "cost_total_rub": 650.0}
            if nm_id == SKU_A
            else {"qty": 7.0, "unit_cost_rub": 120.0, "cost_total_rub": 840.0}
        )
    return {
        "nmId": str(nm_id),
        "product_1c_id": suffix,
        "vendor_code": suffix,
        "name": suffix,
        "stages": stages,
    }


def _build_bundle() -> dict[str, object]:
    return {
        "bundle_version": "sheet_vitrina_onec_ff_stock_partial_regression_smoke",
        "uploaded_at": "2026-05-18T08:00:00Z",
        "config_v2": [
            {"nm_id": SKU_A, "enabled": True, "display_name": "FF SKU A", "group": "FF", "display_order": 1},
            {"nm_id": SKU_B, "enabled": True, "display_name": "FF SKU B", "group": "FF", "display_order": 2},
        ],
        "metrics_v2": [
            {
                "metric_key": "orderSum",
                "enabled": True,
                "scope": "SKU",
                "label_ru": "Сумма заказов",
                "calc_type": "metric",
                "calc_ref": "orderSum",
                "show_in_data": True,
                "format": "rub",
                "display_order": 10,
                "section": "Воронка",
            },
            {
                "metric_key": "orderCount",
                "enabled": True,
                "scope": "SKU",
                "label_ru": "Заказы",
                "calc_type": "metric",
                "calc_ref": "orderCount",
                "show_in_data": True,
                "format": "integer",
                "display_order": 20,
                "section": "Воронка",
            },
            {
                "metric_key": "ads_sum",
                "enabled": True,
                "scope": "SKU",
                "label_ru": "Расход рекламы",
                "calc_type": "metric",
                "calc_ref": "ads_sum",
                "show_in_data": True,
                "format": "rub",
                "display_order": 30,
                "section": "Реклама",
            },
            {
                "metric_key": "total_orderSum",
                "enabled": True,
                "scope": "TOTAL",
                "label_ru": "Сумма заказов всего",
                "calc_type": "metric",
                "calc_ref": "orderSum",
                "show_in_data": True,
                "format": "rub",
                "display_order": 40,
                "section": "Воронка",
            },
        ],
        "formulas_v2": [],
    }


def _data_rows(plan: SheetVitrinaV1Envelope) -> dict[str, list[Any]]:
    return {str(row[1]): list(row) for row in _sheet_rows(plan, "DATA_VITRINA")}


def _sheet_rows(plan: SheetVitrinaV1Envelope, sheet_name: str) -> list[list[Any]]:
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return [list(row) for row in sheet.rows]
    raise AssertionError(f"plan missing sheet {sheet_name}")


def _date_index(plan: SheetVitrinaV1Envelope, column_date: str) -> int:
    for sheet in plan.sheets:
        if sheet.sheet_name == "DATA_VITRINA":
            return _header_date_index(sheet.header, column_date)
    raise AssertionError("plan missing DATA_VITRINA sheet")


def _header_date_index(header: list[Any], column_date: str) -> int:
    try:
        return [str(item) for item in header].index(column_date)
    except ValueError as exc:
        raise AssertionError(f"header missing date {column_date}: {header}") from exc


def _ff_total_row_ids() -> tuple[str, str, str]:
    return (
        f"TOTAL|{FF_TOTAL_QTY}",
        f"TOTAL|{FF_TOTAL_UNIT_COST}",
        f"TOTAL|{FF_TOTAL_COST}",
    )


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


def assert_blank(actual: object, label: str) -> None:
    if actual is not None and str(actual).strip() != "":
        raise AssertionError(f"{label} must be blank, got {actual!r}")


if __name__ == "__main__":
    main()
