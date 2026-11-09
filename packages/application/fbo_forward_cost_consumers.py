"""Immutable intraday expense boundaries for COGS and indicative order profit.

Current inventory is valued by the warehouse owner. Consumers never add the
expense to inventory again, or use its new price for the already observed
part of a day's orders. Date-only sales retain an explicitly provisional
pre-expense basis because their place within that day is unknown.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json

from packages.business_time import business_date_from_timestamp

CONTRACT = "fbo_forward_consumer_boundary_v1"


def number(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def load_effects(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("sheet_vitrina_v1_fbo_forward_cost_effects",)).fetchone():
        return []
    from packages.application.fbo_forward_costs import load_posted_effects
    return [dict(row) for row in load_posted_effects(conn)
            if row.get("status") == "posted"]


def _snapshot(effect):
    value = effect.get("snapshot") or {}
    return json.loads(value) if isinstance(value, str) else value


def _posted_at(effect):
    return str(effect.get("posted_at") or _snapshot(effect).get("posted_at") or "")


def effects_for_day(effects, nm_id, day):
    if not str(nm_id).isdigit():
        return []
    return sorted((e for e in effects if int(e["nm_id"]) == int(nm_id)
        and _posted_at(e) and business_date_from_timestamp(_posted_at(e)) == day),
        key=lambda e: (_moment(_posted_at(e)), int(_snapshot(e).get("posting_sequence") or 0),
                       str(e.get("requested_at") or ""), str(e["effect_id"])))


def _moment(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except ValueError:
        return None


def operation_timestamp(operation, day):
    """Only source-proven aware time on the selected operation day is usable."""
    for key in ("cost_effective_at", "rrDate", "saleDt"):
        value = str((operation or {}).get(key) or "")
        if "T" not in value and " " not in value:
            continue
        moment = _moment(value)
        # WB can serialize a date at midnight. That representation alone is
        # not evidence of a sale's time within the day.
        if (moment is not None and (key == "cost_effective_at" or any((moment.hour, moment.minute, moment.second, moment.microsecond)))
                and business_date_from_timestamp(value) == day):
            return moment.isoformat()
    return ""


def capture_order_boundary(conn, nm_id, posted_at=None):
    """Pin accepted current-day operands; no clock or external file reads.

    The writer checks this captured day against its actual posting day. Its
    existing source CAS pins this entire JSON-safe boundary through admission.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    day = business_date_from_timestamp(posted_at) if posted_at else ""
    versions = "sheet_vitrina_v1_warehouse_functional_versions"
    if not day and versions in tables:
        row = conn.execute(f"SELECT v.business_effective_date FROM {versions} v "
            "JOIN sheet_vitrina_v1_warehouse_functional_active a ON a.version_id=v.version_id WHERE a.slot=1").fetchone()
        day = str(row[0]) if row else ""
    result = dict(contract=CONTRACT, status="pending", business_date=day,
                  nm_id=int(nm_id), reason="current_published_order_boundary_missing")
    table = "sheet_vitrina_v1_ready_snapshots"
    if not day or table not in tables or "registry_upload_current_state" not in tables:
        return result
    active_bundle = conn.execute("SELECT bundle_version FROM registry_upload_current_state WHERE slot=1").fetchone()
    if active_bundle is None:
        return result
    prior = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    for row in conn.execute(f"SELECT snapshot_id,refreshed_at,plan_json FROM {table} "
            "WHERE bundle_version=? AND as_of_date IN (?,?) ORDER BY refreshed_at DESC,snapshot_id DESC LIMIT 1",
            (active_bundle[0], day, prior)):
        plan = json.loads(row[2])
        sheet = next((s for s in plan.get("sheets", [])
            if s.get("sheet_name", s.get("name")) == "DATA_VITRINA"), None)
        if not sheet or day not in sheet.get("header", []):
            continue
        index = sheet["header"].index(day)
        rows = {r[1]: r for r in sheet["rows"]}
        cells = plan.get("metadata", {}).get("server_cell_presentation", {})
        scope = f"SKU:{nm_id}|"
        def value(metric):
            key = scope + metric
            cell = cells.get(key, {}).get(day, {})
            if cell.get("state") == "unavailable" or cell.get("candidate_only") is True and cell.get("management_value") in (None, ""):
                return None
            raw = rows.get(key, [])
            return number(cell.get("management_value", raw[index] if len(raw) > index else None))
        values = {k: value(k) for k in ("orderCount", "orderSum", "ads_sum", "our_wb_unit_cost_rub",
            "own_capital_WB_qty", "own_capital_FF_qty")}
        if any(v is None or v < 0 for v in values.values()) or values["our_wb_unit_cost_rub"] <= 0:
            return {**result, "ready_snapshot_id": row[0], "reason": "published_order_boundary_incomplete"}
        quantity = values["own_capital_WB_qty"] + values["own_capital_FF_qty"]
        if quantity <= 0:
            return {**result, "reason": "published_common_cost_quantity_missing"}
        result.update(status="ready", reason="", ready_snapshot_id=row[0], refreshed_at=row[1],
            bundle_version=active_bundle[0],
            operands={k: format(v, "f") for k, v in values.items()},
            common_quantity=format(quantity, "f"),
            included_effect_ids=[e["effect_id"] for e in plan.get("metadata", {}).get("fbo_forward_cost_epochs", [])
                if int(e["nm_id"]) == int(nm_id)],
            parameter_versions={str(v): cells.get(scope + f"proxy_profit_{v}_rub", {}).get(day, {}).get("evidence", {}).get("parameter_version")
                for v in (3, 4)})
        balances = "sheet_vitrina_v1_warehouse_functional_balances"
        if balances in tables:
            current = conn.execute(f"SELECT b.quantity,b.capital_rub,b.cost_covered_quantity "
                f"FROM {balances} b JOIN sheet_vitrina_v1_warehouse_functional_active a "
                "ON a.version_id=b.version_id WHERE a.slot=1 AND b.warehouse_key='ff' AND b.nm_id=?",
                (int(nm_id),)).fetchone()
            # No FF balance means no positive FF position in the complete
            # active warehouse version; this is not a sparse WB API inference.
            ff_qty, ff_cap = (number(current[0]), number(current[1])) if current else (Decimal(0), Decimal(0))
            if current and (ff_qty is None or ff_cap is None or min(ff_qty, ff_cap) < 0
                    or ff_qty > 0 and ff_cap <= 0 or number(current[2]) != ff_qty):
                return {**result, "status": "pending", "reason": "current_ff_cost_boundary_incomplete"}
            result["ff_quantity"] = format(ff_qty, "f")
            result["ff_capital_rub"] = format(ff_cap, "f")
        result["proof_digest"] = digest(result)
        return result
    return result


def epoch_prices(effects, *, common):
    """Each epoch uses its immutable posting operands, never today's quantity."""
    result = []
    for effect in effects:
        snapshot = _snapshot(effect)
        boundary = snapshot.get("order_boundary") or {}
        if common:
            before = number(boundary.get("operands", {}).get("our_wb_unit_cost_rub"))
            quantity = number(boundary.get("common_quantity"))
            included = set(boundary.get("included_effect_ids", []))
            # Several invoices can be admitted before another Ready is saved.
            # Only adjustments absent from that saved boundary are carried in.
            carry = sum((number(e["amount_rub"]) for e in effects[:len(result)]
                if e["effect_id"] not in included), Decimal(0))
            before = before + carry / quantity if before is not None and quantity and quantity > 0 else None
            ff_quantity, ff_capital = number(boundary.get("ff_quantity")), number(boundary.get("ff_capital_rub"))
            wb_quantity, wb_capital = number(snapshot.get("wb_quantity")), number(snapshot.get("after_capital_rub"))
            if None not in (ff_quantity, ff_capital, wb_quantity, wb_capital) and ff_quantity + wb_quantity > 0:
                after = (ff_capital + wb_capital) / (ff_quantity + wb_quantity)
            else:
                after = before + number(effect["amount_rub"]) / quantity if before is not None else None
        else:
            before, after = number(snapshot.get("before_wac_rub")), number(snapshot.get("after_wac_rub"))
        if before is None or after is None or min(before, after) <= 0:
            raise ValueError("forward_cost_epoch_basis_missing")
        result.append(dict(effect=effect, before=before, after=after))
    return result


def resolve_epoch_cost(base, *, effects, nm_id, operation_date, operation=None, common=False):
    selected = effects_for_day(effects, nm_id, operation_date.isoformat())
    if not selected or base.get("status") != "resolved":
        return base
    epochs = epoch_prices(selected, common=common)
    timestamp = operation_timestamp(operation, operation_date.isoformat())
    current = next((e for e in reversed(epochs)
        if timestamp and _moment(timestamp) >= _moment(_posted_at(e["effect"]))), None)
    price = current["after"] if current else epochs[0]["before"]
    result = {**base, "unit_cost_rub": format(price, "f"),
        "valuation_time_precision": "exact_timestamp" if timestamp else "date_only_frozen",
        "valuation_time_reason": "" if timestamp else "Время продажи неизвестно; сохранена предварительная стоимость до позднего расхода.",
        "forward_cost_boundary": selected[0]["effect_id"]}
    if current:
        result.update(cost_effective_at=_posted_at(current["effect"]),
            forward_cost_epoch=current["effect"]["effect_id"])
    # Money is immutable; refreshing unrelated inventory cannot change the
    # epoch's identity, while ordinary pre-epoch economic signatures stay put.
    result["source_digest"] = digest(dict(base=base.get("source_digest"),
        boundary=selected[0]["effect_id"], epoch=result.get("forward_cost_epoch", ""), price=str(price)))
    return result


def epoch_cache_key(effects, *, nm_id, day, operation=None):
    selected = effects_for_day(effects, nm_id, day)
    if not selected:
        return ""
    timestamp = operation_timestamp(operation, day)
    if not timestamp:
        return "date_only_frozen"
    return next((str(e["effect_id"]) for e in reversed(selected)
        if _moment(timestamp) >= _moment(_posted_at(e))), "before_first_expense")


def order_cost_basis(effects, *, nm_id, day, count, order_sum, current_cost, version, parameters):
    """Cost previously observed orders at their pinned segment's old price."""
    selected = effects_for_day(effects, nm_id, day)
    if not selected:
        return dict(cost=current_cost)
    epochs = epoch_prices(selected, common=True)
    previous_count, cogs = Decimal(0), Decimal(0)
    for epoch in epochs:
        boundary = _snapshot(epoch["effect"])["order_boundary"]
        operands = boundary["operands"]
        observed = number(operands["orderCount"])
        baseline_sum = number(operands["orderSum"])
        if (count is None or order_sum is None or observed < previous_count or count < observed
                or order_sum < baseline_sum or count == observed and order_sum != baseline_sum
                or boundary.get("parameter_versions", {}).get(str(version)) not in (None, getattr(parameters, "version_id", None))):
            return dict(cost=None, reason="История заказов или параметры изменились после расхода; прибыль требует уточнения.")
        cogs += (observed - previous_count) * epoch["before"]
        previous_count = observed
    if current_cost is None and count > previous_count:
        return dict(cost=None, reason="Текущая себестоимость недоступна.")
    if count > previous_count:
        cogs += (count - previous_count) * current_cost
    return dict(cost=cogs / count if count > 0 else current_cost,
        precision_pending=count > previous_count,
        evidence=dict(contract=CONTRACT, effect_ids=[e["effect_id"] for e in selected],
            frozen_order_count=str(previous_count), current_order_count=str(count),
            weighted_order_cost_rub=str(cogs), time_precision="published_order_count_segments",
            precision_pending=count > previous_count))
