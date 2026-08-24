"""Exact forward cutover and immutable FBS lifecycle backlog recovery.

Dry-run reads the active operational store in query-only mode and pins only
stable business identities through status sequence ``C``.  Apply is a separate
owner-gated operation: it installs the durable ``C+1`` forward generation and
replays only the reviewed ``<= C`` identities through the canonical lifecycle
processor.  Rows appended above ``C`` never participate in target equality or
the manifest fingerprint.
"""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from packages.application.ff_pool_cutover import MANIFESTS_TABLE
from packages.application.ff_pool_fbs_lifecycle import (
    BACKLOG_RECOVERY_RUNS_TABLE,
    BACKLOG_RECOVERY_TARGETS_TABLE,
    CURRENT_TABLE,
    DRAIN_STATE_TABLE,
    EVENTS_TABLE,
    FORWARD_GENERATIONS_TABLE,
    FORWARD_STATE_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    LATE_EVIDENCE_TABLE,
    MAPPING_EXTENSION_ALLOCATIONS_TABLE,
    MAPPING_EXTENSIONS_TABLE,
    RECONCILIATION_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    recover_pinned_fbs_lifecycle,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_CHANGES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    PARITY_TABLE,
    RELATIONS_TABLE,
    canonical_decimal_text,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.storage_registry import manifest_payload
from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
from packages.application.warehouse_domain_write_guard import (
    EVENTS_TABLE as WAREHOUSE_DOMAIN_EVENTS_TABLE,
)
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    POLL_RUNS_TABLE,
    STATE_TABLE as COLLECTOR_STATE_TABLE,
    STATUS_CURRENT_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATUS_TRANSITIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)


CONTRACT_NAME = "ff_pool_fbs_forward_recovery_v1"
CONTRACT_VERSION = 1
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_TARGET_COUNT = 100_000
PROJECTION_CHUNK_SIZE = 256
PROJECTION_MAX_ROW_COUNT = 500_000
PROJECTION_MAX_PAYLOAD_BYTES = 256 * 1024 * 1024
PROJECTION_MAX_SCRATCH_BYTES = 384 * 1024 * 1024
PROJECTION_DISK_RESERVE_BYTES = 256 * 1024 * 1024
TARGET_MANIFEST_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
MAX_PRIVACY_SAFE_DIFF_COUNT = 1_000_000

CUTOVER_ORDERS_TABLE = "sheet_vitrina_v1_ff_pool_cutover_order_classifications"
CUTOVER_LATE_CASES_TABLE = "sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases"
FUNCTIONAL_ACTIVE_TABLE = "sheet_vitrina_v1_warehouse_functional_active"
FUNCTIONAL_BALANCES_TABLE = "sheet_vitrina_v1_warehouse_functional_balances"

PREVIEW_SCHEMA_TABLES = (
    MANIFESTS_TABLE,
    CUTOVER_ORDERS_TABLE,
    CUTOVER_LATE_CASES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FACILITY_CHANGES_TABLE,
    FEATURE_EPOCHS_TABLE,
    OPERATIONS_TABLE,
    LINES_TABLE,
    RELATIONS_TABLE,
    BALANCES_TABLE,
    PARITY_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATUS_CURRENT_TABLE,
    STATUS_TRANSITIONS_TABLE,
    POLL_RUNS_TABLE,
    COLLECTOR_STATE_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    IDENTITY_EVIDENCE_TABLE,
    EVENTS_TABLE,
    CURRENT_TABLE,
    RECONCILIATION_TABLE,
    LATE_EVIDENCE_TABLE,
    IDENTITY_PENDING_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    MAPPING_EXTENSIONS_TABLE,
    MAPPING_EXTENSION_ALLOCATIONS_TABLE,
    DRAIN_STATE_TABLE,
    FORWARD_GENERATIONS_TABLE,
    FORWARD_STATE_TABLE,
    BACKLOG_RECOVERY_RUNS_TABLE,
    BACKLOG_RECOVERY_TARGETS_TABLE,
    WAREHOUSE_DOMAIN_EVENTS_TABLE,
    FUNCTIONAL_ACTIVE_TABLE,
    FUNCTIONAL_BALANCES_TABLE,
)


class FfPoolFbsForwardRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class FfPoolFbsForwardRecoveryMutation:
    """Dry-run/apply/readback contract for one active Stage 7C cutover."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        timestamp_factory: Any | None = None,
        scratch_dir: Path | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir).resolve())
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise FfPoolFbsForwardRecoveryError(
                "invalid_deployed_sha", "deployed_sha must be an exact 40-hex SHA"
            )
        self.timestamp_factory = timestamp_factory or _utc_now
        self.scratch_dir = Path(
            scratch_dir
            if scratch_dir is not None
            else self.runtime.runtime_dir / "ff-pool-fbs-forward-recovery-scratch"
        ).expanduser()

    def build_plan(self) -> dict[str, Any]:
        generated_at = str(self.timestamp_factory())
        _require_utc(generated_at)
        storage = self._storage_identity()
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            conn.execute("BEGIN")
            try:
                source = _build_source_snapshot(
                    conn,
                    deployed_sha=self.deployed_sha,
                    storage_identity=storage,
                )
                preview, projection, _ = _preview_recovery(
                    conn,
                    source=source,
                    occurred_at=generated_at,
                    scratch_root=self.scratch_dir,
                )
                _verify_storage(storage, self._storage_identity(conn=conn))
            finally:
                if conn.in_transaction:
                    conn.rollback()
        blockers = list(source["blockers"])
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "deployed_sha": self.deployed_sha,
            "generated_at": generated_at,
            "storage": storage,
            "boundary": {
                "cutover_id": source["cutover_id"],
                "active_cutover_manifest_digest": source["cutover_manifest_digest"],
                "source_max_status_observation_sequence": source["cutoff_sequence"],
                "old_lifecycle_cursor_sequence": source["old_cursor_sequence"],
                "forward_start_status_observation_sequence": source["cutoff_sequence"] + 1,
                "post_cutoff_growth_invalidates_manifest": False,
            },
            "target": {
                "status_observation_sequences": source["target_sequences"],
                "count": len(source["target_sequences"]),
                "stable_business_digest": source["stable_target_digest"],
                "rows": source["target_rows"],
                "location_wac_evidence": source["location_wac_evidence"],
            },
            "past_fulfilled_invariant": source["past_fulfilled_invariant"],
            "predicted_effects": preview,
            "planner": {
                **projection,
                "stable_digest_contract": "canonical_length_delimited_stream_v1",
                "stable_business_effect_contract": (
                    "exact_decimal_numeric_and_target_identity_v1"
                ),
                "source_schema_evidence": source["projection_schema_evidence"],
                "canonical_write_seeds": source["canonical_write_seeds"],
                "target_manifest_payload_bytes": source[
                    "target_manifest_payload_bytes"
                ],
                "max_target_manifest_payload_bytes": (
                    TARGET_MANIFEST_MAX_PAYLOAD_BYTES
                ),
            },
            "recovery": {
                "writer_lock": "warehouse_functional_write_lock",
                "before_image": "private_mode_0600_exact_target",
                "pre_commit": "sqlite_atomic_rollback",
                "ambiguous_transport": "query_only_readback_before_any_retry",
                "idempotency": "manifest_fingerprint_and_per_status_event_identity",
                "forward_cursor_rewind_allowed": False,
            },
            "invariants": {
                "new_status_sequences_above_c_excluded": True,
                "dynamic_generated_or_refreshed_timestamps_excluded": True,
                "target_cas_required": True,
                "quantity_and_capital_delta_exact": True,
                "non_target_rows_unchanged_inside_apply": True,
                "past_fulfilled_frozen_wac_immutable": True,
                "missing_mapping_fallback": False,
                "wb_writes": 0,
            },
            "requires_exact_post_deploy_owner_gate": True,
            "apply_allowed": not blockers,
            "blockers": blockers,
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
        crash: str = "",
    ) -> dict[str, Any]:
        reviewed = dict(reviewed_plan)
        expected = _require_digest(fingerprint, "fingerprint")
        if _plan_fingerprint(reviewed) != expected or reviewed.get("fingerprint") != expected:
            raise FfPoolFbsForwardRecoveryError(
                "reviewed_fingerprint_mismatch",
                "Reviewed recovery manifest fingerprint does not match",
            )
        if str(reviewed.get("deployed_sha") or "") != self.deployed_sha:
            raise FfPoolFbsForwardRecoveryError(
                "deployed_sha_drift", "Reviewed recovery manifest belongs to another SHA"
            )
        if reviewed.get("apply_allowed") is not True or list(reviewed.get("blockers") or []):
            raise FfPoolFbsForwardRecoveryError(
                "reviewed_plan_blocked", "A blocked recovery plan cannot be applied"
            )
        approval = str(approval_reference or "").strip()
        operator = str(actor or "").strip()
        if not approval or not operator:
            raise FfPoolFbsForwardRecoveryError(
                "gate_identity_required", "approval_reference and actor are required"
            )
        existing = self.readback(fingerprint=expected)
        if existing.get("status") == "completed":
            return {**existing, "idempotent": True, "recovered_after_response_loss": True}

        evidence_root = Path(evidence_dir).resolve()
        if not evidence_root.is_absolute():
            raise FfPoolFbsForwardRecoveryError(
                "evidence_dir_not_absolute", "evidence_dir must be absolute"
            )
        evidence_root.mkdir(parents=True, exist_ok=True)
        suffix = expected.removeprefix("sha256:")[:20]
        before_path = evidence_root / f"fbs-forward-recovery-{suffix}.before.json"
        evidence_path = evidence_root / f"fbs-forward-recovery-{suffix}.evidence.json"
        drift_path = evidence_root / f"fbs-forward-recovery-{suffix}.after-image-drift.json"
        now = str(self.timestamp_factory())
        _require_utc(now)
        expected_source = _source_from_reviewed(reviewed)
        storage = self._storage_identity()
        _verify_storage(dict(reviewed["storage"]), storage)

        # Rebuild the canonical expected target outside both the process-owned
        # writer lock and BEGIN IMMEDIATE.  This keeps the expensive coherent
        # snapshot/after-image calculation out of the blocking writer section,
        # while the later target CAS still rejects any business drift.
        with closing(_open_query_only(self.runtime.db_path)) as query:
            query.execute("BEGIN")
            try:
                revalidated_source = _build_source_snapshot(
                    query,
                    deployed_sha=self.deployed_sha,
                    storage_identity=storage,
                    pinned_cutoff=int(expected_source["cutoff_sequence"]),
                    pinned_old_cursor=int(expected_source["old_cursor_sequence"]),
                    pinned_past_event_sequence_max=int(
                        expected_source["past_fulfilled_invariant"][
                            "pinned_event_sequence_max"
                        ]
                    ),
                )
                _verify_target_source(expected_source, revalidated_source)
                revalidated_preview, _, expected_target_result = _preview_recovery(
                    query,
                    source=revalidated_source,
                    occurred_at=now,
                    scratch_root=self.scratch_dir,
                )
                _verify_storage(storage, self._storage_identity(conn=query))
            finally:
                if query.in_transaction:
                    query.rollback()
        reviewed_effect = _stable_business_effect(reviewed["predicted_effects"])
        revalidated_effect = _stable_business_effect(revalidated_preview)
        if reviewed_effect != revalidated_effect:
            precommit_drift = _build_privacy_safe_drift_evidence(
                phase="pre_commit_preview_revalidation",
                manifest_fingerprint=expected,
                deployed_sha=self.deployed_sha,
                expected_effect=reviewed_effect,
                actual_effect=revalidated_effect,
                expected_target_result=(),
                actual_target_result=expected_target_result,
                captured_at=now,
            )
            _write_private(drift_path, precommit_drift)
            raise FfPoolFbsForwardRecoveryError(
                "target_preview_revalidation_drift",
                "Canonical coherent preview differs from the reviewed business effect",
                details={
                    "evidence_path": str(drift_path),
                    "evidence_sha256": _sha256_file(drift_path),
                    "diff_count": int(precommit_drift["diff_count"]),
                },
            )

        with warehouse_functional_write_lock(self.runtime.runtime_dir, timeout_seconds=300):
            with closing(_open_query_only(self.runtime.db_path)) as query:
                query.execute("BEGIN")
                try:
                    fresh = _build_source_snapshot(
                        query,
                        deployed_sha=self.deployed_sha,
                        storage_identity=storage,
                        pinned_cutoff=int(expected_source["cutoff_sequence"]),
                        pinned_old_cursor=int(expected_source["old_cursor_sequence"]),
                        pinned_past_event_sequence_max=int(
                            expected_source["past_fulfilled_invariant"][
                                "pinned_event_sequence_max"
                            ]
                        ),
                    )
                    _verify_target_source(expected_source, fresh)
                finally:
                    if query.in_transaction:
                        query.rollback()
            before_image = {
                "contract_name": CONTRACT_NAME,
                "manifest_fingerprint": expected,
                "deployed_sha": self.deployed_sha,
                "storage": storage,
                "source": fresh,
                "created_at": now,
            }
            _write_private(before_path, before_image)
            if crash == "after_before_image":
                raise FfPoolFbsForwardRecoveryError(
                    "simulated_pre_commit_crash", "Simulated crash before writer transaction"
                )

            conn = sqlite3.connect(self.runtime.db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            try:
                conn.execute("BEGIN IMMEDIATE")
                ensure_ff_pool_fbs_lifecycle_schema(conn)
                locked_storage = self._storage_identity(conn=conn)
                _verify_storage(dict(reviewed["storage"]), locked_storage)
                locked = _build_source_snapshot(
                    conn,
                    deployed_sha=self.deployed_sha,
                    storage_identity=locked_storage,
                    pinned_cutoff=int(expected_source["cutoff_sequence"]),
                    pinned_old_cursor=int(expected_source["old_cursor_sequence"]),
                    pinned_past_event_sequence_max=int(
                        expected_source["past_fulfilled_invariant"][
                            "pinned_event_sequence_max"
                        ]
                    ),
                )
                _verify_target_source(expected_source, locked)
                generation_id = "fbsgen_" + expected.removeprefix("sha256:")[:28]
                recovery_id = "fbsrec_" + expected.removeprefix("sha256:")[:28]
                boundary = dict(reviewed["boundary"])
                conn.execute(
                    f"""INSERT INTO {FORWARD_GENERATIONS_TABLE}(
                           generation_id,cutover_id,contract_version,deployed_sha,
                           storage_generation_id,storage_schema_revision,
                           sqlite_schema_version,cutoff_status_observation_sequence,
                           forward_start_status_observation_sequence,
                           old_cursor_status_observation_sequence,manifest_fingerprint,
                           stable_target_digest,approval_reference,created_by,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        str(boundary["cutover_id"]),
                        CONTRACT_VERSION,
                        self.deployed_sha,
                        str(locked_storage["operational_generation_id"]),
                        str(locked_storage["operational_schema_revision"]),
                        int(locked_storage["sqlite_schema_version"]),
                        int(boundary["source_max_status_observation_sequence"]),
                        int(boundary["forward_start_status_observation_sequence"]),
                        int(boundary["old_lifecycle_cursor_sequence"]),
                        expected,
                        str(expected_source["stable_target_digest"]),
                        approval,
                        operator,
                        now,
                    ),
                )
                conn.execute(
                    f"""INSERT INTO {FORWARD_STATE_TABLE}(
                           generation_id,last_status_observation_sequence,run_count,
                           last_result_digest,updated_at
                       ) VALUES(?,?,0,'',?)""",
                    (generation_id, int(expected_source["cutoff_sequence"]), now),
                )
                manifest = _active_manifest(conn)["manifest"]
                target_sequences = tuple(int(value) for value in expected_source["target_sequences"])
                before_balances = _balance_payload(conn)
                recovery = recover_pinned_fbs_lifecycle(
                    conn,
                    manifest=manifest,
                    status_observation_sequences=target_sequences,
                    occurred_at=now,
                )
                after_balances = _balance_payload(conn)
                result = _target_result_payload(conn, target_sequences)
                actual_preview = _preview_payload(
                    recovery=recovery,
                    before_balances=before_balances,
                    after_balances=after_balances,
                    target_result=result,
                )
                if crash == "simulate_after_image_drift":
                    actual_preview = deepcopy(actual_preview)
                    actual_preview["total_quantity_delta"] = (
                        int(actual_preview["total_quantity_delta"]) + 1
                    )
                actual_effect = _stable_business_effect(actual_preview)
                if actual_effect != revalidated_effect:
                    drift_evidence = _build_privacy_safe_drift_evidence(
                        phase="inside_writer_before_rollback",
                        manifest_fingerprint=expected,
                        deployed_sha=self.deployed_sha,
                        expected_effect=revalidated_effect,
                        actual_effect=actual_effect,
                        expected_target_result=expected_target_result,
                        actual_target_result=result,
                        captured_at=now,
                    )
                    # This external private evidence is fsynced before the
                    # exception unwinds and the SQLite transaction rolls back.
                    _write_private(drift_path, drift_evidence)
                    raise FfPoolFbsForwardRecoveryError(
                        "target_after_image_drift",
                        "Canonical recovery after-image differs from the reviewed target",
                        details={
                            "evidence_path": str(drift_path),
                            "evidence_sha256": _sha256_file(drift_path),
                            "diff_count": int(drift_evidence["diff_count"]),
                            "expected_effect_digest": str(
                                drift_evidence["expected_effect_digest"]
                            ),
                            "actual_effect_digest": str(
                                drift_evidence["actual_effect_digest"]
                            ),
                        },
                    )
                result_digest = _fingerprint(result)
                conn.execute(
                    f"""INSERT INTO {BACKLOG_RECOVERY_RUNS_TABLE}(
                           recovery_id,generation_id,cutover_id,manifest_fingerprint,
                           stable_target_digest,result_digest,summary_json,target_count,
                           status,applied_at
                       ) VALUES(?,?,?,?,?,?,?,?, 'completed',?)""",
                    (
                        recovery_id,
                        generation_id,
                        str(boundary["cutover_id"]),
                        expected,
                        str(expected_source["stable_target_digest"]),
                        result_digest,
                        _json(actual_preview),
                        len(target_sequences),
                        now,
                    ),
                )
                source_by_sequence = {
                    int(row["status_observation_sequence"]): row
                    for row in expected_source["target_rows"]
                }
                result_by_sequence = {
                    int(row["status_observation_sequence"]): row for row in result
                }
                for sequence in target_sequences:
                    target = source_by_sequence[sequence]
                    target_result = result_by_sequence[sequence]
                    conn.execute(
                        f"""INSERT INTO {BACKLOG_RECOVERY_TARGETS_TABLE}(
                               recovery_id,source_status_observation_sequence,order_id,
                               stable_business_digest,before_state_digest,
                               after_state_digest,outcome
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            recovery_id,
                            sequence,
                            int(target["order_id"]),
                            str(target["stable_business_digest"]),
                            str(target["before_state_digest"]),
                            _fingerprint(target_result),
                            str(target_result["outcome"]),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        if crash == "after_commit_before_response":
            raise FfPoolFbsForwardRecoveryError(
                "simulated_ambiguous_transport",
                "Commit may have completed; use query-only readback and do not resubmit",
            )
        readback = self.readback(fingerprint=expected)
        if readback.get("status") != "completed":
            raise FfPoolFbsForwardRecoveryError(
                "post_apply_reconciliation_failed", "Durable recovery readback is incomplete"
            )
        evidence = {
            "contract_name": CONTRACT_NAME,
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
            "status": "completed",
            "idempotent": False,
            "evidence_path": str(evidence_path),
            "evidence_sha256": _sha256_file(evidence_path),
        }

    def readback(self, *, fingerprint: str = "") -> dict[str, Any]:
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            tables = _table_names(conn)
            if not {
                FORWARD_GENERATIONS_TABLE,
                FORWARD_STATE_TABLE,
                BACKLOG_RECOVERY_RUNS_TABLE,
            }.issubset(tables):
                return {"contract_name": CONTRACT_NAME, "status": "not_applied"}
            where = "WHERE generation.manifest_fingerprint=?" if fingerprint else ""
            parameters: tuple[Any, ...] = (fingerprint,) if fingerprint else ()
            row = conn.execute(
                f"""SELECT generation.generation_id,generation.cutover_id,
                           generation.deployed_sha,generation.manifest_fingerprint,
                           generation.cutoff_status_observation_sequence,
                           generation.forward_start_status_observation_sequence,
                           generation.old_cursor_status_observation_sequence,
                           state.last_status_observation_sequence,
                           recovery.recovery_id,recovery.result_digest,
                           recovery.summary_json,recovery.target_count,recovery.status,
                           recovery.applied_at
                    FROM {FORWARD_GENERATIONS_TABLE} AS generation
                    JOIN {FORWARD_STATE_TABLE} AS state
                      ON state.generation_id=generation.generation_id
                    LEFT JOIN {BACKLOG_RECOVERY_RUNS_TABLE} AS recovery
                      ON recovery.generation_id=generation.generation_id
                    {where}
                    ORDER BY generation.created_at DESC LIMIT 1""",
                parameters,
            ).fetchone()
            if row is None:
                return {"contract_name": CONTRACT_NAME, "status": "not_applied"}
            return {
                "contract_name": CONTRACT_NAME,
                "status": str(row[12] or "forward_armed_recovery_pending"),
                "generation_id": str(row[0]),
                "cutover_id": str(row[1]),
                "deployed_sha": str(row[2]),
                "manifest_fingerprint": str(row[3]),
                "cutoff_sequence": int(row[4]),
                "forward_start_sequence": int(row[5]),
                "old_cursor_sequence": int(row[6]),
                "forward_cursor_sequence": int(row[7]),
                "recovery_id": str(row[8] or ""),
                "result_digest": str(row[9] or ""),
                "summary": json.loads(str(row[10] or "{}")),
                "target_count": int(row[11] or 0),
                "applied_at": str(row[13] or ""),
                "mutates_wb": False,
            }

    def verify_noop(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
    ) -> dict[str, Any]:
        """Prove through query-only readback that a repeat would be a no-op."""

        reviewed = dict(reviewed_plan)
        expected = _require_digest(fingerprint, "fingerprint")
        if _plan_fingerprint(reviewed) != expected or reviewed.get("fingerprint") != expected:
            raise FfPoolFbsForwardRecoveryError(
                "reviewed_fingerprint_mismatch",
                "Reviewed recovery manifest fingerprint does not match",
            )
        if str(reviewed.get("deployed_sha") or "") != self.deployed_sha:
            raise FfPoolFbsForwardRecoveryError(
                "deployed_sha_drift", "Reviewed recovery manifest belongs to another SHA"
            )
        durable = self.readback(fingerprint=expected)
        if durable.get("status") != "completed":
            raise FfPoolFbsForwardRecoveryError(
                "repeat_noop_not_proven",
                "Durable completed recovery readback is absent",
                details=durable,
            )
        boundary = dict(reviewed.get("boundary") or {})
        comparisons = {
            "deployed_sha": str(durable.get("deployed_sha") or "") == self.deployed_sha,
            "cutoff_sequence": int(durable.get("cutoff_sequence") or 0)
            == int(boundary.get("source_max_status_observation_sequence") or 0),
            "forward_start_sequence": int(durable.get("forward_start_sequence") or 0)
            == int(boundary.get("forward_start_status_observation_sequence") or 0),
            "old_cursor_sequence": int(durable.get("old_cursor_sequence") or 0)
            == int(boundary.get("old_lifecycle_cursor_sequence") or 0),
            "target_count": int(durable.get("target_count") or 0)
            == int(dict(reviewed.get("target") or {}).get("count") or 0),
            "result_summary": _fingerprint(durable.get("summary") or {})
            == _fingerprint(reviewed.get("predicted_effects") or {}),
        }
        if not all(comparisons.values()):
            raise FfPoolFbsForwardRecoveryError(
                "repeat_noop_readback_drift",
                "Durable recovery readback differs from the reviewed after-image",
                details=comparisons,
            )
        return {
            "contract_name": CONTRACT_NAME,
            "status": "completed_no_op",
            "manifest_fingerprint": expected,
            "deployed_sha": self.deployed_sha,
            "query_only": True,
            "repeat_submit_performed": False,
            "would_write": False,
            "comparisons": comparisons,
            "readback": durable,
        }

    def _storage_identity(self, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        manifest = self.runtime.store_registry.load(require_files=True)
        operational = self.runtime.store_registry.generation("operational", manifest=manifest)
        if conn is None:
            with closing(_open_query_only(self.runtime.db_path)) as query:
                sqlite_schema_version = int(query.execute("PRAGMA schema_version").fetchone()[0])
        else:
            sqlite_schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        return {
            "manifest_sha256": manifest.manifest_sha256,
            "generation_epoch": manifest.generation_epoch,
            "state": manifest.state,
            "operational_generation_id": operational.generation_id,
            "operational_schema_revision": operational.schema_revision,
            "sqlite_schema_version": sqlite_schema_version,
            "manifest_contract": manifest_payload(manifest)["contract_version"],
        }


def _build_source_snapshot(
    conn: sqlite3.Connection,
    *,
    deployed_sha: str,
    storage_identity: Mapping[str, Any],
    pinned_cutoff: int | None = None,
    pinned_old_cursor: int | None = None,
    pinned_past_event_sequence_max: int | None = None,
) -> dict[str, Any]:
    query_only = int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
    if not query_only and pinned_cutoff is None:
        # Apply repeats this function on its writer connection under BEGIN
        # IMMEDIATE.  Planning, including the first T0 snapshot, must be the
        # query-only form and must already own one coherent read transaction.
        raise FfPoolFbsForwardRecoveryError(
            "source_snapshot_not_query_only",
            "Initial recovery source snapshot must be SQLite query-only",
        )
    if query_only and not conn.in_transaction:
        raise FfPoolFbsForwardRecoveryError(
            "source_snapshot_not_coherent",
            "Query-only recovery source snapshot requires an explicit read transaction",
        )
    blockers: list[str] = []
    tables = _table_names(conn)
    required = {
        MANIFESTS_TABLE,
        DRAIN_STATE_TABLE,
        STATUS_OBSERVATIONS_TABLE,
        OBSERVATIONS_TABLE,
        IDENTITY_EVIDENCE_TABLE,
        EVENTS_TABLE,
        CURRENT_TABLE,
        IDENTITY_PENDING_TABLE,
        IDENTITY_PENDING_RESOLUTIONS_TABLE,
        LATE_EVIDENCE_TABLE,
        BALANCES_TABLE,
        FORWARD_GENERATIONS_TABLE,
        FORWARD_STATE_TABLE,
        BACKLOG_RECOVERY_RUNS_TABLE,
        BACKLOG_RECOVERY_TARGETS_TABLE,
    }
    missing = sorted(required - tables)
    if missing:
        raise FfPoolFbsForwardRecoveryError(
            "recovery_schema_missing", "Required deployed recovery schema is missing", details=missing
        )
    active = _active_manifest(conn)
    cutover_id = str(active["cutover_id"])
    existing_generation = conn.execute(
        f"SELECT manifest_fingerprint FROM {FORWARD_GENERATIONS_TABLE} WHERE cutover_id=?",
        (cutover_id,),
    ).fetchone()
    if existing_generation is not None:
        blockers.append("forward_generation_already_exists")
    drain = conn.execute(
        f"SELECT last_status_observation_sequence FROM {DRAIN_STATE_TABLE} WHERE cutover_id=?",
        (cutover_id,),
    ).fetchone()
    if drain is None:
        raise FfPoolFbsForwardRecoveryError(
            "old_cursor_missing", "Active cutover has no durable lifecycle drain cursor"
        )
    live_old_cursor = int(drain[0])
    old_cursor = live_old_cursor if pinned_old_cursor is None else int(pinned_old_cursor)
    if live_old_cursor != old_cursor:
        blockers.append("target_old_cursor_drift")
    live_max = int(
        conn.execute(
            f"SELECT COALESCE(MAX(observation_sequence),0) FROM {STATUS_OBSERVATIONS_TABLE}"
        ).fetchone()[0]
    )
    cutoff = live_max if pinned_cutoff is None else int(pinned_cutoff)
    if cutoff < old_cursor:
        blockers.append("cutoff_before_old_cursor")
    cursor = conn.execute(
        f"""SELECT observation_sequence FROM {STATUS_OBSERVATIONS_TABLE}
            WHERE observation_sequence>? AND observation_sequence<=?
            ORDER BY observation_sequence""",
        (old_cursor, cutoff),
    )
    sequences: list[int] = []
    while True:
        rows = cursor.fetchmany(PROJECTION_CHUNK_SIZE)
        if not rows:
            break
        sequences.extend(int(row[0]) for row in rows)
        if len(sequences) > MAX_TARGET_COUNT:
            raise FfPoolFbsForwardRecoveryError(
                "target_count_exceeds_bound",
                "Pinned recovery target exceeds its deterministic row bound",
                details={"target_count": len(sequences), "max_target_count": MAX_TARGET_COUNT},
            )
    if len(sequences) > MAX_TARGET_COUNT:
        blockers.append("target_count_exceeds_bound")
    target_rows = _stable_target_rows(conn, tuple(sequences), cutoff=cutoff)
    target_manifest_payload_bytes = 0
    for target_row in target_rows:
        target_manifest_payload_bytes += len(_json(target_row).encode("utf-8"))
        if target_manifest_payload_bytes > TARGET_MANIFEST_MAX_PAYLOAD_BYTES:
            raise FfPoolFbsForwardRecoveryError(
                "target_manifest_memory_bound_exceeded",
                "Pinned recovery target payload exceeds its bounded-memory contract",
                details={
                    "payload_bytes": target_manifest_payload_bytes,
                    "max_payload_bytes": TARGET_MANIFEST_MAX_PAYLOAD_BYTES,
                },
            )
    location_wac = _target_location_wac_evidence(conn, target_rows)
    stable_target_digest = _streaming_fingerprint(
        chain(
            ({
                "contract": "ff_pool_fbs_stable_target_stream_v1",
                "cutover_id": cutover_id,
                "cutoff_sequence": cutoff,
                "old_cursor_sequence": old_cursor,
            },),
            target_rows,
            ({
                "contract": "ff_pool_fbs_location_wac_evidence_v1",
                "rows": location_wac,
            },),
        )
    )
    projection_schema_evidence = _projection_schema_evidence(conn)
    canonical_write_seeds = _canonical_write_seeds(conn)
    return {
        "deployed_sha": deployed_sha,
        "storage": dict(storage_identity),
        "cutover_id": cutover_id,
        "cutover_manifest_digest": _fingerprint(active["manifest"]),
        "cutoff_sequence": cutoff,
        "old_cursor_sequence": old_cursor,
        "target_sequences": sequences,
        "target_rows": target_rows,
        "target_manifest_payload_bytes": target_manifest_payload_bytes,
        "stable_target_digest": stable_target_digest,
        "location_wac_evidence": location_wac,
        "past_fulfilled_invariant": _past_fulfilled_invariant(
            conn, pinned_max=pinned_past_event_sequence_max
        ),
        "projection_schema_evidence": projection_schema_evidence,
        "canonical_write_seeds": canonical_write_seeds,
        "blockers": blockers,
    }


def _stable_target_row(
    conn: sqlite3.Connection, sequence: int, *, cutoff: int
) -> dict[str, Any]:
    rows = _stable_target_rows(conn, (int(sequence),), cutoff=cutoff)
    return rows[0]


def _stable_target_rows(
    conn: sqlite3.Connection,
    sequences: tuple[int, ...],
    *,
    cutoff: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for sequence_batch in _chunks(sequences, PROJECTION_CHUNK_SIZE):
        placeholders = ",".join("?" for _ in sequence_batch)
        base_rows = conn.execute(
            f"""SELECT status.observation_sequence,status.order_id,
                       status.order_revision,status.status_digest,
                       status.supplier_status,status.wb_status,
                       status.positive_quantity,source.observation_sequence,
                       source.observation_id,source.source_revision,
                       source.source_created_at,source.warehouse_id,
                       source.office_id,source.nm_id,source.chrt_id,
                       source.seller_sku,source.skus_json
                FROM {STATUS_OBSERVATIONS_TABLE} AS status
                LEFT JOIN {OBSERVATIONS_TABLE} AS source
                  ON source.order_id=status.order_id
                 AND source.source_revision=status.order_revision
                WHERE status.observation_sequence IN ({placeholders})
                ORDER BY status.observation_sequence""",
            sequence_batch,
        ).fetchall()
        if (
            len(base_rows) != len(sequence_batch)
            or any(row[7] is None for row in base_rows)
        ):
            raise FfPoolFbsForwardRecoveryError(
                "target_source_missing",
                "One or more pinned statuses lack their exact order revision",
            )
        order_ids = tuple(sorted({int(row[1]) for row in base_rows}))
        order_placeholders = ",".join("?" for _ in order_ids)
        identity_by_revision: dict[tuple[int, str], list[dict[str, Any]]] = {}
        for item in conn.execute(
            f"""SELECT order_id,evidence_sequence,evidence_id,order_revision,
                       outcome,warehouse_id,nm_id,chrt_id,barcode,seller_sku,
                       warehouse_mapping_id,identity_mapping_id,evidence_digest
                FROM {IDENTITY_EVIDENCE_TABLE}
                WHERE order_id IN ({order_placeholders})
                ORDER BY order_id,evidence_sequence""",
            order_ids,
        ).fetchall():
            key = (int(item[0]), str(item[3]))
            identity_by_revision.setdefault(key, []).append(
                {
                    "evidence_sequence": int(item[1]),
                    "evidence_id": str(item[2]),
                    "order_revision": str(item[3]),
                    "outcome": str(item[4]),
                    "warehouse_id": item[5],
                    "nm_id": item[6],
                    "chrt_id": item[7],
                    "barcode": str(item[8] or ""),
                    "seller_sku": str(item[9] or ""),
                    "warehouse_mapping_id": str(item[10] or ""),
                    "identity_mapping_id": str(item[11] or ""),
                    "evidence_digest": str(item[12]),
                }
            )
        events_by_order: dict[int, list[dict[str, Any]]] = {}
        for item in conn.execute(
            f"""SELECT order_id,event_id,event_type,
                       source_status_observation_sequence,source_revision,
                       status_digest,facility_id,pool,nm_id,quantity,
                       physical_quantity_delta,capital_delta_rub,frozen_wac_rub,
                       evidence_digest,details_json
                FROM {EVENTS_TABLE}
                WHERE order_id IN ({order_placeholders})
                  AND source_status_observation_sequence<=?
                ORDER BY order_id,event_sequence""",
            (*order_ids, int(cutoff)),
        ).fetchall():
            events_by_order.setdefault(int(item[0]), []).append(
                {
                    "event_id": str(item[1]),
                    "event_type": str(item[2]),
                    "source_status_observation_sequence": int(item[3]),
                    "source_revision": str(item[4]),
                    "status_digest": str(item[5]),
                    "facility_id": str(item[6]),
                    "pool": str(item[7]),
                    "nm_id": int(item[8]),
                    "quantity": int(item[9]),
                    "physical_quantity_delta": int(item[10]),
                    "capital_delta_rub": str(item[11]),
                    "frozen_wac_rub": str(item[12]),
                    "evidence_digest": str(item[13]),
                    "details_json": str(item[14]),
                }
            )
        pending_by_sequence = {
            int(item[0]): {
                "pending_id": str(item[1]),
                "order_revision": str(item[2]),
                "status_digest": str(item[3]),
                "deferred_identity_evidence_sequence": int(item[4]),
                "reason_code": str(item[5]),
                "reason_detail_code": str(item[6]),
                "evidence_digest": str(item[7]),
                "resolution_id": str(item[8]) if item[8] is not None else None,
                "resolution_digest": str(item[9]) if item[9] is not None else None,
            }
            for item in conn.execute(
                f"""SELECT pending.source_status_observation_sequence,
                           pending.pending_id,pending.order_revision,
                           pending.status_digest,
                           pending.deferred_identity_evidence_sequence,
                           pending.reason_code,pending.reason_detail_code,
                           pending.evidence_digest,resolution.resolution_id,
                           resolution.resolution_digest
                    FROM {IDENTITY_PENDING_TABLE} AS pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE pending.source_status_observation_sequence
                          IN ({placeholders})""",
                sequence_batch,
            ).fetchall()
        }
        for row in base_rows:
            order_id = int(row[1])
            revision = str(row[2])
            business = {
                "status_observation_sequence": int(row[0]),
                "order_id": order_id,
                "order_revision": revision,
                "status_digest": str(row[3]),
                "supplier_status": str(row[4] or ""),
                "wb_status": str(row[5] or ""),
                "positive_quantity": int(row[6]),
                "order_observation_sequence": int(row[7]),
                "observation_id": str(row[8]),
                "source_revision": str(row[9]),
                "source_created_at": str(row[10]),
                "warehouse_id": int(row[11] or 0),
                "office_id": int(row[12] or 0),
                "nm_id": int(row[13]),
                "chrt_id": int(row[14] or 0),
                "seller_sku": str(row[15] or ""),
                "skus_json": str(row[16] or "[]"),
                "identity_evidence": identity_by_revision.get(
                    (order_id, revision), []
                ),
            }
            before_state = {
                "events": events_by_order.get(order_id, []),
                "pending": pending_by_sequence.get(int(row[0])),
            }
            results.append(
                {
                    **business,
                    "stable_business_digest": _fingerprint(business),
                    "before_state_digest": _fingerprint(before_state),
                }
            )
    return results


def _target_order_state(
    conn: sqlite3.Connection, order_id: int, sequence: int, *, cutoff: int
) -> dict[str, Any]:
    events = [
        dict(row)
        for row in conn.execute(
            f"""SELECT event_id,event_type,source_status_observation_sequence,
                       source_revision,status_digest,facility_id,pool,nm_id,quantity,
                       physical_quantity_delta,capital_delta_rub,frozen_wac_rub,
                       evidence_digest,details_json
                FROM {EVENTS_TABLE}
                WHERE order_id=? AND source_status_observation_sequence<=?
                ORDER BY event_sequence""",
            (order_id, cutoff),
        ).fetchall()
    ]
    pending = conn.execute(
        f"""SELECT pending.pending_id,pending.order_revision,pending.status_digest,
                   pending.deferred_identity_evidence_sequence,pending.reason_code,
                   pending.reason_detail_code,pending.evidence_digest,
                   resolution.resolution_id,resolution.resolution_digest
            FROM {IDENTITY_PENDING_TABLE} AS pending
            LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
              ON resolution.pending_id=pending.pending_id
            WHERE pending.source_status_observation_sequence=?""",
        (sequence,),
    ).fetchone()
    return {
        "events": events,
        "pending": dict(pending) if pending is not None else None,
    }


def _target_location_wac_evidence(
    conn: sqlite3.Connection, target_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    manifest = _active_manifest(conn)["manifest"]
    facility_by_warehouse = {
        int(row["warehouse_id"]): str(row["facility_id"])
        for row in manifest.get("seller_warehouse_mappings") or []
    }
    sku_by_identity = {
        (int(row["nm_id"]), int(row["chrt_id"])): int(
            row.get("target_nm_id", row["nm_id"])
        )
        for row in manifest.get("sku_mappings") or []
    }
    locations: set[tuple[str, int]] = set()
    for row in target_rows:
        facility = facility_by_warehouse.get(int(row["warehouse_id"]))
        nm_id = sku_by_identity.get((int(row["nm_id"]), int(row["chrt_id"])))
        if facility and nm_id:
            locations.add((facility, nm_id))
    evidence: list[dict[str, Any]] = []
    for facility_id, nm_id in sorted(locations):
        row = conn.execute(
            f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool='FBS' AND nm_id=?",
            (facility_id, nm_id),
        ).fetchone()
        if row is None or int(row[0]) <= 0 or Decimal(str(row[1])) <= 0:
            evidence.append(
                {"facility_id": facility_id, "pool": "FBS", "nm_id": nm_id, "wac_rub": None}
            )
            continue
        with localcontext() as context:
            context.prec = 160
            wac = Decimal(str(row[1])) / Decimal(int(row[0]))
        evidence.append(
            {
                "facility_id": facility_id,
                "pool": "FBS",
                "nm_id": nm_id,
                "wac_rub": canonical_decimal_text(wac),
            }
        )
    return evidence


def _past_fulfilled_invariant(
    conn: sqlite3.Connection, *, pinned_max: int | None = None
) -> dict[str, Any]:
    maximum = (
        int(pinned_max)
        if pinned_max is not None
        else int(
            conn.execute(
                f"SELECT COALESCE(MAX(event_sequence),0) FROM {EVENTS_TABLE}"
            ).fetchone()[0]
        )
    )
    cursor = conn.execute(
        f"""SELECT event_sequence,event_id,cutover_id,order_id,
                   source_status_observation_sequence,facility_id,pool,nm_id,
                   quantity,physical_quantity_delta,capital_delta_rub,
                   frozen_wac_rub,evidence_digest
            FROM {EVENTS_TABLE}
            WHERE event_type IN ('opening_handoff_debit','handoff_debit')
              AND event_sequence<=? ORDER BY event_sequence""",
        (maximum,),
    )
    digest = hashlib.sha256()
    _update_streaming_digest(
        digest,
        {
            "contract": "ff_pool_fbs_past_fulfilled_stream_v1",
            "pinned_event_sequence_max": maximum,
        },
    )
    count = 0
    while True:
        rows = cursor.fetchmany(PROJECTION_CHUNK_SIZE)
        if not rows:
            break
        for row in rows:
            _update_streaming_digest(digest, dict(row))
            count += 1
    return {
        "pinned_event_sequence_max": maximum,
        "count": count,
        "digest": "sha256:" + digest.hexdigest(),
        "digest_contract": "canonical_length_delimited_stream_v1",
    }


def _preview_recovery(
    query: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    occurred_at: str,
    scratch_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if int(query.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise FfPoolFbsForwardRecoveryError(
            "preview_source_not_query_only",
            "Recovery preview source must remain SQLite query-only",
        )
    if not query.in_transaction:
        raise FfPoolFbsForwardRecoveryError(
            "preview_source_not_coherent",
            "Recovery preview requires one explicit coherent SQLite read snapshot",
        )
    root = _prepare_private_scratch_root(Path(scratch_root))
    minimum_free = PROJECTION_MAX_SCRATCH_BYTES + PROJECTION_DISK_RESERVE_BYTES
    if int(shutil.disk_usage(root).free) < minimum_free:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_disk_capacity_insufficient",
            "Private coherent preview scratch lacks bounded free disk capacity",
            details={"minimum_free_bytes": minimum_free},
        )
    descriptor, scratch_name = tempfile.mkstemp(
        prefix="coherent-preview-",
        suffix=".sqlite3",
        dir=root,
    )
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    scratch_path = Path(scratch_name).resolve()
    sandbox = sqlite3.connect(scratch_path, timeout=60.0)
    sandbox.row_factory = sqlite3.Row
    try:
        sandbox.execute("PRAGMA journal_mode=DELETE")
        sandbox.execute("PRAGMA temp_store=FILE")
        sandbox.execute("PRAGMA foreign_keys=OFF")
        projection = _build_preview_projection(
            query,
            sandbox,
            source=source,
            scratch_path=scratch_path,
        )
        before_balances = _balance_payload(sandbox)
        recovery = recover_pinned_fbs_lifecycle(
            sandbox,
            manifest=_active_manifest(sandbox)["manifest"],
            status_observation_sequences=tuple(int(value) for value in source["target_sequences"]),
            occurred_at=occurred_at,
        )
        after_balances = _balance_payload(sandbox)
        result = _target_result_payload(
            sandbox, tuple(int(value) for value in source["target_sequences"])
        )
        preview = _preview_payload(
                recovery=recovery,
                before_balances=before_balances,
                after_balances=after_balances,
                target_result=result,
            )
        return preview, projection, result
    finally:
        sandbox.close()
        _remove_private_scratch(scratch_path)


def _build_preview_projection(
    source_conn: sqlite3.Connection,
    scratch: sqlite3.Connection,
    *,
    source: Mapping[str, Any],
    scratch_path: Path,
) -> dict[str, Any]:
    """Copy the coherent lifecycle dependency graph into bounded disk scratch.

    The production connection remains ``mode=ro``/``query_only`` inside one
    explicit read transaction.  The private file-backed scratch contains the
    exact source schema, indexes and triggers for every table touched by the
    canonical lifecycle, but only the pinned ``<= C`` dependency rows.  It is
    therefore apply-equivalent without copying the multi-gigabyte operational
    store or materializing it in RAM.
    """

    tracker = _ProjectionTracker()
    for table in PREVIEW_SCHEMA_TABLES:
        _create_projection_table(source_conn, scratch, table)

    cutover_id = str(source["cutover_id"])
    target_rows = [dict(row) for row in source["target_rows"]]
    sequences = tuple(int(value) for value in source["target_sequences"])
    order_ids = tuple(sorted({int(row["order_id"]) for row in target_rows}))

    _copy_projection_rows(
        source_conn,
        scratch,
        MANIFESTS_TABLE,
        tracker,
        where="cutover_id=?",
        parameters=(cutover_id,),
    )
    _copy_projection_rows(source_conn, scratch, FACILITIES_TABLE, tracker)
    _copy_projection_rows(source_conn, scratch, FEATURE_EPOCHS_TABLE, tracker)
    _copy_projection_rows(source_conn, scratch, BALANCES_TABLE, tracker)
    _copy_projection_rows(source_conn, scratch, WAREHOUSE_MAPPINGS_TABLE, tracker)
    _copy_projection_rows(source_conn, scratch, IDENTITY_MAPPINGS_TABLE, tracker)
    _copy_projection_rows(
        source_conn,
        scratch,
        MAPPING_EXTENSIONS_TABLE,
        tracker,
        where="cutover_id=?",
        parameters=(cutover_id,),
    )
    extension_ids = tuple(
        str(row[0])
        for row in source_conn.execute(
            f"SELECT extension_id FROM {MAPPING_EXTENSIONS_TABLE} "
            "WHERE cutover_id=? ORDER BY extension_id",
            (cutover_id,),
        ).fetchall()
    )
    _copy_projection_in(
        source_conn,
        scratch,
        MAPPING_EXTENSION_ALLOCATIONS_TABLE,
        "extension_id",
        extension_ids,
        tracker,
    )

    _copy_projection_in(
        source_conn,
        scratch,
        STATUS_OBSERVATIONS_TABLE,
        "observation_sequence",
        sequences,
        tracker,
    )
    for table in (
        OBSERVATIONS_TABLE,
        IDENTITY_EVIDENCE_TABLE,
        EVENTS_TABLE,
        CURRENT_TABLE,
        RECONCILIATION_TABLE,
        LATE_EVIDENCE_TABLE,
        CUTOVER_ORDERS_TABLE,
        CUTOVER_LATE_CASES_TABLE,
    ):
        _copy_projection_in(
            source_conn,
            scratch,
            table,
            "order_id",
            order_ids,
            tracker,
        )
    for table in (IDENTITY_PENDING_TABLE, IDENTITY_PENDING_RESOLUTIONS_TABLE):
        _copy_projection_in(
            source_conn,
            scratch,
            table,
            "source_status_observation_sequence",
            sequences,
            tracker,
        )
    _copy_projection_rows(
        source_conn,
        scratch,
        DRAIN_STATE_TABLE,
        tracker,
        where="cutover_id=?",
        parameters=(cutover_id,),
    )
    _copy_projection_rows(
        source_conn,
        scratch,
        WAREHOUSE_DOMAIN_EVENTS_TABLE,
        tracker,
        where=(
            "event_sequence=(SELECT MAX(event_sequence) "
            f"FROM {WAREHOUSE_DOMAIN_EVENTS_TABLE})"
        ),
    )

    active = source_conn.execute(
        f"SELECT version_id FROM {FUNCTIONAL_ACTIVE_TABLE} WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolFbsForwardRecoveryError(
            "aggregate_active_missing",
            "Aggregate FF version is missing from the preview source",
        )
    active_version = str(active[0])
    _copy_projection_rows(
        source_conn,
        scratch,
        FUNCTIONAL_ACTIVE_TABLE,
        tracker,
        where="slot=1",
    )
    _copy_projection_rows(
        source_conn,
        scratch,
        FUNCTIONAL_BALANCES_TABLE,
        tracker,
        where="version_id=? AND warehouse_key='ff'",
        parameters=(active_version,),
    )

    watermarks = tuple(
        sorted(
            {
                str(row[0])
                for row in source_conn.execute(
                    f"SELECT source_watermark FROM {BALANCES_TABLE} "
                    "WHERE length(trim(source_watermark))>0"
                ).fetchall()
            }
        )
    )
    operation_rowids: set[int] = set()
    for batch in _chunks(watermarks, PROJECTION_CHUNK_SIZE):
        placeholders = ",".join("?" for _ in batch)
        operation_rowids.update(
            int(row[0])
            for row in source_conn.execute(
                f"SELECT rowid FROM {OPERATIONS_TABLE} WHERE "
                f"operation_id IN ({placeholders}) OR "
                f"source_revision IN ({placeholders})",
                (*batch, *batch),
            ).fetchall()
        )
    maximum_operation_rowid = int(
        source_conn.execute(
            f"SELECT COALESCE(MAX(rowid),0) FROM {OPERATIONS_TABLE}"
        ).fetchone()[0]
    )
    if maximum_operation_rowid:
        operation_rowids.add(maximum_operation_rowid)
    _copy_projection_in(
        source_conn,
        scratch,
        OPERATIONS_TABLE,
        "rowid",
        tuple(sorted(operation_rowids)),
        tracker,
    )
    operation_ids = tuple(
        str(row[0])
        for row in source_conn.execute(
            f"SELECT operation_id FROM {OPERATIONS_TABLE} WHERE rowid IN "
            f"({','.join('?' for _ in sorted(operation_rowids))}) "
            "ORDER BY rowid",
            tuple(sorted(operation_rowids)),
        ).fetchall()
    ) if operation_rowids else ()
    _copy_projection_in(
        source_conn,
        scratch,
        LINES_TABLE,
        "operation_id",
        operation_ids,
        tracker,
    )

    _seed_projection_autoincrement(
        source_conn,
        scratch,
        table_names=(EVENTS_TABLE,),
    )

    scratch.commit()
    _create_projection_indexes_and_triggers(source_conn, scratch)
    scratch.commit()
    scratch.execute("PRAGMA foreign_keys=ON")
    foreign_key_violations = scratch.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_violations:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_foreign_key_drift",
            "Coherent preview dependency graph fails exact foreign-key readback",
            details={"violation_count": len(foreign_key_violations)},
        )
    expected_schema = dict(source["projection_schema_evidence"])
    actual_schema = _projection_schema_evidence(scratch)
    if actual_schema != expected_schema:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_schema_drift",
            "Coherent preview schema/index/trigger digest differs from production",
            details={
                "expected_digest": expected_schema.get("digest"),
                "actual_digest": actual_schema.get("digest"),
            },
        )
    scratch_bytes = int(Path(scratch_path).stat().st_size)
    if scratch_bytes > PROJECTION_MAX_SCRATCH_BYTES:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_disk_bound_exceeded",
            "Pinned recovery scratch exceeds the bounded-disk contract",
            details={
                "scratch_bytes": scratch_bytes,
                "max_scratch_bytes": PROJECTION_MAX_SCRATCH_BYTES,
            },
        )
    return {
        "contract": "ff_pool_fbs_coherent_dependency_snapshot_v3",
        "source_open_mode": "ro",
        "source_query_only": True,
        "source_explicit_read_transaction": True,
        "scratch_backend": "private_file_backed_coherent_dependency_snapshot",
        "scratch_file_mode": "0600",
        "scratch_journal_mode": "delete",
        "scratch_temp_store": "file",
        "scratch_removed_after_preview": True,
        "whole_database_backup": False,
        "full_relevant_schema_cloned": True,
        "schema_digest_equal": True,
        "schema_evidence": actual_schema,
        "foreign_key_check": "pass",
        "canonical_write_seeds": dict(source["canonical_write_seeds"]),
        "chunk_size": PROJECTION_CHUNK_SIZE,
        "copied_table_count": len(tracker.table_rows),
        "copied_row_count": tracker.row_count,
        "copied_payload_bytes": tracker.payload_bytes,
        "scratch_bytes": scratch_bytes,
        "max_row_count": PROJECTION_MAX_ROW_COUNT,
        "max_payload_bytes": PROJECTION_MAX_PAYLOAD_BYTES,
        "max_scratch_bytes": PROJECTION_MAX_SCRATCH_BYTES,
        "minimum_free_disk_bytes": (
            PROJECTION_MAX_SCRATCH_BYTES + PROJECTION_DISK_RESERVE_BYTES
        ),
        "table_row_counts": dict(sorted(tracker.table_rows.items())),
    }


class _ProjectionTracker:
    def __init__(self) -> None:
        self.row_count = 0
        self.payload_bytes = 0
        self.table_rows: dict[str, int] = {}

    def add(self, table: str, rows: Sequence[Sequence[Any]]) -> None:
        row_count = len(rows)
        payload_bytes = sum(
            sum(_projection_cell_size(value) for value in row) for row in rows
        )
        next_rows = self.row_count + row_count
        next_bytes = self.payload_bytes + payload_bytes
        if (
            next_rows > PROJECTION_MAX_ROW_COUNT
            or next_bytes > PROJECTION_MAX_PAYLOAD_BYTES
        ):
            raise FfPoolFbsForwardRecoveryError(
                "preview_projection_memory_bound_exceeded",
                "Pinned recovery projection exceeds its row/payload bound",
                details={
                    "row_count": next_rows,
                    "max_row_count": PROJECTION_MAX_ROW_COUNT,
                    "payload_bytes": next_bytes,
                    "max_payload_bytes": PROJECTION_MAX_PAYLOAD_BYTES,
                },
            )
        self.row_count = next_rows
        self.payload_bytes = next_bytes
        self.table_rows[table] = self.table_rows.get(table, 0) + row_count


def _prepare_private_scratch_root(path: Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise FfPoolFbsForwardRecoveryError(
            "preview_scratch_root_invalid",
            "Private coherent preview scratch root cannot be a symlink",
        )
    root = candidate.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise FfPoolFbsForwardRecoveryError(
            "preview_scratch_root_invalid",
            "Private coherent preview scratch root is unavailable",
        )
    os.chmod(root, 0o700)
    if root.stat().st_mode & 0o077:
        raise FfPoolFbsForwardRecoveryError(
            "preview_scratch_root_not_private",
            "Private coherent preview scratch root must be mode 0700",
        )
    return root


def _remove_private_scratch(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm", ""):
        candidate = Path(str(path) + suffix)
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue


def _projection_schema_objects(conn: sqlite3.Connection) -> list[dict[str, str]]:
    table_names = set(PREVIEW_SCHEMA_TABLES)
    rows = conn.execute(
        """SELECT type,name,tbl_name,sql FROM sqlite_master
           WHERE sql IS NOT NULL AND type IN ('table','index','trigger')
           ORDER BY CASE type WHEN 'table' THEN 1 WHEN 'index' THEN 2 ELSE 3 END,
                    name"""
    ).fetchall()
    return [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": str(row[3]),
        }
        for row in rows
        if str(row[2]) in table_names
        and not str(row[1]).startswith("sqlite_autoindex_")
    ]


def _projection_schema_evidence(conn: sqlite3.Connection) -> dict[str, Any]:
    objects = _projection_schema_objects(conn)
    table_names = {item["name"] for item in objects if item["type"] == "table"}
    missing = sorted(set(PREVIEW_SCHEMA_TABLES) - table_names)
    if missing:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_schema_missing",
            "Required coherent preview schema is incomplete",
            details=missing,
        )
    counts = {
        kind: sum(1 for item in objects if item["type"] == kind)
        for kind in ("table", "index", "trigger")
    }
    return {
        "contract": "ff_pool_fbs_relevant_sqlite_schema_v1",
        "digest": _fingerprint(objects),
        "table_count": counts["table"],
        "index_count": counts["index"],
        "trigger_count": counts["trigger"],
    }


def _canonical_write_seeds(conn: sqlite3.Connection) -> dict[str, Any]:
    event_sequence_max = int(
        conn.execute(
            f"SELECT COALESCE(MAX(event_sequence),0) FROM {EVENTS_TABLE}"
        ).fetchone()[0]
    )
    sqlite_sequence = 0
    if "sqlite_sequence" in _table_names(conn):
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?",
            (EVENTS_TABLE,),
        ).fetchone()
        sqlite_sequence = int(row[0]) if row is not None else 0
    operation_rowid_max = int(
        conn.execute(
            f"SELECT COALESCE(MAX(rowid),0) FROM {OPERATIONS_TABLE}"
        ).fetchone()[0]
    )
    if sqlite_sequence < event_sequence_max:
        raise FfPoolFbsForwardRecoveryError(
            "canonical_write_sequence_invalid",
            "Lifecycle AUTOINCREMENT seed is behind durable event sequence",
        )
    return {
        "contract": "ff_pool_fbs_canonical_write_seeds_v1",
        "lifecycle_event_sequence_max": event_sequence_max,
        "lifecycle_event_autoincrement": sqlite_sequence,
        "warehouse_operation_rowid_max": operation_rowid_max,
    }


def _create_projection_table(
    source: sqlite3.Connection,
    scratch: sqlite3.Connection,
    table: str,
) -> None:
    row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if row is None or not str(row[0] or "").strip():
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_schema_missing",
            f"Required preview table schema is missing: {table}",
        )
    scratch.execute(str(row[0]))


def _create_projection_indexes_and_triggers(
    source: sqlite3.Connection,
    scratch: sqlite3.Connection,
) -> None:
    for item in _projection_schema_objects(source):
        if item["type"] == "table":
            continue
        scratch.execute(item["sql"])


def _seed_projection_autoincrement(
    source: sqlite3.Connection,
    scratch: sqlite3.Connection,
    *,
    table_names: Sequence[str],
) -> None:
    if "sqlite_sequence" not in _table_names(scratch):
        return
    for table in table_names:
        row = source.execute(
            "SELECT seq FROM sqlite_sequence WHERE name=?",
            (str(table),),
        ).fetchone()
        scratch.execute("DELETE FROM sqlite_sequence WHERE name=?", (str(table),))
        if row is not None:
            scratch.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",
                (str(table), int(row[0])),
            )


def _copy_projection_in(
    source: sqlite3.Connection,
    scratch: sqlite3.Connection,
    table: str,
    column: str,
    values: Iterable[Any],
    tracker: _ProjectionTracker,
) -> None:
    ordered = tuple(dict.fromkeys(values))
    for batch in _chunks(ordered, PROJECTION_CHUNK_SIZE):
        placeholders = ",".join("?" for _ in batch)
        _copy_projection_rows(
            source,
            scratch,
            table,
            tracker,
            where=f'{_quote_identifier(column)} IN ({placeholders})',
            parameters=tuple(batch),
        )


def _copy_projection_rows(
    source: sqlite3.Connection,
    scratch: sqlite3.Connection,
    table: str,
    tracker: _ProjectionTracker,
    *,
    where: str = "",
    parameters: Sequence[Any] = (),
) -> None:
    table_sql_row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if table_sql_row is None:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_schema_missing",
            f"Required preview table schema is missing: {table}",
        )
    table_sql = str(table_sql_row[0] or "")
    info = source.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    columns = tuple(str(row[1]) for row in info)
    if not columns:
        raise FfPoolFbsForwardRecoveryError(
            "preview_projection_schema_missing",
            f"Required preview table has no columns: {table}",
        )
    without_rowid = "WITHOUT ROWID" in table_sql.upper()
    selected_columns = tuple(_quote_identifier(column) for column in columns)
    select_prefix = "" if without_rowid else "rowid AS __projection_rowid__,"
    pk_columns = tuple(
        str(row[1])
        for row in sorted(info, key=lambda item: int(item[5] or 0))
        if int(row[5] or 0)
    )
    order_by = ",".join(_quote_identifier(value) for value in pk_columns)
    if not order_by and not without_rowid:
        order_by = "rowid"
    sql = (
        f"SELECT {select_prefix}{','.join(selected_columns)} "
        f"FROM {_quote_identifier(table)}"
    )
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    cursor = source.execute(sql, tuple(parameters))
    insert_columns = columns if without_rowid else ("rowid", *columns)
    insert_sql = (
        f"INSERT INTO {_quote_identifier(table)}("
        + ",".join(_quote_identifier(value) for value in insert_columns)
        + ") VALUES("
        + ",".join("?" for _ in insert_columns)
        + ")"
    )
    while True:
        rows = cursor.fetchmany(PROJECTION_CHUNK_SIZE)
        if not rows:
            break
        material = [tuple(row) for row in rows]
        tracker.add(table, material)
        scratch.executemany(insert_sql, material)


def _chunks(values: Sequence[Any], size: int) -> Iterable[tuple[Any, ...]]:
    for offset in range(0, len(values), int(size)):
        yield tuple(values[offset : offset + int(size)])


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _projection_cell_size(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, bytes):
        return len(value)
    return len(str(value).encode("utf-8"))


def _preview_payload(
    *,
    recovery: Mapping[str, Any],
    before_balances: list[dict[str, Any]],
    after_balances: list[dict[str, Any]],
    target_result: list[dict[str, Any]],
) -> dict[str, Any]:
    before = {(row["facility_id"], row["pool"], row["nm_id"]): row for row in before_balances}
    after = {(row["facility_id"], row["pool"], row["nm_id"]): row for row in after_balances}
    deltas: list[dict[str, Any]] = []
    total_quantity = 0
    total_capital = Decimal("0")
    for key in sorted(set(before) | set(after)):
        left = before.get(key, {"quantity": 0, "capital_rub": "0"})
        right = after.get(key, {"quantity": 0, "capital_rub": "0"})
        quantity_delta = int(right["quantity"]) - int(left["quantity"])
        capital_delta = Decimal(str(right["capital_rub"])) - Decimal(str(left["capital_rub"]))
        if quantity_delta or capital_delta:
            deltas.append(
                {
                    "facility_id": key[0],
                    "pool": key[1],
                    "nm_id": key[2],
                    "quantity_delta": quantity_delta,
                    "capital_delta_rub": canonical_decimal_text(capital_delta),
                }
            )
            total_quantity += quantity_delta
            total_capital += capital_delta
    outcome_counts: dict[str, int] = {}
    for row in target_result:
        outcome = str(row["outcome"])
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
    return {
        "target_count": len(target_result),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "lifecycle_summary": dict(recovery["summary"]),
        "balance_deltas": deltas,
        "total_quantity_delta": total_quantity,
        "total_capital_delta_rub": canonical_decimal_text(total_capital),
        "target_result_digest": _fingerprint(target_result),
        "wb_write_count": 0,
    }


def _stable_business_effect(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only numeric text scale in the reviewed business effect.

    Technical generation/freshness timestamps are already absent from
    ``_preview_payload``.  Facility, pool, SKU, outcome, quantity, capital,
    event/evidence digests and debit identities remain exact.
    """

    def normalize(item: Any, *, field: str = "") -> Any:
        if isinstance(item, Mapping):
            return {
                str(key): normalize(child, field=str(key))
                for key, child in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, list):
            return [normalize(child, field=field) for child in item]
        if field.endswith("_rub") and item is not None:
            return canonical_decimal_text(Decimal(str(item)))
        return item

    normalized = normalize(dict(value))
    if not isinstance(normalized, dict):  # pragma: no cover - Mapping guarantees it.
        raise TypeError("stable business effect must be an object")
    return normalized


def _privacy_safe_target_effect(
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for row in rows:
        target_key = _fingerprint(
            {
                "status_observation_sequence": int(
                    row.get("status_observation_sequence") or 0
                ),
                "order_id": int(row.get("order_id") or 0),
            }
        )
        events = []
        for event in list(row.get("events") or []):
            material = dict(event)
            events.append(
                {
                    key: material.get(key)
                    for key in (
                        "event_type",
                        "facility_id",
                        "pool",
                        "nm_id",
                        "quantity",
                        "physical_quantity_delta",
                        "capital_delta_rub",
                        "frozen_wac_rub",
                        "evidence_digest",
                    )
                }
            )
        safe.append(
            {
                "target_key_digest": target_key,
                "outcome": str(row.get("outcome") or ""),
                "events": events,
                "pending_reason": str(row.get("pending_reason") or ""),
                "late_evidence_digest": str(
                    row.get("late_evidence_digest") or ""
                ),
            }
        )
    return safe


def _field_level_diffs(expected: Any, actual: Any, *, path: str = "$") -> list[dict[str, Any]]:
    diffs: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, current: str) -> None:
        if len(diffs) >= MAX_PRIVACY_SAFE_DIFF_COUNT:
            raise FfPoolFbsForwardRecoveryError(
                "after_image_diff_bound_exceeded",
                "Privacy-safe after-image diff exceeds its deterministic bound",
                details={"max_diff_count": MAX_PRIVACY_SAFE_DIFF_COUNT},
            )
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            for key in sorted(set(left) | set(right), key=str):
                next_path = f"{current}.{key}"
                if key not in left:
                    diffs.append(
                        {"path": next_path, "kind": "unexpected", "actual": right[key]}
                    )
                elif key not in right:
                    diffs.append(
                        {"path": next_path, "kind": "missing", "expected": left[key]}
                    )
                else:
                    visit(left[key], right[key], next_path)
            return
        if isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                next_path = f"{current}[{index}]"
                if index >= len(left):
                    diffs.append(
                        {"path": next_path, "kind": "unexpected", "actual": right[index]}
                    )
                elif index >= len(right):
                    diffs.append(
                        {"path": next_path, "kind": "missing", "expected": left[index]}
                    )
                else:
                    visit(left[index], right[index], next_path)
            return
        if left != right:
            diffs.append(
                {
                    "path": current,
                    "kind": "changed",
                    "expected": left,
                    "actual": right,
                }
            )

    visit(expected, actual, path)
    return diffs


def _build_privacy_safe_drift_evidence(
    *,
    phase: str,
    manifest_fingerprint: str,
    deployed_sha: str,
    expected_effect: Mapping[str, Any],
    actual_effect: Mapping[str, Any],
    expected_target_result: Iterable[Mapping[str, Any]],
    actual_target_result: Iterable[Mapping[str, Any]],
    captured_at: str,
) -> dict[str, Any]:
    expected_safe = _privacy_safe_target_effect(expected_target_result)
    actual_safe = _privacy_safe_target_effect(actual_target_result)
    effect_diffs = _field_level_diffs(expected_effect, actual_effect, path="$.effect")
    target_diffs = _field_level_diffs(
        expected_safe,
        actual_safe,
        path="$.targets",
    )
    diffs = [*effect_diffs, *target_diffs]
    return {
        "contract": "ff_pool_fbs_privacy_safe_after_image_drift_v1",
        "phase": str(phase),
        "manifest_fingerprint": str(manifest_fingerprint),
        "deployed_sha": str(deployed_sha),
        "captured_at": str(captured_at),
        "privacy": {
            "order_ids_included": False,
            "status_sequences_included": False,
            "pii_included": False,
            "target_identity": "sha256_digest_only",
        },
        "expected_effect_digest": _fingerprint(expected_effect),
        "actual_effect_digest": _fingerprint(actual_effect),
        "expected_target_effect_digest": _fingerprint(expected_safe),
        "actual_target_effect_digest": _fingerprint(actual_safe),
        "diff_count": len(diffs),
        "diffs": diffs,
    }


def _target_result_payload(
    conn: sqlite3.Connection, sequences: tuple[int, ...]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for sequence_batch in _chunks(sequences, PROJECTION_CHUNK_SIZE):
        placeholders = ",".join("?" for _ in sequence_batch)
        status_rows = conn.execute(
            f"""SELECT observation_sequence,order_id
                FROM {STATUS_OBSERVATIONS_TABLE}
                WHERE observation_sequence IN ({placeholders})
                ORDER BY observation_sequence""",
            sequence_batch,
        ).fetchall()
        if len(status_rows) != len(sequence_batch):
            raise FfPoolFbsForwardRecoveryError(
                "target_status_missing_after_apply",
                "One or more target statuses disappeared after apply",
            )
        pending_by_sequence = {
            int(row[0]): row
            for row in conn.execute(
                f"""SELECT pending.source_status_observation_sequence,
                           pending.pending_id,pending.reason_detail_code,
                           resolution.resolution_id
                    FROM {IDENTITY_PENDING_TABLE} AS pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE pending.source_status_observation_sequence
                          IN ({placeholders})""",
                sequence_batch,
            ).fetchall()
        }
        events_by_sequence: dict[int, list[dict[str, Any]]] = {}
        for row in conn.execute(
            f"""SELECT source_status_observation_sequence,event_id,event_type,
                       facility_id,pool,nm_id,quantity,physical_quantity_delta,
                       capital_delta_rub,frozen_wac_rub,evidence_digest
                FROM {EVENTS_TABLE}
                WHERE source_status_observation_sequence IN ({placeholders})
                ORDER BY source_status_observation_sequence,event_sequence""",
            sequence_batch,
        ).fetchall():
            events_by_sequence.setdefault(int(row[0]), []).append(
                {
                    "event_id": str(row[1]),
                    "event_type": str(row[2]),
                    "source_status_observation_sequence": int(row[0]),
                    "facility_id": str(row[3]),
                    "pool": str(row[4]),
                    "nm_id": int(row[5]),
                    "quantity": int(row[6]),
                    "physical_quantity_delta": int(row[7]),
                    "capital_delta_rub": str(row[8]),
                    "frozen_wac_rub": str(row[9]),
                    "evidence_digest": str(row[10]),
                }
            )
        late_by_sequence = {
            int(row[0]): str(row[1])
            for row in conn.execute(
                f"""SELECT source_status_observation_sequence,evidence_digest
                    FROM {LATE_EVIDENCE_TABLE}
                    WHERE source_status_observation_sequence IN ({placeholders})""",
                sequence_batch,
            ).fetchall()
        }
        for status in status_rows:
            sequence = int(status[0])
            order_id = int(status[1])
            pending = pending_by_sequence.get(sequence)
            events = events_by_sequence.get(sequence, [])
            late = late_by_sequence.get(sequence, "")
            if pending is not None and pending[3] is None:
                outcome = "identity_quarantine"
            elif events:
                outcome = "event_applied"
            elif late:
                outcome = "audit_noop"
            else:
                outcome = "already_current"
            results.append(
                {
                    "status_observation_sequence": sequence,
                    "order_id": order_id,
                    "outcome": outcome,
                    "events": events,
                    "pending_reason": (
                        str(pending[2])
                        if pending is not None and pending[3] is None
                        else ""
                    ),
                    "late_evidence_digest": late,
                }
            )
    return results


def _balance_payload(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "facility_id": str(row[0]),
            "pool": str(row[1]),
            "nm_id": int(row[2]),
            "quantity": int(row[3]),
            "capital_rub": canonical_decimal_text(Decimal(str(row[4]))),
        }
        for row in conn.execute(
            f"SELECT facility_id,pool,nm_id,quantity,capital_rub FROM {BALANCES_TABLE} "
            "ORDER BY facility_id,pool,nm_id"
        ).fetchall()
    ]


def _source_from_reviewed(reviewed: Mapping[str, Any]) -> dict[str, Any]:
    boundary = dict(reviewed["boundary"])
    target = dict(reviewed["target"])
    planner = dict(reviewed["planner"])
    return {
        "deployed_sha": str(reviewed["deployed_sha"]),
        "storage": dict(reviewed["storage"]),
        "cutover_id": str(boundary["cutover_id"]),
        "cutover_manifest_digest": str(boundary["active_cutover_manifest_digest"]),
        "cutoff_sequence": int(boundary["source_max_status_observation_sequence"]),
        "old_cursor_sequence": int(boundary["old_lifecycle_cursor_sequence"]),
        "target_sequences": [int(value) for value in target["status_observation_sequences"]],
        "target_rows": list(target["rows"]),
        "stable_target_digest": str(target["stable_business_digest"]),
        "location_wac_evidence": list(target["location_wac_evidence"]),
        "past_fulfilled_invariant": dict(reviewed["past_fulfilled_invariant"]),
        "projection_schema_evidence": dict(planner["source_schema_evidence"]),
        "canonical_write_seeds": dict(planner["canonical_write_seeds"]),
        "blockers": [],
    }


def _verify_target_source(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if actual["blockers"]:
        raise FfPoolFbsForwardRecoveryError(
            "target_source_blocked", "Recovery target is no longer eligible", details=actual["blockers"]
        )
    fields = (
        "deployed_sha",
        "cutover_id",
        "cutover_manifest_digest",
        "cutoff_sequence",
        "old_cursor_sequence",
        "target_sequences",
        "stable_target_digest",
        "target_rows",
        "location_wac_evidence",
        "past_fulfilled_invariant",
        "projection_schema_evidence",
        "canonical_write_seeds",
    )
    drift = [field for field in fields if actual.get(field) != expected.get(field)]
    if drift:
        raise FfPoolFbsForwardRecoveryError(
            "target_source_drift",
            "Pinned <= C recovery business evidence changed",
            details=drift,
        )


def _verify_storage(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> None:
    if dict(expected) != dict(actual):
        raise FfPoolFbsForwardRecoveryError(
            "storage_generation_drift", "Operational storage generation/schema changed"
        )


def _active_manifest(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT cutover_id,manifest_json FROM {MANIFESTS_TABLE} "
        "ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise FfPoolFbsForwardRecoveryError(
            "cutover_not_applied", "Active Stage 7C cutover is unavailable"
        )
    return {"cutover_id": str(row[0]), "manifest": json.loads(str(row[1]))}


def _open_query_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise FfPoolFbsForwardRecoveryError(
            "query_only_not_enforced", "SQLite query-only mode could not be enabled"
        )
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            key: value
            for key, value in plan.items()
            if key not in {"fingerprint", "generated_at"}
        }
    )


def _require_digest(value: str, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        raise FfPoolFbsForwardRecoveryError(
            "invalid_digest", f"{field} must be sha256:<64 lowercase hex>"
        )
    return normalized


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
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _streaming_fingerprint(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        _update_streaming_digest(digest, value)
    return "sha256:" + digest.hexdigest()


def _update_streaming_digest(digest: Any, value: Any) -> None:
    payload = _json(value).encode("utf-8")
    digest.update(len(payload).to_bytes(8, byteorder="big", signed=False))
    digest.update(payload)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_utc(value: str) -> None:
    normalized = str(value)
    if not normalized.endswith("Z"):
        raise FfPoolFbsForwardRecoveryError("invalid_timestamp", "UTC timestamp must end in Z")
    try:
        datetime.fromisoformat(normalized[:-1] + "+00:00")
    except ValueError as exc:
        raise FfPoolFbsForwardRecoveryError("invalid_timestamp", "Invalid UTC timestamp") from exc


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
