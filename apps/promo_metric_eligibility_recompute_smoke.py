"""Smoke-check promo metric eligibility recompute dry-run/apply safety."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.promo_metric_eligibility_recompute import recompute_promo_eligibility
from apps.sheet_vitrina_v1_promo_live_source_smoke import _write_promo_run_fixture
from packages.application.promo_campaign_archive import sync_promo_campaign_archive
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.contracts.promo_live_source import PromoLiveSourceItem, PromoLiveSourceSuccess


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
SNAPSHOT_DATE = "2026-05-03"
ROLE_CLOSED = "accepted_closed_day_snapshot"
PRICES_ACCEPTED_CURRENT_ROLE = "accepted_current_snapshot"


def main() -> None:
    with TemporaryDirectory(prefix="promo-eligibility-recompute-smoke-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        result = runtime.ingest_bundle(bundle, activated_at="2026-05-03T08:00:00Z")
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest failed: {result}")
        requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]][:2]
        equal_nm_id = requested_nm_ids[0]
        over_plan_nm_id = requested_nm_ids[1]

        _write_promo_run_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-05-03__fixture",
            promo_folder="2400__2300__equality-promo",
            promo_id=2400,
            period_id=2300,
            promo_title="Equality promo",
            promo_period_text="03 мая 02:00 -> 03 мая 23:59",
            promo_start_at="2026-05-03T02:00",
            promo_end_at="2026-05-03T23:59",
            workbook_rows=[
                {"nm_id": equal_nm_id, "plan_price": 508.0},
                {"nm_id": over_plan_nm_id, "plan_price": 508.0},
            ],
        )
        sync_promo_campaign_archive(runtime_dir)
        runtime.save_temporal_source_slot_snapshot(
            source_key="prices_snapshot",
            snapshot_date=SNAPSHOT_DATE,
            snapshot_role=PRICES_ACCEPTED_CURRENT_ROLE,
            captured_at="2026-05-03T08:00:00Z",
            payload=SimpleNamespace(
                kind="success",
                snapshot_date=SNAPSHOT_DATE,
                items=[
                    SimpleNamespace(nm_id=equal_nm_id, price_seller=508.0, price_seller_discounted=508.0),
                    SimpleNamespace(nm_id=over_plan_nm_id, price_seller=508.49, price_seller_discounted=508.49),
                ],
            ),
        )
        runtime.save_temporal_source_slot_snapshot(
            source_key="promo_by_price",
            snapshot_date=SNAPSHOT_DATE,
            snapshot_role=ROLE_CLOSED,
            captured_at="2026-05-04T08:00:00Z",
            payload=PromoLiveSourceSuccess(
                kind="success",
                snapshot_date=SNAPSHOT_DATE,
                date_from=SNAPSHOT_DATE,
                date_to=SNAPSHOT_DATE,
                requested_count=2,
                covered_count=2,
                items=[
                    PromoLiveSourceItem(
                        snapshot_date=SNAPSHOT_DATE,
                        nm_id=equal_nm_id,
                        promo_count_by_price=0.0,
                        promo_entry_price_best=508.0,
                        promo_participation=0.0,
                    ),
                    PromoLiveSourceItem(
                        snapshot_date=SNAPSHOT_DATE,
                        nm_id=over_plan_nm_id,
                        promo_count_by_price=0.0,
                        promo_entry_price_best=508.0,
                        promo_participation=0.0,
                    ),
                ],
                detail="old strict fixture",
                trace_run_dir="/tmp/old-strict",
                current_promos=1,
                current_promos_downloaded=1,
                current_promos_blocked=0,
                future_promos=0,
                skipped_past_promos=0,
                ambiguous_promos=0,
            ),
        )
        _insert_ready_snapshot(runtime_dir, equal_nm_id=equal_nm_id, over_plan_nm_id=over_plan_nm_id)

        dry_run = recompute_promo_eligibility(
            runtime_dir=runtime_dir,
            date_from=SNAPSHOT_DATE,
            date_to=SNAPSHOT_DATE,
            all_available=False,
            apply=False,
            backup=False,
        )
        if dry_run["dates_changed"] != 1 or dry_run["metric_cells_changed"] != 4:
            raise AssertionError(f"dry-run must find one equality SKU and 4 metric cells, got {dry_run}")

        applied = recompute_promo_eligibility(
            runtime_dir=runtime_dir,
            date_from=SNAPSHOT_DATE,
            date_to=SNAPSHOT_DATE,
            all_available=False,
            apply=True,
            backup=True,
        )
        if not applied["backup_path"] or not Path(applied["backup_path"]).exists():
            raise AssertionError(f"apply must create SQLite backup, got {applied}")

        after = _read_ready_values(runtime_dir, equal_nm_id=equal_nm_id, over_plan_nm_id=over_plan_nm_id)
        expected = {
            f"SKU:{equal_nm_id}|promo_participation": 1.0,
            f"SKU:{equal_nm_id}|promo_count_by_price": 1.0,
            f"SKU:{over_plan_nm_id}|promo_participation": 0.0,
            f"SKU:{over_plan_nm_id}|promo_count_by_price": 0.0,
            "TOTAL|total_promo_participation": 1.0,
            "TOTAL|total_promo_count_by_price": 1.0,
        }
        if after != expected:
            raise AssertionError(f"ready snapshot values mismatch: {after}")

        idempotent = recompute_promo_eligibility(
            runtime_dir=runtime_dir,
            date_from=SNAPSHOT_DATE,
            date_to=SNAPSHOT_DATE,
            all_available=False,
            apply=False,
            backup=False,
        )
        if idempotent["metric_cells_changed"] != 0 or idempotent["slot_payloads_changed"] != 0:
            raise AssertionError(f"second dry-run must be idempotent, got {idempotent}")

    print("promo_eligibility_recompute: ok -> dry-run/apply/idempotent")
    print("promo_eligibility_recompute_backup: ok -> SQLite backup created for apply")
    print("smoke-check passed")


def _insert_ready_snapshot(runtime_dir: Path, *, equal_nm_id: int, over_plan_nm_id: int) -> None:
    db_path = runtime_dir / "registry_upload_runtime.sqlite3"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        state = conn.execute(
            "SELECT bundle_version, activated_at FROM registry_upload_current_state WHERE slot = 1"
        ).fetchone()
        plan = {
            "plan_version": "promo_eligibility_recompute_smoke",
            "snapshot_id": "promo_eligibility_recompute_smoke",
            "as_of_date": SNAPSHOT_DATE,
            "date_columns": [SNAPSHOT_DATE],
            "temporal_slots": [{"slot_key": "yesterday_closed", "column_date": SNAPSHOT_DATE}],
            "source_temporal_policies": {"promo_by_price": "dual_day_capable"},
            "metadata": {},
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "rows": [
                        ["SKU equality participation", f"SKU:{equal_nm_id}|promo_participation", 0.0],
                        ["SKU equality count", f"SKU:{equal_nm_id}|promo_count_by_price", 0.0],
                        ["SKU over participation", f"SKU:{over_plan_nm_id}|promo_participation", 0.0],
                        ["SKU over count", f"SKU:{over_plan_nm_id}|promo_count_by_price", 0.0],
                        ["Total participation", "TOTAL|total_promo_participation", 0.0],
                        ["Total count", "TOTAL|total_promo_count_by_price", 0.0],
                        ["Entry best untouched", f"SKU:{equal_nm_id}|promo_entry_price_best", 508.0],
                    ],
                },
                {
                    "sheet_name": "STATUS",
                    "rows": [
                        ["promo_by_price[yesterday_closed]", "success", SNAPSHOT_DATE, "", "", "", "", 2, 2, "", "old strict fixture"],
                    ],
                },
            ],
        }
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ready_snapshots(
                bundle_version,
                activated_at,
                as_of_date,
                snapshot_id,
                plan_version,
                refreshed_at,
                plan_json
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state["bundle_version"],
                state["activated_at"],
                SNAPSHOT_DATE,
                "promo_eligibility_recompute_smoke",
                "promo_eligibility_recompute_smoke",
                "2026-05-04T08:00:00Z",
                json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        conn.commit()


def _read_ready_values(runtime_dir: Path, *, equal_nm_id: int, over_plan_nm_id: int) -> dict[str, float]:
    db_path = runtime_dir / "registry_upload_runtime.sqlite3"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date = ?",
            (SNAPSHOT_DATE,),
        ).fetchone()
    plan = json.loads(row["plan_json"])
    rows = {
        data_row[1]: data_row[2]
        for sheet in plan["sheets"]
        if sheet.get("sheet_name") == "DATA_VITRINA"
        for data_row in sheet["rows"]
        if data_row[1]
    }
    return {
        key: float(rows[key])
        for key in (
            f"SKU:{equal_nm_id}|promo_participation",
            f"SKU:{equal_nm_id}|promo_count_by_price",
            f"SKU:{over_plan_nm_id}|promo_participation",
            f"SKU:{over_plan_nm_id}|promo_count_by_price",
            "TOTAL|total_promo_participation",
            "TOTAL|total_promo_count_by_price",
        )
    }


if __name__ == "__main__":
    main()
