"""Owner-gated publication of the exact confirmed Moscow FBS zero cohort."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from packages.application.ff_pool_cutover import MANIFESTS_TABLE
from packages.application.ff_pool_documents import (
    DOCUMENT_LINES_TABLE,
    DOCUMENTS_TABLE,
    REQUESTS_TABLE,
    FfPoolDocumentService,
)
from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry
from packages.contracts.ff_pool_documents import DocumentIdentity


CONTRACT_NAME = "ff_pool_zero_physical_production_v1"
CONTRACT_VERSION = 1
SOURCE_TYPE = "ff_pool_confirmed_zero_physical_v1"
TARGET_FACILITY_ID = "fff_d67e8c823d5f81dd988d00dbfea6"
TARGET_FACILITY_NAME = "FF Москва"
TARGET_FACILITY_CITY = "Москва"
TARGET_POOL = "FBS"
TARGET_NM_IDS = (
    497413772,
    497415593,
    497416931,
    1221231049,
    1221235702,
    1221244040,
    1221249681,
    1235346302,
    1235353505,
    1235356960,
    1235358879,
    1235360281,
    1235361692,
    1235365622,
    1235366828,
    1235368116,
    1235369738,
    1235373410,
    1235374572,
    1235375860,
    1235377899,
    1235379341,
    1235381785,
    1235384726,
    1235387930,
    1235392011,
    1235393709,
    1235398515,
    1235399866,
    1235404761,
    1235405720,
    1235406475,
    1235406984,
    1235407826,
    1235409896,
    1235411727,
    1235412880,
    1235413454,
    1235414081,
    1235419785,
    1235421650,
)
ABSOLUTE_TARGET = 0
DECISION_DATE = "2026-08-19"
REQUEST_ID = "ff-moscow-fbs-zero-physical-41-20260819-v1"
SOURCE_ID = "ff-moscow-fbs-zero-physical-41-20260819"
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")


class FfPoolZeroPhysicalProductionError(RuntimeError):
    pass


class FfPoolZeroPhysicalProductionMutation:
    """Exact dry-run/apply/readback contract for the owner-confirmed cohort."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        deployed_sha: str,
        timestamp_factory: Any | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir))
        self.deployed_sha = str(deployed_sha).strip().lower()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise FfPoolZeroPhysicalProductionError(
                "deployed_sha must be an exact 40-hex SHA"
            )
        self.timestamp_factory = timestamp_factory or _utc_now

    def build_plan(self) -> dict[str, Any]:
        snapshot = _read_snapshot(self.runtime.db_path)
        blockers = list(snapshot["blockers"])
        insert_count = sum(
            1 for item in snapshot["target_rows"] if item["state"] == "missing"
        )
        already_materialized = [
            int(item["nm_id"])
            for item in snapshot["target_rows"]
            if item["state"] != "missing"
        ]
        if already_materialized:
            blockers.append(
                "target rows must all still be missing before first apply: "
                + ", ".join(str(item) for item in already_materialized)
            )
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "deployed_sha": self.deployed_sha,
            "generated_at": str(self.timestamp_factory()),
            "scope": {
                "facility_id": TARGET_FACILITY_ID,
                "facility_name": TARGET_FACILITY_NAME,
                "facility_city": TARGET_FACILITY_CITY,
                "pool": TARGET_POOL,
                "nm_ids": list(TARGET_NM_IDS),
                "absolute_physical_target": ABSOLUTE_TARGET,
            },
            "owner_decision": {
                "decision_date": DECISION_DATE,
                "meaning": "confirmed_physical_zero",
                "missing_row_is_zero_before_apply": False,
            },
            "pre_change": snapshot,
            "expected_effects": {
                "balance_insert_count": insert_count,
                "balance_update_count": 0,
                "pool_inventory_document_count": 1 if not blockers else 0,
                "warehouse_operation_count": 1 if not blockers else 0,
                "absolute_target_line_count": len(TARGET_NM_IDS) if not blockers else 0,
                "movement_line_count": 0,
                "quantity_delta": 0,
                "capital_delta_rub": "0",
                "reservation_write_count": 0,
                "supplier_shipment_write_count": 0,
                "wb_write_count": 0,
                "fbs_calculation_count": 0,
                "factory_order_count": 0,
            },
            "recovery": {
                "tier": "T1",
                "kind": "exact_target_before_image",
                "server_owned": True,
                "missing_before_rows_retained_as_exact_undo_evidence": True,
                "full_database_copy_required": False,
                "recovery_path": "forward_reconciliation_or_separately_authorized_t1_supersession",
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
            raise FfPoolZeroPhysicalProductionError("evidence_dir must be absolute")
        evidence_root.mkdir(parents=True, exist_ok=True)
        evidence_path = evidence_root / (
            "ff-pool-zero-physical-"
            + fingerprint.removeprefix("sha256:")[:16]
            + ".evidence.json"
        )
        if evidence_path.is_file():
            prior = _read_json_object(evidence_path)
            stored_evidence_fingerprint = str(
                prior.get("evidence_fingerprint") or ""
            )
            fingerprint_payload = {
                key: value
                for key, value in prior.items()
                if key != "evidence_fingerprint"
            }
            if (
                str(prior.get("contract_name") or "") != CONTRACT_NAME
                or str(prior.get("deployed_sha") or "") != self.deployed_sha
                or str(prior.get("manifest_fingerprint") or "") != fingerprint
                or str(prior.get("approval_reference") or "")
                != str(approval_reference).strip()
                or str(prior.get("actor") or "") != str(actor).strip()
                or stored_evidence_fingerprint != _fingerprint(fingerprint_payload)
            ):
                raise FfPoolZeroPhysicalProductionError(
                    "existing evidence is invalid or does not match the reviewed manifest"
                )
            readback = self.readback()
            _verify_completed_readback(
                readback,
                require_document=True,
                expected_fingerprint=fingerprint,
                expected_approval_reference=str(approval_reference).strip(),
                expected_actor=str(actor).strip(),
            )
            return {
                **prior,
                "idempotent": True,
                "readback": readback,
                "evidence_path": str(evidence_path),
                "evidence_sha256": _sha256_file(evidence_path),
            }

        existing_readback = self.readback()
        existing_document = dict(existing_readback.get("document_evidence") or {})
        if (
            existing_document.get("state") == "complete"
            and str(existing_document.get("source_revision") or "") == fingerprint
        ):
            return self._finalize_evidence(
                reviewed_plan=reviewed_plan,
                fingerprint=fingerprint,
                approval_reference=str(approval_reference).strip(),
                actor=str(actor).strip(),
                evidence_path=evidence_path,
                readback=existing_readback,
                idempotent=True,
                recovered_after_response_loss=True,
            )

        fresh = self.build_plan()
        if str(fresh["fingerprint"]) != fingerprint:
            raise FfPoolZeroPhysicalProductionError(
                "production ledger state changed after the reviewed dry-run"
            )
        if not fresh["apply_allowed"]:
            raise FfPoolZeroPhysicalProductionError(
                "reviewed production plan is not apply-eligible: "
                + "; ".join(str(item) for item in fresh["blockers"])
            )
        if int(fresh["expected_effects"]["balance_insert_count"]) != len(
            TARGET_NM_IDS
        ):
            raise FfPoolZeroPhysicalProductionError(
                "reviewed plan no longer contains exactly the 41 missing rows"
            )

        scope = dict(reviewed_plan["scope"])
        facility_id = str(scope["facility_id"])
        epoch = int(fresh["pre_change"]["feature_epoch"]["epoch"])
        identity = DocumentIdentity(
            request_id=_request_id_for_fingerprint(fingerprint),
            source_system="owner_business_decision",
            source_type=SOURCE_TYPE,
            source_id=SOURCE_ID,
            source_revision=fingerprint,
            idempotency_epoch=epoch,
            actor=str(actor).strip(),
            business_date=DECISION_DATE,
        )
        manifest = {
            "facility_id": facility_id,
            "scope": TARGET_POOL,
            "targets": [
                {"nm_id": nm_id, "target_fbs": ABSOLUTE_TARGET}
                for nm_id in TARGET_NM_IDS
            ],
            "production_authorization": {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "pool": TARGET_POOL,
                "target_nm_ids": list(TARGET_NM_IDS),
                "expected_missing_nm_ids": list(TARGET_NM_IDS),
                "absolute_target": ABSOLUTE_TARGET,
                "decision_date": DECISION_DATE,
                "reviewed_plan_fingerprint": fingerprint,
                "approval_reference": str(approval_reference).strip(),
            },
        }
        service = FfPoolDocumentService(
            db_path=self.runtime.db_path,
            runtime_dir=self.runtime.runtime_dir,
            timestamp_factory=self.timestamp_factory,
            resume=False,
        )
        preview = service.accept_preview(
            identity=identity,
            document_kind="pool_inventory",
            manifest=manifest,
        )
        if preview.get("state") not in {"ready", "complete"}:
            raise FfPoolZeroPhysicalProductionError(
                "confirmed zero document did not reach ready state: "
                + json.dumps(preview.get("error") or {}, ensure_ascii=False, sort_keys=True)
            )
        posted = (
            preview
            if preview.get("state") == "complete"
            else service.post(str(preview["request_id"]))
        )
        if posted.get("state") != "complete":
            raise FfPoolZeroPhysicalProductionError(
                "confirmed zero document did not complete: "
                + json.dumps(posted.get("error") or {}, ensure_ascii=False, sort_keys=True)
            )
        readback = self.readback()
        _verify_completed_readback(
            readback,
            require_document=True,
            expected_fingerprint=fingerprint,
            expected_approval_reference=str(approval_reference).strip(),
            expected_actor=str(actor).strip(),
        )
        _verify_non_target_invariants(
            before=fresh["pre_change"]["non_target_invariants"],
            after=readback["non_target_invariants"],
        )
        return self._finalize_evidence(
            reviewed_plan=reviewed_plan,
            fingerprint=fingerprint,
            approval_reference=str(approval_reference).strip(),
            actor=str(actor).strip(),
            evidence_path=evidence_path,
            readback=readback,
            idempotent=False,
            recovered_after_response_loss=False,
        )

    def _finalize_evidence(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        fingerprint: str,
        approval_reference: str,
        actor: str,
        evidence_path: Path,
        readback: Mapping[str, Any],
        idempotent: bool,
        recovered_after_response_loss: bool,
    ) -> dict[str, Any]:
        _verify_completed_readback(
            readback,
            require_document=True,
            expected_fingerprint=fingerprint,
            expected_approval_reference=approval_reference,
            expected_actor=actor,
        )
        document = dict(readback.get("document_evidence") or {})
        recovery_id = str(document.get("recovery_operation_id") or "")
        recovery = WarehouseRecoveryRegistry(
            runtime_dir=self.runtime.runtime_dir,
            db_path=self.runtime.db_path,
        ).get_operation(recovery_id)
        if not recovery or str(recovery.get("lifecycle") or "") != "retained":
            raise FfPoolZeroPhysicalProductionError(
                "server-owned T1 recovery evidence is missing or not retained"
            )
        undo_artifacts = [
            item
            for item in recovery.get("artifacts") or []
            if item.get("artifact_kind") == "undo" and item.get("state") == "verified"
        ]
        if len(undo_artifacts) != 1:
            raise FfPoolZeroPhysicalProductionError(
                "server-owned T1 undo evidence is incomplete"
            )
        before_invariants = dict(
            (reviewed_plan.get("pre_change") or {}).get("non_target_invariants") or {}
        )
        after_invariants = dict(readback.get("non_target_invariants") or {})
        changed_invariants = sorted(
            key
            for key in set(before_invariants) | set(after_invariants)
            if before_invariants.get(key) != after_invariants.get(key)
        )
        evidence: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "complete",
            "manifest_fingerprint": fingerprint,
            "deployed_sha": self.deployed_sha,
            "approval_reference": str(approval_reference).strip(),
            "actor": str(actor).strip(),
            "completed_at": str(self.timestamp_factory()),
            "document": {
                "request_id": str(document.get("request_id") or ""),
                "document_id": str(
                    (document.get("document") or {}).get("document_id") or ""
                ),
                "posted_manifest_sha256": str(
                    document.get("posted_manifest_sha256") or ""
                ),
                "absolute_target_line_count": len(TARGET_NM_IDS),
                "movement_line_count": 0,
            },
            "recovery": {
                "operation_id": recovery_id,
                "tier": recovery.get("tier"),
                "lifecycle": recovery.get("lifecycle"),
                "undo_artifact": undo_artifacts[0],
            },
            "pre_change_digest": str(
                (reviewed_plan.get("pre_change") or {}).get("target_digest") or ""
            ),
            "non_target_invariants_before": before_invariants,
            "non_target_invariants_after": after_invariants,
            "non_target_invariants_exact_match": not changed_invariants,
            "post_release_concurrent_drift": changed_invariants,
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
        snapshot = _read_snapshot(self.runtime.db_path)
        planning = InventoryPlanningReadModel(
            db_path=self.runtime.db_path
        ).current_fbs_facilities(requested_nm_ids=snapshot["active_nm_ids"])
        facility_id = str(snapshot["facility"].get("facility_id") or "")
        facility_readiness: list[dict[str, Any]] = []
        for raw in planning.get("facilities") or []:
            if not bool(raw.get("active")):
                continue
            missing_physical = sorted(
                int(item["nm_id"])
                for item in raw.get("sku_values") or []
                if item.get("physical") is None or item.get("available") is None
            )
            is_moscow = (
                str(raw.get("facility_id") or "") == facility_id
                or str(raw.get("city") or "") == TARGET_FACILITY_CITY
                or str(raw.get("name") or "") == TARGET_FACILITY_NAME
            )
            facility_blockers: list[str] = []
            if missing_physical:
                facility_blockers.append(
                    "missing exact facility-specific FBS physical rows: "
                    + ", ".join(str(item) for item in missing_physical)
                )
            if not is_moscow:
                facility_blockers.append(
                    "MVP national demand is executable only for FF Москва"
                )
            facility_readiness.append(
                {
                    "facility_id": str(raw.get("facility_id") or ""),
                    "name": str(raw.get("name") or ""),
                    "city": str(raw.get("city") or ""),
                    "physical": raw.get("physical"),
                    "reserved": raw.get("reserved"),
                    "available": raw.get("available"),
                    "missing_physical_nm_ids": missing_physical,
                    "calculation_enabled": not facility_blockers,
                    "blockers": facility_blockers,
                }
            )
        moscow = next(
            (
                dict(item)
                for item in facility_readiness
                if str(item.get("facility_id") or "") == facility_id
            ),
            {},
        )
        missing = list(moscow.get("missing_physical_nm_ids") or [])
        selected_blockers = [
            *snapshot["blockers"],
            *list(moscow.get("blockers") or []),
        ]
        document_evidence = _document_evidence(self.runtime.db_path)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if not selected_blockers else "blocked",
            "deployed_sha": self.deployed_sha,
            "facility": snapshot["facility"],
            "pool": TARGET_POOL,
            "target_rows": snapshot["target_rows"],
            "target_digest": snapshot["target_digest"],
            "reservations": snapshot["reservations"],
            "totals": snapshot["totals"],
            "non_target_invariants": snapshot["non_target_invariants"],
            "document_evidence": document_evidence,
            "fbs_status_read_model": {
                "facility_id": facility_id,
                "physical": moscow.get("physical"),
                "reserved": moscow.get("reserved"),
                "available": moscow.get("available"),
                "missing_physical_nm_ids": sorted(missing),
                "target_nm_ids_missing": sorted(set(missing) & set(TARGET_NM_IDS)),
                "target_nm_ids_unblocked": not bool(set(missing) & set(TARGET_NM_IDS)),
                "calculation_enabled": not selected_blockers,
                "selected_facility_blockers": selected_blockers,
                "facility_readiness": facility_readiness,
                "other_active_facility_blockers": [
                    {
                        "facility_id": item["facility_id"],
                        "name": item["name"],
                        "blockers": item["blockers"],
                    }
                    for item in facility_readiness
                    if item["facility_id"] != facility_id and item["blockers"]
                ],
            },
            "blockers": selected_blockers,
            "business_mutation_scope": {
                "balance_rows_only": list(TARGET_NM_IDS),
                "movement_line_count": 0,
                "wb_writes": 0,
                "supplier_shipment_writes": 0,
                "calculation_writes": 0,
                "factory_order_writes": 0,
            },
        }


def _read_snapshot(db_path: Path) -> dict[str, Any]:
    with closing(_open_query_only(db_path)) as conn:
        tables = _table_names(conn)
        required = {
            BALANCES_TABLE,
            FACILITIES_TABLE,
            FACILITY_PROFILES_TABLE,
            FEATURE_EPOCHS_TABLE,
            MANIFESTS_TABLE,
            CURRENT_TABLE,
            LINES_TABLE,
            OPERATIONS_TABLE,
            REQUESTS_TABLE,
            DOCUMENTS_TABLE,
            DOCUMENT_LINES_TABLE,
            "registry_upload_current_state",
            "registry_upload_config_v2",
            "sheet_vitrina_v1_nomenclature_items",
        }
        missing_tables = sorted(required - tables)
        if missing_tables:
            return {
                "facility": {},
                "feature_epoch": {},
                "cutover": {},
                "active_nm_ids": [],
                "target_nomenclature": [],
                "target_rows": [],
                "target_digest": _fingerprint([]),
                "reservations": [],
                "totals": {},
                "non_target_invariants": {},
                "blockers": ["missing required tables: " + ", ".join(missing_tables)],
            }
        facility_rows = conn.execute(
            f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                       facility.display_timezone,profile.city
                FROM {FACILITIES_TABLE} facility
                LEFT JOIN {FACILITY_PROFILES_TABLE} profile
                  ON profile.facility_id=facility.facility_id
                WHERE facility.facility_id=? ORDER BY facility.facility_id""",
            (TARGET_FACILITY_ID,),
        ).fetchall()
        facility = dict(facility_rows[0]) if len(facility_rows) == 1 else {}
        feature_row = conn.execute(
            f"SELECT epoch,writer_enabled,reader_enabled,source_revision,created_at "
            f"FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
        ).fetchone()
        cutover_row = conn.execute(
            f"SELECT cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,"
            f"feature_epoch,opening_document_id FROM {MANIFESTS_TABLE} "
            "ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"
        ).fetchone()
        feature = dict(feature_row) if feature_row is not None else {}
        cutover = dict(cutover_row) if cutover_row is not None else {}
        active_nm_ids = [
            int(row[0])
            for row in conn.execute(
                """SELECT config.nm_id
                   FROM registry_upload_current_state current
                   JOIN registry_upload_config_v2 config
                     ON config.bundle_version=current.bundle_version
                   WHERE current.slot=1 AND config.enabled=1
                   ORDER BY config.display_order,config.nm_id"""
            ).fetchall()
        ]
        target_rows: list[dict[str, Any]] = []
        facility_id = str(facility.get("facility_id") or "")
        for nm_id in TARGET_NM_IDS:
            row = conn.execute(
                f"SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,"
                f"wac_rub,source_watermark,updated_at FROM {BALANCES_TABLE} "
                "WHERE facility_id=? AND pool=? AND nm_id=?",
                (facility_id, TARGET_POOL, nm_id),
            ).fetchone()
            target_rows.append(
                {
                    "nm_id": nm_id,
                    "absolute_target": ABSOLUTE_TARGET,
                    "state": "missing" if row is None else "explicit_zero" if _is_zero_balance(row) else "conflict",
                    "row": dict(row) if row is not None else None,
                }
            )
        cutover_id = str(cutover.get("cutover_id") or "")
        reservations = [
            dict(row)
            for row in conn.execute(
                f"""SELECT cutover_id,order_id,state,episode_sequence,source_revision,
                           status_digest,facility_id,pool,nm_id,quantity,debit_event_id,updated_at
                    FROM {CURRENT_TABLE}
                    WHERE cutover_id=? AND facility_id=? AND pool=?
                      AND nm_id IN ({','.join('?' for _ in TARGET_NM_IDS)})
                    ORDER BY order_id""",
                (cutover_id, facility_id, TARGET_POOL, *TARGET_NM_IDS),
            ).fetchall()
        ]
        reserved_by_nm_id = {
            nm_id: sum(
                int(item["quantity"])
                for item in reservations
                if int(item["nm_id"]) == nm_id and item["state"] == "reserved"
            )
            for nm_id in TARGET_NM_IDS
        }
        for item in target_rows:
            physical = (
                None if item["row"] is None else int(item["row"]["quantity"])
            )
            reserved = int(reserved_by_nm_id[int(item["nm_id"])])
            item["physical"] = physical
            item["reserved"] = reserved
            item["available"] = None if physical is None else physical - reserved
        total_rows = conn.execute(
            f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool=? ORDER BY nm_id",
            (facility_id, TARGET_POOL),
        ).fetchall()
        physical_total = sum(int(item["quantity"]) for item in total_rows)
        with localcontext() as context:
            context.prec = 160
            capital_total = sum(
                (Decimal(str(item["capital_rub"])) for item in total_rows),
                Decimal("0"),
            )
        blockers: list[str] = []
        if len(facility_rows) != 1:
            blockers.append("canonical FF Москва identity is missing or ambiguous")
        elif (
            str(facility.get("facility_id") or "") != TARGET_FACILITY_ID
            or str(facility.get("name") or "") != TARGET_FACILITY_NAME
            or not bool(facility.get("active"))
            or str(facility.get("city") or "") != TARGET_FACILITY_CITY
        ):
            blockers.append("canonical FF Москва is inactive or has unexpected city identity")
        if not feature or not cutover:
            blockers.append("active facility-pool epoch/cutover is unavailable")
        elif (
            not bool(feature.get("writer_enabled"))
            or not bool(feature.get("reader_enabled"))
            or int(feature.get("epoch") or 0) != int(cutover.get("feature_epoch") or -1)
        ):
            blockers.append("facility-pool writer/reader epoch is not current")
        nomenclature_rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT item_id,nm_id,barcode,barcodes_json,vendor_code,
                           nomenclature_name,match_key,updated_at
                    FROM sheet_vitrina_v1_nomenclature_items
                    WHERE is_active=1 AND is_hidden=0
                      AND nm_id IN ({','.join('?' for _ in TARGET_NM_IDS)})
                    ORDER BY nm_id,item_id""",
                TARGET_NM_IDS,
            ).fetchall()
        ]
        for nm_id in TARGET_NM_IDS:
            count = sum(
                1 for item in nomenclature_rows if int(item["nm_id"]) == nm_id
            )
            if count != 1:
                blockers.append(
                    f"target SKU {nm_id} lacks one exact active nomenclature identity"
                )
        conflicts = [item["nm_id"] for item in target_rows if item["state"] == "conflict"]
        if conflicts:
            blockers.append(
                "existing target physical rows are not exact zero and cannot be overwritten: "
                + ", ".join(str(item) for item in conflicts)
            )
        target_keys = {(facility_id, TARGET_POOL, nm_id) for nm_id in TARGET_NM_IDS}
        non_target_balance_rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,"
                f"wac_rub,source_watermark,updated_at FROM {BALANCES_TABLE} "
                "ORDER BY facility_id,pool,nm_id"
            ).fetchall()
            if (str(row["facility_id"]), str(row["pool"]), int(row["nm_id"]))
            not in target_keys
        ]
        non_target = {
            "balance_rows_digest": _fingerprint(non_target_balance_rows),
            "balance_row_count": len(non_target_balance_rows),
            "movement_lines_digest": _query_digest(
                conn, f"SELECT * FROM {LINES_TABLE} ORDER BY operation_id,line_no"
            ),
            "movement_line_count": _count(conn, LINES_TABLE),
            "reservation_current_digest": _query_digest(
                conn, f"SELECT * FROM {CURRENT_TABLE} ORDER BY cutover_id,order_id"
            ),
            "reservation_current_count": _count(conn, CURRENT_TABLE),
            "facility_registry_digest": _query_digest(
                conn, f"SELECT * FROM {FACILITIES_TABLE} ORDER BY facility_id"
            ),
            "feature_epoch_digest": _query_digest(
                conn, f"SELECT * FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch"
            ),
            "cutover_manifest_digest": _query_digest(
                conn, f"SELECT * FROM {MANIFESTS_TABLE} ORDER BY cutover_id"
            ),
            "supplier_shipments_digest": _optional_query_digest(
                conn,
                "sheet_vitrina_v1_supplier_shipments",
                "SELECT * FROM sheet_vitrina_v1_supplier_shipments ORDER BY shipment_id",
            ),
            "aggregate_ff_digest": _aggregate_ff_digest(conn, tables),
        }
        return {
            "facility": facility,
            "feature_epoch": feature,
            "cutover": cutover,
            "active_nm_ids": active_nm_ids,
            "target_nomenclature": nomenclature_rows,
            "target_rows": target_rows,
            "target_digest": _fingerprint(target_rows),
            "reservations": reservations,
            "reservations_digest": _fingerprint(reservations),
            "totals": {
                "physical": physical_total,
                "capital_rub": format(capital_total, "f"),
                "row_count": len(total_rows),
                "reserved": sum(reserved_by_nm_id.values()),
            },
            "non_target_invariants": non_target,
            "blockers": blockers,
        }


def _validate_reviewed_plan(
    reviewed_plan: Mapping[str, Any],
    *,
    fingerprint: str,
    deployed_sha: str,
    approval_reference: str,
    actor: str,
) -> None:
    if (
        reviewed_plan.get("contract_name") != CONTRACT_NAME
        or int(reviewed_plan.get("contract_version") or 0) != CONTRACT_VERSION
        or reviewed_plan.get("mode") != "dry_run"
        or reviewed_plan.get("apply_allowed") is not True
        or str(reviewed_plan.get("deployed_sha") or "") != deployed_sha
        or str(reviewed_plan.get("fingerprint") or "") != fingerprint
        or str((reviewed_plan.get("scope") or {}).get("facility_id") or "")
        != TARGET_FACILITY_ID
        or str((reviewed_plan.get("scope") or {}).get("facility_name") or "")
        != TARGET_FACILITY_NAME
        or str((reviewed_plan.get("scope") or {}).get("pool") or "") != TARGET_POOL
        or list((reviewed_plan.get("scope") or {}).get("nm_ids") or [])
        != list(TARGET_NM_IDS)
        or (reviewed_plan.get("scope") or {}).get("absolute_physical_target")
        != ABSOLUTE_TARGET
    ):
        raise FfPoolZeroPhysicalProductionError(
            "reviewed plan does not match the exact Moscow FBS zero cohort"
        )
    expected = _fingerprint(
        {
            key: value
            for key, value in reviewed_plan.items()
            if key not in {"fingerprint", "generated_at"}
        }
    )
    if expected != fingerprint:
        raise FfPoolZeroPhysicalProductionError(
            "reviewed plan fingerprint is invalid"
        )
    if not str(approval_reference).strip() or not str(actor).strip():
        raise FfPoolZeroPhysicalProductionError(
            "apply requires an exact approval reference and actor"
        )


def _document_evidence(db_path: Path) -> dict[str, Any]:
    with closing(_open_query_only(db_path)) as conn:
        request = conn.execute(
            f"""SELECT request_id,state,document_kind,source_system,source_type,
                       source_id,source_revision,actor,preview_manifest_json,posted_document_id,
                       posted_manifest_sha256,recovery_operation_id
                FROM {REQUESTS_TABLE}
                WHERE source_system='owner_business_decision'
                  AND source_type=? AND source_id=?
                ORDER BY CASE WHEN state='complete' THEN 0 ELSE 1 END,
                         accepted_at DESC,request_id DESC
                LIMIT 1""",
            (SOURCE_TYPE, SOURCE_ID),
        ).fetchone()
        if request is None:
            return {"state": "missing"}
        document_id = str(request["posted_document_id"] or "")
        document = (
            conn.execute(
                f"""SELECT document_id,document_kind,root_document_id,operation_id,
                           posted_manifest_sha256,posted_at
                    FROM {DOCUMENTS_TABLE} WHERE document_id=?""",
                (document_id,),
            ).fetchone()
            if document_id
            else None
        )
        lines = [
            dict(row)
            for row in conn.execute(
                f"""SELECT line_no,line_role,facility_id,pool,nm_id,quantity,
                           capital_rub,expense_rub,metadata_json
                    FROM {DOCUMENT_LINES_TABLE} WHERE document_id=?
                    ORDER BY line_no""",
                (document_id,),
            ).fetchall()
        ]
        operation_id = str(document["operation_id"] or "") if document is not None else ""
        movement_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {LINES_TABLE} WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0]
        )
        preview_manifest = _read_json_text_object(request["preview_manifest_json"])
        authorization = dict(preview_manifest.get("production_authorization") or {})
        return {
            "state": str(request["state"]),
            "request_id": str(request["request_id"]),
            "source_system": str(request["source_system"]),
            "source_type": str(request["source_type"]),
            "source_id": str(request["source_id"]),
            "source_revision": str(request["source_revision"]),
            "actor": str(request["actor"]),
            "approval_reference": str(
                authorization.get("approval_reference") or ""
            ),
            "production_authorization": authorization,
            "document_kind": str(request["document_kind"]),
            "document": dict(document) if document is not None else None,
            "lines": lines,
            "movement_line_count": movement_count,
            "posted_manifest_sha256": str(request["posted_manifest_sha256"]),
            "recovery_operation_id": str(request["recovery_operation_id"]),
        }


def _verify_completed_readback(
    readback: Mapping[str, Any],
    *,
    require_document: bool,
    expected_fingerprint: str = "",
    expected_approval_reference: str = "",
    expected_actor: str = "",
) -> None:
    rows = list(readback.get("target_rows") or [])
    if [int(item.get("nm_id") or 0) for item in rows] != list(TARGET_NM_IDS):
        raise FfPoolZeroPhysicalProductionError("target readback cohort drifted")
    if any(item.get("state") != "explicit_zero" for item in rows):
        raise FfPoolZeroPhysicalProductionError(
            "one or more target rows are not explicit physical zero"
        )
    if not bool(
        (readback.get("fbs_status_read_model") or {}).get("target_nm_ids_unblocked")
    ):
        raise FfPoolZeroPhysicalProductionError(
            "FBS readiness still reports a target physical row as missing"
        )
    document = dict(readback.get("document_evidence") or {})
    if require_document and (
        document.get("state") != "complete"
        or document.get("source_system") != "owner_business_decision"
        or document.get("source_type") != SOURCE_TYPE
        or document.get("source_id") != SOURCE_ID
        or (
            bool(expected_fingerprint)
            and document.get("source_revision") != expected_fingerprint
        )
        or (
            bool(expected_approval_reference)
            and document.get("approval_reference") != expected_approval_reference
        )
        or (bool(expected_actor) and document.get("actor") != expected_actor)
        or document.get("document_kind") != "pool_inventory"
        or [int(item.get("nm_id") or 0) for item in document.get("lines") or []]
        != list(TARGET_NM_IDS)
        or any(
            item.get("line_role") != "absolute_target"
            or item.get("facility_id")
            != str((readback.get("facility") or {}).get("facility_id") or "")
            or item.get("pool") != TARGET_POOL
            or int(item.get("quantity") or 0) != 0
            or Decimal(str(item.get("capital_rub") or "0")) != Decimal("0")
            for item in document.get("lines") or []
        )
        or int(document.get("movement_line_count") or 0) != 0
    ):
        raise FfPoolZeroPhysicalProductionError(
            "immutable confirmed-zero document readback is incomplete"
        )


def _verify_non_target_invariants(
    *, before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    if dict(before) != dict(after):
        changed = sorted(
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        )
        raise FfPoolZeroPhysicalProductionError(
            "non-target invariant changed during apply: " + ", ".join(changed)
        )


def _is_zero_balance(row: Mapping[str, Any]) -> bool:
    try:
        capital = Decimal(str(row["capital_rub"]))
        return (
            int(row["quantity"]) == 0
            and capital.is_finite()
            and capital == Decimal("0")
            and row["wac_rub"] is None
        )
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False


def _aggregate_ff_digest(conn: sqlite3.Connection, tables: set[str]) -> str:
    required = {
        "sheet_vitrina_v1_warehouse_functional_active",
        "sheet_vitrina_v1_warehouse_functional_balances",
    }
    if not required.issubset(tables):
        return "unavailable"
    return _query_digest(
        conn,
        """SELECT active.slot,active.version_id,balance.warehouse_key,balance.nm_id,
                  balance.quantity,balance.wac_rub,balance.capital_rub,
                  balance.cost_covered_quantity,balance.quality,balance.certified
           FROM sheet_vitrina_v1_warehouse_functional_active active
           LEFT JOIN sheet_vitrina_v1_warehouse_functional_balances balance
             ON balance.version_id=active.version_id AND balance.warehouse_key='ff'
           WHERE active.slot=1 ORDER BY balance.nm_id""",
    )


def _optional_query_digest(
    conn: sqlite3.Connection, table: str, query: str
) -> str:
    if table not in _table_names(conn):
        return "unavailable"
    return _query_digest(conn, query)


def _query_digest(conn: sqlite3.Connection, query: str) -> str:
    return _fingerprint([dict(row) for row in conn.execute(query).fetchall()])


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _open_query_only(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise FfPoolZeroPhysicalProductionError(
            "canonical runtime SQLite store is missing"
        )
    conn = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _fingerprint(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _request_id_for_fingerprint(fingerprint: str) -> str:
    suffix = str(fingerprint).removeprefix("sha256:")[:16]
    if not re.fullmatch(r"[0-9a-f]{16}", suffix):
        raise FfPoolZeroPhysicalProductionError(
            "reviewed manifest fingerprint cannot form an immutable request id"
        )
    return f"{REQUEST_ID}-{suffix}"


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    raise TypeError(f"unsupported evidence value: {type(value).__name__}")


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FfPoolZeroPhysicalProductionError(
            "existing production evidence is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise FfPoolZeroPhysicalProductionError(
            "existing production evidence must be a JSON object"
        )
    return payload


def _read_json_text_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise FfPoolZeroPhysicalProductionError(
            "stored confirmed-zero preview manifest is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise FfPoolZeroPhysicalProductionError(
            "stored confirmed-zero preview manifest must be a JSON object"
        )
    return payload


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


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
