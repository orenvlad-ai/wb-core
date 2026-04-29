"""Browser smoke-check for the live web-vitrina page composition."""

from __future__ import annotations

import argparse
from dataclasses import asdict
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
    parser.add_argument("--base-url", default="", help="Existing base URL, for example http://89.191.226.88")
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
            expected_percent_rows=None,
            expect_cheap_refresh_same_freshness=None,
            expect_data_refresh_changes_freshness=None,
            expected_final_badge_tone=None,
            run_actions=False,
        )
        _print_summary(result)
        return

    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as ready_base_url:
        ready_result = run_browser_checks(
            ready_base_url,
            ignore_https_errors=False,
            as_of_date="",
            expected_percent_rows={
                "avg_addToCartConversion#1": "11,50%",
                "avg_addToCartConversion#2": "10,50%",
            },
            expect_cheap_refresh_same_freshness=True,
            expect_data_refresh_changes_freshness=True,
            expected_final_badge_tone="error",
        )
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=False) as error_base_url:
        error_result = run_error_state_check(error_base_url, ignore_https_errors=False)
    _print_summary({
        "base_url": ready_result["base_url"],
        "table_rendered": ready_result["table_rendered"],
        "top_panel": ready_result["top_panel"],
        "default_total_first": ready_result["default_total_first"],
        "default_sku_metric_cluster": ready_result["default_sku_metric_cluster"],
        "sku_separators": ready_result["sku_separators"],
        "filter_controls": ready_result["filter_controls"],
        "status_summary": ready_result["status_summary"],
        "activity_surface": ready_result["activity_surface"],
        "compact_widths": ready_result["compact_widths"],
        "percent_formatting": ready_result["percent_formatting"],
        "operator_screen_layout": ready_result["operator_screen_layout"],
        "unified_tab_navigation": ready_result["unified_tab_navigation"],
        "load_refresh_action": ready_result["load_refresh_action"],
        "right_edge_spacer": ready_result["right_edge_spacer"],
        "column_visibility": ready_result["column_visibility"],
        "horizontal_overscroll_guard": ready_result["horizontal_overscroll_guard"],
        "operator_link": ready_result["operator_link"],
        "metric_filter_applied": ready_result["metric_filter_applied"],
        "empty_state_after_search": ready_result["empty_state_after_search"],
        "reset_restores_table": ready_result["reset_restores_table"],
        "reset_restores_default_order": ready_result["reset_restores_default_order"],
        "historical_selector_present": ready_result["historical_selector_present"],
        "preset_calendar_sync": ready_result["preset_calendar_sync"],
        "historical_selector_works": ready_result["historical_selector_works"],
        "historical_reset_works": ready_result["historical_reset_works"],
        "error_state": error_result["error_state"],
    })


class LocalWebVitrinaFixtureServer:
    def __init__(self, *, with_ready_snapshot: bool, now: datetime | None = None) -> None:
        self.with_ready_snapshot = with_ready_snapshot
        self.now = now or NOW
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""
        self._refresh_counter = 0

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
            refreshed_at_factory=self._next_refreshed_at,
            now_factory=lambda: self.now,
            sheet_load_runner=_stub_sheet_load_runner,
        )
        entrypoint.handle_sheet_refresh_request = lambda as_of_date=None, auto_load=False: _stub_sheet_refresh_request(
            entrypoint,
            runtime,
            as_of_date=as_of_date,
        )
        entrypoint.start_sheet_refresh_job = (
            lambda as_of_date=None, auto_load=False: entrypoint.operator_jobs.start(
                operation="refresh",
                runner=lambda log: _stub_sheet_refresh_request(
                    entrypoint,
                    runtime,
                    as_of_date=as_of_date,
                    log=log,
                ),
            )
        )
        _start_completed_refresh_job(entrypoint, runtime)
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

    def _next_refreshed_at(self) -> str:
        refreshed_at = self.now + timedelta(minutes=10 + self._refresh_counter)
        self._refresh_counter += 1
        return refreshed_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_browser_checks(
    base_url: str,
    *,
    ignore_https_errors: bool,
    as_of_date: str = "",
    expected_percent_rows: dict[str, str] | None = None,
    expect_cheap_refresh_same_freshness: bool | None = None,
    expect_data_refresh_changes_freshness: bool | None = None,
    expected_final_badge_tone: str | None = None,
    run_actions: bool = True,
) -> dict[str, object]:
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    if as_of_date:
        page_url = f"{page_url}?as_of_date={as_of_date}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=ignore_https_errors,
            viewport={"width": 1100, "height": 900},
        )
        source_status_detail_urls: list[str] = []

        def route_page_composition(route: object) -> None:
            if "include_source_status=1" in route.request.url:
                source_status_detail_urls.append(route.request.url)
            _route_page_composition_with_delay(route)

        context.route(
            "**/v1/sheet-vitrina-v1/web-vitrina*",
            route_page_composition,
        )
        operator_page = context.new_page()
        operator_link = _check_operator_link(operator_page, base_url)
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="commit")
            page.wait_for_load_state("domcontentloaded")
            top_panel_state = {
                "status_badge_count": page.locator("[data-status-badge]").count(),
                "json_connect_count": page.locator("[data-open-contract]").count(),
                "progress_count": page.locator("[data-global-progress]").count(),
                "progress_hidden": page.locator("[data-global-progress]").evaluate("node => node.hidden"),
            }
            if top_panel_state["status_badge_count"] != 0 or top_panel_state["json_connect_count"] != 0:
                raise AssertionError(f"top panel must not render JSON Connect or a permanent status badge, got {top_panel_state}")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            total_rows = page.locator("[data-table-body] tr").count()
            if total_rows <= 0:
                raise AssertionError("web-vitrina table must render at least one row")
            initial_summary_cards = _read_summary_cards(page)
            status_summary = initial_summary_cards.get("status", {})
            initial_unloaded_activity_surface = _read_activity_surface(
                page,
                allow_empty_log=True,
            )
            if initial_unloaded_activity_surface["loading"]["rows"]:
                raise AssertionError(
                    f"loading details must not auto-render before click, got {initial_unloaded_activity_surface}"
                )
            if initial_unloaded_activity_surface["loading"]["groups"]:
                raise AssertionError(
                    f"initial loading state must not render empty group shells, got {initial_unloaded_activity_surface}"
                )
            if "Источники группы пока не представлены" in initial_unloaded_activity_surface["loading"].get("empty_text", ""):
                raise AssertionError(
                    f"initial unloaded state must not look like missing status payload, got {initial_unloaded_activity_surface}"
                )
            if initial_unloaded_activity_surface["loading"].get("source_status_button") != "Загрузить":
                raise AssertionError(f"source-status load button mismatch, got {initial_unloaded_activity_surface}")
            page.locator("[data-source-status-load]").click()
            page.wait_for_selector("[data-loading-source]", timeout=20000)
            if not source_status_detail_urls:
                raise AssertionError("source-status details request was not captured")
            latest_details_url = source_status_detail_urls[-1]
            if "include_source_status=1" not in latest_details_url or "as_of_date=" not in latest_details_url:
                raise AssertionError(
                    f"source-status lazy-load must request explicit visible snapshot_as_of_date, got {latest_details_url}"
                )
            if "date_from=" in latest_details_url or "date_to=" in latest_details_url:
                raise AssertionError(
                    f"source-status lazy-load must not use a date window as the snapshot key, got {latest_details_url}"
                )
            if base_url.startswith("http://127.0.0.1") and "as_of_date=2026-04-20" not in latest_details_url:
                raise AssertionError(
                    f"fixture source-status details must use visible snapshot 2026-04-20, got {latest_details_url}"
                )
            initial_activity_surface = _read_activity_surface(
                page,
                allow_empty_log=expected_percent_rows is None,
            )
            operator_screen_layout = _check_operator_screen_layout(page)
            unified_tab_navigation = _check_unified_tab_navigation(page)
            first_loading_row = (initial_activity_surface["loading"]["rows"] or [None])[0]
            if not isinstance(first_loading_row, dict):
                raise AssertionError(f"activity surface must expose at least one loading row, got {initial_activity_surface}")
            if first_loading_row.get("source") != "Цены и скидки":
                raise AssertionError(f"activity titles must prefer human Russian labels, got {initial_activity_surface}")
            if not first_loading_row.get("today_reason") or not first_loading_row.get("yesterday_reason"):
                raise AssertionError(f"warning/error activity items must explain the reason in Russian, got {initial_activity_surface}")
            if "Цена со скидкой" not in str(first_loading_row.get("metrics") or ""):
                raise AssertionError(f"activity rows must expose Russian metric labels, got {initial_activity_surface}")
            if "POST /api/v2/list/goods/filter" not in str(first_loading_row.get("technical") or ""):
                raise AssertionError(f"activity rows must keep the technical endpoint, got {initial_activity_surface}")
            historical_panel_present = (
                page.locator("[data-history-panel]").count() == 1
                and page.locator("[data-history-toggle]").count() == 1
                and page.locator("[data-history-label]").count() == 1
                and page.locator("[data-history-prev-month]").count() == 1
                and page.locator("[data-history-next-month]").count() == 1
                and page.locator("[data-history-month-label]").count() == 1
                and page.locator("[data-history-calendar]").count() == 1
                and page.locator("[data-history-presets]").count() == 1
                and page.locator("[data-history-date-from]").count() == 1
                and page.locator("[data-history-date-to]").count() == 1
                and page.locator("[data-history-save]").count() == 1
                and page.locator("[data-history-reset]").count() == 1
            )
            if not historical_panel_present:
                raise AssertionError("historical period selector controls must be present on the page")
            initial_history_state = page.evaluate(
                """() => ({
                  label: (document.querySelector('[data-history-label]') || {}).textContent || '',
                  popoverHidden: !!(document.querySelector('[data-history-popover]') || {}).hidden,
                  dateFrom: (document.querySelector('[data-history-date-from]') || {}).value || '',
                  dateTo: (document.querySelector('[data-history-date-to]') || {}).value || ''
                })"""
            )
            if initial_history_state["label"].strip() != "17.04.2026 - 20.04.2026":
                raise AssertionError(f"default compact history label mismatch, got {initial_history_state}")
            if not initial_history_state["popoverHidden"]:
                raise AssertionError(f"history picker popover must be closed by default, got {initial_history_state}")
            if initial_history_state["dateFrom"] != "2026-04-17" or initial_history_state["dateTo"] != "2026-04-20":
                raise AssertionError(f"default history range must be latest four days, got {initial_history_state}")
            visible_body_text = page.locator("body").inner_text()
            for forbidden_history_text in (
                "История",
                "mode:",
                "supported query:",
                "default as_of_date",
                "route state",
                "Открыт period window",
                "Доступно snapshots",
            ):
                if forbidden_history_text in visible_body_text:
                    raise AssertionError(f"compact picker must not expose technical history text {forbidden_history_text!r}")
            preset_count = page.locator("[data-history-preset]").count()
            if preset_count < 5:
                raise AssertionError(f"historical period presets must be present, got {preset_count}")
            if page.locator("[data-history-mode-badge]").count() != 0:
                raise AssertionError("history panel must not keep the extra Период badge")
            initial_meta = page.locator("[data-page-meta]").inner_text().strip()
            initial_order = _extract_visible_row_order(page)
            if not initial_order:
                raise AssertionError("web-vitrina must expose visible data rows")
            if initial_order[0]["scope_label"] != "ИТОГО":
                raise AssertionError(f"default order must start with TOTAL block, got {initial_order[0]}")
            sku_cluster_ok = _has_sku_metric_cluster(initial_order)
            if not sku_cluster_ok:
                raise AssertionError(f"default order must switch to sku->metrics clustering, got {initial_order[:8]}")
            sku_separators = _check_sku_separators(page)
            right_edge_spacer = _check_right_edge_spacer(page)

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
            table_toolbar = page.evaluate(
                """() => {
                  const toolbar = document.querySelector('[data-table-toolbar]');
                  const tableShell = document.querySelector('[data-table-shell]');
                  const labels = toolbar ? Array.from(toolbar.querySelectorAll('.filter-label')).map((node) => (node.textContent || '').trim()).filter(Boolean) : [];
                  const rect = toolbar ? toolbar.getBoundingClientRect() : {height: 0};
                  const beforeTable = !!toolbar && !!tableShell && !!(toolbar.compareDocumentPosition(tableShell) & Node.DOCUMENT_POSITION_FOLLOWING);
                  return {
                    exists: !!toolbar,
                    beforeTable: beforeTable,
                    labels: labels,
                    height: Math.round(rect.height),
                    oldHeadingCount: Array.from(document.querySelectorAll('h2')).filter((node) => (node.textContent || '').trim() === 'Фильтры и настройки').length,
                    oldPanelTextVisible: (document.body.innerText || '').includes('Search/select/sort и выбор видимых столбцов'),
                    oldResetTextVisible: (document.body.innerText || '').includes('Сбросить фильтры'),
                    columnManagerCount: document.querySelectorAll('[data-column-manager]').length,
                    columnResetCount: document.querySelectorAll('[data-columns-reset]').length
                  };
                }"""
            )
            expected_toolbar_labels = {"Диапазон", "Поиск", "Секции", "Группа", "Scope", "Метрики", "Столбцы", "Сортировка"}
            missing_toolbar_labels = expected_toolbar_labels.difference(set(table_toolbar["labels"]))
            if (
                not table_toolbar["exists"]
                or not table_toolbar["beforeTable"]
                or table_toolbar["oldHeadingCount"]
                or table_toolbar["oldPanelTextVisible"]
                or table_toolbar["oldResetTextVisible"]
                or table_toolbar["columnManagerCount"] != 1
                or table_toolbar["columnResetCount"] != 1
                or missing_toolbar_labels
            ):
                raise AssertionError(
                    f"table controls must live in one compact toolbar above the table, got {table_toolbar}, missing={missing_toolbar_labels}"
                )
            if table_toolbar["height"] > 130:
                raise AssertionError(f"table toolbar must stay compact, got {table_toolbar}")
            compact_widths = _measure_compact_widths(page, strict=expected_percent_rows is not None)
            percent_formatting = _check_percent_formatting(page, expected_rows=expected_percent_rows)
            load_refresh_action = (
                _check_load_refresh_action(
                    page,
                    previous_summary_cards=initial_summary_cards,
                    previous_activity_surface=initial_activity_surface,
                    expect_freshness_change=expect_data_refresh_changes_freshness,
                    expected_final_badge_tone=expected_final_badge_tone,
                )
                if run_actions
                else {"skipped": "read-only public base-url mode"}
            )

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
            preset_calendar_sync = False
            if not as_of_date and run_actions:
                page.locator("[data-history-toggle]").click()
                page.wait_for_selector("[data-history-popover]:not([hidden])", timeout=5000)
                compact_popover_state = page.evaluate(
                    """() => {
                      const popover = document.querySelector('[data-history-popover]');
                      const rect = popover ? popover.getBoundingClientRect() : {width: 0, height: 0};
                      return {
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        monthCount: document.querySelectorAll('[data-history-month]').length,
                        monthLabel: (document.querySelector('[data-history-month-label]') || {}).textContent || '',
                        prevVisible: !!document.querySelector('[data-history-prev-month]'),
                        nextVisible: !!document.querySelector('[data-history-next-month]'),
                        dayCount: document.querySelectorAll('[data-history-day]').length
                      };
                    }"""
                )
                if compact_popover_state["monthCount"] != 1 or compact_popover_state["width"] > 380 or compact_popover_state["height"] > 520:
                    raise AssertionError(f"history picker popover must stay compact and one-month, got {compact_popover_state}")
                if not compact_popover_state["prevVisible"] or not compact_popover_state["nextVisible"] or compact_popover_state["dayCount"] < 28:
                    raise AssertionError(f"history picker must expose month navigation and calendar grid, got {compact_popover_state}")
                page.locator("[data-history-preset='week']").click()
                page.wait_for_function(
                    "() => document.querySelector('[data-history-date-from]').value === '2026-04-14' && document.querySelector('[data-history-date-to]').value === '2026-04-20'",
                    timeout=5000,
                )
                preset_calendar_sync = _check_preset_calendar_sync(page)
                page.locator("[data-history-save]").click()
                page.wait_for_function(
                    "() => new URL(window.location.href).searchParams.get('date_from') === '2026-04-14' && new URL(window.location.href).searchParams.get('date_to') === '2026-04-20'",
                    timeout=5000,
                )
                page.wait_for_function("() => document.querySelector('[data-history-popover]').hidden", timeout=5000)
                page.wait_for_function(
                    "() => document.querySelector('[data-page-meta]').textContent.includes('Снимок: 20.04.2026')",
                    timeout=5000,
                )
                historical_selector_works = (
                    page.locator("[data-table-body] tr").count() > 0
                    and page.locator('[data-col-id^=\"date:\"]').count() >= 7
                )
                page.locator("[data-history-toggle]").click()
                page.wait_for_selector("[data-history-popover]:not([hidden])", timeout=5000)
                page.locator("[data-history-reset]").click()
                page.wait_for_function(
                    "() => !new URL(window.location.href).searchParams.has('date_from') && !new URL(window.location.href).searchParams.has('date_to') && !new URL(window.location.href).searchParams.has('as_of_date')",
                    timeout=5000,
                )
                page.wait_for_function("() => document.querySelector('[data-history-popover]').hidden", timeout=5000)
                page.wait_for_function(
                    "() => (document.querySelector('[data-history-label]').textContent || '').trim() === '17.04.2026 - 20.04.2026'",
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
        "top_panel": top_panel_state,
        "default_total_first": initial_order[0]["scope_label"] == "ИТОГО",
        "default_sku_metric_cluster": sku_cluster_ok,
        "sku_separators": sku_separators,
        "right_edge_spacer": right_edge_spacer,
        "filter_controls": filter_controls,
        "table_toolbar": table_toolbar,
        "status_summary": status_summary,
        "summary_cards": initial_summary_cards,
        "activity_surface": initial_activity_surface,
        "compact_widths": compact_widths,
        "percent_formatting": percent_formatting,
        "operator_screen_layout": operator_screen_layout,
        "unified_tab_navigation": unified_tab_navigation,
        "load_refresh_action": load_refresh_action,
        "column_visibility": column_visibility,
        "horizontal_overscroll_guard": horizontal_overscroll_guard,
        "operator_link": operator_link,
        "metric_filter_applied": metric_filter_applied,
        "empty_state_after_search": empty_state_after_search,
        "reset_restores_table": reset_restores_table,
        "reset_restores_default_order": reset_restores_default_order,
        "historical_selector_present": historical_panel_present,
        "preset_calendar_sync": preset_calendar_sync,
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
    print("web_vitrina_browser_top_panel: ok ->", result["top_panel"])
    print("web_vitrina_browser_status_summary: ok ->", result["status_summary"])
    print("web_vitrina_browser_activity_surface: ok ->", result["activity_surface"])
    print("web_vitrina_browser_compact_widths: ok ->", result["compact_widths"])
    print("web_vitrina_browser_percent_formatting: ok ->", result["percent_formatting"])
    print("web_vitrina_browser_operator_screen_layout: ok ->", result["operator_screen_layout"])
    if "unified_tab_navigation" in result:
        print("web_vitrina_browser_unified_tabs: ok ->", result["unified_tab_navigation"])
    print("web_vitrina_browser_load_refresh_action: ok ->", result["load_refresh_action"])
    print("web_vitrina_browser_right_edge_spacer: ok ->", result["right_edge_spacer"])
    print("web_vitrina_browser_sku_separators: ok ->", result["sku_separators"])
    print("web_vitrina_browser_column_visibility: ok ->", result["column_visibility"])
    print("web_vitrina_browser_horizontal_overscroll_guard: ok ->", result["horizontal_overscroll_guard"])
    print("web_vitrina_browser_operator_link: ok ->", result["operator_link"])
    print("web_vitrina_browser_default_total_first: ok ->", result["default_total_first"])
    print("web_vitrina_browser_default_sku_metric_cluster: ok ->", result["default_sku_metric_cluster"])
    print("web_vitrina_browser_filters: ok ->", result["filter_controls"])
    if "table_toolbar" in result:
        print("web_vitrina_browser_table_toolbar: ok ->", result["table_toolbar"])
    print("web_vitrina_browser_metric_filter: ok ->", result["metric_filter_applied"])
    print("web_vitrina_browser_empty_state: ok ->", result["empty_state_after_search"])
    print("web_vitrina_browser_reset: ok ->", result["reset_restores_table"])
    print("web_vitrina_browser_reset_default_order: ok ->", result["reset_restores_default_order"])
    print("web_vitrina_browser_history_selector: ok ->", result["historical_selector_present"], result["historical_selector_works"], result["preset_calendar_sync"])
    print("web_vitrina_browser_history_reset: ok ->", result["historical_reset_works"])
    if "error_state" in result:
        error_state = result["error_state"]
        print("web_vitrina_browser_error_state: ok ->", error_state["title"])


def _route_page_composition_with_delay(route: object) -> None:
    time.sleep(0.8)
    route.continue_()


def _check_operator_link(page: object, base_url: str) -> dict[str, str]:
    page.goto(base_url + DEFAULT_SHEET_OPERATOR_UI_PATH, wait_until="domcontentloaded")
    page.wait_for_selector("[data-unified-tab-button='vitrina']", timeout=5000)
    tabs = page.locator("[data-unified-tab-button]").evaluate_all(
        "nodes => nodes.map(node => ({id: node.getAttribute('data-unified-tab-button') || '', text: (node.textContent || '').trim(), active: node.classList.contains('is-active')}))"
    )
    tab_texts = [item["text"] for item in tabs]
    if tab_texts != ["Витрина", "Расчет поставок", "Отчеты", "Отзывы", "Исследования"]:
        raise AssertionError(f"operator route must expose the unified top tabs, got {tabs}")
    active_tabs = [item["id"] for item in tabs if item["active"]]
    if active_tabs != ["vitrina"]:
        raise AssertionError(f"operator route must default to the vitrina tab, got {tabs}")
    if page.locator("[data-unified-tab-button]", has_text="Обновление данных").count() != 0:
        raise AssertionError("operator route must not expose a separate Обновление данных tab")
    return {
        "route": DEFAULT_SHEET_OPERATOR_UI_PATH,
        "tabs": ", ".join(tab_texts),
        "default_active": active_tabs[0],
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

def _check_operator_screen_layout(page: object) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const root = document.querySelector('[data-unified-tab-panel="vitrina"]');
          const nodeIndex = selector => {
            const node = root ? root.querySelector(selector) : null;
            if (!node || !root) {
              return -1;
            }
            let current = node;
            while (current && current.parentElement !== root) {
              current = current.parentElement;
            }
            return current ? Array.from(root.children).indexOf(current) : -1;
          };
          const loadButton = document.querySelector('[data-load-refresh-button]');
            const headers = Array.from(document.querySelectorAll('[data-table-head] th')).map(node => (node.textContent || '').trim());
            return {
              unified_tabs: Array.from(document.querySelectorAll('[data-unified-tab-button]')).map(node => (node.textContent || '').trim()),
              active_unified_tab: ((document.querySelector('[data-unified-tab-button].is-active') || {}).textContent || '').trim(),
              update_tab_count: Array.from(document.querySelectorAll('[data-unified-tab-button]')).filter(node => (node.textContent || '').trim() === 'Обновление данных').length,
              retry_button_count: document.querySelectorAll('[data-retry-button]').length,
              top_status_badge_count: document.querySelectorAll('[data-status-badge]').length,
              json_connect_count: document.querySelectorAll('[data-open-contract]').length,
              progress_count: document.querySelectorAll('[data-global-progress]').length,
              load_button_text: loadButton ? (loadButton.textContent || '').trim() : '',
              load_button_class: loadButton ? (loadButton.getAttribute('class') || '') : '',
              headers,
            order: {
              top: nodeIndex('[data-top-panel]'),
              summary: nodeIndex('[data-summary-grid]'),
              toolbar: nodeIndex('[data-table-toolbar]'),
              history: nodeIndex('[data-history-panel]'),
              table: nodeIndex('[data-table-shell]'),
              filters: nodeIndex('[data-filter-controls]'),
              actions: nodeIndex('[data-activity-block]')
            }
          };
        }"""
    )
    if payload["unified_tabs"] != ["Витрина", "Расчет поставок", "Отчеты", "Отзывы", "Исследования"]:
        raise AssertionError(f"web-vitrina must expose the unified top tabs, got {payload}")
    if payload["active_unified_tab"] != "Витрина" or payload["update_tab_count"] != 0:
        raise AssertionError(f"web-vitrina must default to Vitrina and omit update-data tab, got {payload}")
    if payload["retry_button_count"] != 0:
        raise AssertionError(f"removed refresh button must not be rendered, got {payload}")
    if payload["top_status_badge_count"] != 0 or payload["json_connect_count"] != 0:
        raise AssertionError(f"top panel must not render JSON Connect or a permanent status badge, got {payload}")
    if payload["progress_count"] != 1:
        raise AssertionError(f"top panel must expose one global progress component, got {payload}")
    if payload["load_button_text"] != "Загрузить и обновить" or "primary" not in payload["load_button_class"]:
        raise AssertionError(f"load+refresh button must be the single primary action, got {payload}")
    for forbidden in ("Metric Label", "Sections", "Score Label"):
        if forbidden in payload["headers"]:
            raise AssertionError(f"main table headers must be Russian-only, got {payload['headers']}")
    for expected in ("Раздел", "Метрика", "Обновлено"):
        if expected not in payload["headers"]:
            raise AssertionError(f"main table must expose header {expected!r}, got {payload['headers']}")
    order_values = payload["order"]
    expected_order = [order_values[key] for key in ("top", "summary", "toolbar", "table", "actions")]
    if any(value < 0 for value in expected_order) or expected_order != sorted(expected_order):
        raise AssertionError(f"web-vitrina blocks must follow the operator screen order, got {payload}")
    if order_values["history"] != order_values["toolbar"] or order_values["filters"] != order_values["toolbar"]:
        raise AssertionError(f"history and filters must share the compact table toolbar, got {payload}")
    return payload


def _check_unified_tab_navigation(page: object) -> dict[str, object]:
    page.locator('[data-unified-tab-button="factory-order"]').click()
    page.wait_for_function(
        """() => {
          const frame = document.querySelector('[data-operator-embed-frame="factory-order"]');
          const panel = document.querySelector('[data-unified-tab-panel="factory-order"]');
          return !!frame && !!panel && !panel.hidden && (frame.getAttribute('src') || '').includes('embedded_tab=factory-order');
        }""",
        timeout=5000,
    )
    page.locator('[data-unified-tab-button="reports"]').click()
    page.wait_for_function(
        """() => {
          const frame = document.querySelector('[data-operator-embed-frame="reports"]');
          const panel = document.querySelector('[data-unified-tab-panel="reports"]');
          return !!frame && !!panel && !panel.hidden && (frame.getAttribute('src') || '').includes('embedded_tab=reports');
        }""",
        timeout=5000,
    )
    page.locator('[data-unified-tab-button="research"]').click()
    page.wait_for_function(
        """() => {
          const panel = document.querySelector('[data-unified-tab-panel="research"]');
          const active = document.querySelector('[data-unified-tab-button].is-active');
          const title = panel ? (panel.querySelector('h2') || {}).textContent || '' : '';
          const researchButton = panel ? panel.querySelector('[data-research-calculate]') : null;
          return !!panel && !panel.hidden && active &&
            (active.textContent || '').trim() === 'Исследования' &&
            title.trim() === 'Сравнение групп SKU' &&
            !!researchButton;
        }""",
        timeout=5000,
    )
    page.wait_for_function(
        """() => {
          const researchCount = ((document.querySelector('[data-research-sku-summary="research"]') || {}).textContent || '').trim();
          const controlCount = ((document.querySelector('[data-research-sku-summary="control"]') || {}).textContent || '').trim();
          const metricCount = ((document.querySelector('[data-research-metric-summary]') || {}).textContent || '').trim();
          return researchCount.startsWith('Выбрано:') && controlCount.startsWith('Выбрано:') && metricCount.startsWith('Выбрано:');
        }""",
        timeout=10000,
    )
    range_controls = page.evaluate(
        """() => ({
          researchChipCount: document.querySelectorAll('[data-research-promo-filter]').length,
          rangeToggleCount: document.querySelectorAll('[data-research-range-toggle]').length,
          legacyDateInputs: document.querySelectorAll('[data-research-date]').length,
          baselineLabel: (document.querySelector('[data-research-range-label="baseline"]') || {}).textContent || '',
          analysisLabel: (document.querySelector('[data-research-range-label="analysis"]') || {}).textContent || ''
        })"""
    )
    if (
        range_controls["researchChipCount"] != 2
        or range_controls["rangeToggleCount"] != 2
        or range_controls["legacyDateInputs"] != 0
        or " - " in range_controls["baselineLabel"]
    ):
        raise AssertionError(f"research period controls must be compact range pickers with promo chips, got {range_controls}")
    page.locator('[data-research-range-toggle="baseline"]').click()
    page.wait_for_selector('[data-research-range-popover="baseline"]:not([hidden])', timeout=5000)
    page.locator('[data-research-range-day="baseline"][data-date="2026-04-14"]').click()
    page.wait_for_function(
        """() => document.querySelector('[data-research-calculate]').disabled &&
          ((document.querySelector('[data-research-range-label="baseline"]') || {}).textContent || '').includes('Период не выбран')""",
        timeout=5000,
    )
    page.locator('[data-research-range-day="baseline"][data-date="2026-04-15"]').click()
    page.wait_for_function(
        """() => ((document.querySelector('[data-research-range-label="baseline"]') || {}).textContent || '').includes('14.04.2026') &&
          ((document.querySelector('[data-research-range-label="baseline"]') || {}).textContent || '').includes('15.04.2026')""",
        timeout=5000,
    )
    page.locator('[data-research-range-toggle="analysis"]').click()
    page.wait_for_selector('[data-research-range-popover="analysis"]:not([hidden])', timeout=5000)
    page.locator('[data-research-range-day="analysis"][data-date="2026-04-19"]').click()
    page.locator('[data-research-range-day="analysis"][data-date="2026-04-20"]').click()
    research_flow = page.evaluate(
        """() => {
          const researchChip = document.querySelector('[data-research-promo-filter="research"]');
          const controlChip = document.querySelector('[data-research-promo-filter="control"]');
          if (!researchChip || !controlChip) {
            return {ok: false, reason: 'promo chips missing'};
          }
          const fullResearchBoxes = Array.from(document.querySelectorAll('[data-research-sku="research"]:not(:disabled)'));
          const nonPromoResearchBox = fullResearchBoxes.find(node => !(node.closest('label').textContent || '').includes('товар в акции'));
          if (!nonPromoResearchBox) {
            return {ok: false, reason: 'no non-promo SKU checkbox for filter preservation'};
          }
          nonPromoResearchBox.click();
          const selectedResearch = nonPromoResearchBox.value;
          researchChip.click();
          const activeResearchChip = researchChip.classList.contains('is-active');
          const filteredResearchBoxes = Array.from(document.querySelectorAll('[data-research-sku="research"]'));
          const selectedSummary = (document.querySelector('[data-research-sku-options="research"]') || {}).textContent || '';
          const selectedPreserved = selectedSummary.includes(nonPromoResearchBox.closest('label').querySelector('.research-option-main').textContent || selectedResearch);
          researchChip.click();
          const restoredResearchBoxes = Array.from(document.querySelectorAll('[data-research-sku="research"]:not(:disabled)'));
          controlChip.click();
          const filteredControlBoxes = Array.from(document.querySelectorAll('[data-research-sku="control"]:not(:disabled)'));
          const controlBox = filteredControlBoxes.find(node => node.value !== selectedResearch);
          if (!controlBox) {
            return {ok: false, reason: 'no available promo control checkbox'};
          }
          controlBox.click();
          const selectedControl = controlBox.value;
          researchChip.click();
          const disabledInResearchWhileFilterActive = Array.from(document.querySelectorAll('[data-research-sku="research"]'))
            .some(node => node.value === selectedControl && node.disabled);
          researchChip.click();
          controlChip.click();
          const researchBoxes = Array.from(document.querySelectorAll('[data-research-sku="research"]:not(:disabled)'));
          if (researchBoxes.length < 2) {
            return {ok: false, reason: 'not enough research SKU checkboxes'};
          }
          const disabledInControl = Array.from(document.querySelectorAll('[data-research-sku="control"]'))
            .some(node => node.value === selectedResearch && node.disabled);
          const financeMetricPresent = Array.from(document.querySelectorAll('[data-research-metric]'))
            .some(node => (node.value || '').includes('fin_') || node.value === 'total_fin_buyout_rub');
          return {
            ok: activeResearchChip && selectedPreserved && filteredResearchBoxes.length < fullResearchBoxes.length &&
              restoredResearchBoxes.length === fullResearchBoxes.length &&
              disabledInResearchWhileFilterActive && disabledInControl && !financeMetricPresent,
            activeResearchChip,
            filteredResearchCount: filteredResearchBoxes.length,
            fullResearchCount: fullResearchBoxes.length,
            restoredResearchCount: restoredResearchBoxes.length,
            selectedPreserved,
            disabledInResearchWhileFilterActive,
            disabledInControl,
            financeMetricPresent,
            research: selectedResearch,
            control: selectedControl
          };
        }"""
    )
    if not research_flow.get("ok"):
        raise AssertionError(f"research SKU mutual exclusion / metric filter mismatch, got {research_flow}")
    page.locator("[data-research-calculate]").click()
    page.wait_for_selector("[data-research-result-table] tbody tr", timeout=10000)
    result_grid = page.evaluate(
        """() => {
          const shell = document.querySelector('[data-research-result-grid]');
          const scroll = document.querySelector('[data-research-result-scroll]');
          const headers = Array.from(document.querySelectorAll('[data-research-result-table] th')).map(node => (node.textContent || '').trim());
          return {
            shell: !!shell,
            gridLibrary: shell ? shell.getAttribute('data-grid-library') : '',
            scroll: !!scroll,
            maxScrollLeft: scroll ? Math.max(0, scroll.scrollWidth - scroll.clientWidth) : 0,
            headers
          };
        }"""
    )
    expected_research_headers = {
        "Метрика",
        "Агрегация",
        "Исследуемая · база",
        "Исследуемая · анализ",
        "Δ исследуемая",
        "Δ% исследуемая",
        "Контроль · база",
        "Контроль · анализ",
        "Δ контроль",
        "Δ% контроль",
        "Разница изменений",
        "Покрытие",
    }
    if (
        not result_grid["shell"]
        or result_grid["gridLibrary"] != "@gravity-ui/table"
        or not result_grid["scroll"]
        or result_grid["maxScrollLeft"] <= 0
        or expected_research_headers.difference(set(result_grid["headers"]))
    ):
        raise AssertionError(f"research result must use scrollable table/grid pattern, got {result_grid}")
    page.locator('[data-unified-tab-button="vitrina"]').click()
    page.wait_for_function(
        """() => {
          const panel = document.querySelector('[data-unified-tab-panel="vitrina"]');
          const active = document.querySelector('[data-unified-tab-button].is-active');
          return !!panel && !panel.hidden && active && (active.textContent || '').trim() === 'Витрина';
        }""",
        timeout=5000,
    )
    return {
        "factory_order_embed": True,
        "reports_embed": True,
        "research_tab": True,
        "research_calculate_table": page.locator("[data-research-result-table] tbody tr").count(),
        "research_promo_filter": research_flow,
        "research_range_controls": range_controls,
        "research_result_grid": result_grid,
        "restored_default_tab": True,
    }


def _check_load_refresh_action(
    page: object,
    *,
    previous_summary_cards: dict[str, dict[str, str]],
    previous_activity_surface: dict[str, object],
    expect_freshness_change: bool | None,
    expected_final_badge_tone: str | None,
) -> dict[str, object]:
    button = page.locator("[data-load-refresh-button]")
    if button.count() != 1:
        raise AssertionError("load+refresh button must be rendered exactly once")
    with page.expect_response("**/v1/sheet-vitrina-v1/refresh") as load_response_info:
        button.click()
    load_response = load_response_info.value
    if load_response.request.method != "POST":
        raise AssertionError(f"load+refresh button must use POST /refresh, got {load_response.request.method}")
    page.wait_for_function(
        """() => {
          const progress = document.querySelector('[data-global-progress]');
          const bar = document.querySelector('[data-global-progress-bar]');
          return !!progress && !!bar && !progress.hidden && parseFloat(bar.style.width || '0') >= 10;
        }""",
        timeout=5000,
    )
    _wait_for_action_completion(
        page,
        timeout=45000,
        require_enabled_button=True,
    )
    next_summary_cards = _read_summary_cards(page)
    if expected_final_badge_tone is not None:
        expected_label = _badge_label(expected_final_badge_tone)
        status_card = next_summary_cards.get("status") or {}
        if status_card.get("value") != expected_label:
            raise AssertionError(
                f"status summary must end in truthful {expected_final_badge_tone} state, got {status_card}"
            )
    next_activity_surface = _read_activity_surface(page)
    _assert_page_refresh_card_changed(previous_summary_cards, next_summary_cards, action_name="source refresh")
    freshness_changed = not _freshness_card_matches(previous_summary_cards, next_summary_cards)
    if expect_freshness_change is True and not freshness_changed:
        raise AssertionError(
            f"source refresh must advance data freshness in the local fixture, got {previous_summary_cards} -> {next_summary_cards}"
        )
    if expect_freshness_change is False and freshness_changed:
        raise AssertionError("source refresh was expected to keep data freshness unchanged")
    if _activity_block_matches(previous_activity_surface["loading"], next_activity_surface["loading"]):
        raise AssertionError(
            f"source refresh must advance loading table/log state, got {previous_activity_surface} -> {next_activity_surface}"
        )
    progress_hidden = page.locator("[data-global-progress]").evaluate("node => node.hidden")
    return {
        "http_status": load_response.status,
        "method": load_response.request.method,
        "page_refresh_before": previous_summary_cards["page_refresh"]["updated_at"],
        "page_refresh_after": next_summary_cards["page_refresh"]["updated_at"],
        "freshness_before": previous_summary_cards["freshness"]["value"],
        "freshness_after": next_summary_cards["freshness"]["value"],
        "freshness_changed": freshness_changed,
        "progress_hidden_after": progress_hidden,
        "status_summary": next_summary_cards.get("status", {}),
        "activity_surface": next_activity_surface,
    }


def _read_summary_cards(page: object) -> dict[str, dict[str, str]]:
    cards = page.locator("[data-summary-card]").evaluate_all(
        """nodes => Object.fromEntries(
          nodes
            .map(node => {
              const cardId = node.getAttribute('data-summary-card') || '';
              if (!cardId) {
                return null;
              }
              const labelNode = node.querySelector('[data-summary-card-label]');
              const valueNode = node.querySelector('[data-summary-card-value]');
              const detailNode = node.querySelector('[data-summary-card-detail]');
              return [
                cardId,
                {
                  label: (labelNode ? labelNode.textContent : '').trim(),
                  value: (valueNode ? valueNode.textContent : '').trim(),
                  detail: (detailNode ? detailNode.textContent : '').trim(),
                  updated_at: (node.getAttribute('data-summary-card-updated-at') || '').trim()
                }
              ];
            })
            .filter(Boolean)
        )"""
    )
    for required_card_id, required_label in {
        "page_refresh": "Последнее обновление страницы",
        "freshness": "Свежесть данных",
    }.items():
        card = cards.get(required_card_id)
        if card is None:
            raise AssertionError(f"summary cards must expose {required_card_id!r}, got {cards}")
        if card["label"] != required_label:
            raise AssertionError(f"summary card {required_card_id!r} label mismatch, got {cards}")
    if "snapshot " not in cards["freshness"]["detail"] or "as_of_date " not in cards["freshness"]["detail"]:
        raise AssertionError(f"freshness card must expose truthful snapshot markers, got {cards['freshness']}")
    if "T" in cards["freshness"]["value"] or "Z" in cards["freshness"]["value"]:
        raise AssertionError(f"freshness card must render a user-facing timestamp without raw ISO artefacts, got {cards['freshness']}")
    if not cards["page_refresh"]["updated_at"]:
        raise AssertionError(f"page refresh card must expose an exact browser timestamp marker, got {cards['page_refresh']}")
    return cards


def _assert_page_refresh_card_changed(
    previous_summary_cards: dict[str, dict[str, str]],
    next_summary_cards: dict[str, dict[str, str]],
    *,
    action_name: str,
) -> None:
    before = previous_summary_cards["page_refresh"]["updated_at"]
    after = next_summary_cards["page_refresh"]["updated_at"]
    if before == after:
        raise AssertionError(
            f"{action_name} must advance the page refresh marker, got {previous_summary_cards['page_refresh']} -> {next_summary_cards['page_refresh']}"
        )


def _freshness_card_matches(
    previous_summary_cards: dict[str, dict[str, str]],
    next_summary_cards: dict[str, dict[str, str]],
) -> bool:
    before = previous_summary_cards["freshness"]
    after = next_summary_cards["freshness"]
    return before["value"] == after["value"] and before["detail"] == after["detail"]


def _read_activity_surface(page: object, *, allow_empty_log: bool = False) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const readLoadingTable = () => {
            const rows = Array.from(document.querySelectorAll('[data-loading-source]')).map(row => ({
              source_key: row.getAttribute('data-loading-source') || '',
              source_group_id: row.getAttribute('data-loading-source-group') || '',
              source: ((row.querySelector('[data-col-id="source"]') || {}).textContent || '').trim(),
              today_status: ((row.querySelector('[data-col-id="today_status"]') || {}).textContent || '').trim(),
              today_reason: ((row.querySelector('[data-col-id="today_reason"]') || {}).textContent || '').trim(),
              yesterday_status: ((row.querySelector('[data-col-id="yesterday_status"]') || {}).textContent || '').trim(),
              yesterday_reason: ((row.querySelector('[data-col-id="yesterday_reason"]') || {}).textContent || '').trim(),
              metrics: ((row.querySelector('[data-col-id="metrics"]') || {}).textContent || '').trim(),
              technical: ((row.querySelector('[data-col-id="technical_endpoint"]') || {}).textContent || '').trim()
            }));
            const headers = Array.from(document.querySelectorAll('[data-loading-table-head] th')).map(node => ({
              id: node.getAttribute('data-col-id') || '',
              label: (node.textContent || '').trim()
            }));
            const groups = Array.from(document.querySelectorAll('[data-loading-group]')).map(node => ({
              group_id: node.getAttribute('data-loading-group') || '',
              text: (node.textContent || '').trim(),
              has_refresh_button: node.querySelectorAll('[data-refresh-source-group]').length === 1,
              date_value: (node.querySelector('[data-refresh-source-group-date]') || {}).value || '',
              date_min: (node.querySelector('[data-refresh-source-group-date]') || {}).min || '',
              date_max: (node.querySelector('[data-refresh-source-group-date]') || {}).max || '',
              has_session_check: node.querySelectorAll('[data-session-check]').length === 1,
              has_session_recovery_start: node.querySelectorAll('[data-session-recovery-start]').length === 1,
              has_session_launcher: node.querySelectorAll('[data-session-launcher]').length === 1,
              session_state_in_main: !!node.querySelector('.activity-group-main [data-session-state]'),
              session_state_in_controls: !!node.querySelector('.activity-group-actions [data-session-state]')
            }));
            return {
              source_status_button: ((document.querySelector('[data-source-status-load]') || {}).textContent || '').trim(),
              empty_text: ((document.querySelector('[data-loading-table-empty]') || {}).textContent || '').trim(),
              meta: ((document.querySelector('[data-loading-table-meta]') || {}).textContent || '').trim(),
              subtitle: ((document.querySelector('[data-loading-table-subtitle]') || {}).textContent || '').trim(),
              headers: headers,
              groups: groups,
              rows: rows
            };
          };
          return {
            log: {
              status_label: ((document.querySelector('[data-activity-log-status]') || {}).textContent || '').trim(),
              detail: ((document.querySelector('[data-activity-log-detail]') || {}).textContent || '').trim(),
              body: ((document.querySelector('[data-activity-log-body]') || {}).textContent || '').trim(),
              download_href: (document.querySelector('[data-activity-log-download]') || {}).getAttribute('href') || ''
            },
            loading: readLoadingTable(),
            update_block_present: !!document.querySelector('[data-update-summary-list]')
          };
        }"""
    )
    if (
        not allow_empty_log
        and (not payload["log"]["download_href"] or "job?job_id=" not in payload["log"]["download_href"])
    ):
        raise AssertionError(f"log block must keep a truthful job download path, got {payload}")
    loading_ids = [item["source_key"] for item in payload["loading"]["rows"]]
    if payload.get("update_block_present"):
        raise AssertionError(f"removed update summary block must not be present, got {payload}")
    if not loading_ids and allow_empty_log:
        return payload
    if not loading_ids:
        raise AssertionError(f"loading table must expose source rows, got {payload}")
    group_ids = [item["group_id"] for item in payload["loading"]["groups"]]
    if group_ids != ["wb_api", "seller_portal_bot", "other_sources"]:
        raise AssertionError(f"loading table must render grouped source headers, got {payload}")
    if not all(item["has_refresh_button"] for item in payload["loading"]["groups"]):
        raise AssertionError(f"each loading group must expose one group refresh button, got {payload}")
    if not all(item["date_value"] for item in payload["loading"]["groups"]):
        raise AssertionError(f"each loading group must expose a default refresh date, got {payload}")
    seller_group = next(item for item in payload["loading"]["groups"] if item["group_id"] == "seller_portal_bot")
    if not (
        seller_group["has_session_check"]
        and seller_group["has_session_recovery_start"]
        and seller_group["has_session_launcher"]
    ):
        raise AssertionError(f"Seller Portal group must expose session controls, got {payload}")
    if not seller_group["session_state_in_main"] or seller_group["session_state_in_controls"]:
        raise AssertionError(f"Seller Portal session state must be placed in the left group header, got {payload}")
    header_labels = [item["label"] for item in payload["loading"]["headers"]]
    for expected in ("Источник", "Причина сегодня", "Причина вчера", "Метрики", "Технический endpoint"):
        if expected not in header_labels:
            raise AssertionError(f"loading table missing header {expected!r}, got {payload}")
    return payload


def _wait_for_action_completion(
    page: object,
    *,
    timeout: int,
    require_enabled_button: bool = False,
) -> None:
    conditions = ["!!progress", "progress.hidden"]
    if require_enabled_button:
        conditions.append("!!button")
        conditions.append("!button.disabled")
    page.wait_for_function(
        f"""() => {{
          const progress = document.querySelector('[data-global-progress]');
          const button = document.querySelector('[data-load-refresh-button]');
          return {' && '.join(conditions)};
        }}""",
        timeout=timeout,
    )


def _badge_label(tone: str | None) -> str | None:
    mapping = {
        "success": "Успешно",
        "warning": "Внимание",
        "error": "Ошибка",
    }
    if tone is None:
        return None
    return mapping[tone]


def _activity_block_matches(previous_block: dict[str, object], next_block: dict[str, object]) -> bool:
    return (
        previous_block.get("meta") == next_block.get("meta")
        and previous_block.get("subtitle") == next_block.get("subtitle")
        and (
            previous_block.get("items") == next_block.get("items")
            or previous_block.get("rows") == next_block.get("rows")
        )
    )


def _measure_compact_widths(page: object, *, strict: bool) -> dict[str, int]:
    widths = page.locator("[data-table-head] th").evaluate_all(
        """nodes => Object.fromEntries(nodes.map(node => [
          node.getAttribute('data-col-id'),
          Math.round(node.getBoundingClientRect().width)
        ]).filter(item => item[0]))"""
    )
    required = {
        "row_order": 52,
        "scope_label": 145,
        "metric_key": 160,
        "metric_label": 156,
    }
    for column_id, max_width in required.items():
        if int(widths.get(column_id, 0)) <= 0:
            raise AssertionError(f"missing width measurement for {column_id!r}: {widths}")
        if strict and int(widths[column_id]) > max_width:
            raise AssertionError(f"{column_id} must stay compact in browser render, got {widths}")
    for column_id in [key for key in widths if key.startswith("date:")]:
        if strict and int(widths[column_id]) > 94:
            raise AssertionError(f"date column must stay narrow in browser render, got {widths}")
    return {
        "row_order": int(widths["row_order"]),
        "scope_label": int(widths["scope_label"]),
        "metric_key": int(widths["metric_key"]),
        "metric_label": int(widths["metric_label"]),
        "date": int(next(widths[key] for key in widths if key.startswith("date:"))),
    }


def _check_percent_formatting(page: object, *, expected_rows: dict[str, str] | None) -> dict[str, str]:
    percent_rows = page.locator("[data-table-body] tr").evaluate_all(
        """rows => rows
          .map(row => {
            const metricNode = row.querySelector('td[data-col-id="metric_key"]');
            const valueNode = row.querySelector('td[data-col-id^="date:"]');
            if (!metricNode || !valueNode) {
              return null;
            }
            return {
              metric_key: (metricNode.getAttribute('title') || metricNode.textContent || '').trim(),
              value: (valueNode.getAttribute('title') || valueNode.textContent || '').trim()
            };
          })
          .filter(Boolean)
          .filter(item => item.metric_key === 'avg_addToCartConversion')"""
    )
    if len(percent_rows) < 2:
        raise AssertionError(f"percent metric rows must be visible in browser smoke, got {percent_rows}")
    first_value = str(percent_rows[0]["value"])
    second_value = str(percent_rows[1]["value"])
    if expected_rows is not None:
        if first_value != expected_rows["avg_addToCartConversion#1"] or second_value != expected_rows["avg_addToCartConversion#2"]:
            raise AssertionError(f"fractional percent rows must render as scaled percents, got {percent_rows}")
    return {
        "first": first_value,
        "second": second_value,
    }


def _check_right_edge_spacer(page: object) -> dict[str, object]:
    spacer = page.evaluate(
        """() => {
          const headers = Array.from(document.querySelectorAll('[data-table-head] th'));
          const lastHeader = headers[headers.length - 1];
          const previousHeader = headers[headers.length - 2];
          return {
            headerCount: headers.length,
            lastHeaderIsSpacer: !!lastHeader && lastHeader.hasAttribute('data-table-spacer-cell'),
            spacerWidth: lastHeader ? Math.round(lastHeader.getBoundingClientRect().width) : 0,
            previousHeaderId: previousHeader ? (previousHeader.getAttribute('data-col-id') || '') : ''
          };
        }"""
    )
    if not spacer["lastHeaderIsSpacer"] or int(spacer["spacerWidth"]) < 20:
        raise AssertionError(f"table must keep a visible right-edge spacer after the last data column, got {spacer}")
    if not str(spacer["previousHeaderId"]).startswith("date:"):
        raise AssertionError(f"last useful column before the spacer must stay a real date column, got {spacer}")
    return spacer


def _check_sku_separators(page: object) -> dict[str, int]:
    separator_count = page.locator(".sku-separator-row").count()
    if separator_count <= 0:
        raise AssertionError("table must render gray separator rows between adjacent SKU clusters")
    return {
        "separator_count": separator_count,
    }


def _check_preset_calendar_sync(page: object) -> bool:
    state = page.evaluate(
        """() => {
          const start = document.querySelector('[data-history-day="2026-04-14"]');
          const middle = document.querySelector('[data-history-day="2026-04-17"]');
          const end = document.querySelector('[data-history-day="2026-04-20"]');
          return {
            startEdge: !!start && start.classList.contains('range-edge'),
            middleInRange: !!middle && middle.classList.contains('in-range'),
            endEdge: !!end && end.classList.contains('range-edge')
          };
        }"""
    )
    if not state["startEdge"] or not state["middleInRange"] or not state["endEdge"]:
        raise AssertionError(f"history preset must sync the calendar highlight with the date fields, got {state}")
    return True


def _build_plan(
    *,
    as_of_date: str,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    first_in_promo = 1 if as_of_date == "2026-04-20" else 0
    second_in_promo = 0
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
                write_rect="A1:C9",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1000],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 990],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1090],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 0.115],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 0.105],
                    [f"SKU A: Акция", f"SKU:{first_nm_id}|promo_participation", first_in_promo],
                    [f"SKU B: Акция", f"SKU:{second_nm_id}|promo_participation", second_in_promo],
                ],
                row_count=8,
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K4",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot[today_current]",
                        "success",
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        "",
                        "",
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "web_source_snapshot[today_current]",
                        "success",
                        as_of_date,
                        as_of_date,
                        "",
                        as_of_date,
                        as_of_date,
                        2,
                        2,
                        "",
                        "resolution_rule=accepted_prior_current_runtime_cache",
                    ],
                    [
                        "prices_snapshot[today_current]",
                        "error",
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        "",
                        "",
                        2,
                        0,
                        "101,202",
                        "no payload returned",
                    ],
                ],
                row_count=3,
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


def _stub_sheet_load_runner(plan, emit):
    emit(f"load_stub_start snapshot_id={plan.snapshot_id}")
    time.sleep(0.6)
    emit(f"load_stub_finish snapshot_id={plan.snapshot_id}")
    return {
        "status": "success",
        "bridge_kind": "stub",
        "snapshot_id": plan.snapshot_id,
    }


def _stub_sheet_refresh_request(entrypoint, runtime, *, as_of_date=None, log=None):
    emit = log or (lambda _: None)
    plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
    current_state = runtime.load_current_state()
    refreshed_at = entrypoint.refreshed_at_factory()
    emit(f"refresh_stub_start snapshot_id={plan.snapshot_id} refreshed_at={refreshed_at}")
    emit('event=source_step_finish source=seller_funnel_snapshot temporal_slot=today_current endpoint="GET /v1/sales-funnel/daily?date=<YYYY-MM-DD>" kind=success')
    emit('event=source_step_finish source=web_source_snapshot temporal_slot=today_current endpoint="GET /v1/search-analytics/snapshot?date_from=<YYYY-MM-DD>&date_to=<YYYY-MM-DD>" kind=success note="resolution_rule=accepted_prior_current_runtime_cache"')
    emit('event=source_step_finish source=prices_snapshot temporal_slot=today_current endpoint="POST /api/v2/list/goods/filter" kind=error note="no payload returned"')
    refresh_result = runtime.save_sheet_vitrina_ready_snapshot(
        current_state=current_state,
        refreshed_at=refreshed_at,
        plan=plan,
    )
    runtime.save_sheet_vitrina_manual_refresh_result(
        result_payload={
            "technical_status": "success",
            "semantic_status": refresh_result.semantic_status,
            "semantic_label": refresh_result.semantic_label,
            "semantic_tone": refresh_result.semantic_tone,
            "semantic_reason": refresh_result.semantic_reason,
            "snapshot_id": refresh_result.snapshot_id,
            "as_of_date": refresh_result.as_of_date,
            "refreshed_at": refresh_result.refreshed_at,
        },
        refreshed_at=refresh_result.refreshed_at,
    )
    emit(f"refresh_stub_finish snapshot_id={refresh_result.snapshot_id} refreshed_at={refresh_result.refreshed_at}")
    payload = asdict(refresh_result)
    payload["server_context"] = entrypoint.build_sheet_server_context()
    payload["manual_context"] = entrypoint.build_sheet_manual_context()
    return payload


def _start_completed_refresh_job(entrypoint, runtime) -> dict[str, object]:
    job_payload = entrypoint.start_sheet_refresh_job()
    job_id = str(job_payload["job_id"])
    while True:
        snapshot = entrypoint.operator_jobs.get(job_id)
        if snapshot["status"] != "running":
            return snapshot


if __name__ == "__main__":
    main()
