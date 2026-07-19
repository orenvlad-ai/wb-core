"""Smoke coverage for structural zero semantics in 1C stock buckets."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.onec_stocks_block import OnecStocksBlock, normalize_onec_stocks_payload
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.application.sheet_vitrina_v1_onec_stocks import (
    DEFAULT_ONEC_STAGE_MAPPING,
    ONEC_STOCKS_SOURCE_KEY,
    onec_stage_metric_key,
    onec_stage_total_metric_key,
)
from packages.contracts.onec_stocks_block import OnecStocksRequest
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

SKU_A = 920001
SKU_B = 920002
TARGET_DATE = "2026-05-19"
CURRENT_DATE = "2026-05-20"
FF_QTY = onec_stage_total_metric_key("FF_STOCK", "qty")
FF_UNIT_COST = onec_stage_total_metric_key("FF_STOCK", "unit_cost_rub")
FF_COST = onec_stage_total_metric_key("FF_STOCK", "cost_total_rub")
CHINA_QTY = onec_stage_total_metric_key("CHINA_TO_FF", "qty")
CHINA_UNIT_COST = onec_stage_total_metric_key("CHINA_TO_FF", "unit_cost_rub")
CHINA_COST = onec_stage_total_metric_key("CHINA_TO_FF", "cost_total_rub")


def main() -> None:
    _assert_successful_missing_bucket_materializes_zero("FF_STOCK", (FF_QTY, FF_UNIT_COST, FF_COST))
    _assert_successful_missing_bucket_materializes_zero("CHINA_TO_FF", (CHINA_QTY, CHINA_UNIT_COST, CHINA_COST))
    _assert_source_error_does_not_materialize_zero()
    _assert_date_mismatch_does_not_materialize_zero()
    _assert_unknown_stage_does_not_materialize_zero()
    _assert_invalid_attempt_preserves_accepted_truth()
    print("sheet_vitrina_onec_zero_stock_empty_bucket: ok -> structural_zero_only_for_fresh_success")


def _assert_successful_missing_bucket_materializes_zero(
    missing_stage: str,
    metric_keys: tuple[str, str, str],
) -> None:
    with _runtime() as runtime:
        plan = _build_plan(runtime, _OnecZeroStockSource(missing_stage=missing_stage))
        rows = _data_rows(plan)
        date_idx = _date_index(plan, TARGET_DATE)
        for metric_key in metric_keys:
            assert_close(rows[f"TOTAL|{metric_key}"][date_idx], 0.0, f"{missing_stage} {metric_key}")
        status_row = _find_status_row(plan, TARGET_DATE)
        if status_row[1] != "success":
            raise AssertionError(f"empty {missing_stage} bucket must keep success status, got {status_row}")
        note = str(status_row[10] if len(status_row) > 10 else "")
        if f"zero_stock_stage_buckets={missing_stage}" not in note:
            raise AssertionError(f"empty {missing_stage} bucket must be marked zero-stock, got {status_row}")


def _assert_source_error_does_not_materialize_zero() -> None:
    with _runtime() as runtime:
        plan = _build_plan(runtime, _OnecZeroStockSource(mode="error"))
        _assert_blank_total(plan, FF_QTY, TARGET_DATE, "source error qty")
        _assert_blank_total(plan, FF_UNIT_COST, TARGET_DATE, "source error unit cost")
        _assert_blank_total(plan, FF_COST, TARGET_DATE, "source error cost")
        status_row = _find_status_row(plan, TARGET_DATE)
        if status_row[1] == "success" or "zero_stock_stage_buckets=" in str(status_row[10] if len(status_row) > 10 else ""):
            raise AssertionError(f"source error must not become zero-stock success, got {status_row}")


def _assert_date_mismatch_does_not_materialize_zero() -> None:
    with _runtime() as runtime:
        plan = _build_plan(runtime, _OnecZeroStockSource(payload_date="2026-05-18"))
        _assert_blank_total(plan, FF_QTY, TARGET_DATE, "date mismatch qty")
        _assert_blank_total(plan, FF_UNIT_COST, TARGET_DATE, "date mismatch unit cost")
        _assert_blank_total(plan, FF_COST, TARGET_DATE, "date mismatch cost")
        status_row = _find_status_row(plan, TARGET_DATE)
        note = str(status_row[10] if len(status_row) > 10 else "")
        if status_row[1] == "success" or "zero_stock_stage_buckets=" in note:
            raise AssertionError(f"date mismatch must not become zero-stock success, got {status_row}")


def _assert_unknown_stage_does_not_materialize_zero() -> None:
    with _runtime() as runtime:
        plan = _build_plan(runtime, _OnecZeroStockSource(missing_stage="FF_STOCK", include_unknown_stage=True))
        _assert_blank_total(plan, FF_QTY, TARGET_DATE, "unknown stage qty")
        _assert_blank_total(plan, FF_UNIT_COST, TARGET_DATE, "unknown stage unit cost")
        _assert_blank_total(plan, FF_COST, TARGET_DATE, "unknown stage cost")
        status_row = _find_status_row(plan, TARGET_DATE)
        note = str(status_row[10] if len(status_row) > 10 else "")
        if "missing_stage_buckets=FF_STOCK" not in note or "zero_stock_stage_buckets=" in note:
            raise AssertionError(f"unknown stage must stay diagnostic, got {status_row}")


def _assert_invalid_attempt_preserves_accepted_truth() -> None:
    with _runtime() as runtime:
        runtime.save_temporal_source_slot_snapshot(
            source_key=ONEC_STOCKS_SOURCE_KEY,
            snapshot_date=TARGET_DATE,
            snapshot_role="accepted_closed_day_snapshot",
            captured_at="2026-05-19T19:10:00Z",
            payload=_accepted_payload(TARGET_DATE),
        )
        plan = _build_plan(runtime, _OnecZeroStockSource(mode="error"))
        rows = _data_rows(plan)
        date_idx = _date_index(plan, TARGET_DATE)
        assert_close(rows[f"TOTAL|{FF_QTY}"][date_idx], 12.0, "accepted FF_STOCK qty")
        assert_close(rows[f"TOTAL|{FF_COST}"][date_idx], 1490.0, "accepted FF_STOCK cost")
        assert_close(rows[f"TOTAL|{FF_UNIT_COST}"][date_idx], 1490.0 / 12.0, "accepted FF_STOCK unit cost")
        note = str(_find_status_row(plan, TARGET_DATE)[10])
        if "latest_attempt_kind=error" not in note:
            raise AssertionError(f"invalid attempt must disclose accepted preservation, got {note}")


class _RuntimeContext:
    def __init__(self) -> None:
        self._tmp = TemporaryDirectory(prefix="sheet-vitrina-onec-zero-stock-")
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(self._tmp.name))

    def __enter__(self) -> RegistryUploadDbBackedRuntime:
        accepted = self.runtime.ingest_bundle(_build_bundle(), activated_at="2026-05-19T08:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"zero-stock bundle must be accepted, got {accepted}")
        return self.runtime

    def __exit__(self, *_exc: object) -> None:
        self._tmp.cleanup()


def _runtime() -> _RuntimeContext:
    return _RuntimeContext()


def _build_plan(runtime: RegistryUploadDbBackedRuntime, source: "_OnecZeroStockSource") -> SheetVitrinaV1Envelope:
    block = SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        onec_stocks_block=OnecStocksBlock(source, stage_mapping=DEFAULT_ONEC_STAGE_MAPPING),
        now_factory=lambda: datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc),
    )
    return block.build_plan(
        as_of_date=TARGET_DATE,
        execution_mode="manual_operator",
        source_keys=[ONEC_STOCKS_SOURCE_KEY],
        _include_archived_metrics_for_audit=True,
        metric_keys=[
            FF_QTY,
            FF_UNIT_COST,
            FF_COST,
            CHINA_QTY,
            CHINA_UNIT_COST,
            CHINA_COST,
            onec_stage_metric_key("FF_STOCK", "qty"),
            onec_stage_metric_key("FF_STOCK", "unit_cost_rub"),
            onec_stage_metric_key("FF_STOCK", "cost_total_rub"),
            onec_stage_metric_key("CHINA_TO_FF", "qty"),
            onec_stage_metric_key("CHINA_TO_FF", "unit_cost_rub"),
            onec_stage_metric_key("CHINA_TO_FF", "cost_total_rub"),
        ],
    )


def _accepted_payload(snapshot_date: str) -> Any:
    return normalize_onec_stocks_payload(
        _payload(snapshot_date=snapshot_date),
        stage_mapping=DEFAULT_ONEC_STAGE_MAPPING,
    ).result


class _OnecZeroStockSource:
    def __init__(
        self,
        *,
        mode: str = "success",
        missing_stage: str | None = None,
        include_unknown_stage: bool = False,
        payload_date: str | None = None,
    ) -> None:
        self.mode = mode
        self.missing_stage = missing_stage
        self.include_unknown_stage = include_unknown_stage
        self.payload_date = payload_date

    def fetch(self, request: OnecStocksRequest) -> dict[str, object]:
        if self.mode == "error":
            raise RuntimeError("source_error_for_zero_stock_smoke")
        request_date = str(request.date or "").strip()
        if not request_date:
            raise AssertionError("zero-stock smoke requires date-specific 1C requests")
        return _payload(
            snapshot_date=self.payload_date or request_date,
            account_id=request.account_id,
            missing_stage=self.missing_stage,
            include_unknown_stage=self.include_unknown_stage,
        )


def _payload(
    *,
    snapshot_date: str,
    account_id: str = "000000001",
    missing_stage: str | None = None,
    include_unknown_stage: bool = False,
) -> dict[str, object]:
    return {
        "meta": {
            "version": "1.0",
            "marketplace": "WB",
            "account_id": account_id,
            "date": snapshot_date,
            "generated_at": f"{snapshot_date}T07:55:00",
            "currency": "RUB",
        },
        "items": [
            _item(SKU_A, missing_stage=missing_stage, include_unknown_stage=include_unknown_stage),
            _item(SKU_B, missing_stage=missing_stage, include_unknown_stage=include_unknown_stage),
        ],
    }


def _item(nm_id: int, *, missing_stage: str | None, include_unknown_stage: bool) -> dict[str, object]:
    stages: dict[str, dict[str, float]] = {
        "CHINA_TO_FF": {"qty": 2.0, "unit_cost_rub": 60.0, "cost_total_rub": 120.0},
        "FF_STOCK": {"qty": 5.0, "unit_cost_rub": 130.0, "cost_total_rub": 650.0},
        "FF_TO_WB": {"qty": 1.0, "unit_cost_rub": 120.0, "cost_total_rub": 120.0},
        "WB_STOCK": {"qty": 3.0, "unit_cost_rub": 150.0, "cost_total_rub": 450.0},
    }
    if nm_id == SKU_B:
        stages["CHINA_TO_FF"] = {"qty": 3.0, "unit_cost_rub": 60.0, "cost_total_rub": 180.0}
        stages["FF_STOCK"] = {"qty": 7.0, "unit_cost_rub": 120.0, "cost_total_rub": 840.0}
        stages["FF_TO_WB"] = {"qty": 2.0, "unit_cost_rub": 120.0, "cost_total_rub": 240.0}
        stages["WB_STOCK"] = {"qty": 3.0, "unit_cost_rub": 150.0, "cost_total_rub": 450.0}
    if missing_stage:
        stages.pop(missing_stage, None)
    if include_unknown_stage:
        stages["NEW_UNKNOWN_STAGE"] = {"qty": 1.0, "unit_cost_rub": 10.0, "cost_total_rub": 10.0}
    return {
        "nmId": str(nm_id),
        "product_1c_id": str(nm_id),
        "vendor_code": str(nm_id),
        "name": str(nm_id),
        "stages": stages,
    }


def _build_bundle() -> dict[str, object]:
    return {
        "bundle_version": "sheet_vitrina_onec_zero_stock_empty_bucket_smoke",
        "uploaded_at": "2026-05-19T08:00:00Z",
        "config_v2": [
            {"nm_id": SKU_A, "enabled": True, "display_name": "Zero SKU A", "group": "Zero", "display_order": 1},
            {"nm_id": SKU_B, "enabled": True, "display_name": "Zero SKU B", "group": "Zero", "display_order": 2},
        ],
        "metrics_v2": [
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


def _find_status_row(plan: SheetVitrinaV1Envelope, column_date: str) -> list[Any]:
    fallback_row: list[Any] | None = None
    fallback_slot = (
        f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]"
        if column_date == TARGET_DATE
        else f"{ONEC_STOCKS_SOURCE_KEY}[today_current]"
    )
    for row in _sheet_rows(plan, "STATUS"):
        if row[0] in {
            f"{ONEC_STOCKS_SOURCE_KEY}[yesterday_closed]",
            f"{ONEC_STOCKS_SOURCE_KEY}[today_current]",
        }:
            if row[3] == column_date:
                return row
            if row[0] == fallback_slot and fallback_row is None:
                fallback_row = row
    if fallback_row is not None:
        return fallback_row
    raise AssertionError(f"plan missing 1C status row for {column_date}: {_sheet_rows(plan, 'STATUS')}")


def _assert_blank_total(plan: SheetVitrinaV1Envelope, metric_key: str, column_date: str, label: str) -> None:
    rows = _data_rows(plan)
    date_idx = _date_index(plan, column_date)
    value = rows[f"TOTAL|{metric_key}"][date_idx]
    if value is not None and str(value).strip() != "":
        raise AssertionError(f"{label} must stay blank, got {value!r}")


def assert_close(actual: object, expected: float, label: str) -> None:
    if not isinstance(actual, (int, float)) or abs(float(actual) - expected) > 0.01:
        raise AssertionError(f"{label} mismatch: expected {expected}, got {actual!r}")


if __name__ == "__main__":
    main()
