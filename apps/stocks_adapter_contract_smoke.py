"""Sandbox-compatible aggregate WB stocks contract smoke without sockets."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.stocks_block import (  # noqa: E402
    HistoricalCsvBackedStocksSource,
    HttpBackedStocksSource,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    EXECUTION_MODE_PERSISTED_RETRY,
    SheetVitrinaV1LivePlanBlock,
    _persisted_stocks_warehouse_region_map,
)
from packages.application.stocks_block import StocksBlock, transform_legacy_payload  # noqa: E402
from packages.application.wb_incident_policy import (  # noqa: E402
    build_vitrina_incident_stock_projection,
    canonical_seller_id,
)
from packages.contracts.stocks_block import (  # noqa: E402
    StocksItem,
    StocksRequest,
    StocksSuccess,
    StocksWarehouseRow,
)


TOKEN_ENV = "WB_STOCKS_ADAPTER_CONTRACT_SMOKE_TOKEN"
AS_OF_DATE = "2026-08-15"
CAPTURED_AT = "2026-08-16T06:30:00Z"
INPUT_BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _InjectedOpener:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.request_bodies: list[dict[str, Any]] = []

    def __call__(self, request: Any, *, timeout: float) -> _Response:
        del timeout
        self.request_bodies.append(json.loads(request.data.decode("utf-8")))
        return _Response(self.payloads.pop(0))


class _UnavailableCurrentInventory:
    def fetch_warehouse_region_map(self, requested_nm_ids: list[int]) -> dict[str, str]:
        del requested_nm_ids
        raise RuntimeError("synthetic current inventory outage")


class _UnexpectedStocksSource:
    def fetch(self, request: StocksRequest) -> dict[str, Any]:
        raise AssertionError(
            "exact-date accepted stocks snapshot must be reused without refetch: "
            f"{request.snapshot_date}"
        )


def _item(
    nm_id: int,
    warehouse_id: Any,
    *,
    name: str,
    region: str | None = None,
    quantity: int,
) -> dict[str, Any]:
    return {
        "nmId": nm_id,
        "chrtId": nm_id * 10,
        "warehouseId": warehouse_id,
        "warehouseName": name,
        "regionName": region if region is not None else name,
        "quantity": quantity,
        "inWayToClient": 2,
        "inWayFromClient": 1,
    }


def _source(opener: _InjectedOpener) -> HttpBackedStocksSource:
    return HttpBackedStocksSource(
        token_env_var=TOKEN_ENV,
        base_url_env_var="",
        opener=opener,
        min_request_interval_seconds=0,
        reuse_ttl_seconds=0,
        now_factory=lambda: datetime(2026, 8, 16, 6, tzinfo=timezone.utc),
    )


def _canonical_digest(rows: list[dict[str, Any]]) -> str:
    ordered = sorted(
        rows,
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    encoded = json.dumps(
        ordered,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _assert_exact_sentinel_and_raw_evidence() -> None:
    raw_rows = [
        _item(101, -999999, name="Склад WB", quantity=17),
        _item(202, -999999, name="Склад WB", quantity=23),
    ]
    opener = _InjectedOpener([{"data": {"items": raw_rows}}])
    payload = _source(opener).fetch(
        StocksRequest(
            snapshot_type="stocks",
            snapshot_date=AS_OF_DATE,
            nm_ids=[202, 101],
        )
    )
    if opener.request_bodies != [
        {"nmIds": [101, 202], "chrtIds": [], "limit": 250000, "offset": 0}
    ]:
        raise AssertionError(f"batched request shape drifted: {opener.request_bodies}")
    data = payload["data"]
    if data["raw_rows"] != raw_rows:
        raise AssertionError("raw WB rows were replaced by normalized rows")
    if data["raw_rows_digest"] != _canonical_digest(raw_rows):
        raise AssertionError("raw_rows_digest no longer binds exact WB source rows")
    if data["warehouse_granularity_complete"] is not False:
        raise AssertionError("aggregate-only source must expose incomplete granularity")
    if data["normalization"]["aggregate_sentinel_row_count"] != 2:
        raise AssertionError("aggregate normalization provenance is incomplete")
    if not all(
        row["warehouseId"] == 0
        and row["warehouseName"] == "Остальные"
        and row["sourceWarehouseId"] == -999999
        and row["normalizationReason"] == "exact_wb_aggregate_sentinel"
        for row in data["rows"]
    ):
        raise AssertionError(f"normalized service-row provenance was lost: {data['rows']}")
    result = transform_legacy_payload(payload).result
    if result.kind != "success" or result.count != 2:
        raise AssertionError(f"aggregate sentinel must retain SKU coverage: {result}")
    if sum(item.stock_total for item in result.items) != 40:
        raise AssertionError(f"aggregate sentinel quantities were lost: {result.items}")
    if result.warehouse_granularity_complete is not False:
        raise AssertionError("StocksSuccess must retain aggregate-only quality")


def _assert_strict_sentinel_identity() -> None:
    bad_rows = (
        _item(101, -1, name="Склад 1", quantity=1),
        _item(101, -999999, name="Неизвестный", quantity=1),
        _item(101, -999999, name="Склад WB", region="Неизвестный", quantity=1),
        _item(101, -999999.0, name="Склад WB", quantity=1),
        _item(101, "-999999", name="Склад WB", quantity=1),
        _item(101, True, name="Склад WB", quantity=1),
    )
    for bad in bad_rows:
        try:
            _source(_InjectedOpener([{"data": {"items": [bad]}}])).fetch(
                StocksRequest(
                    snapshot_type="stocks",
                    snapshot_date=AS_OF_DATE,
                    nm_ids=[101],
                )
            )
        except RuntimeError as exc:
            if "invalid warehouseId" not in str(exc):
                raise AssertionError(f"unexpected negative-ID failure: {exc}") from exc
        else:
            raise AssertionError(f"non-exact sentinel was accepted: {bad!r}")


def _assert_historical_mixed_granularity() -> None:
    historical = HistoricalCsvBackedStocksSource(
        warehouse_region_resolver=lambda _ids: {
            "Коледино": "Центральный",
            "Склад WB": "Центральный",
        },
        current_inventory_source=_UnavailableCurrentInventory(),  # type: ignore[arg-type]
    )._build_batch_payloads(
        csv_rows=[
            {"NmID": "101", "OfficeName": "Коледино", "15.08.2026": "3"},
            {"NmID": "101", "OfficeName": "Склад WB", "15.08.2026": "17"},
        ],
        requested_nm_ids=[101],
        warehouse_region_map={
            "Коледино": "Центральный",
            "Склад WB": "Центральный",
        },
        date_from=AS_OF_DATE,
        date_to=AS_OF_DATE,
        download_id="smoke-download",
        report_name="smoke-report",
        snapshot_ts="2026-08-16 06:00:00",
    )
    payload = historical.payloads[AS_OF_DATE]
    data = payload["data"]
    if data["warehouse_granularity_complete"] is not False:
        raise AssertionError("mixed concrete + aggregate history must stay incomplete")
    if data["warehouse_granularity"]["mixed_aggregate_and_concrete"] is not True:
        raise AssertionError("mixed historical source quality was not surfaced")
    aggregate_row = next(
        row for row in data["rows"] if row["warehouseName"] == "Склад WB"
    )
    if aggregate_row["regionName"] != "Склад WB":
        raise AssertionError("persisted region fallback allocated aggregate history")
    result = transform_legacy_payload(payload).result
    if result.kind != "success" or result.items[0].stock_total != 20:
        raise AssertionError("mixed historical SKU total was not preserved")


def _assert_live_plan_and_temporal_round_trip() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    enabled_nm_ids = sorted(
        int(item["nm_id"])
        for item in bundle["config_v2"]
        if item["enabled"]
    )
    if len(enabled_nm_ids) != 33:
        raise AssertionError("aggregate regression fixture must contain 33 active SKU")
    with TemporaryDirectory(prefix="wb-stocks-aggregate-live-plan-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        ingested = runtime.ingest_bundle(bundle, activated_at="2026-08-16T06:00:00Z")
        if ingested.status != "accepted":
            raise AssertionError(f"fixture bundle was not accepted: {asdict(ingested)}")
        runtime.append_wb_incident_policy_revision(
            seller_id=canonical_seller_id(),
            active=True,
            warehouse_ids=[120762],
            warehouse_identities=[
                {"warehouse_id": 120762, "warehouse_name": "Коледино"}
            ],
            warehouse_entries=[
                {
                    "warehouse_id": 120762,
                    "warehouse_name": "Коледино",
                    "effective_from": "2026-07-25",
                    "effective_to": "",
                }
            ],
            reason="aggregate smoke policy",
            effective_from="2026-07-25",
            effective_to="",
            policy_status="active",
            actor="smoke",
            created_at="2026-08-16T06:00:01Z",
            source="aggregate_smoke",
        )
        items = [
            StocksItem(
                nm_id=nm_id,
                stock_total=float(index + 1),
                stock_ru_central=0.0,
                stock_ru_northwest=0.0,
                stock_ru_volga=0.0,
                stock_ru_ural=0.0,
                stock_ru_south_caucasus=0.0,
                stock_ru_far_siberia=0.0,
                in_way_to_client=2.0,
                in_way_from_client=1.0,
                wb_contour_total=float(index + 4),
            )
            for index, nm_id in enumerate(enabled_nm_ids)
        ]
        warehouse_rows = [
            StocksWarehouseRow(
                nm_id=item.nm_id,
                warehouse_id=0,
                warehouse_name="Остальные",
                region_name="Склад WB",
                quantity=item.stock_total,
                planning_zone_key=None,
                classification_status="outside_central_planning",
                classification_source="official_aggregate_sentinel",
                in_way_to_client=item.in_way_to_client,
                in_way_from_client=item.in_way_from_client,
            )
            for item in items
        ]
        accepted = StocksSuccess(
            kind="success",
            snapshot_date=AS_OF_DATE,
            count=len(items),
            items=items,
            detail="warehouse_granularity=aggregate_only",
            warehouse_rows=warehouse_rows,
            fetched_at=CAPTURED_AT,
            pagination_complete=True,
            raw_rows_digest="sha256:" + "a" * 64,
            warehouse_granularity_complete=False,
        )
        incident_projection = build_vitrina_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=warehouse_rows,
            snapshot_date=AS_OF_DATE,
            fetched_at=CAPTURED_AT,
            pagination_complete=True,
            raw_rows_digest=accepted.raw_rows_digest,
            warehouse_granularity_complete=False,
        )
        for row in dict(incident_projection.get("by_nm_id") or {}).values():
            regional_incident_values = [
                value
                for key, value in dict(row or {}).items()
                if key.startswith(("actual_stock_ru_", "excluded_stock_ru_", "effective_stock_ru_"))
            ]
            if not regional_incident_values or any(
                value is not None for value in regional_incident_values
            ):
                raise AssertionError(
                    "aggregate incident projection exposed a regional number"
                )
        runtime.save_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=AS_OF_DATE,
            captured_at=CAPTURED_AT,
            payload=accepted,
        )
        loaded, _ = runtime.load_temporal_source_snapshot(
            source_key="stocks",
            snapshot_date=AS_OF_DATE,
        )
        if getattr(loaded, "warehouse_granularity_complete", None) is not False:
            raise AssertionError("aggregate quality did not survive temporal persistence")
        if _persisted_stocks_warehouse_region_map(runtime):
            raise AssertionError("aggregate service rows entered persisted region fallback")

        plan = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            stocks_block=StocksBlock(_UnexpectedStocksSource()),
            now_factory=lambda: datetime(2026, 8, 16, 7, tzinfo=timezone.utc),
        ).build_plan(
            as_of_date=AS_OF_DATE,
            execution_mode=EXECUTION_MODE_PERSISTED_RETRY,
            source_keys=["stocks"],
        )
        data_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
        rows = {str(row[1]): list(row) for row in data_sheet.rows if len(row) >= 4}
        expected_total = float(sum(range(1, len(enabled_nm_ids) + 1)))
        for index, nm_id in enumerate(enabled_nm_ids, start=1):
            value = rows[f"SKU:{nm_id}|stock_total"][2]
            if value != float(index):
                raise AssertionError(
                    "SKU stock_total was lost for "
                    f"{nm_id}: {rows[f'SKU:{nm_id}|stock_total']}; "
                    f"metadata={dict(plan.metadata or {})}"
                )
        if rows["TOTAL|total_stock_total"][2] != expected_total:
            raise AssertionError("aggregate TOTAL stock_total was not preserved")
        unavailable_metric_keys = {
            "stock_ru_central",
            "stock_ru_northwest",
            "stock_ru_volga",
            "stock_ru_south_caucasus",
            "stock_ru_ural",
            "stock_ru_far_siberia",
        }
        for metric_key in unavailable_metric_keys:
            matching = [
                row
                for row_id, row in rows.items()
                if row_id.endswith(f"|{metric_key}")
                or row_id == f"TOTAL|total_{metric_key}"
            ]
            if not matching or any(row[2] not in {"", None} for row in matching):
                raise AssertionError(
                    "unprovable aggregate regional metric became numeric: "
                    f"{metric_key}={matching[:3]}"
                )
        metadata = dict(plan.metadata or {})
        quality = dict(
            dict(metadata.get("incident_projection_quality_by_date") or {}).get(
                AS_OF_DATE
            )
            or {}
        )
        if quality.get("state") != "aggregate_only":
            raise AssertionError(f"aggregate incident quality was not persisted: {quality}")


def main() -> None:
    previous = os.environ.get(TOKEN_ENV)
    os.environ[TOKEN_ENV] = "injected-smoke-token"
    try:
        _assert_exact_sentinel_and_raw_evidence()
        _assert_strict_sentinel_identity()
        _assert_historical_mixed_granularity()
        _assert_live_plan_and_temporal_round_trip()
        print("exact_typed_sentinel: ok")
        print("raw_source_evidence: ok")
        print("mixed_historical_granularity: ok")
        print("temporal_round_trip: ok")
        print("sku_total_preserved_regions_blank: ok")
        print("smoke-check passed")
    finally:
        if previous is None:
            os.environ.pop(TOKEN_ENV, None)
        else:
            os.environ[TOKEN_ENV] = previous


if __name__ == "__main__":
    main()
