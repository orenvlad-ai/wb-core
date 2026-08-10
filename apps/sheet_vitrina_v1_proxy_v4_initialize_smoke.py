"""Guarded dry-run/apply/idempotency smoke for Proxy V4 initialization."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_proxy_v4_initialize import (  # noqa: E402
    run_initialization,
)
from apps.sheet_vitrina_v1_proxy_v4_smoke import (  # noqa: E402
    BUNDLE_FIXTURE,
    _ensure_finance_tables,
    _save_buyout_week,
    _save_finance_week,
)
from packages.application.calculation_parameters import (  # noqa: E402
    CalculationParametersBlock,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    ProxyV4ParametersBlock,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


NOW = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)
DEPLOYED_SHA = "1" * 40


def main() -> None:
    with TemporaryDirectory(prefix="proxy-v4-init-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime_dir = root / "runtime"
        evidence_dir = root / "evidence"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(
            json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8")),
            activated_at="2026-08-01T00:00:00Z",
        )
        if accepted.status != "accepted":
            raise AssertionError(f"fixture ingest failed: {accepted}")
        CalculationParametersBlock(runtime=runtime).ensure_initial_version(
            created_at="2026-07-01T00:00:00Z"
        )
        enabled_nm_ids = [
            item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled
        ]
        _ensure_finance_tables(runtime.db_path)
        for week_start, buyout, first_loaded_at in (
            ("2026-07-06", "0.70", "2026-07-13T07:00:00Z"),
            ("2026-07-13", "0.80", "2026-07-20T07:00:00Z"),
            ("2026-07-20", "0.90", "2026-07-27T07:00:00Z"),
            ("2026-07-27", "1.00", "2026-08-03T07:00:00Z"),
        ):
            _save_buyout_week(runtime, week_start, enabled_nm_ids, Decimal(buyout))
            _save_finance_week(runtime.db_path, week_start, first_loaded_at)
        current_state = runtime.load_current_state()
        target_dates = tuple(f"2026-08-{day:02d}" for day in range(1, 10))
        for as_of_date in target_dates:
            plan = _ready_plan(as_of_date, enabled_nm_ids[:2])
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{as_of_date}T12:00:00Z",
                plan=plan,
            )

        v3_before = _v3_digest(runtime.db_path)
        dry_run = run_initialization(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            apply=False,
            now=NOW,
        )
        if dry_run["status"] != "ready" or dry_run["planned_version_count"] != 2:
            raise AssertionError(f"V4 initialization dry-run failed: {dry_run}")
        if dry_run["target_snapshot_count"] != 9 or dry_run["insert_v4_row_count"] != 54:
            raise AssertionError(f"V4 snapshot scope drifted: {dry_run}")
        manifest = json.loads(Path(dry_run["manifest_path"]).read_text(encoding="utf-8"))
        version_parameters = [
            json.loads(item["parameters_json"])
            for item in manifest["desired"]["version_rows"]
        ]
        if (
            [item["source_week_count"] for item in version_parameters] != [3, 3]
            or [item["finance_net_revenue_weight"] for item in version_parameters]
            != ["3000", "3000"]
            or any(Decimal(item["buyout_order_count_weight"]) <= 0 for item in version_parameters)
        ):
            raise AssertionError(f"initial version coverage/denominators are not reviewable: {version_parameters}")

        ProxyV4ParametersBlock(runtime=runtime, now_factory=lambda: NOW)
        sha_file = root / "deployed.sha"
        sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        applied = run_initialization(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            apply=True,
            manifest_path=Path(dry_run["manifest_path"]),
            expected_manifest_sha256=str(dry_run["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=sha_file,
            approval_reference="owner-gate-test",
            now=NOW,
        )
        if applied["status"] != "reconciled" or not applied["database_written"]:
            raise AssertionError(f"V4 initialization apply failed: {applied}")
        if not applied["non_target_preserved"] or _v3_digest(runtime.db_path) != v3_before:
            raise AssertionError("V4 initialization changed V3 parameters")

        repeated = run_initialization(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            apply=True,
            manifest_path=Path(dry_run["manifest_path"]),
            expected_manifest_sha256=str(dry_run["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=sha_file,
            approval_reference="owner-gate-test",
            now=NOW,
        )
        if repeated["status"] != "already_applied" or not repeated["idempotent_noop"]:
            raise AssertionError(f"V4 initialization repeat was not idempotent: {repeated}")

        block = ProxyV4ParametersBlock(runtime=runtime, now_factory=lambda: NOW)
        if [item["effective_date"] for item in block.get_payload()["history"]] != [
            "2026-08-08",
            "2026-08-01",
        ]:
            raise AssertionError("V4 historical versions were not read back in effective order")
        for as_of_date in target_dates:
            snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
            rows_by_id = {
                str(row[1]): row
                for sheet in snapshot.sheets
                if sheet.sheet_name == "DATA_VITRINA"
                for row in sheet.rows
            }
            for metric_key in (
                "proxy_profit_4_rub",
                "proxy_margin_4_pct",
                "total_proxy_profit_4_rub",
                "proxy_margin_4_pct_total",
            ):
                if not any(row_id.endswith("|" + metric_key) for row_id in rows_by_id):
                    raise AssertionError(f"initialized snapshot misses {metric_key}")
            buyout_rate = Decimal("0.8") if as_of_date <= "2026-08-07" else Decimal("0.9")
            retained = Decimal("0.754")
            eligible: list[tuple[Decimal, Decimal]] = []
            for index, nm_id in enumerate(enabled_nm_ids[:2], start=1):
                order_sum = Decimal(1000 + index * 100)
                order_count = Decimal(10 + index)
                cost = Decimal(20 + index)
                ads = Decimal(30 + index)
                expected_revenue = order_sum * buyout_rate
                expected_profit = (
                    expected_revenue * retained
                    - order_count * buyout_rate * cost
                    - ads
                )
                actual_profit = Decimal(
                    str(rows_by_id[f"SKU:{nm_id}|proxy_profit_4_rub"][2])
                )
                actual_margin = Decimal(
                    str(rows_by_id[f"SKU:{nm_id}|proxy_margin_4_pct"][2])
                )
                if abs(actual_profit - expected_profit) > Decimal("0.0000005"):
                    raise AssertionError(
                        f"historical V4 SKU formula/as-of version drifted on {as_of_date}"
                    )
                if abs(actual_margin - expected_profit / expected_revenue) > Decimal("0.0000005"):
                    raise AssertionError(f"historical V4 SKU margin drifted on {as_of_date}")
                eligible.append((expected_profit, expected_revenue))
            expected_total_profit = sum((item[0] for item in eligible), Decimal("0"))
            expected_total_revenue = sum((item[1] for item in eligible), Decimal("0"))
            actual_total_profit = Decimal(
                str(rows_by_id["TOTAL|total_proxy_profit_4_rub"][2])
            )
            actual_total_margin = Decimal(
                str(rows_by_id["TOTAL|proxy_margin_4_pct_total"][2])
            )
            if abs(actual_total_profit - expected_total_profit) > Decimal("0.0000005"):
                raise AssertionError(f"historical V4 TOTAL profit drifted on {as_of_date}")
            if abs(
                actual_total_margin - expected_total_profit / expected_total_revenue
            ) > Decimal("0.0000005"):
                raise AssertionError(f"historical V4 TOTAL margin is not a ratio of aggregates")

    print("proxy_v4_initialization_dry_run_manifest: ok")
    print("proxy_v4_initialization_backup_apply_readback: ok")
    print("proxy_v4_initialization_idempotency_v3_invariant: ok")
    print("proxy_v4_aug_1_7_aug_8_9_as_of_versions_no_drift: ok")


def _ready_plan(as_of_date: str, nm_ids: list[int]) -> SheetVitrinaV1Envelope:
    rows: list[list[object]] = []
    for index, nm_id in enumerate(nm_ids, start=1):
        label = f"SKU {index}"
        rows.extend(
            [
                [f"{label}: Сумма заказов", f"SKU:{nm_id}|orderSum", 1000 + index * 100],
                [f"{label}: Заказы", f"SKU:{nm_id}|orderCount", 10 + index],
                [f"{label}: Себестоимость", f"SKU:{nm_id}|our_wb_unit_cost_rub", 20 + index],
                [f"{label}: Реклама", f"SKU:{nm_id}|ads_sum", 30 + index],
                [f"{label}: Proxy V3", f"SKU:{nm_id}|proxy_profit_3_rub", 123 + index],
            ]
        )
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__proxy_v4_init_smoke",
        snapshot_id=f"proxy-v4-init-{as_of_date}",
        as_of_date=as_of_date,
        date_columns=[as_of_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="exact_date",
                slot_label=as_of_date,
                column_date=as_of_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:C{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                row_count=len(rows),
                column_count=3,
                header=["label", "key", as_of_date],
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
    )


def _v3_digest(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """SELECT version_id,revision,effective_date,rates_json,fingerprint
               FROM sheet_vitrina_v1_calculation_parameter_versions ORDER BY revision"""
        ).fetchall()
    return json.dumps(rows, sort_keys=True)


if __name__ == "__main__":
    main()
