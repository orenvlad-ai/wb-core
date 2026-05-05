"""Local smoke checks for Seller Portal feedback filter DOM scout helpers."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright  # noqa: E402

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    activate_seller_portal_rating_filter_section,
    click_filter_apply_button,
    inspect_seller_portal_rating_filter_popup,
    open_seller_portal_filters_popup,
    select_seller_portal_rating_filter_stars,
)
from apps.seller_portal_feedbacks_filter_dom_scout import (  # noqa: E402
    CONTRACT_NAME,
    empty_filter_dom_scout,
    render_markdown_report,
    write_report_artifacts,
)


def main() -> None:
    _assert_custom_star_popup_helpers()
    _assert_report_shape()
    print("seller_portal_feedbacks_filter_dom_scout_smoke: OK")


def _assert_custom_star_popup_helpers() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(
                """
                <button id="filtersButton">Фильтры</button>
                <div id="filterPopup" role="dialog" style="display:none; width:420px; padding:16px">
                  <aside>
                    <button id="typeSection">Тип отзыва</button>
                    <button id="ratingSection">Оценка отзыва</button>
                  </aside>
                  <section id="ratingPanel">
                    <div class="star-row"><span role="checkbox" aria-checked="false" class="wb-checkbox" style="display:inline-block;width:16px;height:16px"></span><span>5★</span></div>
                    <div class="star-row"><span role="checkbox" aria-checked="false" class="wb-checkbox" style="display:inline-block;width:16px;height:16px"></span><span>4★</span></div>
                    <div class="star-row"><span role="checkbox" aria-checked="false" class="wb-checkbox" style="display:inline-block;width:16px;height:16px"></span><span>3★</span></div>
                    <div class="star-row"><span role="checkbox" aria-checked="false" class="wb-checkbox" style="display:inline-block;width:16px;height:16px"></span><span>2★</span></div>
                    <div class="star-row"><span role="checkbox" aria-checked="false" class="wb-checkbox" style="display:inline-block;width:16px;height:16px"></span><span>1★</span></div>
                  </section>
                  <button id="applyButton">Применить</button>
                  <button>Сбросить</button>
                </div>
                <script>
                  document.querySelector('#filtersButton').addEventListener('click', () => {
                    document.querySelector('#filterPopup').style.display = 'block';
                  });
                  document.querySelector('#filterPopup').addEventListener('click', (event) => {
                    const row = event.target.closest('.star-row');
                    if (!row) return;
                    const checkbox = row.querySelector('[role="checkbox"]');
                    checkbox.setAttribute('aria-checked', checkbox.getAttribute('aria-checked') === 'true' ? 'false' : 'true');
                  });
                  document.querySelector('#applyButton').addEventListener('click', () => {
                    document.body.setAttribute('data-applied', '1');
                  });
                </script>
                """
            )
            opened = open_seller_portal_filters_popup(page)
            if not opened.get("ok"):
                raise AssertionError(f"filters popup must open: {opened}")
            section = activate_seller_portal_rating_filter_section(page)
            if not section.get("ok"):
                raise AssertionError(f"rating section must activate: {section}")
            before = inspect_seller_portal_rating_filter_popup(page)
            if not before.get("popup_opened") or not before.get("stable_selector_found"):
                raise AssertionError(f"popup summary must see stable selectors: {before}")
            one_star = next((row for row in before.get("rows") or [] if row.get("star") == 1), None)
            if not one_star:
                raise AssertionError(f"1-star row must be mapped: {before}")
            selected = select_seller_portal_rating_filter_stars(page, stars=[1])
            if not selected.get("ok") or selected.get("selected_star_values_after") != [1]:
                raise AssertionError(f"1-star selection must change checked state: {selected}")
            after = inspect_seller_portal_rating_filter_popup(page)
            if after.get("selected_star_values") != [1]:
                raise AssertionError(f"popup reread must show selected 1-star: {after}")
            applied = click_filter_apply_button(page, context_hint="filters")
            if not applied.get("ok") or page.locator("body").get_attribute("data-applied") != "1":
                raise AssertionError(f"apply button must be parsed and clicked: {applied}")
        finally:
            browser.close()


def _assert_report_shape() -> None:
    report = {
        "contract_name": CONTRACT_NAME,
        "mode": "read-only",
        "started_at": "2026-05-05T00:00:00Z",
        "finished_at": "2026-05-05T00:00:01Z",
        "read_only_guards": {
            "complaint_submit_clicked": False,
            "complaint_final_submit_allowed": False,
            "journal_write_allowed": False,
            "submit_clicked_count": 0,
        },
        "filter_dom_scout": {
            **empty_filter_dom_scout(),
            "popup_opened": True,
            "rating_section_opened": True,
            "stable_selector_found": True,
            "screenshot_path": "/tmp/filter.png",
            "one_star_checkbox_selector_summary": "row mapped to star=1 under Оценка отзыва",
            "apply_button_selector_summary": "visible button/role/text Применить",
            "dom_summary": {
                "popup_root": {"tag": "div", "role": "dialog"},
                "rows": [{"star": 1, "checked": True, "text": "1★", "control_class": "wb-checkbox"}],
                "buttons": [{"text": "Применить", "tag": "button", "role": ""}],
            },
        },
        "errors": [],
    }
    markdown = render_markdown_report(report)
    if "Seller Portal Feedback Filter DOM Scout" not in markdown or "1-star checkbox selector" not in markdown:
        raise AssertionError(f"markdown shape mismatch: {markdown}")
    with TemporaryDirectory(prefix="filter-dom-scout-smoke-") as tmp:
        paths = write_report_artifacts(dict(report), Path(tmp))
        if not paths["json"].exists() or not paths["markdown"].exists():
            raise AssertionError(f"report artifacts missing: {paths}")


if __name__ == "__main__":
    main()
