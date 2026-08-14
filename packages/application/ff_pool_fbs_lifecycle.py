"""Exact FBS opening backfill and owner-gated post-cutover lifecycle.

The official WB collector is a read-only evidence source.  This module never
writes WB.  It folds immutable local observations into exact INTEGER quantity
and Decimal-text capital movements only after an applied cutover manifest has
activated the reviewed ``complete/sorted`` handoff rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
from typing import Any, Mapping

from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
    canonical_decimal_text,
    evaluate_ff_pool_aggregate_parity,
    ensure_ff_pool_foundation_schema,
    record_ff_pool_parity_diagnostic,
)
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_CURRENT_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    ensure_wb_fbs_orders_schema,
)


CONTRACT_NAME = "ff_pool_fbs_lifecycle_v1"
EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_events"
CURRENT_TABLE = "sheet_vitrina_v1_ff_pool_fbs_lifecycle_current"
RECONCILIATION_TABLE = "sheet_vitrina_v1_ff_pool_fbs_reconciliation_lane"

HANDOFF_SUPPLIER_STATUS = "complete"
HANDOFF_WB_STATUS = "sorted"
CANCELLATION_SUPPLIER_STATUSES = frozenset({"cancel"})
CANCELLATION_WB_STATUSES = frozenset(
    {"canceled", "canceled_by_client", "declined_by_client", "defect"}
)
LATER_TERMINAL_WB_STATUSES = frozenset({"sold", "accepted_by_client"})
EVENT_TYPES = (
    "opening_reserve",
    "opening_handoff_debit",
    "opening_cancel_noop",
    "cancel_noop",
    "reserve",
    "release",
    "handoff_debit",
    "terminal_noop",
    "post_handoff_reconciliation",
    "late_pre_t_isolated",
)
STATES = (
    "reserved",
    "released",
    "fulfilled",
    "fulfilled_reconciliation",
    "cancelled_noop",
    "late_pre_t_isolated",
)
ZERO = Decimal("0")


class FfPoolFbsLifecycleError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def ensure_ff_pool_fbs_lifecycle_schema(conn: sqlite3.Connection) -> None:
    ensure_ff_pool_foundation_schema(conn)
    ensure_wb_fbs_orders_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {EVENTS_TABLE}(
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL CHECK(typeof(order_id)='integer' AND order_id>0),
            episode_sequence INTEGER NOT NULL
                CHECK(typeof(episode_sequence)='integer' AND episode_sequence>0),
            event_type TEXT NOT NULL CHECK(event_type IN ({_sql_values(EVENT_TYPES)})),
            source_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            supplier_status TEXT NOT NULL,
            wb_status TEXT NOT NULL,
            source_observed_at TEXT NOT NULL,
            facility_id TEXT NOT NULL,
            pool TEXT NOT NULL DEFAULT 'FBS' CHECK(pool='FBS'),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            physical_quantity_delta INTEGER NOT NULL
                CHECK(typeof(physical_quantity_delta)='integer'),
            capital_delta_rub TEXT NOT NULL CHECK({_decimal_check('capital_delta_rub')}),
            frozen_wac_rub TEXT NOT NULL CHECK({_decimal_check('frozen_wac_rub')}),
            evidence_digest TEXT NOT NULL,
            occurred_at TEXT NOT NULL
                CHECK(substr(occurred_at,-1,1)='Z' AND julianday(occurred_at) IS NOT NULL),
            details_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(details_json)),
            UNIQUE(cutover_id,order_id,episode_sequence,event_type,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_events_by_order
        ON {EVENTS_TABLE}(cutover_id,order_id,event_sequence);
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_events_by_location
        ON {EVENTS_TABLE}(facility_id,pool,nm_id,event_sequence);

        CREATE TABLE IF NOT EXISTS {CURRENT_TABLE}(
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            state TEXT NOT NULL CHECK(state IN ({_sql_values(STATES)})),
            episode_sequence INTEGER NOT NULL,
            source_revision TEXT NOT NULL,
            status_digest TEXT NOT NULL,
            supplier_status TEXT NOT NULL,
            wb_status TEXT NOT NULL,
            facility_id TEXT NOT NULL,
            pool TEXT NOT NULL DEFAULT 'FBS' CHECK(pool='FBS'),
            nm_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            frozen_wac_rub TEXT NOT NULL CHECK({_decimal_check('frozen_wac_rub')}),
            debit_event_id TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY(cutover_id,order_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_current_by_location
        ON {CURRENT_TABLE}(cutover_id,facility_id,pool,nm_id,state,order_id);

        CREATE TABLE IF NOT EXISTS {RECONCILIATION_TABLE}(
            reconciliation_id TEXT PRIMARY KEY,
            cutover_id TEXT NOT NULL,
            order_id INTEGER NOT NULL,
            event_id TEXT NOT NULL UNIQUE REFERENCES {EVENTS_TABLE}(event_id),
            reason_code TEXT NOT NULL,
            evidence_digest TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open' CHECK(state='open'),
            created_at TEXT NOT NULL,
            UNIQUE(cutover_id,order_id,evidence_digest)
        );

        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_events_no_update
        BEFORE UPDATE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS lifecycle evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_events_no_delete
        BEFORE DELETE ON {EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS lifecycle evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_reconciliation_no_update
        BEFORE UPDATE ON {RECONCILIATION_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS reconciliation evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_reconciliation_no_delete
        BEFORE DELETE ON {RECONCILIATION_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS reconciliation evidence is append-only'); END;
        """
    )


def classify_pre_t_status(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    cutover_at: str,
) -> dict[str, Any]:
    """Classify one order from immutable status observations visible by T."""

    rows = conn.execute(
        f"""SELECT order_revision,status_digest,supplier_status,wb_status,
                   positive_quantity,observed_at
            FROM {STATUS_OBSERVATIONS_TABLE}
            WHERE order_id=? AND observed_at<=?
            ORDER BY observation_sequence""",
        (int(order_id), str(cutover_at)),
    ).fetchall()
    if not rows:
        return {"classification": "unmatched", "evidence": None}
    handoff_index = next(
        (
            index
            for index, row in enumerate(rows)
            if str(row[2]) == HANDOFF_SUPPLIER_STATUS
            and str(row[3]) == HANDOFF_WB_STATUS
        ),
        None,
    )
    handoff = None if handoff_index is None else rows[handoff_index]
    reconciliation = None
    if handoff is not None:
        classification = "pre_t_handoff_debit"
        evidence = handoff
        reconciliation = next(
            (
                row
                for row in reversed(rows[handoff_index + 1 :])
                if _is_cancellation(str(row[2]), str(row[3]))
            ),
            None,
        )
    else:
        latest = rows[-1]
        if _is_cancellation(str(latest[2]), str(latest[3])):
            classification = "pre_t_cancelled_noop"
        else:
            classification = "pre_t_absorbed_reservation"
        evidence = latest
    result = {
        "classification": classification,
        "evidence": {
            "source_revision": str(evidence[0]),
            "status_digest": str(evidence[1]),
            "supplier_status": str(evidence[2]),
            "wb_status": str(evidence[3]),
            "quantity": int(evidence[4]),
            "observed_at": str(evidence[5]),
        },
    }
    if reconciliation is not None:
        result["post_handoff_reconciliation"] = {
            "source_revision": str(reconciliation[0]),
            "status_digest": str(reconciliation[1]),
            "supplier_status": str(reconciliation[2]),
            "wb_status": str(reconciliation[3]),
            "quantity": int(reconciliation[4]),
            "observed_at": str(reconciliation[5]),
        }
    return result


def apply_opening_fbs_backfill(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    occurred_at: str,
) -> dict[str, Any]:
    """Fold the exact pre-T checkpoint once inside the cutover transaction."""

    _require_approved_policy(manifest)
    cutover_id = str(manifest["cutover_id"])
    counts = {"debit": 0, "reservation": 0, "cancel_noop": 0, "reconciliation": 0}
    quantity = {"debit": 0, "reservation": 0}
    capital = ZERO
    for order in sorted(
        (dict(item) for item in manifest.get("order_classifications") or []),
        key=lambda item: int(item["order_id"]),
    ):
        classification = str(order["classification"])
        if classification == "unmatched" or classification == "post_t_deferred":
            continue
        wac = _frozen_wac(manifest, order)
        evidence = {
            "cutover_id": cutover_id,
            "order_id": int(order["order_id"]),
            "source_revision": str(order["source_revision"]),
            "status_digest": str(order["status_fingerprint"]),
            "classification": classification,
        }
        status_evidence = dict(order.get("status_evidence") or {})
        supplier_status = str(status_evidence.get("supplier_status") or "")
        wb_status = str(status_evidence.get("wb_status") or "")
        status_digest = str(
            status_evidence.get("status_digest") or order["status_fingerprint"]
        )
        status_revision = str(
            status_evidence.get("source_revision") or order["source_revision"]
        )
        status_observed_at = str(
            status_evidence.get("observed_at") or order["observed_at"]
        )
        if classification in {"pre_t_handoff_debit", "pre_t_absorbed_closed"}:
            event = _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=1,
                event_type="opening_handoff_debit",
                state="fulfilled",
                supplier_status=supplier_status,
                wb_status=wb_status,
                status_digest=status_digest,
                source_revision=status_revision,
                source_observed_at=status_observed_at,
                wac=wac,
                physical_delta=-int(order["quantity"]),
                occurred_at=occurred_at,
                evidence=evidence,
            )
            if not event["idempotent"]:
                _apply_exact_physical_delta(
                    conn,
                    manifest=manifest,
                    order=order,
                    event_id=str(event["event_id"]),
                    quantity_delta=-int(order["quantity"]),
                    wac=wac,
                    occurred_at=occurred_at,
                )
            counts["debit"] += 1
            quantity["debit"] += int(order["quantity"])
            capital += wac * Decimal(int(order["quantity"]))
            reconciliation_evidence = dict(
                order.get("post_handoff_reconciliation") or {}
            )
            if reconciliation_evidence:
                reconciliation_event = _append_event(
                    conn,
                    manifest=manifest,
                    order=order,
                    episode_sequence=1,
                    event_type="post_handoff_reconciliation",
                    state="fulfilled_reconciliation",
                    supplier_status=str(
                        reconciliation_evidence["supplier_status"]
                    ),
                    wb_status=str(reconciliation_evidence["wb_status"]),
                    status_digest=str(reconciliation_evidence["status_digest"]),
                    source_revision=str(
                        reconciliation_evidence["source_revision"]
                    ),
                    source_observed_at=str(
                        reconciliation_evidence["observed_at"]
                    ),
                    wac=wac,
                    physical_delta=0,
                    occurred_at=occurred_at,
                    evidence={
                        **evidence,
                        "lane": "post_handoff_cancellation_or_return",
                        "reconciliation_status": reconciliation_evidence,
                    },
                )
                _persist_reconciliation(
                    conn,
                    manifest=manifest,
                    order=order,
                    event=reconciliation_event,
                    created_at=occurred_at,
                )
                counts["reconciliation"] += 1
        elif classification == "pre_t_absorbed_reservation":
            _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=1,
                event_type="opening_reserve",
                state="reserved",
                supplier_status=supplier_status,
                wb_status=wb_status,
                status_digest=status_digest,
                source_revision=status_revision,
                source_observed_at=status_observed_at,
                wac=wac,
                physical_delta=0,
                occurred_at=occurred_at,
                evidence=evidence,
            )
            counts["reservation"] += 1
            quantity["reservation"] += int(order["quantity"])
        elif classification == "pre_t_cancelled_noop":
            _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=1,
                event_type="opening_cancel_noop",
                state="cancelled_noop",
                supplier_status=supplier_status,
                wb_status=wb_status,
                status_digest=status_digest,
                source_revision=status_revision,
                source_observed_at=status_observed_at,
                wac=wac,
                physical_delta=0,
                occurred_at=occurred_at,
                evidence=evidence,
            )
            counts["cancel_noop"] += 1
        else:
            raise FfPoolFbsLifecycleError(
                "unsupported_opening_classification",
                f"Unsupported opening classification: {classification}",
            )
    return {
        "counts": counts,
        "quantity": quantity,
        "debit_capital_rub": canonical_decimal_text(capital),
    }


def process_post_t_fbs_lifecycle(
    conn: sqlite3.Connection,
    *,
    occurred_at: str | None = None,
    limit: int = 500,
    schema_ready: bool = False,
) -> dict[str, Any]:
    """Process bounded post-T evidence; default hard-off before cutover apply."""

    if not schema_ready:
        ensure_ff_pool_fbs_lifecycle_schema(conn)
    manifest_row = conn.execute(
        """SELECT manifest_json FROM sheet_vitrina_v1_ff_pool_cutover_manifests
           ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"""
    ).fetchone()
    if manifest_row is None:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "disabled",
            "reason": "cutover_not_applied",
            "mutates_wb": False,
        }
    manifest = json.loads(str(manifest_row[0]))
    _require_approved_policy(manifest)
    feature = conn.execute(
        f"SELECT writer_enabled,reader_enabled,source_revision FROM {FEATURE_EPOCHS_TABLE} "
        "WHERE epoch=?",
        (int(manifest["feature_epoch"]),),
    ).fetchone()
    if feature is None or int(feature[0]) != 1:
        return {
            "contract_name": CONTRACT_NAME,
            "status": "disabled",
            "reason": "writer_epoch_not_active",
            "mutates_wb": False,
        }
    now = _utc_now() if occurred_at is None else str(occurred_at)
    _require_utc(now)
    bound = max(1, min(int(limit), 500))
    cutover_id = str(manifest["cutover_id"])
    latest_orders = conn.execute(
        f"""SELECT observation_id,order_id,source_revision,source_created_at,
                   observed_at,warehouse_id,nm_id,chrt_id,skus_json
            FROM {OBSERVATIONS_TABLE} AS source
            WHERE observation_sequence=(
                SELECT MAX(latest.observation_sequence) FROM {OBSERVATIONS_TABLE} AS latest
                WHERE latest.order_id=source.order_id
            )
              AND NOT EXISTS(
                  SELECT 1
                  FROM sheet_vitrina_v1_ff_pool_cutover_order_classifications AS opening_order
                  WHERE opening_order.cutover_id=? AND opening_order.order_id=source.order_id
              )
              AND EXISTS(
                  SELECT 1 FROM {STATUS_CURRENT_TABLE} AS status
                  LEFT JOIN {CURRENT_TABLE} AS lifecycle
                    ON lifecycle.cutover_id=? AND lifecycle.order_id=status.order_id
                  WHERE status.order_id=source.order_id
                    AND (lifecycle.order_id IS NULL
                         OR lifecycle.status_digest<>status.status_digest
                         OR lifecycle.episode_sequence<>status.episode_sequence)
              )
            ORDER BY order_id LIMIT ?""",
        (cutover_id, cutover_id, bound),
    ).fetchall()
    summary = {
        "reserved": 0,
        "reservation_refreshed": 0,
        "released": 0,
        "fulfilled": 0,
        "terminal_noop": 0,
        "reconciliation": 0,
        "late_pre_t": 0,
    }
    for raw in latest_orders:
        order_id = int(raw[1])
        source_created_at = str(raw[3] or "")
        if not source_created_at:
            raise FfPoolFbsLifecycleError(
                "source_created_at_missing",
                f"Order {order_id} cannot cross the lifecycle boundary without source time",
            )
        mapped = _map_order(conn, manifest, raw)
        current_status = conn.execute(
            f"""SELECT current.order_revision,current.status_digest,
                       current.supplier_status,current.wb_status,
                       current.local_last_seen_at,current.episode_sequence,
                       evidence.positive_quantity
                FROM {STATUS_CURRENT_TABLE} AS current
                JOIN {STATUS_OBSERVATIONS_TABLE} AS evidence
                  ON evidence.order_id=current.order_id
                 AND evidence.order_revision=current.order_revision
                 AND evidence.status_digest=current.status_digest
                WHERE current.order_id=?""",
            (order_id,),
        ).fetchone()
        if current_status is None:
            continue
        if _parse_utc(source_created_at) <= _parse_utc(str(manifest["cutover_at"])):
            order = _order_payload(raw, mapped, quantity=int(current_status[6]))
            event = _append_event(
                conn,
                manifest=manifest,
                order=order,
                episode_sequence=int(current_status[5]),
                event_type="late_pre_t_isolated",
                state="late_pre_t_isolated",
                supplier_status=str(current_status[2]),
                wb_status=str(current_status[3]),
                status_digest=str(current_status[1]),
                source_revision=str(current_status[0]),
                source_observed_at=str(current_status[4]),
                wac=_frozen_wac(manifest, order),
                physical_delta=0,
                occurred_at=now,
                evidence={"late_pre_t": True, "order_revision": str(current_status[0])},
            )
            if not event["idempotent"]:
                _persist_late_pre_t(conn, manifest=manifest, order=order, event=event, detected_at=now)
                summary["late_pre_t"] += 1
            continue
        order = _order_payload(raw, mapped, quantity=int(current_status[6]))
        state_row = conn.execute(
            f"SELECT state,debit_event_id FROM {CURRENT_TABLE} WHERE cutover_id=? AND order_id=?",
            (cutover_id, order_id),
        ).fetchone()
        supplier_status = str(current_status[2])
        wb_status = str(current_status[3])
        episode = int(current_status[5])
        wac = _frozen_wac(manifest, order)
        common = dict(
            conn=conn,
            manifest=manifest,
            order=order,
            episode_sequence=episode,
            supplier_status=supplier_status,
            wb_status=wb_status,
            status_digest=str(current_status[1]),
            source_revision=str(current_status[0]),
            source_observed_at=str(current_status[4]),
            wac=wac,
            occurred_at=now,
            evidence={
                "order_revision": str(current_status[0]),
                "status_digest": str(current_status[1]),
                "supplier_status": supplier_status,
                "wb_status": wb_status,
            },
        )
        if state_row is None:
            if _is_cancellation(supplier_status, wb_status):
                _append_event(
                    **common,
                    event_type="cancel_noop",
                    state="cancelled_noop",
                    physical_delta=0,
                )
                continue
            reserve = _append_event(
                **common, event_type="reserve", state="reserved", physical_delta=0
            )
            if not reserve["idempotent"]:
                summary["reserved"] += 1
            state_row = ("reserved", "")
        state = str(state_row[0])
        if state in {"released", "cancelled_noop"} and not _is_cancellation(
            supplier_status, wb_status
        ):
            reserve = _append_event(
                **common, event_type="reserve", state="reserved", physical_delta=0
            )
            if not reserve["idempotent"]:
                summary["reserved"] += 1
            state = "reserved"
        if state == "reserved" and _is_cancellation(supplier_status, wb_status):
            event = _append_event(
                **common, event_type="release", state="released", physical_delta=0
            )
            if not event["idempotent"]:
                summary["released"] += 1
        elif state == "reserved" and _is_handoff(supplier_status, wb_status):
            event = _append_event(
                **common,
                event_type="handoff_debit",
                state="fulfilled",
                physical_delta=-int(order["quantity"]),
            )
            if not event["idempotent"]:
                _apply_exact_physical_delta(
                    conn,
                    manifest=manifest,
                    order=order,
                    event_id=str(event["event_id"]),
                    quantity_delta=-int(order["quantity"]),
                    wac=wac,
                    occurred_at=now,
                )
                summary["fulfilled"] += 1
        elif state == "reserved":
            refresh = _append_event(
                **common,
                event_type="reserve",
                state="reserved",
                physical_delta=0,
            )
            if not refresh["idempotent"]:
                summary["reservation_refreshed"] += 1
        elif state in {"fulfilled", "fulfilled_reconciliation"} and _is_cancellation(
            supplier_status, wb_status
        ):
            event = _append_event(
                **common,
                event_type="post_handoff_reconciliation",
                state="fulfilled_reconciliation",
                physical_delta=0,
            )
            if not event["idempotent"]:
                _persist_reconciliation(conn, manifest=manifest, order=order, event=event, created_at=now)
                summary["reconciliation"] += 1
        elif state in {"fulfilled", "fulfilled_reconciliation"} and wb_status in LATER_TERMINAL_WB_STATUSES:
            event = _append_event(
                **common, event_type="terminal_noop", state=state, physical_delta=0
            )
            if not event["idempotent"]:
                summary["terminal_noop"] += 1
    if summary["fulfilled"]:
        _record_current_parity(conn, manifest=manifest, checked_at=now)
    return {
        "contract_name": CONTRACT_NAME,
        "status": "processed",
        "cutover_id": cutover_id,
        "summary": summary,
        "mutates_wb": False,
    }


def available_quantity(
    conn: sqlite3.Connection,
    *,
    cutover_id: str,
    facility_id: str,
    nm_id: int,
) -> dict[str, int]:
    physical_row = conn.execute(
        f"SELECT quantity FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?",
        (facility_id, int(nm_id)),
    ).fetchone()
    physical = int(physical_row[0]) if physical_row else 0
    reserved = int(
        conn.execute(
            f"""SELECT COALESCE(SUM(quantity),0) FROM {CURRENT_TABLE}
                WHERE cutover_id=? AND facility_id=? AND pool='FBS' AND nm_id=?
                  AND state='reserved'""",
            (cutover_id, facility_id, int(nm_id)),
        ).fetchone()[0]
    )
    return {"physical": physical, "reserved": reserved, "available": physical - reserved}


def _append_event(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    episode_sequence: int,
    event_type: str,
    state: str,
    supplier_status: str,
    wb_status: str,
    status_digest: str,
    source_revision: str,
    source_observed_at: str,
    wac: Decimal,
    physical_delta: int,
    occurred_at: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    cutover_id = str(manifest["cutover_id"])
    quantity = int(order["quantity"])
    capital_delta = wac * Decimal(physical_delta)
    evidence_digest = _fingerprint(evidence)
    identity = {
        "cutover_id": cutover_id,
        "order_id": int(order["order_id"]),
        "episode_sequence": int(episode_sequence),
        "event_type": event_type,
        "evidence_digest": evidence_digest,
    }
    event_id = "ffbf_" + _fingerprint(identity).removeprefix("sha256:")[:28]
    inserted = conn.execute(
        f"""INSERT OR IGNORE INTO {EVENTS_TABLE}(
                event_id,cutover_id,order_id,episode_sequence,event_type,
                source_revision,status_digest,supplier_status,wb_status,
                source_observed_at,facility_id,pool,nm_id,quantity,
                physical_quantity_delta,capital_delta_rub,frozen_wac_rub,
                evidence_digest,occurred_at,details_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            cutover_id,
            int(order["order_id"]),
            int(episode_sequence),
            event_type,
            source_revision,
            status_digest,
            supplier_status,
            wb_status,
            source_observed_at,
            str(order["facility_id"]),
            "FBS",
            int(order["nm_id"]),
            quantity,
            int(physical_delta),
            canonical_decimal_text(capital_delta),
            canonical_decimal_text(wac),
            evidence_digest,
            occurred_at,
            _json(evidence),
        ),
    ).rowcount
    if inserted:
        debit_event_id = event_id if physical_delta < 0 else ""
        conn.execute(
            f"""INSERT INTO {CURRENT_TABLE}(
                    cutover_id,order_id,state,episode_sequence,source_revision,
                    status_digest,supplier_status,wb_status,facility_id,pool,nm_id,
                    quantity,frozen_wac_rub,debit_event_id,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(cutover_id,order_id) DO UPDATE SET
                    state=excluded.state,
                    episode_sequence=excluded.episode_sequence,
                    source_revision=excluded.source_revision,
                    status_digest=excluded.status_digest,
                    supplier_status=excluded.supplier_status,
                    wb_status=excluded.wb_status,
                    facility_id=excluded.facility_id,
                    pool=excluded.pool,
                    nm_id=excluded.nm_id,
                    quantity=excluded.quantity,
                    frozen_wac_rub=excluded.frozen_wac_rub,
                    debit_event_id=CASE WHEN excluded.debit_event_id<>''
                        THEN excluded.debit_event_id ELSE {CURRENT_TABLE}.debit_event_id END,
                    updated_at=excluded.updated_at""",
            (
                cutover_id,
                int(order["order_id"]),
                state,
                int(episode_sequence),
                source_revision,
                status_digest,
                supplier_status,
                wb_status,
                str(order["facility_id"]),
                "FBS",
                int(order["nm_id"]),
                quantity,
                canonical_decimal_text(wac),
                debit_event_id,
                occurred_at,
            ),
        )
    return {"event_id": event_id, "idempotent": not bool(inserted)}


def _apply_exact_physical_delta(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    event_id: str,
    quantity_delta: int,
    wac: Decimal,
    occurred_at: str,
) -> None:
    facility_id = str(order["facility_id"])
    nm_id = int(order["nm_id"])
    epoch = int(manifest["feature_epoch"])
    balance = conn.execute(
        f"""SELECT quantity,capital_rub FROM {BALANCES_TABLE}
            WHERE facility_id=? AND pool='FBS' AND nm_id=? AND projection_epoch=?""",
        (facility_id, nm_id, epoch),
    ).fetchone()
    if balance is None:
        raise FfPoolFbsLifecycleError(
            "fbs_balance_missing",
            f"FBS balance is missing for {facility_id}/{nm_id}",
        )
    capital_delta = wac * Decimal(quantity_delta)
    new_quantity = int(balance[0]) + int(quantity_delta)
    new_capital = Decimal(str(balance[1])) + capital_delta
    operation_id = "ffbo_" + hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:28]
    conn.execute(
        f"""INSERT INTO {OPERATIONS_TABLE}(
                operation_id,operation_type,source_system,source_type,source_id,
                source_revision,idempotency_epoch,business_date,posted_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            "fbs_physical_handoff",
            "official_wb_status_shadow",
            "fbs_order_lifecycle_event",
            str(order["order_id"]),
            event_id,
            epoch,
            str(manifest["business_date"]),
            occurred_at,
            _json({"event_id": event_id, "mutates_wb": False}),
        ),
    )
    conn.execute(
        f"""INSERT INTO {LINES_TABLE}(
                operation_id,line_no,facility_id,pool,nm_id,quantity_delta,
                capital_delta_rub,wac_snapshot_rub,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            1,
            facility_id,
            "FBS",
            nm_id,
            int(quantity_delta),
            canonical_decimal_text(capital_delta),
            canonical_decimal_text(wac),
            _json({"order_id": int(order["order_id"]), "event_id": event_id}),
        ),
    )
    conn.execute(
        f"""UPDATE {BALANCES_TABLE}
            SET quantity=?,capital_rub=?,wac_rub=?,source_watermark=?,updated_at=?
            WHERE facility_id=? AND pool='FBS' AND nm_id=? AND projection_epoch=?""",
        (
            new_quantity,
            canonical_decimal_text(new_capital),
            canonical_decimal_text(wac),
            event_id,
            occurred_at,
            facility_id,
            nm_id,
            epoch,
        ),
    )
    _apply_exact_aggregate_projection(
        conn,
        nm_id=nm_id,
        quantity_delta=quantity_delta,
        capital_delta=capital_delta,
    )


def _apply_exact_aggregate_projection(
    conn: sqlite3.Connection,
    *,
    nm_id: int,
    quantity_delta: int,
    capital_delta: Decimal,
) -> None:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolFbsLifecycleError("aggregate_active_missing", "Aggregate FF version is missing")
    row = conn.execute(
        """SELECT quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances
           WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
        (str(active[0]), int(nm_id)),
    ).fetchone()
    if row is None:
        raise FfPoolFbsLifecycleError(
            "aggregate_sku_missing", f"Aggregate FF SKU is missing: {nm_id}"
        )
    quantity = _exact_int(row[0], "aggregate.quantity") + int(quantity_delta)
    capital = Decimal(str(row[1])) + capital_delta
    columns = {
        str(item[1])
        for item in conn.execute(
            "PRAGMA table_info(sheet_vitrina_v1_warehouse_functional_balances)"
        ).fetchall()
    }
    if "wac_rub" in columns:
        wac = None if quantity == 0 else canonical_decimal_text(capital / Decimal(quantity))
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_balances
               SET quantity=?,capital_rub=?,wac_rub=?
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (canonical_decimal_text(Decimal(quantity)), canonical_decimal_text(capital), wac, str(active[0]), int(nm_id)),
        )
    else:
        conn.execute(
            """UPDATE sheet_vitrina_v1_warehouse_functional_balances
               SET quantity=?,capital_rub=?
               WHERE version_id=? AND warehouse_key='ff' AND nm_id=?""",
            (canonical_decimal_text(Decimal(quantity)), canonical_decimal_text(capital), str(active[0]), int(nm_id)),
        )


def _record_current_parity(
    conn: sqlite3.Connection, *, manifest: Mapping[str, Any], checked_at: str
) -> None:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    if active is None:
        raise FfPoolFbsLifecycleError("aggregate_active_missing", "Aggregate FF version is missing")
    aggregate_rows = [
        {
            "nm_id": int(row[0]),
            "quantity": _exact_int(row[1], "aggregate.quantity"),
            "capital_rub": canonical_decimal_text(row[2]),
        }
        for row in conn.execute(
            """SELECT nm_id,quantity,capital_rub
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
            (str(active[0]),),
        ).fetchall()
    ]
    parity = evaluate_ff_pool_aggregate_parity(conn, aggregate_rows)
    if parity.status != "pass":
        raise FfPoolFbsLifecycleError(
            "post_t_parity_failed",
            "Post-T FBS debit diverged from aggregate FF",
            details={"mismatched_nm_ids": list(parity.mismatched_nm_ids)},
        )
    sequence = int(conn.execute(f"SELECT COALESCE(MAX(event_sequence),0) FROM {EVENTS_TABLE}").fetchone()[0])
    record_ff_pool_parity_diagnostic(
        conn,
        diagnostic_id="ffpar_fbs_" + _fingerprint(
            {"cutover_id": manifest["cutover_id"], "event_sequence": sequence}
        ).removeprefix("sha256:")[:24],
        aggregate_revision=str(manifest["aggregate_revision"]),
        checked_at=checked_at,
        result=parity,
        details={"source": "post_t_fbs_lifecycle", "event_sequence": sequence},
    )


def _map_order(
    conn: sqlite3.Connection, manifest: Mapping[str, Any], row: sqlite3.Row
) -> dict[str, Any]:
    warehouse_id = int(row[5] or 0)
    # Mapping values in the cutover manifest are necessary but not sufficient:
    # every order revision must also carry its immutable matched identity proof.
    # ``row`` comes from the latest exact order observation.
    identity = conn.execute(
        f"""SELECT outcome,warehouse_id,nm_id,chrt_id,
                   warehouse_mapping_id,identity_mapping_id
            FROM {IDENTITY_EVIDENCE_TABLE}
            WHERE order_id=? AND order_revision=?
            ORDER BY evidence_sequence DESC LIMIT 1""",
        (int(row[1]), str(row[2])),
    ).fetchone()
    if (
        identity is None
        or str(identity[0]) != "matched"
        or int(identity[1] or 0) != warehouse_id
        or int(identity[2] or 0) != int(row[6])
        or int(identity[3] or 0) != int(row[7] or 0)
        or not str(identity[4] or "")
        or not str(identity[5] or "")
    ):
        raise FfPoolFbsLifecycleError(
            "order_identity_evidence_missing_or_drifted",
            f"Order {int(row[1])} lacks exact matched identity evidence",
        )
    facility_by_warehouse = {
        int(item["warehouse_id"]): str(item["facility_id"])
        for item in manifest.get("seller_warehouse_mappings") or []
    }
    facility_id = facility_by_warehouse.get(warehouse_id)
    if not facility_id:
        raise FfPoolFbsLifecycleError(
            "order_warehouse_unmapped", f"Order {int(row[1])} warehouse is unmapped"
        )
    source_nm_id = int(row[6])
    chrt_id = int(row[7] or 0)
    mapping = next(
        (
            item
            for item in manifest.get("sku_mappings") or []
            if int(item["nm_id"]) == source_nm_id and int(item["chrt_id"]) == chrt_id
        ),
        None,
    )
    if mapping is None:
        raise FfPoolFbsLifecycleError(
            "order_sku_unmapped", f"Order {int(row[1])} identity is unmapped"
        )
    return {
        "facility_id": facility_id,
        "nm_id": int(mapping.get("target_nm_id", mapping.get("nm_id", source_nm_id))),
    }


def _order_payload(
    row: sqlite3.Row, mapped: Mapping[str, Any], *, quantity: int
) -> dict[str, Any]:
    return {
        "order_id": int(row[1]),
        "observation_id": str(row[0]),
        "source_revision": str(row[2]),
        "source_created_at": str(row[3]),
        "observed_at": str(row[4]),
        "facility_id": str(mapped["facility_id"]),
        "pool": "FBS",
        "nm_id": int(mapped["nm_id"]),
        "quantity": int(quantity),
    }


def _frozen_wac(manifest: Mapping[str, Any], order: Mapping[str, Any]) -> Decimal:
    match = next(
        (
            item
            for item in manifest.get("allocations") or []
            if str(item["facility_id"]) == str(order["facility_id"])
            and str(item["pool"]) == "FBS"
            and int(item["nm_id"]) == int(order["nm_id"])
        ),
        None,
    )
    if match is None or match.get("wac_rub") is None:
        raise FfPoolFbsLifecycleError(
            "frozen_wac_missing",
            f"Opening WAC is missing for {order['facility_id']}/{order['nm_id']}",
        )
    return Decimal(str(match["wac_rub"]))


def _persist_late_pre_t(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    event: Mapping[str, Any],
    detected_at: str,
) -> None:
    evidence = {
        "cutover_id": str(manifest["cutover_id"]),
        "order_id": int(order["order_id"]),
        "source_revision": str(order["source_revision"]),
        "event_id": str(event["event_id"]),
    }
    conn.execute(
        """INSERT OR IGNORE INTO sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases(
               case_id,cutover_id,order_id,observation_id,source_revision,
               source_created_at,observed_at,detected_at,state,reason_code,
               display_reason,evidence_digest
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "late_" + _fingerprint(evidence).removeprefix("sha256:")[:28],
            str(manifest["cutover_id"]),
            int(order["order_id"]),
            str(order["observation_id"]),
            str(order["source_revision"]),
            str(order["source_created_at"]),
            str(order["observed_at"]),
            detected_at,
            "isolated",
            "late_pre_t",
            "Поздний заказ до границы",
            _fingerprint(evidence),
        ),
    )


def _persist_reconciliation(
    conn: sqlite3.Connection,
    *,
    manifest: Mapping[str, Any],
    order: Mapping[str, Any],
    event: Mapping[str, Any],
    created_at: str,
) -> None:
    evidence = {
        "event_id": str(event["event_id"]),
        "reason": "post_handoff_cancellation_or_return",
    }
    conn.execute(
        f"""INSERT OR IGNORE INTO {RECONCILIATION_TABLE}(
               reconciliation_id,cutover_id,order_id,event_id,reason_code,
               evidence_digest,state,created_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            "ffbr_" + _fingerprint(evidence).removeprefix("sha256:")[:28],
            str(manifest["cutover_id"]),
            int(order["order_id"]),
            str(event["event_id"]),
            "post_handoff_cancellation_or_return",
            _fingerprint(evidence),
            "open",
            created_at,
        ),
    )


def _require_approved_policy(manifest: Mapping[str, Any]) -> None:
    policy = dict(manifest.get("handoff_policy") or {})
    if not (
        policy.get("approved") is True
        and str(policy.get("supplier_status") or "") == HANDOFF_SUPPLIER_STATUS
        and str(policy.get("wb_status") or "") == HANDOFF_WB_STATUS
        and str(policy.get("approval_reference") or "")
        and policy.get("supplier_status_complete_alone_forbidden") is True
    ):
        raise FfPoolFbsLifecycleError(
            "handoff_policy_not_approved",
            "FBS lifecycle is hard-off until the exact complete/sorted owner gate is applied",
        )


def _is_handoff(supplier_status: str, wb_status: str) -> bool:
    return supplier_status == HANDOFF_SUPPLIER_STATUS and wb_status == HANDOFF_WB_STATUS


def _is_cancellation(supplier_status: str, wb_status: str) -> bool:
    return (
        supplier_status in CANCELLATION_SUPPLIER_STATUSES
        or wb_status in CANCELLATION_WB_STATUSES
    )


def _exact_int(value: Any, field: str) -> int:
    decimal = Decimal(str(value))
    if not decimal.is_finite() or decimal != decimal.to_integral_value():
        raise FfPoolFbsLifecycleError("non_integral_quantity", f"{field} is not exact INTEGER")
    return int(decimal)


def _require_utc(value: str) -> None:
    parsed = _parse_utc(value)
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise FfPoolFbsLifecycleError("timestamp_not_utc", "timestamp must be UTC")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise FfPoolFbsLifecycleError("timezone_required", "timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decimal_check(column: str) -> str:
    return (
        f"typeof({column})='text' AND length({column}) BETWEEN 1 AND 80 "
        f"AND {column} NOT GLOB '*[^0-9.-]*' AND instr(substr({column},2),'-')=0 "
        f"AND length({column})-length(replace({column},'.',''))<=1 "
        f"AND {column} NOT IN ('','-','.','-.') AND substr({column},-1,1)<>'.'"
    )


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join("'" + value.replace("'", "''") + "'" for value in values)
