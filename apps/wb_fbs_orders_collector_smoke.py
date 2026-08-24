"""Stage 7A official read-only FBS order/status shadow checks."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from urllib import parse as urllib_parse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_fbs_orders import (  # noqa: E402
    HttpBackedWbFbsOrdersSource,
    WbFbsOrderStatus,
    WbFbsOrdersPage,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.ff_wb_supply_origins import ASSIGNMENTS_TABLE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    STATE_TABLE,
    WbFbsOrdersCollector,
    WbFbsOrdersError,
)
from packages.application.warehouse_recovery_policy import _is_domain_table  # noqa: E402


class _Source:
    def __init__(self, *, changed: bool = False, stuck: bool = False) -> None:
        self.changed = changed
        self.stuck = stuck
        self.calls: list[dict[str, int | None]] = []

    def list_orders(
        self,
        *,
        limit: int,
        next_cursor: int,
        date_from: int | None,
        date_to: int | None,
    ) -> WbFbsOrdersPage:
        self.calls.append(
            {
                "limit": limit,
                "next_cursor": next_cursor,
                "date_from": date_from,
                "date_to": date_to,
            }
        )
        if self.stuck:
            return WbFbsOrdersPage(
                orders=[_order(9001)],
                next_cursor=55,
                limit=limit,
                date_from=date_from,
                date_to=date_to,
            )
        if next_cursor == 0:
            first = _order(1001, warehouse_id=777 if self.changed else 507)
            return WbFbsOrdersPage(
                orders=[first, dict(first), _order(9999, delivery_type="wbgo")],
                next_cursor=101,
                limit=limit,
                date_from=date_from,
                date_to=date_to,
            )
        if next_cursor == 101:
            invalid = _order(9998)
            invalid.pop("nmId")
            return WbFbsOrdersPage(
                orders=[_order(1002, supply_id=""), invalid],
                next_cursor=0,
                limit=limit,
                date_from=date_from,
                date_to=date_to,
            )
        raise AssertionError(f"unexpected cursor {next_cursor}")

    def list_statuses(self, order_ids: list[int]) -> list[WbFbsOrderStatus]:
        return [
            WbFbsOrderStatus(order_id=item, supplier_status="complete", wb_status="waiting")
            for item in order_ids
        ]


class _Response:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def main() -> None:
    _adapter_contract()
    _empty_page_cursor_contract()
    assert _is_domain_table(OBSERVATIONS_TABLE) and _is_domain_table(STATE_TABLE)
    with TemporaryDirectory(prefix="wb-fbs-orders-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "runtime")
        runtime.list_wb_supplies()
        disabled_source = _Source()
        disabled = WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-12T09:00:00Z",
            unix_time_factory=lambda: 1_786_522_400,
            source=disabled_source,
            enabled=False,
        )
        result = disabled.collect_default_window()
        assert result["status"] == "disabled" and result["writes"] == 0
        assert disabled_source.calls == []
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {OBSERVATIONS_TABLE}").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {STATE_TABLE}").fetchone()[0] == 0

        source = _Source()
        collector = WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-12T09:01:00Z",
            unix_time_factory=lambda: 1_786_522_400,
            source=source,
            enabled=True,
        )
        collected = collector.collect_default_window()
        assert collected["status"] == "success" and collected["complete"]
        assert collected["page_count"] == 2 and collected["raw_order_count"] == 5
        assert collected["accepted_order_count"] == 2
        assert collected["ignored_order_count"] == 2
        assert collected["new_observation_count"] == 2
        assert collected["upstream_method"] == "GET" and not collected["mutates_wb"]
        assert collected["status_observation_count"] == 2
        assert [call["next_cursor"] for call in source.calls] == [0, 101]

        repeated = collector.collect_default_window()
        assert repeated["new_observation_count"] == 0
        changed = WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-12T09:02:00Z",
            unix_time_factory=lambda: 1_786_522_400,
            source=_Source(changed=True),
            enabled=True,
        ).collect_default_window()
        assert changed["new_observation_count"] == 1

        _read_and_immutability_contract(runtime.db_path, collector)
        _stuck_cursor_contract(runtime.db_path)
        _zero_non_target_contract(runtime.db_path)
    print("wb_fbs_orders_collector_smoke: OK")


def _adapter_contract() -> None:
    calls: list[object] = []

    def opener(request: object, *, timeout: float) -> _Response:
        calls.append((request, timeout))
        path = urllib_parse.urlparse(request.full_url).path
        if path == "/api/v3/stocks/507":
            return _Response({"stocks": [{"chrtId": 9001, "stock": 7}]})
        if request.get_method() == "POST":
            return _Response({"orders": [{"id": 42, "supplierStatus": "complete", "wbStatus": "waiting"}]})
        if path == "/api/v3/warehouses":
            return _Response([
                {
                    "id": 507,
                    "officeId": 123,
                    "name": "Exact seller warehouse",
                    "cargoType": 1,
                    "deliveryType": 1,
                    "isDeleting": False,
                    "isProcessing": False,
                }
            ])
        if path == "/api/v3/offices":
            return _Response([
                {
                    "id": 123,
                    "name": "Москва (Софьино)",
                    "city": "Москва_Восток",
                    "federalDistrict": "Центральный федеральный округ",
                }
            ])
        return _Response({"next": 42, "orders": [_order(42)]})

    prior_token = os.environ.get("WB_API_TOKEN")
    prior_base = os.environ.get("WB_FBS_API_BASE_URL")
    os.environ["WB_API_TOKEN"] = "stage5-secret-token"
    os.environ["WB_FBS_API_BASE_URL"] = "https://fbs.test"
    try:
        source = HttpBackedWbFbsOrdersSource(opener=opener)
        page = source.list_orders(
            limit=321,
            next_cursor=17,
            date_from=1_786_435_200,
            date_to=1_786_522_400,
        )
        assert page.next_cursor == 42 and len(page.orders) == 1
        request, timeout = calls[0]
        assert request.get_method() == "GET" and request.data is None
        assert timeout == 30.0 and request.headers["Authorization"] == "stage5-secret-token"
        parsed = urllib_parse.urlparse(request.full_url)
        assert parsed.path == "/api/v3/orders"
        assert urllib_parse.parse_qs(parsed.query) == {
            "limit": ["321"],
            "next": ["17"],
            "dateFrom": ["1786435200"],
            "dateTo": ["1786522400"],
        }
        statuses = source.list_statuses([42])
        assert statuses == [WbFbsOrderStatus(order_id=42, supplier_status="complete", wb_status="waiting")]
        status_request, _timeout = calls[1]
        assert status_request.get_method() == "POST"
        assert urllib_parse.urlparse(status_request.full_url).path == "/api/v3/orders/status"
        assert json.loads(status_request.data) == {"orders": [42]}
        warehouses = source.list_seller_warehouses()
        assert len(warehouses) == 1
        assert warehouses[0].warehouse_id == 507 and warehouses[0].office_id == 123
        warehouse_request, _timeout = calls[2]
        assert warehouse_request.get_method() == "GET"
        assert urllib_parse.urlparse(warehouse_request.full_url).path == "/api/v3/warehouses"
        offices = source.list_offices()
        assert len(offices) == 1
        assert offices[0].office_id == 123 and offices[0].city == "Москва_Восток"
        office_request, _timeout = calls[3]
        assert office_request.get_method() == "GET"
        assert urllib_parse.urlparse(office_request.full_url).path == "/api/v3/offices"
        stocks = source.list_stocks(warehouse_id=507, chrt_ids=[9001])
        assert len(stocks) == 1 and stocks[0].chrt_id == 9001 and stocks[0].amount == 7
        stock_request, _timeout = calls[4]
        assert stock_request.get_method() == "POST"
        assert urllib_parse.urlparse(stock_request.full_url).path == "/api/v3/stocks/507"
        assert json.loads(stock_request.data) == {"chrtIds": [9001]}
        try:
            source.list_orders(date_from=1, date_to=31 * 24 * 60 * 60 + 2)
            raise AssertionError("wide FBS window must fail")
        except ValueError as exc:
            assert "30 calendar days" in str(exc)
        assert len(calls) == 5
    finally:
        _restore_env("WB_API_TOKEN", prior_token)
        _restore_env("WB_FBS_API_BASE_URL", prior_base)


def _empty_page_cursor_contract() -> None:
    class EmptyThenComplete:
        def __init__(self) -> None:
            self.cursors: list[int] = []

        def list_orders(
            self,
            *,
            limit: int,
            next_cursor: int,
            date_from: int | None,
            date_to: int | None,
        ) -> WbFbsOrdersPage:
            self.cursors.append(next_cursor)
            return WbFbsOrdersPage(
                orders=[],
                next_cursor=44 if next_cursor == 0 else 0,
                limit=limit,
                date_from=date_from,
                date_to=date_to,
            )

    with TemporaryDirectory(prefix="wb-fbs-empty-page-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "runtime")
        runtime.list_wb_supplies()
        source = EmptyThenComplete()
        result = WbFbsOrdersCollector(
            db_path=runtime.db_path,
            timestamp_factory=lambda: "2026-08-12T09:00:00Z",
            unix_time_factory=lambda: 1_786_522_400,
            source=source,
            enabled=True,
        ).collect_default_window()
        assert result["complete"] and result["page_count"] == 2
        assert source.cursors == [0, 44]


def _read_and_immutability_contract(db_path: Path, collector: WbFbsOrdersCollector) -> None:
    page = collector.orders_page(limit=1)
    assert page["status"] == "ready" and page["page"]["total"] == 2
    assert page["collector"]["uses_status_post"] is True
    assert page["collector"]["status_post_semantic"] == "official_read_only"
    assert page["shadow"]["backfill_plan"]["review_range_from"] == "2026-08-01"
    assert page["shadow"]["supplier_status_complete_triggers_debit"] is False
    assert page["policy"]["stores_customer_address"] is False
    assert page["policy"]["assigns_ff_origin"] is False
    assert page["policy"]["creates_movement"] is False
    detail = collector.order_detail(1001)
    assert detail["current"]["warehouse_id"] == 777 and len(detail["history"]) == 2
    assert set(detail["current"]) == {
        "observation_id", "order_id", "source_revision", "supply_id", "delivery_type",
        "source_created_at", "warehouse_id", "office_id", "nm_id", "chrt_id", "seller_sku", "skus",
        "cargo_type", "cross_border_type", "is_zero_order", "observed_at",
        "supplier_status", "wb_status", "status_category", "facility_id", "mapping_outcome",
        "mapping_category", "sku_mapping", "reservation", "debit_close_evidence", "transition",
        "cost_coverage", "lifecycle_reason", "reconciliation_evidence",
    }
    with sqlite3.connect(db_path) as conn:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({OBSERVATIONS_TABLE})")}
        assert not columns.intersection({"address", "comment", "rid", "order_uid", "raw_json", "price"})
        indexes = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
                (OBSERVATIONS_TABLE,),
            )
        }
        assert {
            "wb_fbs_observations_by_order",
            "wb_fbs_observations_by_supply",
            "wb_fbs_observations_by_nm",
        }.issubset(indexes)
        try:
            conn.execute(f"UPDATE {OBSERVATIONS_TABLE} SET nm_id=1 WHERE order_id=1001")
            raise AssertionError("FBS observations must be immutable")
        except sqlite3.IntegrityError:
            pass
        try:
            conn.execute(f"DELETE FROM {OBSERVATIONS_TABLE} WHERE order_id=1001")
            raise AssertionError("FBS observations must be append-only")
        except sqlite3.IntegrityError:
            pass
        assert conn.execute(f"SELECT COUNT(*) FROM {STATUS_OBSERVATIONS_TABLE}").fetchone()[0] == 3
        row = conn.execute(
            f"SELECT supplier_status,positive_quantity FROM {STATUS_OBSERVATIONS_TABLE} "
            "WHERE order_id=1001 ORDER BY observation_sequence DESC LIMIT 1"
        ).fetchone()
        assert row == ("complete", 1)
        try:
            conn.execute(f"UPDATE {STATUS_OBSERVATIONS_TABLE} SET wb_status='x' WHERE order_id=1001")
            raise AssertionError("FBS status observations must be immutable")
        except sqlite3.IntegrityError:
            pass

    readonly = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    readonly.execute("PRAGMA query_only=ON")
    before = readonly.total_changes
    collector.orders_page(search="1001")
    collector.order_detail(1001)
    assert readonly.total_changes == before == 0
    readonly.close()


def _stuck_cursor_contract(db_path: Path) -> None:
    collector = WbFbsOrdersCollector(
        db_path=db_path,
        timestamp_factory=lambda: "2026-08-12T09:03:00Z",
        unix_time_factory=lambda: 1_786_522_400,
        source=_Source(stuck=True),
        enabled=True,
    )
    try:
        collector.collect_default_window()
        raise AssertionError("repeated upstream cursor must fail closed")
    except WbFbsOrdersError as exc:
        assert exc.code == "cursor_did_not_advance"
    with sqlite3.connect(db_path) as conn:
        state = conn.execute(f"SELECT last_status,last_error FROM {STATE_TABLE}").fetchone()
        assert state[0] == "failed" and "did not advance" in state[1]


def _zero_non_target_contract(db_path: Path) -> None:
    tables = [
        FACILITIES_TABLE,
        FEATURE_EPOCHS_TABLE,
        ASSIGNMENTS_TABLE,
        "sheet_vitrina_v1_ff_pool_movement_lines",
        "sheet_vitrina_v1_ff_stock_operations",
        "sheet_vitrina_v1_warehouse_business_operations",
    ]
    with sqlite3.connect(db_path) as conn:
        for table in tables:
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def _order(
    order_id: int,
    *,
    supply_id: str = "WB-GI-9001",
    delivery_type: str = "fbs",
    warehouse_id: int = 507,
) -> dict[str, object]:
    return {
        "id": order_id,
        "supplyId": supply_id,
        "deliveryType": delivery_type,
        "createdAt": "2026-08-12T08:00:00Z",
        "warehouseId": warehouse_id,
        "officeId": 123,
        "nmId": 140557512,
        "chrtId": 987654321,
        "skus": ["0001234567890"],
        "cargoType": 1,
        "crossBorderType": 0,
        "isZeroOrder": False,
        "address": {"fullAddress": "must not persist"},
        "comment": "must not persist",
        "orderUid": "must-not-persist",
        "rid": "must-not-persist",
        "price": 1014,
    }


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    main()
