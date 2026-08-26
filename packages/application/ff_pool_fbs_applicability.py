"""Applicability-gated typed FBS physical truth.

Active, stock-managed nomenclature is applicable to every active FF facility by
default.  The only override is dated, append-only applicability evidence.  A
missing physical row is deliberately represented as ``missing`` and is never
coerced to zero by this module.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from packages.application.ff_pool_foundation import BALANCES_TABLE, FACILITIES_TABLE


CONTRACT_NAME = "ff_pool_fbs_physical_component_v1"
APPLICABILITY_EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_applicability_events"
DENSE_INTENTS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_dense_intents"
DENSE_INTENT_EVENTS_TABLE = "sheet_vitrina_v1_ff_pool_fbs_dense_intent_events"
NOMENCLATURE_TABLE = "sheet_vitrina_v1_nomenclature_items"
FBS_CURRENT_TABLE = "sheet_vitrina_v1_ff_pool_fbs_order_current"
DOCUMENTS_TABLE = "sheet_vitrina_v1_ff_pool_documents"
DOCUMENT_LINES_TABLE = "sheet_vitrina_v1_ff_pool_document_lines"
REQUESTS_TABLE = "sheet_vitrina_v1_ff_pool_document_requests"

COMPONENT_STATES = frozenset({"exact", "exact_zero", "missing", "inapplicable"})
APPLICABILITY_STATES = frozenset({"applicable", "inapplicable"})
INTENT_STATES = frozenset(
    {"staged", "materializing", "materialized", "active", "blocked"}
)
BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")


class FbsApplicabilityError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def ensure_ff_pool_fbs_applicability_schema(conn: sqlite3.Connection) -> None:
    """Create bounded non-physical applicability and staged-intent state."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {APPLICABILITY_EVENTS_TABLE}(
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            state TEXT NOT NULL CHECK(state IN ('applicable','inapplicable')),
            effective_from TEXT NOT NULL
                CHECK(length(effective_from)=10 AND date(effective_from)=effective_from),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 500),
            provenance_json TEXT NOT NULL CHECK(json_valid(provenance_json)),
            actor TEXT NOT NULL CHECK(length(trim(actor)) BETWEEN 1 AND 160),
            recorded_at TEXT NOT NULL
                CHECK(substr(recorded_at,-1,1)='Z' AND julianday(recorded_at) IS NOT NULL),
            UNIQUE(facility_id,nm_id,effective_from,state,event_id)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_applicability_current
        ON {APPLICABILITY_EVENTS_TABLE}(
            facility_id,nm_id,effective_from DESC,event_sequence DESC
        );
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_applicability_no_update
        BEFORE UPDATE ON {APPLICABILITY_EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS applicability evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_applicability_no_delete
        BEFORE DELETE ON {APPLICABILITY_EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS applicability evidence is append-only'); END;

        CREATE TABLE IF NOT EXISTS {DENSE_INTENTS_TABLE}(
            intent_id TEXT PRIMARY KEY,
            orchestration_key TEXT NOT NULL UNIQUE,
            request_identity TEXT NOT NULL,
            subject_kind TEXT NOT NULL
                CHECK(subject_kind IN ('facility_activation','sku_activation','repair')),
            subject_id TEXT NOT NULL,
            effective_from TEXT NOT NULL
                CHECK(length(effective_from)=10 AND date(effective_from)=effective_from),
            cutover_at TEXT NOT NULL
                CHECK(substr(cutover_at,-1,1)='Z' AND julianday(cutover_at) IS NOT NULL),
            roster_fingerprint TEXT NOT NULL,
            plan_fingerprint TEXT NOT NULL UNIQUE,
            plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
            actor TEXT NOT NULL CHECK(length(trim(actor)) BETWEEN 1 AND 160),
            created_at TEXT NOT NULL
                CHECK(substr(created_at,-1,1)='Z' AND julianday(created_at) IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_dense_intents_by_subject
        ON {DENSE_INTENTS_TABLE}(subject_kind,subject_id,created_at,intent_id);
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_dense_intent_no_update
        BEFORE UPDATE ON {DENSE_INTENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'Dense FBS intents are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_dense_intent_no_delete
        BEFORE DELETE ON {DENSE_INTENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'Dense FBS intents are append-only'); END;

        CREATE TABLE IF NOT EXISTS {DENSE_INTENT_EVENTS_TABLE}(
            event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            intent_id TEXT NOT NULL REFERENCES {DENSE_INTENTS_TABLE}(intent_id),
            state TEXT NOT NULL
                CHECK(state IN ('staged','materializing','materialized','active','blocked')),
            receipt_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(receipt_json)),
            receipt_fingerprint TEXT NOT NULL,
            recorded_at TEXT NOT NULL
                CHECK(substr(recorded_at,-1,1)='Z' AND julianday(recorded_at) IS NOT NULL),
            UNIQUE(intent_id,state,receipt_fingerprint)
        );
        CREATE INDEX IF NOT EXISTS ff_pool_fbs_dense_intent_events_current
        ON {DENSE_INTENT_EVENTS_TABLE}(intent_id,event_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_dense_intent_event_no_update
        BEFORE UPDATE ON {DENSE_INTENT_EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'Dense FBS intent events are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS ff_pool_fbs_dense_intent_event_no_delete
        BEFORE DELETE ON {DENSE_INTENT_EVENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'Dense FBS intent events are append-only'); END;
        """
    )


def current_business_date(value: str | None = None) -> str:
    if value:
        token = str(value).strip()
        if len(token) == 10:
            date.fromisoformat(token)
            return token
        parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Timestamp must include an explicit timezone")
        return parsed.astimezone(BUSINESS_TIMEZONE).date().isoformat()
    return datetime.now(timezone.utc).astimezone(BUSINESS_TIMEZONE).date().isoformat()


def stock_managed_nomenclature(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the exact active/non-hidden nmId roster or fail on ambiguity."""

    if NOMENCLATURE_TABLE not in _tables(conn):
        return []
    rows = conn.execute(
        f"""SELECT item_id,nm_id,updated_at
            FROM {NOMENCLATURE_TABLE}
            WHERE is_active=1 AND is_hidden=0 AND nm_id IS NOT NULL AND nm_id>0
            ORDER BY nm_id,item_id"""
    ).fetchall()
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        nm_id = int(row[1])
        if nm_id in seen:
            raise FbsApplicabilityError(
                "active_nomenclature_ambiguous",
                f"Active stock-managed nomenclature is ambiguous for nmId {nm_id}",
                details={"nm_id": nm_id},
            )
        seen.add(nm_id)
        result.append(
            {
                "item_id": str(row[0]),
                "nm_id": nm_id,
                "updated_at": str(row[2] or ""),
            }
        )
    return result


def fbs_pair_applicability(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    as_of_date: str,
    facility_active: bool = True,
    sku_active: bool = True,
) -> dict[str, Any]:
    """Resolve default applicability plus the latest dated explicit event."""

    target_date = current_business_date(as_of_date)
    if not facility_active:
        return {
            "applicable": False,
            "reason": "facility_inactive",
            "event": None,
            "effective_from": "",
            "provenance": {"source": FACILITIES_TABLE},
        }
    if not sku_active:
        return {
            "applicable": False,
            "reason": "sku_inactive",
            "event": None,
            "effective_from": "",
            "provenance": {"source": NOMENCLATURE_TABLE},
        }
    event = None
    if APPLICABILITY_EVENTS_TABLE in _tables(conn):
        event = conn.execute(
            f"""SELECT event_id,state,effective_from,reason,provenance_json,actor,recorded_at
                FROM {APPLICABILITY_EVENTS_TABLE}
                WHERE facility_id=? AND nm_id=? AND effective_from<=?
                ORDER BY effective_from DESC,event_sequence DESC LIMIT 1""",
            (str(facility_id), int(nm_id), target_date),
        ).fetchone()
    if event is not None:
        state = str(event[1])
        return {
            "applicable": state == "applicable",
            "reason": str(event[3]),
            "event": str(event[0]),
            "effective_from": str(event[2]),
            "provenance": {
                "source": APPLICABILITY_EVENTS_TABLE,
                "event_id": str(event[0]),
                "state": state,
                "actor": str(event[5]),
                "recorded_at": str(event[6]),
                **_json_object(event[4]),
            },
        }
    return {
        "applicable": True,
        "reason": "default_active_facility_x_stock_managed_sku",
        "event": None,
        "effective_from": target_date,
        "provenance": {"source": "dense_fbs_default_applicability_v1"},
    }


def fbs_physical_component(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    as_of_date: str,
    projection_epoch: int | None = None,
    facility_active: bool = True,
    sku_active: bool = True,
) -> dict[str, Any]:
    """Publish exact/exact_zero/missing/inapplicable with reason/provenance."""

    applicability = fbs_pair_applicability(
        conn,
        facility_id=facility_id,
        nm_id=nm_id,
        as_of_date=as_of_date,
        facility_active=facility_active,
        sku_active=sku_active,
    )
    if not applicability["applicable"]:
        return {
            "contract_name": CONTRACT_NAME,
            "state": "inapplicable",
            "facility_id": str(facility_id),
            "pool": "FBS",
            "nm_id": int(nm_id),
            "quantity": None,
            "capital_rub": None,
            "wac_rub": None,
            "reason": str(applicability["reason"]),
            "provenance": dict(applicability["provenance"]),
        }
    parameters: list[Any] = [str(facility_id), int(nm_id)]
    epoch_clause = ""
    if projection_epoch is not None:
        epoch_clause = " AND projection_epoch=?"
        parameters.append(int(projection_epoch))
    row = conn.execute(
        f"""SELECT projection_epoch,quantity,capital_rub,wac_rub,source_watermark,updated_at
            FROM {BALANCES_TABLE}
            WHERE facility_id=? AND pool='FBS' AND nm_id=?{epoch_clause}""",
        parameters,
    ).fetchone()
    if row is None:
        return {
            "contract_name": CONTRACT_NAME,
            "state": "missing",
            "facility_id": str(facility_id),
            "pool": "FBS",
            "nm_id": int(nm_id),
            "quantity": None,
            "capital_rub": None,
            "wac_rub": None,
            "reason": "applicable_physical_row_missing",
            "provenance": {
                "applicability": dict(applicability),
                "source": BALANCES_TABLE,
                "row_present": False,
            },
        }
    quantity = int(row[1])
    capital = _decimal(row[2])
    if quantity == 0 and (capital != Decimal("0") or row[3] is not None):
        return {
            "contract_name": CONTRACT_NAME,
            "state": "missing",
            "facility_id": str(facility_id),
            "pool": "FBS",
            "nm_id": int(nm_id),
            "quantity": None,
            "capital_rub": None,
            "wac_rub": None,
            "reason": "explicit_zero_shape_invalid",
            "provenance": {
                "applicability": dict(applicability),
                "source": BALANCES_TABLE,
                "row_present": True,
                "projection_epoch": int(row[0]),
                "source_watermark": str(row[4]),
                "updated_at": str(row[5]),
            },
        }
    return {
        "contract_name": CONTRACT_NAME,
        "state": "exact_zero" if quantity == 0 else "exact",
        "facility_id": str(facility_id),
        "pool": "FBS",
        "nm_id": int(nm_id),
        "quantity": quantity,
        "capital_rub": str(capital),
        "wac_rub": row[3],
        "reason": "explicit_physical_row",
        "provenance": {
            "applicability": dict(applicability),
            "source": BALANCES_TABLE,
            "row_present": True,
            "projection_epoch": int(row[0]),
            "source_watermark": str(row[4]),
            "updated_at": str(row[5]),
        },
    }


def require_fbs_pair_writeable(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    effective_date: str,
    projection_epoch: int | None = None,
) -> dict[str, Any]:
    """Fail closed before any receipt/writeoff/reservation/order effect."""

    facility = conn.execute(
        f"SELECT active FROM {FACILITIES_TABLE} WHERE facility_id=?",
        (str(facility_id),),
    ).fetchone()
    component = fbs_physical_component(
        conn,
        facility_id=facility_id,
        nm_id=nm_id,
        as_of_date=effective_date,
        projection_epoch=projection_epoch,
        facility_active=bool(facility[0]) if facility is not None else False,
        sku_active=True,
    )
    if component["state"] == "missing":
        raise FbsApplicabilityError(
            "applicable_fbs_balance_missing",
            f"Applicable FBS physical row is missing for {facility_id}/{nm_id}",
            details=component,
        )
    if component["state"] == "inapplicable":
        raise FbsApplicabilityError(
            "fbs_pair_inapplicable",
            f"FBS pair is explicitly inapplicable for {facility_id}/{nm_id}",
            details=component,
        )
    dense = _dense_balance_cutover(
        conn,
        facility_id=str(facility_id),
        nm_id=int(nm_id),
    )
    if dense and current_business_date(effective_date) < str(dense["effective_from"]):
        raise FbsApplicabilityError(
            "backdated_fbs_event_requires_reconciliation",
            "Backdated FBS events cannot consume a future explicit-zero cutover",
            details={
                "facility_id": str(facility_id),
                "nm_id": int(nm_id),
                "event_date": current_business_date(effective_date),
                "explicit_zero_effective_from": str(dense["effective_from"]),
                "intent_id": str(dense["intent_id"]),
            },
        )
    return component


def append_applicability_event(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    nm_id: int,
    state: str,
    effective_from: str,
    reason: str,
    provenance: Mapping[str, Any],
    actor: str,
    recorded_at: str,
) -> dict[str, Any]:
    """Append one exact dated exception/reinstatement without rewriting history."""

    if APPLICABILITY_EVENTS_TABLE not in _tables(conn):
        ensure_ff_pool_fbs_applicability_schema(conn)
    selected_state = str(state)
    if selected_state not in APPLICABILITY_STATES:
        raise FbsApplicabilityError("invalid_applicability_state", "Invalid FBS applicability state")
    target_date = current_business_date(effective_from)
    today = current_business_date(recorded_at)
    if target_date < today:
        raise FbsApplicabilityError(
            "backdated_applicability_requires_reconciliation",
            "Backdated applicability requires an explicit reconciliation flow",
        )
    exact_reason = str(reason or "").strip()
    exact_actor = str(actor or "").strip()
    if not exact_reason or not exact_actor:
        raise FbsApplicabilityError(
            "applicability_reason_and_actor_required",
            "Applicability evidence requires a reason and actor",
        )
    facility = conn.execute(
        f"SELECT facility_id FROM {FACILITIES_TABLE} WHERE facility_id=?",
        (str(facility_id),),
    ).fetchone()
    if facility is None:
        raise FbsApplicabilityError("facility_not_found", "FBS facility was not found")
    if selected_state == "inapplicable":
        balance = conn.execute(
            f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool='FBS' AND nm_id=?",
            (str(facility_id), int(nm_id)),
        ).fetchone()
        if balance is not None and (
            int(balance[0]) != 0
            or _decimal(balance[1]) != Decimal("0")
            or balance[2] is not None
        ):
            raise FbsApplicabilityError(
                "inapplicable_nonzero_physical_blocked",
                "Only a canonical explicit-zero FBS row can become inapplicable",
            )
        if FBS_CURRENT_TABLE in _tables(conn):
            reserved = int(
                conn.execute(
                    f"SELECT COALESCE(SUM(quantity),0) FROM {FBS_CURRENT_TABLE} "
                    "WHERE facility_id=? AND pool='FBS' AND nm_id=? AND state='reserved'",
                    (str(facility_id), int(nm_id)),
                ).fetchone()[0]
            )
            if reserved:
                raise FbsApplicabilityError(
                    "inapplicable_active_reservation_blocked",
                    "An FBS pair with active reservations cannot become inapplicable",
                )
    else:
        component = fbs_physical_component(
            conn,
            facility_id=facility_id,
            nm_id=nm_id,
            as_of_date=target_date,
            facility_active=True,
            sku_active=True,
        )
        # A prior inapplicable event may hide an already retained row.  Direct
        # row existence is the required coverage proof before reinstatement.
        row = conn.execute(
            f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool='FBS' AND nm_id=?",
            (str(facility_id), int(nm_id)),
        ).fetchone()
        if row is None or (
            int(row[0]) == 0
            and (_decimal(row[1]) != Decimal("0") or row[2] is not None)
        ):
            raise FbsApplicabilityError(
                "applicable_coverage_missing",
                "Reinstating applicability requires an existing exact physical row",
                details=component,
            )
    material = {
        "facility_id": str(facility_id),
        "nm_id": int(nm_id),
        "state": selected_state,
        "effective_from": target_date,
        "reason": exact_reason,
        "provenance": dict(provenance),
        "actor": exact_actor,
        "recorded_at": str(recorded_at),
    }
    event_id = "ffap_" + _fingerprint(
        {key: value for key, value in material.items() if key != "recorded_at"}
    ).removeprefix("sha256:")[:28]
    conn.execute(
        f"""INSERT OR IGNORE INTO {APPLICABILITY_EVENTS_TABLE}(
               event_id,facility_id,nm_id,state,effective_from,reason,
               provenance_json,actor,recorded_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            str(facility_id),
            int(nm_id),
            selected_state,
            target_date,
            exact_reason,
            _json(dict(provenance)),
            exact_actor,
            str(recorded_at),
        ),
    )
    persisted = conn.execute(
        f"""SELECT state,effective_from,reason,provenance_json,actor,recorded_at
            FROM {APPLICABILITY_EVENTS_TABLE} WHERE event_id=?""",
        (event_id,),
    ).fetchone()
    if persisted is None:
        raise FbsApplicabilityError(
            "applicability_event_persist_failed",
            "FBS applicability event was not persisted",
        )
    return {
        "event_id": event_id,
        "facility_id": str(facility_id),
        "nm_id": int(nm_id),
        "state": str(persisted[0]),
        "effective_from": str(persisted[1]),
        "reason": str(persisted[2]),
        "provenance": _json_object(persisted[3]),
        "actor": str(persisted[4]),
        "recorded_at": str(persisted[5]),
    }


def persist_dense_intent(
    conn: sqlite3.Connection,
    *,
    orchestration_key: str,
    request_identity: str,
    subject_kind: str,
    subject_id: str,
    effective_from: str,
    cutover_at: str,
    roster_fingerprint: str,
    plan: Mapping[str, Any],
    actor: str,
) -> dict[str, Any]:
    if DENSE_INTENTS_TABLE not in _tables(conn):
        ensure_ff_pool_fbs_applicability_schema(conn)
    existing = conn.execute(
        f"SELECT * FROM {DENSE_INTENTS_TABLE} WHERE orchestration_key=?",
        (str(orchestration_key),),
    ).fetchone()
    if existing is not None:
        if str(existing[2]) != str(request_identity):
            raise FbsApplicabilityError(
                "dense_intent_identity_conflict",
                "Dense FBS orchestration identity was reused with different evidence",
            )
        return _intent_row(existing)
    plan_payload = dict(plan)
    plan_fingerprint = _fingerprint(plan_payload)
    intent_id = "ffdi_" + _fingerprint(
        {"orchestration_key": orchestration_key, "request_identity": request_identity}
    ).removeprefix("sha256:")[:28]
    conn.execute(
        f"""INSERT INTO {DENSE_INTENTS_TABLE}(
               intent_id,orchestration_key,request_identity,subject_kind,subject_id,
               effective_from,cutover_at,roster_fingerprint,plan_fingerprint,
               plan_json,actor,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            intent_id,
            str(orchestration_key),
            str(request_identity),
            str(subject_kind),
            str(subject_id),
            current_business_date(effective_from),
            str(cutover_at),
            str(roster_fingerprint),
            plan_fingerprint,
            _json(plan_payload),
            str(actor),
            str(cutover_at),
        ),
    )
    append_dense_intent_event(
        conn,
        intent_id=intent_id,
        state="staged",
        receipt={"plan_fingerprint": plan_fingerprint},
        recorded_at=cutover_at,
    )
    row = conn.execute(
        f"SELECT * FROM {DENSE_INTENTS_TABLE} WHERE intent_id=?", (intent_id,)
    ).fetchone()
    if row is None:
        raise FbsApplicabilityError("dense_intent_persist_failed", "Dense FBS intent was not persisted")
    return _intent_row(row)


def append_dense_intent_event(
    conn: sqlite3.Connection,
    *,
    intent_id: str,
    state: str,
    receipt: Mapping[str, Any],
    recorded_at: str,
) -> dict[str, Any]:
    selected = str(state)
    if selected not in INTENT_STATES:
        raise FbsApplicabilityError("invalid_dense_intent_state", "Invalid dense FBS intent state")
    receipt_payload = dict(receipt)
    receipt_fingerprint = _fingerprint(receipt_payload)
    material = {
        "intent_id": str(intent_id),
        "state": selected,
        "receipt_fingerprint": receipt_fingerprint,
    }
    event_id = "ffde_" + _fingerprint(material).removeprefix("sha256:")[:28]
    conn.execute(
        f"""INSERT OR IGNORE INTO {DENSE_INTENT_EVENTS_TABLE}(
               event_id,intent_id,state,receipt_json,receipt_fingerprint,recorded_at
           ) VALUES(?,?,?,?,?,?)""",
        (
            event_id,
            str(intent_id),
            selected,
            _json(receipt_payload),
            receipt_fingerprint,
            str(recorded_at),
        ),
    )
    return {"event_id": event_id, **material, "receipt": receipt_payload}


def dense_intent_state(conn: sqlite3.Connection, intent_id: str) -> dict[str, Any]:
    row = conn.execute(
        f"""SELECT event_id,state,receipt_json,receipt_fingerprint,recorded_at
            FROM {DENSE_INTENT_EVENTS_TABLE} WHERE intent_id=?
            ORDER BY event_sequence DESC LIMIT 1""",
        (str(intent_id),),
    ).fetchone()
    return (
        {
            "event_id": str(row[0]),
            "state": str(row[1]),
            "receipt": _json_object(row[2]),
            "receipt_fingerprint": str(row[3]),
            "recorded_at": str(row[4]),
        }
        if row is not None
        else {"state": "missing"}
    )


def coverage_receipt(
    conn: sqlite3.Connection,
    *,
    pairs: Sequence[tuple[str, int]],
    as_of_date: str,
    projection_epoch: int,
    assumed_active_facility_ids: Sequence[str] = (),
    assumed_active_nm_ids: Sequence[int] = (),
) -> dict[str, Any]:
    assumed_facilities = {str(value) for value in assumed_active_facility_ids}
    # Staged SKU activations are intentionally treated as active for this
    # coverage readback.  Existing pairs in an activation plan are likewise
    # from the pinned stock-managed roster.  The explicit argument remains in
    # the receipt contract so callers must disclose that staged boundary.
    facility_active = {
        str(row[0]): bool(row[1])
        for row in conn.execute(
            f"SELECT facility_id,active FROM {FACILITIES_TABLE} ORDER BY facility_id"
        ).fetchall()
    }
    rows = [
        fbs_physical_component(
            conn,
            facility_id=facility_id,
            nm_id=nm_id,
            as_of_date=as_of_date,
            projection_epoch=projection_epoch,
            facility_active=facility_active.get(facility_id, False)
            or facility_id in assumed_facilities,
            sku_active=True,
        )
        for facility_id, nm_id in sorted(set((str(f), int(n)) for f, n in pairs))
    ]
    incomplete = [
        {"facility_id": row["facility_id"], "nm_id": row["nm_id"], "state": row["state"]}
        for row in rows
        if row["state"] == "missing"
    ]
    receipt = {
        "contract_name": "ff_pool_fbs_dense_coverage_receipt_v1",
        "as_of_date": current_business_date(as_of_date),
        "projection_epoch": int(projection_epoch),
        "pair_count": len(rows),
        "exact_count": sum(row["state"] == "exact" for row in rows),
        "exact_zero_count": sum(row["state"] == "exact_zero" for row in rows),
        "inapplicable_count": sum(row["state"] == "inapplicable" for row in rows),
        "missing_count": len(incomplete),
        "rows": rows,
        "complete": not incomplete,
        "incomplete": incomplete,
    }
    receipt["fingerprint"] = _fingerprint(receipt)
    return receipt


def _dense_balance_cutover(
    conn: sqlite3.Connection, *, facility_id: str, nm_id: int
) -> dict[str, Any] | None:
    if {DOCUMENTS_TABLE, DOCUMENT_LINES_TABLE, REQUESTS_TABLE} - _tables(conn):
        return None
    # Later receipts advance the balance watermark.  The immutable inventory
    # line plus its request manifest is therefore the durable cutover receipt;
    # no second physical or coverage ledger is needed.
    rows = conn.execute(
        f"""SELECT request.preview_manifest_json,line.metadata_json,
                   document.document_id,document.posted_at
            FROM {DOCUMENT_LINES_TABLE} line
            JOIN {DOCUMENTS_TABLE} document
              ON document.document_id=line.document_id
            JOIN {REQUESTS_TABLE} request
              ON request.request_id=document.request_id
            WHERE line.facility_id=? AND line.pool='FBS' AND line.nm_id=?
              AND line.line_role='absolute_target'
              AND document.document_kind='pool_inventory'
            ORDER BY document.posted_at ASC,document.document_id ASC""",
        (str(facility_id), int(nm_id)),
    ).fetchall()
    for row in rows:
        if _json_object(row[1]).get("explicit_physical_zero") is not True:
            continue
        manifest = _json_object(row[0])
        dense = _json_object(manifest.get("dense_fbs_initialization") or {})
        if dense.get("contract_name") != "ff_pool_dense_fbs_initialization_v1":
            continue
        return {
            **dense,
            "document_id": str(row[2]),
            "posted_at": str(row[3]),
        }
    return None


def _intent_row(row: Sequence[Any]) -> dict[str, Any]:
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
        "plan": _json_object(row[9]),
        "actor": str(row[10]),
        "created_at": str(row[11]),
    }


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FbsApplicabilityError("invalid_fbs_capital", "FBS capital is not exact Decimal") from exc
    if not parsed.is_finite():
        raise FbsApplicabilityError("invalid_fbs_capital", "FBS capital must be finite")
    return parsed


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
