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
        "table_header": ready_result["table_header"],
        "default_total_first": ready_result["default_total_first"],
        "default_sku_metric_cluster": ready_result["default_sku_metric_cluster"],
        "dynamic_object_label": ready_result["dynamic_object_label"],
        "sku_separators": ready_result["sku_separators"],
        "filter_controls": ready_result["filter_controls"],
        "status_summary": ready_result["status_summary"],
        "auto_schedule_block": ready_result["auto_schedule_block"],
        "activity_surface": ready_result["activity_surface"],
        "activity_metrics_preview": ready_result["activity_metrics_preview"],
        "compact_widths": ready_result["compact_widths"],
        "percent_formatting": ready_result["percent_formatting"],
        "operator_screen_layout": ready_result["operator_screen_layout"],
        "unified_tab_navigation": ready_result["unified_tab_navigation"],
        "load_refresh_action": ready_result["load_refresh_action"],
        "table_snapshot_cache": ready_result["table_snapshot_cache"],
        "right_edge_spacer": ready_result["right_edge_spacer"],
        "static_group_labels": ready_result["static_group_labels"],
        "column_visibility": ready_result["column_visibility"],
        "horizontal_overscroll_guard": ready_result["horizontal_overscroll_guard"],
        "operator_link": ready_result["operator_link"],
        "metric_toolbar_removed": ready_result["metric_toolbar_removed"],
        "metric_presentation": ready_result["metric_presentation"],
        "empty_state_after_search": ready_result["empty_state_after_search"],
        "reset_restores_table": ready_result["reset_restores_table"],
        "reset_restores_default_order": ready_result["reset_restores_default_order"],
        "historical_selector_present": ready_result["historical_selector_present"],
        "preset_calendar_sync": ready_result["preset_calendar_sync"],
        "historical_selector_works": ready_result["historical_selector_works"],
        "historical_reset_works": ready_result["historical_reset_works"],
        "stale_history_storage": ready_result["stale_history_storage"],
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


def _trigger_hidden_reset(page: object) -> None:
    page.evaluate(
        """() => {
          const reset = document.querySelector('[data-reset-filters]');
          if (reset) {
            reset.click();
          }
        }"""
    )


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
            top_panel_state = page.evaluate(
                """() => {
                  const header = document.querySelector('[data-table-header]');
                  const progress = document.querySelector('[data-global-progress]');
                  return {
                    top_panel_count: document.querySelectorAll('[data-top-panel]').length,
                    table_header_count: document.querySelectorAll('[data-table-header]').length,
                    status_badge_count: document.querySelectorAll('[data-status-badge]').length,
                    json_connect_count: document.querySelectorAll('[data-open-contract]').length,
                    progress_count: document.querySelectorAll('[data-global-progress]').length,
                    progress_hidden: progress ? !!progress.hidden : null,
                    progress_inside_table_header: !!(header && header.querySelector('[data-global-progress]')),
                    load_button_inside_table_header: !!(header && header.querySelector('[data-load-refresh-button]')),
                    page_meta_inside_table_header: !!(header && header.querySelector('[data-page-meta]'))
                  };
                }"""
            )
            if (
                top_panel_state["top_panel_count"] != 0
                or top_panel_state["table_header_count"] != 1
                or top_panel_state["status_badge_count"] != 0
                or top_panel_state["json_connect_count"] != 0
                or top_panel_state["progress_count"] != 1
                or not top_panel_state["progress_inside_table_header"]
                or not top_panel_state["load_button_inside_table_header"]
                or not top_panel_state["page_meta_inside_table_header"]
            ):
                raise AssertionError(f"source strip controls must live in the table header only, got {top_panel_state}")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            table_header_layout = _check_table_header_layout(page)
            total_rows = page.locator("[data-table-body] tr").count()
            if total_rows <= 0:
                raise AssertionError("web-vitrina table must render at least one row")
            auto_schedule_block = _check_auto_schedule_block(page)
            activity_collapsible = _check_activity_collapsible_block(page)
            initial_summary_cards = _read_summary_cards(page)
            status_summary = initial_summary_cards.get("status", {})
            details_request_deadline = time.time() + 5
            while not source_status_detail_urls and time.time() < details_request_deadline:
                time.sleep(0.05)
            page.wait_for_selector("[data-loading-source]", timeout=20000)
            source_status_request_count_after_open = len(source_status_detail_urls)
            if source_status_request_count_after_open != 1:
                raise AssertionError(
                    f"opening activity block must auto-load source-status exactly once, got {source_status_detail_urls}"
                )
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
            page.locator("[data-activity-summary]").click()
            page.locator("[data-activity-summary]").click()
            page.wait_for_selector("[data-loading-source]", timeout=20000)
            if len(source_status_detail_urls) != source_status_request_count_after_open:
                raise AssertionError(
                    f"reopening loaded activity block must not duplicate source-status request, got {source_status_detail_urls}"
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
            activity_metrics_preview = _check_activity_metrics_preview(page)
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
            if initial_history_state["label"].strip() != "08.04.2026 - 21.04.2026":
                raise AssertionError(f"default compact history label mismatch, got {initial_history_state}")
            if not initial_history_state["popoverHidden"]:
                raise AssertionError(f"history picker popover must be closed by default, got {initial_history_state}")
            if initial_history_state["dateFrom"] != "2026-04-08" or initial_history_state["dateTo"] != "2026-04-21":
                raise AssertionError(f"default history range must be rolling two-week preset ending business today, got {initial_history_state}")
            visible_body_text = page.locator("body").inner_text()
            for forbidden_history_text in (
                "mode:",
                "supported query:",
                "default as_of_date",
                "route state",
                "Открыт period window",
                "Доступно snapshots",
                "В выбранном периоде",
                "grid library",
                "sheet_vitrina_v1",
            ):
                if forbidden_history_text in visible_body_text:
                    raise AssertionError(f"compact picker must not expose technical history text {forbidden_history_text!r}")
            preset_count = page.locator("[data-history-preset]").count()
            if preset_count < 5:
                raise AssertionError(f"historical period presets must be present, got {preset_count}")
            if page.locator("[data-history-mode-badge]").count() != 0:
                raise AssertionError("history panel must not keep the extra Период badge")
            initial_meta = page.locator("[data-page-meta]").inner_text().strip()
            _trigger_hidden_reset(page)
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
            initial_order = _extract_visible_row_order(page)
            if not initial_order:
                raise AssertionError("web-vitrina must expose visible data rows")
            if initial_order[0]["scope_label"].lower() != "итого":
                raise AssertionError(f"default order must start with TOTAL block, got {initial_order[0]}")
            sku_cluster_ok = _has_sku_metric_cluster(initial_order)
            if not sku_cluster_ok:
                raise AssertionError(f"default order must switch to sku->metrics clustering, got {initial_order[:8]}")
            dynamic_object_label = _check_dynamic_object_label(page)
            sku_separators = _check_sku_separators(page)
            right_edge_spacer = _check_right_edge_spacer(page)
            static_group_labels = _check_static_group_labels(page)

            filter_controls = {
                "search_absent": page.locator("[data-filter-control='search']").count() == 0,
                "section": page.locator("[data-filter-control='section']").count() == 1,
                "group": page.locator("[data-filter-control='group']").count() == 1,
                "scope_kind_absent": page.locator("[data-filter-control='scope_kind']").count() == 0,
                "metric_absent": page.locator("[data-metric-manager]").count() == 0
                and page.locator("[data-metric-filter-option]").count() == 0,
                "sort_absent": page.locator("[data-filter-control='sort']").count() == 0,
            }
            if not all(filter_controls.values()):
                raise AssertionError(f"missing filter controls: {filter_controls}")
            table_toolbar = page.evaluate(
                """() => {
                  const toolbar = document.querySelector('[data-table-toolbar]');
                  const header = document.querySelector('[data-table-header]');
                  const tableShell = document.querySelector('[data-table-shell]');
                  const labels = header ? Array.from(header.querySelectorAll('.filter-label')).map((node) => (node.textContent || '').trim()).filter(Boolean) : [];
                  const fieldWidths = header ? Object.fromEntries(Array.from(header.querySelectorAll('.toolbar-field')).map((node) => [
                    (node.querySelector('.filter-label') || {}).textContent ? (node.querySelector('.filter-label').textContent || '').trim() : 'unknown',
                    Math.round(node.getBoundingClientRect().width)
                  ])) : {};
                  const headerText = header ? (header.innerText || '') : '';
                  const rect = header ? header.getBoundingClientRect() : {height: 0};
                  const headerStyle = header ? getComputedStyle(header) : {overflowX: '', overflowY: ''};
                  const beforeTable = !!header && !!tableShell && !!(header.compareDocumentPosition(tableShell) & Node.DOCUMENT_POSITION_FOLLOWING);
                  const logoutLink = document.querySelector('[data-logout-link]');
                  const tablist = document.querySelector('[role="tablist"]');
                  return {
                    oldToolbarCount: document.querySelectorAll('[data-table-toolbar]').length,
                    exists: !!header,
                    beforeTable: beforeTable,
                    labels: labels,
                    fieldWidths: fieldWidths,
                    headerText: headerText,
                    height: Math.round(rect.height),
                    overflowX: headerStyle.overflowX,
                    overflowY: headerStyle.overflowY,
                    oldHeadingCount: Array.from(document.querySelectorAll('h2')).filter((node) => (node.textContent || '').trim() === 'Фильтры и настройки').length,
                    oldPanelTextVisible: (document.body.innerText || '').includes('Search/select/sort и выбор видимых столбцов'),
                    oldResetTextVisible: (document.body.innerText || '').includes('Сбросить фильтры'),
                    forbiddenSortVisible: headerText.includes('Сортировка'),
                    forbiddenScopeVisible: headerText.includes('Scope'),
                    forbiddenSummaryVisible: labels.includes('Итог') || /\\b\\d+\\s+из\\s+\\d+\\s+строк\\b/.test(headerText),
                    logoutText: logoutLink ? (logoutLink.textContent || '').trim() : '',
                    logoutHref: logoutLink ? (logoutLink.getAttribute('href') || '') : '',
                    logoutInTablist: !!(logoutLink && tablist && tablist.contains(logoutLink)),
                    logoutLooksLikeTab: !!(logoutLink && (logoutLink.getAttribute('class') || '').includes('unified-tab-button')),
                    columnManagerVisibleCount: Array.from(document.querySelectorAll('[data-column-manager]')).filter((node) => node.offsetParent !== null).length,
                    columnResetVisibleCount: Array.from(document.querySelectorAll('[data-columns-reset]')).filter((node) => node.offsetParent !== null).length
                  };
                }"""
            )
            expected_toolbar_labels = {"Диапазон", "Секции", "Группа"}
            forbidden_toolbar_labels = {"Поиск", "Столбцы", "Сброс"}
            missing_toolbar_labels = expected_toolbar_labels.difference(set(table_toolbar["labels"]))
            if (
                int(table_toolbar["oldToolbarCount"]) != 0
                or not table_toolbar["exists"]
                or not table_toolbar["beforeTable"]
                or table_toolbar["oldHeadingCount"]
                or table_toolbar["oldPanelTextVisible"]
                or table_toolbar["oldResetTextVisible"]
                or table_toolbar["forbiddenSortVisible"]
                or table_toolbar["forbiddenScopeVisible"]
                or "Тип строк" in table_toolbar["headerText"]
                or "Метрики" in table_toolbar["headerText"]
                or forbidden_toolbar_labels.intersection(set(table_toolbar["labels"]))
                or table_toolbar["forbiddenSummaryVisible"]
                or table_toolbar["logoutText"] != "Выйти"
                or table_toolbar["logoutHref"] != "/logout"
                or table_toolbar["logoutInTablist"]
                or table_toolbar["logoutLooksLikeTab"]
                or table_toolbar["overflowX"] != "visible"
                or table_toolbar["overflowY"] != "visible"
                or table_toolbar["columnManagerVisibleCount"] != 0
                or table_toolbar["columnResetVisibleCount"] != 0
                or missing_toolbar_labels
            ):
                raise AssertionError(
                    f"table controls must live in compact header without the old toolbar, got {table_toolbar}, missing={missing_toolbar_labels}"
                )
            if table_toolbar["height"] > 78:
                raise AssertionError(f"table compact header must stay compact, got {table_toolbar}")
            compact_widths = _measure_compact_widths(page, strict=expected_percent_rows is not None)
            sticky_section_offsets = _check_sticky_section_offsets(page)
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
            table_snapshot_cache = (
                _check_table_snapshot_cache(page)
                if run_actions
                else {"skipped": "read-only public base-url mode"}
            )

            metric_toolbar_removed = _check_metric_toolbar_removed(page)
            metric_presentation = _check_metric_presentation_controls(page)

            empty_state_after_search = page.locator("[data-filter-control='search']").count() == 0

            _trigger_hidden_reset(page)
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
            reset_restores_table = page.locator("[data-table-body] tr").count() > 0
            reset_order = _extract_visible_row_order(page)
            reset_restores_default_order = reset_order == initial_order
            if not reset_restores_default_order:
                raise AssertionError(f"reset must restore canonical default order, got {reset_order[:8]}, expected {initial_order[:8]}")

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
                    "() => document.querySelector('[data-history-date-from]').value === '2026-04-15' && document.querySelector('[data-history-date-to]').value === '2026-04-21'",
                    timeout=5000,
                )
                preset_calendar_sync = _check_preset_calendar_sync(page)
                page.locator("[data-history-save]").click()
                page.wait_for_function(
                    "() => new URL(window.location.href).searchParams.get('date_from') === '2026-04-15' && new URL(window.location.href).searchParams.get('date_to') === '2026-04-21'",
                    timeout=5000,
                )
                page.wait_for_function("() => document.querySelector('[data-history-popover]').hidden", timeout=5000)
                page.wait_for_function(
                    "() => document.querySelector('[data-table-shell]') && !document.querySelector('[data-table-shell]').classList.contains('is-hidden')",
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
                    "() => (document.querySelector('[data-history-label]').textContent || '').trim() === '08.04.2026 - 21.04.2026'",
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
            stale_history_storage = _check_stale_history_storage_ignored(page, base_url) if not as_of_date else {"skipped": "historical as_of_date mode"}
        finally:
            operator_page.close()
            context.close()
            browser.close()

    return {
        "base_url": base_url,
        "as_of_date": as_of_date,
        "table_rendered": total_rows > 0,
        "top_panel": top_panel_state,
        "table_header": table_header_layout,
        "default_total_first": initial_order[0]["scope_label"].lower() == "итого",
        "default_sku_metric_cluster": sku_cluster_ok,
        "dynamic_object_label": dynamic_object_label,
        "sku_separators": sku_separators,
        "right_edge_spacer": right_edge_spacer,
        "static_group_labels": static_group_labels,
        "filter_controls": filter_controls,
        "table_toolbar": table_toolbar,
        "status_summary": status_summary,
        "auto_schedule_block": auto_schedule_block,
        "activity_collapsible": activity_collapsible,
        "activity_metrics_preview": activity_metrics_preview,
        "summary_cards": initial_summary_cards,
        "activity_surface": initial_activity_surface,
        "compact_widths": compact_widths,
        "sticky_section_offsets": sticky_section_offsets,
        "percent_formatting": percent_formatting,
        "operator_screen_layout": operator_screen_layout,
        "unified_tab_navigation": unified_tab_navigation,
        "load_refresh_action": load_refresh_action,
        "table_snapshot_cache": table_snapshot_cache,
        "column_visibility": column_visibility,
        "horizontal_overscroll_guard": horizontal_overscroll_guard,
        "operator_link": operator_link,
        "metric_toolbar_removed": metric_toolbar_removed,
        "metric_presentation": metric_presentation,
        "empty_state_after_search": empty_state_after_search,
        "reset_restores_table": reset_restores_table,
        "reset_restores_default_order": reset_restores_default_order,
        "historical_selector_present": historical_panel_present,
        "preset_calendar_sync": preset_calendar_sync,
        "historical_selector_works": historical_selector_works,
        "historical_reset_works": historical_reset_works,
        "stale_history_storage": stale_history_storage,
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
    if "table_header" in result:
        print("web_vitrina_browser_table_header: ok ->", result["table_header"])
    print("web_vitrina_browser_status_summary: ok ->", result["status_summary"])
    if "auto_schedule_block" in result:
        print("web_vitrina_browser_auto_schedule: ok ->", result["auto_schedule_block"])
    print("web_vitrina_browser_activity_surface: ok ->", result["activity_surface"])
    if "activity_metrics_preview" in result:
        print("web_vitrina_browser_activity_metrics_preview: ok ->", result["activity_metrics_preview"])
    print("web_vitrina_browser_compact_widths: ok ->", result["compact_widths"])
    if "sticky_section_offsets" in result:
        print("web_vitrina_browser_sticky_section: ok ->", result["sticky_section_offsets"])
    print("web_vitrina_browser_percent_formatting: ok ->", result["percent_formatting"])
    print("web_vitrina_browser_operator_screen_layout: ok ->", result["operator_screen_layout"])
    if "unified_tab_navigation" in result:
        print("web_vitrina_browser_unified_tabs: ok ->", result["unified_tab_navigation"])
    print("web_vitrina_browser_load_refresh_action: ok ->", result["load_refresh_action"])
    if "table_snapshot_cache" in result:
        print("web_vitrina_browser_table_snapshot_cache: ok ->", result["table_snapshot_cache"])
    print("web_vitrina_browser_right_edge_spacer: ok ->", result["right_edge_spacer"])
    print("web_vitrina_browser_static_group_labels: ok ->", result["static_group_labels"])
    print("web_vitrina_browser_sku_separators: ok ->", result["sku_separators"])
    print("web_vitrina_browser_column_visibility: ok ->", result["column_visibility"])
    print("web_vitrina_browser_horizontal_overscroll_guard: ok ->", result["horizontal_overscroll_guard"])
    print("web_vitrina_browser_operator_link: ok ->", result["operator_link"])
    print("web_vitrina_browser_default_total_first: ok ->", result["default_total_first"])
    print("web_vitrina_browser_default_sku_metric_cluster: ok ->", result["default_sku_metric_cluster"])
    if "dynamic_object_label" in result:
        print("web_vitrina_browser_dynamic_object_label: ok ->", result["dynamic_object_label"])
    print("web_vitrina_browser_filters: ok ->", result["filter_controls"])
    if "table_toolbar" in result:
        print("web_vitrina_browser_table_toolbar: ok ->", result["table_toolbar"])
    print("web_vitrina_browser_metric_toolbar_removed: ok ->", result["metric_toolbar_removed"])
    print("web_vitrina_browser_metric_presentation: ok ->", result["metric_presentation"])
    print("web_vitrina_browser_empty_state: ok ->", result["empty_state_after_search"])
    print("web_vitrina_browser_reset: ok ->", result["reset_restores_table"])
    print("web_vitrina_browser_reset_default_order: ok ->", result["reset_restores_default_order"])
    print("web_vitrina_browser_history_selector: ok ->", result["historical_selector_present"], result["historical_selector_works"], result["preset_calendar_sync"])
    print("web_vitrina_browser_history_reset: ok ->", result["historical_reset_works"])
    if "stale_history_storage" in result:
        print("web_vitrina_browser_stale_history_storage: ok ->", result["stale_history_storage"])
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
    if tab_texts != ["Витрина", "Поставки", "Отчёты", "Отзывы", "Исследования"]:
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


def _check_stale_history_storage_ignored(page: object, base_url: str) -> dict[str, object]:
    if not base_url.startswith("http://127.0.0.1"):
        return {"skipped": "public base-url mode"}
    page.evaluate(
        """() => {
          localStorage.setItem('wb-core:sheet-vitrina-v1:web-vitrina:legacy-period:v0', JSON.stringify({
            date_from: '2026-04-20',
            date_to: '2026-04-24',
            preset: 'legacy'
          }));
          localStorage.setItem('wb_core_web_vitrina_legacy_history_range', '2026-04-20..2026-04-24');
          localStorage.setItem('wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:period', '{broken-json');
        }"""
    )
    page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    state = page.evaluate(
        """() => ({
          label: (document.querySelector('[data-history-label]') || {}).textContent || '',
          dateFrom: (document.querySelector('[data-history-date-from]') || {}).value || '',
          dateTo: (document.querySelector('[data-history-date-to]') || {}).value || '',
          query: window.location.search
        })"""
    )
    if state["label"].strip() != "08.04.2026 - 21.04.2026":
        raise AssertionError(f"stale browser history storage must not override rolling two-week default, got {state}")
    if state["dateFrom"] != "2026-04-08" or state["dateTo"] != "2026-04-21" or state["query"]:
        raise AssertionError(f"stale/broken history storage must fall back to no-query default, got {state}")
    if "20.04.2026 - 24.04.2026" in state["label"]:
        raise AssertionError(f"legacy April range leaked into history label, got {state}")
    return state


def _check_metric_toolbar_removed(page: object) -> dict[str, object]:
    storage_key = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:selected-metrics:v1"
    state_before = page.evaluate(
        """() => {
          const toolbar = document.querySelector('[data-table-toolbar]');
          return {
            labels: toolbar ? Array.from(toolbar.querySelectorAll('.filter-label')).map((node) => (node.textContent || '').trim()).filter(Boolean) : [],
            managerCount: document.querySelectorAll('[data-metric-manager]').length,
            optionCount: document.querySelectorAll('[data-metric-filter-option]').length,
            summaryCount: document.querySelectorAll('[data-metric-summary-label]').length,
            visibleMetricCount: new Set(Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
              .map((node) => node.getAttribute('data-metric-key') || '')
              .filter(Boolean)).size
          };
        }"""
    )
    if (
        "Метрики" in state_before["labels"]
        or int(state_before["managerCount"]) != 0
        or int(state_before["optionCount"]) != 0
        or int(state_before["summaryCount"]) != 0
        or int(state_before["visibleMetricCount"]) < 4
    ):
        raise AssertionError(f"old toolbar metric selector must be absent and non-authoritative, got {state_before}")

    page.evaluate(
        """(key) => window.localStorage.setItem(key, JSON.stringify({
          version: 1,
          selected_metric_keys: ['obsolete_metric_key']
        }))""",
        storage_key,
    )
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    state_after = page.evaluate(
        """() => ({
          managerCount: document.querySelectorAll('[data-metric-manager]').length,
          optionCount: document.querySelectorAll('[data-metric-filter-option]').length,
          visibleMetricCount: new Set(Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
            .map((node) => node.getAttribute('data-metric-key') || '')
            .filter(Boolean)).size
        })"""
    )
    if int(state_after["managerCount"]) != 0 or int(state_after["optionCount"]) != 0 or int(state_after["visibleMetricCount"]) < 4:
        raise AssertionError(f"obsolete old metric selector storage must be ignored safely, got {state_after}")
    return {
        "toolbar_metric_label_absent": "Метрики" not in state_before["labels"],
        "old_manager_absent": state_after["managerCount"] == 0,
        "obsolete_storage_ignored": True,
    }


def _check_metric_presentation_controls(page: object) -> dict[str, object]:
    storage_key = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:metric-presentation:v1"
    _trigger_hidden_reset(page)
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    panel_state = page.evaluate(
        """() => {
          const panel = document.querySelector('[data-metrics-presentation]');
          const table = document.querySelector('[data-table-shell]');
          const beforeTable = !!panel && !!table && !!(panel.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING);
          const oldToolbarCount = document.querySelectorAll('[data-table-toolbar]').length;
          if (panel) {
            panel.open = true;
          }
          const numbers = (value) => (String(value || '').match(/\\d+(?:\\.\\d+)?/g) || []).map(Number);
          const isNearWhite = (value) => {
            const rgb = numbers(value);
            const alpha = rgb.length >= 4 ? rgb[3] : 1;
            return rgb.length >= 3 && alpha > 0.88 && rgb[0] > 235 && rgb[1] > 235 && rgb[2] > 235;
          };
          const text = panel ? (panel.innerText || '') : '';
          const grid = panel ? panel.querySelector('[data-metrics-config-grid]') : null;
          const gridColumns = grid ? getComputedStyle(grid).gridTemplateColumns.split(' ').filter(Boolean).length : 0;
          const scopeTables = Array.from(panel ? panel.querySelectorAll('[data-metrics-config-scope-table]') : []).map((tableNode) => ({
            scopeId: tableNode.getAttribute('data-metrics-config-scope') || '',
            label: ((tableNode.querySelector('.metrics-config-scope-title') || {}).textContent || '').trim(),
            selectionButtonText: ((tableNode.querySelector('[data-metric-selection-toggle]') || {}).textContent || '').trim(),
            syncButtonText: ((tableNode.querySelector('[data-metric-sync-from-total]') || {}).textContent || '').trim(),
            scrollBoxHeight: Math.round(tableNode.getBoundingClientRect().height),
            scrollHeight: Math.round(tableNode.scrollHeight),
            headTopBefore: Math.round(((tableNode.querySelector('.metrics-config-scope-head') || {}).getBoundingClientRect ? tableNode.querySelector('.metrics-config-scope-head').getBoundingClientRect().top : 0)),
            bulkOptions: Array.from(tableNode.querySelectorAll('[data-metric-bulk-display] option')).map((node) => (node.textContent || '').trim()),
            bulkDisabled: !!((tableNode.querySelector('[data-metric-bulk-display]') || {}).disabled),
            checkboxCount: tableNode.querySelectorAll('[data-metric-selection-checkbox]').length,
            headers: Array.from(tableNode.querySelectorAll('.metrics-config-row.is-header [role="columnheader"]')).map((node) => (node.textContent || '').trim()),
            rows: Array.from(tableNode.querySelectorAll('[data-metric-config-row]:not(.is-header)')).map((row) => ({
              metricKey: row.getAttribute('data-metric-config-key') || '',
              label: ((row.querySelector('.metrics-config-label') || {}).textContent || '').trim(),
              group: ((row.querySelector('.metrics-config-group-badge') || {}).textContent || '').trim(),
              status: row.getAttribute('data-metric-display-status') || '',
              handleCount: row.querySelectorAll('[data-metric-drag-handle]').length,
              selectOptions: Array.from(row.querySelectorAll('[data-metric-display-select] option')).map((node) => (node.textContent || '').trim())
            }))
          }));
          const rows = Array.from(panel ? panel.querySelectorAll('[data-metric-config-row]:not(.is-header)') : []);
          const rowHeights = rows.map((row) => row.getBoundingClientRect().height);
          const whiteNodes = Array.from(panel ? panel.querySelectorAll([
            '.metrics-presentation-body',
            '.metrics-config-scope-table',
            '.metrics-config-row',
            '.metrics-config-drag-handle',
            '.metrics-config-display-select'
          ].join(',')) : []).filter((node) => isNearWhite(getComputedStyle(node).backgroundColor));
          return {
            exists: !!panel,
            oldToolbarCount,
            beforeTable,
            summary: ((document.querySelector('[data-metrics-presentation-summary]') || {}).textContent || '').trim(),
            scopeTables,
            groupMoveButtonCount: panel ? panel.querySelectorAll('[data-metric-group-action]').length : 0,
            metricMoveButtonCount: panel ? panel.querySelectorAll('[data-metric-config-action="up"], [data-metric-config-action="down"]').length : 0,
            anchorControlCount: panel ? panel.querySelectorAll('[data-metric-anchor-group], .metrics-config-anchor, input[type="radio"], input[type="checkbox"][data-metric-anchor]').length : 0,
            metricDragHandleCount: panel ? panel.querySelectorAll('[data-metric-drag-handle]').length : 0,
            groupDragHandleCount: panel ? panel.querySelectorAll('[data-metric-group-drag-handle]').length : 0,
            oldLayoutCount: panel ? panel.querySelectorAll('[data-metrics-config-scope-pair], [data-metrics-config-paired-row], [data-metrics-config-zone], [data-metrics-config-group]').length : 0,
            gridColumns,
            oldLabelHits: ['Показывать сразу', 'Скрыто под раскрытием', 'Скрыть под раскрытием', 'Показать сразу', 'Показать ещё', 'ещё '].filter((item) => text.includes(item)),
            whiteNodeCount: whiteNodes.length,
            maxRowHeight: rowHeights.length ? Math.max(...rowHeights) : 0,
            scopeCount: Number(grid ? (grid.getAttribute('data-metrics-config-scope-count') || '0') : '0')
          };
        }"""
    )
    if (
        not panel_state["exists"]
        or int(panel_state["oldToolbarCount"]) != 0
        or panel_state["beforeTable"]
        or int(panel_state["scopeCount"]) < 2
    ):
        raise AssertionError(f"metrics presentation panel must sit below the table without the old toolbar, got {panel_state}")
    if int(panel_state["groupMoveButtonCount"]) != 0 or int(panel_state["metricMoveButtonCount"]) != 0:
        raise AssertionError(f"metrics presentation must remove arrow move buttons, got {panel_state}")
    if int(panel_state["anchorControlCount"]) != 0:
        raise AssertionError(f"metrics presentation must remove manual anchor controls, got {panel_state}")
    if int(panel_state["groupDragHandleCount"]) != 0:
        raise AssertionError(f"metrics presentation must remove group/block DnD controls, got {panel_state}")
    if int(panel_state["metricDragHandleCount"]) <= 0:
        raise AssertionError(f"metrics presentation must expose metric drag handles, got {panel_state}")
    if int(panel_state["oldLayoutCount"]) != 0:
        raise AssertionError(f"metrics presentation must remove old grouped shown/hidden layout, got {panel_state}")
    scope_labels = [str(scope["label"]) for scope in panel_state["scopeTables"]]
    if scope_labels[:2] != ["Итого", "SKU"]:
        raise AssertionError(f"metrics presentation must render two scope tables Итого/SKU, got {panel_state}")
    if int(panel_state["gridColumns"]) != 2:
        raise AssertionError(f"metrics presentation must use two compact desktop tables, got {panel_state}")
    if "Настроено:" not in str(panel_state["summary"]):
        raise AssertionError(f"metrics presentation header must expose compact configured count, got {panel_state}")
    for scope in panel_state["scopeTables"]:
        if scope["headers"] != ["Метрика", "Группа", "Отображение"]:
            raise AssertionError(f"metric settings table headers mismatch, got {scope}")
        if any(row["handleCount"] != 1 for row in scope["rows"]):
            raise AssertionError(f"each metric row must expose exactly one drag handle, got {scope}")
        if any(row["selectOptions"] != ["Показано", "Свернуто", "Скрыто"] for row in scope["rows"]):
            raise AssertionError(f"display selector options mismatch, got {scope}")
        if scope["selectionButtonText"] != "Выбрать":
            raise AssertionError(f"each scope table must expose selection mode button, got {scope}")
        if scope["scopeId"] == "sku" and scope["syncButtonText"] != "Как Итого":
            raise AssertionError(f"SKU table must expose compact sync-from-total action, got {scope}")
        if scope["scopeId"] != "sku" and scope["syncButtonText"]:
            raise AssertionError(f"sync-from-total action must not render for non-SKU scopes, got {scope}")
        if scope["bulkOptions"] != ["Отображение", "Показано", "Свернуто", "Скрыто"] or not scope["bulkDisabled"]:
            raise AssertionError(f"bulk display selector must start disabled with canonical options, got {scope}")
        if int(scope["checkboxCount"]) != 0:
            raise AssertionError(f"selection checkboxes must be hidden outside selection mode, got {scope}")
    if int(panel_state["whiteNodeCount"]) != 0 or panel_state["oldLabelHits"]:
        raise AssertionError(f"metrics presentation must use compact dark two-table layout with no old wording, got {panel_state}")
    if float(panel_state["maxRowHeight"]) > 34:
        raise AssertionError(f"metrics rows must remain compact, got {panel_state}")
    sticky_controls = _check_metric_settings_sticky_controls(page)
    sku_sync = _check_sku_sync_from_total(page, storage_key=storage_key)
    scroll_preservation = _check_metric_settings_scroll_preserved(page)
    _trigger_hidden_reset(page)
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")

    target_scope = next(
        (
            scope for scope in panel_state["scopeTables"]
            if len(scope["rows"]) >= 4 and len({str(row["group"]) for row in scope["rows"]}) >= 2
        ),
        None,
    )
    if target_scope is None:
        raise AssertionError(f"metrics presentation needs a scope with at least four metrics from multiple groups, got {panel_state}")
    scope_id = str(target_scope["scopeId"])
    initial_order = [str(row["metricKey"]) for row in target_scope["rows"]]
    bulk_selection = _check_metric_bulk_selection(page, scope_id=scope_id, initial_order=initial_order, storage_key=storage_key)
    source_row = target_scope["rows"][0]
    target_row = next((row for row in target_scope["rows"][1:] if row["group"] != source_row["group"]), target_scope["rows"][2])
    source_key = str(source_row["metricKey"])
    target_key = str(target_row["metricKey"])
    _drag_metric_after(page, scope_id=scope_id, metric_key=source_key, target_metric_key=target_key)
    page.wait_for_timeout(250)
    moved_order = _metric_scope_order(page, scope_id)
    if not _appears_after(moved_order, source_key, target_key):
        raise AssertionError(f"metric DnD must reorder across the whole scope list, got {moved_order}, source={source_key}, target={target_key}")
    table_order_after_move = _visible_metric_keys(page)
    if not _appears_after(table_order_after_move, source_key, target_key):
        raise AssertionError(f"main table must follow metric DnD order, got {table_order_after_move[:12]}")
    persisted_order = _persisted_metric_scope_order(page, storage_key, scope_id)
    if not _appears_after(persisted_order, source_key, target_key):
        raise AssertionError(f"metric order must persist in metric-presentation storage, got {persisted_order}")

    order_after_move = _metric_scope_order(page, scope_id)
    if len(order_after_move) < 4:
        raise AssertionError(f"metric status check needs at least four rows, got {order_after_move}")
    anchor_key, collapsed_one, collapsed_two, hidden_key = order_after_move[:4]
    _set_metric_display_status(page, scope_id=scope_id, metric_key=anchor_key, status="shown")
    _set_metric_display_status(page, scope_id=scope_id, metric_key=collapsed_one, status="collapsed")
    _set_metric_display_status(page, scope_id=scope_id, metric_key=collapsed_two, status="collapsed")
    _set_metric_display_status(page, scope_id=scope_id, metric_key=hidden_key, status="hidden")
    page.wait_for_timeout(200)
    visual_state = _metric_display_visual_state(page, scope_id, [anchor_key, collapsed_one, hidden_key])
    if visual_state[anchor_key]["status"] != "shown" or not visual_state[anchor_key]["accentVisible"]:
        raise AssertionError(f"shown metric must keep white text and violet accent, got {visual_state}")
    if visual_state[collapsed_one]["status"] != "collapsed" or visual_state[collapsed_one]["accentVisible"]:
        raise AssertionError(f"collapsed metric must stay active without violet accent, got {visual_state}")
    if visual_state[hidden_key]["status"] != "hidden" or not visual_state[hidden_key]["muted"]:
        raise AssertionError(f"hidden metric must stay in settings as muted inactive row, got {visual_state}")

    collapsed_counts = _visible_metric_key_counts(page)
    if int(collapsed_counts.get(anchor_key, 0)) <= 0:
        raise AssertionError(f"anchor metric must remain visible, got counts={collapsed_counts}")
    if int(collapsed_counts.get(collapsed_one, 0)) != 0 or int(collapsed_counts.get(collapsed_two, 0)) != 0:
        raise AssertionError(f"collapsed metrics must be hidden before disclosure, got counts={collapsed_counts}")
    if int(collapsed_counts.get(hidden_key, 0)) != 0:
        raise AssertionError(f"hidden metric must be absent from main table, got counts={collapsed_counts}")
    disclosure_state = _metric_disclosure_state(page)
    if disclosure_state["buttonCount"] < 1 or anchor_key not in disclosure_state["buttonMetricKeys"]:
        raise AssertionError(f"disclosure arrow must sit at nearest previous shown metric, got {disclosure_state}")
    if any("ещё" in text or "Показать" in text for text in disclosure_state["visibleTexts"]):
        raise AssertionError(f"table disclosure must be icon-only without visible count text, got {disclosure_state}")
    if set(disclosure_state["iconTexts"]) != {"▸"}:
        raise AssertionError(f"collapsed disclosure must render only icon arrow, got {disclosure_state}")
    if not disclosure_state["narrowMetricColumnOk"] or not disclosure_state["buttonsInsideMetricCell"]:
        raise AssertionError(f"disclosure arrow must remain inside narrow metric cell, got {disclosure_state}")

    page.locator('[data-metric-anchor-toggle]').first.click()
    page.wait_for_timeout(150)
    expanded_counts = _visible_metric_key_counts(page)
    if int(expanded_counts.get(collapsed_one, 0)) <= 0 or int(expanded_counts.get(collapsed_two, 0)) <= 0:
        raise AssertionError(f"disclosure arrow must reveal collapsed metric rows, got {expanded_counts}")
    expanded_order = _visible_metric_keys(page)
    expected_reveal = [anchor_key, collapsed_one, collapsed_two]
    anchor_index = expanded_order.index(anchor_key)
    if expanded_order[anchor_index:anchor_index + 3] != expected_reveal:
        raise AssertionError(f"collapsed metrics must reveal after anchor in configured order, got {expanded_order[:12]}")
    expanded_disclosure = _metric_disclosure_state(page)
    if set(expanded_disclosure["expandedValues"]) != {"true"} or set(expanded_disclosure["iconTexts"]) != {"▾"}:
        raise AssertionError(f"expanded disclosure must update icon and aria-expanded, got {expanded_disclosure}")
    hierarchy_state = page.evaluate(
        """(keys) => {
          const childRows = keys.map((metricKey) => {
            const row = Array.from(document.querySelectorAll('[data-table-body] tr.metric-child-row')).find((candidate) => {
              const metricCell = candidate.querySelector('td[data-col-id="metric_label"]');
              return metricCell && (metricCell.getAttribute('data-metric-key') || '') === metricKey;
            });
            const guide = row ? row.querySelector('[data-metric-child-guide="true"] .metric-hierarchy-guide') : null;
            const cell = row ? row.querySelector('td[data-col-id="metric_label"]') : null;
            const label = row ? row.querySelector('.metric-label-text') : null;
            return {
              metricKey,
              rowExists: !!row,
              guideExists: !!guide,
              guideLeft: guide ? Math.round(guide.getBoundingClientRect().left) : 0,
              labelLeft: label ? Math.round(label.getBoundingClientRect().left) : 0,
              cellLeft: cell ? Math.round(cell.getBoundingClientRect().left) : 0
            };
          });
          return {
            childRows,
            guideCount: document.querySelectorAll('[data-metric-child-guide="true"] .metric-hierarchy-guide').length,
            visibleCountText: Array.from(document.querySelectorAll('[data-table-body] td[data-col-id="metric_label"]'))
              .map((node) => (node.textContent || '').trim())
              .filter((text) => text.includes('ещё') || text.includes('Показать ещё'))
          };
        }""",
        [collapsed_one, collapsed_two],
    )
    if (
        any(not row["rowExists"] or not row["guideExists"] for row in hierarchy_state["childRows"])
        or any(row["labelLeft"] <= row["cellLeft"] + 18 for row in hierarchy_state["childRows"])
        or hierarchy_state["visibleCountText"]
    ):
        raise AssertionError(f"expanded child metrics must show hierarchy guides without visible count text, got {hierarchy_state}")

    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    persisted_display = _persisted_metric_display(page, storage_key, scope_id)
    if persisted_display.get(collapsed_one) != "collapsed" or persisted_display.get(hidden_key) != "hidden":
        raise AssertionError(f"display statuses must persist after reload, got {persisted_display}")
    reloaded_disclosure = _metric_disclosure_state(page)
    reloaded_counts = _visible_metric_key_counts(page)
    if set(reloaded_disclosure["expandedValues"]) != {"true"} or int(reloaded_counts.get(collapsed_one, 0)) <= 0:
        raise AssertionError(
            f"expanded collapsed-metric disclosure must persist after reload, got disclosure={reloaded_disclosure}, counts={reloaded_counts}"
        )

    _trigger_hidden_reset(page)
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    reset_order = _metric_scope_order(page, scope_id)
    reset_statuses = _metric_scope_statuses(page, scope_id)
    if reset_order != initial_order or any(status != "shown" for status in reset_statuses.values()):
        raise AssertionError(f"reset must restore default order and shown statuses, got order={reset_order}, statuses={reset_statuses}")
    if page.locator("[data-metric-anchor-toggle]").count() != 0:
        raise AssertionError("reset must clear collapsed disclosure state")

    page.evaluate("(key) => window.localStorage.setItem(key, '{broken-json')", storage_key)
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    if page.locator("[data-metrics-presentation]").count() != 1 or page.locator("[data-table-body] tr").count() <= 0:
        raise AssertionError("broken metric-presentation localStorage must not crash the page")
    page.evaluate(
        """(key) => window.localStorage.setItem(key, JSON.stringify({
          version: 1,
          groups: {obsolete: {order: ['x'], hidden: ['y'], expanded: true}},
          anchor_by_group: {obsolete: 'x'}
        }))""",
        storage_key,
    )
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    if page.locator("[data-metrics-presentation]").count() != 1 or page.locator("[data-table-body] tr").count() <= 0:
        raise AssertionError("obsolete grouped metric-presentation localStorage must not crash the page")
    return {
        "scope_tables": scope_labels,
        "sticky_controls": sticky_controls,
        "sku_sync_from_total": sku_sync,
        "scroll_preservation": scroll_preservation,
        "bulk_selection": bulk_selection,
        "order_changed": [target_key, source_key],
        "display_statuses": {"shown": anchor_key, "collapsed": [collapsed_one, collapsed_two], "hidden": hidden_key},
        "hierarchy_guides": hierarchy_state,
        "disclosure_anchor": anchor_key,
        "broken_storage_fallback": True,
        "obsolete_storage_fallback": True,
    }


def _check_metric_settings_sticky_controls(page: object) -> dict[str, object]:
    state = page.evaluate(
        """() => {
          const table = document.querySelector('[data-metrics-config-scope-table][data-metrics-config-scope="sku"]');
          const head = table ? table.querySelector('.metrics-config-scope-head') : null;
          const selectButton = table ? table.querySelector('[data-metric-selection-toggle]') : null;
          const bulkSelect = table ? table.querySelector('[data-metric-bulk-display]') : null;
          const syncButton = table ? table.querySelector('[data-metric-sync-from-total]') : null;
          if (!table || !head || !selectButton || !bulkSelect || !syncButton) {
            return {ok: false, reason: 'missing controls'};
          }
          table.style.maxHeight = '180px';
          const beforeTop = Math.round(head.getBoundingClientRect().top);
          const canScroll = table.scrollHeight > table.clientHeight + 20;
          table.scrollTop = Math.max(0, table.scrollHeight - table.clientHeight);
          table.dispatchEvent(new Event('scroll'));
          const afterTop = Math.round(head.getBoundingClientRect().top);
          const tableRect = table.getBoundingClientRect();
          const headRect = head.getBoundingClientRect();
          return {
            ok: true,
            canScroll,
            beforeTop,
            afterTop,
            topStable: Math.abs(afterTop - beforeTop) <= 2,
            headInsideTable: headRect.top >= tableRect.top - 2 && headRect.bottom <= tableRect.bottom + 2,
            selectionVisible: !!selectButton.offsetParent,
            bulkVisible: !!bulkSelect.offsetParent,
            syncVisible: !!syncButton.offsetParent,
            scrollTop: Math.round(table.scrollTop)
          };
        }"""
    )
    if not state.get("ok") or not state.get("canScroll") or not state.get("topStable") or not state.get("headInsideTable"):
        raise AssertionError(f"metric settings controls must stay sticky inside the settings scroll container, got {state}")
    if not state.get("selectionVisible") or not state.get("bulkVisible") or not state.get("syncVisible"):
        raise AssertionError(f"metric settings sticky head must keep actions visible, got {state}")
    page.evaluate(
        """() => {
          document.querySelectorAll('[data-metrics-config-scope-table]').forEach((node) => { node.scrollTop = 0; });
        }"""
    )
    return state


def _check_metric_settings_scroll_preserved(page: object) -> list[dict[str, object]]:
    state = page.evaluate(
        """async () => {
          const waitForPaint = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          const getTable = (scopeId) => document.querySelector('[data-metrics-config-scope-table][data-metrics-config-scope="' + scopeId + '"]');
          const getScroll = (scopeId) => {
            const table = getTable(scopeId);
            return table ? Math.round(table.scrollTop) : 0;
          };
          const setScroll = (scopeId) => {
            const table = getTable(scopeId);
            if (!table) {
              return 0;
            }
            table.style.maxHeight = '180px';
            const maxScroll = Math.max(0, table.scrollHeight - table.clientHeight);
            table.scrollTop = Math.min(140, maxScroll);
            return Math.round(table.scrollTop);
          };
          const dispatchDragAfter = (source, target) => {
            const rect = target.getBoundingClientRect();
            const data = new DataTransfer();
            const eventInit = {
              bubbles: true,
              cancelable: true,
              dataTransfer: data,
              clientX: rect.left + Math.max(8, Math.min(rect.width / 2, rect.width - 4)),
              clientY: rect.top + Math.max(4, rect.height * 0.75)
            };
            source.dispatchEvent(new DragEvent('dragstart', eventInit));
            target.dispatchEvent(new DragEvent('dragover', eventInit));
            target.dispatchEvent(new DragEvent('drop', eventInit));
            source.dispatchEvent(new DragEvent('dragend', eventInit));
          };
          const result = [];
          for (const scopeId of ['total', 'sku']) {
            let table = getTable(scopeId);
            if (!table) {
              result.push({scopeId, ok: false, reason: 'missing table'});
              continue;
            }
            const beforeToggle = setScroll(scopeId);
            const toggle = table.querySelector('[data-metric-selection-toggle]');
            if (!toggle) {
              result.push({scopeId, ok: false, reason: 'missing toggle'});
              continue;
            }
            toggle.click();
            await waitForPaint();
            const afterToggle = getScroll(scopeId);
            table = getTable(scopeId);
            const checkbox = table ? table.querySelector('[data-metric-selection-checkbox]') : null;
            if (!checkbox) {
              result.push({scopeId, ok: false, reason: 'missing checkbox', beforeToggle, afterToggle});
              continue;
            }
            checkbox.click();
            await waitForPaint();
            const afterCheckbox = getScroll(scopeId);
            const beforeDrop = setScroll(scopeId);
            table = getTable(scopeId);
            const rows = table ? Array.from(table.querySelectorAll('[data-metric-config-row]:not(.is-header)')) : [];
            const sourceHandle = rows[0] ? rows[0].querySelector('[data-metric-drag-handle]') : null;
            const targetRow = rows.length > 3 ? rows[3] : rows[rows.length - 1];
            if (!sourceHandle || !targetRow || targetRow === rows[0]) {
              result.push({scopeId, ok: false, reason: 'missing drag rows', beforeToggle, afterToggle, afterCheckbox, beforeDrop});
              continue;
            }
            dispatchDragAfter(sourceHandle, targetRow);
            await waitForPaint();
            const afterDrop = getScroll(scopeId);
            table = getTable(scopeId);
            const closeToggle = table ? table.querySelector('[data-metric-selection-toggle]') : null;
            if (closeToggle) {
              closeToggle.click();
              await waitForPaint();
            }
            result.push({
              scopeId,
              ok: true,
              beforeToggle,
              afterToggle,
              afterCheckbox,
              beforeDrop,
              afterDrop,
              togglePreserved: afterToggle >= Math.max(0, beforeToggle - 6),
              checkboxPreserved: afterCheckbox >= Math.max(0, beforeToggle - 6),
              dropPreserved: afterDrop >= Math.max(0, beforeDrop - 6)
            });
          }
          return result;
        }"""
    )
    bad = [
        item
        for item in state
        if not item.get("ok")
        or not item.get("togglePreserved")
        or not item.get("checkboxPreserved")
        or not item.get("dropPreserved")
    ]
    if bad:
        raise AssertionError(f"metric settings scroll must survive select/checkbox/drop per scope, got {state}")
    page.evaluate(
        """() => {
          document.querySelectorAll('[data-metrics-config-scope-table]').forEach((node) => { node.scrollTop = 0; });
        }"""
    )
    return state


def _metric_analog_pairs(page: object) -> list[dict[str, str]]:
    return page.evaluate(
        """() => {
          const order = (scopeId) => Array.from(document.querySelectorAll('[data-metric-config-row][data-metric-config-scope="' + scopeId + '"]:not(.is-header)'))
            .map((row) => row.getAttribute('data-metric-config-key') || '')
            .filter(Boolean);
          const candidates = (key) => {
            const result = [];
            const add = (value) => {
              if (value && !result.includes(value)) {
                result.push(value);
              }
            };
            add(key);
            if (key.startsWith('total_')) {
              add(key.slice('total_'.length));
            }
            if (key.endsWith('_total')) {
              add(key.slice(0, -'_total'.length));
            }
            if (key.startsWith('total_') && key.endsWith('_total')) {
              add(key.slice('total_'.length, -'_total'.length));
            }
            return result;
          };
          const totalOrder = order('total');
          const skuOrder = order('sku');
          const skuSet = new Set(skuOrder);
          const used = new Set();
          const pairs = [];
          totalOrder.forEach((totalKey) => {
            const skuKey = candidates(totalKey).find((candidate) => skuSet.has(candidate) && !used.has(candidate));
            if (skuKey) {
              used.add(skuKey);
              pairs.push({totalKey, skuKey});
            }
          });
          return pairs;
        }"""
    )


def _seed_metric_presentation_storage(
    page: object,
    *,
    storage_key: str,
    total_order: list[str],
    sku_order: list[str],
    total_display: dict[str, str],
) -> None:
    page.evaluate(
        """({storageKey, totalOrder, skuOrder, totalDisplay}) => {
          window.localStorage.setItem(storageKey, JSON.stringify({
            version: 2,
            scopes: {
              total: {order: totalOrder, display: totalDisplay, manual: true},
              sku: {order: skuOrder, display: {}, manual: false}
            },
            expanded_anchors: []
          }));
        }""",
        {
            "storageKey": storage_key,
            "totalOrder": total_order,
            "skuOrder": sku_order,
            "totalDisplay": total_display,
        },
    )


def _check_sku_sync_from_total(page: object, *, storage_key: str) -> dict[str, object]:
    _trigger_hidden_reset(page)
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    pairs = _metric_analog_pairs(page)
    if len(pairs) < 2:
        raise AssertionError(f"SKU sync check needs at least two total/SKU analog pairs, got {pairs}")
    total_initial_order = _metric_scope_order(page, "total")
    sku_initial_order = _metric_scope_order(page, "sku")
    pair_a, pair_b = pairs[0], pairs[1]
    custom_total_order = [pair_b["totalKey"], pair_a["totalKey"]] + [
        key for key in total_initial_order if key not in {pair_a["totalKey"], pair_b["totalKey"]}
    ]
    total_display = {pair_b["totalKey"]: "collapsed", pair_a["totalKey"]: "hidden"}
    _seed_metric_presentation_storage(
        page,
        storage_key=storage_key,
        total_order=custom_total_order,
        sku_order=sku_initial_order,
        total_display=total_display,
    )
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    total_after_auto = _metric_scope_order(page, "total")
    sku_after_auto = _metric_scope_order(page, "sku")
    total_status_after_auto = _metric_scope_statuses(page, "total")
    sku_status_after_auto = _metric_scope_statuses(page, "sku")
    expected_sku_prefix = [pair_b["skuKey"], pair_a["skuKey"]]
    if total_after_auto[:2] != custom_total_order[:2]:
        raise AssertionError(f"auto SKU sync must not rewrite total order, got {total_after_auto[:4]}")
    if total_status_after_auto.get(pair_b["totalKey"]) != "collapsed" or total_status_after_auto.get(pair_a["totalKey"]) != "hidden":
        raise AssertionError(f"auto SKU sync must preserve total display statuses, got {total_status_after_auto}")
    if sku_after_auto[:2] != expected_sku_prefix:
        raise AssertionError(f"auto SKU sync must derive SKU order from total analogs, got {sku_after_auto[:6]}, expected {expected_sku_prefix}")
    if sku_status_after_auto.get(pair_b["skuKey"]) != "collapsed" or sku_status_after_auto.get(pair_a["skuKey"]) != "hidden":
        raise AssertionError(f"auto SKU sync must derive SKU statuses from total analogs, got {sku_status_after_auto}")
    if len(sku_after_auto) != len(set(sku_after_auto)) or set(sku_after_auto) != set(sku_initial_order):
        raise AssertionError(f"auto SKU sync must not duplicate or lose SKU metrics, got {sku_after_auto}")

    before_explicit_total_order = _metric_scope_order(page, "total")
    before_explicit_total_statuses = _metric_scope_statuses(page, "total")
    page.locator('[data-metric-sync-from-total][data-metric-config-scope="sku"]').click()
    page.wait_for_timeout(200)
    explicit_total_order = _metric_scope_order(page, "total")
    explicit_total_statuses = _metric_scope_statuses(page, "total")
    if explicit_total_order != before_explicit_total_order or explicit_total_statuses != before_explicit_total_statuses:
        raise AssertionError("explicit SKU sync must not mutate total scope state")
    persisted_sku_order = _persisted_metric_scope_order(page, storage_key, "sku")
    persisted_sku_display = _persisted_metric_display(page, storage_key, "sku")
    if persisted_sku_order[:2] != expected_sku_prefix or persisted_sku_display.get(pair_b["skuKey"]) != "collapsed":
        raise AssertionError(f"explicit SKU sync must persist SKU-only order/status, got order={persisted_sku_order[:6]}, display={persisted_sku_display}")

    _trigger_hidden_reset(page)
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    if _metric_scope_order(page, "total") != total_initial_order or _metric_scope_order(page, "sku") != sku_initial_order:
        raise AssertionError("reset must restore default total/SKU metric presentation after sync check")
    return {
        "pairs": [pair_a, pair_b],
        "auto_prefix": expected_sku_prefix,
        "explicit_persisted": True,
        "total_untouched": True,
    }


def _check_metric_bulk_selection(page: object, *, scope_id: str, initial_order: list[str], storage_key: str) -> dict[str, object]:
    if len(initial_order) < 4:
        raise AssertionError(f"bulk selection check needs at least four metrics, got {initial_order}")
    other_scope_id = "sku" if scope_id == "total" else "total"
    selected_a, unselected_key, selected_b, unselected_target = initial_order[:4]
    multi_target = unselected_key

    _toggle_metric_selection_mode(page, scope_id)
    selection_open = _metric_selection_state(page, scope_id=scope_id, other_scope_id=other_scope_id)
    if (
        selection_open["buttonText"] != "Готово"
        or selection_open["checkboxCount"] != len(initial_order)
        or selection_open["otherCheckboxCount"] != 0
        or not selection_open["bulkDisabled"]
    ):
        raise AssertionError(f"selection mode must be isolated per scope and bulk-disabled while empty, got {selection_open}")

    _set_metric_selected(page, scope_id=scope_id, metric_key=selected_a, selected=True)
    _set_metric_selected(page, scope_id=scope_id, metric_key=selected_b, selected=True)
    selection_filled = _metric_selection_state(page, scope_id=scope_id, other_scope_id=other_scope_id)
    if (
        selection_filled["selectedKeys"] != [selected_a, selected_b]
        or selection_filled["bulkDisabled"]
        or selection_filled["selectedRowCount"] != 2
        or selection_filled["otherCheckboxCount"] != 0
    ):
        raise AssertionError(f"selected metrics must stay ordered and isolated, got {selection_filled}")

    _drag_metric_after(page, scope_id=scope_id, metric_key=unselected_key, target_metric_key=unselected_target)
    page.wait_for_timeout(200)
    after_unselected_drag = _metric_scope_order(page, scope_id)
    selected_after_unselected_drag = _metric_selection_state(page, scope_id=scope_id, other_scope_id=other_scope_id)["selectedKeys"]
    if not _appears_after(after_unselected_drag, unselected_key, unselected_target) or selected_after_unselected_drag != [selected_a, selected_b]:
        raise AssertionError(
            f"unselected drag must move only that metric and preserve selected rows, "
            f"order={after_unselected_drag}, selected={selected_after_unselected_drag}"
        )

    _set_bulk_metric_display_status(page, scope_id=scope_id, status="hidden")
    hidden_statuses = _metric_scope_statuses(page, scope_id)
    hidden_counts = _visible_metric_key_counts(page)
    if hidden_statuses.get(selected_a) != "hidden" or hidden_statuses.get(selected_b) != "hidden":
        raise AssertionError(f"bulk display selector must hide selected rows, got {hidden_statuses}")
    if int(hidden_counts.get(selected_a, 0)) != 0 or int(hidden_counts.get(selected_b, 0)) != 0:
        raise AssertionError(f"bulk-hidden metrics must leave main table but remain in settings, got counts={hidden_counts}")

    _set_bulk_metric_display_status(page, scope_id=scope_id, status="shown")
    page.wait_for_timeout(150)
    _drag_metric_after(page, scope_id=scope_id, metric_key=selected_a, target_metric_key=multi_target)
    page.wait_for_timeout(250)
    after_multi_drag = _metric_scope_order(page, scope_id)
    if len(after_multi_drag) != len(set(after_multi_drag)) or set(after_multi_drag) != set(initial_order):
        raise AssertionError(f"multi-drag must not duplicate or lose metrics, got {after_multi_drag}")
    target_index = after_multi_drag.index(multi_target)
    if after_multi_drag[target_index + 1:target_index + 3] != [selected_a, selected_b]:
        raise AssertionError(f"multi-drag must move selected metrics as an ordered batch, got {after_multi_drag}")
    table_order_after_multi_drag = _visible_metric_keys(page)
    if not _appears_after(table_order_after_multi_drag, selected_a, multi_target) or not _appears_after(table_order_after_multi_drag, selected_b, selected_a):
        raise AssertionError(f"main table must follow multi-drag order, got {table_order_after_multi_drag[:16]}")

    _set_bulk_metric_display_status(page, scope_id=scope_id, status="collapsed")
    page.wait_for_timeout(150)
    persisted_order = _persisted_metric_scope_order(page, storage_key, scope_id)
    persisted_display = _persisted_metric_display(page, storage_key, scope_id)
    persisted_target_index = persisted_order.index(multi_target)
    if persisted_order[persisted_target_index + 1:persisted_target_index + 3] != [selected_a, selected_b]:
        raise AssertionError(f"multi-drag order must persist, got {persisted_order}")
    if persisted_display.get(selected_a) != "collapsed" or persisted_display.get(selected_b) != "collapsed":
        raise AssertionError(f"bulk display statuses must persist, got {persisted_display}")

    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    after_reload_state = _metric_selection_state(page, scope_id=scope_id, other_scope_id=other_scope_id)
    after_reload_order = _metric_scope_order(page, scope_id)
    after_reload_display = _metric_scope_statuses(page, scope_id)
    if after_reload_state["checkboxCount"] != 0 or after_reload_state["buttonText"] != "Выбрать":
        raise AssertionError(f"selection must be transient and cleared after reload, got {after_reload_state}")
    if after_reload_order != persisted_order:
        raise AssertionError(f"multi-drag order must survive reload, got {after_reload_order}, expected {persisted_order}")
    if after_reload_display.get(selected_a) != "collapsed" or after_reload_display.get(selected_b) != "collapsed":
        raise AssertionError(f"bulk display status must survive reload, got {after_reload_display}")

    _trigger_hidden_reset(page)
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    reset_order = _metric_scope_order(page, scope_id)
    reset_statuses = _metric_scope_statuses(page, scope_id)
    if reset_order != initial_order or any(status != "shown" for status in reset_statuses.values()):
        raise AssertionError(f"reset must clear bulk order/status changes, got order={reset_order}, statuses={reset_statuses}")
    return {
        "scope": scope_id,
        "selected": [selected_a, selected_b],
        "unselected_drag": unselected_key,
        "multi_target": multi_target,
        "batch_order_after_target": after_multi_drag[target_index + 1:target_index + 3],
        "persisted_after_reload": True,
        "selection_transient": True,
    }


def _visible_metric_keys(page: object) -> list[str]:
    return page.evaluate(
        """() => Array.from(new Set(Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
          .map((node) => node.getAttribute('data-metric-key') || '')
          .filter(Boolean)))"""
    )


def _css_attr(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _drag_metric_after(page: object, *, scope_id: str, metric_key: str, target_metric_key: str) -> None:
    scope = _css_attr(scope_id)
    metric = _css_attr(metric_key)
    target_metric = _css_attr(target_metric_key)
    source_selector = (
        f'[data-metric-config-row="{metric}"][data-metric-config-scope="{scope}"] [data-metric-drag-handle]'
    )
    target_selector = (
        f'[data-metric-config-row="{target_metric}"][data-metric-config-scope="{scope}"]'
    )
    source = page.locator(source_selector).first
    target = page.locator(target_selector).first
    source.wait_for(state="visible", timeout=5000)
    target.wait_for(state="visible", timeout=5000)
    source.scroll_into_view_if_needed()
    target.scroll_into_view_if_needed()
    _dispatch_html5_drag_after(page, source_selector=source_selector, target_selector=target_selector)


def _dispatch_html5_drag_after(page: object, *, source_selector: str, target_selector: str) -> None:
    page.evaluate(
        """({sourceSelector, targetSelector}) => {
          const source = document.querySelector(sourceSelector);
          const target = document.querySelector(targetSelector);
          if (!source || !target) {
            throw new Error('missing DnD source or target');
          }
          const rect = target.getBoundingClientRect();
          const data = new DataTransfer();
          const eventInit = {
            bubbles: true,
            cancelable: true,
            dataTransfer: data,
            clientX: rect.left + Math.min(Math.max(8, rect.width / 2), Math.max(8, rect.width - 4)),
            clientY: rect.top + Math.max(4, rect.height * 0.75)
          };
          source.dispatchEvent(new DragEvent('dragstart', eventInit));
          target.dispatchEvent(new DragEvent('dragover', eventInit));
          target.dispatchEvent(new DragEvent('drop', eventInit));
          source.dispatchEvent(new DragEvent('dragend', eventInit));
        }""",
        {"sourceSelector": source_selector, "targetSelector": target_selector},
    )


def _toggle_metric_selection_mode(page: object, scope_id: str) -> None:
    clicked = page.evaluate(
        """(scopeId) => {
          const button = document.querySelector('[data-metrics-config-scope-table][data-metrics-config-scope="' + scopeId + '"] [data-metric-selection-toggle]');
          if (!button) {
            return false;
          }
          button.click();
          return true;
        }""",
        scope_id,
    )
    if not clicked:
        raise AssertionError(f"missing selection toggle for {scope_id!r}")
    page.wait_for_timeout(120)


def _set_metric_selected(page: object, *, scope_id: str, metric_key: str, selected: bool) -> None:
    scope = _css_attr(scope_id)
    metric = _css_attr(metric_key)
    checkbox = page.locator(
        f'[data-metric-selection-checkbox][data-metric-config-scope="{scope}"][data-metric-config-key="{metric}"]'
    ).first
    try:
        checkbox.wait_for(state="attached", timeout=5000)
    except Exception as exc:
        current = _metric_selection_state(page, scope_id=scope_id, other_scope_id=("sku" if scope_id == "total" else "total"))
        current_keys = page.evaluate(
            """(scopeId) => Array.from(document.querySelectorAll('[data-metrics-config-scope-table][data-metrics-config-scope="' + scopeId + '"] [data-metric-selection-checkbox]'))
              .map((node) => node.getAttribute('data-metric-config-key') || '')""",
            scope_id,
        )
        raise AssertionError(
            f"missing selection checkbox for {scope_id!r}/{metric_key!r}; state={current}; keys={current_keys}"
        ) from exc
    checkbox.set_checked(selected)
    page.wait_for_timeout(80)


def _set_bulk_metric_display_status(page: object, *, scope_id: str, status: str) -> None:
    changed = page.evaluate(
        """({scopeId, status}) => {
          const select = document.querySelector('[data-metric-bulk-display][data-metric-config-scope="' + scopeId + '"]');
          if (!select || select.disabled) {
            return false;
          }
          select.value = status;
          select.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        }""",
        {"scopeId": scope_id, "status": status},
    )
    if not changed:
        raise AssertionError(f"missing or disabled bulk display selector for {scope_id!r}")
    page.wait_for_timeout(150)


def _metric_selection_state(page: object, *, scope_id: str, other_scope_id: str) -> dict[str, object]:
    return page.evaluate(
        """({scopeId, otherScopeId}) => {
          const scopeTable = document.querySelector('[data-metrics-config-scope-table][data-metrics-config-scope="' + scopeId + '"]');
          const otherScopeTable = document.querySelector('[data-metrics-config-scope-table][data-metrics-config-scope="' + otherScopeId + '"]');
          const bulk = scopeTable ? scopeTable.querySelector('[data-metric-bulk-display]') : null;
          return {
            buttonText: scopeTable ? ((scopeTable.querySelector('[data-metric-selection-toggle]') || {}).textContent || '').trim() : '',
            checkboxCount: scopeTable ? scopeTable.querySelectorAll('[data-metric-selection-checkbox]').length : 0,
            otherCheckboxCount: otherScopeTable ? otherScopeTable.querySelectorAll('[data-metric-selection-checkbox]').length : 0,
            bulkDisabled: bulk ? !!bulk.disabled : true,
            selectedKeys: scopeTable ? Array.from(scopeTable.querySelectorAll('[data-metric-selection-checkbox]:checked'))
              .map((node) => node.getAttribute('data-metric-config-key') || '')
              .filter(Boolean) : [],
            selectedRowCount: scopeTable ? scopeTable.querySelectorAll('[data-metric-config-row].is-selected').length : 0
          };
        }""",
        {"scopeId": scope_id, "otherScopeId": other_scope_id},
    )


def _metric_disclosure_state(page: object) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const buttons = Array.from(document.querySelectorAll('[data-metric-anchor-toggle]'));
          const state = {
            buttonCount: buttons.length,
            buttonMetricKeys: [],
            visibleTexts: [],
            expandedValues: [],
            ariaLabels: [],
            iconTexts: [],
            buttonsInsideMetricCell: true,
            narrowMetricColumnOk: true
          };
          buttons.forEach((button) => {
            const cell = button.closest('td');
            state.buttonMetricKeys.push(cell ? (cell.getAttribute('data-metric-key') || '') : '');
            state.visibleTexts.push((button.textContent || '').trim());
            state.expandedValues.push(button.getAttribute('aria-expanded') || '');
            state.ariaLabels.push(button.getAttribute('aria-label') || '');
            state.iconTexts.push((button.textContent || '').trim());
            if (!cell || cell.getAttribute('data-col-id') !== 'metric_label') {
              state.buttonsInsideMetricCell = false;
            }
          });
          const first = buttons[0] || null;
          if (first) {
            const metricCells = Array.from(document.querySelectorAll('[data-col-id="metric_label"]'));
            const previous = metricCells.map((cell) => ({
              node: cell,
              width: cell.style.width,
              minWidth: cell.style.minWidth,
              maxWidth: cell.style.maxWidth
            }));
            metricCells.forEach((cell) => {
              cell.style.width = '72px';
              cell.style.minWidth = '72px';
              cell.style.maxWidth = '72px';
            });
            const scroll = document.querySelector('[data-table-scroll]');
            const previousScrollLeft = scroll ? scroll.scrollLeft : 0;
            if (scroll) {
              scroll.scrollLeft = Math.min(260, Math.max(0, scroll.scrollWidth - scroll.clientWidth));
            }
            const buttonRect = first.getBoundingClientRect();
            const cell = first.closest('td');
            const cellRect = cell ? cell.getBoundingClientRect() : {left: 0, right: 0, top: 0, bottom: 0};
            state.narrowMetricColumnOk = !!cell &&
              buttonRect.width >= 12 &&
              buttonRect.height >= 12 &&
              buttonRect.left >= cellRect.left - 1 &&
              buttonRect.right <= cellRect.right + 1 &&
              buttonRect.top >= cellRect.top - 1 &&
              buttonRect.bottom <= cellRect.bottom + 1;
            if (scroll) {
              scroll.scrollLeft = previousScrollLeft;
            }
            previous.forEach((item) => {
              item.node.style.width = item.width;
              item.node.style.minWidth = item.minWidth;
              item.node.style.maxWidth = item.maxWidth;
            });
          }
          return state;
        }"""
    )


def _metric_scope_order(page: object, scope_id: str) -> list[str]:
    return page.evaluate(
        """(scopeId) => Array.from(document.querySelectorAll('[data-metric-config-scope="' + scopeId + '"][data-metric-config-row]:not(.is-header)'))
          .map((row) => row.getAttribute('data-metric-config-key') || '')
          .filter(Boolean)""",
        scope_id,
    )


def _metric_scope_statuses(page: object, scope_id: str) -> dict[str, str]:
    return page.evaluate(
        """(scopeId) => Object.fromEntries(Array.from(document.querySelectorAll('[data-metric-config-scope="' + scopeId + '"][data-metric-config-row]:not(.is-header)'))
          .map((row) => [row.getAttribute('data-metric-config-key') || '', row.getAttribute('data-metric-display-status') || '']))""",
        scope_id,
    )


def _set_metric_display_status(page: object, *, scope_id: str, metric_key: str, status: str) -> None:
    changed = page.evaluate(
        """({scopeId, metricKey, status}) => {
          const select = document.querySelector('[data-metric-display-select][data-metric-config-scope="' + scopeId + '"][data-metric-config-key="' + metricKey + '"]');
          if (!select) {
            return false;
          }
          select.value = status;
          select.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        }""",
        {"scopeId": scope_id, "metricKey": metric_key, "status": status},
    )
    if not changed:
        raise AssertionError(f"missing display selector for {scope_id!r}/{metric_key!r}")


def _metric_display_visual_state(page: object, scope_id: str, metric_keys: list[str]) -> dict[str, dict[str, object]]:
    return page.evaluate(
        """({scopeId, metricKeys}) => {
          const numbers = (value) => (String(value || '').match(/\\d+(?:\\.\\d+)?/g) || []).map(Number);
          const luminance = (value) => {
            const rgb = numbers(value);
            return rgb.length >= 3 ? (rgb[0] + rgb[1] + rgb[2]) : 0;
          };
          const result = {};
          metricKeys.forEach((metricKey) => {
            const row = document.querySelector('[data-metric-config-row="' + metricKey + '"][data-metric-config-scope="' + scopeId + '"]');
            const label = row ? row.querySelector('.metrics-config-label') : null;
            const accent = row ? row.querySelector('.metrics-config-accent') : null;
            const accentStyle = accent ? getComputedStyle(accent) : {backgroundColor: ''};
            const labelStyle = label ? getComputedStyle(label) : {color: ''};
            const accentRgb = numbers(accentStyle.backgroundColor);
            result[metricKey] = {
              status: row ? (row.getAttribute('data-metric-display-status') || '') : '',
              accentVisible: accentRgb.length >= 3 && accentRgb[3] !== 0 && (accentRgb[0] + accentRgb[1] + accentRgb[2]) > 0,
              textLuminance: luminance(labelStyle.color),
              muted: luminance(labelStyle.color) < 560
            };
          });
          return result;
        }""",
        {"scopeId": scope_id, "metricKeys": metric_keys},
    )


def _persisted_metric_scope_order(page: object, storage_key: str, scope_id: str) -> list[str]:
    return page.evaluate(
        """({storageKey, scopeId}) => {
          try {
            const parsed = JSON.parse(window.localStorage.getItem(storageKey) || '{}');
            return ((((parsed || {}).scopes || {})[scopeId] || {}).order || []).filter(Boolean);
          } catch (error) {
            return [];
          }
        }""",
        {"storageKey": storage_key, "scopeId": scope_id},
    )


def _persisted_metric_display(page: object, storage_key: str, scope_id: str) -> dict[str, str]:
    return page.evaluate(
        """({storageKey, scopeId}) => {
          try {
            const parsed = JSON.parse(window.localStorage.getItem(storageKey) || '{}');
            return ((((parsed || {}).scopes || {})[scopeId] || {}).display || {});
          } catch (error) {
            return {};
          }
        }""",
        {"storageKey": storage_key, "scopeId": scope_id},
    )


def _appears_after(order: list[str], source: str, target: str) -> bool:
    try:
        return order.index(source) > order.index(target)
    except ValueError:
        return False


def _visible_metric_key_counts(page: object) -> dict[str, int]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
          .map((node) => node.getAttribute('data-metric-key') || '')
          .filter(Boolean)
          .reduce((acc, key) => {
            acc[key] = (acc[key] || 0) + 1;
            return acc;
          }, {})"""
    )


def _assert_details_open(locator: object, expected: bool, label: str) -> None:
    actual = locator.evaluate("node => !!node.open")
    if actual is not expected:
        raise AssertionError(f"{label}, expected open={expected}, got {actual}")


def _check_column_visibility_controls(page: object) -> dict[str, object]:
    visual_state = page.evaluate(
        """() => {
          return {
            managerCount: document.querySelectorAll('[data-column-manager]').length,
            visibleManagerCount: Array.from(document.querySelectorAll('[data-column-manager]')).filter((node) => node.offsetParent !== null).length,
            visibleResetCount: Array.from(document.querySelectorAll('[data-columns-reset]')).filter((node) => node.offsetParent !== null).length,
            visibleColumnsLabel: Array.from(document.querySelectorAll('.filter-label')).some((node) => (node.textContent || '').trim() === 'Столбцы'),
            missingMetricLabelToggle: document.querySelectorAll('[data-column-visibility-id="metric_label"]').length === 0,
            missingScopeLabelToggle: document.querySelectorAll('[data-column-visibility-id="scope_label"]').length === 0,
            missingMetricKeyToggle: document.querySelectorAll('[data-column-visibility-id="metric_key"]').length === 0,
            missingScopeKindToggle: document.querySelectorAll('[data-column-visibility-id="scope_kind"]').length === 0,
            missingSectionToggle: document.querySelectorAll('[data-column-visibility-id="section"]').length === 0,
            dateToggleCount: document.querySelectorAll('[data-column-visibility-id^="date:"]').length
          };
        }"""
    )
    if (
        int(visual_state["managerCount"]) > 1
        or int(visual_state["visibleManagerCount"]) != 0
        or int(visual_state["visibleResetCount"]) != 0
        or visual_state["visibleColumnsLabel"]
        or not visual_state["missingMetricLabelToggle"]
        or not visual_state["missingScopeLabelToggle"]
        or not visual_state["missingMetricKeyToggle"]
        or not visual_state["missingScopeKindToggle"]
        or not visual_state["missingSectionToggle"]
        or visual_state["dateToggleCount"] != 0
    ):
        raise AssertionError(f"old visible column manager must be removed while forced-hidden columns stay non-restorable, got {visual_state}")
    return {
        "visible_column_manager_removed": True,
        "forced_hidden_columns_non_restorable": True,
        "state": visual_state,
    }


def _check_table_snapshot_cache(page: object) -> dict[str, object]:
    marker = "OLD-CACHE-TABLE-SNAPSHOT"
    page.wait_for_function(
        """() => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          return !!indicator &&
            indicator.classList.contains('is-fresh') &&
            !!window.localStorage.getItem('wb_core_web_vitrina_table_snapshot_v1');
        }""",
        timeout=20000,
    )
    cache_state = page.evaluate(
        """(marker) => {
          const key = 'wb_core_web_vitrina_table_snapshot_v1';
          const raw = window.localStorage.getItem(key);
          if (!raw) {
            return {ok: false, reason: 'missing cache'};
          }
          const cached = JSON.parse(raw);
          const payload = cached.payload || {};
          const table = payload.table_surface || {};
          const rows = Array.isArray(table.rows) ? table.rows : [];
          if (!rows.length) {
            return {ok: false, reason: 'empty cached rows'};
          }
          const row = rows[0];
          const values = row.values || {};
          const cell = values.metric_label || values.scope_label || Object.values(values)[0];
          if (!cell) {
            return {ok: false, reason: 'no editable cached cell'};
          }
          cell.display_text = marker;
          cell.value = marker;
          row.search_text = String(row.search_text || '') + ' ' + marker;
          window.localStorage.setItem(key, JSON.stringify(cached));
          return {
            ok: true,
            request_key: cached.request_key || '',
            snapshot_id: cached.snapshot_id || '',
            row_count: rows.length
          };
        }""",
        arg=marker,
    )
    if not cache_state.get("ok"):
        raise AssertionError(f"table snapshot cache must contain a successful table payload, got {cache_state}")

    page.reload(wait_until="domcontentloaded")
    page.wait_for_function(
        """(marker) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          const bodyText = document.body ? (document.body.innerText || '') : '';
          return !!indicator &&
            indicator.classList.contains('is-stale-loading') &&
            bodyText.includes(marker) &&
            document.querySelectorAll('[data-table-body] tr').length > 0;
        }""",
        arg=marker,
        timeout=3000,
    )
    stale_state = page.evaluate(
        """(marker) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          return {
            indicator_class: indicator ? (indicator.getAttribute('class') || '') : '',
            indicator_text: indicator ? ((indicator.textContent || '').trim()) : '',
            marker_visible: (document.body.innerText || '').includes(marker),
            row_count: document.querySelectorAll('[data-table-body] tr').length
          };
        }""",
        arg=marker,
    )
    page.wait_for_function(
        """(marker) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          const bodyText = document.body ? (document.body.innerText || '') : '';
          return !!indicator &&
            indicator.classList.contains('is-fresh') &&
            !bodyText.includes(marker) &&
            document.querySelectorAll('[data-table-body] tr').length > 0;
        }""",
        arg=marker,
        timeout=20000,
    )
    fresh_state = page.evaluate(
        """(marker) => {
          const indicator = document.querySelector('[data-table-freshness-indicator]');
          return {
            indicator_class: indicator ? (indicator.getAttribute('class') || '') : '',
            indicator_text: indicator ? ((indicator.textContent || '').trim()) : '',
            marker_visible: (document.body.innerText || '').includes(marker),
            row_count: document.querySelectorAll('[data-table-body] tr').length
          };
        }""",
        arg=marker,
    )
    return {
        "cached_row_count": cache_state["row_count"],
        "stale_indicator": stale_state["indicator_text"],
        "stale_marker_visible": stale_state["marker_visible"],
        "fresh_indicator": fresh_state["indicator_text"],
        "fresh_marker_visible": fresh_state["marker_visible"],
    }


def _check_table_header_layout(page: object) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const header = document.querySelector('[data-table-header]');
          const pageMeta = header ? header.querySelector('[data-page-meta]') : null;
          const tableMeta = header ? header.querySelector('[data-table-meta]') : null;
          const summary = header ? header.querySelector('[data-table-summary-line]') : null;
          const loadStatus = header ? header.querySelector('[data-table-load-status]') : null;
          const freshnessBadge = header ? header.querySelector('[data-table-freshness-indicator]') : null;
          const freshnessLabel = freshnessBadge ? freshnessBadge.querySelector('[data-table-freshness-label]') : null;
          const objectLabel = header ? header.querySelector('[data-table-object-label]') : null;
          const progress = header ? header.querySelector('[data-global-progress]') : null;
          const loadButton = header ? header.querySelector('[data-load-refresh-button]') : null;
          const headerControls = header ? header.querySelector('[data-table-header-controls]') : null;
          const leftZone = header ? header.querySelector('.table-heading-left') : null;
          const rightZone = header ? header.querySelector('[data-table-heading-right]') : null;
          const source = header ? header.querySelector('.table-source-kicker') : null;
          const text = header ? (header.innerText || '') : '';
          const metaText = pageMeta ? ((pageMeta.textContent || '').trim()) : '';
          const buttonRect = loadButton ? loadButton.getBoundingClientRect() : {left: 0, right: 0, top: 0, bottom: 0};
          const statusRect = loadStatus ? loadStatus.getBoundingClientRect() : {left: 0, right: 0, top: 0, bottom: 0};
          const headerRect = header ? header.getBoundingClientRect() : {left: 0, right: 0, width: 0};
          const rightRect = rightZone ? rightZone.getBoundingClientRect() : {left: 0, right: 0, width: 0};
          const historyButton = header ? header.querySelector('[data-history-toggle]') : null;
          const historyLabel = header ? header.querySelector('[data-history-label]') : null;
          const historyIcon = header ? header.querySelector('.history-control-icon') : null;
          const sectionSelect = header ? header.querySelector('[data-filter-control="section"]') : null;
          const groupSelect = header ? header.querySelector('[data-filter-control="group"]') : null;
          const measureText = (node, value) => {
            if (!node) {
              return 0;
            }
            const probe = document.createElement('span');
            const styles = getComputedStyle(node);
            probe.style.position = 'fixed';
            probe.style.left = '-9999px';
            probe.style.top = '-9999px';
            probe.style.whiteSpace = 'nowrap';
            probe.style.font = styles.font;
            probe.textContent = value;
            document.body.appendChild(probe);
            const width = probe.getBoundingClientRect().width;
            probe.remove();
            return Math.ceil(width);
          };
          const selectState = (node) => {
            if (!node) {
              return {exists: false};
            }
            const rect = node.getBoundingClientRect();
            const styles = getComputedStyle(node);
            const selectedText = node.options && node.selectedIndex >= 0 ? (node.options[node.selectedIndex].text || '') : '';
            return {
              exists: true,
              text: selectedText,
              width: Math.round(rect.width),
              textWidth: measureText(node, selectedText),
              paddingLeft: Math.round(parseFloat(styles.paddingLeft || '0')),
              paddingRight: Math.round(parseFloat(styles.paddingRight || '0')),
            };
          };
          const historyRect = historyButton ? historyButton.getBoundingClientRect() : {width: 0, right: 0};
          const historyLabelRect = historyLabel ? historyLabel.getBoundingClientRect() : {width: 0, right: 0};
          const historyIconRect = historyIcon ? historyIcon.getBoundingClientRect() : {left: 0, right: 0};
          const forbidden = ['sheet_vitrina_v1', 'Основная web-витрина', 'В выбранном периоде', 'grid library', 'rows:', 'columns:', 'Снимок:', 'Вчера:', 'Сегодня:', 'TZ:', 'Статус последней загрузки', 'Последняя загрузка', 'Обновлено:', 'Свежесть данных', 'today_current', 'yesterday_closed', 'load window'];
          const visibleFilterLabels = Array.from(header ? header.querySelectorAll('.filter-label') : [])
            .filter((node) => {
              const rect = node.getBoundingClientRect();
              const styles = getComputedStyle(node);
              return rect.width > 2 && rect.height > 2 && styles.visibility !== 'hidden' && styles.display !== 'none';
            })
            .map((node) => (node.textContent || '').trim())
            .filter(Boolean);
          return {
            top_panel_count: document.querySelectorAll('[data-top-panel]').length,
            table_header_count: document.querySelectorAll('[data-table-header]').length,
            page_meta_outside_header_count: Array.from(document.querySelectorAll('[data-page-meta]'))
              .filter(node => !node.closest('[data-table-header]')).length,
            table_meta_outside_header_count: Array.from(document.querySelectorAll('[data-table-meta]'))
              .filter(node => !node.closest('[data-table-header]')).length,
            progress_inside_header: !!progress,
            load_button_inside_header: !!loadButton,
            controls_inside_header: !!headerControls,
            left_zone_exists: !!leftZone,
            right_zone_exists: !!rightZone,
            right_zone_anchored: !!rightZone && Math.abs(headerRect.right - rightRect.right) <= 16,
            right_zone_near_button: !!rightZone && !!loadButton && Math.abs(rightRect.right - buttonRect.right) <= 4,
            old_toolbar_count: document.querySelectorAll('[data-table-toolbar]').length,
            search_count: document.querySelectorAll('[data-filter-control="search"]').length,
            columns_visible_count: Array.from(document.querySelectorAll('[data-column-manager]')).filter((node) => node.offsetParent !== null).length,
            reset_visible_count: Array.from(document.querySelectorAll('[data-reset-filters]')).filter((node) => node.offsetParent !== null).length,
            source_text: source ? ((source.textContent || '').trim()) : '',
            page_meta: metaText,
            table_meta: tableMeta ? ((tableMeta.textContent || '').trim()) : '',
            summary_text: summary ? ((summary.textContent || '').trim()) : '',
            object_label_text: objectLabel ? ((objectLabel.textContent || '').trim()) : '',
            object_label_hidden: objectLabel ? !!objectLabel.hidden : null,
            title_table_count: Array.from(document.querySelectorAll('h1,h2,h3')).filter((node) => (node.textContent || '').trim() === 'Таблица').length,
            heading_line_order_ok: !!(objectLabel && freshnessBadge && summary &&
              ((objectLabel.compareDocumentPosition(freshnessBadge) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0) &&
              ((freshnessBadge.compareDocumentPosition(summary) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0)),
            visible_filter_labels: visibleFilterLabels,
            load_status_text: loadStatus ? (loadStatus.getAttribute('data-load-status-text') || '') : '',
            load_status_title: loadStatus ? (loadStatus.getAttribute('title') || '') : '',
            load_status_visible_text: loadStatus ? ((loadStatus.textContent || '').trim()) : '',
            load_status_dot_count: loadStatus ? loadStatus.querySelectorAll('.table-load-status-dot').length : 0,
            load_status_width: loadStatus ? Math.round(statusRect.width) : 0,
            load_status_hidden: loadStatus ? !!loadStatus.hidden : null,
            load_button_text: loadButton ? ((loadButton.textContent || '').trim()) : '',
            load_button_right_aligned: !!loadButton && buttonRect.left > headerRect.left + headerRect.width / 2,
            load_status_inline_left_of_button: !!loadStatus && !loadStatus.hidden && statusRect.right <= buttonRect.left + 2 && Math.abs(((statusRect.top + statusRect.bottom) / 2) - ((buttonRect.top + buttonRect.bottom) / 2)) <= 8,
            freshness_label_text: freshnessLabel ? ((freshnessLabel.textContent || '').trim()) : '',
            freshness_title: freshnessBadge ? (freshnessBadge.getAttribute('title') || '') : '',
            asia_yekaterinburg_in_summary: ((summary ? summary.textContent : '').match(/Asia\\/Yekaterinburg/g) || []).length,
            seconds_in_summary: /\\d{1,2}:\\d{2}:\\d{2}/.test(summary ? (summary.textContent || '') : ''),
            history_control: {
              text: historyLabel ? ((historyLabel.textContent || '').trim()) : '',
              width: Math.round(historyRect.width),
              labelWidth: Math.round(historyLabelRect.width),
              labelScrollWidth: historyLabel ? Math.round(historyLabel.scrollWidth) : 0,
              iconGap: historyIcon ? Math.round(historyIconRect.left - historyLabelRect.right) : 0,
              iconRightInset: historyIcon ? Math.round(historyRect.right - historyIconRect.right) : 0,
            },
            section_control: selectState(sectionSelect),
            group_control: selectState(groupSelect),
            header_text: text,
            has_freshness_badge: !!freshnessBadge && !freshnessBadge.hidden,
            forbidden_hits: forbidden.filter((item) => text.includes(item) || metaText.includes(item) || (tableMeta && tableMeta.textContent || '').includes(item))
          };
        }"""
    )
    if payload["top_panel_count"] != 0 or payload["table_header_count"] != 1:
        raise AssertionError(f"source strip must be removed and table header must be unique, got {payload}")
    if payload["page_meta_outside_header_count"] or payload["table_meta_outside_header_count"]:
        raise AssertionError(f"table header meta must not be duplicated outside the header, got {payload}")
    if (
        not payload["progress_inside_header"]
        or not payload["load_button_inside_header"]
        or not payload["controls_inside_header"]
        or not payload["left_zone_exists"]
        or not payload["right_zone_exists"]
        or not payload["right_zone_anchored"]
        or not payload["right_zone_near_button"]
        or int(payload["old_toolbar_count"]) != 0
        or int(payload["search_count"]) != 0
        or int(payload["columns_visible_count"]) != 0
        or int(payload["reset_visible_count"]) != 0
        or payload["source_text"] != ""
        or payload["load_button_text"] != "Загрузить"
        or not payload["load_button_right_aligned"]
        or not payload["load_status_inline_left_of_button"]
        or payload["load_status_hidden"]
        or not payload["has_freshness_badge"]
        or payload["object_label_hidden"]
        or payload["object_label_text"] != "Итого"
        or payload["title_table_count"] != 0
        or not payload["heading_line_order_ok"]
    ):
        raise AssertionError(f"source/header controls must be compactly inside the table header, got {payload}")
    if payload["page_meta"] or payload["table_meta"] or payload["forbidden_hits"]:
        raise AssertionError(f"table header must not expose old technical/source text, got {payload}")
    if payload["visible_filter_labels"]:
        raise AssertionError(f"compact header controls must not show field labels, got {payload}")
    if "обн:" not in payload["summary_text"] or "свеж:" not in payload["summary_text"]:
        raise AssertionError(f"table header summary must expose short ob/fresh timestamps, got {payload}")
    if int(payload["asia_yekaterinburg_in_summary"]) != 0 or payload["seconds_in_summary"]:
        raise AssertionError(f"compact header timestamps must omit visible timezone and seconds, got {payload}")
    if payload["freshness_label_text"] != "акт" or "актуально" not in str(payload["freshness_title"]):
        raise AssertionError(f"actuality badge must be visibly compact while preserving semantic title, got {payload}")
    if payload["load_status_visible_text"] or int(payload["load_status_dot_count"]) != 1 or int(payload["load_status_width"]) > 32:
        raise AssertionError(f"load status must render as compact icon/lamp without visible text, got {payload}")
    if not str(payload["load_status_title"]).startswith("Загрузка: "):
        raise AssertionError(f"load status lamp must keep textual status in title/aria, got {payload}")
    if str(payload["load_status_text"]) not in {
        "успешно",
        "ошибка",
        "предупреждение",
        "нет данных",
    }:
        raise AssertionError(f"load status must use a short semantic value, got {payload}")
    history_control = payload["history_control"]
    if (
        history_control["text"] != "08.04.2026 - 21.04.2026"
        or int(history_control["labelScrollWidth"]) > int(history_control["labelWidth"]) + 2
        or int(history_control["iconGap"]) < 4
        or int(history_control["iconRightInset"]) < 7
    ):
        raise AssertionError(f"date range control must show the full value with balanced icon spacing, got {payload}")
    for control_name in ("section_control", "group_control"):
        control = payload[control_name]
        available_text_width = int(control["width"]) - int(control["paddingLeft"]) - int(control["paddingRight"])
        if (
            not control.get("exists")
            or not str(control.get("text") or "").startswith("Все ")
            or int(control["textWidth"]) > available_text_width + 2
            or int(control["paddingRight"]) < 22
        ):
            raise AssertionError(f"{control_name} must show its full selected label with chevron padding, got {payload}")
    return payload


def _check_operator_screen_layout(page: object) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const numbers = value => (value.match(/\\d+(?:\\.\\d+)?/g) || []).map(Number);
          const isDark = value => {
            const rgb = numbers(value);
            return rgb.length >= 3 && rgb[0] < 45 && rgb[1] < 50 && rgb[2] < 60;
          };
          const hasAccentBorder = value => {
            const rgb = numbers(value);
            return rgb.length >= 3 && rgb[0] === 139 && rgb[1] === 92 && rgb[2] === 246;
          };
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
          const tableHeader = document.querySelector('[data-table-header]');
          const loadButtonStyles = loadButton ? getComputedStyle(loadButton) : null;
          const saveButtons = Array.from(document.querySelectorAll([
            '[data-vitrina-auto-save]',
            '[data-history-save]',
            '[data-feedbacks-range-save]',
            '[data-feedbacks-prompt-save]',
            '[data-feedbacks-auto-save]',
          ].join(',')));
          const headers = Array.from(document.querySelectorAll('[data-table-head] th')).map(node => (node.textContent || '').trim());
          return {
            unified_tabs: Array.from(document.querySelectorAll('[data-unified-tab-button]')).map(node => (node.textContent || '').trim()),
            active_unified_tab: ((document.querySelector('[data-unified-tab-button].is-active') || {}).textContent || '').trim(),
            update_tab_count: Array.from(document.querySelectorAll('[data-unified-tab-button]')).filter(node => (node.textContent || '').trim() === 'Обновление данных').length,
            retry_button_count: document.querySelectorAll('[data-retry-button]').length,
            top_status_badge_count: document.querySelectorAll('[data-status-badge]').length,
            top_panel_count: document.querySelectorAll('[data-top-panel]').length,
            table_header_count: document.querySelectorAll('[data-table-header]').length,
            json_connect_count: document.querySelectorAll('[data-open-contract]').length,
            progress_count: document.querySelectorAll('[data-global-progress]').length,
            progress_inside_table_header: !!(tableHeader && tableHeader.querySelector('[data-global-progress]')),
            load_button_inside_table_header: !!(tableHeader && tableHeader.querySelector('[data-load-refresh-button]')),
            page_meta_inside_table_header: !!(tableHeader && tableHeader.querySelector('[data-page-meta]')),
            load_button_text: loadButton ? (loadButton.textContent || '').trim() : '',
            load_button_class: loadButton ? (loadButton.getAttribute('class') || '') : '',
            load_button_bg: loadButtonStyles ? loadButtonStyles.backgroundColor : '',
            load_button_border: loadButtonStyles ? loadButtonStyles.borderTopColor : '',
            load_button_is_dark: loadButtonStyles ? isDark(loadButtonStyles.backgroundColor) : false,
            load_button_has_accent_border: loadButtonStyles ? hasAccentBorder(loadButtonStyles.borderTopColor) : false,
            save_button_classes: saveButtons.map(node => node.getAttribute('class') || ''),
            headers,
            order: {
              summary: nodeIndex('[data-summary-grid]'),
              auto: nodeIndex('[data-vitrina-auto-schedule]'),
              metrics: nodeIndex('[data-metrics-presentation]'),
              oldToolbar: nodeIndex('[data-table-toolbar]'),
              history: nodeIndex('[data-history-panel]'),
              table: nodeIndex('[data-table-shell]'),
              filters: nodeIndex('[data-filter-controls]'),
              actions: nodeIndex('[data-activity-block]')
            }
          };
        }"""
    )
    if payload["unified_tabs"] != ["Витрина", "Поставки", "Отчёты", "Отзывы", "Исследования"]:
        raise AssertionError(f"web-vitrina must expose the unified top tabs, got {payload}")
    if payload["active_unified_tab"] != "Витрина" or payload["update_tab_count"] != 0:
        raise AssertionError(f"web-vitrina must default to Vitrina and omit update-data tab, got {payload}")
    if payload["retry_button_count"] != 0:
        raise AssertionError(f"removed refresh button must not be rendered, got {payload}")
    if payload["top_status_badge_count"] != 0 or payload["json_connect_count"] != 0:
        raise AssertionError(f"table header must not render JSON Connect or a permanent status badge, got {payload}")
    if (
        payload["top_panel_count"] != 0
        or payload["table_header_count"] != 1
        or payload["progress_count"] != 1
        or not payload["progress_inside_table_header"]
        or not payload["load_button_inside_table_header"]
        or not payload["page_meta_inside_table_header"]
    ):
        raise AssertionError(f"source/header controls must live in the table header, got {payload}")
    if payload["load_button_text"] != "Загрузить" or "primary" not in payload["load_button_class"]:
        raise AssertionError(f"load button must be the single primary action, got {payload}")
    if not payload["load_button_is_dark"] or not payload["load_button_has_accent_border"]:
        raise AssertionError(f"load+refresh button must use dark accent-outline styling, got {payload}")
    if not payload["save_button_classes"] or any(
        "primary" in class_name or "secondary" not in class_name for class_name in payload["save_button_classes"]
    ):
        raise AssertionError(f"save buttons must remain neutral secondary actions, got {payload}")
    for forbidden in ("Metric Label", "Sections", "Score Label"):
        if forbidden in payload["headers"]:
            raise AssertionError(f"main table headers must be Russian-only, got {payload['headers']}")
    for expected in ("Раздел", "Метрика", "Обновлено"):
        if expected not in payload["headers"]:
            raise AssertionError(f"main table must expose header {expected!r}, got {payload['headers']}")
    order_values = payload["order"]
    expected_order = [order_values[key] for key in ("table", "auto", "metrics", "actions")]
    if any(value < 0 for value in expected_order) or expected_order != sorted(expected_order):
        raise AssertionError(f"web-vitrina blocks must follow the operator screen order, got {payload}")
    if order_values["oldToolbar"] != -1:
        raise AssertionError(f"old separate filter toolbar must be absent, got {payload}")
    if order_values["history"] != order_values["table"] or order_values["filters"] != order_values["table"]:
        raise AssertionError(f"history and filters must share the compact table header, got {payload}")
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
    factory_dark_layout = _check_embedded_operator_dark_layout(page, "factory-order")
    page.locator('[data-unified-tab-button="reports"]').click()
    page.wait_for_function(
        """() => {
          const frame = document.querySelector('[data-operator-embed-frame="reports"]');
          const panel = document.querySelector('[data-unified-tab-panel="reports"]');
          return !!frame && !!panel && !panel.hidden && (frame.getAttribute('src') || '').includes('embedded_tab=reports');
        }""",
        timeout=5000,
    )
    reports_dark_layout = _check_embedded_operator_dark_layout(page, "reports")
    page.locator('[data-unified-tab-button="research"]').click()
    page.wait_for_function(
        """() => {
          const panel = document.querySelector('[data-unified-tab-panel="research"]');
          const active = document.querySelector('[data-unified-tab-button].is-active');
          const researchPanel = panel ? panel.querySelector('[data-research-panel]') : null;
          const tabs = researchPanel ? researchPanel.querySelector('.research-section-tabs') : null;
          const titleNode = researchPanel ? researchPanel.querySelector('[data-research-section-panel="comparison"] h2') : null;
          const title = titleNode ? titleNode.textContent || '' : '';
          const researchButton = panel ? panel.querySelector('[data-research-calculate]') : null;
          return !!panel && !panel.hidden && active &&
            (active.textContent || '').trim() === 'Исследования' &&
            !!tabs && researchPanel.firstElementChild === tabs &&
            title.trim() === 'Сравнение групп SKU' &&
            (tabs.compareDocumentPosition(titleNode) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0 &&
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
          analysisLabel: (document.querySelector('[data-research-range-label="analysis"]') || {}).textContent || '',
          promotionsLabel: (document.querySelector('[data-research-range-label="promotions"]') || {}).textContent || ''
        })"""
    )
    if (
        range_controls["researchChipCount"] != 2
        or range_controls["rangeToggleCount"] != 3
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
    page.locator('[data-research-section-tab="promotions"]').click()
    page.wait_for_function(
        """() => {
          const panel = document.querySelector('[data-research-section-panel="promotions"]');
          const active = document.querySelector('[data-research-section-tab].is-active');
          return !!panel && !panel.hidden && active && (active.textContent || '').trim() === 'Акции';
        }""",
        timeout=5000,
    )
    page.locator('[data-research-range-toggle="promotions"]').click()
    page.wait_for_selector('[data-research-range-popover="promotions"]:not([hidden])', timeout=5000)
    page.locator('[data-research-range-day="promotions"][data-date="2026-04-14"]').click()
    page.locator('[data-research-range-day="promotions"][data-date="2026-04-17"]').click()
    page.locator("[data-research-promotions-load]").click()
    page.wait_for_selector("[data-research-promotions-table] tbody tr", timeout=10000)
    promotions_grid = page.evaluate(
        """() => {
          const shell = document.querySelector('[data-research-promotions-grid]');
          const hypothesis = document.querySelector('[data-research-promotions-hypothesis]');
          const table = document.querySelector('[data-research-promotions-table]');
          const headers = Array.from(document.querySelectorAll('[data-research-promotions-table] th')).map(node => (node.textContent || '').trim());
          const firstRow = Array.from(document.querySelectorAll('[data-research-promotions-table] tbody tr:first-child td')).map(node => (node.textContent || '').trim());
          return {
            shell: !!shell,
            gridLibrary: shell ? shell.getAttribute('data-grid-library') : '',
            hypothesisText: hypothesis ? (hypothesis.textContent || '').trim() : '',
            hypothesisVisible: !!hypothesis && hypothesis.getClientRects().length > 0,
            hypothesisBeforeTable: !!hypothesis && !!table &&
              (hypothesis.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0,
            headers,
            rowCount: document.querySelectorAll('[data-research-promotions-table] tbody tr').length,
            firstRow
          };
        }"""
    )
    expected_promotions_headers = {"SKU", "Средняя цена со скидкой", "Медианная цена со скидкой"}
    if (
        not promotions_grid["shell"]
        or promotions_grid["gridLibrary"] != "@gravity-ui/table"
        or "Гипотеза" not in promotions_grid["hypothesisText"]
        or not promotions_grid["hypothesisVisible"]
        or not promotions_grid["hypothesisBeforeTable"]
        or expected_promotions_headers.difference(set(promotions_grid["headers"]))
        or promotions_grid["rowCount"] < 2
        or "₽" not in " ".join(promotions_grid["firstRow"])
    ):
        raise AssertionError(f"research promotions table mismatch, got {promotions_grid}")
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
        "factory_order_dark_layout": factory_dark_layout,
        "reports_dark_layout": reports_dark_layout,
        "research_tab": True,
        "research_calculate_table": page.locator("[data-research-result-table] tbody tr").count(),
        "research_promo_filter": research_flow,
        "research_range_controls": range_controls,
        "research_result_grid": result_grid,
        "research_promotions_grid": promotions_grid,
        "restored_default_tab": True,
    }


def _check_auto_schedule_block(page: object) -> dict[str, object]:
    page.wait_for_selector("[data-vitrina-auto-schedule]", timeout=10000)
    collapsed_state = page.evaluate(
        """() => {
          const panel = document.querySelector('[data-vitrina-auto-schedule]');
          const summary = document.querySelector('[data-vitrina-auto-summary]');
          const disclosure = document.querySelector('.auto-schedule-disclosure');
          return {
            open: !!(panel && panel.open),
            summaryText: summary ? (summary.textContent || '').trim() : '',
            disclosureVisible: !!(disclosure && disclosure.getBoundingClientRect().width > 0),
            disclosureLeftOfTitle: !!(disclosure && summary && (disclosure.compareDocumentPosition(summary.querySelector('.auto-schedule-title')) & Node.DOCUMENT_POSITION_FOLLOWING)),
            sameLine: !!(summary && summary.getBoundingClientRect().height <= 44)
          };
        }"""
    )
    if collapsed_state["open"]:
        raise AssertionError(f"auto schedule block must be collapsed on page load, got {collapsed_state}")
    if "Автообновления" not in collapsed_state["summaryText"] or not collapsed_state["disclosureVisible"] or not collapsed_state["disclosureLeftOfTitle"] or not collapsed_state["sameLine"]:
        raise AssertionError(f"auto schedule collapsed header must expose title and disclosure arrow, got {collapsed_state}")
    page.locator("[data-vitrina-auto-summary]").click()
    page.wait_for_function(
        "() => !!(document.querySelector('[data-vitrina-auto-schedule]') || {}).open",
        timeout=5000,
    )
    page.wait_for_function(
        "() => document.querySelectorAll('[data-vitrina-auto-schedules-body] tr').length > 0 && !(document.querySelector('[data-vitrina-auto-schedule-meta]').textContent || '').includes('загружается')",
        timeout=10000,
    )
    payload = page.evaluate(
        """() => {
          const rows = Array.from(document.querySelectorAll('[data-vitrina-auto-schedules-body] tr')).map(row => {
            const cells = Array.from(row.querySelectorAll('td')).map(cell => (cell.textContent || '').trim());
            const timeInput = row.querySelector('[data-vitrina-auto-field="local_time_hhmm"]');
            const enabledInput = row.querySelector('[data-vitrina-auto-field="enabled"]');
            const deleteButton = row.querySelector('[data-vitrina-auto-delete]');
            const runNowButton = row.querySelector('[data-vitrina-auto-run-now]');
            return {
              text: cells.join(' '),
              time: timeInput ? timeInput.value : '',
              timeDisabled: timeInput ? !!timeInput.disabled : null,
              enabled: enabledInput ? !!enabledInput.checked : null,
              enabledDisabled: enabledInput ? !!enabledInput.disabled : null,
              deleteDisabled: deleteButton ? !!deleteButton.disabled : null,
              runNowDisabled: runNowButton ? !!runNowButton.disabled : null
            };
          });
          const warningNode = document.querySelector('[data-vitrina-auto-error]');
          const warningSummary = warningNode ? warningNode.querySelector('[data-vitrina-auto-warning-summary]') : null;
          const warningText = warningNode ? warningNode.querySelector('[data-vitrina-auto-warning-text]') : null;
          return {
            title: (document.querySelector('[data-vitrina-auto-schedule] .auto-schedule-title') || {}).textContent || '',
            meta: (document.querySelector('[data-vitrina-auto-schedule-meta]') || {}).textContent || '',
            secondsInMeta: /\\d{1,2}:\\d{2}:\\d{2}/.test((document.querySelector('[data-vitrina-auto-schedule-meta]') || {}).textContent || ''),
            status: (document.querySelector('[data-vitrina-auto-status]') || {}).textContent || '',
            error: (document.querySelector('[data-vitrina-auto-error]') || {}).textContent || '',
            warning: warningNode ? {
              hidden: !!warningNode.hidden,
              open: !!warningNode.open,
              className: warningNode.getAttribute('class') || '',
              backgroundColor: window.getComputedStyle(warningNode).backgroundColor,
              summaryColor: warningSummary ? window.getComputedStyle(warningSummary).color : '',
              summary: warningSummary ? (warningSummary.textContent || '').trim() : '',
              text: warningText ? (warningText.textContent || '').trim() : ''
            } : null,
            addCount: document.querySelectorAll('[data-vitrina-auto-add]').length,
            saveCount: document.querySelectorAll('[data-vitrina-auto-save]').length,
            reloadCount: document.querySelectorAll('[data-vitrina-auto-reload]').length,
            rows
          };
        }"""
    )
    times = [row["time"] for row in payload["rows"] if row.get("time")]
    if payload["title"].strip() != "Автообновления":
        raise AssertionError(f"auto schedule block title mismatch: {payload}")
    if (
        "Часовой пояс: Asia/Yekaterinburg" not in payload["meta"]
        or "Следующий запуск:" not in payload["meta"]
        or "runtime_managed_json_schedule" in payload["meta"]
        or payload["meta"].count("Asia/Yekaterinburg") != 1
        or payload["secondsInMeta"]
    ):
        raise AssertionError(f"auto schedule header must expose operator-readable summary without runtime noise, got {payload}")
    if sorted(times) != ["11:00", "20:00"]:
        raise AssertionError(f"auto schedule block must read current runtime schedule, got {payload}")
    if payload["addCount"] != 1 or payload["saveCount"] != 1 or payload["reloadCount"] != 1:
        raise AssertionError(f"auto schedule block must expose add/save/reload controls, got {payload}")
    if not all(row["enabled"] for row in payload["rows"] if row.get("time")):
        raise AssertionError(f"current auto schedule rows must be enabled, got {payload}")
    if not all(not row["timeDisabled"] and not row["enabledDisabled"] and not row["deleteDisabled"] for row in payload["rows"] if row.get("time")):
        raise AssertionError(f"runtime-managed current rows must be editable in runtime UI, got {payload}")
    if not all(row["runNowDisabled"] is False for row in payload["rows"] if row.get("time")):
        raise AssertionError(f"runtime-managed current rows must expose run-now, got {payload}")
    warning = payload.get("warning") or {}
    if not warning.get("hidden"):
        raise AssertionError(f"runtime schedule warning must stay hidden when schedule is editable, got {payload}")

    page.locator("[data-vitrina-auto-add]").click()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('[data-vitrina-auto-field=\"local_time_hhmm\"]')).some(node => node.value === '12:00' && !node.disabled)",
        timeout=5000,
    )
    page.locator("[data-vitrina-auto-save]").click()
    page.wait_for_function(
        """() => {
          const saveButton = document.querySelector('[data-vitrina-auto-save]');
          const times = Array.from(document.querySelectorAll('[data-vitrina-auto-field="local_time_hhmm"]')).map(node => node.value).filter(Boolean);
          return !!saveButton && !saveButton.disabled && (saveButton.textContent || '').trim() === 'Сохранить' && times.length === 3 && times.includes('12:00');
        }""",
        timeout=10000,
    )
    page.locator("[data-vitrina-auto-field=\"local_time_hhmm\"]").last.fill("12:30")
    page.locator("[data-vitrina-auto-save]").click()
    page.wait_for_function(
        """() => {
          const saveButton = document.querySelector('[data-vitrina-auto-save]');
          return !!saveButton && !saveButton.disabled && (saveButton.textContent || '').trim() === 'Сохранить' &&
            Array.from(document.querySelectorAll('[data-vitrina-auto-field="local_time_hhmm"]')).some(node => node.value === '12:30');
        }""",
        timeout=10000,
    )
    page.locator("[data-vitrina-auto-reload]").click()
    page.wait_for_function(
        "() => Array.from(document.querySelectorAll('[data-vitrina-auto-field=\"local_time_hhmm\"]')).some(node => node.value === '12:30')",
        timeout=5000,
    )
    return {
        "times": sorted(times),
        "timezone": "Asia/Yekaterinburg",
        "collapsed_by_default": True,
        "controls": {"add": True, "save": True, "reload": True},
        "runtime_editable": True,
    }


def _check_embedded_operator_dark_layout(page: object, embedded_tab: str) -> dict[str, object]:
    frame = page.frame_locator(f'[data-operator-embed-frame="{embedded_tab}"]')
    frame.locator("body").wait_for(timeout=10000)
    if embedded_tab == "factory-order":
        frame.locator('[data-supply-section-button="factory"]').wait_for(timeout=10000)
    elif embedded_tab == "reports":
        frame.locator('[data-report-section-button="daily"]').wait_for(timeout=10000)
    else:
        raise AssertionError(f"unsupported embedded operator tab {embedded_tab!r}")
    payload = frame.locator("body").evaluate(
        """(body, embeddedTab) => {
          const numbers = value => (value.match(/\\d+(?:\\.\\d+)?/g) || []).map(Number);
          const isDark = value => {
            const rgb = numbers(value);
            return rgb.length >= 3 && rgb[0] < 45 && rgb[1] < 50 && rgb[2] < 60;
          };
          const root = document.documentElement;
          const rootStyles = getComputedStyle(root);
          const activePanel = document.querySelector(`[data-tab-panel="${embeddedTab}"]`);
          const block = activePanel ? activePanel.querySelector(".block") : null;
          const control = activePanel ? activePanel.querySelector("input:not([type='checkbox']), select, .stock-selector-summary") : null;
          const subsectionStrip = activePanel ? activePanel.querySelector(".subsection-strip") : null;
          const activeSubsection = activePanel ? activePanel.querySelector(".subsection-button.is-active") : null;
          const primary = activePanel ? activePanel.querySelector(".primary-button") : null;
          const secondary = activePanel ? activePanel.querySelector(".secondary-button") : null;
          const card = activePanel ? activePanel.querySelector(".dataset-card, .report-card, .plan-report-card, .stock-report-item") : null;
          const tableSurface = activePanel ? activePanel.querySelector(".district-table-wrap, .plan-report-table-wrap, .log-viewport") : null;
          const primaryRgb = primary ? numbers(getComputedStyle(primary).backgroundColor) : [];
          const primaryBorderRgb = primary ? numbers(getComputedStyle(primary).borderTopColor) : [];
          const activeSubsectionRgb = activeSubsection ? numbers(getComputedStyle(activeSubsection).backgroundColor) : [];
          const buttonHeights = Array.from(activePanel ? activePanel.querySelectorAll(".action-button") : [])
            .map(node => Math.round(node.getBoundingClientRect().height))
            .filter(Boolean);
          return {
            bodyClass: body.className,
            rootAccent: rootStyles.getPropertyValue("--accent").trim(),
            rootAccentHover: rootStyles.getPropertyValue("--accent-hover").trim(),
            rootAccentActive: rootStyles.getPropertyValue("--accent-active").trim(),
            rootAccentSoft: rootStyles.getPropertyValue("--accent-soft").trim(),
            rootAccentFocus: rootStyles.getPropertyValue("--accent-focus").trim(),
            topTabsHiddenInEmbed: getComputedStyle(document.querySelector(".tab-strip")).display === "none",
            blockBg: block ? getComputedStyle(block).backgroundColor : "",
            cardBg: card ? getComputedStyle(card).backgroundColor : "",
            controlBg: control ? getComputedStyle(control).backgroundColor : "",
            subsectionDisplay: subsectionStrip ? getComputedStyle(subsectionStrip).display : "",
            subsectionBg: subsectionStrip ? getComputedStyle(subsectionStrip).backgroundColor : "",
            activeSubsectionBg: activeSubsection ? getComputedStyle(activeSubsection).backgroundColor : "",
            primaryBg: primary ? getComputedStyle(primary).backgroundColor : "",
            primaryBorder: primary ? getComputedStyle(primary).borderTopColor : "",
            secondaryBg: secondary ? getComputedStyle(secondary).backgroundColor : "",
            tableSurfaceBg: tableSurface ? getComputedStyle(tableSurface).backgroundColor : "",
            blockIsDark: block ? isDark(getComputedStyle(block).backgroundColor) : false,
            cardIsDark: card ? isDark(getComputedStyle(card).backgroundColor) : false,
            controlIsDark: control ? isDark(getComputedStyle(control).backgroundColor) : false,
            primaryIsDark: primaryRgb.length >= 3 && primaryRgb[0] < 45 && primaryRgb[1] < 50 && primaryRgb[2] < 60,
            primaryHasAccentBorder: primaryBorderRgb.length >= 3 && primaryBorderRgb[0] === 139 && primaryBorderRgb[1] === 92 && primaryBorderRgb[2] === 246,
            primaryLooksGreen: primaryRgb.length >= 3 && primaryRgb[1] > primaryRgb[0] && primaryRgb[1] > primaryRgb[2],
            activeSubsectionUsesAccent: activeSubsectionRgb.length >= 3 && activeSubsectionRgb[0] === 139 && activeSubsectionRgb[1] === 92 && activeSubsectionRgb[2] === 246,
            equalActionButtonHeights: buttonHeights.length === 0 || Math.max(...buttonHeights) - Math.min(...buttonHeights) <= 2,
            buttonHeights,
            staleVitrina2: body.textContent.includes("Витрина 2"),
          };
        }""",
        embedded_tab,
    )
    for key in ("blockIsDark", "controlIsDark", "equalActionButtonHeights"):
        if not payload.get(key):
            raise AssertionError(f"{embedded_tab} embedded operator dark invariant failed for {key}: {payload}")
    if embedded_tab == "factory-order" and (
        not payload.get("primaryIsDark") or not payload.get("primaryHasAccentBorder")
    ):
        raise AssertionError(f"factory-order primary action must use dark accent-outline styling, got {payload}")
    if embedded_tab == "reports" and not payload.get("cardIsDark"):
        raise AssertionError(f"reports cards must render dark, got {payload}")
    if embedded_tab == "reports" and not payload.get("activeSubsectionUsesAccent"):
        raise AssertionError(f"reports segmented active state must use violet accent, got {payload}")
    if payload.get("primaryLooksGreen") or payload.get("staleVitrina2"):
        raise AssertionError(f"{embedded_tab} must not use green primary accent or stale label, got {payload}")
    if payload.get("rootAccent") != "#8B5CF6" or payload.get("rootAccentHover") != "#A78BFA" or payload.get("rootAccentActive") != "#7C3AED":
        raise AssertionError(f"{embedded_tab} must use the violet operator accent tokens, got {payload}")
    if payload.get("subsectionDisplay") != "flex":
        raise AssertionError(f"{embedded_tab} subsection selector must remain a horizontal segmented control, got {payload}")
    return payload


def _check_activity_collapsible_block(page: object) -> dict[str, object]:
    page.wait_for_selector("[data-activity-block]", timeout=10000)
    collapsed = page.evaluate(
        """() => {
          const panel = document.querySelector('[data-activity-block]');
          const summary = document.querySelector('[data-activity-summary]');
          const disclosure = panel ? panel.querySelector('.activity-disclosure') : null;
          const body = panel ? panel.querySelector('.activity-panel-body') : null;
          const bodyRect = body ? body.getBoundingClientRect() : {width: 0, height: 0};
          const summaryText = summary ? (summary.textContent || '').trim() : '';
          return {
            open: !!(panel && panel.open),
            titleOnly: summaryText === 'Действия и состояния',
            disclosureVisible: !!(disclosure && disclosure.getBoundingClientRect().width > 0),
            bodyVisible: !!(body && bodyRect.width > 1 && bodyRect.height > 1),
            technicalInHeader: ['ground', 'manual', 'action', 'update summary', 'runtime', 'Grouped manual actions']
              .some((item) => summaryText.includes(item))
          };
        }"""
    )
    if (
        collapsed["open"]
        or not collapsed["titleOnly"]
        or not collapsed["disclosureVisible"]
        or collapsed["bodyVisible"]
        or collapsed["technicalInHeader"]
    ):
        raise AssertionError(f"activity block must be collapsed by default with title-only shared header, got {collapsed}")
    page.locator("[data-activity-summary]").click()
    page.wait_for_function(
        "() => !!(document.querySelector('[data-activity-block]') || {}).open",
        timeout=5000,
    )
    opened = page.evaluate(
        """() => {
          const panel = document.querySelector('[data-activity-block]');
          const body = panel ? panel.querySelector('.activity-panel-body') : null;
          const bodyRect = body ? body.getBoundingClientRect() : {width: 0, height: 0};
          return {
            open: !!(panel && panel.open),
            bodyVisible: !!(body && bodyRect.width > 1 && bodyRect.height > 1),
            sourceStatusButton: ((document.querySelector('[data-source-status-load]') || {}).textContent || '').trim()
          };
        }"""
    )
    if not opened["open"] or not opened["bodyVisible"] or opened["sourceStatusButton"] not in {"Загрузить", "Загрузка…"}:
        raise AssertionError(f"activity block body must open without losing existing content, got {opened}")
    return {"collapsed": collapsed, "opened": opened}


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
    previous_freshness_marker = _read_summary_period_marker(page)
    with page.expect_response("**/v1/sheet-vitrina-v1/refresh") as load_response_info:
        button.click()
    load_response = load_response_info.value
    if load_response.request.method != "POST":
        raise AssertionError(f"load+refresh button must use POST /refresh, got {load_response.request.method}")
    page.wait_for_function(
        """() => {
          const progress = document.querySelector('[data-global-progress]');
          const pulse = progress ? progress.querySelector('.top-progress-pulse') : null;
          const rect = progress ? progress.getBoundingClientRect() : {width: 0};
          const pulseStyle = pulse ? getComputedStyle(pulse) : null;
          const trackVisible = Array.from(document.querySelectorAll('.top-progress-track, [data-global-progress-bar]'))
            .some((node) => {
              const style = getComputedStyle(node);
              return style.display !== 'none' && style.visibility !== 'hidden' && node.getBoundingClientRect().width > 0;
            });
	          return !!progress && !!pulse && !progress.hidden && progress.getAttribute('data-progress-state') === 'loading' &&
	            progress.classList.contains('is-loading') && !trackVisible && rect.width <= 32 && !!progress.getAttribute('aria-label') &&
	            pulseStyle.animationName === 'topProgressPulse' && pulseStyle.animationDuration !== '0s';
        }""",
        timeout=5000,
    )
    _wait_for_action_completion(
        page,
        timeout=45000,
        require_enabled_button=True,
    )
    final_lamp = page.evaluate(
        """() => {
          const progress = document.querySelector('[data-global-progress]');
          const lamp = document.querySelector('[data-table-load-status]');
          const dot = lamp ? lamp.querySelector('.table-load-status-dot') : null;
          const lampStyle = lamp ? getComputedStyle(lamp) : null;
          const dotStyle = dot ? getComputedStyle(dot) : null;
          return {
            progressHidden: !!progress && progress.hidden,
            progressLoading: !!progress && progress.classList.contains('is-loading'),
            lampVisible: !!lamp && !lamp.hidden && lamp.getBoundingClientRect().width <= 18,
            lampBorder: lampStyle ? lampStyle.borderTopWidth : '',
            lampBackground: lampStyle ? lampStyle.backgroundColor : '',
            dotBoxShadow: dotStyle ? dotStyle.boxShadow : '',
            dotAnimation: dotStyle ? dotStyle.animationName : ''
          };
        }"""
    )
    if not final_lamp.get("progressHidden") or final_lamp.get("progressLoading"):
        raise AssertionError(f"loading progress must stop after refresh, got {final_lamp}")
    if not final_lamp.get("lampVisible"):
        raise AssertionError(f"final load status must be a compact semantic dot, got {final_lamp}")
    if final_lamp.get("lampBorder") not in {"0px", "0"}:
        raise AssertionError(f"final load status dot must not have an outline, got {final_lamp}")
    if final_lamp.get("dotBoxShadow") != "none" or final_lamp.get("dotAnimation") != "none":
        raise AssertionError(f"final load status dot must be static without extra graphics, got {final_lamp}")
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
    next_freshness_marker = _read_summary_period_marker(page)
    freshness_changed = previous_freshness_marker != next_freshness_marker
    if expect_freshness_change is True and not freshness_changed:
        raise AssertionError(
            f"source refresh must advance data freshness in the local fixture, got {previous_freshness_marker} -> {next_freshness_marker}"
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
        "freshness_before": previous_freshness_marker,
        "freshness_after": next_freshness_marker,
        "freshness_changed": freshness_changed,
        "progress_hidden_after": progress_hidden,
        "status_summary": next_summary_cards.get("status", {}),
        "activity_surface": next_activity_surface,
    }


def _read_summary_cards(page: object) -> dict[str, dict[str, str]]:
    legacy_card_count = page.locator(
        "[data-summary-card='page_refresh'], [data-summary-card='status']"
    ).count()
    if legacy_card_count:
        raise AssertionError("updated/status summary must live in the table header, not separate summary cards")
    payload = page.evaluate(
        """() => {
          const node = document.querySelector('[data-table-summary-line]');
          const loadStatusNode = document.querySelector('[data-table-load-status]');
          const updatedNode = node ? node.querySelector('[data-table-summary-updated]') : null;
          const freshnessNode = node ? node.querySelector('[data-table-summary-freshness]') : null;
          const trimPrefix = (value, prefix) => {
            const text = String(value || '').trim();
            return text.startsWith(prefix) ? text.slice(prefix.length).trim() : text;
          };
          return {
            hidden: !node || !!node.hidden,
            load_status_hidden: !loadStatusNode || !!loadStatusNode.hidden,
            text: String((node && node.textContent) || '').trim(),
            load_status_text: String((loadStatusNode && loadStatusNode.getAttribute('data-load-status-text')) || '').trim(),
            load_status_title: String((loadStatusNode && loadStatusNode.getAttribute('title')) || '').trim(),
            load_status_visible_text: String((loadStatusNode && loadStatusNode.textContent) || '').trim(),
            load_status_dot_count: loadStatusNode ? loadStatusNode.querySelectorAll('.table-load-status-dot').length : 0,
            updated: trimPrefix(updatedNode ? updatedNode.textContent : '', 'обн:'),
            updated_at: updatedNode ? String(updatedNode.getAttribute('data-table-summary-updated-at') || '').trim() : '',
            freshness: trimPrefix(freshnessNode ? freshnessNode.textContent : '', 'свеж:'),
            freshness_at: freshnessNode ? String(freshnessNode.getAttribute('data-table-summary-freshness-at') || '').trim() : '',
            freshness_source: freshnessNode ? String(freshnessNode.getAttribute('data-table-summary-freshness-source') || '').trim() : '',
            status: String((loadStatusNode && loadStatusNode.getAttribute('data-load-status-text')) || '').trim(),
            status_detail: ''
          };
        }"""
    )
    if payload.get("hidden"):
        raise AssertionError(f"table header summary line must be visible, got {payload}")
    if payload.get("load_status_hidden"):
        raise AssertionError(f"load status must be visible inline with the load button, got {payload}")
    text = str(payload.get("text") or "")
    load_status_text = str(payload.get("load_status_text") or "")
    if "обн:" not in text or "свеж:" not in text or "Обновлено:" in text or "Свежесть данных:" in text or "Статус:" in text:
        raise AssertionError(f"table header summary must include only short updated/freshness labels, got {payload}")
    if payload.get("load_status_visible_text") or int(payload.get("load_status_dot_count") or 0) != 1:
        raise AssertionError(f"load status must be an icon-only lamp, got {payload}")
    if not str(payload.get("load_status_title") or "").startswith("Загрузка: "):
        raise AssertionError(f"load status lamp must keep textual status in title/aria, got {payload}")
    if "Статус последней загрузки" in load_status_text or "today_current" in load_status_text or "yesterday_closed" in load_status_text:
        raise AssertionError(f"load status must not expose technical/latest-window wording, got {payload}")
    if text.count("Asia/Yekaterinburg") != 0:
        raise AssertionError(f"table header summary must not show visible timezone, got {text!r}")
    cards = {
        "page_refresh": {
            "label": "Обновлено",
            "value": str(payload.get("updated") or ""),
            "detail": "",
            "updated_at": str(payload.get("updated_at") or ""),
        },
        "data_freshness": {
            "label": "Свежесть",
            "value": str(payload.get("freshness") or ""),
            "detail": str(payload.get("freshness_source") or ""),
            "updated_at": str(payload.get("freshness_at") or ""),
        },
        "status": {
            "label": "Статус",
            "value": str(payload.get("status") or ""),
            "detail": str(payload.get("status_detail") or ""),
            "updated_at": "",
        },
    }
    for required_card_id, required_label in {
        "page_refresh": "Обновлено",
        "data_freshness": "Свежесть",
        "status": "Статус",
    }.items():
        card = cards.get(required_card_id)
        if card is None:
            raise AssertionError(f"summary cards must expose {required_card_id!r}, got {cards}")
        if card["label"] != required_label:
            raise AssertionError(f"summary card {required_card_id!r} label mismatch, got {cards}")
    if "freshness" in cards or "rows" in cards:
        raise AssertionError(f"legacy freshness/rows cards must be merged into compact summary strip, got {cards}")
    if "period" in cards:
        raise AssertionError(f"period summary card must not be visible, got {cards}")
    if not cards["page_refresh"]["updated_at"]:
        raise AssertionError(f"page refresh card must expose an exact browser timestamp marker, got {cards['page_refresh']}")
    if not cards["data_freshness"]["value"]:
        raise AssertionError(f"data freshness line must expose a value or unknown state, got {cards['data_freshness']}")
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


def _read_summary_period_marker(page: object) -> str:
    marker = page.locator("[data-summary-grid]").evaluate(
        "node => node.getAttribute('data-summary-data-freshness') || node.getAttribute('data-summary-period-detail') || 'unknown'"
    )
    if not marker:
        raise AssertionError("summary grid must keep the non-visible data freshness marker")
    return marker


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
    if group_ids != ["wb_api", "onec_product_capital", "seller_portal_bot", "other_sources"]:
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


def _check_activity_metrics_preview(page: object) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const toggles = Array.from(document.querySelectorAll('[data-loading-metrics-toggle]'));
          const firstToggle = toggles[0] || null;
          const cell = firstToggle ? firstToggle.closest('[data-col-id="metrics"]') : null;
          const list = cell ? cell.querySelector('.activity-metric-list') : null;
          const beforeRect = list ? list.getBoundingClientRect() : {height: 0};
          const beforeText = list ? (list.textContent || '').trim() : '';
          const beforeClass = list ? (list.getAttribute('class') || '') : '';
          return {
            toggleCount: toggles.length,
            buttonText: firstToggle ? (firstToggle.textContent || '').trim() : '',
            beforeHeight: Math.round(beforeRect.height),
            beforeText,
            beforeClass,
            title: list ? (list.getAttribute('title') || '') : ''
          };
        }"""
    )
    if int(payload["toggleCount"]) < 1:
        raise AssertionError(f"loading table must expose compact metrics disclosure for long source lists, got {payload}")
    if not str(payload["buttonText"]).startswith("Показать все"):
        raise AssertionError(f"collapsed metric list must show explicit expand action, got {payload}")
    if "is-expanded" in str(payload["beforeClass"]) or int(payload["beforeHeight"]) > 48:
        raise AssertionError(f"collapsed metric list must stay compact, got {payload}")
    if not payload["title"] or "," not in str(payload["title"]):
        raise AssertionError(f"collapsed metric list must keep the full list in title, got {payload}")
    page.locator("[data-loading-metrics-toggle]").first.click()
    expanded = page.evaluate(
        """() => {
          const list = document.querySelector('.activity-metric-list.is-expanded');
          const toggle = document.querySelector('[data-loading-metrics-toggle]');
          const rect = list ? list.getBoundingClientRect() : {height: 0};
          return {
            expanded: !!list,
            text: list ? (list.textContent || '').trim() : '',
            buttonText: toggle ? (toggle.textContent || '').trim() : '',
            height: Math.round(rect.height)
          };
        }"""
    )
    if not expanded["expanded"] or expanded["buttonText"] != "Скрыть" or int(expanded["height"]) < int(payload["beforeHeight"]):
        raise AssertionError(f"expanded metric list must reveal full list and collapse action, got before={payload}, after={expanded}")
    page.locator("[data-loading-metrics-toggle]").first.click()
    return {"collapsed": payload, "expanded": expanded}


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
        "success": "успешно",
        "warning": "предупреждение",
        "error": "ошибка",
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
        "metric_label": 156,
        "section": 98,
    }
    for hidden_id in ("row_order", "scope_kind", "scope_key", "scope_label", "group", "nm_id", "metric_key"):
        if hidden_id in widths:
            raise AssertionError(f"{hidden_id} must be hidden from main table render, got {widths}")
    for column_id, max_width in required.items():
        if int(widths.get(column_id, 0)) <= 0:
            raise AssertionError(f"missing width measurement for {column_id!r}: {widths}")
        if strict and int(widths[column_id]) > max_width:
            raise AssertionError(f"{column_id} must stay compact in browser render, got {widths}")
    for column_id in [key for key in widths if key.startswith("date:")]:
        if strict and int(widths[column_id]) > 94:
            raise AssertionError(f"date column must stay narrow in browser render, got {widths}")
    first_visible = next((key for key in widths if key != "header"), "")
    if first_visible != "metric_label":
        raise AssertionError(f"metric_label must be the first visible column, got {widths}")
    return {
        "metric_label": int(widths["metric_label"]),
        "section": int(widths["section"]),
        "date": int(next(widths[key] for key in widths if key.startswith("date:"))),
    }


def _check_sticky_section_offsets(page: object) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const ids = ['metric_label', 'section'];
          const headers = Object.fromEntries(ids.map((id) => {
            const node = document.querySelector('[data-table-head] th[data-col-id="' + id + '"]');
            const style = node ? getComputedStyle(node) : null;
            return [id, {
              exists: !!node,
              position: style ? style.position : '',
              left: style ? Math.round(parseFloat(style.left || '0')) : -1,
              zIndex: style ? Number(style.zIndex || 0) : 0,
              background: style ? style.backgroundColor : ''
            }];
          }));
          const firstRow = document.querySelector('[data-table-body] tr:not(.group-row):not(.sku-separator-row)');
	          const scroll = document.querySelector('[data-table-scroll]');
	          if (scroll) {
	            scroll.scrollLeft = Math.min(420, Math.max(0, scroll.scrollWidth - scroll.clientWidth));
	          }
	          const metricCell = firstRow ? firstRow.querySelector('td[data-col-id="metric_label"]') : null;
	          const sectionCell = firstRow ? firstRow.querySelector('td[data-col-id="section"]') : null;
	          const metricRect = metricCell ? metricCell.getBoundingClientRect() : {left: 0, right: 0, width: 0};
	          const sectionRect = sectionCell ? sectionCell.getBoundingClientRect() : {left: 0, right: 0, width: 0};
	          const sectionCellStyle = sectionCell ? getComputedStyle(sectionCell) : null;
          const dateHeader = document.querySelector('[data-table-head] th[data-col-id^="date:"]');
          const dateHeaderStyle = dateHeader ? getComputedStyle(dateHeader) : null;
          return {
            headers,
            hiddenObjectHeaderCount: document.querySelectorAll('[data-table-head] th[data-col-id="scope_label"]').length,
	            sectionCell: {
	              exists: !!sectionCell,
	              position: sectionCellStyle ? sectionCellStyle.position : '',
	              left: sectionCellStyle ? Math.round(parseFloat(sectionCellStyle.left || '0')) : -1,
	              zIndex: sectionCellStyle ? Number(sectionCellStyle.zIndex || 0) : 0,
	              background: sectionCellStyle ? sectionCellStyle.backgroundColor : '',
	              rectLeft: Math.round(sectionRect.left),
	              rectRight: Math.round(sectionRect.right),
	              rectWidth: Math.round(sectionRect.width)
	            },
	            metricCell: {
	              exists: !!metricCell,
	              rectLeft: Math.round(metricRect.left),
	              rectRight: Math.round(metricRect.right),
	              rectWidth: Math.round(metricRect.width)
	            },
	            dateHeaderZIndex: dateHeaderStyle ? Number(dateHeaderStyle.zIndex || 0) : 0
	          };
	        }"""
    )
    headers = payload["headers"]
    if int(payload["hiddenObjectHeaderCount"]) != 0:
        raise AssertionError(f"object column must not render as a visible header, got {payload}")
    for column_id in ("metric_label", "section"):
        header = headers[column_id]
        if not header["exists"] or header["position"] != "sticky":
            raise AssertionError(f"{column_id} header must be sticky, got {payload}")
    if not payload["sectionCell"]["exists"] or payload["sectionCell"]["position"] != "sticky":
        raise AssertionError(f"section body cells must be sticky, got {payload}")
    if int(headers["metric_label"]["left"]) != 0 or int(headers["section"]["left"]) <= int(headers["metric_label"]["left"]):
        raise AssertionError(f"metric must be the first sticky column and section must follow it, got {payload}")
    if int(headers["section"]["zIndex"]) <= int(payload["dateHeaderZIndex"]):
        raise AssertionError(f"section sticky header must render above date headers, got {payload}")
    if not payload["metricCell"]["exists"]:
        raise AssertionError(f"metric body cells must be visible for sticky overlap check, got {payload}")
    if int(payload["sectionCell"]["rectLeft"]) < int(payload["metricCell"]["rectRight"]) - 1:
        raise AssertionError(f"section sticky column overlaps metric column after horizontal scroll, got {payload}")
    if abs(int(payload["sectionCell"]["rectWidth"]) - 76) > 24 or int(payload["metricCell"]["rectWidth"]) < 120:
        raise AssertionError(f"sticky body column widths must stay stable, got {payload}")
    if payload["sectionCell"]["background"] == "rgba(0, 0, 0, 0)":
        raise AssertionError(f"section sticky cell must have opaque background, got {payload}")
    return payload


def _check_percent_formatting(page: object, *, expected_rows: dict[str, str] | None) -> dict[str, str]:
    percent_rows = page.locator("[data-table-body] tr").evaluate_all(
        """rows => rows
	          .map(row => {
	            const valueNodes = Array.from(row.querySelectorAll('td[data-col-id^="date:"]'));
	            if (!valueNodes.length) {
	              return null;
	            }
	            const valueNode = valueNodes.slice().reverse().find((node) => ((node.getAttribute('title') || node.textContent || '').trim()) !== '—') || valueNodes[valueNodes.length - 1];
	            return {
	              metric_key: (valueNode.getAttribute('data-metric-key') || '').trim(),
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


def _check_static_group_labels(page: object) -> dict[str, object]:
    setup = page.evaluate(
        """() => {
          const scroll = document.querySelector('[data-table-scroll]');
          const rows = Array.from(document.querySelectorAll('[data-table-body] tr.group-row'));
          const labels = rows.map(row => ((row.querySelector('.group-row-label') || {}).textContent || '').trim());
          if (!scroll || rows.length < 2) {
            return {ready: false, labels, reason: 'missing scroll or group rows'};
          }
          const previousHeight = scroll.style.height || '';
          const previousMaxHeight = scroll.style.maxHeight || '';
          scroll.style.height = '120px';
          scroll.style.maxHeight = '120px';
          const targetIndex = Math.min(1, rows.length - 1);
          const target = rows[targetIndex];
          const maxScrollTop = Math.max(0, scroll.scrollHeight - scroll.clientHeight);
          scroll.scrollTop = Math.min(maxScrollTop, Math.max(0, target.offsetTop + 20));
          scroll.dispatchEvent(new Event('scroll', {bubbles: true}));
          return {
            ready: true,
            labels,
            targetIndex,
            scrollTop: scroll.scrollTop,
            maxScrollTop,
            previousHeight,
            previousMaxHeight
          };
        }"""
    )
    page.wait_for_timeout(150)
    payload = page.evaluate(
        """(setup) => {
          const scroll = document.querySelector('[data-table-scroll]');
          const rows = Array.from(document.querySelectorAll('[data-table-body] tr.group-row'));
          const target = rows[setup.targetIndex || 0];
          const cell = target ? target.querySelector('td') : null;
          const label = target ? target.querySelector('.group-row-label') : null;
          const totalGroup = rows.find(row => ((row.querySelector('.group-row-label') || {}).textContent || '').trim() === 'ИТОГО');
          const productGroup = rows.find(row => ((row.querySelector('.group-row-label') || {}).textContent || '').trim() && ((row.querySelector('.group-row-label') || {}).textContent || '').trim() !== 'ИТОГО');
          const totalDataRow = totalGroup ? totalGroup.nextElementSibling : null;
          const productDataRow = productGroup ? productGroup.nextElementSibling : null;
          const totalFirstCell = totalDataRow ? totalDataRow.querySelector('td') : null;
          const productFirstCell = productDataRow ? productDataRow.querySelector('td') : null;
          const header = document.querySelector('[data-table-head] th');
          const table = scroll ? scroll.querySelector('table') : null;
          const scrollRect = scroll ? scroll.getBoundingClientRect() : {left: 0, right: 0, width: 0};
          const labelRect = label ? label.getBoundingClientRect() : {top: 0, bottom: 0, left: 0, width: 0};
          const cellRect = cell ? cell.getBoundingClientRect() : {left: 0, right: 0, width: 0};
          const tableRect = table ? table.getBoundingClientRect() : {width: 0};
          const headerRect = header ? header.getBoundingClientRect() : {bottom: 0};
          const totalRowRect = totalDataRow ? totalDataRow.getBoundingClientRect() : {height: 0};
          const productRowRect = productDataRow ? productDataRow.getBoundingClientRect() : {height: 0};
          const scrollClientWidth = scroll ? scroll.clientWidth : 0;
          const visibleBandLeft = Math.max(cellRect.left, scrollRect.left);
          const visibleBandRight = Math.min(cellRect.right, scrollRect.right);
          const rowClassNames = rows.map(row => row.className || '');
          const rowInlineStyles = rows.map(row => row.getAttribute('style') || '');
          const cellClassNames = rows.map(row => ((row.querySelector('td') || {}).className || ''));
          const cellInlineStyles = rows.map(row => ((row.querySelector('td') || {}).getAttribute ? (row.querySelector('td').getAttribute('style') || '') : ''));
          const cellStyle = cell ? getComputedStyle(cell) : {
            backgroundColor: '',
            pointerEvents: '',
            position: '',
            top: ''
          };
          const labelStyle = label ? getComputedStyle(label) : {backgroundColor: ''};
          const totalCellStyle = totalFirstCell ? getComputedStyle(totalFirstCell) : {paddingTop: '', paddingBottom: '', lineHeight: ''};
          const productCellStyle = productFirstCell ? getComputedStyle(productFirstCell) : {paddingTop: '', paddingBottom: '', lineHeight: ''};
          return {
            ...setup,
            targetLabel: label ? (label.textContent || '').trim() : '',
            labelTop: Math.round(labelRect.top),
            labelBottom: Math.round(labelRect.bottom),
            labelLeft: Math.round(labelRect.left),
            headerBottom: Math.round(headerRect.bottom),
            labelWidth: Math.round(labelRect.width),
            cellWidth: Math.round(cellRect.width),
            cellLeft: Math.round(cellRect.left),
            cellRight: Math.round(cellRect.right),
            tableWidth: Math.round(tableRect.width),
            scrollClientWidth: Math.round(scrollClientWidth),
            scrollLeftEdge: Math.round(scrollRect.left),
            visibleBandWidth: Math.max(0, Math.round(visibleBandRight - visibleBandLeft)),
            cellBackground: cellStyle.backgroundColor,
            labelBackground: labelStyle.backgroundColor,
            cellPosition: cellStyle.position,
            cellTop: cellStyle.top,
            pointerEvents: cellStyle.pointerEvents,
            coversVisibleScrollWidth: Math.max(0, visibleBandRight - visibleBandLeft) >= Math.max(0, scrollClientWidth - 2),
            coversTableWidth: cellRect.width >= Math.max(0, tableRect.width - 2),
            forbiddenRowClass: rowClassNames.some(value => /sticky|fixed/i.test(value)),
            forbiddenCellClass: cellClassNames.some(value => /sticky|fixed/i.test(value)),
            forbiddenRowInlineStyle: rowInlineStyles.some(value => /position\\s*:\\s*(sticky|fixed)/i.test(value)),
            forbiddenCellInlineStyle: cellInlineStyles.some(value => /position\\s*:\\s*(sticky|fixed)/i.test(value)),
            totalDataRowClass: totalDataRow ? (totalDataRow.className || '') : '',
            productDataRowClass: productDataRow ? (productDataRow.className || '') : '',
            totalDataRowHeight: Math.round(totalRowRect.height),
            productDataRowHeight: Math.round(productRowRect.height),
            totalDataRowPaddingTop: totalCellStyle.paddingTop,
            totalDataRowPaddingBottom: totalCellStyle.paddingBottom,
            totalDataRowLineHeight: totalCellStyle.lineHeight,
            productDataRowPaddingTop: productCellStyle.paddingTop,
            productDataRowPaddingBottom: productCellStyle.paddingBottom,
            productDataRowLineHeight: productCellStyle.lineHeight
          };
        }""",
        setup,
    )
    page.evaluate(
        """(setup) => {
          const scroll = document.querySelector('[data-table-scroll]');
          if (!scroll) {
            return;
          }
          scroll.scrollTop = 0;
          scroll.style.height = setup.previousHeight || '';
          scroll.style.maxHeight = setup.previousMaxHeight || '';
        }""",
        setup,
    )
    if not payload.get("ready"):
        return {
            "skipped": "main table now follows metric scope-list order; section/group stays in data columns",
            "labels": payload.get("labels") or [],
            "reason": payload.get("reason") or "",
        }
    labels = [str(item) for item in payload.get("labels") or []]
    if "ИТОГО" not in labels or len([item for item in labels if item and item != "ИТОГО"]) < 1:
        raise AssertionError(f"table must render total and product group labels, got {payload}")
    expected_handoff_label = labels[int(payload["targetIndex"])]
    if payload["targetLabel"] != expected_handoff_label or payload["targetLabel"] == labels[0]:
        raise AssertionError(f"group band target mismatch while scrolling, got {payload}")
    if payload["cellPosition"] in {"sticky", "fixed"}:
        raise AssertionError(f"group rows must scroll as normal rows, got {payload}")
    if payload["forbiddenRowClass"] or payload["forbiddenCellClass"]:
        raise AssertionError(f"group rows must not expose sticky/fixed classes, got {payload}")
    if payload["forbiddenRowInlineStyle"] or payload["forbiddenCellInlineStyle"]:
        raise AssertionError(f"group rows must not expose sticky/fixed inline styles, got {payload}")
    if payload["labelTop"] >= payload["headerBottom"] - 4:
        raise AssertionError(f"group label must scroll away instead of sticking below the table header, got {payload}")
    if payload["cellBackground"] == "rgba(0, 0, 0, 0)":
        raise AssertionError(f"group band must keep its own full-width gray background, got {payload}")
    if payload["labelBackground"] != "rgba(0, 0, 0, 0)" or int(payload["labelWidth"]) <= 0:
        raise AssertionError(f"group label must remain left text on the band, got {payload}")
    if not payload["coversVisibleScrollWidth"] or not payload["coversTableWidth"]:
        raise AssertionError(f"group band must cover the visible scroll area and table width, got {payload}")
    if payload["labelLeft"] < payload["scrollLeftEdge"] - 2 or payload["labelLeft"] > payload["scrollLeftEdge"] + 24:
        raise AssertionError(f"group label must stay aligned to the left edge of the band, got {payload}")
    if payload["totalDataRowClass"] != payload["productDataRowClass"]:
        raise AssertionError(f"TOTAL first data row must use the same row class contract as regular data rows, got {payload}")
    if payload["totalDataRowPaddingTop"] != payload["productDataRowPaddingTop"]:
        raise AssertionError(f"TOTAL first data row must keep normal top padding, got {payload}")
    if payload["totalDataRowPaddingBottom"] != payload["productDataRowPaddingBottom"]:
        raise AssertionError(f"TOTAL first data row must keep normal bottom padding, got {payload}")
    if payload["totalDataRowLineHeight"] != payload["productDataRowLineHeight"]:
        raise AssertionError(f"TOTAL first data row must keep normal line-height, got {payload}")
    if int(payload["totalDataRowHeight"]) < int(payload["productDataRowHeight"]) - 1:
        raise AssertionError(f"TOTAL first data row must not be visually compressed, got {payload}")
    return payload


def _check_sku_separators(page: object) -> dict[str, object]:
    state = page.evaluate(
        """() => {
          const separators = Array.from(document.querySelectorAll('.sku-separator-row'));
          const heights = separators.map((row) => Math.round(row.getBoundingClientRect().height));
          const labels = separators.map((row) => ((row.querySelector('.sku-separator-label') || {}).textContent || '').trim()).filter(Boolean);
          const first = separators[0] || null;
          const previousKind = first && first.previousElementSibling ? first.previousElementSibling.getAttribute('data-row-kind') : '';
          const nextKind = first && first.nextElementSibling ? first.nextElementSibling.getAttribute('data-row-kind') : '';
          const skuSkuSeparatorCount = separators.filter((row) =>
            row.previousElementSibling &&
            row.nextElementSibling &&
            row.previousElementSibling.getAttribute('data-row-kind') === 'sku' &&
            row.nextElementSibling.getAttribute('data-row-kind') === 'sku' &&
            row.previousElementSibling.getAttribute('data-row-scope-label') !== row.nextElementSibling.getAttribute('data-row-scope-label')
          ).length;
          return {
            count: separators.length,
            minHeight: heights.length ? Math.min(...heights) : 0,
            labels,
            labeledCount: labels.length,
            firstLabel: labels[0] || '',
            firstBoundary: previousKind + '->' + nextKind,
            skuSkuSeparatorCount
          };
        }"""
    )
    if (
        int(state["count"]) <= 0
        or int(state["minHeight"]) < 24
        or state["firstBoundary"] != "total->sku"
        or int(state["labeledCount"]) != int(state["count"])
        or not state["firstLabel"]
    ):
        raise AssertionError(f"table must render labeled object separator rows from total to SKU and between SKU blocks, got {state}")
    sticky_state = page.evaluate(
        """() => {
          const scroll = document.querySelector('[data-table-scroll]');
          const metricHeader = document.querySelector('[data-table-head] th[data-col-id="metric_label"]');
          const separator = document.querySelector('.sku-separator-row');
          const label = separator ? separator.querySelector('.sku-separator-label') : null;
          if (!scroll || !metricHeader || !separator || !label) {
            return {ok: false, reason: 'missing nodes'};
          }
          const previousWidth = scroll.style.width || '';
          scroll.style.maxHeight = '180px';
          scroll.style.width = '420px';
          const headHeight = document.querySelector('[data-table-head]') ? document.querySelector('[data-table-head]').getBoundingClientRect().height : 0;
          scroll.scrollTop = Math.max(0, separator.offsetTop - headHeight - 4);
          scroll.scrollLeft = 0;
          scroll.dispatchEvent(new Event('scroll'));
          const metricBefore = metricHeader.getBoundingClientRect();
          const labelBefore = label.getBoundingClientRect();
          const beforeLeft = Math.round(labelBefore.left);
          const maxScrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
          scroll.scrollLeft = maxScrollLeft;
          scroll.dispatchEvent(new Event('scroll'));
          const metricAfter = metricHeader.getBoundingClientRect();
          const labelAfter = label.getBoundingClientRect();
          const originalText = label.textContent || '';
          const originalTitle = label.getAttribute('title') || '';
          label.textContent = 'SKU ' + 'длинное название '.repeat(12).trim();
          label.title = label.textContent;
          const longClientWidth = label.clientWidth;
          const longScrollWidth = label.scrollWidth;
          const longTitle = label.getAttribute('title') || '';
          label.textContent = originalText;
          label.title = originalTitle;
          scroll.style.maxHeight = '';
          scroll.style.width = previousWidth;
          return {
            ok: true,
            maxScrollLeft: Math.round(maxScrollLeft),
            beforeLeft,
            afterLeft: Math.round(labelAfter.left),
            afterRight: Math.round(labelAfter.right),
            metricLeft: Math.round(metricAfter.left),
            metricRight: Math.round(metricAfter.right),
            labelWidth: Math.round(labelAfter.width),
            metricWidth: Math.round(metricAfter.width),
            longClientWidth: Math.round(longClientWidth),
            longScrollWidth: Math.round(longScrollWidth),
            longTitle,
            stickyPosition: getComputedStyle(label).position,
            beforeMetricLeft: Math.round(metricBefore.left)
          };
        }"""
    )
    if (
        not sticky_state.get("ok")
        or int(sticky_state["maxScrollLeft"]) <= 0
        or sticky_state["stickyPosition"] != "sticky"
        or abs(int(sticky_state["beforeLeft"]) - int(sticky_state["afterLeft"])) > 2
        or int(sticky_state["afterLeft"]) < int(sticky_state["metricLeft"]) - 1
        or int(sticky_state["afterRight"]) > int(sticky_state["metricRight"]) + 2
        or int(sticky_state["longScrollWidth"]) <= int(sticky_state["longClientWidth"]) + 2
        or not str(sticky_state["longTitle"]).startswith("SKU ")
    ):
        raise AssertionError(f"SKU separator label must stay sticky inside the metric column and truncate long text, got {sticky_state}")
    page.evaluate("() => { const scroll = document.querySelector('[data-table-scroll]'); if (scroll) { scroll.scrollTop = 0; scroll.scrollLeft = 0; scroll.dispatchEvent(new Event('scroll')); } }")
    return {
        "separator_count": int(state["count"]),
        "min_height": int(state["minHeight"]),
        "first_boundary": str(state["firstBoundary"]),
        "sku_sku_separator_count": int(state["skuSkuSeparatorCount"]),
        "first_label": str(state["firstLabel"]),
        "sticky_after_horizontal_scroll": sticky_state,
    }


def _check_preset_calendar_sync(page: object) -> bool:
    state = page.evaluate(
        """() => {
          const start = document.querySelector('[data-history-day="2026-04-15"]');
          const middle = document.querySelector('[data-history-day="2026-04-18"]');
          const end = document.querySelector('[data-history-day="2026-04-21"]');
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
                write_rect="A1:C17",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100],
                    ["Итого: Заказы", "TOTAL|total_orderCount", 12],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1000],
                    ["Итого: В корзину", "TOTAL|total_cartCount", 18],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 60],
                    [f"SKU B: Показы в воронке", f"SKU:{second_nm_id}|view_count", 40],
                    [f"SKU A: Заказы", f"SKU:{first_nm_id}|orderCount", 7],
                    [f"SKU B: Заказы", f"SKU:{second_nm_id}|orderCount", 5],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 990],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1090],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 0.115],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 0.105],
                    [f"SKU A: Поиск", f"SKU:{first_nm_id}|views_current", 340],
                    [f"SKU B: Поиск", f"SKU:{second_nm_id}|views_current", 280],
                    [f"SKU A: Акция", f"SKU:{first_nm_id}|promo_participation", first_in_promo],
                    [f"SKU B: Акция", f"SKU:{second_nm_id}|promo_participation", second_in_promo],
                ],
                row_count=16,
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
            const metricNode = row.querySelector('td[data-col-id="metric_label"]');
            if (!metricNode || !row.hasAttribute('data-row-scope-label')) {
              return null;
            }
            return {
              scope_label: (row.getAttribute('data-row-scope-label') || '').trim(),
              metric_key: (metricNode.getAttribute('data-metric-key') || '').trim(),
              scope_kind: ((row.getAttribute('data-row-kind') || '').toLowerCase() === 'total' ? 'TOTAL' : 'SKU'),
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


def _check_dynamic_object_label(page: object) -> dict[str, object]:
    initial = page.evaluate(
        """() => {
          const label = document.querySelector('[data-table-object-label]');
          const right = document.querySelector('[data-table-heading-right]');
          const metricHeader = document.querySelector('[data-table-head] th[data-col-id="metric_label"]');
          const objectHeader = document.querySelector('[data-table-head] th[data-col-id="scope_label"]');
          const headers = Array.from(document.querySelectorAll('[data-table-head] th[data-col-id]')).map((node) => node.getAttribute('data-col-id'));
          const rightRect = right ? right.getBoundingClientRect() : {left: 0, right: 0};
          const labelRect = label ? label.getBoundingClientRect() : {width: 0};
          return {
            text: label ? (label.textContent || '').trim() : '',
            hidden: label ? !!label.hidden : true,
            title: label ? (label.getAttribute('title') || '') : '',
            firstHeader: headers[0] || '',
            objectHeaderCount: objectHeader ? 1 : 0,
            metricHeaderLeft: metricHeader ? Math.round(parseFloat(getComputedStyle(metricHeader).left || '0')) : -1,
            rightLeft: Math.round(rightRect.left),
            rightRight: Math.round(rightRect.right),
            labelWidth: Math.round(labelRect.width)
          };
        }"""
    )
    if (
        initial["hidden"]
        or initial["text"] != "Итого"
        or initial["firstHeader"] != "metric_label"
        or int(initial["objectHeaderCount"]) != 0
        or int(initial["metricHeaderLeft"]) != 0
    ):
        raise AssertionError(f"dynamic object label must replace the visible object column, got {initial}")
    long_label_layout = page.evaluate(
        """() => {
          const label = document.querySelector('[data-table-object-label]');
          const right = document.querySelector('[data-table-heading-right]');
          if (!label || !right) {
            return {ok: false, reason: 'missing nodes'};
          }
          const before = right.getBoundingClientRect();
          const originalText = label.textContent || '';
          const originalTitle = label.getAttribute('title') || '';
          label.textContent = 'SKU ' + 'очень длинное название товара '.repeat(8).trim();
          label.title = label.textContent;
          const after = right.getBoundingClientRect();
          const labelClientWidth = label.clientWidth;
          const labelScrollWidth = label.scrollWidth;
          label.textContent = originalText;
          label.title = originalTitle;
          return {
            ok: true,
            beforeLeft: Math.round(before.left),
            afterLeft: Math.round(after.left),
            beforeRight: Math.round(before.right),
            afterRight: Math.round(after.right),
            labelClientWidth: Math.round(labelClientWidth),
            labelScrollWidth: Math.round(labelScrollWidth),
            truncated: labelScrollWidth > labelClientWidth + 2
          };
        }"""
    )
    if (
        not long_label_layout.get("ok")
        or abs(int(long_label_layout["beforeLeft"]) - int(long_label_layout["afterLeft"])) > 2
        or abs(int(long_label_layout["beforeRight"]) - int(long_label_layout["afterRight"])) > 2
        or not long_label_layout.get("truncated")
    ):
        raise AssertionError(f"dynamic object label width must not move the right header area, got {long_label_layout}")
    scrolled = page.evaluate(
        """() => {
          const scroll = document.querySelector('[data-table-scroll]');
          const label = document.querySelector('[data-table-object-label]');
          const right = document.querySelector('[data-table-heading-right]');
          const firstSku = Array.from(document.querySelectorAll('[data-table-body] tr[data-row-kind="sku"]'))[0];
          if (!scroll || !label || !firstSku) {
            return {ok: false, reason: 'missing nodes'};
          }
          const rightBefore = right ? right.getBoundingClientRect() : {left: 0, right: 0};
          scroll.style.maxHeight = '180px';
          scroll.scrollTop = Math.max(0, firstSku.offsetTop + 2);
          scroll.dispatchEvent(new Event('scroll'));
          const rightAfter = right ? right.getBoundingClientRect() : {left: 0, right: 0};
          return {
            ok: true,
            text: (label.textContent || '').trim(),
            title: label.getAttribute('title') || '',
            firstSku: firstSku.getAttribute('data-row-scope-label') || '',
            scrollLeftBefore: scroll.scrollLeft,
            rightBeforeLeft: Math.round(rightBefore.left),
            rightAfterLeft: Math.round(rightAfter.left),
            rightBeforeRight: Math.round(rightBefore.right),
            rightAfterRight: Math.round(rightAfter.right)
          };
        }"""
    )
    page.wait_for_timeout(100)
    scrolled_after = page.evaluate(
        """() => {
          const scroll = document.querySelector('[data-table-scroll]');
          const label = document.querySelector('[data-table-object-label]');
          const beforeText = label ? (label.textContent || '').trim() : '';
          if (scroll) {
            scroll.scrollLeft = 160;
            scroll.dispatchEvent(new Event('scroll'));
          }
          return {
            text: label ? (label.textContent || '').trim() : '',
            title: label ? (label.getAttribute('title') || '') : '',
            beforeText
          };
        }"""
    )
    if (
        not scrolled.get("ok")
        or not scrolled.get("firstSku")
        or scrolled_after["text"] != scrolled["firstSku"]
        or scrolled_after["text"] == "Итого"
        or scrolled_after["title"] != scrolled_after["text"]
        or abs(int(scrolled["rightBeforeLeft"]) - int(scrolled["rightAfterLeft"])) > 2
        or abs(int(scrolled["rightBeforeRight"]) - int(scrolled["rightAfterRight"])) > 2
    ):
        raise AssertionError(f"dynamic object label must update to the top visible SKU and ignore horizontal scroll, got {scrolled} / {scrolled_after}")
    page.evaluate("() => { const scroll = document.querySelector('[data-table-scroll]'); if (scroll) { scroll.style.maxHeight = ''; scroll.scrollTop = 0; scroll.scrollLeft = 0; scroll.dispatchEvent(new Event('scroll')); } }")
    return {
        "initial": initial["text"],
        "scrolled": scrolled_after["text"],
        "right_zone_stable_with_long_label": long_label_layout,
        "object_column_absent": True,
    }


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
