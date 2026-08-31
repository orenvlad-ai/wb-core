#!/usr/bin/env python3
"""Guarded WBC0027 FBS lifecycle and same-date history recovery.

The default command is a query-only coherent dry-run.  Apply accepts only the
exact reviewed manifest fingerprint, takes the canonical warehouse writer
lock, performs one SQLite submit, appends lifecycle resolution/events plus
same-date superseding history captures/finalizations, and records an immutable
readback receipt.  It never writes WB stock and never copies a current FBS
value backwards into history.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_fbs_forward_recovery import (  # noqa: E402
    FfPoolFbsForwardRecoveryError,
    _active_manifest,
    _balance_payload,
    _build_preview_projection,
    _canonical_write_seeds,
    _fingerprint,
    _past_fulfilled_invariant,
    _prepare_private_scratch_root,
    _preview_payload,
    _projection_schema_evidence,
    _stable_target_rows,
    _target_location_wac_evidence,
    _target_result_payload,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    EVENTS_TABLE,
    FORWARD_GENERATIONS_TABLE,
    FORWARD_STATE_TABLE,
    MAPPING_EXTENSION_ALLOCATIONS_TABLE,
    MAPPING_EXTENSIONS_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    QUALITY_RECOVERY_HISTORY_TABLE,
    QUALITY_RECOVERY_RUNS_TABLE,
    QUALITY_RECOVERY_TARGETS_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    recover_pinned_fbs_lifecycle,
    resolve_fbs_lifecycle_status_scope,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    preview_inventory_history_capture,
)
from packages.application.storage_registry import manifest_payload  # noqa: E402
from packages.application.fbs_lifecycle_manifests import (  # noqa: E402
    FbsManifestError,
    IMPACT_MANIFEST_CONTRACT,
    RECOVERY_MANIFEST_CONTRACT,
    attach_digest,
    digest as manifest_digest,
    parse_impact_manifest,
    parse_incident_passport,
    parse_mapping_manifest,
    parse_recovery_manifest,
    read_json as read_manifest_json,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)
from packages.business_time import current_business_date_iso  # noqa: E402


CONTRACT_NAME = RECOVERY_MANIFEST_CONTRACT
CONTRACT_VERSION = 2
# The append-only receipt table predates manifest v2 and retains its storage
# schema discriminator.  The versioned manifest contract is persisted in the
# bound digest/summary rather than rewriting historical receipt rows.
QUALITY_RECOVERY_STORAGE_CONTRACT_VERSION = 1
MAX_TARGET_COUNT = 10_000
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")


class Wbc0027RecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class Wbc0027FbsLifecycleQualityRecovery:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        incident_passport: Mapping[str, Any],
        timestamp_factory: Any | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(runtime_dir).expanduser().resolve()
        )
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise Wbc0027RecoveryError(
                "invalid_deployed_sha", "deployed_sha must be exact 40-hex"
            )
        try:
            self.incident_passport = parse_incident_passport(incident_passport)
        except FbsManifestError as exc:
            raise Wbc0027RecoveryError(exc.code, str(exc), details=exc.details) from exc
        self.timestamp_factory = timestamp_factory or _utc_now
        self.scratch_dir = Path(
            scratch_dir
            if scratch_dir is not None
            else self.runtime.runtime_dir / "wbc0027-fbs-quality-recovery-scratch"
        ).expanduser()

    @property
    def mapping_tuple(self) -> dict[str, Any]:
        return dict(self.incident_passport["tuple"])

    def build_manifests(
        self,
        *,
        hypothetical_mapping: Mapping[str, Any] | None = None,
        mapping_readback_digest: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        observed_at = str(self.timestamp_factory())
        _require_utc(observed_at)
        generated_at = observed_at[:10] + "T00:00:00Z"
        storage = self._storage_identity()
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            conn.execute("BEGIN")
            try:
                source = self._source_snapshot(
                    conn,
                    storage=storage,
                    hypothetical_mapping=hypothetical_mapping,
                )
                preview, target_result = self._preview(conn, source=source, at=generated_at)
                history = _history_plan(
                    conn,
                    source=source,
                    target_result=target_result,
                    generated_at=generated_at,
                    contract_name=CONTRACT_NAME,
                )
                groups = {
                    (str(item["facility_id"]), int(item["nm_id"]))
                    for item in source["resolved_scopes"]
                }
                non_target = _non_target_digest(conn, target_groups=groups)
                wb_baseline = _wb_baseline_digest(conn)
                if self._storage_identity(conn=conn) != storage:
                    raise Wbc0027RecoveryError(
                        "storage_generation_drift", "Storage changed inside dry-run snapshot"
                    )
            finally:
                if conn.in_transaction:
                    conn.rollback()
        blockers = list(source["blockers"])
        delta_groups = {
            (str(item["facility_id"]), int(item["nm_id"]))
            for item in preview["balance_deltas"]
        }
        target_group_set = {
            (str(item["facility_id"]), int(item["nm_id"]))
            for item in source["resolved_scopes"]
        }
        if not delta_groups.issubset(target_group_set):
            blockers.append("predicted_non_target_balance_effect")
        if preview.get("wb_write_count") != 0:
            blockers.append("predicted_wb_write")
        if any(str(row["outcome"]) == "identity_quarantine" for row in target_result):
            blockers.append("target_remains_identity_quarantined")
        if history["blockers"]:
            blockers.extend(str(value) for value in history["blockers"])
        readback_digest = _require_digest(
            mapping_readback_digest
            or manifest_digest(
                {
                    "mode": "hypothetical_mapping",
                    "tuple_digest": self.mapping_tuple["tuple_digest"],
                    "production_mapping_insert_count": 0,
                }
            ),
            "mapping_readback_digest",
        )
        boundary = {
            "storage": storage,
            "cutover_id": source["cutover_id"],
            "cutover_manifest_digest": source["cutover_manifest_digest"],
            "forward_generation_id": source["generation_id"],
            "forward_generation_manifest_digest": source[
                "generation_manifest_fingerprint"
            ],
            "forward_cursor_sequence": source["old_cursor_sequence"],
            "source_cursor_max": source["source_cursor_max"],
            "mapping_readback_digest": readback_digest,
        }
        impact_material: dict[str, Any] = {
            "contract": IMPACT_MANIFEST_CONTRACT,
            "operation_id": str(self.incident_passport["operation_id"]) + ":impact",
            "target": {
                "target_id": self.incident_passport["target"]["target_id"],
                "runtime_sha": self.deployed_sha,
            },
            "mapping_readback_digest": readback_digest,
            "storage": storage,
            "boundary": boundary,
            "unresolved_scan": _impact_scan(source),
            "affected_groups": _affected_group_rows(target_group_set),
            "dependent_surfaces": [
                "fbs_facility_sku",
                "fbs_facility_total",
                "fbs_global_sku",
                "fbs_global_total",
                "stock_total",
                "own_product_capital",
                "warehouse_wac",
                "finance_partner_economics",
                "inventory_history",
            ],
            "history_evidence": {
                "classification_counts": history["classification_counts"],
                "cell_evidence": history["cell_evidence"],
                "digest": history["evidence_digest"],
            },
            "baselines": {
                "non_target_digest": non_target,
                "wb_digest": wb_baseline,
            },
            "blockers": sorted(set(blockers)),
        }
        impact = attach_digest(impact_material, "impact_digest")
        parse_impact_manifest(impact)
        scope = {
            "groups": _group_rows(target_group_set),
            "business_dates": history["business_dates"],
            "target_count": len(source["target_sequences"]),
            "target_sequences": source["target_sequences"],
            "target_row_digests": [
                manifest_digest(dict(item)) for item in source["target_rows"]
            ],
            "stable_target_digest": source["stable_target_digest"],
            "target_rows": source["target_rows"],
            "location_wac_evidence": source["location_wac_evidence"],
            "resolved_scopes": source["resolved_scopes"],
            "mapping_re_evidence": source["mapping_re_evidence"],
            "typed_blocker_rows": source["typed_blocker_rows"],
            "coverage": source["coverage"],
        }
        recovery_material: dict[str, Any] = {
            "contract": RECOVERY_MANIFEST_CONTRACT,
            "operation_id": str(self.incident_passport["operation_id"]) + ":recovery",
            "target": {
                "target_id": self.incident_passport["target"]["target_id"],
                "runtime_sha": self.deployed_sha,
            },
            "impact_digest": impact["impact_digest"],
            "boundary": boundary,
            "scope": scope,
            "predicted_effects": preview,
            "history": history,
            "baselines": {
                "non_target_digest": non_target,
                "wb_digest": wb_baseline,
                "projection_schema_evidence": source["projection_schema_evidence"],
                "canonical_write_seeds": source["canonical_write_seeds"],
                "past_fulfilled_invariant": source["past_fulfilled_invariant"],
            },
            "safety": {
                "default_mode": "query_only_dry_run",
                "one_submit": True,
                "writer_lock": "warehouse_functional_write_lock",
                "target_cas": "exact_source_rows_history_base_and_effect",
                "before_image": "private_mode_0600_exclusive_create",
                "backup": "sqlite_transaction_and_private_before_image",
                "ambiguous_transport": "query_only_readback_no_retry",
                "current_retrocopy": False,
                "immutable_history_overwrite": False,
                "wb_writes": 0,
                "mapping_writes": 0,
                "hypothetical_mapping": hypothetical_mapping is not None,
            },
            "apply_allowed": not blockers,
            "blockers": sorted(set(blockers)),
        }
        recovery = attach_digest(recovery_material, "recovery_digest")
        parse_recovery_manifest(recovery)
        return impact, recovery

    def build_impact_manifest(
        self,
        *,
        hypothetical_mapping: Mapping[str, Any] | None = None,
        mapping_readback_digest: str = "",
    ) -> dict[str, Any]:
        return self.build_manifests(
            hypothetical_mapping=hypothetical_mapping,
            mapping_readback_digest=mapping_readback_digest,
        )[0]

    def build_plan(
        self,
        *,
        hypothetical_mapping: Mapping[str, Any] | None = None,
        mapping_readback_digest: str = "",
    ) -> dict[str, Any]:
        return self.build_manifests(
            hypothetical_mapping=hypothetical_mapping,
            mapping_readback_digest=mapping_readback_digest,
        )[1]

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        approval_reference: str,
        actor: str,
        evidence_dir: Path,
    ) -> dict[str, Any]:
        try:
            reviewed = parse_recovery_manifest(reviewed_plan)
        except FbsManifestError as exc:
            raise Wbc0027RecoveryError(exc.code, str(exc), details=exc.details) from exc
        expected = _require_digest(fingerprint, "fingerprint")
        if reviewed.get("recovery_digest") != expected:
            raise Wbc0027RecoveryError(
                "reviewed_fingerprint_mismatch", "Reviewed plan fingerprint differs"
            )
        if reviewed.get("apply_allowed") is not True or reviewed.get("blockers"):
            raise Wbc0027RecoveryError("reviewed_plan_blocked", "Blocked plan cannot apply")
        if str(dict(reviewed.get("target") or {}).get("runtime_sha") or "") != self.deployed_sha:
            raise Wbc0027RecoveryError("deployed_sha_drift", "Plan belongs to another SHA")
        approval = str(approval_reference or "").strip()
        operator = str(actor or "").strip()
        if not approval or not operator:
            raise Wbc0027RecoveryError(
                "gate_identity_required", "approval_reference and actor are required"
            )
        existing = self.readback(fingerprint=expected)
        if existing.get("status") == "completed":
            return {**existing, "idempotent": True, "repeat_submit_performed": False}

        # A fresh query-only witness must be byte-for-byte business-equivalent
        # to the reviewed plan immediately before the writer lock.
        fresh = self.build_plan(
            mapping_readback_digest=str(
                dict(reviewed["boundary"])["mapping_readback_digest"]
            )
        )
        if fresh.get("recovery_digest") != expected:
            raise Wbc0027RecoveryError(
                "target_source_drift", "Source/history/effect changed after review"
            )
        evidence_root = Path(evidence_dir).expanduser().resolve()
        evidence_root.mkdir(parents=True, exist_ok=True)
        suffix = expected.removeprefix("sha256:")[:20]
        before_path = evidence_root / f"wbc0027-fbs-quality-{suffix}.before.json"
        evidence_path = evidence_root / f"wbc0027-fbs-quality-{suffix}.evidence.json"
        now = str(self.timestamp_factory())
        _require_utc(now)

        with warehouse_functional_write_lock(self.runtime.runtime_dir, timeout_seconds=300):
            locked_storage = self._storage_identity()
            if locked_storage != dict(dict(reviewed["boundary"])["storage"]):
                raise Wbc0027RecoveryError(
                    "storage_generation_drift", "Storage changed before writer submit"
                )
            before_image = {
                "contract_name": CONTRACT_NAME,
                "fingerprint": expected,
                "deployed_sha": self.deployed_sha,
                "storage": locked_storage,
                "boundary": reviewed["boundary"],
                "scope_digest": _fingerprint(reviewed["scope"]),
                "predicted_effect_digest": _fingerprint(reviewed["predicted_effects"]),
                "history_digest": str(dict(reviewed["history"])["digest"]),
                "non_target_digest": str(dict(reviewed["baselines"])["non_target_digest"]),
                "recovery": "atomic rollback before commit; append superseding history after",
                "created_at": now,
            }
            _write_private(before_path, before_image)
            conn = sqlite3.connect(self.runtime.db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.execute("BEGIN IMMEDIATE")
                ensure_ff_pool_fbs_lifecycle_schema(conn)
                if self._storage_identity(conn=conn) != dict(
                    dict(reviewed["boundary"])["storage"]
                ):
                    raise Wbc0027RecoveryError(
                        "storage_generation_drift", "Storage changed inside writer submit"
                    )
                source = self._source_snapshot(conn, storage=locked_storage)
                _verify_reviewed_source(reviewed, source)
                _append_mapping_recovery_identity_evidence(
                    conn,
                    rows=list(source.get("mapping_re_evidence") or []),
                    observed_at=now,
                )
                manifest = _active_manifest(conn)["manifest"]
                sequences = tuple(int(value) for value in source["target_sequences"])
                before_balances = _balance_payload(conn)
                recovery = recover_pinned_fbs_lifecycle(
                    conn,
                    manifest=manifest,
                    status_observation_sequences=sequences,
                    occurred_at=now,
                )
                after_balances = _balance_payload(conn)
                target_result = _target_result_payload(conn, sequences)
                actual_effect = _preview_payload(
                    recovery=recovery,
                    before_balances=before_balances,
                    after_balances=after_balances,
                    target_result=target_result,
                )
                if _fingerprint(actual_effect) != _fingerprint(reviewed["predicted_effects"]):
                    raise Wbc0027RecoveryError(
                        "target_after_image_drift",
                        "Writer after-image differs from reviewed prediction",
                    )
                if any(str(row["outcome"]) == "identity_quarantine" for row in target_result):
                    raise Wbc0027RecoveryError(
                        "target_remains_identity_quarantined",
                        "A reviewed target did not resolve fail closed",
                    )
                history_receipts = _apply_history(
                    conn,
                    reviewed_history=dict(reviewed["history"]),
                    recovery_fingerprint=expected,
                    deployed_sha=self.deployed_sha,
                    approval_reference=approval,
                    applied_at=now,
                )
                reviewed_groups = {
                    (str(item["facility_id"]), int(item["nm_id"]))
                    for item in dict(reviewed["scope"])["groups"]
                }
                if _non_target_digest(conn, target_groups=reviewed_groups) != str(
                    dict(reviewed["baselines"])["non_target_digest"]
                ):
                    raise Wbc0027RecoveryError(
                        "non_target_after_image_drift",
                        "A balance or history row outside the reviewed scope changed",
                    )
                result_digest = _fingerprint(
                    {
                        "effect": actual_effect,
                        "target_result": target_result,
                        "history": history_receipts,
                    }
                )
                recovery_id = "fbsqrec_" + expected.removeprefix("sha256:")[:25]
                boundary = dict(reviewed["boundary"])
                source_evidence = dict(reviewed["baselines"])
                business_dates = list(dict(reviewed["scope"])["business_dates"])
                conn.execute(
                    f"""INSERT INTO {QUALITY_RECOVERY_RUNS_TABLE}(
                           recovery_id,generation_id,cutover_id,contract_version,
                           deployed_sha,storage_generation_id,storage_schema_revision,
                           sqlite_schema_version,source_cutoff_sequence,date_from,date_to,
                           manifest_fingerprint,stable_target_digest,
                           expected_effect_digest,source_history_digest,
                           before_non_target_digest,result_digest,summary_json,
                           target_count,history_capture_count,approval_reference,
                           applied_by,status,applied_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'completed',?)""",
                    (
                        recovery_id,
                        str(boundary["forward_generation_id"]),
                        str(boundary["cutover_id"]),
                        QUALITY_RECOVERY_STORAGE_CONTRACT_VERSION,
                        self.deployed_sha,
                        str(locked_storage["operational_generation_id"]),
                        str(locked_storage["operational_schema_revision"]),
                        int(locked_storage["sqlite_schema_version"]),
                        int(boundary["source_cursor_max"]),
                        str(business_dates[0]),
                        str(business_dates[-1]),
                        expected,
                        str(dict(reviewed["scope"])["stable_target_digest"]),
                        _fingerprint(actual_effect),
                        str(dict(reviewed["history"])["digest"]),
                        str(source_evidence["non_target_digest"]),
                        result_digest,
                        _json(
                            {
                                "effect": actual_effect,
                                "history": history_receipts,
                                "wb_write_count": 0,
                            }
                        ),
                        len(sequences),
                        len(history_receipts),
                        approval,
                        operator,
                        now,
                    ),
                )
                scope_by_sequence = {
                    int(item["status_observation_sequence"]): dict(item)
                    for item in source["resolved_scopes"]
                }
                result_by_sequence = {
                    int(item["status_observation_sequence"]): dict(item)
                    for item in target_result
                }
                target_by_sequence = {
                    int(item["status_observation_sequence"]): dict(item)
                    for item in source["target_rows"]
                }
                for sequence in sequences:
                    scope = scope_by_sequence[sequence]
                    target = target_by_sequence[sequence]
                    result = result_by_sequence[sequence]
                    conn.execute(
                        f"""INSERT INTO {QUALITY_RECOVERY_TARGETS_TABLE}(
                               recovery_id,source_status_observation_sequence,
                               order_id,facility_id,nm_id,stable_business_digest,
                               before_state_digest,after_state_digest,outcome
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (
                            recovery_id,
                            sequence,
                            int(target["order_id"]),
                            str(scope["facility_id"]),
                            int(scope["nm_id"]),
                            str(target["stable_business_digest"]),
                            str(target["before_state_digest"]),
                            _fingerprint(result),
                            str(result["outcome"]),
                        ),
                    )
                for receipt in history_receipts:
                    conn.execute(
                        f"""INSERT INTO {QUALITY_RECOVERY_HISTORY_TABLE}(
                               recovery_id,business_date,capture_id,source_digest,
                               finalization_id,finalization_digest,
                               supersedes_finalization_digest
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
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        readback = self.readback(fingerprint=expected)
        if readback.get("status") != "completed":
            raise Wbc0027RecoveryError(
                "post_apply_readback_failed", "Durable query-only readback is incomplete"
            )
        evidence = {
            "contract_name": CONTRACT_NAME,
            "status": "completed",
            "manifest_fingerprint": expected,
            "deployed_sha": self.deployed_sha,
            "approval_reference": approval,
            "actor": operator,
            "before_image_path": str(before_path),
            "before_image_sha256": _sha256_file(before_path),
            "readback": readback,
            "created_at": now,
        }
        _write_private(evidence_path, evidence)
        return {
            **evidence,
            "idempotent": False,
            "evidence_path": str(evidence_path),
            "evidence_sha256": _sha256_file(evidence_path),
        }

    def readback(self, *, fingerprint: str = "") -> dict[str, Any]:
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            tables = _table_names(conn)
            if QUALITY_RECOVERY_RUNS_TABLE not in tables:
                return {"contract_name": CONTRACT_NAME, "status": "not_applied"}
            where = "WHERE manifest_fingerprint=?" if fingerprint else ""
            params: tuple[Any, ...] = (str(fingerprint),) if fingerprint else ()
            row = conn.execute(
                f"""SELECT recovery_id,generation_id,cutover_id,deployed_sha,
                           source_cutoff_sequence,date_from,date_to,
                           manifest_fingerprint,stable_target_digest,result_digest,
                           summary_json,target_count,history_capture_count,
                           approval_reference,applied_by,status,applied_at
                    FROM {QUALITY_RECOVERY_RUNS_TABLE} {where}
                    ORDER BY applied_at DESC,recovery_id DESC LIMIT 1""",
                params,
            ).fetchone()
            if row is None:
                return {"contract_name": CONTRACT_NAME, "status": "not_applied"}
            recovery_id = str(row[0])
            targets = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {QUALITY_RECOVERY_TARGETS_TABLE} WHERE recovery_id=?",
                    (recovery_id,),
                ).fetchone()[0]
            )
            history = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {QUALITY_RECOVERY_HISTORY_TABLE} WHERE recovery_id=?",
                    (recovery_id,),
                ).fetchone()[0]
            )
            return {
                "contract_name": CONTRACT_NAME,
                "status": str(row[15]),
                "recovery_id": recovery_id,
                "generation_id": str(row[1]),
                "cutover_id": str(row[2]),
                "deployed_sha": str(row[3]),
                "source_cutoff_sequence": int(row[4]),
                "date_from": str(row[5]),
                "date_to": str(row[6]),
                "manifest_fingerprint": str(row[7]),
                "stable_target_digest": str(row[8]),
                "result_digest": str(row[9]),
                "summary": json.loads(str(row[10])),
                "target_count": int(row[11]),
                "target_readback_count": targets,
                "history_capture_count": int(row[12]),
                "history_readback_count": history,
                "approval_reference": str(row[13]),
                "applied_by": str(row[14]),
                "applied_at": str(row[16]),
                "query_only": True,
                "mutates_wb": False,
            }

    def _source_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        storage: Mapping[str, Any],
        hypothetical_mapping: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        required = {
            FORWARD_GENERATIONS_TABLE,
            FORWARD_STATE_TABLE,
            IDENTITY_PENDING_TABLE,
            IDENTITY_PENDING_RESOLUTIONS_TABLE,
            QUALITY_RECOVERY_RUNS_TABLE,
            QUALITY_RECOVERY_TARGETS_TABLE,
            QUALITY_RECOVERY_HISTORY_TABLE,
            EVENTS_TABLE,
            CAPTURES_TABLE,
            COMPONENTS_TABLE,
            FINALIZATIONS_TABLE,
            STATUS_OBSERVATIONS_TABLE,
            OBSERVATIONS_TABLE,
        }
        missing = sorted(required - _table_names(conn))
        if missing:
            raise Wbc0027RecoveryError(
                "recovery_schema_missing", "Required recovery schema is missing", details=missing
            )
        active = _active_manifest(conn)
        cutover_id = str(active["cutover_id"])
        generation = conn.execute(
            f"""SELECT generation.generation_id,generation.manifest_fingerprint,
                       state.last_status_observation_sequence
                FROM {FORWARD_GENERATIONS_TABLE} AS generation
                JOIN {FORWARD_STATE_TABLE} AS state USING(generation_id)
                WHERE generation.cutover_id=?""",
            (cutover_id,),
        ).fetchall()
        if len(generation) != 1:
            raise Wbc0027RecoveryError(
                "forward_generation_missing_or_ambiguous",
                "Exactly one active forward generation is required",
            )
        source_cursor_max = int(
            conn.execute(
                f"SELECT COALESCE(MAX(observation_sequence),0) FROM {STATUS_OBSERVATIONS_TABLE}"
            ).fetchone()[0]
        )
        if source_cursor_max <= 0:
            raise Wbc0027RecoveryError(
                "source_cursor_empty", "FBS status source has no current maximum"
            )
        pending_sequences = [
            int(row[0])
            for row in conn.execute(
                f"""SELECT pending.source_status_observation_sequence
                    FROM {IDENTITY_PENDING_TABLE} AS pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE pending.cutover_id=? AND resolution.pending_id IS NULL
                      AND pending.source_status_observation_sequence<=?
                    ORDER BY pending.source_status_observation_sequence""",
                (cutover_id, source_cursor_max),
            ).fetchall()
        ]
        if len(pending_sequences) > MAX_TARGET_COUNT:
            raise Wbc0027RecoveryError(
                "pending_target_bound_exceeded",
                "Unresolved source candidate count exceeds recovery bound",
                details={"count": len(pending_sequences), "max": MAX_TARGET_COUNT},
            )
        resolved: list[dict[str, Any]] = []
        mapping_re_evidence: list[dict[str, Any]] = []
        raw_typed_blockers: list[dict[str, Any]] = []
        blockers: list[str] = []
        if conn.execute(
            f"SELECT 1 FROM {STATUS_OBSERVATIONS_TABLE} WHERE observation_sequence=?",
            (source_cursor_max,),
        ).fetchone() is None:
            blockers.append("source_cursor_max_missing")
        manifest = dict(active["manifest"])
        for sequence in pending_sequences:
            try:
                scope = resolve_fbs_lifecycle_status_scope(
                    conn,
                    manifest=manifest,
                    status_observation_sequence=sequence,
                )
            except Exception as exc:
                if getattr(exc, "code", "") == "recovery_target_status_drift":
                    blockers.append("candidate_status_drift")
                    raw_typed_blockers.append(
                        _generic_resolution_blocker(
                            sequence=sequence,
                            error_code="recovery_target_status_drift",
                        )
                    )
                    continue
                exact_candidate = _resolve_mapping_recovery_candidate(
                    conn,
                    manifest=manifest,
                    status_observation_sequence=sequence,
                    hypothetical_mapping=hypothetical_mapping,
                )
                if exact_candidate.get("resolved_scope") is not None:
                    scope = dict(exact_candidate["resolved_scope"])
                    mapping_re_evidence.append(dict(exact_candidate["re_evidence"]))
                else:
                    blocker = exact_candidate.get("blocker")
                    if isinstance(blocker, Mapping):
                        raw_typed_blockers.append(dict(blocker))
                    continue
            group = (str(scope["facility_id"]), int(scope["nm_id"]))
            if not group[0] or group[1] <= 0:
                blockers.append("resolved_group_identity_invalid")
                continue
            resolved.append(scope)
        typed_blocker_rows = _aggregate_typed_blockers(raw_typed_blockers)
        if typed_blocker_rows:
            blockers.append("typed_identity_mapping_blockers_present")
        sequences = sorted(
            {int(item["status_observation_sequence"]) for item in resolved}
        )
        actual_groups = {
            (str(item["facility_id"]), int(item["nm_id"])) for item in resolved
        }
        if typed_blocker_rows:
            blockers.append("unresolved_scope_not_fully_resolvable")
        if not sequences:
            blockers.append("empty_recovery_target")
        target_rows = (
            _stable_target_rows(conn, tuple(sequences), cutoff=source_cursor_max)
            if sequences
            else []
        )
        stable_target_digest = _fingerprint(
            {
                "contract": CONTRACT_NAME,
                "cutover_id": cutover_id,
                "generation_id": str(generation[0][0]),
                "source_cursor_max": source_cursor_max,
                "groups": _group_rows(actual_groups),
                "rows": target_rows,
                "resolved_scopes": resolved,
                "mapping_re_evidence": _deduplicate_re_evidence(mapping_re_evidence),
                "typed_blocker_rows": typed_blocker_rows,
            }
        )
        blocked_groups = {
            (str(item["facility_id"]), int(item["nm_id"]))
            for item in typed_blocker_rows
            if str(item.get("facility_id") or "") and int(item.get("nm_id") or 0) > 0
        }
        covered_groups = actual_groups | blocked_groups
        return {
            "deployed_sha": self.deployed_sha,
            "storage": dict(storage),
            "cutover_id": cutover_id,
            "cutover_manifest_digest": _fingerprint(active["manifest"]),
            "generation_id": str(generation[0][0]),
            "generation_manifest_fingerprint": str(generation[0][1]),
            "source_cursor_max": source_cursor_max,
            "old_cursor_sequence": int(generation[0][2]),
            "target_sequences": sequences,
            "target_rows": target_rows,
            "resolved_scopes": sorted(
                resolved, key=lambda item: int(item["status_observation_sequence"])
            ),
            "mapping_re_evidence": _deduplicate_re_evidence(mapping_re_evidence),
            "hypothetical_mapping": (
                dict(hypothetical_mapping) if hypothetical_mapping is not None else None
            ),
            "typed_blocker_rows": typed_blocker_rows,
            "coverage": {
                "candidate_count": len(pending_sequences),
                "resolved_groups": _group_rows(actual_groups),
                "blocked_groups": _group_rows(blocked_groups),
                "covered_groups": _group_rows(covered_groups),
                "classified_count": len(sequences)
                + sum(int(item["status_observation_count"]) for item in typed_blocker_rows),
                "full_unresolved_scan": len(pending_sequences)
                == len(sequences)
                + sum(int(item["status_observation_count"]) for item in typed_blocker_rows),
                "all_groups_resolvable": not typed_blocker_rows,
            },
            "stable_target_digest": stable_target_digest,
            "location_wac_evidence": (
                _target_location_wac_evidence(conn, target_rows) if target_rows else []
            ),
            "past_fulfilled_invariant": _past_fulfilled_invariant(conn),
            "projection_schema_evidence": _projection_schema_evidence(conn),
            "canonical_write_seeds": _canonical_write_seeds(conn),
            "blockers": blockers,
        }

    def _preview(
        self,
        conn: sqlite3.Connection,
        *,
        source: Mapping[str, Any],
        at: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        root = _prepare_private_scratch_root(self.scratch_dir)
        descriptor, name = tempfile.mkstemp(prefix="wbc0027-", suffix=".sqlite3", dir=root)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        path = Path(name)
        scratch = sqlite3.connect(path, timeout=60.0)
        scratch.row_factory = sqlite3.Row
        try:
            scratch.execute("PRAGMA foreign_keys=OFF")
            _build_preview_projection(conn, scratch, source=source, scratch_path=path)
            hypothetical_mapping = source.get("hypothetical_mapping")
            if isinstance(hypothetical_mapping, Mapping):
                _insert_hypothetical_mapping(scratch, hypothetical_mapping)
            _append_mapping_recovery_identity_evidence(
                scratch,
                rows=list(source.get("mapping_re_evidence") or []),
                observed_at=at,
            )
            before = _balance_payload(scratch)
            sequences = tuple(int(value) for value in source["target_sequences"])
            recovery = recover_pinned_fbs_lifecycle(
                scratch,
                manifest=_active_manifest(scratch)["manifest"],
                status_observation_sequences=sequences,
                occurred_at=at,
            )
            after = _balance_payload(scratch)
            result = _target_result_payload(scratch, sequences)
            return (
                _preview_payload(
                    recovery=recovery,
                    before_balances=before,
                    after_balances=after,
                    target_result=result,
                ),
                result,
            )
        except FfPoolFbsForwardRecoveryError as exc:
            raise Wbc0027RecoveryError(exc.code, str(exc), details=exc.details) from exc
        finally:
            scratch.close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def _storage_identity(self, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        manifest = self.runtime.store_registry.load(require_files=True)
        operational = self.runtime.store_registry.generation("operational", manifest=manifest)
        if conn is None:
            with closing(_open_query_only(self.runtime.db_path)) as query:
                schema_version = int(query.execute("PRAGMA schema_version").fetchone()[0])
        else:
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        return {
            "manifest_sha256": manifest.manifest_sha256,
            "generation_epoch": manifest.generation_epoch,
            "state": manifest.state,
            "operational_generation_id": operational.generation_id,
            "operational_schema_revision": operational.schema_revision,
            "sqlite_schema_version": schema_version,
            "manifest_contract": manifest_payload(manifest)["contract_version"],
        }


def _resolve_mapping_recovery_candidate(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    status_observation_sequence: int,
    hypothetical_mapping: Mapping[str, Any] | None,
) -> dict[str, Any]:
    source = conn.execute(
        f"""SELECT status.order_id,status.order_revision,status.status_digest,
                   status.observed_at,source.source_created_at,source.warehouse_id,
                   source.office_id,source.nm_id,source.chrt_id,source.skus_json,
                   pending.deferred_identity_evidence_sequence
            FROM {IDENTITY_PENDING_TABLE} AS pending
            JOIN {STATUS_OBSERVATIONS_TABLE} AS status
              ON status.observation_sequence=pending.source_status_observation_sequence
            JOIN {OBSERVATIONS_TABLE} AS source
              ON source.order_id=status.order_id
             AND source.source_revision=status.order_revision
            LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
              ON resolution.pending_id=pending.pending_id
            WHERE pending.cutover_id=?
              AND pending.source_status_observation_sequence=?
              AND resolution.pending_id IS NULL""",
        (str(manifest["cutover_id"]), int(status_observation_sequence)),
    ).fetchone()
    if source is None:
        return {}
    try:
        barcodes = json.loads(str(source[9] or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(barcodes, list) or not barcodes:
        return {}
    evidence = conn.execute(
        f"""SELECT evidence_sequence,evidence_id,warehouse_mapping_id,
                   evidence_digest,order_revision,warehouse_id,nm_id,chrt_id,
                   barcode,seller_sku,outcome
            FROM {IDENTITY_EVIDENCE_TABLE}
            WHERE order_id=? AND order_revision=? AND warehouse_id=?
              AND nm_id=? AND chrt_id=?
            ORDER BY evidence_sequence DESC""",
        (
            int(source[0]),
            str(source[1]),
            int(source[5] or 0),
            int(source[7]),
            int(source[8] or 0),
        ),
    ).fetchall()
    exact_evidence = [
        row
        for row in evidence
        if str(row[10]) in {"matched", "unmatched_identity"}
        and str(row[8]) in {str(v) for v in barcodes}
        and str(row[9] or "")
    ]
    candidates: list[dict[str, Any]] = []
    for evidence_row in exact_evidence:
        rows = conn.execute(
            f"""SELECT mapping_id,target_nm_id,mapping_digest
                FROM {IDENTITY_MAPPINGS_TABLE}
                WHERE source_nm_id=? AND source_chrt_id=?
                  AND source_barcode=? AND source_sku=? AND active=1
                ORDER BY mapping_id""",
            (
                int(source[7]),
                int(source[8] or 0),
                str(evidence_row[8]),
                str(evidence_row[9]),
            ),
        ).fetchall()
        if len(rows) == 1:
            candidates.append(
                {
                    "evidence_row": evidence_row,
                    "mapping": {
                        "mapping_id": str(rows[0][0]),
                        "source_nm_id": int(source[7]),
                        "source_chrt_id": int(source[8] or 0),
                        "source_barcode": str(evidence_row[8]),
                        "source_sku": str(evidence_row[9]),
                        "target_nm_id": int(rows[0][1]),
                        "mapping_digest": str(rows[0][2]),
                    },
                }
            )
    if hypothetical_mapping is not None:
        hypothetical = dict(hypothetical_mapping)
        matching_evidence = [
            row
            for row in exact_evidence
            if int(source[7]) == int(hypothetical.get("source_nm_id") or 0)
            and int(source[8] or 0) == int(hypothetical.get("source_chrt_id") or 0)
            and str(row[8]) == str(hypothetical.get("source_barcode") or "")
            and str(row[9]) == str(hypothetical.get("source_sku") or "")
        ]
        candidates.extend(
            {"evidence_row": row, "mapping": hypothetical}
            for row in matching_evidence
        )
    unique = {
        (
            str(item["mapping"].get("mapping_id") or ""),
            int(item["mapping"].get("target_nm_id") or 0),
            int(item["mapping"].get("source_nm_id") or 0),
            int(item["mapping"].get("source_chrt_id") or 0),
            str(item["mapping"].get("source_barcode") or ""),
            str(item["mapping"].get("source_sku") or ""),
        ): item
        for item in sorted(candidates, key=lambda value: int(value["evidence_row"][0]))
    }
    if len(unique) != 1:
        tuple_hint = dict(hypothetical_mapping or {})
        return {
            "blocker": _mapping_blocker(
                source=source,
                status_observation_sequence=status_observation_sequence,
                facility_id="",
                identity_error_code="identity_tuple_evidence_missing_or_ambiguous",
                mapping_error_code=(
                    "order_sku_unmapped" if not unique else "active_identity_mapping_ambiguous"
                ),
                tuple_hint=tuple_hint,
            )
        }
    selected = next(iter(unique.values()))
    evidence_row = selected["evidence_row"]
    mapping = dict(selected["mapping"])
    warehouse_rows = conn.execute(
        f"""SELECT mapping.mapping_id,mapping.facility_id
            FROM {WAREHOUSE_MAPPINGS_TABLE} AS mapping
            JOIN {FACILITIES_TABLE} AS facility
              ON facility.facility_id=mapping.facility_id
            WHERE mapping.seller_warehouse_id=?
              AND mapping.active=1 AND facility.active=1
            ORDER BY mapping.mapping_id""",
        (int(source[5] or 0),),
    ).fetchall()
    facilities = {str(row[1]) for row in warehouse_rows}
    facility_id = next(iter(facilities)) if len(facilities) == 1 else ""
    valid_warehouse_ids = {
        str(row[0]) for row in warehouse_rows if str(row[1]) == facility_id
    }
    if (
        not facility_id
        or str(evidence_row[2]) not in valid_warehouse_ids
    ):
        return {
            "blocker": _mapping_blocker(
                source=source,
                status_observation_sequence=status_observation_sequence,
                facility_id=facility_id,
                identity_error_code="warehouse_identity_missing_stale_or_ambiguous",
                mapping_error_code="foreign_facility_mapping",
                tuple_hint=mapping,
            )
        }
    physical = conn.execute(
        f"""SELECT 1 FROM {BALANCES_TABLE}
            WHERE facility_id=? AND pool='FBS' AND nm_id=?
              AND projection_epoch=?""",
        (
            facility_id,
            int(mapping["target_nm_id"]),
            int(manifest["feature_epoch"]),
        ),
    ).fetchall()
    extension = conn.execute(
        f"""SELECT extension_id FROM {MAPPING_EXTENSIONS_TABLE}
            WHERE cutover_id=? AND seller_warehouse_id=?
              AND official_office_id=? AND facility_id=?
              AND warehouse_mapping_id=?
            ORDER BY extension_id""",
        (
            str(manifest["cutover_id"]),
            int(source[5] or 0),
            int(source[6] or 0),
            facility_id,
            str(evidence_row[2]),
        ),
    ).fetchall()
    allocation = (
        conn.execute(
            f"""SELECT 1 FROM {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
                WHERE extension_id=? AND nm_id=?""",
            (str(extension[0][0]), int(mapping["target_nm_id"])),
        ).fetchall()
        if len(extension) == 1
        else []
    )
    if len(physical) != 1 and len(allocation) != 1:
        return {
            "blocker": _mapping_blocker(
                source=source,
                status_observation_sequence=status_observation_sequence,
                facility_id=facility_id,
                identity_error_code="facility_sku_admission_missing",
                mapping_error_code="facility_sku_admission_missing",
                tuple_hint=mapping,
            )
        }
    resolved_scope = {
        "status_observation_sequence": int(status_observation_sequence),
        "order_id": int(source[0]),
        "source_created_at": str(source[4]),
        "source_status_observed_at": str(source[3]),
        "facility_id": facility_id,
        "nm_id": int(mapping["target_nm_id"]),
    }
    re_evidence = {
        "order_id": int(source[0]),
        "order_revision": str(source[1]),
        "warehouse_id": int(source[5] or 0),
        "nm_id": int(mapping["source_nm_id"]),
        "chrt_id": int(mapping["source_chrt_id"]),
        "barcode": str(mapping["source_barcode"]),
        "seller_sku": str(mapping["source_sku"]),
        "warehouse_mapping_id": str(evidence_row[2]),
        "identity_mapping_id": str(mapping["mapping_id"]),
        "source_identity_evidence_sequence": int(evidence_row[0]),
        "source_identity_evidence_digest": str(evidence_row[3]),
    }
    return {"resolved_scope": resolved_scope, "re_evidence": re_evidence}


def _mapping_blocker(
    *,
    source: Sequence[Any],
    status_observation_sequence: int,
    facility_id: str,
    identity_error_code: str,
    mapping_error_code: str,
    tuple_hint: Mapping[str, Any],
) -> dict[str, Any]:
    target_nm_id = int(tuple_hint.get("target_nm_id") or 0)
    tuple_digest = str(tuple_hint.get("tuple_digest") or tuple_hint.get("mapping_digest") or "")
    return {
        "facility_id": str(facility_id),
        "nm_id": target_nm_id,
        "identity_error_code": str(identity_error_code),
        "mapping_error_code": str(mapping_error_code),
        "external_identity_digest": str(tuple_hint.get("external_identity_digest") or ""),
        "tuple_digest": tuple_digest,
        "order_id": int(source[0]),
        "status_observation_sequence": int(status_observation_sequence),
        "status_digest": str(source[2]),
    }


def _generic_resolution_blocker(*, sequence: int, error_code: str) -> dict[str, Any]:
    return {
        "facility_id": "",
        "nm_id": 0,
        "identity_error_code": str(error_code),
        "mapping_error_code": "not_applicable",
        "external_identity_digest": "",
        "tuple_digest": "",
        "order_id": 0,
        "status_observation_sequence": int(sequence),
        "status_digest": "",
    }


def _aggregate_typed_blockers(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int, str, str, str, str], dict[str, Any]] = {}
    for raw in rows:
        item = dict(raw)
        key = (
            str(item.get("facility_id") or ""),
            int(item.get("nm_id") or 0),
            str(item.get("identity_error_code") or ""),
            str(item.get("mapping_error_code") or ""),
            str(item.get("external_identity_digest") or ""),
            str(item.get("tuple_digest") or ""),
        )
        group = grouped.setdefault(
            key,
            {
                "orders": set(),
                "statuses": {},
            },
        )
        if int(item.get("order_id") or 0) > 0:
            group["orders"].add(int(item["order_id"]))
        group["statuses"][int(item["status_observation_sequence"])] = str(
            item.get("status_digest") or ""
        )
    result: list[dict[str, Any]] = []
    for key, evidence in sorted(grouped.items()):
        orders = sorted(evidence["orders"])
        statuses = [
            {"status_observation_sequence": sequence, "status_digest": digest}
            for sequence, digest in sorted(evidence["statuses"].items())
        ]
        result.append(
            {
                "facility_id": key[0],
                "nm_id": key[1],
                "identity_error_code": key[2],
                "mapping_error_code": key[3],
                "external_identity_digest": key[4],
                "tuple_digest": key[5],
                "order_count": len(orders),
                "status_observation_count": len(statuses),
                "order_identity_digest": _fingerprint(orders),
                "status_identity_digest": _fingerprint(statuses),
            }
        )
    return result


def _deduplicate_re_evidence(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_revision_identity: dict[tuple[Any, ...], dict[str, Any]] = {}
    for raw in rows:
        item = dict(raw)
        key = (
            int(item["order_id"]),
            str(item["order_revision"]),
            int(item["warehouse_id"]),
            int(item["nm_id"]),
            int(item["chrt_id"]),
            str(item["barcode"]),
            str(item["seller_sku"]),
        )
        previous = by_revision_identity.get(key)
        if previous is None or int(item["source_identity_evidence_sequence"]) > int(
            previous["source_identity_evidence_sequence"]
        ):
            by_revision_identity[key] = item
    return [by_revision_identity[key] for key in sorted(by_revision_identity)]


def _append_mapping_recovery_identity_evidence(
    conn: sqlite3.Connection,
    *,
    rows: Sequence[Mapping[str, Any]],
    observed_at: str,
) -> int:
    inserted = 0
    for raw in rows:
        item = dict(raw)
        material = {
            "contract": "fbs_lifecycle_mapping_re_evidence/v2",
            "order_id": int(item["order_id"]),
            "order_revision": str(item["order_revision"]),
            "warehouse_id": int(item["warehouse_id"]),
            "nm_id": int(item["nm_id"]),
            "chrt_id": int(item["chrt_id"]),
            "barcode": str(item["barcode"]),
            "seller_sku": str(item["seller_sku"]),
            "warehouse_mapping_id": str(item["warehouse_mapping_id"]),
            "identity_mapping_id": str(item["identity_mapping_id"]),
            "outcome": "matched",
            "source_identity_evidence_sequence": int(
                item["source_identity_evidence_sequence"]
            ),
            "source_identity_evidence_digest": str(
                item["source_identity_evidence_digest"]
            ),
        }
        digest = _fingerprint(material)
        inserted += int(
            conn.execute(
                f"""INSERT OR IGNORE INTO {IDENTITY_EVIDENCE_TABLE}(
                       evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                       barcode,seller_sku,outcome,warehouse_mapping_id,
                       identity_mapping_id,evidence_digest,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?,'matched',?,?,?,?)""",
                (
                    "fbs_map_" + digest.removeprefix("sha256:")[:32],
                    material["order_id"],
                    material["order_revision"],
                    material["warehouse_id"],
                    material["nm_id"],
                    material["chrt_id"],
                    material["barcode"],
                    material["seller_sku"],
                    material["warehouse_mapping_id"],
                    material["identity_mapping_id"],
                    digest,
                    observed_at,
                ),
            ).rowcount
        )
    return inserted


def _insert_hypothetical_mapping(
    conn: sqlite3.Connection,
    mapping: Mapping[str, Any],
) -> None:
    item = dict(mapping)
    conn.execute(
        f"""INSERT INTO {IDENTITY_MAPPINGS_TABLE}(
               mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,
               target_nm_id,mapping_digest,active,created_at,created_by
           ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
        (
            str(item["mapping_id"]),
            int(item["source_nm_id"]),
            int(item["source_chrt_id"]),
            str(item["source_barcode"]),
            str(item["source_sku"]),
            int(item["target_nm_id"]),
            str(item["mapping_digest"]),
            str(item.get("created_at") or "2026-08-31T00:00:00Z"),
            str(item.get("created_by") or "query-only-hypothetical-rehearsal"),
        ),
    )


def _group_rows(groups: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    return [
        {"facility_id": facility_id, "nm_id": nm_id}
        for facility_id, nm_id in sorted(groups)
    ]


def _impact_scan(source: Mapping[str, Any]) -> dict[str, Any]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for raw in source.get("resolved_scopes") or []:
        item = dict(raw)
        grouped.setdefault(
            (str(item["facility_id"]), int(item["nm_id"])), []
        ).append(item)
    classifications: list[dict[str, Any]] = []
    for (facility_id, nm_id), rows in sorted(grouped.items()):
        sequences = sorted(int(item["status_observation_sequence"]) for item in rows)
        earliest = min(
            _business_date(str(item[field]))
            for item in rows
            for field in ("source_created_at", "source_status_observed_at")
        )
        classifications.append(
            {
                "facility_id": facility_id,
                "nm_id": nm_id,
                "classification": "resolvable_exact",
                "earliest_business_date": earliest,
                "reasons": [],
                "sequence_count": len(sequences),
                "sequence_digest": _fingerprint(sequences),
            }
        )
    for raw in source.get("typed_blocker_rows") or []:
        item = dict(raw)
        classifications.append(
            {
                "facility_id": str(item.get("facility_id") or ""),
                "nm_id": int(item.get("nm_id") or 0),
                "classification": "blocked_fail_closed",
                "earliest_business_date": "",
                "reasons": sorted(
                    {
                        str(item.get("identity_error_code") or ""),
                        str(item.get("mapping_error_code") or ""),
                    }
                    - {""}
                ),
                "sequence_count": int(item.get("status_observation_count") or 0),
                "sequence_digest": str(item.get("status_identity_digest") or ""),
            }
        )
    classifications.sort(
        key=lambda item: (
            str(item["facility_id"]),
            int(item["nm_id"]),
            str(item["classification"]),
        )
    )
    result = {
        "source_cursor_max": int(source["source_cursor_max"]),
        "candidate_count": int(dict(source["coverage"])["candidate_count"]),
        "classified_count": int(dict(source["coverage"])["classified_count"]),
        "full_scan": bool(dict(source["coverage"])["full_unresolved_scan"]),
        "classifications": classifications,
    }
    result["classification_digest"] = _fingerprint(classifications)
    return result


def _affected_group_rows(
    groups: Iterable[tuple[str, int]],
) -> list[dict[str, Any]]:
    values = sorted(set(groups))
    rows = [
        {"scope_kind": "FACILITY_SKU", "facility_id": facility_id, "nm_id": nm_id}
        for facility_id, nm_id in values
    ]
    rows.extend(
        {
            "scope_kind": "FACILITY_TOTAL",
            "facility_id": facility_id,
            "nm_id": 0,
        }
        for facility_id in sorted({facility_id for facility_id, _ in values})
    )
    rows.extend(
        {"scope_kind": "GLOBAL_SKU", "facility_id": "", "nm_id": nm_id}
        for nm_id in sorted({nm_id for _, nm_id in values})
    )
    rows.append({"scope_kind": "GLOBAL_TOTAL", "facility_id": "", "nm_id": 0})
    return rows


def _history_plan(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    target_result: Sequence[Mapping[str, Any]],
    generated_at: str,
    contract_name: str,
) -> dict[str, Any]:
    current_date = current_business_date_iso(
        datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    )
    evidence_dates = [
        _business_date(str(item[field]))
        for item in source["resolved_scopes"]
        for field in ("source_created_at", "source_status_observed_at")
    ]
    date_from = min(evidence_dates) if evidence_dates else current_date
    date_to = current_date
    dates = _date_strings(date_from, date_to) if date_from <= date_to else []
    corrections, event_evidence = _history_corrections(
        conn,
        source=source,
        target_result=target_result,
        dates=dates,
    )
    blockers: list[str] = []
    captures: list[dict[str, Any]] = []
    cell_evidence: list[dict[str, Any]] = []
    stable_target_digest = str(source["stable_target_digest"])
    if not dates:
        blockers.append("history_date_boundary_invalid")
    for business_date in dates:
        capture = _latest_capture(conn, business_date=business_date, current_date=current_date)
        if capture is None:
            blockers.append(f"history_base_missing:{business_date}")
            continue
        rows = [
            _stored_component(row)
            for row in conn.execute(
                f"""SELECT scope_kind,scope_key,nm_id,component_kind,component_id,
                           component_label,state,quantity,source_revision,source_digest,
                           source_watermark,provenance_json
                    FROM {COMPONENTS_TABLE} WHERE capture_id=?
                    ORDER BY scope_kind,scope_key,component_kind,component_id""",
                (str(capture["capture_id"]),),
            ).fetchall()
        ]
        by_key = {
            (
                str(item["scope_key"]),
                str(item["component_kind"]),
                str(item["component_id"]),
            ): item
            for item in rows
        }
        date_corrections = {
            group: int(value)
            for (date_value, group), value in corrections.items()
            if date_value == business_date
        }
        facility_total_delta: dict[str, int] = {}
        for (facility_id, nm_id), delta in date_corrections.items():
            key = (f"SKU:{nm_id}", "FBS_FACILITY", facility_id)
            component = by_key.get(key)
            if component is None or component["state"] not in {"exact", "exact_zero"}:
                cell_evidence.append(
                    {
                        "business_date": business_date,
                        "scope_kind": "SKU",
                        "facility_id": facility_id,
                        "nm_id": nm_id,
                        "classification": "remain_missing_no_same_date_evidence",
                        "base_state": (
                            str(component.get("state") or "missing")
                            if component is not None
                            else "missing"
                        ),
                        "evidence_digest": _fingerprint(
                            {
                                "business_date": business_date,
                                "facility_id": facility_id,
                                "nm_id": nm_id,
                                "base_state": (
                                    str(component.get("state") or "missing")
                                    if component is not None
                                    else "missing"
                                ),
                            }
                        ),
                    }
                )
                facility_total_delta[facility_id] = (
                    facility_total_delta.get(facility_id, 0) + delta
                )
                continue
            component["quantity"] = int(component["quantity"]) + delta
            component["state"] = "exact_zero" if int(component["quantity"]) == 0 else "exact"
            facility_total_delta[facility_id] = facility_total_delta.get(facility_id, 0) + delta
            _mark_recovered_component(
                component,
                business_date=business_date,
                stable_target_digest=stable_target_digest,
                event_evidence=event_evidence,
                source_cursor_max=int(source["source_cursor_max"]),
                contract_name=contract_name,
            )
            cell_evidence.append(
                {
                    "business_date": business_date,
                    "scope_kind": "SKU",
                    "facility_id": facility_id,
                    "nm_id": nm_id,
                    "classification": "recoverable_exact",
                    "base_state": str(component["state"]),
                    "evidence_digest": str(component["source_digest"]),
                }
            )
        for facility_id, delta in facility_total_delta.items():
            key = ("TOTAL", "FBS_FACILITY", facility_id)
            component = by_key.get(key)
            if component is None or component["state"] not in {"exact", "exact_zero"}:
                cell_evidence.append(
                    {
                        "business_date": business_date,
                        "scope_kind": "FACILITY_TOTAL",
                        "facility_id": facility_id,
                        "nm_id": 0,
                        "classification": "remain_missing_no_same_date_evidence",
                        "base_state": (
                            str(component.get("state") or "missing")
                            if component is not None
                            else "missing"
                        ),
                        "evidence_digest": _fingerprint(
                            {
                                "business_date": business_date,
                                "facility_id": facility_id,
                                "scope_kind": "FACILITY_TOTAL",
                            }
                        ),
                    }
                )
                continue
            component["quantity"] = int(component["quantity"]) + delta
            component["state"] = "exact_zero" if int(component["quantity"]) == 0 else "exact"
            _mark_recovered_component(
                component,
                business_date=business_date,
                stable_target_digest=stable_target_digest,
                event_evidence=event_evidence,
                source_cursor_max=int(source["source_cursor_max"]),
                contract_name=contract_name,
            )
            cell_evidence.append(
                {
                    "business_date": business_date,
                    "scope_kind": "FACILITY_TOTAL",
                    "facility_id": facility_id,
                    "nm_id": 0,
                    "classification": "recoverable_exact",
                    "base_state": str(component["state"]),
                    "evidence_digest": str(component["source_digest"]),
                }
            )
        roster = json.loads(str(capture["facility_roster_json"]))
        source_manifest = {
            "contract": contract_name,
            "source_cursor_max": int(source["source_cursor_max"]),
            "business_date": business_date,
            "base_capture_id": str(capture["capture_id"]),
            "base_source_digest": str(capture["source_digest"]),
            "stable_target_digest": stable_target_digest,
            "event_evidence_digest": _fingerprint(event_evidence),
            "correction_digest": _fingerprint(
                [
                    {"facility_id": group[0], "nm_id": group[1], "delta": delta}
                    for group, delta in sorted(date_corrections.items())
                ]
            ),
            "derivation": "same_date_source_order_and_status_lifecycle_supersession",
            "current_retrocopy": False,
        }
        preview = preview_inventory_history_capture(
            business_date=business_date,
            capture_kind="historical_backfill",
            formula_version=str(capture["formula_version"]),
            facility_roster=roster,
            source_manifest=source_manifest,
            components=rows,
            captured_at=generated_at,
        )
        prior_finalization = conn.execute(
            f"""SELECT finalization_digest FROM {FINALIZATIONS_TABLE}
                WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1""",
            (business_date,),
        ).fetchone()
        captures.append(
            {
                "business_date": business_date,
                "base_capture_id": str(capture["capture_id"]),
                "base_source_digest": str(capture["source_digest"]),
                "formula_version": str(capture["formula_version"]),
                "facility_roster": preview["facility_roster"],
                "source_manifest": preview["source_manifest"],
                "components": preview["components"],
                "capture_id": preview["capture_id"],
                "source_digest": preview["source_digest"],
                "finalize": business_date < current_date,
                "finalization_identity": (
                    f"wbc0027:{stable_target_digest}:{business_date}"
                ),
                "supersedes_finalization_digest": (
                    str(prior_finalization[0]) if prior_finalization is not None else ""
                ),
            }
        )
    material = {
        "contract": "fbs_same_date_inventory_history_supersession/v2",
        "date_from": date_from,
        "date_to": date_to,
        "business_dates": dates,
        "current_business_date": current_date,
        "event_evidence": event_evidence,
        "corrections": [
            {
                "business_date": date_value,
                "facility_id": group[0],
                "nm_id": group[1],
                "available_quantity_delta": delta,
            }
            for (date_value, group), delta in sorted(corrections.items())
        ],
        "captures": captures,
        "cell_evidence": sorted(
            cell_evidence,
            key=lambda item: (
                str(item["business_date"]),
                str(item["facility_id"]),
                str(item["scope_kind"]),
                int(item["nm_id"]),
            ),
        ),
        "classification_counts": {
            "recoverable_exact": sum(
                item["classification"] == "recoverable_exact" for item in cell_evidence
            ),
            "remain_missing_no_same_date_evidence": sum(
                item["classification"] == "remain_missing_no_same_date_evidence"
                for item in cell_evidence
            ),
        },
        "blockers": sorted(set(blockers)),
    }
    material["evidence_digest"] = _fingerprint(material["cell_evidence"])
    material["digest"] = _fingerprint(_stable_plan_material(material))
    return material


def _history_corrections(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    target_result: Sequence[Mapping[str, Any]],
    dates: Sequence[str],
) -> tuple[dict[tuple[str, tuple[str, int]], int], list[dict[str, Any]]]:
    scopes = {
        int(item["status_observation_sequence"]): (
            str(item["facility_id"]),
            int(item["nm_id"]),
        )
        for item in source["resolved_scopes"]
    }
    target_by_sequence = {
        int(item["status_observation_sequence"]): dict(item)
        for item in source["target_rows"]
    }
    corrections: dict[tuple[str, tuple[str, int]], int] = {}
    evidence: list[dict[str, Any]] = []
    for raw in target_result:
        result = dict(raw)
        sequence = int(result["status_observation_sequence"])
        target = target_by_sequence[sequence]
        group = scopes[sequence]
        source_created_at = str(target["source_created_at"])
        status_row = conn.execute(
            f"SELECT observed_at,status_digest FROM {STATUS_OBSERVATIONS_TABLE} WHERE observation_sequence=?",
            (sequence,),
        ).fetchone()
        if status_row is None:
            raise Wbc0027RecoveryError(
                "history_source_status_missing", "Target status evidence disappeared"
            )
        status_observed_at = str(status_row[0])
        events = [dict(item) for item in result.get("events") or []]
        if not events and str(result.get("outcome")) not in {"already_current", "audit_noop"}:
            raise Wbc0027RecoveryError(
                "history_event_effect_missing", "Target has no exact lifecycle event effect"
            )
        reserved = 0
        physical = 0
        timeline: list[tuple[str, int, int]] = []
        for index, event in enumerate(events):
            event_type = str(event["event_type"])
            effective_at = (
                source_created_at
                if event_type in {"opening_reserve", "reserve"}
                else status_observed_at
            )
            business_date = _business_date(effective_at)
            if event_type in {"opening_reserve", "reserve"}:
                reserved = int(event["quantity"])
            elif event_type == "release":
                reserved = 0
            elif event_type in {"handoff_debit", "opening_handoff_debit"}:
                physical += int(event["physical_quantity_delta"])
                reserved = 0
            timeline.append((business_date, index, physical - reserved))
            evidence.append(
                {
                    "status_observation_sequence": sequence,
                    "status_digest": str(status_row[1]),
                    "event_id": str(event["event_id"]),
                    "event_type": event_type,
                    "facility_id": group[0],
                    "nm_id": group[1],
                    "effective_business_date": business_date,
                    "quantity": int(event["quantity"]),
                    "physical_quantity_delta": int(event["physical_quantity_delta"]),
                    "evidence_digest": str(event["evidence_digest"]),
                }
            )
        for date_value in dates:
            contribution = 0
            for event_date, _, value in timeline:
                if event_date <= date_value:
                    contribution = value
            corrections[(date_value, group)] = (
                corrections.get((date_value, group), 0) + contribution
            )
    evidence.sort(
        key=lambda item: (
            int(item["status_observation_sequence"]),
            str(item["event_id"]),
        )
    )
    return corrections, evidence


def _latest_capture(
    conn: sqlite3.Connection, *, business_date: str, current_date: str
) -> sqlite3.Row | None:
    row = conn.execute(
        f"""SELECT capture.* FROM {FINALIZATIONS_TABLE} finalization
            JOIN {CAPTURES_TABLE} capture ON capture.capture_id=finalization.capture_id
            WHERE finalization.business_date=?
            ORDER BY finalization.finalization_sequence DESC LIMIT 1""",
        (business_date,),
    ).fetchone()
    if row is None and business_date == current_date:
        row = conn.execute(
            f"""SELECT * FROM {CAPTURES_TABLE} WHERE business_date=?
                ORDER BY capture_sequence DESC LIMIT 1""",
            (business_date,),
        ).fetchone()
    return row


def _stored_component(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "scope_kind": str(row[0]),
        "scope_key": str(row[1]),
        "nm_id": row[2],
        "component_kind": str(row[3]),
        "component_id": str(row[4]),
        "component_label": str(row[5]),
        "state": str(row[6]),
        "quantity": row[7],
        "source_revision": str(row[8]),
        "source_digest": str(row[9]),
        "source_watermark": str(row[10]),
        "provenance": json.loads(str(row[11] or "{}")),
    }


def _mark_recovered_component(
    component: dict[str, Any],
    *,
    business_date: str,
    stable_target_digest: str,
    event_evidence: Sequence[Mapping[str, Any]],
    source_cursor_max: int,
    contract_name: str,
) -> None:
    digest = _fingerprint(
        {
            "business_date": business_date,
            "stable_target_digest": stable_target_digest,
            "events": list(event_evidence),
        }
    )
    component["source_revision"] = f"wbc0027:{business_date}"
    component["source_digest"] = digest
    component["source_watermark"] = str(source_cursor_max)
    component["provenance"] = {
        **dict(component.get("provenance") or {}),
        "recovery_contract": contract_name,
        "same_date_source_reconstruction": True,
        "current_retrocopy": False,
        "stable_target_digest": stable_target_digest,
        "event_evidence_digest": _fingerprint(event_evidence),
    }


def _apply_history(
    conn: sqlite3.Connection,
    *,
    reviewed_history: Mapping[str, Any],
    recovery_fingerprint: str,
    deployed_sha: str,
    approval_reference: str,
    applied_at: str,
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for raw in reviewed_history.get("captures") or []:
        item = dict(raw)
        current_base = _latest_capture(
            conn,
            business_date=str(item["business_date"]),
            current_date=str(reviewed_history["current_business_date"]),
        )
        if (
            current_base is None
            or str(current_base["capture_id"]) != str(item["base_capture_id"])
            or str(current_base["source_digest"]) != str(item["base_source_digest"])
        ):
            raise Wbc0027RecoveryError(
                "history_base_cas_drift", "History base changed after review"
            )
        capture = append_inventory_history_capture(
            conn,
            business_date=str(item["business_date"]),
            capture_kind="historical_backfill",
            formula_version=str(item["formula_version"]),
            facility_roster=list(item["facility_roster"]),
            source_manifest=dict(item["source_manifest"]),
            components=list(item["components"]),
            captured_at=applied_at,
            generation_identity=recovery_fingerprint,
        )
        if (
            str(capture["capture_id"]) != str(item["capture_id"])
            or str(capture["source_digest"]) != str(item["source_digest"])
        ):
            raise Wbc0027RecoveryError(
                "history_capture_identity_drift", "Normalized history capture drifted"
            )
        finalization = {
            "finalization_id": "",
            "finalization_digest": "",
            "supersedes_finalization_digest": "",
            "inserted": False,
        }
        if bool(item["finalize"]):
            finalization = append_inventory_history_finalization(
                conn,
                business_date=str(item["business_date"]),
                capture_id=str(capture["capture_id"]),
                finalization_identity=str(item["finalization_identity"]),
                finalized_at=applied_at,
                provenance={
                    "contract": CONTRACT_NAME,
                    "manifest_fingerprint": recovery_fingerprint,
                    "deployed_sha": deployed_sha,
                    "approval_reference": approval_reference,
                    "same_date_source_reconstruction": True,
                },
            )
            if str(finalization["supersedes_finalization_digest"]) != str(
                item["supersedes_finalization_digest"]
            ):
                raise Wbc0027RecoveryError(
                    "history_finalization_cas_drift",
                    "Superseded history finalization changed after review",
                )
        receipts.append(
            {
                "business_date": str(item["business_date"]),
                "capture_id": str(capture["capture_id"]),
                "source_digest": str(capture["source_digest"]),
                "capture_inserted": bool(capture["inserted"]),
                "finalization_id": str(finalization["finalization_id"]),
                "finalization_digest": str(finalization["finalization_digest"]),
                "supersedes_finalization_digest": str(
                    finalization["supersedes_finalization_digest"]
                ),
                "finalization_inserted": bool(finalization["inserted"]),
            }
        )
    return receipts


def _non_target_digest(
    conn: sqlite3.Connection,
    *,
    target_groups: Iterable[tuple[str, int]],
) -> str:
    groups = sorted(set(target_groups))
    target_predicates = " OR ".join("(facility_id=? AND nm_id=?)" for _ in groups)
    where = f"pool<>'FBS' OR NOT ({target_predicates})" if groups else "1=1"
    balance_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT facility_id,pool,nm_id,quantity,capital_rub,wac_rub,
                       projection_epoch,updated_at
                FROM sheet_vitrina_v1_ff_pool_balances
                WHERE {where}
                ORDER BY facility_id,pool,nm_id""",
            tuple(value for group in groups for value in group),
        ).fetchall()
    ]
    # The recovery appends target captures.  Existing immutable history and all
    # non-target component identities must remain byte-for-byte represented.
    target_skus = set(groups)
    target_facilities = {facility_id for facility_id, _ in groups}
    history_rows: list[dict[str, Any]] = []
    for row in conn.execute(
            f"""SELECT component.capture_id,component.scope_kind,
                       component.scope_key,component.nm_id,
                       component.component_kind,component.component_id,
                       component.state,component.quantity,
                       component.source_revision,component.source_digest,
                       component.source_watermark,component.provenance_json
                FROM {COMPONENTS_TABLE} AS component
                JOIN {CAPTURES_TABLE} AS capture USING(capture_id)
                WHERE COALESCE(json_extract(
                          capture.source_manifest_json,'$.contract'
                      ),'')<>?
                ORDER BY component.capture_id,component.scope_kind,
                         component.scope_key,component.component_kind,
                         component.component_id""",
            (CONTRACT_NAME,),
        ).fetchall():
        item = dict(row)
        facility_id = str(item["component_id"])
        nm_id = int(item["nm_id"] or 0)
        target = (
            str(item["component_kind"]) == "FBS_FACILITY"
            and (
                (facility_id, nm_id) in target_skus
                or (str(item["scope_key"]) == "TOTAL" and facility_id in target_facilities)
            )
        )
        if not target:
            history_rows.append(item)
    return _fingerprint({"balances": balance_rows, "history": history_rows})


def _wb_baseline_digest(conn: sqlite3.Connection) -> str:
    rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT capture_id,scope_kind,scope_key,nm_id,component_kind,
                       component_id,state,quantity,source_revision,source_digest,
                       source_watermark
                FROM {COMPONENTS_TABLE}
                WHERE component_kind LIKE 'WB%'
                ORDER BY capture_id,scope_kind,scope_key,component_kind,component_id"""
        ).fetchall()
    ]
    return _fingerprint(rows)


def _verify_reviewed_source(reviewed: Mapping[str, Any], source: Mapping[str, Any]) -> None:
    boundary = dict(reviewed["boundary"])
    scope = dict(reviewed["scope"])
    checks = {
        "cutover_id": str(source["cutover_id"]) == str(boundary["cutover_id"]),
        "cutover_manifest_digest": str(source["cutover_manifest_digest"])
        == str(boundary["cutover_manifest_digest"]),
        "generation_id": str(source["generation_id"])
        == str(boundary["forward_generation_id"]),
        "generation_manifest_fingerprint": str(source["generation_manifest_fingerprint"])
        == str(boundary["forward_generation_manifest_digest"]),
        "cursor": int(source["old_cursor_sequence"])
        == int(boundary["forward_cursor_sequence"]),
        "source_cursor_max": int(source["source_cursor_max"])
        == int(boundary["source_cursor_max"]),
        "target_sequences": list(source["target_sequences"])
        == list(scope["target_sequences"]),
        "stable_target_digest": str(source["stable_target_digest"])
        == str(scope["stable_target_digest"]),
        "target_rows": list(source["target_rows"]) == list(scope["target_rows"]),
        "resolved_scopes": list(source["resolved_scopes"])
        == list(scope["resolved_scopes"]),
        "mapping_re_evidence": list(source["mapping_re_evidence"])
        == list(scope.get("mapping_re_evidence") or []),
        "typed_blocker_rows": list(source["typed_blocker_rows"])
        == list(scope.get("typed_blocker_rows") or []),
        "coverage": dict(source["coverage"]) == dict(scope.get("coverage") or {}),
        "blockers": not source["blockers"],
    }
    if not all(checks.values()):
        raise Wbc0027RecoveryError(
            "target_source_drift", "Pinned recovery source changed", details=checks
        )


def _date_strings(start: str, end: str) -> list[str]:
    start_date = datetime.fromisoformat(start).date()
    end_date = datetime.fromisoformat(end).date()
    values: list[str] = []
    current = start_date
    while current <= end_date:
        values.append(current.isoformat())
        current = current.fromordinal(current.toordinal() + 1)
    return values


def _business_date(timestamp: str) -> str:
    try:
        value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError as exc:
        raise Wbc0027RecoveryError(
            "invalid_source_timestamp", "Lifecycle source timestamp is invalid"
        ) from exc
    return current_business_date_iso(value)


def _open_query_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise Wbc0027RecoveryError("query_only_not_enforced", "SQLite query-only failed")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    return _fingerprint(_stable_plan_material(plan))


def _stable_plan_material(value: Any) -> Any:
    """Remove observation-time metadata while preserving business evidence."""

    if isinstance(value, Mapping):
        return {
            str(key): _stable_plan_material(child)
            for key, child in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in {"fingerprint", "generated_at", "captured_at"}
        }
    if isinstance(value, list):
        return [_stable_plan_material(item) for item in value]
    if isinstance(value, tuple):
        return [_stable_plan_material(item) for item in value]
    return value


def _require_digest(value: str, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise Wbc0027RecoveryError("invalid_digest", f"{field} must be sha256:<64hex>")
    return normalized


def _require_utc(value: str) -> None:
    if not str(value).endswith("Z"):
        raise Wbc0027RecoveryError("invalid_timestamp", "timestamp must be UTC Z")
    datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_private(path: Path, payload: Mapping[str, Any]) -> None:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _read_plan(path: str) -> dict[str, Any]:
    try:
        return parse_recovery_manifest(read_manifest_json(Path(path)))
    except FbsManifestError as exc:
        raise Wbc0027RecoveryError(exc.code, str(exc), details=exc.details) from exc


def run(args: argparse.Namespace) -> int:
    runner = Wbc0027FbsLifecycleQualityRecovery(
        runtime_dir=Path(args.runtime_dir),
        deployed_sha=str(args.deployed_sha),
        incident_passport=read_manifest_json(Path(args.passport_file)),
        scratch_dir=(Path(args.scratch_dir) if args.scratch_dir else None),
    )
    if args.command == "apply":
        payload = runner.apply(
            _read_plan(args.plan_file),
            fingerprint=str(args.fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
            evidence_dir=Path(args.evidence_dir),
        )
    elif args.command == "readback":
        payload = runner.readback(fingerprint=str(args.fingerprint or ""))
    elif args.command == "impact-dry-run":
        mapping_readback = read_manifest_json(Path(args.mapping_readback_file))
        mapping_digest = str(mapping_readback.get("readback_digest") or "")
        payload = runner.build_impact_manifest(
            mapping_readback_digest=mapping_digest,
        )
    else:
        mapping_digest = ""
        if str(getattr(args, "mapping_readback_file", "") or ""):
            mapping_readback = read_manifest_json(Path(args.mapping_readback_file))
            mapping_digest = str(mapping_readback.get("readback_digest") or "")
        payload = runner.build_plan(mapping_readback_digest=mapping_digest)
    if args.output:
        _write_private(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") not in {"blocked", "error"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--passport-file", required=True)
    parser.add_argument("--scratch-dir", default="")
    parser.add_argument("--output", default="")
    sub = parser.add_subparsers(dest="command")
    dry_run = sub.add_parser("dry-run")
    dry_run.add_argument("--mapping-readback-file", default="")
    impact = sub.add_parser("impact-dry-run")
    impact.add_argument("--mapping-readback-file", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--plan-file", required=True)
    apply.add_argument("--fingerprint", required=True)
    apply.add_argument("--approval-reference", required=True)
    apply.add_argument("--actor", required=True)
    apply.add_argument("--evidence-dir", required=True)
    readback = sub.add_parser("readback")
    readback.add_argument("--fingerprint", default="")
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if args.command is None:
            args.command = "dry-run"
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
