"""Targeted smoke-check for the phase-1 web_vitrina_contract v1 builder."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-contract-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 2:
            raise AssertionError("fixture must expose at least two enabled SKU rows")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-20T09:05:00Z",
            plan=_build_plan(
                current_state=current_state,
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
                first_group=enabled[0].group,
            ),
        )

        payload = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )

        if payload.contract_name != "web_vitrina_contract" or payload.contract_version != "v1":
            raise AssertionError(f"contract identity mismatch, got {payload}")
        if payload.page_route != "/sheet-vitrina-v1/vitrina" or payload.read_route != "/v1/sheet-vitrina-v1/web-vitrina":
            raise AssertionError(f"route fixation mismatch, got {payload}")

        if payload.meta.snapshot_id != "web-vitrina-v1-fixture" or payload.meta.row_count != 4:
            raise AssertionError(f"meta mismatch, got {payload.meta}")
        if payload.meta.date_columns != ["2026-04-19", "2026-04-20"]:
            raise AssertionError(f"meta date columns mismatch, got {payload.meta}")
        if [slot.slot_key for slot in payload.meta.temporal_slots] != ["yesterday_closed", "today_current"]:
            raise AssertionError(f"meta temporal slots mismatch, got {payload.meta}")

        if payload.status_summary.refresh_status != "warning":
            raise AssertionError(f"status_summary.refresh_status mismatch, got {payload.status_summary}")
        if payload.status_summary.refresh_status_label != "Внимание":
            raise AssertionError(f"status_summary.refresh_status_label mismatch, got {payload.status_summary}")
        if payload.status_summary.refresh_status_tone != "warning":
            raise AssertionError(f"status_summary.refresh_status_tone mismatch, got {payload.status_summary}")
        if "требуют внимания" not in payload.status_summary.refresh_status_reason:
            raise AssertionError(f"status_summary.refresh_status_reason mismatch, got {payload.status_summary}")
        if payload.status_summary.read_model != "persisted_ready_snapshot":
            raise AssertionError(f"status_summary.read_model mismatch, got {payload.status_summary}")
        if payload.status_summary.source_policy_counts != {
            "dual_day_capable": 1,
            "accepted_current_rollover": 1,
            "manual_overlay": 1,
        }:
            raise AssertionError(f"status_summary.source_policy_counts mismatch, got {payload.status_summary}")
        if payload.status_summary.refresh_outcome_counts != {
            "success": 1,
            "warning": 2,
            "error": 0,
        }:
            raise AssertionError(f"status_summary.refresh_outcome_counts mismatch, got {payload.status_summary}")

        schema_columns = {column.column_id: column for column in payload.schema.columns}
        for required_column in ("scope_kind", "scope_label", "metric_key", "section", "date:2026-04-19", "date:2026-04-20"):
            if required_column not in schema_columns:
                raise AssertionError(f"missing schema column {required_column!r}")
        if schema_columns["date:2026-04-20"].temporal_slot_key != "today_current":
            raise AssertionError(f"temporal slot mapping mismatch, got {schema_columns['date:2026-04-20']}")

        total_row = next((row for row in payload.rows if row.row_id == "TOTAL|total_view_count"), None)
        group_row = next((row for row in payload.rows if row.scope_kind == "GROUP"), None)
        first_sku_row = next((row for row in payload.rows if row.row_id == f"SKU:{enabled[0].nm_id}|view_count"), None)
        second_sku_row = next((row for row in payload.rows if row.row_id == f"SKU:{enabled[1].nm_id}|orderSum"), None)
        if total_row is None or group_row is None or first_sku_row is None or second_sku_row is None:
            raise AssertionError(f"normalized rows missing expected scope variants, got {payload.rows}")
        if total_row.scope_label != "ИТОГО" or total_row.metric_label != "Показы в воронке":
            raise AssertionError(f"TOTAL normalization mismatch, got {total_row}")
        if group_row.group != enabled[0].group or group_row.scope_label != enabled[0].group:
            raise AssertionError(f"GROUP normalization mismatch, got {group_row}")
        if first_sku_row.nm_id != enabled[0].nm_id or first_sku_row.scope_label != enabled[0].display_name:
            raise AssertionError(f"SKU normalization mismatch, got {first_sku_row}")
        if second_sku_row.values_by_date != {"2026-04-19": 5, "2026-04-20": 7}:
            raise AssertionError(f"values_by_date mismatch, got {second_sku_row}")

        if payload.capabilities.exportable or not payload.capabilities.grid_library_agnostic:
            raise AssertionError(f"capabilities mismatch, got {payload.capabilities}")

        print("web_vitrina_contract_identity: ok ->", payload.contract_name, payload.contract_version)
        print("web_vitrina_routes: ok ->", payload.page_route, payload.read_route)
        print("web_vitrina_meta: ok ->", payload.meta.snapshot_id, payload.meta.row_count)
        print("web_vitrina_schema: ok ->", len(payload.schema.columns), "columns")
        print("web_vitrina_rows: ok ->", total_row.row_id, first_sku_row.row_id, second_sku_row.row_id)
        print("web_vitrina_capabilities: ok -> grid-library-agnostic read-only contract")


def _build_plan(
    *,
    current_state: object,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-v1-fixture",
        as_of_date="2026-04-19",
        date_columns=["2026-04-19", "2026-04-20"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-04-19",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-04-20",
            ),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "prices_snapshot": "accepted_current_rollover",
            "cost_price": "manual_overlay",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-19", "2026-04-20"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100, 140],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40, 55],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 20, 30],
                    [f"SKU B: Заказы, шт.", f"SKU:{second_nm_id}|orderSum", 5, 7],
                ],
                row_count=4,
                column_count=4,
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
                        "seller_funnel_snapshot",
                        "success",
                        "fresh",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        2,
                        2,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


if __name__ == "__main__":
    main()
