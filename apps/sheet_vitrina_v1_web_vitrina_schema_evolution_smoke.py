"""Regression smoke for web-vitrina period reads across registry schema evolution."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock  # noqa: E402
from packages.application.sheet_vitrina_v1_inventory_planning import (  # noqa: E402
    INVENTORY_WB_TOTAL_KEY,
)
from packages.application.sheet_vitrina_v1_weighted_seller_price import (  # noqa: E402
    WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
OLD_BUNDLE_VERSION = "schema_evolution_old_without_spp_proxy"
NEW_BUNDLE_VERSION = "schema_evolution_new_with_spp_proxy"
OLD_DATE = "2026-04-20"
NEW_DATE = "2026-04-21"
NOW = datetime(2026, 4, 22, 8, 0, tzinfo=timezone.utc)
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
    with TemporaryDirectory(prefix="web-vitrina-schema-evolution-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        base_bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        nm_id = int(base_bundle["config_v2"][0]["nm_id"])

        old_bundle = _bundle_without_spp_proxy(base_bundle)
        old_result = runtime.ingest_bundle(old_bundle, activated_at="2026-04-20T10:00:00Z")
        if old_result.status != "accepted":
            raise AssertionError(f"old fixture bundle must be accepted, got {old_result}")
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=runtime.load_current_state(),
            refreshed_at="2026-04-20T10:05:00Z",
            plan=_build_old_snapshot(nm_id=nm_id),
        )

        new_bundle = deepcopy(base_bundle)
        new_bundle["bundle_version"] = NEW_BUNDLE_VERSION
        new_bundle["uploaded_at"] = "2026-04-21T10:00:00Z"
        new_result = runtime.ingest_bundle(new_bundle, activated_at="2026-04-21T10:00:00Z")
        if new_result.status != "accepted":
            raise AssertionError(f"new fixture bundle must be accepted, got {new_result}")
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=runtime.load_current_state(),
            refreshed_at="2026-04-21T10:05:00Z",
            plan=_build_new_snapshot(nm_id=nm_id),
        )

        block = SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: NOW)
        readable_dates = block.list_readable_dates(
            date_from=OLD_DATE,
            date_to=NEW_DATE,
            descending=False,
        )
        if readable_dates != [OLD_DATE, NEW_DATE]:
            raise AssertionError(f"readable dates must span old and new bundles, got {readable_dates}")
        materialized_current_dates = block.list_materialized_readable_dates(
            date_from=OLD_DATE,
            date_to=NEW_DATE,
            descending=False,
        )
        if materialized_current_dates != [NEW_DATE]:
            raise AssertionError(
                "group-refresh materialized dates must stay current-bundle scoped, "
                f"got {materialized_current_dates}"
            )

        contract = block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from=OLD_DATE,
            date_to=NEW_DATE,
        )
        if contract.meta.date_columns != [OLD_DATE, NEW_DATE]:
            raise AssertionError(f"period read date columns mismatch, got {contract.meta.date_columns}")
        rows = {row.row_id: row for row in contract.rows}

        _assert_values(
            rows[f"SKU:{nm_id}|orderSum"].values_by_date,
            {OLD_DATE: 111.0, NEW_DATE: 222.0},
            "orders must remain readable after new registry metric",
        )
        _assert_values(
            rows[f"SKU:{nm_id}|price_seller_discounted"].values_by_date,
            {OLD_DATE: 1000.0, NEW_DATE: 1100.0},
            "price snapshot must remain readable after new registry metric",
        )
        _assert_values(
            rows[f"TOTAL|{WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY}"].values_by_date,
            {OLD_DATE: None, NEW_DATE: 1100.0},
            "weighted seller price must stay blank in ready snapshots created before its schema row",
        )
        _assert_values(
            rows[f"SKU:{nm_id}|stock_total"].values_by_date,
            {OLD_DATE: "", NEW_DATE: ""},
            "the unified stock row must not silently relabel legacy WB-only facts",
        )
        _assert_values(
            rows[f"SKU:{nm_id}|{INVENTORY_WB_TOTAL_KEY}"].values_by_date,
            {OLD_DATE: 5.0, NEW_DATE: 7.0},
            "legacy WB-only stocks must remain readable after schema evolution",
        )
        _assert_values(
            rows[f"SKU:{nm_id}|spp"].values_by_date,
            {OLD_DATE: 0.11, NEW_DATE: 0.12},
            "existing SPP must remain readable and unchanged",
        )
        _assert_values(
            rows[f"SKU:{nm_id}|spp_proxy"].values_by_date,
            {OLD_DATE: None, NEW_DATE: 0.23},
            "new SPP proxy must be blank only where old snapshot lacks the row",
        )
        _assert_values(
            rows["TOTAL|avg_spp_proxy"].values_by_date,
            {OLD_DATE: None, NEW_DATE: 0.23},
            "new average SPP proxy must be blank only where old snapshot lacks the row",
        )

        _assert_page_composition_http_schema_evolution(runtime=runtime, runtime_dir=Path(tmp) / "runtime")

    print("web_vitrina_schema_evolution_period_read: ok")


def _bundle_without_spp_proxy(base_bundle: dict[str, Any]) -> dict[str, Any]:
    bundle = deepcopy(base_bundle)
    bundle["bundle_version"] = OLD_BUNDLE_VERSION
    bundle["uploaded_at"] = "2026-04-20T10:00:00Z"
    bundle["metrics_v2"] = [
        item
        for item in bundle["metrics_v2"]
        if str(item.get("metric_key") or "") not in {"spp_proxy", "avg_spp_proxy"}
    ]
    return bundle


def _build_old_snapshot(*, nm_id: int) -> SheetVitrinaV1Envelope:
    return _build_snapshot(
        snapshot_id="old-schema-ready",
        as_of_date=OLD_DATE,
        rows=[
            [f"SKU {nm_id}: Заказы", f"SKU:{nm_id}|orderSum", 111.0],
            [f"SKU {nm_id}: Цена со скидкой", f"SKU:{nm_id}|price_seller_discounted", 1000.0],
            [f"SKU {nm_id}: Остаток", f"SKU:{nm_id}|stock_total", 5.0],
            [f"SKU {nm_id}: SPP", f"SKU:{nm_id}|spp", 0.11],
        ],
    )


def _build_new_snapshot(*, nm_id: int) -> SheetVitrinaV1Envelope:
    return _build_snapshot(
        snapshot_id="new-schema-ready",
        as_of_date=NEW_DATE,
        rows=[
            [
                "Цена продавца взвеш.",
                f"TOTAL|{WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY}",
                1100.0,
            ],
            ["SPP-прокси средняя", "TOTAL|avg_spp_proxy", 0.23],
            [f"SKU {nm_id}: Заказы", f"SKU:{nm_id}|orderSum", 222.0],
            [f"SKU {nm_id}: Цена со скидкой", f"SKU:{nm_id}|price_seller_discounted", 1100.0],
            [f"SKU {nm_id}: Остаток", f"SKU:{nm_id}|stock_total", 7.0],
            [f"SKU {nm_id}: SPP", f"SKU:{nm_id}|spp", 0.12],
            [f"SKU {nm_id}: SPP-прокси", f"SKU:{nm_id}|spp_proxy", 0.23],
        ],
    )


def _build_snapshot(*, snapshot_id: str, as_of_date: str, rows: list[list[Any]]) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=snapshot_id,
        as_of_date=as_of_date,
        date_columns=[as_of_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date=as_of_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:C{len(rows) + 1}",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", as_of_date],
                rows=rows,
                row_count=len(rows),
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
                        "seller_funnel_snapshot",
                        "success",
                        "fresh",
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        1,
                        1,
                        "",
                        "",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _assert_values(actual: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def _assert_page_composition_http_schema_evolution(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    runtime_dir: Path,
) -> None:
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: "2026-04-22T08:00:00Z",
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
        old_period_url = (
            f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}"
            f"?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}"
            "&history_mode=explicit"
            f"&date_from={OLD_DATE}&date_to={OLD_DATE}"
        )
        old_period_status, old_period_payload = _get_json_strict(old_period_url)
        if old_period_status != 200:
            raise AssertionError(f"old-bundle page composition must return 200 JSON, got {old_period_status}")
        if old_period_payload.get("composition_name") != "web_vitrina_page_composition":
            raise AssertionError(f"old-bundle page composition identity mismatch, got {old_period_payload}")
        if (old_period_payload.get("status_summary") or {}).get("source_status_snapshot_as_of_date") != OLD_DATE:
            raise AssertionError(f"old-bundle source-status snapshot key mismatch, got {old_period_payload}")
        old_loading_table = (old_period_payload.get("activity_surface") or {}).get("loading_table") or {}
        if old_loading_table.get("source_status_state") != "not_loaded":
            raise AssertionError(f"old-bundle page shell must stay lazy/not_loaded, got {old_loading_table}")

        details_status, details_payload = _get_json_strict(old_period_url + "&include_source_status=1")
        if details_status != 200:
            raise AssertionError(f"old-bundle source-status page composition must return 200 JSON, got {details_status}")
        details_loading_table = (details_payload.get("activity_surface") or {}).get("loading_table") or {}
        if details_loading_table.get("source_status_state") == "missing_snapshot":
            raise AssertionError(f"old-bundle source-status details must read any-bundle snapshot, got {details_loading_table}")
        if not details_loading_table.get("rows"):
            raise AssertionError(f"old-bundle source-status details must expose persisted status rows, got {details_loading_table}")

        mixed_period_status, mixed_period_payload = _get_json_strict(
            f"{base_url}{DEFAULT_SHEET_WEB_VITRINA_READ_PATH}"
            f"?surface={DEFAULT_SHEET_WEB_VITRINA_PAGE_COMPOSITION_SURFACE}"
            "&history_mode=explicit"
            f"&date_from={OLD_DATE}&date_to={NEW_DATE}"
        )
        if mixed_period_status != 200:
            raise AssertionError(f"mixed-bundle page composition must return 200 JSON, got {mixed_period_status}")
        if (mixed_period_payload.get("status_summary") or {}).get("source_status_snapshot_as_of_date") != NEW_DATE:
            raise AssertionError(f"mixed-bundle source-status snapshot key mismatch, got {mixed_period_payload}")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_json_strict(url: str) -> tuple[int, dict[str, Any]]:
    try:
        with urllib_request.urlopen(url, timeout=20) as response:
            body = response.read()
            content_type = str(response.headers.get("Content-Type") or "")
            if "application/json" not in content_type:
                prefix = body[:500].decode("utf-8", "replace").replace("\n", " ")
                raise AssertionError(f"expected JSON content-type, got {content_type!r}: {prefix}")
            return int(response.status), json.loads(body.decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read()
        content_type = str(exc.headers.get("Content-Type") or "")
        if "application/json" not in content_type:
            prefix = body[:500].decode("utf-8", "replace").replace("\n", " ")
            raise AssertionError(f"expected JSON error response, got status={exc.code} content_type={content_type!r}: {prefix}") from exc
        return int(exc.code), json.loads(body.decode("utf-8"))


if __name__ == "__main__":
    main()
