"""Smoke for guarded SPP recompute runner."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.spp_metric_recompute import DB_FILENAME, run_recompute  # noqa: E402


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        runtime_dir = Path(tmp)
        db_path = runtime_dir / DB_FILENAME
        _init_db(db_path)
        goods_path = runtime_dir / "goods.json"
        goods_path.write_text(
            json.dumps(
                [
                    {"nmID": 210183919, "discountOnSite": 23},
                    {"nmID": 210184534, "discountOnSite": 30},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        dry = run_recompute(
            command="dry-run",
            runtime_dir=runtime_dir,
            date_from="2026-05-05",
            date_to="2026-05-06",
            current_date="2026-05-06",
            storage_state_path=None,
            fixture_goods_json=goods_path,
            backup=False,
        )
        if dry["status"] != "success":
            raise AssertionError(f"dry-run must pass, got {dry}")
        if dry["sku_rows_changed"] != 2:
            raise AssertionError(f"dry-run must detect 2 SKU changes, got {dry['sku_rows_changed']}")
        if dry["metric_cells_changed"] != 3:
            raise AssertionError(f"dry-run must change 2 SKU cells + total avg, got {dry['metric_cells_changed']}")
        if not dry["skipped_dates"] or dry["skipped_dates"][0]["date"] != "2026-05-05":
            raise AssertionError(f"historical date must be skipped truthfully, got {dry['skipped_dates']}")

        blocked = run_recompute(
            command="apply",
            runtime_dir=runtime_dir,
            date_from=None,
            date_to=None,
            current_date="2026-05-06",
            storage_state_path=None,
            fixture_goods_json=goods_path,
            backup=False,
        )
        if blocked["status"] != "blocked":
            raise AssertionError("apply without backup must be blocked")

        applied = run_recompute(
            command="apply",
            runtime_dir=runtime_dir,
            date_from=None,
            date_to=None,
            current_date="2026-05-06",
            storage_state_path=None,
            fixture_goods_json=goods_path,
            backup=True,
        )
        if applied["status"] != "success" or not applied["backup_path"]:
            raise AssertionError(f"apply must pass with backup, got {applied}")

        second = run_recompute(
            command="dry-run",
            runtime_dir=runtime_dir,
            date_from=None,
            date_to=None,
            current_date="2026-05-06",
            storage_state_path=None,
            fixture_goods_json=goods_path,
            backup=False,
        )
        if second["sku_rows_changed"] != 0 or second["metric_cells_changed"] != 0:
            raise AssertionError(f"second dry-run must be idempotent, got {second}")

    print("spp_metric_recompute: ok")


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            CREATE TABLE registry_upload_current_state(slot INTEGER PRIMARY KEY, bundle_version TEXT, activated_at TEXT);
            CREATE TABLE registry_upload_config_v2(
                bundle_version TEXT,
                nm_id INTEGER,
                enabled INTEGER,
                display_name TEXT,
                group_name TEXT,
                display_order INTEGER
            );
            CREATE TABLE temporal_source_slot_snapshots(
                source_key TEXT,
                snapshot_date TEXT,
                snapshot_role TEXT,
                captured_at TEXT,
                payload_json TEXT,
                PRIMARY KEY(source_key, snapshot_date, snapshot_role)
            );
            CREATE TABLE sheet_vitrina_v1_ready_snapshots(
                bundle_version TEXT,
                activated_at TEXT,
                as_of_date TEXT,
                snapshot_id TEXT,
                plan_version TEXT,
                refreshed_at TEXT,
                plan_json TEXT,
                PRIMARY KEY(bundle_version, as_of_date)
            );
            """
        )
        bundle = "bundle"
        conn.execute(
            "INSERT INTO registry_upload_current_state(slot, bundle_version, activated_at) VALUES(1, ?, ?)",
            (bundle, "2026-05-06T00:00:00Z"),
        )
        for order, nm_id in enumerate((210183919, 210184534), start=1):
            conn.execute(
                """
                INSERT INTO registry_upload_config_v2(
                    bundle_version, nm_id, enabled, display_name, group_name, display_order
                )
                VALUES(?, ?, 1, ?, 'Clean', ?)
                """,
                (bundle, nm_id, str(nm_id), order),
            )
        conn.execute(
            """
            INSERT INTO temporal_source_slot_snapshots(
                source_key, snapshot_date, snapshot_role, captured_at, payload_json
            )
            VALUES('spp', '2026-05-06', 'accepted_current_snapshot', 'old', ?)
            """,
            (
                json.dumps(
                    {
                        "kind": "success",
                        "snapshot_date": "2026-05-06",
                        "count": 2,
                        "items": [
                            {"nm_id": 210183919, "spp": 0.28},
                            {"nm_id": 210184534, "spp": 0.28},
                        ],
                    }
                ),
            ),
        )
        plan = {
            "plan_version": "test",
            "snapshot_id": "old",
            "as_of_date": "2026-05-05",
            "metadata": {},
            "sheets": [
                {
                    "sheet_name": "DATA_VITRINA",
                    "header": ["label", "key", "2026-05-05", "2026-05-06"],
                    "rows": [
                        ["Итого: СПП средняя", "TOTAL|avg_spp", 0.28, 0.28],
                        ["СПП", "SKU:210183919|spp", 0.28, 0.28],
                        ["СПП", "SKU:210184534|spp", 0.28, 0.28],
                        ["Показы", "SKU:210184534|view_count", 1, 2],
                    ],
                },
                {
                    "sheet_name": "STATUS",
                    "rows": [
                        [
                            "spp[today_current]",
                            "success",
                            "2026-05-06",
                            "2026-05-06",
                            "",
                            "",
                            "",
                            2,
                            2,
                            "",
                            "old",
                        ]
                    ],
                },
            ],
        }
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_ready_snapshots(
                bundle_version, activated_at, as_of_date, snapshot_id, plan_version, refreshed_at, plan_json
            )
            VALUES(?, 'activated', '2026-05-05', 'old', 'test', 'old', ?)
            """,
            (bundle, json.dumps(plan)),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
