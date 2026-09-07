"""Isolated, default-off FBS periodic snapshot WAC candidates.

No runtime writer or financial consumer imports this module. The initial day
is a frozen observation boundary; subsequent daily periods use their preceding
closed quantity and cost. Closing is explicit, never a wall-clock side effect.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from packages.application.fbs_document_cost import initial_document_state, resolve_document_costs

SCHEMA = "fbs_snapshot_cost_candidate_v2"
POLICY = "fbs_periodic_snapshot_document_wac_v2"
ZERO = Decimal(0)


class FbsSnapshotCostError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def _decimal(value: Any, *, signed: bool = False, integer: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FbsSnapshotCostError("invalid_number") from exc
    if not number.is_finite() or (not signed and number < ZERO) or (integer and number != number.to_integral_value()):
        raise FbsSnapshotCostError("invalid_number")
    return number


def _text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _day(value: str) -> date:
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise FbsSnapshotCostError("invalid_business_date")
    return result


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise FbsSnapshotCostError("timestamp_requires_timezone")
    return result.astimezone(timezone.utc)


def _key(row: dict) -> str:
    nm, facility = row.get("nm_id"), row.get("facility_id")
    if type(nm) is not int or nm <= 0 or not isinstance(facility, str) or not facility or ":" in facility:
        raise FbsSnapshotCostError("invalid_stock_identity")
    return f"{facility}:{nm}"


def _indexed(rows: list[dict]) -> dict[str, dict]:
    result = {}
    for row in rows:
        key = _key(row)
        if key in result:
            raise FbsSnapshotCostError("duplicate_stock_identity")
        result[key] = row
    return result


def _capture(capture: dict) -> tuple[str, dict[str, dict], dict[str, dict]]:
    if capture.get("contract") not in {None, "fbs_snapshot_cost_sources_v2"}:
        raise FbsSnapshotCostError("source_capture_requires_document_cost_v2")
    day = str(capture["business_date"])
    _day(day)
    snapshot = capture["quantity_snapshot"]
    if (snapshot.get("complete") is not True or snapshot.get("date") != day
            or not snapshot.get("id") or not snapshot.get("digest")
            or not snapshot.get("captured_at") or not capture.get("source_digest")
            or capture.get("documents_complete") is not True):
        raise FbsSnapshotCostError("incomplete_source_capture")
    _timestamp(snapshot["captured_at"])
    if _timestamp(snapshot["captured_at"]) > _timestamp(capture["captured_at"]):
        raise FbsSnapshotCostError("snapshot_after_document_capture")
    rows = _indexed(snapshot["rows"])
    if not rows:
        raise FbsSnapshotCostError("empty_source_capture")
    for row in rows.values():
        _decimal(row["quantity"], integer=True)
    documents = {}
    for doc in capture["documents"]:
        identity = str(doc["document_id"])
        if not identity or identity in documents or not doc.get("fingerprint"):
            raise FbsSnapshotCostError("ambiguous_document_identity")
        _day(doc["business_date"])
        documents[identity] = doc
    return day, rows, documents


def _manifest(documents: dict[str, dict]) -> dict[str, str]:
    return {identity: doc["fingerprint"] for identity, doc in documents.items()}


def _observed(doc: dict, quantities: dict) -> dict:
    raw = doc.get("cost_document", {})
    keys = {_key(event) for event in doc["events"]}
    domain = raw.get("domain", {})
    if doc["kind"] == "pool_overhead" and domain.get("scope") in {"FBS", "both"}:
        keys.update(k for k, r in quantities.items() if r["facility_id"] == domain.get("facility_id"))
    return {"fingerprint": doc["fingerprint"], "business_date": doc["business_date"],
            "affects_fbs": bool(doc["events"] or raw.get("lines") or raw.get("movements") or raw.get("expense_lines")),
            "affected_keys": sorted(keys)}


def initialize_candidate(capture: dict) -> dict:
    """Freeze a reviewable baseline, without setting a production start date."""
    day, quantities, documents = _capture(capture)
    basis = capture["baseline_costs"]
    if (basis.get("available") is not True or not basis.get("version_id")
            or basis.get("document_manifest") != _manifest(documents)):
        raise FbsSnapshotCostError("baseline_source_mismatch")
    costs = _indexed(basis["rows"])
    rows = {}
    with localcontext() as context:
        context.prec = 50
        for key, quantity in quantities.items():
            cost = costs.get(key, {})
            wac = _decimal(cost["wac_rub"]) if cost.get("wac_rub") is not None else None
            if wac is not None and not cost.get("source"):
                raise FbsSnapshotCostError("initial_cost_source_missing")
            q = _decimal(quantity["quantity"], integer=True)
            rows[key] = {
                "nm_id": quantity["nm_id"], "facility_id": quantity["facility_id"],
                "quantity": _text(q), "wac_rub": _text(wac),
                "capital_rub": _text(q * wac) if wac is not None else "0" if q == ZERO else None,
                "cost_source": deepcopy(cost.get("source", {})),
                "quality": "accepted_initial_cost" if wac is not None else "empty_unpriced" if q == ZERO else "missing_cost",
            }
    baseline = {
        "business_date": day, "snapshot": deepcopy(capture["quantity_snapshot"]),
        "cost_version_id": basis["version_id"], "rows": rows,
        "absorbed_documents": _manifest(documents), "source_digest": capture["source_digest"],
        "document_cost_state": initial_document_state(capture),
    }
    baseline["id"] = fingerprint(baseline)
    return {
        "schema": SCHEMA, "policy": POLICY, "candidate_only": True,
        "baseline": baseline, "periods": {}, "pending_documents": [],
        "last_document_capture_at": capture["captured_at"],
        "observed_documents": {identity: _observed(doc, quantities)
            for identity, doc in documents.items()},
        "last_attempt": {"date": day, "source_digest": capture["source_digest"], "status": "baseline_frozen"},
    }


def _last_closed(state: dict) -> tuple[str, dict]:
    day, rows = state["baseline"]["business_date"], state["baseline"]["rows"]
    for period_day, period in sorted(state["periods"].items()):
        if period["status"] == "closed":
            day, rows = period_day, period["rows"]
    return day, rows


def evaluate_candidate(state: dict, capture: dict) -> dict:
    """Rebuild the open day from its frozen opening, never from a prior attempt."""
    if state.get("schema") != SCHEMA or state.get("policy") != POLICY or state.get("candidate_only") is not True:
        raise FbsSnapshotCostError("invalid_candidate_state")
    day, quantities, documents = _capture(capture)
    if _timestamp(capture["captured_at"]) < _timestamp(state["last_document_capture_at"]):
        raise FbsSnapshotCostError("document_observation_time_regression")
    result = deepcopy(state)
    result["last_document_capture_at"] = capture["captured_at"]
    closed_day, opening = _last_closed(state)
    known = dict(state["baseline"]["absorbed_documents"])
    for period in state["periods"].values():
        if period["status"] == "closed":
            known.update(period["applied_documents"])
    pending = []
    for identity, digest in known.items():
        doc = documents.get(identity)
        if doc is None or doc["fingerprint"] != digest:
            pending.append({"document_id": identity, "reason": "absorbed_document_missing_or_changed"})
    for identity, doc in documents.items():
        if identity not in known and doc["business_date"] <= closed_day and _observed(doc, quantities)["affects_fbs"]:
            pending.append({"document_id": identity, "reason": "late_closed_period_document", "business_date": doc["business_date"]})
    observed = result["observed_documents"]
    for identity, item in observed.items():
        if identity not in documents and identity not in known and item["affects_fbs"]:
            pending.append({"document_id": identity, "reason": "observed_document_missing", **item})
    observed.update({identity: _observed(doc, quantities)
        for identity, doc in documents.items()})
    result["pending_documents"] = pending
    result["last_attempt"] = {"date": day, "source_digest": capture["source_digest"], "status": "evaluated"}
    if day <= closed_day:
        result["last_attempt"]["status"] = "closed_period_preserved"
        return result
    if _day(day) != _day(closed_day) + timedelta(days=1):
        open_day = (_day(closed_day) + timedelta(days=1)).isoformat()
        prior_open = state["periods"].get(open_day)
        if prior_open is not None and prior_open["status"] == "open":
            # New document observations can revise a still-open prior day.
            # Its saved quantity snapshot remains dated to that day; today's
            # quantity is never substituted into yesterday's valuation.
            retained = deepcopy(capture)
            retained["business_date"] = open_day
            retained["quantity_snapshot"] = deepcopy(prior_open["snapshot"])
            retained["source_digest"] = fingerprint({
                "retained_quantity_snapshot": prior_open["snapshot"],
                "document_capture_source_digest": capture["source_digest"],
            })
            result = evaluate_candidate(state, retained)
            result["periods"][open_day]["document_capture_date"] = day
            result["last_attempt"] = {"date": day, "target_date": open_day,
                "status": "prior_open_period_revalued", "source_digest": capture["source_digest"]}
            return result
        result["last_attempt"].update(status="missing_period_source", expected_date=(_day(closed_day) + timedelta(days=1)).isoformat())
        return result
    previous = state["periods"].get(day)
    if previous and previous["opening_rows"] != opening:
        raise FbsSnapshotCostError("period_opening_drift")
    if previous and _timestamp(capture["quantity_snapshot"]["captured_at"]) < _timestamp(previous["snapshot"]["captured_at"]):
        raise FbsSnapshotCostError("snapshot_time_regression")
    if not set(opening) <= set(quantities):
        raise FbsSnapshotCostError("snapshot_lost_opening_scope")
    opening_facilities = {row["facility_id"] for row in opening.values()}
    if {row["facility_id"] for row in quantities.values()} != opening_facilities:
        raise FbsSnapshotCostError("facility_scope_changed_requires_baseline")
    documents, document_cost_state, document_valuation = resolve_document_costs(state, capture, opening, known)
    events: dict[str, list[dict]] = {}
    applied = {}
    blocked_keys = {key for item in pending if item.get("business_date") == day
                    for key in item.get("affected_keys", [])}
    for identity, doc in documents.items():
        if identity in known or doc["business_date"] != day:
            continue
        unsupported = [event for event in doc["events"] if event["kind"] not in {"receipt", "expense", "outgoing"}]
        if unsupported:
            affected = sorted({_key(event) for event in doc["events"]})
            blocked_keys.update(affected)
            pending.append({"document_id": identity, "reason": "document_requires_independent_cost", "kind": doc["kind"], "affected_keys": affected})
            continue
        for event in doc["events"]:
            key = _key(event)
            if key not in quantities:
                raise FbsSnapshotCostError("document_outside_snapshot_scope")
            events.setdefault(key, []).append({**event, "document_id": identity, "document_fingerprint": doc["fingerprint"]})
        applied[identity] = doc["fingerprint"]
    rows = {}
    diagnostics = []
    with localcontext() as context:
        context.prec = 50
        for key, quantity in quantities.items():
            # New catalog SKUs have no admitted previous stock or cost; a first
            # independent receipt can establish cost. Missing known rows cannot.
            prior = opening.get(key, {"quantity": "0", "wac_rub": None})
            prior_q = _decimal(prior["quantity"], integer=True)
            prior_wac = None if prior["wac_rub"] is None else _decimal(prior["wac_rub"])
            q = _decimal(quantity["quantity"], integer=True)
            received_q, received_value, expense_value = ZERO, ZERO, ZERO
            for event in events.get(key, []):
                if not event.get("source"):
                    raise FbsSnapshotCostError("document_cost_source_missing")
                if event["kind"] == "receipt":
                    amount = _decimal(event["quantity"], integer=True)
                    if amount <= ZERO:
                        raise FbsSnapshotCostError("receipt_quantity_not_positive")
                    received_q += amount
                    received_value += _decimal(event["capital_rub"])
                elif event["kind"] == "expense":
                    if _decimal(event["quantity"], integer=True) != ZERO:
                        raise FbsSnapshotCostError("expense_changes_quantity")
                    expense_value += _decimal(event["capital_rub"], signed=True)
                else:
                    _decimal(event["quantity"], integer=True)
                    if _decimal(event["capital_rub"], signed=True) != ZERO:
                        raise FbsSnapshotCostError("outgoing_must_not_reuse_legacy_capital")
            mass = prior_q + received_q
            wac = prior_wac
            issue = ""
            if received_q != ZERO or expense_value != ZERO:
                if prior_q > ZERO and prior_wac is None:
                    wac, issue = None, "missing_opening_cost"
                elif mass <= ZERO:
                    wac, issue = None, "expense_without_cost_mass"
                else:
                    value = prior_q * (prior_wac if prior_wac is not None else ZERO) + received_value + expense_value
                    if value < ZERO:
                        wac, issue = None, "negative_calculated_capital"
                    else:
                        wac = value / mass
            if q > ZERO and wac is None:
                issue = issue or "missing_cost"
            if key in blocked_keys:
                wac, issue = None, "document_cost_unresolved"
            if issue:
                diagnostics.append({"key": key, "reason": issue})
            if q > prior_q + received_q:
                diagnostics.append({"key": key, "reason": "quantity_increase_without_receipt", "quantity": _text(q - prior_q - received_q)})
            rows[key] = {
                "nm_id": quantity["nm_id"], "facility_id": quantity["facility_id"],
                "opening_quantity": _text(prior_q), "opening_wac_rub": _text(prior_wac),
                "receipt_quantity": _text(received_q), "receipt_capital_rub": _text(received_value),
                "expense_capital_rub": _text(expense_value), "cost_mass_quantity": _text(mass),
                "quantity": _text(q), "wac_rub": _text(wac),
                "capital_rub": _text(q * wac) if wac is not None else "0" if q == ZERO else None,
                "quality": "missing_cost" if issue else "preliminary_snapshot_wac" if wac is not None else "empty_unpriced",
                "documents": deepcopy(events.get(key, [])),
            }
    blocking_diagnostics = [item for item in diagnostics if item["reason"] != "quantity_increase_without_receipt"]
    result["periods"][day] = {
        "status": "open", "quality": "incomplete" if pending or blocking_diagnostics else "preliminary",
        "opening_date": closed_day, "opening_rows": deepcopy(opening), "rows": rows,
        "snapshot": deepcopy(capture["quantity_snapshot"]), "source_digest": capture["source_digest"],
        "applied_documents": applied, "diagnostics": diagnostics,
        "document_cost_state": document_cost_state, "document_valuation": document_valuation,
    }
    return result


def close_candidate_period(state: dict, day: str, *, today: str) -> dict:
    """Explicit candidate closure; schedule and final T0 belong to activation."""
    if state.get("schema") != SCHEMA or state.get("policy") != POLICY or state.get("candidate_only") is not True:
        raise FbsSnapshotCostError("candidate_requires_document_cost_v2_baseline")
    _day(day)
    if _day(today) <= _day(day):
        raise FbsSnapshotCostError("current_day_cannot_close")
    result = deepcopy(state)
    period = result["periods"].get(day)
    if period is None:
        raise FbsSnapshotCostError("period_missing")
    if period["status"] == "closed":
        return result
    if period["quality"] != "preliminary" or result["pending_documents"]:
        raise FbsSnapshotCostError("incomplete_period_cannot_close")
    attempt = result["last_attempt"]
    if (attempt["status"] not in {"evaluated", "prior_open_period_revalued"}
            or attempt.get("target_date", attempt["date"]) != day):
        raise FbsSnapshotCostError("period_requires_revaluation_before_close")
    period["status"] = "closed"
    period["quality"] = "closed_snapshot_wac"
    result["last_attempt"] = {"date": day, "status": "candidate_period_closed", "source_digest": period["source_digest"]}
    return result


def candidate_period_view(state: dict, day: str) -> dict:
    """Exact-date interface for future consumers; no latest-price fallback."""
    if state.get("schema") != SCHEMA or state.get("policy") != POLICY or state.get("candidate_only") is not True:
        raise FbsSnapshotCostError("candidate_requires_document_cost_v2_baseline")
    _day(day)
    baseline = state["baseline"]
    period = state["periods"].get(day)
    result = {"candidate_only": True, "policy": POLICY, "date": day,
              "state_fingerprint": fingerprint(state), "available": False}
    if period is not None:
        rows, status, quality = period["rows"], period["status"], period["quality"]
        document_cost_state = period["document_cost_state"]
        source = {"snapshot_id": period["snapshot"]["id"], "source_digest": period["source_digest"]}
    elif day == baseline["business_date"]:
        rows, status, quality = baseline["rows"], "baseline", "accepted_initial_cost"
        document_cost_state = baseline["document_cost_state"]
        source = {"snapshot_id": baseline["snapshot"]["id"], "source_digest": baseline["source_digest"], "cost_version_id": baseline["cost_version_id"]}
    else:
        return {**result, "reason": "exact_date_cost_unavailable"}
    with localcontext() as context:
        context.prec = 50
        quantity = sum((_decimal(row["quantity"], integer=True) for row in rows.values()), ZERO)
        missing = [key for key, row in rows.items() if row["capital_rub"] is None]
        capital = None if missing else sum((_decimal(row["capital_rub"]) for row in rows.values()), ZERO)
        result.update(available=True, status=status, quality="incomplete" if missing else quality,
                      rows=deepcopy(rows), quantity=_text(quantity), capital_rub=_text(capital),
                      wac_rub=_text(capital / quantity) if capital is not None and quantity > ZERO else None,
                      unpriced_keys=missing, pending_documents=deepcopy(state["pending_documents"]), source=source)
        fbo = document_cost_state["fbo_rows"]
        fbo_q = sum((_decimal(r["quantity"], integer=True) for r in fbo.values()), ZERO)
        fbo_capital = sum((_decimal(r["capital_rub"]) for r in fbo.values()), ZERO)
        # Future total-capital consumers must use the matching FBO allocation,
        # not add legacy FBO expenses on top of the new FBS share.
        result["fbo_component"] = {"rows": deepcopy(fbo), "quantity": _text(fbo_q),
                                   "capital_rub": _text(fbo_capital), "policy": document_cost_state["policy"]}
    return result


class CandidateStore:
    """Small separate SQLite store; existing business databases are rejected."""

    TABLES = {"fbs_cost_candidate_current", "fbs_cost_candidate_revisions"}

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def load(self) -> tuple[dict | None, str | None]:
        if not self.path.exists():
            return None, None
        conn = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            self._check_tables(conn)
            row = conn.execute("SELECT payload_json,fingerprint FROM fbs_cost_candidate_current WHERE slot=1").fetchone()
            if row:
                state = json.loads(row[0])
                if fingerprint(state) != row[1]:
                    raise FbsSnapshotCostError("candidate_fingerprint_mismatch")
                return state, row[1]
            return None, None
        finally:
            conn.close()

    def _check_tables(self, conn: sqlite3.Connection) -> None:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != self.TABLES:
            raise FbsSnapshotCostError("not_an_isolated_candidate_database")

    def save(self, state: dict, *, expected_fingerprint: str | None) -> str:
        if state.get("schema") != SCHEMA or state.get("policy") != POLICY or state.get("candidate_only") is not True:
            raise FbsSnapshotCostError("invalid_candidate_state")
        if self.path.exists():
            self.load()  # Read-only identity admission, before any write pragma.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != self.TABLES:
                raise FbsSnapshotCostError("not_an_isolated_candidate_database")
            conn.execute("CREATE TABLE IF NOT EXISTS fbs_cost_candidate_current(slot INTEGER PRIMARY KEY CHECK(slot=1),fingerprint TEXT NOT NULL,payload_json TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS fbs_cost_candidate_revisions(fingerprint TEXT PRIMARY KEY,payload_json TEXT NOT NULL)")
            for operation in ("UPDATE", "DELETE"):
                conn.execute(f"CREATE TRIGGER IF NOT EXISTS fbs_candidate_no_{operation.lower()} BEFORE {operation} ON fbs_cost_candidate_revisions BEGIN SELECT RAISE(ABORT,'immutable candidate revision'); END")
            row = conn.execute("SELECT fingerprint,payload_json FROM fbs_cost_candidate_current WHERE slot=1").fetchone()
            current = row[0] if row else None
            digest = fingerprint(state)
            if current == digest:
                conn.commit()
                return digest
            if current != expected_fingerprint:
                raise FbsSnapshotCostError("candidate_compare_and_swap_failed")
            if row:
                prior = json.loads(row[1])
                if prior["baseline"] != state["baseline"]:
                    raise FbsSnapshotCostError("frozen_baseline_changed")
                for day, period in prior["periods"].items():
                    if period["status"] == "closed" and state["periods"].get(day) != period:
                        raise FbsSnapshotCostError("closed_period_changed")
            conn.execute("INSERT OR IGNORE INTO fbs_cost_candidate_revisions VALUES(?,?)", (digest, canonical(state)))
            conn.execute("INSERT INTO fbs_cost_candidate_current VALUES(1,?,?) ON CONFLICT(slot) DO UPDATE SET fingerprint=excluded.fingerprint,payload_json=excluded.payload_json", (digest, canonical(state)))
            conn.commit()
            return digest
        finally:
            conn.close()
