#!/usr/bin/env python3
"""Explicit Chromium proof of the real settings writer → HTTP handler → temp DB.

Run with Playwright 1.58.0 and its Chromium installed. This separate browser
entrypoint is not in the base-owned PR Gate dependency map; it never skips or
installs dependencies. Optional --output-dir retains JSON and screenshots.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps import nomenclature_activation_intents_smoke as source  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.supplier_shipments import SupplierShipmentsBlock  # noqa: E402


TEMPLATE = ROOT / "packages/adapters/templates/sheet_vitrina_v1_settings.html"
CONFIG = {"nomenclature_path": "/nomenclature", "sku_groups_path": "/groups",
          "trade_documents_path": "/documents", "can_manage_users": False}
HTML = TEMPLATE.read_text().replace("__SHEET_VITRINA_V1_SETTINGS_CONFIG_JSON__", json.dumps(CONFIG))


def forbid_wb(*args, **kwargs):
    raise AssertionError("WB calls forbidden in browser fixture")


class Form:
    def __init__(self, browser, root: Path, initial: str):
        self.runtime = source.fixture(root)
        self.runtime.save_sku_group({"group_key": "fixture", "label": "Fixture", "is_active": True,
                                     "created_at": source.NOW, "updated_at": source.NOW})
        self.item = {**source._sku(101, updated_at=source.NOW), "match_key": "fixture|iphone16",
                     "barcode": "4600000000101", "barcode_source": "manual",
                     "nomenclature_name": "Fixture item", "purchase_price_yuan": 10}
        if initial == "pending":
            source.staged(self.runtime, [self.item])
        elif initial != "new":
            self.runtime.save_nomenclature_item({**self.item, "is_active": initial == "active"})
        self.block = SupplierShipmentsBlock(
            runtime=self.runtime, timestamp_factory=lambda: "2026-08-26T08:01:00Z",
            barcode_source=SimpleNamespace(fetch_barcodes_by_nm_ids=forbid_wb))
        self.entry = RegistryUploadHttpEntrypoint.__new__(RegistryUploadHttpEntrypoint)
        self.entry.supplier_shipments_block = self.block
        self.entry._attach_wb_finance_cost_recalculation = lambda value: value
        self.writes = []
        self.errors = []
        self.fail_activation = False
        self.context = browser.new_context(viewport={"width": 1600, "height": 1050})
        self.context.route("**/*", self.route)
        self.page = self.context.new_page()
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.reload()

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        assert url.hostname == "fixture.invalid", "unexpected external browser request"
        if url.path == "/settings":
            route.fulfill(status=200, content_type="text/html", body=HTML)
            return
        code = 200
        if url.path == "/nomenclature" and request.method == "GET":
            payload = self.entry.handle_nomenclature_list_request({"visibility": "all"})
        elif url.path == "/groups":
            payload = {"groups": self.runtime.list_sku_groups()}
        elif url.path == "/documents":
            payload = {"documents": []}
        elif url.path.startswith("/nomenclature") and request.method in {"PATCH", "POST"}:
            body = json.loads(request.post_data)
            write = {"method": request.method, "payload": body}
            self.writes.append(write)
            failure = patch.object(source.DenseFbsService, "activate_staged_skus", side_effect=
                                   source.DenseFbsError("fixture_readiness", "Fixture readiness pending"))
            try:
                with failure if self.fail_activation else nullcontext():
                    payload = (self.entry.handle_nomenclature_create_request(body) if request.method == "POST"
                               else self.entry.handle_nomenclature_patch_request(url.path.rsplit("/", 1)[1], body))
            except ValueError as exc:
                code, payload = 400, {"error": str(exc)}
            write["http_status"] = code
        else:
            raise AssertionError("Unexpected fixture request: " + request.method + " " + url.path)
        route.fulfill(status=code, content_type="application/json", body=json.dumps(payload))

    def reload(self):
        self.page.goto("http://fixture.invalid/settings", wait_until="networkidle")

    @property
    def row(self):
        return self.page.locator('#nomenclatureRows tr[data-item-id]').first

    def save(self, *, error=False):
        self.row.locator("[data-save-item]").click()
        message = self.page.locator("#nomenclatureMessage")
        if error:
            expect(message).to_have_class("message error")
        else:
            expect(message).to_have_text("Справочник сохранён.")

    def state(self):
        return self.runtime.list_nomenclature_items()[0]

    def screenshot(self, output: Path | None, name: str):
        if output:
            self.page.screenshot(path=str(output / (name + ".png")), full_page=True)


def check(browser, name: str, output: Path | None) -> dict:
    initial = ("new" if name.startswith("create") else "inactive" if name.startswith("inactive")
               else "active" if name.startswith("active") or name == "late_off_active" else "pending")
    with tempfile.TemporaryDirectory(prefix="nomenclature-browser-") as temporary:
        form = Form(browser, Path(temporary), initial)
        start_counts = source.counts(form.runtime)
        checkbox = form.row.locator('[data-field="is_active"]') if initial != "new" else None
        if initial == "pending":
            expect(checkbox).to_be_checked()
            expect(form.row.locator('[data-activation-status]')).to_have_text("Ожидает активации")
            expect(form.row.locator('[data-activation-status]')).to_be_visible()
            assert form.state()["is_active"] is False
        elif initial != "new":
            assert checkbox.is_checked() == (initial == "active")
            assert form.row.locator('[data-activation-status]').count() == 0

        if name == "client_validation":
            # Desired active must still be validated before metadata omits the flag.
            for field, bad in (("nomenclature_name", ""), ("purchase_price_yuan", "-1"),
                               ("factory_box_size", "0"), ("factory_box_size", "1.5")):
                form.reload()
                form.row.locator('[data-field="' + field + '"]').fill(bad)
                form.save(error=True)
                assert form.writes == [] and form.state()["activation_status"] == "pending"
            assert source.counts(form.runtime) == (0, 0)
        elif initial == "new":
            form.page.locator("#addItemButton").click()
            checkbox = form.row.locator('[data-field="is_active"]')
            expect(checkbox).to_be_checked()
            form.row.locator('[data-field="nomenclature_name"]').fill("Created in browser")
            form.row.locator('[data-field="nm_id"]').fill("101")
            form.row.locator('[data-field="barcode"]').fill("4600000000101")
            if name == "create_off":
                checkbox.uncheck()
            form.save()
            desired = name != "create_off"
            assert form.writes[0]["method"] == "POST"
            assert form.writes[0]["payload"]["is_active"] is desired
            assert form.state()["is_active"] is desired
            assert source.counts(form.runtime) == ((2, 0) if desired else (0, 0))
        else:
            if name.startswith("late_off"):
                form.entry.handle_nomenclature_delete_request(form.item["item_id"])
                assert form.state()["is_active"] is False
            if name.endswith("explicit_off"):
                checkbox.uncheck()
            if name == "inactive_explicit_on":
                checkbox.check()
            field = "purchase_price_yuan" if name == "pending_price" else "nomenclature_name"
            form.row.locator('[data-field="' + field + '"]').fill("11.5" if field == "purchase_price_yuan" else "Edited in browser")
            if name == "pending_failed_save_reload":
                form.fail_activation = True
            if name == "pending_name":
                form.screenshot(output, "pending_status_before_save")
            form.save(error=form.fail_activation)
            assert len(form.writes) == 1 and form.writes[0]["method"] == "PATCH"
            payload = form.writes[0]["payload"]
            if "explicit" in name:
                assert payload["is_active"] is (name == "inactive_explicit_on")
            else:
                assert "is_active" not in payload
            if form.fail_activation:
                assert form.state()["activation_status"] == "pending" and not form.state()["is_active"]
                assert source.counts(form.runtime) == (0, 0)
                form.reload()
                expect(form.row.locator('[data-field="is_active"]')).to_be_checked()
                expect(form.row.locator('[data-activation-status]')).to_be_visible()
                form.screenshot(output, "failed_save_reloaded_pending")
                form.fail_activation = False
            source.ordinary(form.runtime)
            off = name.startswith("late_off") or name.endswith("explicit_off") or name == "inactive_metadata"
            assert form.state()["is_active"] is (not off)
            assert form.state()["activation_status"] == ("inactive" if off else "active")
            expected_counts = start_counts if off or initial == "active" else (2, 0)
            assert source.counts(form.runtime) == expected_counts
            if not off:
                for facility in ("f0", "f1"):
                    source._assert_zero(form.runtime.db_path, facility, 101)
                form.reload()
                assert form.row.locator('[data-activation-status]').count() == 0
            if name == "pending_name":
                form.screenshot(output, "active_after_save")
            # The ordinary retry must not add canonical documents or movements.
            assert source.ordinary(form.runtime)["nomenclature_activation"]["status"] == "no_op"
            assert source.counts(form.runtime) == expected_counts
        assert not form.errors
        result = {"scenario": name, "status": "PASS", "writes": form.writes,
                  "final_is_active": form.state()["is_active"],
                  "source_status": form.state()["activation_status"],
                  "source_revision": form.state()["activation_source_revision"],
                  "canonical_documents_and_lines": source.counts(form.runtime), "page_errors": form.errors}
        form.context.close()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    cases = ["pending_name", "pending_price", "pending_failed_save_reload", "pending_explicit_off",
             "late_off_pending", "late_off_active", "active_metadata", "inactive_metadata",
             "active_explicit_off", "inactive_explicit_on", "create_default_on", "create_off", "client_validation"]
    with sync_playwright() as playwright, patch("socket.create_connection", side_effect=AssertionError("network forbidden")) as network:
        browser = playwright.chromium.launch()
        try:
            results = [check(browser, name, args.output_dir) for name in cases]
        finally:
            browser.close()
        assert network.call_count == 0
    report = {"status": "PASS", "template_sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
              "external_network_calls": network.call_count, "scenarios": results}
    if args.output_dir:
        (args.output_dir / "browser_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
