"""Pure, source-bound daily Finance usability and additive SKU projection.

No IO, clock, storage, retry or publication. Unknown reports never supply zeros.
The optional basis qualifies an exact saved report; it is not a provider-wide
permission to treat missing identities or sale dates as report operations.
"""

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
import hashlib
import json
import math
import re
from typing import Any, Mapping, Sequence

from packages.contracts.source_attempt_diagnostics import source_digest as finance_daily_source_digest


CONTRACT_VERSION = "finance_daily_report_v1"
FINANCE_ENDPOINT = "POST /api/finance/v1/sales-reports/detailed"
FIN_FIELDS = (
    "fin_delivery_rub", "fin_storage_fee", "fin_deduction", "fin_commission",
    "fin_penalty", "fin_additional_payment", "fin_buyout_rub",
    "fin_commission_wb_portal", "fin_acquiring_fee", "fin_loyalty_rub",
)
SOURCE_FIELDS = (
    "retailPriceWithDisc", "commissionPercent", "deliveryService", "paidStorage",
    "deduction", "ppvzSalesCommission", "penalty", "additionalPayment",
    "acquiringFee", "cashbackAmount",
)
ADDITIVE_FIELDS = dict(zip(
    ("deliveryService", "paidStorage", "deduction", "ppvzSalesCommission",
     "penalty", "additionalPayment", "acquiringFee", "cashbackAmount"),
    ("fin_delivery_rub", "fin_storage_fee", "fin_deduction", "fin_commission",
     "fin_penalty", "fin_additional_payment", "fin_acquiring_fee", "fin_loyalty_rub"),
    strict=True,
))
SOURCE_COUNT_FIELDS = (
    "source_row_count", "exact_date_row_count", "target_row_count", "non_target_row_count",
    "invalid_identity_row_count", "invalid_row_count", "date_fallback_count", "date_discard_count",
    "missing_date_row_count", "invalid_date_row_count", "unqualified_fallback_row_count",
    "invalid_rrd_id_row_count", "duplicate_rrd_id_row_count", "seller_level_row_count",
    "unclassified_identity_row_count", "covered_count",
)
SOURCE_FACT_FIELDS = SOURCE_COUNT_FIELDS + (
    "endpoint", "period", "source_date", "source_digest", "source_observed_at", "pagination",
    "seller_operation_groups", "counter_basis", "date_basis_counts", "date_distributions", "selected_date_distributions",
    "missing_required_fields", "invalid_required_fields", "anomaly_codes", "covered_nm_ids",
    "missing_nm_ids", "last_source_rrd_id", "qualified_seller_storage_rrd_ids", "nonfinite_aggregate",
)


# Provider-labelled seller operations with explicit nmId=0. These amounts do
# not belong to a SKU. Preserve their counters and money in the source proof;
# only paidStorage is part of the existing public seller-wide TOTAL.
SELLER_OPERATION_FIELDS = {
    "Возмещение за выдачу и возврат товаров на ПВЗ": frozenset(),
    "Удержание": frozenset({"deduction"}),
}
SELLER_ZERO_FIELDS = ("quantity", "retailAmount", "forPay", "retailPrice")
SELLER_PRODUCT_FIELDS = ("vendorCode", "sku", "title", "brandName", "subjectName", "techSize")
SELLER_MONEY_FIELDS = SOURCE_FIELDS + ("ppvzReward", "vw", "vwNds") + SELLER_ZERO_FIELDS


def _seller_operation(row: Mapping[str, Any]) -> tuple[str, dict[str, float]] | None:
    name = row.get("sellerOperName")
    if (type(row.get("nmId")) is not int or row["nmId"] != 0) and row.get("nmId") != "0":
        return None
    if name not in SELLER_OPERATION_FIELDS or _positive_int(row.get("rrdId")) is None:
        return None
    try:
        money = {key: finite_money(row.get(key)) for key in SELLER_MONEY_FIELDS}
    except ValueError:
        return None
    if (any(row.get(key) != "" for key in SELLER_PRODUCT_FIELDS)
        or any(money[key] != 0 for key in SELLER_ZERO_FIELDS)):
        return None
    if any(money[key] != 0 for key in SOURCE_FIELDS if key not in SELLER_OPERATION_FIELDS[name]):
        return None
    if name == "Удержание" and any(money[key] != 0 for key in ("ppvzReward", "vw", "vwNds")):
        return None
    return str(name), money


def _validate_seller_groups(groups: Any) -> list[int]:
    if not isinstance(groups, dict) or any(name not in SELLER_OPERATION_FIELDS for name in groups):
        raise ValueError("finance_seller_operations_invalid")
    ids = []
    for name, group in groups.items():
        if not isinstance(group, dict) or set(group) != {"rrd_ids", "money"}:
            raise ValueError("finance_seller_operations_invalid")
        keys, money = group["rrd_ids"], group["money"]
        if (not isinstance(keys, list) or not keys or any(type(n) is not int or n <= 0 for n in keys)
            or keys != sorted(set(keys)) or not isinstance(money, dict) or set(money) != set(SELLER_MONEY_FIELDS)):
            raise ValueError("finance_seller_operations_invalid")
        values = {key: finite_money(value) for key, value in money.items()}
        if (any(values[key] != 0 for key in SELLER_ZERO_FIELDS)
            or any(values[key] != 0 for key in SOURCE_FIELDS if key not in SELLER_OPERATION_FIELDS[name])
            or name == "Удержание" and any(values[key] != 0 for key in ("ppvzReward", "vw", "vwNds"))):
            raise ValueError("finance_seller_operations_money_invalid")
        ids.extend(keys)
    if len(ids) != len(set(ids)):
        raise ValueError("finance_seller_operations_duplicate")
    return sorted(ids)


@dataclass(frozen=True)
class FinanceReportBasis:
    """Reviewed evidence for exceptions in this exact report, disabled by default."""

    source_digest: str
    evidence_reference: str
    allowed_date_fallbacks: tuple[str, ...] = ()
    seller_storage_rrd_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class FinanceDailyProjection:
    rows: list[dict[str, Any]]
    diagnostics: dict[str, Any]


def _digest(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("finance_report_evidence_invalid") from exc
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _positive_int(value: Any) -> int | None:
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _date_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw).isoformat()
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", raw):
            return datetime.strptime(raw, "%d.%m.%Y").date().isoformat()
        if re.match(r"\d{4}-\d{2}-\d{2}T", raw):
            # Preserve the provider's date component, without a timezone shift.
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    return ""


def _observed_at(value: Any) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return observed.tzinfo is not None and observed.utcoffset() is not None
    except ValueError:
        return False


def _transport_complete(pagination: Any, *, source_row_count: int, last_source_rrd_id: int | None) -> bool:
    return (
        isinstance(pagination, Mapping) and pagination.get("complete") is True
        and type(pagination.get("terminal_status")) is int and pagination["terminal_status"] == 204
        and type(source_row_count) is int and source_row_count >= 0
        and type(pagination.get("pages")) is int
        and int(bool(source_row_count)) <= pagination["pages"] <= source_row_count
        and type(pagination.get("rrdid_end")) is int
        and (pagination["rrdid_end"] == 0 if source_row_count == 0 else
             type(last_source_rrd_id) is int and last_source_rrd_id > 0
             and pagination["rrdid_end"] == last_source_rrd_id)
        and type(pagination.get("rrdid_start")) is int and pagination["rrdid_start"] == 0
    )


def _basis_packet_valid(basis: Any, source_digest: Any) -> bool:
    if not isinstance(basis, Mapping) or set(basis) != {
        "source_digest", "evidence_reference", "allowed_date_fallbacks", "seller_storage_rrd_ids",
    }:
        return False
    fallbacks, sellers = basis["allowed_date_fallbacks"], basis["seller_storage_rrd_ids"]
    return (
        _sha(basis["source_digest"]) and basis["source_digest"] == source_digest
        and isinstance(basis["evidence_reference"], str) and bool(basis["evidence_reference"].strip())
        and isinstance(fallbacks, (tuple, list)) and isinstance(sellers, (tuple, list))
        and all(value in ("saleDt", "dateFrom") for value in fallbacks)
        and len(set(fallbacks)) == len(fallbacks)
        and all(type(value) is int and value > 0 for value in sellers)
        and len(set(sellers)) == len(sellers)
    )


def _source_facts_digest(evidence: Mapping[str, Any]) -> str:
    if any(field not in evidence for field in SOURCE_FACT_FIELDS):
        raise ValueError("finance_source_facts_missing")
    return _digest({field: evidence[field] for field in SOURCE_FACT_FIELDS})


def _validate_usable_source_facts(evidence: Mapping[str, Any], report: Mapping[str, Any], day: str) -> None:
    """Check internal source-packet relations, including after checksum recomputation."""
    if (report.get("source_facts_version") != "finance_daily_source_facts_v1"
        or report.get("source_facts_digest") != _source_facts_digest(evidence)
        or evidence["counter_basis"] != "observed_source_rows"
        or any(type(evidence[key]) is not int or evidence[key] < 0 for key in SOURCE_COUNT_FIELDS)):
        raise ValueError("finance_source_facts_invalid")
    source_count, exact_count = evidence["source_row_count"], evidence["exact_date_row_count"]
    target, non_target = evidence["target_row_count"], evidence["non_target_row_count"]
    covered, seller = evidence["covered_count"], evidence["seller_level_row_count"]
    for field in ("covered_nm_ids", "missing_nm_ids", "qualified_seller_storage_rrd_ids"):
        keys = evidence[field]
        if (not isinstance(keys, list) or any(type(value) is not int or value <= 0 for value in keys)
            or keys != sorted(set(keys))):
            raise ValueError("finance_source_identity_facts_invalid")
    if (not 0 < exact_count <= source_count or covered > target
        or exact_count + evidence["date_discard_count"] != source_count
        or target + non_target + seller != exact_count
        or evidence["invalid_identity_row_count"] != seller
        or evidence["nonfinite_aggregate"] is not False
        or evidence["missing_required_fields"] != {} or evidence["invalid_required_fields"] != {}
        or any(evidence[key] != 0 for key in (
            "invalid_row_count", "missing_date_row_count", "invalid_date_row_count",
            "unqualified_fallback_row_count", "unclassified_identity_row_count",
            "invalid_rrd_id_row_count", "duplicate_rrd_id_row_count",
        ))
        or type(report.get("seller_sku_count")) is not int
        or not max(1, covered + int(non_target > 0)) <= report["seller_sku_count"] <= covered + non_target
        or not _transport_complete(evidence["pagination"], source_row_count=source_count,
                                   last_source_rrd_id=evidence["last_source_rrd_id"])):
        raise ValueError("finance_source_facts_inconsistent")
    date_counts = evidence["date_basis_counts"]
    date_fields = ("rrDate", "saleDt", "dateFrom")
    if (not isinstance(date_counts, dict) or set(date_counts) != {*date_fields, "missing"}
        or any(type(value) is not int or value < 0 for value in date_counts.values())
        or sum(date_counts.values()) != source_count or date_counts["missing"] != 0
        or date_counts["saleDt"] + date_counts["dateFrom"] != evidence["date_fallback_count"]):
        raise ValueError("finance_source_date_facts_invalid")
    distributions, selected = evidence["date_distributions"], evidence["selected_date_distributions"]
    for groups in (distributions, selected):
        if not isinstance(groups, dict) or set(groups) != set(date_fields):
            raise ValueError("finance_source_date_facts_invalid")
        for values in groups.values():
            if (not isinstance(values, dict)
                or any(not isinstance(key, str) or not key or _date_value(key) != key
                       or type(value) is not int or value <= 0 for key, value in values.items())
                or sum(values.values()) > source_count):
                raise ValueError("finance_source_date_facts_invalid")
    if (any(sum(selected[key].values()) != date_counts[key] for key in date_fields)
        or sum(values.get(day, 0) for values in selected.values()) != exact_count
        or distributions["rrDate"] != selected["rrDate"]
        or any(value > distributions[key].get(date_value, 0)
               for key in date_fields for date_value, value in selected[key].items())):
        raise ValueError("finance_source_date_facts_inconsistent")
    basis = report.get("basis")
    if ("basis" not in report or type(report.get("basis_supplied")) is not bool
        or report["basis_supplied"] != (basis is not None)
        or basis is not None and not _basis_packet_valid(basis, report["source_digest"])):
        raise ValueError("finance_projection_basis_invalid")
    allowed = basis["allowed_date_fallbacks"] if basis is not None else []
    seller_ids = sorted(basis["seller_storage_rrd_ids"]) if basis is not None else []
    operation_ids = _validate_seller_groups(evidence["seller_operation_groups"])
    if (any(date_counts[key] and key not in allowed for key in ("saleDt", "dateFrom"))
        or evidence["qualified_seller_storage_rrd_ids"] != seller_ids
        or set(operation_ids) & set(seller_ids) or seller != len(seller_ids) + len(operation_ids)):
        raise ValueError("finance_projection_basis_inconsistent")
    expected_anomalies = []
    if evidence["date_fallback_count"]:
        expected_anomalies.append("date_fallback_used")
    if evidence["date_discard_count"]:
        expected_anomalies.append("date_rows_discarded")
    if evidence["anomaly_codes"] != expected_anomalies:
        raise ValueError("finance_source_anomalies_inconsistent")


def finite_money(value: Any) -> float:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError("finance_money_invalid")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("finance_money_invalid") from exc
    if not math.isfinite(result):
        raise ValueError("finance_money_nonfinite")
    return result


def _roster(nm_ids: Sequence[int]) -> list[int]:
    if not nm_ids or any(type(value) is not int or value <= 0 for value in nm_ids):
        raise ValueError("finance_roster_invalid")
    if len(nm_ids) != len(set(nm_ids)):
        raise ValueError("finance_roster_duplicate")
    return sorted(nm_ids)


def validate_finance_daily_request(snapshot_date: str, nm_ids: Sequence[int]) -> None:
    _roster(nm_ids)
    if not snapshot_date or _date_value(snapshot_date) != snapshot_date:
        raise ValueError("finance_target_date_invalid")


def project_finance_daily_report(
    rows: Sequence[Any], *, snapshot_date: str, nm_ids: Sequence[int],
    source: Mapping[str, Any], basis: FinanceReportBasis | None = None,
) -> FinanceDailyProjection:
    """Inspect every observed row; project zeros only after all three proofs.

    Rows are emitted only for a complete projection; persistence is caller-owned.
    Partial transport callers still receive safe observed-row diagnostics.
    """
    roster = _roster(nm_ids)
    if not snapshot_date or _date_value(snapshot_date) != snapshot_date:
        raise ValueError("finance_target_date_invalid")
    basis_valid = basis is None or (isinstance(basis, FinanceReportBasis)
        and isinstance(basis.allowed_date_fallbacks, tuple) and isinstance(basis.seller_storage_rrd_ids, tuple)
        and _basis_packet_valid(asdict(basis), source.get("source_digest")))
    supplied_basis = basis
    if not basis_valid:
        basis = None
    wanted = set(roster)
    covered: set[int] = set()
    seller_skus: set[int] = set()
    items: dict[int, dict[str, float]] = {}
    activity_counts: dict[str, int] = {}
    total_storage = 0.0
    evidence: dict[str, Any] = {
        "counter_basis": "observed_source_rows", "source_row_count": len(rows),
        "exact_date_row_count": 0, "target_row_count": 0, "non_target_row_count": 0,
        "invalid_identity_row_count": 0, "invalid_row_count": 0,
        "date_fallback_count": 0, "date_discard_count": 0, "missing_date_row_count": 0,
        "date_basis_counts": {"rrDate": 0, "saleDt": 0, "dateFrom": 0, "missing": 0},
        "date_distributions": {key: {} for key in ("rrDate", "saleDt", "dateFrom")},
        "selected_date_distributions": {key: {} for key in ("rrDate", "saleDt", "dateFrom")},
        "invalid_date_row_count": 0, "unqualified_fallback_row_count": 0,
        "invalid_rrd_id_row_count": 0, "duplicate_rrd_id_row_count": 0,
        "seller_level_row_count": 0, "unclassified_identity_row_count": 0,
        "seller_operation_groups": {},
        "missing_required_fields": {}, "invalid_required_fields": {}, "anomaly_codes": [],
    }
    invalid_aggregate = False
    qualified_seller_rows: set[int] = set()
    seen_rrd_ids: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            evidence["invalid_row_count"] += 1
            continue
        rrd_id = _positive_int(row.get("rrdId"))
        if rrd_id is None:
            evidence["invalid_rrd_id_row_count"] += 1
        elif rrd_id in seen_rrd_ids:
            evidence["duplicate_rrd_id_row_count"] += 1
        else:
            seen_rrd_ids.add(rrd_id)
        parsed = {key: _date_value(row.get(key)) for key in ("rrDate", "saleDt", "dateFrom")}
        for key, day in parsed.items():
            if day:
                distribution = evidence["date_distributions"][key]
                distribution[day] = distribution.get(day, 0) + 1
        invalid_date = any(row.get(key) not in (None, "") and not parsed[key] for key in parsed)
        evidence["invalid_date_row_count"] += int(invalid_date)
        date_basis = next((key for key, day in parsed.items() if day), "missing")
        evidence["date_basis_counts"][date_basis] += 1
        if date_basis != "missing":
            distribution = evidence["selected_date_distributions"][date_basis]
            selected_date = parsed[date_basis]
            distribution[selected_date] = distribution.get(selected_date, 0) + 1
        if date_basis in ("saleDt", "dateFrom"):
            evidence["date_fallback_count"] += 1
            if basis is None or date_basis not in basis.allowed_date_fallbacks:
                evidence["unqualified_fallback_row_count"] += 1
        if date_basis == "missing":
            evidence["missing_date_row_count"] += 1
        if parsed.get(date_basis) != snapshot_date:
            evidence["date_discard_count"] += 1
            continue
        evidence["exact_date_row_count"] += 1
        nm_id = _positive_int(row.get("nmId"))
        seller_operation = _seller_operation(row) if nm_id is None else None
        seller_level = (
            basis is not None and rrd_id in basis.seller_storage_rrd_ids
            and (row.get("nmId") is None or row.get("nmId") == ""
                 or type(row.get("nmId")) is int and row.get("nmId") == 0
                 or row.get("nmId") == "0")
        )
        if nm_id is None:
            # Keep I1's observed non-positive-identity count; classification is separate.
            evidence["invalid_identity_row_count"] += 1
            evidence["seller_level_row_count" if seller_level or seller_operation else "unclassified_identity_row_count"] += 1
            if seller_level:
                qualified_seller_rows.add(rrd_id)
            elif seller_operation:
                name, seller_money = seller_operation
                group = evidence["seller_operation_groups"].setdefault(name, {
                    "rrd_ids": [], "money": {key: 0.0 for key in SELLER_MONEY_FIELDS}})
                group["rrd_ids"].append(rrd_id)
                for key, value in seller_money.items():
                    group["money"][key] += value
                invalid_aggregate |= any(not math.isfinite(v) for v in group["money"].values())
        else:
            seller_skus.add(nm_id)
            if nm_id in wanted:
                evidence["target_row_count"] += 1
                covered.add(nm_id)
                activity_counts[str(nm_id)] = activity_counts.get(str(nm_id), 0) + 1
            else:
                evidence["non_target_row_count"] += 1
        money: dict[str, float] = {}
        fields = SOURCE_FIELDS if nm_id is not None else ("paidStorage",)
        # An exact seller-storage qualification cannot conceal another SKU charge.
        if seller_level:
            fields = tuple(key for key in SOURCE_FIELDS if key == "paidStorage" or key in row)
        invalid_money = False
        for field in fields:
            try:
                money[field] = finite_money(row.get(field))
                if seller_level and field != "paidStorage" and money[field] != 0:
                    raise ValueError("finance_seller_storage_has_other_money")
            except ValueError:
                category = "missing_required_fields" if row.get(field) in (None, "") else "invalid_required_fields"
                evidence[category][field] = evidence[category].get(field, 0) + 1
                invalid_money = True
        if "paidStorage" in money:
            total_storage += money["paidStorage"]
        if nm_id not in wanted or invalid_money:
            continue
        rec = items.setdefault(nm_id, {field: 0.0 for field in FIN_FIELDS})
        for field, target_field in ADDITIVE_FIELDS.items():
            rec[target_field] += money[field]
        doc_type = str(row.get("docTypeName") or "").casefold()
        operation = str(row.get("sellerOperName") or "").casefold()
        sign = 1 if doc_type == "продажа" or operation == "продажа" else (
            -1 if "возврат" in doc_type or "возврат" in operation else 0)
        rec["fin_buyout_rub"] += sign * money["retailPriceWithDisc"]
        rec["fin_commission_wb_portal"] += sign * (money["retailPriceWithDisc"] * money["commissionPercent"] / 100.0)
        invalid_aggregate |= any(not math.isfinite(value) for value in rec.values())
    invalid_aggregate |= not math.isfinite(total_storage)
    for group in evidence["seller_operation_groups"].values():
        group["rrd_ids"].sort()
    evidence.update(covered_count=len(covered), covered_nm_ids=sorted(covered),
                    missing_nm_ids=sorted(wanted - covered), nonfinite_aggregate=bool(invalid_aggregate),
                    qualified_seller_storage_rrd_ids=sorted(qualified_seller_rows),
                    last_source_rrd_id=_positive_int(rows[-1].get("rrdId")) if rows and isinstance(rows[-1], Mapping) else None)
    pagination = source.get("pagination") or {}
    transport_complete = _transport_complete(pagination, source_row_count=len(rows),
                                            last_source_rrd_id=evidence["last_source_rrd_id"])
    source_bound = source.get("endpoint") == FINANCE_ENDPOINT and source.get("period") == "daily" and (
        source.get("source_date") == snapshot_date and _sha(source.get("source_digest"))
        # Retain I1's canonical source hash, and bind a saved-row replay to it.
        and source.get("source_digest") == finance_daily_source_digest(rows)
        and _observed_at(source.get("source_observed_at")))
    blockers = []
    for code, present in (
        ("transport_incomplete", not transport_complete),
        ("pagination_source_mismatch", isinstance(pagination, Mapping) and pagination.get("complete") is True
         and not transport_complete),
        ("source_identity_unconfirmed", not source_bound),
        ("finance_report_basis_invalid", not basis_valid),
        ("empty_unconfirmed", not rows),
        ("date_basis_mismatch_or_unavailable", rows and not evidence["exact_date_row_count"]),
        ("invalid_row", evidence["invalid_row_count"]),
        ("missing_date", evidence["missing_date_row_count"]),
        ("invalid_date", evidence["invalid_date_row_count"]),
        ("unqualified_date_fallback", evidence["unqualified_fallback_row_count"]),
        ("invalid_identity", evidence["unclassified_identity_row_count"]),
        ("invalid_rrd_identity", evidence["invalid_rrd_id_row_count"]),
        ("duplicate_rrd_identity", evidence["duplicate_rrd_id_row_count"]),
        ("missing_required_fields", evidence["missing_required_fields"]),
        ("invalid_required_fields", evidence["invalid_required_fields"]),
        ("nonfinite_aggregate", invalid_aggregate),
        ("seller_basis_row_mismatch", basis is not None and qualified_seller_rows != set(basis.seller_storage_rrd_ids)),
    ):
        if present:
            blockers.append(code)
    report_usable = not blockers
    if report_usable and not seller_skus:
        blockers.append("sku_activity_unproven")
    complete = not blockers
    no_activity = sorted(wanted - covered) if complete else []
    if complete:
        for nm_id in no_activity:
            items[nm_id] = {field: 0.0 for field in FIN_FIELDS}
    projected_rows = [dict(snapshot_date=snapshot_date, nmId=nm_id, **values) for nm_id, values in sorted(items.items())]
    projected_rows.append(dict(snapshot_date=snapshot_date, nmId=0, **{
        field: total_storage if field == "fin_storage_fee" else 0.0 for field in FIN_FIELDS}))
    evidence["anomaly_codes"] = list(blockers)
    for code, present in (("date_fallback_used", evidence["date_fallback_count"]),
                          ("date_rows_discarded", evidence["date_discard_count"])):
        if present:
            evidence["anomaly_codes"].append(code)
    report = {
        "contract_version": CONTRACT_VERSION, "snapshot_date": snapshot_date,
        "transport_complete": transport_complete, "report_usable": report_usable,
        "pagination": dict(pagination) if isinstance(pagination, Mapping) else None,
        "projection_status": "complete" if complete else "unknown",
        "reasons": blockers, "source_digest": source.get("source_digest"),
        "source_observed_at": source.get("source_observed_at"),
        "roster_nm_ids": roster, "roster_digest": _digest(roster),
        "observed_activity_nm_ids": sorted(covered), "activity_row_counts": activity_counts,
        "seller_sku_count": len(seller_skus), "seller_sku_digest": _digest(sorted(seller_skus)),
        "no_activity_nm_ids": no_activity, "projection_nm_ids": roster if complete else [],
        "no_activity_reason": "complete_report_no_matching_operation" if no_activity else None,
        "basis": asdict(basis) if basis is not None else None,
        "basis_supplied": supplied_basis is not None,
        "source_facts_version": "finance_daily_source_facts_v1",
        "source_facts_digest": _source_facts_digest({**source, **evidence}),
    }
    if complete:
        report["values_digest"] = _values_digest(snapshot_date, items, total_storage)
    report["contract_digest"] = _digest(report)
    evidence["finance_report"] = report
    return FinanceDailyProjection(projected_rows if complete else [], evidence)


def _values_digest(snapshot_date: str, items: Mapping[int, Mapping[str, float]], storage: float) -> str:
    return _digest({"snapshot_date": snapshot_date, "items": [
        {"nm_id": nm_id, **{field: finite_money(values[field]) for field in FIN_FIELDS}}
        for nm_id, values in sorted(items.items())], "fin_storage_fee_total": finite_money(storage)})


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return _plain(asdict(value))
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def validate_finance_daily_projection(
    payload: Any, *, expected_date: str, expected_nm_ids: Sequence[int],
) -> dict[str, Any]:
    """Verify a new-version normalized payload; legacy admission is caller-owned."""
    result = _plain(payload)
    if not isinstance(result, dict):
        raise ValueError("finance_projection_payload_invalid")
    result = result.get("result", result)
    if not isinstance(result, dict) or not isinstance(result.get("diagnostics"), dict):
        raise ValueError("finance_projection_payload_invalid")
    diagnostics = result["diagnostics"]
    report = diagnostics.get("finance_report", {})
    if not isinstance(report, dict):
        raise ValueError("finance_projection_contract_invalid")
    validate_finance_daily_request(expected_date, expected_nm_ids)
    roster = _roster(expected_nm_ids)
    if (report.get("contract_version") != CONTRACT_VERSION or result.get("kind") != "success"
        or result.get("snapshot_date") != expected_date or report.get("snapshot_date") != expected_date
        or report.get("projection_status") != "complete" or report.get("report_usable") is not True
        or report.get("transport_complete") is not True or report.get("reasons") != []
        or report.get("roster_nm_ids") != roster or report.get("roster_digest") != _digest(roster)
        or report.get("projection_nm_ids") != roster or not _sha(report.get("source_digest"))
        or report.get("source_digest") != diagnostics.get("source_digest")
        or report.get("source_observed_at") != diagnostics.get("source_observed_at")
        or not _observed_at(report.get("source_observed_at"))
        or report.get("pagination") != diagnostics.get("pagination")
        or type(diagnostics.get("source_row_count")) is not int or diagnostics["source_row_count"] <= 0
        or type(diagnostics.get("exact_date_row_count")) is not int or diagnostics["exact_date_row_count"] <= 0
        or type(report.get("seller_sku_count")) is not int or report["seller_sku_count"] <= 0
        or not _sha(report.get("seller_sku_digest"))
        or diagnostics.get("source_date") != expected_date or diagnostics.get("period") != "daily"
        or diagnostics.get("endpoint") != FINANCE_ENDPOINT
        or report.get("contract_digest") != _digest({key: value for key, value in report.items() if key != "contract_digest"})):
        raise ValueError("finance_projection_contract_invalid")
    _validate_usable_source_facts(diagnostics, report, expected_date)
    observed = report.get("observed_activity_nm_ids", [])
    absent = report.get("no_activity_nm_ids", [])
    if (not isinstance(observed, list) or not isinstance(absent, list)
        or any(type(value) is not int or value <= 0 for value in observed + absent)):
        raise ValueError("finance_projection_activity_invalid")
    counts = report.get("activity_row_counts", {})
    if (not isinstance(counts, dict) or set(counts) != {str(nm_id) for nm_id in observed}
        or any(type(value) is not int or value <= 0 for value in counts.values())
        or type(diagnostics.get("covered_count")) is not int or diagnostics["covered_count"] != len(observed)
        or diagnostics.get("covered_nm_ids") != observed
        or type(diagnostics.get("target_row_count")) is not int or diagnostics["target_row_count"] != sum(counts.values())
        or diagnostics.get("missing_nm_ids") != absent
        or sorted(set(observed) | set(absent)) != roster or set(observed) & set(absent)
        or len(observed) != len(set(observed)) or len(absent) != len(set(absent))
        or absent and report.get("no_activity_reason") != "complete_report_no_matching_operation"):
        raise ValueError("finance_projection_activity_invalid")
    items = result.get("items", [])
    if (not isinstance(items, list) or any(not isinstance(item, dict) or type(item.get("nm_id")) is not int for item in items)
        or sorted(item.get("nm_id") for item in items) != roster or type(result.get("count")) is not int
        or result.get("count") != len(roster)):
        raise ValueError("finance_projection_roster_invalid")
    values = {item["nm_id"]: {field: finite_money(item.get(field)) for field in FIN_FIELDS} for item in items}
    if any(values[nm_id][field] != 0.0 for nm_id in absent for field in FIN_FIELDS):
        raise ValueError("finance_no_activity_value_invalid")
    storage = result.get("storage_total", {})
    if not isinstance(storage, dict) or type(storage.get("nm_id")) is not int or storage.get("nm_id") != 0 or report.get("values_digest") != _values_digest(
        expected_date, values, storage.get("fin_storage_fee_total")):
        raise ValueError("finance_projection_values_changed")
    return report
