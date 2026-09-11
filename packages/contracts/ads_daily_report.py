"""Pure, fail-closed daily Ads validation; no fetch or publication authority.

Reconciliation is a local admission rule, not a claim that WB always supplies
complete statistics. Empty/no-statistics responses cannot establish inactivity.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
import re
from typing import Any, Mapping, Sequence

from packages.contracts.source_attempt_diagnostics import source_digest

FIELDS = ("views", "clicks", "atbs", "orders", "sum", "sum_price")
COUNTS = frozenset(FIELDS[:4])
CONTRACT = "ads_daily_report_v1"


class AdsReportError(ValueError):
    """A payload-free reason usable by the existing source diagnostics."""


@dataclass(frozen=True)
class AdsDatedRoster:
    """Caller-qualified, retained whole-account roster for this exact date.

The producer must qualify the referenced evidence (including deleted campaigns)
before supplying it. A current promotion/count response is not that evidence.
This type binds evidence; it does not authenticate arbitrary caller assertions.
No status/changeTime, omission or no-statistics exclusion is admitted here.
"""

    snapshot_date: str
    account_ref: str
    campaign_ids: tuple[int, ...]
    evidence_ref: str
    evidence_digest: str


def _fail(code: str) -> None:
    raise AdsReportError(code)


def _ids(values: Sequence[int], code: str) -> set[int]:
    if not isinstance(values, (list, tuple)) or any(type(i) is not int or i <= 0 for i in values):
        _fail(code)
    if len(set(values)) != len(values):
        _fail(code)
    return set(values)


def _date(value: Any) -> str:
    if not isinstance(value, str):
        _fail("ads_catalog_day_invalid")
    try:
        if len(value) == 10:
            parsed = date.fromisoformat(value)
        else:
            # fullstats labels a reporting day, not an observation timestamp.
            # Validate the whole label; never accept an arbitrary date prefix.
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if "T" not in value or stamp.tzinfo is None or stamp.time().replace(tzinfo=None).isoformat() != "00:00:00":
                _fail("ads_catalog_day_invalid")
            parsed = stamp.date()
        if value[:10] != parsed.isoformat():
            _fail("ads_catalog_day_invalid")
        return parsed.isoformat()
    except (ValueError, TypeError):
        _fail("ads_catalog_day_invalid")


def validate_ads_request(*, snapshot_date: str, nm_ids: Sequence[int], campaign_ids: Sequence[int]) -> None:
    if _date(snapshot_date) != snapshot_date:
        _fail("ads_catalog_day_invalid")
    _ids(nm_ids, "ads_catalog_scope_invalid")
    _ids(campaign_ids, "ads_catalog_campaign_identity_invalid")


def _metrics(row: Mapping[str, Any], level: str) -> dict[str, Decimal]:
    result = {}
    for field in FIELDS:
        value = row.get(field)
        code = "ads_catalog_sku_spend_missing" if level == "sku" and field == "sum" else "ads_catalog_metric_invalid"
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid or (field in COUNTS and value != int(value)):
            _fail(code)
        result[field] = Decimal(str(value))
    return result


def _sum(rows: Sequence[Mapping[str, Decimal]]) -> dict[str, Decimal]:
    return {field: sum((row[field] for row in rows), Decimal(0)) for field in FIELDS}


def _reconcile(parent: Mapping[str, Decimal], children: Sequence[Mapping[str, Decimal]]) -> None:
    totals = _sum(children)
    for field in FIELDS:
        # Retain the existing two-kopeck spend tolerance; counts reconcile
        # exactly. A zero/nonzero contradiction never fits a rounding tolerance.
        tolerance = Decimal(0) if field in COUNTS else Decimal("0.02")
        if (parent[field] == 0) != (totals[field] == 0) or abs(parent[field] - totals[field]) > tolerance:
            _fail("ads_catalog_unattributed_spend" if field == "sum" else "ads_catalog_metric_totals_mismatch")


def validate_ads_campaign_batch(payload: Any, *, campaign_ids: Sequence[int], snapshot_date: str) -> dict[str, Any]:
    """Validate every campaign/day/platform/SKU, including non-target products.

    Duplicate platform IDs within a day and duplicate SKU IDs within a platform
    fail. The same SKU in different platforms/campaigns is additive and valid.
    Rates are derived downstream from these six validated additive metrics.
    """
    expected = _ids(campaign_ids, "ads_catalog_campaign_identity_invalid")
    if _date(snapshot_date) != snapshot_date:
        _fail("ads_catalog_day_invalid")
    if not isinstance(payload, list) or len(payload) != len(expected):
        _fail("ads_catalog_statistics_incomplete")
    if any(not isinstance(item, Mapping) for item in payload):
        _fail("ads_catalog_statistics_invalid")
    actual = [item.get("advertId") for item in payload]
    if _ids(actual, "ads_catalog_campaign_identity_invalid") != expected:
        _fail("ads_catalog_statistics_incomplete")
    rows = []
    outcomes = []
    for advert in payload:
        days = advert.get("days")
        if not isinstance(days, list):
            _fail("ads_catalog_statistics_invalid")
        if not days:
            if type(advert.get("sum")) in (int, float) and advert["sum"] > 0:
                _fail("ads_catalog_campaign_positive_sum_without_days")
            _fail("ads_catalog_no_statistics_unconfirmed")
        seen_days = set()
        day_metrics = []
        for day in days:
            if not isinstance(day, Mapping) or _date(day.get("date")) != snapshot_date:
                _fail("ads_catalog_day_invalid")
            if snapshot_date in seen_days:
                _fail("ads_catalog_duplicate_day")
            seen_days.add(snapshot_date)
            apps = day.get("apps")
            if not isinstance(apps, list):
                _fail("ads_catalog_day_invalid")
            platforms = set()
            app_metrics = []
            for app in apps:
                if not isinstance(app, Mapping) or not isinstance(app.get("nms"), list):
                    _fail("ads_catalog_sku_breakdown_missing")
                app_id = app.get("appType")
                if type(app_id) is not int or app_id not in (1, 32, 64):
                    _fail("ads_catalog_platform_identity_invalid")
                if app_id in platforms:
                    _fail("ads_catalog_duplicate_platform")
                platforms.add(app_id)
                nms = set()
                nm_metrics = []
                for item in app["nms"]:
                    if not isinstance(item, Mapping) or type(item.get("nmId")) is not int or item["nmId"] <= 0:
                        _fail("ads_catalog_sku_identity_invalid")
                    if item["nmId"] in nms:
                        _fail("ads_catalog_duplicate_sku")
                    nms.add(item["nmId"])
                    metrics = _metrics(item, "sku")
                    nm_metrics.append(metrics)
                    rows.append({"advertId": advert["advertId"], "appType": app_id, "nmId": item["nmId"],
                                 **{f"ads_{k}": float(v) for k, v in metrics.items()}})
                metrics = _metrics(app, "app")
                _reconcile(metrics, nm_metrics)
                app_metrics.append(metrics)
            metrics = _metrics(day, "day")
            _reconcile(metrics, app_metrics)
            day_metrics.append(metrics)
        metrics = _metrics(advert, "campaign")
        _reconcile(metrics, day_metrics)
        outcomes.append({"advertId": advert["advertId"],
                         "state": "no_activity_proven" if not any(metrics.values()) else "observed",
                         "basis": "explicit_exact_day_reconciled_metrics"})
    return {"contract": CONTRACT, "snapshot_date": snapshot_date, "rows": rows,
            "campaign_outcomes": outcomes, "source_digest": source_digest(payload)}


def validate_dated_roster(roster: AdsDatedRoster | None, *, campaign_ids: Sequence[int], snapshot_date: str) -> dict[str, Any]:
    if not isinstance(roster, AdsDatedRoster):
        _fail("ads_catalog_dated_roster_unqualified")
    if roster.snapshot_date != snapshot_date or _date(roster.snapshot_date) != snapshot_date:
        _fail("ads_catalog_dated_roster_mismatch")
    if _ids(roster.campaign_ids, "ads_catalog_dated_roster_mismatch") != _ids(campaign_ids, "ads_catalog_campaign_identity_invalid"):
        _fail("ads_catalog_dated_roster_mismatch")
    if any(not isinstance(v, str) or not v.strip() for v in (roster.account_ref, roster.evidence_ref)) or not isinstance(roster.evidence_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", roster.evidence_digest):
        _fail("ads_catalog_dated_roster_unqualified")
    return {"snapshot_date": snapshot_date, "account_ref": roster.account_ref,
            "campaign_ids": list(roster.campaign_ids), "evidence_ref": roster.evidence_ref,
            "evidence_digest": roster.evidence_digest, "state": "caller_qualified_dated_roster"}


def project_ads_daily_report(payload: Any, *, snapshot_date: str, nm_ids: Sequence[int], roster: AdsDatedRoster | None) -> dict[str, Any]:
    """Whole dated-scope candidate for offline recovery preparation only.

    The qualified roster must enumerate all campaigns that could contribute.
    There is no provider-sentinel or status-based exclusion implementation.
    """
    wanted = _ids(nm_ids, "ads_catalog_scope_invalid")
    binding = validate_dated_roster(roster, campaign_ids=roster.campaign_ids if isinstance(roster, AdsDatedRoster) else (), snapshot_date=snapshot_date)
    validated = validate_ads_campaign_batch(payload, campaign_ids=binding["campaign_ids"], snapshot_date=snapshot_date)
    agg = {nm: {f"ads_{field}": Decimal(0) for field in FIELDS} for nm in wanted}
    for row in validated["rows"]:
        if row["nmId"] in wanted:
            for field in FIELDS:
                agg[row["nmId"]][f"ads_{field}"] += Decimal(str(row[f"ads_{field}"]))
    rows = [{"snapshot_date": snapshot_date, "nmId": nm, **{k: float(v) for k, v in values.items()}}
            for nm, values in sorted(agg.items())]
    if any(not math.isfinite(v) for row in rows for k, v in row.items() if k.startswith("ads_")):
        _fail("ads_catalog_aggregate_nonfinite")
    return {"contract": CONTRACT, "snapshot_date": snapshot_date, "requested_nm_ids": list(nm_ids),
            "roster": binding, "source_digest": validated["source_digest"],
            "campaign_outcomes": validated["campaign_outcomes"], "data": {"rows": rows}}
