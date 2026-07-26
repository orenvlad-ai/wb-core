import json
from pathlib import Path
from tempfile import TemporaryDirectory

from canonical_cost_engine_vitrina_publication import (
    _value_for_metric,
    apply_publication,
    build_publication_report,
)
from packages.application.canonical_cost_engine import ensure_canonical_cost_schema
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)


def main() -> int:
    lookup = {
        101: {
            "stages": {
                "PRODUCTION": {"physical_quantity": 10, "paid_capital_rub": 100, "paid_equivalent_quantity": 10},
                "FF": {"physical_quantity": 2, "paid_capital_rub": 30, "paid_equivalent_quantity": 2},
                "FF_TO_WB": {"physical_quantity": 3, "paid_capital_rub": 45, "paid_equivalent_quantity": 3},
                "WB": {"physical_quantity": 4, "paid_capital_rub": 80, "paid_equivalent_quantity": 4, "recognized_capital_rub": 88},
            }
        }
    }
    assert _value_for_metric("onec_CHINA_TO_FF_qty", 101, lookup) == 10
    assert _value_for_metric("onec_CHINA_TO_FF_unit_cost_rub", 101, lookup) == 10
    assert _value_for_metric("onec_FF_STOCK_qty", 101, lookup) == 2
    assert _value_for_metric("onec_FF_TO_WB_cost_total_rub", 101, lookup) == 45
    assert _value_for_metric("onec_WB_STOCK_unit_cost_rub", 101, lookup) == 20
    assert _value_for_metric("our_wb_unit_cost_rub", 101, lookup) == 22
    with TemporaryDirectory() as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        with _connect(runtime.db_path) as conn:
            _ensure_schema(conn)
            ensure_canonical_cost_schema(conn)
            conn.execute("PRAGMA foreign_keys=OFF")
            plan = {
                "sheets": [
                    {
                        "header": ["label", "key", "2026-07-15"],
                        "rows": [["qty", "SKU:101|onec_CHINA_TO_FF_qty", 0]],
                    }
                ]
            }
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_ready_snapshots(
                    snapshot_id,bundle_version,activated_at,as_of_date,plan_version,
                    refreshed_at,plan_json
                ) VALUES(
                    'snapshot-1','bundle-1','2026-07-15T09:00:00Z','2026-07-15',
                    'plan-v1','2026-07-15T10:00:00Z',?
                )
                """,
                (json.dumps(plan),),
            )
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_canonical_cost_daily_state(
                    as_of_date,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                    recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                    confirmed_quantity,recognized_unit_cost_rub,paid_unit_cost_rub,
                    underaccepted_quantity,underaccepted_recognized_capital_rub,
                    underaccepted_paid_capital_rub,source_quality,diagnostics_json,
                    calculated_at,fingerprint
                ) VALUES(
                    '2026-07-15',101,'PRODUCTION','10','10','100','100','10','10',
                    '10','10','0','0','0','primary_documents','{}','2026-07-15T10:00:00Z','fp-1'
                )
                """
            )
            conn.commit()
        first = build_publication_report(
            runtime.db_path, date_from="2026-07-01", date_to="2026-07-15"
        )
        second = build_publication_report(
            runtime.db_path, date_from="2026-07-01", date_to="2026-07-15"
        )
        assert first == second
        assert first["changed_cells"] == 1
        applied = apply_publication(
            runtime.db_path,
            date_from="2026-07-01",
            date_to="2026-07-15",
            fingerprint=first["fingerprint"],
            backup_dir=Path(tmp) / "backups",
        )
        assert applied["post_run"]["changed_cells"] == 0
        assert applied["recovery_policy"]["tier"] == "T1"
        assert applied["recovery_policy"]["lifecycle"] == "retained"
        assert applied["backup"]["full_database_copy"] is False
        assert applied["backup"]["copy_bytes"] == 0
        no_op = build_publication_report(
            runtime.db_path, date_from="2026-07-01", date_to="2026-07-15"
        )
        assert no_op["changed_cells"] == 0
    print("canonical_cost_engine_vitrina_publication_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
