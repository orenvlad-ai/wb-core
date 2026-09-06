#!/usr/bin/env python3
"""Browser smoke for planning rows in the MAIN Web Vitrina table."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_inventory_planning_smoke import (  # noqa: E402
    CURRENT_DATE,
    _seed_inventory_planning,
)
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_INVENTORY_PLANNING_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)
from packages.application.sheet_vitrina_v1_inventory_planning import (  # noqa: E402
    COMBINED_EFFECTIVE_ALIAS_KEY,
    COMBINED_TOTAL_ALIAS_KEY,
    INVENTORY_FBS_TOTAL_KEY,
    INVENTORY_WB_EFFECTIVE_KEY,
    INVENTORY_WB_TOTAL_KEY,
    inventory_planning_facility_metric_key,
)
from packages.application.sheet_vitrina_v1_incident_stocks import (  # noqa: E402
    INCIDENT_STOCK_METRIC_KEYS,
)


STORAGE_KEY = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:metric-presentation:v1"


def main() -> int:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    base_url = fixture.__enter__()
    try:
        runtime = fixture.entrypoint.runtime
        enabled = [item for item in runtime.load_current_state().config_v2 if item.enabled]
        first_nm_id, second_nm_id = int(enabled[0].nm_id), int(enabled[1].nm_id)
        _seed_inventory_planning(runtime.db_path, nm_ids=(first_nm_id, second_nm_id))
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "DELETE FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='orenburg' AND pool='FBS' AND nm_id=?",
                (second_nm_id,),
            )
            conn.commit()

        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_responses: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                color_scheme="dark",
            )
            context.add_init_script(
                """() => {
                  localStorage.setItem(%s, JSON.stringify({
                    version: 4,
                    presentation: {
                      order: [
                        "pair::total_orderCount::orderCount",
                        "pair::total_wb_stock_effective_qty::wb_stock_effective_qty",
                        "pair::total_stock_total::stock_total"
                      ],
                      display: {
                        "pair::total_orderCount::orderCount": "shown",
                        "pair::total_wb_stock_effective_qty::wb_stock_effective_qty": "hidden",
                        "pair::total_stock_total::stock_total": "hidden"
                      },
                      manual: true
                    },
                    expanded_anchors: [],
                    sku_presets: [{
                      preset_id: "analysis",
                      name: "Анализ",
                      metric_keys: ["orderCount"]
                    }],
                    sku_highlight_metric_keys: [],
                    sku_metric_selection: {
                      mode: "preset",
                      preset_id: "analysis",
                      all: false,
                      metric_keys: []
                    },
                    migrations: {
                      incident_effective_shown_v1: true,
                      sku_presets_seeded_v1: true,
                      unified_presentation_v1: true
                    }
                  }));
                }""" % json.dumps(STORAGE_KEY),
            )
            page = context.new_page()
            page.route(
                "**/v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-options",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"status":"ok","options":[]}',
                ),
            )
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: failed_responses.append(f"{response.status} {response.url}")
                if response.status >= 400
                else None,
            )
            page.goto(
                base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=vitrina",
                wait_until="domcontentloaded",
            )
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=30000)
            page.wait_for_selector(
                f'td[data-metric-key="{INVENTORY_WB_TOTAL_KEY}"]',
                timeout=30000,
            )

            active_tab = page.locator('[data-unified-tab-button="vitrina"]')
            expect(active_tab).to_have_attribute("aria-selected", "true")
            planning_keys = [
                COMBINED_TOTAL_ALIAS_KEY,
                INVENTORY_WB_TOTAL_KEY,
            ]
            for metric_key in planning_keys:
                if page.locator(f'td[data-metric-key="{metric_key}"]').count() < 2:
                    raise AssertionError(
                        f"main table must render TOTAL + per-SKU rows for {metric_key}"
                    )

            expect(
                page.locator(
                    f'td[data-row-id="SKU:{second_nm_id}|{COMBINED_TOTAL_ALIAS_KEY}"]'
                    f'[data-cell-date="{CURRENT_DATE}"]'
                )
            ).to_have_text("30")
            # The missing row belongs to an inactive facility. Migration 161
            # retains that physical/history evidence but excludes it from the
            # current operand instead of publishing a false partial warning.
            expect(
                page.locator(
                    f'td[data-row-id="SKU:{second_nm_id}|{COMBINED_TOTAL_ALIAS_KEY}"]'
                    f'[data-cell-date="{CURRENT_DATE}"] .inventory-partial-marker'
                )
            ).to_have_count(0)
            moscow_key = inventory_planning_facility_metric_key("moscow")
            orenburg_key = inventory_planning_facility_metric_key("orenburg")
            expect(page.locator(f'td[data-metric-key="{INVENTORY_FBS_TOTAL_KEY}"]')).to_have_count(0)
            expect(page.locator(f'td[data-metric-key="{moscow_key}"]')).to_have_count(0)
            expect(page.locator(f'td[data-metric-key="{orenburg_key}"]')).to_have_count(0)
            hidden_keys = {
                INVENTORY_WB_EFFECTIVE_KEY,
                "total_" + INVENTORY_WB_EFFECTIVE_KEY,
                COMBINED_EFFECTIVE_ALIAS_KEY,
                "total_" + COMBINED_EFFECTIVE_ALIAS_KEY,
                *INCIDENT_STOCK_METRIC_KEYS,
            }
            for metric_key in hidden_keys:
                expect(page.locator(f'[data-metric-key="{metric_key}"]')).to_have_count(0)
            body_text = page.locator("body").inner_text()
            for retired_label in (
                "Остаток WB без инц.: всего",
                "Остаток без инц.: всего",
                "Остаток WB инцидент",
            ):
                if retired_label in body_text:
                    raise AssertionError(f"legacy incident row leaked into ordinary UI: {retired_label}")

            page.locator("[data-metrics-settings-open]").click()
            page.wait_for_selector("[data-metrics-presentation]:not([hidden])")
            expected_labels = (
                "Остатки общие",
                "Остатки WB",
                "Остатки FBS Москва",
                "Остатки FBS Оренбург",
            )
            config_labels = page.locator("[data-metric-config-row] .metrics-config-label").all_inner_texts()
            for label in expected_labels:
                if config_labels.count(label) != 1:
                    raise AssertionError(
                        f"logical metric catalog must contain {label!r} exactly once, got {config_labels.count(label)}"
                    )

            fbs_config = page.locator(
                f'[data-metric-config-row][data-sku-metric-key="{moscow_key}"]'
            )
            display_select = fbs_config.locator("[data-metric-display-select]")
            display_select.select_option("shown")
            page.locator("[data-metrics-settings-close]").last.click()
            page.wait_for_selector(
                f'td[data-metric-key="{moscow_key}"]',
                timeout=5000,
            )
            expect(
                page.locator(
                    f'td[data-row-id="SKU:{first_nm_id}|{moscow_key}"]'
                    f'[data-cell-date="{CURRENT_DATE}"]'
                )
            ).to_have_text("-3")

            persisted = page.evaluate(
                "storageKey => JSON.parse(localStorage.getItem(storageKey) || '{}')",
                STORAGE_KEY,
            )
            preset_keys = persisted["sku_presets"][0]["metric_keys"]
            for metric_key in planning_keys:
                if metric_key not in preset_keys:
                    raise AssertionError(
                        f"existing preset must receive newly seen planning metric {metric_key}"
                    )
            migrated_keys = persisted["migrations"]["inventory_planning_metric_keys_v1"]
            catalog_keys = {*planning_keys, moscow_key, orenburg_key}
            if not catalog_keys.issubset(set(migrated_keys)):
                raise AssertionError(f"planning-key migration evidence missing: {persisted}")
            if moscow_key in preset_keys or orenburg_key in preset_keys:
                raise AssertionError("facility rows must enter user presets only after explicit configuration")
            if hidden_keys & set(preset_keys) or hidden_keys & set(migrated_keys):
                raise AssertionError("legacy incident planning keys survived public preference sanitation")

            expect(page.locator(f'td[data-metric-key="{orenburg_key}"]')).to_have_count(0)
            reloaded_persisted = page.evaluate(
                "storageKey => JSON.parse(localStorage.getItem(storageKey) || '{}')",
                STORAGE_KEY,
            )
            if orenburg_key in reloaded_persisted["sku_presets"][0]["metric_keys"]:
                raise AssertionError("new facility became visible without explicit user opt-in")

            page.locator('[data-unified-tab-button="warehouses"]').click()
            page.wait_for_selector(
                "[data-inventory-planning-metrics] [data-inventory-metric-toggle]",
                timeout=30000,
            )
            expected_planning_labels = [
                "Остаток WB: всего",
                "Остаток FBS: всего",
                "Остаток FBS: Москва",
                "Остаток: всего",
            ]
            control_labels = page.locator(
                "[data-inventory-planning-metrics] .inventory-metric-controls label"
            ).all_inner_texts()
            card_labels = page.locator(
                "[data-inventory-planning-metrics] .inventory-metric-card span"
            ).all_inner_texts()
            if control_labels != expected_planning_labels:
                raise AssertionError(
                    f"ordinary planning controls leaked legacy incident metrics: {control_labels}"
                )
            if card_labels != expected_planning_labels:
                raise AssertionError(
                    f"ordinary planning cards leaked legacy incident metrics: {card_labels}"
                )
            # Orenburg is inactive in this fixture; official current cards cover
            # active facilities only, while legacy history above stays intact.
            assert "официальный снимок WB" in page.locator('[data-inventory-planning-meta]').inner_text()
            planning_payload = page.evaluate(
                """async path => {
                  const response = await fetch(path, {headers: {Accept: "application/json"}});
                  if (!response.ok) throw new Error("planning read failed: HTTP " + response.status);
                  return response.json();
                }""",
                DEFAULT_INVENTORY_PLANNING_PATH,
            )
            audit_metric_keys = {
                str(metric.get("metric_key") or "")
                for metric in planning_payload.get("metrics") or []
            }
            if {"wb_effective_total", "effective_total"} - audit_metric_keys:
                raise AssertionError(
                    "presentation cleanup removed retained incident audit evidence"
                )

            from apps.warehouse_official_fbs_cards_smoke import seed, NOW
            from packages.application.inventory_planning_read_model import InventoryPlanningReadModel
            with TemporaryDirectory() as tmp:
                official_path = Path(tmp) / "official.sqlite3"
                seed(official_path).close()
                official_payload = InventoryPlanningReadModel(db_path=official_path).current_official_snapshot(now=NOW)
            page.route("**" + DEFAULT_INVENTORY_PLANNING_PATH, lambda route: route.fulfill(
                status=200, content_type="application/json", body=json.dumps(official_payload)))
            page.locator('[data-inventory-planning-refresh]').click()
            expect(page.locator('[data-inventory-planning-metrics] .inventory-metric-card strong')).to_have_text(
                ["10 шт", "29 008 шт", "29 003 шт", "5 шт", "29 018 шт"], use_inner_text=True)
            disclosure = page.locator('[data-inventory-planning-disclosure]')
            disclosure.locator('xpath=..').evaluate('node => { node.open = true; }')
            assert "Заявлено на WB, шт." in disclosure.inner_text()
            assert "Physical" not in disclosure.inner_text() and "Reserved" not in disclosure.inner_text()
            expected_fbs_time = page.evaluate('value => warehouseDate(value)', official_payload['freshness']['fbs_updated_at'])
            assert expected_fbs_time in page.locator('[data-inventory-planning-meta]').inner_text()
            screenshots = os.environ.get("INVENTORY_PLANNING_SCREENSHOT_DIR", "").strip()
            if screenshots:
                Path(screenshots).mkdir(parents=True, exist_ok=True)
                page.locator('[data-inventory-planning-card]').screenshot(path=str(Path(screenshots) / "official-fbs-mobile.png"))
                page.set_viewport_size({"width": 1366, "height": 900})
                page.locator('[data-inventory-planning-card]').screenshot(path=str(Path(screenshots) / "official-fbs-desktop.png"))
                page.set_viewport_size({"width": 390, "height": 844})

            responsive = page.evaluate(
                """() => ({
                  width: window.innerWidth,
                  colorScheme: getComputedStyle(document.documentElement).colorScheme,
                  planningVisible: !!document.querySelector('[data-inventory-planning-card]')?.getClientRects().length,
                  bodyText: (document.body.innerText || '').length
                })"""
            )
            if (
                responsive["width"] != 390
                or "dark" not in responsive["colorScheme"]
                or not responsive["planningVisible"]
                or responsive["bodyText"] < 100
            ):
                raise AssertionError(f"mobile/dark planning acceptance failed: {responsive}")
            context.close()
            browser.close()

        if console_errors or page_errors:
            raise AssertionError(
                "browser errors: "
                f"console={console_errors}, pageerror={page_errors}, responses={failed_responses}"
            )
    finally:
        fixture.__exit__(None, None, None)

    print("sheet_vitrina_v1_inventory_planning_browser_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
