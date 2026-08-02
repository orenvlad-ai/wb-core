"""Deterministic incident-policy contract smoke; never calls or mutates live WB."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_incident_stocks import (
    INCIDENT_STOCK_METRIC_KEYS,
    extend_metrics_with_incident_stock_metrics,
    incident_stock_metric_key,
    incident_stock_total_metric_key,
    incident_stock_value,
)
from packages.application.wb_incident_policy import (
    WbIncidentPolicyError,
    build_incident_stock_projection,
    build_vitrina_incident_stock_projection,
    get_latest_policy_state,
    get_policy_state,
    save_policy_revision,
)
from packages.contracts.stocks_block import StocksItem, StocksWarehouseRow


def main() -> None:
    with TemporaryDirectory(prefix="wb-incident-policy-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        items = [
            StocksItem(
                nm_id=1,
                stock_total=15,
                stock_ru_central=15,
                stock_ru_northwest=0,
                stock_ru_volga=0,
                stock_ru_ural=0,
                stock_ru_south_caucasus=0,
                stock_ru_far_siberia=0,
                stock_ru_central_north=10,
                stock_ru_central_east=5,
                stock_ru_central_south=0,
                in_way_to_client=4,
                in_way_from_client=2,
                wb_contour_total=21,
            )
        ]
        rows = [
            StocksWarehouseRow(
                nm_id=1,
                warehouse_id=101,
                warehouse_name="Альфа",
                region_name="Центральный",
                quantity=10,
                in_way_to_client=4,
                in_way_from_client=2,
                planning_zone_key="central_north",
                classification_status="mapped",
                classification_source="fixture",
            ),
            StocksWarehouseRow(
                nm_id=1,
                warehouse_id=102,
                warehouse_name="Бета",
                region_name="Центральный",
                quantity=5,
                planning_zone_key="central_east",
                classification_status="mapped",
                classification_source="fixture",
            ),
        ]
        policy = save_policy_revision(
            runtime,
            payload={
                "base_revision": 0,
                "active": True,
                "excluded_wb_warehouse_ids": [101],
                "reason": "fixture incident",
                "effective_from": "2026-07-20",
                "effective_to": "2026-07-21",
                "status": "active",
            },
            actor="operator",
            warehouse_options=[
                {"warehouse_id": 101, "warehouse_name": "Альфа"},
                {"warehouse_id": 102, "warehouse_name": "Бета"},
            ],
            timestamp="2026-07-20T08:00:00Z",
            seller_id="fixture",
        )
        assert policy["revision"] == 1 and policy["actor"] == "operator"
        before_start = get_policy_state(
            runtime,
            snapshot_date="2026-07-19",
            seller_id="fixture",
        )
        assert before_start["active"] is False
        assert before_start["materialize_incident_metrics"] is False

        projection = build_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=rows,
            snapshot_date="2026-07-20",
            fetched_at="2026-07-20T08:00:00+00:00",
            pagination_complete=True,
            raw_rows_digest="sha256:fixture-active",
            seller_id="fixture",
        )
        row = projection["by_nm_id"]["1"]
        assert row["actual_stock_total_mp"] == 15
        assert row["excluded_stock_total_mp"] == 10
        assert row["effective_stock_total_mp"] == 5
        assert row["excluded_stock_ru_central"] == 10
        assert row["excluded_stock_ru_central_north"] == 10
        assert row["effective_stock_ru_central_north"] == 0
        assert row["effective_stock_ru_central_east"] == 5
        assert items[0].stock_total + items[0].in_way_to_client + items[0].in_way_from_client == 21
        assert rows[0].in_way_to_client == 4 and rows[0].in_way_from_client == 2
        assert incident_stock_value(incident_stock_metric_key("fact"), row) == 15
        assert incident_stock_value(incident_stock_metric_key("incident"), row) == 10
        assert incident_stock_value(incident_stock_metric_key("effective"), row) == 5

        cached = build_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=rows,
            snapshot_date="2026-07-20",
            fetched_at="2026-07-20T08:00:00+00:00",
            pagination_complete=True,
            raw_rows_digest="sha256:fixture-active",
            seller_id="fixture",
        )
        assert cached["cache"]["status"] == "hit"

        historical_rows = [
            StocksWarehouseRow(
                nm_id=row.nm_id,
                warehouse_id=None,
                warehouse_name=row.warehouse_name,
                region_name=row.region_name,
                quantity=row.quantity,
                in_way_to_client=row.in_way_to_client,
                in_way_from_client=row.in_way_from_client,
                planning_zone_key=row.planning_zone_key,
                classification_status=row.classification_status,
                classification_source="historical_office_name",
            )
            for row in rows
        ]
        historical = build_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=historical_rows,
            snapshot_date="2026-07-21",
            fetched_at="2026-07-21T08:00:00+00:00",
            pagination_complete=True,
            raw_rows_digest="sha256:fixture-history",
            seller_id="fixture",
        )
        assert historical["by_nm_id"]["1"]["excluded_stock_total_mp"] == 10
        unknown_historical_rows = [
            StocksWarehouseRow(
                nm_id=source_row.nm_id,
                warehouse_id=None,
                warehouse_name=(
                    "Неоднозначный исторический OfficeName"
                    if source_row.warehouse_id == 101
                    else source_row.warehouse_name
                ),
                region_name=source_row.region_name,
                quantity=source_row.quantity,
                in_way_to_client=source_row.in_way_to_client,
                in_way_from_client=source_row.in_way_from_client,
                planning_zone_key=source_row.planning_zone_key,
                classification_status=source_row.classification_status,
                classification_source="historical_office_name",
            )
            for source_row in rows
        ]
        unknown_historical = build_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=unknown_historical_rows,
            snapshot_date="2026-07-21",
            fetched_at="2026-07-21T09:00:00+00:00",
            pagination_complete=True,
            raw_rows_digest="sha256:fixture-history-unknown",
            seller_id="fixture",
        )
        assert unknown_historical["by_nm_id"]["1"]["excluded_stock_total_mp"] == 0
        after_interval = get_policy_state(
            runtime,
            snapshot_date="2026-07-22",
            seller_id="fixture",
        )
        assert after_interval["active"] is False
        assert after_interval["materialize_incident_metrics"] is True

        disabled = save_policy_revision(
            runtime,
            payload={
                "base_revision": 1,
                "active": False,
                "excluded_wb_warehouse_ids": [101],
                "reason": "resolved",
                "effective_from": "2026-07-22",
                "effective_to": "",
                "status": "resolved",
            },
            actor="operator-2",
            warehouse_options=[
                {"warehouse_id": 101, "warehouse_name": "Альфа"},
                {"warehouse_id": 102, "warehouse_name": "Бета"},
            ],
            timestamp="2026-07-22T08:00:00Z",
            seller_id="fixture",
        )
        assert disabled["warehouse_ids"] == [101]
        resolved = get_policy_state(
            runtime,
            snapshot_date="2026-07-22",
            seller_id="fixture",
        )
        assert resolved["active"] is False
        future = build_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=rows,
            snapshot_date="2026-07-22",
            fetched_at="2026-07-22T08:00:00+00:00",
            pagination_complete=True,
            raw_rows_digest="sha256:fixture-disabled",
            seller_id="fixture",
        )
        assert future["by_nm_id"]["1"]["effective_stock_total_mp"] == 15
        assert future["by_nm_id"]["1"]["excluded_stock_total_mp"] == 0

        save_policy_revision(
            runtime,
            payload={
                "base_revision": 0,
                "active": True,
                "excluded_wb_warehouse_ids": [101],
                "reason": "active before scheduled replacement",
                "effective_from": "2026-07-20",
                "effective_to": "",
                "status": "active",
            },
            actor="operator",
            warehouse_options=[{"warehouse_id": 101, "warehouse_name": "Альфа"}],
            timestamp="2026-07-20T09:00:00Z",
            seller_id="scheduled-fixture",
        )
        save_policy_revision(
            runtime,
            payload={
                "base_revision": 1,
                "active": True,
                "excluded_wb_warehouse_ids": [102],
                "reason": "scheduled replacement",
                "effective_from": "2026-08-01",
                "effective_to": "2026-08-02",
                "status": "monitoring",
            },
            actor="operator",
            warehouse_options=[{"warehouse_id": 102, "warehouse_name": "Бета"}],
            timestamp="2026-07-20T10:00:00Z",
            seller_id="scheduled-fixture",
        )
        editable = get_latest_policy_state(
            runtime,
            snapshot_date="2026-07-21",
            seller_id="scheduled-fixture",
        )
        assert editable["revision"] == 2
        assert editable["warehouse_ids"] == [102]
        assert editable["active"] is True
        assert editable["effective_revision"] == 1
        assert editable["effective_warehouse_ids"] == [101]
        after_scheduled_interval = get_policy_state(
            runtime,
            snapshot_date="2026-08-03",
            seller_id="scheduled-fixture",
        )
        assert after_scheduled_interval["revision"] == 2
        assert after_scheduled_interval["active"] is False
        assert after_scheduled_interval["warehouse_ids"] == [102]

        save_policy_revision(
            runtime,
            payload={
                "base_revision": 0,
                "active": True,
                "excluded_wb_warehouse_ids": [101],
                "reason": "resolved status is fail closed",
                "effective_from": "2026-07-20",
                "effective_to": "",
                "status": "resolved",
            },
            actor="operator",
            warehouse_options=[{"warehouse_id": 101, "warehouse_name": "Альфа"}],
            timestamp="2026-07-20T11:00:00Z",
            seller_id="resolved-fixture",
        )
        resolved_toggle_mismatch = get_policy_state(
            runtime,
            snapshot_date="2026-07-20",
            seller_id="resolved-fixture",
        )
        assert resolved_toggle_mismatch["active"] is False

        for invalid_options in (
            [
                {"warehouse_id": 201, "warehouse_name": "Одинаковое имя"},
                {"warehouse_id": 202, "warehouse_name": "Одинаковое имя"},
            ],
            [{"warehouse_id": 201, "warehouse_name": "Другой склад"}],
        ):
            try:
                save_policy_revision(
                    runtime,
                    payload={
                        "base_revision": 0,
                        "active": True,
                        "excluded_wb_warehouse_ids": [201, 202],
                        "reason": "invalid identity fixture",
                        "effective_from": "2026-07-20",
                        "effective_to": "",
                        "status": "active",
                    },
                    actor="operator",
                    warehouse_options=invalid_options,
                    timestamp="2026-07-20T12:00:00Z",
                    seller_id="identity-validation-fixture",
                )
            except WbIncidentPolicyError:
                pass
            else:
                raise AssertionError(
                    "missing or ambiguous warehouse identity must fail closed"
                )

        try:
            build_incident_stock_projection(
                runtime,
                items=items,
                warehouse_rows=rows,
                snapshot_date="2026-07-20",
                fetched_at="2026-07-20T08:00:00+00:00",
                pagination_complete=False,
                raw_rows_digest="sha256:incomplete",
                seller_id="fixture",
            )
        except WbIncidentPolicyError:
            pass
        else:
            raise AssertionError("active policy must fail closed for incomplete pagination")
        try:
            build_incident_stock_projection(
                runtime,
                items=items,
                warehouse_rows=rows,
                snapshot_date="2026-07-20",
                fetched_at="2026-07-20T08:00:00+00:00",
                pagination_complete=True,
                raw_rows_digest="",
                seller_id="fixture",
            )
        except WbIncidentPolicyError:
            pass
        else:
            raise AssertionError(
                "strict default must fail closed when the source digest is missing"
            )

        provisional = build_vitrina_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=[rows[0]],
            snapshot_date="2026-07-20",
            fetched_at="2026-07-20T08:00:00+00:00",
            pagination_complete=False,
            raw_rows_digest="",
            seller_id="fixture",
        )
        provisional_row = provisional["by_nm_id"]["1"]
        assert provisional["projection_mode"] == "vitrina_provisional_received_rows"
        assert provisional["quality"]["completeness_confirmed"] is False
        assert provisional["quality"]["accepted_item_count"] == 1
        assert provisional["quality"]["accepted_warehouse_row_count"] == 1
        assert provisional["snapshot_digest"] == ""
        assert provisional["cache_identity_digest"].startswith(
            "vitrina-accepted-payload:sha256:"
        )
        assert provisional_row["actual_stock_total_mp"] == 15
        assert provisional_row["excluded_stock_total_mp"] == 10
        assert provisional_row["effective_stock_total_mp"] == 5
        assert "2" not in provisional["by_nm_id"]
        assert provisional["invariants"]["status"] == "ok"
        total_invariant = provisional["invariants"]["totals"]["total"]
        assert total_invariant == {
            "projected_sku_count": 1,
            "fact": 15.0,
            "incident": 10.0,
            "effective": 5.0,
        }
        provisional_cached = build_vitrina_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=[rows[0]],
            snapshot_date="2026-07-20",
            fetched_at="2026-07-20T08:00:00+00:00",
            pagination_complete=False,
            raw_rows_digest="",
            seller_id="fixture",
        )
        assert provisional_cached["cache"]["status"] == "hit"
        changed_row = StocksWarehouseRow(
            **{
                **rows[0].__dict__,
                "quantity": 9,
            }
        )
        changed_provisional = build_vitrina_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=[changed_row],
            snapshot_date="2026-07-20",
            fetched_at="2026-07-20T08:00:00+00:00",
            pagination_complete=False,
            raw_rows_digest="",
            seller_id="fixture",
        )
        assert (
            changed_provisional["cache_identity_digest"]
            != provisional["cache_identity_digest"]
        )
        assert changed_provisional["cache"]["status"] == "miss"

        contradictory = build_vitrina_incident_stock_projection(
            runtime,
            items=items,
            warehouse_rows=[
                replace(rows[0], quantity=20),
            ],
            snapshot_date="2026-07-20",
            fetched_at="2026-07-20T08:00:00+00:00",
            pagination_complete=False,
            raw_rows_digest="",
            seller_id="fixture",
            cache_enabled=False,
        )
        contradictory_row = contradictory["by_nm_id"]["1"]
        assert contradictory_row["actual_stock_total_mp"] is None
        assert contradictory_row["excluded_stock_total_mp"] is None
        assert contradictory_row["effective_stock_total_mp"] is None
        assert "превышает factual stock" in (
            contradictory_row["blank_reasons_by_field"]["stock_total_mp"]
        )
        assert contradictory["invariants"]["status"] == "ok"
        try:
            save_policy_revision(
                runtime,
                payload={
                    "base_revision": 0,
                    "active": True,
                    "excluded_wb_warehouse_ids": [0],
                    "reason": "official special bucket fixture",
                    "effective_from": "2026-07-20",
                    "effective_to": "",
                    "status": "active",
                },
                actor="operator",
                warehouse_options=[
                    {
                        "warehouse_id": 0,
                        "warehouse_name": "Остальные — служебная группа WB",
                    }
                ],
                timestamp="2026-07-20T12:30:00Z",
                seller_id="service-bucket-fixture",
            )
        except WbIncidentPolicyError as exc:
            assert "service bucket" in str(exc)
        else:
            raise AssertionError("warehouseId 0 must not become an operational incident destination")

        warehouse_options = [
            {"warehouse_id": 101, "warehouse_name": "Альфа"},
            {"warehouse_id": 102, "warehouse_name": "Бета"},
        ]
        migrated = save_policy_revision(
            runtime,
            payload={
                "base_revision": 0,
                "active": True,
                "excluded_wb_warehouse_ids": [101],
                "reason": "existing 25 July selection",
                "effective_from": "2026-07-25",
                "effective_to": "",
                "status": "active",
            },
            actor="operator",
            warehouse_options=warehouse_options,
            timestamp="2026-07-25T08:00:00Z",
            seller_id="mixed-date-fixture",
        )
        assert migrated["warehouse_entries"][0]["effective_from"] == "2026-07-25"
        mixed = save_policy_revision(
            runtime,
            payload={
                "base_revision": 1,
                "active": True,
                "warehouse_entries": [
                    {"warehouse_id": 101, "effective_from": "2026-07-25"},
                    {"warehouse_id": 102, "effective_from": "2026-08-02"},
                ],
                "reason": "mixed dates",
                "effective_to": "",
                "status": "active",
            },
            actor="operator",
            warehouse_options=warehouse_options,
            timestamp="2026-08-02T08:00:00Z",
            seller_id="mixed-date-fixture",
        )
        assert mixed["revision"] == 2
        assert mixed["changed_from"] == "2026-08-02"
        assert [
            (entry["warehouse_id"], entry["effective_from"])
            for entry in mixed["warehouse_entries"]
        ] == [(101, "2026-07-25"), (102, "2026-08-02")]
        assert get_policy_state(
            runtime,
            snapshot_date="2026-08-01",
            seller_id="mixed-date-fixture",
        )["warehouse_ids"] == [101]
        assert get_policy_state(
            runtime,
            snapshot_date="2026-08-02",
            seller_id="mixed-date-fixture",
        )["warehouse_ids"] == [101, 102]
        repeated = save_policy_revision(
            runtime,
            payload={
                "base_revision": 2,
                "active": True,
                "warehouse_entries": [
                    {"warehouse_id": 101, "effective_from": "2026-07-25"},
                    {"warehouse_id": 102, "effective_from": "2026-08-02"},
                ],
                "reason": "mixed dates",
                "effective_to": "",
                "status": "active",
            },
            actor="operator",
            warehouse_options=warehouse_options,
            timestamp="2026-08-02T08:01:00Z",
            seller_id="mixed-date-fixture",
        )
        assert repeated["idempotency_status"] == "T0" and repeated["revision"] == 2
        try:
            save_policy_revision(
                runtime,
                payload={
                    "base_revision": 2,
                    "active": True,
                    "warehouse_entries": [
                        {"warehouse_id": 101, "effective_from": ""},
                    ],
                    "reason": "missing date",
                    "status": "active",
                },
                actor="operator",
                warehouse_options=warehouse_options,
                seller_id="mixed-date-fixture",
            )
        except WbIncidentPolicyError as exc:
            assert "effective_from is required" in str(exc)
        else:
            raise AssertionError("selected warehouses without a date must fail closed")

        removed = save_policy_revision(
            runtime,
            payload={
                "base_revision": 2,
                "active": True,
                "warehouse_entries": [
                    {"warehouse_id": 101, "effective_from": "2026-07-25"},
                ],
                "change_effective_from": "2026-08-03",
                "reason": "temporarily remove beta",
                "status": "active",
            },
            actor="operator",
            warehouse_options=warehouse_options,
            timestamp="2026-08-03T08:00:00Z",
            seller_id="mixed-date-fixture",
        )
        assert removed["revision"] == 3 and removed["changed_from"] == "2026-08-03"
        try:
            save_policy_revision(
                runtime,
                payload={
                    "base_revision": 3,
                    "active": True,
                    "warehouse_entries": [
                        {"warehouse_id": 101, "effective_from": "2026-07-25"},
                        {"warehouse_id": 102, "effective_from": "2026-08-02"},
                    ],
                    "reason": "overlapping reselect",
                    "status": "active",
                },
                actor="operator",
                warehouse_options=warehouse_options,
                timestamp="2026-08-03T08:01:00Z",
                seller_id="mixed-date-fixture",
            )
        except WbIncidentPolicyError as exc:
            assert "before its prior interval closed" in str(exc)
        else:
            raise AssertionError("overlapping per-warehouse re-selection must fail closed")
        reselected = save_policy_revision(
            runtime,
            payload={
                "base_revision": 3,
                "active": True,
                "warehouse_entries": [
                    {"warehouse_id": 101, "effective_from": "2026-07-25"},
                    {"warehouse_id": 102, "effective_from": "2026-08-03"},
                ],
                "reason": "deterministic reselect",
                "status": "active",
            },
            actor="operator",
            warehouse_options=warehouse_options,
            timestamp="2026-08-03T08:02:00Z",
            seller_id="mixed-date-fixture",
        )
        assert reselected["revision"] == 4
        reselected_audit = runtime.load_latest_wb_incident_policy(
            seller_id="mixed-date-fixture"
        )
        assert [
            (entry["effective_from"], entry["effective_to_exclusive"])
            for entry in reselected_audit["warehouse_entries"]
            if int(entry["warehouse_id"]) == 102
        ] == [("2026-08-02", "2026-08-03"), ("2026-08-03", "")]

        metrics = extend_metrics_with_incident_stock_metrics([])
        assert len(metrics) == 42
        assert {item.metric_key for item in metrics} == set(INCIDENT_STOCK_METRIC_KEYS)
        assert incident_stock_total_metric_key("effective", "central") in INCIDENT_STOCK_METRIC_KEYS
        metric_by_key = {item.metric_key: item for item in metrics}
        short_regions = {
            "total": "всего",
            "central": "Центр",
            "northwest": "СЗ",
            "volga": "Поволжье",
            "south_caucasus": "Юг+СКФО",
            "ural": "Урал",
            "far_siberia": "ДВ+Сибирь",
        }
        for variant in ("fact", "incident", "effective"):
            for region in (
                "total",
                "central",
                "northwest",
                "volga",
                "south_caucasus",
                "ural",
                "far_siberia",
            ):
                sku_key = incident_stock_metric_key(variant, region)
                total_key = incident_stock_total_metric_key(variant, region)
                assert metric_by_key[sku_key].scope == "SKU"
                assert metric_by_key[total_key].scope == "TOTAL"
                assert metric_by_key[total_key].calc_ref == sku_key
                if variant in {"incident", "effective"}:
                    prefix = "Остаток инц.:" if variant == "incident" else "Остаток без инц.:"
                    expected_label = f"{prefix} {short_regions[region]}"
                    assert metric_by_key[sku_key].label_ru == expected_label
                    assert metric_by_key[total_key].label_ru == expected_label

    print("wb_incident_policy_smoke: OK")


if __name__ == "__main__":
    main()
