#!/usr/bin/env python3
"""Browser smoke for SKU inventory balance; all upstream operations are fake."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    _render_sheet_vitrina_web_vitrina_ui,
)


def main() -> None:
    html = _render_sheet_vitrina_web_vitrina_ui(
        read_path="/v1/sheet-vitrina-v1/web-vitrina",
        operator_path="/sheet-vitrina-v1/operator",
        refresh_path="/v1/sheet-vitrina-v1/refresh",
        job_path="/v1/sheet-vitrina-v1/job",
        role="operator",
        allowed_sections=["sku_management"],
        active_tab="sku-management",
    )
    settings = {
        "status": "ok",
        "revision": 2,
        "calculation": {
            "wb_confidence_coefficient": 0.5,
            "safety_stock_days": 10,
            "sales_period_days": 7,
            "bid_scale_min": 0.25,
            "bid_scale_max": 2.0,
            "automatic_training": False,
        },
        "table": {
            "visible_columns": [],
            "column_order": [],
            "column_widths": {},
            "filters": {"search": "", "status": ""},
            "preset": "all",
        },
    }
    calculation = _calculation()
    requests: list[tuple[str, str, dict]] = []
    console_errors: list[str] = []
    latest_job: list[dict | None] = [None]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1500, "height": 1000})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: console_errors.append(str(error)))

        def route_handler(route):
            request = route.request
            path = request.url.split("balance.test", 1)[-1]
            body = json.loads(request.post_data or "{}") if request.post_data else {}
            requests.append((request.method, path, body))
            if path == "/page":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            if path == "/v1/sheet-vitrina-v1/sku-management":
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "rows": [],
                            "settings": {
                                "revision": 0,
                                "forecast": {},
                                "table": {},
                            },
                            "meta": {},
                        }
                    ),
                )
                return
            base = "/v1/sheet-vitrina-v1/sku-management/inventory-balance"
            if path == base:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "ok",
                            "settings": settings,
                            "calculation": calculation,
                            "apply_job": latest_job[0],
                            "registry": _registry(latest_job[0]),
                            "apply_capability": {
                                "live_wb_available": False,
                                "wb_patch_reachable": False,
                            },
                        }
                    ),
                )
                return
            if path == base + "/calculations?limit=20":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_registry(latest_job[0])))
                return
            if path == base + "/calculate":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(calculation))
                return
            if path == base + "/settings":
                settings["revision"] += 1
                settings["calculation"] = body.get("calculation") or settings["calculation"]
                settings["table"] = body.get("table") or settings["table"]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
                return
            if path.endswith("/override"):
                target = calculation["rows"][0]["campaign_recommendations"][1]
                target["manual_target_bid_rub"] = body.get("manual_target_bid_rub")
                target["final_target_bid_rub"] = body.get("manual_target_bid_rub")
                calculation["rows"][0]["old_cpm_campaigns"][0] = target
                route.fulfill(status=200, content_type="application/json", body=json.dumps(calculation))
                return
            if path == base + "/apply-jobs":
                latest_job[0] = _job("pending", 0)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(latest_job[0]))
                return
            if path.endswith("/resume"):
                latest_job[0] = _job("completed", 2)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(latest_job[0]))
                return
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')

        page.route("**/*", route_handler)
        page.goto("http://balance.test/page")
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.locator("[data-inventory-balance-body]").wait_for()
        page.wait_for_timeout(500)
        assert "ibc_browser" in page.locator("[data-inventory-balance-status]").inner_text(), (
            page.locator("[data-inventory-balance-status]").inner_text(),
            page.locator("[data-inventory-balance-error]").inner_text(),
            console_errors,
        )
        assert page.locator('[data-sku-management-subpanel="general"]').is_hidden()
        assert page.locator('[data-sku-management-subpanel="inventory-balance"]').is_visible()
        balance_body_text = page.locator("[data-inventory-balance-body]").inner_text()
        assert "Дефицит" in balance_body_text, (
            balance_body_text,
            page.locator("[data-inventory-balance-status]").inner_text(),
            page.locator("[data-inventory-balance-error]").inner_text(),
            console_errors,
        )
        assert "агрегат WB × 0.5" in balance_body_text
        assert "без складской раскладки" in balance_body_text
        assert "Новые CPC" in page.locator("[data-inventory-balance-head]").inner_text()
        assert "Старые CPM" in page.locator("[data-inventory-balance-head]").inner_text()
        assert page.locator("[data-inventory-balance-xlsx]").is_enabled()

        page.locator("[data-inventory-balance-settings]").evaluate("node => { node.open = true; }")
        coefficient_input = page.locator(
            '[data-inventory-balance-setting="wb_confidence_coefficient"]'
        )
        assert coefficient_input.get_attribute("min") == "0"
        assert coefficient_input.get_attribute("max") == "1"
        page.locator("[data-inventory-balance-preset]").select_option("actionable")
        page.locator('[data-inventory-balance-column-visible="quality"]').uncheck()
        page.locator('[data-inventory-balance-column-up="known_stock_units"]').click()
        page.locator("[data-inventory-balance-settings-save]").click()
        page.wait_for_timeout(50)
        page.locator("[data-inventory-balance-select-all]").click()
        assert page.locator("[data-inventory-balance-apply]").is_enabled()

        override = page.locator('[data-inventory-balance-override="101:8001:search"]')
        override.fill("725.25")
        override.blur()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-status]').textContent.includes('ibc_browser')"
        )
        page.locator("[data-inventory-balance-apply]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_visible()
        confirmation = page.locator("[data-inventory-balance-confirm-body]").inner_text()
        assert "SKU: 1" in confirmation
        assert "campaign targets: 1" in confirmation
        assert "WB PATCH недоступны" in confirmation
        page.locator("[data-inventory-balance-confirm-start]").click()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress-summary]') && document.querySelector('[data-inventory-balance-progress-summary]').textContent.includes('100%')"
        )
        assert "succeeded" in page.locator("[data-inventory-balance-progress-body]").inner_text()
        assert "WB PATCH: нет" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert page.locator("[data-inventory-balance-progress]").get_attribute("data-state") == "completed"
        assert page.locator("[data-inventory-balance-progress-fill]").get_attribute("style") == "width: 100%;"
        assert page.locator("[data-inventory-balance-progress-spinner]").is_hidden()
        assert "ibj_browser" in page.locator("[data-inventory-balance-registry-body]").inner_text()
        assert "manifest sha256:manifest-browser" in page.locator("[data-inventory-balance-registry-body]").inner_text()
        page.locator("[data-inventory-balance-refresh]").click()
        page.wait_for_timeout(100)
        assert "100%" in page.locator("[data-inventory-balance-progress-summary]").inner_text()

        browser.close()

    assert not [item for item in requests if item[0] == "PATCH"]
    override_requests = [item for item in requests if item[1].endswith("/override")]
    assert override_requests and override_requests[-1][2]["manual_target_bid_rub"] == 725.25
    start_requests = [item for item in requests if item[1].endswith("/apply-jobs")]
    assert start_requests and start_requests[-1][2]["mode"] == "dry_run"
    assert start_requests[-1][2]["confirmed"] is True
    settings_requests = [item for item in requests if item[1].endswith("/inventory-balance/settings")]
    assert settings_requests and settings_requests[-1][2]["table"]["preset"] == "actionable"
    assert settings_requests[-1][2]["calculation"]["wb_confidence_coefficient"] == 0.5
    assert "quality" not in settings_requests[-1][2]["table"]["visible_columns"]
    assert settings_requests[-1][2]["table"]["column_order"][2] == "known_stock_units"
    registry_requests = [item for item in requests if item[1].endswith("/calculations?limit=20")]
    assert registry_requests, requests
    assert not console_errors, console_errors
    print("sku_inventory_balance_browser_smoke: ok")


def _calculation() -> dict:
    cpc = {
        "target_key": "101:9001:recommendations",
        "nm_id": 101,
        "advert_id": 9001,
        "campaign_name": "new cpc recommendations",
        "campaign_group": "new_cpc",
        "payment_type": "cpc",
        "placement": "recommendations",
        "cpo_rub": 30,
        "current_bid_rub": 5,
        "calculated_target_bid_rub": 4,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 4,
        "identity_valid": True,
        "manual_override_allowed": True,
        "can_apply": False,
        "allocation_action": "hold_other_group",
        "recommendation_quality": "complete",
    }
    cpm = {
        "target_key": "101:8001:search",
        "nm_id": 101,
        "advert_id": 8001,
        "campaign_name": "old cpm search",
        "campaign_group": "old_cpm",
        "payment_type": "cpm",
        "placement": "search",
        "cpo_rub": 80,
        "current_bid_rub": 1000,
        "calculated_target_bid_rub": 800,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 800,
        "identity_valid": True,
        "manual_override_allowed": True,
        "can_apply": True,
        "allocation_action": "decrease_less_efficient_group",
        "recommendation_quality": "complete",
    }
    return {
        "contract_name": "sheet_vitrina_v1_sku_inventory_balance/v2",
        "calculation_id": "ibc_browser",
        "previous_calculation_id": "ibc_previous",
        "created_at": "2026-08-26T08:00:00+00:00",
        "source_digest": "sha256:browser",
        "formula_version": "sku_inventory_balance_conservative_pace_v2",
        "registry_immutable": True,
        "overrides_are_separate": True,
        "automatic_ml_or_training": False,
        "rows": [
            {
                "nm_id": 101,
                "name": "Deficit SKU",
                "our_sku": "DEF",
                "status": "Дефицит",
                "quality": "complete",
                "quality_warnings": [
                    "Использован официальный агрегат WB по SKU без раскладки по складам и регионам"
                ],
                "known_stock_units": 50,
                "stock_wb_units": 100,
                "wb_confidence_coefficient": 0.5,
                "confidence_adjusted_wb_units": 50,
                "wb_stock_evidence": {
                    "mode": "aggregate_per_sku_total",
                    "quality": "exact_aggregate_total",
                    "warehouse_granularity_complete": False,
                    "incident_projection_applied": False,
                    "raw_rows_digest": "sha256:" + "b" * 64,
                },
                "current_daily_sales": 10,
                "target_daily_sales": 8,
                "pace_change_pct": -20,
                "days_cover": 5,
                "bottleneck_date": "2026-09-10",
                "next_inbound": {"date": "2026-09-10", "quantity": 100},
                "subsequent_inbound": None,
                "campaign_recommendations": [cpc, cpm],
                "new_cpc_campaigns": [cpc],
                "old_cpm_campaigns": [cpm],
                "select_available": True,
            }
        ],
    }


def _job(state: str, terminal: int) -> dict:
    return {
        "job_id": "ibj_browser",
        "calculation_id": "ibc_browser",
        "mode": "dry_run",
        "state": state,
        "progress": {
            "total": 1,
            "terminal": terminal,
            "percent": terminal * 100,
            "states": {"pending": 1 - terminal, "succeeded": terminal},
        },
        "sku_states": [{"nm_id": 101, "state": "succeeded" if terminal else "pending", "target_count": 1}],
        "items": [],
        "external_writes": False,
        "wb_patch_called": False,
    }


def _registry(job: dict | None) -> dict:
    jobs = []
    if job is not None:
        jobs.append(
            {
                "job_id": job["job_id"],
                "mode": "dry_run",
                "state": job["state"],
                "apply_manifest_digest": "sha256:manifest-browser",
                "target_count": job["progress"]["total"],
                "terminal_count": job["progress"]["terminal"],
            }
        )
    return {
        "contract_name": "sheet_vitrina_v1_inventory_balance_registry/v1",
        "items": [
            {
                "calculation_id": "ibc_browser",
                "created_at": "2026-08-26T08:00:00+00:00",
                "created_by": "operator",
                "previous_calculation_id": "ibc_previous",
                "row_count": 1,
                "apply_protocols": [
                    {"protocol": "inventory_balance_apply_job/v1", "mode": "dry_run"},
                    {"protocol": "inventory_balance_live_wb_boundary/v1", "mode": "live_wb", "available": False},
                ],
                "apply_jobs": jobs,
            }
        ],
    }


if __name__ == "__main__":
    main()
