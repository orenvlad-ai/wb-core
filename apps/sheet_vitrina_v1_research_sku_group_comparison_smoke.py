"""Smoke-check the sheet_vitrina_v1 research SKU group comparison routes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH,
    DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH,
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

BUNDLE_FIXTURE = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
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
    with LocalResearchFixtureServer() as base_url:
        options = _get_json(base_url + DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_OPTIONS_PATH)
        sku_options = options["sku_options"]
        metric_options = options["metric_options"]
        if len(sku_options) < 2:
            raise AssertionError(f"research options must expose active SKU list, got {options}")
        metric_keys = {item["metric_key"] for item in metric_options}
        if "total_fin_buyout_rub" in metric_keys:
            raise AssertionError(f"financial metrics must be excluded from research options, got {metric_options}")
        if "avg_price_seller_discounted" not in metric_keys:
            raise AssertionError(f"operational price metric must stay selectable, got {metric_options}")
        if any(str(item.get("section") or "").lower() in {"финансы", "экономика"} for item in metric_options):
            raise AssertionError(f"financial sections must be excluded, got {metric_options}")

        first_sku = int(sku_options[0]["nm_id"])
        second_sku = int(sku_options[1]["nm_id"])
        payload = {
            "research_sku_ids": [first_sku],
            "control_sku_ids": [second_sku],
            "metric_keys": ["avg_price_seller_discounted", "total_view_count"],
            "baseline_period": {"date_from": "2026-04-14", "date_to": "2026-04-15"},
            "analysis_period": {"date_from": "2026-04-19", "date_to": "2026-04-20"},
        }
        result = _post_json(base_url + DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH, payload)
        if result["contract_name"] != "sheet_vitrina_v1_research_sku_group_comparison_result":
            raise AssertionError(f"research calculation contract mismatch, got {result}")
        if result.get("causal_claim") is not False:
            raise AssertionError(f"research result must not claim causal effect, got {result}")
        if len(result["rows"]) != 2:
            raise AssertionError(f"research result must return one row per metric, got {result}")
        price_row = next(row for row in result["rows"] if row["metric_key"] == "avg_price_seller_discounted")
        if price_row["aggregation_method"] != "mean_observed_values":
            raise AssertionError(f"price metrics must be averaged, got {price_row}")
        count_row = next(row for row in result["rows"] if row["metric_key"] == "total_view_count")
        if count_row["aggregation_method"] != "sum_observed_values":
            raise AssertionError(f"count metrics must be summed, got {count_row}")
        if count_row["diff_in_diff_abs"] is None:
            raise AssertionError(f"research result must include deltas, got {count_row}")

        partial_payload = dict(payload)
        partial_payload["baseline_period"] = {"date_from": "2026-04-13", "date_to": "2026-04-14"}
        partial_result = _post_json(base_url + DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH, partial_payload)
        partial_coverage = partial_result["rows"][0]["coverage"]["research_baseline"]
        if partial_coverage["status"] != "partial" or partial_coverage["missing_points"] <= 0:
            raise AssertionError(f"missing dates must surface partial coverage, got {partial_result}")
        if partial_result["rows"][0]["research_baseline_value"] == 0:
            raise AssertionError(f"missing values must not be zero-filled, got {partial_result}")

        _assert_error(
            base_url,
            {**payload, "control_sku_ids": [first_sku]},
            "Один SKU не может быть одновременно",
        )
        _assert_error(
            base_url,
            {**payload, "research_sku_ids": []},
            "Выберите хотя бы один SKU в исследуемой группе",
        )
        _assert_error(
            base_url,
            {**payload, "metric_keys": []},
            "Выберите хотя бы одну метрику",
        )
        _assert_error(
            base_url,
            {**payload, "baseline_period": {"date_from": "2026-04-20", "date_to": "2026-04-14"}},
            "Базовый период",
        )

    print("sheet_vitrina_v1_research_sku_group_comparison: ok")


class LocalResearchFixtureServer:
    def __enter__(self) -> str:
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        self.runtime_dir_obj = TemporaryDirectory(prefix="sheet-vitrina-research-")
        runtime_dir = Path(self.runtime_dir_obj.name) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T15:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        start_date = datetime(2026, 4, 14, tzinfo=timezone.utc).date()
        for offset in range(7):
            snapshot_date = (start_date + timedelta(days=offset)).isoformat()
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=f"{snapshot_date}T15:05:00Z",
                plan=_build_plan(
                    as_of_date=snapshot_date,
                    offset=offset,
                    first_nm_id=int(enabled[0].nm_id),
                    second_nm_id=int(enabled[1].nm_id),
                    first_group=enabled[0].group,
                ),
            )
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir,
            runtime=runtime,
            activated_at_factory=lambda: "2026-04-21T15:00:00Z",
            refreshed_at_factory=lambda: "2026-04-21T15:05:00Z",
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
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.runtime_dir_obj.cleanup()


def _build_plan(
    *,
    as_of_date: str,
    offset: int,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    first_price = 900 + offset * 10
    second_price = 1100 + offset * 5
    first_views = 100 + offset
    second_views = 80 + offset * 2
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"research-fixture-{as_of_date}",
        as_of_date=as_of_date,
        date_columns=[as_of_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="historical_import",
                slot_label="Historical import",
                column_date=as_of_date,
            )
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
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", first_views + second_views],
                    [f"{first_group}: Показы", f"GROUP:{first_group}|total_view_count", first_views],
                    [f"SKU A: Цена продавца", f"SKU:{first_nm_id}|avg_price_seller_discounted", first_price],
                    [f"SKU B: Цена продавца", f"SKU:{second_nm_id}|avg_price_seller_discounted", second_price],
                    [f"SKU A: Показы", f"SKU:{first_nm_id}|total_view_count", first_views],
                    [f"SKU B: Показы", f"SKU:{second_nm_id}|total_view_count", second_views],
                    [f"SKU A: Финансы", f"SKU:{first_nm_id}|total_fin_buyout_rub", 999999],
                    [f"SKU B: Финансы", f"SKU:{second_nm_id}|total_fin_buyout_rub", 888888],
                ],
                row_count=8,
                column_count=3,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
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
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _assert_error(base_url: str, payload: dict, expected_text: str) -> None:
    try:
        _post_json(base_url + DEFAULT_SHEET_RESEARCH_SKU_GROUP_COMPARISON_CALCULATE_PATH, payload)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        if exc.code != 422 or expected_text not in body:
            raise AssertionError(f"expected 422 containing {expected_text!r}, got {exc.code} {body}") from exc
        return
    raise AssertionError(f"expected research payload to be rejected: {payload}")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
