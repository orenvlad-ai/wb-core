"""Verify an observed closed-day SKU spend without changing the accepted snapshot."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import BytesIO
import json
import math
import re

from packages.application.ads_snapshot_payload import resolve_ads_snapshot_payload

TABLE = "ads_sku_daily_spend_evidence"
ROLE = "accepted_closed_day_snapshot"
HEADER = ("id Товара", "id кампании", "Тип конверсии", "Затраты, RUB")
CENT = Decimal("0.01")


def digest(raw):
    return sha256(raw).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def amount(value):
    try:
        money = Decimal(str(value))
        if not money.is_finite() or money < 0 or money != money.quantize(CENT):
            raise ValueError("invalid RUB spend")
        return money.quantize(CENT)
    except InvalidOperation as exc:
        raise ValueError("invalid RUB spend") from exc


def snapshot_amount(value):
    """Decode native float aggregation without relaxing WB's exact cent amounts."""
    try:
        return amount(value)
    except ValueError:
        if (type(value) is not float or not math.isfinite(value) or value <= 0
                or math.ulp(value) >= 0.000001):
            raise
        rounded = Decimal(str(value)).quantize(CENT)
        # Only an adjacent binary float of a nonzero cent amount is eligible.
        # Bound ULP independently so large floats cannot hide fractional kopecks.
        if rounded == 0 or abs(value - float(rounded)) > math.ulp(value):
            raise ValueError("invalid native RUB spend")
        return amount(rounded)


def product_rows(raw):
    """WB can declare A1 as the used range even for a populated worksheet."""
    from openpyxl import load_workbook

    book = load_workbook(BytesIO(raw), read_only=True, data_only=False)
    try:
        if len(book.worksheets) != 1:
            raise ValueError("expected one WB product statistics sheet")
        sheet = book.active
        if not re.fullmatch(r"Статистика \d+ товаров", sheet.title):
            raise ValueError("expected WB product statistics")
        sheet.reset_dimensions()
        rows = sheet.values
        header = tuple(str(cell).strip() for cell in next(rows, ()))
        if any(header.count(name) != 1 for name in HEADER):
            raise ValueError("invalid WB product/spend columns")
        columns = [header.index(name) for name in HEADER]
        products = defaultdict(dict)
        for row in rows:
            if all(cell is None for cell in row):
                continue
            if len(row) <= max(columns):
                raise ValueError("truncated WB product row")
            nm, campaign, conversion, spend = (row[c] for c in columns)
            nm, campaign = str(nm), str(campaign)
            if (not re.fullmatch(r"[1-9][0-9]*", nm)
                    or not re.fullmatch(r"[1-9][0-9]*", campaign)
                    or conversion != "Прямая" or campaign in products[nm]):
                raise ValueError("unsupported or duplicate WB product/campaign row")
            products[nm][campaign] = str(amount(spend))
        if not products:
            raise ValueError("empty product workbook cannot establish zero")
        return dict(products)
    finally:
        book.close()


def check_receipt(receipt, raw, seller, now):
    """The trusted operator supplies capture metadata from the WB export request.

    The workbook itself contains neither the reporting window nor the cabinet.
    File names and campaign start/finish columns are not capture metadata.
    """
    day = date.fromisoformat(receipt["date_from"])
    received = datetime.fromisoformat(receipt["received_at"].replace("Z", "+00:00"))
    if (day.isoformat() != receipt["date_from"] or receipt["date_to"] != day.isoformat()
            or receipt["seller_id"] != seller or not seller
            or not isinstance(receipt["export_id"], str) or not receipt["export_id"].strip()
            or receipt["source"] != "wb_promotion_product_statistics"
            or receipt["scope"] != "all_products_all_campaigns" or receipt["currency"] != "RUB"
            or receipt["sha256"] != digest(raw) or received.utcoffset() is None
            or received > now or day >= received.astimezone(timezone(timedelta(hours=3))).date()):
        raise ValueError("day, account, export scope, capture time or workbook hash mismatch")
    return day.isoformat()


def accepted(conn, day):
    row = conn.execute(
        "SELECT payload_json FROM temporal_source_slot_snapshots WHERE source_key='ads_compact' "
        "AND snapshot_role=? AND snapshot_date=?", (ROLE, day)).fetchone()
    if not row:
        return "", {}
    raw = row[0]
    result, _ = resolve_ads_snapshot_payload(json.loads(raw))
    return digest(raw.encode()), result or {}


def observed(result, day):
    diagnostics = result.get("diagnostics") or {}
    if (result.get("kind") != "incomplete" or result.get("snapshot_date") != day
            or diagnostics.get("zero_fill_applied") is not False
            or diagnostics.get("source_date") != day
            or diagnostics.get("partial_observation_contract") != "ads_partial_observed_v1"):
        return {}
    totals = defaultdict(Decimal)
    invalid = set()
    for item in result["items"]:
        nm = str(item.get("nm_id", item.get("nmId", "")))
        try:
            totals[nm] += snapshot_amount(item.get("ads_sum"))
        except ValueError:
            invalid.add(nm)
    return {nm: total for nm, total in totals.items() if nm not in invalid}


def has_evidence(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                             (TABLE,)).fetchone())


def ensure_schema(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
        seller_id TEXT NOT NULL, day TEXT NOT NULL, snapshot_hash TEXT NOT NULL,
        nm_id TEXT NOT NULL, receipt_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
        campaign_amounts TEXT NOT NULL,
        PRIMARY KEY (seller_id, day, snapshot_hash, nm_id, receipt_hash)
    ) WITHOUT ROWID""")


def preview(conn, raw, receipt, seller, now, *, nm_id=None):
    day = check_receipt(receipt, raw, seller, now)
    products = product_rows(raw)
    snapshot, result = accepted(conn, day)
    totals = observed(result, day)
    retained = set()
    existing = defaultdict(set)
    if has_evidence(conn):
        for nm, receipt_hash, items in conn.execute(
                f"SELECT nm_id,receipt_hash,campaign_amounts FROM {TABLE} WHERE seller_id=? "
                "AND day=? AND snapshot_hash=?", (seller, day, snapshot)):
            retained.add((nm, receipt_hash, items))
            existing[nm].add(items)
    receipt_text = encoded(receipt)
    receipt_hash = digest(receipt_text.encode())
    decisions = {}
    inserts = []
    scope = products.keys() | totals.keys() if nm_id is None else {nm_id}
    for nm in sorted(scope):
        # An omission in a newer full export must invalidate earlier corroboration.
        details = encoded(products.get(nm))
        if nm not in totals:
            decisions[nm] = "no_observed_value"
            continue
        evidence = existing[nm] | {details}
        decisions[nm] = classify(totals[nm], evidence).status
        if (nm, receipt_hash, details) not in retained:
            inserts.append((seller, day, snapshot, nm, receipt_hash, receipt_text, details))
    # A retry of this exact receipt has the same precondition after its commit.
    other_documents = sorted(record for record in retained if record[1] != receipt_hash)
    state = [seller, day, snapshot, nm_id, receipt_hash, other_documents, decisions]
    return {"day": day, "preview_hash": digest(encoded(state).encode()), "decisions": decisions}, inserts


@dataclass(frozen=True)
class SpendDecision:
    status: str
    campaigns: tuple = ()
    amount: Decimal | None = None
    receipts: tuple = ()


def classify(total, evidence, receipts=()):
    if len(evidence) != 1:
        return SpendDecision("conflicting_exports" if evidence else "no_daily_proof")
    detail = json.loads(next(iter(evidence)))
    if detail is None:
        return SpendDecision("no_product_row")
    campaigns = tuple(sorted(detail.items()))
    if sum((amount(v) for _, v in campaigns), Decimal(0)) != total:
        return SpendDecision("spend_mismatch")
    return SpendDecision("verified_sku_spend", campaigns, total, tuple(sorted(receipts)))


def decisions_for_day(conn, seller, day, snapshot, result):
    if not has_evidence(conn):
        return {}
    totals = observed(result, day)
    documents = defaultdict(set)
    receipts = defaultdict(list)
    for nm, details, receipt in conn.execute(
            f"SELECT nm_id,campaign_amounts,receipt_hash FROM {TABLE} WHERE seller_id=? "
            "AND day=? AND snapshot_hash=?", (seller, day, snapshot)):
        documents[nm].add(details)
        receipts[nm].append(receipt)
    return {nm: classify(totals[nm], entries, receipts[nm]) for nm, entries in documents.items()
            if nm in totals}


def verified_days(conn, seller, days):
    """Avoid reading any snapshots on unverified days (including historical databases)."""
    if not days or not has_evidence(conn):
        return {}
    covered = conn.execute(f"SELECT DISTINCT day FROM {TABLE} WHERE seller_id=? "
                           "AND day BETWEEN ? AND ?", (seller, min(days), max(days)))
    result = {}
    for (day,) in covered:
        if day in days:
            snapshot, payload = accepted(conn, day)
            decisions = decisions_for_day(conn, seller, day, snapshot, payload)
            if decisions:
                result[day] = decisions
    return result
