#!/usr/bin/env python3
"""Legacy read-only transit diagnostic for four WB supplies.

The former apply coupled physical movement to positive transit-cost evidence
and copied the whole SQLite store.  Both contracts are superseded by
``warehouse_cost_unified_recovery.py`` and must not be reachable from this
entrypoint.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.seller_portal_transit_costs import (  # noqa: E402
    SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH,
    SellerPortalTransitCostNetworkJsonSource,
)
from packages.application.our_wb_costs import (  # noqa: E402
    OurWbCostBlock,
    classify_wb_supply_transit,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import WarehouseFunctionalBlock  # noqa: E402
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    _sqlite_backup as create_verified_sqlite_backup,
    restore_verified_supplier_backup,
)


TARGET_SUPPLY_IDS = ("41058085", "41058204", "41058408", "41058611")
EXPECTED_RESERVATION_TOTALS = {
    "41058085": 5750.0,
    "41058204": 6250.0,
    "41058408": 14000.0,
    "41058611": 17000.0,
}
EXPECTED_TOTAL_QUANTITY = 43000.0
EXPECTED_PHYSICAL_BEFORE = 74500.0
EXPECTED_PHYSICAL_AFTER = 31500.0
DISCREPANCY_SUPPLY_ID = "40985996"
AUDIT_TABLE = "sheet_vitrina_v1_ff_transit_reservation_recovery_audit"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fingerprint", default="")
    parser.add_argument("--backup-dir", default="")
    args = parser.parse_args(argv)
    if args.apply:
        raise ValueError(
            "legacy transit/reservation apply is disabled; use "
            "apps/warehouse_cost_unified_recovery.py with its exact dry-run fingerprint"
        )
    print(
        json.dumps(
            {
                "mode": "diagnostic",
                "would_change": False,
                "legacy_apply_disabled": True,
                "canonical_runner": "apps/warehouse_cost_unified_recovery.py",
                "reason": (
                    "legacy full-database snapshot planning is disabled; "
                    "run the canonical query-only targeted dry-run"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def build_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    fetch_evidence: bool,
) -> dict[str, Any]:
    """Build the dry-run exclusively on one query-only coherent DB snapshot."""

    with TemporaryDirectory(prefix="ff-transit-recovery-plan-") as temp_dir:
        snapshot_dir = Path(temp_dir) / "runtime"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        _readonly_sqlite_copy(
            runtime.db_path,
            snapshot_dir / "registry_upload_runtime.sqlite3",
        )
        snapshot = RegistryUploadDbBackedRuntime(runtime_dir=snapshot_dir)
        return _build_plan_on_snapshot(
            snapshot,
            evidence_runtime_dir=runtime.runtime_dir,
            fetch_evidence=fetch_evidence,
        )


def _build_plan_on_snapshot(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    evidence_runtime_dir: Path,
    fetch_evidence: bool,
) -> dict[str, Any]:
    records = _records_by_supply(runtime)
    missing_records = sorted(set(TARGET_SUPPLY_IDS) - set(records))
    if missing_records:
        raise ValueError("target WB supplies are missing: " + ",".join(missing_records))
    reservations = [
        dict(item)
        for supply_id in TARGET_SUPPLY_IDS
        for item in runtime.list_ff_stock_reservations(supply_id=supply_id)
    ]
    reservation_totals = {
        supply_id: sum(
            float(item.get("quantity") or 0)
            for item in reservations
            if str(item.get("supply_id") or "") == supply_id
        )
        for supply_id in TARGET_SUPPLY_IDS
    }
    total_reserved = sum(reservation_totals.values())
    already_fulfilled = total_reserved == 0
    if not already_fulfilled and abs(total_reserved - EXPECTED_TOTAL_QUANTITY) > 1e-9:
        raise ValueError(
            f"target reservation total differs from approved 43,000: {total_reserved}"
        )
    if not already_fulfilled and any(
        abs(reservation_totals[supply_id] - expected) > 1e-9
        for supply_id, expected in EXPECTED_RESERVATION_TOTALS.items()
    ):
        raise ValueError(
            "per-supply reservation quantities differ from the approved exact targets"
        )
    compositions = {
        supply_id: _composition(records[supply_id]) for supply_id in TARGET_SUPPLY_IDS
    }
    if not already_fulfilled:
        for supply_id in TARGET_SUPPLY_IDS:
            reserved_by_nm = {
                int(item.get("nm_id") or 0): float(item.get("quantity") or 0)
                for item in reservations
                if str(item.get("supply_id") or "") == supply_id
            }
            packed_by_nm = {
                int(item["nm_id"]): float(item["packed_quantity"])
                for item in compositions[supply_id]
            }
            if reserved_by_nm != packed_by_nm:
                raise ValueError(
                    f"reservation composition drifted for supply {supply_id}"
                )

    existing = {
        str(item.get("supply_id") or ""): dict(item)
        for item in runtime.list_wb_supply_transit_cost_enrichments()
        if str(item.get("supply_id") or "") in TARGET_SUPPLY_IDS
        and str(item.get("status") or "") == "success"
        and float(item.get("amount") or 0) > 0
    }
    evidence: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for supply_id in TARGET_SUPPLY_IDS:
        official = _official_transit_evidence(records[supply_id])
        if official is not None:
            evidence.append(official)
        elif supply_id in existing:
            evidence.append(
                {
                    **existing[supply_id],
                    "publish_enrichment": True,
                }
            )
        else:
            normalized = dict(records[supply_id].get("normalized") or {})
            candidates.append(
                {
                    "supply_id": supply_id,
                    "warehouse_display": str(
                        normalized.get("warehouse_display") or ""
                    ),
                    "supply_date": str(normalized.get("supply_date") or ""),
                }
            )
    if candidates and fetch_evidence:
        fetched_at = _now()
        try:
            evidence.extend(
                {
                    **dict(item),
                    "publish_enrichment": True,
                }
                for item in SellerPortalTransitCostNetworkJsonSource().fetch_costs(
                    candidates,
                    run_id="dryrun_ff_transit_" + hashlib.sha256(
                        _source_digest(runtime).encode()
                    ).hexdigest()[:16],
                    runtime_dir=evidence_runtime_dir,
                    fetched_at=fetched_at,
                )
            )
        except Exception as exc:  # noqa: BLE001 - dry-run reports a truthful evidence blocker.
            evidence.extend(
                {
                    "supply_id": str(candidate["supply_id"]),
                    "amount": None,
                    "currency": "RUB",
                    "status": "evidence_unavailable",
                    "confidence": "none",
                    "source": "seller_portal_network_json",
                    "evidence_type": "authenticated_read_only_browser_network_json",
                    "fetched_at": fetched_at,
                    "error": str(exc),
                    "publish_enrichment": True,
                }
                for candidate in candidates
            )
    evidence_by_supply = {
        str(item.get("supply_id") or ""): dict(item) for item in evidence
    }
    evidence_observations = [
        _evidence_observation(
            supply_id,
            evidence_by_supply.get(supply_id, {}),
        )
        for supply_id in TARGET_SUPPLY_IDS
    ]
    evidence_projection = [
        _stable_evidence(item) for item in evidence_observations
    ]
    evidence_complete = all(
        _canonical_positive_evidence(item)
        for item in evidence_projection
    )
    physical = _physical_state(runtime)
    if (
        not already_fulfilled
        and abs(float(physical.get("total") or 0) - EXPECTED_PHYSICAL_BEFORE)
        > 1e-9
    ):
        raise ValueError(
            "physical FF total differs from the approved exact 74,500 pre-state"
        )
    discrepancy = _discrepancy_projection(records.get(DISCREPANCY_SUPPLY_ID))
    if discrepancy and (
        abs(float(discrepancy.get("underaccepted_total") or 0) - 253.0) > 1e-9
        or abs(float(discrepancy.get("overaccepted_total") or 0) - 249.0) > 1e-9
    ):
        raise ValueError("40985996 SKU-level discrepancy differs from 253/+249")
    source_digest = _source_digest(runtime)
    non_target_digest = _non_target_reservation_digest(runtime)
    planned_at = _stable_business_timestamp()
    candidate: dict[str, Any] | None = None
    candidate_error = ""
    if evidence_complete and not already_fulfilled:
        try:
            candidate = _candidate_recovery_projection(
                runtime,
                evidence=evidence_projection,
                planned_at=planned_at,
                physical_before=physical,
                non_target_reservation_digest=non_target_digest,
                discrepancy_before=discrepancy,
            )
        except Exception as exc:  # noqa: BLE001 - fail-closed dry-run diagnostic.
            candidate_error = f"{exc.__class__.__name__}: {exc}"
    material = {
        "contract_name": "ff_transit_reservation_recovery_plan_v1",
        "target_supply_ids": list(TARGET_SUPPLY_IDS),
        "reservation_totals": reservation_totals,
        "reservation_total": total_reserved,
        "compositions": compositions,
        "transit_cost_evidence": evidence_projection,
        "evidence_observations": evidence_observations,
        "physical_before": physical,
        "expected_physical_delta": (
            0.0 if already_fulfilled else -EXPECTED_TOTAL_QUANTITY
        ),
        "discrepancy_40985996": discrepancy,
        "source_digest": source_digest,
        "non_target_reservation_digest": non_target_digest,
        "planned_at": planned_at,
        "candidate": candidate,
        "candidate_error": candidate_error,
    }
    fingerprint_material = {
        key: value
        for key, value in material.items()
        if key != "evidence_observations"
    }
    apply_allowed = bool(
        evidence_complete
        and not already_fulfilled
        and candidate is not None
        and not candidate_error
    )
    return {
        **material,
        "mode": "dry_run",
        "already_fulfilled": already_fulfilled,
        "apply_allowed": apply_allowed,
        "would_change": apply_allowed,
        "fingerprint": "sha256:" + _hash(fingerprint_material),
    }


def apply_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    del runtime, plan, backup_root
    raise ValueError(
        "legacy transit/reservation apply is disabled; use "
        "apps/warehouse_cost_unified_recovery.py with its exact dry-run fingerprint"
    )


def _apply_plan_locked(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    backup_root: Path,
) -> dict[str, Any]:
    if _source_digest(runtime) != str(plan.get("source_digest") or ""):
        raise ValueError("transit reservation source changed after dry-run")
    if _non_target_reservation_digest(runtime) != str(
        plan.get("non_target_reservation_digest") or ""
    ):
        raise ValueError("non-target reservations changed after dry-run")
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = backup_root / f"registry_upload.ff-transit.{stamp}.sqlite3"
    backup = create_verified_sqlite_backup(runtime.db_path, backup_path)
    applied_at = str(plan.get("planned_at") or "")
    try:
        for evidence in plan.get("transit_cost_evidence") or []:
            if not evidence.get("publish_enrichment"):
                continue
            runtime.upsert_wb_supply_transit_cost_enrichment(
                _stored_evidence(evidence, timestamp=applied_at)
            )
        cost_block = OurWbCostBlock(
            runtime=runtime,
            timestamp_factory=lambda: applied_at,
        )
        cost_layers = cost_block.materialize_wb_supply_cost_layers(
            opening_date="2026-07-01"
        )
        supplies = WbSuppliesBlock(
            runtime=runtime,
            timestamp_factory=lambda: applied_at,
        )
        first_reconciliation = supplies.reconcile_functional_ff_state()
        second_reconciliation = supplies.reconcile_functional_ff_state()
        post = _validate_reconciled_state(
            runtime,
            physical_before=dict(plan.get("physical_before") or {}),
            non_target_reservation_digest=str(
                plan.get("non_target_reservation_digest") or ""
            ),
            discrepancy_before=dict(plan.get("discrepancy_40985996") or {}),
            first_reconciliation=first_reconciliation,
            second_reconciliation=second_reconciliation,
        )
        functional = WarehouseFunctionalBlock(
            runtime=runtime,
            timestamp_factory=lambda: applied_at,
        )
        functional_plan = functional.build_emergency_rebuild_plan()
        expected_functional_fingerprint = str(
            dict(plan.get("candidate") or {})
            .get("functional_publication", {})
            .get("plan_fingerprint")
            or ""
        )
        if (
            str(functional_plan.get("plan_fingerprint") or "")
            != expected_functional_fingerprint
        ):
            raise ValueError(
                "functional publication differs from approved candidate"
            )
        functional_result = functional._apply_plan_locked(  # noqa: SLF001
            functional_plan,
            confirm_fingerprint=str(functional_plan["plan_fingerprint"]),
            backup_dir=backup_root,
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {AUDIT_TABLE}(
                    recovery_id TEXT PRIMARY KEY,
                    plan_fingerprint TEXT NOT NULL UNIQUE,
                    applied_at TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    report_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                f"""INSERT INTO {AUDIT_TABLE}(
                        recovery_id,plan_fingerprint,applied_at,backup_path,report_json
                    ) VALUES(?,?,?,?,?)""",
                (
                    "fftr_" + str(plan["fingerprint"]).split(":", 1)[-1][:24],
                    str(plan["fingerprint"]),
                    applied_at,
                    str(backup_path),
                    json.dumps(dict(plan), ensure_ascii=False, sort_keys=True),
                ),
            )
            conn.commit()
        repeat = build_plan(runtime, fetch_evidence=False)
        if not repeat.get("already_fulfilled") or repeat.get("would_change"):
            raise ValueError("repeat recovery dry-run is not a no-op")
    except Exception:
        restore_verified_supplier_backup(backup_path, runtime.db_path)
        raise
    return {
        **dict(plan),
        "mode": "apply",
        "applied": True,
        "backup": backup,
        "cost_layers_materialized": cost_layers,
        "first_reconciliation": first_reconciliation,
        "repeat_reconciliation": second_reconciliation,
        "functional_publication": functional_result,
        "physical_after": post["physical_after"],
        "remaining_target_reservations": post[
            "remaining_target_reservations"
        ],
        "post_apply": {
            **post,
            "repeat_plan_fingerprint": repeat.get("fingerprint"),
            "repeat_no_op": True,
        },
    }


def _candidate_recovery_projection(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    evidence: list[Mapping[str, Any]],
    planned_at: str,
    physical_before: Mapping[str, Any],
    non_target_reservation_digest: str,
    discrepancy_before: Mapping[str, Any],
) -> dict[str, Any]:
    """Simulate evidence publication, atomic fulfillment, and functional replay."""

    for item in evidence:
        if not item.get("publish_enrichment"):
            continue
        runtime.upsert_wb_supply_transit_cost_enrichment(
            _stored_evidence(item, timestamp=planned_at)
        )
    cost_layers_materialized = OurWbCostBlock(
        runtime=runtime,
        timestamp_factory=lambda: planned_at,
    ).materialize_wb_supply_cost_layers(opening_date="2026-07-01")
    cost_layers = _target_cost_layers(runtime)
    _validate_target_cost_layers(runtime, cost_layers)
    supplies = WbSuppliesBlock(
        runtime=runtime,
        timestamp_factory=lambda: planned_at,
    )
    first = supplies.reconcile_functional_ff_state()
    second = supplies.reconcile_functional_ff_state()
    post = _validate_reconciled_state(
        runtime,
        physical_before=physical_before,
        non_target_reservation_digest=non_target_reservation_digest,
        discrepancy_before=discrepancy_before,
        first_reconciliation=first,
        second_reconciliation=second,
    )
    functional = WarehouseFunctionalBlock(
        runtime=runtime,
        timestamp_factory=lambda: planned_at,
    )
    functional_plan = functional.build_emergency_rebuild_plan()
    invariants = dict(functional_plan.get("invariants") or {})
    if (
        int(invariants.get("negative_balance_count") or 0) != 0
        or int(invariants.get("positive_cost_gap_count") or 0) != 0
    ):
        raise ValueError(
            "candidate functional version contains negative capital or cost gaps"
        )
    records = _records_by_supply(runtime)
    allowed_nm_ids = {
        int(item["nm_id"])
        for supply_id in TARGET_SUPPLY_IDS
        for item in _composition(records[supply_id])
    }
    diff_rows = list(dict(functional_plan.get("diff") or {}).get("lines") or [])
    if any(
        int(item.get("nm_id") or 0) not in allowed_nm_ids
        for item in diff_rows
    ):
        raise ValueError(
            "candidate functional diff changes an unrelated SKU"
        )
    return {
        "cost_layers_materialized": int(cost_layers_materialized),
        "target_cost_layers": cost_layers,
        "first_reconciliation": _reconciliation_projection(first),
        "repeat_reconciliation": _reconciliation_projection(second),
        "post_state": post,
        "functional_publication": {
            "plan_fingerprint": functional_plan.get("plan_fingerprint"),
            "base_active_version_id": functional_plan.get(
                "base_active_version_id"
            ),
            "effective_date": functional_plan.get("effective_date"),
            "local_source_digest": functional_plan.get("local_source_digest"),
            "wb_supply_source_digest": functional_plan.get(
                "wb_supply_source_digest"
            ),
            "calculation_digest": functional_plan.get("calculation_digest"),
            "diff": functional_plan.get("diff"),
            "invariants": invariants,
            "target_stage_rows": _target_stage_rows(functional_plan),
        },
    }


def _validate_reconciled_state(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    physical_before: Mapping[str, Any],
    non_target_reservation_digest: str,
    discrepancy_before: Mapping[str, Any],
    first_reconciliation: Mapping[str, Any],
    second_reconciliation: Mapping[str, Any],
) -> dict[str, Any]:
    remaining = [
        dict(item)
        for supply_id in TARGET_SUPPLY_IDS
        for item in runtime.list_ff_stock_reservations(supply_id=supply_id)
    ]
    if remaining:
        raise ValueError(
            "target FF reservations remain after canonical reconciliation"
        )
    first_debits = dict(first_reconciliation.get("ff_stock_debits") or {})
    second_debits = dict(second_reconciliation.get("ff_stock_debits") or {})
    if int(first_debits.get("created_count") or 0) != len(TARGET_SUPPLY_IDS):
        raise ValueError(
            "candidate did not create exactly one physical debit per target supply"
        )
    if int(second_debits.get("created_count") or 0) != 0:
        raise ValueError("repeat FF reconciliation was not a no-op")
    physical_after = _physical_state(runtime)
    before_total = float(physical_before.get("total") or 0)
    after_total = float(physical_after.get("total") or 0)
    if (
        abs(before_total - EXPECTED_PHYSICAL_BEFORE) > 1e-9
        or abs(after_total - EXPECTED_PHYSICAL_AFTER) > 1e-9
        or abs(after_total - before_total + EXPECTED_TOTAL_QUANTITY) > 1e-9
    ):
        raise ValueError(
            "physical FF transition differs from exact 74,500 → 31,500"
        )
    if any(
        float(item.get("balance") or 0) < -1e-9
        for item in physical_after["rows"]
    ):
        raise ValueError("negative FF quantity after reservation recovery")
    if _non_target_reservation_digest(runtime) != non_target_reservation_digest:
        raise ValueError("non-target reservation invariant changed")
    records = _records_by_supply(runtime)
    discrepancy_after = _discrepancy_projection(
        records.get(DISCREPANCY_SUPPLY_ID)
    )
    if discrepancy_after != dict(discrepancy_before):
        raise ValueError("40985996 SKU-level discrepancy changed")
    return {
        "physical_after": physical_after,
        "remaining_target_reservations": remaining,
        "non_target_reservation_digest": non_target_reservation_digest,
        "discrepancy_40985996": discrepancy_after,
        "created_physical_debit_count": int(
            first_debits.get("created_count") or 0
        ),
        "repeat_created_physical_debit_count": int(
            second_debits.get("created_count") or 0
        ),
    }


def _target_cost_layers(
    runtime: RegistryUploadDbBackedRuntime,
) -> list[dict[str, Any]]:
    return [
        {
            key: item.get(key)
            for key in (
                "wb_supply_id",
                "nm_id",
                "qty_denominator",
                "accepted_qty",
                "transit_amount_total",
                "transit_per_unit_rub",
                "transit_cost_status",
                "sku_ff_unit_cost_rub",
                "pre_acceptance_unit_cost_rub",
                "source_status",
                "inputs_hash",
            )
        }
        for item in runtime.list_current_wb_supply_cost_layers(
            supply_ids=TARGET_SUPPLY_IDS
        )
    ]


def _validate_target_cost_layers(
    runtime: RegistryUploadDbBackedRuntime,
    rows: list[Mapping[str, Any]],
) -> None:
    expected = {
        (supply_id, int(item["nm_id"]))
        for supply_id, record in _records_by_supply(runtime).items()
        if supply_id in TARGET_SUPPLY_IDS
        for item in _composition(record)
    }
    actual = {
        (str(item.get("wb_supply_id") or ""), int(item.get("nm_id") or 0))
        for item in rows
    }
    if actual != expected:
        raise ValueError("target downstream cost layer composition is incomplete")
    for item in rows:
        if (
            str(item.get("transit_cost_status") or "")
            != "transit_confirmed"
            or float(item.get("transit_amount_total") or 0) <= 0
            or float(item.get("pre_acceptance_unit_cost_rub") or 0) <= 0
            or float(item.get("sku_ff_unit_cost_rub") or 0) <= 0
            or not str(item.get("inputs_hash") or "")
        ):
            raise ValueError(
                "target downstream cost layer lacks confirmed positive evidence"
            )


def _reconciliation_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    debits = dict(value.get("ff_stock_debits") or {})
    return {
        "checkpoint_id": str(
            dict(value.get("checkpoint") or {}).get("checkpoint_id") or ""
        ),
        "created_count": int(debits.get("created_count") or 0),
        "created_operation_ids": sorted(
            str(item) for item in debits.get("created_operation_ids") or []
        ),
        "skipped_count": int(debits.get("skipped_count") or 0),
        "skipped_reasons": dict(debits.get("skipped_reasons") or {}),
        "reservation_release_count": int(
            debits.get("reservation_release_count") or 0
        ),
        "reservation_summary": dict(debits.get("reservation_summary") or {}),
    }


def _target_stage_rows(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    target_ids = set(TARGET_SUPPLY_IDS)
    result: list[dict[str, Any]] = []
    for item in plan.get("lines") or []:
        provenance = json.dumps(
            item.get("provenance"),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        matched = sorted(
            supply_id for supply_id in target_ids if supply_id in provenance
        )
        if matched:
            result.append(
                {
                    "warehouse_key": item.get("warehouse_key"),
                    "nm_id": item.get("nm_id"),
                    "quantity": item.get("quantity"),
                    "capital_rub": item.get("capital_rub"),
                    "supply_ids": matched,
                }
            )
    return result


def _evidence_observation(
    supply_id: str,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        key: (
            supply_id
            if key == "supply_id"
            else value.get(key)
        )
        for key in (
            "supply_id",
            "amount",
            "currency",
            "status",
            "confidence",
            "source",
            "evidence_type",
            "source_endpoint_path",
            "source_field",
            "tariff_id",
            "box_amount",
            "number_of_pallets",
            "fetched_at",
            "error",
            "publish_enrichment",
        )
    }


def _stable_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude observation time/error text from the approved business fact."""

    return {
        key: value.get(key)
        for key in (
            "supply_id",
            "amount",
            "currency",
            "status",
            "confidence",
            "source",
            "evidence_type",
            "source_endpoint_path",
            "source_field",
            "tariff_id",
            "box_amount",
            "number_of_pallets",
            "publish_enrichment",
        )
    }


def _canonical_positive_evidence(value: Mapping[str, Any]) -> bool:
    if (
        str(value.get("status") or "") != "success"
        or float(value.get("amount") or 0) <= 0
        or str(value.get("currency") or "RUB").upper() != "RUB"
        or str(value.get("confidence") or "") not in {"high", "medium"}
        or not str(value.get("source") or "")
        or not str(value.get("evidence_type") or "")
    ):
        return False
    source = str(value.get("source") or "")
    evidence_type = str(value.get("evidence_type") or "")
    if source == "official_wb_supply_fact":
        return (
            evidence_type == "canonical_transit_cost_contract"
            and not value.get("publish_enrichment")
        )
    if source == "seller_portal_browser":
        return (
            evidence_type == "network_json"
            and str(value.get("source_endpoint_path") or "")
            == SELLER_PORTAL_SUPPLY_COST_ENDPOINT_PATH
            and value.get("publish_enrichment") is True
        )
    return False


def _official_transit_evidence(
    record: Mapping[str, Any],
) -> dict[str, Any] | None:
    normalized = dict(record.get("normalized") or {})
    denominator = sum(
        float(item.get("packed_quantity") or 0)
        for item in _composition(record)
    )
    classification = classify_wb_supply_transit(
        normalized,
        denominator=denominator,
    )
    if (
        classification.status != "transit_confirmed"
        or float(classification.amount_total or 0) <= 0
    ):
        return None
    return {
        "supply_id": str(
            normalized.get("supply_id")
            or normalized.get("wb_supply_id")
            or record.get("supply_id")
            or ""
        ),
        "amount": float(classification.amount_total or 0),
        "currency": "RUB",
        "status": "success",
        "confidence": "high",
        "source": "official_wb_supply_fact",
        "evidence_type": "canonical_transit_cost_contract",
        "source_endpoint_path": "",
        "source_field": str(classification.evidence or ""),
        "publish_enrichment": False,
    }


def _stored_evidence(
    value: Mapping[str, Any],
    *,
    timestamp: str,
) -> dict[str, Any]:
    amount = float(value.get("amount") or 0)
    return {
        **dict(value),
        "amount_label": f"{amount:,.2f} ₽".replace(",", " "),
        "is_transit": True,
        "fetched_at": timestamp,
        "error": "",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _records_by_supply(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in runtime.list_wb_supplies_cache_records():
        normalized = dict(record.get("normalized") or {})
        supply_id = str(
            normalized.get("supply_id")
            or normalized.get("wb_supply_id")
            or record.get("supply_id")
            or ""
        )
        if supply_id:
            result[supply_id] = dict(record)
    return result


def _composition(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    goods = record.get("raw_goods")
    if not isinstance(goods, list):
        goods = json.loads(str(dict(record.get("normalized") or {}).get("raw_goods_json") or "[]"))
    rows: list[dict[str, Any]] = []
    for item in goods or []:
        if not isinstance(item, Mapping):
            continue
        nm_id = int(item.get("nmID") or item.get("nmId") or item.get("nm_id") or 0)
        packed = float(
            item.get("quantity")
            or item.get("packedQuantity")
            or item.get("packed_quantity")
            or 0
        )
        accepted = float(
            item.get("acceptedQuantity")
            or item.get("accepted_quantity")
            or 0
        )
        if nm_id > 0 and packed > 0:
            rows.append(
                {
                    "nm_id": nm_id,
                    "packed_quantity": packed,
                    "accepted_quantity": accepted,
                }
            )
    return sorted(rows, key=lambda item: item["nm_id"])


def _physical_state(runtime: RegistryUploadDbBackedRuntime) -> dict[str, Any]:
    rows = [
        {"nm_id": int(item.get("nm_id") or 0), "balance": float(item.get("balance") or 0)}
        for item in runtime.list_ff_stock_balances()
    ]
    return {"total": sum(item["balance"] for item in rows), "rows": rows}


def _discrepancy_projection(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    rows = _composition(record)
    differences = [
        {
            "nm_id": item["nm_id"],
            "difference": item["accepted_quantity"] - item["packed_quantity"],
        }
        for item in rows
        if abs(item["accepted_quantity"] - item["packed_quantity"]) > 1e-9
    ]
    return {
        "supply_id": DISCREPANCY_SUPPLY_ID,
        "rows": differences,
        "underaccepted_total": sum(-item["difference"] for item in differences if item["difference"] < 0),
        "overaccepted_total": sum(item["difference"] for item in differences if item["difference"] > 0),
        "netting_forbidden": True,
    }


def _source_digest(runtime: RegistryUploadDbBackedRuntime) -> str:
    records = _records_by_supply(runtime)
    material = {
        "supplies": {
            supply_id: {
                "normalized": records.get(supply_id, {}).get("normalized"),
                "composition": _composition(records[supply_id]) if supply_id in records else [],
            }
            for supply_id in (*TARGET_SUPPLY_IDS, DISCREPANCY_SUPPLY_ID)
        },
        "reservations": [
            item
            for supply_id in TARGET_SUPPLY_IDS
            for item in runtime.list_ff_stock_reservations(supply_id=supply_id)
        ],
        "physical": _physical_state(runtime),
    }
    return "sha256:" + _hash(material)


def _non_target_reservation_digest(runtime: RegistryUploadDbBackedRuntime) -> str:
    return "sha256:" + _hash(
        [
            item
            for item in runtime.list_ff_stock_reservations()
            if str(item.get("supply_id") or "") not in TARGET_SUPPLY_IDS
        ]
    )


def _readonly_sqlite_copy(source: Path, target: Path) -> None:
    """Copy the current database through one mode=ro/query_only snapshot."""

    if target.exists():
        raise ValueError(f"snapshot target already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True) as source_conn,
        sqlite3.connect(target) as target_conn,
    ):
        source_conn.execute("PRAGMA query_only=ON")
        source_conn.backup(target_conn)
        target_conn.commit()
    with sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True) as check:
        check.execute("PRAGMA query_only=ON")
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
    if target.stat().st_size <= 0 or integrity.lower() != "ok":
        target.unlink(missing_ok=True)
        raise ValueError("read-only SQLite snapshot failed integrity_check")


def _stable_business_timestamp() -> str:
    current = datetime.now(ZoneInfo("Asia/Yekaterinburg"))
    return current.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
