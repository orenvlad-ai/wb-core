"""Determinism and relevance smoke for the read-only drift localizer."""

from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_shipment_non_target_drift_diagnostic import build_report


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        before = root / "before.sqlite3"
        after = root / "after.sqlite3"
        with sqlite3.connect(before) as conn:
            conn.executescript(
                """
                CREATE TABLE sheet_vitrina_v1_ready_snapshots(
                    snapshot_id TEXT PRIMARY KEY,as_of_date TEXT,refreshed_at TEXT,plan_json TEXT
                );
                CREATE TABLE sheet_vitrina_v1_supplier_shipment_lines(
                    line_id TEXT PRIMARY KEY,shipment_id TEXT,internal_nm_id INTEGER,qty REAL
                );
                INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(
                    'snapshot-1','2026-07-15','2026-07-15T10:00:00Z','{"value":1}'
                );
                INSERT INTO sheet_vitrina_v1_supplier_shipment_lines VALUES(
                    'line-1','target-shipment',391662410,6000
                );
                """
            )
        shutil.copy2(before, after)
        with sqlite3.connect(after) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='2026-07-15T11:00:00Z'"
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_shipment_lines SET qty=6001 WHERE line_id='line-1'"
            )
            conn.commit()
        first = build_report(
            before,
            after,
            target_shipment_ids=["target-shipment"],
            target_nm_ids=[391662410],
        )
        second = build_report(
            before,
            after,
            target_shipment_ids=["target-shipment"],
            target_nm_ids=[391662410],
        )
        assert first == second
        assert first["change_count"] == 2
        by_table = {item["table"]: item for item in first["changes"]}
        assert by_table["sheet_vitrina_v1_ready_snapshots"]["classification"] == "unrelated_live_activity"
        assert by_table["sheet_vitrina_v1_supplier_shipment_lines"]["classification"] == "relevant_dependency"
        assert first["fingerprint"].startswith("sha256:")
    print("supplier_shipment_non_target_drift_diagnostic_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
