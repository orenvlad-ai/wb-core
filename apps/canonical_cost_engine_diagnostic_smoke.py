"""Non-fail-fast/fixpoint smoke for the canonical diagnostic collector."""

from __future__ import annotations

from argparse import Namespace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.canonical_cost_engine_diagnostic import run  # noqa: E402
from apps.canonical_cost_engine_smoke import (  # noqa: E402
    _insert_fallback_production,
    _insert_ff_balance,
    _insert_primary,
    _insert_snapshot,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _connect,
    _ensure_schema,
)


def main() -> int:
    _clean_pipeline_is_reached()
    _multiple_independent_blockers_are_collected()
    print("canonical_cost_engine_diagnostic_smoke: ok")
    return 0


def _clean_pipeline_is_reached() -> None:
    with TemporaryDirectory() as tmp:
        runtime = _runtime(Path(tmp), include_fallback=True)
        report = run(_args(runtime))
        if report["status"] != "ok" or not report["fixpoint"]["reached"]:
            raise AssertionError("clean diagnostic must reach a zero-blocker fixpoint")
        if report["rebuild"] is None or report["reconciliation"] is None:
            raise AssertionError("collector must execute the actual candidate pipeline")
        if any(item["status"] == "NOT_REACHED" for item in report["coverage_matrix"]):
            raise AssertionError("coverage matrix contains unexplained NOT_REACHED")
        if not all(item["status"] == "PASS" for item in report["coverage_matrix"]):
            raise AssertionError("clean pipeline coverage is not fully PASS")


def _multiple_independent_blockers_are_collected() -> None:
    with TemporaryDirectory() as tmp:
        runtime = _runtime(Path(tmp), include_fallback=False)
        with _connect(runtime.db_path) as conn:
            for index in (1, 2):
                supply_id = f"surplus-supply-{index}"
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_wb_supplies(
                        supply_id,cache_key,normalized_row_json,raw_goods_json,
                        warehouse_id,status_id,quantity_for_size_filter,fact_date,synced_at
                    ) VALUES(?,?,?,?,?,5,1,'2026-07-04','2026-07-04T00:00:00Z')
                    """,
                    (
                        supply_id,
                        f"supply:{supply_id}",
                        json.dumps(
                            {
                                "supply_id": supply_id,
                                "status_id": 5,
                                "fact_date": "2026-07-04",
                                "warehouse_name": "W",
                                "destination_name": "D",
                            }
                        ),
                        json.dumps([{"nmID": 111, "acceptedQuantity": 1}]),
                        "W",
                    ),
                )
                conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_ff_stock_operations(
                            operation_id,operation_type,source_type,source_key,
                            source_object_id,source_object_label,created_at,created_by,
                            sku_count,total_quantity_delta,total_quantity_abs,
                            warnings_json,diagnostics_json
                        ) VALUES(?, 'auto_writeoff','wb_supply',?,?,?,
                                 '2026-07-04T00:00:00Z','fixture',0,0,0,'[]','{}')
                        """,
                        (
                            f"surplus-operation-{index}",
                            f"wb_supply_debit:supply:{supply_id}",
                            supply_id,
                            supply_id,
                        ),
                    )
            conn.commit()
        report = run(_args(runtime))
        primary = [
            item for item in report["blocker_registry"]
            if item["kind"] == "primary"
        ]
        surplus_blockers = [
            item for item in primary
            if item["code"] == "accepted_quantity_exceeds_sent"
        ]
        if len(surplus_blockers) != 2:
            raise AssertionError("collector stopped before visiting every source entity")
        if not any(item["code"] == "baseline_cost_coverage_incomplete" for item in primary):
            raise AssertionError("independent baseline blocker stayed hidden behind source blockers")
        if not report["fixpoint"]["reached"]:
            raise AssertionError("repeated diagnostic pass did not reach fixpoint")
        if report["fixpoint"]["new_blockers_on_last_pass"] != 0:
            raise AssertionError("last diagnostic pass added an unexpected blocker")
        if any(item["status"] == "NOT_REACHED" for item in report["coverage_matrix"]):
            raise AssertionError("blocked coverage contains unexplained NOT_REACHED")
        if report["preservation"]["production_mutation"] is not False:
            raise AssertionError("diagnostic collector may not mutate production")


def _runtime(root: Path, *, include_fallback: bool) -> RegistryUploadDbBackedRuntime:
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    with _connect(runtime.db_path) as conn:
        _ensure_schema(conn)
        _insert_primary(conn)
        _insert_fallback_production(conn, nm_id=222)
        _insert_ff_balance(conn, nm_id=111, quantity=6750)
        if include_fallback:
            _insert_snapshot(
                conn,
                "2026-05-16",
                {222: {"onec_FF_STOCK_unit_cost_rub": 80}},
            )
        _insert_snapshot(
            conn,
            "2026-07-01",
            {111: {"stock_total": 93250}, 222: {"stock_total": 0}},
        )
        conn.commit()
    return runtime


def _args(runtime: RegistryUploadDbBackedRuntime) -> Namespace:
    return Namespace(runtime_dir=str(runtime.runtime_dir), date_to="2026-07-01")


if __name__ == "__main__":
    raise SystemExit(main())
