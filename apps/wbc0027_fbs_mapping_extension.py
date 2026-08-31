#!/usr/bin/env python3
"""Exact WBC0027 canonical SKU mapping extension.

The default command is a query-only dry-run.  It exact-binds the accepted
external diagnosis and an independently computed versioned tuple digest,
proves the current StoreRegistry/cutover/count boundary, and rehearses the
complete derived lifecycle impact with a hypothetical mapping.  Apply performs at
most one mapping-table insert under two material CAS checks.  It has no
lifecycle, balance, history, public or outbox mutation primitive.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
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
from packages.application.fbs_lifecycle_manifests import (  # noqa: E402
    FbsManifestError,
    MAPPING_MANIFEST_CONTRACT,
    attach_digest,
    digest as manifest_digest,
    parse_incident_passport,
    parse_mapping_manifest,
    read_json as read_manifest_json,
)
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


CONTRACT_NAME = MAPPING_MANIFEST_CONTRACT
CONTRACT_VERSION = 2
CANONICAL_TARGET_ID = "wb_core_eu_hosted_runtime_active"
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
        incident_passport: Mapping[str, Any],
        target_id: str = CANONICAL_TARGET_ID,
        timestamp_factory: Any | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if SAFE_SHA_RE.fullmatch(self.deployed_sha) is None:
            raise Wbc0027MappingError(
                "invalid_deployed_sha", "deployed_sha must be exact 40-hex"
            )
        try:
            self.incident_passport = parse_incident_passport(incident_passport)
        except FbsManifestError as exc:
            raise Wbc0027MappingError(exc.code, str(exc), details=exc.details) from exc
        self.target_id = str(target_id or "").strip()
        if self.target_id != CANONICAL_TARGET_ID:
            raise Wbc0027MappingError(
                "non_canonical_target", "Mapping profile target is not canonical"
            )
        self.timestamp_factory = timestamp_factory or _utc_now
        self.recovery = recovery_module.Wbc0027FbsLifecycleQualityRecovery(
            runtime_dir=Path(runtime_dir),
            deployed_sha=self.deployed_sha,
            incident_passport=self.incident_passport,
            timestamp_factory=self.timestamp_factory,
            scratch_dir=scratch_dir,
        )

    @property
    def mapping(self) -> dict[str, Any]:
        identity = dict(self.incident_passport["tuple"])
        tuple_digest = str(identity.pop("tuple_digest"))
        identity.pop("tuple_contract", None)
        return {
            "mapping_id": "fbs_sku_" + tuple_digest.removeprefix("sha256:")[:32],
            **identity,
            "mapping_digest": tuple_digest,
            "active": 1,
            "created_by": "production-apply-runner",
        }

    def build_plan(self) -> dict[str, Any]:
        external_digest = str(self.incident_passport["evidence"]["external_identity_digest"])
        storage = self.recovery._storage_identity()
        admission_source, identity_snapshot = self._current_snapshot(storage=storage)
        blockers = _binding_blockers(
            passport=self.incident_passport,
            storage=storage,
            admission_source=admission_source,
            identity_snapshot=identity_snapshot,
        )
        mapping = self.mapping
        storage_binding = {
            key: storage[key]
            for key in (
                "manifest_sha256",
                "operational_generation_id",
                "operational_schema_revision",
                "sqlite_schema_version",
            )
        }
        evidence = {
            "external_identity_digest": external_digest,
            "owner_digest": str(identity_snapshot["owner_digest"]),
            "warehouse_evidence_digest": manifest_digest(
                {
                    "contract": "fbs_mapping_warehouse_evidence_deferred/v1",
                    "tuple_digest": self.incident_passport["tuple"]["tuple_digest"],
                    "admission": "recovery_phase_only",
                }
            ),
            "facility_admission_digest": manifest_digest(
                {
                    "contract": "fbs_mapping_facility_admission_deferred/v1",
                    "target_nm_id": self.incident_passport["tuple"]["target_nm_id"],
                    "admission": "recovery_phase_only",
                }
            ),
        }
        material_cas = {
            "tuple_digest": self.incident_passport["tuple"]["tuple_digest"],
            "mapping_digest": mapping["mapping_digest"],
            "target_digest": manifest_digest(
                {
                    "target_id": self.target_id,
                    "runtime_sha": self.deployed_sha,
                    "source_runtime_sha": self.incident_passport["target"][
                        "source_runtime_sha"
                    ],
                }
            ),
            "storage_digest": manifest_digest(storage_binding),
            "cutover_digest": manifest_digest(
                {
                    "cutover_id": admission_source["cutover_id"],
                    "cutover_manifest_digest": admission_source["cutover_manifest_digest"],
                    "forward_generation_id": admission_source["generation_id"],
                    "forward_generation_manifest_digest": admission_source[
                        "generation_manifest_fingerprint"
                    ],
                }
            ),
            "identity_digest": manifest_digest(
                _identity_admission_material(identity_snapshot)
            ),
            "evidence_digest": manifest_digest(evidence),
        }
        material_cas["digest"] = manifest_digest(material_cas)
        plan_material: dict[str, Any] = {
            "contract": CONTRACT_NAME,
            "operation_id": self.incident_passport["operation_id"],
            "target": {
                "target_id": self.target_id,
                "runtime_sha": self.deployed_sha,
                "source_runtime_sha": self.incident_passport["target"][
                    "source_runtime_sha"
                ],
            },
            "storage": storage_binding,
            "cutover": {
                "cutover_id": admission_source["cutover_id"],
                "cutover_manifest_digest": admission_source["cutover_manifest_digest"],
                "forward_generation_id": admission_source["generation_id"],
                "forward_generation_manifest_digest": admission_source[
                    "generation_manifest_fingerprint"
                ],
            },
            "tuple": dict(self.incident_passport["tuple"]),
            "evidence": evidence,
            "expectation": dict(self.incident_passport["mapping_expectation"]),
            "proposed_mapping": {
                "mapping_id": mapping["mapping_id"],
                "mapping_digest": mapping["mapping_digest"],
            },
            "material_cas": material_cas,
            "safety": {
                "default_mode": "query_only_dry_run",
                "two_consecutive_material_witnesses_required": True,
                "writer_lock": "warehouse_functional_write_lock",
                "root_storage_admission": "production_apply_evidence",
                "private_before_image": "mode_0600_exclusive_create_fsync",
                "private_backup": "mode_0600_exclusive_create_fsync",
                "operation_journal": "exact_operation_authorization_storage",
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
        plan = attach_digest(plan_material, "manifest_digest")
        try:
            return parse_mapping_manifest(plan)
        except FbsManifestError as exc:
            raise Wbc0027MappingError(exc.code, str(exc), details=exc.details) from exc

    def rehearse(self) -> dict[str, Any]:
        with warehouse_functional_write_lock(
            self.recovery.runtime.runtime_dir,
            blocking=False,
        ) as lock:
            return self._rehearse_locked(lock_receipt=dict(lock))

    def _rehearse_locked(
        self, *, lock_receipt: Mapping[str, Any]
    ) -> dict[str, Any]:
        plan = self.build_plan()
        release_metadata: dict[str, Any] = {}
        release_metadata_path = (
            self.recovery.runtime.runtime_dir.parent / "app" / ".wb-core-deploy.json"
        )
        if release_metadata_path.is_file():
            try:
                raw_release_metadata = json.loads(
                    release_metadata_path.read_text(encoding="utf-8")
                )
                if isinstance(raw_release_metadata, dict):
                    release_metadata = raw_release_metadata
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                release_metadata = {"invalid": True}
        release_state = "fixture_without_runtime_metadata"
        release_binding_ok = not release_metadata
        if release_metadata:
            predecessor_interrupted = bool(
                release_metadata.get("commit") == plan["target"]["source_runtime_sha"]
                and release_metadata.get("deployment_complete") is False
            )
            candidate_terminal = bool(
                release_metadata.get("commit") == self.deployed_sha
                and release_metadata.get("deployment_complete") is True
            )
            release_binding_ok = predecessor_interrupted or candidate_terminal
            release_state = (
                "predecessor_interrupted"
                if predecessor_interrupted
                else "candidate_terminal"
                if candidate_terminal
                else "unbound"
            )
        negative_case_code = ""
        invalid = {
            **plan,
            "safety": {**dict(plan["safety"]), "statuses": []},
        }
        try:
            parse_mapping_manifest(invalid)
        except FbsManifestError as exc:
            negative_case_code = exc.code
        locked_plan_digest = str(self.build_plan()["manifest_digest"])
        private_plan_mode = 0
        with TemporaryDirectory(prefix="wbc0027-fbs-private-plan-") as directory:
            private_path = Path(directory) / "candidate.json"
            _write_private_exclusive(private_path, plan)
            private_plan_mode = private_path.stat().st_mode & 0o777
        hypothetical_readback: dict[str, Any] = {}
        with closing(sqlite3.connect(":memory:")) as scratch:
            scratch.execute(
                """CREATE TABLE hypothetical_mapping(
                       mapping_id TEXT PRIMARY KEY,source_nm_id INTEGER,
                       source_chrt_id INTEGER,source_barcode TEXT,source_sku TEXT,
                       target_nm_id INTEGER,mapping_digest TEXT,active INTEGER)"""
            )
            mapping = self.mapping
            scratch.execute(
                """INSERT INTO hypothetical_mapping VALUES(?,?,?,?,?,?,?,?)""",
                (
                    mapping["mapping_id"], mapping["source_nm_id"],
                    mapping["source_chrt_id"], mapping["source_barcode"],
                    mapping["source_sku"], mapping["target_nm_id"],
                    mapping["mapping_digest"], mapping["active"],
                ),
            )
            row = scratch.execute(
                "SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,"
                "source_sku,target_nm_id,mapping_digest,active "
                "FROM hypothetical_mapping WHERE mapping_id=?",
                (mapping["mapping_id"],),
            ).fetchone()
            if row is not None:
                hypothetical_readback = _mapping_row(row)
        impact: dict[str, Any] | None = None
        recovery: dict[str, Any] | None = None
        if plan["apply_allowed"]:
            impact, recovery = self.recovery.build_manifests(
                hypothetical_mapping={
                    **self.mapping,
                    "tuple_digest": self.incident_passport["tuple"]["tuple_digest"],
                    "external_identity_digest": self.incident_passport["evidence"][
                        "external_identity_digest"
                    ],
                }
            )
        impact_value = dict(impact or {})
        recovery_value = dict(recovery or {})
        history = dict(recovery_value.get("history") or {})
        scope_kinds = sorted(
            {str(item.get("scope_kind") or "") for item in impact_value.get("affected_groups") or []}
        )
        matrix = [
            {
                "check": "runtime_release_interruption_binding",
                "status": "PASS"
                if plan["target"]["runtime_sha"] == self.deployed_sha
                and release_binding_ok
                else "FAIL",
                "evidence": {
                    "candidate_runtime_sha": self.deployed_sha,
                    "source_runtime_sha": plan["target"]["source_runtime_sha"],
                    "release_runtime_contract": self.incident_passport["target"]["release_runtime_contract"],
                    "release_state": release_state,
                    "runtime_release_metadata": release_metadata,
                },
            },
            {
                "check": "strict_mapping_positive_negative",
                "status": "PASS" if negative_case_code == "mapping_scope_field_forbidden" else "FAIL",
                "evidence": {"positive_digest": plan["manifest_digest"], "negative_code": negative_case_code},
            },
            {
                "check": "hypothetical_insert_readback",
                "status": "PASS"
                if hypothetical_readback == _mapping_row_from_plan(self.mapping)
                and recovery_value.get("apply_allowed") is True
                else "FAIL",
                "evidence": {
                    "production_insert_count": 0,
                    "scratch_insert_count": 1,
                    "scratch_readback_digest": manifest_digest(hypothetical_readback),
                    "recovery_digest": recovery_value.get("recovery_digest"),
                },
            },
            {
                "check": "global_impact_full_scan",
                "status": "PASS"
                if dict(impact_value.get("unresolved_scan") or {}).get("full_scan") is True
                and {"FACILITY_SKU", "FACILITY_TOTAL", "GLOBAL_SKU", "GLOBAL_TOTAL"}.issubset(scope_kinds)
                else "FAIL",
                "evidence": {"scope_kinds": scope_kinds, "impact_digest": impact_value.get("impact_digest")},
            },
            {
                "check": "history_evidence_classification",
                "status": "PASS" if not history.get("blockers") else "FAIL",
                "evidence": {"classification_counts": history.get("classification_counts"), "history_digest": history.get("digest")},
            },
            {
                "check": "private_plan_and_shared_lock",
                "status": "PASS" if private_plan_mode == 0o600 and bool(lock_receipt) else "FAIL",
                "evidence": {"private_mode": oct(private_plan_mode), "lock": lock_receipt},
            },
            {
                "check": "material_cas_and_one_submit_boundary",
                "status": "PASS"
                if locked_plan_digest == plan["manifest_digest"]
                and plan["safety"]["one_submit"] is True
                and plan["safety"]["one_insert_max"] == 1
                else "FAIL",
                "evidence": {"locked_manifest_digest": locked_plan_digest, "submit_count": 0},
            },
            {
                "check": "query_only_non_target_wb_readback",
                "status": "PASS"
                if recovery_value.get("safety", {}).get("wb_writes") == 0
                and recovery_value.get("safety", {}).get("mapping_writes") == 0
                else "FAIL",
                "evidence": {"baselines": recovery_value.get("baselines"), "production_write_count": 0},
            },
        ]
        matrix_pass = all(item["status"] == "PASS" for item in matrix)
        return {
            "contract": "fbs_lifecycle_consolidated_rehearsal/v2",
            "mode": "query_only_no_submit",
            "mapping_manifest": plan,
            "impact_manifest": impact,
            "recovery_manifest": recovery,
            "source_database_query_only": True,
            "durable_plan_created": False,
            "mapping_insert_count": 0,
            "recovery_write_count": 0,
            "history_write_count": 0,
            "matrix": matrix,
            "matrix_status": "PASS" if matrix_pass else "FAIL",
            "accepted": bool(
                plan["apply_allowed"]
                and isinstance(impact, Mapping)
                and not impact["blockers"]
                and isinstance(recovery, Mapping)
                and recovery["apply_allowed"]
                and not recovery["blockers"]
                and matrix_pass
            ),
            "blockers": sorted(
                set(plan["blockers"])
                | set((impact or {}).get("blockers") or [])
                | set((recovery or {}).get("blockers") or [])
            ),
            "rehearsal_digest": manifest_digest(
                {
                    "mapping_manifest_digest": plan["manifest_digest"],
                    "impact_digest": (impact or {}).get("impact_digest"),
                    "recovery_digest": (recovery or {}).get("recovery_digest"),
                    "production_mutation_submit_count": 0,
                }
            ),
        }

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        operation_id: str,
        approval_reference: str,
        actor: str,
        evidence_dir: Path,
    ) -> dict[str, Any]:
        try:
            reviewed = parse_mapping_manifest(reviewed_plan)
        except FbsManifestError as exc:
            raise Wbc0027MappingError(exc.code, str(exc), details=exc.details) from exc
        expected = recovery_module._require_digest(fingerprint, "fingerprint")
        external_digest = str(self.incident_passport["evidence"]["external_identity_digest"])
        if reviewed.get("manifest_digest") != expected:
            raise Wbc0027MappingError(
                "reviewed_fingerprint_mismatch", "Reviewed mapping plan differs"
            )
        if reviewed.get("apply_allowed") is not True or reviewed.get("blockers"):
            raise Wbc0027MappingError(
                "reviewed_plan_blocked", "Blocked mapping plan cannot apply"
            )
        if dict(reviewed.get("evidence") or {}).get("external_identity_digest") != external_digest:
            raise Wbc0027MappingError(
                "external_identity_digest_drift", "Accepted diagnosis digest changed"
            )
        approval = str(approval_reference or "").strip()
        operation = str(operation_id or "").strip()
        operator = str(actor or "").strip()
        if (
            not approval
            or not operator
            or re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", operation) is None
        ):
            raise Wbc0027MappingError(
                "gate_identity_required",
                "operation_id, approval_reference and actor are required",
            )
        operation_proof = _operation_proof(
            operation_id=operation,
            approval_reference=approval,
            actor=operator,
        )
        existing = self.readback(
            operation_id=operation,
            approval_reference=approval,
        )
        if existing.get("status") == "completed":
            return {**existing, "idempotent": True, "repeat_submit_performed": False}
        if existing.get("status") == "ambiguous_foreign_operation":
            raise Wbc0027MappingError(
                "existing_mapping_operation_ambiguous",
                "An identical mapping exists without exact same-operation proof",
            )

        fresh = self.build_plan()
        if fresh.get("manifest_digest") != expected:
            raise Wbc0027MappingError(
                "mapping_material_cas_drift", "Mapping material changed before lock"
            )
        evidence_root = recovery_module._admit_private_evidence_root(
            runtime_dir=self.recovery.runtime.runtime_dir,
            evidence_dir=Path(evidence_dir),
            predicted_output_bytes=2 * 1024 * 1024,
        )
        now = str(self.timestamp_factory())
        recovery_module._require_utc(now)
        operation_suffix = hashlib.sha256(operation.encode("utf-8")).hexdigest()[:16]
        before_path = evidence_root / (
            "wbc0027-fbs-mapping-" + operation_suffix + "-" + expected.removeprefix("sha256:")[:20] + ".before.json"
        )
        backup_path = evidence_root / (
            "wbc0027-fbs-mapping-" + operation_suffix + "-" + expected.removeprefix("sha256:")[:20] + ".backup.json"
        )
        journal_path = evidence_root / (
            "wbc0027-fbs-mapping-" + operation_suffix + "-" + expected.removeprefix("sha256:")[:20] + ".journal.json"
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
                admission_source = _mapping_admission_snapshot(
                    conn,
                    recovery=self.recovery,
                )
                identity_snapshot = _identity_snapshot(
                    conn,
                    tuple_value=self.incident_passport["tuple"],
                )
                locked_material = _locked_material(
                    external_identity_digest=external_digest,
                    target_id=self.target_id,
                    deployed_sha=self.deployed_sha,
                    source_runtime_sha=str(
                        self.incident_passport["target"]["source_runtime_sha"]
                    ),
                    storage=storage,
                    admission_source=admission_source,
                    identity_snapshot=identity_snapshot,
                    mapping=mapping,
                )
                if manifest_digest(locked_material) != str(
                    dict(reviewed["material_cas"])["digest"]
                ):
                    raise Wbc0027MappingError(
                        "mapping_material_cas_drift",
                        "Mapping material changed inside writer lock",
                        details={
                            "expected": str(dict(reviewed["material_cas"])["digest"]),
                            "actual": manifest_digest(locked_material),
                            "locked": locked_material,
                            "reviewed": dict(reviewed["material_cas"]),
                        },
                    )
                before_image = {
                    "contract_name": CONTRACT_NAME,
                    "operation_id": operation,
                    "authorization_reference_digest": manifest_digest(approval),
                    "fingerprint": expected,
                    "target_id": self.target_id,
                    "deployed_sha": self.deployed_sha,
                    "external_identity_digest": external_digest,
                    "tuple_digest": self.incident_passport["tuple"]["tuple_digest"],
                    "storage": storage,
                    "cutover": reviewed["cutover"],
                    "identity_snapshot": identity_snapshot,
                    "mapping": mapping,
                    "recovery": "single transaction rollback before commit",
                    "created_at": now,
                }
                _write_private_exclusive(before_path, before_image)
                _write_private_exclusive(
                    backup_path,
                    {
                        "contract": "fbs_identity_mapping_private_backup/v1",
                        "operation_id": operation,
                        "authorization_reference_digest": manifest_digest(approval),
                        "fingerprint": expected,
                        "storage": storage,
                        "cutover": reviewed["cutover"],
                        "identity_snapshot": identity_snapshot,
                        "mapping_before": None,
                    },
                )
                _write_private_exclusive(
                    journal_path,
                    {
                        "contract": "fbs_identity_mapping_operation_journal/v1",
                        "operation_id": operation,
                        "authorization_reference_digest": manifest_digest(approval),
                        "fingerprint": expected,
                        "deployed_sha": self.deployed_sha,
                        "storage": storage,
                        "operation_proof": operation_proof,
                        "before_image_sha256": recovery_module._sha256_file(before_path),
                        "backup_sha256": recovery_module._sha256_file(backup_path),
                        "submit_state": "prepared",
                    },
                )
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
                        operation_proof,
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
        readback = self.readback(
            operation_id=operation,
            approval_reference=approval,
        )
        if readback.get("status") != "completed":
            raise Wbc0027MappingError(
                "mapping_readback_not_reconciled", "Query-only mapping readback failed"
            )
        return {
            **readback,
            "fingerprint": expected,
            "operation_id": operation,
            "approval_reference": approval,
            "applied_by": operator,
            "before_image_path": str(before_path),
            "before_image_sha256": recovery_module._sha256_file(before_path),
            "backup_path": str(backup_path),
            "backup_sha256": recovery_module._sha256_file(backup_path),
            "operation_journal_path": str(journal_path),
            "operation_journal_sha256": recovery_module._sha256_file(journal_path),
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

    def readback(
        self,
        *,
        operation_id: str = "",
        approval_reference: str = "",
    ) -> dict[str, Any]:
        mapping = self.mapping
        with closing(recovery_module._open_query_only(self.recovery.runtime.db_path)) as conn:
            exact_rows = conn.execute(
                f"""SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,
                           source_sku,target_nm_id,mapping_digest,active,
                           created_at,created_by
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
        proof = (
            _operation_proof(
                operation_id=str(operation_id),
                approval_reference=str(approval_reference),
                actor="production-apply-runner",
            )
            if operation_id and approval_reference
            else ""
        )
        exact_operation = bool(exact and proof and str(exact_rows[0][9]) == proof)
        status = (
            "completed"
            if exact_operation
            else "ambiguous_foreign_operation"
            if exact
            else "not_applied"
        )
        return {
            "contract_name": CONTRACT_NAME,
            "status": status,
            "target_id": self.target_id,
            "deployed_sha": self.deployed_sha,
            "mapping": expected if exact else None,
            "operation_id": str(operation_id or ""),
            "operation_proof_exact": exact_operation if proof else None,
            "created_at": str(exact_rows[0][8]) if exact else "",
            "exact_mapping_row_count": len(exact_rows),
            "query_only": True,
            "mapping_insert_count": 0,
            "recovery_write_count": 0,
            "history_write_count": 0,
            "wb_write_count": 0,
            "readback_digest": manifest_digest(
                {
                    "contract": "fbs_identity_mapping_readback/v2",
                    "target_id": self.target_id,
                    "runtime_sha": self.deployed_sha,
                    "mapping": expected if exact else None,
                    "operation_id": str(operation_id or ""),
                    "operation_proof_exact": exact_operation if proof else None,
                    "exact_mapping_row_count": len(exact_rows),
                    "query_only": True,
                }
            ),
        }

    def _current_snapshot(
        self, *, storage: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        with closing(recovery_module._open_query_only(self.recovery.runtime.db_path)) as conn:
            conn.execute("BEGIN")
            try:
                admission_source = _mapping_admission_snapshot(
                    conn,
                    recovery=self.recovery,
                )
                identity = _identity_snapshot(
                    conn,
                    tuple_value=self.incident_passport["tuple"],
                )
                if self.recovery._storage_identity(conn=conn) != dict(storage):
                    raise Wbc0027MappingError(
                        "storage_generation_drift", "Storage changed in query-only snapshot"
                    )
            finally:
                if conn.in_transaction:
                    conn.rollback()
        return admission_source, identity

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
    tuple_value: Mapping[str, Any],
) -> dict[str, Any]:
    identity = dict(tuple_value)
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
    return {
        "tuple_digest": str(identity["tuple_digest"]),
        "active_owner_count": len(owners),
        "owner_digest": recovery_module._fingerprint(owners),
        "active_mapping_count": len(active_mappings),
        "all_mapping_count": len(all_mappings),
        "active_mapping_digest": recovery_module._fingerprint(active_mappings),
        "all_mapping_digest": recovery_module._fingerprint(all_mappings),
    }


def _mapping_admission_snapshot(
    conn: sqlite3.Connection,
    *,
    recovery: recovery_module.Wbc0027FbsLifecycleQualityRecovery,
) -> dict[str, Any]:
    """Return only stable topology needed to admit one identity mapping.

    Order/status/group/date/WAC/cardinality evidence belongs to the later impact
    and recovery phases.  Mapping CAS intentionally excludes it.
    """

    active = recovery_module._active_manifest(conn)
    cutover_id = str(active["cutover_id"])
    rows = conn.execute(
        f"""SELECT generation.generation_id,generation.manifest_fingerprint
            FROM {recovery_module.FORWARD_GENERATIONS_TABLE} AS generation
            JOIN {recovery_module.FORWARD_STATE_TABLE} AS state USING(generation_id)
            WHERE generation.cutover_id=?""",
        (cutover_id,),
    ).fetchall()
    if len(rows) != 1:
        raise Wbc0027MappingError(
            "forward_generation_missing_or_ambiguous",
            "Exactly one active forward generation is required",
        )
    return {
        "cutover_id": cutover_id,
        "cutover_manifest_digest": recovery_module._fingerprint(active["manifest"]),
        "generation_id": str(rows[0][0]),
        "generation_manifest_fingerprint": str(rows[0][1]),
    }


def _identity_admission_material(
    identity_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Stable owner and mapping-absence evidence without cardinality fields."""

    return {
        "tuple_digest": str(identity_snapshot["tuple_digest"]),
        "owner_digest": str(identity_snapshot["owner_digest"]),
        "active_mapping_digest": str(identity_snapshot["active_mapping_digest"]),
        "all_mapping_digest": str(identity_snapshot["all_mapping_digest"]),
    }


def _binding_blockers(
    *,
    passport: Mapping[str, Any],
    storage: Mapping[str, Any],
    admission_source: Mapping[str, Any],
    identity_snapshot: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    expected_storage = dict(passport["storage"])
    expected_cutover = dict(passport["cutover"])
    expectation = dict(passport["mapping_expectation"])
    checks = {
        "storage_manifest_drift": storage.get("manifest_sha256")
        == expected_storage["manifest_sha256"],
        "storage_generation_drift": storage.get("operational_generation_id")
        == expected_storage["operational_generation_id"],
        "operational_schema_revision_drift": storage.get(
            "operational_schema_revision"
        )
        == expected_storage["operational_schema_revision"],
        "sqlite_schema_version_drift": storage.get("sqlite_schema_version")
        == expected_storage["sqlite_schema_version"],
        "cutover_drift": admission_source.get("cutover_id") == expected_cutover["cutover_id"],
        "forward_generation_drift": admission_source.get("generation_id")
        == expected_cutover["forward_generation_id"],
        "tuple_digest_drift": identity_snapshot.get("tuple_digest")
        == passport["tuple"]["tuple_digest"],
        "owner_count_drift": identity_snapshot.get("active_owner_count")
        == expectation["owner_count"],
        "active_mapping_count_drift": identity_snapshot.get("active_mapping_count")
        == expectation["active_mapping_count"],
        "duplicate_mapping_present": identity_snapshot.get("all_mapping_count")
        == expectation["all_mapping_count"],
    }
    for code, passed in checks.items():
        if not passed:
            blockers.append(code)
    return blockers


def _locked_material(
    *,
    external_identity_digest: str,
    target_id: str,
    deployed_sha: str,
    source_runtime_sha: str,
    storage: Mapping[str, Any],
    admission_source: Mapping[str, Any],
    identity_snapshot: Mapping[str, Any],
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    storage_binding = {
        key: storage[key]
        for key in (
            "manifest_sha256",
            "operational_generation_id",
            "operational_schema_revision",
            "sqlite_schema_version",
        )
    }
    evidence = {
        "external_identity_digest": external_identity_digest,
        "owner_digest": str(identity_snapshot["owner_digest"]),
        "warehouse_evidence_digest": manifest_digest(
            {
                "contract": "fbs_mapping_warehouse_evidence_deferred/v1",
                "tuple_digest": str(mapping["mapping_digest"]),
                "admission": "recovery_phase_only",
            }
        ),
        "facility_admission_digest": manifest_digest(
            {
                "contract": "fbs_mapping_facility_admission_deferred/v1",
                "target_nm_id": int(mapping["target_nm_id"]),
                "admission": "recovery_phase_only",
            }
        ),
    }
    return {
        "tuple_digest": str(mapping["mapping_digest"]),
        "mapping_digest": str(mapping["mapping_digest"]),
        "target_digest": manifest_digest(
            {
                "target_id": str(target_id),
                "runtime_sha": str(deployed_sha),
                "source_runtime_sha": str(source_runtime_sha),
            }
        ),
        "storage_digest": manifest_digest(storage_binding),
        "cutover_digest": manifest_digest(
            {
                "cutover_id": admission_source["cutover_id"],
                "cutover_manifest_digest": admission_source["cutover_manifest_digest"],
                "forward_generation_id": admission_source["generation_id"],
                "forward_generation_manifest_digest": admission_source[
                    "generation_manifest_fingerprint"
                ],
            }
        ),
        "identity_digest": manifest_digest(
            _identity_admission_material(identity_snapshot)
        ),
        "evidence_digest": manifest_digest(evidence),
    }


def _operation_proof(
    *,
    operation_id: str,
    approval_reference: str,
    actor: str,
) -> str:
    del actor  # actor remains receipt evidence; idempotency binds operation+authorization.
    material = {
        "contract": "fbs_identity_mapping_operation_proof/v1",
        "operation_id": str(operation_id),
        "authorization_reference_digest": manifest_digest(str(approval_reference)),
    }
    return "production-apply-runner:" + manifest_digest(material)


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
    directory = os.open(output.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_plan(path: str) -> dict[str, Any]:
    try:
        return parse_mapping_manifest(read_manifest_json(Path(path)))
    except FbsManifestError as exc:
        raise Wbc0027MappingError(exc.code, str(exc), details=exc.details) from exc


def run(args: argparse.Namespace) -> int:
    runner = Wbc0027ExactFbsSkuMappingExtension(
        runtime_dir=Path(args.runtime_dir),
        deployed_sha=str(args.deployed_sha),
        incident_passport=read_manifest_json(Path(args.passport_file)),
        target_id=str(args.target_id),
        scratch_dir=(Path(args.scratch_dir) if args.scratch_dir else None),
    )
    if args.command == "mapping-apply":
        payload = runner.apply(
            _read_plan(args.plan_file),
            fingerprint=str(args.fingerprint),
            operation_id=str(args.operation_id),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            evidence_dir=Path(args.evidence_dir),
        )
    elif args.command == "mapping-readback":
        payload = runner.readback(
            operation_id=str(args.operation_id or ""),
            approval_reference=str(args.approval_reference or ""),
        )
    elif args.command == "mapping-rehearsal":
        payload = runner.rehearse()
    else:
        payload = runner.build_plan()
    if args.output:
        output_path = Path(args.output).expanduser().resolve(strict=False)
        recovery_module._admit_private_evidence_root(
            runtime_dir=runner.recovery.runtime.runtime_dir,
            evidence_dir=output_path.parent,
            predicted_output_bytes=max(
                4096,
                len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 1,
            ),
        )
        if args.command in {"mapping-dry-run", "mapping-rehearsal"}:
            _write_private_exclusive(output_path, payload)
        else:
            recovery_module._write_private(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    blocked = bool(payload.get("blockers")) or payload.get("status") in {
        "blocked",
        "error",
    }
    if args.command == "mapping-rehearsal" and payload.get("accepted") is not True:
        blocked = True
    if args.command == "mapping-dry-run" and payload.get("apply_allowed") is not True:
        blocked = True
    return 2 if blocked else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--passport-file", required=True)
    parser.add_argument("--target-id", default=CANONICAL_TARGET_ID)
    parser.add_argument("--scratch-dir", default="")
    parser.add_argument("--output", default="")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("mapping-dry-run")
    sub.add_parser("mapping-rehearsal")
    apply = sub.add_parser("mapping-apply")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--evidence-dir", required=True)
    readback = sub.add_parser("mapping-readback")
    readback.add_argument("--operation-id", default="")
    readback.add_argument("--approval-reference", default="")
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.command is None:
            args.command = "mapping-dry-run"
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
