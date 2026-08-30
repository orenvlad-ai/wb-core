#!/usr/bin/env python3
"""Browser smoke for SKU inventory balance; all upstream operations are fake."""

from __future__ import annotations

import json
import os
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
    latest_operation: list[dict | None] = [None]
    operation_status_reads = [0]
    apply_status_reads = [0]

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
                            "calculation_operation": latest_operation[0],
                            "apply_capability": {
                                "live_wb_available": True,
                                "wb_patch_reachable": True,
                            },
                        }
                    ),
                )
                return
            if path == base + "/calculations?limit=20":
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_registry(latest_job[0])))
                return
            if path == base + "/calculate":
                latest_operation[0] = _operation(
                    str(body.get("operation_id") or ""),
                    state="running",
                    result=None,
                    percent=45,
                )
                route.abort("connectionfailed")
                return
            if path.startswith(base + "/operations/"):
                operation_status_reads[0] += 1
                if operation_status_reads[0] >= 2:
                    latest_operation[0] = _operation(
                        str((latest_operation[0] or {}).get("operation_id") or ""),
                        state="succeeded",
                        result=calculation,
                        percent=100,
                    )
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(latest_operation[0]),
                )
                return
            if path == base + "/settings":
                settings["revision"] += 1
                settings["calculation"] = body.get("calculation") or settings["calculation"]
                settings["table"] = body.get("table") or settings["table"]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
                return
            if path.endswith("/override"):
                target = calculation["rows"][0]["campaign_recommendations"][1]
                manual_target = body.get("manual_target_bid_rub")
                target["manual_target_bid_rub"] = manual_target
                target["final_target_bid_rub"] = (
                    manual_target
                    if manual_target is not None
                    else target["calculated_target_bid_rub"]
                )
                calculation["rows"][0]["old_cpm_campaigns"][0] = target
                route.fulfill(status=200, content_type="application/json", body=json.dumps(calculation))
                return
            if path == base + "/apply-jobs":
                latest_job[0] = _job("pending", 0)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(latest_job[0]))
                return
            if path == base + "/apply-jobs/ibj_browser":
                apply_status_reads[0] += 1
                latest_job[0] = _job(
                    "running" if apply_status_reads[0] == 1 else "completed",
                    0 if apply_status_reads[0] == 1 else 2,
                )
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
        normalized_balance_text = balance_body_text.replace("\xa0", " ")
        assert "Дефицит" in balance_body_text, (
            balance_body_text,
            page.locator("[data-inventory-balance-status]").inner_text(),
            page.locator("[data-inventory-balance-error]").inner_text(),
            console_errors,
        )
        assert "WB только агрегатом × 0.5" in normalized_balance_text
        assert "Снизить 1 000 → 800 ₽/1000 показов (−20%)" in normalized_balance_text
        assert "Оставить 5 ₽/клик" in normalized_balance_text
        assert "Повысить 10 → 15 ₽/клик (+50%)" in normalized_balance_text
        assert "Ещё 1 кампания — в подробностях" in normalized_balance_text
        assert "Ставка не рассчитана" in normalized_balance_text
        assert "Мало статистики рекламы · Темп менять не нужно" in normalized_balance_text
        assert "insufficient_stats" not in normalized_balance_text
        assert "hold_conservative" not in normalized_balance_text
        assert "advert_id" not in normalized_balance_text
        assert page.locator(".inventory-balance-quality.exact").count() == 2
        assert page.locator(".inventory-balance-quality.partial").count() == 1
        assert page.locator(".inventory-balance-quality.insufficient").count() == 1
        assert page.locator(".inventory-balance-main-row").count() == 4
        row_heights = page.locator(".inventory-balance-main-row").evaluate_all(
            "rows => rows.map(row => row.getBoundingClientRect().height)"
        )
        assert all(height <= 62 for height in row_heights), row_heights
        visible_line_counts = page.locator(
            ".inventory-balance-main-row .inventory-balance-two-line"
        ).evaluate_all("nodes => nodes.map(node => node.children.length)")
        assert visible_line_counts and all(count <= 2 for count in visible_line_counts)
        assert page.locator("th.inventory-balance-select-column").evaluate(
            "node => getComputedStyle(node).position"
        ) == "sticky"
        table_shell = page.locator("[data-inventory-balance-table]").locator("xpath=..")
        table_shell.evaluate("node => { node.scrollLeft = node.scrollWidth; }")
        page.wait_for_timeout(50)
        sticky_select_box = page.locator(
            ".inventory-balance-main-row td.inventory-balance-select-column"
        ).first.bounding_box()
        sticky_product_box = page.locator(
            ".inventory-balance-main-row td.inventory-balance-product-column"
        ).first.bounding_box()
        shell_box = table_shell.bounding_box()
        assert sticky_select_box and sticky_product_box and shell_box
        assert abs(sticky_select_box["x"] - shell_box["x"]) <= 2
        assert abs(
            sticky_product_box["x"]
            - (sticky_select_box["x"] + sticky_select_box["width"])
        ) <= 2
        table_shell.evaluate("node => { node.scrollLeft = 0; }")
        assert "Новые CPC" in page.locator("[data-inventory-balance-head]").inner_text()
        assert "Старые CPM" in page.locator("[data-inventory-balance-head]").inner_text()
        assert page.locator("[data-inventory-balance-xlsx]").is_enabled()
        screenshot_path = os.environ.get("SKU_INVENTORY_BALANCE_SCREENSHOT", "").strip()
        if screenshot_path:
            page.screenshot(path=screenshot_path, full_page=True)
        screenshot_right_path = os.environ.get(
            "SKU_INVENTORY_BALANCE_SCREENSHOT_RIGHT", ""
        ).strip()
        if screenshot_right_path:
            page.locator("[data-inventory-balance-table]").evaluate(
                "table => { table.parentElement.scrollLeft = table.parentElement.scrollWidth; }"
            )
            page.wait_for_timeout(50)
            page.screenshot(path=screenshot_right_path, full_page=True)
            page.locator("[data-inventory-balance-table]").evaluate(
                "table => { table.parentElement.scrollLeft = 0; }"
            )

        deficit_row = page.locator('[data-inventory-balance-nm-id="101"]')
        assert "Использован официальный агрегат" not in deficit_row.inner_text()
        deficit_row.locator("[data-inventory-balance-details-toggle]").click()
        deficit_details = page.locator('[data-inventory-balance-details-row="101"]')
        assert deficit_details.is_visible()
        deficit_detail_text = deficit_details.inner_text()
        assert "Использован официальный агрегат WB" in deficit_detail_text
        assert "advert_id 8001" in deficit_detail_text
        assert "placement search" in deficit_detail_text
        assert "CPO 80 ₽/заказ" in deficit_detail_text
        deficit_details.locator("details").evaluate_all(
            "nodes => nodes.forEach(node => { node.open = true; })"
        )
        expanded_deficit_detail_text = deficit_details.inner_text()
        assert "shipment-deficit-1" in expanded_deficit_detail_text
        assert "sha256:wb-deficit" in expanded_deficit_detail_text
        assert "recommendation_quality" in expanded_deficit_detail_text
        assert "calculated_target_bid_rub" in expanded_deficit_detail_text

        insufficient_toggle = page.locator(
            '[data-inventory-balance-details-toggle="303"]'
        )
        insufficient_toggle.focus()
        insufficient_toggle.press("Enter")
        insufficient_details = page.locator('[data-inventory-balance-details-row="303"]')
        assert insufficient_details.is_visible()
        insufficient_details.locator("details").evaluate_all(
            "nodes => nodes.forEach(node => { node.open = true; })"
        )
        assert "insufficient_stats" in insufficient_details.inner_text()
        unavailable_override = insufficient_details.locator(
            '[data-inventory-balance-override="303:9303:search"]'
        )
        assert unavailable_override.input_value() == ""
        assert unavailable_override.get_attribute("placeholder") == "Введите вручную"

        page.locator("[data-inventory-balance-calculate]").click()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-calculation-progress]').getAttribute('data-state') === 'running'"
        )
        assert page.locator("[data-inventory-balance-calculate]").is_disabled()
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-calculation-progress]').getAttribute('data-state') === 'succeeded'"
        )
        assert "Failed to fetch" not in page.locator("[data-inventory-balance-error]").inner_text()
        assert "сохранён в реестре" in page.locator(
            "[data-inventory-balance-calculation-progress-summary]"
        ).inner_text()
        assert page.locator("[data-inventory-balance-calculate]").is_enabled()

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
        assert "(2)" in page.locator("[data-inventory-balance-apply]").inner_text()

        page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-details-toggle]'
        ).click()
        override = page.locator('[data-inventory-balance-override="101:8001:search"]')
        assert override.input_value() == "800"
        override.fill("725.25")
        override.blur()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-details-row=\"101\"]') && document.querySelector('[data-inventory-balance-details-row=\"101\"]').textContent.includes('Изменено вручную')"
        )
        assert "Изменено вручную" in page.locator(
            '[data-inventory-balance-details-row="101"]'
        ).inner_text()
        override.fill("800")
        override.blur()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-details-row=\"101\"]') && !document.querySelector('[data-inventory-balance-details-row=\"101\"]').textContent.includes('Изменено вручную')"
        )
        assert "Изменено вручную" not in page.locator(
            '[data-inventory-balance-details-row="101"]'
        ).inner_text()
        assert override.input_value() == "800"
        override.fill("725.25")
        override.blur()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-details-row=\"101\"]') && document.querySelector('[data-inventory-balance-details-row=\"101\"]').textContent.includes('Изменено вручную')"
        )
        page.locator("[data-inventory-balance-preset]").select_option("all")
        page.locator("[data-inventory-balance-manual-pending]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_visible()
        manual_confirmation = page.locator(
            "[data-inventory-balance-confirm-body]"
        ).inner_text()
        assert "Изменить вручную на портале" in page.locator(
            "[data-inventory-balance-confirm-title]"
        ).inner_text()
        assert "Реестр будет ждать доказанное изменение 24 часа" in manual_confirmation
        assert "Зафиксировать ожидание" in page.locator(
            "[data-inventory-balance-confirm-start]"
        ).inner_text()
        page.locator("[data-inventory-balance-confirm-close]").first.click()
        page.locator("[data-inventory-balance-apply]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_visible()
        confirmation = page.locator("[data-inventory-balance-confirm-body]").inner_text()
        normalized_confirmation = confirmation.replace("\xa0", " ")
        assert "Выбрано SKU\n2" in confirmation
        assert "Ставок\n2" in confirmation
        assert "Повышений\n1" in confirmation
        assert "Понижений\n1" in confirmation
        assert "1 000 → 725,25 ₽/1000 показов" in normalized_confirmation
        assert "10 → 15 ₽/клик" in normalized_confirmation
        assert "Не включено целей без точного изменения: 3" in confirmation
        assert "ставки действительно изменятся в WB" in confirmation
        assert "контрольную ставку" in confirmation
        screenshot_modal_path = os.environ.get(
            "SKU_INVENTORY_BALANCE_SCREENSHOT_MODAL", ""
        ).strip()
        if screenshot_modal_path:
            page.screenshot(path=screenshot_modal_path, full_page=True)
        page.locator("[data-inventory-balance-confirm-start]").click()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress]').getAttribute('data-state') === 'running'"
        )
        assert "Применяем ставки" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress]').getAttribute('data-state') === 'completed'"
        )
        assert "Применено" in page.locator("[data-inventory-balance-progress-body]").inner_text()
        assert "2 применено" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert page.locator("[data-inventory-balance-progress]").get_attribute("data-state") == "completed"
        assert page.locator("[data-inventory-balance-progress-fill]").get_attribute("style") == "width: 100%;"
        assert page.locator("[data-inventory-balance-progress-spinner]").is_hidden()
        assert "ibj_browser" in page.locator("[data-inventory-balance-registry-body]").inner_text()
        assert "manifest sha256:manifest-browser" in page.locator("[data-inventory-balance-registry-body]").inner_text()
        page.locator("[data-inventory-balance-refresh]").click()
        page.wait_for_timeout(100)
        assert "Все ставки применены" in page.locator("[data-inventory-balance-progress-summary]").inner_text()

        latest_job[0] = _job("completed_with_errors", 2)
        page.locator("[data-inventory-balance-refresh]").click()
        page.wait_for_timeout(100)
        partial_summary = page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert "Ставки применены частично" in partial_summary
        assert "1 применено" in partial_summary
        assert "1 требует проверки" in partial_summary
        assert "Требуется проверка" in page.locator(
            "[data-inventory-balance-progress-body]"
        ).inner_text()

        latest_job[0] = _job("stalled", 1)
        page.locator("[data-inventory-balance-refresh]").click()
        page.wait_for_timeout(100)
        assert "Процесс приостановлен" in page.locator(
            "[data-inventory-balance-progress-summary]"
        ).inner_text()
        assert page.locator("[data-inventory-balance-progress-resume]").is_visible()

        browser.close()

    assert not [item for item in requests if item[0] == "PATCH"]
    override_requests = [item for item in requests if item[1].endswith("/override")]
    assert override_requests and override_requests[-1][2]["manual_target_bid_rub"] == 725.25
    assert any(item[2]["manual_target_bid_rub"] is None for item in override_requests)
    start_requests = [item for item in requests if item[1].endswith("/apply-jobs")]
    assert start_requests and start_requests[-1][2]["mode"] == "live_wb"
    assert start_requests[-1][2]["confirmed"] is True
    settings_requests = [item for item in requests if item[1].endswith("/inventory-balance/settings")]
    assert settings_requests and settings_requests[-1][2]["table"]["preset"] == "actionable"
    assert settings_requests[-1][2]["calculation"]["wb_confidence_coefficient"] == 0.5
    assert "quality" not in settings_requests[-1][2]["table"]["visible_columns"]
    assert settings_requests[-1][2]["table"]["column_order"][2] == "known_stock_units"
    registry_requests = [item for item in requests if item[1].endswith("/calculations?limit=20")]
    assert registry_requests, requests
    calculate_requests = [item for item in requests if item[1].endswith("/inventory-balance/calculate")]
    assert len(calculate_requests) == 1, calculate_requests
    assert calculate_requests[0][2]["operation_id"].startswith("ibop_")
    assert calculate_requests[0][2]["idempotency_key"].startswith("ibkey_")
    assert operation_status_reads[0] >= 2
    assert apply_status_reads[0] >= 2
    unexpected_console_errors = [
        item for item in console_errors if "ERR_CONNECTION_FAILED" not in item
    ]
    assert not unexpected_console_errors, unexpected_console_errors
    assert any("ERR_CONNECTION_FAILED" in item for item in console_errors), console_errors
    print("sku_inventory_balance_browser_smoke: ok")


def _calculation() -> dict:
    deficit_cpc = {
        "target_key": "101:9001:recommendations",
        "nm_id": 101,
        "advert_id": 9001,
        "campaign_name": "Поддерживающая CPC",
        "campaign_group": "new_cpc",
        "payment_type": "cpc",
        "placement": "recommendations",
        "cpo_rub": 30,
        "current_bid_rub": 5,
        "calculated_target_bid_rub": 5,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 5,
        "identity_valid": True,
        "manual_override_allowed": True,
        "can_apply": False,
        "allocation_action": "hold_other_group",
        "recommendation_quality": "complete",
    }
    deficit_cpm = {
        "target_key": "101:8001:search",
        "nm_id": 101,
        "advert_id": 8001,
        "campaign_name": "Снижение дефицитной CPM",
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
    excess_cpc = {
        "target_key": "202:9202:search",
        "nm_id": 202,
        "advert_id": 9202,
        "campaign_name": "Продвижение складского избытка с длинным названием",
        "campaign_group": "new_cpc",
        "payment_type": "cpc",
        "placement": "search",
        "cpo_rub": 25,
        "current_bid_rub": 10,
        "calculated_target_bid_rub": 15,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 15,
        "identity_valid": True,
        "manual_override_allowed": True,
        "can_apply": True,
        "allocation_action": "increase_more_efficient_group",
        "recommendation_quality": "complete",
    }
    excess_cpm = {
        "target_key": "202:8202:recommendations",
        "nm_id": 202,
        "advert_id": 8202,
        "campaign_name": "Поддерживающая CPM",
        "campaign_group": "old_cpm",
        "payment_type": "cpm",
        "placement": "recommendations",
        "cpo_rub": 70,
        "current_bid_rub": 1200,
        "calculated_target_bid_rub": 1200,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 1200,
        "identity_valid": True,
        "manual_override_allowed": True,
        "can_apply": False,
        "allocation_action": "hold_other_group",
        "recommendation_quality": "complete",
    }
    excess_cpc_hold = {
        **excess_cpc,
        "target_key": "202:9203:recommendations",
        "advert_id": 9203,
        "campaign_name": "Поддерживающая CPC переизбытка",
        "placement": "recommendations",
        "current_bid_rub": 8,
        "calculated_target_bid_rub": 8,
        "final_target_bid_rub": 8,
        "can_apply": False,
        "allocation_action": "hold_other_group",
    }
    insufficient_cpc = {
        "target_key": "303:9303:search",
        "nm_id": 303,
        "advert_id": 9303,
        "campaign_name": "Кампания без сопоставимой статистики",
        "campaign_group": "new_cpc",
        "payment_type": "cpc",
        "placement": "search",
        "cpo_rub": None,
        "orders": None,
        "spend_rub": 100,
        "current_bid_rub": 12,
        "calculated_target_bid_rub": 12,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 12,
        "identity_valid": True,
        "manual_override_allowed": True,
        "can_apply": False,
        "allocation_action": "hold_conservative",
        "recommendation_quality": "insufficient_stats",
        "calculation_reason": "консервативно без изменения: недостаточно сопоставимой CPO evidence",
    }
    insufficient_cpm = {
        **insufficient_cpc,
        "target_key": "303:8303:recommendations",
        "advert_id": 8303,
        "campaign_name": "Вторая кампания без статистики",
        "campaign_group": "old_cpm",
        "payment_type": "cpm",
        "placement": "recommendations",
        "current_bid_rub": 1500,
        "calculated_target_bid_rub": 1500,
        "final_target_bid_rub": 1500,
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
        "apply_capability": {
            "live_wb_available": True,
            "wb_patch_reachable": True,
            "batch_size": 10,
            "canary_required": True,
            "reload_safe": True,
        },
        "automatic_ml_or_training": False,
        "lineage": {
            "sales_evidence_window": {"sales_period_days": 7, "date_from": "2026-08-20"},
            "supplier_eta_evidence": {
                "method": "empirical_last_completed_shipments",
                "shipment_ids": ["done-1", "done-2", "done-3"],
                "digest": "sha256:eta-browser",
            },
        },
        "rows": [
            {
                "nm_id": 101,
                "name": "Deficit SKU",
                "our_sku": "DEF",
                "status": "Дефицит",
                "quality": "partial",
                "quality_warnings": [
                    "Использован официальный агрегат WB по SKU без раскладки по складам и регионам"
                ],
                "known_stock_units": 50,
                "stock_wb_units": 100,
                "wb_confidence_coefficient": 0.5,
                "confidence_adjusted_wb_units": 50,
                "wb_stock_evidence": {
                    "source_contract": "official_current_stock_snapshot/v1",
                    "mode": "aggregate_per_sku_total",
                    "quality": "exact_aggregate_total",
                    "warehouse_granularity_complete": False,
                    "incident_projection_applied": False,
                    "raw_rows_digest": "sha256:wb-deficit",
                },
                "current_daily_sales": 10,
                "target_daily_sales": 8,
                "pace_change_pct": -20,
                "days_cover": 5,
                "bottleneck_date": "2026-09-10",
                "next_inbound": {
                    "date": "2026-09-10",
                    "quantity": 100,
                    "source_ids": ["shipment-deficit-1"],
                },
                "subsequent_inbound": None,
                "milestones": [
                    {
                        "date": "2026-09-10",
                        "quantity": 100,
                        "sources": ["supplier_shipment"],
                        "source_ids": ["shipment-deficit-1"],
                    }
                ],
                "campaign_recommendations": [deficit_cpc, deficit_cpm],
                "new_cpc_campaigns": [deficit_cpc],
                "old_cpm_campaigns": [deficit_cpm],
                "select_available": True,
            },
            {
                "nm_id": 202,
                "name": "Overstock SKU",
                "our_sku": "OVER",
                "status": "Переизбыток",
                "quality": "complete",
                "quality_warnings": [],
                "known_stock_units": 600,
                "wb_confidence_coefficient": 0.5,
                "wb_stock_evidence": {"mode": "warehouse_granular_incident_projection"},
                "current_daily_sales": 8,
                "target_daily_sales": 12,
                "pace_change_pct": 50,
                "days_cover": 75,
                "bottleneck_date": "2026-09-18",
                "next_inbound": {"date": "2026-09-18", "quantity": 200},
                "subsequent_inbound": None,
                "milestones": [{"date": "2026-09-18", "quantity": 200, "source_ids": ["shipment-overstock-1"]}],
                "campaign_recommendations": [excess_cpc, excess_cpc_hold, excess_cpm],
                "new_cpc_campaigns": [excess_cpc, excess_cpc_hold],
                "old_cpm_campaigns": [excess_cpm],
                "select_available": True,
            },
            {
                "nm_id": 303,
                "name": "Balanced SKU",
                "our_sku": "BAL",
                "status": "Баланс",
                "quality": "complete",
                "quality_warnings": [],
                "known_stock_units": 300,
                "wb_confidence_coefficient": 0.5,
                "wb_stock_evidence": {"mode": "warehouse_granular_incident_projection"},
                "current_daily_sales": 10,
                "target_daily_sales": 10,
                "pace_change_pct": 0,
                "days_cover": 30,
                "bottleneck_date": None,
                "next_inbound": {"date": "2026-09-25", "quantity": 100},
                "subsequent_inbound": None,
                "milestones": [{"date": "2026-09-25", "quantity": 100, "source_ids": ["shipment-balanced-1"]}],
                "campaign_recommendations": [insufficient_cpc, insufficient_cpm],
                "new_cpc_campaigns": [insufficient_cpc],
                "old_cpm_campaigns": [insufficient_cpm],
                "select_available": False,
            },
            {
                "nm_id": 404,
                "name": "Unknown supply SKU",
                "our_sku": "UNKNOWN",
                "status": "Недостаточно данных",
                "quality": "unknown",
                "quality_warnings": [
                    "Нет eligible exact production/in_transit поставок; target не рассчитывается"
                ],
                "known_stock_units": 40,
                "wb_confidence_coefficient": 0.5,
                "wb_stock_evidence": {"mode": "warehouse_granular_incident_projection"},
                "current_daily_sales": 4,
                "target_daily_sales": None,
                "pace_change_pct": None,
                "days_cover": 10,
                "bottleneck_date": None,
                "next_inbound": None,
                "subsequent_inbound": None,
                "milestones": [],
                "campaign_recommendations": [],
                "new_cpc_campaigns": [],
                "old_cpm_campaigns": [],
                "select_available": False,
            },
        ],
    }


def _job(state: str, terminal: int) -> dict:
    total = 2
    succeeded = state == "completed"
    partial = state == "completed_with_errors"
    stalled = state == "stalled"
    applied = 1 if partial or stalled else terminal
    needs_check = 1 if partial else 0
    return {
        "job_id": "ibj_browser",
        "calculation_id": "ibc_browser",
        "mode": "live_wb",
        "state": state,
        "progress": {
            "total": total,
            "terminal": terminal,
            "percent": int(terminal / total * 100),
            "states": {"pending": total - terminal, "succeeded": terminal},
            "applied": applied,
            "verifying": 1 if stalled else 0 if succeeded or partial else total,
            "waiting": 0,
            "failed": 0,
            "needs_check": needs_check,
        },
        "sku_states": [
            {"nm_id": nm_id, "state": "succeeded" if terminal else "pending", "target_count": 1}
            for nm_id in (101, 202)
        ],
        "items": [
            {
                "nm_id": nm_id,
                "advert_id": advert_id,
                "campaign_name": campaign,
                "payment_type": payment,
                "placement": placement,
                "current_bid_rub": current,
                "final_target_bid_rub": target,
                "state": (
                    "succeeded"
                    if succeeded or item_index == 0
                    else "ambiguous"
                    if partial
                    else "delayed"
                    if stalled
                    else "verifying"
                ),
                "phase": (
                    "Применено"
                    if succeeded or item_index == 0
                    else "Требуется проверка"
                    if partial
                    else "WB задерживает подтверждение"
                    if stalled
                    else "Проверяем фактические ставки в WB"
                ),
                "error": "WB не подтвердил ставку" if partial and item_index == 1 else "",
            }
            for item_index, (nm_id, advert_id, campaign, payment, placement, current, target) in enumerate((
                (101, 8001, "Снижение дефицитной CPM", "cpm", "search", 1000, 725.25),
                (202, 9202, "Продвижение CPC", "cpc", "search", 10, 15),
            ))
        ],
        "external_writes": True,
        "wb_patch_called": state != "pending",
    }


def _operation(operation_id: str, *, state: str, result: dict | None, percent: int) -> dict:
    phase = "building_evidence" if state == "running" else state
    return {
        "contract_name": "sheet_vitrina_v1_sku_inventory_balance_operation/v1",
        "operation_id": operation_id,
        "state": state,
        "phase": phase,
        "progress": {"percent": percent, "terminal": state in {"succeeded", "failed"}},
        "calculation_id": result.get("calculation_id") if result else None,
        "result": result,
        "error": None,
        "outcome": {"durable_outcome": "calculation_created"} if result else {},
        "created_at": "2026-08-26T08:00:00+00:00",
        "started_at": "2026-08-26T08:00:01+00:00",
        "finished_at": "2026-08-26T08:01:00+00:00" if result else "",
        "updated_at": "2026-08-26T08:01:00+00:00",
        "retryable_by_new_operation": False,
        "blind_resubmit_allowed": False,
    }


def _registry(job: dict | None) -> dict:
    jobs = []
    if job is not None:
        jobs.append(
            {
                "job_id": job["job_id"],
                "mode": "live_wb",
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
                "row_count": 4,
                "apply_protocols": [
                    {"protocol": "inventory_balance_apply_job/v1", "mode": "dry_run"},
                    {"protocol": "inventory_balance_live_wb_boundary/v1", "mode": "live_wb", "available": True},
                ],
                "apply_jobs": jobs,
            }
        ],
    }


if __name__ == "__main__":
    main()
