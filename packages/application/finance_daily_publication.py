"""Exact, source-proven daily Finance projection for an existing dated ready row."""
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from packages.application.fin_report_daily_block import transform_legacy_payload
from packages.domain.finance_daily_report import (
    FINANCE_ENDPOINT, finite_money, project_finance_daily_report,
    validate_finance_daily_projection,
)
from packages.application.web_vitrina_management_history import digest

SKU_METRICS = ("fin_buyout_rub", "fin_delivery_rub", "fin_commission_wb_portal", "fin_acquiring_fee", "fin_loyalty_rub")
TOTAL_METRICS = tuple("total_" + key for key in SKU_METRICS) + ("fin_storage_fee_total",)
STATUS_KEY = "fin_report_daily[yesterday_closed]"


def data_sheet(plan):
    sheets = [s for s in plan["sheets"] if s["sheet_name"] == "DATA_VITRINA"]
    if len(sheets) != 1:
        raise ValueError("finance-data-sheet-topology")
    return sheets[0]


def roster(plan):
    sheet = data_sheet(plan)
    ids = []
    for row in sheet["rows"]:
        if len(row) > 1 and str(row[1]).endswith("|fin_buyout_rub") and str(row[1]).startswith("SKU:"):
            ids.append(int(row[1].split("|")[0][4:]))
    if not ids or len(ids) != len(set(ids)) or any(n <= 0 for n in ids):
        raise ValueError("finance-dated-roster-invalid")
    return sorted(ids)


def assemble(source, nm_ids):
    day = source["date"]
    if source.get("endpoint") != FINANCE_ENDPOINT or source.get("period") != "daily" or source.get("date_from") != day or source.get("date_to") != day:
        raise ValueError("finance-acquisition-scope-invalid")
    report = source["report"]
    diagnostics = {"source_date": day, "endpoint": FINANCE_ENDPOINT, "period": "daily",
        "source_digest": report["source_digest"], "source_observed_at": report["source_observed_at"],
        "pagination": {"pages": report["pages"], "rrdid_start": 0, "rrdid_end": report["rrd_id_end"],
                       "terminal_status": report["terminal_status"], "complete": report["terminal_status"] == 204}}
    projection = project_finance_daily_report(report["rows"], snapshot_date=day, nm_ids=nm_ids, source=diagnostics)
    diagnostics.update(projection.diagnostics)
    if diagnostics["finance_report"]["projection_status"] != "complete":
        raise ValueError("finance-report-unusable:" + ",".join(diagnostics["finance_report"]["reasons"]))
    result = transform_legacy_payload({"snapshot_date": day, "requested_nm_ids": nm_ids,
                                      "source": diagnostics, "data": {"rows": projection.rows}}).result
    validate_finance_daily_projection(result, expected_date=day, expected_nm_ids=nm_ids)
    return result


def project(plan, result, operation_id):
    """Derive keys from this dated target; reject any incomplete topology."""
    day = result.snapshot_date
    nm_ids = roster(plan)
    proof = validate_finance_daily_projection(result, expected_date=day, expected_nm_ids=nm_ids)
    sheet = data_sheet(plan)
    if plan.get("as_of_date") != day or sheet["header"].count(day) != 1:
        raise ValueError("finance-recovery-requires-own-closed-date-ready")
    index = sheet["header"].index(day)
    expected = {f"SKU:{item.nm_id}|{key}": round(finite_money(getattr(item, key)), 6)
                for item in result.items for key in SKU_METRICS}
    for key in SKU_METRICS:
        expected["TOTAL|total_" + key] = round(sum(finite_money(getattr(item, key)) for item in result.items), 6)
    expected["TOTAL|fin_storage_fee_total"] = round(finite_money(result.storage_total.fin_storage_fee_total), 6)
    actual = [str(row[1]) for row in sheet["rows"] if len(row) > 1 and
              ("|fin_" in str(row[1]) or "|total_fin_" in str(row[1]))]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("finance-exact-key-topology-mismatch")
    after = deepcopy(plan)
    changes = []
    for row in data_sheet(after)["rows"]:
        key = str(row[1])
        if key not in expected:
            continue
        if len(row) <= index:
            raise ValueError("finance-target-row-short")
        if row[index] not in (None, ""):
            finite_money(row[index])
            if row[index] != expected[key]:
                raise ValueError("finance-established-value-preserved")
        changes.append({"row_id": key, "date": day, "before": row[index], "after": expected[key]})
        row[index] = expected[key]
    statuses = [s for s in after["sheets"] if s["sheet_name"] == "STATUS"]
    if len(statuses) != 1:
        raise ValueError("finance-status-topology")
    status = statuses[0]
    matches = [r for r in status["rows"] if r and r[0] == STATUS_KEY]
    if len(matches) != 1:
        raise ValueError("finance-closed-status-topology")
    values = {"source_key": STATUS_KEY, "kind": "success", "freshness": day,
        "snapshot_date": day, "date": "", "date_from": "", "date_to": "",
        "requested_count": len(nm_ids), "covered_count": len(nm_ids), "missing_nm_ids": "",
        "note": (f"resolution_rule=accepted_closed_current_attempt; finance_publication={operation_id}; "
                 f"source_observed_at={proof['source_observed_at']}; source_digest={proof['source_digest']}; "
                 f"observed_activity={len(proof['observed_activity_nm_ids'])}; no_activity={len(proof['no_activity_nm_ids'])}; "
                 f"projection={len(nm_ids)}/{len(nm_ids)}; pages={proof['pagination']['pages']}; terminal_status=204")}
    for key, value in values.items():
        if status["header"].count(key) != 1 or len(matches[0]) <= status["header"].index(key):
            raise ValueError("finance-status-column-topology")
        matches[0][status["header"].index(key)] = value
    if non_target_digest(plan, day, set(expected)) != non_target_digest(after, day, set(expected)):
        raise ValueError("finance-non-target-change")
    return {"plan": after, "changes": changes, "target_keys": sorted(expected),
        "roster_nm_ids": nm_ids, "non_target_digest": non_target_digest(plan, day, set(expected)),
        "normalized_payload": asdict(result)}


def non_target_digest(plan, day, keys):
    copy = deepcopy(plan)
    sheet = data_sheet(copy); index = sheet["header"].index(day)
    for row in sheet["rows"]:
        if len(row) > 1 and row[1] in keys:
            row[index] = "<finance-target>"
    for status in copy["sheets"]:
        if status["sheet_name"] == "STATUS":
            for row in status["rows"]:
                if row and row[0] == STATUS_KEY:
                    row[:] = [STATUS_KEY, "<finance-status-target>"]
    return digest(copy)
