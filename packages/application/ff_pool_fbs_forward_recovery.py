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
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

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
    ensure_ff_pool_fbs_lifecycle_schema,
    recover_pinned_fbs_lifecycle,
)
from packages.application.ff_pool_foundation import BALANCES_TABLE, canonical_decimal_text
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.storage_registry import manifest_payload
from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
)


CONTRACT_NAME = "ff_pool_fbs_forward_recovery_v1"
CONTRACT_VERSION = 1
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")
MAX_TARGET_COUNT = 100_000


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
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir).resolve())
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise FfPoolFbsForwardRecoveryError(
                "invalid_deployed_sha", "deployed_sha must be an exact 40-hex SHA"
            )
        self.timestamp_factory = timestamp_factory or _utc_now

    def build_plan(self) -> dict[str, Any]:
        generated_at = str(self.timestamp_factory())
        _require_utc(generated_at)
        storage = self._storage_identity()
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            source = _build_source_snapshot(
                conn,
                deployed_sha=self.deployed_sha,
                storage_identity=storage,
            )
            preview = _preview_recovery(conn, source=source, occurred_at=generated_at)
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
        now = str(self.timestamp_factory())
        _require_utc(now)
        expected_source = _source_from_reviewed(reviewed)
        storage = self._storage_identity()
        _verify_storage(dict(reviewed["storage"]), storage)

        with warehouse_functional_write_lock(self.runtime.runtime_dir, timeout_seconds=300):
            with closing(_open_query_only(self.runtime.db_path)) as query:
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
                if _fingerprint(actual_preview) != _fingerprint(reviewed["predicted_effects"]):
                    raise FfPoolFbsForwardRecoveryError(
                        "target_after_image_drift",
                        "Canonical recovery after-image differs from the reviewed target",
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
    rows = conn.execute(
        f"""SELECT observation_sequence FROM {STATUS_OBSERVATIONS_TABLE}
            WHERE observation_sequence>? AND observation_sequence<=?
            ORDER BY observation_sequence""",
        (old_cursor, cutoff),
    ).fetchall()
    sequences = [int(row[0]) for row in rows]
    if len(sequences) > MAX_TARGET_COUNT:
        blockers.append("target_count_exceeds_bound")
    target_rows = [_stable_target_row(conn, sequence, cutoff=cutoff) for sequence in sequences]
    location_wac = _target_location_wac_evidence(conn, target_rows)
    stable_target_digest = _fingerprint(
        {
            "cutover_id": cutover_id,
            "cutoff_sequence": cutoff,
            "old_cursor_sequence": old_cursor,
            "target_rows": target_rows,
            "location_wac_evidence": location_wac,
        }
    )
    return {
        "deployed_sha": deployed_sha,
        "storage": dict(storage_identity),
        "cutover_id": cutover_id,
        "cutover_manifest_digest": _fingerprint(active["manifest"]),
        "cutoff_sequence": cutoff,
        "old_cursor_sequence": old_cursor,
        "target_sequences": sequences,
        "target_rows": target_rows,
        "stable_target_digest": stable_target_digest,
        "location_wac_evidence": location_wac,
        "past_fulfilled_invariant": _past_fulfilled_invariant(
            conn, pinned_max=pinned_past_event_sequence_max
        ),
        "blockers": blockers,
    }


def _stable_target_row(
    conn: sqlite3.Connection, sequence: int, *, cutoff: int
) -> dict[str, Any]:
    row = conn.execute(
        f"""SELECT status.observation_sequence,status.order_id,status.order_revision,
                   status.status_digest,status.supplier_status,status.wb_status,
                   status.positive_quantity,source.observation_sequence,
                   source.observation_id,source.source_revision,source.source_created_at,
                   source.warehouse_id,source.office_id,source.nm_id,source.chrt_id,
                   source.seller_sku,source.skus_json
            FROM {STATUS_OBSERVATIONS_TABLE} AS status
            LEFT JOIN {OBSERVATIONS_TABLE} AS source
              ON source.order_id=status.order_id
             AND source.source_revision=status.order_revision
            WHERE status.observation_sequence=?""",
        (int(sequence),),
    ).fetchone()
    if row is None or row[7] is None:
        raise FfPoolFbsForwardRecoveryError(
            "target_source_missing", f"Pinned status {sequence} lacks its exact order revision"
        )
    identity = [
        dict(item)
        for item in conn.execute(
            f"""SELECT evidence_sequence,evidence_id,order_revision,outcome,warehouse_id,
                       nm_id,chrt_id,barcode,seller_sku,warehouse_mapping_id,
                       identity_mapping_id,evidence_digest
                FROM {IDENTITY_EVIDENCE_TABLE}
                WHERE order_id=? AND order_revision=? ORDER BY evidence_sequence""",
            (int(row[1]), str(row[2])),
        ).fetchall()
    ]
    before_state = _target_order_state(
        conn, int(row[1]), int(sequence), cutoff=cutoff
    )
    business = {
        "status_observation_sequence": int(row[0]),
        "order_id": int(row[1]),
        "order_revision": str(row[2]),
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
        "identity_evidence": identity,
    }
    return {
        **business,
        "stable_business_digest": _fingerprint(business),
        "before_state_digest": _fingerprint(before_state),
    }


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
    rows = [
        dict(row)
        for row in conn.execute(
            f"""SELECT event_sequence,event_id,cutover_id,order_id,
                       source_status_observation_sequence,facility_id,pool,nm_id,
                       quantity,physical_quantity_delta,capital_delta_rub,
                       frozen_wac_rub,evidence_digest
                FROM {EVENTS_TABLE}
                WHERE event_type IN ('opening_handoff_debit','handoff_debit')
                  AND event_sequence<=? ORDER BY event_sequence""",
            (maximum,),
        ).fetchall()
    ]
    return {"pinned_event_sequence_max": maximum, "count": len(rows), "digest": _fingerprint(rows)}


def _preview_recovery(
    query: sqlite3.Connection, *, source: Mapping[str, Any], occurred_at: str
) -> dict[str, Any]:
    sandbox = sqlite3.connect(":memory:")
    sandbox.row_factory = sqlite3.Row
    query.backup(sandbox)
    try:
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
        return _preview_payload(
            recovery=recovery,
            before_balances=before_balances,
            after_balances=after_balances,
            target_result=result,
        )
    finally:
        sandbox.close()


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


def _target_result_payload(
    conn: sqlite3.Connection, sequences: tuple[int, ...]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for sequence in sequences:
        status = conn.execute(
            f"SELECT order_id FROM {STATUS_OBSERVATIONS_TABLE} WHERE observation_sequence=?",
            (sequence,),
        ).fetchone()
        if status is None:
            raise FfPoolFbsForwardRecoveryError(
                "target_status_missing_after_apply", f"Target status {sequence} disappeared"
            )
        order_id = int(status[0])
        pending = conn.execute(
            f"""SELECT pending.pending_id,pending.reason_detail_code,
                       resolution.resolution_id
                FROM {IDENTITY_PENDING_TABLE} AS pending
                LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                  ON resolution.pending_id=pending.pending_id
                WHERE pending.source_status_observation_sequence=?""",
            (sequence,),
        ).fetchone()
        events = [
            dict(row)
            for row in conn.execute(
                f"""SELECT event_id,event_type,source_status_observation_sequence,
                           facility_id,pool,nm_id,quantity,physical_quantity_delta,
                           capital_delta_rub,frozen_wac_rub,evidence_digest
                    FROM {EVENTS_TABLE}
                    WHERE order_id=? AND source_status_observation_sequence=?
                    ORDER BY event_sequence""",
                (order_id, sequence),
            ).fetchall()
        ]
        late = conn.execute(
            f"SELECT evidence_digest FROM {LATE_EVIDENCE_TABLE} "
            "WHERE order_id=? AND source_status_observation_sequence=?",
            (order_id, sequence),
        ).fetchone()
        if pending is not None and pending[2] is None:
            outcome = "identity_quarantine"
        elif events:
            outcome = "event_applied"
        elif late is not None:
            outcome = "audit_noop"
        else:
            outcome = "already_current"
        results.append(
            {
                "status_observation_sequence": sequence,
                "order_id": order_id,
                "outcome": outcome,
                "events": events,
                "pending_reason": str(pending[1]) if pending is not None and pending[2] is None else "",
                "late_evidence_digest": str(late[0]) if late is not None else "",
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
