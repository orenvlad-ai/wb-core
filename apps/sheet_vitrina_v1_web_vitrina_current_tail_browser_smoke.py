"""Browser smoke for the visible today_current tail in web-vitrina history selector."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
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
    with LocalCurrentTailFixtureServer() as base_url:
        result = run_browser_check(base_url)
    print("web_vitrina_current_tail_base_url: ok ->", result["base_url"])
    print("web_vitrina_current_tail_button_enabled: ok ->", result["current_tail_button_enabled"])
    print("web_vitrina_current_tail_preset_range: ok ->", result["preset_range"])
    print("web_vitrina_current_tail_period_query: ok ->", result["period_query"])
    print("web_vitrina_current_tail_period_meta: ok ->", result["period_meta"])
    print("web_vitrina_current_tail_period_columns: ok ->", result["period_columns"])


class LocalCurrentTailFixtureServer:
    def __init__(self) -> None:
        self.server = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> str:
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        self.runtime_dir_obj = TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-current-tail-")
        runtime_dir = Path(self.runtime_dir_obj.name) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T15:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-18T15:01:00Z",
            plan=_build_one_day_plan(
                as_of_date="2026-04-18",
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-19T15:02:00Z",
            plan=_build_one_day_plan(
                as_of_date="2026-04-19",
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-20T15:03:00Z",
            plan=_build_default_daily_plan(
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
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


def run_browser_check(base_url: str) -> dict[str, object]:
    page_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(page_url, wait_until="commit")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            current_tail_button = page.locator('[data-history-day="2026-04-21"]')
            if current_tail_button.count() != 1:
                raise AssertionError("current visible tail date must be rendered in the history calendar")
            current_tail_button_enabled = not current_tail_button.is_disabled()
            if not current_tail_button_enabled:
                raise AssertionError("current visible tail date must stay enabled in the history calendar")

            page.locator("[data-history-toggle]").click()
            page.locator("[data-history-preset='week']").click()
            page.wait_for_function(
                "() => document.querySelector('[data-history-date-from]').value === '2026-04-18' && document.querySelector('[data-history-date-to]').value === '2026-04-21'",
                timeout=5000,
            )
            preset_range = {
                "date_from": page.locator("[data-history-date-from]").input_value(),
                "date_to": page.locator("[data-history-date-to]").input_value(),
            }
            page.locator("[data-history-save]").click()
            page.wait_for_function(
                "() => new URL(window.location.href).searchParams.get('date_from') === '2026-04-18' && new URL(window.location.href).searchParams.get('date_to') === '2026-04-21'",
                timeout=5000,
            )
            page.wait_for_function(
                """() => Array.from(
                  document.querySelectorAll("[data-table-head] [data-col-id^='date:']")
                ).some(node => node.getAttribute("data-col-id") === "date:2026-04-21")""",
                timeout=5000,
            )
            period_query = page.evaluate("() => window.location.search")
            period_meta = page.locator("[data-page-meta]").inner_text().strip()
            period_columns = page.locator("[data-table-head] [data-col-id^='date:']").evaluate_all(
                """nodes => Array.from(
                  new Set(nodes.map(node => node.getAttribute('data-col-id') || '').filter(Boolean))
                )"""
            )
            if period_columns[-1] != "date:2026-04-21":
                raise AssertionError(f"period window must expose the saved current-tail column, got {period_columns}")
            if page.locator("[data-table-body] tr").count() <= 0:
                raise AssertionError("period window must keep table rows rendered")
        finally:
            context.close()
            browser.close()

    return {
        "base_url": base_url,
        "current_tail_button_enabled": current_tail_button_enabled,
        "preset_range": preset_range,
        "period_query": period_query,
        "period_meta": period_meta,
        "period_columns": period_columns,
    }


def _build_one_day_plan(
    *,
    as_of_date: str,
    first_nm_id: int,
    second_nm_id: int,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"web-vitrina-current-tail-one-day-{as_of_date}",
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
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100 + int(as_of_date[-2:])],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1000 + int(as_of_date[-2:])],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 900 + int(as_of_date[-2:])],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1000 + int(as_of_date[-2:])],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 10 + int(as_of_date[-2:]) / 10],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 11 + int(as_of_date[-2:]) / 10],
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


def _build_default_daily_plan(
    *,
    first_nm_id: int,
    second_nm_id: int,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-current-tail-default-daily",
        as_of_date="2026-04-20",
        date_columns=["2026-04-20", "2026-04-21"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-04-20",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-04-21",
            ),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "prices_snapshot": "accepted_current_rollover",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D7",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-20", "2026-04-21"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 140, 150],
                    ["Итого: Сумма заказов", "TOTAL|total_orderSum", 1200, 1300],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", 1110, 1120],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", 1210, 1220],
                    [f"SKU A: Конверсия в корзину", f"SKU:{first_nm_id}|avg_addToCartConversion", 13.0, 13.2],
                    [f"SKU B: Конверсия в корзину", f"SKU:{second_nm_id}|avg_addToCartConversion", 12.0, 12.2],
                ],
                row_count=6,
                column_count=4,
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


if __name__ == "__main__":
    main()
