"""Smoke-check for Seller Portal transit cost enrichment boundary."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_portal_transit_costs import parse_supply_cost_payload  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.wb_supplies import WbSuppliesBlock, _normalize_supply_row  # noqa: E402


TARGET_COST_PAYLOAD = {
    "data": {
        "40422317": {"cost": 10164.0, "costInSupplierCurrency": {"amountWithVat": 10164.0}, "tariffID": 1},
        "40421940": {"cost": 45980.0, "costInSupplierCurrency": {"amountWithVat": 45980.0}, "tariffID": 2},
        "40119116": {"cost": 23724.0, "costInSupplierCurrency": {"amountWithVat": 23724.0}, "tariffID": 3},
        "40119056": {"cost": 3043.69, "costInSupplierCurrency": {"amountWithVat": 3043.69}, "tariffID": 4},
    },
    "error": False,
    "errorText": "",
}


class FakeTransitCostSource:
    def __init__(self, amounts: Mapping[str, float]) -> None:
        self.amounts = {str(key): float(value) for key, value in amounts.items()}
        self.calls: list[list[str]] = []

    def fetch_costs(self, candidates: list[Mapping[str, Any]], *, run_id: str, runtime_dir: Path, fetched_at: str):
        supply_ids = [str(item.get("supply_id") or "") for item in candidates]
        self.calls.append(supply_ids)
        results = []
        for supply_id in supply_ids:
            amount = self.amounts.get(supply_id)
            if amount is None:
                results.append(
                    {
                        "supply_id": supply_id,
                        "amount": None,
                        "currency": "RUB",
                        "amount_label": "",
                        "is_transit": True,
                        "source": "seller_portal_browser",
                        "evidence_type": "network_json",
                        "confidence": "none",
                        "fetched_at": fetched_at,
                        "status": "not_found",
                        "error": "target row not found",
                        "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                    }
                )
                continue
            results.append(
                {
                    "supply_id": supply_id,
                    "amount": amount,
                    "currency": "RUB",
                    "amount_label": _format_rub(amount),
                    "is_transit": True,
                    "source": "seller_portal_browser",
                    "evidence_type": "network_json",
                    "confidence": "high",
                    "fetched_at": fetched_at,
                    "status": "success",
                    "error": "",
                    "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                }
            )
        return results


def main() -> None:
    _check_parser_targets()
    _check_runtime_merge_and_background_job()
    print("wb_supplies_transit_cost_enrichment_smoke: OK")


def _check_parser_targets() -> None:
    rows = {item["supply_id"]: item for item in parse_supply_cost_payload(TARGET_COST_PAYLOAD, fetched_at="2026-06-27T00:00:00Z")}
    expected = {"40422317": 10164.0, "40421940": 45980.0, "40119116": 23724.0, "40119056": 3043.69}
    for supply_id, amount in expected.items():
        row = rows.get(supply_id)
        if not row or row.get("amount") != amount or row.get("confidence") != "high":
            raise AssertionError(f"parser target mismatch for {supply_id}: {row}")


def _check_runtime_merge_and_background_job() -> None:
    with TemporaryDirectory(prefix="wb-transit-cost-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        synced_at = "2026-06-27T00:00:00Z"
        unknown_transit = _normalize_supply_row(
            raw_list={"supplyID": 40422317, "statusID": 3, "supplyDate": "2026-07-02T00:00:00+03:00"},
            raw_detail={
                "supplyID": 40422317,
                "statusID": 3,
                "warehouseName": "Новосемейкино",
                "transitWarehouseName": "Чехов 1",
                "quantity": 5250,
                "acceptanceCost": 0,
                "paidAcceptanceCoefficient": 0,
            },
            raw_goods=None,
            raw_package=None,
            warehouse_by_id={},
            synced_at=synced_at,
            warnings=[],
        )
        missing_transit = _normalize_supply_row(
            raw_list={"supplyID": 40422318, "statusID": 3, "supplyDate": "2026-07-02T00:00:00+03:00"},
            raw_detail={
                "supplyID": 40422318,
                "statusID": 3,
                "warehouseName": "Новосемейкино",
                "transitWarehouseName": "Чехов 1",
                "quantity": 100,
                "acceptanceCost": 0,
                "paidAcceptanceCoefficient": 0,
            },
            raw_goods=None,
            raw_package=None,
            warehouse_by_id={},
            synced_at=synced_at,
            warnings=[],
        )
        official_transit = _normalize_supply_row(
            raw_list={"supplyID": 50000001, "statusID": 3, "supplyDate": "2026-07-03T00:00:00+03:00"},
            raw_detail={
                "supplyID": 50000001,
                "statusID": 3,
                "warehouseName": "Коледино",
                "transitWarehouseName": "Казань",
                "quantity": 1000,
                "acceptanceCost": 0,
                "transitCost": 777.0,
            },
            raw_goods=None,
            raw_package=None,
            warehouse_by_id={},
            synced_at=synced_at,
            warnings=[],
        )
        unknown_non_transit = _normalize_supply_row(
            raw_list={"supplyID": 60000001, "statusID": 2, "supplyDate": "2026-07-04T00:00:00+03:00"},
            raw_detail={
                "supplyID": 60000001,
                "statusID": 2,
                "warehouseName": "Электросталь",
                "quantity": 100,
                "acceptanceCost": None,
            },
            raw_goods=None,
            raw_package=None,
            warehouse_by_id={},
            synced_at=synced_at,
            warnings=[],
        )
        runtime.save_wb_supply_rows(
            rows=[unknown_transit, missing_transit, official_transit, unknown_non_transit],
            warehouses=[],
            synced_at=synced_at,
        )
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "50000001",
                "amount": 12345.0,
                "currency": "RUB",
                "amount_label": "12 345 ₽",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "high",
                "fetched_at": synced_at,
                "status": "success",
                "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                "created_at": synced_at,
                "updated_at": synced_at,
            }
        )
        block = WbSuppliesBlock(
            runtime=runtime,
            transit_cost_source=FakeTransitCostSource({"40422317": 10164.0}),
            timestamp_factory=lambda: "2026-06-27T00:00:00Z",
        )
        reconciled_supply_ids: list[list[str]] = []
        block.transit_cost_reconciliation_callback = lambda supply_ids: (
            reconciled_supply_ids.append(list(supply_ids))
            or {
                "status": "complete",
                "supply_ids": list(supply_ids),
                "reservation_fulfillment": "canonical",
            }
        )
        before = block.list_supplies({"size_filter": "all", "limit": 100})
        before_rows = {row["wb_supply_id"]: row for row in before["rows"]}
        if before_rows["40422317"]["effective_cost_source"] != "unknown":
            raise AssertionError(f"unknown transit must remain unknown before enrichment: {before_rows['40422317']}")
        if before_rows["50000001"]["effective_cost_total"] != 777.0 or before_rows["50000001"]["effective_cost_source"] != "official_wb_api":
            raise AssertionError(f"official cost must win over Seller Portal cache: {before_rows['50000001']}")

        response = block.start_transit_cost_enrichment(
            {
                "supply_ids": ["40422317", "40422318", "50000001", "60000001"],
                "limit": 10,
                "force": False,
            }
        )
        if response.get("accepted") is not True or response.get("candidate_count") != 2:
            raise AssertionError(f"only missing transit official-cost-null rows must be candidates, got {response}")
        run_id = str(response["run_id"])
        run = _wait_run(block, run_id)
        if (
            run.get("status") != "partial"
            or run.get("success_count") != 1
            or run.get("not_found_count") != 1
            or run.get("processed_count") != 2
        ):
            raise AssertionError(f"mixed transit cost run must preserve partial success, got {run}")
        if reconciled_supply_ids != [["40422317"]]:
            raise AssertionError(
                "successful cost evidence must trigger one bounded canonical "
                f"cost/reservation reconciliation, got {reconciled_supply_ids}"
            )
        after = block.list_supplies({"size_filter": "all", "limit": 100})
        after_rows = {row["wb_supply_id"]: row for row in after["rows"]}
        enriched = after_rows["40422317"]
        if (
            enriched.get("cost_total") is not None
            or enriched.get("effective_cost_total") != 10164.0
            or enriched.get("effective_cost_source") != "seller_portal_browser"
            or enriched.get("seller_portal_transit_cost_display") != "10 164 ₽"
        ):
            raise AssertionError(f"Seller Portal cost must fill only effective fields: {json.dumps(enriched, ensure_ascii=False)}")
        successful_fact = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        missing_fact = runtime.load_wb_supply_transit_cost_enrichment(
            "40422318"
        ) or {}
        if (
            missing_fact.get("status") != "not_found"
            or missing_fact.get("amount") is not None
            or successful_fact.get("status") != "success"
        ):
            raise AssertionError(
                "mixed outcomes must persist successes without converting missing to zero"
            )
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "40422317",
                "amount": 10164.0,
                "currency": "RUB",
                "amount_label": "10 164 ₽",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "high",
                "fetched_at": "2026-06-27T01:00:00Z",
                "status": "success",
                "error": "",
                "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                "created_at": "2026-06-27T01:00:00Z",
                "updated_at": "2026-06-27T01:00:00Z",
            }
        )
        identical_fact = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        if (
            identical_fact.get("source_revision")
            != successful_fact.get("source_revision")
            or identical_fact.get("success_revision")
            != successful_fact.get("success_revision")
        ):
            raise AssertionError(
                "an identical later success must be a T0 canonical fact"
            )
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "40422317",
                "amount": None,
                "currency": "RUB",
                "amount_label": "",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "none",
                "fetched_at": "2026-06-28T00:00:00Z",
                "status": "session_expired",
                "error": "Seller Portal session expired",
                "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                "created_at": "2026-06-28T00:00:00Z",
                "updated_at": "2026-06-28T00:00:00Z",
            }
        )
        preserved = runtime.load_wb_supply_transit_cost_enrichment(
            "40422317"
        ) or {}
        if (
            preserved.get("status") != "success"
            or preserved.get("amount") != successful_fact.get("amount")
            or preserved.get("source_revision")
            != successful_fact.get("source_revision")
            or preserved.get("last_attempt_status") != "session_expired"
            or "expired" not in str(preserved.get("last_attempt_error") or "")
        ):
            raise AssertionError(
                "failed attempt must preserve the last successful transit fact: "
                + json.dumps(preserved, ensure_ascii=False, sort_keys=True)
            )
        attempts = runtime.list_wb_supply_transit_cost_enrichment_attempts(
            supply_id="40422317"
        )
        if [item.get("status") for item in attempts] != [
            "success",
            "success",
            "session_expired",
        ]:
            raise AssertionError(
                "transit attempts must stay append-only and independently auditable"
            )
        displayed = block.list_supplies({"size_filter": "all", "limit": 100})
        displayed_row = {
            row["wb_supply_id"]: row for row in displayed["rows"]
        }["40422317"]
        if (
            displayed_row.get("effective_cost_total") != 10164.0
            or displayed_row.get(
                "seller_portal_transit_cost_last_attempt_status"
            )
            != "session_expired"
        ):
            raise AssertionError(
                "UI row must expose saved amount together with last-attempt error"
            )
        block.timestamp_factory = lambda: "2026-06-29T00:00:00Z"
        retry_ids = {
            str(item.get("supply_id") or "")
            for item in block._select_transit_cost_enrichment_candidates(
                {"supply_ids": ["40422317"], "limit": 10, "force": False}
            )
        }
        if retry_ids != {"40422317"}:
            raise AssertionError(
                "a preserved success with a failed latest attempt must remain retryable"
            )
        runtime.upsert_wb_supply_transit_cost_enrichment(
            {
                "supply_id": "70000001",
                "amount": 0.0,
                "currency": "RUB",
                "amount_label": "0 ₽",
                "is_transit": True,
                "source": "seller_portal_browser",
                "evidence_type": "network_json",
                "confidence": "high",
                "fetched_at": "2026-06-28T01:00:00Z",
                "status": "success",
                "error": "",
                "source_endpoint_path": "/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost",
                "created_at": "2026-06-28T01:00:00Z",
                "updated_at": "2026-06-28T01:00:00Z",
            }
        )
        confirmed_zero = runtime.load_wb_supply_transit_cost_enrichment(
            "70000001"
        ) or {}
        if confirmed_zero.get("status") != "success" or confirmed_zero.get("amount") != 0.0:
            raise AssertionError("confirmed zero must remain distinct from missing/error")
        autonomous = block.collect_all_due_transit_costs(batch_limit=10, max_batches=3)
        coverage = autonomous.get("coverage") or {}
        if (
            autonomous.get("status") != "awaiting_recalculation"
            or coverage.get("eligible") != 2
            or coverage.get("confirmed") != 1
            or coverage.get("pending") != 1
            or coverage.get("waiting_backoff") != 1
            or coverage.get("auth_status") != "valid"
            or coverage.get("route_status") != "available"
            or coverage.get("fresh_confirmed") != 1
            or coverage.get("freshness_status") != "stale"
            or coverage.get("overall_status") != "degraded"
        ):
            raise AssertionError(
                "autonomous global collection must expose layered coverage truth: "
                + json.dumps(autonomous, ensure_ascii=False, sort_keys=True)
            )
        if set(block.transit_cost_source.calls[-1]) != {"40422317", "40422318"}:
            raise AssertionError(
                "autonomous collection must use the global eligible scope, not the visible list"
            )
        runtime.create_wb_supply_transit_cost_enrichment_run(
            run_id="failed-transit-run",
            status="running",
            phase="browser_network_json",
            started_at="2026-06-29T01:00:00Z",
            candidate_count=1,
        )
        runtime.update_wb_supply_transit_cost_enrichment_run(
            "failed-transit-run",
            status="failed",
            phase="collector_failed",
            updated_at="2026-06-29T01:00:01Z",
            completed_at="2026-06-29T01:00:01Z",
            last_error="collector failed after an older success",
        )
        failed_coverage = block.transit_cost_coverage()
        if failed_coverage.get("collector_status") != "error":
            raise AssertionError(
                "an older successful fact must not hide the latest failed collector run"
            )
        runtime.create_wb_supply_transit_cost_enrichment_run(
            run_id="orphaned-transit-run",
            status="running",
            phase="browser_network_json",
            started_at="2026-06-29T00:00:00Z",
            candidate_count=1,
        )
        block.timestamp_factory = lambda: "2026-06-29T03:00:01Z"
        replacement = block.collect_transit_costs({"limit": 10, "force": False})
        orphan = runtime.load_wb_supply_transit_cost_enrichment_run("orphaned-transit-run") or {}
        if (
            orphan.get("status") != "failed"
            or orphan.get("phase") != "orphan_reconciled"
            or replacement.get("status") == "single_flight_joined"
        ):
            raise AssertionError(
                "stale process-owned runs must be reconciled before the next autonomous batch"
            )


def _wait_run(block: WbSuppliesBlock, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = block.get_transit_cost_enrichment_status({"run_id": run_id})
        run = payload.get("run") or {}
        if run.get("status") not in {"queued", "running"}:
            return run
        time.sleep(0.05)
    raise AssertionError("transit cost fake run did not finish")


def _format_rub(amount: float) -> str:
    if abs(amount - round(amount)) < 0.005:
        return f"{int(round(amount)):,}".replace(",", " ") + " ₽"
    integer, fractional = f"{amount:.2f}".split(".")
    return f"{int(integer):,}".replace(",", " ") + f",{fractional} ₽"


if __name__ == "__main__":
    main()
