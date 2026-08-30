#!/usr/bin/env python3
"""Exact two-phase WBC0027 product-capital and economics recovery."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_historical_missing_repair import (  # noqa: E402
    HISTORICAL_REPAIR_METADATA_KEY,
    SOURCE_OPERATION_DIGEST,
    SOURCE_OPERATION_ID,
    _is_missing,
    _repair_payload,
    _source_operation,
    _source_rows,
    _target_cells,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_PRODUCT_CAPITAL_METRIC_KEYS,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402
from packages.application.warehouse_business_projection import (  # noqa: E402
    CURRENT_ROW_TABLE,
    OUTBOX_TABLE,
    REVISION_TABLE,
    ROW_TABLE,
    STATE_TABLE,
    _fingerprint,
    _metric_rows,
    _persist_projection_revision,
    _version_balances,
    ensure_warehouse_business_projection_schema,
    materialize_warehouse_business_projection_reconciliation,
    reconcile_warehouse_business_projection,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryState,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)
from packages.application.warehouse_sync_lock import warehouse_sync_lock  # noqa: E402


CONTRACT = "wbc0027_capital_recovery_v1"
MUTATION_KIND_PRODUCT = "wbc0027_product_capital_version_bound_recovery"
MUTATION_KIND_ECONOMICS = "wbc0027_functional_economics_missing_recovery"
PRIMARY_DATES = tuple(f"2026-08-{day:02d}" for day in range(17, 30))
SECONDARY_DATES = ("2026-08-13", "2026-08-14", "2026-08-16")
PRODUCT_DATES = (*SECONDARY_DATES, *PRIMARY_DATES)
EVIDENCE_BLOCKED_DATE = "2026-08-15"
HARD_NON_TARGET_DATE = "2026-08-30"
ECONOMICS_DATES = ("2026-08-26", "2026-08-29")
EXPECTED_PRIMARY_ROWS = 936
EXPECTED_PRIMARY_CELLS = 19_656
EXPECTED_PRIMARY_MISMATCHES = 7_655
EXPECTED_EVENT_MISMATCHES = 7_639
EXPECTED_SEPARATE_MISMATCHES = 16
EXPECTED_SECONDARY_ROWS = 216
EXPECTED_SECONDARY_MISMATCHES = 1_791
EXPECTED_ECONOMICS_LOGICAL_REPAIRS = 298
EXPECTED_ECONOMICS_PERSISTED_REPAIRS = 472


class Wbc0027RecoveryError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _sha_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _query_only(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _generation(runtime_dir: Path) -> dict[str, Any]:
    manifest = StoreRegistry(runtime_dir).load(require_files=True)
    operational = manifest.operational
    return {
        "generation_id": operational.generation_id,
        "manifest_sha256": manifest.manifest_sha256,
        "schema_version": operational.schema_revision,
        "operational_path": str((runtime_dir / operational.relative_path).resolve()),
    }


def _deployed_sha(path: Path, expected: str) -> str:
    actual = path.read_text(encoding="utf-8").strip().lower()
    if len(actual) != 40 or any(item not in "0123456789abcdef" for item in actual):
        raise Wbc0027RecoveryError("runtime marker is not an exact SHA")
    if expected and actual != expected.lower():
        raise Wbc0027RecoveryError("reviewed deployed SHA no longer matches runtime")
    return actual


def _coverage_bindings(conn: sqlite3.Connection) -> dict[str, str]:
    bindings: dict[str, str] = {}
    rows = conn.execute(
        "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date DESC"
    ).fetchall()
    for row in rows:
        metadata = dict(json.loads(str(row["plan_json"])).get("metadata") or {})
        coverage = metadata.get("warehouse_history_coverage") or {}
        if not isinstance(coverage, Mapping):
            continue
        for day, raw_entry in coverage.items():
            if not isinstance(raw_entry, Mapping):
                continue
            version_id = str(raw_entry.get("functional_version_id") or "")
            if version_id:
                bindings.setdefault(str(day), version_id)
    return bindings


def _non_target_digest(conn: sqlite3.Connection, excluded_dates: set[str]) -> str:
    rows = conn.execute(
        f"SELECT as_of_date,nm_id,revision_id,row_fingerprint,provenance_json "
        f"FROM {CURRENT_ROW_TABLE} ORDER BY as_of_date,nm_id"
    ).fetchall()
    return _digest(
        [dict(row) for row in rows if str(row["as_of_date"]) not in excluded_dates]
    )


def _product_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    bindings = _coverage_bindings(conn)
    if bindings.get(EVIDENCE_BLOCKED_DATE):
        raise Wbc0027RecoveryError("15.08 unexpectedly acquired a functional binding")
    rows: list[dict[str, Any]] = []
    before_rows: list[dict[str, Any]] = []
    date_receipts: list[dict[str, Any]] = []
    primary_mismatches = event_mismatches = separate_mismatches = 0
    secondary_mismatches = 0
    for day in PRODUCT_DATES:
        version_id = str(bindings.get(day) or "")
        if not version_id:
            raise Wbc0027RecoveryError(f"exact functional binding is missing for {day}")
        version_row = conn.execute(
            "SELECT plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=? AND status='good'",
            (version_id,),
        ).fetchone()
        if version_row is None or not str(version_row["plan_fingerprint"] or ""):
            raise Wbc0027RecoveryError(f"exact functional source digest is missing for {day}")
        source_digest = str(version_row["plan_fingerprint"])
        current = conn.execute(
            f"SELECT * FROM {CURRENT_ROW_TABLE} WHERE as_of_date=? ORDER BY nm_id",
            (day,),
        ).fetchall()
        if len(current) != 72:
            raise Wbc0027RecoveryError(f"{day} current projection scope is not 72 rows")
        current_ids = sorted({int(item["nm_id"]) for item in current if int(item["nm_id"]) > 0})
        exact = _metric_rows(
            _version_balances(conn, version_id=version_id),
            affected_nm_ids=current_ids,
        )
        date_mismatches = date_event = date_separate = 0
        for current_row in current:
            nm_id = int(current_row["nm_id"])
            expected = exact.get(nm_id)
            if expected is None:
                raise Wbc0027RecoveryError(f"{day}/{nm_id} exact scope is incomplete")
            actual_metrics = json.loads(str(current_row["metrics_json"]))
            provenance_before = json.loads(str(current_row["provenance_json"]))
            for metric_key in OWN_PRODUCT_CAPITAL_METRIC_KEYS:
                if actual_metrics.get(metric_key) == expected["metrics"].get(metric_key):
                    continue
                date_mismatches += 1
                if str(provenance_before.get("source") or "") == "canonical_own_capital_events":
                    date_event += 1
                else:
                    date_separate += 1
            provenance = {
                "contract_name": "warehouse_business_projection",
                "contract_version": 1,
                "source": "canonical_functional_warehouse_version",
                "business_effective_date": day,
                "as_of_date": day,
                "snapshot_date": day,
                "base_version_id": version_id,
                "published_version_id": version_id,
                "functional_version_id": version_id,
                "source_digest": source_digest,
                "published_at": "__APPLIED_AT__",
                "missing_exact_projection_date": False,
            }
            provenance["publication_identity"] = _digest(
                {"functional_version_id": version_id, "snapshot_date": day, "nm_id": nm_id, "metrics": expected["metrics"]}
            )
            material = {
                "as_of_date": day,
                "nm_id": nm_id,
                "metrics": dict(expected["metrics"]),
                "presentation": dict(expected["presentation"]),
                "provenance": provenance,
            }
            rows.append({**material, "row_fingerprint": _fingerprint(material)})
            before_rows.append(dict(current_row))
        if day in PRIMARY_DATES:
            primary_mismatches += date_mismatches
            event_mismatches += date_event
            separate_mismatches += date_separate
        else:
            secondary_mismatches += date_mismatches
        date_receipts.append(
            {
                "as_of_date": day,
                "functional_version_id": version_id,
                "scope_count": len(current),
                "cell_count": len(current) * 21,
                "mismatch_count": date_mismatches,
                "event_path_mismatch_count": date_event,
                "separate_mismatch_count": date_separate,
            }
        )
    hard_non_target = reconcile_warehouse_business_projection(
        conn,
        target_dates=[HARD_NON_TARGET_DATE],
    )
    counts = {
        "primary_row_count": sum(item["scope_count"] for item in date_receipts if item["as_of_date"] in PRIMARY_DATES),
        "primary_cell_count": sum(item["cell_count"] for item in date_receipts if item["as_of_date"] in PRIMARY_DATES),
        "primary_mismatch_count": primary_mismatches,
        "event_path_mismatch_count": event_mismatches,
        "separate_20260821_mismatch_count": separate_mismatches,
        "secondary_row_count": sum(item["scope_count"] for item in date_receipts if item["as_of_date"] in SECONDARY_DATES),
        "secondary_mismatch_count": secondary_mismatches,
        "proposed_row_count": len(rows),
    }
    expected = {
        "primary_row_count": EXPECTED_PRIMARY_ROWS,
        "primary_cell_count": EXPECTED_PRIMARY_CELLS,
        "primary_mismatch_count": EXPECTED_PRIMARY_MISMATCHES,
        "event_path_mismatch_count": EXPECTED_EVENT_MISMATCHES,
        "separate_20260821_mismatch_count": EXPECTED_SEPARATE_MISMATCHES,
        "secondary_row_count": EXPECTED_SECONDARY_ROWS,
        "secondary_mismatch_count": EXPECTED_SECONDARY_MISMATCHES,
        "proposed_row_count": EXPECTED_PRIMARY_ROWS + EXPECTED_SECONDARY_ROWS,
    }
    if counts != expected:
        raise Wbc0027RecoveryError(f"product-capital qualification drifted: {counts}")
    if (
        hard_non_target.get("status") != "published_exact"
        or int(hard_non_target.get("mismatch_count") or 0) != 0
    ):
        raise Wbc0027RecoveryError("30.08 hard non-target is no longer exact")
    state_row = conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE slot=1").fetchone()
    if state_row is None:
        raise Wbc0027RecoveryError("product-capital ready pointer is missing")
    return {
        "target_dates": list(PRODUCT_DATES),
        "primary_dates": list(PRIMARY_DATES),
        "secondary_dates": list(SECONDARY_DATES),
        "evidence_blocked": [
            {
                "as_of_date": EVIDENCE_BLOCKED_DATE,
                "status": "EVIDENCE_BLOCKED",
                "reason": "immutable_same_date_functional_version_missing",
            }
        ],
        "hard_non_target": hard_non_target,
        "counts": counts,
        "dates": date_receipts,
        "before_rows": before_rows,
        "proposed_rows": rows,
        "before_target_digest": _digest(before_rows),
        "after_target_digest": _digest(rows),
        "non_target_digest": _non_target_digest(conn, set(PRODUCT_DATES)),
        "before_state": dict(state_row),
    }


def _ready_identity(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (str(row["bundle_version"]), str(row["as_of_date"]), str(row["snapshot_id"]))


def _set_ready_cell(plan: dict[str, Any], *, row_id: str, day: str, value: Any) -> None:
    sheet = next(item for item in plan.get("sheets") or [] if item.get("sheet_name") == "DATA_VITRINA")
    column = list(sheet["header"]).index(day)
    row = next(item for item in sheet["rows"] if str(item[1]) == row_id)
    row[column] = value


def _economics_plan(conn: sqlite3.Connection) -> dict[str, Any]:
    _source_operation(conn)
    source_by_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    for raw in _source_rows(conn):
        before = json.loads(str(raw["before_json"]))
        plan = json.loads(str(before["plan_json"])) if isinstance(before.get("plan_json"), str) else deepcopy(before["plan_json"])
        identity = (str(before["bundle_version"]), str(before["as_of_date"]), str(before["snapshot_id"]))
        source_by_identity[identity] = plan
    current_rows = conn.execute(
        "SELECT bundle_version,as_of_date,snapshot_id,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date"
    ).fetchall()
    patches: list[dict[str, Any]] = []
    logical_repairs = 0
    persisted_repairs = 0
    blocked: set[str] = set()
    for current_row in current_rows:
        identity = _ready_identity(current_row)
        source = source_by_identity.get(identity)
        if source is None:
            continue
        before = json.loads(str(current_row["plan_json"]))
        after = deepcopy(before)
        repaired_dates: list[str] = []
        changed_cells: list[str] = []
        for day in ECONOMICS_DATES:
            if day not in list(before.get("date_columns") or []):
                continue
            current_cells = _target_cells(before, day)
            source_cells = _target_cells(source, day)
            for row_id, current_value in sorted(current_cells.items()):
                source_value = source_cells.get(row_id)
                if not _is_missing(current_value) or _is_missing(source_value):
                    if _is_missing(current_value) and _is_missing(source_value) and identity[1] == day:
                        blocked.add(f"{day}|{row_id}")
                    continue
                _set_ready_cell(after, row_id=row_id, day=day, value=source_value)
                changed_cells.append(f"{day}|{row_id}")
                persisted_repairs += 1
                if identity[1] == day:
                    logical_repairs += 1
            if day == "2026-08-29" and identity[1] == day:
                if any(_is_missing(value) for value in _target_cells(after, day).values()):
                    raise Wbc0027RecoveryError("29.08 qualified economics image remains missing")
                # Copy only the already verified exact evidence/registry closure.
                repaired = _repair_payload(
                    before_payload=after,
                    source={
                        "cells": _target_cells(source, day),
                        "functional_version_id": str(
                            ((source.get("metadata") or {}).get("warehouse_history_coverage") or {}).get(day, {}).get("functional_version_id")
                            or ""
                        ),
                        "coverage": deepcopy(((source.get("metadata") or {}).get("warehouse_history_coverage") or {}).get(day) or {}),
                        "date_evidence": deepcopy(((((source.get("metadata") or {}).get("functional_economics_backfill") or {}).get("inventory_cost_publication") or {}).get("date_evidence") or {}).get(day) or {}),
                        "presentation": {},
                    },
                    business_date=day,
                )
                if _target_cells(repaired, day) != _target_cells(after, day):
                    raise Wbc0027RecoveryError("29.08 source would overwrite non-missing economics")
                after = repaired
            repaired_dates.append(day)
        before_json = _json(before)
        after_json = _json(after)
        if before_json != after_json:
            patches.append(
                {
                    "identity": list(identity),
                    "business_dates": sorted(set(repaired_dates)),
                    "changed_cells": changed_cells,
                    "before_plan_json": before_json,
                    "after_plan_json": after_json,
                    "before_sha256": _sha_text(before_json),
                    "after_sha256": _sha_text(after_json),
                }
            )
    if logical_repairs != EXPECTED_ECONOMICS_LOGICAL_REPAIRS:
        raise Wbc0027RecoveryError(f"economics logical qualification drifted: {logical_repairs}")
    if persisted_repairs != EXPECTED_ECONOMICS_PERSISTED_REPAIRS:
        raise Wbc0027RecoveryError(f"economics persisted qualification drifted: {persisted_repairs}")
    if len(blocked) != 12:
        raise Wbc0027RecoveryError(f"26.08 evidence-blocked economics shape drifted: {len(blocked)}")
    patch_identities = {tuple(item["identity"]) for item in patches}
    non_target_digest = _digest(
        [
            {
                "identity": list(_ready_identity(row)),
                "plan_sha256": _sha_text(str(row["plan_json"])),
            }
            for row in current_rows
            if _ready_identity(row) not in patch_identities
        ]
    )
    return {
        "target_dates": list(ECONOMICS_DATES),
        "logical_repair_count": logical_repairs,
        "persisted_repair_count": persisted_repairs,
        "patch_count": len(patches),
        "source_operation_id": SOURCE_OPERATION_ID,
        "source_digest": SOURCE_OPERATION_DIGEST,
        "protected_invariant": {
            "as_of_date": "2026-08-26",
            "nm_id": 428853741,
            "unit_cost_rub": "117.537167",
            "status": "separate_exact_invariant_preserved",
        },
        "evidence_blocked": sorted(blocked),
        "patches": patches,
        "before_digest": _digest([item["before_sha256"] for item in patches]),
        "after_digest": _digest([item["after_sha256"] for item in patches]),
        "non_target_digest": non_target_digest,
    }


def build_plan(runtime_dir: Path, deployed_sha_file: Path, expected_sha: str) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    deployed_sha = _deployed_sha(deployed_sha_file.resolve(), expected_sha)
    generation = _generation(runtime_dir)
    db_path = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir).db_path
    with _query_only(db_path) as conn:
        product = _product_plan(conn)
        economics = _economics_plan(conn)
        ready_digest = _digest(
            [
                dict(row)
                for row in conn.execute(
                    "SELECT bundle_version,as_of_date,snapshot_id,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date"
                ).fetchall()
            ]
        )
        outbox_digest = _digest(
            [dict(row) for row in conn.execute(f"SELECT * FROM {OUTBOX_TABLE} ORDER BY request_id").fetchall()]
        )
    plan = {
        "contract_name": CONTRACT,
        "contract_version": 1,
        "mode": "recovery",
        "status": "ready",
        "created_at": _now(),
        "deployed_sha": deployed_sha,
        "storage_generation": generation,
        "product_capital": product,
        "functional_economics": economics,
        "ready_snapshot_digest": ready_digest,
        "outbox_digest": outbox_digest,
        "production_mutation_count": 0,
        "one_submit_per_phase": True,
    }
    material = {key: value for key, value in plan.items() if key not in {"created_at", "plan_fingerprint"}}
    plan["plan_fingerprint"] = _digest(material)
    plan["product_operation_id"] = recovery_operation_id(MUTATION_KIND_PRODUCT, plan["plan_fingerprint"])
    plan["economics_operation_id"] = recovery_operation_id(MUTATION_KIND_ECONOMICS, plan["plan_fingerprint"])
    return plan


def _validate_plan(plan: Mapping[str, Any], current: Mapping[str, Any], fingerprint: str) -> None:
    if (
        plan.get("contract_name") != CONTRACT
        or plan.get("mode") != "recovery"
        or plan.get("status") != "ready"
        or plan.get("plan_fingerprint") != fingerprint
        or current.get("plan_fingerprint") != fingerprint
        or current.get("storage_generation") != plan.get("storage_generation")
        or current.get("product_capital", {}).get("before_target_digest")
        != plan.get("product_capital", {}).get("before_target_digest")
        or current.get("functional_economics", {}).get("before_digest")
        != plan.get("functional_economics", {}).get("before_digest")
    ):
        raise Wbc0027RecoveryError("reviewed recovery plan failed exact CAS")


def _t1_product(runtime: RegistryUploadDbBackedRuntime, plan: Mapping[str, Any], applied_at: str) -> dict[str, Any]:
    product = dict(plan["product_capital"])
    fingerprint = str(plan["plan_fingerprint"])
    revision_id = "whbpr_wbc0027_" + fingerprint.removeprefix("sha256:")[:20]
    proposed_rows = deepcopy(product["proposed_rows"])
    for row in proposed_rows:
        row["provenance"]["published_at"] = applied_at
        material = {key: row[key] for key in ("as_of_date", "nm_id", "metrics", "presentation", "provenance")}
        row["row_fingerprint"] = _fingerprint(material)
    registry = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path)
    before_images = []
    for before, after in zip(product["before_rows"], proposed_rows, strict=True):
        before_images.append(
            {
                "table": CURRENT_ROW_TABLE,
                "key": {"as_of_date": before["as_of_date"], "nm_id": before["nm_id"]},
                "before": before,
                "after": {
                    "as_of_date": after["as_of_date"],
                    "nm_id": after["nm_id"],
                    "revision_id": revision_id,
                    "metrics_json": _json(after["metrics"]),
                    "presentation_json": _json(after["presentation"]),
                    "provenance_json": _json(after["provenance"]),
                    "row_fingerprint": after["row_fingerprint"],
                    "published_at": applied_at,
                },
            }
        )
    changed_rows = sum(
        before["row_fingerprint"] != after["row_fingerprint"]
        for before, after in zip(product["before_rows"], proposed_rows, strict=True)
    )
    changed_cells = sum(
        json.loads(str(before["metrics_json"])).get(metric_key)
        != after["metrics"].get(metric_key)
        for before, after in zip(product["before_rows"], proposed_rows, strict=True)
        for metric_key in OWN_PRODUCT_CAPITAL_METRIC_KEYS
    )
    before_state = dict(product["before_state"])
    after_state = {
        "slot": 1,
        "revision_no": int(before_state["revision_no"]) + 1,
        "revision_id": revision_id,
        "source_revision": str(product["after_target_digest"]),
        "business_effective_date": min(product["target_dates"]),
        "published_at": applied_at,
        "status": "ready",
        "updated_at": applied_at,
    }
    before_images.append(
        {
            "table": STATE_TABLE,
            "key": {"slot": 1},
            "before": before_state,
            "after": after_state,
        }
    )
    revision_after = {
        "revision_id": revision_id,
        "stable_source_id": "wbc0027:version-bound-history",
        "source_revision": str(product["after_target_digest"]),
        "business_effective_date": min(product["target_dates"]),
        "published_at": applied_at,
        "status": "active",
        "plan_fingerprint": fingerprint,
        "base_version_id": "multiple_exact_functional_versions",
        "published_version_id": "multiple_exact_functional_versions",
        "affected_nm_ids_json": _json(
            sorted({int(row["nm_id"]) for row in proposed_rows if int(row["nm_id"]) > 0})
        ),
        "affected_dates_json": _json(product["target_dates"]),
        "source_kind": "wbc0027_exact_functional_recovery",
        "changed_row_count": changed_rows,
        "changed_cell_count": changed_cells,
        "diagnostics_json": _json(
            {
                "affected_dates": product["target_dates"],
                "closed_allowlist_size": len(OWN_PRODUCT_CAPITAL_METRIC_KEYS),
                "recovery": True,
            }
        ),
        "error": None,
        "created_at": applied_at,
        "completed_at": applied_at,
    }
    before_images.append(
        {
            "table": REVISION_TABLE,
            "key": {"revision_id": revision_id},
            "before": None,
            "after": revision_after,
        }
    )
    before_images.extend(
        {
            "table": ROW_TABLE,
            "key": {
                "revision_id": revision_id,
                "as_of_date": after["as_of_date"],
                "nm_id": after["nm_id"],
            },
            "before": None,
            "after": {
                "revision_id": revision_id,
                "as_of_date": after["as_of_date"],
                "nm_id": after["nm_id"],
                "metrics_json": _json(after["metrics"]),
                "presentation_json": _json(after["presentation"]),
                "provenance_json": _json(after["provenance"]),
                "row_fingerprint": after["row_fingerprint"],
            },
        }
        for after in proposed_rows
    )
    recovery = registry.prepare_t1(
        mutation_kind=MUTATION_KIND_PRODUCT,
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={"dates": product["target_dates"], "row_count": len(proposed_rows), "deployed_sha": plan["deployed_sha"]},
        before_images=before_images,
        expected_after_images=[item["after"] for item in before_images],
        source_digest=str(product["after_target_digest"]),
        non_target_digest=str(product["non_target_digest"]),
        read_bytes=len(_json(before_images).encode("utf-8")),
    )
    operation_id = str(recovery["operation_id"])
    if recovery.get("lifecycle") == RecoveryState.RETAINED.value:
        return {"status": "idempotent", "operation_id": operation_id, "submit_count": 1}
    if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(operation_id, expected_source_digest=str(product["after_target_digest"]))
    if recovery.get("lifecycle") != RecoveryState.MUTATION_RUNNING.value:
        raise Wbc0027RecoveryError("product T1 recovery is not mutation-ready")
    with sqlite3.connect(runtime.db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        current_state = conn.execute(
            f"SELECT * FROM {STATE_TABLE} WHERE slot=1"
        ).fetchone()
        if current_state is None or dict(current_state) != product["before_state"]:
            raise Wbc0027RecoveryError("product ready-pointer CAS drifted")
        ready_digest = _digest(
            [
                dict(row)
                for row in conn.execute(
                    "SELECT bundle_version,as_of_date,snapshot_id,plan_json "
                    "FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date"
                ).fetchall()
            ]
        )
        if ready_digest != plan["ready_snapshot_digest"]:
            raise Wbc0027RecoveryError("product ready-snapshot CAS drifted")
        outbox_digest = _digest(
            [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {OUTBOX_TABLE} ORDER BY request_id"
                ).fetchall()
            ]
        )
        if outbox_digest != plan["outbox_digest"]:
            raise Wbc0027RecoveryError("product replay-signal CAS drifted")
        if _non_target_digest(conn, set(product["target_dates"])) != product["non_target_digest"]:
            raise Wbc0027RecoveryError("product non-target CAS drifted")
        current_target_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {CURRENT_ROW_TABLE} WHERE as_of_date IN ("
                + ",".join("?" for _ in product["target_dates"])
                + ") ORDER BY as_of_date,nm_id",
                tuple(product["target_dates"]),
            ).fetchall()
        ]
        if _digest(current_target_rows) != product["before_target_digest"]:
            raise Wbc0027RecoveryError("product target CAS drifted")
        result = _persist_projection_revision(
            conn,
            revision_id=revision_id,
            stable_source_id="wbc0027:version-bound-history",
            source_revision=str(product["after_target_digest"]),
            business_effective_date=min(product["target_dates"]),
            published_at=applied_at,
            plan_fingerprint=fingerprint,
            base_version_id="multiple_exact_functional_versions",
            published_version_id="multiple_exact_functional_versions",
            affected_nm_ids=sorted({int(row["nm_id"]) for row in proposed_rows if int(row["nm_id"]) > 0}),
            source_kind="wbc0027_exact_functional_recovery",
            rows=proposed_rows,
            diagnostics={"affected_dates": product["target_dates"], "recovery": True, "closed_allowlist_size": len(OWN_PRODUCT_CAPITAL_METRIC_KEYS)},
        )
        proposed_ids = {
            int(row["nm_id"])
            for row in proposed_rows
            if int(row["nm_id"]) > 0
        }
        pending_rows = conn.execute(
            f"SELECT request_id,business_effective_date,affected_nm_ids_json FROM {OUTBOX_TABLE} "
            "WHERE status='pending_exact_functional' ORDER BY request_id"
        ).fetchall()
        resolved_ids = []
        for pending in pending_rows:
            if str(pending["business_effective_date"]) not in set(product["target_dates"]):
                continue
            affected = {
                int(value)
                for value in json.loads(str(pending["affected_nm_ids_json"]))
                if int(value) > 0
            }
            if affected.issubset(proposed_ids):
                resolved_ids.append(str(pending["request_id"]))
        if resolved_ids:
            placeholders = ",".join("?" for _ in resolved_ids)
            conn.execute(
                f"UPDATE {OUTBOX_TABLE} SET status='published_exact',finished_at=?,error=NULL "
                f"WHERE request_id IN ({placeholders}) AND status='pending_exact_functional'",
                (applied_at, *resolved_ids),
            )
        conn.commit()
    registry.retain(operation_id, after_digest=str(product["after_target_digest"]), non_target_digest=str(product["non_target_digest"]))
    return {
        **result,
        "operation_id": operation_id,
        "submit_count": 1,
        "resolved_replay_signal_count": len(resolved_ids),
    }


def _t1_economics(runtime: RegistryUploadDbBackedRuntime, plan: Mapping[str, Any]) -> dict[str, Any]:
    economics = dict(plan["functional_economics"])
    fingerprint = str(plan["plan_fingerprint"])
    registry = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path)
    before_images = [
        {
            "table": "sheet_vitrina_v1_ready_snapshots",
            "key": {"bundle_version": patch["identity"][0], "as_of_date": patch["identity"][1], "snapshot_id": patch["identity"][2]},
            "before": {
                "bundle_version": patch["identity"][0], "as_of_date": patch["identity"][1], "snapshot_id": patch["identity"][2], "plan_json": patch["before_plan_json"]
            },
            "after": {
                "bundle_version": patch["identity"][0], "as_of_date": patch["identity"][1], "snapshot_id": patch["identity"][2], "plan_json": patch["after_plan_json"]
            },
        }
        for patch in economics["patches"]
    ]
    recovery = registry.prepare_t1(
        mutation_kind=MUTATION_KIND_ECONOMICS,
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={"dates": economics["target_dates"], "logical_repair_count": economics["logical_repair_count"], "source_operation_id": SOURCE_OPERATION_ID},
        before_images=before_images,
        expected_after_images=[item["after"] for item in before_images],
        source_digest=SOURCE_OPERATION_DIGEST,
        non_target_digest=str(economics["non_target_digest"]),
        read_bytes=len(_json(before_images).encode("utf-8")),
    )
    operation_id = str(recovery["operation_id"])
    if recovery.get("lifecycle") == RecoveryState.RETAINED.value:
        return {"status": "idempotent", "operation_id": operation_id, "submit_count": 1}
    if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(operation_id, expected_source_digest=SOURCE_OPERATION_DIGEST)
    if recovery.get("lifecycle") != RecoveryState.MUTATION_RUNNING.value:
        raise Wbc0027RecoveryError("economics T1 recovery is not mutation-ready")
    with sqlite3.connect(runtime.db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        patch_identities = {tuple(item["identity"]) for item in economics["patches"]}
        current_non_target = _digest(
            [
                {
                    "identity": list(_ready_identity(row)),
                    "plan_sha256": _sha_text(str(row["plan_json"])),
                }
                for row in conn.execute(
                    "SELECT bundle_version,as_of_date,snapshot_id,plan_json FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date"
                ).fetchall()
                if _ready_identity(row) not in patch_identities
            ]
        )
        if current_non_target != economics["non_target_digest"]:
            raise Wbc0027RecoveryError("economics non-target CAS drifted")
        for patch in economics["patches"]:
            changed = conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=? AND snapshot_id=? AND plan_json=?",
                (patch["after_plan_json"], *patch["identity"], patch["before_plan_json"]),
            )
            if changed.rowcount != 1:
                raise Wbc0027RecoveryError("economics ready-snapshot CAS failed")
        materialize_warehouse_business_projection_reconciliation(
            conn,
            materialized_at=_now(),
        )
        conn.commit()
    registry.retain(operation_id, after_digest=str(economics["after_digest"]), non_target_digest=str(economics["non_target_digest"]))
    return {"status": "submitted", "operation_id": operation_id, "submit_count": 1, "updated_snapshot_count": len(economics["patches"])}


def apply_plan(
    runtime_dir: Path,
    deployed_sha_file: Path,
    expected_sha: str,
    plan: Mapping[str, Any],
    fingerprint: str,
    approval_reference: str,
    phase: str,
) -> dict[str, Any]:
    if not approval_reference.strip():
        raise Wbc0027RecoveryError("apply requires immutable approval reference")
    if phase not in {"product", "economics"}:
        raise Wbc0027RecoveryError("apply requires one exact recovery phase")
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir.resolve())
    if phase == "product":
        current = build_plan(runtime_dir, deployed_sha_file, expected_sha)
        _validate_plan(plan, current, fingerprint)
        with warehouse_sync_lock(runtime.runtime_dir, operation="wbc0027-product-capital-recovery", timeout_seconds=30):
            result = _t1_product(runtime, plan, _now())
    else:
        _deployed_sha(deployed_sha_file.resolve(), expected_sha)
        with _query_only(runtime.db_path) as conn:
            product = reconcile_warehouse_business_projection(
                conn,
                target_dates=PRODUCT_DATES,
            )
        product_recovery = WarehouseRecoveryRegistry(
            runtime_dir=runtime.runtime_dir,
            db_path=runtime.db_path,
        ).get_operation(str(plan["product_operation_id"]))
        if (
            product.get("status") != "published_exact"
            or product.get("mismatch_count") != 0
            or not isinstance(product_recovery, Mapping)
            or product_recovery.get("lifecycle") != RecoveryState.RETAINED.value
        ):
            raise Wbc0027RecoveryError(
                "economics phase requires reconciled retained product-capital recovery"
            )
        with warehouse_sync_lock(runtime.runtime_dir, operation="wbc0027-functional-economics-recovery", timeout_seconds=30):
            result = _t1_economics(runtime, plan)
    return {
        "contract_name": CONTRACT,
        "status": "submitted",
        "database_written": True,
        "deployed_sha": plan["deployed_sha"],
        "plan_fingerprint": fingerprint,
        "phase": phase,
        "result": result,
        "production_mutation_submit_count": 1,
        "approval_reference": approval_reference,
    }


def readback(runtime_dir: Path, deployed_sha_file: Path, expected_sha: str, plan: Mapping[str, Any]) -> dict[str, Any]:
    _deployed_sha(deployed_sha_file.resolve(), expected_sha)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir.resolve())
    with _query_only(runtime.db_path) as conn:
        product = reconcile_warehouse_business_projection(conn, target_dates=PRODUCT_DATES)
        economics_missing: dict[str, int] = {}
        for day in ECONOMICS_DATES:
            row = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?", (day,)).fetchone()
            cells = _target_cells(json.loads(str(row["plan_json"])), day)
            economics_missing[day] = sum(_is_missing(value) for value in cells.values())
        product_recovery = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path).get_operation(str(plan["product_operation_id"]))
        economics_recovery = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path).get_operation(str(plan["economics_operation_id"]))
    product_exact = bool(
        product.get("status") == "published_exact"
        and product.get("mismatch_count") == 0
        and isinstance(product_recovery, Mapping)
        and product_recovery.get("lifecycle") == RecoveryState.RETAINED.value
    )
    economics_exact = bool(
        economics_missing == {"2026-08-26": 12, "2026-08-29": 0}
        and isinstance(economics_recovery, Mapping)
        and economics_recovery.get("lifecycle") == RecoveryState.RETAINED.value
    )
    exact = product_exact and economics_exact
    return {
        "contract_name": CONTRACT,
        "status": "reconciled" if exact else "pending_reconciliation",
        "query_only": True,
        "database_written": False,
        "product_capital": product,
        "functional_economics_missing": economics_missing,
        "product_operation_id": plan["product_operation_id"],
        "economics_operation_id": plan["economics_operation_id"],
        "product_recovery_lifecycle": (
            str(product_recovery.get("lifecycle") or "")
            if isinstance(product_recovery, Mapping)
            else "missing"
        ),
        "economics_recovery_lifecycle": (
            str(economics_recovery.get("lifecycle") or "")
            if isinstance(economics_recovery, Mapping)
            else "missing"
        ),
        "product_exact": product_exact,
        "economics_exact": economics_exact,
        "evidence_blocked": plan["functional_economics"]["evidence_blocked"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--deployed-sha-file", type=Path, required=True)
    parser.add_argument("--expected-deployed-sha", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--output", type=Path)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--reviewed-plan-stdin", action="store_true")
    apply_parser.add_argument("--fingerprint", required=True)
    apply_parser.add_argument("--approval-reference", required=True)
    apply_parser.add_argument("--phase", choices=("product", "economics"), required=True)
    readback_parser = commands.add_parser("readback")
    readback_parser.add_argument("--reviewed-plan-stdin", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = build_plan(args.runtime_dir, args.deployed_sha_file, args.expected_deployed_sha)
            if args.output:
                args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        else:
            plan = json.load(sys.stdin)
            if args.command == "apply":
                result = apply_plan(
                    args.runtime_dir,
                    args.deployed_sha_file,
                    args.expected_deployed_sha,
                    plan,
                    args.fingerprint,
                    args.approval_reference,
                    args.phase,
                )
            else:
                result = readback(args.runtime_dir, args.deployed_sha_file, args.expected_deployed_sha, plan)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") not in {"blocked", "ambiguous"} else 1
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
