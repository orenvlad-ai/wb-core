"""Targeted smoke-check for the reports tab accordion contract on the operator page."""

from __future__ import annotations

import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_DAILY_REPORT_PATH,
    DEFAULT_SHEET_STOCK_REPORT_PATH,
    _render_sheet_vitrina_operator_ui,
)


def main() -> None:
    html = _render_sheet_vitrina_operator_ui(
        refresh_path="/v1/sheet-vitrina-v1/refresh",
        load_path="/v1/sheet-vitrina-v1/load",
        status_path="/v1/sheet-vitrina-v1/status",
        job_path="/v1/sheet-vitrina-v1/job",
        daily_report_path=DEFAULT_SHEET_DAILY_REPORT_PATH,
        stock_report_path=DEFAULT_SHEET_STOCK_REPORT_PATH,
    )

    for token in (
        "Обновление данных витрины",
        "Расчёт поставок",
        "Отчёты",
        "Ежедневные отчёты",
        "Отчёт по остаткам",
    ):
        if token not in html:
            raise AssertionError(f"reports tab chrome must keep token {token!r}")

    for token in (
        'id="dailyReportToggle"',
        'id="stockReportToggle"',
        'id="dailyReportPanelBody" class="report-accordion-body" hidden',
        'id="stockReportPanelBody" class="report-accordion-body" hidden',
        DEFAULT_SHEET_DAILY_REPORT_PATH,
        DEFAULT_SHEET_STOCK_REPORT_PATH,
    ):
        if token not in html:
            raise AssertionError(f"reports accordion contract must include token {token!r}")

    false_count = len(re.findall(r'aria-expanded="false"', html))
    if false_count < 2:
        raise AssertionError(f"both reports accordions must default to collapsed state, got only {false_count} false toggles")

    print("reports_ui_sections: ok -> Обновление данных витрины / Расчёт поставок / Отчёты")
    print("reports_ui_accordions: ok -> dailyReportToggle / stockReportToggle")
    print("reports_ui_default_collapsed: ok ->", false_count)


if __name__ == "__main__":
    main()
