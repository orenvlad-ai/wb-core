"""Focused browser regression for stable Web Vitrina filter-header geometry."""

from __future__ import annotations

from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


VIEWPORTS = (
    (2640, 900, "screenshot-wide", "static"),
    (1440, 900, "desktop", "static"),
    (1024, 768, "compact-desktop", "absolute"),
    (760, 900, "documented-narrow-760", "absolute"),
    (560, 900, "documented-narrow-560", "absolute"),
)


def _layout_state(page: object) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const query = (selector) => document.querySelector(selector);
          const rect = (node) => {
            const value = node ? node.getBoundingClientRect() : null;
            return value ? {
              left: value.left,
              top: value.top,
              right: value.right,
              bottom: value.bottom,
              width: value.width,
              height: value.height
            } : null;
          };
          const visible = (node) => {
            if (!node) return false;
            const value = rect(node);
            const styles = getComputedStyle(node);
            return value.width > 2 && value.height > 2 &&
              styles.display !== 'none' && styles.visibility !== 'hidden';
          };
          const overlap = (left, right) => !!left && !!right &&
            left.left < right.right && left.right > right.left &&
            left.top < right.bottom && left.bottom > right.top;
          const header = query('[data-table-header]');
          const panel = header ? header.closest('.panel') : null;
          const table = query('[data-table-shell]');
          const row = query('.table-heading-row');
          const slot = query('[data-filters-slot]');
          const rail = query('[data-filters-rail]');
          const right = query('[data-table-heading-right]');
          const loadCluster = query('.table-load-action-cluster');
          const toggle = query('[data-filters-toggle]');
          const section = query('[data-filter-control="section"]');
          const group = query('[data-filter-control="group"]');
          const skuPicker = query('[data-sku-metric-picker]');
          const columnManager = query('[data-column-manager]');
          const toggleStyles = toggle ? getComputedStyle(toggle) : null;
          const railStyles = rail ? getComputedStyle(rail) : null;
          const railRect = rail && !rail.hidden ? rect(rail) : null;
          const rightRect = rect(right);
          const loadRect = rect(loadCluster);
          return {
            header: rect(header),
            panel: rect(panel),
            table: rect(table),
            row: rect(row),
            slot: rect(slot),
            rail: railRect,
            right: rightRect,
            loadCluster: loadRect,
            railHidden: rail ? rail.hidden : null,
            railPosition: railStyles ? railStyles.position : '',
            railInsideSlot: !!(slot && rail && slot.contains(rail)),
            railOverlapsRight: overlap(railRect, rightRect),
            railOverlapsLoad: overlap(railRect, loadRect),
            toggleVisible: visible(toggle),
            sectionVisible: visible(section),
            groupVisible: visible(group),
            skuPickerVisible: visible(skuPicker),
            columnManagerVisible: visible(columnManager),
            sectionLabel: section ? (section.getAttribute('aria-label') || '') : '',
            groupLabel: group ? (group.getAttribute('aria-label') || '') : '',
            toggleExpanded: toggle ? (toggle.getAttribute('aria-expanded') || '') : '',
            toggleControls: toggle ? (toggle.getAttribute('aria-controls') || '') : '',
            toggleTransitionDuration: toggleStyles ? toggleStyles.transitionDuration : '',
            toggleAnimationName: toggleStyles ? toggleStyles.animationName : '',
            railTransitionDuration: railStyles ? railStyles.transitionDuration : '',
            railAnimationName: railStyles ? railStyles.animationName : '',
            documentScrollWidth: document.documentElement.scrollWidth,
            documentClientWidth: document.documentElement.clientWidth
          };
        }"""
    )


def _same_geometry(before: dict[str, object], opened: dict[str, object]) -> bool:
    for node_name, keys in (
        ("header", ("top", "height")),
        ("panel", ("top", "height")),
        ("table", ("top",)),
    ):
        left = before[node_name]
        right = opened[node_name]
        if not left or not right:
            return False
        for key in keys:
            if abs(float(left[key]) - float(right[key])) > 0.25:
                return False
    return True


def _assert_closed(state: dict[str, object], label: str) -> None:
    if (
        state["railHidden"] is not True
        or state["toggleExpanded"] != "false"
        or state["toggleControls"] != "table-filters-rail"
        or not state["toggleVisible"]
        or state["sectionVisible"]
        or state["groupVisible"]
        or state["documentScrollWidth"] > state["documentClientWidth"] + 1
    ):
        raise AssertionError(f"closed filter state mismatch at {label}: {state}")


def _assert_opened(
    before: dict[str, object],
    opened: dict[str, object],
    label: str,
    expected_position: str,
) -> None:
    if (
        opened["railHidden"]
        or opened["toggleExpanded"] != "true"
        or not opened["railInsideSlot"]
        or opened["railPosition"] != expected_position
        or not opened["sectionVisible"]
        or not opened["groupVisible"]
        or not opened["skuPickerVisible"]
        or not opened["columnManagerVisible"]
        or opened["sectionLabel"] != "Секции"
        or opened["groupLabel"] != "Группа"
        or opened["railOverlapsRight"]
        or opened["railOverlapsLoad"]
        or not _same_geometry(before, opened)
        or opened["toggleTransitionDuration"] != "0s"
        or opened["toggleAnimationName"] != "none"
        or opened["railTransitionDuration"] != "0s"
        or opened["railAnimationName"] != "none"
        or opened["documentScrollWidth"] > opened["documentClientWidth"] + 1
    ):
        raise AssertionError(f"opened filter geometry mismatch at {label}: {opened}")


def _exercise_popup_semantics(page: object, label: str) -> None:
    initial_row_count = page.locator("[data-table-body] tr[data-row-kind]").count()
    for selector in (
        "[data-filter-control='section']",
        "[data-filter-control='group']",
    ):
        control = page.locator(selector)
        option_values = control.locator("option").evaluate_all(
            "nodes => nodes.map(node => node.value).filter(value => value && value !== '__all__')"
        )
        if not option_values:
            raise AssertionError(f"missing selectable {selector} option at {label}")
        control.select_option(str(option_values[0]))
        if control.input_value() != option_values[0]:
            raise AssertionError(f"{selector} selection did not apply at {label}")
        if page.locator("[data-filters-rail]").is_hidden():
            raise AssertionError(f"{selector} selection unexpectedly closed filters at {label}")
        control.select_option("__all__")
    if page.locator("[data-table-body] tr[data-row-kind]").count() != initial_row_count:
        raise AssertionError(f"section/group reset did not restore rows at {label}")

    page.locator("[data-sku-metric-toggle]").click()
    if (
        page.locator("[data-sku-metric-toggle]").get_attribute("aria-expanded") != "true"
        or page.locator("[data-sku-metric-panel]").is_hidden()
    ):
        raise AssertionError(f"SKU metric picker did not open at {label}")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.querySelector('[data-filters-rail]').hidden",
        timeout=5000,
    )

    toggle = page.locator("[data-filters-toggle]")
    toggle.focus()
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => !document.querySelector('[data-filters-rail]').hidden",
        timeout=5000,
    )
    page.locator("[data-column-manager] > summary").click()
    if not page.locator("[data-column-manager]").get_attribute("open") == "":
        raise AssertionError(f"bounded column manager did not open at {label}")
    section_checkbox = page.locator(
        "[data-column-visibility-id='section']"
    )
    if section_checkbox.count() != 1 or not section_checkbox.is_checked():
        raise AssertionError(f"section column control missing at {label}")
    section_checkbox.click()
    if page.locator("[data-table-head] th[data-col-id='section']").count() != 0:
        raise AssertionError(f"section column did not hide at {label}")
    section_checkbox.click()
    if page.locator("[data-table-head] th[data-col-id='section']").count() != 1:
        raise AssertionError(f"section column did not restore at {label}")
    page.locator(".shell-header").click(position={"x": 4, "y": 4})
    page.wait_for_function(
        "() => document.querySelector('[data-filters-rail]').hidden",
        timeout=5000,
    )
    if page.locator("[data-column-manager]").get_attribute("open") is not None:
        raise AssertionError(f"outside click did not close column manager at {label}")


def main() -> None:
    results: list[dict[str, object]] = []
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for width, height, label, expected_position in VIEWPORTS:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    color_scheme="dark",
                )
                page = context.new_page()
                response = page.goto(
                    base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                    wait_until="domcontentloaded",
                )
                if response is None or response.status >= 500:
                    raise AssertionError(f"Web Vitrina route failed at {label}: {response}")
                page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
                before = _layout_state(page)
                _assert_closed(before, label)
                page.locator("[data-filters-toggle]").click()
                opened = _layout_state(page)
                _assert_opened(before, opened, label, expected_position)
                _exercise_popup_semantics(page, label)
                closed_again = _layout_state(page)
                _assert_closed(closed_again, label + " after outside click")
                results.append(
                    {
                        "viewport": [width, height],
                        "label": label,
                        "rail_position": expected_position,
                        "header_height": opened["header"]["height"],
                        "table_top": opened["table"]["top"],
                        "panel_height": opened["panel"]["height"],
                    }
                )
                context.close()
            browser.close()
    print({"status": "ok", "viewports": results})


if __name__ == "__main__":
    main()
