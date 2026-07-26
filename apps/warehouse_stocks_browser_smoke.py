#!/usr/bin/env python3
"""Playwright smoke for the shared warehouse UI and legacy FF transition."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import copy
import json
import os
from pathlib import Path
import re
import socket
import sys
from tempfile import TemporaryDirectory
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_stocks_smoke import _block, _seed_runtime  # noqa: E402
from apps.warehouse_stocks_production_ui_flow import (  # noqa: E402
    _allocated_amount_matches_eligible,
    _assert_warehouse_balance_cardinality,
    _metric_date_coverage,
    _sku_management_dom_summary,
    _supplier_financial_detail_url,
    _vitrina_incident_projection_scope,
    _visible_money,
    run_warehouse_ui_flow,
)
from playwright.sync_api import sync_playwright  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.warehouse_functional import (  # noqa: E402
    FUNCTIONAL_CUTOVER_ID,
    STAGES,
    STAGE_WB,
    WarehouseFunctionalBlock,
    WarehouseLine,
    _fingerprint,
    _line_payload,
    _summaries,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    _assert(
        {
            _vitrina_incident_projection_scope(
                "SKU:210183142|wb_stock_fact_qty"
            ),
            _vitrina_incident_projection_scope(
                "SKU:210183142|wb_stock_incident_qty"
            ),
            _vitrina_incident_projection_scope(
                "SKU:210183142|wb_stock_effective_qty"
            ),
        }
        == {"SKU:210183142"},
        "Vitrina acceptance joins all incident families by SKU scope",
    )
    _assert(
        _vitrina_incident_projection_scope(
            "TOTAL|total_wb_stock_effective_qty"
        )
        == "TOTAL",
        "Vitrina acceptance joins TOTAL incident families by TOTAL scope",
    )
    _assert_warehouse_balance_cardinality(
        warehouse_key="ff",
        expected_sku_count=1,
        detail_balances=[
            {"quantity": "6750"},
            {"quantity": "0", "reserved_quantity": "750"},
            {"quantity": "0", "reserved_quantity": "500"},
        ],
        visible_balance_count=3,
        warehouse_name="Склад FF",
    )
    _assert(
        _allocated_amount_matches_eligible(
            "120899.32", "120899.3199999999999999999999"
        ),
        "serialized Decimal allocation residue is conserved",
    )
    _assert(
        not _allocated_amount_matches_eligible("120899.32", "120899.31"),
        "material allocation drift remains rejected",
    )
    _assert(_visible_money("78\u00a0086,09 RUB") == Decimal("78086.09"), "localized RUB money")
    _assert(_visible_money("1\u202f234,50 CNY") == Decimal("1234.50"), "localized CNY money")
    _assert(
        _supplier_financial_detail_url("https://example.invalid/", "shipment id/1")
        == "https://example.invalid/sheet-vitrina-v1/supplier?embedded=operator&shipment_id=shipment%20id%2F1&tab=documents",
        "operator supplier financial detail URL",
    )
    _assert_metric_coverage_applicability()
    _assert_sku_management_dom_evidence()
    with TemporaryDirectory(prefix="warehouse-browser-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime = _seed_runtime(root / "runtime")
        block = _block(runtime)
        plan = block.build_opening_plan()
        block.apply_opening_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        functional = _apply_functional_fixture(
            runtime=runtime,
            opening_plan=plan,
            backup_dir=root / "functional-backups",
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime.runtime_dir,
        )
        with _patched_env({"WB_CORE_WEB_AUTH_REQUIRED": "0"}):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = run_warehouse_ui_flow(
                    base_url=f"http://127.0.0.1:{config.port}",
                    auth_cookie=None,
                    expected_readback=functional.readback(),
                    evidence_dir=root / "ui-evidence",
                    strict_business_acceptance=False,
                    allowed_server_error_paths=(
                        "/v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options",
                    ),
                    allowed_console_error_messages=(
                        "Failed to load resource: the server responded with a status of 422 (Unprocessable Content)",
                        "Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
                    ),
                )
                _assert_route_explicit_settings_frame(f"http://127.0.0.1:{config.port}")
                _assert_manual_sync_failure_keeps_last_good(f"http://127.0.0.1:{config.port}")
                _assert_supplier_registry_stage_cost_frame(f"http://127.0.0.1:{config.port}")
                _assert_stock_report_frame(f"http://127.0.0.1:{config.port}")
                _assert_sku_management_loaded(f"http://127.0.0.1:{config.port}")
                _assert(result.get("status") == "ok", "browser flow status")
                legacy_ff = result.get("legacy_ff_reconciliation") or {}
                ff_evidence = next(
                    item for item in result.get("warehouses") or [] if item.get("warehouse_key") == "ff"
                )
                _assert(result.get("legacy_ff_transition") is True, "legacy FF transition status")
                _assert(legacy_ff.get("loaded_before_screenshot") is True, "legacy FF loaded evidence")
                _assert(legacy_ff.get("document_id") == ff_evidence.get("document_id"), "legacy FF document")
                _assert(legacy_ff.get("sku_count") == ff_evidence.get("sku_count"), "legacy FF SKU count")
                _assert(
                    legacy_ff.get("total_quantity") == ff_evidence.get("total_quantity"),
                    "legacy FF total quantity",
                )
                _assert(
                    legacy_ff.get("balance_rows") == ff_evidence.get("balance_rows"),
                    "legacy FF balance rows",
                )
                _assert(legacy_ff.get("economics_loaded") is True, "legacy FF functional economics")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("warehouse stocks browser smoke: ok")


def _assert_metric_coverage_applicability() -> None:
    day = "2026-07-18"
    metric_keys = (
        "our_wb_unit_cost_rub",
        "proxy_profit_3_rub",
        "proxy_margin_3_pct",
    )
    cells = []
    for scope, order_sum, wb_quantity, values in (
        ("SKU:1", "100", "10", ("10", "20", "5")),
        ("SKU:2", "—", "4", ("—", "—", "—")),
        ("SKU:3", "0", "0", ("10", "0", "—")),
    ):
        cells.append(
            f'<td data-row-id="{scope}|orderSum" data-metric-key="orderSum" '
            f'data-cell-date="{day}">{order_sum}</td>'
        )
        cells.append(
            f'<td data-row-id="{scope}|stock_total" '
            f'data-metric-key="stock_total" data-cell-date="{day}">{wb_quantity}</td>'
        )
        for metric_key, value in zip(metric_keys, values, strict=True):
            cells.append(
                f'<td data-row-id="{scope}|{metric_key}" data-metric-key="{metric_key}" '
                f'data-cell-date="{day}">{value}</td>'
            )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content("<table><tbody><tr>" + "".join(cells) + "</tr></tbody></table>")
        cost = _metric_date_coverage(
            page,
            metric_key="our_wb_unit_cost_rub",
            day=day,
            wb_contour_by_scope={"SKU:1": "10", "SKU:2": "4", "SKU:3": "2"},
        )
        profit = _metric_date_coverage(page, metric_key="proxy_profit_3_rub", day=day)
        margin = _metric_date_coverage(page, metric_key="proxy_margin_3_pct", day=day)
        browser.close()
    _assert(
        cost == {"total": 3, "applicable": 3, "inapplicable": 0, "filled": 2},
        "WB cost applicability uses the contour and exposes a gap even with zero physical stock",
    )
    _assert(
        profit == {"total": 3, "applicable": 2, "inapplicable": 1, "filled": 2},
        "Proxy profit applicability",
    )
    _assert(
        margin == {"total": 3, "applicable": 1, "inapplicable": 2, "filled": 1},
        "zero-revenue margin is undefined",
    )


def _assert_sku_management_dom_evidence() -> None:
    source_rows = [
        {"nm_id": 1, "profit_rub": "100", "margin_pct": "10"},
        {"nm_id": 2, "profit_rub": None, "margin_pct": None},
        {"nm_id": 3, "profit_rub": "25", "margin_pct": "5"},
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(
            """
            <p data-sku-management-status>SKU: 3 · настройки хранятся на сервере</p>
            <table>
              <tr data-sku-row-nm-id="1">
                <td data-sku-cell="profit_rub">100 ₽</td>
                <td data-sku-cell="margin_pct">10%</td>
              </tr>
              <tr data-sku-row-nm-id="2">
                <td data-sku-cell="profit_rub">—</td>
                <td data-sku-cell="margin_pct">—</td>
              </tr>
              <tr data-sku-row-nm-id="3">
                <td data-sku-cell="profit_rub">25 ₽</td>
                <td data-sku-cell="margin_pct">5%</td>
              </tr>
            </table>
            """
        )
        summary = _sku_management_dom_summary(page, source_rows=source_rows)
        page.locator('[data-sku-row-nm-id="3"] [data-sku-cell="profit_rub"]').evaluate(
            "(node) => { node.textContent = '—'; }"
        )
        page.locator('[data-sku-row-nm-id="3"] [data-sku-cell="margin_pct"]').evaluate(
            "(node) => { node.textContent = '—'; }"
        )
        try:
            _sku_management_dom_summary(page, source_rows=source_rows)
        except AssertionError:
            pass
        else:
            raise AssertionError("missing rendered source-populated Proxy 3 row must fail")
        browser.close()
    _assert(summary["row_count"] == 3, "DOM SKU count")
    _assert(summary["proxy_3_row_count"] == 2, "DOM visible Proxy 3 count")


def _assert_manual_sync_failure_keeps_last_good(base_url: str) -> None:
    detail_fragment = "/v1/sheet-vitrina-v1/warehouses/production"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route(
                "**/v1/sheet-vitrina-v1/warehouses/sync",
                lambda route: route.fulfill(
                    status=429,
                    content_type="application/json",
                    body=json.dumps({"error": "injected sync failure"}),
                ),
            )
            page.goto(
                f"{base_url}/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=production",
                wait_until="domcontentloaded",
            )
            page.locator("[data-warehouse-balances] tr").first.wait_for()
            page.locator('[data-warehouse-key="update"]').click()
            page.locator("[data-warehouse-update-view]:not([hidden])").wait_for()
            page.locator("[data-warehouse-update-start]").click()
            page.wait_for_function(
                "() => ((document.querySelector('[data-warehouse-update-status]') || {}).textContent || '').includes('Не завершено:')"
            )
            page.locator('[data-warehouse-key="production"]').click()
            page.locator("[data-warehouse-balances] tr").first.wait_for()
            _assert(
                page.locator("[data-warehouse-status]").inner_text().strip() != "Ошибка загрузки",
                "failed manual sync reloads last-good detail",
            )
            _assert(
                "Данные склада не загружены"
                not in page.locator('[data-unified-tab-panel="warehouses"]').inner_text(),
                "failed manual sync does not leave a generic error surface",
            )
        finally:
            browser.close()


def _assert_route_explicit_settings_frame(base_url: str) -> None:
    from playwright.sync_api import sync_playwright

    process_specs = [
        ("vitrina_refresh", "Обновление Витрины", True, "manage", "Настройки → Автообновления"),
        ("vitrina_closure_retry", "Закрытие и повтор закрытия данных Витрины", True, "manage", "Настройки → Автообновления"),
        ("warehouse_functional", "Склады и себестоимость", True, "manage", "Настройки → Автообновления"),
        ("wb_finance_weekly", "Финансовый отчёт WB", True, "manage", "Настройки → Автообновления"),
        ("feedback_complaints", "Авто-жалобы", True, "monitor", "Отзывы → Авто-жалобы"),
        ("spp_test", "Автоматический тест СПП", False, "monitor", "Цены → Тест СПП"),
        ("autoanswers", "Autoanswers", True, "monitor", "Отзывы → Отзывы"),
    ]
    auto_payload = {
        "schema_version": "auto_updates_owner_policy_v2",
        "master_desired": False,
        "revision": 1,
        "policy_fingerprint": "sha256:browser-fixture",
        "changed_at": "2026-07-23T12:00:00Z",
        "actor": "initial_migration",
        "reason": "browser fixture",
        "overall_status_code": "global_pause",
        "overall_status": "Приостановлено общей паузой",
        "overall_explanation": "Общая пауза временно удерживает все автоматические запуски.",
        "unknown_processes": [],
        "drift_processes": [],
        "processes": [
            {
                "process_key": key,
                "display_name": label,
                "desired": desired,
                "actual": False,
                "lifecycle_state": "suspended_by_master" if desired else "off",
                "drift_status": "matched",
                "suspended_by_master": True,
                "control_capability": capability,
                "control_owner": "settings" if capability == "manage" else "feature",
                "control_location": location,
                "desired_source": "auto_updates_owner_policy" if capability == "manage" else key + "_feature_settings",
                "last_run": "2026-07-23T10:00:00Z",
                "last_success": "2026-07-23T10:00:00Z",
                "next_run": "",
                "last_error": "",
                "runtime_schedule": {},
                "timer": {"properties": {}},
                "provenance": "proven",
                "operator_status_code": "global_pause",
                "operator_status": "Приостановлено общей паузой",
                "operator_explanation": "Desired сохранён, но выполнение удерживается общей паузой.",
            }
            for key, label, desired, capability, location in process_specs
        ],
    }
    autoanswers_fixture = next(
        item
        for item in auto_payload["processes"]
        if item["process_key"] == "autoanswers"
    )
    autoanswers_fixture.update(
        {
            "business_mode": "auto_all",
            "fresh_scheduler_tick": True,
            "runtime_schedule": {
                "policy_epoch": 7,
                "transition_run_id": "transition:fixture",
                "last_scheduler_tick_at": "2026-07-23T10:00:00Z",
            },
            "component_states": {
                "readonly_sync": {"desired": True, "actual": False},
                "worker": {"desired": True, "actual": False},
            },
            "budget": {
                "budget_state": "conservative_unverified",
                "confirmed_actual_usd": 0,
                "uncertainty_hold_usd": 0.2,
                "uncertainty_hold_count": 2,
                "unresolved_uncertainty_count": 0,
                "hold_explanation": (
                    "Консервативный hold — верхняя граница возможного расхода, "
                    "а не подтверждённое списание."
                ),
            },
        }
    )
    auto_current = json.loads(json.dumps(auto_payload))
    auto_posts: list[dict[str, object]] = []
    auto_mode: dict[str, object] = {"next": "", "stale": None}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{base_url}/sheet-vitrina-v1/vitrina", wait_until="domcontentloaded")
            page.locator('[data-unified-tab-button="warehouses"]').click()
            page.locator('[data-unified-tab-panel="warehouses"]:not([hidden])').wait_for()

            def handle_auto_updates(route: object) -> None:
                request = route.request
                if request.method == "POST":
                    body = request.post_data_json
                    auto_posts.append(body)
                    if auto_mode["next"] == "backend_failure":
                        auto_mode["next"] = ""
                        route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "error": "synthetic backend failure",
                                    "code": "auto_updates_action_blocked",
                                }
                            ),
                        )
                        return
                    if (
                        body.get("action") == "set_master"
                        and bool(body.get("desired"))
                        == bool(auto_current["master_desired"])
                    ):
                        route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps(
                                {
                                    "error": "no-op master state",
                                    "code": "auto_updates_no_change",
                                }
                            ),
                        )
                        return
                    before = json.loads(json.dumps(auto_current))
                    changed = json.loads(json.dumps(auto_current))
                    changed["revision"] = int(changed["revision"]) + 1
                    changed["policy_fingerprint"] = (
                        "sha256:browser-fixture-" + str(changed["revision"])
                    )
                    if body.get("action") == "set_process":
                        for process in changed["processes"]:
                            if process["process_key"] == body.get("process_key"):
                                process["desired"] = body.get("desired")
                    elif body.get("action") == "set_master":
                        changed["master_desired"] = bool(body.get("desired"))
                    if changed["master_desired"]:
                        for process in changed["processes"]:
                            process["actual"] = bool(process["desired"])
                            process["drift_status"] = "matched"
                            process["lifecycle_state"] = "running" if process["desired"] else "off"
                            process["suspended_by_master"] = False
                            process["operator_status_code"] = "healthy" if process["desired"] else "user_pause"
                            process["operator_status"] = "Работает штатно" if process["desired"] else "Приостановлено пользователем"
                            process["operator_explanation"] = (
                                "Desired и actual совпадают; runtime readback свежий."
                                if process["desired"]
                                else "Процесс выключен владельцем в функциональном разделе."
                            )
                        changed["drift_processes"] = []
                        changed["overall_status_code"] = "user_pause"
                        changed["overall_status"] = "Приостановлено пользователем"
                        changed["overall_explanation"] = "Часть процессов выключена в своём функциональном разделе."
                    else:
                        for process in changed["processes"]:
                            process["actual"] = False
                            process["drift_status"] = "matched"
                            process["lifecycle_state"] = "suspended_by_master" if process["desired"] else "off"
                            process["suspended_by_master"] = True
                            process["operator_status_code"] = "global_pause"
                            process["operator_status"] = "Приостановлено общей паузой"
                            process["operator_explanation"] = "Desired сохранён, но выполнение удерживается общей паузой."
                        changed["drift_processes"] = []
                        changed["overall_status_code"] = "global_pause"
                        changed["overall_status"] = "Приостановлено общей паузой"
                        changed["overall_explanation"] = "Общая пауза временно удерживает все автоматические запуски."
                    changed["mutation"] = {
                        "status": "confirmed",
                        "persisted": True,
                        "runtime_readback_confirmed": True,
                        "lifecycle_readback_confirmed": True,
                    }
                    auto_current.clear()
                    auto_current.update(changed)
                    auto_current.pop("mutation", None)
                    if auto_mode["next"] == "stale_readback":
                        auto_mode["next"] = "serve_stale_once"
                        auto_mode["stale"] = before
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(changed, ensure_ascii=False),
                    )
                    return
                if auto_mode["next"] == "serve_stale_once":
                    auto_mode["next"] = ""
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps(auto_mode["stale"], ensure_ascii=False),
                    )
                    return
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(auto_current, ensure_ascii=False),
                )

            page.route(
                "**/v1/sheet-vitrina-v1/settings/auto-updates",
                handle_auto_updates,
            )
            page.route(
                "**/v1/sheet-vitrina-v1/settings/calculation-parameters",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "ready",
                            "current": {
                                "effective_date": "2026-07-01",
                                "parameters": {
                                    "buyout_rate": "0.91",
                                    "tax_rate": "0.06",
                                    "wb_agent_and_other_rate": "0.38",
                                    "acquiring_rate": "0",
                                    "wb_logistics_rate": "0",
                                    "wb_storage_rate": "0",
                                    "penalties_adjustments_rate": "0",
                                    "other_expense_rate": "0",
                                },
                            },
                            "history": [],
                            "reference": {
                                "weeks": [
                                    {"week_start": "2026-06-22", "week_end": "2026-06-28"},
                                    {"week_start": "2026-06-29", "week_end": "2026-07-05"},
                                    {"week_start": "2026-07-06", "week_end": "2026-07-12"},
                                ],
                                "rows": [
                                    {
                                        "label": "Агентское вознаграждение",
                                        "weekly_rate_pct": ["33.959072199101011", "0", None],
                                        "weighted_average_pct": "2.766203712870102",
                                        "note": "",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            page.goto(f"{base_url}/sheet-vitrina-v1/settings", wait_until="domcontentloaded")
            frame = page.locator('[data-settings-embed-frame]:not([hidden])')
            frame.wait_for()
            surface = page.frame_locator("[data-settings-embed-frame]")
            surface.locator('html[data-settings-ready="true"]').wait_for()
            surface.locator('[data-settings-group-button="user-directory"]').click()
            rendered_values = surface.locator(
                "#calculationReferenceRows tr:first-child td:not(:first-child)"
            )
            rendered_values.first.wait_for()
            _assert("embedded=1" in str(frame.get_attribute("src") or ""), "settings iframe source")
            _assert(
                rendered_values.all_inner_texts() == ["33,96%", "0%", "—", "2,77%"],
                "settings reference percentages are rounded for display",
            )
            group_labels = surface.locator("[data-settings-group-button]").all_inner_texts()
            _assert(
                group_labels
                == [
                    "Справочники",
                    "Расчётные параметры",
                    "Автообновления",
                    "Пользователи",
                ],
                f"settings top-level navigation changed: {group_labels}",
            )
            _assert(
                surface.get_by_role(
                    "heading",
                    name="Расчётные параметры",
                    exact=True,
                ).count()
                == 0,
                "calculation parameters panel must not repeat its top-level title",
            )
            surface.locator('[data-settings-group-button="auto-updates"]').click()
            surface.locator('[data-auto-update-process="warehouse_functional"]').wait_for()
            surface.locator("[data-vitrina-schedule-editor]").wait_for()
            surface.locator(
                "[data-vitrina-schedule-editor] thead th"
            ).nth(6).wait_for()
            _assert(
                surface.locator("[data-auto-update-process]").count() == 7,
                "auto-updates page must show only seven logical real processes",
            )
            _assert(
                surface.locator("[data-vitrina-schedule-editor]").count() == 1
                and surface.locator("[data-vitrina-schedule-policy]").count() == 1
                and surface.locator("[data-vitrina-schedule-save]").count() == 1,
                "Settings must contain the sole complete Vitrina schedule editor",
            )
            vitrina_editor_text = surface.locator(
                "[data-vitrina-schedule-editor]"
            ).inner_text()
            normalized_vitrina_editor_text = vitrina_editor_text.casefold()
            _assert(
                "следующий" in normalized_vitrina_editor_text
                and "последний успех" in normalized_vitrina_editor_text,
                "Vitrina Settings card must expose runtime schedule readback; "
                f"text={vitrina_editor_text!r}",
            )
            _assert(not auto_posts, "opening Auto-updates must be read-only")
            _assert(
                surface.locator("#autoUpdatesOverallStatus").inner_text().strip()
                == "Приостановлено общей паузой",
                "master hold status must be visible",
            )
            _assert(
                "Desired сохранён, но выполнение удерживается общей паузой."
                in surface.locator(
                    '[data-auto-update-process="warehouse_functional"]'
                ).inner_text(),
                "individual desired ON must remain visible while master is OFF",
            )
            _assert(
                surface.locator(
                    '[data-auto-update-toggle="autoanswers"]'
                ).count() == 0
                and surface.locator(
                    '[data-auto-update-toggle="feedback_complaints"]'
                ).count() == 0
                and surface.locator(
                    '[data-auto-update-toggle="spp_test"]'
                ).count() == 0,
                "feature-owned processes must have no Settings mutation controls",
            )
            _assert(
                "Только мониторинг"
                in surface.locator(
                    '[data-auto-update-process="autoanswers"]'
                ).inner_text(),
                "Autoanswers monitoring card must name its non-manage capability",
            )
            _assert(
                "Отзывы → Отзывы"
                in surface.locator(
                    '[data-auto-update-process="autoanswers"]'
                ).inner_text(),
                "Autoanswers monitoring card must name its feature control location",
            )
            autoanswers_text = surface.locator(
                '[data-auto-update-process="autoanswers"]'
            ).inner_text()
            _assert(
                "Режим" in autoanswers_text
                and "auto_all" in autoanswers_text
                and "Консервативные holds" in autoanswers_text
                and "$0.20" in autoanswers_text
                and "не подтверждённый расход" in autoanswers_text,
                f"Autoanswers monitoring card must explain lifecycle and holds: {autoanswers_text!r}",
            )
            surface.locator(
                '[data-auto-update-toggle="warehouse_functional"]'
            ).click()
            surface.locator("#autoUpdatesMessage").get_by_text(
                "Изменение сохранено и подтверждено повторным runtime readback.",
                exact=True,
            ).wait_for()
            _assert(
                auto_posts
                and auto_posts[-1]["action"] == "set_process"
                and auto_posts[-1]["process_key"] == "warehouse_functional"
                and auto_posts[-1]["desired"] is False
                and auto_posts[-1]["expected_revision"] == 1,
                f"individual toggle must use optimistic audited policy payload: {auto_posts}",
            )
            page.on("dialog", lambda dialog: dialog.accept())
            auto_mode["next"] = "backend_failure"
            surface.locator("#autoUpdatesMasterButton").click()
            surface.locator("#autoUpdatesMessage").get_by_text(
                re.compile("Не применено: synthetic backend failure.*Фактическое состояние перечитано")
            ).wait_for()
            _assert(
                surface.locator("#autoUpdatesOverallStatus").inner_text().strip()
                == "Приостановлено общей паузой",
                "backend failure must preserve and render the actual paused state",
            )
            surface.locator("#autoUpdatesMasterButton").click()
            surface.locator("#autoUpdatesMessage").get_by_text(
                "Изменение сохранено и подтверждено повторным runtime readback.",
                exact=True,
            ).wait_for()
            _assert(
                surface.locator("#autoUpdatesOverallStatus").inner_text().strip()
                == "Приостановлено пользователем",
                "successful resume must render confirmed actual state without reload",
            )
            auto_mode["next"] = "stale_readback"
            surface.locator("#autoUpdatesMasterButton").click()
            surface.locator("#autoUpdatesMessage").get_by_text(
                re.compile("Не применено: Изменение было принято, но повторный runtime readback не подтвердил")
            ).wait_for()
            _assert(
                surface.locator("#autoUpdatesOverallStatus").inner_text().strip()
                == "Приостановлено общей паузой",
                "failed post-write readback must render the later factual state",
            )
            surface.locator("#autoUpdatesMasterButton").click()
            surface.locator("#autoUpdatesMessage").get_by_text(
                "Изменение сохранено и подтверждено повторным runtime readback.",
                exact=True,
            ).wait_for()
            posts_before_reload = len(auto_posts)
            page.reload(wait_until="domcontentloaded")
            frame = page.locator('[data-settings-embed-frame]:not([hidden])')
            frame.wait_for()
            surface = page.frame_locator("[data-settings-embed-frame]")
            surface.locator('[data-settings-group-button="auto-updates"]').click()
            surface.locator('[data-auto-update-process="warehouse_functional"]').wait_for()
            _assert(
                surface.locator("#autoUpdatesOverallStatus").inner_text().strip()
                == "Приостановлено пользователем"
                and len(auto_posts) == posts_before_reload,
                "page reload must preserve confirmed resume state without mutation",
            )
            autoanswers_card = surface.locator(
                '[data-auto-update-process="autoanswers"]'
            )
            _assert(
                autoanswers_card.locator("details:not([open])").count() == 1,
                "Autoanswers technical identifiers must remain collapsed by default",
            )
            _assert(
                len(auto_posts) == posts_before_reload,
                "Autoanswers monitoring render must not mutate owner policy",
            )
            page.set_viewport_size({"width": 560, "height": 900})
            _assert(
                surface.locator("#autoUpdatesGroupPanel").evaluate(
                    "element => element.scrollWidth <= element.clientWidth + 1"
                ),
                "Auto-updates cards must not be clipped at narrow width",
            )
            _assert(
                surface.locator("[data-vitrina-schedule-editor]").evaluate(
                    "element => element.getBoundingClientRect().width > 0"
                ),
                "Vitrina schedule editor must remain visible at narrow width",
            )
            dark_background = surface.locator(
                '[data-auto-update-process="warehouse_functional"]'
            ).evaluate("element => getComputedStyle(element).backgroundColor")
            _assert(
                dark_background
                not in {"rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"},
                "Auto-updates cards must preserve dark-theme surface",
            )
        finally:
            browser.close()


def _assert_supplier_registry_stage_cost_frame(base_url: str) -> None:
    from playwright.sync_api import sync_playwright

    payload = {
        "status": "ready",
        "columns": [
            {
                "shipment_id": "shipment-browser-fixture",
                "title": "Invoice browser fixture",
                "subtitle": "2026-07-01",
                "order_status": "production",
                "order_status_display": "На производстве",
            }
        ],
        "sections": [
            {
                "section_id": "cargo_value",
                "title": "Стоимость товара",
                "rows": [
                    {
                        "row_id": "production_average_cost_rub",
                        "label": "Средняя себестоимость: на производстве",
                        "cells": {
                            "shipment-browser-fixture": {
                                "value": "100",
                                "display": "100 ₽",
                                "status": "complete",
                            }
                        },
                    },
                    {
                        "row_id": "china_to_ff_average_cost_rub",
                        "label": "Средняя себестоимость: Китай → FF",
                        "cells": {
                            "shipment-browser-fixture": {
                                "value": None,
                                "display": "—",
                            }
                        },
                    },
                ],
            }
        ],
        "warnings": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route(
                "**/v1/sheet-vitrina-v1/supply/supplier-shipments/registry",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(payload, ensure_ascii=False),
                ),
            )
            page.goto(
                f"{base_url}/sheet-vitrina-v1/vitrina?tab=factory-order",
                wait_until="domcontentloaded",
            )
            surface = page.frame_locator('[data-operator-embed-frame="factory-order"]')
            surface.locator('[data-supply-mode-button="shipment-registry"]').click()
            surface.get_by_text("Средняя себестоимость: на производстве", exact=True).wait_for()
            surface.get_by_text("Средняя себестоимость: Китай → FF", exact=True).wait_for()
        finally:
            browser.close()


def _assert_stock_report_frame(base_url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(
                f"{base_url}/sheet-vitrina-v1/vitrina?tab=warehouses",
                wait_until="domcontentloaded",
            )
            button = page.locator("[data-open-stock-report]")
            _assert(button.inner_text().strip() == "Отчёт об остатках", "stock report shell label")
            button.click()
            frame = page.locator('[data-warehouse-stock-report-frame]:not([hidden])')
            frame.wait_for()
            page.wait_for_function(
                "Boolean(document.querySelector('[data-warehouse-stock-report-frame]')?.getAttribute('src'))"
            )
            surface = page.frame_locator("[data-warehouse-stock-report-frame]")
            surface.get_by_role("heading", name="Отчёт по остаткам", exact=True).wait_for()
        finally:
            browser.close()


def _assert_sku_management_loaded(base_url: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.route(
                "**/v1/sheet-vitrina-v1/sku-management",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "rows": [
                                {
                                    "nm_id": 210183142,
                                    "sku": "browser fixture SKU",
                                    "name": "Browser fixture SKU",
                                    "risk": "low",
                                    "profit_rub": "123.45",
                                    "margin_pct": "0.25",
                                    "quality": "canonical_daily_projection",
                                }
                            ],
                            "settings": {"forecast": {}, "revision": 0, "table": {}},
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
            page.goto(
                f"{base_url}/sheet-vitrina-v1/vitrina?tab=sku-management",
                wait_until="domcontentloaded",
            )
            page.locator('[data-unified-tab-panel="sku-management"]:not([hidden])').wait_for()
            row = page.locator('[data-sku-row-nm-id="210183142"]')
            row.wait_for()
            _assert(row.locator('[data-sku-cell="profit_rub"]').inner_text().strip() == "123,45 ₽", "loaded SKU profit")
            _assert(row.locator('[data-sku-cell="margin_pct"]').inner_text().strip() == "25,0%", "loaded SKU margin")
            _assert(
                page.locator("[data-sku-management-status]").inner_text().strip().startswith("SKU:"),
                "loaded SKU management status",
            )
            _assert(
                not page.locator("[data-sku-management-error]").inner_text().strip(),
                "loaded SKU management error state",
            )
        finally:
            browser.close()


def _apply_functional_fixture(
    *,
    runtime,
    opening_plan: dict,
    backup_dir: Path,
) -> WarehouseFunctionalBlock:
    block = WarehouseFunctionalBlock(runtime=runtime, timestamp_factory=lambda: "2026-07-18T08:05:00Z")
    block._local_source_digest = lambda **_: "sha256:local-browser-fixture"  # type: ignore[method-assign]
    block._wb_supply_source_digest = lambda **_: "sha256:supply-browser-fixture"  # type: ignore[method-assign]
    lines: list[WarehouseLine] = []
    for document in opening_plan["documents"]:
        stage = str(document["warehouse_key"])
        for raw in document.get("lines") or []:
            quantity = Decimal(str(raw["quantity"]))
            if quantity <= 0:
                continue
            nm_id = int(raw["nm_id"])
            wac = Decimal(100 + nm_id)
            lines.append(
                WarehouseLine(
                    warehouse_key=stage,
                    nm_id=nm_id,
                    quantity=quantity,
                    capital=quantity * wac,
                    cost_covered_quantity=quantity,
                    quality="direct_24_06",
                    provenance={"fixture": True, "source_records": list((raw.get("provenance") or {}).get("source_records") or [])},
                    certified=True,
                    wb_quantity=quantity if stage == STAGE_WB else Decimal("0"),
                )
            )
    summaries = _summaries(lines)
    wb_lines = [item for item in lines if item.warehouse_key == STAGE_WB]
    opening_cost_map = [
        {
            "nm_id": nm_id,
            "ff_unit_cost_rub": str(Decimal(90 + nm_id)),
            "wb_unit_cost_rub": str(Decimal(100 + nm_id)),
            "quality": "direct_24_06",
            "provenance": {"fixture": True},
            "fingerprint": f"sha256:browser-{nm_id}",
        }
        for nm_id in sorted({item.nm_id for item in lines})
    ]
    snapshot = {
        "snapshot_id": "wbsnap_browser_fixture",
        "fetched_at": "2026-07-18T08:00:00Z",
        "snapshot_date": "2026-07-18",
        "requested_nm_ids": sorted({item.nm_id for item in lines}),
        "pagination_complete": True,
        "page_count": 1,
        "page_offsets": [0],
        "raw_row_count": len(wb_lines),
        "raw_rows_digest": "sha256:browser-rows",
        "raw_rows": [{"nmId": item.nm_id, "quantity": str(item.quantity)} for item in wb_lines],
        "items": [
            {
                "nm_id": item.nm_id,
                "quantity": str(item.quantity),
                "in_way_to_client": "0",
                "in_way_from_client": "0",
                "wb_contour_quantity": str(item.quantity),
            }
            for item in wb_lines
        ],
    }
    daily = [
        {
            "as_of_date": "2026-07-18",
            "nm_id": item.nm_id,
            "quantity": str(item.quantity),
            "wac_rub": str(item.wac),
            "capital_rub": str(item.capital),
            "quality": "periodic_snapshot_wac_provisional",
            "provenance": {"fixture": True},
            "fingerprint": f"sha256:browser-daily-{item.nm_id}",
        }
        for item in wb_lines
    ]
    functional_plan = {
        "contract_name": "sheet_vitrina_v1_warehouse_functional",
        "contract_version": "v2",
        "status": "dry_run_ready",
        "kind": "functional_cutover",
        "cutover_id": FUNCTIONAL_CUTOVER_ID,
        "captured_at": "2026-07-18T08:00:00Z",
        "effective_date": "2026-07-18",
        "base_active_version_id": "",
        "local_source_digest": "sha256:local-browser-fixture",
        "wb_supply_source_digest": "sha256:supply-browser-fixture",
        "source_watermarks": {"fixture": True},
        "absorbed_supply_revisions": {},
        "wb_snapshot": snapshot,
        "opening_cost_map": opening_cost_map,
        "historical_wb_cost_projection": daily,
        "lines": [_line_payload(item) for item in lines],
        "summaries": summaries,
        "unmatched_doprinato": [],
        "new_events": [],
        "movement_documents": [],
        "diff": {"changed_line_count": len(lines), "lines": []},
        "invariants": {
            "warehouse_count": len(STAGES),
            "negative_balance_count": 0,
            "positive_cost_gap_count": 0,
            "historical_wb_cost_gap_count": 0,
            "wb_quantity_source": "official_snapshot_only",
            "discrepancy_opening_zero": True,
            "ff_debit_coverage": {"uncovered_supply_count": 0},
        },
    }
    functional_plan["plan_fingerprint"] = _fingerprint(functional_plan)
    block.apply_plan(
        copy.deepcopy(functional_plan),
        confirm_fingerprint=functional_plan["plan_fingerprint"],
        backup_dir=backup_dir,
    )
    return block


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
