"""Targeted invariants for the July warehouse recovery runners."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    own_stage_metric_key,
)
from packages.application.warehouse_early_wb_recovery import (  # noqa: E402
    WarehouseEarlyWbRecoveryError,
    _partial_projection_item,
    _before_images as _early_before_images,
    apply_early_wb_recovery_plan,
)
from packages.application.warehouse_business_projection import (  # noqa: E402
    CURRENT_ROW_TABLE,
    REVISION_TABLE,
    ROW_TABLE,
    STATE_TABLE,
    _persist_projection_revision,
    ensure_warehouse_business_projection_schema,
)
from packages.application.warehouse_historical_recovery import (  # noqa: E402
    DATES,
    WarehouseHistoricalRecoveryError,
    _correct_balances,
    _ready_updates,
    apply_historical_recovery_plan,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryState,
    WarehouseRecoveryRegistry,
)


def main() -> None:
    _check_business_time_replay()
    _check_ready_target_boundary()
    _check_early_partial_unavailable()
    _check_stale_fingerprints()
    _check_early_exact_rollback()
    print("warehouse_historical_recovery_smoke: OK")


def _check_business_time_replay() -> None:
    shipment = {
        "invoice_no": "fixture",
        "shipment_id": "shipment-fixture",
        "first_payment_date": "2026-07-19",
        "actual_shipment_date": "2026-07-21",
        "actual_ff_acceptance_date": "2026-07-23",
        "expenses_complete": True,
        "source_fingerprint": "sha256:source",
        "calculation_fingerprint": "sha256:calculation",
        "lines": [
            {
                "line_id": "line-1",
                "nm_id": 1001,
                "quantity": "10",
                "current_components": [
                    _component("payment", "1000", "2026-07-19"),
                    _component("late-cost", "100", "2026-07-22"),
                ],
            }
        ],
    }
    supply = {
        "business_date": "2026-07-24",
        "operation_id": "wb-debit-fixture",
        "source_revision": "sha256:wb-supply",
        "composition": {"1001": "4"},
        "unit_costs": {"1001": "110"},
    }
    stages = {}
    for day in ("2026-07-19", "2026-07-21", "2026-07-22", "2026-07-23", "2026-07-24"):
        rows = _correct_balances(
            [],
            business_date=day,
            shipment_manifest={"fixture": shipment},
            supply_manifest={"fixture-supply": supply},
            wb_daily={},
            discrepancy_rows={},
            target_nm_ids=[1001],
        )
        stages[day] = {
            str(row["warehouse_key"]): (
                Decimal(str(row["quantity"])),
                Decimal(str(row["capital_rub"])),
            )
            for row in rows
        }
    assert stages["2026-07-19"] == {"production": (Decimal("10"), Decimal("1000"))}
    assert stages["2026-07-21"] == {"china_to_ff": (Decimal("10"), Decimal("1000"))}
    assert stages["2026-07-22"] == {"china_to_ff": (Decimal("10"), Decimal("1100"))}
    assert stages["2026-07-23"] == {"ff": (Decimal("10"), Decimal("1100"))}
    assert stages["2026-07-24"] == {
        "ff": (Decimal("6"), Decimal("660")),
        "ff_to_wb": (Decimal("4"), Decimal("440")),
    }
    for rows in stages.values():
        assert all(quantity > 0 and capital > 0 for quantity, capital in rows.values())


def _component(identity: str, amount: str, day: str) -> dict[str, object]:
    return {
        "source_component_id": identity,
        "component_key": identity,
        "amount_rub": amount,
        "business_effective_date": day,
        "document": {"document_id": identity, "date": day},
    }


def _check_ready_target_boundary() -> None:
    target_metric = own_stage_metric_key("PRODUCTION", "qty")
    other_metric = "orderSum"
    plan = {
        "date_columns": ["2026-07-19", "2026-07-30"],
        "metadata": {},
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "header": ["metric", "row_id", "2026-07-19", "2026-07-30"],
                "rows": [
                    ["target", f"SKU:1001|{target_metric}", "", 777],
                    ["other", f"SKU:1001|{other_metric}", 55, 66],
                ],
            }
        ],
    }
    balances = {day: [] for day in DATES}
    balances["2026-07-19"] = [
            {
                "warehouse_key": "production",
                "nm_id": 1001,
                "quantity": "10",
                "wac_rub": "100",
                "capital_rub": "1000",
                "cost_covered_quantity": "10",
                "quality": "certified",
                "certified": 1,
                "provenance": {},
            }
        ]
    updates = _ready_updates(
        snapshots=[
            {
                "bundle_version": "fixture",
                "as_of_date": "2026-07-30",
                "plan_json": json.dumps(plan),
            }
        ],
        corrected_by_date=balances,
        target_nm_ids=[1001],
        version_ids={day: f"version-{day}" for day in DATES},
        source_digest="sha256:fixture",
    )
    assert len(updates) == 1
    after = json.loads(str(updates[0]["after_plan_json"]))
    rows = after["sheets"][0]["rows"]
    assert rows[0][2] == 10.0
    assert rows[0][3] == 777
    assert rows[1][2:] == [55, 66]


def _check_early_partial_unavailable() -> None:
    item = _partial_projection_item(
        day="2026-07-18",
        nm_id=1001,
        quantity=Decimal("3"),
        wac=Decimal("101.25"),
        capital=Decimal("303.75"),
        source_digest="sha256:source",
        source_fingerprint="sha256:row",
    )
    wb_keys = {
        own_stage_metric_key("WB", "qty"),
        own_stage_metric_key("WB", "unit_cost_rub"),
        own_stage_metric_key("WB", "capital_rub"),
    }
    assert {key for key, value in item["metrics"].items() if value is not None} == wb_keys
    unavailable = set(item["presentation"])
    assert unavailable == set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS) - wb_keys
    assert all(
        item["presentation"][key]["state"] == "unavailable"
        for key in unavailable
    )


def _check_stale_fingerprints() -> None:
    with TemporaryDirectory(prefix="warehouse-history-stale-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(tmp) / "runtime"
        )
        try:
            apply_historical_recovery_plan(
                runtime,
                {"fingerprint": "sha256:current"},
                confirm_fingerprint="sha256:stale",
                approval_reference="smoke",
            )
        except WarehouseHistoricalRecoveryError:
            pass
        else:
            raise AssertionError("Batch A stale fingerprint must fail closed")
        try:
            apply_early_wb_recovery_plan(
                runtime,
                {"fingerprint": "sha256:current"},
                confirm_fingerprint="sha256:stale",
                approval_reference="smoke",
                batch_a_fingerprint="sha256:a",
            )
        except WarehouseEarlyWbRecoveryError:
            pass
        else:
            raise AssertionError("Batch B stale fingerprint must fail closed")


def _check_early_exact_rollback() -> None:
    with TemporaryDirectory(prefix="warehouse-history-rollback-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(tmp) / "runtime"
        )
        runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        projection_row = _partial_projection_item(
            day="2026-07-18",
            nm_id=1001,
            quantity=Decimal("3"),
            wac=Decimal("101.25"),
            capital=Decimal("303.75"),
            source_digest="sha256:source",
            source_fingerprint="sha256:row",
        )
        payload = {
            "revision_id": "revision-rollback-fixture",
            "projection_rows": [projection_row],
            "ready_updates": [
                {
                    "bundle_version": "bundle-fixture",
                    "as_of_date": "2026-07-18",
                    "after_plan_json": '{"state":"after"}',
                }
            ],
        }
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_warehouse_business_projection_schema(conn)
            conn.execute(
                "CREATE TABLE sheet_vitrina_v1_ready_snapshots("
                "bundle_version TEXT NOT NULL,as_of_date TEXT NOT NULL,"
                "plan_json TEXT NOT NULL,PRIMARY KEY(bundle_version,as_of_date))"
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?)",
                ("bundle-fixture", "2026-07-18", '{"state":"before"}'),
            )
            conn.commit()
        plan = {
            "fingerprint": "sha256:rollback-fixture",
            "source_digest": "sha256:source",
            "non_target_digest": "sha256:non-target",
        }
        before_images = _early_before_images(
            runtime.db_path,
            plan=plan,
            payload=payload,
        )
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime.runtime_dir,
            db_path=runtime.db_path,
        )
        operation = registry.prepare_t1(
            mutation_kind="targeted_warehouse_publication",
            closure_kind="sku_date",
            plan_fingerprint=plan["fingerprint"],
            scope={"dates": ["2026-07-18"], "nm_ids": [1001]},
            before_images=before_images,
            source_digest=plan["source_digest"],
            non_target_digest=plan["non_target_digest"],
        )
        operation = registry.begin_mutation(
            str(operation["operation_id"]),
            expected_source_digest=plan["source_digest"],
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            _persist_projection_revision(
                conn,
                revision_id=payload["revision_id"],
                stable_source_id="fixture",
                source_revision=plan["source_digest"],
                business_effective_date="2026-07-18",
                published_at="2026-08-01T00:00:00Z",
                plan_fingerprint=plan["fingerprint"],
                base_version_id="",
                published_version_id="",
                affected_nm_ids=[1001],
                source_kind="fixture",
                rows=[projection_row],
                diagnostics={"affected_dates": ["2026-07-18"]},
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                "WHERE bundle_version=? AND as_of_date=?",
                ('{"state":"after"}', "bundle-fixture", "2026-07-18"),
            )
            conn.commit()
        retained = registry.retain(
            str(operation["operation_id"]),
            after_digest="sha256:after",
            non_target_digest=plan["non_target_digest"],
        )
        assert retained["lifecycle"] == RecoveryState.RETAINED.value
        rolled_back = registry.rollback_t1(
            str(operation["operation_id"]),
            reason="fixture rollback",
        )
        assert rolled_back["lifecycle"] == RecoveryState.ROLLED_BACK.value
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {REVISION_TABLE}").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {ROW_TABLE}").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {CURRENT_ROW_TABLE}").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {STATE_TABLE}").fetchone()[0] == 0
            assert conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots"
            ).fetchone()[0] == '{"state":"before"}'


if __name__ == "__main__":
    main()
