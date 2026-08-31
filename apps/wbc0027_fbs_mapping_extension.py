#!/usr/bin/env python3
"""Exact WBC0027 canonical SKU mapping extension.

The default command is a query-only dry-run.  It exact-binds the accepted
external diagnosis and an independently computed versioned tuple digest,
proves the current StoreRegistry/cutover/count boundary, and rehearses the
four-group lifecycle recovery with a hypothetical mapping.  Apply performs at
most one mapping-table insert under two material CAS checks.  It has no
lifecycle, balance, history, public or outbox mutation primitive.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_fbs_lifecycle_quality_recovery as recovery_module  # noqa: E402
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    EVENTS_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    QUALITY_RECOVERY_HISTORY_TABLE,
    QUALITY_RECOVERY_RUNS_TABLE,
    QUALITY_RECOVERY_TARGETS_TABLE,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    CAPTURES_TABLE,
    FINALIZATIONS_TABLE,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.wb_fbs_orders import IDENTITY_MAPPINGS_TABLE  # noqa: E402


CONTRACT_NAME = "wbc0027_exact_fbs_sku_mapping_extension_v1"
CONTRACT_VERSION = 1
CANONICAL_TARGET_ID = "wb_core_eu_hosted_runtime_active"
DIAGNOSIS_RUNTIME_SHA = "999c53285ca684bd3b1d2caa5992594f8870ffc7"
EXPECTED_OPERATIONAL_GENERATION_ID = "operational-c54072027f14f90b374b"
EXPECTED_MANIFEST_SHA256 = (
    "sha256:8cdd437b7357042092a8be2e1fdce028af2444c81a464465dbadd557b57a2ffb"
)
EXPECTED_SQLITE_SCHEMA_VERSION = 987
EXPECTED_CUTOVER_ID = "ffcut_d2816d894a75390dcaa6514c0a96"
EXPECTED_EXTERNAL_IDENTITY_DIGEST = (
    recovery_module.EXACT_MAPPING_EXTERNAL_IDENTITY_DIGEST
)
EXPECTED_TUPLE_COUNT = 1
EXPECTED_ACTIVE_OWNER_COUNT = 1
EXPECTED_ACTIVE_MAPPING_COUNT = 0
EXPECTED_TARGET_NM_ID = 428855758
EXPECTED_BLOCKER_CARDINALITY = {
    recovery_module.MOSCOW_FACILITY_ID: {"orders": 213, "statuses": 1094},
    recovery_module.ORENBURG_FACILITY_ID: {"orders": 8, "statuses": 41},
}
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")


class Wbc0027MappingError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class Wbc0027ExactFbsSkuMappingExtension:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        target_id: str = CANONICAL_TARGET_ID,
        timestamp_factory: Any | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if SAFE_SHA_RE.fullmatch(self.deployed_sha) is None:
            raise Wbc0027MappingError(
                "invalid_deployed_sha", "deployed_sha must be exact 40-hex"
            )
        self.target_id = str(target_id or "").strip()
        if self.target_id != CANONICAL_TARGET_ID:
            raise Wbc0027MappingError(
                "non_canonical_target", "Mapping profile target is not canonical"
            )
        self.timestamp_factory = timestamp_factory or _utc_now
        self.recovery = recovery_module.Wbc0027FbsLifecycleQualityRecovery(
            runtime_dir=Path(runtime_dir),
            deployed_sha=self.deployed_sha,
            timestamp_factory=self.timestamp_factory,
            scratch_dir=scratch_dir,
        )

    @property
    def mapping(self) -> dict[str, Any]:
        identity = dict(recovery_module.EXACT_MAPPING_TUPLE)
        tuple_digest = recovery_module.exact_mapping_tuple_digest(identity)
        return {
            "mapping_id": "fbs_sku_" + tuple_digest.removeprefix("sha256:")[:32],
            **identity,
            "mapping_digest": tuple_digest,
            "active": 1,
            "created_by": "production-apply-runner",
        }

    def build_plan(self, *, external_identity_digest: str) -> dict[str, Any]:
        generated_at = str(self.timestamp_factory())
        recovery_module._require_utc(generated_at)
        external_digest = recovery_module._require_digest(
            external_identity_digest, "external_identity_digest"
        )
        storage = self.recovery._storage_identity()
        current_source, identity_snapshot = self._current_snapshot(storage=storage)
        blockers = _binding_blockers(
            deployed_sha=self.deployed_sha,
            target_id=self.target_id,
            external_identity_digest=external_digest,
            storage=storage,
            source=current_source,
            identity_snapshot=identity_snapshot,
        )
        hypothetical_rehearsal: dict[str, Any] = {
            "status": "not_run",
            "reason": "mapping_preflight_blocked",
        }
        if not blockers:
            mapping = {**self.mapping, "created_at": generated_at}
            rehearsal = self._hypothetical_recovery_plan(mapping)
            hypothetical_rehearsal = _rehearsal_summary(rehearsal)
            if hypothetical_rehearsal.get("accepted") is not True:
                blockers.append("hypothetical_recovery_rehearsal_not_exact")
        material_cas = {
            "target_id": self.target_id,
            "deployed_sha": self.deployed_sha,
            "diagnosis_runtime_sha": DIAGNOSIS_RUNTIME_SHA,
            "external_identity_digest": external_digest,
            "tuple_contract": recovery_module.EXACT_MAPPING_TUPLE_CONTRACT,
            "tuple_digest": recovery_module.exact_mapping_tuple_digest(),
            "mapping": self.mapping,
            "storage": storage,
            "boundary": {
                "cutover_id": current_source["cutover_id"],
                "cutover_manifest_digest": current_source["cutover_manifest_digest"],
                "forward_generation_id": current_source["generation_id"],
                "forward_generation_manifest_fingerprint": current_source[
                    "generation_manifest_fingerprint"
                ],
            },
            "identity_snapshot": identity_snapshot,
            "typed_blocker_rows": current_source["typed_blocker_rows"],
            "coverage": current_source["coverage"],
        }
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "generated_at": generated_at,
            "target_id": self.target_id,
            "deployed_sha": self.deployed_sha,
            "diagnosis": {
                "runtime_sha": DIAGNOSIS_RUNTIME_SHA,
                "operational_generation_id": EXPECTED_OPERATIONAL_GENERATION_ID,
                "manifest_sha256": EXPECTED_MANIFEST_SHA256,
                "sqlite_schema_version": EXPECTED_SQLITE_SCHEMA_VERSION,
                "cutover_id": EXPECTED_CUTOVER_ID,
                "external_identity_digest": EXPECTED_EXTERNAL_IDENTITY_DIGEST,
            },
            "storage": storage,
            "boundary": material_cas["boundary"],
            "scope": {
                "tuple_contract": recovery_module.EXACT_MAPPING_TUPLE_CONTRACT,
                "tuple_digest": recovery_module.exact_mapping_tuple_digest(),
                "external_identity_digest": external_digest,
                "tuple_count": identity_snapshot["tuple_count"],
                "active_owner_count": identity_snapshot["active_owner_count"],
                "active_mapping_count": identity_snapshot["active_mapping_count"],
                "all_mapping_count": identity_snapshot["all_mapping_count"],
                "target_nm_id": int(self.mapping["target_nm_id"]),
                "mapping": self.mapping,
                "typed_blocker_rows": current_source["typed_blocker_rows"],
                "coverage": current_source["coverage"],
            },
            "hypothetical_rehearsal": hypothetical_rehearsal,
            "material_cas_digest": recovery_module._fingerprint(material_cas),
            "safety": {
                "default_mode": "query_only_dry_run",
                "two_consecutive_material_witnesses_required": True,
                "writer_lock": "warehouse_functional_write_lock",
                "private_before_image": "mode_0600_exclusive_create",
                "one_submit": True,
                "one_insert_max": 1,
                "blind_retry": False,
                "query_only_readback": True,
                "lifecycle_debit_count": 0,
                "balance_write_count": 0,
                "history_write_count": 0,
                "public_write_count": 0,
                "outbox_write_count": 0,
                "wb_write_count": 0,
            },
            "apply_allowed": not blockers,
            "blockers": sorted(set(blockers)),
        }
        plan["fingerprint"] = _plan_fingerprint(plan)
        return plan

    def rehearse(self, *, external_identity_digest: str) -> dict[str, Any]:
        plan = self.build_plan(external_identity_digest=external_identity_digest)
        return {
            "contract_name": CONTRACT_NAME,
            "mode": "query_only_no_create_hypothetical_rehearsal",
            "target_id": plan["target_id"],
            "deployed_sha": plan["deployed_sha"],
            "storage": plan["storage"],
            "boundary": plan["boundary"],
            "scope": plan["scope"],
            "hypothetical_rehearsal": plan["hypothetical_rehearsal"],
            "source_database_query_only": True,
            "durable_plan_created": False,
            "mapping_insert_count": 0,
            "recovery_write_count": 0,
            "history_write_count": 0,
            "apply_allowed": plan["apply_allowed"],
            "blockers": plan["blockers"],
            "fingerprint": plan["fingerprint"],
        }

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        external_identity_digest: str,
        approval_reference: str,
        actor: str,
        evidence_dir: Path,
    ) -> dict[str, Any]:
        reviewed = dict(reviewed_plan)
        expected = recovery_module._require_digest(fingerprint, "fingerprint")
        external_digest = recovery_module._require_digest(
            external_identity_digest, "external_identity_digest"
        )
        if reviewed.get("fingerprint") != expected or _plan_fingerprint(reviewed) != expected:
            raise Wbc0027MappingError(
                "reviewed_fingerprint_mismatch", "Reviewed mapping plan differs"
            )
        if reviewed.get("apply_allowed") is not True or reviewed.get("blockers"):
            raise Wbc0027MappingError(
                "reviewed_plan_blocked", "Blocked mapping plan cannot apply"
            )
        scope = dict(reviewed.get("scope") or {})
        if scope.get("external_identity_digest") != external_digest:
            raise Wbc0027MappingError(
                "external_identity_digest_drift", "Accepted diagnosis digest changed"
            )
        approval = str(approval_reference or "").strip()
        operator = str(actor or "").strip()
        if not approval or not operator:
            raise Wbc0027MappingError(
                "gate_identity_required", "approval_reference and actor are required"
            )
        existing = self.readback()
        if existing.get("status") == "completed":
            return {**existing, "idempotent": True, "repeat_submit_performed": False}

        fresh = self.build_plan(external_identity_digest=external_digest)
        if fresh.get("fingerprint") != expected:
            raise Wbc0027MappingError(
                "mapping_material_cas_drift", "Mapping material changed before lock"
            )
        evidence_root = Path(evidence_dir).expanduser().resolve()
        if not evidence_root.is_dir():
            raise Wbc0027MappingError(
                "evidence_directory_missing", "Private evidence directory is missing"
            )
        now = str(self.timestamp_factory())
        recovery_module._require_utc(now)
        before_path = evidence_root / (
            "wbc0027-fbs-mapping-" + expected.removeprefix("sha256:")[:20] + ".before.json"
        )
        mapping = self.mapping
        with warehouse_functional_write_lock(
            self.recovery.runtime.runtime_dir, timeout_seconds=300
        ):
            conn = sqlite3.connect(self.recovery.runtime.db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.execute("BEGIN IMMEDIATE")
                storage = self.recovery._storage_identity(conn=conn)
                current_source = self.recovery._source_snapshot(conn, storage=storage)
                identity_snapshot = _identity_snapshot(conn, current_source=current_source)
                locked_material = _locked_material(
                    deployed_sha=self.deployed_sha,
                    target_id=self.target_id,
                    external_identity_digest=external_digest,
                    storage=storage,
                    source=current_source,
                    identity_snapshot=identity_snapshot,
                    mapping=mapping,
                )
                if recovery_module._fingerprint(locked_material) != str(
                    reviewed["material_cas_digest"]
                ):
                    raise Wbc0027MappingError(
                        "mapping_material_cas_drift",
                        "Mapping material changed inside writer lock",
                    )
                before_image = {
                    "contract_name": CONTRACT_NAME,
                    "fingerprint": expected,
                    "target_id": self.target_id,
                    "deployed_sha": self.deployed_sha,
                    "external_identity_digest": external_digest,
                    "tuple_digest": recovery_module.exact_mapping_tuple_digest(),
                    "storage": storage,
                    "boundary": reviewed["boundary"],
                    "identity_snapshot": identity_snapshot,
                    "mapping": mapping,
                    "recovery": "single transaction rollback before commit",
                    "created_at": now,
                }
                _write_private_exclusive(before_path, before_image)
                before_changes = int(conn.total_changes)
                conn.set_authorizer(_mapping_only_authorizer)
                conn.execute(
                    f"""INSERT INTO {IDENTITY_MAPPINGS_TABLE}(
                           mapping_id,source_nm_id,source_chrt_id,source_barcode,
                           source_sku,target_nm_id,mapping_digest,active,
                           created_at,created_by
                       ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
                    (
                        str(mapping["mapping_id"]),
                        int(mapping["source_nm_id"]),
                        int(mapping["source_chrt_id"]),
                        str(mapping["source_barcode"]),
                        str(mapping["source_sku"]),
                        int(mapping["target_nm_id"]),
                        str(mapping["mapping_digest"]),
                        now,
                        operator,
                    ),
                )
                inserted = int(conn.total_changes) - before_changes
                if inserted != 1:
                    raise Wbc0027MappingError(
                        "mapping_insert_count_invalid", "Mapping submit was not one insert"
                    )
                row = conn.execute(
                    f"SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,"
                    f"source_sku,target_nm_id,mapping_digest,active "
                    f"FROM {IDENTITY_MAPPINGS_TABLE} WHERE mapping_id=?",
                    (str(mapping["mapping_id"]),),
                ).fetchone()
                if row is None or _mapping_row(row) != _mapping_row_from_plan(mapping):
                    raise Wbc0027MappingError(
                        "mapping_readback_mismatch", "Inserted mapping readback drifted"
                    )
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.set_authorizer(None)
                conn.close()
        readback = self.readback()
        if readback.get("status") != "completed":
            raise Wbc0027MappingError(
                "mapping_readback_not_reconciled", "Query-only mapping readback failed"
            )
        return {
            **readback,
            "fingerprint": expected,
            "approval_reference": approval,
            "applied_by": operator,
            "before_image_path": str(before_path),
            "before_image_sha256": recovery_module._sha256_file(before_path),
            "apply_count": 1,
            "mapping_insert_count": 1,
            "lifecycle_debit_count": 0,
            "balance_write_count": 0,
            "history_write_count": 0,
            "public_write_count": 0,
            "outbox_write_count": 0,
            "wb_write_count": 0,
            "query_only_terminal_readback": True,
        }

    def readback(self) -> dict[str, Any]:
        mapping = self.mapping
        with closing(recovery_module._open_query_only(self.recovery.runtime.db_path)) as conn:
            exact_rows = conn.execute(
                f"""SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,
                           source_sku,target_nm_id,mapping_digest,active
                    FROM {IDENTITY_MAPPINGS_TABLE}
                    WHERE source_nm_id=? AND source_chrt_id=?
                      AND source_barcode=? AND source_sku=?
                    ORDER BY mapping_id""",
                (
                    int(mapping["source_nm_id"]),
                    int(mapping["source_chrt_id"]),
                    str(mapping["source_barcode"]),
                    str(mapping["source_sku"]),
                ),
            ).fetchall()
        expected = _mapping_row_from_plan(mapping)
        exact = len(exact_rows) == 1 and _mapping_row(exact_rows[0]) == expected
        return {
            "contract_name": CONTRACT_NAME,
            "status": "completed" if exact else "not_applied",
            "target_id": self.target_id,
            "deployed_sha": self.deployed_sha,
            "mapping": expected if exact else None,
            "exact_mapping_row_count": len(exact_rows),
            "query_only": True,
            "mapping_insert_count": 0,
            "recovery_write_count": 0,
            "history_write_count": 0,
            "wb_write_count": 0,
        }

    def _current_snapshot(
        self, *, storage: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with closing(recovery_module._open_query_only(self.recovery.runtime.db_path)) as conn:
            conn.execute("BEGIN")
            try:
                source = self.recovery._source_snapshot(conn, storage=storage)
                identity = _identity_snapshot(conn, current_source=source)
                if self.recovery._storage_identity(conn=conn) != dict(storage):
                    raise Wbc0027MappingError(
                        "storage_generation_drift", "Storage changed in query-only snapshot"
                    )
            finally:
                if conn.in_transaction:
                    conn.rollback()
        return source, identity

    def _hypothetical_recovery_plan(
        self, mapping: Mapping[str, Any]
    ) -> dict[str, Any]:
        previous = self.recovery.scratch_dir
        with TemporaryDirectory(prefix="wbc0027-fbs-mapping-rehearsal-") as directory:
            self.recovery.scratch_dir = Path(directory)
            try:
                return self.recovery.build_plan(hypothetical_mapping=mapping)
            finally:
                self.recovery.scratch_dir = previous


def _identity_snapshot(
    conn: sqlite3.Connection,
    *,
    current_source: Mapping[str, Any],
) -> dict[str, Any]:
    identity = dict(recovery_module.EXACT_MAPPING_TUPLE)
    owners = [
        dict(row)
        for row in conn.execute(
            """SELECT item_id,nm_id FROM sheet_vitrina_v1_nomenclature_items
               WHERE is_active=1 AND is_hidden=0 AND nm_id=? AND vendor_code=?
                 AND (barcode=? OR EXISTS(
                     SELECT 1 FROM json_each(barcodes_json) WHERE value=?
                 )) ORDER BY item_id""",
            (
                int(identity["target_nm_id"]),
                str(identity["source_sku"]),
                str(identity["source_barcode"]),
                str(identity["source_barcode"]),
            ),
        )
    ]
    all_mappings = [
        dict(row)
        for row in conn.execute(
            f"""SELECT mapping_id,target_nm_id,mapping_digest,active
                FROM {IDENTITY_MAPPINGS_TABLE}
                WHERE source_nm_id=? AND source_chrt_id=?
                  AND source_barcode=? AND source_sku=?
                ORDER BY mapping_id""",
            (
                int(identity["source_nm_id"]),
                int(identity["source_chrt_id"]),
                str(identity["source_barcode"]),
                str(identity["source_sku"]),
            ),
        )
    ]
    active_mappings = [row for row in all_mappings if int(row["active"]) == 1]
    blocker_rows = [dict(row) for row in current_source["typed_blocker_rows"]]
    tuple_digests = {
        str(row.get("tuple_digest") or "")
        for row in blocker_rows
        if str(row.get("tuple_digest") or "")
    }
    return {
        "tuple_count": len(tuple_digests),
        "tuple_digest": recovery_module.exact_mapping_tuple_digest(),
        "external_identity_digest": EXPECTED_EXTERNAL_IDENTITY_DIGEST,
        "active_owner_count": len(owners),
        "owner_digest": recovery_module._fingerprint(owners),
        "active_mapping_count": len(active_mappings),
        "all_mapping_count": len(all_mappings),
        "active_mapping_digest": recovery_module._fingerprint(active_mappings),
        "all_mapping_digest": recovery_module._fingerprint(all_mappings),
    }


def _binding_blockers(
    *,
    deployed_sha: str,
    target_id: str,
    external_identity_digest: str,
    storage: Mapping[str, Any],
    source: Mapping[str, Any],
    identity_snapshot: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    checks = {
        "canonical_target_drift": target_id == CANONICAL_TARGET_ID,
        "external_identity_digest_drift": external_identity_digest
        == EXPECTED_EXTERNAL_IDENTITY_DIGEST,
        "storage_manifest_drift": storage.get("manifest_sha256")
        == EXPECTED_MANIFEST_SHA256,
        "storage_generation_drift": storage.get("operational_generation_id")
        == EXPECTED_OPERATIONAL_GENERATION_ID,
        "storage_schema_revision_drift": storage.get("sqlite_schema_version")
        == EXPECTED_SQLITE_SCHEMA_VERSION,
        "cutover_drift": source.get("cutover_id") == EXPECTED_CUTOVER_ID,
        "tuple_count_drift": identity_snapshot.get("tuple_count")
        == EXPECTED_TUPLE_COUNT,
        "tuple_digest_drift": identity_snapshot.get("tuple_digest")
        == recovery_module.exact_mapping_tuple_digest(),
        "owner_count_drift": identity_snapshot.get("active_owner_count")
        == EXPECTED_ACTIVE_OWNER_COUNT,
        "active_mapping_count_drift": identity_snapshot.get("active_mapping_count")
        == EXPECTED_ACTIVE_MAPPING_COUNT,
        "duplicate_mapping_present": identity_snapshot.get("all_mapping_count") == 0,
        "target_nm_drift": int(recovery_module.EXACT_MAPPING_TUPLE["target_nm_id"])
        == EXPECTED_TARGET_NM_ID,
    }
    for code, passed in checks.items():
        if not passed:
            blockers.append(code)
    typed = list(source.get("typed_blocker_rows") or [])
    if len(typed) != 2:
        blockers.append("typed_blocker_evidence_absent_or_ambiguous")
    else:
        for row in typed:
            facility_id = str(row.get("facility_id") or "")
            expected = EXPECTED_BLOCKER_CARDINALITY.get(facility_id)
            if (
                expected is None
                or int(row.get("nm_id") or 0) != EXPECTED_TARGET_NM_ID
                or row.get("identity_error_code")
                != "identity_evidence_missing_or_drifted"
                or row.get("mapping_error_code") != "order_sku_unmapped"
                or row.get("external_identity_digest")
                != EXPECTED_EXTERNAL_IDENTITY_DIGEST
                or row.get("tuple_digest")
                != recovery_module.exact_mapping_tuple_digest()
                or int(row.get("order_count") or 0) != expected["orders"]
                or int(row.get("status_observation_count") or 0)
                != expected["statuses"]
            ):
                blockers.append("typed_blocker_scope_or_cardinality_drift")
    coverage = dict(source.get("coverage") or {})
    if coverage.get("full_original_scope_evidenced") is not True:
        blockers.append("exact_four_group_evidence_missing")
    if coverage.get("all_groups_resolvable") is not False:
        blockers.append("mapping_precondition_not_missing")
    if set(source.get("blockers") or []) != {
        "exact_four_group_coverage_missing",
        "typed_identity_mapping_blockers_present",
    }:
        blockers.append("recovery_blocker_set_drift")
    if not SAFE_SHA_RE.fullmatch(str(deployed_sha or "")):
        blockers.append("runtime_sha_invalid")
    return blockers


def _rehearsal_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    scope = dict(plan.get("scope") or {})
    coverage = dict(scope.get("coverage") or {})
    history = dict(plan.get("history") or {})
    expected_groups = [
        {"facility_id": facility_id, "nm_id": nm_id}
        for facility_id, nm_id in recovery_module.TARGET_GROUPS
    ]
    accepted = bool(
        plan.get("apply_allowed") is True
        and plan.get("blockers") == []
        and scope.get("groups") == expected_groups
        and scope.get("typed_blocker_rows") == []
        and coverage.get("all_groups_resolvable") is True
        and coverage.get("resolved_groups")
        == recovery_module._group_rows(recovery_module.TARGET_GROUP_SET)
        and len(scope.get("dates") or []) == 15
        and len(history.get("captures") or []) == 15
        and history.get("blockers") == []
        and dict(plan.get("safety") or {}).get("production_mapping_inserts") == 0
        and dict(plan.get("safety") or {}).get("production_recovery_writes") == 0
        and dict(plan.get("safety") or {}).get("production_history_writes") == 0
    )
    return {
        "status": "ready" if accepted else "blocked",
        "accepted": accepted,
        "recovery_contract_name": plan.get("contract_name"),
        "recovery_fingerprint": plan.get("fingerprint"),
        "resolved_groups": coverage.get("resolved_groups"),
        "target_count": scope.get("target_count"),
        "status_observation_count": len(
            scope.get("status_observation_sequences") or []
        ),
        "date_count": len(scope.get("dates") or []),
        "history_capture_count": len(history.get("captures") or []),
        "stable_target_digest": scope.get("stable_target_digest"),
        "history_digest": history.get("digest"),
        "mapping_insert_count": 0,
        "recovery_write_count": 0,
        "history_write_count": 0,
        "source_database_query_only": True,
        "blockers": list(plan.get("blockers") or []),
    }


def _locked_material(
    *,
    deployed_sha: str,
    target_id: str,
    external_identity_digest: str,
    storage: Mapping[str, Any],
    source: Mapping[str, Any],
    identity_snapshot: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "target_id": target_id,
        "deployed_sha": deployed_sha,
        "diagnosis_runtime_sha": DIAGNOSIS_RUNTIME_SHA,
        "external_identity_digest": external_identity_digest,
        "tuple_contract": recovery_module.EXACT_MAPPING_TUPLE_CONTRACT,
        "tuple_digest": recovery_module.exact_mapping_tuple_digest(),
        "mapping": dict(mapping),
        "storage": dict(storage),
        "boundary": {
            "cutover_id": source["cutover_id"],
            "cutover_manifest_digest": source["cutover_manifest_digest"],
            "forward_generation_id": source["generation_id"],
            "forward_generation_manifest_fingerprint": source[
                "generation_manifest_fingerprint"
            ],
        },
        "identity_snapshot": dict(identity_snapshot),
        "typed_blocker_rows": list(source["typed_blocker_rows"]),
        "coverage": dict(source["coverage"]),
    }


def _mapping_only_authorizer(
    action: int,
    arg1: str | None,
    _arg2: str | None,
    _database: str | None,
    _trigger: str | None,
) -> int:
    if action in {sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_INSERT and str(arg1 or "") != IDENTITY_MAPPINGS_TABLE:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _mapping_row(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "mapping_id": str(row[0]),
        "source_nm_id": int(row[1]),
        "source_chrt_id": int(row[2]),
        "source_barcode": str(row[3]),
        "source_sku": str(row[4]),
        "target_nm_id": int(row[5]),
        "mapping_digest": str(row[6]),
        "active": int(row[7]),
    }


def _mapping_row_from_plan(mapping: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(mapping)
    return {
        "mapping_id": str(item["mapping_id"]),
        "source_nm_id": int(item["source_nm_id"]),
        "source_chrt_id": int(item["source_chrt_id"]),
        "source_barcode": str(item["source_barcode"]),
        "source_sku": str(item["source_sku"]),
        "target_nm_id": int(item["target_nm_id"]),
        "mapping_digest": str(item["mapping_digest"]),
        "active": 1,
    }


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    return recovery_module._fingerprint(recovery_module._stable_plan_material(plan))


def _write_private_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    output = Path(path).resolve()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_plan(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Wbc0027MappingError("invalid_plan", "Reviewed plan must be an object")
    return value


def run(args: argparse.Namespace) -> int:
    runner = Wbc0027ExactFbsSkuMappingExtension(
        runtime_dir=Path(args.runtime_dir),
        deployed_sha=str(args.deployed_sha),
        target_id=str(args.target_id),
        scratch_dir=(Path(args.scratch_dir) if args.scratch_dir else None),
    )
    if args.command == "mapping-apply":
        payload = runner.apply(
            _read_plan(args.plan_file),
            fingerprint=str(args.fingerprint),
            external_identity_digest=str(args.external_identity_digest),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            evidence_dir=Path(args.evidence_dir),
        )
    elif args.command == "mapping-readback":
        payload = runner.readback()
    elif args.command == "mapping-rehearsal":
        if args.output:
            raise Wbc0027MappingError(
                "rehearsal_output_forbidden",
                "Query-only/no-create rehearsal cannot persist an output artifact",
            )
        payload = runner.rehearse(
            external_identity_digest=str(args.external_identity_digest)
        )
    else:
        payload = runner.build_plan(
            external_identity_digest=str(args.external_identity_digest)
        )
    if args.output:
        if args.command == "mapping-dry-run":
            _write_private_exclusive(Path(args.output), payload)
        else:
            recovery_module._write_private(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") not in {"blocked", "error"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--target-id", default=CANONICAL_TARGET_ID)
    parser.add_argument("--scratch-dir", default="")
    parser.add_argument("--output", default="")
    sub = parser.add_subparsers(dest="command")
    dry = sub.add_parser("mapping-dry-run")
    dry.add_argument("--external-identity-digest", required=True)
    rehearsal = sub.add_parser("mapping-rehearsal")
    rehearsal.add_argument("--external-identity-digest", required=True)
    apply = sub.add_parser("mapping-apply")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--external-identity-digest", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--evidence-dir", required=True)
    sub.add_parser("mapping-readback")
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.command is None:
            args.command = "mapping-dry-run"
            args.external_identity_digest = EXPECTED_EXTERNAL_IDENTITY_DIGEST
        return run(args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": getattr(exc, "code", "error"),
                    "error": str(exc),
                    "details": getattr(exc, "details", None),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
