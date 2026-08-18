"""Targeted acceptance smoke for immutable Proxy V4 parameters and formula."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import (  # noqa: E402
    DEFAULT_PROXY_PARAMETERS,
    calculate_proxy_3,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    PROXY_V4_FIXED_BOUNDARY,
    ProxyV4ParametersBlock,
    aggregate_proxy_4,
    build_confirmed_aligned_window,
    build_latest_confirmed_week_window,
    calculate_proxy_4,
    ensure_proxy_v4_schema,
    plan_initial_historical_versions,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_proxy_v4 import (  # noqa: E402
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    extend_metrics_with_proxy_v4,
)
from packages.application.wb_finance_weekly import (  # noqa: E402
    CLASSIFIER_VERSION as WB_FINANCE_CLASSIFIER_VERSION,
)
from packages.application.warehouse_sync_lock import WarehouseSyncBusyError  # noqa: E402
import packages.application.calculation_parameters_v4 as calculation_parameters_v4  # noqa: E402
from packages.contracts.sales_funnel_history_block import (  # noqa: E402
    SalesFunnelHistoryItem,
    SalesFunnelHistorySuccess,
)


BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 8, 9, 8, 0, tzinfo=timezone.utc)


def main() -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-proxy-v4-") as temp_dir:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(temp_dir))
        accepted = runtime.ingest_bundle(
            json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8")),
            activated_at="2026-08-01T00:00:00Z",
        )
        if accepted.status != "accepted":
            raise AssertionError(f"fixture ingest failed: {accepted}")
        enabled_nm_ids = [
            item.nm_id for item in runtime.load_current_state().config_v2 if item.enabled
        ]
        _ensure_finance_tables(runtime.db_path)
        for week_start, buyout, first_loaded_at in (
            ("2026-07-06", Decimal("0.70"), "2026-07-13T07:00:00Z"),
            ("2026-07-13", Decimal("0.80"), "2026-07-20T07:00:00Z"),
            ("2026-07-20", Decimal("0.90"), "2026-07-27T07:00:00Z"),
            ("2026-07-27", Decimal("1.00"), "2026-08-03T07:00:00Z"),
        ):
            _save_buyout_week(runtime, week_start, enabled_nm_ids, buyout)
            _save_finance_week(runtime.db_path, week_start, first_loaded_at)

        first_window = build_confirmed_aligned_window(
            runtime=runtime,
            today=date(2026, 8, 1),
            finance_available_by="2026-08-01",
        )
        if first_window["status"] != "ready":
            raise AssertionError(f"first aligned window must be ready: {first_window}")
        rates = first_window["automatic_rates"]
        expected = {
            "buyout_rate": "0.8",
            "agent_remuneration_rate": "0.1",
            "acquiring_rate": "0.02",
            "wb_logistics_rate": "0.03",
            "wb_storage_rate": "0.01",
            "penalties_adjustments_rate": "0.007",
            "other_expense_rate": "0.014",
        }
        if rates != expected:
            raise AssertionError(f"direct SUM/SUM or V4 expense composition drifted: {rates}")
        expected_three_week_buyout_weight = Decimal(len(enabled_nm_ids) * 21 * 10)
        if (
            first_window["source_week_ranges"]
            != [
                ["2026-07-06", "2026-07-12"],
                ["2026-07-13", "2026-07-19"],
                ["2026-07-20", "2026-07-26"],
            ]
            or Decimal(first_window["aligned_buyout"]["order_count_weight"])
            != expected_three_week_buyout_weight
            or Decimal(first_window["aligned_finance"]["net_revenue"]) != Decimal("3000")
        ):
            raise AssertionError(f"aligned source ranges/denominators are not exact: {first_window}")
        excluded = first_window["finance"]["composition"]["excluded"]
        if not {
            "marketing",
            "transit_logistics",
            "capitalized_transit_logistics",
        }.issubset(excluded):
            raise AssertionError("marketing and both transit fields must stay outside included_expense_rate")
        if first_window["aligned_finance"]["excluded_amounts"] != {
            "transit_logistics": "24",
            "capitalized_transit_logistics": "9",
        }:
            raise AssertionError("excluded transit diagnostics must remain visible and signed")

        versions = plan_initial_historical_versions(
            runtime=runtime,
            tax_rate_resolver=lambda _day: Decimal("0.06"),
        )
        if [item.effective_date for item in versions] != ["2026-08-01", "2026-08-08"]:
            raise AssertionError(f"historical effective dates drifted: {versions}")
        if [item.buyout_rate for item in versions] != [Decimal("0.8"), Decimal("0.9")]:
            raise AssertionError("future confirmed buyout leaked backwards")
        if (
            versions[0].source_week_ranges
            != (
                ("2026-07-06", "2026-07-12"),
                ("2026-07-13", "2026-07-19"),
                ("2026-07-20", "2026-07-26"),
            )
            or versions[1].source_week_ranges
            != (
                ("2026-07-13", "2026-07-19"),
                ("2026-07-20", "2026-07-26"),
                ("2026-07-27", "2026-08-02"),
            )
        ):
            raise AssertionError("historical versions did not freeze their exact as-of weeks")
        _install_historical_versions(runtime.db_path, versions)

        block = ProxyV4ParametersBlock(runtime=runtime, now_factory=lambda: NOW)
        if block.parameters_for_date("2026-07-31") is not None:
            raise AssertionError("V4 must be blank before the fixed 2026-08-01 boundary")
        pre_boundary_rollover = block.materialize_latest_confirmed_window(
            business_date="2026-07-31"
        )
        if pre_boundary_rollover["status"] != "pre_boundary" or pre_boundary_rollover["created"]:
            raise AssertionError(f"V4 rollover crossed its fixed product boundary: {pre_boundary_rollover}")
        if block.parameters_for_date("2026-08-07").buyout_rate != Decimal("0.8"):  # type: ignore[union-attr]
            raise AssertionError("first frozen as-of version did not remain effective")
        if block.parameters_for_date("2026-08-08").buyout_rate != Decimal("0.9"):  # type: ignore[union-attr]
            raise AssertionError("second aligned window was not selected on its boundary")
        rollover = block.materialize_latest_confirmed_window(business_date="2026-08-09")
        if (
            rollover["status"] != "materialized"
            or not rollover["created"]
            or rollover["window"]["source_week_ranges"]
            != [["2026-07-27", "2026-08-02"]]
            or rollover["window"]["automatic_rates"]["buyout_rate"] != "1"
        ):
            raise AssertionError(
                "legacy combined revision must transition prospectively to one latest "
                f"week: {rollover}"
            )
        if block.parameters_for_date("2026-08-08").buyout_rate != Decimal("0.9"):  # type: ignore[union-attr]
            raise AssertionError("same-day selection transition rewrote a prior V4 date")
        if block.parameters_for_date("2026-08-09").buyout_rate != Decimal("1"):  # type: ignore[union-attr]
            raise AssertionError("higher current-day revision did not become effective immediately")
        repeated_transition = block.materialize_latest_confirmed_window(
            business_date="2026-08-09"
        )
        if (
            repeated_transition["status"] != "already_materialized"
            or repeated_transition["created"]
        ):
            raise AssertionError(
                f"same latest-week fingerprint must be idempotent: {repeated_transition}"
            )
        transitioned_payload = block.get_payload()
        if (
            transitioned_payload["formula_input_policy"]
            != "latest_confirmed_common_week"
            or transitioned_payload["current"]["parameters"]["source_selection_mode"]
            != "latest_confirmed_week"
            or transitioned_payload["history"][-1]["parameters"]["source_selection_mode"]
            != "frozen_legacy_multi_week"
        ):
            raise AssertionError(
                f"latest-week/current and frozen legacy metadata are ambiguous: {transitioned_payload}"
            )
        version_count = len(transitioned_payload["history"])
        _save_buyout_week(runtime, "2026-07-27", enabled_nm_ids, Decimal("0.99"))
        repaired_source = block.materialize_latest_confirmed_window(business_date="2026-08-09")
        if repaired_source["status"] != "historical_repair_required" or repaired_source["created"]:
            raise AssertionError(
                f"ordinary rollover must not rewrite a frozen repaired source range: {repaired_source}"
            )
        if len(block.get_payload()["history"]) != version_count:
            raise AssertionError("source repair drift created an ordinary V4 revision")
        _save_buyout_week(runtime, "2026-07-27", enabled_nm_ids, Decimal("1.00"))

        parameters = block.parameters_for_date("2026-08-08")
        calculated = calculate_proxy_4(
            order_sum=Decimal("1000"),
            order_count=Decimal("10"),
            canonical_wb_wac=Decimal("20"),
            ads_sum=Decimal("30"),
            parameters=parameters,
            business_date="2026-08-08",
        )
        expected_profit = Decimal("1000") * Decimal("0.9") * (Decimal("1") - Decimal("0.241")) - Decimal("10") * Decimal("0.9") * Decimal("20") - Decimal("30")
        if calculated["proxy_profit_4"] != expected_profit:
            raise AssertionError(f"Proxy V4 formula drifted: {calculated}")
        before_boundary = calculate_proxy_4(
            order_sum=1000,
            order_count=10,
            canonical_wb_wac=20,
            ads_sum=30,
            parameters=parameters,
            business_date="2026-07-31",
        )
        missing = calculate_proxy_4(
            order_sum=1000,
            order_count=10,
            canonical_wb_wac=None,
            ads_sum=30,
            parameters=parameters,
            business_date="2026-08-08",
        )
        if before_boundary["proxy_profit_4"] is not None or missing["proxy_profit_4"] is not None:
            raise AssertionError("boundary or missing operand must remain blank")
        total = aggregate_proxy_4(
            [
                calculated,
                {"proxy_profit_4": Decimal("100"), "expected_buyout_revenue": Decimal("200")},
                {"proxy_profit_4": None, "expected_buyout_revenue": None},
            ]
        )
        if total["proxy_profit_4"] != expected_profit + Decimal("100"):
            raise AssertionError("TOTAL must sum eligible SKU profits")
        if total["proxy_margin_4"] != (
            expected_profit + Decimal("100")
        ) / (Decimal("900") + Decimal("200")):
            raise AssertionError("TOTAL margin must divide summed profit by summed expected revenue")

        preview = block.preview_tax_version({"tax_rate": "0.07"})
        saved = block.create_tax_version(
            {"tax_rate": "0.07"},
            preview_fingerprint=str(preview["preview_fingerprint"]),
            created_by="smoke",
        )
        if not saved["created_version_id"] or block.parameters_for_date("2026-08-09").tax_rate != Decimal("0.07"):  # type: ignore[union-attr]
            raise AssertionError("manual V4 tax must create a current-business-date version")
        if block.parameters_for_date("2026-08-09").buyout_rate != Decimal("1"):  # type: ignore[union-attr]
            raise AssertionError("manual tax revision changed the selected automatic week")
        if block.parameters_for_date("2026-08-08").tax_rate != Decimal("0.06"):  # type: ignore[union-attr]
            raise AssertionError("manual tax change rewrote frozen history")
        next_business_day = ProxyV4ParametersBlock(
            runtime=runtime,
            now_factory=lambda: datetime(2026, 8, 9, 20, 30, tzinfo=timezone.utc),
        ).preview_tax_version({"tax_rate": "0.08"})
        if next_business_day["effective_date"] != "2026-08-10":
            raise AssertionError(
                f"V4 tax effective date must use Asia/Yekaterinburg boundary: {next_business_day}"
            )

        two_ready = build_confirmed_aligned_window(runtime=runtime, today=date(2026, 8, 15))
        if (
            two_ready["status"] != "ready"
            or two_ready["ready_week_count"] != 2
            or two_ready["source_week_ranges"]
            != [["2026-07-20", "2026-07-26"], ["2026-07-27", "2026-08-02"]]
            or two_ready["automatic_rates"]["buyout_rate"] != "0.95"
            or Decimal(two_ready["aligned_buyout"]["order_count_weight"])
            != Decimal(len(enabled_nm_ids) * 14 * 10)
            or two_ready["aligned_finance"]["net_revenue"] != "2000"
        ):
            raise AssertionError(f"missing latest week must yield exact 2-of-3 intersection: {two_ready}")
        latest_from_two = build_latest_confirmed_week_window(
            runtime=runtime,
            today=date(2026, 8, 15),
        )
        if (
            latest_from_two["status"] != "ready"
            or latest_from_two["common_ready_week_count"] != 2
            or latest_from_two["source_week_ranges"]
            != [["2026-07-27", "2026-08-02"]]
            or latest_from_two["automatic_rates"]["buyout_rate"] != "1"
            or Decimal(latest_from_two["aligned_buyout"]["order_count_weight"])
            != Decimal(len(enabled_nm_ids) * 7 * 10)
            or latest_from_two["aligned_finance"]["net_revenue"] != "1000"
        ):
            raise AssertionError(
                f"formula must select the freshest one of two common READY weeks: {latest_from_two}"
            )
        two_ready_rollover = block.materialize_latest_confirmed_window(business_date="2026-08-15")
        if two_ready_rollover["status"] != "already_materialized" or two_ready_rollover["created"]:
            raise AssertionError(
                "missing newest slot must retain the already selected freshest READY "
                f"week: {two_ready_rollover}"
            )
        if block.materialize_latest_confirmed_window(business_date="2026-08-15")["created"]:
            raise AssertionError("same 2-of-3 source fingerprint was not idempotent")

        _save_buyout_week(runtime, "2026-08-03", enabled_nm_ids, Decimal("0.95"))
        _save_finance_week(runtime.db_path, "2026-08-03", "2026-08-10T07:00:00Z")
        original_lock = calculation_parameters_v4.warehouse_sync_lock

        @contextmanager
        def _busy_lock(_runtime_dir: Path, *, blocking: bool = True):
            del blocking
            raise WarehouseSyncBusyError("warehouse lock is held")
            yield

        calculation_parameters_v4.warehouse_sync_lock = _busy_lock
        pre_advance_count = len(block.get_payload()["history"])
        try:
            lock_busy = block.materialize_latest_confirmed_window(business_date="2026-08-15")
        finally:
            calculation_parameters_v4.warehouse_sync_lock = original_lock
        if lock_busy["status"] != "pending_lock_busy" or lock_busy["created"]:
            raise AssertionError(f"busy rollover must retain the prior immutable version: {lock_busy}")
        advanced = block.materialize_latest_confirmed_window(business_date="2026-08-15")
        if (
            advanced["status"] != "materialized"
            or not advanced["created"]
            or advanced["window"]["source_week_ranges"]
            != [["2026-08-03", "2026-08-09"]]
            or advanced["window"]["automatic_rates"]["buyout_rate"] != "0.95"
            or len(block.get_payload()["history"]) != pre_advance_count + 1
        ):
            raise AssertionError(f"arrival of third READY week must create one version: {advanced}")
        repeated = block.materialize_latest_confirmed_window(business_date="2026-08-15")
        if repeated["status"] != "already_materialized" or repeated["created"]:
            raise AssertionError("same-day next-window rollover was not idempotent")
        if block.parameters_for_date("2026-08-15").tax_rate != Decimal("0.07"):  # type: ignore[union-attr]
            raise AssertionError("automatic rollover must carry the latest manual tax")
        if block.parameters_for_date("2026-08-09").buyout_rate != Decimal("1"):  # type: ignore[union-attr]
            raise AssertionError("later latest-week rollover rewrote Aug 9 history")

        _delete_finance_week(runtime.db_path, "2026-08-03")
        _delete_finance_week(runtime.db_path, "2026-07-27")
        one_ready = build_confirmed_aligned_window(runtime=runtime, today=date(2026, 8, 15))
        if (
            one_ready["status"] != "ready"
            or one_ready["ready_week_count"] != 1
            or one_ready["source_week_ranges"] != [["2026-07-20", "2026-07-26"]]
            or one_ready["automatic_rates"]["buyout_rate"] != "0.9"
            or one_ready["aligned_finance"]["net_revenue"] != "1000"
        ):
            raise AssertionError(f"1-of-3 aligned direct SUM/SUM failed: {one_ready}")
        retained_history_count = len(block.get_payload()["history"])
        shrunk = block.materialize_latest_confirmed_window(business_date="2026-08-15")
        if shrunk["status"] != "stale" or shrunk["created"]:
            raise AssertionError(f"same-slot source loss must retain the last version: {shrunk}")

        _delete_finance_week(runtime.db_path, "2026-07-20")
        zero_ready = block.materialize_latest_confirmed_window(business_date="2026-08-15")
        if zero_ready["status"] != "stale" or zero_ready["created"]:
            raise AssertionError(f"zero READY weeks must retain the last version: {zero_ready}")
        if len(block.get_payload()["history"]) != retained_history_count:
            raise AssertionError("zero-ready fallback created a blank or zero V4 version")
        if block.get_payload()["status"] != "stale":
            raise AssertionError("zero-ready Settings status did not expose last-version fallback")

        _save_finance_week(
            runtime.db_path,
            "2026-07-20",
            "2026-07-27T07:00:00Z",
            metrics=_zero_finance_metrics(),
        )
        _save_buyout_week(runtime, "2026-07-20", enabled_nm_ids, Decimal("0"))
        proven_zero = build_confirmed_aligned_window(runtime=runtime, today=date(2026, 8, 15))
        if (
            proven_zero["status"] != "ready"
            or proven_zero["ready_week_count"] != 1
            or proven_zero["automatic_rates"]["buyout_rate"] != "0"
            or any(
                Decimal(proven_zero["automatic_rates"][field]) != 0
                for field in (
                    "agent_remuneration_rate",
                    "acquiring_rate",
                    "wb_logistics_rate",
                    "wb_storage_rate",
                    "penalties_adjustments_rate",
                    "other_expense_rate",
                )
            )
        ):
            raise AssertionError(f"proven canonical zero was confused with missing: {proven_zero}")
        latest_zero = build_latest_confirmed_week_window(
            runtime=runtime,
            today=date(2026, 8, 15),
        )
        if (
            latest_zero["status"] != "ready"
            or latest_zero["source_week_ranges"]
            != [["2026-07-20", "2026-07-26"]]
            or any(
                Decimal(value) != 0
                for value in latest_zero["automatic_rates"].values()
            )
        ):
            raise AssertionError(
                f"latest-week selection confused proven zero with missing: {latest_zero}"
            )

        _save_buyout_week(runtime, "2026-07-20", enabled_nm_ids, Decimal("0.90"))
        _save_finance_week(
            runtime.db_path,
            "2026-07-20",
            "2026-07-27T07:00:00Z",
            metrics=_scaled_finance_metrics(net_revenue=Decimal("1000"), acquiring=Decimal("10")),
        )
        _save_finance_week(
            runtime.db_path,
            "2026-07-27",
            "2026-08-03T07:00:00Z",
            metrics=_scaled_finance_metrics(net_revenue=Decimal("3000"), acquiring=Decimal("150")),
        )
        _delete_finance_week(runtime.db_path, "2026-08-03")
        weighted_two = build_confirmed_aligned_window(runtime=runtime, today=date(2026, 8, 15))
        if (
            weighted_two["ready_week_count"] != 2
            or weighted_two["automatic_rates"]["acquiring_rate"] != "0.04"
            or weighted_two["aligned_finance"]["net_revenue"] != "4000"
        ):
            raise AssertionError(f"2-of-3 Finance rate is not direct SUM/SUM: {weighted_two}")
        latest_weighted = build_latest_confirmed_week_window(
            runtime=runtime,
            today=date(2026, 8, 15),
        )
        if (
            latest_weighted["source_week_ranges"]
            != [["2026-07-27", "2026-08-02"]]
            or latest_weighted["automatic_rates"]["acquiring_rate"] != "0.05"
            or latest_weighted["aligned_finance"]["net_revenue"] != "3000"
        ):
            raise AssertionError(
                "formula averaged weeks instead of selecting the latest exact week: "
                + repr(latest_weighted)
            )
        _delete_finance_week(runtime.db_path, "2026-07-27")
        weighted_one = build_confirmed_aligned_window(runtime=runtime, today=date(2026, 8, 15))
        if (
            weighted_one["ready_week_count"] != 1
            or weighted_one["automatic_rates"]["acquiring_rate"] != "0.01"
            or weighted_one["aligned_finance"]["net_revenue"] != "1000"
        ):
            raise AssertionError(f"1-of-3 Finance rate is not direct SUM/SUM: {weighted_one}")
        _save_finance_week(runtime.db_path, "2026-07-27", "2026-08-03T07:00:00Z")
        _save_finance_week(runtime.db_path, "2026-08-03", "2026-08-10T07:00:00Z")

        metrics = extend_metrics_with_proxy_v4(runtime.load_current_state().metrics_v2)
        v4 = {item.metric_key: item for item in metrics if item.metric_key in {
            PROXY_V4_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_MARGIN_PCT_METRIC_KEY,
            PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
        }}
        if set(v4) != {
            PROXY_V4_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_MARGIN_PCT_METRIC_KEY,
            PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
        }:
            raise AssertionError("public V4 SKU/TOTAL metric pairs are incomplete")
        if PROXY_V4_FIXED_BOUNDARY != "2026-08-01":
            raise AssertionError("fixed product boundary drifted")

        v3 = calculate_proxy_3(
            order_sum=1000,
            order_count=10,
            canonical_wb_wac=20,
            ads_sum=30,
            parameters=DEFAULT_PROXY_PARAMETERS,
        )
        if v3["proxy_profit_3"] != Decimal("1000") * Decimal("0.91") * Decimal("0.56") - Decimal("10") * Decimal("0.91") * Decimal("20") - Decimal("30"):
            raise AssertionError("Proxy V3 formula changed while adding V4")

        _save_partial_buyout_day(runtime, "2026-08-05", enabled_nm_ids)
        partial = build_confirmed_aligned_window(runtime=runtime, today=date(2026, 8, 15))
        if (
            partial["status"] != "ready"
            or partial["buyout"]["weeks"][2]["status"] != "partial"
            or partial["ready_week_count"] != 2
            or ["2026-08-03", "2026-08-09"] in partial["source_week_ranges"]
        ):
            raise AssertionError(f"partial week was not excluded atomically: {partial}")
        partial_latest = build_latest_confirmed_week_window(
            runtime=runtime,
            today=date(2026, 8, 15),
        )
        if partial_latest["source_week_ranges"] != [["2026-07-27", "2026-08-02"]]:
            raise AssertionError(
                f"partial newest slot did not fall back to the previous common READY week: {partial_latest}"
            )
        _delete_finance_week(runtime.db_path, "2026-07-20")
        exact_intersection = build_confirmed_aligned_window(
            runtime=runtime,
            today=date(2026, 8, 15),
        )
        if (
            exact_intersection["ready_week_count"] != 1
            or exact_intersection["source_week_ranges"]
            != [["2026-07-27", "2026-08-02"]]
        ):
            raise AssertionError(
                "V4 silently mixed unrelated Buyout/Finance week sets: "
                + repr(exact_intersection)
            )
        exact_latest = build_latest_confirmed_week_window(
            runtime=runtime,
            today=date(2026, 8, 15),
        )
        if exact_latest["source_week_ranges"] != [["2026-07-27", "2026-08-02"]]:
            raise AssertionError(
                "latest-week V4 silently mixed or selected unrelated periods: "
                + repr(exact_latest)
            )

    print("proxy_v4_formula_boundary_total: ok")
    print("proxy_v4_aligned_window_sum_sum_transit_exclusion: ok")
    print("proxy_v4_as_of_versions_tax_latest_week_rollover_idempotency: ok")
    print("proxy_v4_latest_common_week_no_average_current_day_freeze: ok")
    print("proxy_v4_one_two_three_week_intersection_zero_fallback: ok")
    print("proxy_v4_public_metric_pairs_v3_unchanged: ok")


def _save_buyout_week(
    runtime: RegistryUploadDbBackedRuntime,
    week_start: str,
    nm_ids: list[int],
    buyout: Decimal,
) -> None:
    start = date.fromisoformat(week_start)
    for offset in range(7):
        day = start + timedelta(days=offset)
        snapshot_date = day.isoformat()
        items = []
        for nm_id in nm_ids:
            items.extend(
                [
                    SalesFunnelHistoryItem(
                        date=snapshot_date,
                        nm_id=nm_id,
                        metric="buyoutPercent",
                        value=float(buyout),
                    ),
                    SalesFunnelHistoryItem(
                        date=snapshot_date,
                        nm_id=nm_id,
                        metric="orderCount",
                        value=10,
                    ),
                ]
            )
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=snapshot_date,
            captured_at=datetime.combine(
                day + timedelta(days=6),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ).replace(hour=12).isoformat().replace("+00:00", "Z"),
            payload=SalesFunnelHistorySuccess(
                kind="success",
                date_from=snapshot_date,
                date_to=snapshot_date,
                count=len(items),
                items=items,
            ),
        )


def _finance_metrics() -> dict[str, str]:
    return {
        "net_revenue": "1000",
        "agent_remuneration": "100",
        "commission": "100",
        "acquiring": "20",
        "logistics": "30",
        "storage": "10",
        "penalties": "5",
        "corrections": "2",
        "subscriptions": "3",
        "paid_services": "4",
        "review_points": "1",
        "other_deductions": "2",
        "acceptance": "10",
        "capitalized_acceptance": "6",
        "transit_logistics": "8",
        "capitalized_transit_logistics": "3",
        "marketing": "999",
        "positive_adjustments": "777",
        "wb_remuneration_adjustment": "555",
    }


def _zero_finance_metrics() -> dict[str, str]:
    return {
        **{key: "0" for key in _finance_metrics()},
        "net_revenue": "1000",
    }


def _scaled_finance_metrics(
    *,
    net_revenue: Decimal,
    acquiring: Decimal,
) -> dict[str, str]:
    base = _finance_metrics()
    scale = net_revenue / Decimal("1000")
    return {
        key: (
            str(net_revenue)
            if key == "net_revenue"
            else str(acquiring)
            if key == "acquiring"
            else str(Decimal(value) * scale)
        )
        for key, value in base.items()
    }


def _ensure_finance_tables(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS wb_finance_weekly_aggregates(
                seller_id TEXT NOT NULL,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                classifier_version TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                report_ids_json TEXT NOT NULL DEFAULT '[]',
                report_types_json TEXT NOT NULL DEFAULT '[]',
                unknown_reasons_json TEXT NOT NULL DEFAULT '[]',
                calculated_at TEXT NOT NULL,
                PRIMARY KEY(seller_id,week_start,week_end)
            );
            CREATE TABLE IF NOT EXISTS wb_finance_weekly_sync(
                seller_id TEXT NOT NULL,
                week_start TEXT NOT NULL,
                week_end TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 1,
                first_loaded_at TEXT,
                last_synced_at TEXT,
                next_retry_at TEXT,
                report_count INTEGER NOT NULL DEFAULT 1,
                raw_row_count INTEGER NOT NULL DEFAULT 1,
                content_hash TEXT NOT NULL,
                unchanged_sync_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                PRIMARY KEY(seller_id,week_start,week_end)
            );
            """
        )


def _save_finance_week(
    db_path: Path,
    week_start: str,
    first_loaded_at: str,
    *,
    metrics: dict[str, str] | None = None,
) -> None:
    start = date.fromisoformat(week_start)
    week_end = (start + timedelta(days=6)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO wb_finance_weekly_aggregates(
                   seller_id,week_start,week_end,classifier_version,metrics_json,
                   report_ids_json,report_types_json,unknown_reasons_json,calculated_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "seller-1",
                week_start,
                week_end,
                WB_FINANCE_CLASSIFIER_VERSION,
                json.dumps(metrics or _finance_metrics(), sort_keys=True),
                "[]",
                "[]",
                "[]",
                first_loaded_at,
            ),
        )
        conn.execute(
            """INSERT OR REPLACE INTO wb_finance_weekly_sync(
                   seller_id,week_start,week_end,status,attempt_count,first_loaded_at,
                   last_synced_at,report_count,raw_row_count,content_hash,unchanged_sync_count
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "seller-1",
                week_start,
                week_end,
                "completed",
                1,
                first_loaded_at,
                first_loaded_at,
                1,
                1,
                f"hash-{week_start}",
                0,
            ),
        )


def _delete_finance_week(db_path: Path, week_start: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM wb_finance_weekly_aggregates WHERE week_start=?", (week_start,))
        conn.execute("DELETE FROM wb_finance_weekly_sync WHERE week_start=?", (week_start,))


def _save_partial_buyout_day(
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_date: str,
    nm_ids: list[int],
) -> None:
    day = date.fromisoformat(snapshot_date)
    items = [
        SalesFunnelHistoryItem(
            date=snapshot_date,
            nm_id=nm_id,
            metric="buyoutPercent",
            value=0.95,
        )
        for nm_id in nm_ids
    ]
    runtime.save_temporal_source_snapshot(
        source_key="sales_funnel_history",
        snapshot_date=snapshot_date,
        captured_at=datetime.combine(
            day + timedelta(days=6),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).replace(hour=12).isoformat().replace("+00:00", "Z"),
        payload=SalesFunnelHistorySuccess(
            kind="success",
            date_from=snapshot_date,
            date_to=snapshot_date,
            count=len(items),
            items=items,
        ),
    )


def _install_historical_versions(db_path: Path, versions: list[object]) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_proxy_v4_schema(conn)
        for item in versions:
            parameters = item.public()
            created_at = f"{item.effective_date}T00:00:00Z"
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_proxy_v4_parameter_versions(
                       version_id,block_key,revision,effective_date,source_window_from,
                       source_window_to,source_window_fingerprint,parameters_json,
                       fingerprint,version_kind,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.version_id,
                    "proxy_profit_margin_v4",
                    item.revision,
                    item.effective_date,
                    item.source_window_from,
                    item.source_window_to,
                    item.source_window_fingerprint,
                    json.dumps(parameters, sort_keys=True),
                    f"test-fingerprint-{item.revision}",
                    "historical_initialization",
                    "production_mutation",
                    created_at,
                ),
            )


if __name__ == "__main__":
    main()
