"""Owner-gated exact seller-warehouse mapping extension and FBS backlog drain.

The official WB adapter is read-only.  Dry-run and readback open SQLite in
query-only mode.  Apply adds one immutable warehouse mapping and one immutable
Stage 7C extension envelope, re-evidences only exact identities, then delegates
all reservation/debit semantics to the ordinary FBS lifecycle drain.
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

from packages.adapters.official_api_rate_budget import FileBackedOfficialApiRateBudget
from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
from packages.application.ff_pool_cutover import (
    MANIFESTS_TABLE,
    ORDERS_TABLE as CUTOVER_ORDERS_TABLE,
    ff_pool_fbs_accounting_boundary_snapshot,
)
from packages.application.ff_pool_documents import (
    DOCUMENT_LINES_TABLE,
    DOCUMENT_RELATIONS_TABLE,
    DOCUMENTS_TABLE,
)
from packages.application.ff_pool_fbs_lifecycle import (
    CURRENT_TABLE,
    DRAIN_STATE_TABLE,
    EVENTS_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    LATE_EVIDENCE_TABLE,
    MAPPING_EXTENSION_ALLOCATIONS_TABLE,
    MAPPING_EXTENSIONS_TABLE,
    drain_post_checkpoint_fbs_lifecycle,
    ensure_ff_pool_fbs_lifecycle_schema,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    OPERATIONS_TABLE,
    canonical_decimal_text,
    evaluate_ff_pool_aggregate_parity,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
from packages.application.warehouse_recovery_policy import (
    RecoveryState,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATE_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATUS_TRANSITIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    WbFbsOrdersCollector,
)


CONTRACT_NAME = "ff_fbs_mapping_extension_production_v1"
CONTRACT_VERSION = 1
TARGET_WAREHOUSE_ID = 854205
TARGET_WAREHOUSE_NAME = "FBS Оренбург"
TARGET_OFFICE_ID = 12223
TARGET_OFFICE_NAME = "Оренбург Центральная"
TARGET_OFFICE_CITY = "Оренбург"
TARGET_FACILITY_ID = "fff_2579bb2741ed4ab23b11bb4c4183"
TARGET_FACILITY_NAME = "FF Оренбург"
MOSCOW_WAREHOUSE_ID = 1988668
MOSCOW_FACILITY_ID = "fff_d67e8c823d5f81dd988d00dbfea6"
RECEIPT_DOCUMENT_ID = "ffpd_690c4f6ba705b75c27292020cfbd"
RECEIPT_ROOT_DOCUMENT_ID = "ffpd_676073b7b74a73cbbfe04a087444"
EXPECTED_RECEIPT_QUANTITY = 26_750
EXPECTED_RECEIPT_CAPITAL_RUB = "2874226.82"
EXPECTED_RECEIPT_SKU_COUNT = 21
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")


class FfFbsMappingExtensionProductionError(RuntimeError):
    pass


class FfFbsMappingExtensionProductionMutation:
    """Exact dry-run/apply/readback contract for warehouse 854205 only."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        timestamp_factory: Any | None = None,
        source: HttpBackedWbFbsOrdersSource | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir))
        self.deployed_sha = str(deployed_sha).strip().lower()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise FfFbsMappingExtensionProductionError(
                "deployed_sha must be an exact 40-hex SHA"
            )
        self.timestamp_factory = timestamp_factory or _utc_now
        self.source = source or HttpBackedWbFbsOrdersSource(
            rate_budget=FileBackedOfficialApiRateBudget(
                runtime_dir=self.runtime.runtime_dir,
                family="wb_fbs_orders",
                min_interval_seconds=0.22,
            )
        )

    def build_plan(self) -> dict[str, Any]:
        generated_at = str(self.timestamp_factory())
        official = self._official_evidence()
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            snapshot = _snapshot(
                conn,
                deployed_sha=self.deployed_sha,
                boundary_at=generated_at,
                requested_boundary=None,
            )
        blockers = [*official["blockers"], *snapshot["blockers"]]
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "deployed_sha": self.deployed_sha,
            "generated_at": generated_at,
            "scope": {
                "seller_warehouse_id": TARGET_WAREHOUSE_ID,
                "official_office_id": TARGET_OFFICE_ID,
                "facility_id": TARGET_FACILITY_ID,
                "pool": "FBS",
                "receipt_document_id": RECEIPT_DOCUMENT_ID,
                "receipt_root_document_id": RECEIPT_ROOT_DOCUMENT_ID,
            },
            "official_evidence": official["evidence"],
            "source": snapshot,
            "expected_effects": {
                "warehouse_mapping_insert_count": 1,
                "mapping_extension_insert_count": 1,
                "extension_allocation_insert_count": len(snapshot["allocations"]),
                "identity_mapping_insert_count": sum(
                    1 for row in snapshot["identity_plan"] if row["action"] == "insert"
                ),
                "identity_mapping_noop_count": sum(
                    1 for row in snapshot["identity_plan"] if row["action"] == "noop"
                ),
                "frozen_target_order_count": snapshot["frozen_backlog"]["order_count"],
                "frozen_target_status_count": snapshot["frozen_backlog"]["status_count"],
                "frozen_identity_evidence_count": snapshot["frozen_backlog"][
                    "order_revision_count"
                ],
                "frozen_expected_final_reserved_count": snapshot["frozen_backlog"][
                    "expected_final_reserved_count"
                ],
                "frozen_expected_final_fulfilled_count": snapshot["frozen_backlog"][
                    "expected_final_fulfilled_count"
                ],
                "frozen_expected_final_cancelled_or_released_count": snapshot[
                    "frozen_backlog"
                ]["expected_final_cancelled_or_released_count"],
                "frozen_expected_late_noop_count": snapshot["frozen_backlog"][
                    "expected_late_noop_count"
                ],
                "wb_write_count": 0,
                "transfer_receipt_write_count": 0,
                "fbo_write_count": 0,
                "factory_order_write_count": 0,
            },
            "recovery": {
                "tier": "T2",
                "kind": "fbs_mapping_backlog_publication",
                "domain_checkpoint": True,
                "private_exact_target_before_image": True,
                "mode": "0600",
                "post_commit_transport_ambiguity": "query_only_readback_before_retry",
            },
            "post_watermark_suffix": {
                "included_by_ordinary_lifecycle": True,
                "invalidates_gate": False,
                "new_identity_mapping_is_not_inferred": True,
            },
            "apply_allowed": not blockers,
            "blockers": blockers,
        }
        plan["fingerprint"] = _fingerprint(
            {
                key: value
                for key, value in plan.items()
                if key not in {"fingerprint", "generated_at"}
            }
        )
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
        _validate_reviewed_plan(
            reviewed_plan,
            fingerprint=fingerprint,
            deployed_sha=self.deployed_sha,
            approval_reference=approval_reference,
            actor=actor,
        )
        evidence_root = Path(evidence_dir).resolve()
        if not evidence_root.is_absolute():
            raise FfFbsMappingExtensionProductionError(
                "evidence_dir must be absolute"
            )
        evidence_root.mkdir(parents=True, exist_ok=True)
        suffix = fingerprint.removeprefix("sha256:")[:16]
        before_path = evidence_root / f"ff-fbs-mapping-{suffix}.before.json"
        evidence_path = evidence_root / f"ff-fbs-mapping-{suffix}.evidence.json"
        if evidence_path.is_file():
            prior = _read_json_object(evidence_path)
            if (
                str(prior.get("manifest_fingerprint") or "") != fingerprint
                or str(prior.get("deployed_sha") or "") != self.deployed_sha
                or str(prior.get("approval_reference") or "")
                != str(approval_reference).strip()
                or str(prior.get("actor") or "") != str(actor).strip()
            ):
                raise FfFbsMappingExtensionProductionError(
                    "existing production evidence identity drifted"
                )
            readback = self.readback()
            _verify_completed_readback(
                readback,
                fingerprint=fingerprint,
                expected_effects=reviewed_plan.get("expected_effects"),
            )
            return {
                **prior,
                "idempotent": True,
                "readback": readback,
                "evidence_path": str(evidence_path),
                "evidence_sha256": _sha256_file(evidence_path),
            }

        existing = self.readback()
        extension = dict(existing.get("mapping_extension") or {})
        if str(extension.get("plan_fingerprint") or "") == fingerprint:
            _verify_completed_readback(
                existing,
                fingerprint=fingerprint,
                expected_effects=reviewed_plan.get("expected_effects"),
            )
            registry = WarehouseRecoveryRegistry(
                runtime_dir=self.runtime.runtime_dir,
                db_path=self.runtime.db_path,
            )
            operation_id = recovery_operation_id(
                "fbs_mapping_backlog_publication", fingerprint
            )
            recovery = registry.get_operation(operation_id)
            if recovery is None:
                raise FfFbsMappingExtensionProductionError(
                    "committed mapping extension lacks its T2 recovery operation"
                )
            if str(recovery.get("lifecycle") or "") == RecoveryState.MUTATION_RUNNING.value:
                recovery = registry.retain(
                    operation_id,
                    after_digest=str(existing["reconciliation_digest"]),
                    non_target_digest=str(
                        (reviewed_plan.get("source") or {})
                        .get("non_target_invariants", {})
                        .get("digest", "")
                    ),
                )
            if str(recovery.get("lifecycle") or "") != RecoveryState.RETAINED.value:
                raise FfFbsMappingExtensionProductionError(
                    "committed mapping extension T2 recovery is not retained"
                )
            return self._finalize_evidence(
                fingerprint=fingerprint,
                approval_reference=str(approval_reference).strip(),
                actor=str(actor).strip(),
                reviewed_plan=reviewed_plan,
                before_image={},
                recovery=recovery,
                readback=existing,
                evidence_path=evidence_path,
                idempotent=True,
                recovered_after_response_loss=True,
            )

        official = self._official_evidence()
        if official["blockers"]:
            raise FfFbsMappingExtensionProductionError(
                "official warehouse evidence drifted: " + "; ".join(official["blockers"])
            )
        if official["evidence"] != reviewed_plan.get("official_evidence"):
            raise FfFbsMappingExtensionProductionError(
                "official warehouse/office evidence changed after owner review"
            )
        reviewed_source = dict(reviewed_plan["source"])
        with closing(_open_query_only(self.runtime.db_path)) as query:
            fresh = _snapshot(
                query,
                deployed_sha=self.deployed_sha,
                boundary_at=str(reviewed_source["accounting_boundary"]["local_boundary_at"]),
                requested_boundary=dict(reviewed_source["accounting_boundary"]),
            )
        _verify_frozen_source(reviewed_source, fresh)
        if fresh["blockers"]:
            raise FfFbsMappingExtensionProductionError(
                "reviewed source is no longer apply-eligible: "
                + "; ".join(str(item) for item in fresh["blockers"])
            )

        registry = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime.runtime_dir,
            db_path=self.runtime.db_path,
        )
        source_digest = str(reviewed_source["source_digest"])
        recovery: dict[str, Any] = {}
        before_image: dict[str, Any] = {}
        now = str(self.timestamp_factory())
        _require_utc(now)
        apply_summary: dict[str, Any] = {}
        commit_attempted = False
        reconciled_after_ambiguous_commit = False
        try:
            with warehouse_functional_write_lock(
                self.runtime.runtime_dir, timeout_seconds=300
            ):
                with closing(_open_query_only(self.runtime.db_path)) as locked_query:
                    checkpoint_source = _snapshot(
                        locked_query,
                        deployed_sha=self.deployed_sha,
                        boundary_at=str(
                            reviewed_source["accounting_boundary"]["local_boundary_at"]
                        ),
                        requested_boundary=dict(reviewed_source["accounting_boundary"]),
                    )
                _verify_frozen_source(reviewed_source, checkpoint_source)
                if checkpoint_source["blockers"]:
                    raise FfFbsMappingExtensionProductionError(
                        "source drifted before the T2 checkpoint: "
                        + "; ".join(
                            str(item) for item in checkpoint_source["blockers"]
                        )
                    )
                recovery = registry.prepare_t2(
                    mutation_kind="fbs_mapping_backlog_publication",
                    plan_fingerprint=fingerprint,
                    scope=dict(reviewed_plan["scope"]),
                    source_digest=source_digest,
                    non_target_digest=str(
                        reviewed_source["non_target_invariants"]["digest"]
                    ),
                    source_watermarks=dict(reviewed_source["accounting_boundary"]),
                    schema_revision=CONTRACT_NAME,
                )
                if (
                    str(recovery.get("lifecycle") or "")
                    == RecoveryState.VERIFIED.value
                ):
                    recovery = registry.begin_mutation(
                        str(recovery["operation_id"]),
                        expected_source_digest=source_digest,
                    )
                if (
                    str(recovery.get("lifecycle") or "")
                    != RecoveryState.MUTATION_RUNNING.value
                ):
                    raise FfFbsMappingExtensionProductionError(
                        "T2 warehouse-domain checkpoint is not mutation-ready"
                    )
                before_image = _write_before_image(
                    db_path=self.runtime.db_path,
                    destination=before_path,
                    reviewed_plan=reviewed_plan,
                )
                conn = sqlite3.connect(self.runtime.db_path, timeout=120.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys=ON")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    locked = _snapshot(
                        conn,
                        deployed_sha=self.deployed_sha,
                        boundary_at=str(
                            reviewed_source["accounting_boundary"]["local_boundary_at"]
                        ),
                        requested_boundary=dict(reviewed_source["accounting_boundary"]),
                    )
                    _verify_frozen_source(reviewed_source, locked)
                    if locked["blockers"]:
                        raise FfFbsMappingExtensionProductionError(
                            "source drifted under writer lock: "
                            + "; ".join(str(item) for item in locked["blockers"])
                        )
                    ensure_ff_pool_fbs_lifecycle_schema(conn)
                    mapping_id, extension_id = _apply_extension_rows(
                        conn,
                        reviewed_plan=reviewed_plan,
                        fingerprint=fingerprint,
                        deployed_sha=self.deployed_sha,
                        approval_reference=str(approval_reference).strip(),
                        actor=str(actor).strip(),
                        created_at=now,
                    )
                    evidence = _append_exact_target_identity_evidence(
                        conn,
                        warehouse_mapping_id=mapping_id,
                        evidenced_at=now,
                    )
                    manifest = _latest_manifest(conn)
                    drain = drain_post_checkpoint_fbs_lifecycle(
                        conn,
                        manifest=manifest,
                        occurred_at=now,
                        limit=100_000,
                    )
                    apply_summary = {
                        "mapping_id": mapping_id,
                        "extension_id": extension_id,
                        "identity_evidence": evidence,
                        "drain": drain,
                    }
                    commit_attempted = True
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        except Exception as exc:
            committed = False
            if commit_attempted:
                with closing(_open_query_only(self.runtime.db_path)) as query:
                    local_readback = _readback(query, deployed_sha=self.deployed_sha)
                committed = (
                    str(
                        (local_readback.get("mapping_extension") or {}).get(
                            "plan_fingerprint"
                        )
                        or ""
                    )
                    == fingerprint
                    and not local_readback.get("blockers")
                )
            if committed:
                reconciled_after_ambiguous_commit = True
            elif recovery.get("operation_id"):
                registry.fail_recoverable(
                    str(recovery["operation_id"]),
                    error=str(exc),
                    next_action="query_only_reconcile_before_resume",
                )
                raise
            else:
                raise

        readback = self.readback()
        _verify_completed_readback(
            readback,
            fingerprint=fingerprint,
            expected_effects=reviewed_plan.get("expected_effects"),
        )
        recovery = registry.retain(
            str(recovery["operation_id"]),
            after_digest=str(readback["reconciliation_digest"]),
            non_target_digest=str(reviewed_source["non_target_invariants"]["digest"]),
        )
        if str(recovery.get("lifecycle") or "") != RecoveryState.RETAINED.value:
            raise FfFbsMappingExtensionProductionError(
                "post-apply T2 recovery did not reach retained state"
            )
        return self._finalize_evidence(
            fingerprint=fingerprint,
            approval_reference=str(approval_reference).strip(),
            actor=str(actor).strip(),
            reviewed_plan=reviewed_plan,
            before_image=before_image,
            recovery=recovery,
            readback=readback,
            evidence_path=evidence_path,
            idempotent=False,
            recovered_after_response_loss=reconciled_after_ambiguous_commit,
            apply_summary=apply_summary,
        )

    def _finalize_evidence(
        self,
        *,
        fingerprint: str,
        approval_reference: str,
        actor: str,
        reviewed_plan: Mapping[str, Any],
        before_image: Mapping[str, Any],
        recovery: Mapping[str, Any],
        readback: Mapping[str, Any],
        evidence_path: Path,
        idempotent: bool,
        recovered_after_response_loss: bool,
        apply_summary: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _verify_completed_readback(
            readback,
            fingerprint=fingerprint,
            expected_effects=reviewed_plan.get("expected_effects"),
        )
        evidence: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "complete",
            "manifest_fingerprint": fingerprint,
            "deployed_sha": self.deployed_sha,
            "approval_reference": approval_reference,
            "actor": actor,
            "completed_at": str(self.timestamp_factory()),
            "source_digest": str((reviewed_plan.get("source") or {}).get("source_digest") or ""),
            "before_image": dict(before_image),
            "recovery": dict(recovery),
            "apply": dict(apply_summary or {}),
            "readback": dict(readback),
            "idempotent": idempotent,
            "recovered_after_response_loss": recovered_after_response_loss,
        }
        evidence["evidence_fingerprint"] = _fingerprint(evidence)
        _write_private_json(evidence_path, evidence)
        return {
            **evidence,
            "evidence_path": str(evidence_path),
            "evidence_sha256": _sha256_file(evidence_path),
        }

    def readback(self) -> dict[str, Any]:
        official = self._official_evidence()
        with closing(_open_query_only(self.runtime.db_path)) as conn:
            result = _readback(conn, deployed_sha=self.deployed_sha)
        public_orders = WbFbsOrdersCollector(
            db_path=self.runtime.db_path,
            timestamp_factory=self.timestamp_factory,
            enabled=False,
        ).orders_page(
            facility_id=TARGET_FACILITY_ID,
            page=1,
            limit=100,
        )
        blockers = [*official["blockers"], *result["blockers"]]
        official_digest = str((official.get("evidence") or {}).get("digest") or "")
        mapping = list(result.get("mapping") or [])
        extension = dict(result.get("mapping_extension") or {})
        if (
            len(mapping) != 1
            or str(mapping[0].get("official_evidence_digest") or "")
            != official_digest
            or str(extension.get("official_evidence_digest") or "")
            != official_digest
        ):
            blockers.append("stored official warehouse/office evidence drifted")
        if public_orders.get("status") != "ready":
            blockers.append("protected FBS order read model is unavailable")
        elif int((public_orders.get("page") or {}).get("total") or 0) < int(
            (result.get("backlog_partition") or {}).get("current_count") or 0
        ):
            blockers.append("protected FBS order read model is incomplete")
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if not blockers else "blocked",
            "deployed_sha": self.deployed_sha,
            "official_evidence": official["evidence"],
            **{key: value for key, value in result.items() if key != "blockers"},
            "public_order_read_model": public_orders,
            "blockers": blockers,
            "query_only": True,
            "wb_writes": 0,
        }
        payload["reconciliation_digest"] = _fingerprint(
            {key: value for key, value in payload.items() if key != "reconciliation_digest"}
        )
        return payload

    def _official_evidence(self) -> dict[str, Any]:
        warehouses = self.source.list_seller_warehouses()
        offices = self.source.list_offices()
        target_warehouse = [
            item for item in warehouses if int(item.warehouse_id) == TARGET_WAREHOUSE_ID
        ]
        target_office = [
            item for item in offices if int(item.office_id) == TARGET_OFFICE_ID
        ]
        moscow = [
            item for item in warehouses if int(item.warehouse_id) == MOSCOW_WAREHOUSE_ID
        ]
        blockers: list[str] = []
        if len(target_warehouse) != 1:
            blockers.append("official warehouse 854205 is missing or ambiguous")
        if len(target_office) != 1:
            blockers.append("official office 12223 is missing or ambiguous")
        if len(moscow) != 1:
            blockers.append("official Moscow warehouse 1988668 is missing or ambiguous")
        warehouse = target_warehouse[0] if len(target_warehouse) == 1 else None
        office = target_office[0] if len(target_office) == 1 else None
        if warehouse is not None and (
            int(warehouse.office_id) != TARGET_OFFICE_ID
            or str(warehouse.name) != TARGET_WAREHOUSE_NAME
            or int(warehouse.cargo_type or 0) != 1
            or int(warehouse.delivery_type or 0) != 1
            or bool(warehouse.is_deleting)
            or bool(warehouse.is_processing)
        ):
            blockers.append("official warehouse 854205 identity or state drifted")
        if office is not None and (
            str(office.name) != TARGET_OFFICE_NAME
            or str(office.city) != TARGET_OFFICE_CITY
        ):
            blockers.append("official office 12223 identity drifted")
        evidence = {
            "seller_warehouse": (
                {}
                if warehouse is None
                else {
                    "warehouse_id": int(warehouse.warehouse_id),
                    "office_id": int(warehouse.office_id),
                    "name": str(warehouse.name),
                    "delivery_type": int(warehouse.delivery_type or 0),
                    "cargo_type": int(warehouse.cargo_type or 0),
                    "is_deleting": bool(warehouse.is_deleting),
                    "is_processing": bool(warehouse.is_processing),
                }
            ),
            "office": (
                {}
                if office is None
                else {
                    "office_id": int(office.office_id),
                    "name": str(office.name),
                    "city": str(office.city),
                    "federal_district": str(office.federal_district),
                }
            ),
            "moscow_seller_warehouse": (
                {}
                if len(moscow) != 1
                else {
                    "warehouse_id": int(moscow[0].warehouse_id),
                    "office_id": int(moscow[0].office_id),
                    "name": str(moscow[0].name),
                }
            ),
        }
        evidence["digest"] = _fingerprint(evidence)
        return {"evidence": evidence, "blockers": blockers}


def _snapshot(
    conn: sqlite3.Connection,
    *,
    deployed_sha: str,
    boundary_at: str,
    requested_boundary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    tables = _table_names(conn)
    required = {
        MANIFESTS_TABLE,
        DOCUMENTS_TABLE,
        DOCUMENT_LINES_TABLE,
        DOCUMENT_RELATIONS_TABLE,
        BALANCES_TABLE,
        FACILITIES_TABLE,
        FACILITY_PROFILES_TABLE,
        FEATURE_EPOCHS_TABLE,
        WAREHOUSE_MAPPINGS_TABLE,
        IDENTITY_MAPPINGS_TABLE,
        IDENTITY_EVIDENCE_TABLE,
        OBSERVATIONS_TABLE,
        STATUS_OBSERVATIONS_TABLE,
        STATUS_TRANSITIONS_TABLE,
        STATE_TABLE,
        CURRENT_TABLE,
        EVENTS_TABLE,
        IDENTITY_PENDING_TABLE,
        IDENTITY_PENDING_RESOLUTIONS_TABLE,
        LATE_EVIDENCE_TABLE,
        CUTOVER_ORDERS_TABLE,
        "sheet_vitrina_v1_nomenclature_items",
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_warehouse_functional_balances",
    }
    missing = sorted(required - tables)
    if missing:
        return _empty_snapshot(["missing required tables: " + ", ".join(missing)])
    blockers: list[str] = []
    boundary = ff_pool_fbs_accounting_boundary_snapshot(
        conn,
        boundary_at=boundary_at,
        watermarks=requested_boundary,
    )
    blockers.extend(
        "accounting boundary: " + str(item) for item in boundary.get("blockers") or []
    )
    if requested_boundary is not None and (
        str(boundary.get("frozen_evidence_digest") or "")
        != str(requested_boundary.get("frozen_evidence_digest") or "")
    ):
        blockers.append("compound frozen accounting boundary digest drifted")
    order_w = int(boundary["order_observation_watermark_sequence"])
    status_w = int(boundary["status_observation_watermark_sequence"])
    cutover_rows = conn.execute(
        f"SELECT * FROM {MANIFESTS_TABLE} ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"
    ).fetchall()
    cutover = dict(cutover_rows[0]) if cutover_rows else {}
    if not cutover:
        blockers.append("Stage 7C manifest is unavailable")
    manifest = json.loads(str(cutover.get("manifest_json") or "{}")) if cutover else {}
    feature = conn.execute(
        f"SELECT epoch,writer_enabled,reader_enabled FROM {FEATURE_EPOCHS_TABLE} "
        "ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if (
        feature is None
        or not bool(feature[1])
        or not bool(feature[2])
        or int(feature[0]) != int(manifest.get("feature_epoch") or -1)
    ):
        blockers.append("current Stage 7C writer/reader epoch is unavailable")
    facility_rows = conn.execute(
        f"""SELECT facility.facility_id,facility.name,facility.active,profile.city
            FROM {FACILITIES_TABLE} AS facility
            LEFT JOIN {FACILITY_PROFILES_TABLE} AS profile USING(facility_id)
            WHERE facility.facility_id=?""",
        (TARGET_FACILITY_ID,),
    ).fetchall()
    facility = dict(facility_rows[0]) if len(facility_rows) == 1 else {}
    if (
        len(facility_rows) != 1
        or str(facility.get("name") or "") != TARGET_FACILITY_NAME
        or str(facility.get("city") or "") != TARGET_OFFICE_CITY
        or not bool(facility.get("active"))
    ):
        blockers.append("canonical active FF Оренбург identity drifted")
    target_mapping_rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} "
            "WHERE seller_warehouse_id=? AND active=1 ORDER BY mapping_id",
            (TARGET_WAREHOUSE_ID,),
        )
    ]
    if target_mapping_rows:
        blockers.append("seller warehouse 854205 already has an active mapping")
    moscow_mapping = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} "
            "WHERE seller_warehouse_id=? AND active=1 ORDER BY mapping_id",
            (MOSCOW_WAREHOUSE_ID,),
        )
    ]
    if (
        len(moscow_mapping) != 1
        or str(moscow_mapping[0].get("facility_id") or "") != MOSCOW_FACILITY_ID
    ):
        blockers.append("Moscow seller warehouse mapping is missing or ambiguous")
    existing_extension = (
        [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM {MAPPING_EXTENSIONS_TABLE} "
                "WHERE seller_warehouse_id=? ORDER BY extension_id",
                (TARGET_WAREHOUSE_ID,),
            )
        ]
        if MAPPING_EXTENSIONS_TABLE in tables
        else []
    )
    if existing_extension:
        blockers.append("seller warehouse 854205 mapping extension already exists")
    receipt_rows = conn.execute(
        f"SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?",
        (RECEIPT_DOCUMENT_ID,),
    ).fetchall()
    receipt = dict(receipt_rows[0]) if len(receipt_rows) == 1 else {}
    relation_rows = conn.execute(
        f"""SELECT * FROM {DOCUMENT_RELATIONS_TABLE}
            WHERE child_document_id=? AND relation_type='receipt_of'""",
        (RECEIPT_DOCUMENT_ID,),
    ).fetchall()
    receipt_lines = [
        dict(row)
        for row in conn.execute(
            f"""SELECT line_no,facility_id,pool,nm_id,quantity,capital_rub,
                       metadata_json
                FROM {DOCUMENT_LINES_TABLE}
                WHERE document_id=? AND facility_id=? AND pool='FBS'
                ORDER BY nm_id,line_no""",
            (RECEIPT_DOCUMENT_ID, TARGET_FACILITY_ID),
        )
    ]
    receipt_total_line_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {DOCUMENT_LINES_TABLE} WHERE document_id=?",
            (RECEIPT_DOCUMENT_ID,),
        ).fetchone()[0]
    )
    receipt_quantity = sum(int(row["quantity"]) for row in receipt_lines)
    receipt_capital = _decimal_sum(row["capital_rub"] for row in receipt_lines)
    if (
        len(receipt_rows) != 1
        or str(receipt.get("document_kind") or "") != "transfer_receipt"
        or str(receipt.get("root_document_id") or "") != RECEIPT_ROOT_DOCUMENT_ID
        or len(relation_rows) != 1
        or str(relation_rows[0]["root_document_id"]) != RECEIPT_ROOT_DOCUMENT_ID
        or receipt_total_line_count != EXPECTED_RECEIPT_SKU_COUNT
        or len(receipt_lines) != EXPECTED_RECEIPT_SKU_COUNT
        or receipt_quantity != EXPECTED_RECEIPT_QUANTITY
        or canonical_decimal_text(receipt_capital) != EXPECTED_RECEIPT_CAPITAL_RUB
    ):
        blockers.append("accepted Orenburg transfer receipt evidence drifted")
    allocations = [
        dict(row)
        for row in conn.execute(
            f"""SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                FROM {BALANCES_TABLE}
                WHERE facility_id=? AND pool='FBS'
                  AND nm_id IN ({','.join('?' for _ in receipt_lines)})
                ORDER BY nm_id""",
            (TARGET_FACILITY_ID, *(int(row["nm_id"]) for row in receipt_lines)),
        )
    ] if receipt_lines else []
    if len(allocations) != len(receipt_lines):
        blockers.append("receipt-backed Orenburg FBS allocation rows are incomplete")
    receipt_by_nm = {int(row["nm_id"]): row for row in receipt_lines}
    for allocation in allocations:
        receipt_line = receipt_by_nm.get(int(allocation["nm_id"]))
        if (
            receipt_line is None
            or int(allocation["quantity"]) != int(receipt_line["quantity"])
            or Decimal(str(allocation["capital_rub"]))
            != Decimal(str(receipt_line["capital_rub"]))
            or not str(allocation.get("source_watermark") or "").startswith(
                RECEIPT_DOCUMENT_ID
            )
            or allocation.get("wac_rub") is None
            or Decimal(str(allocation["wac_rub"])) <= 0
        ):
            blockers.append(
                f"receipt-backed allocation drifted for nmId {allocation['nm_id']}"
            )
    frozen_rows = _frozen_target_rows(
        conn, order_watermark=order_w, status_watermark=status_w
    )
    frozen_order_ids = sorted({int(row["order_id"]) for row in frozen_rows})
    prior_current = _warehouse_order_current_rows(conn)
    prior_events = _warehouse_order_event_rows(conn)
    if prior_current or prior_events:
        blockers.append("Orenburg lifecycle already has reserve/debit effects")
    identity_plan, identity_blockers = _identity_plan(conn, frozen_rows)
    blockers.extend(identity_blockers)
    cutover_id = str(cutover.get("cutover_id") or "")
    known_at_boundary = {
        int(row[0])
        for row in conn.execute(
            f"SELECT order_id FROM {CUTOVER_ORDERS_TABLE} WHERE cutover_id=?",
            (cutover_id,),
        )
    }
    prior_late = {
        int(row[0])
        for row in conn.execute(
            f"SELECT DISTINCT order_id FROM {LATE_EVIDENCE_TABLE} WHERE cutover_id=?",
            (cutover_id,),
        )
    }
    boundary_text = str(
        (manifest.get("accounting_boundary") or {}).get("local_boundary_at") or ""
    )
    try:
        cutover_boundary = datetime.fromisoformat(
            boundary_text.replace("Z", "+00:00")
        )
    except ValueError:
        cutover_boundary = datetime.min.replace(tzinfo=timezone.utc)
        blockers.append("Stage 7C local accounting boundary is invalid")
    late_orders: set[int] = set(prior_late)
    for row in frozen_rows:
        order_id = int(row["order_id"])
        if order_id in known_at_boundary:
            continue
        status_observed = datetime.fromisoformat(
            str(row["status_observed_at"]).replace("Z", "+00:00")
        )
        order_observed = datetime.fromisoformat(
            str(row["order_observed_at"]).replace("Z", "+00:00")
        )
        if status_observed <= cutover_boundary or order_observed <= cutover_boundary:
            late_orders.add(order_id)
    latest_by_order: dict[int, dict[str, Any]] = {}
    handoff_orders: set[int] = set()
    for row in frozen_rows:
        order_id = int(row["order_id"])
        latest_by_order[order_id] = row
        if (
            order_id not in late_orders
            and row["supplier_status"] == "complete"
            and row["wb_status"] == "sorted"
        ):
            handoff_orders.add(order_id)
    cancelled = {
        order_id
        for order_id, row in latest_by_order.items()
        if order_id not in late_orders
        and (
            str(row["supplier_status"]) == "cancel"
            or str(row["wb_status"])
            in {"canceled", "canceled_by_client", "declined_by_client", "defect"}
        )
    }
    expected_fulfilled = len(handoff_orders)
    expected_reserved = len(
        set(frozen_order_ids) - late_orders - handoff_orders - cancelled
    )
    collector = conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE state_id=1").fetchone()
    collector_state = dict(collector) if collector is not None else {}
    if (
        not collector_state
        or str(collector_state.get("last_status") or "") != "success"
        or not bool(collector_state.get("complete"))
        or int(collector_state.get("next_cursor") or 0) != 0
        or str(collector_state.get("last_error") or "")
    ):
        blockers.append("official FBS collector is not healthy/caught up")
    non_target = {
        "moscow_mapping_digest": _fingerprint(moscow_mapping),
        "receipt_digest": _fingerprint(
            {"document": receipt, "relation": [dict(row) for row in relation_rows], "lines": receipt_lines}
        ),
        "cutover_manifest_digest": _fingerprint(
            {key: value for key, value in cutover.items()}
        ),
    }
    non_target["digest"] = _fingerprint(non_target)
    frozen_backlog = {
        "order_count": len(frozen_order_ids),
        "status_count": len(frozen_rows),
        "order_revision_count": len(
            {
                (int(row["order_id"]), str(row["order_revision"]))
                for row in frozen_rows
            }
        ),
        "order_ids_digest": _fingerprint(frozen_order_ids),
        "rows_digest": _fingerprint(frozen_rows),
        "expected_final_reserved_count": expected_reserved,
        "expected_final_fulfilled_count": expected_fulfilled,
        "expected_final_cancelled_or_released_count": len(
            cancelled - handoff_orders
        ),
        "expected_late_noop_count": len(late_orders),
    }
    source_material = {
        "accounting_boundary": boundary,
        "cutover": cutover,
        "receipt_digest": non_target["receipt_digest"],
        "allocations": allocations,
        "frozen_backlog": frozen_backlog,
        "identity_plan": identity_plan,
        "non_target_invariants": non_target,
    }
    return {
        "deployed_sha": deployed_sha,
        "cutover": cutover,
        "accounting_boundary": boundary,
        "facility": facility,
        "receipt": receipt,
        "receipt_relation": [dict(row) for row in relation_rows],
        "receipt_lines": receipt_lines,
        "allocations": allocations,
        "frozen_backlog": frozen_backlog,
        "identity_plan": identity_plan,
        "collector_state": collector_state,
        "prior_target_current": prior_current,
        "prior_target_events": prior_events,
        "non_target_invariants": non_target,
        "source_digest": _fingerprint(source_material),
        "blockers": blockers,
    }


def _empty_snapshot(blockers: list[str]) -> dict[str, Any]:
    return {
        "deployed_sha": "",
        "cutover": {},
        "accounting_boundary": {},
        "facility": {},
        "receipt": {},
        "receipt_relation": [],
        "receipt_lines": [],
        "allocations": [],
        "frozen_backlog": {
            "order_count": 0,
            "status_count": 0,
            "order_revision_count": 0,
            "order_ids_digest": _fingerprint([]),
            "rows_digest": _fingerprint([]),
            "expected_final_reserved_count": 0,
            "expected_final_fulfilled_count": 0,
            "expected_final_cancelled_or_released_count": 0,
            "expected_late_noop_count": 0,
        },
        "identity_plan": [],
        "collector_state": {},
        "prior_target_current": [],
        "prior_target_events": [],
        "non_target_invariants": {"digest": _fingerprint({})},
        "source_digest": _fingerprint({}),
        "blockers": blockers,
    }


def _identity_plan(
    conn: sqlite3.Connection, frozen_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    blockers: list[str] = []
    identities: dict[tuple[int, int, str, str], int] = {}
    for row in frozen_rows:
        barcodes = _json_list(str(row["skus_json"]))
        barcode = str(barcodes[0]).strip() if len(barcodes) == 1 else ""
        key = (
            int(row["nm_id"]),
            int(row["chrt_id"] or 0),
            barcode,
            str(row["seller_sku"] or "").strip(),
        )
        identities[key] = identities.get(key, 0) + 1
    plan: list[dict[str, Any]] = []
    for (nm_id, chrt_id, barcode, seller_sku), count in sorted(identities.items()):
        if not chrt_id or not barcode or not seller_sku:
            blockers.append(
                f"frozen Orenburg identity is incomplete: {nm_id}/{chrt_id}/{barcode}/{seller_sku}"
            )
            continue
        owners = [
            dict(row)
            for row in conn.execute(
                """SELECT item_id,nm_id FROM sheet_vitrina_v1_nomenclature_items
                   WHERE is_active=1 AND is_hidden=0 AND nm_id=?
                     AND vendor_code=?
                     AND (barcode=? OR EXISTS(
                         SELECT 1 FROM json_each(barcodes_json) WHERE value=?
                     ))
                   ORDER BY item_id""",
                (nm_id, seller_sku, barcode, barcode),
            )
        ]
        existing = [
            dict(row)
            for row in conn.execute(
                f"""SELECT mapping_id,target_nm_id FROM {IDENTITY_MAPPINGS_TABLE}
                    WHERE source_nm_id=? AND source_chrt_id=?
                      AND source_barcode=? AND source_sku=? AND active=1
                    ORDER BY mapping_id""",
                (nm_id, chrt_id, barcode, seller_sku),
            )
        ]
        if len(owners) != 1:
            blockers.append(
                f"frozen Orenburg identity has {len(owners)} exact nomenclature owners: "
                f"{nm_id}/{chrt_id}/{barcode}/{seller_sku}"
            )
            continue
        target_nm_id = int(owners[0]["nm_id"])
        action = "insert"
        mapping_id = ""
        if existing:
            if len(existing) != 1 or int(existing[0]["target_nm_id"]) != target_nm_id:
                blockers.append(
                    f"active identity mapping is ambiguous: {nm_id}/{chrt_id}/{barcode}/{seller_sku}"
                )
                continue
            action = "noop"
            mapping_id = str(existing[0]["mapping_id"])
        plan.append(
            {
                "action": action,
                "existing_mapping_id": mapping_id,
                "source_nm_id": nm_id,
                "source_chrt_id": chrt_id,
                "source_barcode": barcode,
                "source_sku": seller_sku,
                "target_nm_id": target_nm_id,
                "nomenclature_item_id": str(owners[0]["item_id"]),
                "frozen_observation_count": count,
                "exact_identity": True,
            }
        )
    return plan, blockers


def _apply_extension_rows(
    conn: sqlite3.Connection,
    *,
    reviewed_plan: Mapping[str, Any],
    fingerprint: str,
    deployed_sha: str,
    approval_reference: str,
    actor: str,
    created_at: str,
) -> tuple[str, str]:
    source = dict(reviewed_plan["source"])
    official = dict(reviewed_plan["official_evidence"])
    mapping_material = {
        "seller_warehouse_id": TARGET_WAREHOUSE_ID,
        "official_office_id": TARGET_OFFICE_ID,
        "facility_id": TARGET_FACILITY_ID,
        "official_evidence_digest": official["digest"],
    }
    mapping_digest = _fingerprint(mapping_material)
    mapping_id = "fbs_wh_" + mapping_digest.removeprefix("sha256:")[:32]
    conn.execute(
        f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
                mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                created_at,created_by,official_office_id,
                official_warehouse_name,official_office_name,
                official_office_city,official_evidence_digest
            ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?)""",
        (
            mapping_id,
            TARGET_WAREHOUSE_ID,
            TARGET_FACILITY_ID,
            mapping_digest,
            created_at,
            actor,
            TARGET_OFFICE_ID,
            TARGET_WAREHOUSE_NAME,
            TARGET_OFFICE_NAME,
            TARGET_OFFICE_CITY,
            str(official["digest"]),
        ),
    )
    for item in source["identity_plan"]:
        if item["action"] == "noop":
            continue
        material = {
            key: item[key]
            for key in (
                "source_nm_id",
                "source_chrt_id",
                "source_barcode",
                "source_sku",
                "target_nm_id",
            )
        }
        digest = _fingerprint(material)
        conn.execute(
            f"""INSERT INTO {IDENTITY_MAPPINGS_TABLE}(
                    mapping_id,source_nm_id,source_chrt_id,source_barcode,
                    source_sku,target_nm_id,mapping_digest,active,created_at,created_by
                ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
            (
                "fbs_sku_" + digest.removeprefix("sha256:")[:32],
                item["source_nm_id"],
                item["source_chrt_id"],
                item["source_barcode"],
                item["source_sku"],
                item["target_nm_id"],
                digest,
                created_at,
                actor,
            ),
        )
    extension_material = {
        "cutover_id": source["cutover"]["cutover_id"],
        "warehouse_mapping_id": mapping_id,
        "seller_warehouse_id": TARGET_WAREHOUSE_ID,
        "official_office_id": TARGET_OFFICE_ID,
        "facility_id": TARGET_FACILITY_ID,
        "source_receipt_document_id": RECEIPT_DOCUMENT_ID,
        "source_receipt_root_document_id": RECEIPT_ROOT_DOCUMENT_ID,
        "source_receipt_digest": source["non_target_invariants"]["receipt_digest"],
        "mapping_digest": mapping_digest,
        "official_evidence_digest": official["digest"],
        "frozen_boundary": source["accounting_boundary"],
        "frozen_rows_digest": source["frozen_backlog"]["rows_digest"],
        "plan_fingerprint": fingerprint,
        "deployed_sha": deployed_sha,
    }
    extension_id = "fffbsext_" + _fingerprint(extension_material).removeprefix(
        "sha256:"
    )[:28]
    conn.execute(
        f"""INSERT INTO {MAPPING_EXTENSIONS_TABLE}(
                extension_id,cutover_id,warehouse_mapping_id,seller_warehouse_id,
                official_office_id,facility_id,source_receipt_document_id,
                source_receipt_root_document_id,source_receipt_digest,mapping_digest,
                official_evidence_digest,frozen_boundary_json,frozen_rows_digest,
                plan_fingerprint,deployed_sha,approval_reference,created_by,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            extension_id,
            source["cutover"]["cutover_id"],
            mapping_id,
            TARGET_WAREHOUSE_ID,
            TARGET_OFFICE_ID,
            TARGET_FACILITY_ID,
            RECEIPT_DOCUMENT_ID,
            RECEIPT_ROOT_DOCUMENT_ID,
            source["non_target_invariants"]["receipt_digest"],
            mapping_digest,
            official["digest"],
            _json(source["accounting_boundary"]),
            source["frozen_backlog"]["rows_digest"],
            fingerprint,
            deployed_sha,
            approval_reference,
            actor,
            created_at,
        ),
    )
    for allocation in source["allocations"]:
        material = {
            "extension_id": extension_id,
            "nm_id": int(allocation["nm_id"]),
            "opening_quantity": int(allocation["quantity"]),
            "opening_capital_rub": canonical_decimal_text(allocation["capital_rub"]),
            "frozen_wac_rub": canonical_decimal_text(allocation["wac_rub"]),
            "source_balance_watermark": str(allocation["source_watermark"]),
        }
        conn.execute(
            f"""INSERT INTO {MAPPING_EXTENSION_ALLOCATIONS_TABLE}(
                    extension_id,nm_id,opening_quantity,opening_capital_rub,
                    frozen_wac_rub,source_balance_watermark,allocation_digest,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                extension_id,
                material["nm_id"],
                material["opening_quantity"],
                material["opening_capital_rub"],
                material["frozen_wac_rub"],
                material["source_balance_watermark"],
                _fingerprint(material),
                created_at,
            ),
        )
    return mapping_id, extension_id


def _append_exact_target_identity_evidence(
    conn: sqlite3.Connection,
    *,
    warehouse_mapping_id: str,
    evidenced_at: str,
) -> dict[str, Any]:
    rows = conn.execute(
        f"""SELECT observation_sequence,observation_id,order_id,source_revision,
                   warehouse_id,office_id,nm_id,chrt_id,seller_sku,skus_json
            FROM {OBSERVATIONS_TABLE}
            WHERE warehouse_id=? AND office_id=?
            ORDER BY observation_sequence""",
        (TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID),
    ).fetchall()
    inserted = 0
    unresolved = 0
    for row in rows:
        barcodes = _json_list(str(row[9]))
        barcode = str(barcodes[0]).strip() if len(barcodes) == 1 else ""
        mappings = conn.execute(
            f"""SELECT mapping_id,target_nm_id FROM {IDENTITY_MAPPINGS_TABLE}
                WHERE source_nm_id=? AND source_chrt_id=? AND source_barcode=?
                  AND source_sku=? AND active=1 ORDER BY mapping_id""",
            (int(row[6]), int(row[7] or 0), barcode, str(row[8] or "")),
        ).fetchall()
        if len(mappings) != 1:
            unresolved += 1
            continue
        evidence = {
            "order_id": int(row[2]),
            "order_revision": str(row[3]),
            "warehouse_id": TARGET_WAREHOUSE_ID,
            "office_id": TARGET_OFFICE_ID,
            "nm_id": int(row[6]),
            "chrt_id": int(row[7] or 0),
            "barcode": barcode,
            "seller_sku": str(row[8] or ""),
            "warehouse_mapping_id": warehouse_mapping_id,
            "identity_mapping_id": str(mappings[0][0]),
            "outcome": "matched",
        }
        digest = _fingerprint(evidence)
        inserted += int(
            conn.execute(
                f"""INSERT OR IGNORE INTO {IDENTITY_EVIDENCE_TABLE}(
                        evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                        barcode,seller_sku,outcome,warehouse_mapping_id,
                        identity_mapping_id,evidence_digest,observed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "fbs_map_" + digest.removeprefix("sha256:")[:32],
                    int(row[2]),
                    str(row[3]),
                    TARGET_WAREHOUSE_ID,
                    int(row[6]),
                    int(row[7] or 0),
                    barcode,
                    str(row[8] or ""),
                    "matched",
                    warehouse_mapping_id,
                    str(mappings[0][0]),
                    digest,
                    evidenced_at,
                ),
            ).rowcount
        )
    return {
        "target_observation_count": len(rows),
        "inserted_count": inserted,
        "unresolved_count": unresolved,
    }


def _readback(conn: sqlite3.Connection, *, deployed_sha: str) -> dict[str, Any]:
    tables = _table_names(conn)
    blockers: list[str] = []
    mapping_rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} "
            "WHERE seller_warehouse_id=? AND active=1 ORDER BY mapping_id",
            (TARGET_WAREHOUSE_ID,),
        )
    ] if WAREHOUSE_MAPPINGS_TABLE in tables else []
    if (
        len(mapping_rows) != 1
        or str(mapping_rows[0].get("facility_id") or "") != TARGET_FACILITY_ID
        or int(mapping_rows[0].get("official_office_id") or 0) != TARGET_OFFICE_ID
        or str(mapping_rows[0].get("official_warehouse_name") or "") != TARGET_WAREHOUSE_NAME
        or str(mapping_rows[0].get("official_office_name") or "") != TARGET_OFFICE_NAME
        or str(mapping_rows[0].get("official_office_city") or "") != TARGET_OFFICE_CITY
    ):
        blockers.append("exact Orenburg warehouse mapping is missing or ambiguous")
    moscow_rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} "
            "WHERE seller_warehouse_id=? AND active=1 ORDER BY mapping_id",
            (MOSCOW_WAREHOUSE_ID,),
        )
    ] if WAREHOUSE_MAPPINGS_TABLE in tables else []
    if len(moscow_rows) != 1 or str(moscow_rows[0].get("facility_id") or "") != MOSCOW_FACILITY_ID:
        blockers.append("Moscow mapping changed")
    extension_rows = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {MAPPING_EXTENSIONS_TABLE} "
            "WHERE seller_warehouse_id=? ORDER BY extension_id",
            (TARGET_WAREHOUSE_ID,),
        )
    ] if MAPPING_EXTENSIONS_TABLE in tables else []
    extension = extension_rows[0] if len(extension_rows) == 1 else {}
    cutover = _latest_manifest(conn) if MANIFESTS_TABLE in tables else {}
    cutover_id = str(cutover.get("cutover_id") or "")
    if (
        len(extension_rows) != 1
        or str(extension.get("cutover_id") or "") != cutover_id
        or str(extension.get("facility_id") or "") != TARGET_FACILITY_ID
        or str(extension.get("deployed_sha") or "") != deployed_sha
        or not mapping_rows
        or str(extension.get("warehouse_mapping_id") or "")
        != str(mapping_rows[0].get("mapping_id") or "")
        or str(extension.get("mapping_digest") or "")
        != str(mapping_rows[0].get("mapping_digest") or "")
        or str(extension.get("official_evidence_digest") or "")
        != str(mapping_rows[0].get("official_evidence_digest") or "")
        or str(extension.get("source_receipt_document_id") or "")
        != RECEIPT_DOCUMENT_ID
        or str(extension.get("source_receipt_root_document_id") or "")
        != RECEIPT_ROOT_DOCUMENT_ID
    ):
        blockers.append("immutable Stage 7C mapping extension is missing or inconsistent")
    allocations = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {MAPPING_EXTENSION_ALLOCATIONS_TABLE} "
            "WHERE extension_id=? ORDER BY nm_id",
            (str(extension.get("extension_id") or ""),),
        )
    ] if MAPPING_EXTENSION_ALLOCATIONS_TABLE in tables and extension else []
    if len(allocations) != EXPECTED_RECEIPT_SKU_COUNT:
        blockers.append("mapping extension frozen allocations are incomplete")
    elif (
        sum(int(row["opening_quantity"]) for row in allocations)
        != EXPECTED_RECEIPT_QUANTITY
        or canonical_decimal_text(
            _decimal_sum(row["opening_capital_rub"] for row in allocations)
        )
        != EXPECTED_RECEIPT_CAPITAL_RUB
    ):
        blockers.append("mapping extension frozen allocation totals drifted")
    receipt = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?",
            (RECEIPT_DOCUMENT_ID,),
        )
    ] if DOCUMENTS_TABLE in tables else []
    receipt_lines = [
        dict(row)
        for row in conn.execute(
            f"""SELECT line_no,facility_id,pool,nm_id,quantity,capital_rub,
                       metadata_json
                FROM {DOCUMENT_LINES_TABLE}
                WHERE document_id=? AND facility_id=? AND pool='FBS'
                ORDER BY nm_id,line_no""",
            (RECEIPT_DOCUMENT_ID, TARGET_FACILITY_ID),
        )
    ] if DOCUMENT_LINES_TABLE in tables else []
    receipt_total_line_count = (
        int(
            conn.execute(
                f"SELECT COUNT(*) FROM {DOCUMENT_LINES_TABLE} WHERE document_id=?",
                (RECEIPT_DOCUMENT_ID,),
            ).fetchone()[0]
        )
        if DOCUMENT_LINES_TABLE in tables
        else 0
    )
    receipt_relation = [
        dict(row)
        for row in conn.execute(
            f"""SELECT * FROM {DOCUMENT_RELATIONS_TABLE}
                WHERE child_document_id=? AND relation_type='receipt_of'
                ORDER BY parent_document_id""",
            (RECEIPT_DOCUMENT_ID,),
        )
    ] if DOCUMENT_RELATIONS_TABLE in tables else []
    receipt_digest = _fingerprint(
        {
            "document": receipt[0] if len(receipt) == 1 else {},
            "relation": receipt_relation,
            "lines": receipt_lines,
        }
    )
    if (
        len(receipt) != 1
        or str(receipt[0].get("document_kind") or "") != "transfer_receipt"
        or str(receipt[0].get("root_document_id") or "") != RECEIPT_ROOT_DOCUMENT_ID
        or len(receipt_relation) != 1
        or str(receipt_relation[0].get("root_document_id") or "")
        != RECEIPT_ROOT_DOCUMENT_ID
        or receipt_total_line_count != EXPECTED_RECEIPT_SKU_COUNT
        or len(receipt_lines) != EXPECTED_RECEIPT_SKU_COUNT
        or len(
            {
                int(row["nm_id"])
                for row in receipt_lines
                if row["facility_id"] == TARGET_FACILITY_ID and row["pool"] == "FBS"
            }
        )
        != EXPECTED_RECEIPT_SKU_COUNT
        or sum(int(row["quantity"]) for row in receipt_lines if row["facility_id"] == TARGET_FACILITY_ID and row["pool"] == "FBS")
        != EXPECTED_RECEIPT_QUANTITY
        or canonical_decimal_text(
            _decimal_sum(
                row["capital_rub"]
                for row in receipt_lines
                if row["facility_id"] == TARGET_FACILITY_ID and row["pool"] == "FBS"
            )
        )
        != EXPECTED_RECEIPT_CAPITAL_RUB
        or str(extension.get("source_receipt_digest") or "") != receipt_digest
    ):
        blockers.append("accepted transfer receipt changed")
    current = _warehouse_order_current_rows(conn) if CURRENT_TABLE in tables else []
    events = _warehouse_order_event_rows(conn) if EVENTS_TABLE in tables else []
    if any(str(row.get("facility_id") or "") != TARGET_FACILITY_ID for row in current):
        blockers.append("an Orenburg order has lifecycle state outside FF Оренбург")
    if any(str(row.get("facility_id") or "") != TARGET_FACILITY_ID for row in events):
        blockers.append("an Orenburg order has lifecycle evidence outside FF Оренбург")
    duplicate_events = [
        dict(row)
        for row in conn.execute(
            f"""SELECT order_id,source_status_observation_sequence,event_type,COUNT(*) AS count
                FROM {EVENTS_TABLE}
                WHERE facility_id=?
                GROUP BY order_id,source_status_observation_sequence,event_type
                HAVING COUNT(*)>1""",
            (TARGET_FACILITY_ID,),
        )
    ] if EVENTS_TABLE in tables else []
    if duplicate_events:
        blockers.append("duplicate Orenburg lifecycle events exist")
    target_order_ids = sorted({int(row["order_id"]) for row in current})
    duplicate_operations = (
        [
            dict(row)
            for row in conn.execute(
                f"""SELECT source_id,source_revision,COUNT(*) AS count
                    FROM {OPERATIONS_TABLE}
                    WHERE source_type='fbs_order_lifecycle_event'
                      AND source_id IN ({','.join('?' for _ in target_order_ids)})
                    GROUP BY source_id,source_revision HAVING COUNT(*)>1""",
                tuple(str(order_id) for order_id in target_order_ids),
            )
        ]
        if target_order_ids
        else []
    )
    if duplicate_operations:
        blockers.append("duplicate Orenburg warehouse operations exist")
    pending = [
        dict(row)
        for row in conn.execute(
            f"""SELECT pending.pending_id,pending.order_id,
                       pending.source_status_observation_sequence
                FROM {IDENTITY_PENDING_TABLE} AS pending
                JOIN {STATUS_OBSERVATIONS_TABLE} AS status
                  ON status.observation_sequence=pending.source_status_observation_sequence
                JOIN {OBSERVATIONS_TABLE} AS source
                  ON source.order_id=status.order_id
                 AND source.source_revision=status.order_revision
                LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} AS resolution
                  ON resolution.pending_id=pending.pending_id
                WHERE source.warehouse_id=? AND source.office_id=?
                  AND resolution.pending_id IS NULL
                ORDER BY pending.source_status_observation_sequence""",
            (TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID),
        )
    ] if IDENTITY_PENDING_TABLE in tables else []
    drain = conn.execute(
        f"SELECT * FROM {DRAIN_STATE_TABLE} WHERE cutover_id=?", (cutover_id,)
    ).fetchone() if DRAIN_STATE_TABLE in tables and cutover_id else None
    frozen_boundary = json.loads(str(extension.get("frozen_boundary_json") or "{}")) if extension else {}
    frozen_status_w = int(
        frozen_boundary.get("status_observation_watermark_sequence") or 0
    )
    frozen_order_w = int(
        frozen_boundary.get("order_observation_watermark_sequence") or 0
    )
    frozen_status_rows = _frozen_target_rows(
        conn,
        order_watermark=frozen_order_w,
        status_watermark=frozen_status_w,
    )
    if (
        str(extension.get("frozen_rows_digest") or "")
        != _fingerprint(frozen_status_rows)
    ):
        blockers.append("complete frozen Orenburg backlog digest drifted")
    if frozen_boundary:
        reread_boundary = ff_pool_fbs_accounting_boundary_snapshot(
            conn,
            boundary_at=str(frozen_boundary.get("local_boundary_at") or ""),
            watermarks=frozen_boundary,
        )
        if (
            reread_boundary.get("blockers")
            or str(reread_boundary.get("frozen_evidence_digest") or "")
            != str(frozen_boundary.get("frozen_evidence_digest") or "")
        ):
            blockers.append("compound frozen accounting boundary drifted")
    frozen_order_ids = sorted({int(row["order_id"]) for row in frozen_status_rows})
    frozen_identity_material = {
        (
            int(row["order_id"]),
            str(row["order_revision"]),
            int(row["nm_id"]),
            int(row["chrt_id"] or 0),
            str((_json_list(str(row["skus_json"])) or [""])[0]),
            str(row["seller_sku"] or ""),
        )
        for row in frozen_status_rows
        if len(_json_list(str(row["skus_json"]))) == 1
    }
    matched_identity_material = {
        (
            int(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3] or 0),
            str(row[4] or ""),
            str(row[5] or ""),
        )
        for row in conn.execute(
            f"""SELECT evidence.order_id,evidence.order_revision,
                       evidence.nm_id,evidence.chrt_id,evidence.barcode,
                       evidence.seller_sku
                FROM {IDENTITY_EVIDENCE_TABLE} AS evidence
                JOIN {IDENTITY_MAPPINGS_TABLE} AS identity
                  ON identity.mapping_id=evidence.identity_mapping_id
                 AND identity.source_nm_id=evidence.nm_id
                 AND identity.source_chrt_id=evidence.chrt_id
                 AND identity.source_barcode=evidence.barcode
                 AND identity.source_sku=evidence.seller_sku
                 AND identity.active=1
                WHERE evidence.warehouse_id=? AND evidence.outcome='matched'
                  AND evidence.warehouse_mapping_id=?""",
            (
                TARGET_WAREHOUSE_ID,
                str(mapping_rows[0].get("mapping_id") or "") if mapping_rows else "",
            ),
        )
    }
    missing_frozen_identity_material = sorted(
        frozen_identity_material - matched_identity_material
    )
    if missing_frozen_identity_material:
        blockers.append("frozen Orenburg exact identity re-evidence is incomplete")
    current_order_ids = {int(row["order_id"]) for row in current}
    event_covered_frozen_sequences = {
        int(row[0])
        for row in conn.execute(
            f"""SELECT DISTINCT event.source_status_observation_sequence
                FROM {EVENTS_TABLE} AS event
                WHERE event.order_id IN ({','.join('?' for _ in frozen_order_ids)})
                  AND event.source_status_observation_sequence>0""",
            tuple(frozen_order_ids),
        )
    } if frozen_order_ids else set()
    late_covered_frozen_sequences: set[int] = set()
    if frozen_order_ids and LATE_EVIDENCE_TABLE in tables:
        late_covered_frozen_sequences.update(
            int(row[0])
            for row in conn.execute(
                f"""SELECT DISTINCT source_status_observation_sequence
                    FROM {LATE_EVIDENCE_TABLE}
                    WHERE order_id IN ({','.join('?' for _ in frozen_order_ids)})""",
                tuple(frozen_order_ids),
            )
        )
    covered_frozen_status_sequences = (
        event_covered_frozen_sequences | late_covered_frozen_sequences
    )
    current_required_order_ids = {
        int(row["order_id"])
        for row in frozen_status_rows
        if int(row["status_sequence"]) in event_covered_frozen_sequences
    }
    missing_frozen_current = sorted(current_required_order_ids - current_order_ids)
    missing_frozen_status_effects = sorted(
        int(row["status_sequence"])
        for row in frozen_status_rows
        if int(row["status_sequence"]) not in covered_frozen_status_sequences
    )
    frozen_pending = [
        row
        for row in pending
        if int(row["source_status_observation_sequence"]) <= frozen_status_w
    ]
    post_w_pending = [
        row
        for row in pending
        if int(row["source_status_observation_sequence"]) > frozen_status_w
    ]
    frozen_current = [
        row for row in current if int(row["order_id"]) in set(frozen_order_ids)
    ]
    frozen_late_order_ids = {
        int(row["order_id"])
        for row in frozen_status_rows
        if int(row["status_sequence"]) in late_covered_frozen_sequences
    } - current_required_order_ids
    if frozen_pending:
        blockers.append("frozen exact Orenburg backlog still contains unresolved rows")
    if missing_frozen_current or missing_frozen_status_effects:
        blockers.append("frozen Orenburg backlog lifecycle partition is incomplete")
    if drain is None or int(drain[4]) < int(frozen_boundary.get("status_observation_watermark_sequence") or 0):
        blockers.append("ordinary collector drain has not caught through frozen W")
    current_status_max = int(
        conn.execute(
            f"SELECT COALESCE(MAX(observation_sequence),0) FROM {STATUS_OBSERVATIONS_TABLE}"
        ).fetchone()[0]
    )
    if drain is None or int(drain[4]) < current_status_max:
        blockers.append("ordinary collector drain has not caught through post-W suffix")
    collector = conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE state_id=1").fetchone() if STATE_TABLE in tables else None
    collector_state = dict(collector) if collector is not None else {}
    if (
        not collector_state
        or str(collector_state.get("last_status") or "") != "success"
        or not bool(collector_state.get("complete"))
        or int(collector_state.get("next_cursor") or 0) != 0
        or str(collector_state.get("last_error") or "")
    ):
        blockers.append("official collector is not healthy after mapping apply")
    balances = [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' ORDER BY nm_id",
            (TARGET_FACILITY_ID,),
        )
    ] if BALANCES_TABLE in tables else []
    reserved_by_nm: dict[int, int] = {}
    for row in current:
        if str(row["state"]) == "reserved":
            reserved_by_nm[int(row["nm_id"])] = reserved_by_nm.get(int(row["nm_id"]), 0) + int(row["quantity"])
    availability = [
        {
            "nm_id": int(row["nm_id"]),
            "physical": int(row["quantity"]),
            "reserved": int(reserved_by_nm.get(int(row["nm_id"]), 0)),
            "available": int(row["quantity"]) - int(reserved_by_nm.get(int(row["nm_id"]), 0)),
            "capital_rub": str(row["capital_rub"]),
            "wac_rub": row["wac_rub"],
        }
        for row in balances
    ]
    parity = _parity(conn)
    if parity.get("status") != "pass":
        blockers.append("current facility-pool aggregate parity failed")
    return {
        "mapping": mapping_rows,
        "mapping_extension": extension,
        "mapping_extension_allocations": allocations,
        "moscow_mapping": moscow_rows,
        "receipt": receipt,
        "receipt_relation": receipt_relation,
        "receipt_digest": receipt_digest,
        "receipt_lines_digest": _fingerprint(receipt_lines),
        "backlog_partition": {
            "frozen_order_count": len(frozen_order_ids),
            "frozen_status_count": len(frozen_status_rows),
            "frozen_identity_evidence_count": len(
                frozen_identity_material & matched_identity_material
            ),
            "missing_frozen_identity_evidence_count": len(
                missing_frozen_identity_material
            ),
            "frozen_rows_digest": _fingerprint(frozen_status_rows),
            "missing_frozen_current_order_ids": missing_frozen_current,
            "missing_frozen_status_effect_sequences": missing_frozen_status_effects,
            "frozen_reserved_count": sum(
                1 for row in frozen_current if row["state"] == "reserved"
            ),
            "frozen_fulfilled_count": sum(
                1
                for row in frozen_current
                if row["state"] in {"fulfilled", "fulfilled_reconciliation"}
            ),
            "frozen_cancelled_or_released_count": sum(
                1
                for row in frozen_current
                if row["state"] in {"cancelled_noop", "released"}
            ),
            "frozen_late_noop_count": len(frozen_late_order_ids),
            "current_count": len(current),
            "event_count": len(events),
            "unresolved_pending_count": len(pending),
            "frozen_unresolved_pending_count": len(frozen_pending),
            "post_w_unresolved_pending_count": len(post_w_pending),
            "reserved_count": sum(1 for row in current if row["state"] == "reserved"),
            "fulfilled_count": sum(1 for row in current if row["state"] in {"fulfilled", "fulfilled_reconciliation"}),
            "cancelled_noop_count": sum(1 for row in current if row["state"] == "cancelled_noop"),
            "released_count": sum(1 for row in current if row["state"] == "released"),
        },
        "duplicate_events": duplicate_events,
        "duplicate_operations": duplicate_operations,
        "collector_state": collector_state,
        "drain_state": dict(drain) if drain is not None else {},
        "current_status_observation_max": current_status_max,
        "frozen_boundary": frozen_boundary,
        "availability": availability,
        "pool_aggregate_parity": parity,
        "ui_api_evidence": {
            "warehouse_id": TARGET_WAREHOUSE_ID,
            "facility_id": TARGET_FACILITY_ID,
            "order_count": len(current),
            "raw_payload_exposed": False,
            "pii_exposed": False,
        },
        "blockers": blockers,
    }


def _frozen_target_rows(
    conn: sqlite3.Connection,
    *,
    order_watermark: int,
    status_watermark: int,
) -> list[dict[str, Any]]:
    """Return the complete immutable Orenburg target subset under compound W."""

    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT status.observation_sequence AS status_sequence,
                       status.order_id,status.order_revision,status.status_digest,
                       status.supplier_status,status.wb_status,
                       status.positive_quantity,status.observed_at AS status_observed_at,
                       source.observation_sequence AS order_sequence,
                       source.observation_id,source.source_revision,
                       source.source_created_at,source.observed_at AS order_observed_at,
                       source.warehouse_id,source.office_id,source.nm_id,
                       source.chrt_id,source.seller_sku,source.skus_json
                FROM {STATUS_OBSERVATIONS_TABLE} AS status
                JOIN {OBSERVATIONS_TABLE} AS source
                  ON source.order_id=status.order_id
                 AND source.source_revision=status.order_revision
                WHERE status.observation_sequence<=?
                  AND source.observation_sequence<=?
                  AND source.warehouse_id=? AND source.office_id=?
                ORDER BY status.observation_sequence""",
            (
                int(status_watermark),
                int(order_watermark),
                TARGET_WAREHOUSE_ID,
                TARGET_OFFICE_ID,
            ),
        )
    ]


def _parity(conn: sqlite3.Connection) -> dict[str, Any]:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        return {"status": "missing", "mismatched_nm_ids": []}
    aggregate_rows = [
        {"nm_id": int(row[0]), "quantity": row[1], "capital_rub": row[2]}
        for row in conn.execute(
            """SELECT nm_id,quantity,capital_rub
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
            (str(active[0]),),
        )
    ]
    result = evaluate_ff_pool_aggregate_parity(conn, aggregate_rows)
    return {
        "status": result.status,
        "quantity_mismatched_nm_ids": list(result.quantity_mismatched_nm_ids),
        "canonical_capital_mismatched_nm_ids": list(
            result.canonical_capital_mismatched_nm_ids
        ),
        "raw_capital_mismatched_nm_ids": list(result.raw_capital_mismatched_nm_ids),
    }


def _verify_frozen_source(reviewed: Mapping[str, Any], fresh: Mapping[str, Any]) -> None:
    keys = (
        "source_digest",
        "accounting_boundary",
        "cutover",
        "receipt",
        "receipt_relation",
        "receipt_lines",
        "allocations",
        "frozen_backlog",
        "identity_plan",
        "non_target_invariants",
    )
    drift = [key for key in keys if reviewed.get(key) != fresh.get(key)]
    if drift:
        raise FfFbsMappingExtensionProductionError(
            "frozen production source drifted: " + ", ".join(drift)
        )


def _write_before_image(
    *, db_path: Path, destination: Path, reviewed_plan: Mapping[str, Any]
) -> dict[str, Any]:
    if destination.is_file():
        return {
            "path": str(destination),
            "sha256": _sha256_file(destination),
            "mode": oct(destination.stat().st_mode & 0o777),
            "resumed": True,
        }
    source = dict(reviewed_plan["source"])
    with closing(_open_query_only(db_path)) as conn:
        target_observations = _rows_where(
            conn,
            OBSERVATIONS_TABLE,
            "warehouse_id=? AND office_id=?",
            (TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID),
        )
        order_ids = sorted({int(row["order_id"]) for row in target_observations})
        pending_rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT pending.* FROM {IDENTITY_PENDING_TABLE} AS pending
                    JOIN {STATUS_OBSERVATIONS_TABLE} AS status
                      ON status.observation_sequence=
                         pending.source_status_observation_sequence
                    JOIN {OBSERVATIONS_TABLE} AS source
                      ON source.order_id=status.order_id
                     AND source.source_revision=status.order_revision
                    WHERE source.warehouse_id=? AND source.office_id=?
                    ORDER BY pending.source_status_observation_sequence""",
                (TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID),
            )
        ]
        pending_ids = [str(row["pending_id"]) for row in pending_rows]
        payload = {
            "contract_name": CONTRACT_NAME,
            "kind": "exact_target_before_image",
            "fingerprint": str(reviewed_plan["fingerprint"]),
            "warehouse_mappings": _rows_where(
                conn, WAREHOUSE_MAPPINGS_TABLE, "seller_warehouse_id=?", (TARGET_WAREHOUSE_ID,)
            ),
            "identity_mappings": [
                dict(row)
                for item in source["identity_plan"]
                for row in conn.execute(
                    f"""SELECT * FROM {IDENTITY_MAPPINGS_TABLE}
                        WHERE source_nm_id=? AND source_chrt_id=?
                          AND source_barcode=? AND source_sku=? ORDER BY mapping_id""",
                    (
                        item["source_nm_id"], item["source_chrt_id"],
                        item["source_barcode"], item["source_sku"],
                    ),
                )
            ],
            "target_identity_evidence": _rows_where(
                conn, IDENTITY_EVIDENCE_TABLE, "warehouse_id=?", (TARGET_WAREHOUSE_ID,)
            ),
            "target_observations": target_observations,
            "target_status_observations": (
                [
                    dict(row)
                    for row in conn.execute(
                        f"""SELECT status.* FROM {STATUS_OBSERVATIONS_TABLE} AS status
                            WHERE status.order_id IN (
                                {','.join('?' for _ in order_ids)}
                            ) ORDER BY status.observation_sequence""",
                        tuple(order_ids),
                    )
                ]
                if order_ids
                else []
            ),
            "target_identity_pending": pending_rows,
            "target_identity_pending_resolutions": (
                [
                    dict(row)
                    for row in conn.execute(
                        f"""SELECT * FROM {IDENTITY_PENDING_RESOLUTIONS_TABLE}
                            WHERE pending_id IN (
                                {','.join('?' for _ in pending_ids)}
                            ) ORDER BY source_status_observation_sequence""",
                        tuple(pending_ids),
                    )
                ]
                if pending_ids
                else []
            ),
            "target_current": _warehouse_order_current_rows(conn),
            "target_events": _warehouse_order_event_rows(conn),
            "target_balances": _rows_where(
                conn, BALANCES_TABLE, "facility_id=? AND pool='FBS'", (TARGET_FACILITY_ID,)
            ),
            "order_ids": order_ids,
            "recovery": {
                "automatic_destructive_rollback": False,
                "forward_reconciliation": True,
                "t2_domain_checkpoint_is_primary": True,
            },
        }
    _write_private_json(destination, payload)
    return {
        "path": str(destination),
        "sha256": _sha256_file(destination),
        "mode": oct(destination.stat().st_mode & 0o777),
        "resumed": False,
    }


def _validate_reviewed_plan(
    reviewed: Mapping[str, Any],
    *,
    fingerprint: str,
    deployed_sha: str,
    approval_reference: str,
    actor: str,
) -> None:
    if (
        str(reviewed.get("contract_name") or "") != CONTRACT_NAME
        or int(reviewed.get("contract_version") or 0) != CONTRACT_VERSION
        or str(reviewed.get("mode") or "") != "dry_run"
        or str(reviewed.get("deployed_sha") or "") != deployed_sha
        or str(reviewed.get("fingerprint") or "") != fingerprint
        or not bool(reviewed.get("apply_allowed"))
        or dict(reviewed.get("scope") or {}).get("seller_warehouse_id")
        != TARGET_WAREHOUSE_ID
    ):
        raise FfFbsMappingExtensionProductionError(
            "reviewed plan does not match this exact production apply"
        )
    recalculated = _fingerprint(
        {
            key: value
            for key, value in reviewed.items()
            if key not in {"fingerprint", "generated_at"}
        }
    )
    if recalculated != fingerprint:
        raise FfFbsMappingExtensionProductionError(
            "reviewed plan fingerprint is invalid"
        )
    if not str(approval_reference).strip() or not str(actor).strip():
        raise FfFbsMappingExtensionProductionError(
            "apply requires exact approval reference and actor"
        )


def _verify_completed_readback(
    readback: Mapping[str, Any],
    *,
    fingerprint: str,
    expected_effects: Mapping[str, Any] | None = None,
) -> None:
    partition = dict(readback.get("backlog_partition") or {})
    expected = dict(expected_effects or {})
    expected_mismatch = bool(expected) and any(
        int(partition.get(actual_key) or 0) != int(expected.get(expected_key) or 0)
        for actual_key, expected_key in (
            ("frozen_order_count", "frozen_target_order_count"),
            ("frozen_status_count", "frozen_target_status_count"),
            ("frozen_identity_evidence_count", "frozen_identity_evidence_count"),
            ("frozen_reserved_count", "frozen_expected_final_reserved_count"),
            ("frozen_fulfilled_count", "frozen_expected_final_fulfilled_count"),
            (
                "frozen_cancelled_or_released_count",
                "frozen_expected_final_cancelled_or_released_count",
            ),
            ("frozen_late_noop_count", "frozen_expected_late_noop_count"),
        )
    )
    if (
        str(readback.get("status") or "") != "ready"
        or readback.get("blockers")
        or str((readback.get("mapping_extension") or {}).get("plan_fingerprint") or "")
        != fingerprint
        or int((readback.get("backlog_partition") or {}).get("frozen_unresolved_pending_count") or 0)
        != 0
        or (readback.get("pool_aggregate_parity") or {}).get("status") != "pass"
        or expected_mismatch
    ):
        raise FfFbsMappingExtensionProductionError(
            "post-apply query-only reconciliation is incomplete: "
            + _json(
                {
                    "blockers": list(readback.get("blockers") or []),
                    "backlog_partition": partition,
                    "expected_effects": expected,
                    "expected_mismatch": expected_mismatch,
                }
            )
        )


def _target_current_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT current.* FROM {CURRENT_TABLE} AS current
                WHERE current.facility_id=? ORDER BY current.order_id""",
            (TARGET_FACILITY_ID,),
        )
    ]


def _target_event_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT event.* FROM {EVENTS_TABLE} AS event
                WHERE event.facility_id=? ORDER BY event.event_sequence""",
            (TARGET_FACILITY_ID,),
        )
    ]


def _warehouse_order_current_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT current.* FROM {CURRENT_TABLE} AS current
                WHERE EXISTS(
                    SELECT 1 FROM {OBSERVATIONS_TABLE} AS source
                    WHERE source.order_id=current.order_id
                      AND source.warehouse_id=? AND source.office_id=?
                )
                ORDER BY current.order_id""",
            (TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID),
        )
    ]


def _warehouse_order_event_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT event.* FROM {EVENTS_TABLE} AS event
                WHERE EXISTS(
                    SELECT 1 FROM {OBSERVATIONS_TABLE} AS source
                    WHERE source.order_id=event.order_id
                      AND source.warehouse_id=? AND source.office_id=?
                )
                ORDER BY event.event_sequence""",
            (TARGET_WAREHOUSE_ID, TARGET_OFFICE_ID),
        )
    ]


def _latest_manifest(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        f"SELECT manifest_json FROM {MANIFESTS_TABLE} "
        "ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise FfFbsMappingExtensionProductionError("Stage 7C manifest is unavailable")
    return json.loads(str(row[0]))


def _rows_where(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    parameters: tuple[Any, ...],
) -> list[dict[str, Any]]:
    if table not in _table_names(conn):
        return []
    return [
        dict(row)
        for row in conn.execute(f"SELECT * FROM {table} WHERE {where}", parameters)
    ]


def _open_query_only(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{Path(db_path).resolve()}?mode=ro", uri=True, timeout=60.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _decimal_sum(values: Any) -> Decimal:
    with localcontext() as context:
        context.prec = 160
        return sum((Decimal(str(value)) for value in values), Decimal("0"))


def _json_list(raw: str) -> list[Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _require_utc(value: str) -> None:
    if not str(value).endswith("Z"):
        raise FfFbsMappingExtensionProductionError("timestamp must be canonical UTC")
    datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FfFbsMappingExtensionProductionError("evidence must be a JSON object")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
