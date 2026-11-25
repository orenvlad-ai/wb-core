"""Disposable black-box acceptance of the source bench, never production."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.request import Request

from .checks import verify
from .http import HttpTape
from .sources import Replay

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = Path(__file__).with_name("scenario.json")


def cli(*args):
    # A real production-looking environment must not select live configuration.
    env = {**os.environ, "WB_API_TOKEN": "must-not-be-used",
           "WB_FINANCE_API_BASE_URL": "https://must-not-be-used.invalid",
           "REGISTRY_UPLOAD_RUNTIME_DIR": "/must-not-be-used"}
    return subprocess.run([sys.executable, "-m", "apps.local_cycle_lab", *map(str, args)],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)


class SourceBenchSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory(prefix="wbc-source-bench-smoke-")
        cls.root = Path(cls.tmp.name)
        cls.scenario = json.loads(SCENARIO.read_text())

        def launch(variant):
            output = cli("--variant", variant, "--state", cls.root / variant)
            if output.returncode:
                raise AssertionError(output.stderr)
            return variant, json.loads(output.stdout)

        try:
            with ThreadPoolExecutor(max_workers=3) as pool:
                cls.results = dict(pool.map(launch, ("normal", "zero", "missing")))
        except BaseException:
            cls.tmp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_real_published_values_and_quality_for_all_variants(self):
        for variant, result in self.results.items():
            with self.subTest(variant=variant):
                self.assertEqual(result["verification"]["status"], "passed")
                self.assertGreaterEqual(result["verification"]["checks"], 75)
                self.assertEqual(result["cells"]["TOTAL|fin_storage_fee_total"]["2026-11-23"], 11)
        self.assertEqual(self.results["zero"]["cells"]["SKU:101|ads_sum"]["2026-11-23"], 0)
        self.assertEqual(self.results["missing"]["cells"]["SKU:101|ads_sum"]["2026-11-23"], "")

    def test_another_sku_changes_cabinet_storage_without_changing_displayed_sales(self):
        scenario = deepcopy(self.scenario)
        scenario["finance_rows"][1]["paidStorage"] = "19"
        scenario["expected"]["storage_total"] = 21  # 2 + 19; SKU 999 is outside CONFIG.
        path = self.root / "extra-storage.json"
        path.write_text(json.dumps(scenario))
        output = cli("--scenario", path)
        self.assertEqual(output.returncode, 0, output.stderr)
        result = json.loads(output.stdout)
        for day in scenario["days"]:
            self.assertEqual(result["cells"]["TOTAL|fin_storage_fee_total"][day], 21)
            self.assertEqual(result["cells"]["SKU:101|fin_buyout_rub"][day], 400)

    def test_restart_keeps_catalog_edit_and_does_not_reseed(self):
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        runtime = RegistryUploadDbBackedRuntime(self.root / "normal")
        item = runtime.load_nomenclature_item("test-101")
        item["our_sku"] = "MANUALLY-EDITED-101"
        runtime.save_nomenclature_item(item)
        output = cli("--state", self.root / "normal")
        self.assertEqual(output.returncode, 0, output.stderr)
        result = json.loads(output.stdout)
        self.assertNotIn("bootstrap", result["phase_totals"])
        self.assertNotIn("setup", result["phase_totals"])
        self.assertEqual(runtime.load_nomenclature_item("test-101")["our_sku"], "MANUALLY-EDITED-101")
        # This proves catalog persistence only; manual UI document acceptance is separate.

    def test_foreign_state_and_mismatched_variant_are_refused_before_database_open(self):
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("untouched")
        for path, extra in ((foreign, ()), (self.root / "normal", ("--variant", "zero"))):
            output = cli("--state", path, *extra)
            self.assertNotEqual(output.returncode, 0)
            self.assertIn("не принадлежит этому сценарию", output.stderr)
        self.assertEqual(list(foreign.iterdir()), [foreign / "keep.txt"])
        self.assertEqual((foreign / "keep.txt").read_text(), "untouched")

    def test_unplanned_http_method_query_and_body_have_no_fallback(self):
        for request in (
            Request("https://marketplace-api.wildberries.ru/api/v3/warehouses?extra=1"),
            Request("https://marketplace-api.wildberries.ru/api/v3/warehouses", method="DELETE"),
            Request("https://marketplace-api.wildberries.ru/api/v3/stocks/7001", data=b'{"chrtIds":[9999]}', method="POST"),
        ):
            replay = Replay(self.scenario, "normal")
            with self.assertRaisesRegex(ValueError, "Запрос вне тестового сценария"):
                HttpTape(replay).open(request)
            self.assertEqual(len(replay.unexpected), 1)
            self.assertFalse(replay.totals)

    def test_wrong_published_amount_and_lost_partial_flag_fail_verification(self):
        wrong = deepcopy(self.results["normal"])
        wrong["cells"]["TOTAL|fin_storage_fee_total"]["2026-11-23"] = 2
        with self.assertRaisesRegex(ValueError, "fin_storage_fee_total"):
            verify(wrong, self.scenario, "normal")
        wrong = deepcopy(self.results["zero"])
        wrong["cell_presentation"]["SKU:101|ads_sum"]["2026-11-23"]["completeness_state"] = "complete"
        with self.assertRaisesRegex(ValueError, "ads completeness"):
            verify(wrong, self.scenario, "zero")

    def test_current_process_guard_blocks_network_and_child_launch(self):
        code = '''
import socket, subprocess
from apps.local_cycle_lab.__main__ import offline_guard
violations=[]
offline_guard(violations)
for operation in (lambda: socket.create_connection(("127.0.0.1", 9)),
                  lambda: subprocess.Popen(["/usr/bin/true"])):
    try:
        operation()
    except RuntimeError:
        pass
    else:
        raise AssertionError("operation escaped")
assert violations == ["socket.getaddrinfo", "subprocess.Popen"], violations
'''
        output = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(output.returncode, 0, output.stderr)


if __name__ == "__main__":
    unittest.main()
