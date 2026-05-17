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
        "sku_separators": ready_result["sku_separators"],
        "filter_controls": ready_result["filter_controls"],
        "status_summary": ready_result["status_summary"],
        "auto_schedule_block": ready_result["auto_schedule_block"],
        "activity_surface": ready_result["activity_surface"],
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
        "metric_filter_applied": ready_result["metric_filter_applied"],
        "metric_presentation": ready_result["metric_presentation"],
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
            initial_summary_cards = _read_summary_cards(page)
            status_summary = initial_summary_cards.get("status", {})
            initial_unloaded_activity_surface = _read_activity_surface(
                page,
                allow_empty_log=True,
            )
            initial_loading_rows = initial_unloaded_activity_surface["loading"]["rows"]
            initial_loading_groups = initial_unloaded_activity_surface["loading"]["groups"]
            if not any(row["source_key"] == "onec_stocks" for row in initial_loading_rows):
                raise AssertionError(
                    f"initial loading shell must expose the 1C source row before click, got {initial_unloaded_activity_surface}"
                )
            if not any(group["group_id"] == "onec_product_capital" for group in initial_loading_groups):
                raise AssertionError(
                    f"initial loading shell must expose the 1C source group before click, got {initial_unloaded_activity_surface}"
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
            if initial_history_state["label"].strip() != "15.04.2026 - 21.04.2026":
                raise AssertionError(f"default compact history label mismatch, got {initial_history_state}")
            if not initial_history_state["popoverHidden"]:
                raise AssertionError(f"history picker popover must be closed by default, got {initial_history_state}")
            if initial_history_state["dateFrom"] != "2026-04-15" or initial_history_state["dateTo"] != "2026-04-21":
                raise AssertionError(f"default history range must be week ending business today, got {initial_history_state}")
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
            static_group_labels = _check_static_group_labels(page)

            filter_controls = {
                "search": page.locator("[data-filter-control='search']").count() == 1,
                "section": page.locator("[data-filter-control='section']").count() == 1,
                "group": page.locator("[data-filter-control='group']").count() == 1,
                "scope_kind_absent": page.locator("[data-filter-control='scope_kind']").count() == 0,
                "metric": page.locator("[data-metric-manager]").count() == 1
                and page.locator("[data-metric-filter-option]").count() >= 2,
                "sort_absent": page.locator("[data-filter-control='sort']").count() == 0,
            }
            if not all(filter_controls.values()):
                raise AssertionError(f"missing filter controls: {filter_controls}")
            table_toolbar = page.evaluate(
                """() => {
                  const toolbar = document.querySelector('[data-table-toolbar]');
                  const tableShell = document.querySelector('[data-table-shell]');
                  const labels = toolbar ? Array.from(toolbar.querySelectorAll('.filter-label')).map((node) => (node.textContent || '').trim()).filter(Boolean) : [];
                  const fieldWidths = toolbar ? Object.fromEntries(Array.from(toolbar.querySelectorAll('.toolbar-field')).map((node) => [
                    (node.querySelector('.filter-label') || {}).textContent ? (node.querySelector('.filter-label').textContent || '').trim() : 'unknown',
                    Math.round(node.getBoundingClientRect().width)
                  ])) : {};
                  const toolbarText = toolbar ? (toolbar.innerText || '') : '';
                  const resetText = toolbar ? ((toolbar.querySelector('[data-reset-filters]') || {}).textContent || '').trim() : '';
                  const rect = toolbar ? toolbar.getBoundingClientRect() : {height: 0};
                  const toolbarStyle = toolbar ? getComputedStyle(toolbar) : {overflowX: '', overflowY: ''};
                  const beforeTable = !!toolbar && !!tableShell && !!(toolbar.compareDocumentPosition(tableShell) & Node.DOCUMENT_POSITION_FOLLOWING);
                  const logoutLink = document.querySelector('[data-logout-link]');
                  const tablist = document.querySelector('[role="tablist"]');
                  return {
                    exists: !!toolbar,
                    beforeTable: beforeTable,
                    labels: labels,
                    fieldWidths: fieldWidths,
                    toolbarText: toolbarText,
                    resetText: resetText,
                    height: Math.round(rect.height),
                    overflowX: toolbarStyle.overflowX,
                    overflowY: toolbarStyle.overflowY,
                    oldHeadingCount: Array.from(document.querySelectorAll('h2')).filter((node) => (node.textContent || '').trim() === 'Фильтры и настройки').length,
                    oldPanelTextVisible: (document.body.innerText || '').includes('Search/select/sort и выбор видимых столбцов'),
                    oldResetTextVisible: (document.body.innerText || '').includes('Сбросить фильтры'),
                    forbiddenSortVisible: toolbarText.includes('Сортировка'),
                    forbiddenScopeVisible: toolbarText.includes('Scope'),
                    forbiddenSummaryVisible: labels.includes('Итог') || /\\b\\d+\\s+из\\s+\\d+\\s+строк\\b/.test(toolbarText),
                    logoutText: logoutLink ? (logoutLink.textContent || '').trim() : '',
                    logoutHref: logoutLink ? (logoutLink.getAttribute('href') || '') : '',
                    logoutInTablist: !!(logoutLink && tablist && tablist.contains(logoutLink)),
                    logoutLooksLikeTab: !!(logoutLink && (logoutLink.getAttribute('class') || '').includes('unified-tab-button')),
                    columnManagerCount: document.querySelectorAll('[data-column-manager]').length,
                    columnResetCount: document.querySelectorAll('[data-columns-reset]').length
                  };
                }"""
            )
            expected_toolbar_labels = {"Диапазон", "Поиск", "Секции", "Группа", "Метрики", "Столбцы"}
            missing_toolbar_labels = expected_toolbar_labels.difference(set(table_toolbar["labels"]))
            if (
                not table_toolbar["exists"]
                or not table_toolbar["beforeTable"]
                or table_toolbar["oldHeadingCount"]
                or table_toolbar["oldPanelTextVisible"]
                or table_toolbar["oldResetTextVisible"]
                or table_toolbar["forbiddenSortVisible"]
                or table_toolbar["forbiddenScopeVisible"]
                or "Тип строк" in table_toolbar["toolbarText"]
                or table_toolbar["forbiddenSummaryVisible"]
                or table_toolbar["resetText"] != "Сброс"
                or table_toolbar["logoutText"] != "Выйти"
                or table_toolbar["logoutHref"] != "/logout"
                or table_toolbar["logoutInTablist"]
                or table_toolbar["logoutLooksLikeTab"]
                or table_toolbar["overflowX"] != "visible"
                or table_toolbar["overflowY"] != "visible"
                or int(table_toolbar["fieldWidths"].get("Поиск") or 0) > 180
                or table_toolbar["columnManagerCount"] != 1
                or table_toolbar["columnResetCount"] != 1
                or missing_toolbar_labels
            ):
                raise AssertionError(
                    f"table controls must live in one compact toolbar above the table, got {table_toolbar}, missing={missing_toolbar_labels}"
                )
            if table_toolbar["height"] > 78:
                raise AssertionError(f"table toolbar must stay compact, got {table_toolbar}")
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

            metric_filter_applied = _check_metric_multiselect_controls(page)
            metric_presentation = _check_metric_presentation_controls(page)

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
                    "() => (document.querySelector('[data-history-label]').textContent || '').trim() === '15.04.2026 - 21.04.2026'",
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
        "table_header": table_header_layout,
        "default_total_first": initial_order[0]["scope_label"] == "ИТОГО",
        "default_sku_metric_cluster": sku_cluster_ok,
        "sku_separators": sku_separators,
        "right_edge_spacer": right_edge_spacer,
        "static_group_labels": static_group_labels,
        "filter_controls": filter_controls,
        "table_toolbar": table_toolbar,
        "status_summary": status_summary,
        "auto_schedule_block": auto_schedule_block,
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
        "metric_filter_applied": metric_filter_applied,
        "metric_presentation": metric_presentation,
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
    if "table_header" in result:
        print("web_vitrina_browser_table_header: ok ->", result["table_header"])
    print("web_vitrina_browser_status_summary: ok ->", result["status_summary"])
    if "auto_schedule_block" in result:
        print("web_vitrina_browser_auto_schedule: ok ->", result["auto_schedule_block"])
    print("web_vitrina_browser_activity_surface: ok ->", result["activity_surface"])
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
    print("web_vitrina_browser_filters: ok ->", result["filter_controls"])
    if "table_toolbar" in result:
        print("web_vitrina_browser_table_toolbar: ok ->", result["table_toolbar"])
    print("web_vitrina_browser_metric_filter: ok ->", result["metric_filter_applied"])
    print("web_vitrina_browser_metric_presentation: ok ->", result["metric_presentation"])
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


def _check_metric_multiselect_controls(page: object) -> dict[str, object]:
    storage_key = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:selected-metrics:v1"
    manager = page.locator("[data-metric-manager]")
    if manager.count() != 1:
        raise AssertionError("metric manager must be rendered once")

    manager.locator("summary").click()
    _assert_details_open(manager, True, "metric manager must open")
    option_count = page.locator("[data-metric-filter-option]").count()
    if option_count < 2:
        raise AssertionError(f"metric manager must expose at least two checkbox rows, got {option_count}")
    metric_rows = page.evaluate(
        """() => Array.from(document.querySelectorAll('[data-metric-filter-option]')).slice(0, 2).map((node) => ({
          value: node.value || '',
          label: ((node.closest('label') || {}).innerText || '').trim()
        }))"""
    )
    selected_keys = [str(item["value"]) for item in metric_rows if item.get("value")]
    if len(selected_keys) != 2 or selected_keys[0] == selected_keys[1]:
        raise AssertionError(f"metric manager must expose distinct metric keys, got {metric_rows}")
    grouped_state = page.evaluate(
        """() => ({
          scopeLabels: Array.from(document.querySelectorAll('[data-metric-scope-title]')).map((node) => (node.textContent || '').trim()),
          sectionLabels: Array.from(document.querySelectorAll('[data-metric-section-title]')).map((node) => (node.textContent || '').trim()),
          groups: Array.from(document.querySelectorAll('[data-metric-scope-group]')).map((group) => ({
            id: group.getAttribute('data-metric-scope-group') || '',
            label: ((group.querySelector('[data-metric-scope-title]') || {}).textContent || '').trim(),
            optionCount: group.querySelectorAll('[data-metric-filter-option]').length,
            sectionCount: group.querySelectorAll('[data-metric-section-group]').length
          }))
        })"""
    )
    if grouped_state["scopeLabels"] != ["Итого", "SKU"]:
        raise AssertionError(f"metric manager must group options by Итого/SKU, got {grouped_state}")
    if not grouped_state["sectionLabels"] or any(not label for label in grouped_state["sectionLabels"]):
        raise AssertionError(f"metric manager must expose existing section headings, got {grouped_state}")
    if any(int(item["optionCount"]) <= 0 or int(item["sectionCount"]) <= 0 for item in grouped_state["groups"]):
        raise AssertionError(f"metric manager grouped sections must contain checkbox rows, got {grouped_state}")
    initial_summary = page.locator("[data-metric-summary-label]").inner_text().strip()
    if initial_summary != "Все метрики":
        raise AssertionError(f"default metric summary must show all metrics, got {initial_summary!r}")
    if not page.locator("[data-metric-filter-all]").is_checked():
        raise AssertionError("all-metrics checkbox must be checked by default")

    page.locator("[data-metric-filter-all]").click()
    _assert_details_open(manager, True, "all-metrics checkbox click must keep dropdown open")
    zero_summary = page.locator("[data-metric-summary-label]").inner_text().strip()
    if zero_summary != "Метрики: 0":
        raise AssertionError(f"empty metric selection summary mismatch, got {zero_summary!r}")

    page.locator("[data-metric-filter-option]").nth(0).check()
    _assert_details_open(manager, True, "metric checkbox click must keep dropdown open")
    page.locator("[data-metric-filter-option]").nth(1).check()
    _assert_details_open(manager, True, "second metric checkbox click must keep dropdown open")
    page.wait_for_timeout(150)
    selected_summary = page.locator("[data-metric-summary-label]").inner_text().strip()
    if selected_summary != "Метрики: 2":
        raise AssertionError(f"selected metric summary mismatch, got {selected_summary!r}")
    visible_keys = _visible_metric_keys(page)
    if set(visible_keys) != set(selected_keys):
        raise AssertionError(f"table must show rows for exactly the two selected metrics, got {visible_keys}, expected {selected_keys}")

    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    restored_summary = page.locator("[data-metric-summary-label]").inner_text().strip()
    restored_visible_keys = _visible_metric_keys(page)
    if restored_summary != "Метрики: 2" or set(restored_visible_keys) != set(selected_keys):
        raise AssertionError(
            f"metric selection must survive reload, got summary={restored_summary!r}, keys={restored_visible_keys}"
        )

    page.evaluate("(key) => window.localStorage.setItem(key, '{broken-json')", storage_key)
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    corrupted_summary = page.locator("[data-metric-summary-label]").inner_text().strip()
    if corrupted_summary != "Все метрики":
        raise AssertionError(f"corrupted metric localStorage must fall back to all metrics, got {corrupted_summary!r}")

    page.evaluate(
        """(key) => window.localStorage.setItem(key, JSON.stringify({
          version: 1,
          selected_metric_keys: ['obsolete_metric_key']
        }))""",
        storage_key,
    )
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    obsolete_summary = page.locator("[data-metric-summary-label]").inner_text().strip()
    all_visible_keys = _visible_metric_keys(page)
    if obsolete_summary != "Все метрики" or len(set(all_visible_keys)) < option_count:
        raise AssertionError(
            f"obsolete metric localStorage must fall back to all metrics, got summary={obsolete_summary!r}, keys={all_visible_keys}"
        )
    return {
        "checkbox_rows": option_count,
        "selected_keys": selected_keys,
        "groups": grouped_state["scopeLabels"],
        "sections": grouped_state["sectionLabels"],
        "restored_after_reload": True,
        "corrupted_storage_fallback": True,
        "obsolete_storage_fallback": True,
    }


def _check_metric_presentation_controls(page: object) -> dict[str, object]:
    storage_key = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:metric-presentation:v1"
    page.locator("[data-reset-filters]").click()
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    panel_state = page.evaluate(
        """() => {
          const toolbar = document.querySelector('[data-table-toolbar]');
          const panel = document.querySelector('[data-metrics-presentation]');
          const table = document.querySelector('[data-table-shell]');
          const beforeTable = !!panel && !!table && !!(panel.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING);
          const afterToolbar = !!toolbar && !!panel && !!(toolbar.compareDocumentPosition(panel) & Node.DOCUMENT_POSITION_FOLLOWING);
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
          const rows = Array.from(document.querySelectorAll('[data-metric-config-row]'));
          const groupsById = new Map();
          rows.forEach((row) => {
            const groupId = row.getAttribute('data-metric-config-group-id') || '';
            if (!groupsById.has(groupId)) {
              groupsById.set(groupId, []);
            }
            groupsById.get(groupId).push(row.getAttribute('data-metric-config-row') || '');
          });
          const groups = Array.from(groupsById.entries()).map(([id, groupRows]) => ({id, rows: groupRows}));
          const scopeGroups = Array.from(panel ? panel.querySelectorAll('[data-metrics-config-zone="visible"]') : []).map((zone) => ({
            scopeId: zone.getAttribute('data-metrics-config-scope') || '',
            groups: Array.from(zone.querySelectorAll('[data-metrics-config-group]')).map((section) => {
              const up = section.querySelector('[data-metric-group-action="up"]');
              const down = section.querySelector('[data-metric-group-action="down"]');
              return {
                id: section.getAttribute('data-metrics-config-group') || '',
                rows: Array.from(section.querySelectorAll('[data-metric-config-row]'))
                  .map((row) => row.getAttribute('data-metric-config-row') || '')
                  .filter(Boolean),
                upDisabled: !!(up && up.disabled),
                downDisabled: !!(down && down.disabled)
              };
            }).filter((group) => group.id && group.rows.length)
          })).filter((scope) => scope.scopeId && scope.groups.length);
          const rowHeights = rows.map((row) => row.getBoundingClientRect().height);
          const arrowGaps = rows.map((row) => {
            const label = row.querySelector('.metrics-config-label');
            const up = row.querySelector('[data-metric-config-action="up"]');
            if (!label || !up) {
              return 0;
            }
            return Math.max(0, up.getBoundingClientRect().left - label.getBoundingClientRect().right);
          });
          const whiteNodes = Array.from(panel ? panel.querySelectorAll([
            '.metrics-presentation-body',
            '.metrics-config-zone',
            '.metrics-config-row',
            '.metrics-config-button',
            '.metrics-config-block-move-button'
          ].join(',')) : []).filter((node) => isNearWhite(getComputedStyle(node).backgroundColor));
          const actionLabels = Array.from(panel ? panel.querySelectorAll('[data-metric-config-action="hide"], [data-metric-config-action="show"]') : [])
            .map((node) => (node.textContent || '').trim());
          return {
            exists: !!panel,
            afterToolbar,
            beforeTable,
            summary: ((document.querySelector('[data-metrics-presentation-summary]') || {}).textContent || '').trim(),
            groupCount: groups.length,
            groups,
            scopeGroups,
            blockControlCount: panel ? panel.querySelectorAll('[data-metric-group-action]').length : 0,
            gridColumns,
            zoneTitles: Array.from(panel ? panel.querySelectorAll('.metrics-config-zone-kind') : []).map((node) => (node.textContent || '').trim()),
            actionLabels,
            oldLabelHits: ['Показывать сразу', 'Скрыто под раскрытием', 'Скрыть под раскрытием', 'Показать сразу'].filter((item) => text.includes(item)),
            whiteNodeCount: whiteNodes.length,
            maxRowHeight: rowHeights.length ? Math.max(...rowHeights) : 0,
            maxArrowGap: arrowGaps.length ? Math.max(...arrowGaps) : 0,
            scopeCount: Number(grid ? (grid.getAttribute('data-metrics-config-scope-count') || '0') : '0')
          };
        }"""
    )
    if (
        not panel_state["exists"]
        or not panel_state["afterToolbar"]
        or not panel_state["beforeTable"]
        or int(panel_state["groupCount"]) <= 0
    ):
        raise AssertionError(f"metrics presentation panel must sit between toolbar and table, got {panel_state}")
    if int(panel_state["blockControlCount"]) <= 0:
        raise AssertionError(f"metrics presentation groups must expose block order controls, got {panel_state}")
    for scope in panel_state["scopeGroups"]:
        scope_groups = scope["groups"]
        if scope_groups and not scope_groups[0]["upDisabled"]:
            raise AssertionError(f"first metric group in scope must disable ↑, got {scope}")
        if scope_groups and not scope_groups[-1]["downDisabled"]:
            raise AssertionError(f"last metric group in scope must disable ↓, got {scope}")
    block_scope = next(
        (scope for scope in panel_state["scopeGroups"] if len(scope["groups"]) >= 2),
        None,
    )
    if block_scope is None:
        raise AssertionError(f"metrics presentation needs at least two groups in one scope for block-order controls, got {panel_state}")
    target_group = next((group for group in panel_state["groups"] if len(group["rows"]) >= 2), None)
    if target_group is None:
        raise AssertionError(f"metrics presentation needs a group with at least two selected metrics, got {panel_state}")
    if int(panel_state["scopeCount"]) >= 2 and int(panel_state["gridColumns"]) != 4:
        raise AssertionError(f"metrics presentation must use four desktop columns for two scope buckets, got {panel_state}")
    if int(panel_state["whiteNodeCount"]) != 0 or panel_state["oldLabelHits"]:
        raise AssertionError(f"metrics presentation must use compact dark labels with no old wording, got {panel_state}")
    if "Показано" not in panel_state["zoneTitles"] or "Скрыто" not in panel_state["zoneTitles"]:
        raise AssertionError(f"metrics presentation zones must be named Показано/Скрыто, got {panel_state}")
    if not set(panel_state["actionLabels"]).issubset({"Скрыть", "Показать"}):
        raise AssertionError(f"metrics presentation action buttons must be compact, got {panel_state}")
    if float(panel_state["maxRowHeight"]) > 34 or float(panel_state["maxArrowGap"]) > 10:
        raise AssertionError(f"metrics rows must be compact and keep arrows near labels, got {panel_state}")

    block_scope_id = str(block_scope["scopeId"])
    initial_block_order = [str(group["id"]) for group in block_scope["groups"]]
    first_block = block_scope["groups"][0]
    second_block = block_scope["groups"][1]
    selected_block_keys = [str(first_block["rows"][0]), str(second_block["rows"][0])]
    if selected_block_keys[0] == selected_block_keys[1]:
        raise AssertionError(f"block-order check needs distinct metric keys, got {selected_block_keys}")

    page.evaluate(
        """(keys) => {
          const allNode = document.querySelector('[data-metric-filter-all]');
          if (allNode && allNode.checked) {
            allNode.click();
          }
          keys.forEach((key) => {
            const node = Array.from(document.querySelectorAll('[data-metric-filter-option]')).find((item) => item.value === key);
            if (node && !node.checked) {
              node.click();
            }
          });
          const panel = document.querySelector('[data-metrics-presentation]');
          if (panel) {
            panel.open = true;
          }
        }""",
        selected_block_keys,
    )
    page.wait_for_function(
        """(keys) => {
          const visibleKeys = Array.from(new Set(Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
            .map((node) => node.getAttribute('data-metric-key') || '')
            .filter(Boolean)));
          return visibleKeys.length === keys.length && keys.every((key) => visibleKeys.includes(key));
        }""",
        arg=selected_block_keys,
        timeout=5000,
    )
    selected_scope_order = _metric_group_order_for_scope(page, block_scope_id)
    if selected_scope_order[:2] != initial_block_order[:2]:
        raise AssertionError(
            f"selected block groups must keep default order before move, got {selected_scope_order}, expected {initial_block_order[:2]}"
        )
    _click_metric_group_action(page, scope_id=block_scope_id, group_id=str(first_block["id"]), action="down")
    page.wait_for_timeout(150)
    moved_scope_order = _metric_group_order_for_scope(page, block_scope_id)
    if moved_scope_order[:2] != [initial_block_order[1], initial_block_order[0]]:
        raise AssertionError(f"block order controls must reorder panel groups, got {moved_scope_order}")
    table_order_after_group_move = _visible_metric_keys(page)
    if table_order_after_group_move[:2] != [selected_block_keys[1], selected_block_keys[0]]:
        raise AssertionError(
            f"block order controls must reorder table groups, got {table_order_after_group_move[:6]}, expected {selected_block_keys[::-1]}"
        )
    persisted_group_order = _persisted_metric_group_order(page, storage_key, block_scope_id)
    if persisted_group_order[:2] != [initial_block_order[1], initial_block_order[0]]:
        raise AssertionError(f"block order must persist in metric-presentation storage, got {persisted_group_order}")
    persisted_custom_scopes = _persisted_metric_group_order_custom_scopes(page, storage_key)
    if block_scope_id not in persisted_custom_scopes:
        raise AssertionError(f"block order persistence must mark the moved scope as custom, got {persisted_custom_scopes}")

    page.locator("[data-reset-filters]").click()
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    page.evaluate("() => { const panel = document.querySelector('[data-metrics-presentation]'); if (panel) { panel.open = true; } }")
    reset_scope_order = _metric_group_order_for_scope(page, block_scope_id)
    if reset_scope_order != initial_block_order:
        raise AssertionError(f"reset must restore default block order, got {reset_scope_order}, expected {initial_block_order}")
    reset_persisted_group_order = _persisted_metric_group_order(page, storage_key, block_scope_id)
    if reset_persisted_group_order != initial_block_order:
        raise AssertionError(
            f"reset must persist default block order, got {reset_persisted_group_order}, expected {initial_block_order}"
        )
    reset_custom_scopes = _persisted_metric_group_order_custom_scopes(page, storage_key)
    if reset_custom_scopes:
        raise AssertionError(f"reset must clear custom block-order scopes, got {reset_custom_scopes}")

    selected_keys = [str(target_group["rows"][0]), str(target_group["rows"][1])]

    page.evaluate(
        """(keys) => {
          const allNode = document.querySelector('[data-metric-filter-all]');
          if (allNode && allNode.checked) {
            allNode.click();
          }
          keys.forEach((key) => {
            const node = Array.from(document.querySelectorAll('[data-metric-filter-option]')).find((item) => item.value === key);
            if (node && !node.checked) {
              node.click();
            }
          });
          const panel = document.querySelector('[data-metrics-presentation]');
          if (panel) {
            panel.open = true;
          }
        }""",
        selected_keys,
    )
    page.wait_for_function(
        """(keys) => {
          const visibleKeys = Array.from(new Set(Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
            .map((node) => node.getAttribute('data-metric-key') || '')
            .filter(Boolean)));
          return visibleKeys.length === keys.length && keys.every((key) => visibleKeys.includes(key));
        }""",
        arg=selected_keys,
        timeout=5000,
    )
    visible_before = _visible_metric_key_counts(page)
    if set(visible_before) != set(selected_keys):
        raise AssertionError(f"metrics panel must not keep phantom unselected metrics, got {visible_before}, expected {selected_keys}")

    _click_metric_config_action(page, group_id=target_group["id"], metric_key=selected_keys[0], action="down")
    page.wait_for_timeout(150)
    panel_order_after_move = page.evaluate(
        """(groupId) => Array.from(document.querySelectorAll('[data-metric-config-row][data-metric-config-group-id="' + groupId + '"]'))
          .map((row) => row.getAttribute('data-metric-config-row') || '')""",
        target_group["id"],
    )
    if panel_order_after_move[:2] != [selected_keys[1], selected_keys[0]]:
        raise AssertionError(f"metric order controls must reorder panel rows, got {panel_order_after_move}")
    table_order_after_move = _visible_metric_keys(page)
    if table_order_after_move[:2] != [selected_keys[1], selected_keys[0]]:
        raise AssertionError(f"metric order controls must reorder table rows, got {table_order_after_move[:6]}")

    _click_metric_config_action(page, group_id=target_group["id"], metric_key=selected_keys[0], action="hide")
    page.wait_for_timeout(150)
    hidden_counts = _visible_metric_key_counts(page)
    if int(hidden_counts.get(selected_keys[0], 0)) >= int(visible_before.get(selected_keys[0], 0)):
        raise AssertionError(f"hidden-under-anchor metric must reduce visible table rows, got before={visible_before}, after={hidden_counts}")
    toggle = page.locator("[data-metric-group-toggle]").first
    if toggle.count() != 1:
        raise AssertionError("hidden metric must expose one anchor toggle in the table")
    toggle_text = toggle.inner_text().strip()
    if "Показать ещё" not in toggle_text:
        raise AssertionError(f"anchor toggle must invite expansion, got {toggle_text!r}")
    toggle.click()
    page.wait_for_timeout(150)
    expanded_counts = _visible_metric_key_counts(page)
    if int(expanded_counts.get(selected_keys[0], 0)) <= int(hidden_counts.get(selected_keys[0], 0)):
        raise AssertionError(f"anchor toggle must reveal hidden metric rows, got hidden={hidden_counts}, expanded={expanded_counts}")
    if "Скрыть" not in toggle.inner_text().strip():
        raise AssertionError("expanded anchor toggle must switch to Скрыть")

    page.locator("[data-reset-filters]").click()
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=5000)
    reset_toggle_count = page.locator("[data-metric-group-toggle]").count()
    if reset_toggle_count != 0:
        raise AssertionError("reset must clear hidden-under-anchor presentation state")

    page.evaluate("(key) => window.localStorage.setItem(key, '{broken-json')", storage_key)
    page.reload(wait_until="commit")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    if page.locator("[data-metrics-presentation]").count() != 1 or page.locator("[data-table-body] tr").count() <= 0:
        raise AssertionError("broken metric-presentation localStorage must not crash the page")
    return {
        "selected_keys": selected_keys,
        "block_order_scope": block_scope_id,
        "block_order_changed": moved_scope_order[:2],
        "order_changed": panel_order_after_move[:2],
        "hidden_count_before_expand": hidden_counts.get(selected_keys[0], 0),
        "expanded_count": expanded_counts.get(selected_keys[0], 0),
        "broken_storage_fallback": True,
    }


def _visible_metric_keys(page: object) -> list[str]:
    return page.evaluate(
        """() => Array.from(new Set(Array.from(document.querySelectorAll('[data-table-body] [data-metric-key]'))
          .map((node) => node.getAttribute('data-metric-key') || '')
          .filter(Boolean)))"""
    )


def _metric_group_order_for_scope(page: object, scope_id: str) -> list[str]:
    return page.evaluate(
        """(scopeId) => {
          const selector = '[data-metrics-config-zone="visible"][data-metrics-config-scope="' + scopeId + '"] [data-metrics-config-group]';
          return Array.from(document.querySelectorAll(selector))
            .map((node) => node.getAttribute('data-metrics-config-group') || '')
            .filter(Boolean);
        }""",
        scope_id,
    )


def _click_metric_group_action(page: object, *, scope_id: str, group_id: str, action: str) -> None:
    clicked = page.evaluate(
        """({scopeId, groupId, action}) => {
          const button = Array.from(document.querySelectorAll('[data-metric-group-action]')).find((node) =>
            !node.disabled &&
            (node.getAttribute('data-metric-config-scope') || '') === scopeId &&
            (node.getAttribute('data-metric-config-group') || '') === groupId &&
            (node.getAttribute('data-metric-group-action') || '') === action
          );
          if (!button) {
            return false;
          }
          button.click();
          return true;
        }""",
        {"scopeId": scope_id, "groupId": group_id, "action": action},
    )
    if not clicked:
        raise AssertionError(f"missing metric group action {action!r} for {scope_id!r}/{group_id!r}")


def _persisted_metric_group_order(page: object, storage_key: str, scope_id: str) -> list[str]:
    return page.evaluate(
        """({storageKey, scopeId}) => {
          try {
            const parsed = JSON.parse(window.localStorage.getItem(storageKey) || '{}');
            return (((parsed || {}).group_order_by_scope || {})[scopeId] || []).filter(Boolean);
          } catch (error) {
            return [];
          }
        }""",
        {"storageKey": storage_key, "scopeId": scope_id},
    )


def _persisted_metric_group_order_custom_scopes(page: object, storage_key: str) -> list[str]:
    return page.evaluate(
        """(storageKey) => {
          try {
            const parsed = JSON.parse(window.localStorage.getItem(storageKey) || '{}');
            return ((parsed || {}).group_order_custom_scopes || []).filter(Boolean);
          } catch (error) {
            return [];
          }
        }""",
        storage_key,
    )


def _click_metric_config_action(page: object, *, group_id: str, metric_key: str, action: str) -> None:
    clicked = page.evaluate(
        """({groupId, metricKey, action}) => {
          const button = Array.from(document.querySelectorAll('[data-metric-config-action]')).find((node) =>
            (node.getAttribute('data-metric-config-group') || '') === groupId &&
            (node.getAttribute('data-metric-config-key') || '') === metricKey &&
            (node.getAttribute('data-metric-config-action') || '') === action
          );
          if (!button) {
            return false;
          }
          button.click();
          return true;
        }""",
        {"groupId": group_id, "metricKey": metric_key, "action": action},
    )
    if not clicked:
        raise AssertionError(f"missing metric presentation action {action!r} for {group_id!r}/{metric_key!r}")


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
    manager = page.locator("[data-column-manager]")
    if manager.count() != 1:
        raise AssertionError("column visibility manager must be rendered once")
    page.evaluate("() => { const node = document.querySelector('[data-column-manager]'); if (node) { node.open = true; } }")
    visual_state = page.evaluate(
        """() => {
          const rows = Array.from(document.querySelectorAll('[data-column-visibility-controls] .column-checkbox'));
          const rects = rows.map((node) => Math.round(node.getBoundingClientRect().height));
          const styles = rows.map((node) => {
            const style = getComputedStyle(node);
            return {
              borderLeft: style.borderLeftWidth,
              borderRight: style.borderRightWidth,
              borderRadius: style.borderRadius,
              background: style.backgroundColor
            };
          });
          return {
            rowCount: rows.length,
            maxHeight: rects.length ? Math.max(...rects) : 0,
            cardLikeRows: styles.filter((style) => style.borderLeft !== '0px' || style.borderRight !== '0px').length,
            missingMetricLabelToggle: document.querySelectorAll('[data-column-visibility-id="metric_label"]').length === 0,
            missingScopeLabelToggle: document.querySelectorAll('[data-column-visibility-id="scope_label"]').length === 0,
            missingSectionToggle: document.querySelectorAll('[data-column-visibility-id="section"]').length === 0,
            dateToggleCount: document.querySelectorAll('[data-column-visibility-id^="date:"]').length
          };
        }"""
    )
    if (
        visual_state["rowCount"] <= 0
        or visual_state["maxHeight"] > 48
        or visual_state["cardLikeRows"] != 0
        or not visual_state["missingMetricLabelToggle"]
        or not visual_state["missingScopeLabelToggle"]
        or not visual_state["missingSectionToggle"]
        or visual_state["dateToggleCount"] != 0
    ):
        raise AssertionError(f"column manager must render a compact checklist without mandatory/date toggles, got {visual_state}")
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
        "compact_checklist": visual_state,
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
          const progress = header ? header.querySelector('[data-global-progress]') : null;
          const loadButton = header ? header.querySelector('[data-load-refresh-button]') : null;
          const source = header ? header.querySelector('.table-source-kicker') : null;
          const text = header ? (header.innerText || '') : '';
          const metaText = pageMeta ? ((pageMeta.textContent || '').trim()) : '';
          const buttonRect = loadButton ? loadButton.getBoundingClientRect() : {left: 0, right: 0, top: 0, bottom: 0};
          const statusRect = loadStatus ? loadStatus.getBoundingClientRect() : {left: 0, right: 0, top: 0, bottom: 0};
          const headerRect = header ? header.getBoundingClientRect() : {left: 0, right: 0, width: 0};
          const forbidden = ['sheet_vitrina_v1', 'Основная web-витрина', 'В выбранном периоде', 'grid library', 'rows:', 'columns:', 'Снимок:', 'Вчера:', 'Сегодня:', 'TZ:', 'Статус последней загрузки', 'today_current', 'yesterday_closed', 'load window'];
          return {
            top_panel_count: document.querySelectorAll('[data-top-panel]').length,
            table_header_count: document.querySelectorAll('[data-table-header]').length,
            page_meta_outside_header_count: Array.from(document.querySelectorAll('[data-page-meta]'))
              .filter(node => !node.closest('[data-table-header]')).length,
            table_meta_outside_header_count: Array.from(document.querySelectorAll('[data-table-meta]'))
              .filter(node => !node.closest('[data-table-header]')).length,
            progress_inside_header: !!progress,
            load_button_inside_header: !!loadButton,
            source_text: source ? ((source.textContent || '').trim()) : '',
            page_meta: metaText,
            table_meta: tableMeta ? ((tableMeta.textContent || '').trim()) : '',
            summary_text: summary ? ((summary.textContent || '').trim()) : '',
            load_status_text: loadStatus ? ((loadStatus.textContent || '').trim()) : '',
            load_status_hidden: loadStatus ? !!loadStatus.hidden : null,
            load_button_text: loadButton ? ((loadButton.textContent || '').trim()) : '',
            load_button_right_aligned: !!loadButton && buttonRect.left > headerRect.left + headerRect.width / 2,
            load_status_inline_left_of_button: !!loadStatus && !loadStatus.hidden && statusRect.right <= buttonRect.left + 2 && Math.abs(((statusRect.top + statusRect.bottom) / 2) - ((buttonRect.top + buttonRect.bottom) / 2)) <= 8,
            asia_yekaterinburg_in_meta: (metaText.match(/Asia\\/Yekaterinburg/g) || []).length,
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
        or payload["source_text"] != ""
        or payload["load_button_text"] != "Загрузить"
        or not payload["load_button_right_aligned"]
        or not payload["load_status_inline_left_of_button"]
        or payload["load_status_hidden"]
        or not payload["has_freshness_badge"]
    ):
        raise AssertionError(f"source/header controls must be compactly inside the table header, got {payload}")
    if payload["page_meta"] or payload["table_meta"] or payload["forbidden_hits"]:
        raise AssertionError(f"table header must not expose old technical/source text, got {payload}")
    if "Обновлено:" not in payload["summary_text"] or "Свежесть данных:" not in payload["summary_text"]:
        raise AssertionError(f"table header summary must expose compact freshness timestamps, got {payload}")
    if not str(payload["load_status_text"]).startswith("Последняя загрузка: "):
        raise AssertionError(f"load status must use compact latest-load wording, got {payload}")
    if str(payload["load_status_text"]).removeprefix("Последняя загрузка: ") not in {
        "успешно",
        "ошибка",
        "предупреждение",
        "нет данных",
    }:
        raise AssertionError(f"load status must use a short semantic value, got {payload}")
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
              toolbar: nodeIndex('[data-table-toolbar]'),
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
    expected_order = [order_values[key] for key in ("summary", "toolbar", "table", "actions")]
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
            disclosureVisible: !!(disclosure && disclosure.getBoundingClientRect().width > 0)
          };
        }"""
    )
    if collapsed_state["open"]:
        raise AssertionError(f"auto schedule block must be collapsed on page load, got {collapsed_state}")
    if "Автообновления" not in collapsed_state["summaryText"] or not collapsed_state["disclosureVisible"]:
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
    if "Asia/Yekaterinburg" not in payload["meta"]:
        raise AssertionError(f"auto schedule block must expose business timezone, got {payload}")
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
            load_status_text: String((loadStatusNode && loadStatusNode.textContent) || '').trim(),
            updated: trimPrefix(updatedNode ? updatedNode.textContent : '', 'Обновлено:'),
            updated_at: updatedNode ? String(updatedNode.getAttribute('data-table-summary-updated-at') || '').trim() : '',
            freshness: trimPrefix(freshnessNode ? freshnessNode.textContent : '', 'Свежесть данных:'),
            freshness_at: freshnessNode ? String(freshnessNode.getAttribute('data-table-summary-freshness-at') || '').trim() : '',
            freshness_source: freshnessNode ? String(freshnessNode.getAttribute('data-table-summary-freshness-source') || '').trim() : '',
            status: trimPrefix(loadStatusNode ? loadStatusNode.textContent : '', 'Последняя загрузка:'),
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
    if "Обновлено:" not in text or "Свежесть данных:" not in text or "Статус:" in text:
        raise AssertionError(f"table header summary must include only updated/freshness labels, got {payload}")
    if not load_status_text.startswith("Последняя загрузка: "):
        raise AssertionError(f"load status must describe the latest load only with compact wording, got {payload}")
    if "Статус последней загрузки" in load_status_text or "today_current" in load_status_text or "yesterday_closed" in load_status_text:
        raise AssertionError(f"load status must not expose technical/latest-window wording, got {payload}")
    if text.count("Asia/Yekaterinburg") > 1:
        raise AssertionError(f"table header summary must show timezone once at most, got {text!r}")
    cards = {
        "page_refresh": {
            "label": "Обновлено",
            "value": str(payload.get("updated") or ""),
            "detail": "",
            "updated_at": str(payload.get("updated_at") or ""),
        },
        "data_freshness": {
            "label": "Свежесть данных",
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
        "data_freshness": "Свежесть данных",
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
        "row_order": 52,
        "scope_label": 145,
        "metric_key": 160,
        "metric_label": 156,
        "section": 98,
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
        "section": int(widths["section"]),
        "date": int(next(widths[key] for key in widths if key.startswith("date:"))),
    }


def _check_sticky_section_offsets(page: object) -> dict[str, object]:
    payload = page.evaluate(
        """() => {
          const ids = ['row_order', 'scope_label', 'metric_label', 'section'];
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
          const sectionCell = firstRow ? firstRow.querySelector('td[data-col-id="section"]') : null;
          const sectionCellStyle = sectionCell ? getComputedStyle(sectionCell) : null;
          const dateHeader = document.querySelector('[data-table-head] th[data-col-id^="date:"]');
          const dateHeaderStyle = dateHeader ? getComputedStyle(dateHeader) : null;
          return {
            headers,
            sectionCell: {
              exists: !!sectionCell,
              position: sectionCellStyle ? sectionCellStyle.position : '',
              left: sectionCellStyle ? Math.round(parseFloat(sectionCellStyle.left || '0')) : -1,
              zIndex: sectionCellStyle ? Number(sectionCellStyle.zIndex || 0) : 0,
              background: sectionCellStyle ? sectionCellStyle.backgroundColor : ''
            },
            dateHeaderZIndex: dateHeaderStyle ? Number(dateHeaderStyle.zIndex || 0) : 0
          };
        }"""
    )
    headers = payload["headers"]
    for column_id in ("scope_label", "metric_label", "section"):
        header = headers[column_id]
        if not header["exists"] or header["position"] != "sticky":
            raise AssertionError(f"{column_id} header must be sticky, got {payload}")
    if not payload["sectionCell"]["exists"] or payload["sectionCell"]["position"] != "sticky":
        raise AssertionError(f"section body cells must be sticky, got {payload}")
    if not (int(headers["scope_label"]["left"]) < int(headers["metric_label"]["left"]) < int(headers["section"]["left"])):
        raise AssertionError(f"sticky left offsets must increase through object/metric/section, got {payload}")
    if int(headers["section"]["zIndex"]) <= int(payload["dateHeaderZIndex"]):
        raise AssertionError(f"section sticky header must render above date headers, got {payload}")
    if payload["sectionCell"]["background"] == "rgba(0, 0, 0, 0)":
        raise AssertionError(f"section sticky cell must have opaque background, got {payload}")
    return payload


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
        raise AssertionError(f"static group labels smoke could not find table groups, got {payload}")
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
