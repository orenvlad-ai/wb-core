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
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    QUALITY_RECOVERY_HISTORY_TABLE,
    QUALITY_RECOVERY_RUNS_TABLE,
    QUALITY_RECOVERY_TARGETS_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    recover_pinned_fbs_lifecycle,
    resolve_fbs_lifecycle_status_scope,
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
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
)
from packages.business_time import current_business_date_iso  # noqa: E402


CONTRACT_NAME = "wbc0027_fbs_lifecycle_quality_recovery_v1"
CONTRACT_VERSION = 1
SOURCE_CUTOFF_SEQUENCE = 28_050_157
DATE_FROM = "2026-08-17"
DATE_TO = "2026-08-31"
MOSCOW_FACILITY_ID = "fff_d67e8c823d5f81dd988d00dbfea6"
ORENBURG_FACILITY_ID = "fff_2579bb2741ed4ab23b11bb4c4183"
TARGET_GROUPS = (
    (MOSCOW_FACILITY_ID, 210183919),
    (MOSCOW_FACILITY_ID, 428855560),
    (MOSCOW_FACILITY_ID, 428855758),
    (ORENBURG_FACILITY_ID, 428855758),
)
TARGET_GROUP_SET = frozenset(TARGET_GROUPS)
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
        self.timestamp_factory = timestamp_factory or _utc_now
        self.scratch_dir = Path(
            scratch_dir
            if scratch_dir is not None
            else self.runtime.runtime_dir / "wbc0027-fbs-quality-recovery-scratch"
        ).expanduser()

    def build_plan(self) -> dict[str, Any]:
        generated_at = str(self.timestamp_factory())
        _require_utc(generated_at)
        storage = self._storage_identity()
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            conn.execute("BEGIN")
            try:
                source = self._source_snapshot(conn, storage=storage)
                preview, target_result = self._preview(conn, source=source, at=generated_at)
                history = _history_plan(
                    conn,
                    source=source,
                    target_result=target_result,
                    generated_at=generated_at,
                )
                non_target = _non_target_digest(conn)
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
        if not delta_groups.issubset(TARGET_GROUP_SET):
            blockers.append("predicted_non_target_balance_effect")
        if preview.get("wb_write_count") != 0:
            blockers.append("predicted_wb_write")
        if any(str(row["outcome"]) == "identity_quarantine" for row in target_result):
            blockers.append("target_remains_identity_quarantined")
        if history["blockers"]:
            blockers.extend(str(value) for value in history["blockers"])
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "generated_at": generated_at,
            "deployed_sha": self.deployed_sha,
            "storage": storage,
            "boundary": {
                "cutover_id": source["cutover_id"],
                "cutover_manifest_digest": source["cutover_manifest_digest"],
                "forward_generation_id": source["generation_id"],
                "forward_generation_manifest_fingerprint": source[
                    "generation_manifest_fingerprint"
                ],
                "forward_cursor_sequence": source["old_cursor_sequence"],
                "source_cutoff_sequence": SOURCE_CUTOFF_SEQUENCE,
                "date_from": DATE_FROM,
                "date_to": DATE_TO,
            },
            "scope": {
                "groups": [
                    {"facility_id": facility_id, "nm_id": nm_id}
                    for facility_id, nm_id in TARGET_GROUPS
                ],
                "dates": _date_strings(DATE_FROM, DATE_TO),
                "target_count": len(source["target_sequences"]),
                "status_observation_sequences": source["target_sequences"],
                "stable_target_digest": source["stable_target_digest"],
                "target_rows": source["target_rows"],
                "location_wac_evidence": source["location_wac_evidence"],
                "resolved_scopes": source["resolved_scopes"],
            },
            "predicted_effects": preview,
            "history": history,
            "source_evidence": {
                "projection_schema_evidence": source["projection_schema_evidence"],
                "canonical_write_seeds": source["canonical_write_seeds"],
                "past_fulfilled_invariant": source["past_fulfilled_invariant"],
                "non_target_digest": non_target,
            },
            "safety": {
                "default_mode": "query_only_dry_run",
                "one_submit": True,
                "writer_lock": "warehouse_functional_write_lock",
                "target_cas": "exact_source_rows_history_base_and_effect",
                "before_image": "private_mode_0600",
                "recovery": "sqlite_atomic_rollback_and_append_only_supersession",
                "ambiguous_transport": "query_only_readback_before_any_retry",
                "current_retrocopy": False,
                "immutable_history_overwrite": False,
                "wb_writes": 0,
            },
            "apply_allowed": not blockers,
            "blockers": sorted(set(blockers)),
        }
        plan["fingerprint"] = _plan_fingerprint(plan)
        return plan

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        approval_reference: str,
        actor: str,
        evidence_dir: Path,
    ) -> dict[str, Any]:
        reviewed = dict(reviewed_plan)
        expected = _require_digest(fingerprint, "fingerprint")
        if reviewed.get("fingerprint") != expected or _plan_fingerprint(reviewed) != expected:
            raise Wbc0027RecoveryError(
                "reviewed_fingerprint_mismatch", "Reviewed plan fingerprint differs"
            )
        if reviewed.get("apply_allowed") is not True or reviewed.get("blockers"):
            raise Wbc0027RecoveryError("reviewed_plan_blocked", "Blocked plan cannot apply")
        if str(reviewed.get("deployed_sha") or "") != self.deployed_sha:
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
        fresh = self.build_plan()
        if fresh.get("fingerprint") != expected:
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
            if locked_storage != dict(reviewed["storage"]):
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
                "non_target_digest": str(
                    dict(reviewed["source_evidence"])["non_target_digest"]
                ),
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
                if self._storage_identity(conn=conn) != dict(reviewed["storage"]):
                    raise Wbc0027RecoveryError(
                        "storage_generation_drift", "Storage changed inside writer submit"
                    )
                source = self._source_snapshot(conn, storage=locked_storage)
                _verify_reviewed_source(reviewed, source)
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
                if _non_target_digest(conn) != str(
                    dict(reviewed["source_evidence"])["non_target_digest"]
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
                source_evidence = dict(reviewed["source_evidence"])
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
                        CONTRACT_VERSION,
                        self.deployed_sha,
                        str(locked_storage["operational_generation_id"]),
                        str(locked_storage["operational_schema_revision"]),
                        int(locked_storage["sqlite_schema_version"]),
                        SOURCE_CUTOFF_SEQUENCE,
                        DATE_FROM,
                        DATE_TO,
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
        self, conn: sqlite3.Connection, *, storage: Mapping[str, Any]
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
                (cutover_id, SOURCE_CUTOFF_SEQUENCE),
            ).fetchall()
        ]
        if len(pending_sequences) > MAX_TARGET_COUNT:
            raise Wbc0027RecoveryError(
                "pending_target_bound_exceeded",
                "Unresolved source candidate count exceeds recovery bound",
                details={"count": len(pending_sequences), "max": MAX_TARGET_COUNT},
            )
        resolved: list[dict[str, Any]] = []
        blockers: list[str] = []
        if conn.execute(
            f"SELECT 1 FROM {STATUS_OBSERVATIONS_TABLE} WHERE observation_sequence=?",
            (SOURCE_CUTOFF_SEQUENCE,),
        ).fetchone() is None:
            blockers.append("exact_source_cutoff_sequence_missing")
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
                continue
            group = (str(scope["facility_id"]), int(scope["nm_id"]))
            if (
                _business_date(str(scope["source_created_at"])) > DATE_TO
                or _business_date(str(scope["source_status_observed_at"])) > DATE_TO
            ):
                blockers.append("target_effect_outside_exact_date_boundary")
            if group in TARGET_GROUP_SET:
                resolved.append(scope)
            elif (
                group[0] in {MOSCOW_FACILITY_ID, ORENBURG_FACILITY_ID}
                and group[1] in {210183919, 428855560, 428855758}
            ):
                blockers.append("unexpected_group_inside_bounded_business_scope")
        sequences = sorted(
            {int(item["status_observation_sequence"]) for item in resolved}
        )
        actual_groups = {
            (str(item["facility_id"]), int(item["nm_id"])) for item in resolved
        }
        if actual_groups != TARGET_GROUP_SET:
            blockers.append("exact_four_group_coverage_missing")
        if not sequences:
            blockers.append("empty_recovery_target")
        target_rows = (
            _stable_target_rows(conn, tuple(sequences), cutoff=SOURCE_CUTOFF_SEQUENCE)
            if sequences
            else []
        )
        stable_target_digest = _fingerprint(
            {
                "contract": CONTRACT_NAME,
                "cutover_id": cutover_id,
                "generation_id": str(generation[0][0]),
                "source_cutoff_sequence": SOURCE_CUTOFF_SEQUENCE,
                "date_from": DATE_FROM,
                "date_to": DATE_TO,
                "groups": list(TARGET_GROUPS),
                "rows": target_rows,
                "resolved_scopes": resolved,
            }
        )
        return {
            "deployed_sha": self.deployed_sha,
            "storage": dict(storage),
            "cutover_id": cutover_id,
            "cutover_manifest_digest": _fingerprint(active["manifest"]),
            "generation_id": str(generation[0][0]),
            "generation_manifest_fingerprint": str(generation[0][1]),
            "cutoff_sequence": SOURCE_CUTOFF_SEQUENCE,
            "old_cursor_sequence": int(generation[0][2]),
            "target_sequences": sequences,
            "target_rows": target_rows,
            "resolved_scopes": sorted(
                resolved, key=lambda item: int(item["status_observation_sequence"])
            ),
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


def _history_plan(
    conn: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    target_result: Sequence[Mapping[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    dates = _date_strings(DATE_FROM, DATE_TO)
    corrections, event_evidence = _history_corrections(
        conn,
        source=source,
        target_result=target_result,
        dates=dates,
    )
    blockers: list[str] = []
    captures: list[dict[str, Any]] = []
    current_date = current_business_date_iso(datetime.fromisoformat(generated_at.replace("Z", "+00:00")))
    stable_target_digest = str(source["stable_target_digest"])
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
                blockers.append(f"history_target_not_exact:{business_date}:{facility_id}:{nm_id}")
                continue
            component["quantity"] = int(component["quantity"]) + delta
            component["state"] = "exact_zero" if int(component["quantity"]) == 0 else "exact"
            facility_total_delta[facility_id] = facility_total_delta.get(facility_id, 0) + delta
            _mark_recovered_component(
                component,
                business_date=business_date,
                stable_target_digest=stable_target_digest,
                event_evidence=event_evidence,
            )
        for facility_id, delta in facility_total_delta.items():
            key = ("TOTAL", "FBS_FACILITY", facility_id)
            component = by_key.get(key)
            if component is None or component["state"] not in {"exact", "exact_zero"}:
                blockers.append(f"history_total_not_exact:{business_date}:{facility_id}")
                continue
            component["quantity"] = int(component["quantity"]) + delta
            component["state"] = "exact_zero" if int(component["quantity"]) == 0 else "exact"
            _mark_recovered_component(
                component,
                business_date=business_date,
                stable_target_digest=stable_target_digest,
                event_evidence=event_evidence,
            )
        roster = json.loads(str(capture["facility_roster_json"]))
        source_manifest = {
            "contract": CONTRACT_NAME,
            "source_cutoff_sequence": SOURCE_CUTOFF_SEQUENCE,
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
        "contract": "wbc0027_same_date_inventory_history_supersession_v1",
        "date_from": DATE_FROM,
        "date_to": DATE_TO,
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
        "blockers": sorted(set(blockers)),
    }
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
    component["source_watermark"] = str(SOURCE_CUTOFF_SEQUENCE)
    component["provenance"] = {
        **dict(component.get("provenance") or {}),
        "recovery_contract": CONTRACT_NAME,
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


def _non_target_digest(conn: sqlite3.Connection) -> str:
    target_predicates = " OR ".join("(facility_id=? AND nm_id=?)" for _ in TARGET_GROUPS)
    balance_rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT facility_id,pool,nm_id,quantity,capital_rub,wac_rub,
                       projection_epoch,updated_at
                FROM sheet_vitrina_v1_ff_pool_balances
                WHERE pool<>'FBS' OR NOT ({target_predicates})
                ORDER BY facility_id,pool,nm_id""",
            tuple(value for group in TARGET_GROUPS for value in group),
        ).fetchall()
    ]
    # The recovery appends target captures.  Existing immutable history and all
    # non-target component identities must remain byte-for-byte represented.
    history_rows = [
        dict(row)
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
                  AND NOT(
                    component.component_kind='FBS_FACILITY'
                    AND component.component_id IN (?,?)
                    AND (component.scope_key='TOTAL' OR component.nm_id IN (?,?,?))
                )
                ORDER BY component.capture_id,component.scope_kind,
                         component.scope_key,component.component_kind,
                         component.component_id""",
            (
                CONTRACT_NAME,
                MOSCOW_FACILITY_ID,
                ORENBURG_FACILITY_ID,
                210183919,
                428855560,
                428855758,
            ),
        ).fetchall()
    ]
    return _fingerprint({"balances": balance_rows, "history": history_rows})


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
        == str(boundary["forward_generation_manifest_fingerprint"]),
        "cursor": int(source["old_cursor_sequence"])
        == int(boundary["forward_cursor_sequence"]),
        "target_sequences": list(source["target_sequences"])
        == list(scope["status_observation_sequences"]),
        "stable_target_digest": str(source["stable_target_digest"])
        == str(scope["stable_target_digest"]),
        "target_rows": list(source["target_rows"]) == list(scope["target_rows"]),
        "resolved_scopes": list(source["resolved_scopes"])
        == list(scope["resolved_scopes"]),
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
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise Wbc0027RecoveryError("invalid_plan", "Reviewed plan must be an object")
    return value


def run(args: argparse.Namespace) -> int:
    runner = Wbc0027FbsLifecycleQualityRecovery(
        runtime_dir=Path(args.runtime_dir),
        deployed_sha=str(args.deployed_sha),
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
    else:
        payload = runner.build_plan()
    if args.output:
        _write_private(Path(args.output), payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") not in {"blocked", "error"} else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--scratch-dir", default="")
    parser.add_argument("--output", default="")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("dry-run")
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
