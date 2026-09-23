"""Synthetic Ads/Finance evidence through real adapters and acceptance wrappers; no network."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.fin_report_daily_finance_transport_smoke import Clock, _client, _row
from apps.ads_daily_report_contract_smoke import campaign as complete_campaign, roster as dated_roster
from apps.sheet_vitrina_v1_business_time_smoke import _build_live_plan
from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource
from packages.adapters.wb_finance_api import FinanceApiError, FinanceFetchResult, FinanceHttpResult, FinanceRateLimited
from packages.application.ads_compact_block import AdsCompactBlock, transform_legacy_payload as ads_transform
from packages.application.fin_report_daily_block import FinReportDailyBlock, transform_legacy_payload as finance_transform
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import (
    _append_source_slot_diagnostic, _build_refresh_source_summary, _capture_live_source,
    _is_valid_temporal_candidate, _start_source_slot_diagnostic, _source_attempt_status_note,
    TEMPORAL_ROLE_ACCEPTED_CLOSED, SOURCE_TEMPORAL_POLICIES,
)
from packages.contracts.ads_compact_block import AdsCompactRequest
from packages.contracts.fin_report_daily_block import FinReportDailyRequest
from packages.contracts.source_attempt_diagnostics import SourceAttemptError

DAY = "2026-08-27"


def ads_result(pages, ids=(5, 6), nm_ids=(101, 102), batch_size=50):
    source = HttpBackedAdsCompactSource(complete_catalog=True, max_ids_per_request=batch_size, batch_sleep_seconds=0, dated_roster=dated_roster(ids, DAY))
    responses = [{"all": len(ids), "adverts": [{"status": 9, "count": len(ids),
                 "advert_list": [{"advertId": i} for i in ids]}]}] + deepcopy(pages)
    def fake_json(**_kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    source._get_json = fake_json
    with patch("packages.adapters.ads_compact_block.load_runtime_config",
               return_value=SimpleNamespace(base_url="unused", token="fixture", timeout_seconds=1)):
        return AdsCompactBlock(source).execute(AdsCompactRequest("ads_compact", DAY, list(nm_ids))).result


def campaign(nm_id=101, advert_id=5):
    return complete_campaign(nm_id, advert_id, day=DAY)


def capture(source, loader):
    return _capture_live_source(source_key=source, temporal_slot="yesterday_closed",
        temporal_policy=SOURCE_TEMPORAL_POLICIES[source], column_date=DAY,
        requested_nm_ids=[101, 102], loader=loader)


def finance_result(path, pages, nm_ids=(101, 102)):
    responses = list(pages)
    client = _client(path, Clock(), lambda _request: responses.pop(0))
    return FinReportDailyBlock(HttpBackedFinReportDailySource(client=client)).execute(
        FinReportDailyRequest("fin_report_daily", DAY, list(nm_ids))).result


def check_ads():
    status, payload = capture("ads_compact", lambda: ads_result([[campaign()]]))
    assert payload is None and status.kind == "error"
    d = status.diagnostics
    assert d["expected_campaign_ids"] == [5, 6] and d["returned_campaign_ids"] == [5]
    assert d["missing_campaign_ids"] == [6] and d["duplicate_campaign_ids"] == []
    assert d["error_code"] == "ads_catalog_statistics_incomplete" and d["source_observed_at"]
    assert d["counter_basis"] == "observed_campaign_responses"
    assert d["batches"][0]["source_date"] == DAY and d["batches"][0]["digest"].startswith("sha256:")
    assert d["attempted_campaign_ids"] == [5, 6] and d["not_attempted_campaign_ids"] == []
    assert d["missing_from_received_batches_campaign_ids"] == [6]
    assert d["batches"][0]["response_kind"] == "array"
    # One omitted response in the first batch must not classify the untouched
    # second batch as provider omissions. Preserve the whole expected roster.
    many_ids = tuple(range(1, 60))
    first_page = [complete_campaign(advert_id=i, day=DAY) for i in range(1, 50)]
    interrupted, interrupted_payload = capture("ads_compact", lambda: ads_result([first_page], ids=many_ids))
    evidence = interrupted.diagnostics
    assert interrupted_payload is None and evidence["batch_count"] == 2
    assert len(evidence["batches"]) == 1 and len(evidence["expected_campaign_ids"]) == 59
    assert evidence["missing_campaign_ids"] == list(range(50, 60))
    assert evidence["missing_from_received_batches_campaign_ids"] == [50]
    assert evidence["not_attempted_campaign_ids"] == list(range(51, 60))
    null_status, null_payload = capture("ads_compact", lambda: ads_result([None], ids=(5,)))
    assert null_payload is None and null_status.kind == "error"
    assert null_status.diagnostics["batches"][0]["response_kind"] == "null"
    assert null_status.diagnostics["missing_from_received_batches_campaign_ids"] == [5]
    duplicate, _ = capture("ads_compact", lambda: ads_result([[campaign(), campaign()]]))
    assert duplicate.diagnostics["duplicate_campaign_ids"] == [5]
    assert duplicate.diagnostics["missing_campaign_ids"] == [6]
    partial, _ = capture("ads_compact", lambda: ads_result([[campaign()], OSError("private provider detail")], batch_size=1))
    assert partial.diagnostics["batches"][0]["returned_campaign_ids"] == [5]
    assert partial.diagnostics["batches"][1]["status"] == "error"
    assert partial.diagnostics["attempted_campaign_ids"] == [5, 6]
    assert partial.diagnostics["not_attempted_campaign_ids"] == []
    assert partial.diagnostics["missing_from_received_batches_campaign_ids"] == []
    assert partial.diagnostics["response_error_campaign_ids"] == [6]
    assert partial.diagnostics["batches"][1]["response_kind"] is None
    assert "private provider detail" not in json.dumps(asdict(partial))
    empty_status, _ = capture("ads_compact", lambda: ads_result([[{"advertId": 5, "sum": 20, "days": []}]], ids=(5,)))
    assert empty_status.diagnostics["error_code"] == "ads_catalog_campaign_positive_sum_without_days"
    assert "campaign_positive_sum_without_days" in empty_status.diagnostics["anomaly_codes"]
    missing = campaign()
    del missing["days"][0]["apps"][0]["nms"][0]["views"]
    lossy, _ = capture("ads_compact", lambda: ads_result([[missing]], ids=(5,)))
    assert lossy.kind == "error"
    assert lossy.diagnostics["batches"][0]["missing_metric_fields"] == {"views": 1}
    missing_spend = campaign()
    del missing_spend["days"][0]["apps"][0]["nms"][0]["sum"]
    rejected, _ = capture("ads_compact", lambda: ads_result([[missing_spend]], ids=(5,)))
    assert rejected.diagnostics["error_code"] == "ads_catalog_sku_spend_missing"
    assert rejected.diagnostics["batches"][0]["missing_metric_fields"] == {"sum": 1}
    huge_integer = campaign(nm_id=999)
    huge_integer["days"][0]["apps"][0]["nms"][0]["views"] = 10 ** 400
    outside_scope, _ = capture("ads_compact", lambda: ads_result([[huge_integer]], ids=(5,)))
    assert outside_scope.kind == "error"
    assert outside_scope.diagnostics["batches"][0]["invalid_metric_fields"] == {"views": 1}
    wrong_date = deepcopy(huge_integer)
    wrong_date["days"][0]["date"] = "2026-08-26"
    wrong_date_status, wrong_date_payload = capture("ads_compact", lambda: ads_result([[wrong_date]], ids=(5,)))
    assert wrong_date_payload is None
    assert wrong_date_status.diagnostics["error_code"] == "ads_catalog_day_invalid"
    assert wrong_date_status.diagnostics["batches"][0]["invalid_metric_fields"] == {"views": 1}
    for raw_id, expected_id in ((5.0, 5), (True, 1)):
        noncanonical, _ = capture("ads_compact", lambda: ads_result([[campaign(advert_id=raw_id)]], ids=(expected_id,)))
        assert noncanonical.kind == "error"
        evidence = noncanonical.diagnostics
        assert evidence["counter_basis"] == "observed_campaign_responses"
        assert evidence["returned_campaign_ids"] == [expected_id] and evidence["missing_campaign_ids"] == []
        assert evidence["batches"][0]["noncanonical_campaign_ids"] == [expected_id]
        assert "noncanonical_campaign_identity" in evidence["anomaly_codes"]
        note = _source_attempt_status_note(noncanonical)
        assert "returned_campaign_count=1" in note and "missing_campaign_count=0" in note
    summary = _source_attempt_status_note(empty_status)
    assert "campaign_positive_sum_without_days" in summary
    # Evidence scales with campaign IDs and batches, never with raw per-SKU rows.
    ids = tuple(range(1, 1001))
    pages = [[complete_campaign(advert_id=i, day=DAY, zero=True) for i in ids[start:start + 50]] for start in range(0, len(ids), 50)]
    scaled = ads_result(pages, ids=ids)
    size = len(json.dumps(scaled.diagnostics).encode())
    assert size < 100_000, size
    print(f"diagnostics_size: Ads 1000 campaigns={size} bytes")
    for malformed in ([None], [{"advertId": [], "days": []}]):
        bad_status, bad_payload = capture("ads_compact", lambda: ads_result([malformed], ids=(5,)))
        assert bad_payload is None and bad_status.kind == "error"
        assert bad_status.diagnostics["expected_campaign_ids"] == [5]
        assert bad_status.diagnostics["batches"][0]["digest"]
    return status


def check_finance(root):
    rows = [_row(10, 101), _row(20, 102), _row(30, 999)]
    rows[2]["paidStorage"] = 9
    complete = finance_result(root / "complete", [FinanceHttpResult(200, rows, {}), FinanceHttpResult(204, [], {})])
    d = complete.diagnostics
    assert (d["source_row_count"], d["exact_date_row_count"], d["target_row_count"], d["covered_count"]) == (3, 3, 2, 2)
    assert d["non_target_row_count"] == 1 and complete.storage_total.fin_storage_fee_total == 13
    empty_status, empty = capture("fin_report_daily", lambda: finance_result(root / "empty", [FinanceHttpResult(204, [], {})]))
    assert empty is None
    assert empty_status.diagnostics["source_row_count"] == 0 and "empty_unconfirmed" in empty_status.diagnostics["anomaly_codes"]
    assert not _is_valid_temporal_candidate(source_key="fin_report_daily", status=empty_status,
        payload=empty, column_date=DAY, temporal_slot="yesterday_closed")
    wrong = _row(10, 101); wrong["rrDate"] = "2026-08-26"
    absent = _row(20, 102); del absent["rrDate"]
    fallback = _row(30, 101); del fallback["rrDate"]; fallback["saleDt"] = DAY
    dates, dates_payload = capture("fin_report_daily", lambda: finance_result(root / "dates", [FinanceHttpResult(200, [wrong, absent, fallback], {}), FinanceHttpResult(204, [], {})]))
    assert dates_payload is None and "unqualified_date_fallback" in dates.diagnostics["anomaly_codes"]
    assert dates.diagnostics["date_discard_count"] == 2 and dates.diagnostics["date_fallback_count"] == 1
    assert dates.diagnostics["missing_date_row_count"] == 1 and dates.diagnostics["exact_date_row_count"] == 1
    wrong_only, wrong_payload = capture("fin_report_daily", lambda: finance_result(root / "wrong", [FinanceHttpResult(200, [wrong], {}), FinanceHttpResult(204, [], {})]))
    assert wrong_payload is None
    assert "date_basis_mismatch_or_unavailable" in wrong_only.diagnostics["anomaly_codes"]
    bad = _row(20, 102); del bad["acquiringFee"]
    mapping, _ = capture("fin_report_daily", lambda: finance_result(root / "mapping", [
        FinanceHttpResult(200, [_row(10, 101), bad], {}), FinanceHttpResult(204, [], {})]))
    assert mapping.kind == "error" and mapping.diagnostics["source_row_count"] == 2
    assert mapping.diagnostics["missing_required_fields"] == {"acquiringFee": 1}
    assert mapping.diagnostics["pagination"]["complete"] is True
    partial, payload = capture("fin_report_daily", lambda: finance_result(root / "partial", [
        FinanceHttpResult(200, [_row(10, 101)], {}), FinanceHttpResult(429, [], {"Retry-After": "75"})]))
    assert payload is None and partial.kind == "rate_limited"
    d = partial.diagnostics
    assert (d["source_row_count"], d["exact_date_row_count"], d["target_row_count"], d["covered_count"]) == (1, 1, 1, 1)
    assert d["pagination"] == {"pages": 1, "rrdid_start": 0, "rrdid_end": 10, "terminal_status": 429, "complete": False}
    assert d["missing_nm_ids"] == [102] and d["source_digest"].startswith("sha256:")
    assert d["next_retry_at"] and "rrdId" not in json.dumps(asdict(partial))
    # Failure-time defaults cannot overwrite the timestamp of the last response.
    calls = 0
    def timeout_after_page(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return FinanceHttpResult(200, [_row(10, 101)], {})
        raise TimeoutError("private transport detail")
    client = _client(root / "timeout", Clock(), timeout_after_page)
    timeout_status, _ = capture("fin_report_daily", lambda: FinReportDailyBlock(HttpBackedFinReportDailySource(client=client)).execute(
        FinReportDailyRequest("fin_report_daily", DAY, [101, 102])).result)
    assert calls == 2 and timeout_status.diagnostics["source_row_count"] == 1
    assert timeout_status.diagnostics["source_observed_at"]
    assert timeout_status.diagnostics["pagination"]["terminal_status"] is None
    assert "private transport detail" not in json.dumps(asdict(timeout_status))
    # The shared weekly caller still receives the original typed rejection.
    client = _client(root / "weekly", Clock(), lambda _request: FinanceHttpResult(429, [], {}))
    try:
        client.fetch_week(datetime(2026, 8, 27).date(), datetime(2026, 8, 27).date())
    except FinanceRateLimited as exc:
        assert isinstance(exc, FinanceApiError) and exc.period == "weekly"
        assert "_observed_rows" not in str(exc) and "rows" not in repr(exc)
    else:
        raise AssertionError("weekly rejection type changed")
    malformed_client = SimpleNamespace(fetch_report=lambda **kwargs: FinanceFetchResult(
        rows=[None], pages=1, rrd_id_end=1, terminal_status=204, source_digest="fixture-digest"))
    malformed, result = capture("fin_report_daily", lambda: FinReportDailyBlock(HttpBackedFinReportDailySource(client=malformed_client)).execute(
        FinReportDailyRequest("fin_report_daily", DAY, [101, 102])).result)
    assert result is None and malformed.diagnostics["source_row_count"] == 1
    assert malformed.diagnostics["invalid_row_count"] == 1 and malformed.diagnostics["error_code"] == "finance_daily_report_unusable"
    # Even a malformed diagnostic digest cannot replace a typed transport error.
    mixed_keys = _row(10, 101); mixed_keys[1] = "not-provider-data"
    responses = [FinanceHttpResult(200, [mixed_keys], {}), FinanceHttpResult(429, [], {})]
    client = _client(root / "malformed-digest", Clock(), lambda _request: responses.pop(0))
    try:
        client.fetch_report(date_from=DAY, date_to=DAY, period="weekly")
    except FinanceRateLimited as exc:
        assert exc.pages == 1 and exc.cursor == 10 and exc.source_digest is None
    else:
        raise AssertionError("diagnostics masked the original typed error")
    return complete, partial


def check_wrappers(root, accepted, failed):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    block = _build_live_plan(runtime)
    block.now_factory = lambda: datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
    kwargs = dict(source_key="fin_report_daily", temporal_slot="yesterday_closed",
        temporal_policy=SOURCE_TEMPORAL_POLICIES["fin_report_daily"], column_date=DAY,
        requested_nm_ids=[101, 102], execution_mode="auto_daily", accepted_role=TEMPORAL_ROLE_ACCEPTED_CLOSED,
        allow_persisted_retry=True, current_web_source_sync_note=None)
    def fail():
        raise SourceAttemptError(failed.diagnostics["error_code"], failed.diagnostics)
    status, result = block._capture_temporal_source_with_acceptance(**kwargs, loader=fail)
    assert result is None and status.kind.startswith("closure_")
    assert status.diagnostics["source_row_count"] == 1 and status.diagnostics["error_code"] == "rate_limited"
    accepted_at = "2026-08-28T01:00:00Z"
    accepted_observed_at = accepted.diagnostics["source_observed_at"]
    runtime.save_temporal_source_slot_snapshot(source_key="fin_report_daily", snapshot_date=DAY,
        snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED, captured_at=accepted_at, payload=accepted)
    before = runtime.load_temporal_source_slot_snapshot(source_key="fin_report_daily", snapshot_date=DAY,
        snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED)
    preserved, payload = block._capture_temporal_source_with_acceptance(**kwargs, loader=fail)
    after = runtime.load_temporal_source_slot_snapshot(source_key="fin_report_daily", snapshot_date=DAY,
        snapshot_role=TEMPORAL_ROLE_ACCEPTED_CLOSED)
    assert before == after and payload.items[0].fin_buyout_rub == 100
    assert preserved.kind == "success" and preserved.diagnostics["preserved_snapshot"]["accepted_at"] == accepted_at
    assert preserved.diagnostics["source_observed_at"] == accepted_observed_at
    assert preserved.diagnostics["latest_attempt"]["diagnostics"]["source_row_count"] == 1
    assert "source_row_count=1" in _source_attempt_status_note(preserved)
    assert f"accepted_at={accepted_at}" in _source_attempt_status_note(preserved)
    diag = {}
    started = _start_source_slot_diagnostic(source_key="fin_report_daily", temporal_slot="yesterday_closed", requested_date=DAY, started_at="2026-08-28T12:00:00Z")
    _append_source_slot_diagnostic(diag, source_started=started, finished_at="2026-08-28T12:00:00Z",
        status=preserved, payload=payload, origin="fallback_preserved")
    assert diag["source_slots"][0]["page_count"] == 1
    assert diag["source_slots"][0]["rows_fetched"] == 1
    assert diag["source_slots"][0]["source_diagnostics"]["preserved_snapshot"]["accepted_at"] == accepted_at


def check_legacy():
    ads = ads_transform({"snapshot_date": DAY, "data": {"rows": []}})
    fin = finance_transform({"snapshot_date": DAY, "data": {"rows": []}})
    assert ads.result.diagnostics["batch_count"] is None
    assert fin.result.diagnostics["pagination"]["pages"] is None
    assert fin.result.diagnostics["source_row_count"] is None
    summary = _build_refresh_source_summary([{"source_key": "fin_report_daily", "rows_fetched": None}])
    assert summary[0]["rows_fetched"] is None
    # The second transformation boundary retains the original observation.
    source = SimpleNamespace(fetch=lambda request: {"snapshot_date": DAY, "source": {"source_row_count": 7}, "data": {"rows": [None]}})
    status, result = capture("ads_compact", lambda: AdsCompactBlock(source).execute(AdsCompactRequest("ads_compact", DAY, [101])).result)
    assert result is None and status.diagnostics["source_row_count"] == 7
    assert status.diagnostics["error_code"] == "ads_compact_transform_failed"


def main():
    with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
         patch("urllib.request.urlopen", side_effect=AssertionError("HTTP forbidden")), \
         TemporaryDirectory(prefix="source-attempt-diagnostics-") as tmp:
        root = Path(tmp)
        check_ads()
        accepted, failed = check_finance(root)
        check_wrappers(root / "wrappers", accepted, failed)
        check_legacy()
    print("source_attempt_diagnostics: ok -> synthetic Ads/Finance, partial evidence, closure, last-good, legacy unknown; network forbidden")


if __name__ == "__main__":
    main()
