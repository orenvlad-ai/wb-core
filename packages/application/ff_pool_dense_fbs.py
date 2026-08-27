"""Durable staged activation for applicability-gated dense FBS physical rows."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import errno
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence

from packages.application.ff_pool_documents import (
    DOCUMENT_LINES_TABLE,
    LINES_TABLE,
    REQUESTS_TABLE,
    FfPoolDocumentService,
)
from packages.application.ff_pool_fbs_applicability import (
    APPLICABILITY_EVENTS_TABLE,
    DENSE_INTENT_EVENTS_TABLE,
    DENSE_INTENTS_TABLE,
    FBS_CURRENT_TABLE,
    append_applicability_event,
    append_dense_intent_event,
    coverage_receipt,
    current_business_date,
    dense_intent_state,
    ensure_ff_pool_fbs_applicability_schema,
    fbs_pair_applicability,
    fbs_physical_component,
    persist_dense_intent,
    stock_managed_nomenclature,
)
from packages.business_time import business_date_from_timestamp
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_CHANGES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)
from packages.application.business_data_write_barrier import barrier_status
from packages.contracts.ff_pool_documents import DocumentIdentity


CONTRACT_NAME = "ff_pool_dense_fbs_initialization_v1"
SOURCE_SYSTEM = "wb_core_dense_fbs"
ZERO_REPAIR_MANIFEST_SCHEMA = "ff_pool_dense_fbs_zero_repair_manifest_v2"
ZERO_REPAIR_PLAN_SCHEMA = "ff_pool_dense_fbs_zero_repair_plan_v2"
ZERO_REPAIR_OPERATION_NAMESPACE = "wbc0013:dense-fbs-zero-repair:v2"
SOURCE_TYPE = CONTRACT_NAME
NOMENCLATURE_TABLE = "sheet_vitrina_v1_nomenclature_items"


class DenseFbsError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


class DenseFbsResumableError(DenseFbsError):
    """Canonical request evidence proves that exact-id resume is safe."""


class DenseFbsService:
    """Plan, materialize through pool_inventory, read back, then publish active."""

    def __init__(
        self,
        *,
        db_path: Path,
        runtime_dir: Path,
        timestamp_factory: Any | None = None,
        document_service_factory: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.runtime_dir = Path(runtime_dir)
        self.timestamp_factory = timestamp_factory or _utc_now
        self.document_service_factory = document_service_factory

    def activate_facility(
        self,
        *,
        facility_id: str,
        expected_updated_at: str,
        request_id: str,
        request_identity: str,
        actor: str,
    ) -> dict[str, Any]:
        orchestration_key = f"facility:{request_id}:dense-fbs"
        with warehouse_functional_write_lock(self.runtime_dir):
            intent = self._load_or_plan_facility_intent(
                orchestration_key=orchestration_key,
                facility_id=str(facility_id),
                expected_updated_at=str(expected_updated_at),
                request_identity=str(request_identity),
                actor=str(actor),
            )
            materialized = self._materialize(intent)
            now = self._now()
            with self._write() as conn:
                ensure_ff_pool_fbs_applicability_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT * FROM {FACILITIES_TABLE} WHERE facility_id=?",
                    (str(facility_id),),
                ).fetchone()
                intent_already_active = (
                    dense_intent_state(conn, str(intent["intent_id"]))["state"]
                    == "active"
                )
                if row is None:
                    if intent_already_active:
                        conn.rollback()
                        raise DenseFbsError(
                            "facility_activation_request_already_terminal",
                            "Previously activated facility was later removed",
                        )
                    raise self._terminal_publication_error(
                        conn,
                        intent,
                        DenseFbsError(
                            "facility_not_found", "Staged facility disappeared"
                        ),
                    )
                if bool(row["active"]):
                    if not intent_already_active:
                        raise self._terminal_publication_error(
                            conn,
                            intent,
                            DenseFbsError(
                                "facility_activation_cas_drift",
                                "Facility became active outside its dense FBS publication",
                            ),
                        )
                    conn.rollback()
                    return {
                        "contract_name": CONTRACT_NAME,
                        "intent_id": intent["intent_id"],
                        "state": "active",
                        "coverage": materialized,
                        "idempotent": True,
                    }
                if intent_already_active:
                    conn.rollback()
                    raise DenseFbsError(
                        "facility_activation_request_already_terminal",
                        "Previously activated facility was later changed; use a new activation request",
                    )
                if str(row["updated_at"]) != str(expected_updated_at):
                    raise self._terminal_publication_error(
                        conn,
                        intent,
                        DenseFbsError(
                            "facility_activation_cas_drift",
                            "Facility changed after dense FBS activation was staged",
                            details={"current_updated_at": str(row["updated_at"])},
                        ),
                    )
                try:
                    self._verify_materialized_under_transaction(conn, intent)
                except DenseFbsError as exc:
                    raise self._terminal_publication_error(conn, intent, exc) from exc
                profile = conn.execute(
                    f"SELECT city FROM {FACILITY_PROFILES_TABLE} WHERE facility_id=?",
                    (str(facility_id),),
                ).fetchone()
                previous = {
                    "facility_id": str(row["facility_id"]),
                    "code": str(row["code"]),
                    "name": str(row["name"]),
                    "city": str(profile[0] or "") if profile is not None else "",
                    "active": False,
                    "display_timezone": str(row["display_timezone"]),
                }
                current = {**previous, "active": True}
                changed = conn.execute(
                    f"UPDATE {FACILITIES_TABLE} SET active=1,updated_at=? "
                    "WHERE facility_id=? AND active=0 AND updated_at=?",
                    (now, str(facility_id), str(expected_updated_at)),
                ).rowcount
                if changed != 1:
                    raise self._terminal_publication_error(
                        conn,
                        intent,
                        DenseFbsError(
                            "facility_activation_cas_drift",
                            "Facility activation CAS did not match exactly one staged row",
                        ),
                    )
                change_id = (
                    "fffc_"
                    + _fingerprint(
                        {
                            "request_id": request_id,
                            "action": "activated",
                            "facility_id": facility_id,
                        }
                    ).removeprefix("sha256:")[:28]
                )
                conn.execute(
                    f"""INSERT OR IGNORE INTO {FACILITY_CHANGES_TABLE}(
                           change_id,request_id,request_identity,facility_id,action,actor,
                           previous_json,current_json,changed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        change_id,
                        str(request_id),
                        str(request_identity),
                        str(facility_id),
                        "activated",
                        str(actor),
                        _json(previous),
                        _json(current),
                        now,
                    ),
                )
                append_dense_intent_event(
                    conn,
                    intent_id=str(intent["intent_id"]),
                    state="active",
                    receipt={
                        "coverage_fingerprint": str(materialized["fingerprint"]),
                        "facility_id": str(facility_id),
                        "activated_at": now,
                    },
                    recorded_at=now,
                )
                conn.commit()
            return {
                "contract_name": CONTRACT_NAME,
                "intent_id": intent["intent_id"],
                "state": "active",
                "coverage": materialized,
                "idempotent": False,
            }

    def activate_staged_skus(
        self,
        *,
        staged_items: Sequence[Mapping[str, Any]],
        orchestration_key: str,
        request_identity: str,
        actor: str,
    ) -> dict[str, Any]:
        normalized = sorted(
            (
                {
                    "item_id": str(item["item_id"]),
                    "nm_id": int(item["nm_id"]),
                    "updated_at": str(item["updated_at"]),
                }
                for item in staged_items
            ),
            key=lambda item: (item["nm_id"], item["item_id"]),
        )
        if not normalized:
            return {
                "contract_name": CONTRACT_NAME,
                "state": "active",
                "idempotent": True,
            }
        if len({item["nm_id"] for item in normalized}) != len(normalized):
            raise DenseFbsError(
                "staged_nomenclature_ambiguous",
                "One dense FBS activation batch cannot publish duplicate nmId identities",
            )
        with warehouse_functional_write_lock(self.runtime_dir):
            intent = self._load_or_plan_sku_intent(
                orchestration_key=str(orchestration_key),
                staged_items=normalized,
                request_identity=str(request_identity),
                actor=str(actor),
            )
            materialized = self._materialize(intent)
            now = self._now()
            with self._write() as conn:
                ensure_ff_pool_fbs_applicability_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._verify_materialized_under_transaction(conn, intent)
                except DenseFbsError as exc:
                    raise self._terminal_publication_error(conn, intent, exc) from exc
                intent_already_active = (
                    dense_intent_state(conn, str(intent["intent_id"]))["state"]
                    == "active"
                )
                for item in normalized:
                    row = conn.execute(
                        f"SELECT is_active,is_hidden,nm_id,updated_at FROM {NOMENCLATURE_TABLE} "
                        "WHERE item_id=?",
                        (item["item_id"],),
                    ).fetchone()
                    if row is None:
                        if intent_already_active:
                            conn.rollback()
                            raise DenseFbsError(
                                "sku_activation_request_already_terminal",
                                "Previously activated nomenclature was later removed",
                            )
                        raise self._terminal_publication_error(
                            conn,
                            intent,
                            DenseFbsError(
                                "staged_nomenclature_missing",
                                f"Staged nomenclature item disappeared: {item['item_id']}",
                            ),
                        )
                    if (
                        bool(row[0])
                        and not bool(row[1])
                        and int(row[2]) == item["nm_id"]
                        and str(row[3]) == item["updated_at"]
                    ):
                        if intent_already_active:
                            continue
                        raise self._terminal_publication_error(
                            conn,
                            intent,
                            DenseFbsError(
                                "sku_activation_cas_drift",
                                "Staged nomenclature became active outside dense publication",
                                details={"item_id": item["item_id"]},
                            ),
                        )
                    if intent_already_active:
                        conn.rollback()
                        raise DenseFbsError(
                            "sku_activation_request_already_terminal",
                            "Previously activated nomenclature was later changed; use a new activation request",
                            details={"item_id": item["item_id"]},
                        )
                    if (
                        bool(row[0])
                        or bool(row[1])
                        or int(row[2] or 0) != item["nm_id"]
                        or str(row[3]) != item["updated_at"]
                    ):
                        raise self._terminal_publication_error(
                            conn,
                            intent,
                            DenseFbsError(
                                "sku_activation_cas_drift",
                                "Staged nomenclature changed before dense coverage completed",
                                details={"item_id": item["item_id"]},
                            ),
                        )
                if intent_already_active:
                    conn.rollback()
                    return {
                        "contract_name": CONTRACT_NAME,
                        "intent_id": intent["intent_id"],
                        "state": "active",
                        "coverage": materialized,
                        "idempotent": True,
                    }
                for item in normalized:
                    row = conn.execute(
                        f"SELECT is_active FROM {NOMENCLATURE_TABLE} WHERE item_id=?",
                        (item["item_id"],),
                    ).fetchone()
                    if row is not None and bool(row[0]):
                        continue
                    changed = conn.execute(
                        f"UPDATE {NOMENCLATURE_TABLE} SET is_active=1 "
                        "WHERE item_id=? AND is_active=0 AND is_hidden=0 AND nm_id=? AND updated_at=?",
                        (item["item_id"], item["nm_id"], item["updated_at"]),
                    ).rowcount
                    if changed != 1:
                        raise self._terminal_publication_error(
                            conn,
                            intent,
                            DenseFbsError(
                                "sku_activation_cas_drift",
                                "SKU activation CAS did not match exactly one staged row",
                            ),
                        )
                append_dense_intent_event(
                    conn,
                    intent_id=str(intent["intent_id"]),
                    state="active",
                    receipt={
                        "coverage_fingerprint": str(materialized["fingerprint"]),
                        "nm_ids": [item["nm_id"] for item in normalized],
                        "activated_at": now,
                    },
                    recorded_at=now,
                )
                conn.commit()
            return {
                "contract_name": CONTRACT_NAME,
                "intent_id": intent["intent_id"],
                "state": "active",
                "coverage": materialized,
                "idempotent": False,
            }

    def record_applicability(
        self,
        *,
        facility_id: str,
        nm_id: int,
        state: str,
        effective_from: str,
        reason: str,
        provenance: Mapping[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        with warehouse_functional_write_lock(self.runtime_dir):
            now = self._now()
            with self._write() as conn:
                ensure_ff_pool_fbs_applicability_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                event = append_applicability_event(
                    conn,
                    facility_id=str(facility_id),
                    nm_id=int(nm_id),
                    state=str(state),
                    effective_from=str(effective_from),
                    reason=str(reason),
                    provenance=dict(provenance),
                    actor=str(actor),
                    recorded_at=now,
                )
                conn.commit()
            return event

    def build_zero_repair_plan(
        self,
        *,
        facility_id: str,
        historical_exact_zero_nm_ids: Sequence[int],
        default_applicable_absent_history_nm_ids: Sequence[int],
        seller_warehouse_id: int,
        official_office_id: int,
        expected_roster_nm_ids: Sequence[int],
        expected_existing_nm_ids: Sequence[int],
        historical_business_date: str,
        canonical_target: Mapping[str, Any],
        storage_generation: Mapping[str, Any],
        qualified_at: str = "",
    ) -> dict[str, Any]:
        """Query-only deterministic plan using the same dense pool_inventory shape."""

        historical_nm_ids = _strict_positive_partition(
            historical_exact_zero_nm_ids,
            name="historical_exact_zero",
        )
        absent_history_nm_ids = _strict_positive_partition(
            default_applicable_absent_history_nm_ids,
            name="default_applicable_absent_history",
        )
        overlap = sorted(set(historical_nm_ids) & set(absent_history_nm_ids))
        if overlap:
            raise DenseFbsError(
                "repair_manifest_partition_overlap",
                "Dense FBS repair partitions must be disjoint",
                details={"overlap_nm_ids": overlap},
            )
        selected_nm_ids = sorted((*historical_nm_ids, *absent_history_nm_ids))
        if not selected_nm_ids:
            raise DenseFbsError(
                "repair_scope_invalid",
                "Repair nmId scope must be non-empty and positive",
            )
        expected_roster = _strict_positive_partition(
            expected_roster_nm_ids,
            name="expected_roster_nm_ids",
        )
        expected_existing = _strict_positive_partition(
            expected_existing_nm_ids,
            name="expected_existing_nm_ids",
        )
        expected_partition = sorted((*selected_nm_ids, *expected_existing))
        if expected_partition != expected_roster:
            raise DenseFbsError(
                "repair_manifest_roster_partition_invalid",
                "Expected roster must equal the exact target and existing-row union",
                details={
                    "expected_roster_nm_ids": expected_roster,
                    "partition_union_nm_ids": expected_partition,
                },
            )
        with self._read() as conn:
            ensure_tables = _tables(conn)
            if (
                not {
                    FACILITIES_TABLE,
                    BALANCES_TABLE,
                    FEATURE_EPOCHS_TABLE,
                    NOMENCLATURE_TABLE,
                }
                <= ensure_tables
            ):
                raise DenseFbsError(
                    "repair_schema_unavailable",
                    "Dense FBS repair schema is unavailable",
                )
            conn.execute("PRAGMA query_only=ON")
            facility = conn.execute(
                f"SELECT facility_id,code,name,active,display_timezone,updated_at "
                f"FROM {FACILITIES_TABLE} WHERE facility_id=?",
                (str(facility_id),),
            ).fetchone()
            epoch = _writer_epoch(conn)
            blockers: list[str] = []
            exact_target = dict(canonical_target)
            exact_storage = dict(storage_generation)
            if not bool(exact_target.get("accepted")):
                blockers.append(
                    "explicit canonical hosted-runtime target was not accepted"
                )
            if bool(exact_storage.get("implicit")) or not bool(
                exact_storage.get("query_only")
            ):
                blockers.append(
                    "operational StoreRegistry generation is implicit or not query-only"
                )
            if facility is None or not bool(facility["active"]):
                blockers.append("exact target facility is missing or inactive")
            roster = stock_managed_nomenclature(conn)
            roster_nm_ids = [int(item["nm_id"]) for item in roster]
            if roster_nm_ids != expected_roster:
                blockers.append(
                    "stock-managed roster identities drifted from the manifest"
                )
            missing_target_identities = sorted(
                set(selected_nm_ids) - set(roster_nm_ids)
            )
            if missing_target_identities:
                blockers.append(
                    "target SKU lacks one exact active nomenclature identity: "
                    + ", ".join(map(str, missing_target_identities))
                )
            target_applicability = [
                {
                    "nm_id": nm_id,
                    **fbs_pair_applicability(
                        conn,
                        facility_id=str(facility_id),
                        nm_id=nm_id,
                        as_of_date=current_business_date(),
                        facility_active=bool(facility and facility["active"]),
                        sku_active=nm_id in set(roster_nm_ids),
                    ),
                }
                for nm_id in selected_nm_ids
            ]
            inapplicable_targets = [
                int(item["nm_id"])
                for item in target_applicability
                if not bool(item["applicable"])
            ]
            if inapplicable_targets:
                blockers.append(
                    "repair targets are not applicable at the canonical EKT business date: "
                    + ", ".join(map(str, inapplicable_targets))
                )
            target_rows = [
                _repair_balance_cas_row(
                    conn,
                    facility_id=str(facility_id),
                    nm_id=nm_id,
                )
                for nm_id in selected_nm_ids
            ]
            present_targets = [
                int(row["nm_id"]) for row in target_rows if bool(row["row_present"])
            ]
            if present_targets:
                blockers.append(
                    "repair targets must all still be physically missing: "
                    + ", ".join(map(str, present_targets))
                )
            conflicts = [
                int(row["nm_id"])
                for row in target_rows
                if bool(row["row_present"]) and not _canonical_zero_row(row)
            ]
            if conflicts:
                blockers.append(
                    "target physical rows are not canonical explicit zero: "
                    + ", ".join(map(str, conflicts))
                )
            placeholders = ",".join("?" for _ in selected_nm_ids)
            target_facility_non_target_count, target_facility_non_target_digest = (
                _streaming_query_digest(
                    conn,
                    f"""SELECT facility_id,pool,nm_id,projection_epoch,quantity,
                           capital_rub,wac_rub,source_watermark,updated_at
                      FROM {BALANCES_TABLE}
                     WHERE facility_id=? AND pool='FBS'
                       AND nm_id NOT IN ({placeholders})
                     ORDER BY nm_id""",
                    (str(facility_id), *selected_nm_ids),
                )
            )
            (
                target_facility_non_target_material_count,
                target_facility_non_target_material_digest,
            ) = _streaming_query_digest(
                conn,
                f"""SELECT facility_id,pool,nm_id,projection_epoch,quantity,
                           capital_rub,wac_rub,source_watermark
                      FROM {BALANCES_TABLE}
                     WHERE facility_id=? AND pool='FBS'
                       AND nm_id NOT IN ({placeholders})
                     ORDER BY nm_id""",
                (str(facility_id), *selected_nm_ids),
            )
            non_target_nm_ids = [
                int(row[0])
                for row in conn.execute(
                    f"""SELECT nm_id FROM {BALANCES_TABLE}
                         WHERE facility_id=? AND pool='FBS'
                           AND nm_id NOT IN ({placeholders})
                         ORDER BY nm_id""",
                    (str(facility_id), *selected_nm_ids),
                ).fetchall()
            ]
            if non_target_nm_ids != expected_existing:
                blockers.append(
                    "target facility existing identities drifted from the manifest"
                )
            exact_roster_partition = sorted(selected_nm_ids + non_target_nm_ids)
            if roster_nm_ids != exact_roster_partition:
                blockers.append(
                    "active stock-managed roster is not exactly targets plus current "
                    "target-facility FBS identities"
                )
            mapping_evidence = _exact_repair_mapping_evidence(
                conn,
                facility_id=str(facility_id),
                seller_warehouse_id=int(seller_warehouse_id),
                official_office_id=int(official_office_id),
                expected_allocation_nm_ids=non_target_nm_ids,
            )
            blockers.extend(mapping_evidence["blockers"])
            expected_existing_count = len(expected_existing)
            if (
                int(mapping_evidence.get("allocation_count") or 0)
                != expected_existing_count
            ):
                blockers.append(
                    "mapping-extension allocation count drifted: "
                    f"expected {expected_existing_count}, found "
                    f"{int(mapping_evidence.get('allocation_count') or 0)}"
                )
            if (
                list(mapping_evidence.get("allocation_nm_ids") or [])
                != non_target_nm_ids
            ):
                blockers.append(
                    "mapping-extension allocation identities do not exactly match "
                    "the current target-facility FBS non-target identities"
                )

            target_effects = _target_effect_evidence(
                conn,
                facility_id=str(facility_id),
                seller_warehouse_id=int(seller_warehouse_id),
                nm_ids=selected_nm_ids,
            )
            if int(target_effects["effect_row_count"]) != 0:
                blockers.append(
                    "repair targets already have FBS movement/document/lifecycle/"
                    "reservation/order effects"
                )
            history_evidence = _historical_zero_evidence(
                conn,
                facility_id=str(facility_id),
                nm_ids=historical_nm_ids,
                business_date=str(historical_business_date),
            )
            blockers.extend(history_evidence["blockers"])
            absent_history_evidence = _default_absent_history_evidence(
                conn,
                facility_id=str(facility_id),
                seller_warehouse_id=int(seller_warehouse_id),
                nm_ids=absent_history_nm_ids,
                as_of_date=current_business_date(),
            )
            blockers.extend(absent_history_evidence["blockers"])

            scoped_non_targets = _scoped_repair_non_targets(
                conn,
                facility_id=str(facility_id),
                roster_nm_ids=roster_nm_ids,
                target_nm_ids=selected_nm_ids,
                historical_business_date=str(historical_business_date),
                seller_warehouse_id=int(seller_warehouse_id),
            )
            operation_material = {
                "schema": ZERO_REPAIR_MANIFEST_SCHEMA,
                "namespace": ZERO_REPAIR_OPERATION_NAMESPACE,
                "facility_id": str(facility_id),
                "seller_warehouse_id": int(seller_warehouse_id),
                "official_office_id": int(official_office_id),
                "historical_business_date": str(historical_business_date),
                "partitions": {
                    "historical_exact_zero": historical_nm_ids,
                    "default_applicable_absent_history": absent_history_nm_ids,
                },
                "expected_roster_nm_ids": expected_roster,
                "expected_existing_nm_ids": expected_existing,
            }
            repair_operation_id = (
                ZERO_REPAIR_OPERATION_NAMESPACE
                + ":"
                + _fingerprint(operation_material)[7:39]
            )
            # Qualification/publication time is current operational evidence;
            # the immutable historical date is proof for only the 12-row
            # historical partition and must never be reused as a document date.
            repair_qualified_at = str(qualified_at).strip() or self._now()
            dense_manifest = _activation_plan(
                conn,
                subject_kind="repair",
                subject_id=repair_operation_id,
                facilities=[dict(facility)] if facility is not None else [],
                sku_roster=roster,
                assumed_active_facility_ids=[],
                assumed_active_nm_ids=[],
                materialize_nm_ids=selected_nm_ids,
                epoch=epoch,
                effective_from=current_business_date(repair_qualified_at),
                cutover_at=repair_qualified_at,
                expected_subject={
                    "facility_id": str(facility_id),
                    "seller_warehouse_id": int(seller_warehouse_id),
                    "official_office_id": int(official_office_id),
                    "target_nm_ids": selected_nm_ids,
                },
            )
            dense_manifest["zero_repair_receipt_boundary"] = {
                "facility_id": str(facility_id),
                "target_nm_ids": selected_nm_ids,
                "expected_non_target_nm_ids": non_target_nm_ids,
                "expected_non_target_row_count": target_facility_non_target_count,
                "expected_non_target_digest": target_facility_non_target_digest,
            }
            if len(dense_manifest.get("documents") or []) != 1:
                blockers.append(
                    "repair must resolve to one exact pool_inventory document"
                )
            plan_boundary = {
                "projection_epoch": int(epoch),
                "sqlite_schema_version": int(
                    conn.execute("PRAGMA schema_version").fetchone()[0]
                ),
                "mapping_fingerprint": str(mapping_evidence["fingerprint"]),
                "roster_fingerprint": _fingerprint(roster),
                "target_effects_fingerprint": str(target_effects["fingerprint"]),
                "historical_evidence_fingerprint": str(history_evidence["fingerprint"]),
                "absent_history_lifecycle_fingerprint": str(
                    absent_history_evidence["fingerprint"]
                ),
                "scoped_non_targets_fingerprint": str(
                    scoped_non_targets["fingerprint"]
                ),
                "canonical_target_fingerprint": _fingerprint(exact_target),
                "storage_generation_fingerprint": _fingerprint(exact_storage),
            }
            plan_boundary["fingerprint"] = _fingerprint(plan_boundary)
            plan = {
                "contract_name": ZERO_REPAIR_PLAN_SCHEMA,
                "mode": "dry_run",
                "facility": dict(facility) if facility is not None else {},
                "pool": "FBS",
                "projection_epoch": epoch,
                "canonical_target": exact_target,
                "storage_generation": exact_storage,
                "plan_boundary": plan_boundary,
                "mapping_evidence": mapping_evidence,
                "stock_managed_roster": {
                    "expected_count": len(expected_roster),
                    "expected_nm_ids": expected_roster,
                    "actual_count": len(roster_nm_ids),
                    "nm_ids": roster_nm_ids,
                    "fingerprint": _fingerprint(roster),
                    "exact_partition_proven": roster_nm_ids == exact_roster_partition,
                },
                "nm_ids": selected_nm_ids,
                "partitions": {
                    "historical_exact_zero": historical_nm_ids,
                    "default_applicable_absent_history": absent_history_nm_ids,
                    "historical_exact_zero_digest": _fingerprint(historical_nm_ids),
                    "default_applicable_absent_history_digest": _fingerprint(
                        absent_history_nm_ids
                    ),
                    "union_digest": _fingerprint(selected_nm_ids),
                },
                "targets": [
                    {"nm_id": nm_id, "target_fbs": 0} for nm_id in selected_nm_ids
                ],
                "dense_fbs_initialization": dense_manifest,
                "input_manifest": {
                    "schema": ZERO_REPAIR_MANIFEST_SCHEMA,
                    "namespace": ZERO_REPAIR_OPERATION_NAMESPACE,
                    "operation_id": repair_operation_id,
                    "qualified_at": repair_qualified_at,
                    "facility_id": str(facility_id),
                    "seller_warehouse_id": int(seller_warehouse_id),
                    "official_office_id": int(official_office_id),
                    "expected_roster_nm_ids": expected_roster,
                    "expected_existing_nm_ids": expected_existing,
                    "historical_business_date": str(historical_business_date),
                    "partitions": {
                        "historical_exact_zero": historical_nm_ids,
                        "default_applicable_absent_history": absent_history_nm_ids,
                    },
                },
                "target_rows": target_rows,
                "target_applicability": target_applicability,
                "target_effects": target_effects,
                "historical_zero_evidence": history_evidence,
                "default_absent_history_evidence": absent_history_evidence,
                "expected_effects": {
                    "balance_insert_count": sum(
                        not row["row_present"] for row in target_rows
                    ),
                    "balance_update_count": 0,
                    "quantity_delta": 0,
                    "capital_delta_rub": "0",
                    "wac_effect": None,
                    "movement_line_count": 0,
                    "pool_inventory_document_count": 1 if not blockers else 0,
                },
                "non_targets": {
                    "target_facility_existing_fbs_row_count": target_facility_non_target_count,
                    "target_facility_existing_fbs_nm_ids": non_target_nm_ids,
                    "target_facility_existing_fbs_digest": target_facility_non_target_digest,
                    "target_facility_existing_fbs_material_count": target_facility_non_target_material_count,
                    "target_facility_existing_fbs_material_digest": target_facility_non_target_material_digest,
                    **scoped_non_targets,
                },
                "storage": {
                    "whole_database_copy": False,
                    "query_only": True,
                    "bounded_target_row_count": len(selected_nm_ids),
                    "non_target_digest_fetch_chunk_rows": 512,
                    "non_target_rows_retained_in_memory": False,
                    "full_operational_table_scan_allowed": False,
                    "scope_bound_nm_id_count": len(roster_nm_ids),
                },
                "apply_allowed": not blockers,
                "blockers": blockers,
                "apply_entrypoint_exposed": True,
            }
            plan["material_qualification_digest"] = _zero_repair_material_digest(plan)
            plan["fingerprint"] = _fingerprint(plan)
            return plan

    def apply_zero_repair_plan(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        approval_reference: str,
        actor: str,
    ) -> dict[str, Any]:
        """Owner-gated, one-document zero insertion with exact-id reconciliation."""

        normalized = deepcopy(dict(plan))
        fingerprint = str(normalized.get("fingerprint") or "")
        if (
            not fingerprint
            or fingerprint != str(confirm_fingerprint)
            or fingerprint
            != _fingerprint(
                {
                    key: value
                    for key, value in normalized.items()
                    if key != "fingerprint"
                }
            )
            or not bool(normalized.get("apply_allowed"))
        ):
            raise DenseFbsError(
                "repair_plan_fingerprint_mismatch",
                "Exact apply-allowed zero repair plan fingerprint is required",
            )
        if not str(approval_reference).strip() or not str(actor).strip():
            raise DenseFbsError(
                "repair_owner_gate_missing",
                "Approval reference and actor are required for zero repair",
            )
        operation_id = str(normalized["input_manifest"]["operation_id"])
        with self._read() as conn:
            existing = (
                conn.execute(
                    f"SELECT * FROM {DENSE_INTENTS_TABLE} WHERE orchestration_key=?",
                    (operation_id,),
                ).fetchone()
                if DENSE_INTENTS_TABLE in _tables(conn)
                else None
            )
            if existing is not None:
                expected_identity = _fingerprint(
                    {
                        "plan_fingerprint": fingerprint,
                        "approval_reference": str(approval_reference),
                    }
                )
                intent = _intent_from_row(existing, expected_identity=expected_identity)
                if str(intent["actor"]) != str(actor):
                    raise DenseFbsError(
                        "repair_owner_gate_conflict",
                        "Zero repair resume actor differs from its durable intent",
                    )
                existing_state = dense_intent_state(conn, str(intent["intent_id"]))
                if existing_state.get("state") == "active":
                    materialized = conn.execute(
                        f"SELECT receipt_json FROM {DENSE_INTENT_EVENTS_TABLE} "
                        "WHERE intent_id=? AND state='materialized' "
                        "ORDER BY event_sequence DESC LIMIT 1",
                        (str(intent["intent_id"]),),
                    ).fetchone()
                    return {
                        "contract_name": CONTRACT_NAME,
                        "state": "active",
                        "intent_id": str(intent["intent_id"]),
                        "plan_fingerprint": fingerprint,
                        "coverage": (
                            json.loads(str(materialized[0]))
                            if materialized is not None
                            else {}
                        ),
                        "idempotent": True,
                    }
        dense_plan = dict(normalized.get("dense_fbs_initialization") or {})
        if (
            dense_plan.get("subject_kind") != "repair"
            or len(dense_plan.get("documents") or []) != 1
            or any(
                int(target.get("target_fbs") or 0) != 0
                for target in dense_plan["documents"][0].get("targets") or []
            )
        ):
            raise DenseFbsError(
                "repair_document_scope_invalid",
                "Zero repair must contain one exact all-zero pool_inventory document",
            )
        before_barrier = barrier_status(self.runtime_dir)
        if before_barrier.get("active") is not False:
            raise DenseFbsError(
                "repair_write_barrier_active",
                "Dense FBS repair requires one inactive read-only barrier readback",
            )
        self._revalidate_zero_repair_plan(normalized)
        with warehouse_functional_write_lock(self.runtime_dir):
            under_lock_barrier = barrier_status(self.runtime_dir)
            if under_lock_barrier.get("active") is not False or under_lock_barrier.get(
                "status"
            ) != before_barrier.get("status"):
                raise DenseFbsError(
                    "repair_write_barrier_drift",
                    "Dense FBS write barrier changed at the shared-lock boundary",
                )
            self._revalidate_zero_repair_plan(normalized)
            with self._write() as conn:
                ensure_ff_pool_fbs_applicability_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                request_identity = _fingerprint(
                    {
                        "plan_fingerprint": fingerprint,
                        "approval_reference": str(approval_reference),
                    }
                )
                intent = persist_dense_intent(
                    conn,
                    orchestration_key=operation_id,
                    request_identity=request_identity,
                    subject_kind="repair",
                    subject_id=str(dense_plan["subject_id"]),
                    effective_from=str(dense_plan["effective_from"]),
                    cutover_at=str(dense_plan["cutover_at"]),
                    roster_fingerprint=str(dense_plan["roster_fingerprint"]),
                    plan=dense_plan,
                    actor=str(actor),
                )
                conn.commit()
            with self._read() as conn:
                prior_state = dense_intent_state(conn, str(intent["intent_id"]))
            receipt = self._materialize(intent)
            with self._write() as conn:
                state = dense_intent_state(conn, str(intent["intent_id"]))
                if state.get("state") != "active":
                    append_dense_intent_event(
                        conn,
                        intent_id=str(intent["intent_id"]),
                        state="active",
                        receipt={
                            "repair_complete": True,
                            "approval_reference_digest": _fingerprint(
                                str(approval_reference)
                            ),
                            "coverage_fingerprint": str(receipt["fingerprint"]),
                        },
                        recorded_at=self._now(),
                    )
                    conn.commit()
        return {
            "contract_name": CONTRACT_NAME,
            "state": "active",
            "intent_id": str(intent["intent_id"]),
            "plan_fingerprint": fingerprint,
            "coverage": receipt,
            "idempotent": bool(prior_state.get("state") == "active"),
        }

    def readback_zero_repair(self, *, operation_id: str) -> dict[str, Any]:
        """Query-only exact operation reconciliation; it never submits a document."""

        with self._read() as conn:
            if DENSE_INTENTS_TABLE not in _tables(conn):
                return {"contract_name": CONTRACT_NAME, "state": "not_found"}
            row = conn.execute(
                f"SELECT * FROM {DENSE_INTENTS_TABLE} WHERE orchestration_key=?",
                (str(operation_id),),
            ).fetchone()
            if row is None:
                return {"contract_name": CONTRACT_NAME, "state": "not_found"}
            intent = _intent_from_row(
                row, expected_identity=str(row["request_identity"])
            )
            dense_plan = dict(intent.get("plan") or {})
            documents = list(dense_plan.get("documents") or [])
            targets = sorted(
                int(item["nm_id"])
                for document in documents
                for item in document.get("targets") or []
            )
            receipt_boundary = dict(
                dense_plan.get("zero_repair_receipt_boundary") or {}
            )
            facility_id = str(receipt_boundary.get("facility_id") or "")
            placeholders = ",".join("?" for _ in targets) or "NULL"
            zero_row_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE facility_id=? "
                    f"AND pool='FBS' AND nm_id IN ({placeholders}) "
                    "AND quantity=0 AND CAST(capital_rub AS NUMERIC)=0 "
                    "AND wac_rub IS NULL",
                    (facility_id, *targets),
                ).fetchone()[0]
            )
            document_count = int(
                conn.execute(
                    f"SELECT COUNT(DISTINCT document_id) FROM {DOCUMENT_LINES_TABLE} "
                    f"WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders}) "
                    "AND line_role='absolute_target'",
                    (facility_id, *targets),
                ).fetchone()[0]
            )
            target_line_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {DOCUMENT_LINES_TABLE} "
                    f"WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders}) "
                    "AND line_role='absolute_target'",
                    (facility_id, *targets),
                ).fetchone()[0]
            )
            non_target_count, non_target_digest = _streaming_query_digest(
                conn,
                f"SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,"
                f"wac_rub,source_watermark,updated_at FROM {BALANCES_TABLE} "
                f"WHERE facility_id=? AND pool='FBS' AND nm_id NOT IN ({placeholders}) "
                "ORDER BY nm_id",
                (facility_id, *targets),
            )
            non_target_preserved = bool(receipt_boundary) and (
                non_target_count
                == int(receipt_boundary.get("expected_non_target_row_count") or -1)
                and non_target_digest
                == str(receipt_boundary.get("expected_non_target_digest") or "")
            )
            return {
                "contract_name": CONTRACT_NAME,
                "operation_id": str(operation_id),
                "intent_id": str(intent["intent_id"]),
                "query_only": True,
                "expected_target_count": len(targets),
                "zero_row_count": zero_row_count,
                "pool_inventory_document_count": document_count,
                "absolute_target_line_count": target_line_count,
                "non_target_row_count": non_target_count,
                "non_target_digest": non_target_digest,
                "non_target_preserved": non_target_preserved,
                "exact_reconciled": (
                    bool(targets)
                    and zero_row_count == len(targets)
                    and document_count == 1
                    and target_line_count == len(targets)
                    and non_target_preserved
                ),
                **dense_intent_state(conn, str(intent["intent_id"])),
            }

    def _revalidate_zero_repair_plan(self, plan: Mapping[str, Any]) -> None:
        manifest = dict(plan.get("input_manifest") or {})
        fresh = self.build_zero_repair_plan(
            facility_id=str(manifest["facility_id"]),
            historical_exact_zero_nm_ids=list(
                manifest["partitions"]["historical_exact_zero"]
            ),
            default_applicable_absent_history_nm_ids=list(
                manifest["partitions"]["default_applicable_absent_history"]
            ),
            seller_warehouse_id=int(manifest["seller_warehouse_id"]),
            official_office_id=int(manifest["official_office_id"]),
            expected_roster_nm_ids=list(manifest["expected_roster_nm_ids"]),
            expected_existing_nm_ids=list(manifest["expected_existing_nm_ids"]),
            historical_business_date=str(manifest["historical_business_date"]),
            canonical_target=dict(plan["canonical_target"]),
            storage_generation=dict(plan["storage_generation"]),
            qualified_at=str(manifest["qualified_at"]),
        )
        if str(fresh["fingerprint"]) != str(plan["fingerprint"]):
            raise DenseFbsError(
                "repair_plan_cas_drift",
                "Zero repair qualification changed after planning",
                details={"fresh_fingerprint": str(fresh["fingerprint"])},
            )

    def _load_or_plan_facility_intent(
        self,
        *,
        orchestration_key: str,
        facility_id: str,
        expected_updated_at: str,
        request_identity: str,
        actor: str,
    ) -> dict[str, Any]:
        with self._write() as conn:
            ensure_ff_pool_fbs_applicability_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"SELECT * FROM {DENSE_INTENTS_TABLE} WHERE orchestration_key=?",
                (orchestration_key,),
            ).fetchone()
            if existing is not None:
                return _intent_from_row(existing, expected_identity=request_identity)
            facility = conn.execute(
                f"SELECT facility_id,active,display_timezone,updated_at FROM {FACILITIES_TABLE} "
                "WHERE facility_id=?",
                (facility_id,),
            ).fetchone()
            if facility is None:
                raise DenseFbsError("facility_not_found", "Facility was not found")
            if bool(facility["active"]):
                raise DenseFbsError(
                    "facility_already_active", "Facility is already active"
                )
            if str(facility["updated_at"]) != expected_updated_at:
                raise DenseFbsError(
                    "facility_activation_cas_drift",
                    "Facility revision differs from the activation request",
                )
            now = self._now()
            business_date = _business_date(now)
            roster = stock_managed_nomenclature(conn)
            epoch = _writer_epoch(conn)
            plan = _activation_plan(
                conn,
                subject_kind="facility_activation",
                subject_id=facility_id,
                facilities=[dict(facility)],
                sku_roster=roster,
                assumed_active_facility_ids=[facility_id],
                assumed_active_nm_ids=[],
                materialize_nm_ids=None,
                epoch=epoch,
                effective_from=business_date,
                cutover_at=now,
                expected_subject={
                    "facility_id": facility_id,
                    "updated_at": expected_updated_at,
                    "active": False,
                },
            )
            intent = persist_dense_intent(
                conn,
                orchestration_key=orchestration_key,
                request_identity=request_identity,
                subject_kind="facility_activation",
                subject_id=facility_id,
                effective_from=business_date,
                cutover_at=now,
                roster_fingerprint=str(plan["roster_fingerprint"]),
                plan=plan,
                actor=actor,
            )
            conn.commit()
            return intent

    def _load_or_plan_sku_intent(
        self,
        *,
        orchestration_key: str,
        staged_items: Sequence[Mapping[str, Any]],
        request_identity: str,
        actor: str,
    ) -> dict[str, Any]:
        with self._write() as conn:
            ensure_ff_pool_fbs_applicability_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"SELECT * FROM {DENSE_INTENTS_TABLE} WHERE orchestration_key=?",
                (orchestration_key,),
            ).fetchone()
            if existing is not None:
                return _intent_from_row(existing, expected_identity=request_identity)
            for item in staged_items:
                row = conn.execute(
                    f"SELECT is_active,is_hidden,nm_id,updated_at FROM {NOMENCLATURE_TABLE} "
                    "WHERE item_id=?",
                    (str(item["item_id"]),),
                ).fetchone()
                if (
                    row is None
                    or bool(row[0])
                    or bool(row[1])
                    or (int(row[2] or 0), str(row[3]))
                    != (int(item["nm_id"]), str(item["updated_at"]))
                ):
                    raise DenseFbsError(
                        "staged_nomenclature_cas_drift",
                        "Nomenclature is not in the exact staged inactive state",
                        details={"item_id": str(item["item_id"])},
                    )
            active_roster = stock_managed_nomenclature(conn)
            active_nm_ids = {int(item["nm_id"]) for item in active_roster}
            for item in staged_items:
                if int(item["nm_id"]) in active_nm_ids:
                    raise DenseFbsError(
                        "active_nomenclature_ambiguous",
                        f"nmId {item['nm_id']} already has an active stock-managed identity",
                    )
            roster = sorted(
                [*active_roster, *[dict(item) for item in staged_items]],
                key=lambda item: (int(item["nm_id"]), str(item["item_id"])),
            )
            facilities = [
                dict(row)
                for row in conn.execute(
                    f"SELECT facility_id,active,display_timezone,updated_at "
                    f"FROM {FACILITIES_TABLE} WHERE active=1 ORDER BY code,facility_id"
                ).fetchall()
            ]
            now = self._now()
            business_date = _business_date(now)
            epoch = _writer_epoch(conn, allow_empty_facilities=not facilities)
            plan = _activation_plan(
                conn,
                subject_kind="sku_activation",
                subject_id=_fingerprint(staged_items),
                facilities=facilities,
                sku_roster=roster,
                assumed_active_facility_ids=[],
                assumed_active_nm_ids=[int(item["nm_id"]) for item in staged_items],
                materialize_nm_ids=[int(item["nm_id"]) for item in staged_items],
                epoch=epoch,
                effective_from=business_date,
                cutover_at=now,
                expected_subject={
                    "staged_items": [dict(item) for item in staged_items]
                },
            )
            intent = persist_dense_intent(
                conn,
                orchestration_key=orchestration_key,
                request_identity=request_identity,
                subject_kind="sku_activation",
                subject_id=str(plan["subject_id"]),
                effective_from=business_date,
                cutover_at=now,
                roster_fingerprint=str(plan["roster_fingerprint"]),
                plan=plan,
                actor=actor,
            )
            conn.commit()
            return intent

    def _materialize(self, intent: Mapping[str, Any]) -> dict[str, Any]:
        state = self._intent_state(str(intent["intent_id"]))
        if state.get("state") == "active":
            with self._read() as conn:
                row = conn.execute(
                    f"""SELECT receipt_json FROM {DENSE_INTENT_EVENTS_TABLE}
                        WHERE intent_id=? AND state='materialized'
                        ORDER BY event_sequence DESC LIMIT 1""",
                    (str(intent["intent_id"]),),
                ).fetchone()
            if row is None:
                raise DenseFbsError(
                    "dense_fbs_materialized_receipt_missing",
                    "Active dense FBS intent lacks its materialized coverage receipt",
                )
            return dict(json.loads(str(row[0])))
        if state.get("state") == "blocked":
            raise DenseFbsError(
                "dense_fbs_intent_blocked",
                "Dense FBS intent is terminally blocked and cannot be retried",
                details=dict(state.get("receipt") or {}),
            )
        now = self._now()
        with self._write() as conn:
            append_dense_intent_event(
                conn,
                intent_id=str(intent["intent_id"]),
                state="materializing",
                receipt={"plan_fingerprint": str(intent["plan_fingerprint"])},
                recorded_at=now,
            )
            conn.commit()
        plan = dict(intent["plan"])
        documents: list[dict[str, Any]] = []
        try:
            with self._read() as conn:
                _verify_compact_existing_coverage(conn, plan=plan)
            for specification in plan.get("documents") or []:
                documents.append(self._materialize_document(intent, specification))
            with self._read() as conn:
                receipt = coverage_receipt(
                    conn,
                    pairs=[
                        (str(item["facility_id"]), int(item["nm_id"]))
                        for item in plan.get("pairs") or []
                    ],
                    as_of_date=str(intent["effective_from"]),
                    projection_epoch=int(plan["projection_epoch"]),
                    assumed_active_facility_ids=list(
                        plan.get("assumed_active_facility_ids") or []
                    ),
                    assumed_active_nm_ids=list(plan.get("assumed_active_nm_ids") or []),
                )
            if not receipt["complete"]:
                raise DenseFbsError(
                    "dense_fbs_coverage_incomplete",
                    "Dense FBS coverage readback remains incomplete",
                    details=receipt["incomplete"],
                )
        except DenseFbsResumableError as exc:
            resumable = {
                "code": exc.code,
                "message": str(exc),
                "details": exc.details,
                "resume_requires_same_orchestration_identity": True,
                "blind_retry_allowed": False,
            }
            with self._write() as conn:
                append_dense_intent_event(
                    conn,
                    intent_id=str(intent["intent_id"]),
                    state="resumable",
                    receipt=resumable,
                    recorded_at=self._now(),
                )
                conn.commit()
            raise
        except Exception as exc:
            if _recoverable_materialization_error(exc):
                resumable = {
                    "code": "dense_fbs_materialization_resumable",
                    "message": str(exc),
                    "recoverable_error_type": type(exc).__name__,
                    "resume_requires_same_orchestration_identity": True,
                    "blind_retry_allowed": False,
                }
                with self._write() as conn:
                    append_dense_intent_event(
                        conn,
                        intent_id=str(intent["intent_id"]),
                        state="resumable",
                        receipt=resumable,
                        recorded_at=self._now(),
                    )
                    conn.commit()
                raise DenseFbsResumableError(
                    "dense_fbs_materialization_resumable",
                    "Dense FBS materialization hit a recoverable exact-intent failure",
                    details=resumable,
                ) from exc
            code = str(getattr(exc, "code", "dense_fbs_materialization_failed"))
            details = getattr(exc, "details", None)
            blocked = {
                "code": code,
                "message": str(exc),
                "details": details,
                "blind_retry_allowed": False,
            }
            with self._write() as conn:
                append_dense_intent_event(
                    conn,
                    intent_id=str(intent["intent_id"]),
                    state="blocked",
                    receipt=blocked,
                    recorded_at=self._now(),
                )
                conn.commit()
            if isinstance(exc, DenseFbsError):
                raise
            raise DenseFbsError(code, str(exc), details=details) from exc
        receipt = {**receipt, "documents": documents}
        receipt["fingerprint"] = _fingerprint(
            {key: value for key, value in receipt.items() if key != "fingerprint"}
        )
        with self._write() as conn:
            append_dense_intent_event(
                conn,
                intent_id=str(intent["intent_id"]),
                state="materialized",
                receipt=receipt,
                recorded_at=self._now(),
            )
            conn.commit()
        return receipt

    def _materialize_document(
        self, intent: Mapping[str, Any], specification: Mapping[str, Any]
    ) -> dict[str, Any]:
        service = self._document_service()
        identity = DocumentIdentity(
            request_id=str(specification["request_id"]),
            source_system=SOURCE_SYSTEM,
            source_type=SOURCE_TYPE,
            source_id=f"{intent['intent_id']}:{specification['facility_id']}",
            source_revision=str(intent["plan_fingerprint"]),
            idempotency_epoch=int(intent["plan"]["projection_epoch"]),
            actor=str(intent["actor"]),
            business_date=str(specification["business_date"]),
        )
        dense = {
            "contract_name": CONTRACT_NAME,
            "intent_id": str(intent["intent_id"]),
            "subject_kind": str(intent["subject_kind"]),
            "subject_id": str(intent["subject_id"]),
            "effective_from": str(intent["effective_from"]),
            "cutover_at": str(intent["cutover_at"]),
            "roster_fingerprint": str(intent["roster_fingerprint"]),
            "plan_fingerprint": str(intent["plan_fingerprint"]),
            "applicable_nm_ids": list(specification["applicable_nm_ids"]),
            "expected_balance_rows": list(specification["expected_balance_rows"]),
        }
        manifest = {
            "facility_id": str(specification["facility_id"]),
            "scope": "FBS",
            "targets": list(specification["targets"]),
            "dense_fbs_initialization": dense,
        }
        try:
            preview = service.accept_preview(
                identity=identity,
                document_kind="pool_inventory",
                manifest=manifest,
            )
        except Exception as exc:
            status = service.status(request_id=identity.request_id)
            return _document_transport_readback(
                status=status,
                identity=identity,
                phase="accept_preview",
                error=exc,
            )
        status = preview
        if str(status.get("state") or "") == "processing":
            status = service.resume_request(str(status["request_id"]))
        if str(status.get("state") or "") == "accepted":
            try:
                status = service.process_request(str(status["request_id"]))
            except Exception as exc:
                status = service.status(request_id=str(preview["request_id"]))
                return _document_transport_readback(
                    status=status,
                    identity=identity,
                    phase="process_request",
                    error=exc,
                )
        if str(status.get("state") or "") in {"ready", "posted", "replay"}:
            try:
                status = service.post(str(status["request_id"]))
            except Exception as exc:
                # Never submit again in the same invocation.  Immutable
                # canonical readback decides complete vs exact-id resume.
                status = service.status(request_id=str(preview["request_id"]))
                return _document_transport_readback(
                    status=status,
                    identity=identity,
                    phase="post",
                    error=exc,
                )
        if str(status.get("state") or "") in {
            "accepted",
            "processing",
            "ready",
            "posted",
            "replay",
        }:
            raise DenseFbsResumableError(
                "dense_fbs_document_resumable",
                "Canonical pool_inventory request is durable but not complete",
                details={
                    "request_id": str(status.get("request_id") or identity.request_id),
                    "canonical_state": str(status.get("state") or ""),
                },
            )
        if str(status.get("state") or "") != "complete":
            error = dict(status.get("error") or {})
            raise DenseFbsError(
                str(error.get("code") or "dense_fbs_document_incomplete"),
                "Dense FBS pool_inventory document did not complete",
                details={
                    "request_id": str(status.get("request_id") or identity.request_id),
                    "state": str(status.get("state") or ""),
                    "error": error,
                },
            )
        document = dict(status.get("document") or {})
        return {
            "request_id": str(status["request_id"]),
            "document_id": str(document.get("document_id") or ""),
            "posted_manifest_sha256": str(status.get("posted_manifest_sha256") or ""),
            "state": "complete",
        }

    def _verify_materialized_under_transaction(
        self, conn: sqlite3.Connection, intent: Mapping[str, Any]
    ) -> dict[str, Any]:
        plan = dict(intent["plan"])
        _verify_compact_existing_coverage(conn, plan=plan)
        receipt = coverage_receipt(
            conn,
            pairs=[
                (str(item["facility_id"]), int(item["nm_id"]))
                for item in plan.get("pairs") or []
            ],
            as_of_date=str(intent["effective_from"]),
            projection_epoch=int(plan["projection_epoch"]),
            assumed_active_facility_ids=list(
                plan.get("assumed_active_facility_ids") or []
            ),
            assumed_active_nm_ids=list(plan.get("assumed_active_nm_ids") or []),
        )
        if not receipt["complete"]:
            raise DenseFbsError(
                "dense_fbs_activation_before_coverage_blocked",
                "Active publication is blocked until dense FBS coverage completes",
                details=receipt["incomplete"],
            )
        for specification in plan.get("documents") or []:
            request = conn.execute(
                f"SELECT state,source_revision,posted_document_id FROM {REQUESTS_TABLE} "
                "WHERE client_request_id=? OR request_id=? ORDER BY request_id LIMIT 1",
                (str(specification["request_id"]), str(specification["request_id"])),
            ).fetchone()
            if (
                request is None
                or str(request["state"]) != "complete"
                or str(request["source_revision"]) != str(intent["plan_fingerprint"])
                or not str(request["posted_document_id"] or "")
            ):
                raise DenseFbsError(
                    "dense_fbs_document_readback_incomplete",
                    "Active publication lacks a completed canonical pool_inventory receipt",
                    details={"request_id": str(specification["request_id"])},
                )
        return receipt

    def _intent_state(self, intent_id: str) -> dict[str, Any]:
        with self._read() as conn:
            return dense_intent_state(conn, intent_id)

    def _terminal_publication_error(
        self,
        conn: sqlite3.Connection,
        intent: Mapping[str, Any],
        error: DenseFbsError,
    ) -> DenseFbsError:
        """Roll back registry work, then commit only terminal intent evidence."""

        conn.rollback()
        with self._write() as evidence_conn:
            evidence_conn.execute("BEGIN IMMEDIATE")
            append_dense_intent_event(
                evidence_conn,
                intent_id=str(intent["intent_id"]),
                state="blocked",
                receipt={
                    "code": error.code,
                    "message": str(error),
                    "details": error.details,
                    "phase": "active_publication",
                    "blind_retry_allowed": False,
                },
                recorded_at=self._now(),
            )
            evidence_conn.commit()
        return error

    def _document_service(self) -> FfPoolDocumentService:
        if self.document_service_factory is not None:
            return self.document_service_factory(
                db_path=self.db_path,
                runtime_dir=self.runtime_dir,
                timestamp_factory=self.timestamp_factory,
                resume=False,
            )
        return FfPoolDocumentService(
            db_path=self.db_path,
            runtime_dir=self.runtime_dir,
            timestamp_factory=self.timestamp_factory,
            resume=False,
        )

    def _read(self) -> sqlite3.Connection:
        uri = f"file:{self.db_path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _write(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _now(self) -> str:
        value = str(self.timestamp_factory())
        if not value.endswith("Z"):
            raise DenseFbsError(
                "invalid_timestamp", "Dense FBS timestamp must be UTC Z"
            )
        return value


def _activation_plan(
    conn: sqlite3.Connection,
    *,
    subject_kind: str,
    subject_id: str,
    facilities: Sequence[Mapping[str, Any]],
    sku_roster: Sequence[Mapping[str, Any]],
    assumed_active_facility_ids: Sequence[str],
    assumed_active_nm_ids: Sequence[int],
    materialize_nm_ids: Sequence[int] | None,
    epoch: int,
    effective_from: str,
    cutover_at: str,
    expected_subject: Mapping[str, Any],
) -> dict[str, Any]:
    roster_nm_ids = [int(item["nm_id"]) for item in sku_roster]
    selected_materialize_nm_ids = (
        set(roster_nm_ids)
        if materialize_nm_ids is None
        else {int(value) for value in materialize_nm_ids}
    )
    if not selected_materialize_nm_ids <= set(roster_nm_ids):
        raise DenseFbsError(
            "dense_fbs_materialization_scope_invalid",
            "Dense FBS materialization scope is outside the pinned SKU roster",
        )
    if len(set(roster_nm_ids)) != len(roster_nm_ids):
        raise DenseFbsError(
            "active_nomenclature_ambiguous",
            "Dense FBS roster contains duplicate nmId identities",
        )
    pairs: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    existing_nm_ids = sorted(set(roster_nm_ids) - selected_materialize_nm_ids)
    existing_coverage_proof = _compact_existing_coverage(
        conn,
        facilities=facilities,
        nm_ids=existing_nm_ids,
        as_of_date=effective_from,
        projection_epoch=epoch,
    )
    for facility in facilities:
        facility_id = str(facility["facility_id"])
        applicable_nm_ids: list[int] = []
        expected_rows: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        for nm_id in sorted(roster_nm_ids):
            if nm_id not in selected_materialize_nm_ids:
                continue
            applicability = fbs_pair_applicability(
                conn,
                facility_id=facility_id,
                nm_id=nm_id,
                as_of_date=effective_from,
                facility_active=True,
                sku_active=True,
            )
            pairs.append(
                {
                    "facility_id": facility_id,
                    "nm_id": nm_id,
                    "applicability": applicability,
                }
            )
            if not applicability["applicable"]:
                continue
            row = _balance_cas_row(
                conn,
                facility_id=facility_id,
                nm_id=nm_id,
                epoch=epoch,
            )
            applicable_nm_ids.append(nm_id)
            expected_rows.append(row)
            targets.append(
                {
                    "nm_id": nm_id,
                    "target_fbs": int(row["quantity"]) if row["row_present"] else 0,
                }
            )
        if targets:
            documents.append(
                {
                    "facility_id": facility_id,
                    "business_date": effective_from,
                    "request_id": "dense-fbs-"
                    + _fingerprint(
                        {
                            "subject_kind": subject_kind,
                            "subject_id": subject_id,
                            "facility_id": facility_id,
                            "cutover_at": cutover_at,
                        }
                    ).removeprefix("sha256:")[:32],
                    "applicable_nm_ids": applicable_nm_ids,
                    "expected_balance_rows": expected_rows,
                    "targets": targets,
                }
            )
    roster = {
        "facilities": [
            {
                "facility_id": str(item["facility_id"]),
                "active": bool(item["active"]),
                "updated_at": str(item["updated_at"]),
            }
            for item in facilities
        ],
        "skus": [
            {
                "item_id": str(item["item_id"]),
                "nm_id": int(item["nm_id"]),
                "updated_at": str(item["updated_at"]),
            }
            for item in sku_roster
        ],
        "pairs": pairs,
    }
    return {
        "contract_name": CONTRACT_NAME,
        "subject_kind": subject_kind,
        "subject_id": str(subject_id),
        "projection_epoch": int(epoch),
        "effective_from": str(effective_from),
        "cutover_at": str(cutover_at),
        "roster_fingerprint": _fingerprint(roster),
        "roster": roster,
        "pairs": pairs,
        "documents": documents,
        "assumed_active_facility_ids": sorted(
            {str(value) for value in assumed_active_facility_ids}
        ),
        "assumed_active_nm_ids": sorted(
            {int(value) for value in assumed_active_nm_ids}
        ),
        "materialize_nm_ids": sorted(selected_materialize_nm_ids),
        "existing_coverage_proof": existing_coverage_proof,
        "expected_subject": dict(expected_subject),
        "storage": {
            "whole_database_copy": False,
            "bounded_pair_count": len(pairs),
            "bounded_document_count": len(documents),
        },
    }


def _document_transport_readback(
    *,
    status: Mapping[str, Any],
    identity: DocumentIdentity,
    phase: str,
    error: Exception,
) -> dict[str, Any]:
    """Classify ambiguous transport only from the canonical request state."""

    state = str(status.get("state") or "")
    request_id = str(status.get("request_id") or identity.request_id)
    if state == "complete":
        document = dict(status.get("document") or {})
        return {
            "request_id": request_id,
            "document_id": str(document.get("document_id") or ""),
            "posted_manifest_sha256": str(status.get("posted_manifest_sha256") or ""),
            "state": "complete",
            "transport_reconciled": True,
        }
    if state in {"accepted", "processing", "ready", "posted", "replay"}:
        raise DenseFbsResumableError(
            "dense_fbs_document_transport_resumable",
            "Transport failed but canonical request evidence permits exact-id resume",
            details={
                "request_id": request_id,
                "canonical_state": state,
                "transport_phase": str(phase),
                "transport_error_type": type(error).__name__,
                "canonical_submit_repeated": False,
            },
        ) from error
    canonical_error = dict(status.get("error") or {})
    raise DenseFbsError(
        str(canonical_error.get("code") or "dense_fbs_document_transport_unresolved"),
        "Transport result lacks a safe resumable canonical request state",
        details={
            "request_id": request_id,
            "canonical_state": state or "not_found",
            "transport_phase": str(phase),
            "transport_error_type": type(error).__name__,
            "canonical_error": canonical_error,
        },
    ) from error


def _compact_existing_coverage(
    conn: sqlite3.Connection,
    *,
    facilities: Sequence[Mapping[str, Any]],
    nm_ids: Sequence[int],
    as_of_date: str,
    projection_epoch: int,
) -> dict[str, Any]:
    """Hash existing coverage without persisting a facility x SKU cross-product."""

    digest = hashlib.sha256()
    counts = {"covered": 0, "inapplicable": 0, "missing": 0}
    incomplete_sample: list[dict[str, Any]] = []
    pair_count = 0
    selected_nm_ids = sorted({int(value) for value in nm_ids})
    for facility in sorted(facilities, key=lambda item: str(item["facility_id"])):
        facility_id = str(facility["facility_id"])
        for nm_id in selected_nm_ids:
            component = fbs_physical_component(
                conn,
                facility_id=facility_id,
                nm_id=nm_id,
                as_of_date=as_of_date,
                projection_epoch=int(projection_epoch),
                facility_active=True,
                sku_active=True,
            )
            state = str(component["state"])
            coverage_state = (
                "covered"
                if state in {"exact", "exact_zero"}
                else "inapplicable"
                if state == "inapplicable"
                else "missing"
            )
            counts[coverage_state] += 1
            pair_count += 1
            provenance = dict(component.get("provenance") or {})
            applicability = dict(provenance.get("applicability") or provenance)
            material = {
                "facility_id": facility_id,
                "nm_id": nm_id,
                "coverage_state": coverage_state,
                "applicability_event": str(
                    applicability.get("event") or applicability.get("event_id") or ""
                ),
                "applicability_reason": str(applicability.get("reason") or ""),
                "applicability_effective_from": str(
                    applicability.get("effective_from") or ""
                ),
            }
            digest.update(_json(material).encode("utf-8"))
            digest.update(b"\n")
            if coverage_state == "missing" and len(incomplete_sample) < 25:
                incomplete_sample.append(
                    {"facility_id": facility_id, "nm_id": nm_id, "state": state}
                )
    receipt = {
        "contract_name": "ff_pool_fbs_compact_existing_coverage_v1",
        "as_of_date": str(as_of_date),
        "projection_epoch": int(projection_epoch),
        "pair_count": pair_count,
        "covered_count": counts["covered"],
        "inapplicable_count": counts["inapplicable"],
        "missing_count": counts["missing"],
        "incomplete_sample": incomplete_sample,
        "rows_persisted": False,
        "stream_sha256": "sha256:" + digest.hexdigest(),
        "complete": counts["missing"] == 0,
    }
    receipt["fingerprint"] = _fingerprint(receipt)
    return receipt


def _verify_compact_existing_coverage(
    conn: sqlite3.Connection,
    *,
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    planned = dict(plan.get("existing_coverage_proof") or {})
    if not planned or int(planned.get("pair_count") or 0) == 0:
        return planned
    materialize_nm_ids = {int(value) for value in plan.get("materialize_nm_ids") or []}
    roster = dict(plan.get("roster") or {})
    existing_nm_ids = [
        int(item["nm_id"])
        for item in roster.get("skus") or []
        if int(item["nm_id"]) not in materialize_nm_ids
    ]
    live = _compact_existing_coverage(
        conn,
        facilities=[dict(item) for item in roster.get("facilities") or []],
        nm_ids=existing_nm_ids,
        as_of_date=str(plan["effective_from"]),
        projection_epoch=int(plan["projection_epoch"]),
    )
    if not bool(live["complete"]):
        raise DenseFbsError(
            "preexisting_dense_fbs_coverage_incomplete",
            "SKU activation cannot repair pre-existing FBS coverage gaps",
            details={
                "missing_count": int(live["missing_count"]),
                "incomplete_sample": list(live["incomplete_sample"]),
            },
        )
    if str(live["fingerprint"]) != str(planned.get("fingerprint") or ""):
        raise DenseFbsError(
            "preexisting_dense_fbs_coverage_drift",
            "Existing dense FBS coverage changed after SKU activation was staged",
            details={
                "planned_fingerprint": str(planned.get("fingerprint") or ""),
                "live_fingerprint": str(live["fingerprint"]),
            },
        )
    return live


def _balance_cas_row(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    epoch: int,
) -> dict[str, Any]:
    row = conn.execute(
        f"""SELECT projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at
            FROM {BALANCES_TABLE}
            WHERE facility_id=? AND pool='FBS' AND nm_id=? AND projection_epoch=?""",
        (str(facility_id), int(nm_id), int(epoch)),
    ).fetchone()
    if row is None:
        return {"nm_id": int(nm_id), "row_present": False}
    return {
        "nm_id": int(nm_id),
        "row_present": True,
        "projection_epoch": int(row[0]),
        "quantity": int(row[1]),
        "capital_rub": str(row[2]),
        "wac_rub": row[3],
        "source_watermark": str(row[4]),
        "updated_at": str(row[5]),
    }


def _repair_balance_cas_row(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
) -> dict[str, Any]:
    """Pin absence across every epoch for one future repair target."""

    row = conn.execute(
        f"""SELECT projection_epoch,quantity,capital_rub,wac_rub,
                   source_watermark,updated_at
              FROM {BALANCES_TABLE}
             WHERE facility_id=? AND pool='FBS' AND nm_id=?""",
        (str(facility_id), int(nm_id)),
    ).fetchone()
    if row is None:
        return {"nm_id": int(nm_id), "row_present": False}
    return {
        "nm_id": int(nm_id),
        "row_present": True,
        "projection_epoch": int(row[0]),
        "quantity": int(row[1]),
        "capital_rub": str(row[2]),
        "wac_rub": row[3],
        "source_watermark": str(row[4]),
        "updated_at": str(row[5]),
    }


def _canonical_zero_row(row: Mapping[str, Any]) -> bool:
    try:
        capital = Decimal(str(row.get("capital_rub") or "0"))
    except (InvalidOperation, ValueError):
        return False
    return (
        int(row.get("quantity") or 0) == 0
        and capital.is_finite()
        and capital == Decimal("0")
        and row.get("wac_rub") is None
    )


def _writer_epoch(
    conn: sqlite3.Connection, *, allow_empty_facilities: bool = False
) -> int:
    row = conn.execute(
        f"SELECT epoch,writer_enabled FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    if row is None:
        if allow_empty_facilities:
            # No physical rows can be created without a facility.  Publishing
            # the first SKU active is safe; its later facility activation will
            # require an actual writer epoch and dense materialization.
            return 0
        raise DenseFbsError(
            "facility_pool_writer_epoch_missing",
            "Dense FBS materialization requires an active facility-pool writer epoch",
        )
    if not bool(row[1]):
        raise DenseFbsError(
            "facility_pool_writer_epoch_off",
            "Dense FBS materialization requires an enabled facility-pool writer epoch",
        )
    return int(row[0])


def _exact_repair_mapping_evidence(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    seller_warehouse_id: int,
    official_office_id: int,
    expected_allocation_nm_ids: Sequence[int],
) -> dict[str, Any]:
    mappings = "sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings"
    extensions = "sheet_vitrina_v1_ff_pool_fbs_mapping_extensions"
    allocations = "sheet_vitrina_v1_ff_pool_fbs_mapping_extension_allocations"
    tables = _tables(conn)
    blockers: list[str] = []
    if {mappings, extensions, allocations} - tables:
        blockers.append("exact mapping-extension evidence schema is unavailable")
        result = {
            "mapping": {},
            "extension": {},
            "allocation_count": 0,
            "blockers": blockers,
        }
        result["fingerprint"] = _fingerprint(result)
        return result
    required_mapping_columns = {
        "mapping_id",
        "seller_warehouse_id",
        "facility_id",
        "mapping_digest",
        "active",
        "official_office_id",
        "official_evidence_digest",
    }
    mapping_columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({mappings})")
    }
    if required_mapping_columns - mapping_columns:
        blockers.append("official warehouse mapping evidence columns are unavailable")
        result = {
            "mapping": {},
            "extension": {},
            "allocation_count": 0,
            "blockers": blockers,
        }
        result["fingerprint"] = _fingerprint(result)
        return result
    mapping_rows = conn.execute(
        f"""SELECT mapping_id,seller_warehouse_id,facility_id,mapping_digest,
                   official_office_id,official_evidence_digest,created_at,created_by
              FROM {mappings}
             WHERE seller_warehouse_id=? AND active=1
             ORDER BY created_at,mapping_id""",
        (int(seller_warehouse_id),),
    ).fetchall()
    exact_mapping_rows = [
        row
        for row in mapping_rows
        if str(row[2]) == str(facility_id)
        and int(row[4]) == int(official_office_id)
        and bool(str(row[3] or ""))
        and bool(str(row[5] or ""))
    ]
    if len(mapping_rows) != 1 or len(exact_mapping_rows) != 1:
        blockers.append(
            "seller warehouse mapping is not one exact active "
            f"{seller_warehouse_id}/{official_office_id}->{facility_id} identity"
        )
    mapping = dict(exact_mapping_rows[0]) if len(exact_mapping_rows) == 1 else {}
    extension_rows = (
        conn.execute(
            f"""SELECT extension_id,cutover_id,warehouse_mapping_id,
                       seller_warehouse_id,official_office_id,facility_id,
                       source_receipt_document_id,source_receipt_root_document_id,
                       source_receipt_digest,mapping_digest,official_evidence_digest,
                       frozen_rows_digest,plan_fingerprint,deployed_sha,
                       approval_reference,created_by,created_at
                  FROM {extensions}
                 WHERE warehouse_mapping_id=? AND seller_warehouse_id=?
                   AND official_office_id=? AND facility_id=?
                 ORDER BY created_at,extension_id""",
            (
                str(mapping.get("mapping_id") or ""),
                int(seller_warehouse_id),
                int(official_office_id),
                str(facility_id),
            ),
        ).fetchall()
        if mapping
        else []
    )
    if len(extension_rows) != 1:
        blockers.append(
            "exact accepted facility mapping extension is missing or ambiguous"
        )
    extension = dict(extension_rows[0]) if len(extension_rows) == 1 else {}
    if extension and (
        str(extension.get("mapping_digest") or "")
        != str(mapping.get("mapping_digest") or "")
        or str(extension.get("official_evidence_digest") or "")
        != str(mapping.get("official_evidence_digest") or "")
        or len(str(extension.get("deployed_sha") or "")) != 40
        or not all(
            str(extension.get(key) or "")
            for key in (
                "source_receipt_document_id",
                "source_receipt_root_document_id",
                "source_receipt_digest",
                "frozen_rows_digest",
                "plan_fingerprint",
                "approval_reference",
            )
        )
    ):
        blockers.append(
            "facility mapping-extension provenance is incomplete or drifted"
        )
    allocation_count, allocation_digest = (
        _streaming_query_digest(
            conn,
            f"""SELECT extension_id,nm_id,opening_quantity,opening_capital_rub,
                       frozen_wac_rub,source_balance_watermark,allocation_digest,created_at
                  FROM {allocations} WHERE extension_id=? ORDER BY nm_id""",
            (str(extension.get("extension_id") or ""),),
        )
        if extension
        else (0, _fingerprint([]))
    )
    allocation_nm_ids = (
        [
            int(row[0])
            for row in conn.execute(
                f"SELECT nm_id FROM {allocations} WHERE extension_id=? ORDER BY nm_id",
                (str(extension.get("extension_id") or ""),),
            ).fetchall()
        ]
        if extension
        else []
    )
    allocation_balance_consistency = _repair_allocation_balance_consistency(
        conn,
        extension_id=str(extension.get("extension_id") or ""),
        facility_id=str(facility_id),
        expected_nm_ids=expected_allocation_nm_ids,
    )
    blockers.extend(allocation_balance_consistency["blockers"])
    result = {
        "seller_warehouse_id": int(seller_warehouse_id),
        "official_office_id": int(official_office_id),
        "facility_id": str(facility_id),
        "mapping": mapping,
        "extension": extension,
        "allocation_count": allocation_count,
        "allocation_nm_ids": allocation_nm_ids,
        "allocation_digest": allocation_digest,
        "allocation_balance_consistency": allocation_balance_consistency,
        "blockers": blockers,
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def _repair_allocation_balance_consistency(
    conn: sqlite3.Connection,
    *,
    extension_id: str,
    facility_id: str,
    expected_nm_ids: Sequence[int],
) -> dict[str, Any]:
    """Bind the 21 receipt allocations to current canonical FBS identities.

    Opening values are compared to the current balance only while both rows
    still carry the same source watermark. A later canonical watermark may
    legitimately reflect ordinary lifecycle movements, so only its canonical
    physical shape and exact identity remain safe invariants in that case.
    """

    allocations = "sheet_vitrina_v1_ff_pool_fbs_mapping_extension_allocations"
    selected = sorted({int(value) for value in expected_nm_ids})
    rows = conn.execute(
        f"""SELECT allocation.nm_id,allocation.opening_quantity,
                   allocation.opening_capital_rub,allocation.frozen_wac_rub,
                   allocation.source_balance_watermark,allocation.allocation_digest,
                   balance.quantity,balance.capital_rub,balance.wac_rub,
                   balance.source_watermark,balance.projection_epoch
              FROM {allocations} allocation
              LEFT JOIN {BALANCES_TABLE} balance
                ON balance.facility_id=? AND balance.pool='FBS'
               AND balance.nm_id=allocation.nm_id
             WHERE allocation.extension_id=?
             ORDER BY allocation.nm_id""",
        (str(facility_id), str(extension_id)),
    ).fetchall()
    blockers: list[str] = []
    invalid_allocation_nm_ids: list[int] = []
    missing_current_nm_ids: list[int] = []
    invalid_current_nm_ids: list[int] = []
    same_source_nm_ids: list[int] = []
    same_source_value_drift_nm_ids: list[int] = []
    current_nm_ids: list[int] = []
    for row in rows:
        nm_id = int(row[0])
        try:
            opening_quantity = int(row[1])
            opening_capital = Decimal(str(row[2]))
            frozen_wac = Decimal(str(row[3]))
        except (InvalidOperation, TypeError, ValueError):
            invalid_allocation_nm_ids.append(nm_id)
            continue
        if (
            opening_quantity <= 0
            or not opening_capital.is_finite()
            or opening_capital <= 0
            or not frozen_wac.is_finite()
            or frozen_wac <= 0
            or not str(row[4] or "")
            or not str(row[5] or "")
            or str(row[5])
            != _fingerprint(
                {
                    "extension_id": str(extension_id),
                    "nm_id": nm_id,
                    "opening_quantity": opening_quantity,
                    "opening_capital_rub": str(row[2]),
                    "frozen_wac_rub": str(row[3]),
                    "source_balance_watermark": str(row[4]),
                }
            )
        ):
            invalid_allocation_nm_ids.append(nm_id)
        if row[6] is None:
            missing_current_nm_ids.append(nm_id)
            continue
        current_nm_ids.append(nm_id)
        try:
            current_quantity = int(row[6])
            current_capital = Decimal(str(row[7]))
            current_wac = None if row[8] is None else Decimal(str(row[8]))
        except (InvalidOperation, TypeError, ValueError):
            invalid_current_nm_ids.append(nm_id)
            continue
        current_is_canonical = (
            current_quantity == 0
            and current_capital.is_finite()
            and current_capital == 0
            and current_wac is None
        ) or (
            current_quantity > 0
            and current_capital.is_finite()
            and current_capital > 0
            and current_wac is not None
            and current_wac.is_finite()
            and current_wac > 0
        )
        if not current_is_canonical or not str(row[9] or ""):
            invalid_current_nm_ids.append(nm_id)
        if str(row[9] or "") == str(row[4] or ""):
            same_source_nm_ids.append(nm_id)
            if (
                current_quantity != opening_quantity
                or current_capital != opening_capital
                or current_wac != frozen_wac
            ):
                same_source_value_drift_nm_ids.append(nm_id)
    if invalid_allocation_nm_ids:
        blockers.append(
            "mapping-extension allocations are not receipt-backed positive WAC rows: "
            + ", ".join(map(str, sorted(set(invalid_allocation_nm_ids))))
        )
    if missing_current_nm_ids:
        blockers.append(
            "mapping-extension allocation lacks its current canonical FBS row: "
            + ", ".join(map(str, sorted(set(missing_current_nm_ids))))
        )
    if invalid_current_nm_ids:
        blockers.append(
            "current facility FBS allocation row has a non-canonical shape: "
            + ", ".join(map(str, sorted(set(invalid_current_nm_ids))))
        )
    if same_source_value_drift_nm_ids:
        blockers.append(
            "current facility FBS row drifted from its matching allocation source: "
            + ", ".join(map(str, sorted(set(same_source_value_drift_nm_ids))))
        )
    if current_nm_ids != selected:
        blockers.append(
            "current canonical allocation identities do not equal the exact non-target set"
        )
    evidence = {
        "expected_nm_ids": selected,
        "current_nm_ids": current_nm_ids,
        "positive_receipt_allocation_count": len(rows)
        - len(set(invalid_allocation_nm_ids)),
        "same_source_watermark_nm_ids": same_source_nm_ids,
        "same_source_value_match_count": len(same_source_nm_ids)
        - len(set(same_source_value_drift_nm_ids)),
        "blockers": blockers,
    }
    evidence["fingerprint"] = _fingerprint(evidence)
    return evidence


def _target_effect_evidence(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    seller_warehouse_id: int,
    nm_ids: Sequence[int],
) -> dict[str, Any]:
    tables = _tables(conn)
    selected = sorted({int(value) for value in nm_ids})
    placeholders = ",".join("?" for _ in selected)
    empty = (0, _fingerprint([]))
    specifications: dict[str, tuple[str, tuple[Any, ...]] | None] = {
        "movement_lines": (
            f"""SELECT line.operation_id,line.line_no,line.facility_id,line.pool,
                       line.nm_id,line.quantity_delta,line.capital_delta_rub,
                       line.wac_snapshot_rub,line.metadata_json,
                       operation.business_date,operation.posted_at
                  FROM {LINES_TABLE} line
                  JOIN {OPERATIONS_TABLE} operation USING(operation_id)
                 WHERE line.facility_id=? AND line.pool='FBS'
                   AND line.nm_id IN ({placeholders})
                 ORDER BY line.operation_id,line.line_no""",
            (str(facility_id), *selected),
        )
        if LINES_TABLE in tables
        else None,
        "document_lines": (
            f"""SELECT document_id,line_no,facility_id,pool,nm_id,line_role,
                       quantity,capital_rub,expense_rub,metadata_json
                  FROM {DOCUMENT_LINES_TABLE}
                 WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders})
                 ORDER BY document_id,line_no""",
            (str(facility_id), *selected),
        )
        if DOCUMENT_LINES_TABLE in tables
        else None,
        "lifecycle_events": (
            f"""SELECT event_sequence,event_id,order_id,event_type,facility_id,nm_id,
                       physical_quantity_delta,evidence_digest,occurred_at
                  FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events
                 WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders})
                 ORDER BY event_sequence""",
            (str(facility_id), *selected),
        )
        if "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events" in tables
        else None,
        "lifecycle_current": (
            f"""SELECT cutover_id,order_id,state,facility_id,nm_id,quantity,updated_at
                  FROM {FBS_CURRENT_TABLE}
                 WHERE facility_id=? AND pool='FBS' AND nm_id IN ({placeholders})
                 ORDER BY cutover_id,order_id""",
            (str(facility_id), *selected),
        )
        if FBS_CURRENT_TABLE in tables
        else None,
        "legacy_reservations": (
            f"""SELECT line.nm_id,SUM(line.quantity_delta) net_quantity
                  FROM sheet_vitrina_v1_ff_stock_reservation_lines line
                  JOIN sheet_vitrina_v1_ff_stock_reservation_operations operation
                    ON operation.operation_id=line.operation_id
                 WHERE line.nm_id IN ({placeholders})
                 GROUP BY line.nm_id HAVING SUM(line.quantity_delta)<>0
                 ORDER BY line.nm_id""",
            tuple(selected),
        )
        if {
            "sheet_vitrina_v1_ff_stock_reservation_lines",
            "sheet_vitrina_v1_ff_stock_reservation_operations",
        }
        <= tables
        else None,
        "official_orders": (
            f"""SELECT observation_sequence,order_id,source_revision,warehouse_id,
                       office_id,nm_id,observed_at
                  FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
                 WHERE warehouse_id=? AND nm_id IN ({placeholders})
                 ORDER BY observation_sequence""",
            (int(seller_warehouse_id), *selected),
        )
        if "sheet_vitrina_v1_wb_supplies_fbs_order_observations" in tables
        else None,
        "identity_mapped_order_evidence": (
            f"""SELECT evidence.evidence_sequence,evidence.order_id,
                       evidence.order_revision,evidence.warehouse_id,
                       evidence.nm_id,evidence.identity_mapping_id,
                       mapping.target_nm_id,evidence.outcome,evidence.observed_at
                  FROM sheet_vitrina_v1_wb_supplies_fbs_identity_evidence evidence
                  JOIN sheet_vitrina_v1_wb_supplies_fbs_identity_mappings mapping
                    ON mapping.mapping_id=evidence.identity_mapping_id
                 WHERE evidence.warehouse_id=?
                   AND mapping.target_nm_id IN ({placeholders})
                 ORDER BY evidence.evidence_sequence""",
            (int(seller_warehouse_id), *selected),
        )
        if {
            "sheet_vitrina_v1_wb_supplies_fbs_identity_evidence",
            "sheet_vitrina_v1_wb_supplies_fbs_identity_mappings",
        }
        <= tables
        else None,
    }
    result: dict[str, Any] = {}
    total = 0
    for key, specification in specifications.items():
        count, digest = (
            _streaming_query_digest(conn, specification[0], specification[1])
            if specification is not None
            else empty
        )
        result[f"{key}_count"] = count
        result[f"{key}_digest"] = digest
        total += count
    result["effect_row_count"] = total
    result["fingerprint"] = _fingerprint(result)
    return result


def _strict_positive_partition(values: Sequence[int], *, name: str) -> list[int]:
    """Validate an exact manifest partition without silently de-duplicating it."""

    if not isinstance(values, (list, tuple)):
        raise DenseFbsError(
            "repair_manifest_partition_invalid",
            f"{name} must be one JSON array of positive integer nmIds",
        )
    normalized: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or int(value) <= 0:
            raise DenseFbsError(
                "repair_manifest_partition_invalid",
                f"{name} must contain only positive integer nmIds",
            )
        normalized.append(int(value))
    duplicates = sorted(
        value for value in set(normalized) if normalized.count(value) > 1
    )
    if duplicates:
        raise DenseFbsError(
            "repair_manifest_partition_duplicate",
            f"{name} contains duplicate nmIds",
            details={"duplicate_nm_ids": duplicates},
        )
    return sorted(normalized)


def _default_absent_history_evidence(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    seller_warehouse_id: int,
    nm_ids: Sequence[int],
    as_of_date: str,
) -> dict[str, Any]:
    """Prove the default-applicable partition is WB-Content sourced and history-free."""

    selected = sorted(int(value) for value in nm_ids)
    placeholders = ",".join("?" for _ in selected)
    blockers: list[str] = []
    tables = _tables(conn)
    rows = []
    if NOMENCLATURE_TABLE in tables and selected:
        rows = conn.execute(
            f"""SELECT item_id,is_active,is_hidden,nm_id,vendor_code,wb_title,
                       wb_updated_at,wb_synced_at,wb_sync_status,
                       wb_sync_evidence_json,updated_at
                  FROM {NOMENCLATURE_TABLE}
                 WHERE nm_id IN ({placeholders})
                 ORDER BY nm_id,item_id""",
            tuple(selected),
        ).fetchall()
    lifecycle_rows: list[dict[str, Any]] = []
    for row in rows:
        try:
            evidence = json.loads(str(row[9] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            evidence = {}
        lifecycle_rows.append(
            {
                "item_id": str(row[0]),
                "is_active": bool(row[1]),
                "is_hidden": bool(row[2]),
                "nm_id": int(row[3]),
                "vendor_code": str(row[4] or ""),
                "wb_title": str(row[5] or ""),
                "wb_updated_at": str(row[6] or ""),
                "wb_synced_at": str(row[7] or ""),
                "wb_sync_status": str(row[8] or ""),
                "wb_sync_evidence": evidence,
                "updated_at": str(row[10] or ""),
            }
        )
    if [row["nm_id"] for row in lifecycle_rows] != selected:
        blockers.append(
            "default-applicable absent-history partition lacks one exact active nomenclature identity"
        )
    invalid_lifecycle = [
        row["nm_id"]
        for row in lifecycle_rows
        if not row["is_active"]
        or row["is_hidden"]
        or row["wb_sync_evidence"].get("source") != "wb_content_cards"
        or row["wb_sync_evidence"].get("endpoint") != "/content/v2/get/cards/list"
        or row["wb_sync_evidence"].get("nm_id") != row["nm_id"]
        or str(row["wb_sync_evidence"].get("vendor_code") or "") != row["vendor_code"]
        or str(row["wb_sync_evidence"].get("result") or "")
        not in {"created", "matched"}
        or not str(row["wb_sync_evidence"].get("match_type") or "")
        or not row["wb_synced_at"]
        or row["wb_sync_status"]
        != (
            "created"
            if row["wb_sync_evidence"].get("result") == "created"
            else "matched_"
            + str(row["wb_sync_evidence"].get("match_type") or "")
        )
    ]
    if invalid_lifecycle:
        blockers.append(
            "default-applicable targets lack canonical WB Content lifecycle/source identity: "
            + ", ".join(map(str, invalid_lifecycle))
        )

    history_count = 0
    history_digest = _fingerprint([])
    captures = "sheet_vitrina_v1_inventory_history_captures"
    components = "sheet_vitrina_v1_inventory_history_components"
    finalizations = "sheet_vitrina_v1_inventory_history_finalizations"
    if selected and {captures, components, finalizations} <= tables:
        history_count, history_digest = _streaming_query_digest(
            conn,
            f"""SELECT finalization.business_date,component.capture_id,
                       component.scope_kind,component.scope_key,component.nm_id,
                       component.component_kind,component.component_id,
                       component.state,component.quantity,component.source_revision,
                       component.source_digest,component.source_watermark,
                       component.provenance_json
                  FROM {finalizations} finalization
                  JOIN {components} component
                    ON component.capture_id=finalization.capture_id
                 WHERE component.component_kind='FBS_FACILITY'
                   AND component.component_id=?
                   AND component.nm_id IN ({placeholders})
                 ORDER BY finalization.business_date,component.capture_id,
                          component.nm_id""",
            (str(facility_id), *selected),
        )
    if history_count:
        blockers.append(
            "default-applicable absent-history targets already have accepted target-facility history"
        )
    result = {
        "as_of_date": str(as_of_date),
        "facility_id": str(facility_id),
        "seller_warehouse_id": int(seller_warehouse_id),
        "nm_ids": selected,
        "lifecycle_rows": lifecycle_rows,
        "lifecycle_digest": _fingerprint(lifecycle_rows),
        "accepted_target_facility_history_count": history_count,
        "accepted_target_facility_history_digest": history_digest,
        "default_applicability_required": True,
        "blockers": blockers,
    }
    result["fingerprint"] = _fingerprint(result)
    return result


_VOLATILE_QUALIFICATION_KEYS = {
    "qualified_at",
    "cutover_at",
    "created_at",
    "updated_at",
    "recorded_at",
    "posted_at",
    "observed_at",
    "captured_at",
    "finalized_at",
    "wb_synced_at",
    "wb_updated_at",
}


def _without_volatile_qualification_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_volatile_qualification_fields(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_QUALIFICATION_KEYS
            and str(key) not in {"fingerprint", "material_qualification_digest"}
        }
    if isinstance(value, list):
        return [_without_volatile_qualification_fields(item) for item in value]
    return value


def _zero_repair_material_digest(plan: Mapping[str, Any]) -> str:
    """Stable JIT qualification digest; wall-clock evidence is intentionally excluded."""

    non_targets = dict(plan.get("non_targets") or {})
    target_effects = dict(plan.get("target_effects") or {})
    effect_counts = {
        key: value
        for key, value in target_effects.items()
        if key.endswith("_count")
    }
    mapping = dict(plan.get("mapping_evidence") or {})
    material = {
        "contract_name": plan.get("contract_name"),
        "facility": plan.get("facility"),
        "pool": plan.get("pool"),
        "canonical_target": plan.get("canonical_target"),
        "storage_generation": plan.get("storage_generation"),
        "stock_managed_roster": plan.get("stock_managed_roster"),
        "nm_ids": plan.get("nm_ids"),
        "partitions": plan.get("partitions"),
        "input_manifest": plan.get("input_manifest"),
        "target_rows": plan.get("target_rows"),
        "target_applicability": plan.get("target_applicability"),
        "target_effect_counts": effect_counts,
        "historical_zero_evidence": plan.get("historical_zero_evidence"),
        "default_absent_history_evidence": plan.get(
            "default_absent_history_evidence"
        ),
        "mapping_evidence": {
            "seller_warehouse_id": mapping.get("seller_warehouse_id"),
            "official_office_id": mapping.get("official_office_id"),
            "facility_id": mapping.get("facility_id"),
            "mapping": mapping.get("mapping"),
            "extension": mapping.get("extension"),
            "allocation_count": mapping.get("allocation_count"),
            "allocation_nm_ids": mapping.get("allocation_nm_ids"),
            "allocation_balance_consistency": mapping.get(
                "allocation_balance_consistency"
            ),
            "blockers": mapping.get("blockers"),
        },
        "non_targets": {
            "target_facility_existing_fbs_row_count": non_targets.get(
                "target_facility_existing_fbs_row_count"
            ),
            "target_facility_existing_fbs_nm_ids": non_targets.get(
                "target_facility_existing_fbs_nm_ids"
            ),
            "target_facility_existing_fbs_material_count": non_targets.get(
                "target_facility_existing_fbs_material_count"
            ),
            "target_facility_existing_fbs_material_digest": non_targets.get(
                "target_facility_existing_fbs_material_digest"
            ),
        },
        "expected_effects": plan.get("expected_effects"),
        "blockers": plan.get("blockers"),
        "apply_allowed": plan.get("apply_allowed"),
    }
    return _fingerprint(
        _without_volatile_qualification_fields(material)
    )


def _historical_zero_evidence(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_ids: Sequence[int],
    business_date: str,
) -> dict[str, Any]:
    captures = "sheet_vitrina_v1_inventory_history_captures"
    components = "sheet_vitrina_v1_inventory_history_components"
    finalizations = "sheet_vitrina_v1_inventory_history_finalizations"
    tables = _tables(conn)
    blockers: list[str] = []
    selected = sorted({int(value) for value in nm_ids})
    if {captures, components, finalizations} - tables:
        blockers.append("accepted inventory-history evidence schema is unavailable")
        result = {"business_date": str(business_date), "rows": [], "blockers": blockers}
        result["fingerprint"] = _fingerprint(result)
        return result
    finalization = conn.execute(
        f"""SELECT finalization_sequence,finalization_id,capture_id,
                   finalization_identity,finalization_digest,finalized_at
              FROM {finalizations} WHERE business_date=?
             ORDER BY finalization_sequence DESC LIMIT 1""",
        (str(business_date),),
    ).fetchone()
    if finalization is None:
        blockers.append(f"latest accepted history is missing for {business_date}")
        result = {"business_date": str(business_date), "rows": [], "blockers": blockers}
        result["fingerprint"] = _fingerprint(result)
        return result
    capture = conn.execute(
        f"""SELECT capture_id,business_date,capture_kind,formula_version,
                   bundle_version,ready_snapshot_id,ready_plan_version,
                   generation_identity,facility_roster_revision,source_digest,
                   captured_at
              FROM {captures} WHERE capture_id=?""",
        (str(finalization[2]),),
    ).fetchone()
    if capture is None or str(capture[1]) != str(business_date):
        blockers.append("latest finalization does not bind an exact same-date capture")
    placeholders = ",".join("?" for _ in selected)
    rows = [
        {
            "nm_id": int(row[0]),
            "state": str(row[1]),
            "quantity": row[2],
            "source_revision": str(row[3]),
            "source_digest": str(row[4]),
            "source_watermark": str(row[5]),
            "provenance": json.loads(str(row[6] or "{}")),
        }
        for row in conn.execute(
            f"""SELECT nm_id,state,quantity,source_revision,source_digest,
                       source_watermark,provenance_json
                  FROM {components}
                 WHERE capture_id=? AND component_kind='FBS_FACILITY'
                   AND component_id=? AND nm_id IN ({placeholders})
                 ORDER BY nm_id""",
            (str(finalization[2]), str(facility_id), *selected),
        ).fetchall()
    ]
    if [item["nm_id"] for item in rows] != selected:
        blockers.append(
            f"{business_date} accepted history does not cover the exact target set"
        )
    invalid = [
        item["nm_id"]
        for item in rows
        if item["state"] != "exact_zero"
        or int(item["quantity"] if item["quantity"] is not None else -1) != 0
        or str(item["provenance"].get("source") or "")
        != "fbs_mapping_extension_allocation"
    ]
    if invalid:
        blockers.append(
            "historical targets are not exact_zero with mapping-extension provenance: "
            + ", ".join(map(str, invalid))
        )
    next_date = (
        datetime.fromisoformat(str(business_date)).date() + timedelta(days=1)
    ).isoformat()
    next_finalization = conn.execute(
        f"""SELECT capture_id,finalization_digest FROM {finalizations}
             WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1""",
        (next_date,),
    ).fetchone()
    next_rows: list[dict[str, Any]] = []
    if next_finalization is not None:
        next_rows = [
            {
                "nm_id": int(row[0]),
                "state": str(row[1]),
                "quantity": row[2],
                "provenance": json.loads(str(row[3] or "{}")),
            }
            for row in conn.execute(
                f"""SELECT nm_id,state,quantity,provenance_json FROM {components}
                     WHERE capture_id=? AND component_kind='FBS_FACILITY'
                       AND component_id=? AND nm_id IN ({placeholders})
                     ORDER BY nm_id""",
                (str(next_finalization[0]), str(facility_id), *selected),
            ).fetchall()
        ]
    retrocopied = [
        item["nm_id"]
        for item in next_rows
        if "dense_fbs" in str(item["provenance"].get("source") or "")
    ]
    if retrocopied:
        blockers.append(
            f"{next_date} history contains forbidden current dense-FBS retrocopy"
        )
    result = {
        "business_date": str(business_date),
        "latest_finalization": dict(finalization),
        "accepted_capture": dict(capture) if capture is not None else {},
        "rows": rows,
        "exact_zero_count": sum(item["state"] == "exact_zero" for item in rows),
        "mapping_extension_provenance_count": sum(
            str(item["provenance"].get("source") or "")
            == "fbs_mapping_extension_allocation"
            for item in rows
        ),
        "next_business_date": next_date,
        "next_day_finalization_digest": (
            str(next_finalization[1]) if next_finalization is not None else ""
        ),
        "next_day_target_count": len(next_rows),
        "next_day_target_digest": _fingerprint(next_rows),
        "forbidden_next_day_retrocopy_count": len(retrocopied),
        "query_only_no_history_rewrite": True,
        "blockers": blockers,
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def _scoped_repair_non_targets(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    roster_nm_ids: Sequence[int],
    target_nm_ids: Sequence[int],
    historical_business_date: str,
    seller_warehouse_id: int,
) -> dict[str, Any]:
    tables = _tables(conn)
    roster = sorted({int(value) for value in roster_nm_ids})
    targets = {int(value) for value in target_nm_ids}
    non_targets = [value for value in roster if value not in targets]
    roster_placeholders = ",".join("?" for _ in roster) or "NULL"
    non_target_placeholders = ",".join("?" for _ in non_targets) or "NULL"
    result: dict[str, Any] = {}

    def record(key: str, sql: str | None, parameters: Sequence[Any] = ()) -> None:
        count, digest = (
            _streaming_query_digest(conn, sql, parameters)
            if sql is not None
            else (0, _fingerprint([]))
        )
        result[f"{key}_count"] = count
        result[f"{key}_digest"] = digest

    record(
        "moscow_fbs_balances",
        f"""SELECT balance.facility_id,balance.nm_id,balance.projection_epoch,
                   balance.quantity,balance.capital_rub,balance.wac_rub,
                   balance.source_watermark,balance.updated_at
              FROM {BALANCES_TABLE} balance
              JOIN {FACILITIES_TABLE} facility USING(facility_id)
             WHERE balance.facility_id<>? AND balance.pool='FBS'
               AND balance.nm_id IN ({roster_placeholders})
             ORDER BY balance.facility_id,balance.nm_id""",
        (str(facility_id), *roster),
    )
    record(
        "fbo_balances",
        f"""SELECT facility_id,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                   source_watermark,updated_at FROM {BALANCES_TABLE}
             WHERE pool='FBO' AND nm_id IN ({roster_placeholders})
             ORDER BY facility_id,nm_id""",
        tuple(roster),
    )
    record(
        "target_facility_non_target_movements",
        f"""SELECT line.operation_id,line.line_no,line.nm_id,line.quantity_delta,
                   line.capital_delta_rub,line.wac_snapshot_rub,line.metadata_json,
                   operation.business_date,operation.posted_at
              FROM {LINES_TABLE} line
              JOIN {OPERATIONS_TABLE} operation USING(operation_id)
             WHERE line.facility_id=? AND line.pool='FBS'
               AND line.nm_id IN ({non_target_placeholders})
             ORDER BY line.operation_id,line.line_no"""
        if {LINES_TABLE, OPERATIONS_TABLE} <= tables
        else None,
        (str(facility_id), *non_targets),
    )
    record(
        "target_facility_non_target_documents",
        f"""SELECT document_id,line_no,nm_id,line_role,quantity,capital_rub,expense_rub,
                   metadata_json FROM {DOCUMENT_LINES_TABLE}
             WHERE facility_id=? AND pool='FBS'
               AND nm_id IN ({non_target_placeholders})
             ORDER BY document_id,line_no"""
        if DOCUMENT_LINES_TABLE in tables
        else None,
        (str(facility_id), *non_targets),
    )
    record(
        "reservations_orders",
        f"""SELECT cutover_id,order_id,state,facility_id,nm_id,quantity,
                   frozen_wac_rub,updated_at FROM {FBS_CURRENT_TABLE}
             WHERE facility_id=? AND pool='FBS'
               AND nm_id IN ({non_target_placeholders})
             ORDER BY cutover_id,order_id"""
        if FBS_CURRENT_TABLE in tables
        else None,
        (str(facility_id), *non_targets),
    )
    record(
        "official_orders",
        f"""SELECT observation_sequence,order_id,source_revision,warehouse_id,
                   office_id,nm_id,observed_at
              FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
             WHERE warehouse_id=? AND nm_id IN ({non_target_placeholders})
             ORDER BY observation_sequence"""
        if "sheet_vitrina_v1_wb_supplies_fbs_order_observations" in tables
        else None,
        (int(seller_warehouse_id), *non_targets),
    )
    active_version = (
        conn.execute(
            "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active "
            "WHERE slot=1"
        ).fetchone()
        if "sheet_vitrina_v1_warehouse_functional_active" in tables
        else None
    )
    record(
        "functional_aggregate_cost_wac",
        f"""SELECT version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client
              FROM sheet_vitrina_v1_warehouse_functional_balances
             WHERE version_id=? AND nm_id IN ({roster_placeholders})
             ORDER BY warehouse_key,nm_id"""
        if active_version is not None
        and "sheet_vitrina_v1_warehouse_functional_balances" in tables
        else None,
        ((str(active_version[0]), *roster) if active_version is not None else ()),
    )
    record(
        "wb_snapshots",
        """SELECT snapshot.snapshot_id,snapshot.version_id,snapshot.fetched_at,
                  snapshot.snapshot_date,snapshot.pagination_complete,
                  snapshot.page_count,snapshot.raw_row_count,snapshot.raw_rows_digest,
                  snapshot.created_at
             FROM sheet_vitrina_v1_warehouse_functional_active active
             JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
               ON snapshot.version_id=active.version_id
            WHERE active.slot=1 ORDER BY snapshot.snapshot_id"""
        if {
            "sheet_vitrina_v1_warehouse_functional_active",
            "sheet_vitrina_v1_warehouse_wb_snapshots",
        }
        <= tables
        else None,
    )
    history_captures = "sheet_vitrina_v1_inventory_history_captures"
    history_components = "sheet_vitrina_v1_inventory_history_components"
    history_finalizations = "sheet_vitrina_v1_inventory_history_finalizations"
    history_dates = [
        str(historical_business_date),
        (
            datetime.fromisoformat(str(historical_business_date)).date()
            + timedelta(days=1)
        ).isoformat(),
    ]
    history_capture_ids: list[str] = []
    if {history_captures, history_components, history_finalizations} <= tables:
        for business_date in history_dates:
            row = conn.execute(
                f"""SELECT capture_id FROM {history_finalizations}
                     WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1""",
                (business_date,),
            ).fetchone()
            if row is not None:
                history_capture_ids.append(str(row[0]))
    history_placeholders = ",".join("?" for _ in history_capture_ids) or "NULL"
    record(
        "inventory_history",
        f"""SELECT capture_id,scope_kind,scope_key,nm_id,component_kind,
                   component_id,state,quantity,source_revision,source_digest,
                   source_watermark,provenance_json
              FROM {history_components}
             WHERE capture_id IN ({history_placeholders})
               AND (nm_id IS NULL OR nm_id IN ({roster_placeholders}))
             ORDER BY capture_id,scope_kind,scope_key,component_kind,component_id"""
        if history_capture_ids
        else None,
        (*history_capture_ids, *roster),
    )
    record(
        "applicability_events",
        f"""SELECT event_sequence,event_id,facility_id,nm_id,state,effective_from,
                   reason,provenance_json,actor,recorded_at
              FROM {APPLICABILITY_EVENTS_TABLE}
             WHERE nm_id IN ({roster_placeholders})
             ORDER BY event_sequence"""
        if APPLICABILITY_EVENTS_TABLE in tables
        else None,
        tuple(roster),
    )
    result["scope"] = {
        "facility_id": str(facility_id),
        "roster_nm_id_count": len(roster),
        "target_nm_id_count": len(targets),
        "non_target_nm_id_count": len(non_targets),
        "history_dates": history_dates,
        "unscoped_operational_table_hashes": False,
    }
    result["fingerprint"] = _fingerprint(result)
    return result


def _streaming_query_digest(
    conn: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any] = (),
) -> tuple[int, str]:
    """Hash exact ordered rows in bounded memory without a SQLite copy/dump."""

    digest = hashlib.sha256(b"ff_pool_dense_fbs_stream_v1\x00")
    count = 0
    cursor = conn.execute(sql, tuple(parameters))
    columns = [str(item[0]) for item in cursor.description or ()]
    while True:
        rows = cursor.fetchmany(512)
        if not rows:
            break
        for row in rows:
            payload = _json(
                {column: row[index] for index, column in enumerate(columns)}
            ).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
    return count, "sha256:" + digest.hexdigest()


def _intent_from_row(row: Sequence[Any], *, expected_identity: str) -> dict[str, Any]:
    if str(row[2]) != str(expected_identity):
        raise DenseFbsError(
            "dense_intent_identity_conflict",
            "Dense FBS orchestration identity changed during resume",
        )
    return {
        "intent_id": str(row[0]),
        "orchestration_key": str(row[1]),
        "request_identity": str(row[2]),
        "subject_kind": str(row[3]),
        "subject_id": str(row[4]),
        "effective_from": str(row[5]),
        "cutover_at": str(row[6]),
        "roster_fingerprint": str(row[7]),
        "plan_fingerprint": str(row[8]),
        "plan": json.loads(str(row[9])),
        "actor": str(row[10]),
        "created_at": str(row[11]),
    }


def _recoverable_materialization_error(exc: Exception) -> bool:
    """Keep transient local I/O failures resumable under the exact durable intent."""

    if isinstance(exc, DenseFbsError):
        return False
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "database is locked",
                "database table is locked",
                "database is busy",
                "interrupted",
                "disk i/o error",
            )
        )
    return isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN,
        errno.EINTR,
        errno.ETIMEDOUT,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.EPIPE,
    }


def _business_date(timestamp: str) -> str:
    return business_date_from_timestamp(timestamp)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
