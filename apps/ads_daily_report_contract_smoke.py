"""Synthetic exact-date Ads contract tests. All account HTTP is mocked."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.adapters.ads_compact_block import HttpBackedAdsCompactSource
from packages.application.ads_compact_block import AdsCompactBlock
from packages.contracts.ads_compact_block import AdsCompactRequest
from packages.contracts.ads_daily_report import (
    AdsDatedRoster, AdsReportError, FIELDS, MAX_EXACT_ADS_COUNT,
    project_ads_daily_report, validate_ads_campaign_batch,
)
from packages.contracts.source_attempt_diagnostics import SourceAttemptError, source_digest

DAY = "2026-09-10"


def campaign(nm_id=101, advert_id=5, *, day=DAY, zero=False):
    metrics = dict(views=50, clicks=3, atbs=1, orders=1, sum=7, sum_price=100)
    if zero:
        metrics = dict.fromkeys(FIELDS, 0)
    return {"advertId": advert_id, **metrics, "days": [{"date": day, **metrics,
            "apps": [{"appType": 1, **metrics, "nms": [{"nmId": nm_id, **metrics}]}]}]}


def roster(ids=(5,), day=DAY):
    # Synthetic caller qualification, never a production/provider assertion.
    return AdsDatedRoster(day, "synthetic-account", tuple(ids), "fixture://dated-roster",
                          source_digest({"day": day, "campaigns": ids}))


def run_source(pages, ids=(5,), nms=(101, 102), batch_size=50, binding=True):
    source = HttpBackedAdsCompactSource(complete_catalog=True, max_ids_per_request=batch_size,
        batch_sleep_seconds=0, dated_roster=roster(ids) if binding else None)
    responses = [{"all": len(ids), "adverts": [{"status": 9, "count": len(ids),
                 "advert_list": [{"advertId": i} for i in ids]}]}] + [p if isinstance(p, Exception) else deepcopy(p) for p in pages]
    calls = []
    def fake_json(**kwargs):
        calls.append(kwargs["url"])
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    source._get_json = fake_json
    with patch("packages.adapters.ads_compact_block.load_runtime_config",
               return_value=SimpleNamespace(base_url="unused", token="fixture", timeout_seconds=1)):
        result = AdsCompactBlock(source).execute(AdsCompactRequest("ads_compact", DAY, list(nms))).result
    return result, calls


class AdsContractTests(unittest.TestCase):
    def validate(self, payload):
        return validate_ads_campaign_batch(payload, campaign_ids=[5], snapshot_date=DAY)

    def reject(self, payload, code=None):
        with self.assertRaises(AdsReportError) as caught:
            self.validate(payload)
        if code:
            self.assertEqual(str(caught.exception), code)

    def test_campaign_shape_identity_and_omission(self):
        for payload in (None, {}, [], [None], [campaign(), campaign()]):
            with self.subTest(payload=repr(payload)[:50]): self.reject(payload)
        for identity in (True, 5.0, "5", 0, -5, [], None, 6):
            with self.subTest(identity=identity): self.reject([campaign(advert_id=identity)])
        for ids in ([5, 5], [True], [5.0], [-1]):
            with self.subTest(ids=ids), self.assertRaises(AdsReportError):
                validate_ads_campaign_batch([campaign()], campaign_ids=ids, snapshot_date=DAY)

    def test_no_statistics_not_zero(self):
        for total in (None, 0, 1):
            item = {"advertId": 5, "days": []}
            if total is not None: item["sum"] = total
            self.reject([item], "ads_catalog_campaign_positive_sum_without_days" if total else "ads_catalog_no_statistics_unconfirmed")
        item = campaign(); item["days"][0]["apps"] = []
        self.reject([item])
        item = campaign(); item["days"][0]["apps"][0]["nms"] = []
        self.reject([item])

    def test_every_required_metric_at_every_level(self):
        for level in range(4):
            for field in FIELDS:
                for bad in ("missing", None, True, "0", -1, float("nan"), float("inf"), 10 ** 400):
                    with self.subTest(level=level, field=field, bad=repr(bad)[:10]):
                        item = campaign()
                        row = [item, item["days"][0], item["days"][0]["apps"][0], item["days"][0]["apps"][0]["nms"][0]][level]
                        if bad == "missing": del row[field]
                        else: row[field] = bad
                        self.reject([item])
        item = campaign(); item["views"] = 0.5; self.reject([item])

    def test_reconciliation_all_metrics_and_levels(self):
        for level in range(3):
            for field in FIELDS:
                with self.subTest(level=level, field=field):
                    item = campaign()
                    [item, item["days"][0], item["days"][0]["apps"][0]][level][field] += 1
                    self.reject([item])
        item = campaign(zero=True); item["sum"] = .01
        self.reject([item])  # A tolerance cannot turn a positive total into zero.

    def test_dates_and_duplicate_keys(self):
        for label in ("2026-09-09", "2026-09-11", DAY + "Tbad", DAY + "garbage", DAY + "T23:00:00Z", "2026-02-30", DAY + "T00:00:00"):
            with self.subTest(label=label): self.reject([campaign(day=label)], "ads_catalog_day_invalid")
        for label in (DAY, DAY + "T00:00:00Z", DAY + "T00:00:00+03:00"):
            self.validate([campaign(day=label)])
        item = campaign(); item["days"].append(deepcopy(item["days"][0])); self.reject([item], "ads_catalog_duplicate_day")
        item = campaign(); item["days"][0]["apps"].append(deepcopy(item["days"][0]["apps"][0])); self.reject([item], "ads_catalog_duplicate_platform")
        item = campaign(); item["days"][0]["apps"][0]["nms"] *= 2; self.reject([item], "ads_catalog_duplicate_sku")
        for key, values in (("appType", (True, "1", 2, None)), ("nmId", (True, 101.0, "101", 0, None))):
            for value in values:
                item = campaign(); app = item["days"][0]["apps"][0]
                (app if key == "appType" else app["nms"][0])[key] = value
                self.reject([item])

    def test_cross_platform_and_campaign_addition(self):
        item = campaign(); day = item["days"][0]
        other = deepcopy(day["apps"][0]); other["appType"] = 32; day["apps"].append(other)
        for key in FIELDS: item[key] *= 2; day[key] *= 2
        projected = project_ads_daily_report([item, campaign(advert_id=6)], snapshot_date=DAY,
            nm_ids=[101, 102], roster=roster((5, 6)))
        self.assertEqual([r["ads_sum"] for r in projected["data"]["rows"]], [21, 0])
        self.assertEqual(projected["data"]["rows"][0]["ads_views"], 150)

    def test_full_scope_no_activity_and_non_target(self):
        result = project_ads_daily_report([campaign(nm_id=999)], snapshot_date=DAY, nm_ids=list(range(1, 93)), roster=roster())
        self.assertEqual(len(result["data"]["rows"]), 92)
        self.assertTrue(all(r["ads_sum"] == 0 for r in result["data"]["rows"]))
        zero = self.validate([campaign(zero=True)])
        self.assertEqual(zero["campaign_outcomes"][0]["state"], "no_activity_proven")
        empty = project_ads_daily_report([], snapshot_date=DAY, nm_ids=[101], roster=roster(()))
        self.assertEqual(empty["data"]["rows"][0]["ads_views"], 0)
        item = campaign(nm_id=999); del item["days"][0]["apps"][0]["nms"][0]["views"]
        self.reject([item])

    def test_roster_unknown_or_drift_never_proves_zero(self):
        for binding in (None, replace(roster(), snapshot_date="2026-09-11"), replace(roster(), campaign_ids=(5, 6)),
                        replace(roster(), evidence_digest="missing"), replace(roster(), evidence_ref="")):
            with self.subTest(binding=binding), self.assertRaises(AdsReportError):
                project_ads_daily_report([campaign()], snapshot_date=DAY, nm_ids=[101, 102], roster=binding)
        # A vanished/omitted ID remains unknown even when the remaining source is valid.
        with self.assertRaises(AdsReportError):
            project_ads_daily_report([campaign()], snapshot_date=DAY, nm_ids=[101], roster=roster((5, 6)))
        with self.assertRaises(SourceAttemptError) as caught:
            run_source([[campaign()]], binding=False)
        self.assertEqual(caught.exception.code, "ads_catalog_dated_roster_unqualified")

    def test_adapter_multibatch_rates_and_diagnostics(self):
        result, calls = run_source([[campaign()], [campaign(advert_id=6)]], ids=(5, 6), batch_size=1)
        self.assertEqual(len(calls), 3)
        self.assertEqual([i.ads_sum for i in result.items], [14, 0])
        self.assertEqual(result.items[0].ads_cpc, 14 / 6)
        self.assertEqual(result.items[0].ads_ctr, 6 / 100)
        d = result.diagnostics
        self.assertEqual(d["expected_campaign_ids"], [5, 6])
        self.assertEqual(d["returned_campaign_ids"], [5, 6])
        self.assertEqual(d["missing_campaign_ids"], [])
        self.assertEqual(d["counter_basis"], "observed_campaign_responses")
        self.assertEqual(len(d["batches"]), 2)

    def test_partial_batch_and_failures_preserve_observed_counters(self):
        for failure in (SourceAttemptError("ads_http_error", {"http_status": 429}),
                        SourceAttemptError("ads_http_error", {"http_status": 500}),
                        TimeoutError("private detail"), []):
            with self.subTest(failure=str(failure)), self.assertRaises(SourceAttemptError) as caught:
                run_source([[campaign()], failure], ids=(5, 6), batch_size=1)
            d = caught.exception.diagnostics
            self.assertEqual(d["returned_campaign_ids"], [5])
            self.assertEqual(d["missing_campaign_ids"], [6])
            self.assertNotIn("private detail", json.dumps(d))
        with self.assertRaises(SourceAttemptError) as caught:
            run_source([[{"advertId": 5, "sum": 20, "days": []}]])
        self.assertIn("campaign_positive_sum_without_days", caught.exception.diagnostics["anomaly_codes"])

    def test_request_limit_and_legacy_compatibility(self):
        for size in (0, -1, 51, True, 1.5):
            with self.subTest(size=size), self.assertRaises(ValueError):
                HttpBackedAdsCompactSource(max_ids_per_request=size)
        source = HttpBackedAdsCompactSource(complete_catalog=False)
        sparse = campaign(); del sparse["days"][0]["apps"][0]["nms"][0]["views"]
        source._get_json = lambda **_: [sparse]
        rows = source._fetch_compact_rows(base_url="unused", token="fixture", advert_ids=[5],
            snapshot_date=DAY, nm_ids=[101, 102], timeout_seconds=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ads_views"], 0)

    def test_50_campaign_limit_is_not_a_sku_limit(self):
        ids = tuple(range(1, 52))
        pages = [[campaign(advert_id=i) for i in ids[:50]], [campaign(advert_id=51)]]
        result, calls = run_source(pages, ids=ids, nms=tuple(range(1, 93)))
        self.assertEqual(len(result.items), 92)
        self.assertEqual(len(calls), 3)  # One catalog and exactly two fullstats; no probes.
        self.assertEqual(len(result.diagnostics["batches"][0]["expected_campaign_ids"]), 50)
        source = HttpBackedAdsCompactSource(complete_catalog=True)
        source._get_json = lambda **_: self.fail("invalid scope reached HTTP")
        for nms in ([101, 101], [True], [101.0], [0]):
            with self.subTest(nms=nms), self.assertRaises(SourceAttemptError):
                source._fetch_compact_rows(base_url="unused", token="fixture", advert_ids=[5],
                    snapshot_date=DAY, nm_ids=nms, timeout_seconds=1)

    def test_aggregate_overflow_is_not_published(self):
        items = [campaign(), campaign(advert_id=6)]
        for item in items:
            day = item["days"][0]; app = day["apps"][0]
            for level in (item, day, app, app["nms"][0]): level["sum"] = 1e308
        with self.assertRaises(AdsReportError):
            project_ads_daily_report(items, snapshot_date=DAY, nm_ids=[101], roster=roster((5, 6)))
        with self.assertRaises(SourceAttemptError):
            run_source([items], ids=(5, 6))

    def test_count_domain_exact_reconciliation_and_projection(self):
        def counted(value, field="views", advert_id=5):
            item = campaign(advert_id=advert_id)
            day = item["days"][0]; app = day["apps"][0]
            for row in (item, day, app, app["nms"][0]): row[field] = value
            return item
        for field in FIELDS[:4]:
            cases = (
                [counted(2**53 + 1, field)],
                [counted(MAX_EXACT_ADS_COUNT, field), counted(2, field, 6)],
            )
            for items in cases:
                ids = tuple(item["advertId"] for item in items)
                with self.subTest(field=field, campaigns=len(ids)):
                    with self.assertRaisesRegex(AdsReportError, "ads_catalog_count_out_of_range"):
                        project_ads_daily_report(items, snapshot_date=DAY, nm_ids=[101], roster=roster(ids))
                    with self.assertRaisesRegex(SourceAttemptError, "ads_catalog_count_out_of_range"):
                        run_source([items], ids=ids, nms=(101,))
            # Both a direct boundary and a cross-campaign sum at it are exact.
            for items in ([counted(MAX_EXACT_ADS_COUNT, field)],
                          [counted(MAX_EXACT_ADS_COUNT - 2, field), counted(2, field, 6)]):
                ids = tuple(item["advertId"] for item in items)
                pure = project_ads_daily_report(items, snapshot_date=DAY, nm_ids=[101], roster=roster(ids))
                adapter, _ = run_source([items], ids=ids, nms=(101,))
                self.assertEqual(pure["data"]["rows"][0][f"ads_{field}"], MAX_EXACT_ADS_COUNT)
                self.assertEqual(getattr(adapter.items[0], f"ads_{field}"), MAX_EXACT_ADS_COUNT)
        malformed = counted(10**28)
        malformed["days"][0]["apps"][0]["nms"][0]["views"] += 1
        self.reject([malformed], "ads_catalog_count_out_of_range")
        # A one-unit difference inside the admitted domain also fails exactly.
        mismatch = counted(MAX_EXACT_ADS_COUNT - 1)
        mismatch["days"][0]["apps"][0]["nms"][0]["views"] += 1
        self.reject([mismatch], "ads_catalog_metric_totals_mismatch")


if __name__ == "__main__":
    unittest.main(verbosity=2)
