"""Browser smoke for the operator stock-report sortable table."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_stock_report_smoke import (  # noqa: E402
    BUNDLE_FIXTURE,
    CAPTURED_AT,
    NOW,
    _build_plan,
    _closed_sku_values,
    _seed_nomenclature,
    _seed_sales_history,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with _StockReportFixtureServer() as base_url:
        result = run_browser_checks(base_url)
    print("stock_report_table_browser: ok ->", result["row_count"], "rows")
    print("stock_report_table_sort_stock: ok ->", result["stock_sort_first"])
    print("stock_report_table_sort_promo: ok ->", result["promo_sort_first"])


def run_browser_checks(base_url: str) -> dict[str, object]:
    page_url = f"{base_url}{DEFAULT_SHEET_OPERATOR_UI_PATH}?embedded_tab=reports"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 980, "height": 850})
        page = context.new_page()
        stock_report_requests: list[str] = []
        page.on(
            "request",
            lambda request: stock_report_requests.append(request.url)
            if "/v1/sheet-vitrina-v1/stock-report" in request.url
            else None,
        )
        try:
            page.goto(page_url, wait_until="domcontentloaded")
            page.locator('[data-report-section-button="stock"]').click()
            page.wait_for_timeout(500)
            if stock_report_requests:
                raise AssertionError(f"stock report must not auto-fetch before Рассчитать, got {stock_report_requests}")
            page.locator(
                "#stockReportStatus",
                has_text="Настройте SKU, период и столбцы, затем нажмите «Рассчитать».",
            ).wait_for(timeout=10000)
            if not page.locator("#stockReportContent").evaluate("node => node.hidden"):
                raise AssertionError("stock report table must stay hidden before the first manual calculation")
            page.locator("#stockReportColumnSelector").wait_for(timeout=10000)
            page.locator("#stockReportColumnSummary", has_text="Столбцы: база").wait_for(timeout=10000)

            page.locator("#stockReportSalesAvgPeriodDays").fill("3")
            with page.expect_response(lambda response: "/v1/sheet-vitrina-v1/stock-report" in response.url):
                page.locator("#stockReportApplyButton").click()
            page.locator("#stockReportRows table.stock-report-table").wait_for(timeout=10000)
            page.locator("#stockReportLead", has_text="Период усреднения продаж: 3").wait_for(timeout=10000)
            if len(stock_report_requests) != 1:
                raise AssertionError(f"manual calculation must fetch stock-report exactly once, got {stock_report_requests}")
            row_count = page.locator("#stockReportRows tbody tr").count()
            if row_count < 4:
                raise AssertionError(f"stock report table must render active SKU rows, got {row_count}")
            for token in ("SKU", "Акция", "Ост. всего", "Ноль", "Центр", "СЗ", "Прив.", "Урал", "Юг", "Прод./дн.", "Дн. всего"):
                page.locator("#stockReportRows thead", has_text=token).wait_for(timeout=10000)
            header_text = page.locator("#stockReportRows thead").inner_text()
            if "Остаток всего" in header_text or "Дней: Центральный" in header_text:
                raise AssertionError(f"stock report table must use short visible labels, got {header_text!r}")
            if page.locator("#stockReportRows tbody", has_text="Да").count() < 1:
                raise AssertionError("promotion participation column must render Да")
            if page.locator("#stockReportRows tbody", has_text="Нет").count() < 1:
                raise AssertionError("promotion participation column must render Нет")
            scroll_evidence = page.locator("[data-stock-report-table-wrap]").evaluate(
                """node => {
                    const firstHeader = node.querySelector('th:first-child');
                    const firstCell = node.querySelector('td:first-child');
                    node.scrollLeft = 120;
                    const headerStyle = window.getComputedStyle(firstHeader);
                    const cellStyle = window.getComputedStyle(firstCell);
                    return {
                        clientWidth: node.clientWidth,
                        scrollWidth: node.scrollWidth,
                        scrollLeft: node.scrollLeft,
                        headerPosition: headerStyle.position,
                        headerLeft: headerStyle.left,
                        cellPosition: cellStyle.position,
                        cellLeft: cellStyle.left
                    };
                }"""
            )
            if scroll_evidence["scrollWidth"] <= scroll_evidence["clientWidth"] or scroll_evidence["scrollLeft"] <= 0:
                raise AssertionError(f"stock report wrapper must scroll horizontally, got {scroll_evidence}")
            if scroll_evidence["headerPosition"] != "sticky" or scroll_evidence["cellPosition"] != "sticky":
                raise AssertionError(f"SKU column must be sticky, got {scroll_evidence}")
            if scroll_evidence["headerLeft"] != "0px" or scroll_evidence["cellLeft"] != "0px":
                raise AssertionError(f"SKU sticky left must be 0px, got {scroll_evidence}")

            before_sort_first = _first_sku_cell_text(page)
            page.locator('[data-stock-report-sort="stock_total"]').click()
            page.wait_for_timeout(250)
            if len(stock_report_requests) != 1:
                raise AssertionError("stock report sort must re-render locally without fetch")
            stock_sort_first = _first_sku_cell_text(page)
            stock_sort_value = _first_row_cell_text(page, 2)
            if stock_sort_first == before_sort_first or stock_sort_value != "40":
                raise AssertionError(
                    "stock_total ascending sort must reorder rows by raw numeric stock; "
                    f"before={before_sort_first!r}, after={stock_sort_first!r}, stock={stock_sort_value!r}"
                )

            page.locator('[data-stock-report-sort="promotion_participation"]').click()
            page.wait_for_timeout(250)
            if len(stock_report_requests) != 1:
                raise AssertionError("stock report promo sort must not fetch")
            promo_sort_first = _first_sku_cell_text(page)
            promo_sort_value = _first_row_cell_text(page, 1)
            if promo_sort_value != "Нет":
                raise AssertionError(f"promo ascending sort must put Нет before Да/null, got {promo_sort_first!r} / {promo_sort_value!r}")
            page.locator('[data-stock-report-sort="promotion_participation"]').click()
            promo_desc_first = _first_sku_cell_text(page)
            promo_desc_value = _first_row_cell_text(page, 1)
            if promo_desc_value != "Да":
                raise AssertionError(f"promo descending sort must put Да first, got {promo_desc_first!r} / {promo_desc_value!r}")
            page.wait_for_timeout(250)
            if len(stock_report_requests) != 1:
                raise AssertionError("stock report promo sort direction toggle must not fetch")

            page.locator("#stockReportColumnSelector").click()
            page.locator("#stockReportColumnsAllButton").click()
            page.locator("#stockReportRows thead", has_text="Дн. Центр").wait_for(timeout=10000)
            page.wait_for_timeout(250)
            if len(stock_report_requests) != 1:
                raise AssertionError("stock report column visibility change must not fetch")
            persisted_columns = page.evaluate(
                """() => {
                    const raw = window.localStorage.getItem('wb-core:sheet-vitrina-v1:operator-ui-state:v1');
                    return raw ? JSON.parse(raw) : {};
                }"""
            )
            if "stock_report_visible_column_keys" not in persisted_columns:
                raise AssertionError(f"stock report column visibility must persist, got {persisted_columns}")

            page.locator("#stockReportSkuSelector").click()
            first_checkbox = page.locator("#stockReportSkuList input[type='checkbox']").first
            first_checkbox.uncheck()
            page.wait_for_timeout(250)
            if len(stock_report_requests) != 1:
                raise AssertionError("stock report SKU draft changes must not fetch")
            page.keyboard.press("Escape")
            with page.expect_response(lambda response: "/v1/sheet-vitrina-v1/stock-report" in response.url):
                page.locator("#stockReportApplyButton").click()
            page.locator("#stockReportRows table.stock-report-table").wait_for(timeout=10000)
            if len(stock_report_requests) != 2:
                raise AssertionError("next Рассчитать after SKU draft change must fetch exactly once")
        finally:
            browser.close()
    return {
        "row_count": row_count,
        "stock_sort_first": stock_sort_first,
        "promo_sort_first": promo_sort_first,
        "stock_report_request_count": len(stock_report_requests),
        "scroll_evidence": scroll_evidence,
    }


def _first_sku_cell_text(page: object) -> str:
    return str(page.locator("#stockReportRows tbody tr").first.locator("td").first.inner_text()).strip()


def _first_row_cell_text(page: object, index: int) -> str:
    return str(page.locator("#stockReportRows tbody tr").first.locator("td").nth(index).inner_text()).strip()


class _StockReportFixtureServer:
    def __enter__(self) -> str:
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        self.runtime_dir_obj = TemporaryDirectory(prefix="sheet-vitrina-stock-report-browser-")
        runtime_dir = Path(self.runtime_dir_obj.name) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at=CAPTURED_AT)
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        nm_ids = [item.nm_id for item in enabled[:4]]
        metric_labels = {item.metric_key: item.label_ru for item in current_state.metrics_v2 if item.enabled}
        _seed_nomenclature(runtime, nm_ids[0])
        _seed_sales_history(runtime, nm_ids)
        for snapshot_date in ["2026-04-15", "2026-04-16", "2026-04-17", "2026-04-18"]:
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{snapshot_date}T09:05:00Z",
                plan=_build_plan(
                    as_of_date=snapshot_date,
                    current_state=current_state,
                    metric_labels=metric_labels,
                    closed_sku_values=_closed_sku_values(snapshot_date, nm_ids),
                    today_sku_values={nm_id: {"stock_total": 999.0} for nm_id in nm_ids},
                ),
            )

        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: CAPTURED_AT,
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        self.server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.runtime_dir_obj.cleanup()


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
