"""Portable regressions for the receipt policy used by the offline Linux acceptance."""

from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import sys

if __package__ in (None, ""):
    sys.path[0] = str(Path(__file__).resolve().parents[2])

from apps.local_cycle_lab.cycle_warnings import check_warnings, expected_warnings, UnexpectedCycleWarning


def receipt_for(missing_ads):
    day = date(2026, 11, 24)
    stages = []
    for stage, warnings in expected_warnings(day, 1, missing_ads=missing_ads).items():
        for warning in warnings:
            for field in ("batch_id", "content_hash", "accepted_digest"):
                if field in warning:
                    prefix = "sha256:" if field == "accepted_digest" else ""
                    warning[field] = prefix + "f" * 64 if warning[field] else ""
        stages.append(
            dict(
                stage=stage,
                status="degraded" if warnings else "complete",
                warnings=warnings,
                versions={"proof": "v"},
            )
        )
    return dict(business_date=day.isoformat(), status="degraded", stages=stages)


class WarningPolicyTests(unittest.TestCase):
    def test_provider_time_survives_process_restart(self):
        from apps.local_cycle_lab.__main__ import ReplayClock
        now = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "clock.json"
            first = ReplayClock(now, path)
            first.advance(240)
            again = ReplayClock(now, path)
            self.assertEqual(again.time(), first.time())
            again.advance(60)
            self.assertEqual(ReplayClock(now, path).time(), now.timestamp() + 300)

    def test_negative_control_allows_only_one_observed_failure(self):
        receipt = receipt_for(True)
        with self.assertRaises(UnexpectedCycleWarning) as failure:
            check_warnings(receipt, 1)
        self.assertEqual(failure.exception.stage, "api_sources")
        check_warnings(receipt, 1, missing_ads=True)
        for index in (1, 2, 7):
            broken = deepcopy(receipt)
            broken["stages"][index]["warnings"].pop()
            with self.assertRaises(UnexpectedCycleWarning):
                check_warnings(broken, 1, missing_ads=True)

    def test_extra_warning_duplicate_or_malformed_digest(self):
        receipt = receipt_for(False)
        check_warnings(receipt, 1)
        warnings = receipt["stages"][1]["warnings"]
        warnings.append(warnings[0])
        with self.assertRaises(UnexpectedCycleWarning):
            check_warnings(receipt, 1)
        warnings.pop()
        warnings[0]["accepted_digest"] = True
        with self.assertRaisesRegex(ValueError, "Malformed accepted_digest"):
            check_warnings(receipt, 1)


if __name__ == "__main__":
    unittest.main()
