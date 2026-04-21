"""HTTP integration smoke-check for the phase-1 web-vitrina sibling routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
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
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-http-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        current_result = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
        if current_result.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {current_result}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        start_date = datetime(2026, 4, 14, tzinfo=timezone.utc).date()
        for offset in range(7):
            snapshot_date = (start_date + timedelta(days=offset)).isoformat()
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{snapshot_date}T09:05:00Z",
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
            activated_at_factory=lambda: "2026-04-20T09:00:00Z",
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
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{config.port}"

            contract_status, contract_payload = _get_json(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}")
            if contract_status != 200:
                raise AssertionError(f"web-vitrina read route must return 200, got {contract_status}")
            if contract_payload.get("contract_name") != "web_vitrina_contract" or contract_payload.get("contract_version") != "v1":
                raise AssertionError(f"web-vitrina contract identity mismatch, got {contract_payload}")
            if contract_payload.get("page_route") != DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
                raise AssertionError(f"web-vitrina page route mismatch, got {contract_payload}")
            if contract_payload.get("read_route") != DEFAULT_SHEET_WEB_VITRINA_READ_PATH:
                raise AssertionError(f"web-vitrina read route mismatch, got {contract_payload}")
            if contract_payload.get("meta", {}).get("row_count") != 4:
                raise AssertionError(f"web-vitrina meta row_count mismatch, got {contract_payload}")
            if contract_payload.get("status_summary", {}).get("read_model") != "persisted_ready_snapshot":
                raise AssertionError(f"web-vitrina read seam mismatch, got {contract_payload}")
            if contract_payload.get("meta", {}).get("as_of_date") != "2026-04-19":
                raise AssertionError(f"web-vitrina default as_of_date mismatch, got {contract_payload}")
            row_ids = [row["row_id"] for row in contract_payload.get("rows") or []]
            if row_ids != [
                "TOTAL|total_view_count",
                f"GROUP:{enabled[0].group}|view_count",
                f"SKU:{enabled[0].nm_id}|view_count",
                f"SKU:{enabled[1].nm_id}|orderSum",
            ]:
                raise AssertionError(f"web-vitrina rows mismatch, got {row_ids}")

            period_status, period_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?date_from=2026-04-18&date_to=2026-04-20"
            )
            if period_status != 200:
                raise AssertionError(f"web-vitrina period route must return 200, got {period_status}")
            if period_payload.get("status_summary", {}).get("read_model") != "persisted_ready_snapshot_window":
                raise AssertionError(f"web-vitrina period read seam mismatch, got {period_payload}")
            if period_payload.get("meta", {}).get("as_of_date") != "2026-04-20":
                raise AssertionError(f"web-vitrina period as_of_date mismatch, got {period_payload}")
            if period_payload.get("meta", {}).get("date_columns") != ["2026-04-18", "2026-04-19", "2026-04-20"]:
                raise AssertionError(f"web-vitrina period date columns mismatch, got {period_payload}")

            composition_status, composition_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}"
            )
            if composition_status != 200:
                raise AssertionError(f"web-vitrina page composition surface must return 200, got {composition_status}")
            if composition_payload.get("composition_name") != "web_vitrina_page_composition":
                raise AssertionError(f"web-vitrina page composition identity mismatch, got {composition_payload}")
            if composition_payload.get("meta", {}).get("current_state") != "ready":
                raise AssertionError(f"web-vitrina page composition state mismatch, got {composition_payload}")
            if composition_payload.get("table_surface", {}).get("total_row_count") != 4:
                raise AssertionError(f"web-vitrina page composition row count mismatch, got {composition_payload}")
            historical_access = composition_payload.get("historical_access") or {}
            if historical_access.get("current_mode") != "default":
                raise AssertionError(f"web-vitrina historical selector mode mismatch, got {composition_payload}")
            if historical_access.get("supported_query_mode") != "date_window":
                raise AssertionError(f"web-vitrina historical query mode mismatch, got {historical_access}")
            if [item.get("value") for item in historical_access.get("options") or []] != [
                "2026-04-20",
                "2026-04-19",
                "2026-04-18",
                "2026-04-17",
                "2026-04-16",
                "2026-04-15",
                "2026-04-14",
            ]:
                raise AssertionError(f"web-vitrina historical selector options mismatch, got {historical_access}")
            if [item.get("preset_id") for item in historical_access.get("preset_options") or []] != [
                "week",
                "two_weeks",
                "month",
                "quarter",
                "year",
            ]:
                raise AssertionError(f"web-vitrina preset options mismatch, got {historical_access}")

            period_composition_status, period_composition_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}&date_from=2026-04-18&date_to=2026-04-20"
            )
            if period_composition_status != 200:
                raise AssertionError(f"web-vitrina period page composition must return 200, got {period_composition_status}")
            if period_composition_payload.get("historical_access", {}).get("current_mode") != "historical_period":
                raise AssertionError(f"web-vitrina period page composition mode mismatch, got {period_composition_payload}")
            if period_composition_payload.get("historical_access", {}).get("selected_date_from") != "2026-04-18":
                raise AssertionError(f"web-vitrina period selected_date_from mismatch, got {period_composition_payload}")
            if period_composition_payload.get("historical_access", {}).get("selected_date_to") != "2026-04-20":
                raise AssertionError(f"web-vitrina period selected_date_to mismatch, got {period_composition_payload}")

            page_status, page_html = _get_text(f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_UI_PATH}")
            if page_status != 200:
                raise AssertionError(f"web-vitrina page route must return 200, got {page_status}")
            for expected in (
                "Web-витрина",
                "Phase 4 Web-Vitrina Page Composition",
                DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
                DEFAULT_SHEET_OPERATOR_UI_PATH,
                "surface=page_composition",
                "web_vitrina_page_composition",
                "web_vitrina_view_model",
                "web_vitrina_gravity_table_adapter",
                "data-filter-controls",
                "data-history-calendar",
                "data-history-presets",
                "data-history-date-from",
                "data-history-date-to",
                "Сбросить",
                "Сохранить",
            ):
                if expected not in page_html:
                    raise AssertionError(f"web-vitrina page shell must expose {expected!r}")

            dated_status, dated_payload = _get_json(
                f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}?as_of_date=2026-04-19"
            )
            if dated_status != 200 or dated_payload.get("meta", {}).get("as_of_date") != "2026-04-19":
                raise AssertionError(f"web-vitrina read route must honor as_of_date, got {dated_status} {dated_payload}")

            print("web_vitrina_read_route: ok ->", contract_payload["meta"]["snapshot_id"])
            print("web_vitrina_period_route: ok ->", period_payload["meta"]["date_columns"])
            print("web_vitrina_page_composition_surface: ok ->", composition_payload["composition_name"], composition_payload["meta"]["current_state"])
            print("web_vitrina_history_selector_surface: ok ->", historical_access["current_mode"], historical_access["supported_query_mode"], len(historical_access["options"]))
            print("web_vitrina_page_route: ok ->", DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
            print("web_vitrina_query_override: ok ->", dated_payload["meta"]["as_of_date"])
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _build_plan(
    *,
    as_of_date: str,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"web-vitrina-http-fixture-{as_of_date}",
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
                write_rect="A1:C5",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 20],
                    [f"SKU B: Заказы, шт.", f"SKU:{second_nm_id}|orderSum", 5],
                ],
                row_count=4,
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


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    with urllib_request.urlopen(url) as response:
        return int(response.status), json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> tuple[int, str]:
    with urllib_request.urlopen(url) as response:
        return int(response.status), response.read().decode("utf-8")


if __name__ == "__main__":
    main()
