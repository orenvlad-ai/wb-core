"""Bounded July 2026 warehouse/product-capital history recovery.

The runner reconstructs eleven immutable functional business-date versions from
current canonical source evidence.  It never edits supplier, CNY, financial,
FF-ledger, WB-supply, or raw-WB records.  A dry-run is the default; apply is
accepted only for the exact reviewed fingerprint and is protected by a T1
target-scoped undo journal.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_business_projection import (
    CURRENT_ROW_TABLE,
    REVISION_TABLE,
    ROW_TABLE,
    STATE_TABLE,
    _metric_rows,
    _persist_projection_revision,
)
from packages.application.warehouse_functional import (
    FUNCTIONAL_CUTOVER_ID,
    _functional_local_source_view,
    _source_rows,
    _supplier_cost_allocations,
    _supply_revisions,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
)
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (
    _data_sheet,
    _date_columns,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.warehouse_recovery_policy import (
    RecoveryState,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)


CONTRACT_NAME = "warehouse_historical_recovery_2026_07_v2"
DATE_FROM = "2026-07-19"
DATE_TO = "2026-07-29"
DATES = tuple(f"2026-07-{day:02d}" for day in range(19, 30))
TARGET_SHIPMENTS = {
    "26GN390": "sup_b3070385b00b4eb680bd805d751d65be",
    "26GN462": "sup_de634b58323544c487183c3108f3cbfd",
    "26GN527": "sup_adc29a3cba934403bca4842c2add8b7d",
    "26GN582": "sup_eb8f1541b9594d168d689d5cff7e81d0",
    "26GN583": "sup_35a64348998a47de895ea225a6aeed71",
}
TARGET_SUPPLIES = (
    "40985996",
    "41058085",
    "41058204",
    "41058408",
    "41058611",
)
SUPPLY_BUSINESS_DATES = {
    "40985996": "2026-07-21",
    "41058085": "2026-07-23",
    "41058204": "2026-07-23",
    "41058408": "2026-07-23",
    "41058611": "2026-07-23",
}
EXPECTED_SHIPMENT_QUANTITIES = {
    "26GN390": Decimal("80250"),
    "26GN462": Decimal("55250"),
    "26GN527": Decimal("66000"),
    "26GN582": Decimal("53750"),
    "26GN583": Decimal("79400"),
}
EXPECTED_SUPPLY_QUANTITIES = {
    "40985996": Decimal("12500"),
    "41058085": Decimal("5750"),
    "41058204": Decimal("6250"),
    "41058408": Decimal("14000"),
    "41058611": Decimal("17000"),
}
EXPECTED_CHANGED_PAIRS_BY_DATE = {
    "2026-07-19": 32,
    "2026-07-20": 65,
    "2026-07-21": 68,
    "2026-07-22": 68,
    "2026-07-23": 68,
    "2026-07-24": 68,
    "2026-07-25": 62,
    "2026-07-26": 15,
    "2026-07-27": 20,
    "2026-07-28": 20,
    "2026-07-29": 29,
}
EXPECTED_NET_CAPITAL_DELTA_BY_DATE = {
    "2026-07-19": Decimal("1383345.89"),
    "2026-07-20": Decimal("4914251.63"),
    "2026-07-21": Decimal("4989997.60"),
    "2026-07-22": Decimal("4989997.60"),
    "2026-07-23": Decimal("4426626.49812802756846262368"),
    "2026-07-24": Decimal("105403.96987895065575149424"),
    "2026-07-25": Decimal("32348.23269606847126204224"),
    "2026-07-26": Decimal("109.00269606847126204219"),
    "2026-07-27": Decimal("109.00269606847126204219"),
    "2026-07-28": Decimal("109.00269606847126204219"),
    "2026-07-29": Decimal("1554447.48540919814537188807"),
}
EXPECTED_NET_QUANTITY_DELTA_BY_DATE = {
    "2026-07-19": Decimal("0"),
    "2026-07-20": Decimal("79400"),
    "2026-07-21": Decimal("79400"),
    "2026-07-22": Decimal("79400"),
    "2026-07-23": Decimal("66904"),
    "2026-07-24": Decimal("-249"),
    "2026-07-25": Decimal("1"),
    "2026-07-26": Decimal("1"),
    "2026-07-27": Decimal("1"),
    "2026-07-28": Decimal("1"),
    "2026-07-29": Decimal("13997"),
}
EXPECTED_TARGET_NM_ID_COUNT = 68
EXPECTED_CHANGED_PAIR_COUNT = 515
TARGET_STAGES = (
    "production",
    "china_to_ff",
    "ff",
    "ff_to_wb",
    "wb",
    "wb_acceptance_discrepancy",
)
SUPPLIER_STAGES = ("production", "china_to_ff", "ff")
ZERO = Decimal("0")
TOLERANCE = Decimal("0.02")


class WarehouseHistoricalRecoveryError(RuntimeError):
    """Fail-closed historical-recovery contract violation."""


def build_historical_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, Any]:
    """Build an exact query-only manifest from current production evidence."""

    with _connect(runtime.db_path, read_only=True) as conn:
        sources = _functional_local_source_view(
            _source_rows(conn, recovery_end_date=DATE_TO)
        )
        allocations = _supplier_cost_allocations(sources)
        shipment_manifest = _shipment_manifest(
            conn,
            allocations=allocations,
            sources=sources,
        )
        supply_manifest = _supply_manifest(conn, sources=sources)
        target_nm_ids = sorted(
            {
                int(line["nm_id"])
                for item in shipment_manifest.values()
                for line in item["lines"]
            }
            | {
                int(nm_id)
                for item in supply_manifest.values()
                for nm_id in item["composition"]
            }
        )
        if len(target_nm_ids) != EXPECTED_TARGET_NM_ID_COUNT:
            raise WarehouseHistoricalRecoveryError(
                "current source closure no longer contains exactly 68 target nmID"
            )
        base_versions = _base_versions(conn)
        wb_daily = _wb_daily_rows(conn, target_nm_ids=target_nm_ids)
        discrepancy_rows = _current_409_discrepancy_rows(conn)
        opening_rows = _balance_rows(
            conn, str(base_versions[DATE_FROM]["version_id"])
        )
        corrected_by_date: dict[str, list[dict[str, Any]]] = {}
        changed_rows: list[dict[str, Any]] = []
        version_manifest: list[dict[str, Any]] = []
        for business_date in DATES:
            base = base_versions[business_date]
            before = _balance_rows(conn, str(base["version_id"]))
            after = _correct_balances(
                _reconstruction_base(
                    opening_rows,
                    exact_date_rows=before,
                    target_nm_ids=target_nm_ids,
                ),
                business_date=business_date,
                shipment_manifest=shipment_manifest,
                supply_manifest=supply_manifest,
                wb_daily=wb_daily.get(business_date, {}),
                discrepancy_rows=discrepancy_rows,
                target_nm_ids=target_nm_ids,
            )
            corrected_by_date[business_date] = after
            changed = _changed_target_rows(
                before,
                after,
                business_date=business_date,
                target_nm_ids=target_nm_ids,
            )
            changed_rows.extend(changed)
            version_manifest.append(
                {
                    "business_date": business_date,
                    "base_version_id": str(base["version_id"]),
                    "base_plan_fingerprint": str(base["plan_fingerprint"]),
                    "base_snapshot_id": str(base["snapshot_id"]),
                    "base_snapshot_digest": str(base["raw_rows_digest"]),
                    "before_target_digest": _target_balance_digest(
                        before, target_nm_ids
                    ),
                    "after_target_digest": _target_balance_digest(
                        after, target_nm_ids
                    ),
                    "before_total": _balance_total(before, target_nm_ids),
                    "after_total": _balance_total(after, target_nm_ids),
                    "changed_pair_count": len(
                        {int(item["nm_id"]) for item in changed}
                    ),
                }
            )
        _validate_changed_scope(
            changed_rows=changed_rows,
            version_manifest=version_manifest,
        )
        configured_nm_ids = _configured_nm_ids(conn)
        ready_pairs = sorted(
            {
                (str(item["business_date"]), int(item["nm_id"]))
                for item in changed_rows
                if int(item["nm_id"]) in configured_nm_ids
            }
        )
        active = dict(
            conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active "
                "WHERE slot=1"
            ).fetchone()
        )
        ready_snapshots = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ready_snapshots "
                "ORDER BY bundle_version,as_of_date"
            ).fetchall()
        ]
        source_material = {
            "contract_name": CONTRACT_NAME,
            "date_from": DATE_FROM,
            "date_to": DATE_TO,
            "shipment_sources": {
                invoice_no: {
                    key: item[key]
                    for key in (
                        "shipment_id",
                        "source_fingerprint",
                        "calculation_fingerprint",
                        "first_payment_date",
                        "actual_shipment_date",
                        "actual_ff_acceptance_date",
                        "event_digest",
                    )
                }
                for invoice_no, item in shipment_manifest.items()
            },
            "supply_sources": {
                supply_id: {
                    key: item[key]
                    for key in (
                        "operation_id",
                        "source_revision",
                        "source_timestamp",
                        "operation_digest",
                        "business_date",
                    )
                }
                for supply_id, item in supply_manifest.items()
            },
            "base_versions": version_manifest,
            "wb_daily_digest": _fingerprint(wb_daily),
            "configured_nm_ids": sorted(configured_nm_ids),
        }
        source_digest = _fingerprint(source_material)
        version_ids = {
            day: _stable_id("whfv_hist", {"source": source_digest, "date": day})
            for day in DATES
        }
        for item in version_manifest:
            day = str(item["business_date"])
            item["version_id"] = version_ids[day]
            item["version_plan_fingerprint"] = _fingerprint(
                {
                    "contract_name": CONTRACT_NAME,
                    "source_digest": source_digest,
                    "business_date": day,
                    "base_version_id": item["base_version_id"],
                    "after_target_digest": item["after_target_digest"],
                }
            )
        projection_rows = _projection_rows(
            corrected_by_date,
            target_nm_ids=target_nm_ids,
            version_ids=version_ids,
            source_digest=source_digest,
        )
        ready_updates = _ready_updates(
            snapshots=ready_snapshots,
            corrected_by_date=corrected_by_date,
            target_nm_ids=target_nm_ids,
            version_ids=version_ids,
            source_digest=source_digest,
        )
        non_target_digest = _non_target_digest(
            conn,
            target_nm_ids=target_nm_ids,
        )
        manifest_material = {
            "contract_name": CONTRACT_NAME,
            "scope": {
                "dates": list(DATES),
                "target_nm_ids": target_nm_ids,
                "shipment_ids": sorted(TARGET_SHIPMENTS.values()),
                "supply_ids": list(TARGET_SUPPLIES),
            },
            "source_digest": source_digest,
            "non_target_digest": non_target_digest,
            "active_pointer": active,
            "versions": version_manifest,
            "changed_rows": changed_rows,
            "ready_pairs": [[day, nm_id] for day, nm_id in ready_pairs],
            "ready_updates": [
                {
                    key: item[key]
                    for key in (
                        "bundle_version",
                        "as_of_date",
                        "before_plan_sha256",
                        "after_plan_sha256",
                        "changed_cells",
                        "presentation_changes",
                        "coverage_changes",
                    )
                }
                for item in ready_updates
            ],
            "projection_row_count": len(projection_rows),
        }
        fingerprint = _fingerprint(
            {
                "contract_name": CONTRACT_NAME,
                "scope": manifest_material["scope"],
                "source_digest": source_digest,
                "non_target_digest": non_target_digest,
                "versions": [
                    {
                        "business_date": item["business_date"],
                        "base_version_id": item["base_version_id"],
                        "base_plan_fingerprint": item[
                            "base_plan_fingerprint"
                        ],
                        "after_target_digest": item["after_target_digest"],
                        "version_id": item["version_id"],
                        "version_plan_fingerprint": item[
                            "version_plan_fingerprint"
                        ],
                    }
                    for item in version_manifest
                ],
                "projection_rows": [
                    {
                        "as_of_date": item["as_of_date"],
                        "nm_id": item["nm_id"],
                        "row_fingerprint": item["row_fingerprint"],
                    }
                    for item in projection_rows
                ],
            }
        )
        revision_id = _stable_id(
            "whbpr_hist",
            {"fingerprint": fingerprint, "source": source_digest},
        )
        already_applied = _versions_already_applied(
            conn,
            version_manifest=version_manifest,
        )
        return {
            **manifest_material,
            "fingerprint": fingerprint,
            "mode": "dry_run",
            "would_change": not already_applied,
            "already_applied": already_applied,
            "expected": {
                "changed_pair_count": len(
                    {
                        (str(item["business_date"]), int(item["nm_id"]))
                        for item in changed_rows
                    }
                ),
                "changed_pairs_by_date": {
                    day: len(
                        {
                            int(item["nm_id"])
                            for item in changed_rows
                            if str(item["business_date"]) == day
                        }
                    )
                    for day in DATES
                },
                "configured_ready_pair_count": len(ready_pairs),
                "target_nm_id_count": EXPECTED_TARGET_NM_ID_COUNT,
                "source_flow_count": 10,
                "functional_version_count": len(DATES),
                "projection_row_count": len(projection_rows),
                "ready_snapshot_update_count": len(ready_updates),
                "skipped_unconfigured_nm_id_count": len(
                    set(target_nm_ids) - configured_nm_ids
                ),
            },
            "recovery": {
                "tier": "T1",
                "mutation_kind": "targeted_warehouse_publication",
                "closure_kind": "sku_date",
                "full_database_copy": False,
                "finance_raw_rows_read": 0,
                "full_database_integrity_scan": False,
                "rollback": "exact target-scoped before images",
            },
            "second_run_criterion": {
                "tier": "T0",
                "new_versions": 0,
                "changed_rows": 0,
                "changed_cells": 0,
                "recovery_bytes": 0,
                "mutations": 0,
            },
            "_apply_payload": {
                "corrected_by_date": corrected_by_date,
                "projection_rows": projection_rows,
                "ready_updates": ready_updates,
                "revision_id": revision_id,
                "version_ids": version_ids,
                "base_versions": base_versions,
            },
        }


def _reconstruction_base(
    opening_rows: Sequence[Mapping[str, Any]],
    *,
    exact_date_rows: Sequence[Mapping[str, Any]],
    target_nm_ids: Sequence[int],
) -> list[dict[str, Any]]:
    """Use 19 July as the non-WB opening and each day's exact WB snapshot.

    Later immutable versions legitimately omit depleted FF rows, so they cannot
    be reverse-mutated into a historical opening.  The target non-WB stages are
    rebuilt from the earliest exact opening plus dated source events, while the
    official WB stage remains an exact-date replacement snapshot.
    """

    target = {int(value) for value in target_nm_ids}
    selected: dict[tuple[str, int], dict[str, Any]] = {
        (str(row["warehouse_key"]), int(row["nm_id"])): deepcopy(dict(row))
        for row in exact_date_rows
        if int(row["nm_id"]) not in target
        or str(row["warehouse_key"]) == "wb"
    }
    for row in opening_rows:
        key = (str(row["warehouse_key"]), int(row["nm_id"]))
        if key[1] in target and key[0] != "wb":
            selected[key] = deepcopy(dict(row))
    return [selected[key] for key in sorted(selected)]


def apply_historical_recovery_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    """Apply one exact dry-run manifest under a target-scoped T1 journal."""

    fingerprint = str(plan.get("fingerprint") or "")
    if not fingerprint or fingerprint != str(confirm_fingerprint or ""):
        raise WarehouseHistoricalRecoveryError(
            "apply requires the exact current historical-recovery fingerprint"
        )
    if not str(approval_reference or "").strip():
        raise WarehouseHistoricalRecoveryError(
            "exact bounded human-gate provenance is required"
        )
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    operation_id = recovery_operation_id(
        "targeted_warehouse_publication",
        fingerprint,
    )
    existing = registry.get_operation(operation_id)
    if (
        existing is not None
        and str(existing.get("lifecycle")) == RecoveryState.RETAINED.value
    ):
        readback = readback_historical_recovery(
            runtime,
            fingerprint=fingerprint,
            plan=plan,
        )
        return {
            **_public_plan(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "second_run": {
                "tier": "T0",
                "recovery_bytes": 0,
                "changed_rows": 0,
                "changed_cells": 0,
                "mutations": 0,
            },
            "readback": readback,
            "recovery_policy": existing,
        }
    if not bool(plan.get("would_change")):
        recovery = registry.plan_noop(
            mutation_kind="targeted_warehouse_publication",
            closure_kind="sku_date",
            plan_fingerprint=fingerprint,
            scope=dict(plan["scope"]),
        )
        return {
            **_public_plan(plan),
            "mode": "apply",
            "applied": False,
            "idempotent": True,
            "recovery_policy": recovery,
        }
    payload = dict(plan["_apply_payload"])
    before_images = _before_images(
        runtime.db_path,
        plan=plan,
        payload=payload,
    )
    recovery = registry.prepare_t1(
        mutation_kind="targeted_warehouse_publication",
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={
            **dict(plan["scope"]),
            "approval_reference": str(approval_reference),
        },
        before_images=before_images,
        source_digest=str(plan["source_digest"]),
        non_target_digest=str(plan["non_target_digest"]),
        read_bytes=sum(
            len(_canonical_json(item).encode("utf-8"))
            for item in before_images
        ),
    )
    if str(recovery.get("lifecycle")) == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(
            str(recovery["operation_id"]),
            expected_source_digest=str(plan["source_digest"]),
        )
    applied_at = _now()
    try:
        with warehouse_functional_write_lock(
            runtime.runtime_dir,
            timeout_seconds=300,
        ):
            fresh = build_historical_recovery_plan(runtime)
            if str(fresh["fingerprint"]) != fingerprint:
                raise WarehouseHistoricalRecoveryError(
                    "canonical source or target fingerprint changed after dry-run"
                )
            with _connect(runtime.db_path, read_only=False) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _apply_versions(
                        conn,
                        plan=plan,
                        payload=payload,
                        applied_at=applied_at,
                    )
                    projection_result = _persist_projection_revision(
                        conn,
                        revision_id=str(payload["revision_id"]),
                        plan_fingerprint=fingerprint,
                        stable_source_id=(
                            "historical_recovery:2026-07-19:2026-07-29"
                        ),
                        source_revision=str(plan["source_digest"]),
                        business_effective_date=DATE_FROM,
                        published_at=applied_at,
                        base_version_id=str(
                            plan["versions"][0]["base_version_id"]
                        ),
                        published_version_id=str(
                            plan["versions"][-1]["version_id"]
                        ),
                        affected_nm_ids=list(plan["scope"]["target_nm_ids"]),
                        source_kind="historical_business_recovery",
                        rows=list(payload["projection_rows"]),
                        diagnostics={
                            "affected_dates": list(DATES),
                            "contract_name": CONTRACT_NAME,
                            "approval_reference": str(approval_reference),
                            "source_digest": str(plan["source_digest"]),
                        },
                    )
                    _apply_ready_updates(
                        conn,
                        updates=list(payload["ready_updates"]),
                    )
                    active_after = dict(
                        conn.execute(
                            "SELECT * FROM "
                            "sheet_vitrina_v1_warehouse_functional_active "
                            "WHERE slot=1"
                        ).fetchone()
                    )
                    if active_after != dict(plan["active_pointer"]):
                        raise WarehouseHistoricalRecoveryError(
                            "historical publication changed current active pointer"
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
    except Exception as exc:
        registry.fail_recoverable(
            str(recovery["operation_id"]),
            error=str(exc),
            next_action="rollback_or_replan_historical_recovery",
        )
        raise
    readback = readback_historical_recovery(
        runtime,
        fingerprint=fingerprint,
        plan=plan,
    )
    recovery = registry.retain(
        str(recovery["operation_id"]),
        after_digest=str(readback["after_digest"]),
        non_target_digest=str(plan["non_target_digest"]),
    )
    return {
        **_public_plan(plan),
        "mode": "apply",
        "applied": True,
        "idempotent": False,
        "applied_at": applied_at,
        "approval_reference": str(approval_reference),
        "projection": projection_result,
        "readback": readback,
        "recovery_policy": recovery,
    }


def rollback_historical_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    fingerprint: str,
    reason: str,
) -> dict[str, Any]:
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    dependents = [
        operation
        for operation in registry.list_operations(limit=1000)
        if str((operation.get("scope") or {}).get("batch_a_fingerprint") or "")
        == str(fingerprint)
        and str(operation.get("lifecycle") or "")
        in {
            RecoveryState.MUTATION_RUNNING.value,
            RecoveryState.RETAINED.value,
        }
    ]
    if dependents:
        raise WarehouseHistoricalRecoveryError(
            "Batch A rollback requires dependent Batch B rollback first: "
            + ",".join(
                str(item.get("operation_id") or "")
                for item in dependents
            )
        )
    operation_id = recovery_operation_id(
        "targeted_warehouse_publication",
        str(fingerprint),
    )
    return registry.rollback_t1(operation_id, reason=str(reason))


def readback_historical_recovery(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    fingerprint: str,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(plan["_apply_payload"])
    version_rows: list[dict[str, Any]] = []
    with _connect(runtime.db_path, read_only=True) as conn:
        for item in plan["versions"]:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions "
                "WHERE version_id=?",
                (str(item["version_id"]),),
            ).fetchone()
            if row is None:
                raise WarehouseHistoricalRecoveryError(
                    "historical functional version is missing after apply"
                )
            balances = _balance_rows(conn, str(item["version_id"]))
            actual_digest = _target_balance_digest(
                balances,
                plan["scope"]["target_nm_ids"],
            )
            if actual_digest != str(item["after_target_digest"]):
                raise WarehouseHistoricalRecoveryError(
                    "historical functional balance readback mismatch: "
                    + str(item["business_date"])
                )
            version_rows.append(
                {
                    "business_date": str(item["business_date"]),
                    "version_id": str(item["version_id"]),
                    "target_digest": actual_digest,
                    "row_count": len(balances),
                }
            )
        projection_count = _verify_projection_readback(
            conn,
            revision_id=str(payload["revision_id"]),
            expected_rows=payload["projection_rows"],
        )
        revision = conn.execute(
            f"SELECT * FROM {REVISION_TABLE} WHERE plan_fingerprint=?",
            (str(fingerprint),),
        ).fetchone()
        if revision is None or str(revision["status"]) != "active":
            raise WarehouseHistoricalRecoveryError(
                "historical business projection revision is not active"
            )
        for update in payload["ready_updates"]:
            stored = conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version=? AND as_of_date=?",
                (update["bundle_version"], update["as_of_date"]),
            ).fetchone()
            if (
                stored is None
                or "sha256:" + _sha(str(stored["plan_json"]))
                != str(update["after_plan_sha256"])
            ):
                raise WarehouseHistoricalRecoveryError(
                    "ready snapshot readback mismatch"
                )
        active = dict(
            conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active "
                "WHERE slot=1"
            ).fetchone()
        )
        if active != dict(plan["active_pointer"]):
            raise WarehouseHistoricalRecoveryError(
                "current open-day functional pointer changed"
            )
        non_target_digest = _non_target_digest(
            conn,
            target_nm_ids=plan["scope"]["target_nm_ids"],
        )
        if non_target_digest != str(plan["non_target_digest"]):
            raise WarehouseHistoricalRecoveryError(
                "Batch A non-target digest changed during apply"
            )
        after_digest = _fingerprint(
            {
                "versions": version_rows,
                "projection_revision": dict(revision),
                "ready_updates": [
                    {
                        "bundle_version": item["bundle_version"],
                        "as_of_date": item["as_of_date"],
                        "after_plan_sha256": item["after_plan_sha256"],
                    }
                    for item in payload["ready_updates"]
                ],
                "active": active,
            }
        )
    return {
        "status": "verified",
        "fingerprint": str(fingerprint),
        "functional_versions": version_rows,
        "projection_current_row_count": projection_count,
        "ready_snapshot_update_count": len(payload["ready_updates"]),
        "active_pointer_unchanged": True,
        "non_target_digest": non_target_digest,
        "non_target_unchanged": True,
        "after_digest": after_digest,
    }


def _shipment_manifest(
    conn: sqlite3.Connection,
    *,
    allocations: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    events = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_own_capital_events "
            "WHERE shipment_id IN (?,?,?,?,?) "
            "AND event_type IN ('supplier_payment','cost_payment') "
            "ORDER BY effective_date,event_type,event_id",
            tuple(TARGET_SHIPMENTS.values()),
        ).fetchall()
    ]
    by_shipment: dict[str, list[dict[str, Any]]] = {
        shipment_id: [] for shipment_id in TARGET_SHIPMENTS.values()
    }
    for event in events:
        by_shipment[str(event["shipment_id"])].append(event)
    shipment_rows = {
        str(row["shipment_id"]): dict(row)
        for row in sources.get("shipments") or []
        if str(row.get("shipment_id") or "") in TARGET_SHIPMENTS.values()
    }
    component_dates: dict[str, str] = {}
    for operation in sources.get("cny_operations") or []:
        operation_id = str(operation.get("operation_id") or "")
        operation_date = str(operation.get("operation_date") or "")[:10]
        if operation_id and operation_date:
            component_dates["cny_operation:" + operation_id] = operation_date
    for event in events:
        payload = _loads(event.get("payload_json"), {})
        provenance = dict(payload.get("provenance") or {})
        for line_id in provenance.get("expense_line_ids") or []:
            component_dates["expense_line:" + str(line_id)] = str(
                event["effective_date"]
            )
    result: dict[str, dict[str, Any]] = {}
    for invoice_no, shipment_id in TARGET_SHIPMENTS.items():
        allocation = dict(allocations.get(shipment_id) or {})
        shipment = shipment_rows.get(shipment_id)
        if shipment is None:
            raise WarehouseHistoricalRecoveryError(
                f"target supplier shipment disappeared: {invoice_no}"
            )
        if str(shipment.get("invoice_no") or "") != invoice_no:
            raise WarehouseHistoricalRecoveryError(
                f"supplier shipment identity changed: {shipment_id}"
            )
        if allocation.get("blockers"):
            raise WarehouseHistoricalRecoveryError(
                f"current canonical supplier proof has blockers: {invoice_no}"
            )
        lines = []
        for line in allocation.get("lines") or []:
            components = []
            for raw_component in line.get("components") or []:
                component = deepcopy(dict(raw_component))
                source_component_id = str(
                    component.get("source_component_id") or ""
                )
                effective_date = str(
                    component_dates.get(source_component_id)
                    or dict(component.get("document") or {}).get("date")
                    or ""
                )[:10]
                if not effective_date:
                    raise WarehouseHistoricalRecoveryError(
                        "canonical supplier component has no business date: "
                        f"{invoice_no}/{source_component_id}"
                    )
                component["business_effective_date"] = effective_date
                components.append(component)
            lines.append(
                {
                    "line_id": str(line["line_id"]),
                    "nm_id": int(line["nm_id"]),
                    "quantity": str(line["quantity"]),
                    "current_components": components,
                }
            )
        quantity = sum((_decimal(line["quantity"]) for line in lines), ZERO)
        if quantity != EXPECTED_SHIPMENT_QUANTITIES[invoice_no]:
            raise WarehouseHistoricalRecoveryError(
                f"target supplier quantity changed for {invoice_no}: {quantity}"
            )
        shipment_events = by_shipment.get(shipment_id) or []
        first_payment = min(
            (
                str(item["effective_date"])
                for item in shipment_events
                if str(item["event_type"]) == "supplier_payment"
            ),
            default="",
        )
        if not first_payment:
            raise WarehouseHistoricalRecoveryError(
                f"target supplier payment evidence is absent: {invoice_no}"
            )
        result[invoice_no] = {
            "shipment_id": shipment_id,
            "invoice_no": invoice_no,
            "source_fingerprint": str(allocation["source_fingerprint"]),
            "calculation_fingerprint": str(
                allocation["calculation_fingerprint"]
            ),
            "first_payment_date": first_payment,
            "actual_shipment_date": str(
                shipment.get("actual_shipment_date") or ""
            )[:10],
            "actual_ff_acceptance_date": str(
                shipment.get("actual_ff_acceptance_date") or ""
            )[:10],
            "expenses_complete": bool(shipment.get("expenses_complete")),
            "lines": lines,
            "events": shipment_events,
            "event_digest": _fingerprint(shipment_events),
        }
    if result["26GN390"]["actual_ff_acceptance_date"] != "2026-07-21":
        raise WarehouseHistoricalRecoveryError(
            "26GN390 factual FF acceptance date drifted"
        )
    if result["26GN527"]["actual_shipment_date"] != "2026-07-21":
        raise WarehouseHistoricalRecoveryError(
            "26GN527 factual shipment date drifted"
        )
    return result


def _supply_manifest(
    conn: sqlite3.Connection,
    *,
    sources: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    operation_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_stock_operations "
            "WHERE source_type='wb_supply' AND source_object_id IN (?,?,?,?,?) "
            "ORDER BY created_at,operation_id",
            TARGET_SUPPLIES,
        ).fetchall()
    ]
    by_supply: dict[str, list[dict[str, Any]]] = {}
    for row in operation_rows:
        by_supply.setdefault(str(row["source_object_id"]), []).append(row)
    revisions = _supply_revisions(sources.get("wb_supplies") or [])
    corrections = {
        str(row["supply_id"]): dict(row)
        for row in sources.get("box_corrections") or []
    }
    result: dict[str, dict[str, Any]] = {}
    for supply_id in TARGET_SUPPLIES:
        rows = by_supply.get(supply_id) or []
        debit_rows = [
            row for row in rows if str(row["operation_type"]) == "auto_writeoff"
        ]
        if len(debit_rows) != 1:
            raise WarehouseHistoricalRecoveryError(
                f"expected one immutable FF debit for WB supply {supply_id}"
            )
        operation = debit_rows[0]
        diagnostics = _loads(operation.get("diagnostics_json"), {})
        guard = dict(
            dict(diagnostics.get("downstream_cost_state") or {}).get("guard")
            or {}
        )
        lines = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ff_stock_operation_lines "
                "WHERE operation_id=? ORDER BY line_no",
                (str(operation["operation_id"]),),
            ).fetchall()
        ]
        composition = {
            int(row["nm_id"]): abs(_decimal(row["quantity_delta"]))
            for row in lines
        }
        correction = corrections.get(supply_id)
        if correction is not None:
            composition = {
                int(key): _decimal(value)
                for key, value in _loads(
                    correction.get("corrected_composition_json"), {}
                ).items()
            }
        quantity = sum(composition.values(), ZERO)
        if quantity != EXPECTED_SUPPLY_QUANTITIES[supply_id]:
            raise WarehouseHistoricalRecoveryError(
                f"target WB supply quantity changed for {supply_id}: {quantity}"
            )
        unit_costs = _functional_supply_unit_costs(
            conn,
            supply_id=supply_id,
            nm_ids=composition,
        )
        for nm_id in composition:
            state = dict(guard.get(str(nm_id)) or guard.get(nm_id) or {})
            diagnostic_unit_cost = _decimal(
                state.get("sku_ff_unit_cost_rub")
            )
            if diagnostic_unit_cost <= ZERO:
                raise WarehouseHistoricalRecoveryError(
                    "immutable FF debit diagnostic cost is missing: "
                    f"{supply_id}/{nm_id}"
                )
            if unit_costs.get(nm_id, ZERO) <= ZERO:
                raise WarehouseHistoricalRecoveryError(
                    "canonical functional movement cost is missing: "
                    f"{supply_id}/{nm_id}"
                )
        source_timestamp = str(diagnostics.get("source_timestamp") or "")[:10]
        expected_date = SUPPLY_BUSINESS_DATES[supply_id]
        if not source_timestamp or source_timestamp > expected_date:
            raise WarehouseHistoricalRecoveryError(
                f"WB supply source date conflicts with reviewed business date: {supply_id}"
            )
        result[supply_id] = {
            "operation_id": str(operation["operation_id"]),
            "source_revision": str(revisions.get(supply_id) or ""),
            "source_timestamp": source_timestamp,
            "business_date": expected_date,
            "composition": {
                str(key): str(value)
                for key, value in sorted(composition.items())
            },
            "unit_costs": {
                str(key): str(value)
                for key, value in sorted(unit_costs.items())
            },
            "operation_digest": _fingerprint(
                {
                    "operation": operation,
                    "lines": lines,
                    "correction": correction,
                    "guard": guard,
                }
            ),
        }
    return result


def _functional_supply_unit_costs(
    conn: sqlite3.Connection,
    *,
    supply_id: str,
    nm_ids: Iterable[int],
) -> dict[int, Decimal]:
    target = {int(value) for value in nm_ids}
    result: dict[int, Decimal] = {}
    for raw in conn.execute(
        """
        SELECT event.nm_id,event.provenance_json
        FROM sheet_vitrina_v1_warehouse_functional_events event
        WHERE event.event_type='wb_final_acceptance'
          AND event.source_id LIKE ?
        ORDER BY event.created_at DESC,event.event_id DESC
        """,
        (str(supply_id) + ":%",),
    ).fetchall():
        nm_id = int(raw["nm_id"])
        provenance = _loads(raw["provenance_json"], {})
        unit_cost = _decimal(
            provenance.get("ff_wac_at_ledger_debit_rub")
        )
        if nm_id in target and unit_cost > ZERO:
            result.setdefault(nm_id, unit_cost)
    rows = conn.execute(
        """
        SELECT balance.nm_id,balance.provenance_json
        FROM sheet_vitrina_v1_warehouse_functional_balances balance
        JOIN sheet_vitrina_v1_warehouse_functional_versions version
          ON version.version_id=balance.version_id
        WHERE balance.warehouse_key IN ('ff_to_wb','ff')
        ORDER BY version.created_at DESC,version.version_id DESC,
                 balance.warehouse_key,balance.nm_id
        """
    ).fetchall()
    for raw in rows:
        nm_id = int(raw["nm_id"])
        if nm_id not in target or nm_id in result:
            continue
        provenance = _loads(raw["provenance_json"], {})
        for record in provenance.get("source_records") or []:
            record_supply = str(
                record.get("supply_id")
                or record.get("wb_supply_id")
                or ""
            )
            if record_supply == supply_id:
                unit_cost = _decimal(
                    record.get("ff_wac_at_ledger_debit_rub")
                    or record.get("pre_acceptance_unit_cost_rub")
                )
                if unit_cost > ZERO:
                    result[nm_id] = unit_cost
                    break
            for operation in record.get("operations") or []:
                if str(operation.get("source_object_id") or "") != supply_id:
                    continue
                unit_cost = _decimal(operation.get("unit_cost_rub"))
                if unit_cost > ZERO:
                    result[nm_id] = unit_cost
                    break
            if nm_id in result:
                break
    return result


def _base_versions(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for business_date in DATES:
        row = conn.execute(
            """
            SELECT version.*,snapshot.snapshot_id,snapshot.raw_rows_digest,
                   snapshot.snapshot_date
            FROM sheet_vitrina_v1_warehouse_functional_versions version
            JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
              ON snapshot.version_id=version.version_id
            WHERE version.cutover_id=?
              AND version.status='good'
              AND version.version_kind!='historical_business_recovery'
              AND snapshot.snapshot_date=?
            ORDER BY version.created_at DESC,version.version_id DESC
            LIMIT 1
            """,
            (FUNCTIONAL_CUTOVER_ID, business_date),
        ).fetchone()
        if row is None:
            raise WarehouseHistoricalRecoveryError(
                f"exact base functional version is missing: {business_date}"
            )
        result[business_date] = dict(row)
    return result


def _balance_rows(
    conn: sqlite3.Connection,
    version_id: str,
) -> list[dict[str, Any]]:
    rows = []
    for raw in conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
        "WHERE version_id=? ORDER BY warehouse_key,nm_id",
        (version_id,),
    ).fetchall():
        row = dict(raw)
        row["provenance"] = _loads(row.pop("provenance_json"), {})
        rows.append(row)
    return rows


def _correct_balances(
    before: Sequence[Mapping[str, Any]],
    *,
    business_date: str,
    shipment_manifest: Mapping[str, Mapping[str, Any]],
    supply_manifest: Mapping[str, Mapping[str, Any]],
    wb_daily: Mapping[int, Mapping[str, Any]],
    discrepancy_rows: Mapping[int, Mapping[str, Any]],
    target_nm_ids: Sequence[int],
) -> list[dict[str, Any]]:
    target_shipments = set(TARGET_SHIPMENTS.values())
    target_supplies = set(TARGET_SUPPLIES)
    by_key = {
        (str(row["warehouse_key"]), int(row["nm_id"])): deepcopy(dict(row))
        for row in before
    }
    for key, row in list(by_key.items()):
        stage, nm_id = key
        if nm_id not in set(target_nm_ids):
            continue
        if stage in {"production", "china_to_ff"}:
            sources = [
                deepcopy(item)
                for item in dict(row.get("provenance") or {}).get(
                    "source_records", []
                )
                if str(item.get("shipment_id") or "") not in target_shipments
            ]
            _replace_row_from_source_records(by_key, key, row, sources)
        elif stage == "ff":
            quantity = _decimal(row.get("quantity"))
            capital = _decimal(row.get("capital_rub"))
            provenance = deepcopy(dict(row.get("provenance") or {}))
            records = []
            for raw_record in provenance.get("source_records") or []:
                record = deepcopy(dict(raw_record))
                operations = []
                for operation in record.get("operations") or []:
                    source_object_id = str(
                        operation.get("source_object_id") or ""
                    )
                    if (
                        source_object_id in target_shipments
                        or source_object_id in target_supplies
                    ):
                        delta = _decimal(operation.get("quantity_delta"))
                        quantity -= delta
                        capital -= delta * _decimal(
                            operation.get("unit_cost_rub")
                        )
                    else:
                        operations.append(deepcopy(operation))
                record["operations"] = operations
                records.append(record)
            row["quantity"] = str(quantity)
            row["capital_rub"] = str(capital)
            row["provenance"] = {**provenance, "source_records": records}
            _normalize_row(by_key, key, row)
        elif stage == "ff_to_wb":
            quantity = _decimal(row.get("quantity"))
            capital = _decimal(row.get("capital_rub"))
            records = []
            for record in dict(row.get("provenance") or {}).get(
                "source_records", []
            ):
                if str(
                    record.get("supply_id")
                    or record.get("wb_supply_id")
                    or ""
                ) in target_supplies:
                    quantity -= _decimal(record.get("flow_quantity"))
                    capital -= _decimal(record.get("flow_capital_rub"))
                else:
                    records.append(deepcopy(record))
            row["quantity"] = str(quantity)
            row["capital_rub"] = str(capital)
            row["provenance"] = {
                **dict(row.get("provenance") or {}),
                "source_records": records,
            }
            _normalize_row(by_key, key, row)
        elif stage == "wb_acceptance_discrepancy":
            records = list(
                dict(row.get("provenance") or {}).get("source_records", [])
            )
            if any(_nested_supply_id(item) == "40985996" for item in records):
                by_key.pop(key, None)
    for shipment in shipment_manifest.values():
        desired_stage = _supplier_stage(
            business_date=business_date,
            shipment=shipment,
        )
        if desired_stage is None:
            continue
        capital_by_nm = _supplier_capital_by_nm(
            shipment,
            business_date=business_date,
        )
        for line in shipment["lines"]:
            nm_id = int(line["nm_id"])
            quantity = _decimal(line["quantity"])
            capital = capital_by_nm.get(nm_id, ZERO)
            if capital <= ZERO:
                raise WarehouseHistoricalRecoveryError(
                    "positive supplier quantity has no exact-date cost: "
                    f"{shipment['invoice_no']}/{business_date}/{nm_id}"
                )
            record = _supplier_record(
                shipment,
                line=line,
                business_date=business_date,
                capital=capital,
            )
            if desired_stage == "ff":
                _add_ff_delta(
                    by_key,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=capital,
                    operation={
                        "business_date": business_date,
                        "operation_id": (
                            "historical_supplier_receipt:"
                            + str(shipment["shipment_id"])
                        ),
                        "quantity_delta": str(quantity),
                        "source": record,
                        "source_object_id": str(shipment["shipment_id"]),
                        "source_type": "supplier_shipment",
                        "unit_cost_rub": str(capital / quantity),
                    },
                )
            else:
                _add_source_record(
                    by_key,
                    stage=desired_stage,
                    nm_id=nm_id,
                    record=record,
                    quality=str(record["quality"]),
                    certified=bool(record["expenses_complete_certification"]),
                )
    for supply_id, supply in supply_manifest.items():
        if business_date < str(supply["business_date"]):
            continue
        for raw_nm_id, raw_quantity in supply["composition"].items():
            nm_id = int(raw_nm_id)
            quantity = _decimal(raw_quantity)
            unit_cost = _decimal(supply["unit_costs"][str(nm_id)])
            capital = quantity * unit_cost
            _add_ff_delta(
                by_key,
                nm_id=nm_id,
                quantity=-quantity,
                capital=-capital,
                operation={
                    "business_date": str(supply["business_date"]),
                    "operation_id": str(supply["operation_id"]),
                    "quantity_delta": str(-quantity),
                    "source": {"quality": "proportional_wac_outbound"},
                    "source_object_id": supply_id,
                    "source_type": "wb_supply",
                    "unit_cost_rub": str(unit_cost),
                },
            )
            if supply_id == "40985996" and business_date >= "2026-07-23":
                continue
            _add_source_record(
                by_key,
                stage="ff_to_wb",
                nm_id=nm_id,
                record={
                    "business_date": str(supply["business_date"]),
                    "cost_covered_quantity": str(quantity),
                    "cost_freshness": "current",
                    "ff_wac_at_ledger_debit_rub": str(unit_cost),
                    "flow_capital_rub": str(capital),
                    "flow_quantity": str(quantity),
                    "source_revision": str(supply["source_revision"]),
                    "supply_id": supply_id,
                    "wb_supply_id": supply_id,
                    "historical_recovery": True,
                },
                quality="moving_weighted_average",
                certified=False,
            )
    if business_date >= "2026-07-23":
        for nm_id, source_row in discrepancy_rows.items():
            key = ("wb_acceptance_discrepancy", int(nm_id))
            by_key[key] = deepcopy(dict(source_row))
    # WB is an official replacement snapshot, not an additive receipt ledger.
    # Exact-date base versions already carry that complete snapshot (the audit
    # found zero replacement mismatches), so the recovery deliberately leaves
    # WB rows byte/semantically unchanged. ``wb_daily`` is still fingerprinted
    # and consumed by the dependent Proxy publication.
    del wb_daily
    result = []
    for key, row in sorted(by_key.items()):
        _normalize_row(by_key, key, row)
        if key in by_key:
            result.append(by_key[key])
    return result


def _supplier_capital_by_nm(
    shipment: Mapping[str, Any],
    *,
    business_date: str,
) -> dict[int, Decimal]:
    return {
        int(line["nm_id"]): sum(
            (
                _decimal(component.get("amount_rub"))
                for component in line.get("current_components") or []
                if str(component.get("business_effective_date") or "")
                <= business_date
            ),
            ZERO,
        )
        for line in shipment["lines"]
    }


def _supplier_stage(
    *,
    business_date: str,
    shipment: Mapping[str, Any],
) -> str | None:
    if business_date < str(shipment["first_payment_date"]):
        return None
    acceptance = str(shipment.get("actual_ff_acceptance_date") or "")
    actual_shipment = str(shipment.get("actual_shipment_date") or "")
    if acceptance and business_date >= acceptance:
        return "ff"
    if actual_shipment and business_date >= actual_shipment:
        return "china_to_ff"
    return "production"


def _supplier_record(
    shipment: Mapping[str, Any],
    *,
    line: Mapping[str, Any],
    business_date: str,
    capital: Decimal,
) -> dict[str, Any]:
    components = [
        {
            "source_component_id": str(
                component["source_component_id"]
            ),
            "component_key": str(component["component_key"]),
            "effective_date": str(
                component["business_effective_date"]
            ),
            "capital_rub": str(component["amount_rub"]),
            "document": deepcopy(component.get("document") or {}),
        }
        for component in line.get("current_components") or []
        if str(component["business_effective_date"]) <= business_date
    ]
    all_effective = all(
        str(component["business_effective_date"]) <= business_date
        for component in line.get("current_components") or []
    )
    certified = bool(shipment["expenses_complete"] and all_effective)
    quantity = _decimal(line["quantity"])
    return {
        "actual_shipment_date": str(shipment["actual_shipment_date"]),
        "actual_ff_acceptance_date": str(
            shipment["actual_ff_acceptance_date"]
        ),
        "business_date": business_date,
        "calculation_fingerprint": str(
            shipment["calculation_fingerprint"]
        ),
        "expenses_complete_certification": certified,
        "flow_capital_rub": str(capital),
        "flow_quantity": str(quantity),
        "invoice_no": str(shipment["invoice_no"]),
        "line_id": str(line["line_id"]),
        "nm_id": int(line["nm_id"]),
        "quality": (
            "certified"
            if certified
            else "confirmed_payments_provisional_expenses"
        ),
        "shipment_id": str(shipment["shipment_id"]),
        "source_fingerprint": str(shipment["source_fingerprint"]),
        "business_components": components,
        "unit_cost_rub": str(capital / quantity),
        "historical_recovery": True,
    }


def _replace_row_from_source_records(
    by_key: dict[tuple[str, int], dict[str, Any]],
    key: tuple[str, int],
    row: Mapping[str, Any],
    sources: Sequence[Mapping[str, Any]],
) -> None:
    value = deepcopy(dict(row))
    quantity = sum((_decimal(item.get("flow_quantity")) for item in sources), ZERO)
    capital = sum(
        (_decimal(item.get("flow_capital_rub")) for item in sources),
        ZERO,
    )
    value["quantity"] = str(quantity)
    value["capital_rub"] = str(capital)
    value["cost_covered_quantity"] = str(
        sum(
            (
                _decimal(item.get("flow_quantity"))
                for item in sources
                if _decimal(item.get("flow_capital_rub")) > ZERO
            ),
            ZERO,
        )
    )
    value["provenance"] = {
        **dict(value.get("provenance") or {}),
        "source_records": list(sources),
    }
    _normalize_row(by_key, key, value)


def _add_source_record(
    by_key: dict[tuple[str, int], dict[str, Any]],
    *,
    stage: str,
    nm_id: int,
    record: Mapping[str, Any],
    quality: str,
    certified: bool,
) -> None:
    key = (stage, int(nm_id))
    row = deepcopy(
        by_key.get(key)
        or {
            "warehouse_key": stage,
            "nm_id": int(nm_id),
            "quantity": "0",
            "wac_rub": None,
            "capital_rub": "0",
            "cost_covered_quantity": "0",
            "quality": quality,
            "certified": int(certified),
            "wb_quantity": "0",
            "wb_in_way_to_client": "0",
            "wb_in_way_from_client": "0",
            "provenance": {"source_records": []},
        }
    )
    sources = list(dict(row.get("provenance") or {}).get("source_records", []))
    sources.append(deepcopy(dict(record)))
    row["quantity"] = str(
        _decimal(row["quantity"]) + _decimal(record.get("flow_quantity"))
    )
    row["capital_rub"] = str(
        _decimal(row["capital_rub"])
        + _decimal(record.get("flow_capital_rub"))
    )
    row["cost_covered_quantity"] = str(
        _decimal(row["cost_covered_quantity"])
        + (
            _decimal(record.get("flow_quantity"))
            if _decimal(record.get("flow_capital_rub")) > ZERO
            else ZERO
        )
    )
    row["quality"] = (
        quality
        if _decimal(row["quantity"]) == _decimal(record.get("flow_quantity"))
        else "mixed:" + ",".join(sorted({str(row["quality"]), quality}))
    )
    row["certified"] = int(bool(row.get("certified")) and certified)
    row["provenance"] = {
        **dict(row.get("provenance") or {}),
        "source_records": sources,
        "historical_recovery": True,
    }
    _normalize_row(by_key, key, row)


def _add_ff_delta(
    by_key: dict[tuple[str, int], dict[str, Any]],
    *,
    nm_id: int,
    quantity: Decimal,
    capital: Decimal,
    operation: Mapping[str, Any],
) -> None:
    key = ("ff", int(nm_id))
    row = deepcopy(
        by_key.get(key)
        or {
            "warehouse_key": "ff",
            "nm_id": int(nm_id),
            "quantity": "0",
            "wac_rub": None,
            "capital_rub": "0",
            "cost_covered_quantity": "0",
            "quality": "moving_weighted_average",
            "certified": 0,
            "wb_quantity": "0",
            "wb_in_way_to_client": "0",
            "wb_in_way_from_client": "0",
            "provenance": {"source_records": []},
        }
    )
    next_quantity = _decimal(row["quantity"]) + quantity
    next_capital = _decimal(row["capital_rub"]) + capital
    if next_quantity < ZERO or next_capital < -TOLERANCE:
        raise WarehouseHistoricalRecoveryError(
            "historical FF replay would become negative: "
            f"nmID={nm_id}, operation={operation.get('operation_id')}, "
            f"quantity={next_quantity}, capital={next_capital}"
        )
    records = list(dict(row.get("provenance") or {}).get("source_records", []))
    records.append(
        {
            "historical_recovery": True,
            "operations": [deepcopy(dict(operation))],
        }
    )
    row["quantity"] = str(next_quantity)
    row["capital_rub"] = str(max(next_capital, ZERO))
    row["cost_covered_quantity"] = str(next_quantity)
    row["quality"] = "moving_weighted_average"
    row["certified"] = 0
    row["provenance"] = {
        **dict(row.get("provenance") or {}),
        "source_records": records,
        "historical_recovery": True,
    }
    _normalize_row(by_key, key, row)


def _normalize_row(
    by_key: dict[tuple[str, int], dict[str, Any]],
    key: tuple[str, int],
    row: Mapping[str, Any],
) -> None:
    value = deepcopy(dict(row))
    quantity = _decimal(value.get("quantity"))
    capital = _decimal(value.get("capital_rub"))
    if quantity < ZERO or capital < -TOLERANCE:
        raise WarehouseHistoricalRecoveryError(
            f"negative functional balance: {key[0]}/{key[1]}"
        )
    if quantity <= ZERO:
        by_key.pop(key, None)
        return
    capital = max(capital, ZERO)
    if capital <= ZERO:
        raise WarehouseHistoricalRecoveryError(
            f"positive functional quantity has no capital: {key[0]}/{key[1]}"
        )
    covered = min(
        quantity,
        max(_decimal(value.get("cost_covered_quantity")), ZERO),
    )
    if covered <= ZERO:
        covered = quantity
    value["quantity"] = str(quantity)
    value["capital_rub"] = str(capital)
    value["cost_covered_quantity"] = str(covered)
    value["wac_rub"] = (
        str(capital / quantity) if covered >= quantity else None
    )
    value["warehouse_key"] = key[0]
    value["nm_id"] = key[1]
    value.setdefault("wb_quantity", "0")
    value.setdefault("wb_in_way_to_client", "0")
    value.setdefault("wb_in_way_from_client", "0")
    value["certified"] = int(bool(value.get("certified")))
    by_key[key] = value


def _changed_target_rows(
    before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]],
    *,
    business_date: str,
    target_nm_ids: Sequence[int],
) -> list[dict[str, Any]]:
    target = set(int(value) for value in target_nm_ids)
    before_by_key = {
        (str(row["warehouse_key"]), int(row["nm_id"])): row
        for row in before
        if int(row["nm_id"]) in target
    }
    after_by_key = {
        (str(row["warehouse_key"]), int(row["nm_id"])): row
        for row in after
        if int(row["nm_id"]) in target
    }
    result = []
    for stage, nm_id in sorted(set(before_by_key) | set(after_by_key)):
        left = before_by_key.get((stage, nm_id), {})
        right = after_by_key.get((stage, nm_id), {})
        before_values = {
            "quantity": str(left.get("quantity") or "0"),
            "capital_rub": str(left.get("capital_rub") or "0"),
            "wac_rub": left.get("wac_rub"),
        }
        after_values = {
            "quantity": str(right.get("quantity") or "0"),
            "capital_rub": str(right.get("capital_rub") or "0"),
            "wac_rub": right.get("wac_rub"),
        }
        if _row_semantic(before_values) == _row_semantic(after_values):
            continue
        result.append(
            {
                "business_date": business_date,
                "nm_id": nm_id,
                "stage": stage,
                "before": before_values,
                "after": after_values,
            }
        )
    return result


def _validate_changed_scope(
    *,
    changed_rows: Sequence[Mapping[str, Any]],
    version_manifest: Sequence[Mapping[str, Any]],
) -> None:
    pairs_by_date = {
        day: {
            int(item["nm_id"])
            for item in changed_rows
            if str(item["business_date"]) == day
        }
        for day in DATES
    }
    actual_counts = {
        day: len(values) for day, values in pairs_by_date.items()
    }
    if actual_counts != EXPECTED_CHANGED_PAIRS_BY_DATE:
        raise WarehouseHistoricalRecoveryError(
            "fresh exact-date changed-pair closure differs from audited scope: "
            + _canonical_json(
                {
                    "counts": actual_counts,
                    "nm_ids": {
                        day: sorted(values)
                        for day, values in pairs_by_date.items()
                    },
                    "pair_deltas": {
                        day: _pair_delta_diagnostics(
                            changed_rows,
                            business_date=day,
                        )
                        for day in DATES
                        if day in {
                            "2026-07-19",
                            "2026-07-20",
                            "2026-07-25",
                        }
                    },
                    "net_capital": {
                        str(item["business_date"]): str(
                            _decimal(dict(item["after_total"])["capital_rub"])
                            - _decimal(
                                dict(item["before_total"])["capital_rub"]
                            )
                        )
                        for item in version_manifest
                    },
                }
            )
        )
    if sum(actual_counts.values()) != EXPECTED_CHANGED_PAIR_COUNT:
        raise WarehouseHistoricalRecoveryError(
            "fresh audited 19–29 changed date/SKU pair count is not 515"
        )
    actual_capital = {
        str(item["business_date"]): (
            _decimal(dict(item["after_total"])["capital_rub"])
            - _decimal(dict(item["before_total"])["capital_rub"])
        )
        for item in version_manifest
    }
    mismatches = {
        day: {
            "actual": str(actual_capital[day]),
            "expected": str(expected),
        }
        for day, expected in EXPECTED_NET_CAPITAL_DELTA_BY_DATE.items()
        if abs(actual_capital[day] - expected) > TOLERANCE
    }
    if mismatches:
        raise WarehouseHistoricalRecoveryError(
            "fresh exact-date net-capital delta differs from audited scope: "
            + _canonical_json(mismatches)
        )
    actual_quantity = {
        str(item["business_date"]): (
            _decimal(dict(item["after_total"])["quantity"])
            - _decimal(dict(item["before_total"])["quantity"])
        )
        for item in version_manifest
    }
    quantity_mismatches = {
        day: {
            "actual": str(actual_quantity[day]),
            "expected": str(expected),
        }
        for day, expected in EXPECTED_NET_QUANTITY_DELTA_BY_DATE.items()
        if actual_quantity[day] != expected
    }
    if quantity_mismatches:
        raise WarehouseHistoricalRecoveryError(
            "fresh exact-date net-quantity delta differs from audited scope: "
            + _canonical_json(quantity_mismatches)
        )


def _pair_delta_diagnostics(
    changed_rows: Sequence[Mapping[str, Any]],
    *,
    business_date: str,
) -> list[dict[str, Any]]:
    by_nm: dict[int, dict[str, Decimal]] = {}
    for item in changed_rows:
        if str(item["business_date"]) != business_date:
            continue
        nm_id = int(item["nm_id"])
        target = by_nm.setdefault(
            nm_id,
            {
                "net_quantity": ZERO,
                "net_capital": ZERO,
                "max_stage_quantity": ZERO,
                "max_stage_capital": ZERO,
            },
        )
        quantity_delta = (
            _decimal(dict(item["after"])["quantity"])
            - _decimal(dict(item["before"])["quantity"])
        )
        capital_delta = (
            _decimal(dict(item["after"])["capital_rub"])
            - _decimal(dict(item["before"])["capital_rub"])
        )
        target["net_quantity"] += quantity_delta
        target["net_capital"] += capital_delta
        target["max_stage_quantity"] = max(
            target["max_stage_quantity"],
            abs(quantity_delta),
        )
        target["max_stage_capital"] = max(
            target["max_stage_capital"],
            abs(capital_delta),
        )
    return [
        {
            "nm_id": nm_id,
            **{key: str(value) for key, value in values.items()},
        }
        for nm_id, values in sorted(
            by_nm.items(),
            key=lambda item: (
                item[1]["max_stage_capital"],
                item[1]["max_stage_quantity"],
                item[0],
            ),
        )
    ]


def _wb_daily_rows(
    conn: sqlite3.Connection,
    *,
    target_nm_ids: Sequence[int],
) -> dict[str, dict[int, dict[str, Any]]]:
    placeholders = ",".join("?" for _ in target_nm_ids)
    result: dict[str, dict[int, dict[str, Any]]] = {
        day: {} for day in DATES
    }
    for row in conn.execute(
        f"""
        SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
        WHERE cutover_id=? AND as_of_date BETWEEN ? AND ?
          AND nm_id IN ({placeholders})
        ORDER BY as_of_date,nm_id
        """,
        (
            FUNCTIONAL_CUTOVER_ID,
            DATE_FROM,
            DATE_TO,
            *target_nm_ids,
        ),
    ).fetchall():
        result[str(row["as_of_date"])][int(row["nm_id"])] = dict(row)
    return result


def _current_409_discrepancy_rows(
    conn: sqlite3.Connection,
) -> dict[int, dict[str, Any]]:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active "
        "WHERE slot=1"
    ).fetchone()
    rows: dict[int, dict[str, Any]] = {}
    if active is None:
        return rows
    for raw in conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
        "WHERE version_id=? AND warehouse_key='wb_acceptance_discrepancy' "
        "ORDER BY nm_id",
        (str(active["version_id"]),),
    ).fetchall():
        row = dict(raw)
        provenance = _loads(row.pop("provenance_json"), {})
        receipts = [
            deepcopy(dict(receipt))
            for source_record in provenance.get("source_records") or []
            for receipt in dict(source_record).get("receipts") or []
            if _nested_supply_id(receipt) == "40985996"
        ]
        if not receipts:
            continue
        quantity = sum(
            (_decimal(receipt.get("quantity")) for receipt in receipts), ZERO
        )
        capital = sum(
            (_decimal(receipt.get("capital")) for receipt in receipts), ZERO
        )
        if quantity <= ZERO or capital <= ZERO:
            raise WarehouseHistoricalRecoveryError(
                "40985996 discrepancy receipt has missing quantity/capital"
            )
        row.update(
            {
                "quantity": str(quantity),
                "capital_rub": str(capital),
                "wac_rub": str(capital / quantity),
                "cost_covered_quantity": str(quantity),
                "provenance": {
                    "source_records": [
                        {
                            "receipts": receipts,
                            "doprinato_matches": [],
                            "paid_acceptance_excluded": True,
                        }
                    ],
                    "historical_recovery": True,
                },
            }
        )
        rows[int(row["nm_id"])] = row

    correction = conn.execute(
        "SELECT corrected_composition_json,accepted_composition_json "
        "FROM sheet_vitrina_v1_wb_supply_box_corrections "
        "WHERE supply_id='40985996' AND status='applied' "
        "ORDER BY applied_at DESC LIMIT 1"
    ).fetchone()
    corrected = _loads(correction["corrected_composition_json"], {}) \
        if correction is not None else {}
    accepted = _loads(correction["accepted_composition_json"], {}) \
        if correction is not None else {}
    expected_gaps = {
        int(raw_nm_id): max(
            _decimal(raw_quantity) - _decimal(accepted.get(str(raw_nm_id))),
            ZERO,
        )
        for raw_nm_id, raw_quantity in corrected.items()
        if max(
            _decimal(raw_quantity) - _decimal(accepted.get(str(raw_nm_id))),
            ZERO,
        )
        > ZERO
    }
    actual_gaps = {
        int(nm_id): _decimal(row["quantity"])
        for nm_id, row in rows.items()
    }
    if correction is None or actual_gaps != expected_gaps or any(
        _decimal(row["capital_rub"]) <= ZERO for row in rows.values()
    ):
        raise WarehouseHistoricalRecoveryError(
            "current 40985996 discrepancy receipts do not match exact "
            "packed-minus-accepted source evidence: "
            + _canonical_json(
                {
                    "source_gaps": {
                        str(key): str(value)
                        for key, value in sorted(expected_gaps.items())
                    },
                    "receipt_gaps": {
                        str(key): str(value)
                        for key, value in sorted(actual_gaps.items())
                    },
                }
            )
        )
    return rows


def _projection_rows(
    corrected_by_date: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    target_nm_ids: Sequence[int],
    version_ids: Mapping[str, str],
    source_digest: str,
) -> list[dict[str, Any]]:
    rows = []
    for day in DATES:
        metrics_by_nm = _metric_rows(
            corrected_by_date[day],
            affected_nm_ids=target_nm_ids,
        )
        for nm_id, value in sorted(metrics_by_nm.items()):
            provenance = {
                "contract_name": CONTRACT_NAME,
                "source_digest": source_digest,
                "as_of_date": day,
                "functional_version_id": version_ids[day],
                "owned_projection": True,
            }
            item = {
                "as_of_date": day,
                "nm_id": int(nm_id),
                "metrics": dict(value.get("metrics") or {}),
                "presentation": dict(value.get("presentation") or {}),
                "provenance": provenance,
            }
            item["row_fingerprint"] = _fingerprint(item)
            rows.append(item)
    return rows


def _ready_updates(
    *,
    snapshots: Sequence[Mapping[str, Any]],
    corrected_by_date: Mapping[str, Sequence[Mapping[str, Any]]],
    target_nm_ids: Sequence[int],
    version_ids: Mapping[str, str],
    source_digest: str,
) -> list[dict[str, Any]]:
    target = {int(value) for value in target_nm_ids}
    warehouse_metrics = {
        day: _metric_rows(
            corrected_by_date[day],
            affected_nm_ids=target,
        )
        for day in DATES
    }
    updates = []
    for snapshot in snapshots:
        before = str(snapshot["plan_json"])
        plan = json.loads(before)
        sheet = _data_sheet(plan)
        rows = sheet.get("rows")
        if not isinstance(rows, list):
            raise WarehouseHistoricalRecoveryError(
                "ready snapshot DATA_VITRINA rows are missing"
            )
        dates = _date_columns(plan)
        by_id = {
            str(row[1]).strip(): row
            for row in rows
            if isinstance(row, list)
            and len(row) >= 2
            and isinstance(row[1], str)
            and "|" in str(row[1])
        }
        metadata = plan.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            raise WarehouseHistoricalRecoveryError(
                "ready snapshot metadata must be an object"
            )
        changed_cells = 0
        presentation_changes = 0
        touched_dates: list[str] = []
        for index, day in enumerate(dates):
            if day not in warehouse_metrics:
                continue
            touched_dates.append(day)
            metrics_by_nm = warehouse_metrics[day]
            for nm_id, item in metrics_by_nm.items():
                scope = "TOTAL" if int(nm_id) == 0 else f"SKU:{nm_id}"
                allowed_keys = (
                    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS
                    if int(nm_id) == 0
                    else OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS
                )
                for metric_key in allowed_keys:
                    row = by_id.get(f"{scope}|{metric_key}")
                    if row is None:
                        if int(nm_id) == 0 or int(nm_id) in target:
                            continue
                        continue
                    changed_cells += _set_ready_cell(
                        row,
                        index=index,
                        value=dict(item.get("metrics") or {}).get(metric_key),
                    )
                    presentation_changes += _set_ready_presentation(
                        metadata,
                        row_id=f"{scope}|{metric_key}",
                        day=day,
                        value=dict(item.get("presentation") or {}).get(metric_key),
                    )
        if not touched_dates:
            continue
        marker = {
            "contract_name": CONTRACT_NAME,
            "source_digest": source_digest,
            "date_from": min(touched_dates),
            "date_to": max(touched_dates),
            "affected_nm_ids": sorted(target),
            "functional_version_ids": {
                day: version_ids[day] for day in touched_dates
            },
        }
        marker_changed = int(
            metadata.get("warehouse_historical_recovery") != marker
        )
        metadata["warehouse_historical_recovery"] = marker
        if not (changed_cells or presentation_changes or marker_changed):
            continue
        after = _canonical_json(plan)
        updates.append(
            {
                "bundle_version": str(snapshot["bundle_version"]),
                "as_of_date": str(snapshot["as_of_date"]),
                "before_plan_sha256": "sha256:" + _sha(before),
                "after_plan_sha256": "sha256:" + _sha(after),
                "after_plan_json": after,
                "changed_cells": changed_cells,
                "presentation_changes": presentation_changes,
                "coverage_changes": marker_changed,
            }
        )
    return updates


def _set_ready_cell(row: list[Any], *, index: int, value: Any) -> int:
    cell_index = 2 + index
    while len(row) <= cell_index:
        row.append("")
    normalized = "" if value is None else float(value)
    current = row[cell_index]
    if current in (None, "") or normalized in (None, ""):
        same = current in (None, "") and normalized in (None, "")
    else:
        try:
            same = abs(
                Decimal(str(current).replace(",", "."))
                - Decimal(str(normalized))
            ) <= Decimal("0.0000005")
        except (ValueError, ArithmeticError):
            same = False
    if same:
        return 0
    row[cell_index] = normalized
    return 1


def _set_ready_presentation(
    metadata: dict[str, Any],
    *,
    row_id: str,
    day: str,
    value: Any,
) -> int:
    raw = metadata.setdefault("server_cell_presentation", {})
    if not isinstance(raw, dict):
        raise WarehouseHistoricalRecoveryError(
            "ready snapshot server_cell_presentation must be an object"
        )
    by_date = raw.setdefault(row_id, {})
    if not isinstance(by_date, dict):
        raise WarehouseHistoricalRecoveryError(
            f"ready snapshot presentation for {row_id} must be an object"
        )
    expected = deepcopy(dict(value)) if isinstance(value, Mapping) else None
    if expected:
        if by_date.get(day) == expected:
            return 0
        by_date[day] = expected
        return 1
    if day not in by_date:
        return 0
    by_date.pop(day, None)
    if not by_date:
        raw.pop(row_id, None)
    return 1


def _before_images(
    db_path: Path,
    *,
    plan: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    with _connect(db_path, read_only=True) as conn:
        for item in plan["versions"]:
            version_id = str(item["version_id"])
            version = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions "
                "WHERE version_id=?",
                (version_id,),
            ).fetchone()
            images.append(
                _before_image(
                    "sheet_vitrina_v1_warehouse_functional_versions",
                    {"version_id": version_id},
                    dict(version) if version is not None else None,
                )
            )
            snapshot_id = _stable_id(
                "wbsnap_hist",
                {
                    "version_id": version_id,
                    "date": item["business_date"],
                },
            )
            snapshot = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_wb_snapshots "
                "WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            images.append(
                _before_image(
                    "sheet_vitrina_v1_warehouse_wb_snapshots",
                    {"snapshot_id": snapshot_id},
                    dict(snapshot) if snapshot is not None else None,
                )
            )
            for row in payload["corrected_by_date"][item["business_date"]]:
                balance_key = {
                    "version_id": version_id,
                    "warehouse_key": str(row["warehouse_key"]),
                    "nm_id": int(row["nm_id"]),
                }
                balance = conn.execute(
                    "SELECT * FROM "
                    "sheet_vitrina_v1_warehouse_functional_balances "
                    "WHERE version_id=? AND warehouse_key=? AND nm_id=?",
                    tuple(balance_key.values()),
                ).fetchone()
                images.append(
                    _before_image(
                        "sheet_vitrina_v1_warehouse_functional_balances",
                        balance_key,
                        dict(balance) if balance is not None else None,
                    )
                )
        revision_id = str(payload["revision_id"])
        revision = conn.execute(
            f"SELECT * FROM {REVISION_TABLE} WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        images.append(
            _before_image(
                REVISION_TABLE,
                {"revision_id": revision_id},
                dict(revision) if revision is not None else None,
            )
        )
        for row in payload["projection_rows"]:
            projection_key = {
                "revision_id": revision_id,
                "as_of_date": str(row["as_of_date"]),
                "nm_id": int(row["nm_id"]),
            }
            projection = conn.execute(
                f"SELECT * FROM {ROW_TABLE} "
                "WHERE revision_id=? AND as_of_date=? AND nm_id=?",
                tuple(projection_key.values()),
            ).fetchone()
            images.append(
                _before_image(
                    ROW_TABLE,
                    projection_key,
                    dict(projection) if projection is not None else None,
                )
            )
            current = conn.execute(
                f"SELECT * FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date=? AND nm_id=?",
                (str(row["as_of_date"]), int(row["nm_id"])),
            ).fetchone()
            images.append(
                _before_image(
                    CURRENT_ROW_TABLE,
                    {
                        "as_of_date": str(row["as_of_date"]),
                        "nm_id": int(row["nm_id"]),
                    },
                    dict(current) if current is not None else None,
                )
            )
        state = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
        images.append(
            _before_image(
                STATE_TABLE,
                {"slot": 1},
                dict(state) if state is not None else None,
            )
        )
        for update in payload["ready_updates"]:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version=? AND as_of_date=?",
                (update["bundle_version"], update["as_of_date"]),
            ).fetchone()
            if row is None:
                raise WarehouseHistoricalRecoveryError(
                    "target ready snapshot disappeared before T1 capture"
                )
            images.append(
                _before_image(
                    "sheet_vitrina_v1_ready_snapshots",
                    {
                        "bundle_version": str(update["bundle_version"]),
                        "as_of_date": str(update["as_of_date"]),
                    },
                    dict(row),
                )
            )
    return images


def _apply_versions(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
    payload: Mapping[str, Any],
    applied_at: str,
) -> None:
    for item in plan["versions"]:
        day = str(item["business_date"])
        version_id = str(item["version_id"])
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                version_id,cutover_id,version_kind,effective_at,status,
                plan_fingerprint,local_source_digest,source_watermarks_json,
                created_at,business_effective_date,published_at
            ) VALUES(?,?,'historical_business_recovery',?,'good',?,?,?,?,?,?)
            """,
            (
                version_id,
                FUNCTIONAL_CUTOVER_ID,
                day + "T23:59:59Z",
                str(item["version_plan_fingerprint"]),
                str(plan["source_digest"]),
                _canonical_json(
                    {
                        "contract_name": CONTRACT_NAME,
                        "base_version_id": item["base_version_id"],
                        "target_scope": dict(plan["scope"]),
                    }
                ),
                applied_at,
                day,
                applied_at,
            ),
        )
        for row in payload["corrected_by_date"][day]:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                    version_id,warehouse_key,nm_id,quantity,wac_rub,
                    capital_rub,cost_covered_quantity,quality,certified,
                    wb_quantity,wb_in_way_to_client,wb_in_way_from_client,
                    provenance_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    version_id,
                    str(row["warehouse_key"]),
                    int(row["nm_id"]),
                    str(row["quantity"]),
                    row.get("wac_rub"),
                    str(row["capital_rub"]),
                    str(row["cost_covered_quantity"]),
                    str(row["quality"]),
                    int(row["certified"]),
                    str(row.get("wb_quantity") or "0"),
                    str(row.get("wb_in_way_to_client") or "0"),
                    str(row.get("wb_in_way_from_client") or "0"),
                    _canonical_json(row.get("provenance") or {}),
                ),
            )
        base_snapshot = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_wb_snapshots "
            "WHERE snapshot_id=?",
            (str(item["base_snapshot_id"]),),
        ).fetchone()
        if base_snapshot is None:
            raise WarehouseHistoricalRecoveryError(
                "base WB snapshot disappeared before historical clone"
            )
        snapshot = dict(base_snapshot)
        snapshot["snapshot_id"] = _stable_id(
            "wbsnap_hist",
            {"version_id": version_id, "date": day},
        )
        snapshot["version_id"] = version_id
        snapshot["created_at"] = applied_at
        columns = list(snapshot)
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(snapshot[column] for column in columns),
        )


def _apply_ready_updates(
    conn: sqlite3.Connection,
    *,
    updates: Sequence[Mapping[str, Any]],
) -> None:
    for item in updates:
        row = conn.execute(
            "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
            "WHERE bundle_version=? AND as_of_date=?",
            (item["bundle_version"], item["as_of_date"]),
        ).fetchone()
        if (
            row is None
            or "sha256:" + _sha(str(row["plan_json"]))
            != str(item["before_plan_sha256"])
        ):
            raise WarehouseHistoricalRecoveryError(
                "ready snapshot changed after exact dry-run"
            )
        changed = conn.execute(
            "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
            "WHERE bundle_version=? AND as_of_date=? AND plan_json=?",
            (
                item["after_plan_json"],
                item["bundle_version"],
                item["as_of_date"],
                str(row["plan_json"]),
            ),
        )
        if int(changed.rowcount or 0) != 1:
            raise WarehouseHistoricalRecoveryError(
                "ready snapshot optimistic update conflict"
            )


def _configured_nm_ids(conn: sqlite3.Connection) -> set[int]:
    columns = {
        str(row["name"])
        for row in conn.execute(
            "PRAGMA table_info(registry_upload_config_v2)"
        ).fetchall()
    }
    nm_column = "nm_id" if "nm_id" in columns else "nmid"
    enabled_column = (
        "enabled" if "enabled" in columns else "is_enabled"
    )
    bundle_clause = (
        "AND bundle_version=(SELECT bundle_version FROM "
        "registry_upload_current_state WHERE slot=1)"
        if "bundle_version" in columns
        else ""
    )
    return {
        int(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT {nm_column} FROM registry_upload_config_v2 "
            f"WHERE {enabled_column}=1 {bundle_clause}"
        ).fetchall()
        if row[0] is not None and int(row[0]) > 0
    }


def _non_target_digest(
    conn: sqlite3.Connection,
    *,
    target_nm_ids: Sequence[int],
) -> str:
    placeholders = ",".join("?" for _ in target_nm_ids)
    material = {
        "active": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active"
            ).fetchall()
        ],
        "outside_versions": [
            dict(row)
            for row in conn.execute(
                """
                SELECT version_id,version_kind,effective_at,status,
                       plan_fingerprint,local_source_digest,created_at,
                       business_effective_date,published_at
                FROM sheet_vitrina_v1_warehouse_functional_versions
                WHERE COALESCE(business_effective_date,substr(effective_at,1,10))
                      NOT BETWEEN ? AND ?
                ORDER BY version_id
                """,
                (DATE_FROM, DATE_TO),
            ).fetchall()
        ],
        "outside_balances": [
            dict(row)
            for row in conn.execute(
                """
                SELECT balance.*
                FROM sheet_vitrina_v1_warehouse_functional_balances balance
                JOIN sheet_vitrina_v1_warehouse_functional_versions version
                  ON version.version_id=balance.version_id
                WHERE COALESCE(
                        version.business_effective_date,
                        substr(version.effective_at,1,10)
                      ) NOT BETWEEN ? AND ?
                ORDER BY balance.version_id,balance.warehouse_key,balance.nm_id
                """,
                (DATE_FROM, DATE_TO),
            ).fetchall()
        ],
        "outside_projection_current": [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {CURRENT_ROW_TABLE} "
                "WHERE as_of_date NOT BETWEEN ? AND ? "
                "ORDER BY as_of_date,nm_id",
                (DATE_FROM, DATE_TO),
            ).fetchall()
        ],
        "non_target_balances": [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT balance.*
                FROM sheet_vitrina_v1_warehouse_functional_balances balance
                JOIN sheet_vitrina_v1_warehouse_functional_versions version
                  ON version.version_id=balance.version_id
                WHERE COALESCE(
                        version.business_effective_date,
                        substr(version.effective_at,1,10)
                      ) BETWEEN ? AND ?
                  AND version.version_kind!='historical_business_recovery'
                  AND balance.nm_id NOT IN ({placeholders})
                ORDER BY balance.version_id,balance.warehouse_key,balance.nm_id
                """,
                (DATE_FROM, DATE_TO, *target_nm_ids),
            ).fetchall()
        ],
    }
    return _fingerprint(material)


def _verify_projection_readback(
    conn: sqlite3.Connection,
    *,
    revision_id: str,
    expected_rows: Sequence[Mapping[str, Any]],
) -> int:
    expected = sorted(
        (
            str(row["as_of_date"]),
            int(row["nm_id"]),
            str(row["row_fingerprint"]),
        )
        for row in expected_rows
    )
    immutable = [
        (
            str(row["as_of_date"]),
            int(row["nm_id"]),
            str(row["row_fingerprint"]),
        )
        for row in conn.execute(
            f"SELECT as_of_date,nm_id,row_fingerprint FROM {ROW_TABLE} "
            "WHERE revision_id=? ORDER BY as_of_date,nm_id",
            (revision_id,),
        ).fetchall()
    ]
    if immutable != expected:
        raise WarehouseHistoricalRecoveryError(
            "immutable business-projection row readback mismatch"
        )
    if not expected:
        return 0
    expected_by_key = {
        (day, nm_id): row_fingerprint
        for day, nm_id, row_fingerprint in expected
    }
    current_by_key = {
        (str(row["as_of_date"]), int(row["nm_id"])): (
            str(row["revision_id"]),
            str(row["row_fingerprint"]),
        )
        for row in conn.execute(
            f"SELECT as_of_date,nm_id,revision_id,row_fingerprint "
            f"FROM {CURRENT_ROW_TABLE} WHERE as_of_date BETWEEN ? AND ?",
            (expected[0][0], expected[-1][0]),
        ).fetchall()
    }
    mismatches = [
        [day, nm_id]
        for (day, nm_id), row_fingerprint in expected_by_key.items()
        if current_by_key.get((day, nm_id))
        != (revision_id, row_fingerprint)
    ]
    if mismatches:
        raise WarehouseHistoricalRecoveryError(
            "current business-projection row readback mismatch: "
            + _canonical_json(mismatches[:20])
        )
    return len(expected)


def _versions_already_applied(
    conn: sqlite3.Connection,
    *,
    version_manifest: Sequence[Mapping[str, Any]],
) -> bool:
    return all(
        conn.execute(
            "SELECT 1 FROM sheet_vitrina_v1_warehouse_functional_versions "
            "WHERE version_id=? AND plan_fingerprint=?",
            (str(item["version_id"]), str(item["version_plan_fingerprint"])),
        ).fetchone()
        is not None
        for item in version_manifest
    )


def _target_balance_digest(
    rows: Sequence[Mapping[str, Any]],
    target_nm_ids: Iterable[int],
) -> str:
    target = {int(value) for value in target_nm_ids}
    return _fingerprint(
        [
            {
                key: row.get(key)
                for key in (
                    "warehouse_key",
                    "nm_id",
                    "quantity",
                    "wac_rub",
                    "capital_rub",
                    "cost_covered_quantity",
                    "quality",
                    "certified",
                    "wb_quantity",
                    "wb_in_way_to_client",
                    "wb_in_way_from_client",
                )
            }
            for row in rows
            if int(row["nm_id"]) in target
        ]
    )


def _balance_total(
    rows: Sequence[Mapping[str, Any]],
    target_nm_ids: Iterable[int],
) -> dict[str, str]:
    target = {int(value) for value in target_nm_ids}
    selected = [row for row in rows if int(row["nm_id"]) in target]
    return {
        "quantity": str(
            sum((_decimal(row["quantity"]) for row in selected), ZERO)
        ),
        "capital_rub": str(
            sum((_decimal(row["capital_rub"]) for row in selected), ZERO)
        ),
    }


def _row_semantic(value: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal | None]:
    wac = value.get("wac_rub")
    return (
        _decimal(value.get("quantity")),
        _decimal(value.get("capital_rub")),
        _decimal(wac) if wac not in (None, "") else None,
    )


def _nested_supply_id(value: Any) -> str:
    result = ""

    def visit(item: Any) -> None:
        nonlocal result
        if result:
            return
        if isinstance(item, Mapping):
            candidate = str(
                item.get("supply_id") or item.get("wb_supply_id") or ""
            )
            if candidate:
                result = candidate
                return
            for nested in item.values():
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return result


def _before_image(
    table: str,
    key: Mapping[str, Any],
    before: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "table": str(table),
        "key": dict(key),
        "before": dict(before) if before is not None else None,
        "after": None,
    }


def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(value)
        for key, value in plan.items()
        if not str(key).startswith("_")
    }


def public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return the machine-readable manifest without internal apply payloads."""

    return _public_plan(plan)


def _connect(
    db_path: Path,
    *,
    read_only: bool,
) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(
            f"file:{Path(db_path).resolve()}?mode=ro",
            uri=True,
            timeout=300,
        )
        conn.execute("PRAGMA query_only=ON")
    else:
        conn = sqlite3.connect(Path(db_path).resolve(), timeout=300)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=300000")
    return conn


def _loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return deepcopy(default)
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return deepcopy(default)


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + _sha(_canonical_json(value))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: Any) -> str:
    return prefix + "_" + _sha(_canonical_json(value))[:24]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
