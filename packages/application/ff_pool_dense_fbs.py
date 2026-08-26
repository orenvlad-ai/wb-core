"""Durable staged activation for applicability-gated dense FBS physical rows."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from packages.application.ff_pool_documents import (
    DOCUMENT_LINES_TABLE,
    DOCUMENTS_TABLE,
    LINES_TABLE,
    REQUESTS_TABLE,
    FfPoolDocumentService,
)
from packages.application.ff_pool_fbs_applicability import (
    APPLICABILITY_EVENTS_TABLE,
    DENSE_INTENT_EVENTS_TABLE,
    DENSE_INTENTS_TABLE,
    FBS_CURRENT_TABLE,
    FbsApplicabilityError,
    append_applicability_event,
    append_dense_intent_event,
    coverage_receipt,
    dense_intent_state,
    ensure_ff_pool_fbs_applicability_schema,
    fbs_pair_applicability,
    persist_dense_intent,
    stock_managed_nomenclature,
)
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
from packages.contracts.ff_pool_documents import DocumentIdentity


CONTRACT_NAME = "ff_pool_dense_fbs_initialization_v1"
SOURCE_SYSTEM = "wb_core_dense_fbs"
SOURCE_TYPE = CONTRACT_NAME
NOMENCLATURE_TABLE = "sheet_vitrina_v1_nomenclature_items"


class DenseFbsError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


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
                if row is None:
                    raise DenseFbsError("facility_not_found", "Staged facility disappeared")
                if bool(row["active"]):
                    conn.rollback()
                    return {
                        "contract_name": CONTRACT_NAME,
                        "intent_id": intent["intent_id"],
                        "state": "active",
                        "coverage": materialized,
                        "idempotent": True,
                    }
                if str(row["updated_at"]) != str(expected_updated_at):
                    raise DenseFbsError(
                        "facility_activation_cas_drift",
                        "Facility changed after dense FBS activation was staged",
                        details={"current_updated_at": str(row["updated_at"])},
                    )
                self._verify_materialized_under_transaction(conn, intent)
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
                    raise DenseFbsError(
                        "facility_activation_cas_drift",
                        "Facility activation CAS did not match exactly one staged row",
                    )
                change_id = "fffc_" + _fingerprint(
                    {
                        "request_id": request_id,
                        "action": "activated",
                        "facility_id": facility_id,
                    }
                ).removeprefix("sha256:")[:28]
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
            return {"contract_name": CONTRACT_NAME, "state": "active", "idempotent": True}
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
                self._verify_materialized_under_transaction(conn, intent)
                for item in normalized:
                    row = conn.execute(
                        f"SELECT is_active,is_hidden,nm_id,updated_at FROM {NOMENCLATURE_TABLE} "
                        "WHERE item_id=?",
                        (item["item_id"],),
                    ).fetchone()
                    if row is None:
                        raise DenseFbsError(
                            "staged_nomenclature_missing",
                            f"Staged nomenclature item disappeared: {item['item_id']}",
                        )
                    if (
                        bool(row[0])
                        and not bool(row[1])
                        and int(row[2]) == item["nm_id"]
                        and str(row[3]) == item["updated_at"]
                    ):
                        continue
                    if (
                        bool(row[0])
                        or bool(row[1])
                        or int(row[2] or 0) != item["nm_id"]
                        or str(row[3]) != item["updated_at"]
                    ):
                        raise DenseFbsError(
                            "sku_activation_cas_drift",
                            "Staged nomenclature changed before dense coverage completed",
                            details={"item_id": item["item_id"]},
                        )
                    changed = conn.execute(
                        f"UPDATE {NOMENCLATURE_TABLE} SET is_active=1 "
                        "WHERE item_id=? AND is_active=0 AND is_hidden=0 AND nm_id=? AND updated_at=?",
                        (item["item_id"], item["nm_id"], item["updated_at"]),
                    ).rowcount
                    if changed != 1:
                        raise DenseFbsError(
                            "sku_activation_cas_drift",
                            "SKU activation CAS did not match exactly one staged row",
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
        nm_ids: Sequence[int],
        expected_existing_non_target_count: int | None = None,
    ) -> dict[str, Any]:
        """Query-only deterministic plan using the same dense pool_inventory shape."""

        selected_nm_ids = sorted({int(value) for value in nm_ids})
        if not selected_nm_ids or any(value <= 0 for value in selected_nm_ids):
            raise DenseFbsError("repair_scope_invalid", "Repair nmId scope must be non-empty and positive")
        with self._read() as conn:
            ensure_tables = _tables(conn)
            if not {
                FACILITIES_TABLE,
                BALANCES_TABLE,
                FEATURE_EPOCHS_TABLE,
                NOMENCLATURE_TABLE,
            } <= ensure_tables:
                raise DenseFbsError("repair_schema_unavailable", "Dense FBS repair schema is unavailable")
            facility = conn.execute(
                f"SELECT facility_id,code,name,active,display_timezone,updated_at "
                f"FROM {FACILITIES_TABLE} WHERE facility_id=?",
                (str(facility_id),),
            ).fetchone()
            epoch = _writer_epoch(conn)
            blockers: list[str] = []
            if facility is None or not bool(facility["active"]):
                blockers.append("exact target facility is missing or inactive")
            catalog = conn.execute(
                f"SELECT item_id,nm_id,updated_at FROM {NOMENCLATURE_TABLE} "
                f"WHERE is_active=1 AND is_hidden=0 AND nm_id IN ({','.join('?' for _ in selected_nm_ids)}) "
                "ORDER BY nm_id,item_id",
                selected_nm_ids,
            ).fetchall()
            counts = {
                nm_id: sum(int(row["nm_id"]) == nm_id for row in catalog)
                for nm_id in selected_nm_ids
            }
            ambiguous = [nm_id for nm_id, count in counts.items() if count != 1]
            if ambiguous:
                blockers.append(
                    "target SKU lacks one exact active nomenclature identity: "
                    + ", ".join(map(str, ambiguous))
                )
            target_rows = [
                _balance_cas_row(
                    conn,
                    facility_id=str(facility_id),
                    nm_id=nm_id,
                    epoch=epoch,
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
                if bool(row["row_present"])
                and not _canonical_zero_row(row)
            ]
            if conflicts:
                blockers.append(
                    "target physical rows are not canonical explicit zero: "
                    + ", ".join(map(str, conflicts))
                )
            placeholders = ",".join("?" for _ in selected_nm_ids)
            balance_non_target_count, balance_non_target_digest = _streaming_query_digest(
                conn,
                f"""SELECT * FROM {BALANCES_TABLE}
                    WHERE NOT (
                        facility_id=? AND pool='FBS'
                        AND nm_id IN ({placeholders})
                    )
                    ORDER BY facility_id,pool,nm_id""",
                (str(facility_id), *selected_nm_ids),
            )
            target_facility_non_target_count = int(
                conn.execute(
                    f"""SELECT COUNT(*) FROM {BALANCES_TABLE}
                        WHERE facility_id=? AND pool='FBS'
                          AND nm_id NOT IN ({placeholders})""",
                    (str(facility_id), *selected_nm_ids),
                ).fetchone()[0]
            )
            if (
                expected_existing_non_target_count is not None
                and target_facility_non_target_count
                != int(expected_existing_non_target_count)
            ):
                blockers.append(
                    "target facility non-target FBS row count drifted: "
                    f"expected {int(expected_existing_non_target_count)}, "
                    f"found {target_facility_non_target_count}"
                )
            dense_manifest = {
                "contract_name": CONTRACT_NAME,
                "subject_kind": "repair",
                "subject_id": str(facility_id),
                "intent_id": "future_owner_gated_intent",
                "effective_from": "future_apply_t0",
                "cutover_at": "future_apply_t0",
                "roster_fingerprint": _fingerprint(selected_nm_ids),
                "applicable_nm_ids": selected_nm_ids,
                "expected_balance_rows": target_rows,
            }
            plan = {
                "contract_name": "ff_pool_dense_fbs_zero_repair_plan_v1",
                "mode": "dry_run",
                "facility": dict(facility) if facility is not None else {},
                "pool": "FBS",
                "projection_epoch": epoch,
                "nm_ids": selected_nm_ids,
                "targets": [
                    {"nm_id": nm_id, "target_fbs": 0}
                    for nm_id in selected_nm_ids
                ],
                "dense_fbs_initialization": dense_manifest,
                "target_rows": target_rows,
                "expected_effects": {
                    "balance_insert_count": sum(not row["row_present"] for row in target_rows),
                    "balance_update_count": 0,
                    "quantity_delta": 0,
                    "capital_delta_rub": "0",
                    "wac_effect": None,
                    "movement_line_count": 0,
                    "pool_inventory_document_count": 1 if not blockers else 0,
                },
                "non_targets": {
                    "target_facility_existing_fbs_row_count": target_facility_non_target_count,
                    "balance_row_count": balance_non_target_count,
                    "balance_digest": balance_non_target_digest,
                    **_optional_table_digests(conn),
                },
                "storage": {
                    "whole_database_copy": False,
                    "bounded_target_row_count": len(selected_nm_ids),
                    "non_target_digest_fetch_chunk_rows": 512,
                    "non_target_rows_retained_in_memory": False,
                },
                "apply_allowed": not blockers,
                "blockers": blockers,
                "apply_entrypoint_exposed": False,
            }
            plan["fingerprint"] = _fingerprint(plan)
            return plan

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
                raise DenseFbsError("facility_already_active", "Facility is already active")
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
                if row is None or bool(row[0]) or bool(row[1]) or (
                    int(row[2] or 0), str(row[3])
                ) != (int(item["nm_id"]), str(item["updated_at"])):
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
                expected_subject={"staged_items": [dict(item) for item in staged_items]},
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
            materialize_nm_ids = {
                int(value) for value in plan.get("materialize_nm_ids") or []
            }
            preexisting_pairs = [
                (str(item["facility_id"]), int(item["nm_id"]))
                for item in plan.get("pairs") or []
                if int(item["nm_id"]) not in materialize_nm_ids
            ]
            if preexisting_pairs:
                with self._read() as conn:
                    preexisting_receipt = coverage_receipt(
                        conn,
                        pairs=preexisting_pairs,
                        as_of_date=str(intent["effective_from"]),
                        projection_epoch=int(plan["projection_epoch"]),
                    )
                if not preexisting_receipt["complete"]:
                    raise DenseFbsError(
                        "preexisting_dense_fbs_coverage_incomplete",
                        "SKU activation cannot repair pre-existing FBS coverage gaps",
                        details=preexisting_receipt["incomplete"],
                    )
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
                    assumed_active_facility_ids=list(plan.get("assumed_active_facility_ids") or []),
                    assumed_active_nm_ids=list(plan.get("assumed_active_nm_ids") or []),
                )
            if not receipt["complete"]:
                raise DenseFbsError(
                    "dense_fbs_coverage_incomplete",
                    "Dense FBS coverage readback remains incomplete",
                    details=receipt["incomplete"],
                )
        except Exception as exc:
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
        preview = service.accept_preview(
            identity=identity,
            document_kind="pool_inventory",
            manifest=manifest,
        )
        status = preview
        if str(status.get("state") or "") == "ready":
            try:
                status = service.post(str(status["request_id"]))
            except Exception:
                # Ambiguous transport is reconciled by immutable readback; a
                # non-complete state is re-raised below and is never retried.
                status = service.status(request_id=str(preview["request_id"]))
        if str(status.get("state") or "") != "complete":
            raise DenseFbsError(
                "dense_fbs_document_incomplete",
                "Dense FBS pool_inventory document did not complete",
                details={
                    "request_id": str(status.get("request_id") or identity.request_id),
                    "state": str(status.get("state") or ""),
                    "error": status.get("error"),
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
        receipt = coverage_receipt(
            conn,
            pairs=[
                (str(item["facility_id"]), int(item["nm_id"]))
                for item in plan.get("pairs") or []
            ],
            as_of_date=str(intent["effective_from"]),
            projection_epoch=int(plan["projection_epoch"]),
            assumed_active_facility_ids=list(plan.get("assumed_active_facility_ids") or []),
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
            raise DenseFbsError("invalid_timestamp", "Dense FBS timestamp must be UTC Z")
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
    for facility in facilities:
        facility_id = str(facility["facility_id"])
        applicable_nm_ids: list[int] = []
        expected_rows: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        for nm_id in sorted(roster_nm_ids):
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
            if nm_id not in selected_materialize_nm_ids:
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
        "assumed_active_nm_ids": sorted({int(value) for value in assumed_active_nm_ids}),
        "materialize_nm_ids": sorted(selected_materialize_nm_ids),
        "expected_subject": dict(expected_subject),
        "storage": {
            "whole_database_copy": False,
            "bounded_pair_count": len(pairs),
            "bounded_document_count": len(documents),
        },
    }


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


def _optional_table_digests(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = _tables(conn)
    specifications = {
        "movement_lines": (LINES_TABLE, "operation_id,line_no"),
        "operations": (OPERATIONS_TABLE, "operation_id"),
        "documents": (DOCUMENTS_TABLE, "document_id"),
        "document_lines": (DOCUMENT_LINES_TABLE, "document_id,line_no"),
        "reservations_orders": (FBS_CURRENT_TABLE, "cutover_id,order_id"),
        "applicability_events": (APPLICABILITY_EVENTS_TABLE, "event_sequence"),
        "inventory_history": (
            "sheet_vitrina_v1_inventory_history_components",
            "capture_id,scope_kind,scope_key,component_kind,component_id",
        ),
        "functional_balances": (
            "sheet_vitrina_v1_warehouse_functional_balances",
            "version_id,warehouse_key,nm_id",
        ),
        "wb_snapshots": (
            "sheet_vitrina_v1_warehouse_wb_snapshots",
            "snapshot_id",
        ),
    }
    result: dict[str, Any] = {}
    for key, (table, order) in specifications.items():
        if table not in tables:
            result[f"{key}_count"] = 0
            result[f"{key}_digest"] = _fingerprint([])
            continue
        count, digest = _streaming_query_digest(
            conn, f"SELECT * FROM {table} ORDER BY {order}"
        )
        result[f"{key}_count"] = count
        result[f"{key}_digest"] = digest
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
                {
                    column: row[index]
                    for index, column in enumerate(columns)
                }
            ).encode("utf-8")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
            count += 1
    return count, "sha256:" + digest.hexdigest()


def _intent_from_row(
    row: Sequence[Any], *, expected_identity: str
) -> dict[str, Any]:
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


def _business_date(timestamp: str) -> str:
    parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo("Asia/Yekaterinburg")).date().isoformat()


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
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
