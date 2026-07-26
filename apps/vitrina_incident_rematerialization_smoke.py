"""Fixture-backed bounded rematerialization smoke; never touches production."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_incident_stocks import (
    incident_stock_metric_key,
    incident_stock_total_metric_key,
)
from packages.application.vitrina_incident_rematerialization import (
    apply_vitrina_incident_rematerialization,
    plan_vitrina_incident_rematerialization,
)
from packages.application.wb_incident_policy import save_policy_revision
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)
from packages.contracts.stocks_block import (
    StocksItem,
    StocksSuccess,
    StocksWarehouseRow,
)


BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
TARGET_DATE = "2026-07-25"


def _plan(nm_ids: list[int]) -> SheetVitrinaV1Envelope:
    rows: list[list[object]] = []
    for region in ("total", "central"):
        for variant in ("fact", "incident", "effective"):
            sku_metric = incident_stock_metric_key(variant, region)
            total_metric = incident_stock_total_metric_key(variant, region)
            rows.append([f"Итого {total_metric}", f"TOTAL|{total_metric}", ""])
            for nm_id in nm_ids:
                rows.append(
                    [f"SKU {nm_id} {sku_metric}", f"SKU:{nm_id}|{sku_metric}", ""]
                )
    rows.append(["Контроль капитала", f"SKU:{nm_ids[0]}|own_total_capital_rub", 999.0])
    return SheetVitrinaV1Envelope(
        plan_version="fixture-v1",
        snapshot_id="incident-rematerialization-fixture",
        as_of_date=TARGET_DATE,
        date_columns=[TARGET_DATE],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="yesterday_closed",
                column_date=TARGET_DATE,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:C20",
                clear_range="A1:C20",
                write_mode="replace",
                partial_update_allowed=False,
                header=["label", "key", TARGET_DATE],
                rows=rows,
                row_count=len(rows),
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:C2",
                clear_range="A1:C2",
                write_mode="replace",
                partial_update_allowed=False,
                header=["source", "status", "message"],
                rows=[["stocks", "ready", "fixture"]],
                row_count=1,
                column_count=3,
            ),
        ],
        metadata={
            "server_cell_presentation": {},
            "unrelated_control": {"capital_digest": "fixture-unchanged"},
        },
    )


def _value_map(snapshot: SheetVitrinaV1Envelope) -> dict[str, object]:
    sheet = next(item for item in snapshot.sheets if item.sheet_name == "DATA_VITRINA")
    return {str(row[1]): row[2] for row in sheet.rows}


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="vitrina-incident-rematerialization-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-07-25T08:00:00Z")
        assert accepted.status == "accepted"
        current = runtime.load_current_state()
        nm_ids = [int(item.nm_id) for item in current.config_v2 if item.enabled][:2]
        assert len(nm_ids) == 2
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current,
            refreshed_at="2026-07-25T08:05:00Z",
            plan=_plan(nm_ids),
        )
        runtime.save_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=TARGET_DATE,
            captured_at="2026-07-25T08:03:00Z",
            payload=StocksSuccess(
                kind="success",
                snapshot_date=TARGET_DATE,
                count=2,
                items=[
                    StocksItem(
                        nm_id=nm_ids[0],
                        stock_total=15,
                        stock_ru_central=15,
                        stock_ru_northwest=0,
                        stock_ru_volga=0,
                        stock_ru_ural=0,
                        stock_ru_south_caucasus=0,
                        stock_ru_far_siberia=0,
                    ),
                    StocksItem(
                        nm_id=nm_ids[1],
                        stock_total=7,
                        stock_ru_central=7,
                        stock_ru_northwest=0,
                        stock_ru_volga=0,
                        stock_ru_ural=0,
                        stock_ru_south_caucasus=0,
                        stock_ru_far_siberia=0,
                    ),
                ],
                warehouse_rows=[
                    StocksWarehouseRow(
                        nm_id=nm_ids[0],
                        warehouse_id=None,
                        warehouse_name="Альфа",
                        region_name="Центральный",
                        quantity=10,
                        planning_zone_key="central_north",
                        classification_status="mapped",
                        classification_source="fixture",
                    ),
                    StocksWarehouseRow(
                        nm_id=nm_ids[0],
                        warehouse_id=None,
                        warehouse_name="Бета",
                        region_name="Центральный",
                        quantity=5,
                        planning_zone_key="central_east",
                        classification_status="mapped",
                        classification_source="fixture",
                    ),
                    StocksWarehouseRow(
                        nm_id=nm_ids[1],
                        warehouse_id=None,
                        warehouse_name="Бета",
                        region_name="Центральный",
                        quantity=7,
                        planning_zone_key="central_east",
                        classification_status="mapped",
                        classification_source="fixture",
                    ),
                ],
                pagination_complete=False,
                raw_rows_digest="",
            ),
        )
        save_policy_revision(
            runtime,
            payload={
                "base_revision": 0,
                "active": True,
                "excluded_wb_warehouse_ids": [101],
                "reason": "fixture incident",
                "effective_from": TARGET_DATE,
                "effective_to": "",
                "status": "active",
            },
            actor="fixture",
            warehouse_options=[
                {"warehouse_id": 101, "warehouse_name": "Альфа"},
                {"warehouse_id": 102, "warehouse_name": "Бета"},
            ],
            timestamp="2026-07-25T08:10:00Z",
        )
        stock_before = json.dumps(
            runtime.load_temporal_source_snapshot(
            source_key="stocks", snapshot_date=TARGET_DATE
            )[0].__dict__,
            ensure_ascii=False,
            sort_keys=True,
            default=lambda value: value.__dict__,
        )
        before = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=TARGET_DATE)
        before_values = _value_map(before)

        reviewed, _ = plan_vitrina_incident_rematerialization(
            runtime,
            date_from=TARGET_DATE,
            date_to=TARGET_DATE,
            generated_at="2026-07-25T08:15:00Z",
        )
        assert reviewed["changed_snapshot_count"] == 1
        assert reviewed["changed_cells"] > 0
        assert reviewed["non_target_invariant"] == "unchanged"
        assert _value_map(
            runtime.load_sheet_vitrina_ready_snapshot(as_of_date=TARGET_DATE)
        ) == before_values

        result = apply_vitrina_incident_rematerialization(
            runtime,
            reviewed_plan=reviewed,
            fingerprint=str(reviewed["fingerprint"]),
            approval_reference="fixture-human-gate",
            actor="fixture",
            applied_at="2026-07-25T08:20:00Z",
        )
        assert result["status"] == "applied"
        assert result["readback_changed_cells"] == 0
        after = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=TARGET_DATE)
        values = _value_map(after)
        first = nm_ids[0]
        second = nm_ids[1]
        assert values[f"SKU:{first}|wb_stock_fact_qty"] == 15
        assert values[f"SKU:{first}|wb_stock_incident_qty"] == 10
        assert values[f"SKU:{first}|wb_stock_effective_qty"] == 5
        assert values[f"SKU:{second}|wb_stock_fact_qty"] == ""
        assert values[f"SKU:{second}|wb_stock_incident_qty"] == ""
        assert values[f"SKU:{second}|wb_stock_effective_qty"] == ""
        assert values["TOTAL|total_wb_stock_fact_qty"] == 15
        assert values["TOTAL|total_wb_stock_incident_qty"] == 10
        assert values["TOTAL|total_wb_stock_effective_qty"] == 5
        assert (
            values["TOTAL|total_wb_stock_effective_qty"]
            == values["TOTAL|total_wb_stock_fact_qty"]
            - values["TOTAL|total_wb_stock_incident_qty"]
        )
        assert values[f"SKU:{first}|own_total_capital_rub"] == 999.0
        quality = after.metadata["incident_projection_quality_by_date"][TARGET_DATE]
        assert quality["state"] == "provisional_received_rows"
        assert quality["pagination_complete"] is False
        presentation = after.metadata["server_cell_presentation"]
        adjusted = presentation[f"SKU:{first}|wb_stock_incident_qty"][TARGET_DATE]
        assert adjusted["state"] == "incident_adjusted"
        assert adjusted["quality_state"] == "provisional_received_rows"
        unavailable = presentation[f"SKU:{second}|wb_stock_fact_qty"][TARGET_DATE]
        assert unavailable["state"] == "unavailable"
        assert unavailable["quality_state"] == "provisional_received_rows"
        assert "полнота WB не подтверждена" in unavailable["quality_reason"]
        stock_after = json.dumps(
            runtime.load_temporal_source_snapshot(
                source_key="stocks", snapshot_date=TARGET_DATE
            )[0].__dict__,
            ensure_ascii=False,
            sort_keys=True,
            default=lambda value: value.__dict__,
        )
        assert stock_after == stock_before
        with sqlite3.connect(runtime.db_path) as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_incident_rematerialization_audit"
            ).fetchone()[0]
        assert audit_count == 1

        repeat, _ = plan_vitrina_incident_rematerialization(
            runtime,
            date_from=TARGET_DATE,
            date_to=TARGET_DATE,
            generated_at="2026-07-25T09:15:00Z",
        )
        assert repeat["changed_cells"] == 0
        assert repeat["changed_snapshot_count"] == 0
        assert (
            repeat["snapshots"][0]["before_plan_digest"]
            == repeat["snapshots"][0]["after_plan_digest"]
        )

    print("vitrina_incident_rematerialization_smoke: OK")


if __name__ == "__main__":
    main()
