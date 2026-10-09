"""Optional reference observations, built only while publishing a monitor snapshot.

No market age cutoff inherits the stock policy. A failed/missing refresh retains
known seller/promo/bid values as stale with their original source timestamp.
Buyer observations never carry over an unavailable or changed auth context.
"""
from __future__ import annotations

from contextlib import closing
import json
import math
import sqlite3

from packages.application.wb_buyer_authenticated_observations import load_daily_projection

FIELDS = {"seller_price": "prices_snapshot", "buyer_wallet_price": "wb_buyer_authenticated",
          "promo_participation": "promo_by_price", "cpm_bid": "ads_placement_index", "cpc_bid": "ads_placement_index"}


def _number(value, *, zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return value if value >= 0 and (zero or value > 0) else None


def _cell(field, value=None, captured_at=None, *, status="missing", warning="", **extra):
    return {"value": value, "captured_at": captured_at, "status": status,
            "source": FIELDS[field], "warning": warning, **extra}


def _retain(field, previous, warning):
    if previous and previous.get("value") is not None:
        return {**previous, "status": "stale", "warning": warning}
    return _cell(field, warning=warning)


def _temporal(runtime, source, today):
    """Read accepted source slots, not provisional/closed current-price copies.

    Prices exist only in accepted_current_snapshot slots. Promo also has its
    existing daily cache. Lookups use source/date indexes, decode at most one
    accepted payload per storage source, and never walk per-SKU history.
    """
    role = "accepted_current_snapshot"
    candidates, latest = [], []
    with closing(sqlite3.connect(runtime.db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = []
        if "temporal_source_slot_snapshots" in tables:
            sources.append(("temporal_source_slot_snapshots", " AND snapshot_role=?", (role,)))
        if source == "promo_by_price" and "temporal_source_snapshots" in tables:
            sources.append(("temporal_source_snapshots", "", ()))
        for table, role_filter, role_args in sources:
            args = (source, today, *role_args)
            condition = "WHERE source_key=? AND snapshot_date<=?" + role_filter
            observed = conn.execute(f"SELECT snapshot_date,captured_at FROM {table} {condition} ORDER BY snapshot_date DESC LIMIT 1", args).fetchone()
            accepted = conn.execute(f"SELECT snapshot_date,captured_at,payload_json FROM {table} {condition} AND json_extract(payload_json,'$.kind')='success' ORDER BY snapshot_date DESC LIMIT 1", args).fetchone()
            if observed:
                latest.append(observed)
            if accepted:
                candidates.append(accepted)
        if not candidates:
            return {}, None, True
        row = max(candidates, key=lambda r: r[:2])
        stale = row[0] < today or max(latest) != row[:2]
        if "temporal_source_closure_state" in tables:
            state = conn.execute("SELECT state,last_reason,last_attempt_at FROM temporal_source_closure_state WHERE source_key=? AND target_date<=? AND slot_kind='today_current' ORDER BY target_date DESC LIMIT 1", (source, today)).fetchone()
            if state and (state[2] or "") >= row[1]:
                stale = stale or state[0] != "success" or state[1] == "accepted_snapshot_preserved_after_invalid_attempt"
    payload = json.loads(row[2])
    items = {item["nm_id"]: item for item in payload.get("items", [])
             if isinstance(item, dict) and type(item.get("nm_id")) is int}
    return items, row[1], stale


def _campaign_membership(index, campaigns, captured_at):
    """Keep unknown active bids omitted by AdsBlock's valid-bid reverse index."""
    from packages.application.sheet_vitrina_v1_ads import SUPPORTED_PLACEMENTS, _optional_int
    result = {nm: list(rows) for nm, rows in index.items()}
    for campaign in campaigns:
        if campaign.get("status") != 9 or campaign.get("payment_type") not in ("cpm", "cpc"):
            continue
        placements = [p for p, enabled in campaign.get("placements", {}).items()
                      if enabled and p in SUPPORTED_PLACEMENTS] or ["unknown"]
        for item in campaign.get("nm_settings", []):
            nm = _optional_int(item.get("nm_id"))
            if nm is None:
                continue
            rows = result.setdefault(nm, [])
            for placement in placements:
                if any(r.get("advert_id") == campaign.get("advert_id") and r.get("placement") == placement for r in rows):
                    continue
                rows.append({"advert_id": campaign.get("advert_id"), "payment_type": campaign.get("payment_type"),
                             "status": 9, "placement": placement, "current_bid_rub": None,
                             "campaign_fetched_at": captured_at})
    return result


def build_market(runtime, *, nm_ids, today, previous, ads_loader):
    """Failures are confined to one optional source, never the stock producer."""
    result = {nm: {field: _cell(field) for field in FIELDS} for nm in nm_ids}
    for field, payload_field in (("seller_price", "price_seller_discounted"), ("promo_participation", "promo_participation")):
        try:
            items, captured_at, stale = _temporal(runtime, FIELDS[field], today)
            for nm in nm_ids:
                value = _number(items.get(nm, {}).get(payload_field), zero=field == "promo_participation")
                if value is None:
                    result[nm][field] = _retain(field, previous.get(nm, {}).get(field), "Новое справочное значение недоступно.")
                else:
                    result[nm][field] = _cell(field, value, captured_at, status="stale" if stale else "ready",
                        warning="Показано последнее подтверждённое значение; обновление источника не подтверждено." if stale else "")
        except Exception:
            for nm in nm_ids:
                result[nm][field] = _retain(field, previous.get(nm, {}).get(field), "Источник справочных данных не удалось прочитать.")

    # Reuse the existing account-context certification; never read anonymous SPP.
    try:
        projection = load_daily_projection(runtime, today, list(nm_ids))
        diagnostics = projection.get("diagnostics", {})
        context = diagnostics.get("current_auth_run_reference")
        attempts = diagnostics.get("latest_attempt_measured_at_by_nm_id", {})
        for item in projection.get("items", []):
            nm = item.get("nm_id")
            if nm not in result:
                continue
            value = _number(item.get("buyer_wallet_price_rub"))
            measured = item.get("measured_at")
            if value is None or not context or item.get("auth_run_reference") != context:
                continue
            stale = str(attempts.get(str(nm), "")) > str(measured or "")
            result[nm]["buyer_wallet_price"] = _cell("buyer_wallet_price", value, measured,
                status="stale" if stale else "ready", warning="Последнее измерение не удалось; показана цена из того же покупательского аккаунта." if stale else "")
    except Exception:
        pass
    for nm in nm_ids:
        if result[nm]["buyer_wallet_price"]["value"] is None:
            result[nm]["buyer_wallet_price"]["warning"] = "Нет подтверждённой цены кошелька в текущем покупательском аккаунте."

    try:
        read = ads_loader()  # One whole-catalog batch; never one call per SKU.
        index = read["index"]
        if not isinstance(index, dict):
            raise ValueError("ads_index_invalid")
        if "campaigns" in read:
            index = _campaign_membership(index, read["campaigns"], read.get("captured_at"))
        for nm in nm_ids:
            rows = index.get(nm, [])
            for payment in ("cpm", "cpc"):
                field = payment + "_bid"
                active = [r for r in rows if r.get("status") == 9 and r.get("payment_type") == payment]
                captured = max((str(r.get("campaign_fetched_at") or "") for r in active), default="") or read.get("captured_at")
                details = [{"advert_id": r.get("advert_id"), "placement": r.get("placement"), "bid": _number(r.get("current_bid_rub"))} for r in active]
                campaigns = {r.get("advert_id") for r in active}
                bids = {d["bid"] for d in details}
                if not active:
                    result[nm][field] = _cell(field, captured_at=captured, warning="Нет подтверждённой ставки активной кампании.")
                elif len(campaigns) != 1 or None in campaigns or len(bids) != 1 or None in bids:
                    result[nm][field] = _cell(field, captured_at=captured, status="ambiguous", warning="Несколько активных кампаний или разные ставки по местам размещения.", details=details)
                else:
                    result[nm][field] = _cell(field, next(iter(bids)), captured, status="ready", details=details)
    except Exception:
        for nm in nm_ids:
            for field in ("cpm_bid", "cpc_bid"):
                result[nm][field] = _retain(field, previous.get(nm, {}).get(field), "Ставки не обновились; показано последнее известное значение.")
    return result
