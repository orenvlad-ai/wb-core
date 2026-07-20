#!/usr/bin/env python3
"""Playwright smoke for the shared warehouse UI and legacy FF transition."""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import copy
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_stocks_smoke import _block, _seed_runtime  # noqa: E402
from apps.warehouse_stocks_production_ui_flow import (  # noqa: E402
    _metric_date_coverage,
    _supplier_financial_detail_url,
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
    _assert(_visible_money("78\u00a0086,09 RUB") == Decimal("78086.09"), "localized RUB money")
    _assert(_visible_money("1\u202f234,50 CNY") == Decimal("1234.50"), "localized CNY money")
    _assert(
        _supplier_financial_detail_url("https://example.invalid/", "shipment id/1")
        == "https://example.invalid/sheet-vitrina-v1/supplier?embedded=operator&shipment_id=shipment%20id%2F1&tab=documents",
        "operator supplier financial detail URL",
    )
    _assert_metric_coverage_applicability()
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
            with page.expect_request(
                lambda request: detail_fragment in request.url and request.method == "GET",
                timeout=5000,
            ):
                page.locator("[data-warehouse-sync]").click()
            page.wait_for_function(
                "() => ((document.querySelector('[data-warehouse-status]') || {}).textContent || '').trim() !== 'Загрузка…'"
            )
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

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{base_url}/sheet-vitrina-v1/vitrina", wait_until="domcontentloaded")
            page.locator('[data-unified-tab-button="warehouses"]').click()
            page.locator('[data-unified-tab-panel="warehouses"]:not([hidden])').wait_for()
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
