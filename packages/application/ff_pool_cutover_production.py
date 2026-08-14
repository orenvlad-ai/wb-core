"""Owner-gated production runner for checkpoint-frozen Stage 7C opening.

Dry-run fixes local accounting boundary ``T`` and compound sequence vector
``W`` in a query-only transaction.  Apply accepts that exact reviewed gate,
selects only the operational apply time under canonical barriers, revalidates
the frozen/business-critical source under the SQLite write lock, and delegates
the atomic opening plus post-``W`` drain to :mod:`ff_pool_cutover`.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from packages.application.ff_pool_cutover import (
    CHECKPOINTS_TABLE,
    MANIFESTS_TABLE,
    PENDING_SHIPMENTS_TABLE,
    PROPOSAL_CONTRACT,
    RECOVERY_EVENTS_TABLE,
    _fingerprint,
    apply_ff_pool_cutover,
    ff_pool_fbs_accounting_boundary_snapshot,
    ff_pool_cutover_preflight_snapshot,
    read_ff_pool_cutover_status,
)
from packages.application.ff_pool_fbs_lifecycle import (
    CURRENT_TABLE as FBS_CURRENT_TABLE,
    DRAIN_STATE_TABLE as FBS_DRAIN_STATE_TABLE,
    EVENTS_TABLE as FBS_EVENTS_TABLE,
    LATE_EVIDENCE_TABLE as FBS_LATE_EVIDENCE_TABLE,
    RECONCILIATION_TABLE as FBS_RECONCILIATION_TABLE,
    classify_pre_t_status,
)
from packages.application.ff_pool_foundation import FACILITIES_TABLE, canonical_decimal_text
from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_domain_write_guard import EVENTS_TABLE as DOMAIN_EVENTS_TABLE
from packages.application.warehouse_recovery_policy import (
    RecoveryState,
    WarehouseRecoveryRegistry,
)
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATE_TABLE as COLLECTOR_STATE_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATUS_TRANSITIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)


CONTRACT_NAME = "ff_pool_cutover_production_v1"
CONTRACT_VERSION = 2
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,159}")
ZERO = Decimal("0")


class FfPoolCutoverProductionError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class FfPoolCutoverProductionMutation:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        env_file: Path,
        deployed_sha: str,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir).resolve())
        self.env_file = Path(env_file).resolve()
        self.deployed_sha = str(deployed_sha or "").strip()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise FfPoolCutoverProductionError(
                "invalid_deployed_sha", "deployed_sha must be an exact 40-hex SHA"
            )
        self.timestamp_factory = timestamp_factory or _utc_now

    def build_gate_plan(
        self,
        *,
        excluded_shipment_ids: Sequence[str],
        opening_facility_id: str = "",
        proposed_window_minutes: int = 15,
    ) -> dict[str, Any]:
        now = str(self.timestamp_factory())
        _require_utc(now)
        shipment_ids = _shipment_ids(excluded_shipment_ids)
        advisory_minutes = max(5, min(int(proposed_window_minutes), 60))
        with _open_query_only(self.runtime.db_path) as conn:
            conn.execute("BEGIN")
            accounting_boundary = ff_pool_fbs_accounting_boundary_snapshot(
                conn, boundary_at=now
            )
            source = _build_source_snapshot(
                conn,
                deployed_sha=self.deployed_sha,
                as_of=now,
                excluded_shipment_ids=shipment_ids,
                opening_facility_id=opening_facility_id,
                accounting_boundary=accounting_boundary,
                validate_collector_current=True,
            )
            conn.rollback()
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run_owner_gate",
            "deployed_sha": self.deployed_sha,
            "generated_at": now,
            "cutover_boundary": {
                "chosen": True,
                "local_boundary_at": now,
                "timezone": "UTC",
                "kind": "durable_local_observation_sequence_and_observed_at",
                "rule": (
                    "T and compound W are frozen by the query-only manifest; "
                    "apply time is selected only after canonical barriers are held"
                ),
                "post_watermark_growth_invalidates_gate": False,
            },
            "operator_timing_advisory": {
                "minutes": advisory_minutes,
                "expires_gate": False,
                "note": "Operational scheduling only; frozen W/T remains immutable",
            },
            "source": source,
            "source_fingerprint": _fingerprint(source),
            "handoff_policy": {
                "decision": "proposed",
                "supplier_status": "complete",
                "wb_status": "sorted",
                "supplier_status_complete_alone_forbidden": True,
                "automatic_without_owner_gate": False,
                "official_semantics": (
                    "WB-controlled handoff: supplierStatus=complete AND wbStatus=sorted"
                ),
                "official_sources": [
                    "https://dev.wildberries.ru/openapi/orders-fbs/",
                    "https://dev.wildberries.ru/openapi-other/sandbox-environment",
                ],
                "observed_complete_waiting_to_complete_sorted_distinct_orders": source[
                    "observed_sorted_transition_distinct_orders"
                ],
            },
            "backup_and_recovery": {
                "backup_kind": "central_t2_domain_checkpoint_plus_exact_target_before_image",
                "backup_mode": "0600",
                "before_commit": "atomic rollback",
                "ambiguous_after_commit": "retain barriers and exact readback",
                "after_live_events": "forward reconciliation; no blind delete or replay",
                "idempotency": "cutover_id + exact manifest digest + per-order event identity",
            },
            "expected_effects": {
                "opening_allocation": source["opening_summary"],
                "historical_fbs": source["historical_fbs_summary"],
                "excluded_pending_receipts": source["excluded_pending_receipts"],
                "wb_writes": 0,
                "supplier_acceptance_writes": 0,
                "collector_remains_enabled": True,
                "post_watermark_delta": "atomic_bounded_drain_then_idempotent_collector_drain",
            },
            "apply_allowed": not source["blockers"],
            "requires_exact_owner_gate": True,
            "blockers": source["blockers"],
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
        backup_dir: Path,
        external_barrier_evidence: Mapping[str, Any],
        crash: str = "",
    ) -> dict[str, Any]:
        reviewed = dict(reviewed_plan)
        expected_gate = _require_digest(fingerprint, "fingerprint")
        if _plan_fingerprint(reviewed) != expected_gate or str(
            reviewed.get("fingerprint") or ""
        ) != expected_gate:
            raise FfPoolCutoverProductionError(
                "reviewed_fingerprint_mismatch", "Reviewed gate plan fingerprint does not match"
            )
        if str(reviewed.get("deployed_sha") or "") != self.deployed_sha:
            raise FfPoolCutoverProductionError(
                "deployed_sha_drift", "Reviewed plan belongs to another deployed SHA"
            )
        if reviewed.get("apply_allowed") is not True or list(reviewed.get("blockers") or []):
            raise FfPoolCutoverProductionError(
                "reviewed_plan_blocked", "A blocked gate plan cannot be applied"
            )
        approval = str(approval_reference or "").strip()
        operator = str(actor or "").strip()
        if not approval or not operator:
            raise FfPoolCutoverProductionError(
                "gate_identity_required", "approval_reference and actor are required"
            )
        external = _validate_external_barrier(external_barrier_evidence)
        cutover_id = "ffcut_" + expected_gate.removeprefix("sha256:")[:28]
        base_epoch_id = "ffepoch_" + expected_gate.removeprefix("sha256:")[:28]
        resumed = self._resume_if_applied(
            cutover_id=cutover_id,
            epoch_id=base_epoch_id,
            gate_fingerprint=expected_gate,
            actor=operator,
        )
        if resumed is not None:
            return resumed
        epoch_id = _next_epoch_attempt_id(self.runtime.db_path, base_epoch_id)
        source = dict(reviewed["source"])
        with _open_query_only(self.runtime.db_path) as query:
            query.execute("BEGIN")
            live_source = _build_source_snapshot(
                query,
                deployed_sha=self.deployed_sha,
                as_of=str(source["accounting_boundary"]["local_boundary_at"]),
                excluded_shipment_ids=tuple(source["excluded_shipment_ids"]),
                opening_facility_id=str(source["opening_facility_id"]),
                accounting_boundary=dict(source["accounting_boundary"]),
                validate_collector_current=False,
            )
            query.rollback()
        if _fingerprint(live_source) != str(reviewed["source_fingerprint"]):
            raise FfPoolCutoverProductionError(
                "gate_source_drift",
                "Current source changed after owner review; rebuild and reapprove the dry-run",
            )
        with _open_query_only(self.runtime.db_path) as query:
            _require_collector_current_ready(query)
        recovery_registry = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime.runtime_dir,
            db_path=self.runtime.db_path,
        )
        recovery_scope = _recovery_scope(
            cutover_id=cutover_id,
            deployed_sha=self.deployed_sha,
            gate_fingerprint=expected_gate,
        )
        recovery = recovery_registry.prepare_t2(
            mutation_kind="warehouse_opening_publication",
            plan_fingerprint=expected_gate,
            scope=recovery_scope,
            source_digest=str(reviewed["source_fingerprint"]),
            non_target_digest=str((source.get("non_target") or {}).get("digest") or ""),
            source_watermarks={
                "collector": dict(source.get("collector_checkpoint") or {}),
                "target_feature_epoch": int(source.get("target_feature_epoch") or 0),
                "excluded_shipment_ids": list(source.get("excluded_shipment_ids") or []),
            },
            schema_revision="ff_pool_cutover_production_v1",
        )
        if str(recovery.get("lifecycle") or "") == RecoveryState.VERIFIED.value:
            recovery = recovery_registry.begin_mutation(
                str(recovery["operation_id"]),
                expected_source_digest=str(reviewed["source_fingerprint"]),
            )
        if str(recovery.get("lifecycle") or "") != RecoveryState.MUTATION_RUNNING.value:
            raise FfPoolCutoverProductionError(
                "recovery_not_ready", "Warehouse-domain recovery checkpoint is not mutation-ready"
            )
        target_before_image = _write_backup(
            db_path=self.runtime.db_path,
            backup_dir=Path(backup_dir),
            fingerprint=expected_gate,
            reviewed_plan=reviewed,
            external_barrier=external,
        )
        t = str(self.timestamp_factory())
        _require_utc(t)
        barrier_digest = expected_gate
        conn = sqlite3.connect(self.runtime.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        held_committed = False
        apply_committed = False
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO {DOMAIN_EVENTS_TABLE}(
                        epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                (
                    epoch_id,
                    "held",
                    barrier_digest,
                    self.deployed_sha,
                    t,
                    operator,
                    _json(
                        {
                            "owner_gate_fingerprint": expected_gate,
                            "approval_reference": approval,
                            "external_barrier_evidence_digest": _fingerprint(external),
                        }
                    ),
                ),
            )
            conn.commit()
            held_committed = True
            proposal = _proposal_from_source(
                source=source,
                cutover_id=cutover_id,
                epoch_id=epoch_id,
                barrier_digest=barrier_digest,
                cutover_at=t,
                approval_reference=approval,
                external_barrier=external,
            )
            from packages.application.ff_pool_cutover import build_ff_pool_cutover_plan

            plan = build_ff_pool_cutover_plan(
                conn,
                proposal=proposal,
                deployed_sha=self.deployed_sha,
                cutover_at=t,
            )
            if plan["status"] != "ready" or not plan["apply_allowed"]:
                raise FfPoolCutoverProductionError(
                    "live_t_revalidation_failed",
                    "Exact live T plan is blocked",
                    details=plan["blockers"],
                )
            result = apply_ff_pool_cutover(
                conn,
                proposal=proposal,
                deployed_sha=self.deployed_sha,
                cutover_at=t,
                expected_manifest_digest=str(plan["manifest_digest"]),
                approval_reference=approval,
                actor=operator,
                crash=crash,
                frozen_source_revalidator=lambda locked_conn: self._revalidate_locked_source(
                    locked_conn,
                    reviewed_source=source,
                    expected_fingerprint=str(reviewed["source_fingerprint"]),
                ),
            )
            apply_committed = True
            if result["readback"]["status"] != "pass":
                raise FfPoolCutoverProductionError(
                    "exact_readback_failed", "Applied cutover did not pass exact readback"
                )
            recovery = recovery_registry.retain(
                str(recovery["operation_id"]),
                after_digest=str(plan["manifest_digest"]),
                non_target_digest=str((source.get("non_target") or {}).get("digest") or ""),
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO {RECOVERY_EVENTS_TABLE}(
                        cutover_id,event_type,event_at,evidence_digest,details_json
                    ) VALUES(?,?,?,?,?)""",
                (
                    cutover_id,
                    "readback_passed",
                    t,
                    str(plan["manifest_digest"]),
                    _json(
                        {
                            "target_before_image_sha256": target_before_image["sha256"],
                            "warehouse_recovery_operation_id": recovery["operation_id"],
                        }
                    ),
                ),
            )
            conn.execute(
                f"""INSERT INTO {DOMAIN_EVENTS_TABLE}(
                        epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                (
                    epoch_id,
                    "reconciled",
                    barrier_digest,
                    self.deployed_sha,
                    t,
                    operator,
                    _json({"cutover_manifest_digest": plan["manifest_digest"]}),
                ),
            )
            conn.execute(
                f"""INSERT INTO {DOMAIN_EVENTS_TABLE}(
                        epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                    ) VALUES(?,?,?,?,?,?,?)""",
                (
                    epoch_id,
                    "released",
                    barrier_digest,
                    self.deployed_sha,
                    t,
                    operator,
                    _json({"exact_readback": "pass"}),
                ),
            )
            conn.commit()
            final = read_ff_pool_cutover_status(conn)
            return {
                "contract_name": CONTRACT_NAME,
                "status": "applied_reconciled",
                "owner_gate_fingerprint": expected_gate,
                "cutover_manifest_digest": plan["manifest_digest"],
                "cutover_at": t,
                "approval_reference": approval,
                "backup": {
                    "target_before_image": target_before_image,
                    "warehouse_recovery": recovery,
                },
                "apply": result,
                "readback": final,
                "external_barrier_restore_required": True,
                "idempotent": False,
                "wb_writes": 0,
            }
        except Exception as exc:
            conn.rollback()
            try:
                current_recovery = recovery_registry.get_operation(
                    str(recovery["operation_id"])
                )
                if current_recovery is not None and str(
                    current_recovery.get("lifecycle") or ""
                ) == RecoveryState.MUTATION_RUNNING.value:
                    recovery_registry.fail_recoverable(
                        str(recovery["operation_id"]),
                        error=str(exc),
                        next_action="exact_ff_pool_cutover_readback_or_retry",
                    )
            except Exception:
                pass
            if held_committed and not apply_committed:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        f"""INSERT INTO {DOMAIN_EVENTS_TABLE}(
                                epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                            ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            epoch_id,
                            "aborted",
                            barrier_digest,
                            self.deployed_sha,
                            t,
                            operator,
                            _json({"reason": "pre_commit_revalidation_or_apply_failure"}),
                        ),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
            raise
        finally:
            conn.close()

    def _revalidate_locked_source(
        self,
        conn: sqlite3.Connection,
        *,
        reviewed_source: Mapping[str, Any],
        expected_fingerprint: str,
    ) -> None:
        """Re-hash frozen and business-critical evidence under BEGIN IMMEDIATE."""

        live = _build_source_snapshot(
            conn,
            deployed_sha=self.deployed_sha,
            as_of=str(reviewed_source["accounting_boundary"]["local_boundary_at"]),
            excluded_shipment_ids=tuple(reviewed_source["excluded_shipment_ids"]),
            opening_facility_id=str(reviewed_source["opening_facility_id"]),
            accounting_boundary=dict(reviewed_source["accounting_boundary"]),
            validate_collector_current=False,
        )
        if _fingerprint(live) != str(expected_fingerprint):
            raise FfPoolCutoverProductionError(
                "gate_source_drift",
                "Frozen or business-critical source drifted under the held write boundary",
            )

    def readback(self) -> dict[str, Any]:
        with closing(sqlite3.connect(self.runtime.db_path, timeout=30.0)) as conn:
            conn.row_factory = sqlite3.Row
            status = read_ff_pool_cutover_status(conn)
            counts = {}
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for name, table in (
                ("fbs_events", FBS_EVENTS_TABLE),
                ("fbs_current", FBS_CURRENT_TABLE),
                ("fbs_reconciliation", FBS_RECONCILIATION_TABLE),
                ("fbs_drain_state", FBS_DRAIN_STATE_TABLE),
                ("fbs_late_evidence", FBS_LATE_EVIDENCE_TABLE),
                ("excluded_pending_receipts", PENDING_SHIPMENTS_TABLE),
            ):
                counts[name] = (
                    int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    if table in tables
                    else 0
                )
            return {
                "contract_name": CONTRACT_NAME,
                "status": status.get("status"),
                "cutover": status,
                "counts": counts,
                "fbs_drain": (status.get("readback") or {}).get("fbs_drain"),
                "wb_writes": 0,
            }

    def _resume_if_applied(
        self,
        *,
        cutover_id: str,
        epoch_id: str,
        gate_fingerprint: str,
        actor: str,
    ) -> dict[str, Any] | None:
        conn = sqlite3.connect(self.runtime.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            manifest = conn.execute(
                f"SELECT manifest_digest,deployed_sha,manifest_json FROM {MANIFESTS_TABLE} WHERE cutover_id=?",
                (cutover_id,),
            ).fetchone()
            if manifest is None:
                phase = conn.execute(
                    f"SELECT phase FROM {DOMAIN_EVENTS_TABLE} WHERE epoch_id=? "
                    "ORDER BY event_sequence DESC LIMIT 1",
                    (epoch_id,),
                ).fetchone()
                if phase is not None and str(phase[0]) not in {"released", "aborted"}:
                    raise FfPoolCutoverProductionError(
                        "incomplete_epoch_requires_recovery",
                        "A prior apply owns an incomplete write epoch; exact recovery is required",
                    )
                return None
            if str(manifest[1]) != self.deployed_sha:
                raise FfPoolCutoverProductionError(
                    "applied_sha_mismatch", "Existing cutover belongs to another deployed SHA"
                )
            readback = read_ff_pool_cutover_status(conn)
            if (readback.get("readback") or {}).get("status") != "pass":
                raise FfPoolCutoverProductionError(
                    "applied_readback_requires_recovery",
                    "Existing cutover needs forward recovery before retry",
                )
            persisted_manifest = json.loads(str(manifest[2]))
            persisted_epoch_id = str(
                persisted_manifest.get("write_epoch_id") or epoch_id
            )
            phase_row = conn.execute(
                f"SELECT phase,manifest_digest FROM {DOMAIN_EVENTS_TABLE} WHERE epoch_id=? "
                "ORDER BY event_sequence DESC LIMIT 1",
                (persisted_epoch_id,),
            ).fetchone()
            if phase_row is None or str(phase_row[1]) != gate_fingerprint:
                raise FfPoolCutoverProductionError(
                    "applied_gate_identity_mismatch", "Existing cutover gate identity differs"
                )
            phase = str(phase_row[0])
            now = str(self.timestamp_factory())
            if phase == "readback_required":
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    f"""INSERT OR IGNORE INTO {RECOVERY_EVENTS_TABLE}(
                            cutover_id,event_type,event_at,evidence_digest,details_json
                        ) VALUES(?,?,?,?,?)""",
                    (
                        cutover_id,
                        "readback_passed",
                        now,
                        str(manifest[0]),
                        _json({"resumed": True}),
                    ),
                )
                for next_phase in ("reconciled", "released"):
                    conn.execute(
                        f"""INSERT INTO {DOMAIN_EVENTS_TABLE}(
                                epoch_id,phase,manifest_digest,deployed_sha,event_at,actor,details_json
                            ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            persisted_epoch_id,
                            next_phase,
                            gate_fingerprint,
                            self.deployed_sha,
                            now,
                            actor,
                            _json({"resumed_exact_readback": True}),
                        ),
                    )
                conn.commit()
                phase = "released"
                readback = read_ff_pool_cutover_status(conn)
            if phase != "released":
                raise FfPoolCutoverProductionError(
                    "applied_epoch_not_released",
                    f"Existing cutover is in phase {phase}; exact recovery is required",
                )
            recovery = _reconcile_applied_recovery(
                runtime_dir=self.runtime.runtime_dir,
                db_path=self.runtime.db_path,
                cutover_id=cutover_id,
                deployed_sha=self.deployed_sha,
                gate_fingerprint=gate_fingerprint,
                manifest_digest=str(manifest[0]),
            )
            return {
                "contract_name": CONTRACT_NAME,
                "status": "already_applied_reconciled",
                "owner_gate_fingerprint": gate_fingerprint,
                "cutover_manifest_digest": str(manifest[0]),
                "readback": readback,
                "warehouse_recovery": recovery,
                "idempotent": True,
                "external_barrier_restore_required": True,
                "wb_writes": 0,
            }
        finally:
            conn.close()


def _build_source_snapshot(
    conn: sqlite3.Connection,
    *,
    deployed_sha: str,
    as_of: str,
    excluded_shipment_ids: tuple[str, ...],
    opening_facility_id: str,
    accounting_boundary: Mapping[str, Any],
    validate_collector_current: bool,
) -> dict[str, Any]:
    preflight = ff_pool_cutover_preflight_snapshot(conn)
    blockers: list[dict[str, Any]] = list(preflight.get("blockers") or [])
    if preflight.get("status") not in {"ready", "blocked"}:
        blockers.append({"code": "cutover_preflight_unavailable", "status": preflight.get("status")})
    facility_id = _opening_facility(
        conn, requested=opening_facility_id, blockers=blockers
    )
    aggregate_rows = list((preflight.get("aggregate") or {}).get("rows") or [])
    allocations = [
        {
            "facility_id": facility_id,
            "pool": "FBS",
            "nm_id": int(row["nm_id"]),
            "quantity": int(row["quantity"]),
            "capital_rub": canonical_decimal_text(row["capital_rub"]),
        }
        for row in aggregate_rows
        if int(row["quantity"]) != 0
    ]
    warehouse_mappings, warehouse_map = _warehouse_mappings(conn, blockers=blockers)
    requested_boundary = dict(accounting_boundary)
    boundary = ff_pool_fbs_accounting_boundary_snapshot(
        conn,
        boundary_at=str(requested_boundary["local_boundary_at"]),
        watermarks=requested_boundary,
    )
    if (
        str(requested_boundary.get("frozen_evidence_digest") or "")
        and str(requested_boundary["frozen_evidence_digest"])
        != str(boundary["frozen_evidence_digest"])
    ):
        blockers.append(
            {
                "code": "fbs_frozen_evidence_digest_stale",
                "expected": requested_boundary["frozen_evidence_digest"],
                "actual": boundary["frozen_evidence_digest"],
            }
        )
    blockers.extend(list(boundary.get("blockers") or []))
    order_watermark = _exact_integer(
        boundary.get("order_observation_watermark_sequence", 0),
        "accounting_boundary.order_observation_watermark_sequence",
    )
    status_watermark = _exact_integer(
        boundary.get("status_observation_watermark_sequence", 0),
        "accounting_boundary.status_observation_watermark_sequence",
    )
    transition_watermark = _exact_integer(
        boundary.get("status_transition_watermark_sequence", 0),
        "accounting_boundary.status_transition_watermark_sequence",
    )
    latest_orders = conn.execute(
        f"""SELECT observation_sequence,observation_id,order_id,source_revision,
                   source_created_at,observed_at,warehouse_id,nm_id,chrt_id,
                   seller_sku,skus_json
            FROM {OBSERVATIONS_TABLE} AS source
            WHERE observation_sequence<=?
              AND observation_sequence=(
                SELECT MAX(latest.observation_sequence) FROM {OBSERVATIONS_TABLE} AS latest
                WHERE latest.order_id=source.order_id
                  AND latest.observation_sequence<=?
            )
            ORDER BY order_id""",
        (order_watermark, order_watermark),
    ).fetchall()
    sku_mappings, sku_map = _sku_mappings(
        conn, latest_orders=latest_orders, blockers=blockers
    )
    mapping_digest = _fingerprint(
        {"warehouses": warehouse_mappings, "skus": sku_mappings}
    )
    classifications: list[dict[str, Any]] = []
    for row in latest_orders:
        order_id = int(row[2])
        warehouse = warehouse_map.get(int(row[6] or 0))
        sku = sku_map.get((int(row[7]), int(row[8] or 0)))
        status: dict[str, Any] = {"evidence": None}
        evidence: Mapping[str, Any] | None = None
        if warehouse is None or sku is None:
            classification = "unmatched"
            status_digest = _fingerprint({"order_id": order_id, "unmatched": True})
            target_facility: str | None = None
            target_nm_id = int(row[7])
            order_quantity = 1
        else:
            status = classify_pre_t_status(
                conn,
                order_id=order_id,
                cutover_at=str(boundary["local_boundary_at"]),
                max_observation_sequence=status_watermark,
            )
            classification = str(status["classification"])
            evidence = status.get("evidence")
            if evidence is None:
                blockers.append({"code": "official_status_evidence_missing", "order_id": order_id})
                classification = "unmatched"
                status_digest = _fingerprint({"order_id": order_id, "status_missing": True})
                order_quantity = 1
            else:
                status_digest = str(evidence["status_digest"])
                order_quantity = int(evidence["quantity"])
            target_facility = str(warehouse["facility_id"])
            target_nm_id = int(sku["target_nm_id"])
        classifications.append(
            {
                "order_id": order_id,
                "observation_sequence": int(row[0]),
                "status_observation_sequence": (
                    0
                    if evidence is None
                    else int(evidence["observation_sequence"])
                ),
                "observation_id": str(row[1]),
                "source_revision": str(row[3]),
                "source_created_at": str(row[4]),
                "observed_at": str(row[5]),
                "classification": classification,
                "facility_id": target_facility,
                "pool": None if classification == "unmatched" else "FBS",
                "nm_id": target_nm_id,
                "quantity": order_quantity,
                "status_fingerprint": status_digest,
                "status_evidence": None if evidence is None else dict(evidence),
                "post_handoff_reconciliation": (
                    None
                    if warehouse is None or sku is None
                    else status.get("post_handoff_reconciliation")
                ),
                "mapping_digest": mapping_digest,
            }
        )
    watermark = order_watermark
    observation_digest = _fingerprint(
        {"watermark": watermark, "classifications": classifications}
    )
    collector = _collector_checkpoint(
        conn,
        accounting_boundary=boundary,
        observation_digest=observation_digest,
        blockers=blockers,
        validate_current=validate_collector_current,
    )
    fbw = _fbw_origin_assignments(
        conn,
        active_supplies=list(preflight.get("active_fbw_supplies") or []),
        blockers=blockers,
    )
    shipments = _excluded_shipments(
        conn,
        shipment_ids=excluded_shipment_ids,
        facility_id=facility_id,
        blockers=blockers,
    )
    historical = _historical_summary(
        classifications=classifications, allocations=allocations
    )
    sorted_count = int(
        conn.execute(
            f"""SELECT COUNT(DISTINCT order_id) FROM {STATUS_TRANSITIONS_TABLE}
                WHERE previous_supplier_status='complete' AND previous_wb_status='waiting'
                  AND current_supplier_status='complete' AND current_wb_status='sorted'
                  AND transition_sequence<=?""",
            (transition_watermark,),
        ).fetchone()[0]
    )
    if any(item["classification"] == "unmatched" for item in classifications):
        blockers.append(
            {
                "code": "one_or_more_fbs_orders_unmatched",
                "count": sum(item["classification"] == "unmatched" for item in classifications),
            }
        )
    return {
        "deployed_sha": deployed_sha,
        "as_of": as_of,
        "accounting_boundary": {
            key: value for key, value in boundary.items() if key != "blockers"
        },
        "opening_facility_id": facility_id,
        "excluded_shipment_ids": list(excluded_shipment_ids),
        "target_feature_epoch": int(preflight.get("target_feature_epoch") or 0),
        "aggregate": preflight.get("aggregate"),
        "allocations": allocations,
        "opening_summary": {
            "quantity": sum(int(item["quantity"]) for item in allocations),
            "capital_rub": canonical_decimal_text(
                sum((Decimal(str(item["capital_rub"])) for item in allocations), ZERO)
            ),
            "facility_id": facility_id,
            "FBS": True,
            "FBO_opening_zero": True,
        },
        "order_classifications": classifications,
        "historical_fbs_summary": historical,
        "seller_warehouse_mappings": warehouse_mappings,
        "sku_mappings": sku_mappings,
        "mapping_digest": mapping_digest,
        "fbw_origin_assignments": fbw,
        "excluded_pending_receipts": shipments,
        "collector_checkpoint": collector,
        "non_target": preflight.get("non_target"),
        "observed_sorted_transition_distinct_orders": sorted_count,
        "blockers": blockers,
    }


def _proposal_from_source(
    *,
    source: Mapping[str, Any],
    cutover_id: str,
    epoch_id: str,
    barrier_digest: str,
    cutover_at: str,
    approval_reference: str,
    external_barrier: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_name": PROPOSAL_CONTRACT,
        "cutover_id": cutover_id,
        "business_date": _parse_utc(cutover_at)
        .astimezone(ZoneInfo("Asia/Yekaterinburg"))
        .date()
        .isoformat(),
        "target_feature_epoch": int(source["target_feature_epoch"]),
        "write_epoch_id": epoch_id,
        "control_manifest_digest": barrier_digest,
        "control_evidence": {
            "maintenance_quiet": True,
            "http_write_barrier_active": True,
            "warehouse_timer_held": True,
            "warehouse_lock_held": False,
            "evidence_digest": _fingerprint(external_barrier),
        },
        "handoff_policy": {
            "decision": "approved",
            "supplier_status": "complete",
            "wb_status": "sorted",
            "approval_reference": approval_reference,
            "observed_complete_waiting_to_complete_sorted_distinct_orders": int(
                source["observed_sorted_transition_distinct_orders"]
            ),
        },
        "allocations": list(source["allocations"]),
        "order_classifications": [
            {
                "order_id": item["order_id"],
                "classification": item["classification"],
                "facility_id": item["facility_id"] or "",
                "quantity": item["quantity"],
                "status_fingerprint": item["status_fingerprint"],
                "status_evidence": item["status_evidence"],
                "post_handoff_reconciliation": item[
                    "post_handoff_reconciliation"
                ],
                "mapping_digest": item["mapping_digest"],
            }
            for item in source["order_classifications"]
        ],
        "seller_warehouse_mappings": list(source["seller_warehouse_mappings"]),
        "sku_mappings": list(source["sku_mappings"]),
        "fbw_origin_assignments": list(source["fbw_origin_assignments"]),
        "china_shipments": [
            {
                "shipment_id": item["shipment_id"],
                "classification": "excluded_pending_receipt",
                "facility_id": item["facility_id"],
                "pools": item["pools"],
                "evidence_digest": item["evidence_digest"],
            }
            for item in source["excluded_pending_receipts"]
        ],
        "collector_checkpoint": dict(source["collector_checkpoint"]),
        "non_target_evidence_digest": str(source["non_target"]["digest"]),
    }


def _opening_facility(
    conn: sqlite3.Connection, *, requested: str, blockers: list[dict[str, Any]]
) -> str:
    if requested:
        row = conn.execute(
            f"SELECT facility_id FROM {FACILITIES_TABLE} WHERE facility_id=? AND active=1",
            (requested,),
        ).fetchone()
        if row is None:
            blockers.append({"code": "opening_facility_missing_or_inactive", "facility_id": requested})
        return requested
    rows = conn.execute(
        f"""SELECT facility.facility_id
            FROM {FACILITIES_TABLE} AS facility
            LEFT JOIN sheet_vitrina_v1_ff_facility_profiles AS profile
              ON profile.facility_id=facility.facility_id
            WHERE facility.active=1
              AND (facility.name LIKE '%Москва%'
                   OR COALESCE(profile.city,'')='Москва')
            ORDER BY facility.facility_id"""
    ).fetchall()
    if len(rows) != 1:
        blockers.append({"code": "opening_moscow_facility_ambiguous", "candidate_count": len(rows)})
        return "missing_moscow_facility"
    return str(rows[0][0])


def _warehouse_mappings(
    conn: sqlite3.Connection, *, blockers: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    rows = [
        {
            "warehouse_id": int(row[0]),
            "facility_id": str(row[1]),
            "evidence_digest": str(row[2]),
        }
        for row in conn.execute(
            f"""SELECT seller_warehouse_id,facility_id,mapping_digest
                FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1
                ORDER BY seller_warehouse_id,mapping_id"""
        ).fetchall()
    ]
    mapping: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["warehouse_id"] in mapping:
            blockers.append({"code": "active_warehouse_mapping_ambiguous", "warehouse_id": row["warehouse_id"]})
        mapping[row["warehouse_id"]] = row
    return rows, mapping


def _sku_mappings(
    conn: sqlite3.Connection,
    *,
    latest_orders: list[sqlite3.Row],
    blockers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[int, int], dict[str, Any]]]:
    output: dict[tuple[int, int], dict[str, Any]] = {}
    for row in latest_orders:
        order_id = int(row[2])
        revision = str(row[3])
        evidence = conn.execute(
            f"""SELECT outcome,identity_mapping_id FROM {IDENTITY_EVIDENCE_TABLE}
                WHERE order_id=? AND order_revision=?
                ORDER BY evidence_sequence DESC LIMIT 1""",
            (order_id, revision),
        ).fetchone()
        if evidence is None or str(evidence[0]) != "matched":
            blockers.append({"code": "order_identity_not_matched", "order_id": order_id})
            continue
        active = conn.execute(
            f"""SELECT source_nm_id,source_chrt_id,target_nm_id
                FROM {IDENTITY_MAPPINGS_TABLE}
                WHERE mapping_id=? AND active=1""",
            (str(evidence[1]),),
        ).fetchone()
        if active is None:
            blockers.append({"code": "order_identity_mapping_inactive", "order_id": order_id})
            continue
        source_nm_id = int(active[0])
        chrt_id = int(active[1])
        item = {
            "nm_id": source_nm_id,
            "source_nm_id": source_nm_id,
            "target_nm_id": int(active[2]),
            "chrt_id": chrt_id,
            "identity_digest": _fingerprint(
                {
                    "nm_id": int(row[7]),
                    "chrt_id": int(row[8] or 0),
                    "skus": json.loads(str(row[10])),
                }
            ),
        }
        key = (source_nm_id, chrt_id)
        if key in output and output[key] != item:
            blockers.append({"code": "order_identity_mapping_ambiguous", "key": list(key)})
        output[key] = item
    rows = sorted(output.values(), key=lambda item: (item["nm_id"], item["chrt_id"]))
    return rows, output


def _collector_checkpoint(
    conn: sqlite3.Connection,
    *,
    accounting_boundary: Mapping[str, Any],
    observation_digest: str,
    blockers: list[dict[str, Any]],
    validate_current: bool,
) -> dict[str, Any]:
    if validate_current:
        try:
            _require_collector_current_ready(conn)
        except FfPoolCutoverProductionError as exc:
            blockers.append({"code": exc.code, "message": str(exc)})
    boundary = {
        key: value
        for key, value in dict(accounting_boundary).items()
        if key != "blockers"
    }
    return {
        "accounting_boundary_at": str(boundary["local_boundary_at"]),
        "observation_watermark_sequence": int(
            boundary["order_observation_watermark_sequence"]
        ),
        "observation_watermark_digest": observation_digest,
        "status_observation_watermark_sequence": int(
            boundary["status_observation_watermark_sequence"]
        ),
        "status_transition_watermark_sequence": int(
            boundary["status_transition_watermark_sequence"]
        ),
        "frozen_evidence_digest": str(boundary["frozen_evidence_digest"]),
        "frozen_streams": dict(boundary["frozen_streams"]),
        "post_watermark_growth_invalidates_gate": False,
    }


def _require_collector_current_ready(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        f"""SELECT last_status,last_success_at
            FROM {COLLECTOR_STATE_TABLE} WHERE state_id=1"""
    ).fetchone()
    if row is None:
        raise FfPoolCutoverProductionError(
            "collector_checkpoint_missing", "FBS collector checkpoint is missing"
        )
    if str(row[0]) != "success" or not str(row[1]):
        raise FfPoolCutoverProductionError(
            "collector_checkpoint_not_success",
            "FBS collector must have a current successful checkpoint",
        )


def _fbw_origin_assignments(
    conn: sqlite3.Connection,
    *,
    active_supplies: list[dict[str, Any]],
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for supply in active_supplies:
        row = conn.execute(
            f"""SELECT facility_id,pool,request_fingerprint
                FROM {ASSIGNMENTS_TABLE}
                WHERE wb_supply_cache_key=? AND source_revision=?
                ORDER BY assigned_at DESC,assignment_id DESC LIMIT 1""",
            (str(supply["wb_supply_cache_key"]), str(supply["source_revision"])),
        ).fetchone()
        if row is None:
            blockers.append(
                {
                    "code": "active_fbw_origin_unassigned",
                    "wb_supply_cache_key": supply["wb_supply_cache_key"],
                }
            )
            continue
        output.append(
            {
                **supply,
                "facility_id": str(row[0]),
                "pool": str(row[1]),
                "evidence_digest": str(row[2]),
            }
        )
    return output


def _excluded_shipments(
    conn: sqlite3.Connection,
    *,
    shipment_ids: tuple[str, ...],
    facility_id: str,
    blockers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for shipment_id in shipment_ids:
        row = conn.execute(
            """SELECT shipment_id,COALESCE(invoice_no,''),COALESCE(actual_shipment_date,''),
                      COALESCE(actual_ff_acceptance_date,''),product_qty_total,
                      COALESCE(archived_at,'')
               FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?""",
            (shipment_id,),
        ).fetchone()
        if row is None:
            blockers.append({"code": "excluded_shipment_missing", "shipment_id": shipment_id})
            continue
        line = conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(qty),0)
               FROM sheet_vitrina_v1_supplier_shipment_lines
               WHERE shipment_id=? AND line_type='product'""",
            (shipment_id,),
        ).fetchone()
        receipt_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations WHERE source_key=?",
                (f"supplier_shipment_acceptance:{shipment_id}",),
            ).fetchone()[0]
        )
        cost_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers "
                "WHERE supplier_shipment_id=?",
                (shipment_id,),
            ).fetchone()[0]
        )
        quantity = _exact_integer(row[4], "shipment quantity")
        line_quantity = _exact_integer(line[1], "shipment product line quantity")
        evidence = {
            "shipment_id": shipment_id,
            "invoice_no": str(row[1]),
            "actual_shipment_date": str(row[2]),
            "actual_ff_acceptance_date": str(row[3]),
            "shipment_quantity": quantity,
            "product_line_count": int(line[0]),
            "product_line_quantity": line_quantity,
            "receipt_operation_count": receipt_count,
            "cost_layer_count": cost_count,
        }
        if (
            not str(row[2])
            or str(row[3])
            or str(row[5])
            or quantity <= 0
            or int(line[0]) <= 0
            or quantity != line_quantity
            or receipt_count
            or cost_count
        ):
            blockers.append(
                {
                    "code": "excluded_pending_receipt_not_clean",
                    "shipment_id": shipment_id,
                    "evidence": evidence,
                }
            )
        output.append(
            {
                "shipment_id": shipment_id,
                "invoice_no": str(row[1]),
                "classification": "excluded_pending_receipt",
                "facility_id": facility_id,
                "pools": ["FBO", "FBS"],
                "expected_quantity": quantity,
                "evidence_digest": _fingerprint(evidence),
                "post_cutover_state": "in_transit",
                "opening_quantity": 0,
                "opening_capital_rub": "0",
                "historical_fbs_debit_quantity": 0,
                "guided_acceptance_required": True,
            }
        )
    return output


def _historical_summary(
    *,
    classifications: list[dict[str, Any]],
    allocations: list[dict[str, Any]],
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    quantities: dict[str, int] = {}
    debit_capital = ZERO
    for item in classifications:
        classification = str(item["classification"])
        counts[classification] = counts.get(classification, 0) + 1
        quantities[classification] = quantities.get(classification, 0) + int(item["quantity"])
        if classification in {"pre_t_handoff_debit", "pre_t_absorbed_closed"}:
            allocation = next(
                row
                for row in allocations
                if row["facility_id"] == item["facility_id"]
                and row["pool"] == "FBS"
                and int(row["nm_id"]) == int(item["nm_id"])
            )
            debit_capital += (
                Decimal(str(allocation["capital_rub"]))
                / Decimal(int(allocation["quantity"]))
                * Decimal(int(item["quantity"]))
            )
    return {
        "counts": counts,
        "quantities": quantities,
        "debit_capital_rub": canonical_decimal_text(debit_capital),
        "post_handoff_reconciliation_count": sum(
            item.get("post_handoff_reconciliation") is not None
            for item in classifications
        ),
        "approximate_accounting": False,
        "post_handoff_cancellation_lane": "separate_reconciliation",
    }


def _validate_external_barrier(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    required_true = (
        "maintenance_quiet",
        "http_write_barrier_active",
        "warehouse_timer_held",
        "supplier_acceptance_writer_held",
        "fbs_collector_continues",
    )
    if any(result.get(key) is not True for key in required_true):
        raise FfPoolCutoverProductionError(
            "external_barrier_incomplete", "Canonical external barrier evidence is incomplete"
        )
    if result.get("warehouse_lock_held") is not False:
        raise FfPoolCutoverProductionError(
            "warehouse_lock_not_drained", "Warehouse writer lock must be drained"
        )
    return result


def _recovery_scope(
    *, cutover_id: str, deployed_sha: str, gate_fingerprint: str
) -> dict[str, Any]:
    return {
        "cutover_id": str(cutover_id),
        "deployed_sha": str(deployed_sha),
        "owner_gate_fingerprint": str(gate_fingerprint),
    }


def _next_epoch_attempt_id(db_path: Path, base_epoch_id: str) -> str:
    """Use a fresh barrier epoch only after an exact pre-commit abort."""

    with closing(sqlite3.connect(db_path, timeout=30.0)) as conn:
        rows = conn.execute(
            f"""SELECT epoch_id,phase,event_sequence
                FROM {DOMAIN_EVENTS_TABLE}
                WHERE epoch_id=? OR epoch_id GLOB ?
                ORDER BY event_sequence""",
            (base_epoch_id, base_epoch_id + "_r[0-9]*"),
        ).fetchall()
    if not rows:
        return base_epoch_id
    latest_epoch_id, latest_phase, _sequence = rows[-1]
    if str(latest_phase) != "aborted":
        raise FfPoolCutoverProductionError(
            "incomplete_epoch_requires_recovery",
            f"Prior write epoch {latest_epoch_id} is in phase {latest_phase}",
        )
    attempts = {str(row[0]) for row in rows}
    suffix = len(attempts)
    candidate = f"{base_epoch_id}_r{suffix}"
    while candidate in attempts:
        suffix += 1
        candidate = f"{base_epoch_id}_r{suffix}"
    return candidate


def _reconcile_applied_recovery(
    *,
    runtime_dir: Path,
    db_path: Path,
    cutover_id: str,
    deployed_sha: str,
    gate_fingerprint: str,
    manifest_digest: str,
) -> dict[str, Any] | None:
    """Close a retained T2 checkpoint after exact post-commit readback."""

    registry = WarehouseRecoveryRegistry(runtime_dir=runtime_dir, db_path=db_path)
    existing = next(
        (
            item
            for item in registry.list_operations(limit=1000)
            if str(item.get("mutation_kind") or "") == "warehouse_opening_publication"
            and str(item.get("plan_fingerprint") or "") == gate_fingerprint
        ),
        None,
    )
    if existing is None:
        return None
    lifecycle = str(existing.get("lifecycle") or "")
    if lifecycle == RecoveryState.FAILED_RECOVERABLE.value:
        existing = registry.prepare_t2(
            mutation_kind="warehouse_opening_publication",
            plan_fingerprint=gate_fingerprint,
            scope=_recovery_scope(
                cutover_id=cutover_id,
                deployed_sha=deployed_sha,
                gate_fingerprint=gate_fingerprint,
            ),
            source_digest=str(existing.get("source_digest") or ""),
            non_target_digest=str(existing.get("non_target_digest") or ""),
            source_watermarks={"recovery": "exact_applied_readback"},
            schema_revision="ff_pool_cutover_production_v1",
        )
        lifecycle = str(existing.get("lifecycle") or "")
    if lifecycle == RecoveryState.VERIFIED.value:
        existing = registry.begin_mutation(str(existing["operation_id"]))
        lifecycle = str(existing.get("lifecycle") or "")
    if lifecycle == RecoveryState.MUTATION_RUNNING.value:
        existing = registry.retain(
            str(existing["operation_id"]), after_digest=manifest_digest
        )
        lifecycle = str(existing.get("lifecycle") or "")
    if lifecycle not in {
        RecoveryState.RETAINED.value,
        RecoveryState.RELEASED.value,
    }:
        raise FfPoolCutoverProductionError(
            "recovery_not_reconciled",
            f"Applied cutover recovery is in lifecycle {lifecycle}",
        )
    return existing


def _write_backup(
    *,
    db_path: Path,
    backup_dir: Path,
    fingerprint: str,
    reviewed_plan: Mapping[str, Any],
    external_barrier: Mapping[str, Any],
) -> dict[str, Any]:
    directory = Path(backup_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / f"ff-pool-cutover-{fingerprint.removeprefix('sha256:')[:20]}.json"
    with _open_query_only(Path(db_path)) as conn:
        target_counts = {}
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in (
            MANIFESTS_TABLE,
            CHECKPOINTS_TABLE,
            PENDING_SHIPMENTS_TABLE,
            FBS_EVENTS_TABLE,
            FBS_CURRENT_TABLE,
            FBS_RECONCILIATION_TABLE,
            FBS_DRAIN_STATE_TABLE,
            FBS_LATE_EVIDENCE_TABLE,
        ):
            target_counts[table] = (
                int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                if table in tables
                else 0
            )
        aggregate = ff_pool_cutover_preflight_snapshot(conn).get("aggregate")
    payload = {
        "contract_name": CONTRACT_NAME,
        "owner_gate_fingerprint": fingerprint,
        "reviewed_source_fingerprint": reviewed_plan["source_fingerprint"],
        "target_counts": target_counts,
        "aggregate_before": aggregate,
        "excluded_pending_receipts": reviewed_plan["source"]["excluded_pending_receipts"],
        "external_barrier_digest": _fingerprint(external_barrier),
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = path.read_bytes()
        if existing != encoded:
            raise FfPoolCutoverProductionError(
                "target_before_image_conflict",
                "Existing exact target before-image differs for this gate fingerprint",
            )
        os.chmod(path, 0o600)
    else:
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return {
        "path": str(path),
        "sha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "mode": "0600",
    }


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    material = {
        key: value
        for key, value in plan.items()
        if key not in {"fingerprint"}
    }
    return _fingerprint(material)


def _shipment_ids(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(sorted({str(value or "").strip() for value in values if str(value or "").strip()}))
    if not result:
        raise FfPoolCutoverProductionError(
            "excluded_shipment_required", "At least one exact excluded shipment ID is required"
        )
    if any(not SAFE_ID_RE.fullmatch(value) for value in result):
        raise FfPoolCutoverProductionError(
            "invalid_shipment_id", "Excluded shipment IDs must be bounded safe identifiers"
        )
    return result


def _exact_integer(value: Any, field: str) -> int:
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise FfPoolCutoverProductionError(
            "non_integral_quantity", f"{field} is not an exact integer"
        )
    return int(decimal)


def _require_digest(value: str, field: str) -> str:
    result = str(value or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", result):
        raise FfPoolCutoverProductionError("invalid_digest", f"{field} must be sha256:<64 hex>")
    return result


def _open_query_only(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FfPoolCutoverProductionError("operational_store_missing", "Operational store is missing")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise FfPoolCutoverProductionError("timezone_required", "timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_utc(value: str) -> None:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FfPoolCutoverProductionError("timestamp_not_utc", "timestamp must be UTC")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
