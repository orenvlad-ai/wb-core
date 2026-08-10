"""Smoke-check immutable Proxy V4 history across an ordinary full refresh."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.proxy_v4_historical_projection import (  # noqa: E402
    PROXY_V4_PROJECTION_METADATA_KEY,
    PROXY_V4_RECONCILIATION_METADATA_KEY,
    preserve_proxy_v4_historical_cells,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _with_full_refresh_metadata,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


DATES = ["2026-07-31", "2026-08-01", "2026-08-09", "2026-08-10"]


def main() -> None:
    previous = _plan(
        profit=["", 100, 200, 300],
        margin=["", 0.10, 0.20, 0.30],
        order_sum=[900, 1000, 1100, 1200],
        include_second_sku=False,
        marker=True,
    )
    recalculated = _plan(
        profit=[999, 101, 201, 400],
        margin=[0.99, 0.11, 0.21, 0.40],
        order_sum=[901, 1001, 1101, 1201],
        include_second_sku=True,
        marker=False,
    )
    preserved = _with_full_refresh_metadata(
        recalculated,
        refreshed_at="2026-08-10T06:00:00Z",
        previous_plan=previous,
        previous_refreshed_at="2026-08-10T05:00:00Z",
        business_date="2026-08-10",
    )
    rows = _rows(preserved)
    if rows["TOTAL|total_proxy_profit_4_rub"][2:] != ["", 100, 200, 400]:
        raise AssertionError("ordinary refresh rewrote frozen TOTAL V4 profit history")
    if rows["TOTAL|proxy_margin_4_pct_total"][2:] != ["", 0.10, 0.20, 0.40]:
        raise AssertionError("ordinary refresh rewrote frozen TOTAL V4 margin history")
    if rows["SKU:1|proxy_profit_4_rub"][2:] != ["", 100, 200, 400]:
        raise AssertionError("ordinary refresh rewrote frozen SKU V4 history")
    if rows["SKU:2|proxy_profit_4_rub"][2:] != ["", "", "", 400]:
        raise AssertionError("missing historical V4 cells were retroactively invented")
    if rows["SKU:1|orderSum"][2:] != [901, 1001, 1101, 1201]:
        raise AssertionError("V4 preservation changed a non-V4 source row")

    metadata = dict(preserved.metadata or {})
    marker = dict(metadata.get(PROXY_V4_PROJECTION_METADATA_KEY) or {})
    if sorted((marker.get("eligibility_by_date") or {}).keys()) != [
        "2026-08-01",
        "2026-08-09",
    ]:
        raise AssertionError(f"historical initialization marker was not bounded: {marker}")
    reconciliation = dict(
        metadata.get(PROXY_V4_RECONCILIATION_METADATA_KEY) or {}
    )
    if reconciliation.get("approval_reference") != "owner-repair-gate-test":
        raise AssertionError("ordinary refresh lost the guarded repair provenance")
    summary = dict(metadata.get("proxy_v4_history_preservation") or {})
    if (
        summary.get("business_date") != "2026-08-10"
        or summary.get("preserved_dates") != DATES[:-1]
        or int(summary.get("preserved_cell_count") or 0) != 18
    ):
        raise AssertionError(f"V4 preservation evidence is incomplete: {summary}")

    repeated, repeated_summary = preserve_proxy_v4_historical_cells(
        preserved,
        previous_plan=previous,
        business_date="2026-08-10",
    )
    if _target_rows(repeated) != _target_rows(preserved):
        raise AssertionError("V4 ordinary-refresh preservation is not idempotent")
    if int(repeated_summary.get("changed_cell_count", -1)) != 0:
        raise AssertionError(f"idempotent preservation reported changes: {repeated_summary}")

    print("proxy_v4_refresh_historical_cells_frozen: ok")
    print("proxy_v4_refresh_missing_stays_blank_current_day_updates: ok")
    print("proxy_v4_refresh_preservation_metadata_idempotent: ok")


def _plan(
    *,
    profit: list[object],
    margin: list[object],
    order_sum: list[object],
    include_second_sku: bool,
    marker: bool,
) -> SheetVitrinaV1Envelope:
    rows: list[list[object]] = [
        ["Order sum", "SKU:1|orderSum", *order_sum],
        ["Proxy profit 4", "TOTAL|total_proxy_profit_4_rub", *profit],
        ["Proxy margin 4", "TOTAL|proxy_margin_4_pct_total", *margin],
        ["SKU 1 profit", "SKU:1|proxy_profit_4_rub", *profit],
        ["SKU 1 margin", "SKU:1|proxy_margin_4_pct", *margin],
    ]
    if include_second_sku:
        rows.extend(
            [
                ["SKU 2 profit", "SKU:2|proxy_profit_4_rub", *profit],
                ["SKU 2 margin", "SKU:2|proxy_margin_4_pct", *margin],
            ]
        )
    metadata = {}
    if marker:
        metadata[PROXY_V4_PROJECTION_METADATA_KEY] = {
            "contract_version": "proxy_v4_historical_projection_v1",
            "materialized_at": "2026-08-10T05:44:45Z",
            "date_from": "2026-08-01",
            "date_to": "2026-08-10",
            "eligibility_by_date": {
                "2026-08-01": {"version_id": "v1"},
                "2026-08-09": {"version_id": "v2"},
                "2026-08-10": {"version_id": "v3"},
            },
        }
        metadata[PROXY_V4_RECONCILIATION_METADATA_KEY] = {
            "contract_version": "proxy_v4_historical_reconciliation_v1",
            "approval_reference": "owner-repair-gate-test",
        }
    return SheetVitrinaV1Envelope(
        plan_version="proxy_v4_refresh_preservation_smoke",
        snapshot_id="proxy-v4-refresh-preservation",
        as_of_date="2026-08-09",
        date_columns=list(DATES),
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key=day,
                slot_label=day,
                column_date=day,
            )
            for day in DATES
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:F{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                row_count=len(rows),
                column_count=6,
                header=["label", "key", *DATES],
                rows=rows,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:A1",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                row_count=1,
                column_count=1,
                header=["status"],
                rows=[["ok"]],
            ),
        ],
        metadata=metadata,
    )


def _rows(plan: SheetVitrinaV1Envelope) -> dict[str, list[object]]:
    sheet = next(item for item in plan.sheets if item.sheet_name == "DATA_VITRINA")
    return {str(row[1]): list(row) for row in sheet.rows}


def _target_rows(plan: SheetVitrinaV1Envelope) -> dict[str, list[object]]:
    return {
        key: row
        for key, row in _rows(plan).items()
        if key.endswith(
            (
                "|proxy_profit_4_rub",
                "|proxy_margin_4_pct",
                "|total_proxy_profit_4_rub",
                "|proxy_margin_4_pct_total",
            )
        )
    }


if __name__ == "__main__":
    main()
