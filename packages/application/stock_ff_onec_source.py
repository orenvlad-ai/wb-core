"""Read-side resolver for stock_ff rows from materialized 1C FF_STOCK metrics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_onec_stocks import onec_stage_metric_key
from packages.contracts.factory_order_supply import (
    STOCK_FF_SOURCE_ONEC_FF_STOCK,
    FactoryOrderStockFfOnecState,
    FactoryOrderStockFfRow,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


ONEC_FF_STOCK_STAGE_BUCKET = "FF_STOCK"
ONEC_FF_STOCK_QTY_METRIC_KEY = onec_stage_metric_key(ONEC_FF_STOCK_STAGE_BUCKET, "qty")
ONEC_FF_STOCK_SOURCE_LABEL_RU = "1С / Фулфилмент"


@dataclass(frozen=True)
class OnecStockFfResolveResult:
    rows: list[FactoryOrderStockFfRow]
    state: FactoryOrderStockFfOnecState


def build_onec_stock_ff_state(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    active_skus: list[tuple[int, str]],
    sample_size: int = 5,
) -> FactoryOrderStockFfOnecState:
    return resolve_onec_stock_ff_rows(
        runtime=runtime,
        active_skus=active_skus,
        sample_size=sample_size,
    ).state


def resolve_onec_stock_ff_rows(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    active_skus: list[tuple[int, str]],
    sample_size: int = 5,
) -> OnecStockFfResolveResult:
    if not active_skus:
        return OnecStockFfResolveResult(
            rows=[],
            state=_state(
                status="error",
                errors=("current registry config_v2 does not contain enabled rows for 1C FF_STOCK check",),
            ),
        )

    try:
        plan = runtime.load_sheet_vitrina_ready_snapshot()
    except Exception as exc:
        return OnecStockFfResolveResult(
            rows=[],
            state=_state(
                status="missing",
                active_sku_count=len(active_skus),
                errors=(f"sheet_vitrina_v1 ready snapshot with 1C metrics is missing: {exc}",),
            ),
        )

    try:
        data_sheet = _sheet(plan, "DATA_VITRINA")
    except ValueError as exc:
        return OnecStockFfResolveResult(
            rows=[],
            state=_state(
                status="missing",
                snapshot_date=str(plan.as_of_date or ""),
                active_sku_count=len(active_skus),
                errors=(str(exc),),
            ),
        )

    snapshot_date, date_index = _select_latest_date_column(plan, data_sheet.header)
    row_by_id = {
        str(row[1]): list(row)
        for row in data_sheet.rows
        if len(row) > 1 and str(row[1] or "").strip()
    }

    rows: list[FactoryOrderStockFfRow] = []
    sample_rows: list[dict[str, Any]] = []
    missing_nm_ids: list[int] = []
    errors: list[str] = []
    positive_count = 0
    zero_count = 0
    total_stock = 0.0

    for nm_id, sku_comment in active_skus:
        row_id = f"SKU:{int(nm_id)}|{ONEC_FF_STOCK_QTY_METRIC_KEY}"
        row = row_by_id.get(row_id)
        if row is None or date_index >= len(row) or _is_blank(row[date_index]):
            missing_nm_ids.append(int(nm_id))
            continue
        try:
            quantity = float(row[date_index])
        except (TypeError, ValueError):
            errors.append(f"nmId {nm_id}: metric {ONEC_FF_STOCK_QTY_METRIC_KEY} is not numeric")
            continue
        if quantity < 0:
            errors.append(f"nmId {nm_id}: metric {ONEC_FF_STOCK_QTY_METRIC_KEY} is negative")
            continue
        normalized_quantity = round(quantity, 6)
        if normalized_quantity > 0:
            positive_count += 1
        else:
            zero_count += 1
        total_stock += normalized_quantity
        stock_row = FactoryOrderStockFfRow(
            nm_id=int(nm_id),
            sku_comment=str(sku_comment or ""),
            stock_ff=normalized_quantity,
            snapshot_date=snapshot_date,
            comment="source=1C FF_STOCK",
        )
        rows.append(stock_row)
        if len(sample_rows) < sample_size:
            sample_rows.append(
                {
                    "nm_id": stock_row.nm_id,
                    "sku_comment": stock_row.sku_comment,
                    "stock_ff": stock_row.stock_ff,
                    "snapshot_date": stock_row.snapshot_date,
                    "comment": stock_row.comment,
                }
            )

    covered_count = len(rows)
    missing_count = len(active_skus) - covered_count
    warnings: list[str] = []
    if missing_nm_ids:
        warnings.append(
            "1C FF_STOCK does not cover active SKU: "
            + ", ".join(str(item) for item in missing_nm_ids[:20])
            + ("..." if len(missing_nm_ids) > 20 else "")
        )
    if errors:
        status = "error"
    elif missing_count:
        status = "partial" if covered_count else "missing"
    else:
        status = "ready"

    return OnecStockFfResolveResult(
        rows=rows,
        state=_state(
            status=status,
            snapshot_date=snapshot_date,
            active_sku_count=len(active_skus),
            covered_sku_count=covered_count,
            positive_stock_sku_count=positive_count,
            zero_stock_sku_count=zero_count,
            missing_sku_count=missing_count,
            total_stock_ff=round(total_stock, 6),
            warnings=tuple(warnings),
            errors=tuple(errors),
            sample_rows=tuple(sample_rows),
        ),
    )


def _sheet(plan: SheetVitrinaV1Envelope, sheet_name: str):
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return sheet
    raise ValueError(f"sheet_vitrina_v1 ready snapshot missing {sheet_name} sheet")


def _select_latest_date_column(plan: SheetVitrinaV1Envelope, header: list[Any]) -> tuple[str, int]:
    header_values = [str(item or "").strip() for item in header]
    candidate_dates = [str(item or "").strip() for item in plan.date_columns if str(item or "").strip()]
    if candidate_dates:
        for candidate in reversed(candidate_dates):
            if candidate in header_values:
                return candidate, header_values.index(candidate)
    fallback_date = str(plan.as_of_date or "").strip()
    if fallback_date and fallback_date in header_values:
        return fallback_date, header_values.index(fallback_date)
    if len(header_values) > 2:
        return header_values[-1], len(header_values) - 1
    raise ValueError("DATA_VITRINA ready snapshot has no date columns for 1C FF_STOCK")


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _state(
    *,
    status: str,
    snapshot_date: str = "",
    active_sku_count: int = 0,
    covered_sku_count: int = 0,
    positive_stock_sku_count: int = 0,
    zero_stock_sku_count: int = 0,
    missing_sku_count: int = 0,
    total_stock_ff: float = 0.0,
    warnings: tuple[str, ...] = (),
    errors: tuple[str, ...] = (),
    sample_rows: tuple[dict[str, Any], ...] = (),
) -> FactoryOrderStockFfOnecState:
    return FactoryOrderStockFfOnecState(
        status=status,
        source=STOCK_FF_SOURCE_ONEC_FF_STOCK,
        source_label_ru=ONEC_FF_STOCK_SOURCE_LABEL_RU,
        snapshot_date=snapshot_date,
        active_sku_count=int(active_sku_count),
        covered_sku_count=int(covered_sku_count),
        positive_stock_sku_count=int(positive_stock_sku_count),
        zero_stock_sku_count=int(zero_stock_sku_count),
        missing_sku_count=int(missing_sku_count),
        total_stock_ff=float(total_stock_ff),
        warnings=tuple(warnings),
        errors=tuple(errors),
        sample_rows=tuple(sample_rows),
    )
