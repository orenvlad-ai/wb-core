"""Targeted smoke-check for the reports tab subsection contract on the operator page."""

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
        "Обновление данных",
        "Расчёт поставок",
        "Отчёты",
        "Ежедневные отчёты",
        "Отчёт по остаткам",
    ):
        if token not in html:
            raise AssertionError(f"reports tab chrome must keep token {token!r}")

    for token in (
        'data-report-section-button="daily"',
        'data-report-section-button="stock"',
        'data-report-section-panel="daily"',
        'data-report-section-panel="stock" hidden',
        'id="dailyReportPeriod"',
        'id="stockReportPeriod"',
        DEFAULT_SHEET_DAILY_REPORT_PATH,
        DEFAULT_SHEET_STOCK_REPORT_PATH,
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

    print("reports_ui_sections: ok -> Обновление данных / Расчёт поставок / Отчёты")
    print("reports_ui_subsections: ok -> daily / stock")
    print("reports_ui_heading_dedup: ok -> no panel-body h1 duplicates")


if __name__ == "__main__":
    main()
