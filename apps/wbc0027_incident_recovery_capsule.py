#!/usr/bin/env python3
"""One-shot WBC0027 incident recovery capsule.

The capsule is query-only by default.  Qualification produces one immutable
combined manifest plus a closed receipt.  Apply is available only for that
exact manifest and one separately supplied human authorization.  The single
business transaction contains the mapping insert and the reviewed lifecycle,
balance/capital, functional projection and same-date history writes.

Incident facilities, SKU values, status identities, counts and dates are read
from the incident passport and current canonical store and are persisted only
inside the generated capsule manifest.  They are deliberately not constants in
this module.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_fbs_lifecycle_quality_recovery as recovery_module  # noqa: E402
from apps import wbc0027_fbs_mapping_extension as mapping_module  # noqa: E402
from packages.application.fbs_lifecycle_manifests import (  # noqa: E402
    FbsManifestError,
    attach_digest,
    canonical_bytes,
    digest as manifest_digest,
    parse_impact_manifest,
    parse_mapping_manifest,
    parse_recovery_manifest,
    read_json,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    QUALITY_RECOVERY_HISTORY_TABLE,
    QUALITY_RECOVERY_RUNS_TABLE,
    QUALITY_RECOVERY_TARGETS_TABLE,
    recover_pinned_fbs_lifecycle,
)
from packages.application import ff_pool_fbs_forward_recovery as forward_module  # noqa: E402
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    APPLIES_TABLE,
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.wb_fbs_orders import IDENTITY_MAPPINGS_TABLE  # noqa: E402


MANIFEST_CONTRACT = "wbc0027_incident_recovery_capsule_manifest/v1"
QUALIFICATION_CONTRACT = "wbc0027_incident_recovery_capsule_qualification/v1"
READBACK_CONTRACT = "wbc0027_incident_recovery_capsule_readback/v1"
BACKUP_CONTRACT = "wbc0027_incident_recovery_capsule_backup_evidence/v1"
RELEASE_BINDING_CONTRACT = "wbc0027_incident_recovery_capsule_release_binding/v1"
CANONICAL_TARGET_ID = "wb_core_eu_hosted_runtime_active"
SAFE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
TABLE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class CapsuleError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class Wbc0027IncidentRecoveryCapsule:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        incident_passport: Mapping[str, Any],
        release_binding: Mapping[str, Any],
        timestamp_factory: Any | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if SHA_RE.fullmatch(self.deployed_sha) is None:
            raise CapsuleError("invalid_deployed_sha", "deployed_sha must be exact 40-hex")
        self.release_binding = _parse_release_binding(
            release_binding,
            deployed_sha=self.deployed_sha,
        )
        self.timestamp_factory = timestamp_factory or _utc_now
        self.mapping = mapping_module.Wbc0027ExactFbsSkuMappingExtension(
            runtime_dir=Path(runtime_dir),
            deployed_sha=self.deployed_sha,
            incident_passport=incident_passport,
            target_id=CANONICAL_TARGET_ID,
            timestamp_factory=self.timestamp_factory,
            scratch_dir=scratch_dir,
        )
        self.recovery = self.mapping.recovery

    def qualification(self, *, operation_id: str, evidence_dir: Path) -> dict[str, Any]:
        operation = _require_id(operation_id, "operation_id")
        evidence_root = recovery_module._admit_private_evidence_root(
            runtime_dir=self.recovery.runtime.runtime_dir,
            evidence_dir=Path(evidence_dir),
            predicted_output_bytes=96 * 1024 * 1024,
        )
        first = self.mapping.rehearse()
        second = self.mapping.rehearse()
        _require_accepted_rehearsal(first)
        _require_accepted_rehearsal(second)
        witness_keys = (
            ("mapping_manifest", "manifest_digest"),
            ("impact_manifest", "impact_digest"),
            ("recovery_manifest", "recovery_digest"),
        )
        first_witness = {
            digest_key: str(dict(first[manifest_key])[digest_key])
            for manifest_key, digest_key in witness_keys
        }
        second_witness = {
            digest_key: str(dict(second[manifest_key])[digest_key])
            for manifest_key, digest_key in witness_keys
        }
        if first_witness != second_witness:
            raise CapsuleError(
                "material_witness_drift",
                "Two query-only witnesses do not describe the same material state",
                details={"first": first_witness, "second": second_witness},
            )
        mapping_plan = parse_mapping_manifest(dict(first["mapping_manifest"]))
        impact_plan = parse_impact_manifest(dict(first["impact_manifest"]))
        recovery_plan = parse_recovery_manifest(dict(first["recovery_manifest"]))
        expected_writes = self._simulate_expected_writes(
            mapping_plan=mapping_plan,
            recovery_plan=recovery_plan,
            operation_id=operation,
        )
        history = dict(recovery_plan["history"])
        predicted = dict(recovery_plan["predicted_effects"])
        manifest_material: dict[str, Any] = {
            "contract": MANIFEST_CONTRACT,
            "operation_id": operation,
            "capsule_release": self.release_binding,
            "target": {
                "target_id": CANONICAL_TARGET_ID,
                "runtime_sha": self.deployed_sha,
            },
            "storage": dict(dict(recovery_plan["boundary"])["storage"]),
            "boundary": dict(recovery_plan["boundary"]),
            "missing_identity": {
                "tuple": dict(mapping_plan["tuple"]),
                "owner_evidence_digest": str(
                    dict(mapping_plan["evidence"])["owner_digest"]
                ),
                "mapping_absence": {
                    "active_mapping_count": int(
                        dict(mapping_plan["expectation"])["active_mapping_count"]
                    ),
                    "all_mapping_count": int(
                        dict(mapping_plan["expectation"])["all_mapping_count"]
                    ),
                },
                "proposed_mapping": {
                    **mapping_module._mapping_row_from_plan(self.mapping.mapping),
                    "created_by": "exact_capsule_operation_proof",
                },
                "material_cas": dict(mapping_plan["material_cas"]),
            },
            "scope": dict(recovery_plan["scope"]),
            "impact": impact_plan,
            "predicted_effects": predicted,
            "history": {
                **history,
                "exact_append_supersession_count": len(history.get("captures") or []),
                "recoverable_exact": [
                    item
                    for item in history.get("cell_evidence") or []
                    if item.get("classification") == "recoverable_exact"
                ],
                "remain_missing": [
                    item
                    for item in history.get("cell_evidence") or []
                    if item.get("classification")
                    == "remain_missing_no_same_date_evidence"
                ],
            },
            "baselines": {
                **dict(recovery_plan["baselines"]),
                "wb_history_semantic_digest": _planned_wb_history_digest(history),
                "wb_physical_prechange_digest": str(
                    dict(recovery_plan["baselines"])["wb_digest"]
                ),
            },
            "expected_writes": expected_writes,
            "backup_recovery": {
                "qualification_before_image": "private_mode_0600_exclusive_fsync",
                "apply_before_image": "private_mode_0600_exclusive_fsync",
                "sqlite_atomicity": "one_BEGIN_IMMEDIATE_one_COMMIT",
                "rollback": "rollback_same_transaction_before_commit",
                "ambiguous_transport": "same_operation_query_only_readback_only",
                "root_storage_owner": "production_apply_evidence",
            },
            "apply_contract": {
                "default_off": True,
                "single_human_gate": True,
                "one_operation_identity": True,
                "submit_count_max": 1,
                "mapping_insert_count": 1,
                "sqlite_transaction_count": 1,
                "writer_lock": "warehouse_functional_write_lock",
                "material_cas": "mapping_plus_complete_recovery_manifest",
                "unexpected_table_write": "deny_and_rollback",
                "wb_write_count": 0,
                "current_retrocopy": False,
            },
            "post_apply_readback_contract": {
                "mapping": "exact_operation_proof",
                "lifecycle_status_coverage": "all_manifest_status_sequences_resolved",
                "surfaces": [
                    "FBS_FACILITY_SKU",
                    "FBS_FACILITY_TOTAL",
                    "FBS_GLOBAL_SKU",
                    "FBS_GLOBAL_TOTAL",
                    "COMBINED_TOTAL",
                    "CAPITAL",
                    "WAC",
                    "FUNCTIONAL_ECONOMICS",
                    "SAME_DATE_HISTORY",
                    "REMAIN_MISSING",
                ],
                "non_target_unchanged": True,
                "wb_unchanged": True,
            },
        }
        manifest = attach_digest(manifest_material, "manifest_digest")
        _parse_capsule_manifest(manifest, deployed_sha=self.deployed_sha)
        suffix = manifest["manifest_digest"].removeprefix("sha256:")[:24]
        manifest_path = evidence_root / f"wbc0027-incident-capsule-{suffix}.manifest.json"
        backup_path = evidence_root / f"wbc0027-incident-capsule-{suffix}.backup.json"
        receipt_path = evidence_root / f"wbc0027-incident-capsule-{suffix}.qualification.json"
        backup = {
            "contract": BACKUP_CONTRACT,
            "operation_id": operation,
            "manifest_digest": manifest["manifest_digest"],
            "storage": manifest["storage"],
            "boundary": manifest["boundary"],
            "mapping_before": None,
            "target_rows": dict(manifest["scope"])["target_rows"],
            "history_bases": [
                {
                    "business_date": item["business_date"],
                    "base_capture_id": item["base_capture_id"],
                    "base_source_digest": item["base_source_digest"],
                    "supersedes_finalization_digest": item[
                        "supersedes_finalization_digest"
                    ],
                }
                for item in dict(manifest["history"])["captures"]
            ],
            "non_target_digest": dict(manifest["baselines"])["non_target_digest"],
            "wb_digest": dict(manifest["baselines"])["wb_digest"],
            "recovery": "restore mapping absence and reviewed before-images only",
        }
        mapping_module._write_private_exclusive(manifest_path, manifest)
        mapping_module._write_private_exclusive(backup_path, backup)
        receipt_material: dict[str, Any] = {
            "contract": QUALIFICATION_CONTRACT,
            "state": "HUMAN_REQUIRED",
            "reason": "single_incident_capsule_apply_gate",
            "operation_id": operation,
            "capsule_release": self.release_binding,
            "manifest": {
                "path": str(manifest_path),
                "sha256": recovery_module._sha256_file(manifest_path),
                "digest": manifest["manifest_digest"],
                "size_bytes": manifest_path.stat().st_size,
                "mode": oct(manifest_path.stat().st_mode & 0o777),
            },
            "backup_evidence": {
                "path": str(backup_path),
                "sha256": recovery_module._sha256_file(backup_path),
                "size_bytes": backup_path.stat().st_size,
                "mode": oct(backup_path.stat().st_mode & 0o777),
            },
            "witnesses": [
                {"ordinal": 1, **first_witness, "rehearsal_digest": first["rehearsal_digest"]},
                {"ordinal": 2, **second_witness, "rehearsal_digest": second["rehearsal_digest"]},
            ],
            "lock_witnesses": [
                _matrix_evidence(first, "private_plan_and_shared_lock"),
                _matrix_evidence(second, "private_plan_and_shared_lock"),
            ],
            "root_storage_admission": "passed_by_private_evidence_root",
            "expected_writes": expected_writes,
            "production_mutation_submit_count": 0,
            "mapping_write_count": 0,
            "lifecycle_write_count": 0,
            "history_write_count": 0,
            "wb_write_count": 0,
            "github_apply_marker_required": False,
            "next_state": "HUMAN_REQUIRED",
        }
        receipt = attach_digest(receipt_material, "qualification_digest")
        mapping_module._write_private_exclusive(receipt_path, receipt)
        return {
            "state": "HUMAN_REQUIRED",
            "manifest_path": str(manifest_path),
            "manifest_sha256": recovery_module._sha256_file(manifest_path),
            "manifest_digest": manifest["manifest_digest"],
            "backup_path": str(backup_path),
            "backup_sha256": recovery_module._sha256_file(backup_path),
            "qualification_path": str(receipt_path),
            "qualification_sha256": recovery_module._sha256_file(receipt_path),
            "qualification_digest": receipt["qualification_digest"],
            "production_mutation_submit_count": 0,
        }

    def apply(
        self,
        *,
        manifest: Mapping[str, Any],
        qualification: Mapping[str, Any],
        operation_id: str,
        approval_reference: str,
        actor: str,
        evidence_dir: Path,
    ) -> dict[str, Any]:
        reviewed = _parse_capsule_manifest(manifest, deployed_sha=self.deployed_sha)
        qualified = _parse_qualification(qualification, manifest=reviewed)
        operation = _require_id(operation_id, "operation_id")
        if operation != reviewed["operation_id"] or operation != qualified["operation_id"]:
            raise CapsuleError("operation_identity_drift", "Apply operation is not qualified")
        approval = str(approval_reference or "").strip()
        operator = _require_id(actor, "actor")
        if not approval or len(approval) > 2000:
            raise CapsuleError("human_gate_required", "Exact human authorization is required")
        expected_body = _authorization_body(
            release=self.release_binding,
            operation_id=operation,
            manifest_digest=str(reviewed["manifest_digest"]),
            qualification_digest=str(qualified["qualification_digest"]),
        )
        if approval != expected_body:
            raise CapsuleError(
                "human_gate_binding_mismatch",
                "Authorization body does not exactly bind release, manifest and qualification",
                details={"expected": expected_body},
            )
        existing = self.readback(
            manifest=reviewed,
            operation_id=operation,
            approval_reference=approval,
        )
        if existing.get("state") == "done":
            return {**existing, "apply_count": 0, "already_terminal": True}
        evidence_root = recovery_module._admit_private_evidence_root(
            runtime_dir=self.recovery.runtime.runtime_dir,
            evidence_dir=Path(evidence_dir),
            predicted_output_bytes=96 * 1024 * 1024,
        )
        suffix = str(reviewed["manifest_digest"]).removeprefix("sha256:")[:24]
        before_path = evidence_root / f"wbc0027-incident-capsule-{suffix}.apply-before.json"
        journal_path = evidence_root / f"wbc0027-incident-capsule-{suffix}.apply-journal.json"
        mapping_module._write_private_exclusive(
            before_path,
            {
                "contract": BACKUP_CONTRACT,
                "operation_id": operation,
                "manifest_digest": reviewed["manifest_digest"],
                "authorization_digest": manifest_digest(approval),
                "mapping_before": None,
                "target_rows": dict(reviewed["scope"])["target_rows"],
                "history_bases": [
                    {
                        key: item[key]
                        for key in (
                            "business_date",
                            "base_capture_id",
                            "base_source_digest",
                            "supersedes_finalization_digest",
                        )
                    }
                    for item in dict(reviewed["history"])["captures"]
                ],
                "baselines": reviewed["baselines"],
            },
        )
        mapping_module._write_private_exclusive(
            journal_path,
            {
                "contract": "wbc0027_incident_recovery_capsule_operation_journal/v1",
                "operation_id": operation,
                "manifest_digest": reviewed["manifest_digest"],
                "qualification_digest": qualified["qualification_digest"],
                "authorization_digest": manifest_digest(approval),
                "before_image_sha256": recovery_module._sha256_file(before_path),
                "submit_state": "prepared",
            },
        )
        now = str(self.timestamp_factory())
        recovery_module._require_utc(now)
        with warehouse_functional_write_lock(
            self.recovery.runtime.runtime_dir,
            timeout_seconds=300,
        ):
            conn = sqlite3.connect(self.recovery.runtime.db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                _install_write_audit(conn)
                conn.commit()
                allowed = {
                    str(item["table"])
                    for item in dict(reviewed["expected_writes"])["tables"]
                }
                conn.set_authorizer(_scoped_authorizer(allowed))
                conn.execute("BEGIN IMMEDIATE")
                result = self._execute_transaction(
                    conn,
                    mapping_plan=dict(reviewed["missing_identity"]),
                    recovery_plan=reviewed,
                    operation_id=operation,
                    approval_reference=approval,
                    actor=operator,
                    applied_at=now,
                    simulation=False,
                )
                actual_writes = _read_write_audit(conn)
                expected_writes = dict(reviewed["expected_writes"])
                expected_write_audit = {
                    key: expected_writes[key]
                    for key in ("tables", "total_rows", "digest")
                }
                if actual_writes != expected_write_audit:
                    raise CapsuleError(
                        "table_write_set_drift",
                        "Actual table/row writes differ from the qualified manifest",
                        details={"expected": expected_writes, "actual": actual_writes},
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
            manifest=reviewed,
            operation_id=operation,
            approval_reference=approval,
        )
        if readback.get("state") != "done":
            raise CapsuleError(
                "post_apply_readback_failed",
                "The one submit is not proven by query-only readback",
                details=readback,
            )
        return {
            "contract": "wbc0027_incident_recovery_capsule_apply_receipt/v1",
            "state": "done",
            "operation_id": operation,
            "manifest_digest": reviewed["manifest_digest"],
            "qualification_digest": qualified["qualification_digest"],
            "apply_count": 1,
            "sqlite_transaction_count": 1,
            "before_image_path": str(before_path),
            "before_image_sha256": recovery_module._sha256_file(before_path),
            "journal_path": str(journal_path),
            "journal_sha256": recovery_module._sha256_file(journal_path),
            "transaction": result,
            "readback": readback,
        }

    def readback(
        self,
        *,
        manifest: Mapping[str, Any],
        operation_id: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        reviewed = _parse_capsule_manifest(manifest, deployed_sha=self.deployed_sha)
        operation = _require_id(operation_id, "operation_id")
        approval = str(approval_reference or "").strip()
        mapping_expected = dict(dict(reviewed["missing_identity"])["proposed_mapping"])
        recovery_fingerprint = str(dict(reviewed["expected_writes"])["recovery_fingerprint"])
        recovery_readback = self.recovery.readback(
            fingerprint=recovery_fingerprint,
            operation_id=operation,
            approval_reference=approval,
        )
        with closing(recovery_module._open_query_only(self.recovery.runtime.db_path)) as conn:
            mapping_rows = conn.execute(
                f"""SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,
                           source_sku,target_nm_id,mapping_digest,active,created_by
                    FROM {IDENTITY_MAPPINGS_TABLE}
                    WHERE source_nm_id=? AND source_chrt_id=?
                      AND source_barcode=? AND source_sku=?
                    ORDER BY mapping_id""",
                (
                    int(mapping_expected["source_nm_id"]),
                    int(mapping_expected["source_chrt_id"]),
                    str(mapping_expected["source_barcode"]),
                    str(mapping_expected["source_sku"]),
                ),
            ).fetchall()
            exact_mapping = len(mapping_rows) == 1 and {
                **mapping_module._mapping_row(mapping_rows[0][:8]),
                "created_by": str(mapping_rows[0][8]),
            } == {
                **{key: mapping_expected[key] for key in mapping_module._mapping_row_from_plan(mapping_expected)},
                "created_by": mapping_module._operation_proof(
                    operation_id=operation,
                    approval_reference=approval,
                    actor="capsule",
                ),
            }
            sequences = [int(value) for value in dict(reviewed["scope"])["target_sequences"]]
            resolved_count = 0
            for offset in range(0, len(sequences), 500):
                batch = sequences[offset : offset + 500]
                placeholders = ",".join("?" for _ in batch)
                resolved_count += int(
                    conn.execute(
                        f"""SELECT COUNT(*)
                            FROM {IDENTITY_PENDING_RESOLUTIONS_TABLE} resolution
                            JOIN sheet_vitrina_v1_ff_pool_fbs_identity_pending pending
                              ON pending.pending_id=resolution.pending_id
                            WHERE pending.source_status_observation_sequence
                              IN ({placeholders})""",
                        tuple(batch),
                    ).fetchone()[0]
                )
            source = {
                "cutover_id": dict(reviewed["boundary"])["cutover_id"],
                "resolved_scopes": dict(reviewed["scope"])["resolved_scopes"],
            }
            surfaces = recovery_module._dependent_surface_snapshot(conn, source=source)
            target_groups = {
                (str(item["facility_id"]), int(item["nm_id"]))
                for item in dict(reviewed["scope"])["groups"]
            }
            non_target = recovery_module._non_target_digest(conn, target_groups=target_groups)
            wb = _stored_wb_history_digest(
                conn,
                captures=dict(reviewed["history"])["captures"],
            )
            history_rows = []
            for item in dict(reviewed["history"])["captures"]:
                capture = conn.execute(
                    "SELECT capture_id,source_digest FROM sheet_vitrina_v1_inventory_history_captures WHERE capture_id=?",
                    (str(item["capture_id"]),),
                ).fetchone()
                finalization = conn.execute(
                    """SELECT finalization_id,finalization_digest,supersedes_finalization_digest
                       FROM sheet_vitrina_v1_inventory_history_finalizations
                       WHERE business_date=? AND capture_id=? AND finalization_identity=?""",
                    (
                        str(item["business_date"]),
                        str(item["capture_id"]),
                        str(item["finalization_identity"]),
                    ),
                ).fetchone()
                history_rows.append(
                    {
                        "business_date": str(item["business_date"]),
                        "capture_exact": bool(
                            capture is not None
                            and str(capture[0]) == str(item["capture_id"])
                            and str(capture[1]) == str(item["source_digest"])
                        ),
                        "finalization_exact": bool(
                            not bool(item["finalize"])
                            or finalization is not None
                            and str(finalization[2])
                            == str(item["supersedes_finalization_digest"])
                        ),
                    }
                )
            remain_missing_rows = _remain_missing_readback(
                conn,
                captures=dict(reviewed["history"])["captures"],
                remain_missing=dict(reviewed["history"])["remain_missing"],
            )
        predicted_after = list(
            dict(dict(reviewed["predicted_effects"])["dependent_surface_plan"])["after"]
        )
        checks = {
            "mapping_exact": exact_mapping,
            "lifecycle_status_coverage_exact": resolved_count == len(sequences),
            "recovery_receipt_exact": recovery_readback.get("status") == "completed",
            "dependent_surfaces_exact": surfaces == predicted_after,
            "same_date_history_exact": all(
                item["capture_exact"] and item["finalization_exact"] for item in history_rows
            ),
            "remain_missing_list_preserved": len(remain_missing_rows)
            == len(dict(reviewed["history"])["remain_missing"])
            and all(item["exact"] for item in remain_missing_rows),
            "non_target_unchanged": non_target
            == str(dict(reviewed["baselines"])["non_target_digest"]),
            "wb_unchanged": wb
            == str(dict(reviewed["baselines"])["wb_history_semantic_digest"]),
        }
        state = "done" if all(checks.values()) else "blocked"
        material = {
            "contract": READBACK_CONTRACT,
            "state": state,
            "operation_id": operation,
            "manifest_digest": reviewed["manifest_digest"],
            "query_only": True,
            "production_mutation_submit_count": 0,
            "checks": checks,
            "mapping": mapping_expected if exact_mapping else None,
            "status_sequence_count": len(sequences),
            "resolved_status_sequence_count": resolved_count,
            "dependent_surfaces": surfaces,
            "history": history_rows,
            "remain_missing": remain_missing_rows,
            "non_target_digest": non_target,
            "wb_digest": wb,
            "recovery": recovery_readback,
        }
        return attach_digest(material, "readback_digest")

    def _simulate_expected_writes(
        self,
        *,
        mapping_plan: Mapping[str, Any],
        recovery_plan: Mapping[str, Any],
        operation_id: str,
    ) -> dict[str, Any]:
        storage = dict(dict(recovery_plan["boundary"])["storage"])
        hypothetical = {
            **self.mapping.mapping,
            "tuple_digest": dict(mapping_plan["tuple"])["tuple_digest"],
            "external_identity_digest": dict(mapping_plan["evidence"])[
                "external_identity_digest"
            ],
        }
        with closing(recovery_module._open_query_only(self.recovery.runtime.db_path)) as source_conn:
            source_conn.execute("BEGIN")
            source = self.recovery._source_snapshot(
                source_conn,
                storage=storage,
                hypothetical_mapping=hypothetical,
            )
            root = recovery_module._prepare_private_scratch_root(self.recovery.scratch_dir)
            descriptor, name = tempfile.mkstemp(prefix="wbc0027-capsule-", suffix=".sqlite3", dir=root)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
            scratch_path = Path(name)
            scratch = sqlite3.connect(scratch_path, timeout=120.0)
            scratch.row_factory = sqlite3.Row
            try:
                scratch.execute("PRAGMA foreign_keys=OFF")
                recovery_module._build_preview_projection(
                    source_conn,
                    scratch,
                    source=source,
                    scratch_path=scratch_path,
                )
                _extend_scratch_with_history(
                    source_conn,
                    scratch,
                    recovery_plan=recovery_plan,
                )
                _install_write_audit(scratch)
                scratch.commit()
                scratch.execute("BEGIN IMMEDIATE")
                self._execute_transaction(
                    scratch,
                    mapping_plan={
                        "tuple": dict(mapping_plan["tuple"]),
                        "proposed_mapping": {
                            **mapping_module._mapping_row_from_plan(self.mapping.mapping),
                            "created_by": "exact_capsule_operation_proof",
                        },
                        "material_cas": dict(mapping_plan["material_cas"]),
                    },
                    recovery_plan={
                        **dict(recovery_plan),
                        "expected_writes": {"recovery_fingerprint": recovery_plan["recovery_digest"]},
                    },
                    operation_id=operation_id,
                    approval_reference=_simulation_authorization(
                        release=self.release_binding,
                        operation_id=operation_id,
                        recovery_digest=str(recovery_plan["recovery_digest"]),
                    ),
                    actor="qualification-simulation",
                    applied_at=str(self.timestamp_factory()),
                    simulation=True,
                )
                writes = _read_write_audit(scratch)
                scratch.rollback()
            finally:
                scratch.close()
                try:
                    scratch_path.unlink()
                except FileNotFoundError:
                    pass
                if source_conn.in_transaction:
                    source_conn.rollback()
        return {
            **writes,
            "recovery_fingerprint": str(recovery_plan["recovery_digest"]),
            "logical": {
                "mapping_insert_count": 1,
                "target_status_count": int(dict(recovery_plan["scope"])["target_count"]),
                "mapping_re_evidence_count": len(
                    dict(recovery_plan["scope"])["mapping_re_evidence"]
                ),
                "lifecycle": dict(dict(recovery_plan["predicted_effects"])["lifecycle_summary"]),
                "balance_delta_count": len(
                    dict(recovery_plan["predicted_effects"])["balance_deltas"]
                ),
                "history_capture_count": len(dict(recovery_plan["history"])["captures"]),
                "history_component_count": sum(
                    len(item["components"])
                    for item in dict(recovery_plan["history"])["captures"]
                ),
                "history_finalization_count": sum(
                    bool(item["finalize"])
                    for item in dict(recovery_plan["history"])["captures"]
                ),
                "wb_write_count": 0,
            },
        }

    def _execute_transaction(
        self,
        conn: sqlite3.Connection,
        *,
        mapping_plan: Mapping[str, Any],
        recovery_plan: Mapping[str, Any],
        operation_id: str,
        approval_reference: str,
        actor: str,
        applied_at: str,
        simulation: bool,
    ) -> dict[str, Any]:
        mapping_value = dict(mapping_plan["proposed_mapping"])
        operation_proof = mapping_module._operation_proof(
            operation_id=operation_id,
            approval_reference=approval_reference,
            actor=actor,
        )
        storage = (
            dict(dict(recovery_plan["boundary"])["storage"])
            if simulation
            else self.recovery._storage_identity(conn=conn)
        )
        if storage != dict(dict(recovery_plan["boundary"])["storage"]):
            raise CapsuleError("storage_generation_drift", "Storage changed inside capsule transaction")
        if not simulation:
            admission_source = mapping_module._mapping_admission_snapshot(
                conn,
                recovery=self.recovery,
            )
            identity_snapshot = mapping_module._identity_snapshot(
                conn,
                tuple_value=self.mapping.incident_passport["tuple"],
            )
            locked_material = mapping_module._locked_material(
                external_identity_digest=str(
                    self.mapping.incident_passport["evidence"]["external_identity_digest"]
                ),
                target_id=CANONICAL_TARGET_ID,
                deployed_sha=self.deployed_sha,
                source_runtime_sha=str(
                    self.mapping.incident_passport["target"]["source_runtime_sha"]
                ),
                storage=storage,
                admission_source=admission_source,
                identity_snapshot=identity_snapshot,
                mapping=self.mapping.mapping,
            )
            if manifest_digest(locked_material) != str(
                dict(mapping_plan["material_cas"])["digest"]
            ):
                raise CapsuleError(
                    "mapping_material_cas_drift",
                    "Tuple, owner, mapping absence, storage or cutover changed",
                )
        inserted = conn.execute(
            f"""INSERT INTO {IDENTITY_MAPPINGS_TABLE}(
                   mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,
                   target_nm_id,mapping_digest,active,created_at,created_by
               ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
            (
                str(mapping_value["mapping_id"]),
                int(mapping_value["source_nm_id"]),
                int(mapping_value["source_chrt_id"]),
                str(mapping_value["source_barcode"]),
                str(mapping_value["source_sku"]),
                int(mapping_value["target_nm_id"]),
                str(mapping_value["mapping_digest"]),
                applied_at,
                operation_proof,
            ),
        ).rowcount
        if inserted != 1:
            raise CapsuleError("mapping_insert_count_invalid", "Capsule mapping insert was not exactly one")
        source = self.recovery._source_snapshot(conn, storage=storage)
        recovery_module._verify_reviewed_source(recovery_plan, source)
        recovery_module._append_mapping_recovery_identity_evidence(
            conn,
            rows=list(source.get("mapping_re_evidence") or []),
            observed_at=applied_at,
        )
        manifest = recovery_module._active_manifest(conn)["manifest"]
        sequences = tuple(int(value) for value in source["target_sequences"])
        before_balances = recovery_module._balance_payload(conn)
        before_surfaces = recovery_module._dependent_surface_snapshot(conn, source=source)
        recovery_result = recover_pinned_fbs_lifecycle(
            conn,
            manifest=manifest,
            status_observation_sequences=sequences,
            occurred_at=applied_at,
        )
        after_balances = recovery_module._balance_payload(conn)
        after_surfaces = recovery_module._dependent_surface_snapshot(conn, source=source)
        target_result = recovery_module._target_result_payload(conn, sequences)
        effect = recovery_module._preview_payload(
            recovery=recovery_result,
            before_balances=before_balances,
            after_balances=after_balances,
            target_result=target_result,
        )
        effect["dependent_surface_plan"] = {
            "contract": "fbs_lifecycle_dependent_surface_plan/v1",
            "surface_kinds": [
                "FACILITY_SKU",
                "FACILITY_TOTAL",
                "FUNCTIONAL_ECONOMICS",
                "GLOBAL_SKU",
                "GLOBAL_TOTAL",
            ],
            "before": before_surfaces,
            "after": after_surfaces,
            "before_digest": recovery_module._fingerprint(before_surfaces),
            "after_digest": recovery_module._fingerprint(after_surfaces),
        }
        if recovery_module._fingerprint(effect) != recovery_module._fingerprint(
            recovery_plan["predicted_effects"]
        ):
            raise CapsuleError("target_after_image_drift", "Capsule after-image differs from manifest")
        if any(str(item["outcome"]) == "identity_quarantine" for item in target_result):
            raise CapsuleError("target_remains_identity_quarantined", "A target remained quarantined")
        recovery_fingerprint = str(
            dict(recovery_plan.get("expected_writes") or {}).get("recovery_fingerprint")
            or recovery_plan.get("recovery_digest")
            or ""
        )
        if DIGEST_RE.fullmatch(recovery_fingerprint) is None:
            raise CapsuleError("recovery_fingerprint_missing", "Recovery digest is not bound")
        _verify_planned_wb_history_copy(
            conn,
            captures=dict(recovery_plan["history"])["captures"],
        )
        history_receipts = recovery_module._apply_history(
            conn,
            reviewed_history=dict(recovery_plan["history"]),
            recovery_fingerprint=recovery_fingerprint,
            deployed_sha=self.deployed_sha,
            approval_reference=approval_reference,
            applied_at=applied_at,
        )
        reviewed_groups = {
            (str(item["facility_id"]), int(item["nm_id"]))
            for item in dict(recovery_plan["scope"])["groups"]
        }
        if not simulation:
            if recovery_module._non_target_digest(conn, target_groups=reviewed_groups) != str(
                dict(recovery_plan["baselines"])["non_target_digest"]
            ):
                raise CapsuleError("non_target_after_image_drift", "Non-target rows changed")
            if _stored_wb_history_digest(
                conn,
                captures=dict(recovery_plan["history"])["captures"],
            ) != str(dict(recovery_plan["baselines"])["wb_history_semantic_digest"]):
                raise CapsuleError("wb_after_image_drift", "WB history values changed")
        result_digest = recovery_module._fingerprint(
            {"effect": effect, "target_result": target_result, "history": history_receipts}
        )
        recovery_id = "fbscap_" + recovery_fingerprint.removeprefix("sha256:")[:25]
        boundary = dict(recovery_plan["boundary"])
        business_dates = list(dict(recovery_plan["scope"])["business_dates"])
        conn.execute(
            f"""INSERT INTO {QUALITY_RECOVERY_RUNS_TABLE}(
                   recovery_id,generation_id,cutover_id,contract_version,deployed_sha,
                   storage_generation_id,storage_schema_revision,sqlite_schema_version,
                   source_cutoff_sequence,date_from,date_to,manifest_fingerprint,
                   stable_target_digest,expected_effect_digest,source_history_digest,
                   before_non_target_digest,result_digest,summary_json,target_count,
                   history_capture_count,approval_reference,applied_by,status,applied_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'completed',?)""",
            (
                recovery_id,
                str(boundary["forward_generation_id"]),
                str(boundary["cutover_id"]),
                recovery_module.QUALITY_RECOVERY_STORAGE_CONTRACT_VERSION,
                self.deployed_sha,
                str(storage["operational_generation_id"]),
                str(storage["operational_schema_revision"]),
                int(storage["sqlite_schema_version"]),
                int(boundary["source_cursor_max"]),
                str(business_dates[0]),
                str(business_dates[-1]),
                recovery_fingerprint,
                str(dict(recovery_plan["scope"])["stable_target_digest"]),
                recovery_module._fingerprint(effect),
                str(dict(recovery_plan["history"])["digest"]),
                str(dict(recovery_plan["baselines"])["non_target_digest"]),
                result_digest,
                recovery_module._json(
                    {
                        "operation_id": operation_id,
                        "authorization_reference_digest": recovery_module._fingerprint(
                            approval_reference
                        ),
                        "effect": effect,
                        "history": history_receipts,
                        "capsule_manifest_digest": recovery_plan.get("manifest_digest"),
                        "wb_write_count": 0,
                    }
                ),
                len(sequences),
                len(history_receipts),
                approval_reference,
                actor,
                applied_at,
            ),
        )
        scope_by_sequence = {
            int(item["status_observation_sequence"]): dict(item)
            for item in source["resolved_scopes"]
        }
        result_by_sequence = {
            int(item["status_observation_sequence"]): dict(item) for item in target_result
        }
        target_by_sequence = {
            int(item["status_observation_sequence"]): dict(item) for item in source["target_rows"]
        }
        for sequence in sequences:
            scope = scope_by_sequence[sequence]
            target = target_by_sequence[sequence]
            result = result_by_sequence[sequence]
            conn.execute(
                f"""INSERT INTO {QUALITY_RECOVERY_TARGETS_TABLE}(
                       recovery_id,source_status_observation_sequence,order_id,facility_id,
                       nm_id,stable_business_digest,before_state_digest,after_state_digest,outcome
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    recovery_id,
                    sequence,
                    int(target["order_id"]),
                    str(scope["facility_id"]),
                    int(scope["nm_id"]),
                    str(target["stable_business_digest"]),
                    str(target["before_state_digest"]),
                    recovery_module._fingerprint(result),
                    str(result["outcome"]),
                ),
            )
        for receipt in history_receipts:
            conn.execute(
                f"""INSERT INTO {QUALITY_RECOVERY_HISTORY_TABLE}(
                       recovery_id,business_date,capture_id,source_digest,finalization_id,
                       finalization_digest,supersedes_finalization_digest
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    recovery_id,
                    str(receipt["business_date"]),
                    str(receipt["capture_id"]),
                    str(receipt["source_digest"]),
                    str(receipt["finalization_id"]),
                    str(receipt["finalization_digest"]),
                    str(receipt["supersedes_finalization_digest"]),
                ),
            )
        return {
            "mapping_insert_count": 1,
            "target_count": len(sequences),
            "history_capture_count": len(history_receipts),
            "effect_digest": recovery_module._fingerprint(effect),
            "result_digest": result_digest,
        }


def _parse_release_binding(value: Mapping[str, Any], *, deployed_sha: str) -> dict[str, Any]:
    item = dict(value)
    required = {
        "contract",
        "repository",
        "pull_request",
        "release_operation_id",
        "release_kind",
        "state",
        "base_sha",
        "head_sha",
        "merge_sha",
        "deployed_sha",
        "plan_hash",
        "gate_workflow_run_id",
        "release_receipt_digest",
    }
    if set(item) != required:
        raise CapsuleError("release_binding_schema_invalid", "Release binding fields are not exact")
    if (
        item["contract"] != RELEASE_BINDING_CONTRACT
        or item["repository"] != "orenvlad-ai/wb-core"
        or item["release_kind"] != "live_runtime"
        or item["state"] != "done"
        or int(item["pull_request"]) <= 0
        or int(item["gate_workflow_run_id"]) <= 0
        or any(SHA_RE.fullmatch(str(item[key])) is None for key in ("base_sha", "head_sha", "merge_sha", "deployed_sha"))
        or str(item["merge_sha"]) != deployed_sha
        or str(item["deployed_sha"]) != deployed_sha
        or DIGEST_RE.fullmatch(str(item["release_receipt_digest"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(item["plan_hash"])) is None
        or re.fullmatch(r"release-v2-[0-9a-f]{32}", str(item["release_operation_id"])) is None
    ):
        raise CapsuleError("release_binding_invalid", "Capsule release is not exact live_runtime/done")
    return json.loads(canonical_bytes(item))


def _parse_capsule_manifest(value: Mapping[str, Any], *, deployed_sha: str) -> dict[str, Any]:
    item = dict(value)
    required = {
        "contract",
        "operation_id",
        "capsule_release",
        "target",
        "storage",
        "boundary",
        "missing_identity",
        "scope",
        "impact",
        "predicted_effects",
        "history",
        "baselines",
        "expected_writes",
        "backup_recovery",
        "apply_contract",
        "post_apply_readback_contract",
        "manifest_digest",
    }
    if set(item) != required:
        raise CapsuleError("capsule_manifest_schema_invalid", "Capsule manifest fields are not exact")
    if item["contract"] != MANIFEST_CONTRACT:
        raise CapsuleError("capsule_manifest_contract_invalid", "Capsule contract is invalid")
    _require_id(str(item["operation_id"]), "operation_id")
    _parse_release_binding(dict(item["capsule_release"]), deployed_sha=deployed_sha)
    target = dict(item["target"])
    if target != {"target_id": CANONICAL_TARGET_ID, "runtime_sha": deployed_sha}:
        raise CapsuleError("capsule_target_drift", "Capsule target/runtime is not exact")
    missing = dict(item["missing_identity"])
    if set(missing) != {
        "tuple",
        "owner_evidence_digest",
        "mapping_absence",
        "proposed_mapping",
        "material_cas",
    }:
        raise CapsuleError("capsule_mapping_schema_invalid", "Capsule mapping scope is invalid")
    absence = dict(missing["mapping_absence"])
    if absence != {"active_mapping_count": 0, "all_mapping_count": 0}:
        raise CapsuleError("capsule_mapping_not_absent", "Capsule requires exact mapping absence")
    parse_impact_manifest(dict(item["impact"]))
    expected = dict(item["expected_writes"])
    if (
        not isinstance(expected.get("tables"), list)
        or not expected["tables"]
        or DIGEST_RE.fullmatch(str(expected.get("digest") or "")) is None
        or DIGEST_RE.fullmatch(str(expected.get("recovery_fingerprint") or "")) is None
        or any(
            not isinstance(row, Mapping)
            or TABLE_RE.fullmatch(str(row.get("table") or "")) is None
            or any(int(row.get(key) or 0) < 0 for key in ("insert", "update", "delete"))
            for row in expected["tables"]
        )
    ):
        raise CapsuleError("capsule_expected_writes_invalid", "Expected table writes are invalid")
    material = {key: val for key, val in item.items() if key != "manifest_digest"}
    if item["manifest_digest"] != manifest_digest(material):
        raise CapsuleError("capsule_manifest_digest_mismatch", "Capsule manifest digest is not canonical")
    return json.loads(canonical_bytes(item))


def _parse_qualification(
    value: Mapping[str, Any], *, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    item = dict(value)
    if (
        item.get("contract") != QUALIFICATION_CONTRACT
        or item.get("state") != "HUMAN_REQUIRED"
        or item.get("operation_id") != manifest.get("operation_id")
        or dict(item.get("manifest") or {}).get("digest") != manifest.get("manifest_digest")
        or int(item.get("production_mutation_submit_count") or 0) != 0
        or item.get("next_state") != "HUMAN_REQUIRED"
        or DIGEST_RE.fullmatch(str(item.get("qualification_digest") or "")) is None
    ):
        raise CapsuleError("qualification_binding_invalid", "Qualification does not bind this capsule")
    material = {key: val for key, val in item.items() if key != "qualification_digest"}
    if item["qualification_digest"] != manifest_digest(material):
        raise CapsuleError("qualification_digest_mismatch", "Qualification digest is not canonical")
    return json.loads(canonical_bytes(item))


def _install_write_audit(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS capsule_write_audit(table_name TEXT NOT NULL, action TEXT NOT NULL)"
    )
    conn.execute("DELETE FROM temp.capsule_write_audit")
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    ]
    for index, table in enumerate(tables):
        if TABLE_RE.fullmatch(table) is None:
            raise CapsuleError("unsafe_table_name", "SQLite table name is not auditable")
        quoted = '"' + table.replace('"', '""') + '"'
        literal = table.replace("'", "''")
        for action, event in (("insert", "INSERT"), ("update", "UPDATE"), ("delete", "DELETE")):
            conn.execute(
                f"CREATE TEMP TRIGGER capsule_audit_{index}_{action} AFTER {event} ON main.{quoted} "
                f"BEGIN INSERT INTO capsule_write_audit(table_name,action) VALUES('{literal}','{action}'); END"
            )


def _extend_scratch_with_history(
    source: sqlite3.Connection,
    scratch: sqlite3.Connection,
    *,
    recovery_plan: Mapping[str, Any],
) -> None:
    tracker = forward_module._ProjectionTracker()
    cutover_id = str(dict(recovery_plan["boundary"])["cutover_id"])
    forward_module._copy_projection_rows(
        source,
        scratch,
        recovery_module.FORWARD_GENERATIONS_TABLE,
        tracker,
        where="cutover_id=?",
        parameters=(cutover_id,),
    )
    generation_ids = [
        str(row[0])
        for row in source.execute(
            f"SELECT generation_id FROM {recovery_module.FORWARD_GENERATIONS_TABLE} WHERE cutover_id=?",
            (cutover_id,),
        ).fetchall()
    ]
    forward_module._copy_projection_in(
        source,
        scratch,
        recovery_module.FORWARD_STATE_TABLE,
        "generation_id",
        generation_ids,
        tracker,
    )
    tables = (CAPTURES_TABLE, COMPONENTS_TABLE, FINALIZATIONS_TABLE, APPLIES_TABLE)
    for table in tables:
        if scratch.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            forward_module._create_projection_table(source, scratch, table)
    captures = [
        str(item["base_capture_id"])
        for item in dict(recovery_plan["history"])["captures"]
    ]
    dates = [
        str(item["business_date"])
        for item in dict(recovery_plan["history"])["captures"]
    ]
    forward_module._copy_projection_in(
        source,
        scratch,
        CAPTURES_TABLE,
        "capture_id",
        captures,
        tracker,
    )
    forward_module._copy_projection_in(
        source,
        scratch,
        COMPONENTS_TABLE,
        "capture_id",
        captures,
        tracker,
    )
    forward_module._copy_projection_in(
        source,
        scratch,
        FINALIZATIONS_TABLE,
        "business_date",
        dates,
        tracker,
    )
    placeholders = ",".join("?" for _ in tables)
    for row in source.execute(
        "SELECT sql FROM sqlite_master WHERE type IN ('index','trigger') "
        f"AND tbl_name IN ({placeholders}) AND sql IS NOT NULL ORDER BY type,name",
        tables,
    ).fetchall():
        scratch.execute(str(row[0]))
    forward_module._seed_projection_autoincrement(
        source,
        scratch,
        table_names=tables,
    )


def _wb_components_from_capture(
    conn: sqlite3.Connection,
    *,
    capture_id: str,
) -> list[dict[str, Any]]:
    values = [
        recovery_module._stored_component(row)
        for row in conn.execute(
            f"""SELECT scope_kind,scope_key,nm_id,component_kind,component_id,
                       component_label,state,quantity,source_revision,source_digest,
                       source_watermark,provenance_json
                FROM {COMPONENTS_TABLE}
                WHERE capture_id=? AND component_kind LIKE 'WB%'
                ORDER BY scope_kind,scope_key,component_kind,component_id""",
            (str(capture_id),),
        ).fetchall()
    ]
    return sorted(values, key=canonical_bytes)


def _planned_wb_components(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    values = [
        {key: value for key, value in dict(component).items() if key != "captured_at"}
        for component in item.get("components") or []
        if str(component.get("component_kind") or "").startswith("WB")
    ]
    return sorted(values, key=canonical_bytes)


def _remain_missing_readback(
    conn: sqlite3.Connection,
    *,
    captures: Sequence[Mapping[str, Any]],
    remain_missing: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    captures_by_date = {
        str(item["business_date"]): dict(item) for item in captures
    }
    values: list[dict[str, Any]] = []
    for raw in remain_missing:
        item = dict(raw)
        capture = captures_by_date.get(str(item["business_date"]))
        scope_key = (
            "TOTAL"
            if str(item["scope_kind"]) == "FACILITY_TOTAL"
            else f"SKU:{int(item['nm_id'])}"
        )
        planned = None
        if capture is not None:
            for raw_component in capture.get("components") or []:
                component = {
                    key: value
                    for key, value in dict(raw_component).items()
                    if key != "captured_at"
                }
                if (
                    str(component["scope_key"]) == scope_key
                    and str(component["component_kind"]) == "FBS_FACILITY"
                    and str(component["component_id"]) == str(item["facility_id"])
                ):
                    planned = component
                    break
        stored_row = None
        if capture is not None:
            stored_row = conn.execute(
                f"""SELECT scope_kind,scope_key,nm_id,component_kind,component_id,
                           component_label,state,quantity,source_revision,source_digest,
                           source_watermark,provenance_json
                    FROM {COMPONENTS_TABLE}
                    WHERE capture_id=? AND scope_key=?
                      AND component_kind='FBS_FACILITY' AND component_id=?""",
                (
                    str(capture["capture_id"]),
                    scope_key,
                    str(item["facility_id"]),
                ),
            ).fetchone()
        stored = recovery_module._stored_component(stored_row) if stored_row else None
        expected_state = str(item["base_state"])
        exact = (
            capture is not None
            and str(item.get("classification"))
            == "remain_missing_no_same_date_evidence"
            and (
                (planned is None and stored is None and expected_state == "missing")
                or planned is not None
                and stored == planned
                and str(planned.get("state")) == expected_state
            )
        )
        values.append(
            {
                **item,
                "capture_id": str(capture["capture_id"]) if capture else "",
                "stored_state": (
                    str(stored["state"]) if stored is not None else "missing"
                ),
                "exact": bool(exact),
            }
        )
    return values


def _verify_planned_wb_history_copy(
    conn: sqlite3.Connection,
    *,
    captures: Sequence[Mapping[str, Any]],
) -> None:
    for item in captures:
        planned = _planned_wb_components(item)
        base = _wb_components_from_capture(
            conn,
            capture_id=str(item["base_capture_id"]),
        )
        if planned != base:
            raise CapsuleError(
                "wb_history_plan_drift",
                "Planned same-date capture would change WB components",
                details={
                    "business_date": item["business_date"],
                    "planned_digest": manifest_digest(planned),
                    "base_digest": manifest_digest(base),
                },
            )


def _planned_wb_history_digest(history: Mapping[str, Any]) -> str:
    material = [
        {
            "business_date": str(item["business_date"]),
            "capture_id": str(item["capture_id"]),
            "components": _planned_wb_components(item),
        }
        for item in history.get("captures") or []
    ]
    return manifest_digest(material)


def _stored_wb_history_digest(
    conn: sqlite3.Connection,
    *,
    captures: Sequence[Mapping[str, Any]],
) -> str:
    material = [
        {
            "business_date": str(item["business_date"]),
            "capture_id": str(item["capture_id"]),
            "components": _wb_components_from_capture(
                conn,
                capture_id=str(item["capture_id"]),
            ),
        }
        for item in captures
    ]
    return manifest_digest(material)


def _read_write_audit(conn: sqlite3.Connection) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {}
    for table, action, count in conn.execute(
        "SELECT table_name,action,COUNT(*) FROM temp.capsule_write_audit GROUP BY table_name,action ORDER BY table_name,action"
    ).fetchall():
        counts.setdefault(str(table), Counter())[str(action)] = int(count)
    tables = [
        {
            "table": table,
            "insert": int(actions.get("insert", 0)),
            "update": int(actions.get("update", 0)),
            "delete": int(actions.get("delete", 0)),
        }
        for table, actions in sorted(counts.items())
    ]
    material = {"tables": tables, "total_rows": sum(sum(row[key] for key in ("insert", "update", "delete")) for row in tables)}
    return {**material, "digest": manifest_digest(material)}


def _scoped_authorizer(allowed_tables: set[str]) -> Any:
    def authorizer(
        action: int,
        arg1: str | None,
        _arg2: str | None,
        database: str | None,
        _trigger: str | None,
    ) -> int:
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            if str(database or "") == "main" and str(arg1 or "") not in allowed_tables:
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


def _matrix_evidence(rehearsal: Mapping[str, Any], check: str) -> dict[str, Any]:
    for raw in rehearsal.get("matrix") or []:
        item = dict(raw)
        if item.get("check") == check:
            return dict(item.get("evidence") or {})
    raise CapsuleError("rehearsal_matrix_missing", f"Rehearsal check is missing: {check}")


def _require_accepted_rehearsal(value: Mapping[str, Any]) -> None:
    if (
        value.get("contract") != "fbs_lifecycle_consolidated_rehearsal/v2"
        or value.get("mode") != "query_only_no_submit"
        or value.get("accepted") is not True
        or value.get("matrix_status") != "PASS"
        or value.get("source_database_query_only") is not True
        or int(value.get("mapping_insert_count") or 0) != 0
        or int(value.get("recovery_write_count") or 0) != 0
        or int(value.get("history_write_count") or 0) != 0
        or value.get("blockers")
    ):
        raise CapsuleError("query_only_rehearsal_blocked", "Capsule rehearsal did not qualify")


def _authorization_body(
    *,
    release: Mapping[str, Any],
    operation_id: str,
    manifest_digest: str,
    qualification_digest: str,
) -> str:
    return (
        "/wb-core apply-incident-capsule-v1 task WBC0027 target "
        f"{CANONICAL_TARGET_ID} pr {int(release['pull_request'])} release "
        f"{release['release_operation_id']} deployed {release['deployed_sha']} manifest "
        f"{manifest_digest} qualification {qualification_digest} operation {operation_id} "
        "mapping-inserts 1 submits 1"
    )


def _simulation_authorization(
    *, release: Mapping[str, Any], operation_id: str, recovery_digest: str
) -> str:
    return (
        "qualification-simulation:"
        + manifest_digest(
            {
                "release": release,
                "operation_id": operation_id,
                "recovery_digest": recovery_digest,
            }
        )
    )


def _require_id(value: str, field: str) -> str:
    item = str(value or "").strip()
    if SAFE_ID_RE.fullmatch(item) is None:
        raise CapsuleError("invalid_identity", f"{field} is invalid")
    return item


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _write_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool) -> None:
    if exclusive:
        mapping_module._write_private_exclusive(path, payload)
    else:
        recovery_module._write_private(path, payload)


def run(args: argparse.Namespace) -> int:
    capsule = Wbc0027IncidentRecoveryCapsule(
        runtime_dir=Path(args.runtime_dir),
        deployed_sha=str(args.deployed_sha),
        incident_passport=read_json(Path(args.passport_file)),
        release_binding=read_json(Path(args.release_binding_file)),
        scratch_dir=Path(args.scratch_dir) if args.scratch_dir else None,
    )
    if args.command == "qualification":
        payload = capsule.qualification(
            operation_id=str(args.operation_id),
            evidence_dir=Path(args.evidence_dir),
        )
    elif args.command == "apply":
        payload = capsule.apply(
            manifest=read_json(Path(args.manifest_file)),
            qualification=read_json(Path(args.qualification_file)),
            operation_id=str(args.operation_id),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            evidence_dir=Path(args.evidence_dir),
        )
    elif args.command == "readback":
        payload = capsule.readback(
            manifest=read_json(Path(args.manifest_file)),
            operation_id=str(args.operation_id),
            approval_reference=str(args.approval_reference),
        )
    else:
        raise CapsuleError("invalid_command", "Capsule command is required")
    if args.output:
        _write_json(Path(args.output), payload, exclusive=False)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("state") in {"HUMAN_REQUIRED", "done"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--passport-file", required=True)
    parser.add_argument("--release-binding-file", required=True)
    parser.add_argument("--scratch-dir", default="")
    parser.add_argument("--output", default="")
    sub = parser.add_subparsers(dest="command", required=True)
    qualification = sub.add_parser("qualification")
    qualification.add_argument("--operation-id", required=True)
    qualification.add_argument("--evidence-dir", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--manifest-file", required=True)
    apply.add_argument("--qualification-file", required=True)
    apply.add_argument("--operation-id", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--evidence-dir", required=True)
    readback = sub.add_parser("readback")
    readback.add_argument("--manifest-file", required=True)
    readback.add_argument("--operation-id", required=True)
    readback.add_argument("--approval-reference", required=True)
    return parser


def main() -> int:
    try:
        return run(build_parser().parse_args())
    except (CapsuleError, FbsManifestError) as exc:
        print(
            json.dumps(
                {
                    "state": "blocked",
                    "error_code": getattr(exc, "code", "capsule_error"),
                    "error": str(exc),
                    "details": getattr(exc, "details", None),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
