#!/usr/bin/env python3
"""Browser smoke for SKU inventory balance; all upstream operations are fake."""

from __future__ import annotations

from copy import deepcopy
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
    new_calculation = deepcopy(calculation)
    new_calculation["calculation_id"] = "ibc_browser_new"
    new_calculation["created_at"] = "2026-08-30T09:10:00+00:00"
    new_calculation["as_of_date"] = "2026-08-30"
    new_calculation["rows"][0]["name"] = "Newest Deficit SKU"
    current_calculation = [calculation]
    live_available = [True]
    latest_get_count = [0]
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
                latest_get_count[0] += 1
                current_calculation[0]["apply_capability"]["live_wb_available"] = live_available[0]
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "ok",
                            "settings": settings,
                            "calculation": current_calculation[0],
                            "apply_job": latest_job[0],
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
                route.fulfill(status=503, content_type="application/json", body='{"error":"delayed registry"}')
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
                    current_calculation[0] = new_calculation
                    latest_operation[0] = _operation(
                        str((latest_operation[0] or {}).get("operation_id") or ""),
                        state="succeeded",
                        result=current_calculation[0],
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
                target = next(
                    target
                    for row in current_calculation[0]["rows"]
                    for target in row["campaign_recommendations"]
                    if target["target_key"] == body.get("target_key")
                )
                manual_target = body.get("manual_target_bid_rub")
                target["manual_target_bid_rub"] = manual_target
                target["final_target_bid_rub"] = (
                    manual_target
                    if manual_target is not None
                    else target["calculated_target_bid_rub"]
                )
                target["can_apply"] = (
                    target["final_target_bid_rub"] is not None
                    and float(target["final_target_bid_rub"])
                    != float(target["current_bid_rub"])
                )
                row = next(row for row in current_calculation[0]["rows"] if row["nm_id"] == target["nm_id"])
                row["select_available"] = any(item.get("can_apply") for item in row["campaign_recommendations"])
                group_key = "new_cpc_campaigns" if target["campaign_group"] == "new_cpc" else "old_cpm_campaigns"
                row[group_key] = [item for item in row["campaign_recommendations"] if item["campaign_group"] == target["campaign_group"]]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(current_calculation[0]))
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
        status_text = page.locator("[data-inventory-balance-status]").inner_text()
        assert (
            "Последний расчёт:" in status_text
            and "данные на 26.08.2026" in status_text
            and "источник 26.08.2026" in status_text
        ), (
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
        assert "WB общим итогом × 0.5" in normalized_balance_text
        assert "Сейчас 1 000" in normalized_balance_text
        assert "Сейчас 5" in normalized_balance_text
        assert "Сейчас 10" in normalized_balance_text
        assert "Поддерживающая CPC переизбытка" in normalized_balance_text
        assert "insufficient_stats" not in normalized_balance_text
        assert "hold_conservative" not in normalized_balance_text
        assert "advert_id" not in normalized_balance_text
        assert page.locator("[data-inventory-balance-details-toggle]").count() == 0
        assert page.locator("[data-inventory-balance-details-row]").count() == 0
        assert page.locator("[data-inventory-balance-registry]").count() == 0
        assert page.locator("[data-inventory-balance-refresh]").count() == 0
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
        assert "CPC · текущая / новая" in page.locator("[data-inventory-balance-head]").inner_text()
        assert "CPM · текущая / новая" in page.locator("[data-inventory-balance-head]").inner_text()
        assert page.locator("[data-inventory-balance-xlsx]").is_enabled()
        assert page.locator('[data-inventory-balance-nm-id="202"] [data-inventory-balance-override]').count() == 3
        assert page.locator('[data-inventory-balance-override="101:8001:search"]').input_value() == "800"
        unavailable_override = page.locator('[data-inventory-balance-override="303:9303:search"]')
        assert unavailable_override.input_value() == ""
        assert unavailable_override.get_attribute("placeholder") == "Новая"
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

        page.locator("[data-inventory-balance-calculate]").click()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-calculation-progress]').getAttribute('data-state') === 'running'"
        )
        assert page.locator("[data-inventory-balance-calculate]").is_disabled()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-calculation-progress]').getAttribute('data-state') === 'succeeded'"
        )
        assert "Failed to fetch" not in page.locator("[data-inventory-balance-error]").inner_text()
        assert "сохранён в реестре" in page.locator(
            "[data-inventory-balance-calculation-progress-summary]"
        ).inner_text()
        assert "Newest Deficit SKU" in page.locator("[data-inventory-balance-body]").inner_text()
        assert "данные на 30.08.2026" in page.locator("[data-inventory-balance-status]").inner_text()
        assert page.locator("[data-inventory-balance-calculate]").is_enabled()
        completed_gets = latest_get_count[0]
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.locator("[data-inventory-balance-body]").wait_for()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-body]').textContent.includes('Newest Deficit SKU')"
        )
        assert latest_get_count[0] > completed_gets
        before_reopen = latest_get_count[0]
        live_available[0] = False
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_timeout(100)
        assert latest_get_count[0] > before_reopen
        assert "live WB недоступен" in page.locator("[data-inventory-balance-status]").inner_text()
        assert "Live-применение WB сейчас недоступно" in page.locator("[data-inventory-balance-apply-reason]").inner_text()
        live_available[0] = True
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_timeout(100)

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

        override = page.locator('[data-inventory-balance-override="101:8001:search"]')
        assert override.input_value() == "800"
        override_count_before = len([item for item in requests if item[1].endswith("/override")])
        override.fill("725.25")
        page.wait_for_timeout(650)
        assert len([item for item in requests if item[1].endswith("/override")]) == override_count_before + 1
        assert override.input_value() == "725.25"
        page.locator("[data-inventory-balance-preset]").select_option("all")
        excess_override = page.locator(
            '[data-inventory-balance-override="202:9202:search"]'
        )
        excess_override.fill("")
        page.wait_for_timeout(650)
        assert page.locator(
            '[data-inventory-balance-override="202:9202:search"]'
        ).input_value() == ""
        assert "(1)" in page.locator("[data-inventory-balance-apply]").inner_text()
        page.locator(
            '[data-inventory-balance-override="202:9202:search"]'
        ).fill("1701")
        page.locator(
            '[data-inventory-balance-override="202:9202:search"]'
        ).press("Enter")
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-apply]').textContent.includes('(2)')"
        )
        assert "(2)" in page.locator("[data-inventory-balance-apply]").inner_text()
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
        assert "10 → 1 701 ₽/клик" in normalized_confirmation
        assert "Повышение на 1 691 ₽ превышает прежний контрольный порог 100 ₽" in normalized_confirmation
        assert "Новая ставка 1 701 ₽ превышает прежний контрольный потолок 1 000 ₽" in normalized_confirmation
        assert "Повышение на 16 910% превышает прежний контрольный порог 50%" in normalized_confirmation
        assert "Не включено пустых, равных текущим или недоступных целей: 3" in confirmation
        assert "эти точные ставки будут отправлены в WB" in confirmation
        assert "без промежуточных шагов" in confirmation
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
        assert page.locator("[data-inventory-balance-confirm]").is_visible()
        assert "Применяем ставки" in page.locator("[data-inventory-balance-confirm-title]").inner_text()
        assert "Применяем ставки" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.locator("[data-inventory-balance-confirm]").wait_for(state="visible")
        assert page.locator("[data-inventory-balance-confirm]").is_visible()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress]').getAttribute('data-state') === 'completed'"
        )
        assert "Применено" in page.locator("[data-inventory-balance-confirm-body]").inner_text()
        assert "2 применено" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert page.locator("[data-inventory-balance-progress]").get_attribute("data-state") == "completed"
        assert page.locator("[data-inventory-balance-progress-fill]").get_attribute("style") == "width: 100%;"
        assert page.locator("[data-inventory-balance-progress-spinner]").is_hidden()
        assert "применено" in page.locator('[data-inventory-balance-nm-id="101"]').inner_text()
        assert "Все ставки применены" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        page.locator("[data-inventory-balance-confirm-cancel]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_hidden()
        page.locator("[data-inventory-balance-progress-open]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_visible()

        latest_job[0] = _job("completed_with_errors", 2)
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress-summary]').textContent.includes('Ставки применены частично')"
        )
        partial_summary = page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert "Ставки применены частично" in partial_summary
        assert "1 применено" in partial_summary
        assert "1 требует проверки" in partial_summary
        if page.locator("[data-inventory-balance-confirm]").is_hidden():
            page.locator("[data-inventory-balance-progress-open]").click()
        assert "Требуется проверка" in page.locator("[data-inventory-balance-confirm-body]").inner_text()

        zero_job = _job("completed_with_errors", 2)
        zero_job["progress"].update({"applied": 0, "failed": 2, "needs_check": 0})
        zero_job["items"][0].update({
            "current_bid_rub": 1500,
            "final_target_bid_rub": 1701,
            "state": "failed",
            "phase": "Не применено",
            "error_code": "safety_guard",
            "error": "requested_bid_rub exceeds absolute increase threshold",
        })
        zero_job["items"][1].update({"state": "skipped", "phase": "Не отправлено", "error": "Контрольная ставка не прошла проверку"})
        latest_job[0] = zero_job
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress-summary]').textContent.includes('Ничего не применено')"
        )
        assert "Ничего не применено" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        if page.locator("[data-inventory-balance-confirm]").is_hidden():
            page.locator("[data-inventory-balance-progress-open]").click()
        zero_text = page.locator("[data-inventory-balance-confirm-body]").inner_text().replace("\xa0", " ")
        assert "Повышение на 201 ₽ превышает прежний контрольный порог 100 ₽" in zero_text
        assert "Ставка в WB не отправлялась" in zero_text

        latest_job[0] = _job("stalled", 1)
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress-summary]').textContent.includes('Процесс приостановлен')"
        )
        assert "Процесс приостановлен" in page.locator(
            "[data-inventory-balance-progress-summary]"
        ).inner_text()
        assert page.locator("[data-inventory-balance-progress-resume]").is_visible()

        browser.close()

    assert not [item for item in requests if item[0] == "PATCH"]
    override_requests = [item for item in requests if item[1].endswith("/override")]
    assert any(
        item[2].get("target_key") == "101:8001:search"
        and item[2].get("manual_target_bid_rub") == 725.25
        for item in override_requests
    )
    start_requests = [item for item in requests if item[1].endswith("/apply-jobs")]
    assert start_requests and start_requests[-1][2]["mode"] == "live_wb"
    assert start_requests[-1][2]["confirmed"] is True
    assert start_requests[-1][2]["target_keys"] == [
        "101:8001:search",
        "202:9202:search",
    ]
    settings_requests = [item for item in requests if item[1].endswith("/inventory-balance/settings")]
    assert settings_requests and settings_requests[-1][2]["table"]["preset"] == "actionable"
    assert settings_requests[-1][2]["calculation"]["wb_confidence_coefficient"] == 0.5
    assert "quality" not in settings_requests[-1][2]["table"]["visible_columns"]
    assert settings_requests[-1][2]["table"]["column_order"][2] == "known_stock_units"
    registry_requests = [item for item in requests if item[1].endswith("/calculations?limit=20")]
    assert not registry_requests, registry_requests
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
        "calculated_target_bid_rub": 1701,
        "manual_target_bid_rub": None,
        "final_target_bid_rub": 1701,
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
        "as_of_date": "2026-08-26",
        "source_generated_at": "2026-08-26T07:55:00+00:00",
        "ads_period": {"date_from": "2026-08-20", "date_to": "2026-08-25"},
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
            "owner_confirmation_policy": {
                "contract_name": "inventory_balance_owner_confirmed_bid_thresholds/v1",
                "safety_threshold_policy": "owner_confirmed_balance",
                "thresholds": {
                    "absolute_max_bid_rub": 1000,
                    "max_absolute_increase_rub": 100,
                    "max_percent_increase": 50,
                },
                "direct_submit": True,
                "staircase_submit": False,
            },
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
                (202, 9202, "Продвижение CPC", "cpc", "search", 10, 1701),
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
