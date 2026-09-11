#!/usr/bin/env python3
"""Offline F01-F05: actual Finance adapter/application and reusable pure proof."""

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.fin_report_daily_finance_transport_smoke import Clock, _client, _row
from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.adapters.wb_finance_api import FinanceApiError, FinanceHttpResult, WbFinanceApiClient, _rows_digest
from packages.application.fin_report_daily_block import FinReportDailyBlock, transform_legacy_payload
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
from packages.contracts.source_attempt_diagnostics import SourceAttemptError
from packages.domain.finance_daily_report import (
    CONTRACT_VERSION, FIN_FIELDS, SOURCE_FIELDS, SOURCE_FACT_FIELDS, FinanceReportBasis,
    finance_daily_source_digest, project_finance_daily_report, validate_finance_daily_projection,
)

DAY = "2026-09-08"
ROSTER = list(range(1001, 1093))


def row(rrd_id, nm_id, **changes):
    return {**_row(rrd_id, nm_id), "rrDate": DAY, **changes}


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [namespace(item) for item in value]
    return value


class Fixtures:
    def __init__(self, root):
        self.root = root
        self.number = 0
        self.requests = []

    def run(self, rows, *, roster=ROSTER, basis=None, responses=None, max_pages=200,
            deadline_seconds=240, request_seconds=0, snapshot_date=DAY):
        self.number += 1
        pages = list(responses) if responses is not None else (
            [FinanceHttpResult(200, rows, {}), FinanceHttpResult(204, [], {})]
            if rows else [FinanceHttpResult(204, [], {})])
        clock = Clock()
        def request(payload):
            self.requests.append(dict(payload))
            clock.sleep(request_seconds)
            response = pages.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        client = _client(self.root / str(self.number), clock, request, deadline_seconds=deadline_seconds)
        client.max_pages = max_pages
        block = FinReportDailyBlock(HttpBackedFinReportDailySource(client=client, report_basis=basis))
        result = block.execute(FinReportDailyRequest("fin_report_daily", snapshot_date, list(roster))).result
        validate_finance_daily_projection(result, expected_date=snapshot_date, expected_nm_ids=roster)
        return result

    def rejected(self, rows, reason, **kwargs):
        try:
            self.run(rows, **kwargs)
        except SourceAttemptError as exc:
            assert exc.code == "finance_daily_report_unusable", exc.code
            report = exc.diagnostics["finance_report"]
            assert report["projection_status"] == "unknown" and report["no_activity_nm_ids"] == []
            assert reason in report["reasons"], report
            json.dumps(exc.diagnostics, allow_nan=False)
            return exc.diagnostics
        raise AssertionError(f"unusable report accepted: {reason}")


def check_projection(fixtures):
    rows = [row(i + 1, nm_id) for i, nm_id in enumerate(ROSTER[:33])]
    result = fixtures.run(rows)
    d = result.diagnostics
    proof = d["finance_report"]
    assert proof["contract_version"] == CONTRACT_VERSION and result.count == 92
    assert d["source_row_count"] == d["exact_date_row_count"] == d["target_row_count"] == 33
    assert d["covered_count"] == 33 and d["missing_nm_ids"] == ROSTER[33:]
    assert proof["observed_activity_nm_ids"] == ROSTER[:33]
    assert proof["no_activity_nm_ids"] == ROSTER[33:] and proof["projection_nm_ids"] == ROSTER
    assert proof["source_digest"] == _rows_digest(rows)
    assert proof["source_observed_at"] == d["source_observed_at"]
    by_nm = {item.nm_id: item for item in result.items}
    for nm_id in ROSTER[33:]:
        assert all(getattr(by_nm[nm_id], field) == 0 for field in FIN_FIELDS)
    assert sum(item.fin_buyout_rub for item in result.items) == 3300
    assert result.storage_total.fin_storage_fee_total == 66
    encoded = asdict(result)
    assert validate_finance_daily_projection(namespace(encoded), expected_date=DAY, expected_nm_ids=ROSTER) == proof
    assert validate_finance_daily_projection(json.loads(json.dumps(encoded)), expected_date=DAY, expected_nm_ids=ROSTER) == proof
    assert len(json.dumps(d).encode()) < 20_000
    for expected_date, expected_roster in (("2026-09-09", ROSTER), (DAY, ROSTER[:33])):
        try:
            validate_finance_daily_projection(encoded, expected_date=expected_date, expected_nm_ids=expected_roster)
        except ValueError:
            pass
        else:
            raise AssertionError("proof admitted for another date or a reduced roster")
    for change in ("money", "missing", "duplicate", "date", "provenance", "storage", "source", "bool_identity",
                   "observation", "pagination", "covered", "missing_activity", "nonfinite"):
        damaged = deepcopy(encoded)
        if change == "money":
            damaged["items"][0]["fin_buyout_rub"] += 1
        elif change == "missing":
            del damaged["items"][0]["fin_acquiring_fee"]
        elif change == "duplicate":
            damaged["items"].append(deepcopy(damaged["items"][0]))
        elif change == "date":
            damaged["snapshot_date"] = "2026-09-09"
        elif change == "provenance":
            damaged["diagnostics"]["finance_report"]["no_activity_nm_ids"] = []
        elif change == "storage":
            damaged["storage_total"]["fin_storage_fee_total"] += 1
        elif change == "source":
            damaged["diagnostics"]["source_digest"] = "sha256:" + "0" * 64
        elif change == "bool_identity":
            damaged["items"][0]["nm_id"] = True
        elif change == "observation":
            damaged["diagnostics"]["source_observed_at"] = "2026-09-10T12:00:00Z"
        elif change == "pagination":
            damaged["diagnostics"]["pagination"]["complete"] = False
        elif change == "covered":
            damaged["diagnostics"]["covered_count"] = 92
        elif change == "missing_activity":
            damaged["diagnostics"]["missing_nm_ids"] = []
        else:
            damaged["items"][0]["fin_buyout_rub"] = float("nan")
        try:
            validate_finance_daily_projection(damaged, expected_date=DAY, expected_nm_ids=ROSTER)
        except ValueError:
            pass
        else:
            raise AssertionError(f"altered projection accepted: {change}")
    # Legacy accepted33 remains sparse; no fabricated new-version proof/zeros.
    legacy_rows = [{"snapshot_date": DAY, "nmId": item.nm_id,
                    **{key: getattr(item, key) for key in FIN_FIELDS}} for item in result.items[:33]]
    legacy = transform_legacy_payload({"snapshot_date": DAY, "data": {"rows": legacy_rows}}).result
    assert legacy.count == 33 and "finance_report" not in legacy.diagnostics
    assert legacy.diagnostics["covered_count"] is None and legacy.diagnostics["source_row_count"] is None
    return rows, result


def check_pure_unknown(rows, result):
    assert finance_daily_source_digest(rows) == _rows_digest(rows)
    for change, reason in (("partial", "transport_incomplete"), ("observation", "source_identity_unconfirmed"),
                           ("date", "source_identity_unconfirmed"), ("digest", "source_identity_unconfirmed")):
        source = deepcopy(result.diagnostics)
        if change == "partial":
            source["pagination"]["complete"] = False
        elif change == "observation":
            source["source_observed_at"] = "2026-09-10T12:00:00"  # no timezone evidence
        elif change == "date":
            source["source_date"] = "2026-09-09"
        else:
            source["source_digest"] = None
        projected = project_finance_daily_report(rows, snapshot_date=DAY, nm_ids=ROSTER, source=source)
        assert projected.rows == []
        assert projected.diagnostics["covered_count"] == 33
        assert projected.diagnostics["finance_report"]["no_activity_nm_ids"] == []
        assert reason in projected.diagnostics["finance_report"]["reasons"]
    changed_rows = deepcopy(rows)
    changed_rows[0]["paidStorage"] = "99"
    projected = project_finance_daily_report(changed_rows, snapshot_date=DAY, nm_ids=ROSTER, source=result.diagnostics)
    assert projected.rows == []
    assert "source_identity_unconfirmed" in projected.diagnostics["finance_report"]["reasons"]


def check_review_regressions(rows, result):
    """R1/R2: public saved packet admission, with no new acquisition fixture."""
    def digest(value):
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()

    def reseal(packet):
        diagnostics = packet["diagnostics"]
        report = diagnostics["finance_report"]
        report["source_facts_digest"] = digest({key: diagnostics[key] for key in SOURCE_FACT_FIELDS})
        report["contract_digest"] = digest({key: value for key, value in report.items() if key != "contract_digest"})

    def rejected(packet, case):
        try:
            validate_finance_daily_projection(packet, expected_date=DAY, expected_nm_ids=ROSTER)
        except ValueError:
            return
        raise AssertionError(f"contradictory saved packet accepted: {case}")

    encoded = asdict(result)
    corruptions = {
        "source_row_count": 1, "exact_date_row_count": 999,
        "invalid_required_fields": {"acquiringFee": 1}, "counter_basis": "legacy_unknown",
        "date_basis_counts": {"rrDate": 0, "saleDt": 33, "dateFrom": 0, "missing": 0},
        "seller_level_row_count": 33, "anomaly_codes": ["invalid_identity"],
        "nonfinite_aggregate": True, "non_target_row_count": 1, "date_discard_count": 1,
        "qualified_seller_storage_rrd_ids": [1], "invalid_rrd_id_row_count": 1,
        "last_source_rrd_id": 1,
        "date_distributions": {"rrDate": {DAY: 1}, "saleDt": {}, "dateFrom": {}},
        "selected_date_distributions": {"rrDate": {"2026-09-09": 33}, "saleDt": {}, "dateFrom": {}},
    }
    for field, value in corruptions.items():
        for recompute in (False, True):
            damaged = deepcopy(encoded)
            damaged["diagnostics"][field] = value
            if recompute:
                reseal(damaged)
            rejected(damaged, (field, recompute))
    # Every explicitly bound source fact rejects independent storage corruption.
    for field in SOURCE_FACT_FIELDS:
        damaged = deepcopy(encoded)
        damaged["diagnostics"][field] = None
        rejected(damaged, ("missing source fact", field))

    fallback_rows = [row(1, ROSTER[0], rrDate=None, saleDt=DAY)]
    source = {**result.diagnostics, "source_digest": finance_daily_source_digest(fallback_rows),
              "pagination": {"pages": 1, "rrdid_start": 0, "rrdid_end": 1, "terminal_status": 204, "complete": True}}
    basis = FinanceReportBasis(source["source_digest"], "fixture:reviewed-saved-source", allowed_date_fallbacks=("saleDt",))
    projected = project_finance_daily_report(fallback_rows, snapshot_date=DAY, nm_ids=ROSTER, source=source, basis=basis)
    fallback = asdict(transform_legacy_payload({"snapshot_date": DAY, "requested_nm_ids": ROSTER,
        "source": {**source, **projected.diagnostics}, "data": {"rows": projected.rows}}).result)
    validate_finance_daily_projection(fallback, expected_date=DAY, expected_nm_ids=ROSTER)
    for field, value in (
        ("source_digest", "sha256:" + "0" * 64), ("evidence_reference", " "),
        ("allowed_date_fallbacks", []), ("allowed_date_fallbacks", ["rrDate"]),
        ("allowed_date_fallbacks", ["saleDt", "saleDt"]),
        ("seller_storage_rrd_ids", [0]), ("seller_storage_rrd_ids", [2, 2]),
        ("seller_storage_rrd_ids", [2]),
    ):
        damaged = deepcopy(fallback)
        damaged["diagnostics"]["finance_report"]["basis"][field] = value
        reseal(damaged)
        rejected(damaged, ("basis", field, value))
    for field, value in (("basis", None), ("basis_supplied", False)):
        damaged = deepcopy(fallback)
        damaged["diagnostics"]["finance_report"][field] = value
        reseal(damaged)
        rejected(damaged, field)

    for field, value in (("rrdid_end", 1), ("pages", 1000)):
        damaged_source = deepcopy(result.diagnostics)
        damaged_source["pagination"][field] = value
        projected = project_finance_daily_report(rows, snapshot_date=DAY, nm_ids=ROSTER, source=damaged_source)
        assert projected.rows == [] and projected.diagnostics["finance_report"]["no_activity_nm_ids"] == []
        assert "pagination_source_mismatch" in projected.diagnostics["finance_report"]["reasons"]
        try:
            transform_legacy_payload({"snapshot_date": DAY, "requested_nm_ids": ROSTER,
                "source": {**damaged_source, **projected.diagnostics}, "data": {"rows": projected.rows}})
        except ValueError:
            pass
        else:
            raise AssertionError(f"incompatible pagination normalized: {field}")
        damaged_packet = deepcopy(encoded)
        damaged_packet["diagnostics"]["pagination"][field] = value
        damaged_packet["diagnostics"]["finance_report"]["pagination"][field] = value
        reseal(damaged_packet)
        rejected(damaged_packet, ("normalized pagination", field))
    empty_source = {**source, "source_digest": finance_daily_source_digest([]),
                    "pagination": {"pages": 0, "rrdid_start": 0, "rrdid_end": 0, "terminal_status": 204, "complete": True}}
    empty = project_finance_daily_report([], snapshot_date=DAY, nm_ids=ROSTER, source=empty_source)
    assert empty.rows == [] and empty.diagnostics["finance_report"]["transport_complete"] is True
    assert "empty_unconfirmed" in empty.diagnostics["finance_report"]["reasons"]


def check_storage(fixtures):
    non_target = fixtures.run([row(1, 999, paidStorage="9")])
    assert non_target.storage_total.fin_storage_fee_total == 9
    assert all(item.fin_buyout_rub == 0 for item in non_target.items)
    assert non_target.diagnostics["covered_count"] == 0
    assert non_target.diagnostics["finance_report"]["no_activity_nm_ids"] == ROSTER
    storage = {"rrdId": 2, "rrDate": DAY, "nmId": 0, "paidStorage": "5",
               "sellerOperName": "fixture-only reviewed seller storage"}
    rows = [row(1, ROSTER[0]), storage]
    unknown = fixtures.rejected(rows, "invalid_identity")
    assert unknown["invalid_identity_row_count"] == unknown["unclassified_identity_row_count"] == 1
    # Qualification is exact-report/exact-row evidence, never an invented type allowlist.
    basis = FinanceReportBasis(_rows_digest(rows), "fixture:exact-saved-report-review", seller_storage_rrd_ids=(2,))
    accepted = fixtures.run(rows, basis=basis)
    assert accepted.storage_total.fin_storage_fee_total == 7
    assert accepted.items[0].fin_storage_fee == 2
    assert accepted.diagnostics["seller_level_row_count"] == 1
    assert accepted.diagnostics["invalid_identity_row_count"] == 1  # I1 counter preserved
    assert accepted.diagnostics["unclassified_identity_row_count"] == 0
    assert all(item.nm_id > 0 for item in accepted.items)
    fixtures.rejected(rows, "finance_report_basis_invalid", basis=replace(basis, source_digest="sha256:" + "0" * 64))
    fixtures.rejected(rows, "seller_basis_row_mismatch", basis=replace(basis, seller_storage_rrd_ids=(3,)))
    extra_charge = [row(1, ROSTER[0]), {**storage, "deliveryService": "7"}]
    fixtures.rejected(extra_charge, "invalid_required_fields", basis=replace(basis, source_digest=_rows_digest(extra_charge)))
    seller_only_basis = replace(basis, source_digest=_rows_digest([storage]))
    seller_only = fixtures.rejected([storage], "sku_activity_unproven", basis=seller_only_basis)
    assert seller_only["finance_report"]["report_usable"] is True


def check_activity_and_dates(fixtures):
    result = fixtures.run([row(1, ROSTER[0]), row(2, ROSTER[0], docTypeName="Возврат", sellerOperName="Возврат"),
                           row(3, ROSTER[1], docTypeName="", sellerOperName="Удержание", retailPriceWithDisc="0")])
    assert result.items[0].fin_buyout_rub == 0
    assert result.items[0].fin_acquiring_fee == 6
    proof = result.diagnostics["finance_report"]
    assert proof["observed_activity_nm_ids"] == ROSTER[:2]
    assert proof["activity_row_counts"] == {str(ROSTER[0]): 2, str(ROSTER[1]): 1}
    returned = fixtures.run([row(1, ROSTER[0], docTypeName="Возврат", sellerOperName="Возврат")])
    assert returned.items[0].fin_buyout_rub == -100
    fixtures.rejected([], "empty_unconfirmed")
    fixtures.rejected([row(1, ROSTER[0], rrDate="2026-09-07")], "date_basis_mismatch_or_unavailable")
    fixtures.rejected([row(1, ROSTER[0], rrDate=None)], "missing_date")
    fixtures.rejected([row(1, ROSTER[0], rrDate="2026-02-30", saleDt=DAY)], "invalid_date")
    for fallback_field in ("saleDt", "dateFrom"):
        rows = [row(1, ROSTER[0], rrDate=None, **{fallback_field: DAY})]
        rejected = fixtures.rejected(rows, "unqualified_date_fallback")
        assert rejected["date_fallback_count"] == rejected["exact_date_row_count"] == 1
        basis = FinanceReportBasis(_rows_digest(rows), "fixture:verified-date-basis", allowed_date_fallbacks=(fallback_field,))
        qualified = fixtures.run(rows, basis=basis)
        assert qualified.items[0].fin_buyout_rub == 100
        assert qualified.diagnostics["date_basis_counts"][fallback_field] == 1
    # rrDate wins; the other provider dates are observed, not converted into the report date.
    dated = fixtures.run([row(1, ROSTER[0], rrDate=DAY + "T23:00:00-03:00", saleDt="2026-09-01", dateFrom="2026-09-07"),
                          row(2, 999, rrDate="2026-09-07")])
    assert dated.items[0].fin_buyout_rub == 100 and dated.storage_total.fin_storage_fee_total == 2
    assert dated.diagnostics["date_discard_count"] == 1 and dated.diagnostics["date_fallback_count"] == 0


def check_invalid_data(fixtures):
    for identity in (None, 0, -1, True, float(ROSTER[0]), "broken", "1.5"):
        fixtures.rejected([row(1, identity)], "invalid_identity")
    for identity in (None, 0, True, "bad"):
        fixtures.rejected([row(identity, ROSTER[0]), row(2, ROSTER[1])], "invalid_rrd_identity")
    fixtures.rejected([row(1, ROSTER[0]), row(1, ROSTER[1])], "duplicate_rrd_identity")
    for field in SOURCE_FIELDS:
        for bad in (None, "", True, float("nan"), float("inf"), "-Infinity", "not-money"):
            reason = "missing_required_fields" if bad in (None, "") else "invalid_required_fields"
            d = fixtures.rejected([row(1, ROSTER[0], **{field: bad})], reason)
            assert d["covered_count"] == 1 and d["pagination"]["complete"] is True
            assert d[reason][field] == 1
    missing = row(1, ROSTER[0]); del missing["acquiringFee"]
    fixtures.rejected([missing], "missing_required_fields")
    fixtures.rejected([row(1, 999, acquiringFee=None)], "missing_required_fields")
    fixtures.rejected([row(1, ROSTER[0], retailPriceWithDisc="1e308", commissionPercent="1e308")], "nonfinite_aggregate")
    fixtures.rejected([row(1, ROSTER[0], paidStorage="1e308"), row(2, ROSTER[0], paidStorage="1e308")], "nonfinite_aggregate")
    before = len(fixtures.requests)
    for roster in ([1, 1], [True], [0], []):
        try:
            fixtures.run([row(1, 1)], roster=roster)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid roster reached source")
    for day in ("", "2026-02-30", "08.09.2026", DAY + "T00:00:00Z"):
        try:
            fixtures.run([row(1, 1)], snapshot_date=day)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid request date reached source")
    assert len(fixtures.requests) == before


def check_partial_transport(fixtures):
    cases = [
        ("partial_report", [FinanceHttpResult(200, [], {})], 0, 0, {}),
        ("rate_limited", [FinanceHttpResult(200, [row(1, ROSTER[0])], {}), FinanceHttpResult(429, [], {"Retry-After": "75"})], 1, 1, {}),
        ("transport_error", [FinanceHttpResult(200, [row(1, ROSTER[0])], {}), TimeoutError("private source error")], 1, 1, {}),
        ("stuck_cursor", [FinanceHttpResult(200, [row(1, ROSTER[0])], {}), FinanceHttpResult(200, [row(1, ROSTER[1])], {})], 2, 2, {}),
        ("invalid_row", [FinanceHttpResult(200, [row(1, ROSTER[0]), None], {})], 1, 2, {}),
        ("max_pages", [FinanceHttpResult(200, [row(1, ROSTER[0])], {})], 1, 1, {"max_pages": 1}),
        ("deadline", [FinanceHttpResult(200, [row(1, ROSTER[0])], {})], 1, 1,
         {"deadline_seconds": 1, "request_seconds": 2}),
    ]
    for code, responses, pages, count, options in cases:
        try:
            fixtures.run([], responses=responses, **options)
        except FinanceApiError as exc:
            assert exc.code == code and exc.pages == pages
            assert exc.diagnostics["source_row_count"] == count
            assert exc.diagnostics["finance_report"]["projection_status"] == "unknown"
            assert exc.diagnostics["finance_report"]["no_activity_nm_ids"] == []
            assert exc.diagnostics["pagination"]["complete"] is False
            assert "private source error" not in json.dumps(exc.diagnostics)
        else:
            raise AssertionError(f"partial source accepted: {code}")


def check_http_decoder(root):
    class Response:
        headers = {}
        def __init__(self, status, rows): self.status, self.rows = status, rows
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self): return json.dumps(self.rows).encode() if self.status == 200 else b""
    for period in ("daily", "weekly"):
        clock = Clock()
        responses = [Response(200, [row(1, ROSTER[0]), None]), Response(204, [])]
        client = WbFinanceApiClient("fixture-token", rate_gate_root=root / period,
            wall_time=clock.time, monotonic=clock.time, sleep=clock.sleep)
        with patch("urllib.request.urlopen", side_effect=lambda *_args, **_kwargs: responses.pop(0)):
            if period == "daily":
                try:
                    client.fetch_report(date_from=DAY, date_to=DAY, period=period)
                except FinanceApiError as exc:
                    assert exc.code == "invalid_row" and len(exc._observed_rows) == 2
                else:
                    raise AssertionError("daily HTTP decoder silently dropped a row")
            else:
                result = client.fetch_report(date_from=DAY, date_to=DAY, period=period)
                assert len(result.rows) == 1 and result.terminal_status == 204  # weekly compatibility


def main():
    with TemporaryDirectory(prefix="finance-report-contract-") as tmp, patch(
        "urllib.request.urlopen", side_effect=AssertionError("network forbidden")
    ), patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
        root = Path(tmp)
        fixtures = Fixtures(root / "reports")
        rows, result = check_projection(fixtures)
        check_pure_unknown(rows, result)
        check_review_regressions(rows, result)
        check_storage(fixtures)
        check_activity_and_dates(fixtures)
        check_invalid_data(fixtures)
        check_partial_transport(fixtures)
        check_http_decoder(root / "decoder")
        print(f"finance_daily_report_contract: OK; {fixtures.number} offline adapter cases; F01-F05, legacy, R1/R2 packet regressions")


if __name__ == "__main__":
    main()
