"""Targeted acceptance smoke for immutable Proxy V4 parameters and formula."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import (  # noqa: E402
    DEFAULT_PROXY_PARAMETERS,
    calculate_proxy_3,
)
from packages.application.calculation_parameters_v4 import (  # noqa: E402
    PROXY_V4_FIXED_BOUNDARY,
    ProxyV4Parameters,
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
    PROXY_V4_MARGIN_PER_UNIT_LABEL_RU,
    PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
    extend_metrics_with_proxy_v4,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    SlotLookups,
    TemporalLiveSources,
    _MetricEvaluator,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (  # noqa: E402
    _include_proxy_v4_unit_margin_rows,
)
from packages.application.web_vitrina_view_model import (  # noqa: E402
    _FORMATTER_LIBRARY,
    _resolve_cell_kind_and_formatter,
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
from packages.contracts.registry_upload_bundle_v1 import (  # noqa: E402
    ConfigV2Item,
    MetricV2Item,
)
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot  # noqa: E402
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow  # noqa: E402


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

        unit_parameters = _unit_margin_parameters()
        unit_control = calculate_proxy_4(
            order_sum=Decimal("11000"),
            order_count=Decimal("100"),
            canonical_wb_wac=Decimal("10"),
            ads_sum=Decimal("0"),
            parameters=unit_parameters,
            business_date="2026-08-09",
        )
        if (
            unit_control["expected_buyout_qty"] != Decimal("91")
            or unit_control["proxy_profit_4"] != Decimal("9100")
            or unit_control["proxy_margin_per_unit"] != Decimal("100")
        ):
            raise AssertionError(
                "unit margin must use 100 orders × 91% = 91 proxy units and yield 100 ₽/шт: "
                f"{unit_control}"
            )
        weighted_unit = aggregate_proxy_4(
            [
                unit_control,
                {
                    "proxy_profit_4": Decimal("4550"),
                    "expected_buyout_revenue": Decimal("4641"),
                    "expected_buyout_qty": Decimal("9.1"),
                },
                {
                    "proxy_profit_4": None,
                    "expected_buyout_revenue": None,
                    "expected_buyout_qty": None,
                },
            ]
        )
        expected_weighted_unit = Decimal("13650") / Decimal("100.1")
        if weighted_unit["proxy_margin_per_unit"] != expected_weighted_unit:
            raise AssertionError(
                f"TOTAL unit margin must be direct ratio of matched sums: {weighted_unit}"
            )
        if weighted_unit["proxy_margin_per_unit"] == Decimal("300"):
            raise AssertionError("TOTAL unit margin must not average 100 and 500 ₽/шт SKU values")
        negative_unit = calculate_proxy_4(
            order_sum=Decimal("1000"),
            order_count=Decimal("100"),
            canonical_wb_wac=Decimal("10"),
            ads_sum=Decimal("910"),
            parameters=unit_parameters,
            business_date="2026-08-09",
        )
        if negative_unit["proxy_margin_per_unit"] != Decimal("-10"):
            raise AssertionError(f"negative confirmed profit must remain negative: {negative_unit}")
        zero_unit = calculate_proxy_4(
            order_sum=Decimal("0"),
            order_count=Decimal("0"),
            canonical_wb_wac=Decimal("10"),
            ads_sum=Decimal("0"),
            parameters=unit_parameters,
            business_date="2026-08-09",
        )
        if zero_unit["proxy_margin_per_unit"] is not None:
            raise AssertionError("nonpositive expected buyout quantity must stay blank")
        if calculate_proxy_4(
            order_sum=Decimal("11000"),
            order_count=None,
            canonical_wb_wac=Decimal("10"),
            ads_sum=Decimal("0"),
            parameters=unit_parameters,
            business_date="2026-08-09",
        )["proxy_margin_per_unit"] is not None:
            raise AssertionError("missing orderCount must stay blank")

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
            PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
            PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        }}
        if set(v4) != {
            PROXY_V4_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
            PROXY_V4_MARGIN_PCT_METRIC_KEY,
            PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
            PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
            PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        }:
            raise AssertionError("public V4 SKU/TOTAL metric pairs are incomplete")
        sku_unit_metric = v4[PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY]
        total_unit_metric = v4[PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY]
        if (
            sku_unit_metric.label_ru != PROXY_V4_MARGIN_PER_UNIT_LABEL_RU
            or total_unit_metric.label_ru != PROXY_V4_MARGIN_PER_UNIT_LABEL_RU
            or sku_unit_metric.format != "rub_per_unit"
            or total_unit_metric.format != "rub_per_unit"
            or total_unit_metric.display_order + 1 != sku_unit_metric.display_order
        ):
            raise AssertionError(
                f"unit-margin registry label/format/order drifted: {total_unit_metric}, {sku_unit_metric}"
            )
        cell_kind, formatter_id = _resolve_cell_kind_and_formatter(
            column_id="date:2026-08-09",
            value=100,
            row_format="rub_per_unit",
        )
        formatter = _FORMATTER_LIBRARY.get(str(formatter_id))
        if (
            cell_kind != "money"
            or formatter_id != "money_rub_per_unit"
            or formatter is None
            or formatter.suffix != " ₽/шт"
        ):
            raise AssertionError("unit-margin value formatter must render ₽/шт")
        if PROXY_V4_FIXED_BOUNDARY != "2026-08-01":
            raise AssertionError("fixed product boundary drifted")

        _assert_unit_margin_evaluator_and_read_side()

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
    print("proxy_v4_unit_margin_weighted_total_read_side_pair: ok")
    print("proxy_v4_public_metric_pairs_v3_unchanged: ok")


def _unit_margin_parameters() -> ProxyV4Parameters:
    return ProxyV4Parameters(
        effective_date="2026-08-01",
        buyout_rate=Decimal("0.91"),
        tax_rate=Decimal("0"),
        agent_remuneration_rate=Decimal("0"),
        acquiring_rate=Decimal("0"),
        wb_logistics_rate=Decimal("0"),
        wb_storage_rate=Decimal("0"),
        penalties_adjustments_rate=Decimal("0"),
        other_expense_rate=Decimal("0"),
        source_window_from="2026-07-20",
        source_window_to="2026-07-26",
        source_window_fingerprint="synthetic-unit-margin",
        source_week_ranges=(("2026-07-20", "2026-07-26"),),
        source_slot_from="2026-07-20",
        source_slot_to="2026-07-26",
        buyout_order_count_weight=Decimal("130"),
        finance_net_revenue_weight=Decimal("1"),
        formula_version="proxy_profit_4_v2_no_transit",
        version_id="synthetic-unit-margin",
        revision=1,
    )


def _assert_unit_margin_evaluator_and_read_side() -> None:
    first_nm_id, second_nm_id, uncovered_nm_id = 900001, 900002, 900003
    config = [
        ConfigV2Item(
            nm_id=first_nm_id,
            enabled=True,
            display_name="SKU 100 ₽/шт",
            group="Контроль",
            display_order=1,
        ),
        ConfigV2Item(
            nm_id=second_nm_id,
            enabled=True,
            display_name="SKU 500 ₽/шт",
            group="Контроль",
            display_order=2,
        ),
        ConfigV2Item(
            nm_id=uncovered_nm_id,
            enabled=True,
            display_name="SKU без себестоимости",
            group="Контроль",
            display_order=3,
        ),
    ]
    base_metrics = [
        MetricV2Item(
            metric_key=metric_key,
            enabled=True,
            scope="SKU",
            label_ru=metric_key,
            calc_type="metric",
            calc_ref=metric_key,
            show_in_data=True,
            format="number",
            display_order=index,
            section="Тест",
        )
        for index, metric_key in enumerate(("orderSum", "orderCount", "ads_sum"), start=1)
    ]
    metrics = extend_metrics_with_proxy_v4(base_metrics)
    metrics_by_key = {item.metric_key: item for item in metrics}
    parameters = _unit_margin_parameters()
    lookups = _unit_margin_slot_lookup(
        first_nm_id=first_nm_id,
        second_nm_id=second_nm_id,
        uncovered_nm_id=uncovered_nm_id,
    )
    evaluator = _MetricEvaluator(
        enabled_config=config,
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[
                SheetVitrinaV1TemporalSlot(
                    slot_key="control",
                    slot_label="control",
                    column_date="2026-08-09",
                )
            ],
            statuses=[],
            slot_lookups={"control": lookups},
            source_temporal_policies={},
        ),
        proxy_v4_parameters_resolver=lambda _date: parameters,
    )
    first = evaluator.resolve_sku(
        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        first_nm_id,
        "control",
    )
    second = evaluator.resolve_sku(
        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        second_nm_id,
        "control",
    )
    expected_total = 13650.0 / 100.1
    if first != 100.0 or second != 500.0:
        raise AssertionError(f"SKU unit margins must be 100/500 ₽/шт, got {first}/{second}")
    total = evaluator.resolve_total(
        PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        "control",
    )
    group = evaluator.resolve_group(
        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        "Контроль",
        "control",
    )
    if abs(float(total or 0.0) - expected_total) > 0.000001:
        raise AssertionError(f"TOTAL unit margin must be weighted direct ratio, got {total}")
    if abs(float(group or 0.0) - expected_total) > 0.000001:
        raise AssertionError(f"GROUP unit margin must use the same matched ratio, got {group}")
    if abs(float(total or 0.0) - ((float(first) + float(second)) / 2.0)) < 0.000001:
        raise AssertionError("TOTAL unit margin became arithmetic mean")
    if evaluator.resolve_sku(
        PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        uncovered_nm_id,
        "control",
    ) is not None:
        raise AssertionError("known uncovered sales must stay outside unit margin")

    missing_lookups = deepcopy(lookups)
    missing_lookups.our_wb_cost_lookup.pop(second_nm_id)
    missing_evaluator = _MetricEvaluator(
        enabled_config=config[:2],
        metrics_by_key=metrics_by_key,
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[
                SheetVitrinaV1TemporalSlot(
                    slot_key="missing",
                    slot_label="missing",
                    column_date="2026-08-09",
                )
            ],
            statuses=[],
            slot_lookups={"missing": missing_lookups},
            source_temporal_policies={},
        ),
        proxy_v4_parameters_resolver=lambda _date: parameters,
    )
    if missing_evaluator.resolve_total(
        PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY,
        "missing",
    ) is not None:
        raise AssertionError("TOTAL unit margin must fail closed on a covered SKU missing evidence")

    date_columns = ["2026-08-09", "2026-08-10"]
    rows = _unit_margin_contract_rows(
        config=config,
        date_columns=date_columns,
    )

    class FakeRuntime:
        def load_our_wb_cost_daily_state(self, *, as_of_date: str):
            if as_of_date not in date_columns:
                return {}
            return lookups.our_wb_cost_lookup

    completed = _include_proxy_v4_unit_margin_rows(
        rows,
        runtime=FakeRuntime(),  # type: ignore[arg-type]
        date_columns=date_columns,
        enabled_config=config,
        sku_metric=metrics_by_key[PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY],
        total_metric=metrics_by_key[
            PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY
        ],
        parameters_for_date=lambda _date: parameters,
    )
    completed_by_id = {row.row_id: row for row in completed}
    first_row = completed_by_id[
        f"SKU:{first_nm_id}|{PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY}"
    ]
    second_row = completed_by_id[
        f"SKU:{second_nm_id}|{PROXY_V4_MARGIN_PER_UNIT_RUB_METRIC_KEY}"
    ]
    total_row = completed_by_id[
        f"TOTAL|{PROXY_V4_TOTAL_MARGIN_PER_UNIT_RUB_METRIC_KEY}"
    ]
    if first_row.values_by_date != {"2026-08-09": 100.0, "2026-08-10": -10.0}:
        raise AssertionError(f"read-side SKU history/negative semantics drifted: {first_row}")
    if second_row.values_by_date != {"2026-08-09": 500.0, "2026-08-10": ""}:
        raise AssertionError(f"read-side missing profit must stay blank: {second_row}")
    if (
        abs(float(total_row.values_by_date["2026-08-09"]) - expected_total) > 0.000001
        or total_row.values_by_date["2026-08-10"] != ""
    ):
        raise AssertionError(f"read-side TOTAL must weight and fail closed by date: {total_row}")
    if (
        first_row.metric_label != PROXY_V4_MARGIN_PER_UNIT_LABEL_RU
        or total_row.metric_label != PROXY_V4_MARGIN_PER_UNIT_LABEL_RU
        or first_row.format != "rub_per_unit"
        or total_row.format != "rub_per_unit"
    ):
        raise AssertionError("read-side unit-margin label/format pair drifted")
    total_index = completed.index(total_row)
    total_margin_index = next(
        index
        for index, row in enumerate(completed)
        if row.row_id == f"TOTAL|{PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY}"
    )
    if total_index != total_margin_index + 1:
        raise AssertionError("TOTAL unit margin must immediately follow V4 margin")


def _unit_margin_slot_lookup(
    *,
    first_nm_id: int,
    second_nm_id: int,
    uncovered_nm_id: int,
) -> SlotLookups:
    return SlotLookups(
        seller_funnel_lookup={},
        history_lookup={
            first_nm_id: {"orderSum": 11000.0, "orderCount": 100.0},
            second_nm_id: {"orderSum": 5100.0, "orderCount": 10.0},
            uncovered_nm_id: {"orderSum": 2000.0, "orderCount": 20.0},
        },
        web_lookup={},
        prices_lookup={},
        sf_period_lookup={},
        spp_lookup={},
        ads_bids_lookup={},
        stocks_lookup={},
        onec_stocks_lookup={},
        ads_compact_lookup={
            first_nm_id: SimpleNamespace(ads_sum=0.0),
            second_nm_id: SimpleNamespace(ads_sum=0.0),
            uncovered_nm_id: SimpleNamespace(ads_sum=0.0),
        },
        fin_lookup={},
        fin_storage_fee_total=None,
        cost_price_lookup={},
        promo_lookup={},
        our_wb_cost_lookup={
            first_nm_id: _unit_margin_cost_state(
                sales_revenue=11000.0,
                order_count=100.0,
                cogs=1000.0,
            ),
            second_nm_id: _unit_margin_cost_state(
                sales_revenue=5100.0,
                order_count=10.0,
                cogs=100.0,
            ),
            uncovered_nm_id: {
                "our_wb_unit_cost_rub": None,
                "daily_profit_coverage": {
                    "sales_revenue_rub": 2000.0,
                    "covered_sales_revenue_rub": 0.0,
                    "uncovered_sales_revenue_rub": 2000.0,
                    "sales_order_count": 20.0,
                    "covered_sales_order_count": 0.0,
                    "covered_sales_units": 0.0,
                    "covered_sales_cogs_rub": 0.0,
                },
            },
        },
        column_date="2026-08-09",
    )


def _unit_margin_cost_state(
    *,
    sales_revenue: float,
    order_count: float,
    cogs: float,
) -> dict[str, object]:
    return {
        "our_wb_unit_cost_rub": 10.0,
        "calculated_at": "2026-08-09T12:00:00Z",
        "daily_profit_coverage": {
            "sales_revenue_rub": sales_revenue,
            "covered_sales_revenue_rub": sales_revenue,
            "uncovered_sales_revenue_rub": 0.0,
            "sales_order_count": order_count,
            "covered_sales_order_count": order_count,
            "covered_sales_units": order_count,
            "covered_sales_cogs_rub": cogs,
        },
    }


def _unit_margin_contract_rows(
    *,
    config: list[ConfigV2Item],
    date_columns: list[str],
) -> list[WebVitrinaContractRow]:
    profits_by_nm_id = {
        config[0].nm_id: [9100.0, -910.0],
        config[1].nm_id: [4550.0, ""],
        config[2].nm_id: ["", ""],
    }
    order_sums = {
        config[0].nm_id: 11000.0,
        config[1].nm_id: 5100.0,
        config[2].nm_id: 2000.0,
    }
    order_counts = {
        config[0].nm_id: 100.0,
        config[1].nm_id: 10.0,
        config[2].nm_id: 20.0,
    }
    rows = [
        WebVitrinaContractRow(
            row_id=f"TOTAL|{PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY}",
            row_order=1,
            scope_kind="TOTAL",
            scope_key="TOTAL",
            scope_label="ИТОГО",
            metric_key=PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
            metric_label="Прокси маржинальность 4",
            row_last_updated_at="2026-08-09T12:00:00Z",
            section="Экономика",
            group=None,
            nm_id=None,
            format="percent",
            values_by_date={column_date: 0.1 for column_date in date_columns},
        )
    ]
    for config_item in config:
        scope_key = f"SKU:{config_item.nm_id}"
        values = {
            date_columns[index]: value
            for index, value in enumerate(profits_by_nm_id[config_item.nm_id])
        }
        common = {
            "scope_kind": "SKU",
            "scope_key": scope_key,
            "scope_label": config_item.display_name,
            "row_last_updated_at": "2026-08-09T12:00:00Z",
            "section": "Экономика",
            "group": config_item.group,
            "nm_id": config_item.nm_id,
        }
        rows.extend(
            [
                WebVitrinaContractRow(
                    row_id=f"{scope_key}|{PROXY_V4_PROFIT_RUB_METRIC_KEY}",
                    row_order=len(rows) + 1,
                    metric_key=PROXY_V4_PROFIT_RUB_METRIC_KEY,
                    metric_label="Proxy прибыль 4",
                    format="rub",
                    values_by_date=values,
                    **common,
                ),
                WebVitrinaContractRow(
                    row_id=f"{scope_key}|{PROXY_V4_MARGIN_PCT_METRIC_KEY}",
                    row_order=len(rows) + 2,
                    metric_key=PROXY_V4_MARGIN_PCT_METRIC_KEY,
                    metric_label="Прокси маржинальность 4",
                    format="percent",
                    values_by_date={column_date: 0.1 for column_date in date_columns},
                    **common,
                ),
                WebVitrinaContractRow(
                    row_id=f"{scope_key}|orderSum",
                    row_order=len(rows) + 3,
                    metric_key="orderSum",
                    metric_label="Сумма заказов",
                    format="rub",
                    values_by_date={
                        column_date: order_sums[config_item.nm_id]
                        for column_date in date_columns
                    },
                    **common,
                ),
                WebVitrinaContractRow(
                    row_id=f"{scope_key}|orderCount",
                    row_order=len(rows) + 4,
                    metric_key="orderCount",
                    metric_label="Заказы",
                    format="integer",
                    values_by_date={
                        column_date: order_counts[config_item.nm_id]
                        for column_date in date_columns
                    },
                    **common,
                ),
            ]
        )
    return rows


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
