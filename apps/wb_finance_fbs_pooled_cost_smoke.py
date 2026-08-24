#!/usr/bin/env python3
"""Synthetic proof for pooled FBS Finance cost and exact-day fallback."""

from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.canonical_wb_cost_resolver import (  # noqa: E402
    CHANNEL_LOCATION_COST_FORMULA_VERSION,
    resolve_channel_location_cost,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    ensure_ff_pool_foundation_schema,
)


DAY = "2026-08-20"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wb-finance-fbs-pool-") as raw:
        path = Path(raw) / "operational.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_ff_pool_foundation_schema(conn)
            conn.execute(
                f"INSERT INTO {FEATURE_EPOCHS_TABLE} VALUES(1,1,1,'fixture','2026-08-20T00:00:00Z','{{}}')"
            )
            for facility_id, code in (("ff_a", "A"), ("ff_b", "B")):
                conn.execute(
                    f"INSERT INTO {FACILITIES_TABLE} VALUES(?,?,?,1,'UTC','2026-08-20T00:00:00Z','2026-08-20T00:00:00Z')",
                    (facility_id, code, f"Facility {code}"),
                )
            for number, (facility_id, quantity, capital) in enumerate(
                (("ff_a", 10, "1000"), ("ff_b", 30, "6000")), start=1
            ):
                operation_id = f"opening_{number}"
                conn.execute(
                    f"""INSERT INTO {OPERATIONS_TABLE} VALUES(
                           ?,'facility_pool_opening','fixture','opening',?,'v1',1,?,
                           '2026-08-20T00:00:00Z','{{}}')""",
                    (operation_id, operation_id, DAY),
                )
                conn.execute(
                    f"INSERT INTO {LINES_TABLE} VALUES(?,1,?,'FBS',101,?,?,NULL,'{{}}')",
                    (operation_id, facility_id, quantity, capital),
                )
                conn.execute(
                    f"INSERT INTO {BALANCES_TABLE} VALUES(?,'FBS',101,1,?,?,?,'fixture','2026-08-20T00:00:00Z')",
                    (facility_id, quantity, capital, str(int(capital) / quantity)),
                )
            conn.execute(
                """CREATE TABLE sheet_vitrina_v1_ready_snapshots(
                       bundle_version TEXT,activated_at TEXT,as_of_date TEXT,
                       snapshot_id TEXT,plan_version TEXT,refreshed_at TEXT,
                       plan_json TEXT,PRIMARY KEY(bundle_version,as_of_date))"""
            )
            plan = {
                "date_columns": [DAY, "2026-08-21", "2026-08-22"],
                "sheets": [
                    {
                        "name": "DATA_VITRINA",
                        "header": ["label", "key", DAY, "2026-08-21", "2026-08-22"],
                        "rows": [
                            ["SKU 101", "SKU:101|our_wb_unit_cost_rub", "", "", "66"],
                            ["SKU 202", "SKU:202|our_wb_unit_cost_rub", "88", "99", ""],
                            ["SKU 303", "SKU:303|our_wb_unit_cost_rub", "", "77", ""],
                        ],
                    }
                ],
                "metadata": {
                    "functional_economics_backfill": {
                        "inventory_cost_publication": {
                            "formula_version": "our_inventory_wac_wb_ff_v1",
                            "date_evidence": {
                                DAY: {}, "2026-08-21": {}, "2026-08-22": {}
                            },
                        }
                    }
                },
            }
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)",
                (
                    "fixture", "2026-08-21T00:00:00Z", DAY, "snapshot-fixture",
                    "v1", "2026-08-21T00:00:00Z",
                    json.dumps(plan, ensure_ascii=False),
                ),
            )
            before = conn.execute(
                f"SELECT facility_id,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} ORDER BY facility_id"
            ).fetchall()
            pooled = resolve_channel_location_cost(
                conn,
                nm_id="101",
                operation_date=date.fromisoformat(DAY),
                operation={"deliveryType": "fbs"},
            )
            carried = resolve_channel_location_cost(
                conn,
                nm_id="101",
                operation_date=date(2026, 8, 21),
                operation={"deliveryType": "fbs"},
            )
            fallback = resolve_channel_location_cost(
                conn,
                nm_id="202",
                operation_date=date.fromisoformat(DAY),
                operation={"deliveryType": "fbs"},
            )
            no_lookahead = resolve_channel_location_cost(
                conn,
                nm_id="303",
                operation_date=date.fromisoformat(DAY),
                operation={"deliveryType": "fbs"},
            )
            conn.execute(
                f"""INSERT INTO {OPERATIONS_TABLE} VALUES(
                       'deplete','order_handoff','fixture','deplete','deplete','v1',1,
                       '2026-08-22','2026-08-22T00:00:00Z','{{}}')"""
            )
            conn.execute(
                f"INSERT INTO {LINES_TABLE} VALUES('deplete',1,'ff_a','FBS',101,-10,-1000,NULL,'{{}}')"
            )
            conn.execute(
                f"INSERT INTO {LINES_TABLE} VALUES('deplete',2,'ff_b','FBS',101,-30,-6000,NULL,'{{}}')"
            )
            depleted_fallback = resolve_channel_location_cost(
                conn,
                nm_id="101",
                operation_date=date(2026, 8, 22),
                operation={"deliveryType": "fbs"},
            )
            after = conn.execute(
                f"SELECT facility_id,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} ORDER BY facility_id"
            ).fetchall()
        assert CHANNEL_LOCATION_COST_FORMULA_VERSION.endswith("_v2")
        assert pooled["status"] == "resolved"
        assert pooled["unit_cost_rub"] == "175"
        assert pooled["selection_method"] == "sum_fbs_physical_capital_divided_by_quantity"
        assert pooled["facility_id"] == ""
        assert carried["unit_cost_rub"] == "175"
        assert carried["physical_balance_as_of_date"] == DAY
        assert fallback["status"] == "resolved" and fallback["unit_cost_rub"] == "88"
        assert fallback["quality"] == "same_day_common_inventory_fallback"
        assert no_lookahead["status"] == "missing"
        assert depleted_fallback["unit_cost_rub"] == "66"
        assert depleted_fallback["quality"] == "same_day_common_inventory_fallback"
        assert [tuple(row) for row in before] == [tuple(row) for row in after]
    print("wb finance pooled FBS cost smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
