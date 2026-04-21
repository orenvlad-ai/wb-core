"""Browser smoke-check for the live web-vitrina page composition."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import time

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 21, 15, 0, tzinfo=timezone.utc)
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser smoke-check the web-vitrina page.")
    parser.add_argument("--base-url", default="", help="Existing base URL, for example https://api.selleros.pro")
    parser.add_argument("--as-of-date", default="", help="Optional as_of_date query parameter for historical read-side checks.")
    parser.add_argument(
        "--ignore-https-errors",
        action="store_true",
        help="Ignore TLS validation errors in the browser context.",
    )
    args = parser.parse_args()

    if args.base_url:
        result = run_browser_checks(
            args.base_url.rstrip("/"),
            ignore_https_errors=args.ignore_https_errors,
            as_of_date=args.as_of_date.strip(),
        )
        _print_summary(result)
        return

    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as ready_base_url:
        ready_result = run_browser_checks(ready_base_url, ignore_https_errors=False, as_of_date="")
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=False) as error_base_url:
        error_result = run_error_state_check(error_base_url, ignore_https_errors=False)
    _print_summary({
        "base_url": ready_result["base_url"],
        "table_rendered": ready_result["table_rendered"],
        "default_total_first": ready_result["default_total_first"],
        "default_sku_metric_cluster": ready_result["default_sku_metric_cluster"],
        "filter_controls": ready_result["filter_controls"],
        "status_badge": ready_result["status_badge"],
        "column_visibility": ready_result["column_visibility"],
        "horizontal_overscroll_guard": ready_result["horizontal_overscroll_guard"],
        "operator_link": ready_result["operator_link"],
        "metric_filter_applied": ready_result["metric_filter_applied"],
        "empty_state_after_search": ready_result["empty_state_after_search"],
        "reset_restores_table": ready_result["reset_restores_table"],
        "reset_restores_default_order": ready_result["reset_restores_default_order"],
        "historical_selector_present": ready_result["historical_selector_present"],
        "historical_selector_works": ready_result["historical_selector_works"],
        "historical_reset_works": ready_result["historical_reset_works"],
        "error_state": error_result["error_state"],
    })


class LocalWebVitrinaFixtureServer:
    def __init__(self, *, with_ready_snapshot: bool) -> None:
        self.with_ready_snapshot = with_ready_snapshot
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> str:
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        self.runtime_dir_obj = TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-browser-")
        runtime_dir = Path(self.runtime_dir_obj.name) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T15:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if self.with_ready_snapshot:
            start_date = datetime(2026, 4, 14, tzinfo=timezone.utc).date()
            for offset in range(7):
                snapshot_date = (start_date + timedelta(days=offset)).isoformat()
                runtime.save_sheet_vitrina_ready_snapshot(
                    current_state=current_state,
                    refreshed_at=f"{snapshot_date}T15:05:00Z",
                    plan=_build_plan(
                        as_of_date=snapshot_date,
                        first_nm_id=enabled[0].nm_id,
                        second_nm_id=enabled[1].nm_id,
                        first_group=enabled[0].group,
                    ),
                )

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-04-21T15:00:00Z",
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
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
        self.base_url = f"http://127.0.0.1:{config.port}"
        return self.base_url

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.runtime_dir_obj.cleanup()


def run_browser_checks(base_url: str, *, ignore_https_errors: bool, as_of_date: str = "") -> dict[str, object]:
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    if as_of_date:
        page_url = f"{page_url}?as_of_date={as_of_date}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=ignore_https_errors,
            viewport={"width": 1100, "height": 900},
        )
        context.route(
            "**/v1/sheet-vitrina-v1/web-vitrina*",
            lambda route: _route_page_composition_with_delay(route),
        )
        operator_page = context.new_page()
        operator_link = _check_operator_link(operator_page, base_url)
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="commit")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            total_rows = page.locator("[data-table-body] tr").count()
            if total_rows <= 0:
                raise AssertionError("web-vitrina table must render at least one row")
            final_badge = {
                "label": page.locator("[data-status-badge]").inner_text().strip(),
                "class_name": page.locator("[data-status-badge]").get_attribute("class") or "",
            }
            if final_badge["label"] != "Успешно" or "tone-success" not in final_badge["class_name"]:
                raise AssertionError(f"status badge must end in Russian success state, got {final_badge}")
            historical_panel_present = (
                page.locator("[data-history-calendar]").count() == 1
                and page.locator("[data-history-presets]").count() == 1
                and page.locator("[data-history-date-from]").count() == 1
                and page.locator("[data-history-date-to]").count() == 1
                and page.locator("[data-history-save]").count() == 1
                and page.locator("[data-history-reset]").count() == 1
            )
            if not historical_panel_present:
                raise AssertionError("historical period selector controls must be present on the page")
            preset_count = page.locator("[data-history-preset]").count()
            if preset_count < 5:
                raise AssertionError(f"historical period presets must be present, got {preset_count}")
            initial_meta = page.locator("[data-page-meta]").inner_text().strip()
            initial_order = _extract_visible_row_order(page)
            if not initial_order:
                raise AssertionError("web-vitrina must expose visible data rows")
            if initial_order[0]["scope_label"] != "ИТОГО":
                raise AssertionError(f"default order must start with TOTAL block, got {initial_order[0]}")
            sku_cluster_ok = _has_sku_metric_cluster(initial_order)
            if not sku_cluster_ok:
                raise AssertionError(f"default order must switch to sku->metrics clustering, got {initial_order[:8]}")

            filter_controls = {
                "search": page.locator("[data-filter-control='search']").count() == 1,
                "section": page.locator("[data-filter-control='section']").count() == 1,
                "group": page.locator("[data-filter-control='group']").count() == 1,
                "scope_kind": page.locator("[data-filter-control='scope_kind']").count() == 1,
                "metric": page.locator("[data-filter-control='metric']").count() == 1,
                "sort": page.locator("[data-filter-control='sort']").count() == 1,
            }
            if not all(filter_controls.values()):
                raise AssertionError(f"missing filter controls: {filter_controls}")

            metric_select = page.locator("[data-filter-control='metric']")
            metric_options = metric_select.locator("option").evaluate_all(
                "nodes => nodes.map(node => ({value: node.value, text: node.textContent || ''}))"
            )
            metric_option = next((item for item in metric_options if item["value"] != "__all__"), None)
            metric_filter_applied = False
            if metric_option is not None:
                metric_select.select_option(metric_option["value"])
                page.wait_for_timeout(150)
                metric_filter_applied = page.locator("[data-filter-summary]").inner_text().strip() != ""

            page.locator("[data-filter-control='search']").fill("zzzz-no-matches")
            page.wait_for_selector("[data-table-state]:not(.is-hidden)", timeout=5000)
            empty_state_after_search = "Пустой результат" in page.locator("[data-state-title]").inner_text()

            page.locator("[data-reset-filters]").click()
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
            reset_restores_table = page.locator("[data-table-body] tr").count() > 0
            reset_order = _extract_visible_row_order(page)
            reset_restores_default_order = reset_order == initial_order
            if not reset_restores_default_order:
                raise AssertionError(f"reset must restore canonical default order, got {reset_order[:8]}")

            column_visibility = _check_column_visibility_controls(page)
            horizontal_overscroll_guard = page.evaluate(
                """() => {
                  const node = document.querySelector('[data-table-scroll]');
                  if (!node) {
                    return {overscrollBehaviorX: '', leftPrevented: false, rightPrevented: false, maxScrollLeft: 0};
                  }
                  node.scrollLeft = 0;
                  const leftEvent = new WheelEvent('wheel', {deltaX: -120, deltaY: 0, cancelable: true});
                  const leftPrevented = !node.dispatchEvent(leftEvent);
                  node.scrollLeft = Math.max(0, node.scrollWidth - node.clientWidth);
                  const rightEvent = new WheelEvent('wheel', {deltaX: 120, deltaY: 0, cancelable: true});
                  const rightPrevented = !node.dispatchEvent(rightEvent);
                  return {
                    overscrollBehaviorX: getComputedStyle(node).overscrollBehaviorX || '',
                    leftPrevented: leftPrevented,
                    rightPrevented: rightPrevented,
                    maxScrollLeft: Math.max(0, node.scrollWidth - node.clientWidth)
                  };
                }"""
            )
            if horizontal_overscroll_guard["overscrollBehaviorX"] not in {"contain", "none"}:
                raise AssertionError(f"table scroll must keep horizontal overscroll contained, got {horizontal_overscroll_guard}")
            if not horizontal_overscroll_guard["leftPrevented"] or not horizontal_overscroll_guard["rightPrevented"]:
                raise AssertionError(f"table scroll must block browser-back overscroll at both edges, got {horizontal_overscroll_guard}")

            initial_query = page.evaluate("() => window.location.search")
            historical_selector_works = False
            historical_reset_works = False
            if not as_of_date:
                page.locator("[data-history-preset='week']").click()
                page.wait_for_function(
                    "() => document.querySelector('[data-history-date-from]').value === '2026-04-14' && document.querySelector('[data-history-date-to]').value === '2026-04-20'",
                    timeout=5000,
                )
                page.locator("[data-history-save]").click()
                page.wait_for_function(
                    "() => new URL(window.location.href).searchParams.get('date_from') === '2026-04-14' && new URL(window.location.href).searchParams.get('date_to') === '2026-04-20'",
                    timeout=5000,
                )
                page.wait_for_function(
                    "() => document.querySelector('[data-page-meta]').textContent.includes('as_of_date 2026-04-20')",
                    timeout=5000,
                )
                historical_selector_works = (
                    page.locator("[data-table-body] tr").count() > 0
                    and page.locator('[data-col-id^=\"date:\"]').count() >= 7
                )
                page.locator("[data-history-reset]").click()
                page.wait_for_function(
                    "() => !new URL(window.location.href).searchParams.has('date_from') && !new URL(window.location.href).searchParams.has('date_to') && !new URL(window.location.href).searchParams.has('as_of_date')",
                    timeout=5000,
                )
                page.wait_for_function(
                    "(expected) => document.querySelector('[data-page-meta]').textContent === expected",
                    arg=initial_meta,
                    timeout=5000,
                )
                historical_reset_works = page.locator("[data-table-body] tr").count() > 0 and page.evaluate(
                    "() => window.location.search"
                ) == initial_query
        finally:
            operator_page.close()
            context.close()
            browser.close()

    return {
        "base_url": base_url,
        "as_of_date": as_of_date,
        "table_rendered": total_rows > 0,
        "default_total_first": initial_order[0]["scope_label"] == "ИТОГО",
        "default_sku_metric_cluster": sku_cluster_ok,
        "filter_controls": filter_controls,
        "status_badge": final_badge,
        "column_visibility": column_visibility,
        "horizontal_overscroll_guard": horizontal_overscroll_guard,
        "operator_link": operator_link,
        "metric_filter_applied": metric_filter_applied,
        "empty_state_after_search": empty_state_after_search,
        "reset_restores_table": reset_restores_table,
        "reset_restores_default_order": reset_restores_default_order,
        "historical_selector_present": historical_panel_present,
        "historical_selector_works": historical_selector_works,
        "historical_reset_works": historical_reset_works,
    }


def run_error_state_check(base_url: str, *, ignore_https_errors: bool) -> dict[str, object]:
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=ignore_https_errors)
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="commit")
            page.wait_for_selector("[data-table-state]:not(.is-hidden)", timeout=20000)
            error_title = page.locator("[data-state-title]").inner_text().strip()
            error_body = page.locator("[data-state-body]").inner_text().strip()
        finally:
            context.close()
            browser.close()
    if error_title != "Витрина недоступна":
        raise AssertionError(f"error state title mismatch, got {error_title!r}")
    if "ready snapshot" not in error_body:
        raise AssertionError(f"error state body mismatch, got {error_body!r}")
    return {
        "error_state": {
            "title": error_title,
            "body": error_body,
        }
    }


def _print_summary(result: dict[str, object]) -> None:
    print("web_vitrina_browser_base_url: ok ->", result["base_url"])
    if result.get("as_of_date"):
        print("web_vitrina_browser_as_of_date: ok ->", result["as_of_date"])
    print("web_vitrina_browser_table: ok ->", result["table_rendered"])
    print("web_vitrina_browser_status_badge: ok ->", result["status_badge"])
    print("web_vitrina_browser_column_visibility: ok ->", result["column_visibility"])
    print("web_vitrina_browser_horizontal_overscroll_guard: ok ->", result["horizontal_overscroll_guard"])
    print("web_vitrina_browser_operator_link: ok ->", result["operator_link"])
    print("web_vitrina_browser_default_total_first: ok ->", result["default_total_first"])
    print("web_vitrina_browser_default_sku_metric_cluster: ok ->", result["default_sku_metric_cluster"])
    print("web_vitrina_browser_filters: ok ->", result["filter_controls"])
    print("web_vitrina_browser_metric_filter: ok ->", result["metric_filter_applied"])
    print("web_vitrina_browser_empty_state: ok ->", result["empty_state_after_search"])
    print("web_vitrina_browser_reset: ok ->", result["reset_restores_table"])
    print("web_vitrina_browser_reset_default_order: ok ->", result["reset_restores_default_order"])
    print("web_vitrina_browser_history_selector: ok ->", result["historical_selector_present"], result["historical_selector_works"])
    print("web_vitrina_browser_history_reset: ok ->", result["historical_reset_works"])
    if "error_state" in result:
        error_state = result["error_state"]
        print("web_vitrina_browser_error_state: ok ->", error_state["title"])


def _route_page_composition_with_delay(route: object) -> None:
    time.sleep(0.8)
    route.continue_()


def _check_operator_link(page: object, base_url: str) -> dict[str, str]:
    page.goto(base_url + DEFAULT_SHEET_OPERATOR_UI_PATH, wait_until="domcontentloaded")
    link = page.locator('[data-tab-panel="vitrina"] .eyebrow-link').first
    href = link.get_attribute("href") or ""
    if href != DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
        raise AssertionError(f"operator eyebrow link must target the new vitrina route, got {href!r}")
    with page.context.expect_page() as popup_info:
        link.click()
    popup_page = popup_info.value
    try:
        popup_page.wait_for_url("**" + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, timeout=5000)
        target_url = popup_page.url
    finally:
        popup_page.close()
    return {
        "href": href,
        "target_url": target_url,
    }


def _check_column_visibility_controls(page: object) -> dict[str, object]:
    manager = page.locator("[data-column-manager]")
    if manager.count() != 1:
        raise AssertionError("column visibility manager must be rendered once")
    page.evaluate("() => { const node = document.querySelector('[data-column-manager]'); if (node) { node.open = true; } }")
    page.locator('[data-column-visibility-id="metric_key"]').uncheck()
    page.locator('[data-column-visibility-id="scope_kind"]').uncheck()
    page.wait_for_function(
        """() =>
          document.querySelectorAll('[data-table-head] [data-col-id="metric_key"]').length === 0 &&
          document.querySelectorAll('[data-table-head] [data-col-id="scope_kind"]').length === 0
        """,
        timeout=5000,
    )
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.evaluate("() => { const node = document.querySelector('[data-column-manager]'); if (node) { node.open = true; } }")
    metric_hidden_after_reload = page.locator('[data-table-head] [data-col-id="metric_key"]').count() == 0
    scope_kind_hidden_after_reload = page.locator('[data-table-head] [data-col-id="scope_kind"]').count() == 0
    if not metric_hidden_after_reload or not scope_kind_hidden_after_reload:
        raise AssertionError("column visibility must persist across reload for optional columns")
    page.locator("[data-columns-reset]").click()
    page.wait_for_function(
        """() =>
          document.querySelectorAll('[data-table-head] [data-col-id="metric_key"]').length === 1 &&
          document.querySelectorAll('[data-table-head] [data-col-id="scope_kind"]').length === 1
        """,
        timeout=5000,
    )
    metric_checkbox_checked = page.locator('[data-column-visibility-id="metric_key"]').is_checked()
    scope_checkbox_checked = page.locator('[data-column-visibility-id="scope_kind"]').is_checked()
    if not metric_checkbox_checked or not scope_checkbox_checked:
        raise AssertionError("column visibility reset must restore optional column checkboxes")
    return {
        "persisted_hidden_columns": ["metric_key", "scope_kind"],
        "metric_hidden_after_reload": metric_hidden_after_reload,
        "scope_kind_hidden_after_reload": scope_kind_hidden_after_reload,
        "metric_checkbox_checked_after_reset": metric_checkbox_checked,
        "scope_checkbox_checked_after_reset": scope_checkbox_checked,
    }


def _build_plan(
    *,
    as_of_date: str,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"web-vitrina-browser-fixture-{as_of_date}",
        as_of_date=as_of_date,
        date_columns=[as_of_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="historical_import",
                slot_label="Historical import",
                column_date=as_of_date,
            ),
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:C7",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1000],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 990],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1090],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 11.5],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 10.5],
                ],
                row_count=6,
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K1",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[],
                row_count=0,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _extract_visible_row_order(page: object) -> list[dict[str, str]]:
    return page.locator("[data-table-body] tr").evaluate_all(
        """rows => rows
          .map(row => {
            const scopeLabelNode = row.querySelector('td[data-col-id="scope_label"]');
            const metricKeyNode = row.querySelector('td[data-col-id="metric_key"]');
            const scopeKindNode = row.querySelector('td[data-col-id="scope_kind"]');
            if (!scopeLabelNode || !metricKeyNode || !scopeKindNode) {
              return null;
            }
            return {
              scope_label: (scopeLabelNode.getAttribute('title') || scopeLabelNode.textContent || '').trim(),
              metric_key: (metricKeyNode.getAttribute('title') || metricKeyNode.textContent || '').trim(),
              scope_kind: (scopeKindNode.getAttribute('title') || scopeKindNode.textContent || '').trim(),
            };
          })
          .filter(Boolean)"""
    )


def _has_sku_metric_cluster(rows: list[dict[str, str]]) -> bool:
    sku_rows = [row for row in rows if row.get("scope_kind") == "SKU"]
    if len(sku_rows) < 2:
        return False
    return (
        sku_rows[0].get("scope_label") == sku_rows[1].get("scope_label")
        and sku_rows[0].get("metric_key") != sku_rows[1].get("metric_key")
    )


if __name__ == "__main__":
    main()
