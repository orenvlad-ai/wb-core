"""Regression checks for durable targeted late-transit-cost replay."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.wb_supplies import _normalize_supply_row  # noqa: E402
from packages.application.wb_transit_cost_replay import (  # noqa: E402
    reconcile_completed_transit_costs,
)


ENDPOINT = "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost"


class FakeCostBlock:
    def __init__(self, *, changed: int = 1, error: str = "") -> None:
        self.changed = changed
        self.error = error
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def materialize_wb_supply_cost_layers(
        self,
        *,
        opening_date: str,
        supply_ids: list[str],
    ) -> int:
        self.calls.append((opening_date, tuple(supply_ids)))
        if self.error:
            raise ValueError(self.error)
        return self.changed


def main() -> None:
    with TemporaryDirectory(prefix="wb-transit-replay-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(tmp) / "runtime"
        )
        _save_supply(runtime, "40422317")
        _save_success(runtime, "40422317", 10164.0, "2026-07-15T00:00:00Z")
        first = reconcile_completed_transit_costs(
            runtime=runtime,
            cost_block=FakeCostBlock(changed=2),  # type: ignore[arg-type]
            supply_ids=["40422317"],
            timestamp_factory=lambda: "2026-08-01T00:00:00Z",
        )
        queued = first["targeted_recalculations"][0]["queue"]
        if (
            queued.get("status") != "queued"
            or queued.get("stable_source_id") != "wb_transit_cost:40422317"
            or queued.get("effective_date") != "2026-07-04"
            or json.loads(queued.get("affected_nm_ids_json") or "[]")
            != [1001, 1002]
            or first.get("physical_movements_created") != 0
        ):
            raise AssertionError(f"late transit replay scope mismatch: {first}")
        physical_before = _physical_digest(runtime)
        repeated = reconcile_completed_transit_costs(
            runtime=runtime,
            cost_block=FakeCostBlock(changed=0),  # type: ignore[arg-type]
            supply_ids=["40422317"],
            timestamp_factory=lambda: "2026-08-01T01:00:00Z",
        )
        repeat_queue = repeated["targeted_recalculations"][0]["queue"]
        if (repeat_queue.get("recovery_policy") or {}).get("tier") != "T0":
            raise AssertionError("unchanged transit fact replay must be a queue T0")
        if _physical_digest(runtime) != physical_before:
            raise AssertionError("late cost replay must not create physical movement")
        if runtime.finalize_completed_wb_transit_cost_recalculations(
            completed_at="2026-08-01T01:05:00Z"
        ).get("changed_supply_count") != 0:
            raise AssertionError("queued replay must not be finalized early")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_targeted_recalc_queue "
                "SET status='complete',finished_at=? WHERE stable_source_id=?",
                (
                    "2026-08-01T01:10:00Z",
                    "wb_transit_cost:40422317",
                ),
            )
            conn.commit()
        finalized = runtime.finalize_completed_wb_transit_cost_recalculations(
            completed_at="2026-08-01T01:15:00Z"
        )
        if finalized.get("changed_supply_count") != 1:
            raise AssertionError(
                "completed exact replay must finalize one canonical fact"
            )
        completed_fact = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        if completed_fact.get("recalculation_status") != "complete":
            raise AssertionError("completed replay status must be durable")

        before_fact = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        _save_success(runtime, "40422317", 10165.0, "2026-07-15T01:00:00Z")
        after_fact = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        if (
            after_fact.get("success_revision")
            != int(before_fact.get("success_revision") or 0) + 1
            or after_fact.get("source_revision") == before_fact.get("source_revision")
        ):
            raise AssertionError("changed success must create exactly one fact revision")

        try:
            reconcile_completed_transit_costs(
                runtime=runtime,
                cost_block=FakeCostBlock(error="synthetic materialization failure"),  # type: ignore[arg-type]
                supply_ids=["40422317"],
                timestamp_factory=lambda: "2026-08-01T02:00:00Z",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("replay materialization failure must fail closed")
        failed = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        if (
            failed.get("status") != "success"
            or failed.get("amount") != 10165.0
            or failed.get("recalculation_status") != "recalculation_error"
            or "synthetic" not in str(failed.get("recalculation_error") or "")
        ):
            raise AssertionError(
                "saved fact must survive replay failure with retryable error state"
            )
    print("wb_transit_cost_replay_smoke: OK")


def _save_supply(runtime: RegistryUploadDbBackedRuntime, supply_id: str) -> None:
    synced_at = "2026-07-15T00:00:00Z"
    row = _normalize_supply_row(
        raw_list={
            "supplyID": int(supply_id),
            "statusID": 6,
            "supplyDate": "2026-07-04T00:00:00+03:00",
        },
        raw_detail={
            "supplyID": int(supply_id),
            "statusID": 6,
            "warehouseName": "Новосемейкино",
            "transitWarehouseName": "Чехов 1",
            "quantity": 10,
            "acceptanceCost": 0,
        },
        raw_goods=[
            {"nmID": 1001, "quantity": 6, "acceptedQuantity": 5},
            {"nmID": 1002, "quantity": 4, "acceptedQuantity": 3},
        ],
        raw_package=None,
        warehouse_by_id={},
        synced_at=synced_at,
        warnings=[],
    )
    runtime.save_wb_supply_rows(
        rows=[row],
        warehouses=[],
        synced_at=synced_at,
    )


def _save_success(
    runtime: RegistryUploadDbBackedRuntime,
    supply_id: str,
    amount: float,
    timestamp: str,
) -> None:
    runtime.upsert_wb_supply_transit_cost_enrichment(
        {
            "supply_id": supply_id,
            "amount": amount,
            "currency": "RUB",
            "amount_label": f"{amount:.2f} ₽",
            "is_transit": True,
            "source": "seller_portal_browser",
            "evidence_type": "network_json",
            "confidence": "high",
            "fetched_at": timestamp,
            "status": "success",
            "error": "",
            "source_endpoint_path": ENDPOINT,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
    )


def _physical_digest(runtime: RegistryUploadDbBackedRuntime) -> str:
    with sqlite3.connect(runtime.db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "sheet_vitrina_v1_warehouse_functional_events" not in tables:
            return "absent"
        rows: list[tuple[Any, ...]] = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_events "
            "ORDER BY event_id"
        ).fetchall()
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)


if __name__ == "__main__":
    main()
