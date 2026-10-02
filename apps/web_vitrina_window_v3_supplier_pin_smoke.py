"""Current functional supplier certification stays on the V3 pinned snapshot."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_supplier_cost_state_replay_smoke import _seed
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application import sheet_vitrina_v1_web_vitrina as vitrina
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.warehouse_business_projection import ensure_functional_version_business_time_schema
from packages.application import warehouse_functional
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.application.web_vitrina_window_v3 import (
    _current_supplier_certification_material, _table_names,
)


DAY = "2026-10-02"


def _material(runtime: RegistryUploadDbBackedRuntime) -> object:
    with window_read_context(runtime.db_path) as context:
        conn = context.borrow(runtime.db_path)
        return _current_supplier_certification_material(
            conn, _table_names(conn), business_date=DAY, dates=[DAY],
        )


def main() -> None:
    with TemporaryDirectory(prefix="window-supplier-pin-") as folder:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(folder))
        _seed(runtime)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_functional_version_business_time_schema(conn)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_functional_cutovers("
                "cutover_id,cutover_at,status,plan_fingerprint,source_watermarks_json,"
                "absorbed_supply_revisions_json,backup_json,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                ("warehouse_functional_cutover_v1", DAY + "T06:00:00Z", "posted",
                 "sha256:cutover-fixture", "{}", "{}", "{}", DAY + "T06:00:00Z",
                 DAY + "T06:00:00Z"),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_functional_versions "
                "SET business_effective_date=?,published_at=? WHERE version_id='whfv_smoke'",
                (DAY, DAY + "T06:01:00Z"),
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots("
                "snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,"
                "pagination_complete,page_count,page_offsets_json,raw_row_count,"
                "raw_rows_digest,raw_rows_json,items_json,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("wb-window-fixture", "whfv_smoke", DAY + "T06:00:00Z", DAY,
                 "[391660889]", 1, 1, "[0]", 0, "sha256:empty", "[]", "[]",
                DAY + "T06:00:00Z"),
            )
        frozen_now = datetime(2026, 10, 2, 10, tzinfo=timezone.utc)
        with window_read_context(runtime.db_path):
            with patch.object(vitrina, "OwnProductCapitalBlock") as constructor:
                constructor.return_value.functional_warehouse_cutover_date.return_value = ""
                vitrina._read_time_warehouse_cell_presentation(
                    runtime=runtime, now=frozen_now,
                    snapshot=SimpleNamespace(metadata={}, date_columns=[DAY]),
                    enabled_config=[], displayed_metrics=[],
                )
                assert constructor.call_args.kwargs["timestamp_factory"]() == frozen_now.isoformat()
        original = _material(runtime)
        assert original[0] == "active_supplier_shipments", original
        capital = OwnProductCapitalBlock.__new__(OwnProductCapitalBlock)
        capital.runtime = runtime
        capital.timestamp_factory = lambda: DAY + "T10:00:00Z"
        with window_read_context(runtime.db_path) as context:
            conn = context.borrow(runtime.db_path)
            pinned = _current_supplier_certification_material(
                conn, _table_names(conn), business_date=DAY, dates=[DAY],
            )
            with patch.object(warehouse_functional, "_connect_readonly",
                              side_effect=AssertionError("unpinned supplier connection")):
                before = capital._load_functional_daily_metric_lookup(
                    DAY, requested_nm_ids=[391660889], revalidate_current_sources=True,
                )
                proof_before = warehouse_functional.load_supplier_line_cost_breakdown(
                    runtime=runtime, shipment_id="26GN390",
                )
            assert before and 391660889 in before
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute(
                    "UPDATE sheet_vitrina_v1_cny_ledger_operations "
                    "SET rub_value_delta='110' WHERE operation_id='payment-390'"
                )
            assert _current_supplier_certification_material(
                conn, _table_names(conn), business_date=DAY, dates=[DAY],
            ) == pinned
            with patch.object(warehouse_functional, "_connect_readonly",
                              side_effect=AssertionError("unpinned supplier connection")):
                pinned_after_write = capital._load_functional_daily_metric_lookup(
                    DAY, requested_nm_ids=[391660889], revalidate_current_sources=True,
                )
                proof_pinned = warehouse_functional.load_supplier_line_cost_breakdown(
                    runtime=runtime, shipment_id="26GN390",
                )
            assert pinned_after_write == before
            assert proof_pinned == proof_before
        assert _material(runtime) != original
        proof_after = warehouse_functional.load_supplier_line_cost_breakdown(
            runtime=runtime, shipment_id="26GN390",
        )
        assert proof_after["average_unit_cost_rub"] != proof_before["average_unit_cost_rub"]
    print("window_v3_supplier_pin: ok")


if __name__ == "__main__":
    main()
