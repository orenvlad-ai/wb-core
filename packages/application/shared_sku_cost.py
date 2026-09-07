"""Versioned, candidate-only daily SKU cost shared by Finance and Partner."""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Mapping, Sequence

from packages.application.fbs_snapshot_cost import candidate_period_view, fingerprint

POLICY = "shared_sku_daily_snapshot_wac_v1"
SCHEMA = "shared_sku_cost_day_v1"
COMPONENTS = ("WB", "FF_FBS", "FF_FBO")
ZERO = Decimal(0)


class SharedSkuCostError(ValueError):
    pass


def _day(value: str) -> str:
    if date.fromisoformat(value).isoformat() != value:
        raise SharedSkuCostError("invalid_business_date")
    return value


def _number(value, *, quantity=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SharedSkuCostError("invalid_component_number") from exc
    if not result.is_finite() or result < 0 or (quantity and result != result.to_integral_value()):
        raise SharedSkuCostError("invalid_component_number")
    return result


def _nm(value) -> str:
    result = str(value)
    if not result.isdigit() or int(result) <= 0 or str(int(result)) != result:
        raise SharedSkuCostError("invalid_nm_id")
    return result


def _component(name: str, rows: list[dict], *, absent_is_zero=False) -> dict:
    if not rows and not absent_is_zero:
        return {"component": name, "status": "missing", "reason": "component_row_missing"}
    quantity, capital = ZERO, ZERO
    try:
        for row in rows:
            if row.get("status") in {"missing", "incomplete", "unavailable"}:
                raise SharedSkuCostError(str(row.get("reason") or "component_incomplete"))
            q = _number(row["quantity"], quantity=True)
            c = _number(row["capital_rub"])
            if (q == ZERO and c != ZERO) or (q > ZERO and c <= ZERO):
                raise SharedSkuCostError("component_capital_not_covered")
            quantity += q
            capital += c
    except (KeyError, SharedSkuCostError) as exc:
        return {"component": name, "status": "missing", "reason": str(exc)}
    return {"component": name, "status": "resolved", "reason": "",
            "quantity": format(quantity, "f"), "capital_rub": format(capital, "f"),
            "rows": deepcopy(rows), "zero_basis": "complete_fbo_document_book" if not rows else ""}


def build_shared_cost_day(fbs_state: dict, wb_capture: dict, day: str) -> dict:
    """Blend the same SKU and exact date; never substitute a legacy FF price."""
    _day(day)
    if (wb_capture.get("contract") != "shared_sku_cost_wb_source_v1"
            or wb_capture.get("business_date") != day or not wb_capture.get("source_digest")):
        raise SharedSkuCostError("wb_source_contract_or_date_mismatch")
    view = candidate_period_view(fbs_state, day)
    if not view["available"]:
        raise SharedSkuCostError("exact_fbs_period_unavailable")
    if view["status"] not in {"baseline", "open", "closed"}:
        raise SharedSkuCostError("unsupported_fbs_period_status")
    fbs, fbo, wb = {}, {}, {}
    for row in view["rows"].values():
        fbs.setdefault(_nm(row["nm_id"]), []).append(row)
    # An absent FBO position is zero only in the complete document book
    # produced by the same FBS state. It is not inferred from a sparse API.
    for row in view["fbo_component"]["rows"].values():
        fbo.setdefault(_nm(row["nm_id"]), []).append(row)
    for row in wb_capture["rows"]:
        key = _nm(row["nm_id"])
        if key in wb:
            raise SharedSkuCostError("duplicate_wb_sku")
        wb[key] = row
    if (set(fbo) | set(wb)) - set(fbs):
        raise SharedSkuCostError("component_sku_outside_fbs_catalog")
    global_reason = ""
    if not wb_capture.get("authority_complete", wb_capture.get("complete")):
        global_reason = str(wb_capture.get("reason") or "wb_source_incomplete")
    if (view["status"] == "open" and view.get("pending_documents")) or view.get("quality") == "incomplete":
        global_reason = "fbs_period_incomplete"
    rows = {}
    with localcontext() as ctx:
        ctx.prec = 50
        for nm in sorted(fbs, key=int):
            parts = [_component("WB", [wb[nm]] if nm in wb else []),
                     _component("FF_FBS", fbs[nm]),
                     _component("FF_FBO", fbo.get(nm, []), absent_is_zero=True)]
            reason = global_reason or next((p["reason"] for p in parts if p["status"] != "resolved"), "")
            q = c = price = None
            if not reason:
                q = sum((_number(p["quantity"], quantity=True) for p in parts), ZERO)
                c = sum((_number(p["capital_rub"]) for p in parts), ZERO)
                if q == ZERO:
                    reason = "no_inventory_weight_for_shared_cost"
                else:
                    price = c / q
            row = {"nm_id": int(nm), "quantity": format(q, "f") if q is not None else None,
                   "capital_rub": format(c, "f") if c is not None else None,
                   "unit_cost_rub": format(price, "f") if price is not None else None,
                   "status": "missing" if reason else "resolved", "reason": reason,
                   "components": parts}
            row["source_digest"] = fingerprint(row)
            rows[nm] = row
    result = {"schema": SCHEMA, "policy": POLICY, "candidate_only": True,
              "business_date": day, "components": list(COMPONENTS),
              "status": "closed" if view["status"] == "closed" else "preliminary",
              "quality": "incomplete" if global_reason or any(r["status"] == "missing" and r["reason"] != "no_inventory_weight_for_shared_cost" for r in rows.values()) else "complete",
              "reason": global_reason, "rows": rows,
              "source": {"fbs_baseline_id": fbs_state["baseline"]["id"],
                         "fbs_period_fingerprint": fingerprint(fbs_state["periods"].get(day, fbs_state["baseline"])),
                         "fbs_period_source": view["source"],
                         "fbs_status": view["status"],
                         "fbo_policy": view["fbo_component"]["policy"],
                         "wb_version_id": wb_capture.get("version_id", ""),
                         "wb_source_digest": wb_capture["source_digest"]}}
    result["version_id"] = fingerprint(result)
    return result


def _validate(period: dict) -> None:
    if (period.get("schema") != SCHEMA or period.get("policy") != POLICY
            or period.get("candidate_only") is not True or period.get("components") != list(COMPONENTS)
            or period.get("status") not in {"preliminary", "closed"}
            or period.get("quality") not in {"complete", "incomplete"}):
        raise SharedSkuCostError("invalid_shared_cost_period")
    _day(period["business_date"])
    if not period.get("source", {}).get("fbs_baseline_id"):
        raise SharedSkuCostError("shared_cost_initial_basis_missing")
    if period.get("version_id") != fingerprint({k: v for k, v in period.items() if k != "version_id"}):
        raise SharedSkuCostError("shared_cost_fingerprint_mismatch")
    if period["status"] == "closed" and period["quality"] != "complete":
        raise SharedSkuCostError("cannot_close_incomplete_shared_cost")
    for nm, row in period["rows"].items():
        if _nm(nm) != _nm(row["nm_id"]):
            raise SharedSkuCostError("shared_cost_sku_identity_mismatch")
        if row["status"] == "resolved" and _number(row["unit_cost_rub"]) <= 0:
            raise SharedSkuCostError("shared_cost_nonpositive")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


@dataclass(frozen=True, init=False)
class SharedSkuCostSnapshot:
    """One immutable date/version set pinned for the whole report calculation."""
    effective_date: str
    version_id: str
    formula_version: str
    _days: Mapping

    def __init__(self, periods: Sequence[dict], *, effective_date: str):
        _day(effective_date)
        days = {}
        for period in periods:
            _validate(period)
            day = period["business_date"]
            if day < effective_date or day in days:
                raise SharedSkuCostError("duplicate_or_pretransition_shared_day")
            days[day] = deepcopy(period)
        if len({p["source"]["fbs_baseline_id"] for p in days.values()}) > 1:
            raise SharedSkuCostError("mixed_fbs_initial_cost_bases")
        identity = {"effective_date": effective_date, "policy": POLICY,
                    "days": {day: p["version_id"] for day, p in sorted(days.items())}}
        object.__setattr__(self, "effective_date", effective_date)
        object.__setattr__(self, "version_id", fingerprint(identity))
        object.__setattr__(self, "formula_version", POLICY)
        object.__setattr__(self, "_days", _freeze(days))

    def applies_to(self, operation_date: date) -> bool:
        return operation_date.isoformat() >= self.effective_date

    def metadata(self) -> dict:
        return {"cost_method_version": POLICY, "policy_date": self.effective_date,
                "effective_date": self.effective_date, "version_id": self.version_id,
                "candidate_only": True}

    def resolve(self, *, nm_id: str, operation_date: date) -> dict:
        day = operation_date.isoformat()
        base = {"nm_id": str(nm_id), "operation_date": day, "canonical_source_date": day,
                "formula_version": POLICY, "channel": "COMMON", "pool": "WB+FBS+FBO",
                "facility_id": "", "channel_classification": "shared_sku_cost",
                "selection_method": "same_sku_same_date_sum_capital_divided_by_sum_quantity",
                "source_table": "shared_sku_cost_versions", "candidate_only": True,
                "policy_date": self.effective_date, "snapshot_version_id": self.version_id}
        if not self.applies_to(operation_date):
            return {**base, "status": "missing", "reason": "before_shared_cost_transition"}
        try:
            nm = _nm(nm_id)
        except SharedSkuCostError:
            return {**base, "status": "missing", "reason": "shared_cost_nm_id_unresolved"}
        period = self._days.get(day)
        if period is None:
            return {**base, "status": "missing", "reason": "shared_cost_exact_date_missing"}
        row = period["rows"].get(nm)
        quality = "shared_daily_cost_closed" if period["status"] == "closed" else "shared_daily_cost_preliminary"
        base.update(canonical_source_version=period["version_id"],
                    canonical_source_identity=f"{POLICY}:{day}:{nm}:{period['version_id']}",
                    source_digest=fingerprint({"period": period["version_id"], "row": row["source_digest"] if row else "missing"}),
                    quality=quality, projection_quality=quality)
        if row is None:
            return {**base, "status": "missing", "reason": "shared_cost_sku_missing"}
        if row["status"] != "resolved":
            return {**base, "status": "missing", "reason": row["reason"]}
        return {**base, "status": "resolved", "reason": "", "unit_cost_rub": row["unit_cost_rub"]}


class SharedSkuCostStore:
    """Separate candidate file with immutable versions and CAS day pointers."""
    TABLES = {"shared_sku_cost_versions", "shared_sku_cost_days"}

    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    def _admit(self, conn):
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != self.TABLES:
            raise SharedSkuCostError("not_an_isolated_shared_cost_database")

    def read(self, day: str) -> dict | None:
        _day(day)
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.as_uri()+"?mode=ro", uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            self._admit(conn)
            row = conn.execute("SELECT v.payload_json FROM shared_sku_cost_days d JOIN shared_sku_cost_versions v USING(version_id) WHERE d.business_date=?", (day,)).fetchone()
            result = json.loads(row[0]) if row else None
            if result is not None:
                _validate(result)
            return result

    def save(self, period: dict, *, expected_version: str | None) -> str:
        _validate(period)
        day, version = period["business_date"], period["version_id"]
        if self.path.exists():
            self.read(day)  # admission before any write pragma
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and tables != self.TABLES:
                raise SharedSkuCostError("not_an_isolated_shared_cost_database")
            conn.execute("CREATE TABLE IF NOT EXISTS shared_sku_cost_versions(version_id TEXT PRIMARY KEY,business_date TEXT NOT NULL,payload_json TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS shared_sku_cost_days(business_date TEXT PRIMARY KEY,version_id TEXT NOT NULL REFERENCES shared_sku_cost_versions(version_id))")
            for saved in conn.execute("SELECT payload_json FROM shared_sku_cost_versions LIMIT 1"):
                prior = json.loads(saved[0])
                if prior["source"]["fbs_baseline_id"] != period["source"]["fbs_baseline_id"]:
                    raise SharedSkuCostError("mixed_fbs_initial_cost_bases")
            old = conn.execute("SELECT v.version_id,v.payload_json FROM shared_sku_cost_days d JOIN shared_sku_cost_versions v USING(version_id) WHERE d.business_date=?", (day,)).fetchone()
            if (old[0] if old else None) != expected_version:
                raise SharedSkuCostError("shared_cost_compare_and_swap_failed")
            if old and old[0] == version:
                return version
            if old and json.loads(old[1])["status"] == "closed":
                raise SharedSkuCostError("closed_shared_cost_is_immutable")
            conn.execute("INSERT OR IGNORE INTO shared_sku_cost_versions VALUES(?,?,?)", (version, day, json.dumps(period, ensure_ascii=False, sort_keys=True)))
            conn.execute("INSERT INTO shared_sku_cost_days VALUES(?,?) ON CONFLICT(business_date) DO UPDATE SET version_id=excluded.version_id", (day, version))
        return version

    def snapshot(self, *, effective_date: str) -> SharedSkuCostSnapshot:
        _day(effective_date)
        if not self.path.exists():
            return SharedSkuCostSnapshot([], effective_date=effective_date)
        conn = sqlite3.connect(self.path.as_uri()+"?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            self._admit(conn)
            periods = [json.loads(row[0]) for row in conn.execute(
                "SELECT v.payload_json FROM shared_sku_cost_days d JOIN shared_sku_cost_versions v USING(version_id) WHERE d.business_date>=? ORDER BY d.business_date", (effective_date,))]
            return SharedSkuCostSnapshot(periods, effective_date=effective_date)
        finally:
            conn.close()
