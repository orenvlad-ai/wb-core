"""Focused browser regression for multiline Web Vitrina SKU separators."""

from __future__ import annotations

from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
    _check_sku_separators,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


VIEWPORTS = (
    {"name": "wide", "width": 1440, "height": 900},
    {"name": "desktop", "width": 1024, "height": 768},
    {"name": "narrow", "width": 560, "height": 820},
)
TABLE_FONT_SIZES = (9, 10.5, 13, 16)


def _collect_layout_sample(
    page: object,
    *,
    viewport: dict[str, int | str],
    font_size: float,
) -> dict[str, object]:
    page.set_viewport_size(
        {"width": int(viewport["width"]), "height": int(viewport["height"])}
    )
    sample = page.evaluate(
        """({fontSize}) => {
          const root = document.documentElement;
          root.style.setProperty('--table-font-size', fontSize + 'px');
          const scroll = document.querySelector('[data-table-scroll]');
          const metricHeader = document.querySelector('[data-table-head] th[data-col-id="metric_label"]');
          const row = document.querySelector('.sku-separator-row');
          const label = row ? row.querySelector('.sku-separator-label') : null;
          const nextRow = row ? row.nextElementSibling : null;
          if (!scroll || !metricHeader || !row || !label || !nextRow) {
            return {ok: false, reason: 'missing separator layout nodes'};
          }

          const originalText = label.textContent || '';
          const originalTitle = label.getAttribute('title') || '';
          const originalScrollLeft = scroll.scrollLeft;
          const measure = (text) => {
            label.textContent = text;
            const styles = getComputedStyle(label);
            const rect = label.getBoundingClientRect();
            const rowRect = row.getBoundingClientRect();
            const nextRect = nextRow.getBoundingClientRect();
            const lineHeight = parseFloat(styles.lineHeight) || 0;
            const paddingTop = parseFloat(styles.paddingTop) || 0;
            const paddingBottom = parseFloat(styles.paddingBottom) || 0;
            const range = document.createRange();
            range.selectNodeContents(label);
            const fragments = Array.from(range.getClientRects());
            const textInside = fragments.every((fragment) => (
              fragment.left >= rect.left - 1 &&
              fragment.right <= rect.right + 1 &&
              fragment.top >= rect.top - 1 &&
              fragment.bottom <= rect.bottom + 1
            ));
            return {
              text,
              rowHeight: rowRect.height,
              rowBottom: rowRect.bottom,
              nextTop: nextRect.top,
              labelHeight: rect.height,
              labelWidth: rect.width,
              labelLeft: rect.left,
              labelRight: rect.right,
              clientHeight: label.clientHeight,
              scrollHeight: label.scrollHeight,
              clientWidth: label.clientWidth,
              scrollWidth: label.scrollWidth,
              fontSize: parseFloat(styles.fontSize) || 0,
              lineHeight,
              paddingTop,
              paddingRight: parseFloat(styles.paddingRight) || 0,
              paddingBottom,
              paddingLeft: parseFloat(styles.paddingLeft) || 0,
              lineCount: fragments.length,
              textInside,
              overflow: styles.overflow,
              overflowWrap: styles.overflowWrap,
              textOverflow: styles.textOverflow,
              whiteSpace: styles.whiteSpace,
              wordBreak: styles.wordBreak,
              position: styles.position
            };
          };

          const actual = measure(originalText);
          const short = measure('SKU 1');
          const latin = measure('clean iPhone 14 Pro Max Extra Long Marketplace Product Name');
          const russian = measure('Чехол премиальный сверхпрочный для телефона большой серии');
          const token = measure('SKU-' + 'SUPERCALIFRAGILISTICEXPIALIDOCIOUS'.repeat(4));

          label.textContent = latin.text;
          scroll.scrollLeft = 0;
          scroll.dispatchEvent(new Event('scroll'));
          const stickyBefore = label.getBoundingClientRect();
          const maxScrollLeft = Math.max(0, scroll.scrollWidth - scroll.clientWidth);
          scroll.scrollLeft = maxScrollLeft;
          scroll.dispatchEvent(new Event('scroll'));
          const stickyAfter = label.getBoundingClientRect();
          const metricAfter = metricHeader.getBoundingClientRect();

          label.textContent = originalText;
          label.setAttribute('title', originalTitle);
          scroll.scrollLeft = originalScrollLeft;
          scroll.dispatchEvent(new Event('scroll'));
          return {
            ok: true,
            actual,
            short,
            longCases: [latin, russian, token],
            originalText,
            title: label.getAttribute('title') || '',
            tagName: label.tagName,
            clickId: label.getAttribute('data-open-vitrina-sku') || '',
            maxScrollLeft,
            stickyBeforeLeft: stickyBefore.left,
            stickyAfterLeft: stickyAfter.left,
            stickyAfterRight: stickyAfter.right,
            metricAfterLeft: metricAfter.left,
            metricAfterRight: metricAfter.right,
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: window.innerWidth
          };
        }""",
        {"fontSize": font_size},
    )
    if not sample.get("ok"):
        raise AssertionError(
            f"SKU separator sample is unavailable for {viewport['name']}/{font_size}px: {sample}"
        )
    sample["viewport"] = str(viewport["name"])
    sample["tableFontSize"] = font_size
    return sample


def _assert_layout_sample(sample: dict[str, object]) -> None:
    actual = sample["actual"]
    short = sample["short"]
    long_cases = sample["longCases"]
    assert isinstance(actual, dict)
    assert isinstance(short, dict)
    assert isinstance(long_cases, list)
    prefix = f"{sample['viewport']}/{sample['tableFontSize']}px"
    expected_font_size = 2 * float(sample["tableFontSize"])

    for name, state in (("actual", actual), ("short", short)):
        if (
            abs(float(state["fontSize"]) - expected_font_size) > 0.01
            or float(state["paddingTop"]) <= 0
            or float(state["paddingRight"]) <= 0
            or float(state["paddingBottom"]) <= 0
            or float(state["paddingLeft"]) <= 0
            or state["position"] != "sticky"
            or state["overflow"] != "visible"
            or state["overflowWrap"] != "anywhere"
            or state["textOverflow"] != "clip"
            or state["whiteSpace"] != "normal"
            or state["wordBreak"] != "normal"
            or float(state["scrollWidth"]) > float(state["clientWidth"]) + 1
            or float(state["scrollHeight"]) > float(state["clientHeight"]) + 1
            or not state["textInside"]
            or float(state["rowBottom"]) > float(state["nextTop"]) + 1
        ):
            raise AssertionError(f"{prefix} {name} SKU separator is clipped or unstable: {state}")

    if (
        int(short["lineCount"]) != 1
        or float(short["rowHeight"]) > float(short["lineHeight"]) * 1.7
        or float(short["labelHeight"]) > float(short["lineHeight"]) * 1.7
    ):
        raise AssertionError(f"{prefix} short SKU separator must stay one compact line: {short}")

    if (
        sample["viewport"] in {"wide", "desktop"}
        and float(sample["tableFontSize"]) == 10.5
        and (
            int(actual["lineCount"]) < 2
            or float(actual["rowHeight"]) <= float(short["rowHeight"]) + 1
        )
    ):
        raise AssertionError(
            f"{prefix} fixture SKU name must visibly wrap and grow at the default table font: {actual}"
        )

    for state in long_cases:
        if (
            int(state["lineCount"]) < 2
            or float(state["rowHeight"]) <= float(short["rowHeight"]) + 1
            or float(state["labelHeight"]) <= float(short["labelHeight"]) + 1
            or float(state["scrollWidth"]) > float(state["clientWidth"]) + 1
            or float(state["scrollHeight"]) > float(state["clientHeight"]) + 1
            or not state["textInside"]
            or float(state["rowBottom"]) > float(state["nextTop"]) + 1
            or state["overflow"] != "visible"
            or state["overflowWrap"] != "anywhere"
            or state["textOverflow"] != "clip"
            or state["whiteSpace"] != "normal"
            or state["wordBreak"] != "normal"
        ):
            raise AssertionError(f"{prefix} long SKU separator must fully wrap and grow: {state}")

    if (
        sample["originalText"] != "clean iPhone 14"
        or sample["tagName"] != "BUTTON"
        or not str(sample["title"]).startswith("Открыть управление SKU:")
        or not str(sample["clickId"]).isdigit()
        or int(float(sample["maxScrollLeft"])) <= 0
        or abs(float(sample["stickyBeforeLeft"]) - float(sample["stickyAfterLeft"])) > 2
        or float(sample["stickyAfterLeft"]) < float(sample["metricAfterLeft"]) - 1
        or float(sample["stickyAfterRight"]) > float(sample["metricAfterRight"]) + 2
        or float(sample["documentWidth"]) > float(sample["viewportWidth"]) + 1
    ):
        raise AssertionError(
            f"{prefix} SKU separator must preserve title/button/sticky semantics without document overflow: {sample}"
        )


def main() -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_responses: list[str] = []
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                color_scheme="dark",
            )
            page = context.new_page()
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: failed_responses.append(
                    f"{response.status} {response.url}"
                )
                if response.status >= 500
                else None,
            )
            response = page.goto(
                base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                wait_until="domcontentloaded",
            )
            if response is None or response.status >= 500:
                raise AssertionError(f"Web Vitrina route failed: {response}")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            result = _check_sku_separators(page)
            layout_samples = [
                _collect_layout_sample(page, viewport=viewport, font_size=font_size)
                for viewport in VIEWPORTS
                for font_size in TABLE_FONT_SIZES
            ]
            for sample in layout_samples:
                _assert_layout_sample(sample)
            page.set_viewport_size({"width": 1440, "height": 900})
            page.evaluate("() => document.documentElement.style.removeProperty('--table-font-size')")
            separator_button = page.locator(".sku-separator-label[data-open-vitrina-sku]").first
            separator_button.click()
            page.wait_for_selector("[data-sku-management-modal]:not([hidden])", timeout=5000)
            click_state = page.evaluate(
                """() => {
                  const modal = document.querySelector('[data-sku-management-modal]');
                  return {
                    modalVisible: !!modal && !modal.hidden,
                    modalText: modal ? (modal.textContent || '') : ''
                  };
                }"""
            )
            if not click_state["modalVisible"] or "clean iPhone 14" not in click_state["modalText"]:
                raise AssertionError(f"SKU separator click must keep opening its SKU modal, got {click_state}")
            page.keyboard.press("Escape")
            page.wait_for_selector("[data-sku-management-modal]", state="hidden", timeout=5000)
            if console_errors or page_errors or failed_responses:
                raise AssertionError(
                    "separator page must render without fatal browser errors: "
                    f"console={console_errors}, page={page_errors}, responses={failed_responses}"
                )
            context.close()
            browser.close()
    print(
        {
            "status": "ok",
            "separator": result,
            "layout_samples": [
                {
                    "viewport": sample["viewport"],
                    "table_font_size": sample["tableFontSize"],
                    "actual_lines": sample["actual"]["lineCount"],
                    "short_lines": sample["short"]["lineCount"],
                    "long_lines": [case["lineCount"] for case in sample["longCases"]],
                }
                for sample in layout_samples
            ],
            "click": {"modal_visible": True, "sku": "clean iPhone 14"},
        }
    )


if __name__ == "__main__":
    main()
