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
            "column_widths": {
                "new_cpc_campaigns": 600,
                "old_cpm_campaigns": 600,
            },
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
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: console_errors.append(str(error)))

        def route_handler(route):
            request = route.request
            path = request.url.split("balance.test", 1)[-1]
            body = json.loads(request.post_data or "{}") if request.post_data else {}
            requests.append((request.method, path, body))
            if path.split("?", 1)[0] == "/page":
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
                if body.get("manual_target_bid_rub") == 777.77:
                    route.fulfill(
                        status=409,
                        content_type="application/json",
                        body='{"error":"controlled override save failure"}',
                    )
                    return
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
                )
                target["can_apply"] = (
                    target["final_target_bid_rub"] is not None
                    and float(target["final_target_bid_rub"])
                    != float(target["current_bid_rub"])
                )
                row = next(row for row in current_calculation[0]["rows"] if row["nm_id"] == target["nm_id"])
                row["select_available"] = any(
                    item.get("can_apply") or item.get("state_action_available")
                    for item in row["campaign_recommendations"]
                )
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
                    0 if apply_status_reads[0] == 1 else 3,
                )
                route.fulfill(status=200, content_type="application/json", body=json.dumps(latest_job[0]))
                return
            if path.endswith("/resume"):
                latest_job[0] = _job("completed", 3)
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
        assert "1 000" in normalized_balance_text
        assert "5 ₽/клик" in normalized_balance_text
        assert "10 ₽/клик" in normalized_balance_text
        assert "₽/1000 показов" in normalized_balance_text
        assert "₽/клик" in normalized_balance_text
        assert "insufficient_stats" not in normalized_balance_text
        assert "hold_conservative" not in normalized_balance_text
        assert "advert_id" not in normalized_balance_text
        inline_targets = page.locator(".inventory-balance-inline-target")
        assert inline_targets.count() >= 5
        assert all("Кампания" not in text for text in inline_targets.all_inner_texts())
        disclosures = page.locator(".inventory-balance-info-button")
        assert disclosures.count() >= 5
        first_disclosure = disclosures.first
        assert "Поддерживающая CPC" in (
            first_disclosure.locator("xpath=..").get_attribute("title") or ""
        )
        first_disclosure.click()
        disclosure = page.locator(".inventory-balance-campaign-disclosure-popover:popover-open")
        disclosure_text = disclosure.inner_text()
        assert "Поддерживающая CPC" in disclosure_text
        assert "advert_id 9001" in disclosure_text
        assert "Фактическое состояние: активна" in disclosure_text
        assert "Identity: exact nmID → advert_id → размещение" in disclosure_text
        page.keyboard.press("Escape")
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
        assert page.locator(".inventory-balance-selection-status").first.evaluate(
            "node => getComputedStyle(node).whiteSpace") == "nowrap"
        assert all(64 <= height <= 90 for height in row_heights), row_heights
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
        ) <= 2, {"select": sticky_select_box, "product": sticky_product_box, "shell": shell_box}
        table_shell.evaluate("node => { node.scrollLeft = 0; }")
        assert "CPC · ставка" in page.locator("[data-inventory-balance-head]").inner_text()
        assert "CPM · ставка" in page.locator("[data-inventory-balance-head]").inner_text()
        layout_1366 = page.evaluate(
            """
            () => {
              const columns=Array.from(document.querySelectorAll('[data-inventory-balance-head] th'));
              const cpcIndex=columns.findIndex(node => node.dataset.inventoryBalanceColumn === 'new_cpc_campaigns');
              const cpmIndex=columns.findIndex(node => node.dataset.inventoryBalanceColumn === 'old_cpm_campaigns');
              const row=document.querySelector('[data-inventory-balance-nm-id="101"]');
              const cell=row.children[cpmIndex];
              const target=cell.querySelector('.inventory-balance-inline-target');
              const input=target.querySelector('.inventory-balance-inline-input');
              const info=target.querySelector('.inventory-balance-info-button');
              const visible=Array.from(target.children).filter(node => {
                const style=getComputedStyle(node);
                return style.display !== 'none' && !node.matches('[popover]');
              });
              const gaps=[];
              for(let index=1; index<visible.length; index+=1){
                gaps.push(visible[index].getBoundingClientRect().left-visible[index-1].getBoundingClientRect().right);
              }
              const defaults=Object.fromEntries(INVENTORY_BALANCE_COLUMNS.map(item => [item.id,item.width]));
              return {
                viewport:innerWidth,
                defaultCpc:defaults.new_cpc_campaigns,
                defaultCpm:defaults.old_cpm_campaigns,
                cpcWidth:columns[cpcIndex].getBoundingClientRect().width,
                cpmWidth:columns[cpmIndex].getBoundingClientRect().width,
                inputWidth:input.getBoundingClientRect().width,
                inputCaret:getComputedStyle(input).caretColor,
                cellPaddingLeft:parseFloat(getComputedStyle(cell).paddingLeft),
                cellPaddingRight:parseFloat(getComputedStyle(cell).paddingRight),
                targetClientWidth:target.clientWidth,
                targetScrollWidth:target.scrollWidth,
                targetRight:target.getBoundingClientRect().right,
                infoRight:info.getBoundingClientRect().right,
                maxGap:Math.max(...gaps),
                minGap:Math.min(...gaps),
                pageOverflow:document.documentElement.scrollWidth-innerWidth,
              };
            }
            """
        )
        for zoom in [1, .8]:
            page.evaluate("zoom => document.documentElement.style.zoom=String(zoom)", zoom)
            geometry = page.locator('.inventory-balance-inline-target').evaluate_all("""nodes => nodes.map(node=>{
              const input=node.querySelector('input').getBoundingClientRect(), current=node.querySelector('.inventory-balance-current').getBoundingClientRect(), cell=node.closest('td').getBoundingClientRect();
              return {inputBottom:input.bottom,currentTop:current.top,currentBottom:current.bottom,cellBottom:cell.bottom,font:parseFloat(getComputedStyle(node.querySelector('.inventory-balance-current')).fontSize)};
            })""")
            assert all(box["currentTop"] >= box["inputBottom"] and box["currentBottom"] <= box["cellBottom"] and box["font"] >= 12 for box in geometry), (zoom, geometry)
        page.evaluate("document.documentElement.style.zoom='1'")
        assert layout_1366["viewport"] == 1366, layout_1366
        assert layout_1366["defaultCpc"] == layout_1366["defaultCpm"] == 200, layout_1366
        assert 190 <= layout_1366["cpcWidth"] <= 202, layout_1366
        assert abs(layout_1366["cpcWidth"] - layout_1366["cpmWidth"]) <= 1, layout_1366
        assert layout_1366["inputWidth"] >= 90, layout_1366
        assert layout_1366["inputCaret"] != "rgba(0, 0, 0, 0)", layout_1366
        assert layout_1366["cellPaddingLeft"] <= 5 and layout_1366["cellPaddingRight"] <= 5, layout_1366
        assert layout_1366["targetScrollWidth"] <= layout_1366["targetClientWidth"] + 1, layout_1366
        assert abs(layout_1366["targetRight"] - layout_1366["infoRight"]) <= 6, layout_1366
        assert 0 <= layout_1366["minGap"] <= layout_1366["maxGap"] <= 8, layout_1366
        assert layout_1366["pageOverflow"] <= 1, layout_1366
        page.set_viewport_size({"width": 1100, "height": 820})
        page.wait_for_timeout(50)
        layout_narrow = page.evaluate(
            """
            () => {
              const columns=Array.from(document.querySelectorAll('[data-inventory-balance-head] th'));
              const cpmIndex=columns.findIndex(node => node.dataset.inventoryBalanceColumn === 'old_cpm_campaigns');
              const row=document.querySelector('[data-inventory-balance-nm-id="101"]');
              const target=row.children[cpmIndex].querySelector('.inventory-balance-inline-target');
              const input=target.querySelector('.inventory-balance-inline-input');
              return {
                viewport:innerWidth,
                columnWidth:columns[cpmIndex].getBoundingClientRect().width,
                inputWidth:input.getBoundingClientRect().width,
                targetClientWidth:target.clientWidth,
                targetScrollWidth:target.scrollWidth,
                pageOverflow:document.documentElement.scrollWidth-innerWidth,
              };
            }
            """
        )
        assert layout_narrow["viewport"] == 1100, layout_narrow
        assert 190 <= layout_narrow["columnWidth"] <= 202, layout_narrow
        assert layout_narrow["inputWidth"] >= 90, layout_narrow
        assert layout_narrow["targetScrollWidth"] <= layout_narrow["targetClientWidth"] + 1, layout_narrow
        assert layout_narrow["pageOverflow"] <= 1, layout_narrow
        screenshot_narrow_path = os.environ.get(
            "SKU_INVENTORY_BALANCE_SCREENSHOT_NARROW", ""
        ).strip()
        if screenshot_narrow_path:
            table_shell.evaluate("node => { node.scrollLeft = node.scrollWidth; }")
            page.wait_for_timeout(50)
            page.screenshot(path=screenshot_narrow_path, full_page=True)
            table_shell.evaluate("node => { node.scrollLeft = 0; }")
        page.set_viewport_size({"width": 1366, "height": 900})
        assert page.locator("[data-inventory-balance-xlsx]").is_enabled()
        assert page.locator('[data-inventory-balance-nm-id="202"] [data-inventory-balance-override]').count() == 3
        assert page.locator('[data-inventory-balance-override="101:8001:search"]').input_value() == ""
        unavailable_override = page.locator('[data-inventory-balance-override="303:9303:search"]')
        assert unavailable_override.input_value() == ""
        assert page.evaluate("inventoryBalancePaceHint({pace_change_pct:null})") == ""
        assert unavailable_override.get_attribute("placeholder") == "Новая"
        neutral_state = page.locator(
            '[data-inventory-balance-nm-id="303"] .inventory-balance-state-status.neutral'
        ).first
        assert neutral_state.is_disabled()
        assert "Статус WB 7" in (neutral_state.get_attribute("title") or "")

        _run_sort_regressions(page)
        assert page.locator('.inventory-balance-target-unit').count() == 0
        assert page.locator('.inventory-balance-campaign-cell .inventory-balance-state-status').count() == 0
        assert page.locator('.inventory-balance-status-cell .inventory-balance-info-button').count() == 0
        assert page.locator('.inventory-balance-status-cell .inventory-balance-state-status').count() >= 5

        paused_state = page.locator(
            '[data-inventory-balance-nm-id="101"] .inventory-balance-state-status.paused'
        ).first
        paused_state.click()
        paused_menu = page.locator(".inventory-balance-state-menu:popover-open")
        assert paused_menu.inner_text().strip() == "возобновить"
        paused_menu.locator("[data-inventory-balance-state-choice]").click()
        page.locator('[data-inventory-balance-sort="old_cpm_status"]').click()
        assert page.locator('[data-inventory-balance-state-pending="state:101:8001"]').count()==1
        pending_cpm_layout = page.locator(
            '[data-inventory-balance-override="101:8001:search"]'
        ).locator("xpath=../../..").evaluate(
            """
            target => {
              const info=target.querySelector('.inventory-balance-info-button');
              return {
                clientWidth:target.clientWidth,
                scrollWidth:target.scrollWidth,
                targetRight:target.getBoundingClientRect().right,
                infoRight:info.getBoundingClientRect().right,
              };
            }
            """
        )
        assert pending_cpm_layout["scrollWidth"] <= pending_cpm_layout["clientWidth"] + 1, pending_cpm_layout
        assert abs(pending_cpm_layout["targetRight"] - pending_cpm_layout["infoRight"]) <= 6, pending_cpm_layout
        page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-state-pending="state:101:8001"] [data-inventory-balance-state-cancel]'
        ).click()

        active_state = page.locator(
            '[data-inventory-balance-nm-id="101"] .inventory-balance-state-status.active'
        ).first
        assert active_state.inner_text() == "▶"
        apply_requests_before_state = len(
            [item for item in requests if item[1].endswith("/apply-jobs")]
        )
        active_state.click()
        state_menu = page.locator(".inventory-balance-state-menu:popover-open")
        assert state_menu.is_visible()
        assert state_menu.inner_text().strip() == "остановить"
        state_menu.locator("[data-inventory-balance-state-choice]").click()
        assert page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-state-pending]'
        ).inner_text().startswith("Ост.")
        assert active_state.inner_text() == "▶"
        assert page.locator('[data-inventory-balance-select="101"]').is_checked()
        assert len([item for item in requests if item[1].endswith("/apply-jobs")]) == apply_requests_before_state
        page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-state-cancel]'
        ).click()
        assert page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-state-pending]'
        ).count() == 0
        assert page.locator('[data-inventory-balance-select="101"]').is_checked() is False

        first_click_cell = page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-select-cell]'
        )
        first_click_checkbox = page.locator('[data-inventory-balance-select="101"]')
        manual_override = page.locator(
            '[data-inventory-balance-override="101:8001:search"]'
        )
        manual_override.click()
        assert page.evaluate(
            "document.activeElement === document.querySelector('[data-inventory-balance-override=\"101:8001:search\"]')"
        )
        assert page.evaluate(
            "getComputedStyle(document.activeElement).caretColor !== 'rgba(0, 0, 0, 0)'"
        )
        page.evaluate("""() => { const original = window.fetch; window.fetch = async (...args) => { const response = await original(...args); if(String(args[0]).endsWith('/override')) await new Promise(resolve => setTimeout(resolve, 500)); return response; }; }""")
        override_requests_before = len([item for item in requests if item[1].endswith("/override")])
        input_node = manual_override.element_handle()
        manual_override.press_sequentially("725", delay=600)
        assert input_node.evaluate("node => node.isConnected && document.activeElement === node")
        assert manual_override.input_value() == "725"
        assert len([item for item in requests if item[1].endswith("/override")]) == override_requests_before
        manual_override.press("Home")
        manual_override.press("ArrowRight")
        manual_override.press_sequentially("0", delay=600)
        assert manual_override.input_value() == "7025"
        assert input_node.evaluate("node => document.activeElement === node")
        manual_override.fill("725.25")
        next_field = page.locator('[data-inventory-balance-override="202:9202:search"]')
        next_node = next_field.element_handle()
        next_field.click()
        assert next_node.evaluate("node => node.isConnected && document.activeElement === node")
        page.wait_for_timeout(650)
        assert next_node.evaluate("node => node.isConnected && document.activeElement === node")

        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-select=\"101\"]').checked && !document.querySelector('#inventory-balance-select-status-101').textContent.includes('сохраняется')"
        )
        assert page.evaluate(
            "selectedInventoryBalanceTargets().map(item => item.target_key)"
        ) == ["101:8001:search"]
        assert page.evaluate(
            "Array.from(state.inventoryBalance.autoSelectionReasons.get(101) || [])"
        ) == ["bid:101:8001:search"]
        assert "(1)" in page.locator("[data-inventory-balance-apply]").inner_text()

        first_click_checkbox.uncheck()
        assert first_click_checkbox.is_checked() is False
        assert page.evaluate(
            "Array.from(state.inventoryBalance.autoSelectionReasons.get(101) || [])"
        ) == []

        manual_override.fill("777.77")
        manual_override.press("Enter")
        page.wait_for_function(
            "document.querySelector('#inventory-balance-select-status-101').textContent.includes('ошибка сохранения')"
        )
        assert first_click_checkbox.is_checked() is False
        assert "controlled override save failure" in page.locator(
            "[data-inventory-balance-error]"
        ).inner_text()

        manual_override.fill("710.5")
        manual_override.press("Enter")
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-select=\"101\"]').checked && inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub === 710.5"
        )
        assert first_click_checkbox.is_checked()
        manual_override.fill("")
        manual_override.press("Enter")
        page.wait_for_function(
            "!document.querySelector('[data-inventory-balance-select=\"101\"]').checked && state.inventoryBalance.overrideSaving.size === 0 && state.inventoryBalance.overrideTimers.size === 0 && inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub == null"
        )
        assert page.evaluate("selectedInventoryBalanceTargets().length") == 0
        manual_override.fill("725.25")
        manual_override.press("Enter")
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-select=\"101\"]').checked && inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub === 725.25"
        )

        explicit_checkbox = page.locator('[data-inventory-balance-select="202"]')
        explicit_bid = page.locator('[data-inventory-balance-override="202:9202:search"]')
        explicit_bid.fill("12")
        explicit_bid.press("Enter")
        page.wait_for_function("inventoryBalanceTargetByKey('202:9202:search').manual_target_bid_rub === 12")
        explicit_checkbox.uncheck()
        explicit_checkbox.check()
        explicit_state = page.locator(
            '[data-inventory-balance-nm-id="202"] .inventory-balance-state-status.active'
        ).first
        explicit_state.click()
        page.locator(
            ".inventory-balance-state-menu:popover-open [data-inventory-balance-state-choice]"
        ).click()
        page.locator(
            '[data-inventory-balance-nm-id="202"] [data-inventory-balance-state-cancel]'
        ).click()
        assert explicit_checkbox.is_checked(), "pending cancellation removed explicit selection"
        explicit_checkbox.uncheck()

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

        override = page.locator('[data-inventory-balance-override="101:8001:search"]')
        assert override.input_value() == ""
        override_count_before = len([item for item in requests if item[1].endswith("/override")])
        override.fill("725.25")
        override.press("Enter")
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-select=\"101\"]').checked"
        )
        assert len([item for item in requests if item[1].endswith("/override")]) == override_count_before + 1, [
            item for item in requests if item[1].endswith("/override")
        ]
        assert override.input_value() == "725.25"
        assert page.evaluate(
            "selectedInventoryBalanceTargets().map(item => item.target_key)"
        ) == ["101:8001:search"]
        assert page.locator("[data-inventory-balance-apply]").is_enabled()
        assert "(1)" in page.locator("[data-inventory-balance-apply]").inner_text()

        state_action = page.locator(
            '[data-inventory-balance-nm-id="101"] .inventory-balance-state-status.active'
        ).first
        state_action.click()
        page.locator(
            ".inventory-balance-state-menu:popover-open [data-inventory-balance-state-choice]"
        ).click()
        assert "(2)" in page.locator("[data-inventory-balance-apply]").inner_text()
        assert page.evaluate(
            "selectedInventoryBalanceStateActions().map(item => item.target_key)"
        ) == ["state:101:9001"]

        page.locator("[data-inventory-balance-preset]").select_option("all")
        page.locator("[data-inventory-balance-select-all]").click()
        assert "(2)" in page.locator("[data-inventory-balance-apply]").inner_text(), "select-all must not invent bids"
        second_bid = page.locator('[data-inventory-balance-override="202:9202:search"]')
        second_bid.fill("1701")
        second_bid.press("Enter")
        page.wait_for_function("inventoryBalanceTargetByKey('202:9202:search').manual_target_bid_rub === 1701")
        page.locator("[data-inventory-balance-select-all]").click()
        assert "(3)" in page.locator("[data-inventory-balance-apply]").inner_text()
        assert page.evaluate(
            "selectedInventoryBalanceTargets().map(item => item.target_key)"
        ) == ["101:8001:search", "202:9202:search"]
        assert page.evaluate(
            "Array.from(state.inventoryBalance.selectAllSelectionNmIds).sort((a,b) => a-b)"
        ) == [101, 202]
        page.locator("[data-inventory-balance-select-all]").click()
        assert "(3)" in page.locator("[data-inventory-balance-apply]").inner_text()

        override.fill("")
        override.press("Enter")
        page.wait_for_function(
            "state.inventoryBalance.overrideSaving.size === 0 && state.inventoryBalance.overrideTimers.size === 0 && inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub == null"
        )
        assert page.locator('[data-inventory-balance-select="101"]').is_checked()
        assert page.evaluate(
            "selectedInventoryBalanceTargets().map(item => item.target_key)"
        ) == ["202:9202:search"]
        override.fill("725.25")
        override.press("Enter")
        page.wait_for_function(
            "state.inventoryBalance.overrideSaving.size === 0 && inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub === 725.25"
        )

        page.locator(
            '[data-inventory-balance-nm-id="101"] [data-inventory-balance-state-cancel]'
        ).click()
        assert page.locator('[data-inventory-balance-select="101"]').is_checked()
        assert "(2)" in page.locator("[data-inventory-balance-apply]").inner_text()
        state_action.click()
        page.locator(
            ".inventory-balance-state-menu:popover-open [data-inventory-balance-state-choice]"
        ).click()
        assert "(3)" in page.locator("[data-inventory-balance-apply]").inner_text()
        assert not [item for item in requests if item[1].endswith("/apply-jobs")]

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
        assert not [item for item in requests if item[1].endswith("/apply-jobs")]
        confirmation = page.locator("[data-inventory-balance-confirm-body]").inner_text()
        normalized_confirmation = confirmation.replace("\xa0", " ")
        assert "Выбрано SKU\n2" in confirmation
        assert "Ручных ставок\n2" in confirmation
        assert "Рекомендаци" not in confirmation
        assert "Статусов\n1" in confirmation
        assert "Повышений / понижений\n1 / 1" in confirmation
        assert "1 000 → 725,25 ₽/1000 показов" in normalized_confirmation
        assert "10 → 1 701 ₽/клик" in normalized_confirmation
        assert "активна → на паузе (остановить)" in normalized_confirmation
        assert "Ручная ставка · advert_id 8001" in normalized_confirmation
        assert "Ручная ставка · advert_id 9202" in normalized_confirmation
        assert "Ожидающее действие · advert_id 9001" in normalized_confirmation
        assert "Повышение на 1 691 ₽ превышает прежний контрольный порог 100 ₽" in normalized_confirmation
        assert "Новая ставка 1 701 ₽ превышает прежний контрольный потолок 1 000 ₽" in normalized_confirmation
        assert "Повышение на 16 910% превышает прежний контрольный порог 50%" in normalized_confirmation
        assert "Не включено пустых, равных текущим или недоступных целей: 2" in confirmation
        assert "эти точные изменения будут отправлены в WB" in confirmation
        assert "Перед отправкой проверим актуальные ставки" in confirmation
        assert "подтвердим результат в WB" in confirmation
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
        assert "Применяем изменения" in page.locator("[data-inventory-balance-confirm-title]").inner_text()
        assert "Применяем изменения" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        assert page.locator("[data-inventory-balance-confirm]").is_hidden()
        page.locator("[data-inventory-balance-progress-open]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_visible()
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress]').getAttribute('data-state') === 'completed'"
        )
        assert "Применено" in page.locator("[data-inventory-balance-confirm-body]").inner_text()
        assert "3 применено" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert page.locator("[data-inventory-balance-progress]").get_attribute("data-state") == "completed"
        assert page.locator("[data-inventory-balance-progress-fill]").get_attribute("style") == "width: 100%;"
        assert page.locator("[data-inventory-balance-progress-spinner]").is_hidden()
        assert "применено" in page.locator('[data-inventory-balance-nm-id="101"]').inner_text()
        assert "Все изменения применены" in page.locator("[data-inventory-balance-progress-summary]").inner_text()
        page.locator("[data-inventory-balance-confirm-cancel]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_hidden()
        page.locator("[data-inventory-balance-progress-open]").click()
        assert page.locator("[data-inventory-balance-confirm]").is_visible()

        latest_job[0] = _job("completed_with_errors", 3)
        page.reload()
        page.locator('[data-sku-management-subtab="inventory-balance"]').click(force=True)
        page.wait_for_function(
            "document.querySelector('[data-inventory-balance-progress-summary]').textContent.includes('Изменения применены частично')"
        )
        partial_summary = page.locator("[data-inventory-balance-progress-summary]").inner_text()
        assert "Изменения применены частично" in partial_summary
        assert "2 применено" in partial_summary
        assert "1 требует проверки" in partial_summary
        if page.locator("[data-inventory-balance-confirm]").is_hidden():
            page.locator("[data-inventory-balance-progress-open]").click()
        assert "Требуется проверка" in page.locator("[data-inventory-balance-confirm-body]").inner_text()

        zero_job = _job("completed_with_errors", 3)
        zero_job["progress"].update({"applied": 0, "failed": 3, "needs_check": 0})
        zero_job["items"][0].update({
            "current_bid_rub": 1500,
            "final_target_bid_rub": 1701,
            "state": "failed",
            "phase": "Не применено",
            "error_code": "safety_guard",
            "error": "requested_bid_rub exceeds absolute increase threshold",
        })
        zero_job["items"][1].update({"state": "skipped", "phase": "Не отправлено", "error": "Контрольная ставка не прошла проверку"})
        zero_job["items"][2].update({"state": "skipped", "phase": "Не отправлено", "error": "Контрольное изменение не прошло проверку"})
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
        page.evaluate("""() => { const item=state.inventoryBalance.applyJob.items[0];item.result={preflight:{error_code:'stale_campaign_state',observed_campaign_state:'active'}};renderInventoryBalanceConfirmationProgress(); }""")
        result_cell=page.locator('[data-inventory-balance-confirm-body] tbody tr').first.locator('td').nth(3)
        assert "Действие не отправлено; состояние в исходном расчёте устарело." in result_cell.inner_text()
        assert result_cell.evaluate("node => getComputedStyle(node).whiteSpace === 'normal' && node.scrollHeight <= node.clientHeight + 1"), result_cell.inner_text()


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

        page.evaluate("""() => {
          const target=inventoryBalanceTargetByKey('101:8001:search');
          const job={job_id:'test-proof',created_at:'2026-09-01T00:00:00Z',updated_at:'2026-09-01T00:01:00Z',items:[{target_key:target.target_key,nm_id:101,advert_id:8001,action_type:'bid_change',state:'succeeded',final_target_bid_rub:725.25,result:{readback_status:'matching',confirmed_bid_minor:72525}}]};
          target.manual_target_bid_rub=725.25;target.override_updated_at='2026-08-31T00:00:00Z';
          applyInventoryBalanceJobObservations(job);
          if(target.current_bid_rub!==725.25||target.manual_target_bid_rub!==null)throw Error('proven current/draft clear');
          target.manual_target_bid_rub=725.25;target.override_updated_at='2026-09-02T00:00:00Z';
          applyInventoryBalanceJobObservations(job);
          if(target.manual_target_bid_rub!==725.25)throw Error('newer draft lost');
          target.current_bid_rub=800;target.current_bid_evidence={observed_at:'2026-09-03T00:00:00Z'};
          applyInventoryBalanceJobObservations(job);
          if(target.current_bid_rub!==800)throw Error('newer proof overwritten');
          delete target.current_bid_evidence;state.inventoryBalance.calculation.created_at='2026-09-04T00:00:00Z';
          applyInventoryBalanceJobObservations(job);
          if(target.current_bid_rub!==800)throw Error('newer calculation overwritten');
        }""")
        _run_navigation_permissions(browser)
        _run_terminal_result_regressions(browser, html)
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
    assert start_requests[-1][2]["state_actions"] == [
        {"nm_id": 101, "advert_id": 9001, "action": "pause"}
    ]
    assert start_requests[-1][2]["nm_ids"] == []
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
        item
        for item in console_errors
        if "ERR_CONNECTION_FAILED" not in item and "status of 409" not in item
    ]
    assert not unexpected_console_errors, unexpected_console_errors
    assert any("ERR_CONNECTION_FAILED" in item for item in console_errors), console_errors
    assert any("status of 409" in item for item in console_errors), console_errors
    print("sku_inventory_balance_browser_smoke: ok")


def _run_sort_regressions(page) -> None:
    def ids():
        return page.locator('.inventory-balance-main-row').evaluate_all("nodes=>nodes.map(node=>Number(node.dataset.inventoryBalanceNmId))")
    def signature():
        return page.locator('.inventory-balance-main-row').evaluate_all("nodes=>Object.fromEntries(nodes.map(node=>[node.dataset.inventoryBalanceNmId,{text:node.innerText,inputs:Array.from(node.querySelectorAll('input')).map(input=>[input.dataset.inventoryBalanceOverride||'select',input.value,input.checked])}]))")
    original = page.evaluate("JSON.parse(JSON.stringify(state.inventoryBalance.calculation))")
    before = signature()
    assert page.locator('[data-inventory-balance-sort="select"]').count()==0
    for column,descending,ascending in [
        ('new_cpc_campaigns',[303,202,101,404],[101,202,303,404]),
        ('old_cpm_campaigns',[303,202,101,404],[101,202,303,404]),
    ]:
        button=page.locator(f'[data-inventory-balance-sort="{column}"]')
        button.click()
        assert ids()==descending, (column,ids())
        assert button.inner_text().endswith('↓')
        assert signature()==before, "sorting split SKU data"
        button.click()
        assert ids()==ascending, (column,ids())
        assert button.inner_text().endswith('↑')
        assert signature()==before
    page.evaluate("""() => {const rows=state.inventoryBalance.calculation.rows;rows[1].new_cpc_campaigns[0].campaign_state='paused';rows[1].old_cpm_campaigns[0].campaign_state='active';renderInventoryBalance();}""")
    for column,descending,ascending in [
        ('new_cpc_status',[101,202,303,404],[202,101,303,404]),
        ('old_cpm_status',[202,101,303,404],[101,202,303,404]),
    ]:
        button=page.locator(f'[data-inventory-balance-sort="{column}"]')
        button.click();assert ids()==descending,(column,ids())
        button.click();assert ids()==ascending,(column,ids())
    # First displayed target wins the comparison, not max/any. Missing stays last.
    assert page.evaluate("""() => {
      const rows=[{nm_id:1,new_cpc_campaigns:[{current_bid_rub:5},{current_bid_rub:999}]},{nm_id:2,new_cpc_campaigns:[{current_bid_rub:10},{current_bid_rub:1}]},{nm_id:3,new_cpc_campaigns:[]}];
      return inventoryBalanceSortRows(rows,{column:'new_cpc_campaigns',direction:'desc'}).map(row=>row.nm_id);
    }""")==[2,1,3]
    assert page.evaluate("""() => {
      const rows=[{nm_id:1,new_cpc_campaigns:[{nm_id:1,advert_id:1,campaign_state:'paused'},{nm_id:1,advert_id:2,campaign_state:'active'}]},{nm_id:2,new_cpc_campaigns:[{nm_id:2,advert_id:3,campaign_state:'active'}]}];
      return inventoryBalanceSortRows(rows,{column:'new_cpc_status',direction:'desc'}).map(row=>row.nm_id);
    }""")==[2,1]
    for column,values in [('known_stock_units',[2,10,None]),('next_inbound',[{'date':'2026-09-01','quantity':2},{'date':'2026-10-01','quantity':1},{}]),('product',['А','Б',None])]:
        assert page.evaluate("""({column,values})=>{const rows=values.map((value,index)=>({nm_id:index,[column==='product'?'name':column]:value}));return inventoryBalanceSortRows(rows,{column,direction:'asc'}).map(row=>row.nm_id);} """, {'column':column,'values':values})==[0,1,2]
    page.evaluate("""()=>{window.__sortNativeFetch=window.fetch;window.fetch=async(...args)=>{const response=await window.__sortNativeFetch(...args);if(String(args[0]).endsWith('/override'))await new Promise(resolve=>setTimeout(resolve,300));return response;};}""")
    entry=page.locator('[data-inventory-balance-override="101:8001:search"]')
    entry.fill('73')
    page.locator('[data-inventory-balance-sort="new_cpc_campaigns"]').click()
    page.wait_for_function("state.inventoryBalance.overrideSaving.has('101:8001:search')")
    assert entry.input_value()=='73'
    assert page.evaluate("state.inventoryBalance.overrideDrafts.get('101:8001:search')")=='73'
    page.wait_for_function("inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub===73 && state.inventoryBalance.overrideSaving.size===0")
    assert entry.input_value()=='73'
    entry.fill('');entry.press('Enter')
    page.wait_for_function("inventoryBalanceTargetByKey('101:8001:search').manual_target_bid_rub==null && state.inventoryBalance.overrideSaving.size===0")
    page.evaluate("window.fetch=window.__sortNativeFetch;delete window.__sortNativeFetch")
    page.evaluate("calculation=>{state.inventoryBalance.calculation=calculation;state.inventoryBalance.table.sort={};renderInventoryBalance();}",original)


def _run_navigation_permissions(browser) -> None:
    for sections, expected in [
        (["sku_management"], ["inventory-balance", "change-registry"]),
        (["ads"], ["ads"]), (["prices"], ["prices"]),
        (["prices", "ads"], ["prices", "ads"]),
        (["sku_management", "prices", "ads"], ["inventory-balance", "prices", "ads", "change-registry"]),
    ]:
        html = _render_sheet_vitrina_web_vitrina_ui(
            read_path="/v1/sheet-vitrina-v1/web-vitrina", operator_path="/sheet-vitrina-v1/operator",
            refresh_path="/v1/sheet-vitrina-v1/refresh", job_path="/v1/sheet-vitrina-v1/job",
            role="operator", allowed_sections=sections, active_tab="sku-management")
        page = browser.new_page()
        calls = []
        def handle(route):
            path = route.request.url.split("navigation.test", 1)[-1]
            if path.startswith("/page"):
                route.fulfill(status=200, content_type="text/html", body=html)
            else:
                calls.append(path)
                route.fulfill(status=200, content_type="application/json", body='{"rows":[],"settings":{},"meta":{}}')
        page.route("**/*", handle)
        page.goto("http://navigation.test/page?tab=sku-management")
        assert page.locator('[data-unified-tab-button="sku-management"]').is_visible(), sections
        assert page.locator('[data-unified-tab-button="prices"]').is_hidden()
        assert page.locator('[data-unified-tab-button="ads"]').is_hidden()
        assert page.locator('[data-sku-management-subtab="general"]').count() == 0
        visible = page.locator('[data-sku-management-subtab]:visible').evaluate_all("nodes => nodes.map(node=>node.dataset.skuManagementSubtab)")
        assert visible == expected, (sections, visible)
        assert page.evaluate("state.inventoryBalance.activeSubtab") == expected[0]
        for name in expected:
            page.locator(f'[data-sku-management-subtab="{name}"]').click()
            assert page.locator(f'[data-sku-management-subpanel="{name}"]').is_visible()
        for name in [item for item in expected if item in {"prices", "ads"}]:
            page.goto(f"http://navigation.test/page?tab={name}")
            assert page.evaluate("state.inventoryBalance.activeSubtab") == name
            page.reload()
            assert page.evaluate("state.inventoryBalance.activeSubtab") == name
            page.goto(f"http://navigation.test/page?tab=sku-management&sku_tab={name}")
            assert page.evaluate("state.inventoryBalance.activeSubtab") == name
        assert "/v1/sheet-vitrina-v1/sku-management" not in calls, (sections, calls)
        if "sku_management" not in sections:
            assert not any("inventory-balance" in path or "change-registry" in path for path in calls), calls
        page.close()


def _run_terminal_result_regressions(browser, html: str) -> None:
    """Initial latest 500 and stale secondary responses cannot hide a result."""

    page = browser.new_page(viewport={"width": 1500, "height": 1000})
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: console_errors.append(str(error)))
    old_calculation = _calculation()
    terminal_calculation = deepcopy(old_calculation)
    terminal_calculation["calculation_id"] = "ibc_terminal_after_latest_500"
    terminal_calculation["rows"][0]["name"] = "Terminal Result SKU"
    newer_calculation = deepcopy(terminal_calculation)
    newer_calculation["calculation_id"] = "ibc_newer_than_stale_override"
    newer_calculation["rows"][0]["name"] = "Newer Result SKU"
    calculate_runs = [0]
    active_result = [terminal_calculation]
    latest_reads = [0]
    requests: list[tuple[str, str]] = []

    def route_handler(route):
        request = route.request
        path = request.url.split("balance-regression.test", 1)[-1]
        requests.append((request.method, path))
        body = json.loads(request.post_data or "{}") if request.post_data else {}
        if path.split("?", 1)[0] == "/page":
            route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=html
            )
            return
        if path == "/v1/sheet-vitrina-v1/sku-management":
            route.fulfill(
                status=200,
                content_type="application/json",
                body='{"rows":[],"settings":{"revision":0,"forecast":{},"table":{}},"meta":{}}',
            )
            return
        base = "/v1/sheet-vitrina-v1/sku-management/inventory-balance"
        if path == base:
            latest_reads[0] += 1
            if latest_reads[0] == 1:
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body='{"error":"manual_pending lookup failed"}',
                )
            else:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "ok",
                            "settings": {
                                "revision": 1,
                                "calculation": {},
                                "table": {
                                    "visible_columns": [],
                                    "column_order": [],
                                },
                            },
                            "calculation": old_calculation,
                            "apply_job": None,
                            "calculation_operation": None,
                        }
                    ),
                )
            return
        if path == base + "/calculate":
            calculate_runs[0] += 1
            active_result[0] = (
                terminal_calculation
                if calculate_runs[0] == 1
                else newer_calculation
            )
            route.fulfill(
                status=202,
                content_type="application/json",
                body=json.dumps(
                    {
                        "accepted": True,
                        "operation_id": str(body.get("operation_id") or ""),
                    }
                ),
            )
            return
        if path.startswith(base + "/operations/"):
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    _operation(
                        path.rsplit("/", 1)[-1],
                        state="succeeded",
                        result=active_result[0],
                        percent=100,
                    )
                ),
            )
            return
        route.fulfill(
            status=404,
            content_type="application/json",
            body='{"error":"not found"}',
        )

    page.route("**/*", route_handler)
    page.goto("http://balance-regression.test/page")
    page.evaluate(
        """
        () => {
          const nativeFetch = window.fetch.bind(window);
          const base = "/v1/sheet-vitrina-v1/sku-management/inventory-balance";
          window.__holdBalanceLatest = false;
          window.__holdBalanceOverride = false;
          window.fetch = (input, init) => {
            const url = String(typeof input === "string" ? input : input.url || "");
            if (window.__holdBalanceLatest && url.endsWith(base)) {
              window.__holdBalanceLatest = false;
              return new Promise((resolve) => {
                window.__resolveHeldLatest = (payload) => resolve(new Response(
                  JSON.stringify(payload),
                  {status: 200, headers: {"Content-Type": "application/json"}}
                ));
              });
            }
            if (window.__holdBalanceOverride && url.endsWith("/override")) {
              window.__holdBalanceOverride = false;
              return new Promise((resolve) => {
                window.__resolveHeldOverride = (payload) => resolve(new Response(
                  JSON.stringify(payload),
                  {status: 200, headers: {"Content-Type": "application/json"}}
                ));
              });
            }
            return nativeFetch(input, init);
          };
        }
        """
    )
    page.wait_for_function(
        "document.querySelector('[data-inventory-balance-error]').textContent.includes('terminal result')"
    )
    assert page.locator("[data-inventory-balance-calculate]").is_enabled()

    page.evaluate("window.__holdBalanceLatest = true")
    page.locator("[data-inventory-balance-calculate]").click()
    page.locator('[data-sku-management-subtab="inventory-balance"]').click(
        force=True
    )
    page.wait_for_function("typeof window.__resolveHeldLatest === 'function'")
    page.wait_for_timeout(2500)
    assert "Terminal Result SKU" in page.locator(
        "[data-inventory-balance-body]"
    ).inner_text(), {
        "body": page.locator("[data-inventory-balance-body]").inner_text(),
        "error": page.locator("[data-inventory-balance-error]").inner_text(),
        "progress": page.locator(
            "[data-inventory-balance-calculation-progress-summary]"
        ).inner_text(),
        "requests": requests,
        "console": console_errors,
    }
    page.evaluate(
        "payload => window.__resolveHeldLatest(payload)",
        {
            "status": "ok",
            "settings": {
                "revision": 1,
                "calculation": {},
                "table": {"visible_columns": [], "column_order": []},
            },
            "calculation": old_calculation,
            "apply_job": None,
            "calculation_operation": None,
        },
    )
    page.wait_for_timeout(100)
    assert "Terminal Result SKU" in page.locator(
        "[data-inventory-balance-body]"
    ).inner_text(), {
        "body": page.locator("[data-inventory-balance-body]").inner_text(),
        "calculation_id": page.evaluate(
            "state.inventoryBalance.calculation && state.inventoryBalance.calculation.calculation_id"
        ),
        "load_token": page.evaluate("state.inventoryBalance.loadRequestToken"),
    }
    assert "Deficit SKU" not in page.locator(
        "[data-inventory-balance-body]"
    ).inner_text()

    page.evaluate("window.__holdBalanceOverride = true")
    page.locator(
        '[data-inventory-balance-override="101:8001:search"]'
    ).fill("666")
    page.locator('[data-inventory-balance-override="101:8001:search"]').press("Enter")
    page.wait_for_function("typeof window.__resolveHeldOverride === 'function'")
    page.locator("[data-inventory-balance-calculate]").click()
    page.wait_for_function(
        "document.querySelector('[data-inventory-balance-body]').textContent.includes('Newer Result SKU')"
    )
    stale_override = deepcopy(terminal_calculation)
    stale_override["rows"][0]["old_cpm_campaigns"][0][
        "manual_target_bid_rub"
    ] = 666
    page.evaluate(
        "payload => window.__resolveHeldOverride(payload)", stale_override
    )
    page.wait_for_timeout(100)
    assert "Newer Result SKU" in page.locator(
        "[data-inventory-balance-body]"
    ).inner_text()
    unexpected_console_errors = [
        item for item in console_errors if "status of 500" not in item
    ]
    assert not unexpected_console_errors, unexpected_console_errors
    assert any("status of 500" in item for item in console_errors), console_errors
    page.close()


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
    state_targets = (
        (deficit_cpc, 9, "active", "pause", "paused", "остановить"),
        (deficit_cpm, 11, "paused", "start", "active", "возобновить"),
        (excess_cpc, 9, "active", "pause", "paused", "остановить"),
        (excess_cpc_hold, 9, "active", "pause", "paused", "остановить"),
        (excess_cpm, 4, "ready", "start", "active", "запустить"),
    )
    for target, status, current_state, action, requested_state, label in state_targets:
        target.update(
            {
                "campaign_status": status,
                "campaign_state": current_state,
                "state_action_available": True,
                "state_action": action,
                "state_action_label": label,
                "state_target_key": f"state:{target['nm_id']}:{target['advert_id']}",
                "requested_campaign_state": requested_state,
                "campaign_state_recommendation_item_id": (
                    f"ibsr_browser_{target['advert_id']}"
                ),
            }
        )
    for target in (insufficient_cpc, insufficient_cpm):
        target.update(
            {
                "campaign_status": 7,
                "campaign_state": "complete",
                "state_action_available": False,
                "state_action": "",
                "state_action_label": "",
                "requested_campaign_state": "",
            }
        )
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
    total = 3
    succeeded = state == "completed"
    partial = state == "completed_with_errors"
    stalled = state == "stalled"
    applied = 2 if partial else 1 if stalled else terminal
    needs_check = 1 if partial else 0
    item_specs = (
        ("bid_change", 101, 8001, "Снижение дефицитной CPM", "cpm", "search", 1000, 725.25),
        ("bid_change", 202, 9202, "Продвижение CPC", "cpc", "search", 10, 1701),
        ("campaign_state", 101, 9001, "Поддерживающая CPC", "cpc", "", None, None),
    )
    items = []
    for item_index, (
        action_type,
        nm_id,
        advert_id,
        campaign,
        payment,
        placement,
        current,
        target,
    ) in enumerate(item_specs):
        if succeeded:
            item_state = "succeeded"
        elif partial:
            item_state = "succeeded" if item_index < 2 else "ambiguous"
        elif stalled:
            item_state = "succeeded" if item_index == 0 else "delayed"
        else:
            item_state = "verifying"
        phase = {
            "succeeded": "Применено",
            "ambiguous": "Требуется проверка",
            "delayed": "WB задерживает подтверждение",
            "verifying": "Проверяем фактическое состояние в WB",
        }[item_state]
        item = {
            "action_type": action_type,
            "nm_id": nm_id,
            "advert_id": advert_id,
            "campaign_name": campaign,
            "payment_type": payment,
            "placement": placement,
            "current_bid_rub": current,
            "final_target_bid_rub": target,
            "state": item_state,
            "phase": phase,
            "error": (
                "WB не подтвердил состояние"
                if item_state == "ambiguous"
                else ""
            ),
        }
        if action_type == "campaign_state":
            item.update(
                {
                    "current_campaign_state": "active",
                    "requested_campaign_state": "paused",
                    "state_action": "pause",
                    "state_action_label": "остановить",
                }
            )
        items.append(item)
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
        "items": items,
        "external_writes": True,
        "wb_patch_called": state != "pending",
        "wb_campaign_action_called": state != "pending",
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
