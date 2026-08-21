#!/usr/bin/env python3
"""HTTP/UI contract smoke for the independent FBS fulfillment order block."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error, request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.fbs_fulfillment_order_supply_smoke import (
    INPUT_BUNDLE_FIXTURE,
    MOSCOW_ID,
    _seed_facilities,
    _seed_sales_history,
    _seed_shipments,
)
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_FACTORY_ORDER_STATUS_PATH,
    DEFAULT_FBS_FULFILLMENT_ORDER_CALCULATE_PATH,
    DEFAULT_FBS_FULFILLMENT_ORDER_RECOMMENDATION_PATH,
    DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint,
)
from packages.application.simple_xlsx import read_first_sheet_rows
from packages.contracts.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypointConfig,
)


NOW = datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-04-18T09:00:00Z"


def main() -> int:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="fbs-fulfillment-http-") as raw:
        runtime_dir = Path(raw) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.ingest_bundle(bundle, activated_at=NOW_TEXT)
        active_nm_ids = [
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled
        ]
        _seed_facilities(runtime, active_nm_ids)
        _seed_sales_history(runtime, active_nm_ids)
        _seed_shipments(runtime, active_nm_ids)
        _seed_legacy_result(runtime)

        port = _reserve_free_port()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: NOW_TEXT,
            now_factory=lambda: NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=port,
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            html_status, html = _get_text(
                base + DEFAULT_SHEET_OPERATOR_UI_PATH + "?embedded_tab=factory-order"
            )
            assert html_status == 200
            for token in (
                "Заказ на фулфилмент (FBS)",
                "Остатки WB не учитываются",
                "Последние N дней",
                "Произвольный период",
                "Целевой фулфилмент",
                "fbsFulfillmentCalculateButton",
                "legacyFactoryOrderDetails",
                "Старый WB-сценарий",
                DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH,
                DEFAULT_FBS_FULFILLMENT_ORDER_CALCULATE_PATH,
            ):
                assert token in html, token
            assert 'data-supply-section-panel="fbs-fulfillment"' in html
            assert 'data-supply-section-panel="factory" hidden' in html

            status_code, status = _get_json(
                base + DEFAULT_FBS_FULFILLMENT_ORDER_STATUS_PATH
            )
            assert status_code == 200, status
            assert status["wb_stock_used"] is False
            facilities = {item["facility_id"]: item for item in status["facilities"]}
            assert facilities[MOSCOW_ID]["calculation_enabled"] is True
            assert facilities["ff-orenburg"]["calculation_enabled"] is False

            invalid_code, invalid = _post_json(
                base + DEFAULT_FBS_FULFILLMENT_ORDER_CALCULATE_PATH,
                {
                    "target_facility_id": MOSCOW_ID,
                    "sales_history_mode": "custom_period",
                    "sales_date_from": "2026-04-10",
                },
            )
            assert invalid_code == 422
            assert "обязательны" in invalid["error"]

            calc_code, result = _post_json(
                base + DEFAULT_FBS_FULFILLMENT_ORDER_CALCULATE_PATH,
                {
                    "target_facility_id": MOSCOW_ID,
                    "production_days": 10,
                    "factory_to_target_ff_days": 5,
                    "ff_safety_days": 3,
                    "order_cycle_days": 2,
                    "order_batch_qty": 50,
                    "sales_history_mode": "custom_period",
                    "sales_date_from": "2026-04-10",
                    "sales_date_to": "2026-04-12",
                },
            )
            assert calc_code == 200, result
            assert result["wb_stock_used"] is False
            assert result["sales_window"]["calendar_day_count"] == 3
            assert result["sales_window"]["outside_window_samples_used"] is False
            assert result["target_facility_id"] == MOSCOW_ID

            export_code, export_body, export_headers = _get_bytes(
                base + DEFAULT_FBS_FULFILLMENT_ORDER_RECOMMENDATION_PATH
            )
            assert export_code == 200
            assert "spreadsheetml.sheet" in export_headers.get("Content-Type", "")
            export_rows = read_first_sheet_rows(export_body)
            assert "WB stock used" in export_rows[0]
            assert "Включённые даты" in export_rows[0]
            assert "Итоговый demand basis, шт/день" in export_rows[0]
            assert "Целевой фулфилмент" in export_rows[0]

            legacy_code, legacy = _get_json(base + DEFAULT_FACTORY_ORDER_STATUS_PATH)
            assert legacy_code == 200
            assert legacy["legacy_scenario"] is True
            assert legacy["status"] == "stale"
            assert legacy["current_source_readiness"]["ready"] is False
            assert legacy["current_source_readiness"][
                "saved_result_is_currently_ready"
            ] is False
            assert legacy["current_source_readiness"]["last_result_calculated_at"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("sheet_vitrina_v1_fbs_fulfillment_order_http_smoke: ok")
    return 0


def _seed_legacy_result(runtime: RegistryUploadDbBackedRuntime) -> None:
    runtime.save_factory_order_result_state(
        calculated_at=NOW_TEXT,
        payload={
            "status": "success",
            "calculation_id": "legacy-factory-result",
            "calculated_at": NOW_TEXT,
            "report_date": "2026-04-18",
            "horizon_days": 60,
            "target_window_days": 74,
            "inbound_window_end": "2026-07-01",
            "settings": {
                "prod_lead_time_days": 30,
                "lead_time_factory_to_ff_days": 30,
                "lead_time_ff_to_wb_days": 15,
                "safety_days_mp": 15,
                "safety_days_ff": 15,
                "cycle_order_days": 14,
                "order_batch_qty": 250,
                "sales_avg_period_days": 14,
            },
            "datasets": {},
            "summary": {"total_qty": 0, "estimated_weight": 0, "estimated_volume": 0},
            "rows": [],
        },
        evidence={"contract_name": "legacy-smoke", "contract_version": 1},
        export_bytes=b"legacy",
        export_filename="legacy.xlsx",
        export_content_type="application/octet-stream",
    )


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_text(url: str) -> tuple[int, str]:
    try:
        with urllib_request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _get_json(url: str) -> tuple[int, dict[str, object]]:
    status, text = _get_text(url)
    return status, json.loads(text)


def _post_json(url: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib_request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_bytes(url: str) -> tuple[int, bytes, dict[str, str]]:
    try:
        with urllib_request.urlopen(url, timeout=10) as response:
            return response.status, response.read(), dict(response.headers.items())
    except error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items())


if __name__ == "__main__":
    raise SystemExit(main())
