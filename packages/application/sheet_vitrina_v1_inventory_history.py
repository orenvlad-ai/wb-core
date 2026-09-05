"""Compact append-only inventory history for the main Web Vitrina.

The history stores typed WB/FBS components, never rendered table snapshots.
Current refreshes and the historical backfill use the same append/finalize
primitives.  A closed business day resolves to its latest immutable
finalization; a later accepted revision appends a superseding finalization.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping, Sequence

from packages.application.inventory_planning_read_model import (
    FORMULA_VERSION,
    _fbs_facilities,
)
from packages.application.ff_pool_fbs_lifecycle import (
    fbs_lifecycle_group_blocked,
    fbs_lifecycle_quality_coverage,
)
from packages.application.wb_incident_policy import canonical_seller_id
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope


CONTRACT_NAME = "sheet_vitrina_v1_inventory_history"
CONTRACT_VERSION = 1
CAPTURES_TABLE = "sheet_vitrina_v1_inventory_history_captures"
COMPONENTS_TABLE = "sheet_vitrina_v1_inventory_history_components"
FINALIZATIONS_TABLE = "sheet_vitrina_v1_inventory_history_finalizations"
APPLIES_TABLE = "sheet_vitrina_v1_inventory_history_applies"

COMPONENT_STATES = frozenset({"exact", "exact_zero", "missing", "inapplicable"})
SCOPE_KINDS = frozenset({"TOTAL", "SKU"})
COMPONENT_KINDS = frozenset({"WB", "FBS_FACILITY"})


def ensure_inventory_history_schema(conn: sqlite3.Connection) -> None:
    """Create the canonical compact history with indefinite retention."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {CAPTURES_TABLE}(
            capture_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL UNIQUE,
            business_date TEXT NOT NULL
                CHECK(length(business_date)=10 AND date(business_date)=business_date),
            capture_kind TEXT NOT NULL
                CHECK(capture_kind IN ('accepted_refresh','historical_backfill')),
            formula_version TEXT NOT NULL,
            bundle_version TEXT NOT NULL DEFAULT '',
            ready_snapshot_id TEXT NOT NULL DEFAULT '',
            ready_plan_version TEXT NOT NULL DEFAULT '',
            generation_identity TEXT NOT NULL DEFAULT '',
            facility_roster_revision TEXT NOT NULL,
            facility_roster_json TEXT NOT NULL CHECK(json_valid(facility_roster_json)),
            source_manifest_json TEXT NOT NULL CHECK(json_valid(source_manifest_json)),
            source_digest TEXT NOT NULL,
            captured_at TEXT NOT NULL
                CHECK(substr(captured_at,-1,1)='Z' AND julianday(captured_at) IS NOT NULL),
            UNIQUE(business_date,source_digest)
        );
        CREATE INDEX IF NOT EXISTS inventory_history_capture_by_date
        ON {CAPTURES_TABLE}(business_date,capture_sequence DESC);

        CREATE TABLE IF NOT EXISTS {COMPONENTS_TABLE}(
            capture_id TEXT NOT NULL REFERENCES {CAPTURES_TABLE}(capture_id),
            scope_kind TEXT NOT NULL CHECK(scope_kind IN ('TOTAL','SKU')),
            scope_key TEXT NOT NULL,
            nm_id INTEGER,
            component_kind TEXT NOT NULL CHECK(component_kind IN ('WB','FBS_FACILITY')),
            component_id TEXT NOT NULL,
            component_label TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK(state IN ('exact','exact_zero','missing','inapplicable')),
            quantity INTEGER,
            source_revision TEXT NOT NULL DEFAULT '',
            source_digest TEXT NOT NULL DEFAULT '',
            source_watermark TEXT NOT NULL DEFAULT '',
            provenance_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(provenance_json)),
            captured_at TEXT NOT NULL,
            CHECK((state='exact' AND quantity IS NOT NULL AND quantity<>0)
               OR (state='exact_zero' AND quantity=0)
               OR (state IN ('missing','inapplicable') AND quantity IS NULL)),
            CHECK((scope_kind='TOTAL' AND scope_key='TOTAL' AND nm_id IS NULL)
               OR (scope_kind='SKU' AND scope_key='SKU:' || nm_id AND nm_id>0)),
            CHECK((component_kind='WB' AND component_id='WB')
               OR (component_kind='FBS_FACILITY' AND component_id<>'WB')),
            PRIMARY KEY(capture_id,scope_kind,scope_key,component_kind,component_id)
        );
        CREATE INDEX IF NOT EXISTS inventory_history_component_by_scope
        ON {COMPONENTS_TABLE}(scope_kind,scope_key,component_kind,component_id,capture_id);

        CREATE TABLE IF NOT EXISTS {FINALIZATIONS_TABLE}(
            finalization_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            finalization_id TEXT NOT NULL UNIQUE,
            business_date TEXT NOT NULL,
            capture_id TEXT NOT NULL REFERENCES {CAPTURES_TABLE}(capture_id),
            finalization_identity TEXT NOT NULL,
            finalization_digest TEXT NOT NULL UNIQUE,
            supersedes_finalization_digest TEXT NOT NULL DEFAULT '',
            finalized_at TEXT NOT NULL
                CHECK(substr(finalized_at,-1,1)='Z' AND julianday(finalized_at) IS NOT NULL),
            provenance_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(provenance_json)),
            UNIQUE(business_date,capture_id,finalization_identity)
        );
        CREATE INDEX IF NOT EXISTS inventory_history_finalization_by_date
        ON {FINALIZATIONS_TABLE}(business_date,finalization_sequence DESC);

        CREATE TABLE IF NOT EXISTS {APPLIES_TABLE}(
            manifest_hash TEXT PRIMARY KEY,
            deployed_sha TEXT NOT NULL CHECK(length(deployed_sha)=40),
            schema_generation TEXT NOT NULL,
            source_watermarks_digest TEXT NOT NULL,
            expected_capture_count INTEGER NOT NULL,
            expected_component_count INTEGER NOT NULL,
            applied_capture_count INTEGER NOT NULL,
            applied_component_count INTEGER NOT NULL,
            before_digest TEXT NOT NULL,
            after_digest TEXT NOT NULL,
            approval_reference TEXT NOT NULL,
            recovery_evidence_path TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            reconciliation_json TEXT NOT NULL CHECK(json_valid(reconciliation_json))
        );

        CREATE TRIGGER IF NOT EXISTS inventory_history_capture_no_update
        BEFORE UPDATE ON {CAPTURES_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history captures are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_capture_no_delete
        BEFORE DELETE ON {CAPTURES_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history captures have indefinite retention'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_component_no_update
        BEFORE UPDATE ON {COMPONENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history components are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_component_no_delete
        BEFORE DELETE ON {COMPONENTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history components have indefinite retention'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_finalization_no_update
        BEFORE UPDATE ON {FINALIZATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history finalizations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_finalization_no_delete
        BEFORE DELETE ON {FINALIZATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history finalizations have indefinite retention'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_apply_no_update
        BEFORE UPDATE ON {APPLIES_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history apply evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS inventory_history_apply_no_delete
        BEFORE DELETE ON {APPLIES_TABLE}
        BEGIN SELECT RAISE(ABORT,'inventory history apply evidence is append-only'); END;
        """
    )


def capture_inventory_history_from_ready_plan(
    conn: sqlite3.Connection,
    *,
    plan: SheetVitrinaV1Envelope,
    bundle_version: str,
    refreshed_at: str,
    generation_identity: str = "",
) -> dict[str, Any]:
    """Append the current accepted component revision and finalize the closed day."""

    _ensure_inventory_history_schema_ready(conn)
    current_date = _slot_date(plan, "today_current")
    closed_date = _slot_date(plan, "yesterday_closed")
    result: dict[str, Any] = {
        "status": "skipped",
        "capture_id": "",
        "capture_inserted": False,
        "closed_capture_id": "",
        "closed_capture_inserted": False,
        "finalization_inserted": False,
    }
    if current_date:
        wb_evidence = _canonical_current_wb_evidence(
            conn,
            plan=plan,
            business_date=current_date,
        )
        wb_by_scope = dict(wb_evidence.get("values") or {})
        if wb_by_scope:
            fbs = _fbs_facilities(
                conn,
                seller_id=canonical_seller_id(),
                requested_nm_ids=sorted(
                    int(scope_key.split(":", 1)[1])
                    for scope_key in wb_by_scope
                    if scope_key.startswith("SKU:")
                ),
                include_seller_stock_reconciliation=False,
            )
            roster = _facility_roster(fbs)
            components = _current_components(
                wb_by_scope=wb_by_scope,
                wb_component_sources=dict(wb_evidence.get("component_sources") or {}),
                fbs=fbs,
            )
            source_manifest = {
                "contract": "accepted_ready_inventory_capture_v1",
                "ready_snapshot_id": plan.snapshot_id,
                "ready_plan_version": plan.plan_version,
                "bundle_version": bundle_version,
                "business_date": current_date,
                "wb": dict(wb_evidence.get("source_manifest") or {}),
                "fbs": _current_fbs_source(conn, fbs=fbs),
            }
            capture = append_inventory_history_capture(
                conn,
                business_date=current_date,
                capture_kind="accepted_refresh",
                formula_version=FORMULA_VERSION,
                bundle_version=bundle_version,
                ready_snapshot_id=plan.snapshot_id,
                ready_plan_version=plan.plan_version,
                generation_identity=generation_identity,
                facility_roster=roster,
                source_manifest=source_manifest,
                components=components,
                captured_at=refreshed_at,
            )
            result.update(
                status="captured",
                capture_id=capture["capture_id"],
                capture_inserted=bool(capture["inserted"]),
            )
    if closed_date:
        closed_capture = _append_closed_date_ready_capture(
            conn,
            plan=plan,
            bundle_version=bundle_version,
            business_date=closed_date,
            refreshed_at=refreshed_at,
            generation_identity=generation_identity,
        )
        if closed_capture:
            result["closed_capture_id"] = str(closed_capture["capture_id"])
            result["closed_capture_inserted"] = bool(closed_capture["inserted"])
        candidate = (
            str(closed_capture["capture_id"])
            if closed_capture
            else _latest_capture_id(conn, business_date=closed_date)
        )
        if candidate:
            finalization = append_inventory_history_finalization(
                conn,
                business_date=closed_date,
                capture_id=candidate,
                finalization_identity=(
                    f"ready:{bundle_version}:{plan.snapshot_id}:{closed_date}"
                ),
                finalized_at=refreshed_at,
                provenance={
                    "contract": "sheet_vitrina_general_day_closure_v1",
                    "ready_snapshot_id": plan.snapshot_id,
                    "ready_as_of_date": plan.as_of_date,
                    "temporal_slot": "yesterday_closed",
                },
            )
            result["finalization_inserted"] = bool(finalization["inserted"])
            result["finalization_id"] = finalization["finalization_id"]
    return result


def append_inventory_history_capture(
    conn: sqlite3.Connection,
    *,
    business_date: str,
    capture_kind: str,
    formula_version: str,
    facility_roster: Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    captured_at: str,
    bundle_version: str = "",
    ready_snapshot_id: str = "",
    ready_plan_version: str = "",
    generation_identity: str = "",
) -> dict[str, Any]:
    """Append one immutable component revision; identical evidence is idempotent."""

    _ensure_inventory_history_schema_ready(conn)
    preview = preview_inventory_history_capture(
        business_date=business_date,
        capture_kind=capture_kind,
        formula_version=formula_version,
        facility_roster=facility_roster,
        source_manifest=source_manifest,
        components=components,
        captured_at=captured_at,
    )
    normalized_roster = preview["facility_roster"]
    normalized_components = preview["components"]
    roster_revision = str(preview["facility_roster_revision"])
    source_payload = preview["source_manifest"]
    source_digest = str(preview["source_digest"])
    capture_id = str(preview["capture_id"])
    inserted = conn.execute(
        f"""INSERT OR IGNORE INTO {CAPTURES_TABLE}(
                capture_id,business_date,capture_kind,formula_version,bundle_version,
                ready_snapshot_id,ready_plan_version,generation_identity,
                facility_roster_revision,facility_roster_json,source_manifest_json,
                source_digest,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            capture_id,
            business_date,
            capture_kind,
            formula_version,
            bundle_version,
            ready_snapshot_id,
            ready_plan_version,
            generation_identity,
            roster_revision,
            _json(normalized_roster),
            _json(source_payload),
            source_digest,
            captured_at,
        ),
    ).rowcount
    if inserted:
        conn.executemany(
            f"""INSERT INTO {COMPONENTS_TABLE}(
                    capture_id,scope_kind,scope_key,nm_id,component_kind,
                    component_id,component_label,state,quantity,source_revision,
                    source_digest,source_watermark,provenance_json,captured_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    capture_id,
                    item["scope_kind"],
                    item["scope_key"],
                    item["nm_id"],
                    item["component_kind"],
                    item["component_id"],
                    item["component_label"],
                    item["state"],
                    item["quantity"],
                    item["source_revision"],
                    item["source_digest"],
                    item["source_watermark"],
                    _json(item["provenance"]),
                    captured_at,
                )
                for item in normalized_components
            ],
        )
    else:
        row = conn.execute(
            f"SELECT capture_id FROM {CAPTURES_TABLE} WHERE business_date=? AND source_digest=?",
            (business_date, source_digest),
        ).fetchone()
        if row is None:
            raise RuntimeError("inventory history idempotent capture readback failed")
        capture_id = str(row[0])
    return {
        "capture_id": capture_id,
        "source_digest": source_digest,
        "facility_roster_revision": roster_revision,
        "component_count": len(normalized_components),
        "inserted": bool(inserted),
    }


def preview_inventory_history_capture(
    *,
    business_date: str,
    capture_kind: str,
    formula_version: str,
    facility_roster: Sequence[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
    components: Sequence[Mapping[str, Any]],
    captured_at: str,
) -> dict[str, Any]:
    """Return the exact normalized capture identity without touching SQLite."""

    normalized_roster = _normalize_roster(facility_roster)
    normalized_components = _normalize_components(components, captured_at=captured_at)
    roster_revision = _fingerprint(normalized_roster)
    source_payload = _jsonable(dict(source_manifest))
    source_digest = _fingerprint(
        {
            "formula_version": formula_version,
            "facility_roster_revision": roster_revision,
            "source_manifest": source_payload,
            # Observation time is append metadata, not source evidence.  A
            # retry of the same accepted revision at a later writer timestamp
            # must resolve to the same immutable capture.
            "components": [
                {key: value for key, value in item.items() if key != "captured_at"}
                for item in normalized_components
            ],
        }
    )
    identity = {
        "business_date": business_date,
        "capture_kind": capture_kind,
        "source_digest": source_digest,
    }
    return {
        "capture_id": "ivhc_" + _fingerprint(identity).removeprefix("sha256:")[:28],
        "source_digest": source_digest,
        "facility_roster_revision": roster_revision,
        "facility_roster": normalized_roster,
        "source_manifest": source_payload,
        "components": normalized_components,
    }


def append_inventory_history_finalization(
    conn: sqlite3.Connection,
    *,
    business_date: str,
    capture_id: str,
    finalization_identity: str,
    finalized_at: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Append the accepted closed-day pointer, preserving prior revisions."""

    _ensure_inventory_history_schema_ready(conn)
    capture = conn.execute(
        f"SELECT business_date,source_digest FROM {CAPTURES_TABLE} WHERE capture_id=?",
        (capture_id,),
    ).fetchone()
    if capture is None or str(capture[0]) != business_date:
        raise ValueError("inventory history finalization capture/date mismatch")
    previous = conn.execute(
        f"""SELECT finalization_digest FROM {FINALIZATIONS_TABLE}
            WHERE business_date=? ORDER BY finalization_sequence DESC LIMIT 1""",
        (business_date,),
    ).fetchone()
    previous_digest = str(previous[0]) if previous is not None else ""
    payload = {
        "business_date": business_date,
        "capture_id": capture_id,
        "capture_source_digest": str(capture[1]),
        "finalization_identity": finalization_identity,
        "supersedes": previous_digest,
        "provenance": _jsonable(dict(provenance)),
    }
    finalization_digest = _fingerprint(payload)
    finalization_id = "ivhf_" + finalization_digest.removeprefix("sha256:")[:28]
    inserted = conn.execute(
        f"""INSERT OR IGNORE INTO {FINALIZATIONS_TABLE}(
                finalization_id,business_date,capture_id,finalization_identity,
                finalization_digest,supersedes_finalization_digest,finalized_at,
                provenance_json
            ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            finalization_id,
            business_date,
            capture_id,
            finalization_identity,
            finalization_digest,
            previous_digest,
            finalized_at,
            _json(provenance),
        ),
    ).rowcount
    if not inserted:
        row = conn.execute(
            f"""SELECT finalization_id,finalization_digest,
                       supersedes_finalization_digest
                FROM {FINALIZATIONS_TABLE}
                WHERE business_date=? AND capture_id=? AND finalization_identity=?""",
            (business_date, capture_id, finalization_identity),
        ).fetchone()
        if row is None:
            raise RuntimeError("inventory history idempotent finalization readback failed")
        finalization_id, finalization_digest = str(row[0]), str(row[1])
        previous_digest = str(row[2])
    return {
        "finalization_id": finalization_id,
        "finalization_digest": finalization_digest,
        "supersedes_finalization_digest": previous_digest,
        "inserted": bool(inserted),
    }


def read_inventory_history_window(
    db_path: Path,
    *,
    dates: Iterable[str],
    current_date: str,
    lifecycle_quality_resolver: Callable[
        [str, Iterable[int] | None], Mapping[str, Any]
    ]
    | None = None,
) -> dict[str, Any]:
    """Read already-folded component revisions for a bounded date window."""

    requested_dates = sorted({str(value) for value in dates if str(value)})
    if not requested_dates or not Path(db_path).is_file():
        return {"contract": CONTRACT_NAME, "dates": {}, "facilities": []}
    conn = sqlite3.connect(
        f"file:{Path(db_path).resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if {CAPTURES_TABLE, COMPONENTS_TABLE, FINALIZATIONS_TABLE} - tables:
            return {"contract": CONTRACT_NAME, "dates": {}, "facilities": []}
        capture_by_date: dict[str, sqlite3.Row] = {}
        for business_date in requested_dates:
            row = conn.execute(
                f"""SELECT capture.*,finalization.finalization_id,
                           finalization.finalization_digest,finalization.finalized_at
                    FROM {FINALIZATIONS_TABLE} finalization
                    JOIN {CAPTURES_TABLE} capture
                      ON capture.capture_id=finalization.capture_id
                    WHERE finalization.business_date=?
                    ORDER BY finalization.finalization_sequence DESC LIMIT 1""",
                (business_date,),
            ).fetchone()
            if row is None and business_date == current_date:
                row = conn.execute(
                    f"""SELECT capture.*,'' AS finalization_id,
                               '' AS finalization_digest,'' AS finalized_at
                        FROM {CAPTURES_TABLE} capture
                        WHERE capture.business_date=?
                        ORDER BY capture.capture_sequence DESC LIMIT 1""",
                    (business_date,),
                ).fetchone()
            if row is not None:
                capture_by_date[business_date] = row
        result_dates: dict[str, Any] = {}
        facility_catalog: dict[str, dict[str, Any]] = {}
        for business_date, capture in capture_by_date.items():
            roster = _loads(capture["facility_roster_json"], [])
            for item in roster if isinstance(roster, list) else []:
                if isinstance(item, Mapping) and str(item.get("facility_id") or ""):
                    facility_catalog[str(item["facility_id"])] = dict(item)
            rows = conn.execute(
                f"""SELECT * FROM {COMPONENTS_TABLE}
                    WHERE capture_id=?
                    ORDER BY scope_kind,scope_key,component_kind,component_id""",
                (str(capture["capture_id"]),),
            ).fetchall()
            requested_nm_ids = {
                int(row["nm_id"])
                for row in rows
                if row["nm_id"] is not None and int(row["nm_id"]) > 0
            }
            lifecycle_quality = (
                dict(lifecycle_quality_resolver(business_date, requested_nm_ids))
                if lifecycle_quality_resolver is not None
                else fbs_lifecycle_quality_coverage(
                    conn,
                    as_of_date=business_date,
                    requested_nm_ids=requested_nm_ids,
                )
            )
            scopes: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                component = dict(row)
                if (
                    str(component["component_kind"]) == "FBS_FACILITY"
                    and fbs_lifecycle_group_blocked(
                        lifecycle_quality,
                        facility_id=str(component["component_id"]),
                        nm_id=(
                            None
                            if component["nm_id"] is None
                            else int(component["nm_id"])
                        ),
                    )
                ):
                    component.update(
                        {
                            "state": "missing",
                            "quantity": None,
                            "source_revision": "lifecycle_identity_coverage_pending",
                            "source_digest": str(
                                lifecycle_quality.get("digest") or ""
                            ),
                            "source_watermark": "",
                        }
                    )
                scopes.setdefault(str(row["scope_key"]), []).append(component)
            result_dates[business_date] = {
                "capture_id": str(capture["capture_id"]),
                "source_digest": str(capture["source_digest"]),
                "formula_version": str(capture["formula_version"]),
                "captured_at": str(capture["captured_at"]),
                "finalization_id": str(capture["finalization_id"]),
                "finalization_digest": str(capture["finalization_digest"]),
                "finalized_at": str(capture["finalized_at"]),
                "scopes": {
                    scope_key: _materialize_scope(components)
                    for scope_key, components in scopes.items()
                },
            }
        return {
            "contract": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "dates": result_dates,
            "facilities": sorted(
                facility_catalog.values(),
                key=lambda item: (
                    int(item.get("display_order") or 0),
                    str(item.get("code") or ""),
                    str(item.get("facility_id") or ""),
                ),
            ),
        }
    finally:
        conn.close()


def _materialize_scope(components: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    wb = next(
        (dict(item) for item in components if str(item["component_kind"]) == "WB"),
        None,
    )
    facilities = {
        str(item["component_id"]): {
            "facility_id": str(item["component_id"]),
            "label": str(item["component_label"]),
            "state": str(item["state"]),
            "value": item["quantity"],
            "source_revision": str(item["source_revision"]),
            "source_digest": str(item["source_digest"]),
            "source_watermark": str(item["source_watermark"]),
        }
        for item in components
        if str(item["component_kind"]) == "FBS_FACILITY"
    }
    applicable = [item for item in facilities.values() if item["state"] != "inapplicable"]
    fbs_missing = [item for item in applicable if item["state"] == "missing"]
    fbs_known = [
        int(item["value"])
        for item in applicable
        if item["state"] in {"exact", "exact_zero"}
    ]
    missing_labels = [str(item["label"]) for item in fbs_missing]
    wb_state = str(wb["state"]) if wb is not None else "missing"
    wb_value = wb["quantity"] if wb is not None else None
    if wb_state == "missing":
        missing_labels.insert(0, "WB")
    known_values = [*fbs_known]
    if wb_state in {"exact", "exact_zero"}:
        known_values.insert(0, int(wb_value))
    quality = "partial" if missing_labels else "full"
    return {
        "wb": {
            "state": wb_state,
            "value": wb_value,
            "source_revision": str(wb["source_revision"]) if wb else "",
            "source_digest": str(wb["source_digest"]) if wb else "",
            "source_watermark": str(wb["source_watermark"]) if wb else "",
        },
        "facilities": facilities,
        "fbs_total": sum(fbs_known),
        "total": sum(known_values) if known_values else None,
        "quality": quality if known_values or not missing_labels else "unavailable",
        "missing_components": missing_labels,
    }


def _append_closed_date_ready_capture(
    conn: sqlite3.Connection,
    *,
    plan: SheetVitrinaV1Envelope,
    bundle_version: str,
    business_date: str,
    refreshed_at: str,
    generation_identity: str,
) -> dict[str, Any] | None:
    """Append a late closed-date WB revision without copying current FBS.

    The accepted ready column is date-bound WB evidence.  FBS is deliberately
    inherited only from the latest immutable capture for that exact business
    date.  If no same-date capture exists, the normal finalization path remains
    fail-closed instead of manufacturing historical FBS from current balances.
    """

    ready_wb = _ready_wb_components(plan, business_date=business_date)
    if not ready_wb:
        return None
    base = conn.execute(
        f"""SELECT capture_id,facility_roster_json
            FROM {CAPTURES_TABLE} WHERE business_date=?
            ORDER BY capture_sequence DESC LIMIT 1""",
        (business_date,),
    ).fetchone()
    if base is None:
        return None
    base_capture_id = str(base[0])
    roster = _loads(base[1], [])
    if not isinstance(roster, list):
        roster = []
    stored = conn.execute(
        f"""SELECT scope_kind,scope_key,nm_id,component_kind,component_id,
                   component_label,state,quantity,source_revision,source_digest,
                   source_watermark,provenance_json
            FROM {COMPONENTS_TABLE} WHERE capture_id=?
            ORDER BY scope_kind,scope_key,component_kind,component_id""",
        (base_capture_id,),
    ).fetchall()
    previous_components = [_stored_component(row) for row in stored]
    previous_wb = {
        str(item["scope_key"]): item
        for item in previous_components
        if str(item["component_kind"]) == "WB"
    }
    previous_fbs = {
        (str(item["scope_key"]), str(item["component_id"])): item
        for item in previous_components
        if str(item["component_kind"]) == "FBS_FACILITY"
    }
    scope_keys = set(previous_wb) | set(ready_wb)
    if not scope_keys:
        return None
    column_digest = _fingerprint(
        {
            "business_date": business_date,
            "values": {key: ready_wb[key] for key in sorted(ready_wb, key=_scope_sort_key)},
        }
    )
    ready_revision = f"ready:{bundle_version}:{plan.snapshot_id}:{business_date}"
    components: list[dict[str, Any]] = []
    normalized_roster = [dict(item) for item in roster if isinstance(item, Mapping)]
    for scope_key in sorted(scope_keys, key=_scope_sort_key):
        scope_kind = "TOTAL" if scope_key == "TOTAL" else "SKU"
        nm_id = None if scope_kind == "TOTAL" else int(scope_key.split(":", 1)[1])
        if scope_key in ready_wb:
            wb_value = ready_wb[scope_key]
            components.append(
                _component(
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    nm_id=nm_id,
                    component_kind="WB",
                    component_id="WB",
                    component_label="WB",
                    value=wb_value,
                    state=_value_state(wb_value),
                    source_revision=ready_revision,
                    source_digest=column_digest,
                    source_watermark=str(plan.snapshot_id),
                    provenance={
                        "source": "accepted_ready_snapshot.exact_date_stock_total",
                        "business_date": business_date,
                        "ready_snapshot_id": plan.snapshot_id,
                        "ready_plan_version": plan.plan_version,
                    },
                )
            )
        elif scope_key in previous_wb:
            components.append(dict(previous_wb[scope_key]))
        for facility in normalized_roster:
            facility_id = str(facility.get("facility_id") or "")
            if not facility_id:
                continue
            prior = previous_fbs.get((scope_key, facility_id))
            if prior is not None:
                components.append(dict(prior))
                continue
            applicable = bool(facility.get("applicable"))
            components.append(
                _component(
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    nm_id=nm_id,
                    component_kind="FBS_FACILITY",
                    component_id=facility_id,
                    component_label=str(facility.get("name") or facility_id),
                    value=None,
                    state="missing" if applicable else "inapplicable",
                    source_revision="",
                    source_digest="",
                    source_watermark="",
                    provenance={
                        "source": "prior_same_date_capture.roster_without_scope_component",
                        "business_date": business_date,
                    },
                )
            )
    fbs_evidence_digest = _fingerprint(
        {
            "business_date": business_date,
            "facility_roster": normalized_roster,
            "components": [
                item
                for item in components
                if str(item.get("component_kind") or "") == "FBS_FACILITY"
            ],
        }
    )
    return append_inventory_history_capture(
        conn,
        business_date=business_date,
        capture_kind="accepted_refresh",
        formula_version=FORMULA_VERSION,
        bundle_version=bundle_version,
        ready_snapshot_id=plan.snapshot_id,
        ready_plan_version=plan.plan_version,
        generation_identity=generation_identity,
        facility_roster=normalized_roster,
        source_manifest={
            "contract": "accepted_ready_closed_inventory_revision_v2",
            "business_date": business_date,
            "ready_snapshot_id": plan.snapshot_id,
            "ready_plan_version": plan.plan_version,
            "bundle_version": bundle_version,
            "wb": {
                "source_revision": ready_revision,
                "column_digest": column_digest,
                "scope_count": len(ready_wb),
                "exact_scope_count": sum(value is not None for value in ready_wb.values()),
            },
            "fbs": {
                "source": "prior_same_date_capture_only",
                "business_date": business_date,
                "component_digest": fbs_evidence_digest,
                "current_balance_read": False,
            },
        },
        components=components,
        captured_at=refreshed_at,
    )


def _canonical_current_wb_evidence(
    conn: sqlite3.Connection,
    *,
    plan: SheetVitrinaV1Envelope,
    business_date: str,
) -> dict[str, Any]:
    """Resolve the WB operand used by the current Web Vitrina projection."""

    ready = _ready_wb_components(plan, business_date=business_date)
    ready_digest = _fingerprint(
        {
            "business_date": business_date,
            "values": {key: ready[key] for key in sorted(ready, key=_scope_sort_key)},
        }
    )
    active_values, active_source = _active_wb_components(
        conn,
        business_date=business_date,
    )
    values: dict[str, int | None] = {}
    component_sources: dict[str, dict[str, Any]] = {}
    if active_values:
        for scope_key in sorted(set(active_values) | set(ready), key=_scope_sort_key):
            if scope_key in active_values:
                values[scope_key] = active_values[scope_key]
                component_sources[scope_key] = {
                    "source_revision": "wb_snapshot:" + str(active_source["snapshot_id"]),
                    "source_digest": str(active_source["snapshot_digest"]),
                    "source_watermark": str(active_source["fetched_at"]),
                    "provenance": {
                        "source": "active_wb_snapshot.current_ui_operand",
                        "snapshot_id": str(active_source["snapshot_id"]),
                        "snapshot_date": business_date,
                    },
                }
            else:
                values[scope_key] = ready[scope_key]
                component_sources[scope_key] = _ready_wb_component_source(
                    plan=plan,
                    business_date=business_date,
                    column_digest=ready_digest,
                    role="accepted_current_column_extra_scope",
                )
        overlap = set(active_values) & set(ready)
        source_manifest = {
            **active_source,
            "contract": "active_wb_snapshot_current_ui_v1",
            "accepted_current_column_digest": ready_digest,
            "accepted_overlap_equivalent": all(
                active_values[key] == ready[key] for key in overlap
            ),
            "accepted_overlap_scope_count": len(overlap),
        }
    else:
        values = dict(ready)
        component_sources = {
            scope_key: _ready_wb_component_source(
                plan=plan,
                business_date=business_date,
                column_digest=ready_digest,
                role="accepted_current_column",
            )
            for scope_key in ready
        }
        source_manifest = {
            "contract": "accepted_ready_current_column_v1",
            "business_date": business_date,
            "ready_snapshot_id": plan.snapshot_id,
            "ready_plan_version": plan.plan_version,
            "column_digest": ready_digest,
        }
    return {
        "values": values,
        "component_sources": component_sources,
        "source_manifest": source_manifest,
    }


def _active_wb_components(
    conn: sqlite3.Connection,
    *,
    business_date: str,
) -> tuple[dict[str, int], dict[str, Any]]:
    if not _table_exists(conn, "sheet_vitrina_v1_warehouse_functional_active"):
        return {}, {}
    if not _table_exists(conn, "sheet_vitrina_v1_warehouse_wb_snapshots"):
        return {}, {}
    row = conn.execute(
        """SELECT snapshot.snapshot_id,snapshot.raw_rows_digest,snapshot.snapshot_date,
                  snapshot.fetched_at,snapshot.version_id,snapshot.items_json
             FROM sheet_vitrina_v1_warehouse_functional_active active
             JOIN sheet_vitrina_v1_warehouse_wb_snapshots snapshot
               ON snapshot.version_id=active.version_id
            WHERE active.slot=1
            ORDER BY snapshot.created_at DESC,snapshot.snapshot_id DESC LIMIT 1"""
    ).fetchone()
    if row is None or str(row[2]) != business_date:
        return {}, {}
    items = _loads(row[5], [])
    by_scope: dict[str, int] = {}
    seen: set[int] = set()
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        try:
            nm_id = int(raw.get("nm_id"))
        except (TypeError, ValueError):
            continue
        quantity = _optional_integer(raw.get("quantity"))
        if nm_id <= 0 or quantity is None or nm_id in seen:
            continue
        seen.add(nm_id)
        by_scope[f"SKU:{nm_id}"] = quantity
    by_scope["TOTAL"] = sum(by_scope.values())
    return by_scope, {
        "snapshot_id": str(row[0]),
        "snapshot_digest": str(row[1]),
        "snapshot_date": str(row[2]),
        "fetched_at": str(row[3]),
        "version_id": str(row[4]),
    }


def _ready_wb_component_source(
    *,
    plan: SheetVitrinaV1Envelope,
    business_date: str,
    column_digest: str,
    role: str,
) -> dict[str, Any]:
    return {
        "source_revision": f"ready:{plan.snapshot_id}:{business_date}",
        "source_digest": column_digest,
        "source_watermark": str(plan.snapshot_id),
        "provenance": {
            "source": role,
            "business_date": business_date,
            "ready_snapshot_id": plan.snapshot_id,
            "ready_plan_version": plan.plan_version,
        },
    }


def _stored_component(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "scope_kind": str(row[0]),
        "scope_key": str(row[1]),
        "nm_id": row[2],
        "component_kind": str(row[3]),
        "component_id": str(row[4]),
        "component_label": str(row[5]),
        "state": str(row[6]),
        "quantity": row[7],
        "source_revision": str(row[8]),
        "source_digest": str(row[9]),
        "source_watermark": str(row[10]),
        "provenance": _loads(row[11], {}),
    }


def _current_components(
    *,
    wb_by_scope: Mapping[str, int | None],
    wb_component_sources: Mapping[str, Mapping[str, Any]],
    fbs: Mapping[str, Any],
) -> list[dict[str, Any]]:
    facility_rows = [
        dict(item) for item in list(fbs.get("facilities") or []) if isinstance(item, Mapping)
    ]
    fbs_skus = {
        int(item["nm_id"])
        for facility in facility_rows
        for item in list(facility.get("sku_values") or [])
        if isinstance(item, Mapping) and str(item.get("nm_id") or "").isdigit()
    }
    scope_keys = set(wb_by_scope) | {f"SKU:{nm_id}" for nm_id in fbs_skus}
    components: list[dict[str, Any]] = []
    for scope_key in sorted(scope_keys, key=_scope_sort_key):
        scope_kind = "TOTAL" if scope_key == "TOTAL" else "SKU"
        nm_id = None if scope_kind == "TOTAL" else int(scope_key.split(":", 1)[1])
        wb_value = wb_by_scope.get(scope_key)
        wb_source = dict(wb_component_sources.get(scope_key) or {})
        components.append(
            _component(
                scope_kind=scope_kind,
                scope_key=scope_key,
                nm_id=nm_id,
                component_kind="WB",
                component_id="WB",
                component_label="WB",
                value=wb_value,
                state=_value_state(wb_value),
                source_revision=str(wb_source.get("source_revision") or ""),
                source_digest=str(wb_source.get("source_digest") or ""),
                source_watermark=str(wb_source.get("source_watermark") or ""),
                provenance=dict(wb_source.get("provenance") or {}),
            )
        )
        for facility in facility_rows:
            facility_id = str(facility.get("facility_id") or "")
            if not facility_id:
                continue
            if scope_kind == "TOTAL":
                value = facility.get("available")
                component_state = str(facility.get("state") or "missing")
                component_reason = str(facility.get("reason") or "")
                component_provenance = dict(facility.get("provenance") or {})
            else:
                sku = next(
                    (
                        item
                        for item in list(facility.get("sku_values") or [])
                        if int(item.get("nm_id") or 0) == nm_id
                    ),
                    None,
                )
                value = sku.get("available") if isinstance(sku, Mapping) else None
                component_state = (
                    str(sku.get("state") or "missing")
                    if isinstance(sku, Mapping)
                    else "missing"
                )
                component_reason = (
                    str(sku.get("reason") or sku.get("reason_ru") or "")
                    if isinstance(sku, Mapping)
                    else "applicable_physical_row_missing"
                )
                component_provenance = (
                    dict(sku.get("provenance") or {})
                    if isinstance(sku, Mapping)
                    else {}
                )
            if component_state not in COMPONENT_STATES:
                component_state = _value_state(value)
            components.append(
                _component(
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    nm_id=nm_id,
                    component_kind="FBS_FACILITY",
                    component_id=facility_id,
                    component_label=str(facility.get("name") or facility_id),
                    value=value,
                    state=component_state,
                    source_revision=str(facility.get("source_revision") or ""),
                    source_digest=str(facility.get("source_digest") or ""),
                    source_watermark=str(facility.get("source_watermark") or ""),
                    provenance={
                        "source": "ff_pool_FBS_physical_minus_active_reserved",
                        "active": bool(facility.get("active")),
                        "applicable": component_state != "inapplicable",
                        "reason": component_reason,
                        "typed_physical_provenance": component_provenance,
                        "updated_at": str(facility.get("updated_at") or ""),
                    },
                )
            )
    return components


def _component(
    *,
    scope_kind: str,
    scope_key: str,
    nm_id: int | None,
    component_kind: str,
    component_id: str,
    component_label: str,
    value: Any,
    state: str,
    source_revision: str,
    source_digest: str,
    source_watermark: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "scope_kind": scope_kind,
        "scope_key": scope_key,
        "nm_id": nm_id,
        "component_kind": component_kind,
        "component_id": component_id,
        "component_label": component_label,
        "state": state,
        "quantity": None if state in {"missing", "inapplicable"} else int(value),
        "source_revision": source_revision,
        "source_digest": source_digest,
        "source_watermark": source_watermark,
        "provenance": dict(provenance),
    }


def _facility_roster(fbs: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "facility_id": str(item.get("facility_id") or ""),
            "code": str(item.get("code") or ""),
            "name": str(item.get("name") or item.get("facility_id") or ""),
            "active": bool(item.get("active")),
            "applicable": bool(item.get("applicable")),
            "effective_from": str(item.get("effective_from") or ""),
            "display_order": int(item.get("display_order") or index),
        }
        for index, item in enumerate(list(fbs.get("facilities") or []), start=1)
        if isinstance(item, Mapping) and str(item.get("facility_id") or "")
    ]


def _ready_wb_components(
    plan: SheetVitrinaV1Envelope,
    *,
    business_date: str,
) -> dict[str, int | None]:
    sheet = next(
        (item for item in plan.sheets if str(item.sheet_name) == "DATA_VITRINA"),
        None,
    )
    if sheet is None or business_date not in list(sheet.header):
        return {}
    column_index = list(sheet.header).index(business_date)
    result: dict[str, int | None] = {}
    for row in sheet.rows:
        if len(row) <= max(1, column_index):
            continue
        row_key = str(row[1] or "")
        value = row[column_index]
        presentation = dict(plan.metadata or {}).get("server_cell_presentation", {}).get(row_key, {}).get(business_date, {})
        if presentation.get("source") == "official_fbs_management_inventory_v1":
            # This public row is WB + official FBS. Only its frozen WB operand
            # belongs in the canonical WB history component, including closure.
            # A missing operand must never fall back to the combined number.
            value = presentation.get("wb_component_value")
        if row_key == "TOTAL|total_stock_total":
            result["TOTAL"] = _optional_integer(value)
        elif row_key.startswith("SKU:") and row_key.endswith("|stock_total"):
            scope_key = row_key.split("|", 1)[0]
            try:
                int(scope_key.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            result[scope_key] = _optional_integer(value)
    return result


def _current_fbs_source(conn: sqlite3.Connection, *, fbs: Mapping[str, Any]) -> dict[str, Any]:
    epoch = dict(fbs.get("formula_epoch") or {})
    return {
        "cutover_id": str(epoch.get("cutover_id") or ""),
        "feature_epoch": epoch.get("feature_epoch"),
        "effective_from": str(epoch.get("effective_from") or ""),
        "facility_roster_revision": _fingerprint(_facility_roster(fbs)),
        "balance_watermark": max(
            (str(item.get("source_watermark") or "") for item in fbs.get("facilities") or []),
            default="",
        ),
        "updated_at": str(fbs.get("updated_at") or ""),
        "seller_stock_role": "excluded_reconciliation_only",
    }


def _normalize_roster(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows, start=1):
        facility_id = str(raw.get("facility_id") or "").strip()
        if not facility_id or facility_id in seen:
            raise ValueError("inventory history facility roster is invalid or duplicated")
        seen.add(facility_id)
        result.append(
            {
                "facility_id": facility_id,
                "code": str(raw.get("code") or ""),
                "name": str(raw.get("name") or facility_id),
                "active": bool(raw.get("active")),
                "applicable": bool(raw.get("applicable")),
                "effective_from": str(raw.get("effective_from") or ""),
                "display_order": int(raw.get("display_order") or index),
            }
        )
    return sorted(
        result,
        key=lambda item: (item["display_order"], item["code"], item["facility_id"]),
    )


def _normalize_components(
    rows: Sequence[Mapping[str, Any]],
    *,
    captured_at: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw in rows:
        scope_kind = str(raw.get("scope_kind") or "")
        scope_key = str(raw.get("scope_key") or "")
        component_kind = str(raw.get("component_kind") or "")
        component_id = str(raw.get("component_id") or "")
        state = str(raw.get("state") or "")
        nm_id = raw.get("nm_id")
        if scope_kind not in SCOPE_KINDS or component_kind not in COMPONENT_KINDS:
            raise ValueError("inventory history component type is invalid")
        if state not in COMPONENT_STATES:
            raise ValueError("inventory history component state is invalid")
        if scope_kind == "TOTAL":
            if scope_key != "TOTAL" or nm_id is not None:
                raise ValueError("inventory history TOTAL scope identity is invalid")
        else:
            nm_id = int(nm_id)
            if nm_id <= 0 or scope_key != f"SKU:{nm_id}":
                raise ValueError("inventory history SKU scope identity is invalid")
        quantity = raw.get("quantity")
        if state in {"missing", "inapplicable"}:
            quantity = None
        else:
            quantity = int(quantity)
            if (state == "exact_zero") != (quantity == 0):
                raise ValueError("inventory history exact/zero state is inconsistent")
        key = (scope_kind, scope_key, component_kind, component_id)
        if key in seen:
            raise ValueError("inventory history component is duplicated")
        seen.add(key)
        result.append(
            {
                "scope_kind": scope_kind,
                "scope_key": scope_key,
                "nm_id": nm_id,
                "component_kind": component_kind,
                "component_id": component_id,
                "component_label": str(raw.get("component_label") or component_id),
                "state": state,
                "quantity": quantity,
                "source_revision": str(raw.get("source_revision") or ""),
                "source_digest": str(raw.get("source_digest") or ""),
                "source_watermark": str(raw.get("source_watermark") or ""),
                "provenance": _jsonable(dict(raw.get("provenance") or {})),
                "captured_at": captured_at,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            0 if item["scope_kind"] == "TOTAL" else 1,
            item["scope_key"],
            item["component_kind"],
            item["component_id"],
        ),
    )


def _slot_date(plan: SheetVitrinaV1Envelope, slot_key: str) -> str:
    return next(
        (
            str(item.column_date)
            for item in plan.temporal_slots
            if str(item.slot_key) == slot_key
        ),
        "",
    )


def _latest_capture_id(conn: sqlite3.Connection, *, business_date: str) -> str:
    row = conn.execute(
        f"""SELECT capture_id FROM {CAPTURES_TABLE}
            WHERE business_date=? ORDER BY capture_sequence DESC LIMIT 1""",
        (business_date,),
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _value_state(value: Any) -> str:
    return "missing" if value is None else "exact_zero" if int(value) == 0 else "exact"


def _optional_integer(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not number.is_integer():
        return None
    return int(number)


def _scope_sort_key(scope_key: str) -> tuple[int, int]:
    if scope_key == "TOTAL":
        return (0, 0)
    try:
        return (1, int(scope_key.split(":", 1)[1]))
    except (IndexError, ValueError):
        return (2, 0)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _ensure_inventory_history_schema_ready(conn: sqlite3.Connection) -> None:
    required = {CAPTURES_TABLE, COMPONENTS_TABLE, FINALIZATIONS_TABLE, APPLIES_TABLE}
    existing = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if required <= existing:
        return
    if conn.in_transaction:
        raise RuntimeError(
            "inventory history schema must be ensured before the write transaction"
        )
    ensure_inventory_history_schema(conn)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
