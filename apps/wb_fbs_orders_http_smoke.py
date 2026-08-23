"""Protected query-only HTTP boundary checks for Stage 7A FBS shadow."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
from urllib import error as urllib_error, request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_FF_POOL_FBS_ORDERS_PATH,
    DEFAULT_INVENTORY_PLANNING_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_UPLOAD_PATH,
    _is_ff_pool_mutation_path,
    build_registry_upload_http_server,
)
from packages.adapters.wb_fbs_orders import WbFbsOrdersPage, WbFbsOrderStatus  # noqa: E402
from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    CUTOVER_MANIFESTS_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    LIFECYCLE_DRAIN_STATE_TABLE,
    LIFECYCLE_IDENTITY_PENDING_RESOLUTIONS_TABLE,
    LIFECYCLE_IDENTITY_PENDING_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    WbFbsOrdersCollector,
    _finance_sales_without_cost,
    _lifecycle_processor_status,
)
from packages.contracts.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypointConfig,
)


class _Source:
    def list_orders(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int | None,
        date_to: int | None,
    ) -> WbFbsOrdersPage:
        assert next_cursor == 0
        orders = [
            _order(55000001, 507, 140557512),
            _order(55000002, 507, 140557513),
            _order(55000003, 507, 140557514, created_at="2026-08-11T08:00:00Z"),
            _order(55000004, 507, 140557515),
            _order(55000005, 507, 140557516),
            _order(55000006, 999, 140557517),
            _order(55000007, 507, 140557518, complete_identity=False),
            _order(55000008, 508, 140557519),
        ]
        orders[0]["address"] = {"fullAddress": "must not cross HTTP"}
        orders[0]["comment"] = "must not cross HTTP"
        return WbFbsOrdersPage(
            orders=orders,
            next_cursor=0,
            limit=limit,
            date_from=date_from,
            date_to=date_to,
        )

    def list_statuses(self, order_ids: list[int]) -> list[WbFbsOrderStatus]:
        assert order_ids == list(range(55000001, 55000009))
        pairs = {
            55000001: ("confirm", "waiting"),
            55000002: ("complete", "sorted"),
            55000003: ("complete", "sold"),
            55000004: ("cancel", "canceled"),
            55000006: ("confirm", "waiting"),
            55000007: ("confirm", "waiting"),
            55000008: ("confirm", "waiting"),
        }
        return [
            WbFbsOrderStatus(
                order_id=order_id,
                supplier_status=pairs[order_id][0],
                wb_status=pairs[order_id][1],
            )
            for order_id in order_ids
            if order_id in pairs
        ]


def _order(
    order_id: int,
    warehouse_id: int,
    nm_id: int,
    *,
    complete_identity: bool = True,
    created_at: str = "2026-08-12T08:00:00Z",
) -> dict[str, object]:
    barcode = str(10_000_000_000_000 + nm_id)
    return {
        "id": order_id,
        "supplyId": f"WB-GI-{order_id}",
        "deliveryType": "fbs",
        "createdAt": created_at,
        "warehouseId": warehouse_id,
        "officeId": 123,
        "nmId": nm_id,
        "chrtId": nm_id + 1_000_000 if complete_identity else None,
        "article": f"SKU-{nm_id}" if complete_identity else "",
        "skus": [barcode] if complete_identity else [],
        "cargoType": 1,
        "crossBorderType": 0,
        "isZeroOrder": False,
    }


def main() -> None:
    assert not _is_ff_pool_mutation_path(DEFAULT_FF_POOL_FBS_ORDERS_PATH)
    assert not _is_ff_pool_mutation_path(f"{DEFAULT_FF_POOL_FBS_ORDERS_PATH}/55000001")
    with TemporaryDirectory(prefix="wb-fbs-orders-http-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES('smoke-warehouse-map',507,'moscow','sha256:warehouse-map',1,'2026-08-12T08:30:00Z','smoke')"
            )
            conn.executemany(
                f"INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES(?,?,?,?,1,'2026-08-12T08:30:00Z','smoke')",
                (
                    ("smoke-warehouse-ambiguous-a", 508, "moscow", "sha256:warehouse-a"),
                    ("smoke-warehouse-ambiguous-b", 508, "orenburg", "sha256:warehouse-b"),
                ),
            )
            identity_rows = []
            for nm_id in (*range(140557512, 140557517), 140557519):
                identity_rows.append(
                    (
                        f"smoke-identity-{nm_id}",
                        nm_id,
                        nm_id + 1_000_000,
                        str(10_000_000_000_000 + nm_id),
                        f"SKU-{nm_id}",
                        nm_id,
                        f"sha256:identity-{nm_id}",
                    )
                )
            conn.executemany(
                f"INSERT INTO {IDENTITY_MAPPINGS_TABLE}(mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,target_nm_id,mapping_digest,active,created_at,created_by) VALUES(?,?,?,?,?,?,?,1,'2026-08-12T08:30:00Z','smoke')",
                identity_rows,
            )
            conn.commit()
        WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-12T09:00:00Z",
            unix_time_factory=lambda: 1_786_522_400,
            source=_Source(),
            enabled=True,
        ).collect_default_window()
        before = _target_counts(runtime.db_path)
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
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime_dir, runtime=runtime
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "INSERT INTO wb_finance_weekly_sku_aggregates("
                "seller_id,week_start,week_end,nm_id,formula_version,metrics_json,"
                "coverage_json,raw_source_digest,week_content_hash,cost_state_hash,"
                "raw_row_count,calculated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "smoke",
                    "2026-08-10",
                    "2026-08-16",
                    "140557514",
                    "smoke-v1",
                    "{}",
                    json.dumps(
                        {
                            "uncovered_fbs_sales_revenue_rub": "900.00",
                            "uncovered_fbs_sales_order_count": 2,
                            "uncovered_fbs_sales_units": 2,
                            "problem_skus": [
                                {
                                    "channel": "FBS",
                                    "reason": "fbs_handoff_cost_event_missing",
                                    "operation_count": 2,
                                }
                            ],
                        }
                    ),
                    "sha256:raw",
                    "sha256:week",
                    "sha256:cost",
                    2,
                    "2026-08-12T09:00:00Z",
                ),
            )
            conn.commit()
        server = build_registry_upload_http_server(
            config,
            entrypoint=entrypoint,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{config.port}"
            root = f"{base}{DEFAULT_FF_POOL_FBS_ORDERS_PATH}"
            inventory_code, inventory, _ = _json_request(
                f"{base}{DEFAULT_INVENTORY_PLANNING_PATH}"
            )
            assert inventory_code == 200
            assert inventory["contract_name"] == "inventory_planning_read_model"
            assert inventory["status"] == "wb_snapshot_unavailable"
            code, payload, headers = _json_request(root)
            assert code == 200 and payload["contract_name"] == "wb_fbs_orders_readonly_shadow_v1"
            assert payload["page"]["total"] == 8 and len(payload["rows"]) == 8
            assert payload["counters"] == {
                "total": 8,
                "active": 4,
                "handed_over": 1,
                "sold_closed": 1,
                "canceled": 1,
                "reconciliation": 1,
                "cost_unresolved": 2,
                "unmatched": 1,
                "deferred": 1,
                "ambiguous": 1,
            }, payload["counters"]
            assert payload["cost_coverage_warning"] == {
                "status": "error",
                "finance_sales_without_cost": {
                    "status": "error",
                    "amount_rub": "900.00",
                    "order_count": 2,
                    "units": 2,
                    "reason": "uncovered_realized_fbs_sales",
                    "source": {
                        "table": "wb_finance_weekly_sku_aggregates",
                        "scope": "all_published_non_account_sku_weeks",
                        "row_count": 1,
                        "invalid_row_count": 0,
                        "published_week_count": 1,
                        "period_start": "2026-08-10",
                        "period_end": "2026-08-16",
                        "latest_calculated_at": "2026-08-12T09:00:00Z",
                    },
                    "weeks": [
                        {
                            "week_start": "2026-08-10",
                            "week_end": "2026-08-16",
                            "amount_rub": "900.00",
                            "order_count": 2,
                            "units": 2,
                            "row_count": 1,
                            "seller_scope_count": 1,
                            "calculated_at": "2026-08-12T09:00:00Z",
                        }
                    ],
                    "reason_counts": {"fbs_handoff_cost_event_missing": 2},
                },
                "lifecycle_unresolved": {
                    "status": "error",
                    "order_count": 2,
                    "reason": "current_lifecycle_cost_unresolved",
                    "status_counts": {"handed_over": 1, "sold_closed": 1},
                    "reason_counts": {"fbs_handoff_cost_event_missing": 2},
                    "facility_counts": {"moscow": 2},
                    "source": {
                        "scope": "current_fbs_orders_matching_page_filters",
                        "date_field": "source_created_at",
                        "date_from": None,
                        "date_to": None,
                        "first_created_at": "2026-08-11T08:00:00Z",
                        "last_created_at": "2026-08-12T08:00:00Z",
                        "filtered_list_status_category": "cost_unresolved",
                    },
                },
                "scopes_are_independent": True,
                "contains_pii": False,
            }
            assert payload["lifecycle_processor"] == {
                "status": "disabled",
                "reason": "cutover_not_applied",
                "cutover_id": None,
                "cursor_sequence": None,
                "latest_status_sequence": None,
                "lag_observation_count": None,
                "pending_identity_count": 0,
                "pending_reason_counts": {},
                "cursor_updated_at": None,
                "latest_status_observed_at": None,
                "contains_pii": False,
            }
            assert payload["policy"]["upstream_get_only"] is False
            assert payload["policy"]["status_post_is_read_semantic"] is True
            assert payload["policy"]["creates_movement"] is False
            assert payload["policy"]["assigns_ff_origin"] is False
            first_order = next(row for row in payload["rows"] if row["order_id"] == 55000001)
            assert "address" not in first_order and "comment" not in first_order
            assert first_order["supplier_status"] == "confirm"
            assert first_order["wb_status"] == "waiting"
            assert first_order["facility_id"] == "moscow"
            assert first_order["sku_mapping"]["target_nm_id"] == 140557512
            etag = next((value for key, value in headers.items() if key.lower() == "etag"), "")
            assert etag.startswith('"sha256:')
            not_modified, empty, response_headers = _json_request(
                root, headers={"If-None-Match": etag}
            )
            assert not_modified == 304 and empty == {}
            assert next(
                value for key, value in response_headers.items() if key.lower() == "etag"
            ) == etag

            detail_code, detail, _ = _json_request(f"{root}/55000001")
            assert detail_code == 200 and detail["current"]["order_id"] == 55000001
            assert detail["current"]["supply_id"] == "WB-GI-55000001"
            assert detail["current"]["supplier_status"] == "confirm"
            assert detail["current"]["status_category"] == "active"
            assert detail["current"]["facility_id"] == "moscow"
            assert detail["privacy"]["contains_pii"] is False
            serialized_detail = json.dumps(detail, ensure_ascii=False).casefold()
            assert "must not cross http" not in serialized_detail
            assert "raw_payload" not in detail
            filtered_code, filtered, _ = _json_request(
                f"{root}?nm_id=140557512&supply_id=WB-GI-55000001&facility_id=moscow&supplier_status=confirm&wb_status=waiting&status_category=active&date_from=2026-08-12&date_to=2026-08-12&limit=1"
            )
            assert filtered_code == 200 and filtered["page"]["total"] == 1
            date_code, date_filtered, _ = _json_request(
                f"{root}?date_from=2026-08-12&date_to=2026-08-12&limit=10"
            )
            assert date_code == 200
            assert date_filtered["counters"]["cost_unresolved"] == 1
            date_warning = date_filtered["cost_coverage_warning"]
            assert date_warning["lifecycle_unresolved"]["order_count"] == 1
            assert date_warning["lifecycle_unresolved"]["status_counts"] == {
                "handed_over": 1
            }
            assert date_warning["lifecycle_unresolved"]["source"]["date_from"] == (
                "2026-08-12"
            )
            assert date_warning["lifecycle_unresolved"]["source"]["date_to"] == (
                "2026-08-12"
            )
            assert date_warning["finance_sales_without_cost"]["amount_rub"] == (
                "900.00"
            )
            assert date_warning["finance_sales_without_cost"]["order_count"] == 2
            for category in (
                "handed_over",
                "sold_closed",
                "canceled",
                "reconciliation",
                "unmatched",
                "deferred",
                "ambiguous",
            ):
                category_code, category_payload, _ = _json_request(
                    f"{root}?status_category={category}&limit=1"
                )
                assert category_code == 200
                assert category_payload["page"]["total"] == 1
                assert len(category_payload["rows"]) == 1
            unresolved_code, unresolved, _ = _json_request(
                f"{root}?status_category=cost_unresolved&limit=10"
            )
            assert unresolved_code == 200
            assert unresolved["page"]["total"] == 2
            assert all(
                row["cost_coverage"]["status"] == "cost_unresolved"
                for row in unresolved["rows"]
            )
            page_code, second_page, _ = _json_request(f"{root}?limit=2&page=2")
            assert page_code == 200
            assert second_page["page"]["number"] == 2
            assert second_page["page"]["total"] == 8
            assert second_page["page"]["has_previous"] is True
            assert second_page["page"]["has_next"] is True
            assert len(second_page["rows"]) == 2
            missing_code, missing, _ = _json_request(f"{root}/99999999")
            assert missing_code == 404 and missing["code"] == "fbs_order_not_found"
            invalid_code, invalid, _ = _json_request(f"{root}?search=%25")
            assert invalid_code == 422 and invalid["code"] == "invalid_search"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert _target_counts(runtime.db_path) == before
    _assert_lifecycle_cursor_freshness()
    _assert_finance_missing_is_not_zero()
    template = (
        ROOT / "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
    ).read_text(encoding="utf-8")
    warning_renderer = template.split("function renderFbsCostWarning", 1)[1].split(
        "function renderFbsLifecycleStatus", 1
    )[0]
    assert "Продажи без себестоимости: ' + escapeHtml(amountText)" not in template
    assert "unresolved_fbs_order_count" not in warning_renderer
    assert "finance_sales_without_cost" in warning_renderer
    assert "lifecycle_unresolved" in warning_renderer
    assert "Finance · продажи без себестоимости" in template
    assert "Текущие FBS-заказы без lifecycle-себестоимости" in template
    assert "data-fbs-lifecycle-status" in template
    print("wb_fbs_orders_http_smoke: OK")


def _assert_lifecycle_cursor_freshness() -> None:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            f"CREATE TABLE {CUTOVER_MANIFESTS_TABLE}(cutover_id TEXT,cutover_at TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {STATUS_OBSERVATIONS_TABLE}(observation_sequence INTEGER,observed_at TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {LIFECYCLE_DRAIN_STATE_TABLE}(cutover_id TEXT,last_status_observation_sequence INTEGER,updated_at TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {LIFECYCLE_IDENTITY_PENDING_TABLE}(pending_id TEXT,cutover_id TEXT,reason_detail_code TEXT)"
        )
        conn.execute(
            f"CREATE TABLE {LIFECYCLE_IDENTITY_PENDING_RESOLUTIONS_TABLE}(pending_id TEXT)"
        )
        conn.execute(
            f"INSERT INTO {CUTOVER_MANIFESTS_TABLE} VALUES('cutover-smoke','2026-08-12T08:00:00Z')"
        )
        conn.executemany(
            f"INSERT INTO {STATUS_OBSERVATIONS_TABLE} VALUES(?,?)",
            ((1, "2026-08-12T08:01:00Z"), (2, "2026-08-12T08:02:00Z")),
        )
        conn.execute(
            f"INSERT INTO {LIFECYCLE_DRAIN_STATE_TABLE} VALUES('cutover-smoke',1,'2026-08-12T08:01:30Z')"
        )
        conn.execute(
            f"INSERT INTO {LIFECYCLE_IDENTITY_PENDING_TABLE} VALUES('pending-smoke','cutover-smoke','order_sku_unmapped')"
        )
        lagging = _lifecycle_processor_status(conn)
        assert lagging["status"] == "lagging"
        assert lagging["cursor_sequence"] == 1
        assert lagging["latest_status_sequence"] == 2
        assert lagging["lag_observation_count"] == 1
        assert lagging["pending_identity_count"] == 1
        assert lagging["pending_reason_counts"] == {"order_sku_unmapped": 1}
        assert lagging["latest_status_observed_at"] == "2026-08-12T08:02:00Z"
        conn.execute(
            f"UPDATE {LIFECYCLE_DRAIN_STATE_TABLE} SET last_status_observation_sequence=2,updated_at='2026-08-12T08:02:30Z'"
        )
        quarantined = _lifecycle_processor_status(conn)
        assert quarantined["status"] == "current_with_quarantine"
        assert quarantined["lag_observation_count"] == 0
        conn.execute(
            f"INSERT INTO {LIFECYCLE_IDENTITY_PENDING_RESOLUTIONS_TABLE} VALUES('pending-smoke')"
        )
        current = _lifecycle_processor_status(conn)
        assert current["status"] == "current"
        assert current["pending_identity_count"] == 0


def _assert_finance_missing_is_not_zero() -> None:
    with sqlite3.connect(":memory:") as conn:
        missing = _finance_sales_without_cost(conn)
        assert missing["status"] == "unavailable"
        assert missing["amount_rub"] is None
        assert missing["order_count"] is None
        assert missing["units"] is None
        conn.execute(
            "CREATE TABLE wb_finance_weekly_sku_aggregates("
            "seller_id TEXT,week_start TEXT,week_end TEXT,nm_id TEXT,"
            "coverage_json TEXT,calculated_at TEXT)"
        )
        conn.execute(
            "INSERT INTO wb_finance_weekly_sku_aggregates VALUES("
            "'smoke','2026-08-10','2026-08-16','101','not-json',"
            "'2026-08-12T09:00:00Z')"
        )
        invalid = _finance_sales_without_cost(conn)
        assert invalid["status"] == "error"
        assert invalid["reason"] == "finance_coverage_rows_invalid"
        assert invalid["amount_rub"] is None
        assert invalid["order_count"] is None
        assert invalid["units"] is None
        assert invalid["source"]["invalid_row_count"] == 1


def _target_counts(db_path: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(db_path) as conn:
        return (
            int(conn.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS_TABLE}").fetchone()[0]),
            int(conn.execute(f"SELECT COUNT(*) FROM {ASSIGNMENTS_TABLE}").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_movement_lines").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations").fetchone()[0]),
        )


def _json_request(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    request = urllib_request.Request(
        url,
        method="GET",
        headers={"Accept": "application/json", **(headers or {})},
    )
    try:
        response = urllib_request.urlopen(request, timeout=20)
        raw = response.read()
        return response.status, json.loads(raw.decode("utf-8")) if raw else {}, dict(response.headers)
    except urllib_error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw.decode("utf-8")) if raw else {}, dict(exc.headers)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


if __name__ == "__main__":
    main()
