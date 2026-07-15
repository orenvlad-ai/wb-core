"""Audited atomic correction of a supplier shipment factual shipment date."""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping
from uuid import uuid4

from packages.application.canonical_cost_engine import (
    CANONICAL_TABLE_PREFIX,
    CUTOVER_DATE,
    STAGES,
    CanonicalCostBlocked,
    CanonicalCostEngine,
    ensure_canonical_cost_schema,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.supplier_financial_document_exact_policy import (
    AUTHORIZED_FINANCIAL_DOCUMENT_CONFIRMATION_IDENTITY,
)
from packages.application.supplier_shipment_status import (
    HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
    supplier_business_today,
    validate_supplier_factual_dates,
)


CORRECTION_SOURCE = "operator_factual_date_correction"
HISTORICAL_ADOPTION_SOURCE = "historical_factual_date_adoption"
HISTORICAL_STATUS_EVENT_TABLE = (
    "sheet_vitrina_v1_supplier_shipment_historical_status_events"
)
AUTHORIZED_HISTORICAL_EXCEPTION_INVOICE_NO = "26GN237"
AUTHORIZED_FACTUAL_ADOPTION_IDENTITY = {
    "shipment_id": "sup_b3070385b00b4eb680bd805d751d65be",
    "invoice_no": "26GN390",
    "invoice_document_id": "tdoc_baa149260aad400681f225761e0cbcc0",
    "source_file_sha256": (
        "59910f328db9e0e47ab06839eae9d378e6abf49822566581fd85320ece03d9d4"
    ),
}
AUTHORIZED_HISTORICAL_EXCEPTION_IDENTITY = {
    "shipment_id": "sup_b8009d513e12422cacb91e40983c16af",
    "invoice_no": "26GN237",
    "invoice_date": "2026-03-29",
    "shipment_date": "2026-05-22",
    "invoice_document_id": "tdoc_42087454b84d4977a48f987658c6becd",
    "source_file_sha256": (
        "92e5a2d63a1330f6c4a7812d9c90425cf7707545a8ac318618449f17d6578085"
    ),
}
FINANCIAL_DOCUMENT_CONFIRMATION_SOURCE = (
    "exact_chained_supplier_financial_document_confirmation"
)
MONEY_QUANT = Decimal("0.000001")
CORRECTION_TABLE = "sheet_vitrina_v1_supplier_shipment_factual_corrections"
ACTIVE_CORRECTION_STATUSES = {"queued", "running"}
FINAL_CORRECTION_STATUSES = {"success", "error"}
VOLATILE_CANONICAL_COLUMNS = {"calculated_at", "created_at", "superseded_at"}
ProgressEmitter = Callable[[str], None]
PROTECTED_COLLATERAL_TABLES = (
    "sheet_vitrina_v1_supplier_shipments",
    "sheet_vitrina_v1_supplier_shipment_lines",
    "sheet_vitrina_v1_supplier_financial_documents",
    "sheet_vitrina_v1_supplier_financial_expense_lines",
    "sheet_vitrina_v1_trade_documents",
    "sheet_vitrina_v1_invoice_contract_links",
    "sheet_vitrina_v1_supplier_ff_cost_layers",
    "sheet_vitrina_v1_supplier_ff_cost_layer_lines",
    "sheet_vitrina_v1_cny_documents",
    "sheet_vitrina_v1_cny_ledger_operations",
    "sheet_vitrina_v1_ff_stock_operations",
    "sheet_vitrina_v1_ff_stock_operation_lines",
    "sheet_vitrina_v1_wb_supplies",
    "sheet_vitrina_v1_wb_supply_cost_layers",
    "sheet_vitrina_v1_nomenclature_items",
    "sheet_vitrina_v1_ready_snapshots",
    "sheet_vitrina_v1_onec_stocks",
    "sheet_vitrina_v1_own_capital_payment_layers",
    "sheet_vitrina_v1_own_capital_events",
    "sheet_vitrina_v1_own_capital_wb_outstanding",
    "sheet_vitrina_v1_wb_opening_baseline",
)


class SupplierShipmentFactualCorrectionError(RuntimeError):
    """A safe operator-visible correction failure."""


class SupplierShipmentCandidateCollateralDrift(ValueError):
    """Exact disposable-candidate collateral drift with sanitized row evidence."""

    def __init__(self, report: Mapping[str, Any]) -> None:
        super().__init__("candidate changed non-target data")
        self.report = dict(report)


class SupplierShipmentFactualCorrectionBlock:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _default_timestamp_factory
        self.failure_injector = failure_injector

    def create_job(
        self,
        *,
        shipment_id: str,
        new_actual_shipment_date: Any,
        actor: str,
    ) -> dict[str, Any]:
        shipment_id = _required_text(shipment_id, "shipment_id")
        actor = _required_text(actor or "operator", "actor")
        raw_header = self._raw_header(shipment_id)
        if raw_header is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        requested_at = self.timestamp_factory()
        business_today = supplier_business_today(timestamp=requested_at)
        resolution = validate_supplier_factual_dates(
            actual_shipment_date=new_actual_shipment_date,
            actual_ff_acceptance_date=raw_header.get("actual_ff_acceptance_date"),
            business_today=business_today,
        )
        new_value = str(new_actual_shipment_date or "").strip()
        old_value = str(raw_header.get("actual_shipment_date") or "").strip()
        if new_value == old_value:
            dry_run = self.dry_run(
                shipment_id=shipment_id,
                new_actual_shipment_date=new_value,
                actor=actor,
                expected_old_value=old_value,
            )
            if not dry_run["would_change"]:
                return {
                    "status": "zero_change",
                    "shipment_id": shipment_id,
                    "old_value": old_value,
                    "new_value": new_value,
                    "derived_status": resolution.to_dict(),
                    "report": dry_run,
                    "active": False,
                    "finished": True,
                }
        request_fingerprint = _hash(
            {
                "shipment_id": shipment_id,
                "old_value": old_value,
                "new_value": new_value,
                "actor": actor,
                "source": CORRECTION_SOURCE,
                "target_header_digest": _target_header_digest(self.runtime.db_path, shipment_id),
            }
        )
        correction_id = "ssfc_job_" + uuid4().hex
        with _connect(self.runtime.db_path) as conn:
            _ensure_correction_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                active = conn.execute(
                    f"SELECT * FROM {CORRECTION_TABLE} WHERE shipment_id=? AND status IN ('queued','running') ORDER BY requested_at DESC LIMIT 1",
                    (shipment_id,),
                ).fetchone()
                if active is not None:
                    if str(active["request_fingerprint"] or "") == request_fingerprint:
                        conn.rollback()
                        return {**_correction_row_to_dict(active), "deduplicated": True}
                    raise SupplierShipmentFactualCorrectionError(
                        "Для этой поставки уже выполняется изменение фактической даты."
                    )
                conn.execute(
                    f"""
                    INSERT INTO {CORRECTION_TABLE}(
                        correction_id,request_fingerprint,apply_fingerprint,shipment_id,
                        old_value,new_value,actor,source,reason,status,phase,progress_text,
                        requested_at,started_at,updated_at,completed_at,report_json,
                        backup_json,error_code,error_message
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        correction_id,
                        request_fingerprint,
                        "",
                        shipment_id,
                        old_value,
                        new_value,
                        actor,
                        CORRECTION_SOURCE,
                        CORRECTION_SOURCE,
                        "queued",
                        "saving",
                        "Сохраняем изменение",
                        requested_at,
                        "",
                        requested_at,
                        "",
                        "{}",
                        "{}",
                        "",
                        "",
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_job(correction_id)

    def run_job(self, correction_id: str, emit: ProgressEmitter | None = None) -> dict[str, Any]:
        job = self.get_job(correction_id)
        if job["status"] == "zero_change":
            return job
        shipment_id = str(job["shipment_id"])
        new_value = str(job["new_value"])
        actor = str(job["actor"])
        self._set_job_state(
            correction_id,
            status="running",
            phase="saving",
            progress_text="Сохраняем изменение",
            started=True,
        )
        _emit(emit, "Сохраняем изменение")
        try:
            self._set_job_state(
                correction_id,
                status="running",
                phase="recalculating",
                progress_text="Пересчитываем зависимые данные",
            )
            _emit(emit, "Пересчитываем зависимые данные")
            dry_run = self.dry_run(
                shipment_id=shipment_id,
                new_actual_shipment_date=new_value,
                actor=actor,
                expected_old_value=str(job["old_value"]),
                require_cross_cutover_rebuild=True,
            )
            self._set_job_state(
                correction_id,
                status="running",
                phase="verifying",
                progress_text="Проверяем результат",
                apply_fingerprint=str(dry_run["fingerprint"]),
                report=dry_run,
            )
            _emit(emit, "Проверяем результат")
            result = self.apply(
                shipment_id=shipment_id,
                new_actual_shipment_date=new_value,
                actor=actor,
                fingerprint=str(dry_run["fingerprint"]),
                backup_dir=self.runtime.runtime_dir / "backups" / "supplier_factual_date_corrections",
                expected_old_value=str(job["old_value"]),
                correction_id=correction_id,
                require_cross_cutover_rebuild=True,
            )
            self._set_job_state(
                correction_id,
                status="success",
                phase="completed",
                progress_text="Изменение сохранено и проверено",
                report=result,
                backup=result.get("backup"),
                completed=True,
            )
            return self.get_job(correction_id)
        except Exception as exc:
            safe_message = _safe_error_message(exc)
            self._set_job_state(
                correction_id,
                status="error",
                phase="failed",
                progress_text="Изменение не применено",
                error_code=type(exc).__name__,
                error_message=safe_message,
                completed=True,
            )
            raise SupplierShipmentFactualCorrectionError(safe_message) from exc

    def dry_run(
        self,
        *,
        shipment_id: str,
        new_actual_shipment_date: Any,
        actor: str,
        expected_old_value: str | None = None,
        expected_invoice_no: str | None = None,
        expected_invoice_document_id: str | None = None,
        require_cross_cutover_rebuild: bool = True,
        historical_status_change: Mapping[str, Any] | None = None,
        financial_document_confirmation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._candidate(
            shipment_id=shipment_id,
            new_actual_shipment_date=new_actual_shipment_date,
            actor=actor,
            expected_old_value=expected_old_value,
            expected_invoice_no=expected_invoice_no,
            expected_invoice_document_id=expected_invoice_document_id,
            require_cross_cutover_rebuild=require_cross_cutover_rebuild,
            historical_status_change=historical_status_change,
            financial_document_confirmation=financial_document_confirmation,
        ) as candidate:
            return dict(candidate["report"])

    @contextmanager
    def candidate(
        self,
        *,
        shipment_id: str,
        new_actual_shipment_date: Any,
        actor: str,
        expected_old_value: str | None = None,
        expected_invoice_no: str | None = None,
        expected_invoice_document_id: str | None = None,
        require_cross_cutover_rebuild: bool = True,
        historical_status_change: Mapping[str, Any] | None = None,
        financial_document_confirmation: Mapping[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Expose the verified disposable candidate to chained dry-run planners."""

        with self._candidate(
            shipment_id=shipment_id,
            new_actual_shipment_date=new_actual_shipment_date,
            actor=actor,
            expected_old_value=expected_old_value,
            expected_invoice_no=expected_invoice_no,
            expected_invoice_document_id=expected_invoice_document_id,
            require_cross_cutover_rebuild=require_cross_cutover_rebuild,
            historical_status_change=historical_status_change,
            financial_document_confirmation=financial_document_confirmation,
        ) as candidate:
            yield candidate

    def apply(
        self,
        *,
        shipment_id: str,
        new_actual_shipment_date: Any,
        actor: str,
        fingerprint: str,
        backup_dir: Path,
        expected_old_value: str | None = None,
        expected_invoice_no: str | None = None,
        expected_invoice_document_id: str | None = None,
        correction_id: str | None = None,
        require_cross_cutover_rebuild: bool = True,
        historical_status_change: Mapping[str, Any] | None = None,
        financial_document_confirmation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        approved_fingerprint = _required_text(fingerprint, "fingerprint")
        backup_root = Path(backup_dir)
        with self._candidate(
            shipment_id=shipment_id,
            new_actual_shipment_date=new_actual_shipment_date,
            actor=actor,
            expected_old_value=expected_old_value,
            expected_invoice_no=expected_invoice_no,
            expected_invoice_document_id=expected_invoice_document_id,
            require_cross_cutover_rebuild=require_cross_cutover_rebuild,
            historical_status_change=historical_status_change,
            financial_document_confirmation=financial_document_confirmation,
        ) as candidate:
            report = dict(candidate["report"])
            historical_plan = report.get("historical_status_change")
            financial_document_plan = report.get("financial_document_confirmation")
            target_financial_document_ids = (
                [str(financial_document_plan["document_id"])]
                if financial_document_plan
                else []
            )
            target_shipment_ids = [shipment_id]
            if historical_plan:
                target_shipment_ids.append(str(historical_plan["shipment_id"]))
            if str(report["fingerprint"]) != approved_fingerprint:
                raise ValueError("apply requires the exact current dry-run fingerprint")
            if not report["would_change"]:
                return {
                    **report,
                    "mode": "apply",
                    "applied": False,
                    "backup": None,
                    "post_run": {"changed": 0, "idempotent": True},
                }
            source_db = self.runtime.db_path
            source_inode = source_db.stat().st_ino
            backup_path = backup_root / (
                f"{source_db.stem}.supplier-factual-date-backup-"
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
            )
            backup = _sqlite_backup(source_db, backup_path)
            materialized = (
                _read_canonical_tables(Path(candidate["db_path"]))
                if report.get("rebuild", {}).get("status") == "ok"
                else {}
            )
            applied_at = self.timestamp_factory()
            correction_identity = correction_id or "ssfc_" + approved_fingerprint[:24]
            self._inject_failure("before_transaction")
            source_engine = CanonicalCostEngine(runtime=self.runtime)
            with _connect(source_db) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if report["target_before_digest"] != _target_headers_digest_conn(
                        conn, target_shipment_ids
                    ):
                        raise ValueError("optimistic target shipment drift")
                    if report["legacy_pre_cutover_digest"] != _legacy_pre_cutover_digest_conn(conn):
                        raise ValueError("optimistic pre-cutover digest drift")
                    if report["canonical_before_digest"] != _canonical_digest_conn(
                        conn,
                        date_from=CUTOVER_DATE,
                        date_to=report["scope"]["date_to"],
                    ):
                        raise ValueError("optimistic canonical input drift")
                    current_anomalies = source_engine.source_anomaly_preflight(
                        date_to=report["scope"]["date_to"]
                    )
                    if (
                        current_anomalies.get("fingerprint")
                        != report["source_anomaly_preflight"].get("fingerprint")
                    ):
                        raise ValueError("optimistic canonical source evidence drift")
                    target_nm_ids = report["reconciliation"].get("target_nm_ids") or []
                    if financial_document_plan:
                        current_financial_plan = (
                            _prepare_financial_document_confirmation_conn(
                                conn,
                                document_id=str(financial_document_plan["document_id"]),
                                actor=actor,
                                target_shipment_id=shipment_id,
                            )
                        )
                        for field in (
                            "evidence_fingerprint",
                            "before_digest",
                            "previous_status",
                        ):
                            if current_financial_plan[field] != financial_document_plan[field]:
                                raise ValueError(
                                    "optimistic financial document evidence drift"
                                )
                    source_collateral_before_digest = _non_target_digest_many_conn(
                        conn,
                        target_shipment_ids,
                        target_financial_document_ids=target_financial_document_ids,
                    )
                    canonical_non_target_before_digest = (
                        _canonical_non_target_digest_conn(conn, target_nm_ids)
                    )
                    expected_rollforward = report["expected_canonical_rollforward"]
                    if (
                        canonical_non_target_before_digest
                        != expected_rollforward["before_digest"]
                    ):
                        raise ValueError("optimistic canonical non-target input drift")
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_supplier_shipments
                        SET actual_shipment_date=?, order_status=?, updated_at=?
                        WHERE shipment_id=?
                        """,
                        (
                            report["new_value"] or None,
                            report["derived_status"]["order_status"],
                            applied_at,
                            shipment_id,
                        ),
                    )
                    if historical_plan:
                        conn.execute(
                            """
                            UPDATE sheet_vitrina_v1_supplier_shipments
                            SET historical_status_exception=?,order_status=?,updated_at=?
                            WHERE shipment_id=?
                            """,
                            (
                                historical_plan["new_exception"],
                                historical_plan["derived_status"]["order_status"],
                                applied_at,
                                historical_plan["shipment_id"],
                            ),
                        )
                    if financial_document_plan:
                        if financial_document_plan["previous_status"] != "confirmed":
                            updated = conn.execute(
                                """
                                UPDATE sheet_vitrina_v1_supplier_financial_documents
                                SET parse_status='confirmed',updated_at=?
                                WHERE document_id=? AND supplier_order_id=? AND parse_status=?
                                """,
                                (
                                    applied_at,
                                    financial_document_plan["document_id"],
                                    financial_document_plan["shipment_id"],
                                    financial_document_plan["previous_status"],
                                ),
                            )
                            if int(updated.rowcount or 0) != 1:
                                raise ValueError(
                                    "financial document confirmation identity drift in transaction"
                                )
                    if materialized:
                        ensure_canonical_cost_schema(conn)
                        _replace_canonical_tables(conn, materialized)
                    _ensure_correction_schema(conn)
                    if historical_plan:
                        _ensure_historical_status_schema(conn)
                        event_id = "sshse_" + approved_fingerprint[:24]
                        conn.execute(
                            f"""
                            INSERT INTO {HISTORICAL_STATUS_EVENT_TABLE}(
                                event_id,shipment_id,exception_code,action,
                                previous_exception,new_exception,reason,provenance,
                                actor,evidence_fingerprint,request_fingerprint,
                                apply_fingerprint,reverses_event_id,reversible,status,
                                created_at,updated_at,metadata_json
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(event_id) DO NOTHING
                            """,
                            (
                                event_id,
                                historical_plan["shipment_id"],
                                historical_plan["exception_code"],
                                historical_plan["action"],
                                historical_plan["previous_exception"],
                                historical_plan["new_exception"],
                                historical_plan["reason"],
                                historical_plan["provenance"],
                                actor,
                                historical_plan["evidence_fingerprint"],
                                report["request_fingerprint"],
                                approved_fingerprint,
                                historical_plan.get("reverses_event_id") or "",
                                1,
                                "success",
                                applied_at,
                                applied_at,
                                _json({
                                    "no_acceptance_movement": True,
                                    "no_ff_cost_layer": True,
                                    "factual_dates_unchanged": True,
                                    "reversible_to": historical_plan[
                                        "previous_exception"
                                    ],
                                }),
                            ),
                        )
                    conn.execute(
                        f"""
                        INSERT INTO {CORRECTION_TABLE}(
                            correction_id,request_fingerprint,apply_fingerprint,shipment_id,
                            old_value,new_value,actor,source,reason,status,phase,progress_text,
                            requested_at,started_at,updated_at,completed_at,report_json,
                            backup_json,error_code,error_message
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(correction_id) DO UPDATE SET
                            apply_fingerprint=excluded.apply_fingerprint,
                            status='success',phase='completed',progress_text=excluded.progress_text,
                            updated_at=excluded.updated_at,completed_at=excluded.completed_at,
                            report_json=excluded.report_json,backup_json=excluded.backup_json,
                            error_code='',error_message=''
                        """,
                        (
                            correction_identity,
                            report["request_fingerprint"],
                            approved_fingerprint,
                            shipment_id,
                            report["old_value"],
                            report["new_value"],
                            actor,
                            report["source"],
                            report["reason"],
                            "success",
                            "completed",
                            "Изменение сохранено и проверено",
                            applied_at,
                            applied_at,
                            applied_at,
                            applied_at,
                            _json(report),
                            _json(backup),
                            "",
                            "",
                        ),
                    )
                    if report["target_after_digest"] != _target_headers_digest_conn(
                        conn, target_shipment_ids
                    ):
                        raise ValueError("candidate target header digest mismatch in transaction")
                    if report["candidate_canonical_digest"] != _canonical_digest_conn(
                        conn,
                        date_from=CUTOVER_DATE,
                        date_to=report["scope"]["date_to"],
                    ):
                        raise ValueError("candidate canonical digest mismatch in transaction")
                    if financial_document_plan:
                        confirmed_financial_plan = (
                            _prepare_financial_document_confirmation_conn(
                                conn,
                                document_id=str(financial_document_plan["document_id"]),
                                actor=actor,
                                target_shipment_id=shipment_id,
                            )
                        )
                        if (
                            confirmed_financial_plan["previous_status"] != "confirmed"
                            or confirmed_financial_plan["evidence_fingerprint"]
                            != financial_document_plan["evidence_fingerprint"]
                            or confirmed_financial_plan["before_digest"]
                            != financial_document_plan["after_digest"]
                        ):
                            raise ValueError(
                                "financial document confirmation verification failed in transaction"
                            )
                    source_collateral_after_digest = _non_target_digest_many_conn(
                        conn,
                        target_shipment_ids,
                        target_financial_document_ids=target_financial_document_ids,
                    )
                    if (
                        source_collateral_before_digest
                        != source_collateral_after_digest
                    ):
                        raise ValueError("apply changed source collateral rows in transaction")
                    canonical_non_target_after_digest = (
                        _canonical_non_target_digest_conn(conn, target_nm_ids)
                    )
                    if (
                        canonical_non_target_after_digest
                        != expected_rollforward["after_digest"]
                    ):
                        raise ValueError(
                            "apply canonical non-target output differs from approved rollforward"
                        )
                    if report["legacy_pre_cutover_digest"] != _legacy_pre_cutover_digest_conn(conn):
                        raise ValueError("pre-cutover history changed in transaction")
                    self._inject_failure("before_commit")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            try:
                self._inject_failure("before_post_verify")
                if source_db.stat().st_ino != source_inode:
                    raise ValueError("live SQLite inode changed")
                if _integrity_check(source_db) != "ok":
                    raise ValueError("post-apply integrity check failed")
                post = (
                    _post_rebuild(self.runtime, date_to=report["scope"]["date_to"])
                    if report.get("rebuild", {}).get("status") == "ok"
                    else {"changed": 0, "idempotent": True, "status": "not_materialized"}
                )
                if post["changed"] != 0:
                    raise ValueError("post-apply rebuild was not zero-change")
                if report["legacy_pre_cutover_digest"] != _legacy_pre_cutover_digest(source_db):
                    raise ValueError("post-apply pre-cutover digest mismatch")
                self._inject_failure("after_post_verify")
            except Exception:
                _restore_backup_in_place(backup_path, source_db)
                if source_db.stat().st_ino != source_inode or _integrity_check(source_db) != "ok":
                    raise ValueError("post-apply restore verification failed")
                raise
            return {
                **report,
                "mode": "apply",
                "applied": True,
                "backup": backup,
                "post_run": post,
            }

    def get_job(self, correction_id: str) -> dict[str, Any]:
        correction_id = _required_text(correction_id, "correction_id")
        with _connect(self.runtime.db_path) as conn:
            if not _table_exists(conn, CORRECTION_TABLE):
                raise ValueError(f"supplier factual correction not found: {correction_id}")
            row = conn.execute(
                f"SELECT * FROM {CORRECTION_TABLE} WHERE correction_id=?",
                (correction_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"supplier factual correction not found: {correction_id}")
        return _correction_row_to_dict(row)

    def latest_for_shipment(self, shipment_id: str) -> dict[str, Any] | None:
        with _connect(self.runtime.db_path) as conn:
            if not _table_exists(conn, CORRECTION_TABLE):
                return None
            row = conn.execute(
                f"SELECT * FROM {CORRECTION_TABLE} WHERE shipment_id=? ORDER BY requested_at DESC,correction_id DESC LIMIT 1",
                (shipment_id,),
            ).fetchone()
        return _correction_row_to_dict(row) if row is not None else None

    def prepare_backup(self, backup_dir: Path) -> dict[str, Any]:
        backup_root = Path(backup_dir)
        source_db = self.runtime.db_path
        backup_path = backup_root / (
            f"{source_db.stem}.supplier-factual-date-preflight-backup-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        )
        return _sqlite_backup(source_db, backup_path)

    @contextmanager
    def _candidate(
        self,
        *,
        shipment_id: str,
        new_actual_shipment_date: Any,
        actor: str,
        expected_old_value: str | None,
        expected_invoice_no: str | None,
        expected_invoice_document_id: str | None,
        require_cross_cutover_rebuild: bool,
        historical_status_change: Mapping[str, Any] | None,
        financial_document_confirmation: Mapping[str, Any] | None,
    ) -> Iterator[dict[str, Any]]:
        shipment_id = _required_text(shipment_id, "shipment_id")
        actor = _required_text(actor or "operator", "actor")
        source_db = self.runtime.db_path
        if not source_db.is_file():
            raise ValueError("runtime SQLite database does not exist")
        raw_header = self._raw_header(shipment_id)
        if raw_header is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        old_value = str(raw_header.get("actual_shipment_date") or "").strip()
        new_value = str(new_actual_shipment_date or "").strip()
        if expected_old_value is not None and old_value != str(expected_old_value or "").strip():
            raise ValueError(
                f"actual_shipment_date drift: expected {expected_old_value!r}, got {old_value!r}"
            )
        if expected_invoice_no is not None and str(raw_header.get("invoice_no") or "") != expected_invoice_no:
            raise ValueError("shipment invoice_no does not match the authorized correction")
        if (
            expected_invoice_document_id is not None
            and str(raw_header.get("invoice_document_id") or "") != expected_invoice_document_id
        ):
            raise ValueError("shipment invoice_document_id does not match the authorized correction")
        adoption_requested = bool(
            old_value == new_value
            and expected_invoice_no
            == AUTHORIZED_FACTUAL_ADOPTION_IDENTITY["invoice_no"]
            and expected_invoice_document_id
            == AUTHORIZED_FACTUAL_ADOPTION_IDENTITY["invoice_document_id"]
        )
        if adoption_requested:
            for field, expected in AUTHORIZED_FACTUAL_ADOPTION_IDENTITY.items():
                if str(raw_header.get(field) or "") != expected:
                    raise ValueError(
                        f"historical factual adoption exact identity mismatch: {field}"
                    )
        operation_timestamp = self.timestamp_factory()
        business_today = supplier_business_today(timestamp=operation_timestamp)
        historical_plan = _prepare_historical_status_change(
            source_db,
            historical_status_change,
            actor=actor,
            business_today=business_today,
        )
        financial_document_plan = _prepare_financial_document_confirmation(
            source_db,
            financial_document_confirmation,
            actor=actor,
            target_shipment_id=shipment_id,
        )
        target_financial_document_ids = (
            [str(financial_document_plan["document_id"])]
            if financial_document_plan is not None
            else []
        )
        financial_canonical_before = (
            _canonical_financial_document_effect(
                source_db, str(financial_document_plan["document_id"])
            )
            if financial_document_plan is not None
            else None
        )
        target_shipment_ids = [shipment_id]
        if historical_plan is not None:
            historical_shipment_id = str(historical_plan["shipment_id"])
            if historical_shipment_id == shipment_id:
                raise ValueError("historical status target must differ from factual-date target")
            target_shipment_ids.append(historical_shipment_id)
        derived = validate_supplier_factual_dates(
            actual_shipment_date=new_value,
            actual_ff_acceptance_date=raw_header.get("actual_ff_acceptance_date"),
            business_today=business_today,
            historical_status_exception=raw_header.get(
                "historical_status_exception"
            ),
        )
        historical_adoption = adoption_requested
        correction_source = (
            HISTORICAL_ADOPTION_SOURCE if historical_adoption else CORRECTION_SOURCE
        )
        target_before_digest = _target_headers_digest(
            source_db, target_shipment_ids
        )
        legacy_digest = _legacy_pre_cutover_digest(source_db)
        canonical_before = _canonical_digest(
            source_db,
            date_from=CUTOVER_DATE,
            date_to=business_today,
        )
        preflight = _preflight(source_db, shipment_id, business_today)
        historical_preflight = (
            _preflight(
                source_db,
                str(historical_plan["shipment_id"]),
                business_today,
            )
            if historical_plan is not None
            else None
        )
        target_nm_ids = sorted(
            set(preflight.get("nm_ids") or [])
            | set((historical_preflight or {}).get("nm_ids") or [])
            | set((financial_document_plan or {}).get("nm_ids") or [])
        )
        source_collateral_before_digest = _non_target_digest_many(
            source_db,
            target_shipment_ids,
            target_financial_document_ids=target_financial_document_ids,
        )
        canonical_non_target_before_digest = _canonical_non_target_digest(
            source_db, target_nm_ids
        )
        baseline_fingerprint_before = _current_baseline_fingerprint(source_db)
        stage_snapshots_before = {
            CUTOVER_DATE: _target_stage_snapshot(
                source_db,
                CUTOVER_DATE,
                nm_ids=target_nm_ids,
            ),
            business_today: _target_stage_snapshot(
                source_db,
                business_today,
                nm_ids=target_nm_ids,
            ),
        }
        legacy_dispatch_dates = {
            str(item.get("effective_date") or "")
            for item in preflight.get("legacy_supplier_dispatch") or []
            if str(item.get("effective_date") or "")
        }
        crosses_cutover = _crosses_cutover(old_value, new_value) or any(
            _crosses_cutover(value, new_value) for value in legacy_dispatch_dates
        )
        with tempfile.TemporaryDirectory(prefix="supplier-factual-date-candidate-") as temp_dir:
            candidate_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir) / "runtime")
            candidate_runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
            _sqlite_backup(source_db, candidate_runtime.db_path)
            with _connect(candidate_runtime.db_path) as conn:
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_shipments
                    SET actual_shipment_date=?, order_status=?, updated_at=?
                    WHERE shipment_id=?
                    """,
                    (new_value or None, derived.order_status, operation_timestamp, shipment_id),
                )
                if historical_plan is not None:
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_supplier_shipments
                        SET historical_status_exception=?,order_status=?,updated_at=?
                        WHERE shipment_id=?
                        """,
                        (
                            historical_plan["new_exception"],
                            historical_plan["derived_status"]["order_status"],
                            operation_timestamp,
                            historical_plan["shipment_id"],
                        ),
                    )
                if financial_document_plan is not None:
                    if financial_document_plan["previous_status"] != "confirmed":
                        updated = conn.execute(
                            """
                            UPDATE sheet_vitrina_v1_supplier_financial_documents
                            SET parse_status='confirmed',updated_at=?
                            WHERE document_id=? AND supplier_order_id=? AND parse_status=?
                            """,
                            (
                                operation_timestamp,
                                financial_document_plan["document_id"],
                                financial_document_plan["shipment_id"],
                                financial_document_plan["previous_status"],
                            ),
                        )
                        if int(updated.rowcount or 0) != 1:
                            raise ValueError(
                                "financial document confirmation candidate identity drift"
                            )
                conn.commit()
            engine = CanonicalCostEngine(
                runtime=candidate_runtime,
                timestamp_factory=lambda: operation_timestamp,
            )
            source_anomaly_preflight = engine.source_anomaly_preflight(
                date_to=business_today
            )
            if source_anomaly_preflight.get("status") != "ok":
                raise SupplierShipmentFactualCorrectionError(
                    "Canonical rebuild заблокирован: cutover_source_anomaly_preflight_blocked."
                )
            baseline = engine.current_baseline_report()
            baseline_fingerprint_after = str((baseline or {}).get("fingerprint") or "")
            if baseline_fingerprint_before != baseline_fingerprint_after:
                raise ValueError("candidate changed canonical baseline fingerprint")
            rebuild_payload: dict[str, Any]
            if baseline is None:
                if crosses_cutover and require_cross_cutover_rebuild:
                    raise SupplierShipmentFactualCorrectionError(
                        "Canonical baseline не материализован; cross-cutover correction заблокирована."
                    )
                rebuild_payload = {
                    "status": "not_materialized",
                    "component_rows_changed": 0,
                    "movement_rows_changed": 0,
                    "outstanding_rows_changed": 0,
                    "daily_rows_changed": 0,
                }
                canonical_after = canonical_before
                canonical_counts = {}
            else:
                try:
                    first = engine.rebuild(date_from=CUTOVER_DATE, date_to=business_today)
                    first_digest = _canonical_digest(
                        candidate_runtime.db_path,
                        date_from=CUTOVER_DATE,
                        date_to=business_today,
                    )
                    second = engine.rebuild(date_from=CUTOVER_DATE, date_to=business_today)
                    second_digest = _canonical_digest(
                        candidate_runtime.db_path,
                        date_from=CUTOVER_DATE,
                        date_to=business_today,
                    )
                except CanonicalCostBlocked as exc:
                    raise SupplierShipmentFactualCorrectionError(
                        f"Canonical rebuild заблокирован: {exc.code}."
                    ) from exc
                changed_second = sum(
                    int(value)
                    for value in (
                        second.component_rows_changed,
                        second.movement_rows_changed,
                        second.outstanding_rows_changed,
                        second.daily_rows_changed,
                    )
                )
                if first_digest != second_digest or changed_second:
                    raise ValueError("candidate second rebuild is not zero-change idempotent")
                rebuild_payload = {"status": "ok", **asdict(first), "second_run_changed": 0}
                canonical_after = first_digest
                canonical_counts = _canonical_changed_counts(source_db, candidate_runtime.db_path)
            financial_canonical_after = (
                _canonical_financial_document_effect(
                    candidate_runtime.db_path,
                    str(financial_document_plan["document_id"]),
                )
                if financial_document_plan is not None
                else None
            )
            if financial_document_plan is not None:
                accounting_proof = financial_document_plan["accounting_proof"]
                if (
                    financial_canonical_after is None
                    or financial_canonical_after["component_count"]
                    != accounting_proof["event_count"]
                    or financial_canonical_after["unique_nm_id_count"]
                    != accounting_proof["unique_nm_id_count"]
                    or financial_canonical_after["recognized_capital_rub"]
                    != accounting_proof["stored_event_capital_rub"]
                    or financial_canonical_after["paid_capital_rub"]
                    != accounting_proof["stored_event_capital_rub"]
                    or financial_canonical_after["paid_equivalent_quantity"] != "0"
                    or int(rebuild_payload.get("movement_rows_changed") or 0) != 0
                    or int(rebuild_payload.get("outstanding_rows_changed") or 0) != 0
                ):
                    raise ValueError(
                        "financial document canonical accounting proof failed"
                    )
                if (
                    financial_document_plan["previous_status"] == "parsed"
                    and int((financial_canonical_before or {}).get("component_count") or 0)
                    != 0
                ):
                    raise ValueError(
                        "financial document was already present in canonical components"
                    )
            reconciliation = (
                _candidate_reconciliation(
                    candidate_runtime.db_path,
                    business_today,
                    nm_ids=target_nm_ids,
                )
                if baseline is not None
                else {
                    "status": "not_materialized",
                    "as_of_date": business_today,
                    "target_nm_ids": target_nm_ids,
                    "stages": {},
                }
            )
            stage_snapshots_after = {
                CUTOVER_DATE: _target_stage_snapshot(
                    candidate_runtime.db_path,
                    CUTOVER_DATE,
                    nm_ids=target_nm_ids,
                ),
                business_today: _target_stage_snapshot(
                    candidate_runtime.db_path,
                    business_today,
                    nm_ids=target_nm_ids,
                ),
            }
            reconciliation["target_before"] = stage_snapshots_before
            reconciliation["target_after"] = stage_snapshots_after
            reconciliation["target_delta"] = _stage_snapshot_delta(
                stage_snapshots_before,
                stage_snapshots_after,
            )
            if financial_document_plan is not None:
                candidate_financial_plan = _prepare_financial_document_confirmation(
                    candidate_runtime.db_path,
                    {"document_id": financial_document_plan["document_id"]},
                    actor=actor,
                    target_shipment_id=shipment_id,
                )
                if (
                    candidate_financial_plan is None
                    or candidate_financial_plan["previous_status"] != "confirmed"
                    or candidate_financial_plan["evidence_fingerprint"]
                    != financial_document_plan["evidence_fingerprint"]
                    or candidate_financial_plan["before_digest"]
                    != financial_document_plan["after_digest"]
                ):
                    raise ValueError(
                        "financial document confirmation candidate verification failed"
                    )
            target_after_digest = _target_headers_digest(
                candidate_runtime.db_path, target_shipment_ids
            )
            source_collateral_candidate_digest = _non_target_digest_many(
                candidate_runtime.db_path,
                target_shipment_ids,
                target_financial_document_ids=target_financial_document_ids,
            )
            canonical_non_target_candidate_digest = _canonical_non_target_digest(
                candidate_runtime.db_path, target_nm_ids
            )
            collateral_changes = _candidate_collateral_change_report(
                source_db,
                candidate_runtime.db_path,
                shipment_ids=target_shipment_ids,
                target_nm_ids=target_nm_ids,
                target_financial_document_ids=target_financial_document_ids,
            )
            source_collateral_changes = [
                item
                for item in collateral_changes["changes"]
                if item["scope"] == "source_rows"
            ]
            if (
                source_collateral_before_digest
                != source_collateral_candidate_digest
                or source_collateral_changes
            ):
                raise SupplierShipmentCandidateCollateralDrift(collateral_changes)
            canonical_rollforward = _expected_canonical_rollforward_report(
                before_digest=canonical_non_target_before_digest,
                after_digest=canonical_non_target_candidate_digest,
                changes=[
                    item
                    for item in collateral_changes["changes"]
                    if item["scope"] == "canonical_rows_outside_target_skus"
                ],
            )
            if legacy_digest != _legacy_pre_cutover_digest(candidate_runtime.db_path):
                raise ValueError("candidate changed pre-cutover history")
            request_fingerprint = _hash(
                {
                    "shipment_id": shipment_id,
                    "old_value": old_value,
                    "new_value": new_value,
                    "actor": actor,
                    "source": correction_source,
                    "target_before_digest": target_before_digest,
                    "historical_status_change": historical_plan,
                    "financial_document_confirmation": financial_document_plan,
                    "financial_document_canonical_before": financial_canonical_before,
                    "financial_document_canonical_after": financial_canonical_after,
                }
            )
            dependency_closure = {
                "target_before_digest": target_before_digest,
                "preflight": preflight,
                "historical_preflight": historical_preflight,
                "historical_status_change": historical_plan,
                "financial_document_confirmation": financial_document_plan,
                "financial_document_canonical_before": financial_canonical_before,
                "financial_document_canonical_after": financial_canonical_after,
                "source_anomaly_preflight": source_anomaly_preflight,
                "baseline_fingerprint": baseline_fingerprint_before,
                "legacy_pre_cutover_digest": legacy_digest,
                "canonical_before_digest": canonical_before,
                "candidate_canonical_digest": canonical_after,
                "expected_canonical_rollforward": canonical_rollforward,
                "rebuild": rebuild_payload,
                "target_before": stage_snapshots_before,
                "target_after": stage_snapshots_after,
                "reconciliation": reconciliation,
            }
            dependency_closure_digest = _hash(dependency_closure)
            plan = {
                "contract_name": "supplier_shipment_factual_reconciliation_v4",
                "status": "ready",
                "scope": {"date_from": CUTOVER_DATE, "date_to": business_today},
                "shipment_id": shipment_id,
                "invoice_no": str(raw_header.get("invoice_no") or ""),
                "invoice_document_id": str(raw_header.get("invoice_document_id") or ""),
                "old_value": old_value,
                "new_value": new_value,
                "actor": actor,
                "source": correction_source,
                "reason": correction_source,
                "factual_date_already_correct_before_apply": historical_adoption,
                "historical_status_change": historical_plan,
                "financial_document_confirmation": financial_document_plan,
                "financial_document_canonical_before": financial_canonical_before,
                "financial_document_canonical_after": financial_canonical_after,
                "crosses_cutover": crosses_cutover,
                "baseline_fingerprint_before": baseline_fingerprint_before,
                "baseline_fingerprint_after": baseline_fingerprint_after,
                "derived_status": derived.to_dict(),
                "request_fingerprint": request_fingerprint,
                "target_before_digest": target_before_digest,
                "target_after_digest": target_after_digest,
                "dependency_closure_digest": dependency_closure_digest,
                "legacy_pre_cutover_digest": legacy_digest,
                "canonical_before_digest": canonical_before,
                "candidate_canonical_digest": canonical_after,
                "expected_canonical_rollforward": canonical_rollforward,
                "canonical_changed_rows": canonical_counts,
                "rebuild": rebuild_payload,
                "reconciliation": reconciliation,
                "preflight": preflight,
                "historical_preflight": historical_preflight,
                "source_anomaly_preflight": source_anomaly_preflight,
                "affected_read_models": [
                    "supplier shipment detail/list",
                    "shipment registry matrix",
                    "factory-order supplier inbound",
                    "stock report supplier stages",
                    "SKU management nearest supplier inbound",
                    "canonical daily cost/capital state",
                ],
                "preserved_sources": [
                    "supplier payments and CNY ledger",
                    "invoice amounts and documents",
                    "FF acceptance and stock receipt",
                    "landed-cost components",
                    "WB supplies",
                    "other supplier shipments",
                    "legacy own-capital evidence",
                ],
            }
            fingerprint = _hash(plan)
            successful = _successful_correction_by_values(source_db, shipment_id, new_value)
            historical_successful = (
                _successful_historical_status_change(source_db, historical_plan)
                if historical_plan is not None
                else True
            )
            report = {
                **plan,
                "mode": "dry-run",
                "fingerprint": fingerprint,
                "would_change": bool(
                    target_before_digest != target_after_digest
                    or canonical_before != canonical_after
                    or not successful
                    or not historical_successful
                    or bool(
                        financial_document_plan is not None
                        and financial_document_plan["previous_status"] != "confirmed"
                    )
                ),
                "applied": False,
                "backup": None,
                "post_run": None,
                "integrity_check": _integrity_check(source_db),
                "collateral_invariant": {
                    "contract_name": "supplier_correction_transaction_collateral_v2",
                    "source_scope": "all protected non-target source rows",
                    "source_digest": source_collateral_before_digest,
                    "candidate_digest": source_collateral_candidate_digest,
                    "candidate_unchanged": source_collateral_before_digest
                    == source_collateral_candidate_digest,
                    "candidate_source_digest": source_collateral_candidate_digest,
                    "candidate_source_unchanged": source_collateral_before_digest
                    == source_collateral_candidate_digest,
                    "canonical_scope": "all canonical rows outside target SKU closure",
                    "canonical_before_digest": canonical_non_target_before_digest,
                    "canonical_candidate_digest": canonical_non_target_candidate_digest,
                    "expected_canonical_rollforward_fingerprint": canonical_rollforward[
                        "fingerprint"
                    ],
                    "source_snapshot_included_in_human_fingerprint": False,
                    "expected_canonical_rollforward_included_in_human_fingerprint": True,
                    "apply_contract": (
                        "BEGIN IMMEDIATE source before/after equality plus exact "
                        "canonical before/candidate-after equality"
                    ),
                },
            }
            yield {
                "db_path": candidate_runtime.db_path,
                "report": report,
            }

    def _inject_failure(self, phase: str) -> None:
        if self.failure_injector is not None:
            self.failure_injector(phase)

    def _raw_header(self, shipment_id: str) -> dict[str, Any] | None:
        with _connect(self.runtime.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
                (shipment_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _set_job_state(
        self,
        correction_id: str,
        *,
        status: str,
        phase: str,
        progress_text: str,
        started: bool = False,
        completed: bool = False,
        apply_fingerprint: str | None = None,
        report: Mapping[str, Any] | None = None,
        backup: Mapping[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        now = self.timestamp_factory()
        with _connect(self.runtime.db_path) as conn:
            _ensure_correction_schema(conn)
            conn.execute(
                f"""
                UPDATE {CORRECTION_TABLE}
                SET status=?,phase=?,progress_text=?,updated_at=?,
                    started_at=CASE WHEN ? THEN ? ELSE started_at END,
                    completed_at=CASE WHEN ? THEN ? ELSE completed_at END,
                    apply_fingerprint=COALESCE(?,apply_fingerprint),
                    report_json=COALESCE(?,report_json),backup_json=COALESCE(?,backup_json),
                    error_code=?,error_message=?
                WHERE correction_id=?
                """,
                (
                    status,
                    phase,
                    progress_text,
                    now,
                    1 if started else 0,
                    now,
                    1 if completed else 0,
                    now,
                    apply_fingerprint,
                    _json(report) if report is not None else None,
                    _json(backup) if backup is not None else None,
                    error_code,
                    error_message,
                    correction_id,
                ),
            )
            conn.commit()


def _prepare_historical_status_change(
    db_path: Path,
    change: Mapping[str, Any] | None,
    *,
    actor: str,
    business_today: str,
) -> dict[str, Any] | None:
    if not change:
        return None
    shipment_id = _required_text(change.get("shipment_id"), "historical shipment_id")
    action = _required_text(change.get("action") or "activate", "historical action")
    if action not in {"activate", "revert"}:
        raise ValueError("historical action must be activate or revert")
    exception_code = _required_text(
        change.get("exception_code")
        or HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE,
        "historical exception_code",
    )
    if exception_code != HISTORICAL_STATUS_EXCEPTION_LEGACY_FF_ACCEPTED_WITHOUT_DATE:
        raise ValueError("unsupported historical status exception")
    expected_invoice_no = _required_text(
        change.get("expected_invoice_no"), "historical expected_invoice_no"
    )
    if expected_invoice_no != AUTHORIZED_HISTORICAL_EXCEPTION_INVOICE_NO:
        raise ValueError("historical exception is authorized exact-only for 26GN237")
    if shipment_id != AUTHORIZED_HISTORICAL_EXCEPTION_IDENTITY["shipment_id"]:
        raise ValueError("historical exception shipment_id is not in the exact policy")
    reason = _required_text(change.get("reason"), "historical reason")
    provenance = _required_text(change.get("provenance"), "historical provenance")
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"historical supplier shipment not found: {shipment_id}")
        header = dict(row)
        history_head: dict[str, Any] = {}
        if _table_exists(conn, HISTORICAL_STATUS_EVENT_TABLE):
            history_row = conn.execute(
                f"""
                SELECT event_id,action,new_exception,apply_fingerprint,created_at,
                       evidence_fingerprint,reason,provenance
                FROM {HISTORICAL_STATUS_EVENT_TABLE}
                WHERE shipment_id=? AND status='success'
                ORDER BY created_at DESC,event_id DESC LIMIT 1
                """,
                (shipment_id,),
            ).fetchone()
            if history_row is not None:
                history_head = dict(history_row)
    if str(header.get("invoice_no") or "") != expected_invoice_no:
        raise ValueError("historical shipment invoice_no identity mismatch")
    for field, expected in AUTHORIZED_HISTORICAL_EXCEPTION_IDENTITY.items():
        if field == "shipment_id":
            continue
        if str(header.get(field) or "") != str(expected):
            raise ValueError(f"historical shipment exact identity mismatch: {field}")
    expected_invoice_date = str(change.get("expected_invoice_date") or "").strip()
    if expected_invoice_date and str(header.get("invoice_date") or "") != expected_invoice_date:
        raise ValueError("historical shipment invoice_date identity mismatch")
    expected_shipment_date = str(change.get("expected_shipment_date") or "").strip()
    if expected_shipment_date and str(header.get("shipment_date") or "") != expected_shipment_date:
        raise ValueError("historical shipment shipment_date identity mismatch")
    if action == "activate" and (
        str(header.get("actual_shipment_date") or "").strip()
        or str(header.get("actual_ff_acceptance_date") or "").strip()
    ):
        raise ValueError("historical accepted-without-date requires both factual dates to remain empty")
    previous = str(header.get("historical_status_exception") or "").strip()
    evidence = _historical_status_evidence(db_path, shipment_id)
    exact_repeat = bool(
        action == "activate"
        and previous == exception_code
        and str(history_head.get("action") or "") == "activate"
        and str(history_head.get("new_exception") or "") == exception_code
        and str(history_head.get("evidence_fingerprint") or "")
        == evidence["fingerprint"]
        and str(history_head.get("reason") or "") == reason
        and str(history_head.get("provenance") or "") == provenance
    )
    default_expected_previous = (
        exception_code if exact_repeat or action == "revert" else ""
    )
    expected_previous = str(
        change.get("expected_current_exception", default_expected_previous) or ""
    ).strip()
    if previous != expected_previous:
        raise ValueError(
            "historical status exception drift: "
            f"expected {expected_previous!r}, got {previous!r}"
        )
    reverses_event_id = str(change.get("reverses_event_id") or "").strip()
    if action == "revert":
        if not reverses_event_id:
            raise ValueError("historical revert requires reverses_event_id")
        with _connect(db_path) as conn:
            if not _table_exists(conn, HISTORICAL_STATUS_EVENT_TABLE):
                raise ValueError("historical revert activation event is missing")
            activation = conn.execute(
                f"""
                SELECT 1 FROM {HISTORICAL_STATUS_EVENT_TABLE}
                WHERE event_id=? AND shipment_id=? AND action='activate'
                  AND new_exception=? AND status='success'
                LIMIT 1
                """,
                (reverses_event_id, shipment_id, exception_code),
            ).fetchone()
        if activation is None:
            raise ValueError("historical revert activation identity mismatch")
    elif reverses_event_id:
        raise ValueError("historical activation cannot reverse another event")
    new_exception = exception_code if action == "activate" else ""
    derived = validate_supplier_factual_dates(
        actual_shipment_date=header.get("actual_shipment_date"),
        actual_ff_acceptance_date=header.get("actual_ff_acceptance_date"),
        business_today=business_today,
        historical_status_exception=new_exception,
    )
    expected_evidence = str(change.get("expected_evidence_fingerprint") or "").strip()
    if expected_evidence and expected_evidence != evidence["fingerprint"]:
        raise ValueError("historical status evidence fingerprint mismatch")
    return {
        "shipment_id": shipment_id,
        "invoice_no": expected_invoice_no,
        "invoice_date": str(header.get("invoice_date") or ""),
        "action": action,
        "exception_code": exception_code,
        "previous_exception": previous,
        "new_exception": new_exception,
        "reason": reason,
        "provenance": provenance,
        "actor": actor,
        "evidence_fingerprint": evidence["fingerprint"],
        "evidence_summary": evidence["summary"],
        "reverses_event_id": reverses_event_id,
        "reversible": True,
        "history_head": history_head,
        "derived_status": derived.to_dict(),
    }


def _historical_status_evidence(db_path: Path, shipment_id: str) -> dict[str, Any]:
    with _connect(db_path) as conn:
        header_row = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        if header_row is None:
            raise ValueError(f"supplier shipment not found: {shipment_id}")
        raw_header = dict(header_row)
        header = {
            key: raw_header.get(key)
            for key in (
                "shipment_id",
                "created_at",
                "shipment_date",
                "actual_shipment_date",
                "actual_ff_acceptance_date",
                "invoice_no",
                "invoice_date",
                "invoice_document_id",
                "supplier_name",
                "product_qty_total",
                "invoice_amount_total",
                "source_file_sha256",
            )
        }
        lines = [
            dict(row)
            for row in conn.execute(
                """
                SELECT line_id,line_type,sort_order,internal_nm_id,qty,amount,match_status
                FROM sheet_vitrina_v1_supplier_shipment_lines
                WHERE shipment_id=? ORDER BY sort_order,line_id
                """,
                (shipment_id,),
            ).fetchall()
        ]
        ff_operations = 0
        ff_layers = 0
        if _table_exists(conn, "sheet_vitrina_v1_ff_stock_operations"):
            ff_operations = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations WHERE source_object_id=?",
                    (shipment_id,),
                ).fetchone()[0]
            )
        if _table_exists(conn, "sheet_vitrina_v1_supplier_ff_cost_layers"):
            ff_layers = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_ff_cost_layers WHERE supplier_shipment_id=?",
                    (shipment_id,),
                ).fetchone()[0]
            )
    summary = {
        "header": header,
        "lines": lines,
        "existing_acceptance_operation_count": ff_operations,
        "existing_ff_cost_layer_count": ff_layers,
    }
    return {"fingerprint": _hash(summary), "summary": summary}


def _prepare_financial_document_confirmation(
    db_path: Path,
    change: Mapping[str, Any] | None,
    *,
    actor: str,
    target_shipment_id: str,
) -> dict[str, Any] | None:
    if not change:
        return None
    document_id = _required_text(
        change.get("document_id"), "financial confirmation document_id"
    )
    expected = AUTHORIZED_FINANCIAL_DOCUMENT_CONFIRMATION_IDENTITY
    if document_id != expected["document_id"]:
        raise ValueError("financial document confirmation is exact-only for invoice 136")
    if target_shipment_id != expected["shipment_id"]:
        raise ValueError("financial document confirmation shipment identity mismatch")
    with _connect(db_path) as conn:
        return _prepare_financial_document_confirmation_conn(
            conn,
            document_id=document_id,
            actor=actor,
            target_shipment_id=target_shipment_id,
        )


def _prepare_financial_document_confirmation_conn(
    conn: sqlite3.Connection,
    *,
    document_id: str,
    actor: str,
    target_shipment_id: str,
) -> dict[str, Any]:
    expected = AUTHORIZED_FINANCIAL_DOCUMENT_CONFIRMATION_IDENTITY
    if document_id != expected["document_id"] or target_shipment_id != expected["shipment_id"]:
        raise ValueError("financial document confirmation exact identity mismatch")
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_supplier_financial_documents "
        "WHERE document_id=? AND supplier_order_id=?",
        (document_id, target_shipment_id),
    ).fetchone()
    if row is None:
        raise ValueError("authorized financial document is missing")
    document = dict(row)
    previous_status = str(document.get("parse_status") or "").strip()
    if previous_status not in {"parsed", "confirmed"}:
        raise ValueError("authorized financial document status is not confirmable")
    for field in (
        "document_id",
        "document_type",
        "document_number",
        "document_date",
        "currency",
        "file_sha256",
    ):
        if str(document.get(field) or "") != str(expected[field]):
            raise ValueError(f"financial document exact identity mismatch: {field}")
    if str(document.get("supplier_order_id") or "") != str(expected["shipment_id"]):
        raise ValueError("financial document exact identity mismatch: shipment_id")
    if _exact_decimal(document.get("total_amount_rub")) != _exact_decimal(
        expected["total_amount_rub"]
    ):
        raise ValueError("financial document exact identity mismatch: total_amount_rub")

    expense_rows = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_financial_expense_lines "
            "WHERE financial_document_id=? ORDER BY sort_order,line_id",
            (document_id,),
        ).fetchall()
    ]
    if len(expense_rows) != 1:
        raise ValueError("authorized financial document expense-line cardinality drift")
    expense = expense_rows[0]
    expense_expectations = {
        "line_id": expected["expense_line_id"],
        "financial_document_id": document_id,
        "supplier_order_id": target_shipment_id,
        "category": expected["expense_category"],
        "stage": expected["expense_stage"],
        "currency": expected["currency"],
    }
    for field, value in expense_expectations.items():
        if str(expense.get(field) or "") != str(value):
            raise ValueError(f"financial expense line exact identity mismatch: {field}")
    if _exact_decimal(expense.get("amount_rub")) != _exact_decimal(
        expected["expense_amount_rub"]
    ):
        raise ValueError("financial expense line exact identity mismatch: amount_rub")

    product_rows = [
        dict(item)
        for item in conn.execute(
            """
            SELECT line_id,sort_order,internal_nm_id,qty,unit_price,amount,match_status
            FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id=? AND line_type='product'
            ORDER BY sort_order,line_id
            """,
            (target_shipment_id,),
        ).fetchall()
    ]
    if len(product_rows) != int(expected["event_count"]):
        raise ValueError("financial allocation product-line cardinality drift")
    normalized_products: list[dict[str, Any]] = []
    seen_nm_ids: set[int] = set()
    for item in product_rows:
        nm_id = int(item.get("internal_nm_id") or 0)
        quantity = _exact_decimal(item.get("qty"))
        amount = _exact_decimal(item.get("amount"))
        if amount <= 0:
            amount = quantity * _exact_decimal(item.get("unit_price"))
        if (
            nm_id <= 0
            or nm_id in seen_nm_ids
            or quantity <= 0
            or amount <= 0
            or str(item.get("match_status") or "")
            not in {"matched", "matched_by_compatibility"}
        ):
            raise ValueError("financial allocation product-line evidence is not exact")
        seen_nm_ids.add(nm_id)
        normalized_products.append(
            {
                "line_id": str(item.get("line_id") or ""),
                "sort_order": int(item.get("sort_order") or 0),
                "nm_id": nm_id,
                "quantity": _decimal_text(quantity),
                "invoice_value": _decimal_text(amount),
            }
        )
    total_value = sum(
        (_exact_decimal(item["invoice_value"]) for item in normalized_products),
        Decimal("0"),
    )
    expense_amount = _exact_decimal(expense.get("amount_rub"))
    remaining = expense_amount
    expected_allocations: list[dict[str, Any]] = []
    for index, item in enumerate(normalized_products, start=1):
        allocated = (
            remaining
            if index == len(normalized_products)
            else expense_amount
            * _exact_decimal(item["invoice_value"])
            / total_value
        )
        remaining -= allocated
        expected_allocations.append(
            {
                "ordinal": index,
                "nm_id": item["nm_id"],
                "capital_rub": _money_text(allocated),
            }
        )

    event_prefix = f"cost_payment:financial_expense:{document_id}:"
    events = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_own_capital_events "
            "WHERE event_id LIKE ? ESCAPE '\\' ORDER BY event_id",
            (_literal_like_prefix(event_prefix),),
        ).fetchall()
    ]
    if len(events) != int(expected["event_count"]):
        raise ValueError("financial allocation event cardinality drift")
    events_by_ordinal: dict[int, dict[str, Any]] = {}
    for event in events:
        parts = str(event.get("event_id") or "").split(":")
        if len(parts) < 2:
            raise ValueError("financial allocation event identity drift")
        try:
            ordinal = int(parts[-1])
        except ValueError as exc:
            raise ValueError("financial allocation event ordinal drift") from exc
        if ordinal in events_by_ordinal:
            raise ValueError("financial allocation duplicate event ordinal")
        events_by_ordinal[ordinal] = event

    allocation_evidence: list[dict[str, Any]] = []
    event_semantic: list[dict[str, Any]] = []
    for allocation in expected_allocations:
        event = events_by_ordinal.get(int(allocation["ordinal"]))
        if event is None:
            raise ValueError("financial allocation event sequence drift")
        expected_event_id = (
            f"{event_prefix}{allocation['nm_id']}:{allocation['ordinal']}"
        )
        exact_fields = {
            "event_id": expected_event_id,
            "event_type": "cost_payment",
            "effective_date": expected["document_date"],
            "shipment_id": target_shipment_id,
            "supply_id": "",
            "nm_id": allocation["nm_id"],
            "stage_from": "",
            "stage_to": expected["event_stage"],
            "quantity": "0",
            "confirmed_quantity": "0",
            "cost_layer_id": (
                f"expense:financial_expense:{document_id}:"
                f"{allocation['nm_id']}:{allocation['ordinal']}"
            ),
            "evidence_hash": expected["event_evidence_hash"],
        }
        for field, value in exact_fields.items():
            if field in {"nm_id"}:
                matches = int(event.get(field) or 0) == int(value)
            elif field in {"quantity", "confirmed_quantity"}:
                matches = _exact_decimal(event.get(field)) == _exact_decimal(value)
            else:
                matches = str(event.get(field) or "") == str(value)
            if not matches:
                raise ValueError(f"financial allocation event drift: {field}")
        persisted_capital = _money_text(_exact_decimal(event.get("capital_rub")))
        if persisted_capital != allocation["capital_rub"]:
            raise ValueError("financial allocation amount drift")
        allocation_evidence.append(
            {
                **allocation,
                "event_id": expected_event_id,
                "persisted_capital_rub": persisted_capital,
            }
        )
        event_semantic.append(
            {
                **exact_fields,
                "capital_rub": persisted_capital,
                "payload_fingerprint": _hash(str(event.get("payload_json") or "{}")),
                "created_at": str(event.get("created_at") or ""),
            }
        )
    stored_total = sum(
        (_exact_decimal(item["persisted_capital_rub"]) for item in allocation_evidence),
        Decimal("0"),
    )
    rounding_delta = expense_amount - stored_total
    rounding_tolerance = MONEY_QUANT * Decimal(len(allocation_evidence))
    if abs(rounding_delta) > rounding_tolerance:
        raise ValueError("financial allocation conservation drift")

    document_semantic = {
        key: value
        for key, value in document.items()
        if key not in {"updated_at", "stored_file_path", "parse_status"}
    }
    evidence = {
        "contract_name": "supplier_financial_document_confirmation_evidence_v1",
        "document": document_semantic,
        "expense_lines": expense_rows,
        "product_lines": normalized_products,
        "allocations": allocation_evidence,
        "events": event_semantic,
        "allocation_method": "product_invoice_value_proportional",
        "event_count": len(event_semantic),
        "stored_allocation_sum_rub": _money_text(stored_total),
        "document_amount_rub": _money_text(expense_amount),
        "rounding_delta_rub": _money_text(rounding_delta),
        "rounding_tolerance_rub": _money_text(rounding_tolerance),
        "quantity_delta": "0",
        "new_capital_event_count": 0,
    }
    evidence_fingerprint = _hash(evidence)
    before_digest = _hash(
        {"evidence_fingerprint": evidence_fingerprint, "parse_status": previous_status}
    )
    after_digest = _hash(
        {"evidence_fingerprint": evidence_fingerprint, "parse_status": "confirmed"}
    )
    return {
        "contract_name": "supplier_financial_document_confirmation_v1",
        "source": FINANCIAL_DOCUMENT_CONFIRMATION_SOURCE,
        "document_id": document_id,
        "shipment_id": target_shipment_id,
        "document_number": str(document.get("document_number") or ""),
        "document_date": str(document.get("document_date") or ""),
        "document_type": str(document.get("document_type") or ""),
        "file_sha256": str(document.get("file_sha256") or ""),
        "expense_line_id": str(expense.get("line_id") or ""),
        "previous_status": previous_status,
        "new_status": "confirmed",
        "actor": actor,
        "reason": "operator_confirmed_invoice_136_for_canonical_cost",
        "provenance": "user_confirmed_business_document_in_chained_reconciliation",
        "evidence_fingerprint": evidence_fingerprint,
        "before_digest": before_digest,
        "after_digest": after_digest,
        "nm_ids": sorted(seen_nm_ids),
        "accounting_proof": {
            "expense_amount_rub": _money_text(expense_amount),
            "stored_event_capital_rub": _money_text(stored_total),
            "rounding_delta_rub": _money_text(rounding_delta),
            "rounding_tolerance_rub": _money_text(rounding_tolerance),
            "event_count": len(event_semantic),
            "unique_nm_id_count": len(seen_nm_ids),
            "quantity_delta": "0",
            "new_event_count": 0,
            "event_evidence_hash": str(expected["event_evidence_hash"]),
            "allocation_method": "product_invoice_value_proportional",
            "allocations": allocation_evidence,
            "event_ids": [item["event_id"] for item in allocation_evidence],
            "all_allocations_match": True,
            "all_quantity_zero": True,
            "double_count_prevented": True,
        },
        "reversible_by_verified_chain_backup": True,
        "volatile_fields_excluded_from_evidence": ["updated_at", "stored_file_path"],
    }


def _exact_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid decimal evidence: {value!r}") from exc


def _money_text(value: Decimal) -> str:
    return _decimal_text(value.quantize(MONEY_QUANT))


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f").rstrip("0").rstrip(".")
    return text or "0"


def _preflight(db_path: Path, shipment_id: str, date_to: str) -> dict[str, Any]:
    with _connect(db_path) as conn:
        header = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
            (shipment_id,),
        ).fetchone()
        lines = conn.execute(
            "SELECT internal_nm_id,qty,amount,line_id FROM sheet_vitrina_v1_supplier_shipment_lines WHERE shipment_id=? ORDER BY sort_order,line_id",
            (shipment_id,),
        ).fetchall()
        nm_ids = sorted({int(row["internal_nm_id"]) for row in lines if row["internal_nm_id"] is not None})
        legacy_movements = []
        if _table_exists(conn, "sheet_vitrina_v1_own_capital_events"):
            legacy_movements = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT event_id,effective_date,stage_from,stage_to,nm_id,quantity,evidence_hash,created_at
                    FROM sheet_vitrina_v1_own_capital_events
                    WHERE event_id LIKE ? ESCAPE '\\'
                    ORDER BY event_id
                    """,
                    (_literal_like_prefix(f"stage_transfer:supplier_dispatch:{shipment_id}:"),),
                ).fetchall()
            ]
        canonical_rows = []
        if _table_exists(conn, "sheet_vitrina_v1_canonical_cost_daily_state") and nm_ids:
            placeholders = ",".join("?" for _ in nm_ids)
            canonical_rows = [
                dict(row)
                for row in conn.execute(
                    f"""
                    SELECT as_of_date,nm_id,stage,physical_quantity,paid_equivalent_quantity,
                           recognized_capital_rub,paid_capital_rub,cost_covered_quantity,confirmed_quantity,fingerprint
                    FROM sheet_vitrina_v1_canonical_cost_daily_state
                    WHERE as_of_date=? AND nm_id IN ({placeholders})
                    ORDER BY nm_id,stage
                    """,
                    (date_to, *nm_ids),
                ).fetchall()
            ]
    return {
        "header": dict(header) if header is not None else None,
        "line_count": len(lines),
        "nm_ids": nm_ids,
        "legacy_supplier_dispatch": legacy_movements,
        "canonical_daily_state": canonical_rows,
        "partial_state_detected": bool(
            header is not None
            and legacy_movements
            and any(
                str(row.get("effective_date") or "")
                != str(header["actual_shipment_date"] or "")
                for row in legacy_movements
            )
        ),
    }


def _post_rebuild(runtime: RegistryUploadDbBackedRuntime, *, date_to: str) -> dict[str, Any]:
    engine = CanonicalCostEngine(runtime=runtime)
    if engine.current_baseline_report() is None:
        return {"changed": 0, "idempotent": True, "status": "not_materialized"}
    result = engine.rebuild(date_from=CUTOVER_DATE, date_to=date_to)
    changed = sum(
        int(value)
        for value in (
            result.component_rows_changed,
            result.movement_rows_changed,
            result.outstanding_rows_changed,
            result.daily_rows_changed,
        )
    )
    return {"changed": changed, "idempotent": changed == 0, "result": asdict(result)}


def _canonical_changed_counts(before: Path, after: Path) -> dict[str, int]:
    before_rows = _read_canonical_tables(before)
    after_rows = _read_canonical_tables(after)
    result: dict[str, int] = {}
    for table in sorted(set(before_rows) | set(after_rows)):
        before_set = {tuple(row) for row in before_rows.get(table, ([], []))[1]}
        after_set = {tuple(row) for row in after_rows.get(table, ([], []))[1]}
        result[table] = len(before_set.symmetric_difference(after_set))
    return result


def _candidate_reconciliation(
    db_path: Path,
    as_of_date: str,
    *,
    nm_ids: list[int],
) -> dict[str, Any]:
    """Prove physical/capital/coverage invariants for the bounded candidate."""

    with _connect(db_path) as conn:
        if not _table_exists(conn, "sheet_vitrina_v1_canonical_cost_daily_state"):
            return {
                "status": "not_materialized",
                "as_of_date": as_of_date,
                "target_nm_ids": list(nm_ids),
                "stages": {},
            }
        stage_rows = conn.execute(
            """
            SELECT stage,SUM(physical_quantity+0) physical_quantity,
                   SUM(paid_equivalent_quantity+0) paid_equivalent_quantity,
                   SUM(recognized_capital_rub+0) recognized_capital_rub,
                   SUM(paid_capital_rub+0) paid_capital_rub,
                   SUM(cost_covered_quantity+0) cost_covered_quantity,
                   SUM(confirmed_quantity+0) confirmed_quantity
            FROM sheet_vitrina_v1_canonical_cost_daily_state
            WHERE as_of_date=? GROUP BY stage ORDER BY stage
            """,
            (as_of_date,),
        ).fetchall()
        ff_ledger_quantity = conn.execute(
            "SELECT COALESCE(SUM(quantity_delta+0),0) FROM sheet_vitrina_v1_ff_stock_operation_lines"
        ).fetchone()[0]
        outstanding = conn.execute(
            """
            SELECT COALESCE(SUM(open_quantity+0),0) quantity,
                   COALESCE(SUM((open_quantity+0)*(cost_coverage_share+0)),0) covered_quantity,
                   COALESCE(SUM(paid_equivalent_quantity+0),0) paid_equivalent_quantity
            FROM sheet_vitrina_v1_canonical_cost_wb_outstanding_layers
            WHERE is_current=1
            """
        ).fetchone()
        target_rows: list[sqlite3.Row] = []
        if nm_ids:
            placeholders = ",".join("?" for _ in nm_ids)
            target_rows = conn.execute(
                f"""
                SELECT nm_id,stage,physical_quantity,paid_equivalent_quantity,
                       recognized_capital_rub,paid_capital_rub,cost_covered_quantity,
                       confirmed_quantity,recognized_unit_cost_rub,paid_unit_cost_rub
                FROM sheet_vitrina_v1_canonical_cost_daily_state
                WHERE as_of_date=? AND nm_id IN ({placeholders})
                ORDER BY nm_id,stage
                """,
                (as_of_date, *nm_ids),
            ).fetchall()
    stages = {str(row["stage"]): dict(row) for row in stage_rows}
    ff_candidate_quantity = float(stages.get("FF", {}).get("physical_quantity") or 0)
    if abs(ff_candidate_quantity - float(ff_ledger_quantity or 0)) > 0.000001:
        raise ValueError("candidate FF quantity does not reconcile to ff_stock_ledger")
    invariant_failures: list[str] = []
    for row in stage_rows:
        physical = float(row["physical_quantity"] or 0)
        paid_equivalent = float(row["paid_equivalent_quantity"] or 0)
        covered = float(row["cost_covered_quantity"] or 0)
        confirmed = float(row["confirmed_quantity"] or 0)
        if min(physical, paid_equivalent, covered, confirmed) < -0.000001:
            invariant_failures.append(f"negative_quantity:{row['stage']}")
        if paid_equivalent - physical > 0.000001:
            invariant_failures.append(f"paid_equivalent_exceeds_physical:{row['stage']}")
        if covered - physical > 0.000001:
            invariant_failures.append(f"coverage_exceeds_physical:{row['stage']}")
        if confirmed - physical > 0.000001:
            invariant_failures.append(f"confirmed_exceeds_physical:{row['stage']}")
    if invariant_failures:
        raise ValueError("candidate stage/capital/coverage invariants failed: " + ",".join(invariant_failures))
    return {
        "status": "ok",
        "as_of_date": as_of_date,
        "target_nm_ids": list(nm_ids),
        "stages": {stage: stages.get(stage, {}) for stage in STAGES},
        "target_daily_state": [dict(row) for row in target_rows],
        "ff_ledger_quantity": float(ff_ledger_quantity or 0),
        "ff_candidate_quantity": ff_candidate_quantity,
        "wb_outstanding": dict(outstanding),
        "invariant_failures": [],
    }


def _current_baseline_fingerprint(db_path: Path) -> str:
    with _connect(db_path) as conn:
        if not _table_exists(conn, "sheet_vitrina_v1_canonical_cost_baseline_versions"):
            return ""
        row = conn.execute(
            """
            SELECT fingerprint FROM sheet_vitrina_v1_canonical_cost_baseline_versions
            WHERE is_current=1 LIMIT 1
            """
        ).fetchone()
    return str(row["fingerprint"] or "") if row is not None else ""


def _target_stage_snapshot(
    db_path: Path,
    as_of_date: str,
    *,
    nm_ids: list[int],
) -> dict[str, dict[str, float]]:
    if not nm_ids:
        return {}
    with _connect(db_path) as conn:
        if not _table_exists(conn, "sheet_vitrina_v1_canonical_cost_daily_state"):
            return {}
        placeholders = ",".join("?" for _ in nm_ids)
        rows = conn.execute(
            f"""
            SELECT stage,SUM(physical_quantity+0) physical_quantity,
                   SUM(paid_equivalent_quantity+0) paid_equivalent_quantity,
                   SUM(recognized_capital_rub+0) recognized_capital_rub,
                   SUM(paid_capital_rub+0) paid_capital_rub,
                   SUM(cost_covered_quantity+0) cost_covered_quantity,
                   SUM(confirmed_quantity+0) confirmed_quantity
            FROM sheet_vitrina_v1_canonical_cost_daily_state
            WHERE as_of_date=? AND nm_id IN ({placeholders})
            GROUP BY stage ORDER BY stage
            """,
            (as_of_date, *nm_ids),
        ).fetchall()
    fields = (
        "physical_quantity",
        "paid_equivalent_quantity",
        "recognized_capital_rub",
        "paid_capital_rub",
        "cost_covered_quantity",
        "confirmed_quantity",
    )
    return {
        str(row["stage"]): {field: float(row[field] or 0) for field in fields}
        for row in rows
    }


def _stage_snapshot_delta(
    before: Mapping[str, Mapping[str, Mapping[str, float]]],
    after: Mapping[str, Mapping[str, Mapping[str, float]]],
) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {}
    for as_of_date in sorted(set(before) | set(after)):
        before_stages = before.get(as_of_date, {})
        after_stages = after.get(as_of_date, {})
        stage_delta: dict[str, dict[str, float]] = {}
        for stage in sorted(set(before_stages) | set(after_stages)):
            before_values = before_stages.get(stage, {})
            after_values = after_stages.get(stage, {})
            fields = sorted(set(before_values) | set(after_values))
            stage_delta[stage] = {
                field: float(after_values.get(field, 0)) - float(before_values.get(field, 0))
                for field in fields
            }
        result[as_of_date] = stage_delta
    return result


def _read_canonical_tables(db_path: Path) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    with _connect(db_path) as conn:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
                (CANONICAL_TABLE_PREFIX + "%",),
            ).fetchall()
        ]
        result: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
        for table in tables:
            columns = [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]
            rows = [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]
            result[table] = (columns, rows)
        return result


def _replace_canonical_tables(
    conn: sqlite3.Connection,
    materialized: Mapping[str, tuple[list[str], list[tuple[Any, ...]]]],
) -> None:
    order = (
        "sheet_vitrina_v1_canonical_cost_daily_state",
        "sheet_vitrina_v1_canonical_cost_wb_outstanding_layers",
        "sheet_vitrina_v1_canonical_cost_movement_layers",
        "sheet_vitrina_v1_canonical_cost_components",
        "sheet_vitrina_v1_canonical_cost_baseline_lines",
        "sheet_vitrina_v1_canonical_cost_baseline_versions",
    )
    for table in order:
        if table in materialized:
            conn.execute(f'DELETE FROM "{table}"')
    for table in reversed(order):
        if table not in materialized:
            continue
        columns, rows = materialized[table]
        if not rows:
            continue
        column_sql = ",".join(f'"{column}"' for column in columns)
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            f'INSERT INTO "{table}"({column_sql}) VALUES({placeholders})',
            rows,
        )


def _target_header_digest(db_path: Path, shipment_id: str) -> str:
    with _connect(db_path) as conn:
        return _target_header_digest_conn(conn, shipment_id)


def _target_header_digest_conn(conn: sqlite3.Connection, shipment_id: str) -> str:
    row = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_supplier_shipments WHERE shipment_id=?",
        (shipment_id,),
    ).fetchone()
    if row is None:
        return _hash(None)
    header = dict(row)
    header.pop("updated_at", None)
    lines = [
        dict(item)
        for item in conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id=? ORDER BY sort_order,line_id
            """,
            (shipment_id,),
        ).fetchall()
    ]
    return _hash({"header": header, "lines": lines})


def _target_headers_digest(db_path: Path, shipment_ids: Iterable[str]) -> str:
    with _connect(db_path) as conn:
        return _target_headers_digest_conn(conn, shipment_ids)


def _target_headers_digest_conn(
    conn: sqlite3.Connection, shipment_ids: Iterable[str]
) -> str:
    return _hash(
        [
            {
                "shipment_id": shipment_id,
                "digest": _target_header_digest_conn(conn, shipment_id),
            }
            for shipment_id in sorted({str(item) for item in shipment_ids})
        ]
    )


def _non_target_digest(db_path: Path, shipment_id: str) -> str:
    with _connect(db_path) as conn:
        return _non_target_digest_conn(conn, shipment_id)


def _non_target_digest_conn(conn: sqlite3.Connection, shipment_id: str) -> str:
    evidence: dict[str, Any] = {}
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    filtered = {
        "sheet_vitrina_v1_supplier_shipments": ("shipment_id <> ?", (shipment_id,)),
        "sheet_vitrina_v1_supplier_shipment_lines": ("shipment_id <> ?", (shipment_id,)),
    }
    for table in PROTECTED_COLLATERAL_TABLES:
        if table not in existing:
            continue
        where, params = filtered.get(table, ("1=1", ()))
        evidence[table] = [
            list(row)
            for row in conn.execute(
                f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid',
                params,
            )
        ]
    return _hash(evidence)


def _non_target_digest_many(
    db_path: Path,
    shipment_ids: Iterable[str],
    *,
    target_financial_document_ids: Iterable[str] = (),
) -> str:
    with _connect(db_path) as conn:
        return _non_target_digest_many_conn(
            conn,
            shipment_ids,
            target_financial_document_ids=target_financial_document_ids,
        )


def _non_target_digest_many_conn(
    conn: sqlite3.Connection,
    shipment_ids: Iterable[str],
    *,
    target_financial_document_ids: Iterable[str] = (),
) -> str:
    ids = sorted({str(item) for item in shipment_ids if str(item)})
    if not ids:
        raise ValueError("at least one target shipment is required")
    placeholders = ",".join("?" for _ in ids)
    filtered = {
        "sheet_vitrina_v1_supplier_shipments": (
            f"shipment_id NOT IN ({placeholders})",
            tuple(ids),
        ),
        "sheet_vitrina_v1_supplier_shipment_lines": (
            f"shipment_id NOT IN ({placeholders})",
            tuple(ids),
        ),
    }
    financial_document_ids = sorted(
        {str(item) for item in target_financial_document_ids if str(item)}
    )
    if financial_document_ids:
        document_placeholders = ",".join("?" for _ in financial_document_ids)
        filtered["sheet_vitrina_v1_supplier_financial_documents"] = (
            f"document_id NOT IN ({document_placeholders})",
            tuple(financial_document_ids),
        )
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    evidence: dict[str, Any] = {}
    for table in PROTECTED_COLLATERAL_TABLES:
        if table not in existing:
            continue
        where, params = filtered.get(table, ("1=1", ()))
        evidence[table] = [
            list(row)
            for row in conn.execute(
                f'SELECT * FROM "{table}" WHERE {where} ORDER BY rowid',
                params,
            )
        ]
    return _hash(evidence)


def _collateral_digest_many(
    db_path: Path,
    shipment_ids: Iterable[str],
    target_nm_ids: Iterable[int],
    *,
    target_financial_document_ids: Iterable[str] = (),
) -> str:
    with _connect(db_path) as conn:
        return _collateral_digest_many_conn(
            conn,
            shipment_ids,
            target_nm_ids,
            target_financial_document_ids=target_financial_document_ids,
        )


def _candidate_collateral_change_report(
    source_db: Path,
    candidate_db: Path,
    *,
    shipment_ids: Iterable[str],
    target_nm_ids: Iterable[int],
    target_financial_document_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Localize every row covered by the transaction collateral invariant."""

    targets = sorted({str(item) for item in shipment_ids if str(item)})
    nm_ids = sorted({int(item) for item in target_nm_ids})
    financial_document_ids = sorted(
        {str(item) for item in target_financial_document_ids if str(item)}
    )
    with _connect(source_db) as before_conn, _connect(candidate_db) as after_conn:
        before = _collateral_row_inventory_conn(
            before_conn, targets, nm_ids, financial_document_ids
        )
        after = _collateral_row_inventory_conn(
            after_conn, targets, nm_ids, financial_document_ids
        )
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after), key=repr):
        before_row = before.get(key)
        after_row = after.get(key)
        before_values = dict((before_row or {}).get("values") or {})
        after_values = dict((after_row or {}).get("values") or {})
        before_fingerprint = _hash(list(before_values.items())) if before_row else None
        after_fingerprint = _hash(list(after_values.items())) if after_row else None
        if before_fingerprint == after_fingerprint:
            continue
        fields = sorted(
            field
            for field in set(before_values) | set(after_values)
            if before_values.get(field) != after_values.get(field)
        )
        sample = after_row or before_row or {}
        scope = str(sample.get("scope") or "")
        changes.append(
            {
                "scope": scope,
                "table": sample.get("table"),
                "identity": sample.get("identity"),
                "change_kind": (
                    "added"
                    if before_row is None
                    else "removed"
                    if after_row is None
                    else "updated"
                ),
                "before_row_fingerprint": before_fingerprint,
                "after_row_fingerprint": after_fingerprint,
                "changed_fields": fields,
                "values": {
                    field: {
                        "before": _safe_collateral_value(
                            field, before_values.get(field)
                        ),
                        "after": _safe_collateral_value(
                            field, after_values.get(field)
                        ),
                    }
                    for field in fields
                },
                "writer": (
                    "canonical cost engine rebuild"
                    if scope == "canonical_rows_outside_target_skus"
                    else _collateral_writer(str(sample.get("table") or ""))
                ),
                "target_shipment_related": False,
                "target_sku_dependency_related": False,
                "can_change_canonical_candidate": scope
                == "canonical_rows_outside_target_skus",
                "can_change_accounting_effects": scope
                == "canonical_rows_outside_target_skus",
                "classification": "forbidden_candidate_collateral_change",
                "recommended_policy_category": (
                    "canonical_rebuild_collateral"
                    if scope == "canonical_rows_outside_target_skus"
                    else "source_collateral"
                ),
            }
        )
    payload = {
        "contract_name": "supplier_candidate_collateral_localization_v1",
        "read_only_production": True,
        "disposable_candidate_mutation_only": True,
        "target_shipment_ids": targets,
        "target_nm_ids": nm_ids,
        "target_financial_document_ids": financial_document_ids,
        "source_digest": _collateral_digest_many(
            source_db,
            targets,
            nm_ids,
            target_financial_document_ids=financial_document_ids,
        ),
        "candidate_digest": _collateral_digest_many(
            candidate_db,
            targets,
            nm_ids,
            target_financial_document_ids=financial_document_ids,
        ),
        "change_count": len(changes),
        "changes": changes,
    }
    return {**payload, "fingerprint": _hash(payload)}


def _collateral_row_inventory_conn(
    conn: sqlite3.Connection,
    shipment_ids: list[str],
    target_nm_ids: list[int],
    target_financial_document_ids: list[str],
) -> dict[tuple[Any, ...], dict[str, Any]]:
    inventory: dict[tuple[Any, ...], dict[str, Any]] = {}
    existing = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    shipment_placeholders = ",".join("?" for _ in shipment_ids)
    for table in PROTECTED_COLLATERAL_TABLES:
        if table not in existing:
            continue
        where = "1=1"
        params: tuple[Any, ...] = ()
        if table in {
            "sheet_vitrina_v1_supplier_shipments",
            "sheet_vitrina_v1_supplier_shipment_lines",
        }:
            where = f"shipment_id NOT IN ({shipment_placeholders})"
            params = tuple(shipment_ids)
        elif (
            table == "sheet_vitrina_v1_supplier_financial_documents"
            and target_financial_document_ids
        ):
            document_placeholders = ",".join(
                "?" for _ in target_financial_document_ids
            )
            where = f"document_id NOT IN ({document_placeholders})"
            params = tuple(target_financial_document_ids)
        _append_collateral_table_inventory(
            conn,
            inventory,
            scope="source_rows",
            table=table,
            where=where,
            params=params,
            excluded_columns=set(),
        )
    canonical_tables = sorted(
        table for table in existing if table.startswith(CANONICAL_TABLE_PREFIX)
    )
    for table in canonical_tables:
        info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        available = {
            str(row[1])
            for row in info
            if str(row[1]) not in VOLATILE_CANONICAL_COLUMNS
        }
        where = "1=1"
        params = ()
        if target_nm_ids and "nm_id" in available:
            placeholders = ",".join("?" for _ in target_nm_ids)
            where = f"CAST(nm_id AS INTEGER) NOT IN ({placeholders})"
            params = tuple(target_nm_ids)
        _append_collateral_table_inventory(
            conn,
            inventory,
            scope="canonical_rows_outside_target_skus",
            table=table,
            where=where,
            params=params,
            excluded_columns=VOLATILE_CANONICAL_COLUMNS,
        )
    return inventory


def _append_collateral_table_inventory(
    conn: sqlite3.Connection,
    inventory: dict[tuple[Any, ...], dict[str, Any]],
    *,
    scope: str,
    table: str,
    where: str,
    params: tuple[Any, ...],
    excluded_columns: set[str],
) -> None:
    info = list(conn.execute(f'PRAGMA table_info("{table}")'))
    columns = [str(row[1]) for row in info if str(row[1]) not in excluded_columns]
    if not columns:
        return
    primary = [
        str(row[1])
        for row in sorted(info, key=lambda item: int(item[5] or 0))
        if int(row[5] or 0) > 0 and str(row[1]) in columns
    ]
    selected = ",".join(f'"{column}"' for column in columns)
    order = ",".join(f'"{column}"' for column in (primary or columns))
    rows = conn.execute(
        f'SELECT {selected} FROM "{table}" WHERE {where} ORDER BY {order}', params
    ).fetchall()
    for row in rows:
        values = {column: row[index] for index, column in enumerate(columns)}
        raw_identity = tuple(values[column] for column in (primary or columns))
        identity = tuple(_identity_collateral_value(value) for value in raw_identity)
        identity_fields = primary or columns
        inventory[(scope, table, *identity)] = {
            "scope": scope,
            "table": table,
            "identity": {
                field: _safe_collateral_value(field, value)
                for field, value in zip(identity_fields, raw_identity)
            },
            "values": values,
        }


def _identity_collateral_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return _hash({"bytes": value.hex()})
    try:
        hash(value)
    except TypeError:
        return _hash(value)
    return value


def _safe_collateral_value(field: str, value: Any) -> Any:
    if value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    text = str(value)
    sensitive = (
        "json",
        "payload",
        "raw_",
        "blob",
        "path",
        "filename",
        "comment",
        "name",
    )
    if any(part in field.lower() for part in sensitive) or len(text) > 120:
        return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "characters": len(text)}
    return text


def _collateral_writer(table: str) -> str:
    return {
        "sheet_vitrina_v1_ready_snapshots": "sheet-vitrina refresh/materialization/publication",
        "sheet_vitrina_v1_wb_supplies": "WB supplies sync/enrichment",
        "sheet_vitrina_v1_onec_stocks": "1C stock refresh",
        "sheet_vitrina_v1_supplier_financial_documents": "supplier financial document writer/read refresh",
    }.get(table, "repository-owned domain writer")


def _collateral_digest_many_conn(
    conn: sqlite3.Connection,
    shipment_ids: Iterable[str],
    target_nm_ids: Iterable[int],
    *,
    target_financial_document_ids: Iterable[str] = (),
) -> str:
    """Hash rows that the bounded correction is forbidden to change.

    The value is deliberately transaction-local.  It is not an absolute live
    snapshot in the human approval fingerprint: unrelated refresh writers may
    legitimately change these rows before apply.  BEGIN IMMEDIATE freezes the
    comparison boundary and before/after equality proves that this apply did
    not touch collateral rows.
    """

    return _hash(
        {
            "source_rows": _non_target_digest_many_conn(
                conn,
                shipment_ids,
                target_financial_document_ids=target_financial_document_ids,
            ),
            "canonical_rows_outside_target_skus": _canonical_non_target_digest_conn(
                conn, target_nm_ids
            ),
        }
    )


def _expected_canonical_rollforward_report(
    *,
    before_digest: str,
    after_digest: str,
    changes: list[Mapping[str, Any]],
) -> dict[str, Any]:
    exact_changes = [
        {
            **dict(item),
            "classification": "expected_full_rebuild_rollforward",
            "recommended_policy_category": "exact_canonical_rebuild_output",
        }
        for item in changes
    ]
    payload = {
        "contract_name": "supplier_canonical_rollforward_v1",
        "scope": "canonical rows outside target SKU closure changed by the required full rebuild",
        "before_digest": before_digest,
        "after_digest": after_digest,
        "change_count": len(exact_changes),
        "changes": exact_changes,
        "wildcards": False,
        "source_rows_changed": False,
        "apply_contract": "exact before digest and exact candidate after digest",
    }
    return {**payload, "fingerprint": _hash(payload)}


def _canonical_non_target_digest(
    db_path: Path, target_nm_ids: Iterable[int]
) -> str:
    with _connect(db_path) as conn:
        return _canonical_non_target_digest_conn(conn, target_nm_ids)


def _canonical_non_target_digest_conn(
    conn: sqlite3.Connection,
    target_nm_ids: Iterable[int],
) -> str:
    nm_ids = sorted({int(item) for item in target_nm_ids})
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
            (CANONICAL_TABLE_PREFIX + "%",),
        ).fetchall()
    ]
    evidence: list[dict[str, Any]] = []
    for table in tables:
        table_info = list(conn.execute(f'PRAGMA table_info("{table}")'))
        columns = [
            str(row[1])
            for row in table_info
            if str(row[1]) not in VOLATILE_CANONICAL_COLUMNS
        ]
        if not columns:
            continue
        where = "1=1"
        params: tuple[Any, ...] = ()
        if nm_ids and "nm_id" in columns:
            placeholders = ",".join("?" for _ in nm_ids)
            where = f"CAST(nm_id AS INTEGER) NOT IN ({placeholders})"
            params = tuple(nm_ids)
        selected = ",".join(f'"{column}"' for column in columns)
        primary = [
            str(row[1])
            for row in sorted(table_info, key=lambda item: int(item[5] or 0))
            if int(row[5] or 0) > 0
        ]
        order = ",".join(f'"{column}"' for column in (primary or columns))
        rows = conn.execute(
            f'SELECT {selected} FROM "{table}" WHERE {where} ORDER BY {order}',
            params,
        ).fetchall()
        if not rows:
            # Schema-only creation on the disposable candidate is not a
            # collateral business-row change.
            continue
        evidence.append(
            {"table": table, "columns": columns, "rows": [list(row) for row in rows]}
        )
    return _hash(evidence)


def _legacy_pre_cutover_digest(db_path: Path) -> str:
    with _connect(db_path) as conn:
        return _legacy_pre_cutover_digest_conn(conn)


def _legacy_pre_cutover_digest_conn(conn: sqlite3.Connection) -> str:
    evidence: dict[str, Any] = {}
    for table in (
        "sheet_vitrina_v1_ready_snapshots",
        "sheet_vitrina_v1_wb_cost_daily_state",
        "sheet_vitrina_v1_own_capital_daily_state",
    ):
        if not _table_exists(conn, table):
            continue
        evidence[table] = [
            list(row)
            for row in conn.execute(
                f'SELECT * FROM "{table}" WHERE as_of_date < ? ORDER BY rowid',
                (CUTOVER_DATE,),
            )
        ]
    return _hash(evidence)


def _canonical_digest(db_path: Path, *, date_from: str, date_to: str) -> str:
    with _connect(db_path) as conn:
        return _canonical_digest_conn(conn, date_from=date_from, date_to=date_to)


def _canonical_digest_conn(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> str:
    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE ? ORDER BY name",
            (CANONICAL_TABLE_PREFIX + "%",),
        ).fetchall()
    ]
    evidence: list[Any] = []
    for table in tables:
        columns = [
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table}")')
            if str(row[1]) not in VOLATILE_CANONICAL_COLUMNS
        ]
        if not columns:
            continue
        select_columns = ",".join(f'"{column}"' for column in columns)
        if table.endswith("daily_state"):
            rows = conn.execute(
                f'SELECT {select_columns} FROM "{table}" WHERE as_of_date BETWEEN ? AND ? ORDER BY as_of_date,nm_id,stage',
                (date_from, date_to),
            ).fetchall()
        else:
            rows = conn.execute(
                f'SELECT {select_columns} FROM "{table}" ORDER BY rowid'
            ).fetchall()
        if not rows:
            continue
        evidence.append({"table": table, "columns": columns, "rows": [list(row) for row in rows]})
    return _hash(evidence)


def _canonical_financial_document_effect(
    db_path: Path, document_id: str
) -> dict[str, Any]:
    with _connect(db_path) as conn:
        if not _table_exists(
            conn, "sheet_vitrina_v1_canonical_cost_components"
        ):
            rows: list[dict[str, Any]] = []
        else:
            rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT component_identity,component_type,shipment_id,nm_id,quantity,
                           recognized_amount_rub,recognized_date,paid_amount_rub,paid_date,
                           paid_equivalent_quantity,allocation_method,source_document_id,
                           source_line_id,evidence_json,confirmation_status,fingerprint
                    FROM sheet_vitrina_v1_canonical_cost_components
                    WHERE source_document_id=? AND is_current=1
                    ORDER BY component_identity
                    """,
                    (document_id,),
                ).fetchall()
            ]
    recognized = sum(
        (_exact_decimal(row["recognized_amount_rub"]) for row in rows),
        Decimal("0"),
    )
    paid = sum(
        (_exact_decimal(row["paid_amount_rub"]) for row in rows), Decimal("0")
    )
    paid_equivalent = sum(
        (_exact_decimal(row["paid_equivalent_quantity"]) for row in rows),
        Decimal("0"),
    )
    quantity_context = sum(
        (_exact_decimal(row["quantity"]) for row in rows), Decimal("0")
    )
    payload = {
        "contract_name": "supplier_financial_document_canonical_effect_v1",
        "document_id": document_id,
        "component_count": len(rows),
        "unique_component_identity_count": len(
            {str(row["component_identity"]) for row in rows}
        ),
        "unique_nm_id_count": len({int(row["nm_id"]) for row in rows}),
        "recognized_capital_rub": _money_text(recognized),
        "paid_capital_rub": _money_text(paid),
        "paid_equivalent_quantity": _money_text(paid_equivalent),
        "component_quantity_context": _money_text(quantity_context),
        "physical_movement_quantity": "0",
        "components": rows,
    }
    return {**payload, "fingerprint": _hash(payload)}


def _successful_correction_by_values(db_path: Path, shipment_id: str, new_value: str) -> bool:
    with _connect(db_path) as conn:
        if not _table_exists(conn, CORRECTION_TABLE):
            return False
        row = conn.execute(
            f"SELECT 1 FROM {CORRECTION_TABLE} WHERE shipment_id=? AND new_value=? AND status='success' LIMIT 1",
            (shipment_id, new_value),
        ).fetchone()
    return row is not None


def _successful_historical_status_change(
    db_path: Path, plan: Mapping[str, Any]
) -> bool:
    with _connect(db_path) as conn:
        if not _table_exists(conn, HISTORICAL_STATUS_EVENT_TABLE):
            return False
        row = conn.execute(
            f"""
            SELECT 1 FROM {HISTORICAL_STATUS_EVENT_TABLE}
            WHERE shipment_id=? AND action=? AND new_exception=?
              AND evidence_fingerprint=? AND status='success'
            LIMIT 1
            """,
            (
                plan["shipment_id"],
                plan["action"],
                plan["new_exception"],
                plan["evidence_fingerprint"],
            ),
        ).fetchone()
    return row is not None


def _ensure_correction_schema(conn: sqlite3.Connection) -> None:
    script = f"""
        CREATE TABLE IF NOT EXISTS {CORRECTION_TABLE} (
            correction_id TEXT PRIMARY KEY,
            request_fingerprint TEXT NOT NULL,
            apply_fingerprint TEXT NOT NULL DEFAULT '',
            shipment_id TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            actor TEXT NOT NULL,
            source TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            phase TEXT NOT NULL,
            progress_text TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            report_json TEXT NOT NULL DEFAULT '{{}}',
            backup_json TEXT NOT NULL DEFAULT '{{}}',
            error_code TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS supplier_factual_corrections_by_shipment
        ON {CORRECTION_TABLE}(shipment_id, requested_at DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS supplier_factual_corrections_one_active
        ON {CORRECTION_TABLE}(shipment_id)
        WHERE status IN ('queued','running');
        """
    for statement in script.split(";"):
        sql = statement.strip()
        if sql:
            conn.execute(sql)


def _ensure_historical_status_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {HISTORICAL_STATUS_EVENT_TABLE} (
            event_id TEXT PRIMARY KEY,
            shipment_id TEXT NOT NULL,
            exception_code TEXT NOT NULL,
            action TEXT NOT NULL,
            previous_exception TEXT NOT NULL DEFAULT '',
            new_exception TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            provenance TEXT NOT NULL,
            actor TEXT NOT NULL,
            evidence_fingerprint TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            apply_fingerprint TEXT NOT NULL,
            reverses_event_id TEXT,
            reversible INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{{}}'
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS supplier_historical_status_events_by_shipment
        ON {HISTORICAL_STATUS_EVENT_TABLE}(shipment_id,created_at DESC,event_id DESC)
        """
    )


def _correction_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    payload = dict(row)
    payload["report"] = _json_object(payload.pop("report_json", ""))
    payload["backup"] = _json_object(payload.pop("backup_json", ""))
    payload["active"] = str(payload.get("status") or "") in ACTIVE_CORRECTION_STATUSES
    payload["finished"] = str(payload.get("status") or "") in FINAL_CORRECTION_STATUSES
    return payload


def _sqlite_backup(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with closing(sqlite3.connect(source)) as source_conn, closing(sqlite3.connect(destination)) as dest_conn:
        source_conn.backup(dest_conn)
        dest_conn.commit()
    if _integrity_check(destination) != "ok":
        destination.unlink(missing_ok=True)
        raise ValueError("coherent SQLite backup integrity_check failed")
    destination.chmod(0o600)
    return {
        "path": str(destination),
        "filename": destination.name,
        "sha256": _file_hash(destination),
        "size_bytes": destination.stat().st_size,
        "mode": "0600",
        "integrity_check": "ok",
    }


def _restore_backup_in_place(backup: Path, destination: Path) -> None:
    with closing(sqlite3.connect(backup)) as source_conn, closing(
        sqlite3.connect(destination, timeout=60)
    ) as destination_conn:
        source_conn.backup(destination_conn)
        destination_conn.commit()


def restore_verified_supplier_backup(backup: Path, destination: Path) -> dict[str, Any]:
    """Restore one repo-owned SQLite backup and verify inode/integrity."""

    inode = destination.stat().st_ino
    _restore_backup_in_place(Path(backup), Path(destination))
    integrity = _integrity_check(Path(destination))
    if destination.stat().st_ino != inode or integrity != "ok":
        raise ValueError("supplier backup restore verification failed")
    return {
        "path": str(destination),
        "inode_preserved": True,
        "integrity": integrity,
        "restored_from": str(backup),
    }


def _integrity_check(db_path: Path) -> str:
    with closing(sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)) as conn:
        value = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if value.lower() != "ok":
        raise ValueError(f"SQLite integrity_check failed: {value}")
    return "ok"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _crosses_cutover(old_value: str, new_value: str) -> bool:
    if not old_value:
        return bool(new_value and new_value < CUTOVER_DATE)
    if not new_value:
        return old_value < CUTOVER_DATE
    return (old_value < CUTOVER_DATE <= new_value) or (new_value < CUTOVER_DATE <= old_value)


def _literal_like_prefix(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _safe_error_message(exc: Exception) -> str:
    text = str(exc or "").strip()
    if "movement identity already exists" in text.lower():
        return "Не удалось согласованно пересчитать зависимые данные; прежняя версия сохранена."
    if isinstance(exc, SupplierShipmentFactualCorrectionError):
        return text
    return "Не удалось сохранить изменение. Прежняя согласованная версия сохранена."


def _emit(emit: ProgressEmitter | None, message: str) -> None:
    if emit is not None:
        emit(message)


def _required_text(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _json_object(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _default_timestamp_factory() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
