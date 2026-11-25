"""Exact allowed degradations of the small, dated expense scenario."""

from collections import Counter
from datetime import date, timedelta
import json
import re


class UnexpectedCycleWarning(ValueError):
    def __init__(self, stage, missing, unexpected):
        self.stage, self.missing, self.unexpected = stage, missing, unexpected
        super().__init__(f"{stage}: missing={missing}, unexpected={unexpected}")


def expected_warnings(day, number, *, missing_ads):
    from packages.application.sheet_vitrina_v1_cycle import STAGES

    expected = {stage: [] for stage in STAGES}
    for offset, slot in ((1, "yesterday_closed"), (0, "today_current")):
        stamp = (day - timedelta(days=offset)).isoformat()
        for source, kind, policy, digest in (
            ("onec_stocks", "success", "archive_only", True),
            ("ads_compact", "incomplete", "accepted_partial", True),
            ("stocks", "not_available", "temporal_role_unavailable", False),
        ):
            if source == "stocks" and offset:
                continue
            expected["api_sources"].append(
                dict(
                    source_key=source,
                    temporal_slot=slot,
                    date=stamp,
                    kind=kind,
                    latest_attempt_kind=(
                        "error" if missing_ads and source == "ads_compact" and offset == 1 else kind
                    ),
                    policy=policy,
                    accepted_digest=digest,
                )
            )
    for offset in range(1, 15):
        stamp = (day - timedelta(days=offset)).isoformat()
        if offset <= 2:
            if number == 1:
                provisional = dict(
                    source_key="canonical_finance_daily",
                    date=stamp,
                    status="loaded_preliminary",
                    projection_status="loaded_preliminary",
                    policy="accepted_provisional",
                    accepted=True,
                    batch_id=True,
                    content_hash=True,
                )
                expected["finance_sources"].append(dict(provisional, attempted=True))
                expected["daily_projection"].append(dict(provisional, attempted=False))
        else:
            expected["daily_projection"].append(
                dict(
                    source_key="canonical_finance_daily",
                    date=stamp,
                    policy="official_missing",
                    status="missing",
                    accepted=False,
                )
            )
    expected["finance_sources"].append(
        dict(source_key="canonical_finance_weekly", policy="loaded_preliminary" if number == 1 else "waiting")
    )
    expected["final_ready"].append(
        dict(source_key="final_ready", policy="truthful_report_degraded", semantic_status="warning")
    )
    return expected


def signature(warning):
    item = dict(warning)
    for field in ("batch_id", "content_hash", "accepted_digest"):
        if field in item:
            value = item[field]
            if value == "":
                item[field] = False
            elif isinstance(value, str) and re.fullmatch(
                ("sha256:" if field == "accepted_digest" else "") + "[0-9a-f]{64}", value
            ):
                item[field] = True
            else:
                raise ValueError(f"Malformed {field} in {item.get('source_key')} warning")
    return json.dumps(item, sort_keys=True)


def check_warnings(receipt, number, *, before_stage=None, missing_ads=False):
    expected = expected_warnings(
        date.fromisoformat(receipt["business_date"]), number, missing_ads=missing_ads
    )
    stages = receipt["stages"]
    if [s["stage"] for s in stages] != list(expected):
        raise ValueError("Wrong stage sequence")
    for stage in stages:
        name = stage["stage"]
        if name == before_stage:
            return
        observed = Counter(signature(w) for w in stage["warnings"])
        allowed = Counter(json.dumps(w, sort_keys=True) for w in expected[name])
        if observed != allowed:
            raise UnexpectedCycleWarning(
                name, list((allowed - observed).elements()), list((observed - allowed).elements())
            )
        if stage["status"] != ("degraded" if allowed else "complete") or not stage["versions"]:
            raise ValueError(f"{name}: wrong terminal status or absent versions")
    if receipt["status"] != "degraded":
        raise ValueError("Overall status does not match expected stage warnings")
