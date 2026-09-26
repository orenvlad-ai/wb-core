"""Compact factory-order XLSX shared by the FBS and legacy planners."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from packages.application.simple_xlsx import build_single_sheet_workbook_bytes


_HEADERS = ["nmId", "SKU description", "Barcode", "Recommended order quantity"]
_UNRESOLVED_STATUSES = {"missing", "multiple", "sync_error", "token_missing"}


def build_factory_order_recommendation(
    *,
    rows: Iterable[tuple[int, str, int]],
    total_quantity: int,
    estimated_weight: float,
    estimated_volume: float,
    nomenclature_items: Iterable[Mapping[str, Any]],
) -> bytes:
    """Keep planning rows intact and leave unresolved barcodes visibly blank."""
    barcodes = _unambiguous_barcodes(nomenclature_items)
    sheet_rows: list[list[Any]] = [_HEADERS]
    for nm_id, sku_comment, quantity in rows:
        sheet_rows.append(
            [str(nm_id), str(sku_comment), barcodes.get(int(nm_id), ""), int(quantity)]
        )
    sheet_rows.extend(
        [
            [],
            ["Total quantity", "", "", int(total_quantity)],
            ["Estimated weight, kg", "", "", float(estimated_weight)],
            ["Estimated volume, m³", "", "", float(estimated_volume)],
        ]
    )
    widths = [
        min(255, max(12, max(len(str(row[index])) for row in sheet_rows if len(row) > index) + 3))
        for index in range(4)
    ]
    return build_single_sheet_workbook_bytes(
        "Recommendation",
        sheet_rows,
        column_widths=widths,
        text_columns={1, 3},
    )


def _unambiguous_barcodes(items: Iterable[Mapping[str, Any]]) -> dict[int, str]:
    candidates: dict[int, list[str | None]] = defaultdict(list)
    for item in items:
        if item.get("is_active") is False:
            continue
        try:
            nm_id = int(item.get("nm_id") or 0)
        except (TypeError, ValueError):
            continue
        if nm_id <= 0:
            continue
        status = str(item.get("barcode_status") or "").strip().lower()
        primary = str(item.get("barcode") or "").strip()
        if status in _UNRESOLVED_STATUSES:
            candidates[nm_id].append(None)
            continue
        if status == "manual":
            candidates[nm_id].append(primary or None)
            continue
        values = {primary} if primary else set()
        listed = item.get("barcodes")
        if isinstance(listed, (list, tuple)):
            values.update(str(value).strip() for value in listed if str(value or "").strip())
        candidates[nm_id].append(next(iter(values)) if len(values) == 1 else None)
    return {
        nm_id: values[0]
        for nm_id, values in candidates.items()
        if len(values) == 1 and values[0] is not None
    }
