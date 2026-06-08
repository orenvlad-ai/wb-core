"""Targeted smoke-checks for WB regional stock-depletion demand methodology."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.factory_order_sales_history import persist_sales_history_result_exact_dates
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.wb_regional_demand import (
    REGIONAL_DEMAND_METHOD_STOCK_DEPLETION,
    REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK,
    STOCKS_SOURCE_KEY,
    build_result_diagnostics,
    estimate_wb_regional_demand,
)
from packages.application.wb_regional_supply import _allocate_boxes
from packages.contracts.sales_funnel_history_block import SalesFunnelHistoryItem, SalesFunnelHistorySuccess
from packages.contracts.stocks_block import StocksEnvelope, StocksItem, StocksSuccess
from packages.contracts.wb_regional_supply import (
    DISTRICT_CENTRAL,
    DISTRICT_FAR_SIBERIA,
    DISTRICT_KEYS,
    DISTRICT_NORTHWEST,
    DISTRICT_SOUTH_CAUCASUS,
    DISTRICT_URAL,
    DISTRICT_VOLGA,
)


NOW_ISO = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
NM_ID = 900001
FIELD_BY_KEY = {
    DISTRICT_CENTRAL: "stock_ru_central",
    DISTRICT_NORTHWEST: "stock_ru_northwest",
    DISTRICT_VOLGA: "stock_ru_volga",
    DISTRICT_URAL: "stock_ru_ural",
    DISTRICT_SOUTH_CAUCASUS: "stock_ru_south_caucasus",
    DISTRICT_FAR_SIBERIA: "stock_ru_far_siberia",
}


def main() -> None:
    _assert_clean_14_days()
    _assert_depletion_total_differs_from_order_count()
    _assert_refill_day_is_excluded_and_lookup_walks_back()
    _assert_out_of_stock_day_is_excluded()
    _assert_zero_depletion_district_stays_present()
    _assert_far_siberia_no_depletion_stays_present()
    _assert_far_siberia_exclusion_changes_validation()
    _assert_district_selection_validation_errors()
    _assert_fallback_uses_selected_district_stock_share()
    _assert_insufficient_history_uses_explicit_fallback()
    _assert_allocation_prefers_marginal_saved_units()
    _assert_allocation_tie_breaks()
    _assert_allocation_ff_enough_equals_full_recommendation()
    print("wb_regional_demand_methodology: ok")
    print("wb_regional_allocation_tail: ok")


def _assert_clean_14_days() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=100.0,
            depletion_by_key={
                DISTRICT_CENTRAL: 10.0,
                DISTRICT_NORTHWEST: 20.0,
                DISTRICT_VOLGA: 0.0,
                DISTRICT_URAL: 30.0,
                DISTRICT_SOUTH_CAUCASUS: 40.0,
                DISTRICT_FAR_SIBERIA: 0.0,
            },
        )
        estimate = _estimate(runtime, report_date)
        _assert_method(estimate, REGIONAL_DEMAND_METHOD_STOCK_DEPLETION)
        _assert_close(estimate.daily_demand_total, 100.0, "daily demand must use orderCount")
        shares = estimate.average_depletion_share_by_district
        _assert_close(shares[DISTRICT_CENTRAL], 0.1, "central share")
        _assert_close(shares[DISTRICT_NORTHWEST], 0.2, "northwest share")
        _assert_close(shares[DISTRICT_VOLGA], 0.0, "volga zero-depletion share")
        _assert_close(shares[DISTRICT_URAL], 0.3, "ural share")
        _assert_close(shares[DISTRICT_SOUTH_CAUCASUS], 0.4, "south share")
        _assert_close(shares[DISTRICT_FAR_SIBERIA], 0.0, "far/siberia zero-depletion share")
        if estimate.diagnostics.get("selected_valid_day_count") != 14:
            raise AssertionError("clean window must select exactly 14 valid depletion days")
        if estimate.diagnostics.get("initial_window_valid_day_count") != 14:
            raise AssertionError("clean window must not need backward lookup")
        result_diagnostics = build_result_diagnostics({NM_ID: estimate})
        if result_diagnostics.get("warnings"):
            raise AssertionError(f"clean stock-depletion window must not emit warnings, got {result_diagnostics}")


def _assert_depletion_total_differs_from_order_count() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=100.0,
            depletion_by_key={
                DISTRICT_CENTRAL: 30.0,
                DISTRICT_NORTHWEST: 90.0,
                DISTRICT_VOLGA: 0.0,
                DISTRICT_URAL: 0.0,
                DISTRICT_SOUTH_CAUCASUS: 0.0,
                DISTRICT_FAR_SIBERIA: 0.0,
            },
        )
        estimate = _estimate(runtime, report_date)
        _assert_close(estimate.daily_demand_total, 100.0, "orderCount must remain total demand when depletion is 120")
        _assert_close(estimate.district_daily_demand_by_key[DISTRICT_CENTRAL], 25.0, "central demand must use depletion share")
        _assert_close(estimate.district_daily_demand_by_key[DISTRICT_NORTHWEST], 75.0, "northwest demand must use depletion share")


def _assert_refill_day_is_excluded_and_lookup_walks_back() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 20)
        dirty_date = date(2026, 5, 10)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=100.0,
            depletion_by_key={key: (10.0 if key == DISTRICT_CENTRAL else 0.0) for key in DISTRICT_KEYS},
            extra_older_days=1,
            overrides={dirty_date: {DISTRICT_CENTRAL: -5.0}},
        )
        estimate = _estimate(runtime, report_date)
        _assert_method(estimate, REGIONAL_DEMAND_METHOD_STOCK_DEPLETION)
        diagnostics = estimate.diagnostics
        if diagnostics.get("selected_valid_day_count") != 14:
            raise AssertionError("refill exclusion must still collect 14 valid days")
        if diagnostics.get("excluded_day_reason_counts", {}).get("district_restock_or_upward_correction") != 1:
            raise AssertionError(f"refill day must be excluded, got {diagnostics}")
        if diagnostics.get("initial_window_valid_day_count") != 13:
            raise AssertionError("dirty initial window must be visible in diagnostics")


def _assert_out_of_stock_day_is_excluded() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 20)
        oos_date = date(2026, 5, 10)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=100.0,
            depletion_by_key={key: (10.0 if key in {DISTRICT_CENTRAL, DISTRICT_NORTHWEST} else 0.0) for key in DISTRICT_KEYS},
            extra_older_days=2,
            overrides={
                oos_date: {DISTRICT_CENTRAL: 999999.0},
                oos_date + timedelta(days=1): {DISTRICT_CENTRAL: -5000.0},
            },
        )
        estimate = _estimate(runtime, report_date)
        diagnostics = estimate.diagnostics
        if diagnostics.get("excluded_day_reason_counts", {}).get("district_out_of_stock_risk") != 1:
            raise AssertionError(f"OOS day must be excluded, got {diagnostics}")
        if diagnostics.get("selected_valid_day_count") != 14:
            raise AssertionError("OOS exclusion must walk backward to 14 valid days")


def _assert_zero_depletion_district_stays_present() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=80.0,
            depletion_by_key={
                DISTRICT_CENTRAL: 20.0,
                DISTRICT_NORTHWEST: 20.0,
                DISTRICT_VOLGA: 0.0,
                DISTRICT_URAL: 20.0,
                DISTRICT_SOUTH_CAUCASUS: 20.0,
                DISTRICT_FAR_SIBERIA: 0.0,
            },
        )
        estimate = _estimate(runtime, report_date)
        if DISTRICT_VOLGA not in estimate.average_depletion_share_by_district:
            raise AssertionError("zero-depletion district must stay in share map")
        _assert_close(estimate.average_depletion_share_by_district[DISTRICT_VOLGA], 0.0, "zero-depletion district share")


def _assert_far_siberia_no_depletion_stays_present() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=70.0,
            depletion_by_key={
                DISTRICT_CENTRAL: 35.0,
                DISTRICT_NORTHWEST: 35.0,
                DISTRICT_VOLGA: 0.0,
                DISTRICT_URAL: 0.0,
                DISTRICT_SOUTH_CAUCASUS: 0.0,
                DISTRICT_FAR_SIBERIA: 0.0,
            },
        )
        estimate = _estimate(runtime, report_date)
        if DISTRICT_FAR_SIBERIA not in estimate.district_daily_demand_by_key:
            raise AssertionError("far/siberia district must not be removed")
        _assert_close(estimate.district_daily_demand_by_key[DISTRICT_FAR_SIBERIA], 0.0, "far/siberia no-depletion demand")


def _assert_far_siberia_exclusion_changes_validation() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=14,
            order_count=70.0,
            depletion_by_key={
                DISTRICT_CENTRAL: 20.0,
                DISTRICT_NORTHWEST: 20.0,
                DISTRICT_VOLGA: 0.0,
                DISTRICT_URAL: 0.0,
                DISTRICT_SOUTH_CAUCASUS: 0.0,
                DISTRICT_FAR_SIBERIA: 999999.0,
            },
        )
        included = _estimate(runtime, report_date)
        _assert_method(included, REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK)
        if "district_out_of_stock_risk" not in included.diagnostics.get("excluded_day_reason_counts", {}):
            raise AssertionError(f"far/siberia OOS must invalidate included-district days, got {included.diagnostics}")

        excluded = _estimate(
            runtime,
            report_date,
            included_district_keys=tuple(key for key in DISTRICT_KEYS if key != DISTRICT_FAR_SIBERIA),
        )
        _assert_method(excluded, REGIONAL_DEMAND_METHOD_STOCK_DEPLETION)
        if excluded.diagnostics.get("selected_valid_day_count") != 14:
            raise AssertionError(f"excluding far/siberia must collect clean days, got {excluded.diagnostics}")
        _assert_close(excluded.average_depletion_share_by_district[DISTRICT_FAR_SIBERIA], 0.0, "excluded district share")
        _assert_close(
            sum(excluded.average_depletion_share_by_district[key] for key in DISTRICT_KEYS if key != DISTRICT_FAR_SIBERIA),
            1.0,
            "included shares must normalize to 1",
        )
        if excluded.diagnostics.get("excluded_district_keys") != [DISTRICT_FAR_SIBERIA]:
            raise AssertionError("diagnostics must expose excluded far/siberia district")


def _assert_district_selection_validation_errors() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=1,
            order_count=10.0,
            depletion_by_key={key: (1.0 if key == DISTRICT_CENTRAL else 0.0) for key in DISTRICT_KEYS},
        )
        try:
            _estimate(runtime, report_date, included_district_keys=())
        except ValueError as exc:
            if "Выберите хотя бы один округ" not in str(exc):
                raise AssertionError(f"empty district selection must return clear error, got {exc}") from exc
        else:
            raise AssertionError("empty district selection must be rejected")
        try:
            _estimate(runtime, report_date, included_district_keys=("central", "unknown"))
        except ValueError as exc:
            if "unknown" not in str(exc):
                raise AssertionError(f"invalid district key must be named, got {exc}") from exc
        else:
            raise AssertionError("invalid district key must be rejected")


def _assert_fallback_uses_selected_district_stock_share() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=7,
            order_count=100.0,
            depletion_by_key={key: (10.0 if key == DISTRICT_CENTRAL else 0.0) for key in DISTRICT_KEYS},
        )
        estimate = _estimate(
            runtime,
            report_date,
            requested_valid_day_count=14,
            included_district_keys=(DISTRICT_CENTRAL, DISTRICT_NORTHWEST),
            current_stock_by_key={
                DISTRICT_CENTRAL: 10.0,
                DISTRICT_NORTHWEST: 30.0,
                DISTRICT_VOLGA: 1000.0,
                DISTRICT_URAL: 1000.0,
                DISTRICT_SOUTH_CAUCASUS: 1000.0,
                DISTRICT_FAR_SIBERIA: 1000.0,
            },
        )
        _assert_method(estimate, REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK)
        _assert_close(estimate.average_depletion_share_by_district[DISTRICT_CENTRAL], 0.25, "selected fallback central share")
        _assert_close(estimate.average_depletion_share_by_district[DISTRICT_NORTHWEST], 0.75, "selected fallback northwest share")
        for district_key in (DISTRICT_VOLGA, DISTRICT_URAL, DISTRICT_SOUTH_CAUCASUS, DISTRICT_FAR_SIBERIA):
            _assert_close(estimate.average_depletion_share_by_district[district_key], 0.0, f"excluded fallback share {district_key}")


def _assert_insufficient_history_uses_explicit_fallback() -> None:
    with _runtime() as runtime:
        report_date = date(2026, 5, 1)
        _seed_history(
            runtime,
            report_date=report_date,
            requested_days=7,
            order_count=100.0,
            depletion_by_key={key: (10.0 if key == DISTRICT_CENTRAL else 0.0) for key in DISTRICT_KEYS},
        )
        estimate = _estimate(
            runtime,
            report_date,
            requested_valid_day_count=14,
            current_stock_by_key={
                DISTRICT_CENTRAL: 10.0,
                DISTRICT_NORTHWEST: 30.0,
                DISTRICT_VOLGA: 0.0,
                DISTRICT_URAL: 0.0,
                DISTRICT_SOUTH_CAUCASUS: 0.0,
                DISTRICT_FAR_SIBERIA: 0.0,
            },
        )
        _assert_method(estimate, REGIONAL_DEMAND_METHOD_STOCK_SHARE_FALLBACK)
        if not estimate.diagnostics.get("fallback_used"):
            raise AssertionError("insufficient history must use explicit fallback")
        if "fallback" not in estimate.warning:
            raise AssertionError("fallback must produce a human-readable warning")
        _assert_close(estimate.average_depletion_share_by_district[DISTRICT_CENTRAL], 0.25, "fallback central stock share")
        _assert_close(estimate.average_depletion_share_by_district[DISTRICT_NORTHWEST], 0.75, "fallback northwest stock share")


def _assert_allocation_prefers_marginal_saved_units() -> None:
    allocation = _allocate_boxes(
        full_recommendation_by_key={
            DISTRICT_CENTRAL: 50,
            DISTRICT_NORTHWEST: 50,
            DISTRICT_VOLGA: 0,
            DISTRICT_URAL: 0,
            DISTRICT_SOUTH_CAUCASUS: 0,
            DISTRICT_FAR_SIBERIA: 0,
        },
        raw_recommendation_by_key={
            DISTRICT_CENTRAL: 5.0,
            DISTRICT_NORTHWEST: 50.0,
            DISTRICT_VOLGA: 0.0,
            DISTRICT_URAL: 0.0,
            DISTRICT_SOUTH_CAUCASUS: 0.0,
            DISTRICT_FAR_SIBERIA: 0.0,
        },
        district_daily_demand_by_key={key: 10.0 for key in DISTRICT_KEYS},
        projected_stock_by_key={key: 0.0 for key in DISTRICT_KEYS},
        available_stock_ff=50.0,
        order_batch_qty=50,
    )
    if allocation[DISTRICT_NORTHWEST] != 50 or allocation[DISTRICT_CENTRAL] != 0:
        raise AssertionError(f"next box must go to the district with higher rescued units, got {allocation}")


def _assert_allocation_tie_breaks() -> None:
    lower_coverage = _allocate_boxes(
        full_recommendation_by_key=_two_district_recommendation(50, 50),
        raw_recommendation_by_key=_two_district_raw(50.0, 50.0),
        district_daily_demand_by_key={key: 10.0 for key in DISTRICT_KEYS},
        projected_stock_by_key={**{key: 0.0 for key in DISTRICT_KEYS}, DISTRICT_CENTRAL: 100.0},
        available_stock_ff=50.0,
        order_batch_qty=50,
    )
    if lower_coverage[DISTRICT_NORTHWEST] != 50:
        raise AssertionError(f"lower coverage tie-break must win, got {lower_coverage}")

    higher_demand = _allocate_boxes(
        full_recommendation_by_key=_two_district_recommendation(50, 50),
        raw_recommendation_by_key=_two_district_raw(50.0, 50.0),
        district_daily_demand_by_key={**{key: 0.0 for key in DISTRICT_KEYS}, DISTRICT_CENTRAL: 5.0, DISTRICT_NORTHWEST: 10.0},
        projected_stock_by_key={key: 0.0 for key in DISTRICT_KEYS},
        available_stock_ff=50.0,
        order_batch_qty=50,
    )
    if higher_demand[DISTRICT_NORTHWEST] != 50:
        raise AssertionError(f"higher demand tie-break must win, got {higher_demand}")

    stable_order = _allocate_boxes(
        full_recommendation_by_key=_two_district_recommendation(50, 50),
        raw_recommendation_by_key=_two_district_raw(50.0, 50.0),
        district_daily_demand_by_key={key: 10.0 for key in DISTRICT_KEYS},
        projected_stock_by_key={key: 0.0 for key in DISTRICT_KEYS},
        available_stock_ff=50.0,
        order_batch_qty=50,
    )
    if stable_order[DISTRICT_CENTRAL] != 50:
        raise AssertionError(f"stable district order must break exact ties, got {stable_order}")


def _assert_allocation_ff_enough_equals_full_recommendation() -> None:
    full = _two_district_recommendation(50, 100)
    allocation = _allocate_boxes(
        full_recommendation_by_key=full,
        raw_recommendation_by_key=_two_district_raw(40.0, 90.0),
        district_daily_demand_by_key={key: 10.0 for key in DISTRICT_KEYS},
        projected_stock_by_key={key: 0.0 for key in DISTRICT_KEYS},
        available_stock_ff=150.0,
        order_batch_qty=50,
    )
    if allocation != full:
        raise AssertionError(f"when FF is enough, allocation must equal full recommendation, got {allocation}")


def _estimate(
    runtime: RegistryUploadDbBackedRuntime,
    report_date: date,
    *,
    requested_valid_day_count: int = 14,
    current_stock_by_key: dict[str, float] | None = None,
    included_district_keys: tuple[str, ...] | None = None,
):
    estimates = estimate_wb_regional_demand(
        runtime=runtime,
        report_date=report_date,
        nm_ids=[NM_ID],
        requested_valid_day_count=requested_valid_day_count,
        district_field_by_key=FIELD_BY_KEY,
        current_stock_by_nm={NM_ID: current_stock_by_key or {key: 100.0 for key in DISTRICT_KEYS}},
        included_district_keys=included_district_keys,
    )
    return estimates[NM_ID]


def _seed_history(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    report_date: date,
    requested_days: int,
    order_count: float,
    depletion_by_key: dict[str, float],
    extra_older_days: int = 0,
    overrides: dict[date, dict[str, float]] | None = None,
) -> None:
    overrides = overrides or {}
    first_depletion_date = report_date - timedelta(days=requested_days + extra_older_days)
    last_depletion_date = report_date - timedelta(days=1)
    snapshot_dates = [
        first_depletion_date - timedelta(days=1) + timedelta(days=offset)
        for offset in range((last_depletion_date - first_depletion_date).days + 2)
    ]
    stock_by_key = {
        DISTRICT_CENTRAL: 5000.0,
        DISTRICT_NORTHWEST: 5000.0,
        DISTRICT_VOLGA: 5000.0,
        DISTRICT_URAL: 5000.0,
        DISTRICT_SOUTH_CAUCASUS: 5000.0,
        DISTRICT_FAR_SIBERIA: 5000.0,
    }
    _save_stock_snapshot(runtime, snapshot_dates[0], stock_by_key)
    sales_items: list[SalesFunnelHistoryItem] = []
    for depletion_date in snapshot_dates[1:]:
        day_depletion = dict(depletion_by_key)
        day_depletion.update(overrides.get(depletion_date, {}))
        for key in DISTRICT_KEYS:
            stock_by_key[key] = max(stock_by_key[key] - day_depletion.get(key, 0.0), 0.0)
        _save_stock_snapshot(runtime, depletion_date, stock_by_key)
        sales_items.append(
            SalesFunnelHistoryItem(
                date=depletion_date.isoformat(),
                nm_id=NM_ID,
                metric="orderCount",
                value=float(order_count),
            )
        )
    persist_sales_history_result_exact_dates(
        runtime=runtime,
        payload=SalesFunnelHistorySuccess(
            kind="success",
            date_from=first_depletion_date.isoformat(),
            date_to=last_depletion_date.isoformat(),
            count=len(sales_items),
            items=sales_items,
        ),
        captured_at=NOW_ISO,
    )


def _save_stock_snapshot(runtime: RegistryUploadDbBackedRuntime, snapshot_date: date, stock_by_key: dict[str, float]) -> None:
    item = StocksItem(
        nm_id=NM_ID,
        stock_total=sum(float(stock_by_key[key]) for key in DISTRICT_KEYS),
        stock_ru_central=float(stock_by_key[DISTRICT_CENTRAL]),
        stock_ru_northwest=float(stock_by_key[DISTRICT_NORTHWEST]),
        stock_ru_volga=float(stock_by_key[DISTRICT_VOLGA]),
        stock_ru_ural=float(stock_by_key[DISTRICT_URAL]),
        stock_ru_south_caucasus=float(stock_by_key[DISTRICT_SOUTH_CAUCASUS]),
        stock_ru_far_siberia=float(stock_by_key[DISTRICT_FAR_SIBERIA]),
    )
    runtime.save_temporal_source_snapshot(
        source_key=STOCKS_SOURCE_KEY,
        snapshot_date=snapshot_date.isoformat(),
        captured_at=NOW_ISO,
        payload=StocksEnvelope(
            result=StocksSuccess(
                kind="success",
                snapshot_date=snapshot_date.isoformat(),
                count=1,
                items=[item],
            )
        ),
    )


def _two_district_recommendation(central: int, northwest: int) -> dict[str, int]:
    return {
        DISTRICT_CENTRAL: central,
        DISTRICT_NORTHWEST: northwest,
        DISTRICT_VOLGA: 0,
        DISTRICT_URAL: 0,
        DISTRICT_SOUTH_CAUCASUS: 0,
        DISTRICT_FAR_SIBERIA: 0,
    }


def _two_district_raw(central: float, northwest: float) -> dict[str, float]:
    return {
        DISTRICT_CENTRAL: central,
        DISTRICT_NORTHWEST: northwest,
        DISTRICT_VOLGA: 0.0,
        DISTRICT_URAL: 0.0,
        DISTRICT_SOUTH_CAUCASUS: 0.0,
        DISTRICT_FAR_SIBERIA: 0.0,
    }


def _assert_method(estimate: object, expected: str) -> None:
    diagnostics = getattr(estimate, "diagnostics", {})
    actual = diagnostics.get("regional_demand_method")
    if actual != expected:
        raise AssertionError(f"expected method {expected}, got {actual}: {diagnostics}")


def _assert_close(actual: float, expected: float, label: str) -> None:
    if abs(float(actual) - float(expected)) > 1e-9:
        raise AssertionError(f"{label}: expected {expected}, got {actual}")


class _runtime:
    def __enter__(self) -> RegistryUploadDbBackedRuntime:
        self._tmp = TemporaryDirectory(prefix="wb-regional-demand-")
        return RegistryUploadDbBackedRuntime(runtime_dir=Path(self._tmp.name) / "runtime")

    def __exit__(self, exc_type, exc, tb) -> None:
        self._tmp.cleanup()


if __name__ == "__main__":
    main()
