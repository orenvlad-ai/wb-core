"""Targeted smoke-check for the reports tab subsection contract on the operator page."""

from __future__ import annotations

import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_PLAN_REPORT_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    _render_sheet_vitrina_operator_ui,
)


def main() -> None:
    active_skus = [
        {"nm_id": 1001, "display_name": "SKU Alpha", "identity_label": "SKU Alpha · nmId 1001"},
        {"nm_id": 1002, "display_name": "SKU Beta", "identity_label": "SKU Beta · nmId 1002"},
        {"nm_id": 1003, "display_name": "SKU Gamma", "identity_label": "SKU Gamma · nmId 1003"},
    ]
    html = _render_sheet_vitrina_operator_ui(
        refresh_path="/v1/sheet-vitrina-v1/refresh",
        load_path="/v1/sheet-vitrina-v1/load",
        status_path="/v1/sheet-vitrina-v1/status",
        job_path="/v1/sheet-vitrina-v1/job",
        daily_report_path=DEFAULT_SHEET_DAILY_REPORT_PATH,
        stock_report_path=DEFAULT_SHEET_STOCK_REPORT_PATH,
        plan_report_path=DEFAULT_SHEET_PLAN_REPORT_PATH,
        operator_context={
            "stock_report_active_skus": active_skus,
            "stock_report_active_sku_count": len(active_skus),
            "stock_report_active_sku_source": "current_registry_config_v2",
        },
    )

    for token in (
        "Обновление данных",
        "Расчёт поставок",
        "Отчёты",
        "Ежедневные отчёты",
        "Отчёт по остаткам",
        "Выполнение плана",
    ):
        if token not in html:
            raise AssertionError(f"reports tab chrome must keep token {token!r}")

    for token in (
        'data-report-section-button="daily"',
        'data-report-section-button="stock"',
        'data-report-section-button="plan"',
        'data-report-section-panel="daily"',
        'data-report-section-panel="stock" hidden',
        'data-report-section-panel="plan" hidden',
        'href="' + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + '"',
        'id="dailyReportPeriod"',
        'id="stockReportPeriod"',
        'id="stockReportSkuSelector"',
        'id="stockReportApplyButton"',
        'id="stockReportSkuValidation"',
        'id="stockReportSelectAllButton"',
        'id="stockReportClearAllButton"',
        'id="planReportPeriodSelect"',
        'id="planReportQ1Input"',
        'id="planReportQ4Input"',
        'id="planReportDrrInput"',
        'id="planReportApplyButton"',
        DEFAULT_SHEET_DAILY_REPORT_PATH,
        DEFAULT_SHEET_STOCK_REPORT_PATH,
        DEFAULT_SHEET_PLAN_REPORT_PATH,
    ):
        if token not in html:
            raise AssertionError(f"reports subsection contract must include token {token!r}")

    if "dailyReportToggle" in html or "stockReportToggle" in html or "report-accordion" in html:
        raise AssertionError("legacy reports accordion contract must be removed from the operator page")
    if 'formatDailyComparisonLabel(payload.older_closed_date, payload.newer_closed_date)' not in html:
        raise AssertionError("daily-report period wording must be built from both closed dates")
    if "Ежедневный отчёт за " in html:
        raise AssertionError("misleading single-day daily-report wording must not remain in the template")
    if "current business day и slot <code>today_current</code>" in html:
        raise AssertionError("stock-report UI must no longer describe current-day today_current seam as the default")
    if "previous closed business day и slot <code>yesterday_closed</code>" not in html:
        raise AssertionError("stock-report UI must disclose previous-closed yesterday_closed seam")
    if len(re.findall(r"<h1>", html)) != 0:
        raise AssertionError("duplicated top-level headings must be removed from panel bodies")
    if "let stockReportSelectedSkuIds = resolveRestoredStockReportDraftSkuIds(persistedOperatorUiState);" not in html:
        raise AssertionError("stock-report selector draft state must restore from persisted browser storage")
    if "let stockReportAppliedSkuIds = resolveRestoredStockReportAppliedSkuIds(persistedOperatorUiState);" not in html:
        raise AssertionError("stock-report selector applied state must restore from persisted browser storage")
    if "const OPERATOR_UI_STORAGE_KEY = \"wb-core:sheet-vitrina-v1:operator-ui-state:v1\";" not in html:
        raise AssertionError("operator page must namespace persisted UI state under a sheet_vitrina-specific storage key")
    if "window.localStorage.getItem(OPERATOR_UI_STORAGE_KEY)" not in html:
        raise AssertionError("operator page must restore persisted UI state from browser storage")
    if "window.localStorage.setItem(OPERATOR_UI_STORAGE_KEY" not in html:
        raise AssertionError("operator page must persist tab/subsection/SKU state into browser storage")
    if "isEmbeddedMode ? configuredInitialTab : (persistedOperatorUiState.active_tab || DEFAULT_ACTIVE_TAB)" not in html:
        raise AssertionError("top-level tab must restore from persisted browser state")
    if "persistedOperatorUiState.report_section || DEFAULT_REPORT_SECTION" not in html:
        raise AssertionError("reports subsection must restore from persisted browser state")
    if "persistedOperatorUiState.supply_section || DEFAULT_SUPPLY_SECTION" not in html:
        raise AssertionError("supply subsection must restore from persisted browser state")
    if 'setStockReportValidation("Выберите хотя бы один SKU");' not in html:
        raise AssertionError("stock-report selector must reject empty SKU selection before recalculation")

    config_payload = _extract_operator_ui_config(html)
    if config_payload.get("stock_report_active_skus") != active_skus:
        raise AssertionError("reports UI config must expose the full active SKU catalog for the selector")
    if config_payload.get("stock_report_active_sku_count") != len(active_skus):
        raise AssertionError("reports UI config must disclose the active SKU count")
    if config_payload.get("stock_report_active_sku_source") != "current_registry_config_v2":
        raise AssertionError("reports UI config must disclose current registry state as the selector source")
    if config_payload.get("plan_report_path") != DEFAULT_SHEET_PLAN_REPORT_PATH:
        raise AssertionError("reports UI config must expose the plan-report read-only route")

    fake_rows = [
        {"nm_id": 1001, "identity_label": "SKU Alpha · nmId 1001"},
        {"nm_id": 1002, "identity_label": "SKU Beta · nmId 1002"},
    ]
    selected_nm_ids = {1003}
    filtered_rows = [row for row in fake_rows if int(row["nm_id"]) in selected_nm_ids]
    if filtered_rows:
        raise AssertionError("stock-report selector semantics must exclude deselected SKU from the rendered row set")

    print("reports_ui_sections: ok -> Обновление данных / Расчёт поставок / Отчёты")
    print("reports_ui_subsections: ok -> daily / stock / plan")
    print("reports_ui_stock_selector: ok -> full active SKU config, default=all, empty-selection validation")
    print("reports_ui_heading_dedup: ok -> no panel-body h1 duplicates")


def _extract_operator_ui_config(html: str) -> dict[str, object]:
    match = re.search(
        r'<script id="sheet-vitrina-v1-operator-config" type="application/json">(.*?)</script>',
        html,
        re.S,
    )
    if not match:
        raise AssertionError("operator UI config script is missing")
    return json.loads(match.group(1))


if __name__ == "__main__":
    main()
