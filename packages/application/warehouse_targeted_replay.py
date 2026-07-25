"""Bounded append-only publication for one supplier shipment dependency closure.

This module is intentionally independent of the monolithic Finance raw store.
Preview reads only one shipment and the active functional rows for its SKU
closure.  Apply changes the source header and publishes a successor functional
version in one SQLite transaction; rollback is the active-version pointer plus
the exact before image recorded in the audit row.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
from typing import Any, Callable, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_functional import (
    FUNCTIONAL_CUTOVER_ID,
    STAGE_CHINA_TO_FF,
    STAGE_PRODUCTION,
    ensure_warehouse_functional_schema,
    load_supplier_line_cost_breakdown,
)
from packages.application.warehouse_functional_lock import (
    warehouse_functional_write_lock,
)


TARGETED_PUBLICATION_TABLE = (
    "sheet_vitrina_v1_warehouse_targeted_publications"
)
TARGETED_UNDO_TABLE = "sheet_vitrina_v1_warehouse_targeted_undo_manifests"
QUEUE_TABLE = "sheet_vitrina_v1_warehouse_targeted_recalc_queue"
FINANCE_RAW_TABLE = "wb_finance_weekly_raw_rows"
ZERO = Decimal("0")


class WarehouseTargetedReplayError(RuntimeError):
    """A bounded target replay could not be proven safe."""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        f"file:{db_path.resolve()}?mode=ro",
        uri=True,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        conn.close()
        raise WarehouseTargetedReplayError(
            "targeted replay preview could not enable SQLite query_only"
        )
    return conn


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _fingerprint(value: Any) -> str:
    return "sha256:" + _hash(value)


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def _text(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001"))
    return format(normalized.normalize(), "f")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_warehouse_targeted_replay_schema(conn: sqlite3.Connection) -> None:
    ensure_warehouse_functional_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {TARGETED_PUBLICATION_TABLE}(
            publication_id TEXT PRIMARY KEY,
            stable_source_id TEXT NOT NULL,
            source_revision TEXT NOT NULL,
            earliest_business_date TEXT NOT NULL,
            affected_nm_ids_json TEXT NOT NULL,
            base_version_id TEXT NOT NULL,
            version_id TEXT NOT NULL UNIQUE,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            target_before_digest TEXT NOT NULL,
            target_after_digest TEXT NOT NULL,
            non_target_before_digest TEXT NOT NULL,
            non_target_after_digest TEXT NOT NULL,
            status TEXT NOT NULL,
            blocker_summary_json TEXT NOT NULL,
            diagnostics_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS warehouse_targeted_publication_source
        ON {TARGETED_PUBLICATION_TABLE}(stable_source_id,created_at);

        CREATE TABLE IF NOT EXISTS {TARGETED_UNDO_TABLE}(
            undo_id TEXT PRIMARY KEY,
            publication_id TEXT NOT NULL UNIQUE,
            shipment_id TEXT NOT NULL,
            before_header_json TEXT NOT NULL,
            after_header_json TEXT NOT NULL,
            base_version_id TEXT NOT NULL,
            published_version_id TEXT NOT NULL,
            target_rows_before_json TEXT NOT NULL,
            target_rows_after_json TEXT NOT NULL,
            manifest_digest TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            rolled_back_at TEXT
        );
        """
    )


def _header_and_revision(
    conn: sqlite3.Connection,
    shipment_id: str,
) -> tuple[dict[str, Any], str, list[int], int]:
    header = conn.execute(
        """
        SELECT shipment_id,invoice_no,shipment_date,actual_shipment_date,
               actual_ff_acceptance_date,order_status,match_status,
               expenses_complete,updated_at
        FROM sheet_vitrina_v1_supplier_shipments
        WHERE shipment_id=?
        """,
        (shipment_id,),
    ).fetchone()
    if header is None:
        raise WarehouseTargetedReplayError(
            f"supplier shipment not found: {shipment_id}"
        )
    lines = [
        dict(row)
        for row in conn.execute(
            """
            SELECT line_id,line_type,sort_order,internal_nm_id,qty,unit_price,
                   amount,match_status
            FROM sheet_vitrina_v1_supplier_shipment_lines
            WHERE shipment_id=?
            ORDER BY sort_order,line_id
            """,
            (shipment_id,),
        ).fetchall()
    ]
    document_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT document_id,document_type,document_date,parse_status,
                   parser_version,updated_at
            FROM sheet_vitrina_v1_supplier_financial_documents
            WHERE supplier_order_id=?
            ORDER BY document_id
            """,
            (shipment_id,),
        ).fetchall()
    ]
    expense_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT line_id,financial_document_id,sort_order,category,
                   amount,currency,amount_rub,vat_rate,vat_amount_rub,status,
                   raw_json
            FROM sheet_vitrina_v1_supplier_financial_expense_lines
            WHERE supplier_order_id=?
            ORDER BY financial_document_id,sort_order,line_id
            """,
            (shipment_id,),
        ).fetchall()
    ]
    cny_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT operation_id,operation_type,operation_date,sequence_key,
                   cny_delta,rub_value_delta,source_document_id,updated_at
            FROM sheet_vitrina_v1_cny_ledger_operations
            WHERE source_order_id=?
            ORDER BY sequence_key,operation_id
            """,
            (shipment_id,),
        ).fetchall()
    ]
    material = {
        "header": dict(header),
        "lines": lines,
        "documents": document_rows,
        "expenses": expense_rows,
        "cny_operations": cny_rows,
    }
    nm_ids = sorted(
        {
            int(row.get("internal_nm_id") or 0)
            for row in lines
            if str(row.get("line_type") or "") == "product"
            and int(row.get("internal_nm_id") or 0) > 0
        }
    )
    return dict(header), _fingerprint(material), nm_ids, len(_json(material).encode("utf-8"))


def _active_target_rows(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    nm_ids: Iterable[int],
) -> list[dict[str, Any]]:
    selected = sorted({int(value) for value in nm_ids if int(value) > 0})
    if not selected:
        return []
    placeholders = ",".join("?" for _ in selected)
    rows = conn.execute(
        f"""
        SELECT *
        FROM sheet_vitrina_v1_warehouse_functional_balances
        WHERE version_id=?
          AND warehouse_key IN (?,?)
          AND nm_id IN ({placeholders})
        ORDER BY warehouse_key,nm_id
        """,
        (version_id, STAGE_PRODUCTION, STAGE_CHINA_TO_FF, *selected),
    ).fetchall()
    result = []
    for raw in rows:
        item = dict(raw)
        item.pop("version_id", None)
        item["provenance"] = json.loads(str(item.pop("provenance_json") or "{}"))
        result.append(item)
    return result


def _non_target_digest(
    conn: sqlite3.Connection,
    *,
    version_id: str,
    affected_nm_ids: Iterable[int],
) -> str:
    selected = sorted({int(value) for value in affected_nm_ids if int(value) > 0})
    parameters: list[Any] = [version_id]
    exclusion = ""
    if selected:
        exclusion = " AND nm_id NOT IN (" + ",".join("?" for _ in selected) + ")"
        parameters.extend(selected)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                   cost_covered_quantity,quality,certified,wb_quantity,
                   wb_in_way_to_client,wb_in_way_from_client,provenance_json
            FROM sheet_vitrina_v1_warehouse_functional_balances
            WHERE version_id=?
            """
            + exclusion
            + " ORDER BY warehouse_key,nm_id",
            tuple(parameters),
        ).fetchall()
    ]
    return _fingerprint(rows)


def _allocation_records(
    allocation: Mapping[str, Any],
    *,
    shipment_id: str,
    old_records: Mapping[int, list[dict[str, Any]]],
    actual_shipment_date: str,
) -> tuple[str, dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    stage = str(allocation.get("stage") or "")
    if stage not in {STAGE_PRODUCTION, STAGE_CHINA_TO_FF}:
        stage = STAGE_CHINA_TO_FF if actual_shipment_date else STAGE_PRODUCTION
    blockers = [dict(item) for item in allocation.get("blockers") or []]
    grouped: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for raw in allocation.get("lines") or []:
        nm_id = int(raw.get("nm_id") or 0)
        if nm_id > 0:
            grouped[nm_id].append(dict(raw))
    result: dict[int, list[dict[str, Any]]] = {}
    flow_id = "supplier_flow_" + hashlib.sha256(shipment_id.encode("utf-8")).hexdigest()[:20]
    for nm_id in sorted(set(grouped) | set(old_records)):
        rows = grouped.get(nm_id) or []
        quantity = sum((_decimal(row.get("quantity")) for row in rows), ZERO)
        capital = sum((_decimal(row.get("capital_rub")) for row in rows), ZERO)
        if blockers and old_records.get(nm_id):
            preserved = []
            for source in old_records[nm_id]:
                preserved.append(
                    {
                        **source,
                        "business_date": actual_shipment_date
                        or str(allocation.get("first_payment_date") or allocation.get("invoice_date") or "")[:10],
                        "actual_shipment_date": actual_shipment_date,
                        "cost_freshness": "unavailable",
                        "cost_blockers": blockers,
                    }
                )
            result[nm_id] = preserved
            continue
        if quantity <= ZERO and not old_records.get(nm_id):
            continue
        quality = (
            "certified"
            if bool(allocation.get("expenses_complete")) and not blockers
            else "confirmed_payments_provisional_expenses"
            if capital > ZERO
            else "cost_unavailable"
        )
        record = {
            "supplier_flow_id": flow_id,
            "shipment_id": shipment_id,
            "invoice_no": str(allocation.get("invoice_no") or ""),
            "invoice_date": str(allocation.get("invoice_date") or "")[:10],
            "business_date": actual_shipment_date
            or str(allocation.get("first_payment_date") or allocation.get("invoice_date") or "")[:10],
            "actual_shipment_date": actual_shipment_date,
            "flow_quantity": _text(quantity),
            "flow_capital_rub": _text(capital),
            "quality": quality,
            "expenses_complete_certification": bool(allocation.get("expenses_complete"))
            and not blockers,
            "source_fingerprint": str(allocation.get("source_fingerprint") or ""),
            "calculation_fingerprint": str(allocation.get("calculation_fingerprint") or ""),
            "line_cost_breakdown": rows,
            "cost_freshness": "current"
            if bool(allocation.get("expenses_complete")) and not blockers
            else "preliminary"
            if capital > ZERO
            else "unavailable",
            "cost_blockers": blockers,
        }
        result[nm_id] = [record]
    return stage, result, blockers


def _rebuild_target_rows(
    before_rows: Iterable[Mapping[str, Any]],
    *,
    shipment_id: str,
    allocation: Mapping[str, Any],
    actual_shipment_date: str,
    nm_ids: Iterable[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    before = {
        (str(row.get("warehouse_key") or ""), int(row.get("nm_id") or 0)): dict(row)
        for row in before_rows
    }
    old_records: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in before.values():
        for source in dict(row.get("provenance") or {}).get("source_records") or []:
            if str(source.get("shipment_id") or "") == shipment_id:
                old_records[int(row["nm_id"])].append(dict(source))
    if before and not old_records:
        raise WarehouseTargetedReplayError(
            "target_source_identity_missing: active aggregate cannot isolate the shipment"
        )
    destination, new_records, blockers = _allocation_records(
        allocation,
        shipment_id=shipment_id,
        old_records=old_records,
        actual_shipment_date=actual_shipment_date,
    )
    output: list[dict[str, Any]] = []
    for nm_id in sorted({int(value) for value in nm_ids if int(value) > 0}):
        for stage in (STAGE_PRODUCTION, STAGE_CHINA_TO_FF):
            current = before.get((stage, nm_id))
            if current is None:
                current = {
                    "warehouse_key": stage,
                    "nm_id": nm_id,
                    "quantity": "0",
                    "wac_rub": None,
                    "capital_rub": "0",
                    "cost_covered_quantity": "0",
                    "quality": "empty",
                    "certified": 0,
                    "wb_quantity": "0",
                    "wb_in_way_to_client": "0",
                    "wb_in_way_from_client": "0",
                    "provenance": {"source_records": []},
                }
            sources = [
                dict(item)
                for item in dict(current.get("provenance") or {}).get("source_records") or []
                if str(item.get("shipment_id") or "") != shipment_id
            ]
            if stage == destination:
                sources.extend(new_records.get(nm_id) or [])
            quantity = sum((_decimal(item.get("flow_quantity")) for item in sources), ZERO)
            capital = sum((_decimal(item.get("flow_capital_rub")) for item in sources), ZERO)
            covered = sum(
                (
                    _decimal(item.get("flow_quantity"))
                    for item in sources
                    if str(item.get("cost_freshness") or "") != "unavailable"
                    and _decimal(item.get("flow_capital_rub")) > ZERO
                ),
                ZERO,
            )
            if quantity <= ZERO:
                continue
            qualities = sorted(
                {
                    str(item.get("quality") or "cost_unavailable")
                    for item in sources
                }
            )
            fully_covered = covered >= quantity and capital > ZERO
            output.append(
                {
                    **{key: value for key, value in current.items() if key != "version_id"},
                    "warehouse_key": stage,
                    "nm_id": nm_id,
                    "quantity": _text(quantity),
                    "wac_rub": _text(capital / quantity) if fully_covered else None,
                    "capital_rub": _text(capital),
                    "cost_covered_quantity": _text(min(covered, quantity)),
                    "quality": qualities[0] if len(qualities) == 1 else "mixed:" + ",".join(qualities),
                    "certified": int(
                        fully_covered
                        and all(
                            bool(item.get("expenses_complete_certification"))
                            for item in sources
                        )
                    ),
                    "provenance": {
                        "source_records": sources,
                        "targeted_replay": True,
                        "cost_semantics": (
                            "known_capital_only"
                            if not fully_covered
                            else "complete_capital"
                        ),
                    },
                }
            )
    return output, blockers


class WarehouseTargetedSupplierReplay:
    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        timestamp_factory: Callable[[], str] | None = None,
        failure_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.timestamp_factory = timestamp_factory or _now
        self.failure_injector = failure_injector

    def build_plan(
        self,
        *,
        shipment_id: str,
        new_actual_shipment_date: str,
        new_order_status: str,
        expected_old_value: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        selected_id = str(shipment_id or "").strip()
        new_date = str(new_actual_shipment_date or "").strip()
        phase_started = time.perf_counter()
        with _connect_readonly(self.runtime.db_path) as conn:
            header, source_revision, nm_ids, source_bytes = _header_and_revision(
                conn, selected_id
            )
            old_date = str(header.get("actual_shipment_date") or "").strip()
            if expected_old_value is not None and old_date != str(expected_old_value or "").strip():
                raise WarehouseTargetedReplayError(
                    f"actual_shipment_date drift: expected {expected_old_value!r}, got {old_date!r}"
                )
            active = conn.execute(
                "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
            ).fetchone()
            if active is None:
                raise WarehouseTargetedReplayError(
                    "active functional warehouse version is missing"
                )
            base_version_id = str(active["version_id"])
            before_rows = _active_target_rows(
                conn, version_id=base_version_id, nm_ids=nm_ids
            )
            non_target_digest = _non_target_digest(
                conn,
                version_id=base_version_id,
                affected_nm_ids=nm_ids,
            )
        source_read_ms = round((time.perf_counter() - phase_started) * 1000, 3)
        phase_started = time.perf_counter()
        allocation = load_supplier_line_cost_breakdown(
            runtime=self.runtime,
            shipment_id=selected_id,
            actual_shipment_date_override=new_date,
        )
        if not allocation:
            raise WarehouseTargetedReplayError(
                "target supplier cost/quantity allocation is unavailable"
            )
        after_rows, blockers = _rebuild_target_rows(
            before_rows,
            shipment_id=selected_id,
            allocation=allocation,
            actual_shipment_date=new_date,
            nm_ids=nm_ids,
        )
        calculation_ms = round((time.perf_counter() - phase_started) * 1000, 3)
        before_digest = _fingerprint(before_rows)
        after_digest = _fingerprint(after_rows)
        would_change = old_date != new_date or before_digest != after_digest
        earliest = min(
            value
            for value in (
                str(header.get("shipment_date") or "")[:10],
                old_date[:10],
                new_date[:10],
            )
            if value
        )
        fingerprint_material = {
            "contract": "warehouse_targeted_supplier_replay_v1",
            "shipment_id": selected_id,
            "old_actual_shipment_date": old_date,
            "new_actual_shipment_date": new_date,
            "new_order_status": str(new_order_status or ""),
            "source_revision": source_revision,
            "base_version_id": base_version_id,
            "affected_nm_ids": nm_ids,
            "earliest_business_date": earliest,
            "target_before_digest": before_digest,
            "target_after_digest": after_digest,
            "non_target_digest": non_target_digest,
            "supplier_cost_state": {
                "source_fingerprint": str(
                    allocation.get("source_fingerprint") or ""
                ),
                "calculation_fingerprint": str(
                    allocation.get("calculation_fingerprint") or ""
                ),
                "expenses_complete": bool(allocation.get("expenses_complete")),
                "calculation_available": not bool(allocation.get("blockers")),
            },
        }
        plan_fingerprint = _fingerprint(fingerprint_material)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        return {
            **fingerprint_material,
            "plan_fingerprint": plan_fingerprint,
            "fingerprint": plan_fingerprint,
            "would_change": would_change,
            "before_header": header,
            "after_header": {
                **header,
                "actual_shipment_date": new_date or None,
                "order_status": str(new_order_status or ""),
            },
            "target_rows_before": before_rows,
            "target_rows_after": after_rows,
            "blocker_summary": blockers,
            "scope": {
                "shipment_ids": [selected_id],
                "affected_nm_ids": nm_ids,
                "earliest_business_date": earliest,
                "direct_consumers": ["warehouse_functional_active"],
            },
            "performance": {
                "elapsed_ms": elapsed_ms,
                "phase_timings_ms": {
                    "target_source_read": source_read_ms,
                    "target_calculation": calculation_ms,
                },
                "affected_counts": {
                    "shipment_count": 1,
                    "sku_count": len(nm_ids),
                    "balance_rows_before": len(before_rows),
                    "balance_rows_after": len(after_rows),
                },
                "read_bytes_upper_bound": source_bytes
                + len(_json(before_rows).encode("utf-8")),
                "write_bytes_estimate": len(_json(after_rows).encode("utf-8")),
                "copy_bytes": 0,
                "full_database_copy": False,
                "full_database_integrity_scan": False,
                "finance_raw_rows_read": 0,
                "tables_read": [
                    "sheet_vitrina_v1_supplier_shipments",
                    "sheet_vitrina_v1_supplier_shipment_lines",
                    "sheet_vitrina_v1_supplier_financial_documents",
                    "sheet_vitrina_v1_supplier_financial_expense_lines",
                    "sheet_vitrina_v1_cny_ledger_operations",
                    "sheet_vitrina_v1_warehouse_functional_active",
                    "sheet_vitrina_v1_warehouse_functional_balances",
                ],
                "excluded_tables": [FINANCE_RAW_TABLE],
                "complexity": "O(target shipment rows + affected SKU rows)",
            },
        }

    def apply(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        lock_wait_ms: int = 0,
    ) -> dict[str, Any]:
        with warehouse_functional_write_lock(
            self.runtime.runtime_dir,
            timeout_seconds=300,
        ) as lock_info:
            return self._apply_locked(
                plan,
                confirm_fingerprint=confirm_fingerprint,
                lock_wait_ms=max(lock_wait_ms, int(lock_info["wait_ms"])),
            )

    def _apply_locked(
        self,
        plan: Mapping[str, Any],
        *,
        confirm_fingerprint: str,
        lock_wait_ms: int,
    ) -> dict[str, Any]:
        approved = str(confirm_fingerprint or "")
        if approved != str(plan.get("plan_fingerprint") or ""):
            raise WarehouseTargetedReplayError(
                "exact reviewed targeted plan fingerprint is required"
            )
        if not bool(plan.get("would_change")):
            return {**dict(plan), "applied": False, "idempotent": True}
        free_bytes = shutil.disk_usage(self.runtime.runtime_dir).free
        required_bytes = max(
            4 * 1024 * 1024,
            int((plan.get("performance") or {}).get("write_bytes_estimate") or 0)
            * 8,
        )
        if free_bytes < required_bytes:
            raise WarehouseTargetedReplayError(
                f"targeted_replay_capacity_insufficient: required={required_bytes}, free={free_bytes}"
            )
        now = self.timestamp_factory()
        shipment_id = str(plan["shipment_id"])
        base_version_id = str(plan["base_version_id"])
        fingerprint = str(plan["plan_fingerprint"])
        version_id = "whfv_target_" + fingerprint.removeprefix("sha256:")[:20]
        publication_id = "whtp_" + fingerprint.removeprefix("sha256:")[:24]
        queue_id = "whrq_" + _hash(
            {
                "stable_source_id": f"supplier_shipment:{shipment_id}",
                "source_revision": plan["source_revision"],
            }
        )[:24]
        with _connect(self.runtime.db_path) as conn:
            ensure_warehouse_targeted_replay_schema(conn)
            existing = conn.execute(
                f"SELECT * FROM {TARGETED_PUBLICATION_TABLE} WHERE plan_fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing is not None and str(existing["status"]) == "complete":
                return {
                    **dict(plan),
                    "applied": False,
                    "idempotent": True,
                    "version_id": str(existing["version_id"]),
                    "publication_id": str(existing["publication_id"]),
                }
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._inject("after_begin")
                current_header, current_revision, _, _ = _header_and_revision(
                    conn, shipment_id
                )
                if current_revision != str(plan["source_revision"]):
                    raise WarehouseTargetedReplayError(
                        "stale targeted preview: source revision changed"
                    )
                active = conn.execute(
                    "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
                ).fetchone()
                if active is None or str(active["version_id"]) != base_version_id:
                    raise WarehouseTargetedReplayError(
                        "stale targeted preview: active functional version changed"
                    )
                current_rows = _active_target_rows(
                    conn,
                    version_id=base_version_id,
                    nm_ids=plan["affected_nm_ids"],
                )
                if _fingerprint(current_rows) != str(plan["target_before_digest"]):
                    raise WarehouseTargetedReplayError(
                        "stale targeted preview: target rows changed"
                    )
                non_target_before = _non_target_digest(
                    conn,
                    version_id=base_version_id,
                    affected_nm_ids=plan["affected_nm_ids"],
                )
                if non_target_before != str(plan["non_target_digest"]):
                    raise WarehouseTargetedReplayError(
                        "stale targeted preview: non-target active rows changed"
                    )
                conn.execute(
                    f"""
                    UPDATE {QUEUE_TABLE}
                    SET status='complete',finished_at=?,error='superseded by newer coalesced revision'
                    WHERE stable_source_id=? AND status='queued'
                    """,
                    (now, f"supplier_shipment:{shipment_id}"),
                )
                conn.execute(
                    f"""
                    INSERT INTO {QUEUE_TABLE}(
                        queue_id,stable_source_id,source_revision,effective_date,
                        affected_nm_ids_json,status,requested_at,started_at,finished_at,error
                    ) VALUES(?,?,?,?,?,'running',?,?,NULL,NULL)
                    ON CONFLICT(stable_source_id,source_revision) DO UPDATE SET
                        status='running',started_at=excluded.started_at,
                        finished_at=NULL,error=NULL
                    """,
                    (
                        queue_id,
                        f"supplier_shipment:{shipment_id}",
                        str(plan["source_revision"]),
                        str(plan["earliest_business_date"]),
                        _json(plan["affected_nm_ids"]),
                        now,
                        now,
                    ),
                )
                updated = conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_supplier_shipments
                    SET actual_shipment_date=?,order_status=?,updated_at=?
                    WHERE shipment_id=? AND COALESCE(actual_shipment_date,'')=?
                    """,
                    (
                        str(plan["new_actual_shipment_date"]) or None,
                        str(plan["new_order_status"]),
                        now,
                        shipment_id,
                        str(plan["old_actual_shipment_date"]),
                    ),
                )
                if int(updated.rowcount or 0) != 1:
                    raise WarehouseTargetedReplayError(
                        "target header changed before atomic apply"
                    )
                base_version = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions
                    WHERE version_id=?
                    """,
                    (base_version_id,),
                ).fetchone()
                if base_version is None:
                    raise WarehouseTargetedReplayError(
                        "base functional version disappeared"
                    )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_functional_versions(
                        version_id,cutover_id,version_kind,effective_at,status,
                        plan_fingerprint,local_source_digest,source_watermarks_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        version_id,
                        FUNCTIONAL_CUTOVER_ID,
                        "targeted_supplier_replay",
                        now,
                        "good",
                        fingerprint,
                        str(plan["source_revision"]),
                        _json(
                            {
                                "base_version_id": base_version_id,
                                "stable_source_id": f"supplier_shipment:{shipment_id}",
                                "source_revision": plan["source_revision"],
                                "earliest_business_date": plan["earliest_business_date"],
                            }
                        ),
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                        version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                        cost_covered_quantity,quality,certified,wb_quantity,
                        wb_in_way_to_client,wb_in_way_from_client,provenance_json
                    )
                    SELECT ?,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                           cost_covered_quantity,quality,certified,wb_quantity,
                           wb_in_way_to_client,wb_in_way_from_client,provenance_json
                    FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id=?
                    """,
                    (version_id, base_version_id),
                )
                affected = sorted({int(value) for value in plan["affected_nm_ids"]})
                placeholders = ",".join("?" for _ in affected)
                conn.execute(
                    f"""
                    DELETE FROM sheet_vitrina_v1_warehouse_functional_balances
                    WHERE version_id=? AND warehouse_key IN (?,?)
                      AND nm_id IN ({placeholders})
                    """,
                    (version_id, STAGE_PRODUCTION, STAGE_CHINA_TO_FF, *affected),
                )
                for item in plan["target_rows_after"]:
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_warehouse_functional_balances(
                            version_id,warehouse_key,nm_id,quantity,wac_rub,capital_rub,
                            cost_covered_quantity,quality,certified,wb_quantity,
                            wb_in_way_to_client,wb_in_way_from_client,provenance_json
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            version_id,
                            item["warehouse_key"],
                            int(item["nm_id"]),
                            item["quantity"],
                            item.get("wac_rub"),
                            item["capital_rub"],
                            item["cost_covered_quantity"],
                            item["quality"],
                            int(bool(item["certified"])),
                            item.get("wb_quantity") or "0",
                            item.get("wb_in_way_to_client") or "0",
                            item.get("wb_in_way_from_client") or "0",
                            _json(item.get("provenance") or {}),
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_functional_ff_reservations(
                        version_id,supply_id,nm_id,quantity
                    )
                    SELECT ?,supply_id,nm_id,quantity
                    FROM sheet_vitrina_v1_warehouse_functional_ff_reservations
                    WHERE version_id=?
                    """,
                    (version_id, base_version_id),
                )
                snapshot = conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_warehouse_wb_snapshots
                    WHERE version_id=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (base_version_id,),
                ).fetchone()
                if snapshot is not None:
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
                            snapshot_id,version_id,fetched_at,snapshot_date,
                            requested_nm_ids_json,pagination_complete,page_count,
                            page_offsets_json,raw_row_count,raw_rows_digest,
                            raw_rows_json,items_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            "wbsnap_target_"
                            + fingerprint.removeprefix("sha256:")[:20],
                            version_id,
                            snapshot["fetched_at"],
                            snapshot["snapshot_date"],
                            snapshot["requested_nm_ids_json"],
                            snapshot["pagination_complete"],
                            snapshot["page_count"],
                            snapshot["page_offsets_json"],
                            snapshot["raw_row_count"],
                            snapshot["raw_rows_digest"],
                            snapshot["raw_rows_json"],
                            snapshot["items_json"],
                            now,
                        ),
                    )
                for unmatched in conn.execute(
                    """
                    SELECT * FROM sheet_vitrina_v1_warehouse_unmatched_doprinato
                    WHERE version_id=? ORDER BY unmatched_id
                    """,
                    (base_version_id,),
                ).fetchall():
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_warehouse_unmatched_doprinato(
                            unmatched_id,version_id,source_id,business_date,nm_id,
                            quantity,matched_quantity,reason,provenance_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            "unmatched_target_"
                            + _hash(
                                {
                                    "version_id": version_id,
                                    "base_id": unmatched["unmatched_id"],
                                }
                            )[:20],
                            version_id,
                            unmatched["source_id"],
                            unmatched["business_date"],
                            unmatched["nm_id"],
                            unmatched["quantity"],
                            unmatched["matched_quantity"],
                            unmatched["reason"],
                            unmatched["provenance_json"],
                            now,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_states(
                        version_id,shipment_id,source_fingerprint,calculation_fingerprint,
                        expenses_complete,calculation_available,created_at
                    )
                    SELECT ?,shipment_id,source_fingerprint,calculation_fingerprint,
                           expenses_complete,calculation_available,?
                    FROM sheet_vitrina_v1_warehouse_supplier_cost_states
                    WHERE version_id=?
                    """,
                    (version_id, now, base_version_id),
                )
                conn.execute(
                    """
                    DELETE FROM sheet_vitrina_v1_warehouse_supplier_cost_states
                    WHERE version_id=? AND shipment_id=?
                    """,
                    (version_id, shipment_id),
                )
                cost_state = dict(plan["supplier_cost_state"])
                conn.execute(
                    """
                    INSERT INTO sheet_vitrina_v1_warehouse_supplier_cost_states(
                        version_id,shipment_id,source_fingerprint,calculation_fingerprint,
                        expenses_complete,calculation_available,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        version_id,
                        shipment_id,
                        str(cost_state.get("source_fingerprint") or ""),
                        str(cost_state.get("calculation_fingerprint") or ""),
                        int(bool(cost_state.get("expenses_complete"))),
                        int(bool(cost_state.get("calculation_available"))),
                        now,
                    ),
                )
                for stage in sorted(
                    {
                        str(item["warehouse_key"])
                        for item in conn.execute(
                            """
                            SELECT DISTINCT warehouse_key
                            FROM sheet_vitrina_v1_warehouse_functional_balances
                            WHERE version_id=?
                            """,
                            (version_id,),
                        ).fetchall()
                    }
                ):
                    document_id = (
                        "whdoc_target_"
                        + _hash({"version_id": version_id, "stage": stage})[:20]
                    )
                    stage_rows = conn.execute(
                        """
                        SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                        WHERE version_id=? AND warehouse_key=?
                        ORDER BY nm_id
                        """,
                        (version_id, stage),
                    ).fetchall()
                    quantity = sum(
                        (_decimal(item["quantity"]) for item in stage_rows),
                        ZERO,
                    )
                    capital = sum(
                        (_decimal(item["capital_rub"]) for item in stage_rows),
                        ZERO,
                    )
                    conn.execute(
                        """
                        INSERT INTO sheet_vitrina_v1_warehouse_functional_documents(
                            document_id,version_id,warehouse_key,document_type,
                            occurred_at,source_id,source_fingerprint,quantity,
                            capital_rub,provenance_json,created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            document_id,
                            version_id,
                            stage,
                            "targeted_supplier_replay",
                            now,
                            publication_id,
                            _fingerprint(
                                [
                                    {
                                        "nm_id": int(item["nm_id"]),
                                        "quantity": item["quantity"],
                                        "capital_rub": item["capital_rub"],
                                        "provenance_json": item["provenance_json"],
                                    }
                                    for item in stage_rows
                                ]
                            ),
                            _text(quantity),
                            _text(capital),
                            _json(
                                {
                                    "base_version_id": base_version_id,
                                    "stable_source_id": (
                                        f"supplier_shipment:{shipment_id}"
                                    ),
                                    "targeted_replay": True,
                                }
                            ),
                            now,
                        ),
                    )
                    for item in stage_rows:
                        conn.execute(
                            """
                            INSERT INTO sheet_vitrina_v1_warehouse_functional_document_lines(
                                line_id,document_id,version_id,nm_id,quantity,
                                wac_rub,capital_rub,provenance_json,created_at
                            ) VALUES(?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                "whdocline_target_"
                                + _hash(
                                    {
                                        "document_id": document_id,
                                        "nm_id": int(item["nm_id"]),
                                    }
                                )[:20],
                                document_id,
                                version_id,
                                int(item["nm_id"]),
                                item["quantity"],
                                item["wac_rub"],
                                item["capital_rub"],
                                item["provenance_json"],
                                now,
                            ),
                        )
                after_digest = _fingerprint(
                    _active_target_rows(
                        conn,
                        version_id=version_id,
                        nm_ids=plan["affected_nm_ids"],
                    )
                )
                if after_digest != str(plan["target_after_digest"]):
                    raise WarehouseTargetedReplayError(
                        "published target rows differ from reviewed plan"
                    )
                non_target_after = _non_target_digest(
                    conn,
                    version_id=version_id,
                    affected_nm_ids=plan["affected_nm_ids"],
                )
                if non_target_after != non_target_before:
                    raise WarehouseTargetedReplayError(
                        "targeted publication changed non-target balances"
                    )
                undo = {
                    "shipment_id": shipment_id,
                    "before_header": plan["before_header"],
                    "after_header": plan["after_header"],
                    "base_version_id": base_version_id,
                    "published_version_id": version_id,
                    "target_rows_before": plan["target_rows_before"],
                    "target_rows_after": plan["target_rows_after"],
                }
                undo_digest = _fingerprint(undo)
                conn.execute(
                    f"""
                    INSERT INTO {TARGETED_UNDO_TABLE}(
                        undo_id,publication_id,shipment_id,before_header_json,
                        after_header_json,base_version_id,published_version_id,
                        target_rows_before_json,target_rows_after_json,
                        manifest_digest,status,created_at,rolled_back_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'ready',?,NULL)
                    """,
                    (
                        "whtu_" + undo_digest.removeprefix("sha256:")[:24],
                        publication_id,
                        shipment_id,
                        _json(plan["before_header"]),
                        _json(plan["after_header"]),
                        base_version_id,
                        version_id,
                        _json(plan["target_rows_before"]),
                        _json(plan["target_rows_after"]),
                        undo_digest,
                        now,
                    ),
                )
                diagnostics = {
                    **dict(plan["performance"]),
                    "lock_wait_ms": int(lock_wait_ms),
                    "capacity": {
                        "required_bytes": required_bytes,
                        "free_bytes": free_bytes,
                    },
                    "queue_id": queue_id,
                    "rollback_manifest_digest": undo_digest,
                }
                conn.execute(
                    f"""
                    INSERT INTO {TARGETED_PUBLICATION_TABLE}(
                        publication_id,stable_source_id,source_revision,
                        earliest_business_date,affected_nm_ids_json,
                        base_version_id,version_id,plan_fingerprint,
                        target_before_digest,target_after_digest,
                        non_target_before_digest,non_target_after_digest,status,
                        blocker_summary_json,diagnostics_json,created_at,completed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        publication_id,
                        f"supplier_shipment:{shipment_id}",
                        str(plan["source_revision"]),
                        str(plan["earliest_business_date"]),
                        _json(plan["affected_nm_ids"]),
                        base_version_id,
                        version_id,
                        fingerprint,
                        str(plan["target_before_digest"]),
                        after_digest,
                        non_target_before,
                        non_target_after,
                        "complete",
                        _json(plan.get("blocker_summary") or []),
                        _json(diagnostics),
                        now,
                        now,
                    ),
                )
                self._inject("before_switch")
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_warehouse_functional_active
                    SET version_id=?,updated_at=? WHERE slot=1
                    """,
                    (version_id, now),
                )
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_warehouse_wb_sync_status
                    SET active_version_id=?,updated_at=? WHERE slot=1
                    """,
                    (version_id, now),
                )
                conn.execute(
                    f"""
                    UPDATE {QUEUE_TABLE}
                    SET status='complete',finished_at=?,error=NULL
                    WHERE queue_id=? AND status='running'
                    """,
                    (now, queue_id),
                )
                self._inject("before_commit")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            **dict(plan),
            "applied": True,
            "idempotent": False,
            "version_id": version_id,
            "publication_id": publication_id,
            "backup": {
                "kind": "target_scoped_before_image",
                "full_database_copy": False,
                "copy_bytes": 0,
            },
            "post_run": {
                "changed": 0,
                "idempotent": True,
                "target_digest": str(plan["target_after_digest"]),
                "non_target_digest": str(plan["non_target_digest"]),
            },
        }

    def rollback(self, *, manifest_digest: str) -> dict[str, Any]:
        """Restore the exact target header and active pointer from one manifest."""

        selected_digest = str(manifest_digest or "").strip()
        if not selected_digest.startswith("sha256:"):
            raise WarehouseTargetedReplayError(
                "exact targeted rollback manifest digest is required"
            )
        with warehouse_functional_write_lock(
            self.runtime.runtime_dir,
            timeout_seconds=300,
        ) as lock_info:
            now = self.timestamp_factory()
            with _connect(self.runtime.db_path) as conn:
                ensure_warehouse_targeted_replay_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    manifest = conn.execute(
                        f"""
                        SELECT * FROM {TARGETED_UNDO_TABLE}
                        WHERE manifest_digest=?
                        """,
                        (selected_digest,),
                    ).fetchone()
                    if manifest is None:
                        raise WarehouseTargetedReplayError(
                            "targeted rollback manifest was not found"
                        )
                    if str(manifest["status"]) == "rolled_back":
                        return {
                            "rolled_back": False,
                            "idempotent": True,
                            "manifest_digest": selected_digest,
                            "lock_wait_ms": int(lock_info["wait_ms"]),
                        }
                    if str(manifest["status"]) != "ready":
                        raise WarehouseTargetedReplayError(
                            "targeted rollback manifest is not ready"
                        )
                    publication = conn.execute(
                        f"""
                        SELECT * FROM {TARGETED_PUBLICATION_TABLE}
                        WHERE publication_id=?
                        """,
                        (str(manifest["publication_id"]),),
                    ).fetchone()
                    if publication is None:
                        raise WarehouseTargetedReplayError(
                            "targeted publication audit is missing"
                        )
                    active = conn.execute(
                        """
                        SELECT version_id
                        FROM sheet_vitrina_v1_warehouse_functional_active
                        WHERE slot=1
                        """
                    ).fetchone()
                    published_version_id = str(
                        manifest["published_version_id"]
                    )
                    if (
                        active is None
                        or str(active["version_id"]) != published_version_id
                    ):
                        raise WarehouseTargetedReplayError(
                            "targeted rollback rejected: published version is no longer active"
                        )
                    shipment_id = str(manifest["shipment_id"])
                    before_header = json.loads(
                        str(manifest["before_header_json"])
                    )
                    after_header = json.loads(
                        str(manifest["after_header_json"])
                    )
                    current_header, _, _, _ = _header_and_revision(
                        conn, shipment_id
                    )
                    for key in ("actual_shipment_date", "order_status"):
                        if str(current_header.get(key) or "") != str(
                            after_header.get(key) or ""
                        ):
                            raise WarehouseTargetedReplayError(
                                "targeted rollback rejected: target header changed after publication"
                            )
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_supplier_shipments
                        SET actual_shipment_date=?,order_status=?,updated_at=?
                        WHERE shipment_id=?
                        """,
                        (
                            before_header.get("actual_shipment_date"),
                            str(before_header.get("order_status") or ""),
                            str(before_header.get("updated_at") or now),
                            shipment_id,
                        ),
                    )
                    base_version_id = str(manifest["base_version_id"])
                    conn.execute(
                        """
                        UPDATE sheet_vitrina_v1_warehouse_functional_active
                        SET version_id=?,updated_at=? WHERE slot=1
                        """,
                        (base_version_id, now),
                    )
                    if conn.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type='table'
                          AND name='sheet_vitrina_v1_warehouse_wb_sync_status'
                        """
                    ).fetchone():
                        conn.execute(
                            """
                            UPDATE sheet_vitrina_v1_warehouse_wb_sync_status
                            SET active_version_id=?,updated_at=? WHERE slot=1
                            """,
                            (base_version_id, now),
                        )
                    conn.execute(
                        f"""
                        UPDATE {TARGETED_PUBLICATION_TABLE}
                        SET status='rolled_back',completed_at=?
                        WHERE publication_id=?
                        """,
                        (now, str(manifest["publication_id"])),
                    )
                    conn.execute(
                        f"""
                        UPDATE {TARGETED_UNDO_TABLE}
                        SET status='rolled_back',rolled_back_at=?
                        WHERE manifest_digest=? AND status='ready'
                        """,
                        (now, selected_digest),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        return {
            "rolled_back": True,
            "idempotent": False,
            "manifest_digest": selected_digest,
            "shipment_id": shipment_id,
            "restored_version_id": base_version_id,
            "lock_wait_ms": int(lock_info["wait_ms"]),
        }

    def _inject(self, phase: str) -> None:
        if self.failure_injector is not None:
            self.failure_injector(phase)
