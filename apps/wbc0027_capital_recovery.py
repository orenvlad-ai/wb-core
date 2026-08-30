#!/usr/bin/env python3
"""Exact two-phase WBC0027 product-capital and economics recovery."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
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
from apps.ff_pool_dense_fbs import _write_private  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_PRODUCT_CAPITAL_METRIC_KEYS,
)
from packages.application.root_storage_policy import (  # noqa: E402
    RootStoragePolicyError,
    admit_root_write,
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


PHASE_CONTRACT = "wbc0027_capital_recovery_phase_v2"
PROFILE = "product-capital-qualified-economics"
CANONICAL_TARGET_ID = "wb_core_eu_hosted_runtime_active"
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
EXPECTED_PRODUCT_ROWS = EXPECTED_PRIMARY_ROWS + EXPECTED_SECONDARY_ROWS
EXPECTED_PRODUCT_CELLS = 24_192
EXPECTED_PRODUCT_MISMATCHES = EXPECTED_PRIMARY_MISMATCHES + EXPECTED_SECONDARY_MISMATCHES
EXPECTED_SPECIAL_DATE = "2026-08-21"
EXPECTED_SPECIAL_NM_ID = 497413772
PRIVATE_PLAN_MAX_BYTES = 64_000_000
LEGACY_RELEASE_OPERATION_IDS = frozenset(
    {
        "wbc0027-product-capital-and-qualified-economics-v1",
        "wbc0027-product-capital-and-qualified-economics-v2",
        "release-v2-cc4ad52ab06962a66265f76cc3df20e9",
        "release-v2-adf15aa6d2ab81803f88b38cedc6f883",
    }
)
LEGACY_PHASE_OPERATION_IDS = frozenset(
    {
        "recovery_1d51ce2f15a001b6cfe241008b8b7232",
        "recovery_76e81cc53831c2f6bbb3148efe8a9aa8",
    }
)
LEGACY_MANIFEST_SHA256 = (
    "sha256:84a4bef9d6cba4c969988d880ab56bde06db307f3caf87a42305f7fe8c8680ee"
)
LEGACY_APPROVAL_REFERENCE = "https://github.com/orenvlad-ai/wb-core/issues/1126#issuecomment-5471418411"


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
        "product_row_count": len(rows),
        "product_cell_count": sum(item["cell_count"] for item in date_receipts),
        "product_mismatch_count": primary_mismatches + secondary_mismatches,
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
        "product_row_count": EXPECTED_PRODUCT_ROWS,
        "product_cell_count": EXPECTED_PRODUCT_CELLS,
        "product_mismatch_count": EXPECTED_PRODUCT_MISMATCHES,
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


def _file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _phase_generation(generation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "generation_id": str(generation.get("generation_id") or ""),
        "manifest_sha256": str(generation.get("manifest_sha256") or ""),
        "schema_version": int(generation.get("schema_version") or 0),
    }


def _validate_goal_namespace(operation_id: str) -> str:
    normalized = str(operation_id or "").strip()
    if (
        re.fullmatch(r"production-goal-v1-[0-9a-f]{32}", normalized) is None
        or normalized in LEGACY_RELEASE_OPERATION_IDS
    ):
        raise Wbc0027RecoveryError("WBC0027 goal operation namespace is foreign or legacy")
    return normalized


def _validate_phase_operation_id(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        re.fullmatch(r"recovery_[0-9a-f]{32}", normalized) is None
        or normalized in LEGACY_PHASE_OPERATION_IDS
    ):
        raise Wbc0027RecoveryError("WBC0027 phase operation identity is foreign or legacy")
    return normalized


def _phase_envelope(
    *,
    phase: str,
    goal_operation_id: str,
    deployed_sha: str,
    generation: Mapping[str, Any],
    material: Mapping[str, Any],
) -> dict[str, Any]:
    if phase not in {"product", "economics"}:
        raise Wbc0027RecoveryError("WBC0027 phase is unsupported")
    generation_binding = _phase_generation(generation)
    if (
        not generation_binding["generation_id"]
        or re.fullmatch(
            r"sha256:[0-9a-f]{64}", generation_binding["manifest_sha256"]
        )
        is None
        or generation_binding["schema_version"] <= 0
    ):
        raise Wbc0027RecoveryError("WBC0027 StoreRegistry generation is incomplete")
    material_digest = _digest(material)
    phase_fingerprint = _digest(
        {
            "schema": PHASE_CONTRACT,
            "profile": PROFILE,
            "target_id": CANONICAL_TARGET_ID,
            "goal_operation_id": _validate_goal_namespace(goal_operation_id),
            "phase": phase,
            "deployed_sha": deployed_sha,
            "storage_generation": generation_binding,
            "material_digest": material_digest,
        }
    )
    mutation_kind = (
        MUTATION_KIND_PRODUCT if phase == "product" else MUTATION_KIND_ECONOMICS
    )
    phase_operation_id = recovery_operation_id(mutation_kind, phase_fingerprint)
    _validate_phase_operation_id(phase_operation_id)
    return {
        "schema": PHASE_CONTRACT,
        "profile": PROFILE,
        "target_id": CANONICAL_TARGET_ID,
        "goal_operation_id": goal_operation_id,
        "phase": phase,
        "deployed_sha": deployed_sha,
        "storage_generation": generation_binding,
        "material": dict(material),
        "material_qualification_digest": material_digest,
        "phase_fingerprint": phase_fingerprint,
        "phase_operation_id": phase_operation_id,
        "production_mutation_count": 0,
        "mutation_submit_count": 0,
        "query_only": True,
        "database_written": False,
    }


def _product_special_evidence(product: Mapping[str, Any]) -> dict[str, Any]:
    before_by_identity = {
        (str(row["as_of_date"]), int(row["nm_id"])): row
        for row in product["before_rows"]
    }
    after_by_identity = {
        (str(row["as_of_date"]), int(row["nm_id"])): row
        for row in product["proposed_rows"]
    }
    identity = (EXPECTED_SPECIAL_DATE, EXPECTED_SPECIAL_NM_ID)
    before = before_by_identity.get(identity)
    after = after_by_identity.get(identity)
    if before is None or after is None:
        raise Wbc0027RecoveryError("21.08 special SKU target row is missing")
    before_metrics = json.loads(str(before["metrics_json"]))
    after_metrics = dict(after["metrics"])
    changed = [
        {
            "metric_key": metric_key,
            "before": before_metrics.get(metric_key),
            "after": after_metrics.get(metric_key),
        }
        for metric_key in OWN_PRODUCT_CAPITAL_METRIC_KEYS
        if before_metrics.get(metric_key) != after_metrics.get(metric_key)
    ]
    if len(changed) != EXPECTED_SEPARATE_MISMATCHES:
        raise Wbc0027RecoveryError("21.08 special SKU mismatch set is not exact 16")
    return {
        "as_of_date": EXPECTED_SPECIAL_DATE,
        "nm_id": EXPECTED_SPECIAL_NM_ID,
        "cell_count": len(changed),
        "digest": _digest(changed),
        "cells": changed,
    }


def _hard_non_target_semantics(conn: sqlite3.Connection) -> dict[str, Any]:
    dates = [
        str(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT as_of_date FROM {CURRENT_ROW_TABLE} "
            "WHERE as_of_date>=? ORDER BY as_of_date",
            (HARD_NON_TARGET_DATE,),
        ).fetchall()
    ]
    if HARD_NON_TARGET_DATE not in dates:
        raise Wbc0027RecoveryError("30.08 hard non-target row set is missing")
    receipt = reconcile_warehouse_business_projection(conn, target_dates=dates)
    if (
        receipt.get("status") != "published_exact"
        or int(receipt.get("mismatch_count") or 0) != 0
    ):
        raise Wbc0027RecoveryError("30.08 and later hard non-target is not exact")
    return {
        "from_date": HARD_NON_TARGET_DATE,
        "predicate": "all_persisted_dates_exact_functional",
        "all_exact": True,
        "observed_dates": dates,
        "observed_date_count": len(dates),
        "observation_digest": _digest(receipt),
    }


def _product_material(product: Mapping[str, Any], hard: Mapping[str, Any]) -> dict[str, Any]:
    sources: dict[str, dict[str, str]] = {}
    for row in product["proposed_rows"]:
        day = str(row["as_of_date"])
        provenance = dict(row["provenance"])
        sources.setdefault(
            day,
            {
                "functional_version_id": str(
                    provenance.get("functional_version_id") or ""
                ),
                "source_digest": str(provenance.get("source_digest") or ""),
                "snapshot_date": str(provenance.get("snapshot_date") or ""),
            },
        )
    return {
        "phase": "product",
        "target_dates": list(product["target_dates"]),
        "primary_dates": list(product["primary_dates"]),
        "secondary_dates": list(product["secondary_dates"]),
        "counts": dict(product["counts"]),
        "sources": [{"as_of_date": day, **sources[day]} for day in sorted(sources)],
        "exact_before_rows": list(product["before_rows"]),
        "exact_after_rows": list(product["proposed_rows"]),
        "before_target_digest": str(product["before_target_digest"]),
        "after_target_digest": str(product["after_target_digest"]),
        "special_20260821": _product_special_evidence(product),
        "evidence_blocked": list(product["evidence_blocked"]),
        "hard_non_target": {
            "from_date": hard["from_date"],
            "predicate": hard["predicate"],
            "all_exact": hard["all_exact"],
        },
    }


def build_product_candidate(
    *,
    runtime_dir: Path,
    deployed_sha_file: Path,
    expected_sha: str,
    goal_operation_id: str,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    deployed_sha = _deployed_sha(deployed_sha_file.resolve(), expected_sha)
    generation = _generation(runtime_dir)
    db_path = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir).db_path
    with _query_only(db_path) as conn:
        product = _product_plan(conn)
        hard = _hard_non_target_semantics(conn)
        ready_snapshot_audit_digest = _digest(
            [
                dict(row)
                for row in conn.execute(
                    "SELECT bundle_version,as_of_date,snapshot_id,plan_json "
                    "FROM sheet_vitrina_v1_ready_snapshots ORDER BY as_of_date"
                ).fetchall()
            ]
        )
        outbox_audit_digest = _digest(
            [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM {OUTBOX_TABLE} ORDER BY request_id"
                ).fetchall()
            ]
        )
    candidate = _phase_envelope(
        phase="product",
        goal_operation_id=goal_operation_id,
        deployed_sha=deployed_sha,
        generation=generation,
        material=_product_material(product, hard),
    )
    candidate.update(
        {
            "created_at": _now(),
            "product_capital": product,
            "audit_observations": {
                "ready_snapshot_digest": ready_snapshot_audit_digest,
                "outbox_digest": outbox_audit_digest,
                "ready_pointer_digest": _digest(product["before_state"]),
                "hard_non_target": hard,
                "role": "audit_only_not_apply_cas",
            },
        }
    )
    return candidate


def _strip_economics_targets(payload: Mapping[str, Any], days: list[str]) -> dict[str, Any]:
    stripped = deepcopy(dict(payload))
    sheet = next(
        item
        for item in stripped.get("sheets") or []
        if item.get("sheet_name") == "DATA_VITRINA"
    )
    header = list(sheet["header"])
    target_row_ids: set[str] = set()
    for day in days:
        if day not in header:
            continue
        column = header.index(day)
        day_ids = set(_target_cells(stripped, day))
        target_row_ids.update(day_ids)
        for row in sheet["rows"]:
            if not isinstance(row, list) or len(row) < 2 or str(row[1]) not in day_ids:
                continue
            while len(row) <= column:
                row.append("")
            row[column] = "__WBC0027_TARGET_CELL__"
    metadata = stripped.get("metadata")
    if not isinstance(metadata, dict):
        return stripped
    coverage = metadata.get("warehouse_history_coverage")
    if isinstance(coverage, dict):
        for day in days:
            coverage.pop(day, None)
    marker = metadata.get("functional_economics_backfill")
    if isinstance(marker, dict):
        publication = marker.get("inventory_cost_publication")
        if isinstance(publication, dict):
            date_evidence = publication.get("date_evidence")
            if isinstance(date_evidence, dict):
                for day in days:
                    date_evidence.pop(day, None)
    presentation = metadata.get("server_cell_presentation")
    if isinstance(presentation, dict):
        for row_id in target_row_ids:
            by_date = presentation.get(row_id)
            if not isinstance(by_date, dict):
                continue
            for day in days:
                by_date.pop(day, None)
            if not by_date:
                presentation.pop(row_id, None)
        if not presentation:
            metadata.pop("server_cell_presentation", None)
    registry = metadata.get(HISTORICAL_REPAIR_METADATA_KEY)
    if isinstance(registry, dict) and isinstance(registry.get("dates"), dict):
        for day in days:
            registry["dates"].pop(day, None)
        if not registry["dates"]:
            metadata.pop(HISTORICAL_REPAIR_METADATA_KEY, None)
    return stripped


def _economics_semantic_patches(economics: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for patch in economics["patches"]:
        days = list(patch["business_dates"])
        before = json.loads(str(patch["before_plan_json"]))
        after = json.loads(str(patch["after_plan_json"]))
        before_non_target = _digest(_strip_economics_targets(before, days))
        after_non_target = _digest(_strip_economics_targets(after, days))
        if before_non_target != after_non_target:
            raise Wbc0027RecoveryError("economics candidate changes a semantic non-target")
        result.append(
            {
                "identity": list(patch["identity"]),
                "business_dates": days,
                "changed_cells": list(patch["changed_cells"]),
                "exact_before_cells": {
                    day: _target_cells(before, day) for day in days
                },
                "exact_after_cells": {
                    day: _target_cells(after, day) for day in days
                },
                "semantic_non_target_preserved": True,
            }
        )
    return result


def _economics_material(
    economics: Mapping[str, Any], *, product_phase_operation_id: str
) -> dict[str, Any]:
    semantic_patches = _economics_semantic_patches(economics)
    return {
        "phase": "economics",
        "target_dates": list(economics["target_dates"]),
        "logical_repair_count": int(economics["logical_repair_count"]),
        "persisted_repair_count": int(economics["persisted_repair_count"]),
        "patch_count": int(economics["patch_count"]),
        "source_operation_id": str(economics["source_operation_id"]),
        "source_digest": str(economics["source_digest"]),
        "protected_invariant": dict(economics["protected_invariant"]),
        "evidence_blocked": list(economics["evidence_blocked"]),
        "semantic_patches": semantic_patches,
        "exact_target_before_digest": _digest(
            [item["exact_before_cells"] for item in semantic_patches]
        ),
        "exact_target_after_digest": _digest(
            [item["exact_after_cells"] for item in semantic_patches]
        ),
        "product_phase_operation_id": product_phase_operation_id,
        "unrelated_20260821_proxy_v4": "preserve_current_semantic_value",
        "finance": "hard_non_target",
        "hard_non_target_from": HARD_NON_TARGET_DATE,
    }


def _product_predecessor_status(
    runtime_dir: Path, product_phase_operation_id: str
) -> dict[str, Any]:
    operation_id = _validate_phase_operation_id(product_phase_operation_id)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir.resolve())
    recovery = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir, db_path=runtime.db_path
    ).get_operation(operation_id)
    with _query_only(runtime.db_path) as conn:
        reconciliation = reconcile_warehouse_business_projection(
            conn, target_dates=PRODUCT_DATES
        )
    return {
        "operation_id": operation_id,
        "recovery_lifecycle": (
            str(recovery.get("lifecycle") or "")
            if isinstance(recovery, Mapping)
            else "missing"
        ),
        "reconciliation_status": str(reconciliation.get("status") or ""),
        "mismatch_count": int(reconciliation.get("mismatch_count") or 0),
        "reconciled": bool(
            isinstance(recovery, Mapping)
            and recovery.get("lifecycle") == RecoveryState.RETAINED.value
            and reconciliation.get("status") == "published_exact"
            and int(reconciliation.get("mismatch_count") or 0) == 0
        ),
    }


def build_economics_candidate(
    *,
    runtime_dir: Path,
    deployed_sha_file: Path,
    expected_sha: str,
    goal_operation_id: str,
    product_phase_operation_id: str,
    require_product_reconciled: bool,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.resolve()
    deployed_sha = _deployed_sha(deployed_sha_file.resolve(), expected_sha)
    generation = _generation(runtime_dir)
    predecessor = (
        _product_predecessor_status(runtime_dir, product_phase_operation_id)
        if product_phase_operation_id
        else {
            "operation_id": "",
            "recovery_lifecycle": "not_checked",
            "reconciliation_status": "not_checked",
            "mismatch_count": 0,
            "reconciled": False,
        }
    )
    if require_product_reconciled and predecessor.get("reconciled") is not True:
        raise Wbc0027RecoveryError(
            "economics candidate requires retained reconciled product phase"
        )
    predecessor_operation_id = str(product_phase_operation_id or "")
    if not predecessor_operation_id:
        raise Wbc0027RecoveryError("economics candidate lacks product phase identity")
    _validate_phase_operation_id(predecessor_operation_id)
    db_path = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir).db_path
    with _query_only(db_path) as conn:
        economics = _economics_plan(conn)
    candidate = _phase_envelope(
        phase="economics",
        goal_operation_id=goal_operation_id,
        deployed_sha=deployed_sha,
        generation=generation,
        material=_economics_material(
            economics, product_phase_operation_id=predecessor_operation_id
        ),
    )
    candidate.update(
        {
            "created_at": _now(),
            "functional_economics": economics,
            "product_predecessor": predecessor,
            "audit_observations": {
                "raw_non_target_digest": str(economics["non_target_digest"]),
                "role": "audit_only_not_apply_cas",
            },
        }
    )
    return candidate


def _candidate_path(evidence_dir: Path, candidate: Mapping[str, Any]) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = str(candidate["material_qualification_digest"]).removeprefix("sha256:")[:12]
    return evidence_dir / f"wbc0027-{candidate['phase']}-plan-{timestamp}-{suffix}.json"


def _candidate_size(candidate: Mapping[str, Any]) -> int:
    return len(
        json.dumps(
            dict(candidate), ensure_ascii=False, sort_keys=True, indent=2, default=str
        ).encode("utf-8")
        + b"\n"
    )


def publish_candidate(
    *,
    candidate: Mapping[str, Any],
    evidence_dir: Path,
    no_create: bool,
    admission_factory: object = admit_root_write,
    writer: object = _write_private,
) -> dict[str, Any]:
    evidence_dir = evidence_dir.resolve()
    operation_id = _validate_goal_namespace(str(candidate["goal_operation_id"]))
    if evidence_dir.name != operation_id or evidence_dir.parent.name != "production-goals":
        raise Wbc0027RecoveryError("private evidence path escapes goal namespace")
    path = _candidate_path(evidence_dir, candidate)
    predicted_bytes = _candidate_size(candidate)
    if predicted_bytes > PRIVATE_PLAN_MAX_BYTES:
        raise Wbc0027RecoveryError("private phase plan exceeds bounded size")
    if no_create:
        try:
            admission = admission_factory(
                owner="production_apply_evidence",
                destination=path,
                predicted_output_bytes=predicted_bytes,
            )
        except RootStoragePolicyError as exc:
            raise Wbc0027RecoveryError(f"{type(exc).__name__}: {exc}") from exc
        persistence = {
            "owner": "production_apply_evidence",
            "destination": str(path),
            "evidence_dir": str(evidence_dir),
            "evidence_dir_mode": "0700",
            "file_mode": "0600",
            "parent_mode": "0700",
            "size_bytes": predicted_bytes,
            "max_size_bytes": PRIVATE_PLAN_MAX_BYTES,
            "bounded_size": True,
            "atomic_publish": True,
            "no_overwrite": True,
            "durable_file_fsync": True,
            "durable_directory_fsync": True,
            "root_storage_admission": admission,
            "no_create": True,
            "simulated": True,
        }
        manifest_path = ""
        manifest_sha256 = ""
    else:
        if (
            not evidence_dir.is_dir()
            or evidence_dir.is_symlink()
            or evidence_dir.stat().st_mode & 0o777 != 0o700
        ):
            raise Wbc0027RecoveryError("private evidence directory must be mode 0700")
        written = writer(
            path,
            dict(candidate),
            owner="production_apply_evidence",
            max_output_bytes=PRIVATE_PLAN_MAX_BYTES,
            require_private_parent=True,
            no_overwrite=True,
        )
        if not written.get("written"):
            raise Wbc0027RecoveryError(
                f"{written.get('error_type') or 'PrivatePlanPersistenceError'}: "
                f"{written.get('error') or written.get('reason') or 'private plan persistence failed'}"
            )
        persistence = {
            "owner": "production_apply_evidence",
            "destination": str(path),
            "evidence_dir": str(evidence_dir),
            "evidence_dir_mode": "0700",
            **{
                key: value
                for key, value in written.items()
                if key not in {"written", "mode", "path"}
            },
            "no_create": False,
            "simulated": False,
        }
        manifest_path = str(path)
        manifest_sha256 = _file_digest(path)
    material = dict(candidate["material"])
    result = {
        "status": "ready",
        "phase": candidate["phase"],
        "profile": PROFILE,
        "target_id": CANONICAL_TARGET_ID,
        "goal_operation_id": operation_id,
        "phase_operation_id": candidate["phase_operation_id"],
        "phase_fingerprint": candidate["phase_fingerprint"],
        "material_qualification_digest": candidate[
            "material_qualification_digest"
        ],
        "deployed_sha": candidate["deployed_sha"],
        "storage_generation": candidate["storage_generation"],
        "query_only": True,
        "database_written": False,
        "production_mutation_count": 0,
        "manifest_path": manifest_path,
        "manifest_sha256": manifest_sha256,
        "plan_persistence": persistence,
        "legacy_release_operation_reusable": False,
        "legacy_phase_operation_reusable": False,
    }
    if candidate["phase"] == "product":
        counts = dict(material["counts"])
        result.update(
            {
                "product_counts": counts,
                "before_target_digest": material["before_target_digest"],
                "after_target_digest": material["after_target_digest"],
                "special_20260821": material["special_20260821"],
                "evidence_blocked": material["evidence_blocked"],
                "hard_non_target": material["hard_non_target"],
            }
        )
    else:
        result.update(
            {
                "logical_repair_count": material["logical_repair_count"],
                "persisted_repair_count": material["persisted_repair_count"],
                "patch_count": material["patch_count"],
                "evidence_blocked": material["evidence_blocked"],
                "protected_invariant": material["protected_invariant"],
                "product_phase_operation_id": material[
                    "product_phase_operation_id"
                ],
                "product_predecessor": candidate["product_predecessor"],
            }
        )
    return result


def _t1_product(runtime: RegistryUploadDbBackedRuntime, plan: Mapping[str, Any], applied_at: str) -> dict[str, Any]:
    product = dict(plan["product_capital"])
    fingerprint = str(plan["phase_fingerprint"])
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
        scope={
            "dates": product["target_dates"],
            "row_count": len(proposed_rows),
            "deployed_sha": plan["deployed_sha"],
            "profile": PROFILE,
            "goal_operation_id": plan["goal_operation_id"],
            "phase": "product",
        },
        before_images=before_images,
        expected_after_images=[item["after"] for item in before_images],
        source_digest=str(plan["material_qualification_digest"]),
        non_target_digest=str(product["non_target_digest"]),
        read_bytes=len(_json(before_images).encode("utf-8")),
    )
    operation_id = str(recovery["operation_id"])
    if operation_id != plan["phase_operation_id"]:
        raise Wbc0027RecoveryError("product T1 operation identity drifted")
    if recovery.get("lifecycle") == RecoveryState.RETAINED.value:
        return {"status": "idempotent", "operation_id": operation_id, "submit_count": 1}
    if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(
            operation_id,
            expected_source_digest=str(plan["material_qualification_digest"]),
        )
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
        current_non_target_before = _non_target_digest(
            conn, set(product["target_dates"])
        )
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
        current_non_target_after = _non_target_digest(
            conn, set(product["target_dates"])
        )
        if current_non_target_after != current_non_target_before:
            raise Wbc0027RecoveryError("product semantic non-target changed in transaction")
        conn.commit()
    registry.retain(
        operation_id,
        after_digest=str(product["after_target_digest"]),
        non_target_digest=current_non_target_before,
    )
    return {
        **result,
        "operation_id": operation_id,
        "submit_count": 1,
        "resolved_replay_signal_count": 0,
        "outbox_mutation_count": 0,
        "semantic_non_target_preserved": True,
    }


def _t1_economics(runtime: RegistryUploadDbBackedRuntime, plan: Mapping[str, Any]) -> dict[str, Any]:
    economics = dict(plan["functional_economics"])
    fingerprint = str(plan["phase_fingerprint"])
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
        scope={
            "dates": economics["target_dates"],
            "logical_repair_count": economics["logical_repair_count"],
            "source_operation_id": SOURCE_OPERATION_ID,
            "profile": PROFILE,
            "goal_operation_id": plan["goal_operation_id"],
            "phase": "economics",
            "product_phase_operation_id": plan["material"][
                "product_phase_operation_id"
            ],
        },
        before_images=before_images,
        expected_after_images=[item["after"] for item in before_images],
        source_digest=str(plan["material_qualification_digest"]),
        non_target_digest=str(economics["non_target_digest"]),
        read_bytes=len(_json(before_images).encode("utf-8")),
    )
    operation_id = str(recovery["operation_id"])
    if operation_id != plan["phase_operation_id"]:
        raise Wbc0027RecoveryError("economics T1 operation identity drifted")
    if recovery.get("lifecycle") == RecoveryState.RETAINED.value:
        return {"status": "idempotent", "operation_id": operation_id, "submit_count": 1}
    if recovery.get("lifecycle") == RecoveryState.VERIFIED.value:
        recovery = registry.begin_mutation(
            operation_id,
            expected_source_digest=str(plan["material_qualification_digest"]),
        )
    if recovery.get("lifecycle") != RecoveryState.MUTATION_RUNNING.value:
        raise Wbc0027RecoveryError("economics T1 recovery is not mutation-ready")
    with sqlite3.connect(runtime.db_path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        ensure_warehouse_business_projection_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        patch_identities = {tuple(item["identity"]) for item in economics["patches"]}
        patches_by_identity = {
            tuple(item["identity"]): item for item in economics["patches"]
        }

        def semantic_non_target_digest() -> str:
            rows = conn.execute(
                "SELECT bundle_version,as_of_date,snapshot_id,plan_json "
                "FROM sheet_vitrina_v1_ready_snapshots "
                "ORDER BY bundle_version,as_of_date,snapshot_id"
            ).fetchall()
            material: list[dict[str, Any]] = []
            for row in rows:
                identity = _ready_identity(row)
                payload = json.loads(str(row["plan_json"]))
                if identity in patch_identities:
                    patch = patches_by_identity[identity]
                    payload = _strip_economics_targets(
                        payload, list(patch["business_dates"])
                    )
                material.append(
                    {"identity": list(identity), "semantic_payload": payload}
                )
            return _digest(material)

        current_non_target_before = semantic_non_target_digest()
        for patch in economics["patches"]:
            changed = conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=? AND snapshot_id=? AND plan_json=?",
                (patch["after_plan_json"], *patch["identity"], patch["before_plan_json"]),
            )
            if changed.rowcount != 1:
                raise Wbc0027RecoveryError("economics ready-snapshot CAS failed")
            exact_after = conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                tuple(patch["identity"]),
            ).fetchone()
            if exact_after is None or str(exact_after["plan_json"]) != str(
                patch["after_plan_json"]
            ):
                raise Wbc0027RecoveryError("economics exact target readback failed")
        materialize_warehouse_business_projection_reconciliation(
            conn,
            materialized_at=_now(),
        )
        current_non_target_after = semantic_non_target_digest()
        if current_non_target_after != current_non_target_before:
            raise Wbc0027RecoveryError(
                "economics semantic non-target changed in transaction"
            )
        conn.commit()
    registry.retain(
        operation_id,
        after_digest=str(economics["after_digest"]),
        non_target_digest=current_non_target_before,
    )
    return {
        "status": "submitted",
        "operation_id": operation_id,
        "submit_count": 1,
        "updated_snapshot_count": len(economics["patches"]),
        "semantic_non_target_preserved": True,
    }


def _load_reviewed_candidate(
    *, manifest_path: Path, manifest_sha256: str, goal_operation_id: str
) -> dict[str, Any]:
    path = manifest_path.resolve()
    operation_id = _validate_goal_namespace(goal_operation_id)
    if (
        manifest_sha256 == LEGACY_MANIFEST_SHA256
        or re.fullmatch(r"sha256:[0-9a-f]{64}", manifest_sha256) is None
    ):
        raise Wbc0027RecoveryError("legacy or malformed WBC0027 manifest digest")
    if (
        path.is_symlink()
        or not path.is_file()
        or path.parent.name != operation_id
        or path.parent.parent.name != "production-goals"
        or path.stat().st_mode & 0o777 != 0o600
        or path.parent.stat().st_mode & 0o777 != 0o700
    ):
        raise Wbc0027RecoveryError("reviewed candidate is not one private goal plan")
    if _file_digest(path) != manifest_sha256:
        raise Wbc0027RecoveryError("reviewed candidate manifest digest drifted")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Wbc0027RecoveryError("reviewed candidate is not one JSON object")
    return value


def _validate_candidate(
    candidate: Mapping[str, Any],
    *,
    phase: str,
    goal_operation_id: str,
    expected_sha: str,
    generation: Mapping[str, Any],
    phase_operation_id: str,
    phase_fingerprint: str,
) -> None:
    if phase not in {"product", "economics"}:
        raise Wbc0027RecoveryError("WBC0027 reviewed phase is unsupported")
    operation_id = _validate_goal_namespace(goal_operation_id)
    reviewed_phase_operation = _validate_phase_operation_id(phase_operation_id)
    material = candidate.get("material")
    if not isinstance(material, Mapping):
        raise Wbc0027RecoveryError("WBC0027 reviewed material is missing")
    expected = _phase_envelope(
        phase=phase,
        goal_operation_id=operation_id,
        deployed_sha=expected_sha,
        generation=generation,
        material=material,
    )
    if not (
        candidate.get("schema") == PHASE_CONTRACT
        and candidate.get("profile") == PROFILE
        and candidate.get("target_id") == CANONICAL_TARGET_ID
        and candidate.get("goal_operation_id") == operation_id
        and candidate.get("phase") == phase
        and candidate.get("deployed_sha") == expected_sha
        and candidate.get("storage_generation") == _phase_generation(generation)
        and candidate.get("material_qualification_digest")
        == expected["material_qualification_digest"]
        and candidate.get("phase_fingerprint") == expected["phase_fingerprint"]
        and candidate.get("phase_operation_id") == expected["phase_operation_id"]
        and phase_fingerprint == expected["phase_fingerprint"]
        and reviewed_phase_operation == expected["phase_operation_id"]
        and candidate.get("query_only") is True
        and candidate.get("database_written") is False
        and candidate.get("production_mutation_count") == 0
        and candidate.get("mutation_submit_count") == 0
    ):
        raise Wbc0027RecoveryError("reviewed WBC0027 candidate escaped exact bindings")
    if phase == "product":
        counts = material.get("counts") or {}
        required = {
            "primary_row_count": EXPECTED_PRIMARY_ROWS,
            "primary_cell_count": EXPECTED_PRIMARY_CELLS,
            "primary_mismatch_count": EXPECTED_PRIMARY_MISMATCHES,
            "secondary_row_count": EXPECTED_SECONDARY_ROWS,
            "secondary_mismatch_count": EXPECTED_SECONDARY_MISMATCHES,
            "product_row_count": EXPECTED_PRODUCT_ROWS,
            "product_cell_count": EXPECTED_PRODUCT_CELLS,
            "product_mismatch_count": EXPECTED_PRODUCT_MISMATCHES,
        }
        if any(int(counts.get(key) or -1) != value for key, value in required.items()):
            raise Wbc0027RecoveryError("reviewed product counts are not production-exact")
        special = material.get("special_20260821") or {}
        if not (
            special.get("as_of_date") == EXPECTED_SPECIAL_DATE
            and special.get("nm_id") == EXPECTED_SPECIAL_NM_ID
            and special.get("cell_count") == EXPECTED_SEPARATE_MISMATCHES
            and (material.get("evidence_blocked") or [{}])[0].get("as_of_date")
            == EVIDENCE_BLOCKED_DATE
        ):
            raise Wbc0027RecoveryError("reviewed product special boundary drifted")
    else:
        if not (
            material.get("logical_repair_count")
            == EXPECTED_ECONOMICS_LOGICAL_REPAIRS
            and material.get("persisted_repair_count")
            == EXPECTED_ECONOMICS_PERSISTED_REPAIRS
            and len(material.get("evidence_blocked") or []) == 12
            and (material.get("protected_invariant") or {}).get("unit_cost_rub")
            == "117.537167"
        ):
            raise Wbc0027RecoveryError("reviewed economics qualification drifted")


def _failure_state(runtime: RegistryUploadDbBackedRuntime, operation_id: str) -> str:
    recovery = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir, db_path=runtime.db_path
    ).get_operation(operation_id)
    lifecycle = str(recovery.get("lifecycle") or "") if isinstance(recovery, Mapping) else ""
    if lifecycle == RecoveryState.RETAINED.value:
        return "applied"
    if lifecycle in {
        RecoveryState.MUTATION_RUNNING.value,
        RecoveryState.FAILED_RECOVERABLE.value,
    }:
        return "ambiguous"
    return "not_applied"


def apply_phase(
    *,
    runtime_dir: Path,
    deployed_sha_file: Path,
    expected_sha: str,
    phase: str,
    goal_operation_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    phase_operation_id: str,
    phase_fingerprint: str,
    approval_reference: str,
) -> dict[str, Any]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir.resolve())
    operation_id = str(phase_operation_id or "")
    try:
        if (
            not approval_reference.strip()
            or approval_reference == LEGACY_APPROVAL_REFERENCE
            or "5471418411" in approval_reference
        ):
            raise Wbc0027RecoveryError("fresh immutable scope-goal approval is required")
        candidate = _load_reviewed_candidate(
            manifest_path=manifest_path,
            manifest_sha256=manifest_sha256,
            goal_operation_id=goal_operation_id,
        )
        deployed_sha = _deployed_sha(deployed_sha_file.resolve(), expected_sha)
        generation = _generation(runtime.runtime_dir)
        _validate_candidate(
            candidate,
            phase=phase,
            goal_operation_id=goal_operation_id,
            expected_sha=deployed_sha,
            generation=generation,
            phase_operation_id=phase_operation_id,
            phase_fingerprint=phase_fingerprint,
        )
        lock_operation = f"wbc0027-{phase}-jit-recovery"
        with warehouse_sync_lock(
            runtime.runtime_dir, operation=lock_operation, timeout_seconds=30
        ):
            if phase == "product":
                fresh = build_product_candidate(
                    runtime_dir=runtime.runtime_dir,
                    deployed_sha_file=deployed_sha_file,
                    expected_sha=expected_sha,
                    goal_operation_id=goal_operation_id,
                )
            else:
                predecessor_id = str(
                    (candidate.get("material") or {}).get(
                        "product_phase_operation_id"
                    )
                    or ""
                )
                fresh = build_economics_candidate(
                    runtime_dir=runtime.runtime_dir,
                    deployed_sha_file=deployed_sha_file,
                    expected_sha=expected_sha,
                    goal_operation_id=goal_operation_id,
                    product_phase_operation_id=predecessor_id,
                    require_product_reconciled=True,
                )
            if any(
                fresh.get(key) != candidate.get(key)
                for key in (
                    "material_qualification_digest",
                    "phase_fingerprint",
                    "phase_operation_id",
                    "storage_generation",
                    "deployed_sha",
                )
            ):
                raise Wbc0027RecoveryError(
                    "fresh writer-lock candidate no longer matches reviewed material"
                )
            result = (
                _t1_product(runtime, fresh, _now())
                if phase == "product"
                else _t1_economics(runtime, fresh)
            )
        idempotent = result.get("status") == "idempotent"
        return {
            "contract_name": PHASE_CONTRACT,
            "status": "applied",
            "phase": phase,
            "profile": PROFILE,
            "target_id": CANONICAL_TARGET_ID,
            "goal_operation_id": goal_operation_id,
            "phase_operation_id": phase_operation_id,
            "phase_fingerprint": phase_fingerprint,
            "deployed_sha": deployed_sha,
            "storage_generation": _phase_generation(generation),
            "database_written": not idempotent,
            "production_mutation_submit_count": 0 if idempotent else 1,
            "result": result,
            "approval_reference": approval_reference,
        }
    except Exception as exc:
        state = "not_applied"
        if re.fullmatch(r"recovery_[0-9a-f]{32}", operation_id):
            try:
                state = _failure_state(runtime, operation_id)
            except Exception:
                state = "ambiguous"
        return {
            "contract_name": PHASE_CONTRACT,
            "status": state,
            "phase": phase,
            "profile": PROFILE,
            "target_id": CANONICAL_TARGET_ID,
            "goal_operation_id": goal_operation_id,
            "phase_operation_id": operation_id,
            "database_written": state != "not_applied",
            "production_mutation_submit_count": 0,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def readback_phase(
    *,
    runtime_dir: Path,
    deployed_sha_file: Path,
    expected_sha: str,
    phase: str,
    goal_operation_id: str,
    manifest_path: Path,
    manifest_sha256: str,
    phase_operation_id: str,
    phase_fingerprint: str,
) -> dict[str, Any]:
    candidate = _load_reviewed_candidate(
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        goal_operation_id=goal_operation_id,
    )
    deployed_sha = _deployed_sha(deployed_sha_file.resolve(), expected_sha)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir.resolve())
    generation = _generation(runtime.runtime_dir)
    _validate_candidate(
        candidate,
        phase=phase,
        goal_operation_id=goal_operation_id,
        expected_sha=deployed_sha,
        generation=generation,
        phase_operation_id=phase_operation_id,
        phase_fingerprint=phase_fingerprint,
    )
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir, db_path=runtime.db_path
    )
    recovery = registry.get_operation(phase_operation_id)
    lifecycle = str(recovery.get("lifecycle") or "") if isinstance(recovery, Mapping) else "missing"
    with _query_only(runtime.db_path) as conn:
        product = reconcile_warehouse_business_projection(
            conn, target_dates=PRODUCT_DATES
        )
        hard = _hard_non_target_semantics(conn)
        economics_missing: dict[str, int] = {}
        economics_target_exact = True
        if phase == "economics":
            semantic_patches = list((candidate.get("material") or {}).get("semantic_patches") or [])
            for patch in semantic_patches:
                identity = tuple(patch["identity"])
                row = conn.execute(
                    "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    identity,
                ).fetchone()
                if row is None:
                    economics_target_exact = False
                    continue
                payload = json.loads(str(row["plan_json"]))
                for day, expected_cells in patch["exact_after_cells"].items():
                    if _target_cells(payload, day) != expected_cells:
                        economics_target_exact = False
            for day in ECONOMICS_DATES:
                rows = conn.execute(
                    "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?",
                    (day,),
                ).fetchall()
                economics_missing[day] = sum(
                    sum(_is_missing(value) for value in _target_cells(json.loads(str(row["plan_json"])), day).values())
                    for row in rows
                )
    product_exact = bool(
        product.get("status") == "published_exact"
        and int(product.get("mismatch_count") or 0) == 0
        and hard.get("all_exact") is True
    )
    phase_exact = bool(
        lifecycle == RecoveryState.RETAINED.value
        and (
            product_exact
            if phase == "product"
            else economics_target_exact
            and economics_missing == {"2026-08-26": 12, "2026-08-29": 0}
        )
    )
    return {
        "contract_name": PHASE_CONTRACT,
        "status": "reconciled" if phase_exact else "pending_reconciliation",
        "phase": phase,
        "profile": PROFILE,
        "target_id": CANONICAL_TARGET_ID,
        "goal_operation_id": goal_operation_id,
        "phase_operation_id": phase_operation_id,
        "phase_fingerprint": phase_fingerprint,
        "deployed_sha": deployed_sha,
        "storage_generation": _phase_generation(generation),
        "query_only": True,
        "database_written": False,
        "production_mutation_submit_count": 0,
        "recovery_lifecycle": lifecycle,
        "product_exact": product_exact,
        "product_capital": product,
        "hard_non_target": hard,
        "economics_target_exact": economics_target_exact,
        "functional_economics_missing": economics_missing,
        "evidence_blocked": (candidate.get("material") or {}).get("evidence_blocked"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--deployed-sha-file", type=Path, required=True)
    parser.add_argument("--expected-deployed-sha", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--phase", choices=("product", "economics"), required=True)
    plan_parser.add_argument("--product-phase-operation-id", default="")
    plan_parser.add_argument("--no-create", action="store_true")
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--phase", choices=("product", "economics"), required=True)
    apply_parser.add_argument("--manifest", type=Path, required=True)
    apply_parser.add_argument("--manifest-sha256", required=True)
    apply_parser.add_argument("--phase-operation-id", required=True)
    apply_parser.add_argument("--phase-fingerprint", required=True)
    apply_parser.add_argument("--approval-reference", required=True)
    readback_parser = commands.add_parser("readback")
    readback_parser.add_argument("--phase", choices=("product", "economics"), required=True)
    readback_parser.add_argument("--manifest", type=Path, required=True)
    readback_parser.add_argument("--manifest-sha256", required=True)
    readback_parser.add_argument("--phase-operation-id", required=True)
    readback_parser.add_argument("--phase-fingerprint", required=True)
    args = parser.parse_args()
    try:
        if args.profile != PROFILE or args.target_id != CANONICAL_TARGET_ID:
            raise Wbc0027RecoveryError("WBC0027 CLI profile or target is not exact")
        _validate_goal_namespace(args.operation_id)
        if args.command == "plan":
            if args.phase == "product":
                candidate = build_product_candidate(
                    runtime_dir=args.runtime_dir,
                    deployed_sha_file=args.deployed_sha_file,
                    expected_sha=args.expected_deployed_sha,
                    goal_operation_id=args.operation_id,
                )
            else:
                candidate = build_economics_candidate(
                    runtime_dir=args.runtime_dir,
                    deployed_sha_file=args.deployed_sha_file,
                    expected_sha=args.expected_deployed_sha,
                    goal_operation_id=args.operation_id,
                    product_phase_operation_id=args.product_phase_operation_id,
                    require_product_reconciled=not args.no_create,
                )
            result = publish_candidate(
                candidate=candidate,
                evidence_dir=args.evidence_dir,
                no_create=args.no_create,
            )
        elif args.command == "apply":
            result = apply_phase(
                runtime_dir=args.runtime_dir,
                deployed_sha_file=args.deployed_sha_file,
                expected_sha=args.expected_deployed_sha,
                phase=args.phase,
                goal_operation_id=args.operation_id,
                manifest_path=args.manifest,
                manifest_sha256=args.manifest_sha256,
                phase_operation_id=args.phase_operation_id,
                phase_fingerprint=args.phase_fingerprint,
                approval_reference=args.approval_reference,
            )
        else:
            result = readback_phase(
                runtime_dir=args.runtime_dir,
                deployed_sha_file=args.deployed_sha_file,
                expected_sha=args.expected_deployed_sha,
                phase=args.phase,
                goal_operation_id=args.operation_id,
                manifest_path=args.manifest,
                manifest_sha256=args.manifest_sha256,
                phase_operation_id=args.phase_operation_id,
                phase_fingerprint=args.phase_fingerprint,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") in {"ready", "applied", "reconciled"} else 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "not_applied" if args.command == "apply" else "blocked",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "production_mutation_submit_count": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
