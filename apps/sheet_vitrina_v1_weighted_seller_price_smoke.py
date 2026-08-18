"""Targeted regression for weighted seller price and its retained legacy average."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_archived_metrics import (  # noqa: E402
    ARCHIVED_PUBLIC_METRIC_KEYS,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    SlotLookups,
    TemporalLiveSources,
    _MetricEvaluator,
    _expand_selected_source_keys_for_dependencies,
)
from packages.application.sheet_vitrina_v1_weighted_seller_price import (  # noqa: E402
    LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY,
    SELLER_PRICE_DISCOUNTED_METRIC_KEY,
    SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY,
    WEIGHTED_SELLER_PRICE_DISCOUNTED_AGGREGATION_RULE,
    WEIGHTED_SELLER_PRICE_DISCOUNTED_LABEL_RU,
    WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY,
    extend_metrics_with_weighted_seller_price,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot  # noqa: E402


def main() -> None:
    metrics = _validated_metrics()
    if _expand_selected_source_keys_for_dependencies({"prices_snapshot"}) != {
        "prices_snapshot",
        "sales_funnel_history",
    }:
        raise AssertionError("price refresh must acquire exact-date orderCount weights")
    evaluator = _evaluator(
        metrics,
        slots={
            "day_a": ({1001: 500, 1002: 100}, {1001: 9, 1002: 1}),
            "day_b": ({1001: 200, 1002: 600}, {1001: 4, 1002: 1}),
        },
    )
    _assert_close(
        evaluator.resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a"),
        460.0,
        "weighted acceptance fixture",
    )
    _assert_close(
        evaluator.resolve_total(LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a"),
        300.0,
        "legacy arithmetic fixture",
    )
    _assert_close(
        evaluator.resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_b"),
        280.0,
        "exact-date weighted fixture",
    )

    zero_denominator = _evaluator(
        metrics,
        slots={"day_a": ({1001: 500, 1002: 100}, {1001: 0, 1002: -2})},
    ).resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a")
    if zero_denominator is not None:
        raise AssertionError(f"zero positive weight must be blank, got {zero_denominator}")

    incomplete = _evaluator(
        metrics,
        slots={"day_a": ({1001: 500, 1002: None}, {1001: 9, 1002: 1})},
    ).resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a")
    if incomplete is not None:
        raise AssertionError(f"positive-weight missing price must fail closed, got {incomplete}")

    invalid = _evaluator(
        metrics,
        slots={"day_a": ({1001: 500, 1002: float("nan")}, {1001: 9, 1002: 1})},
    ).resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a")
    if invalid is not None:
        raise AssertionError(f"positive-weight invalid price must fail closed, got {invalid}")

    nonpositive_price = _evaluator(
        metrics,
        slots={"day_a": ({1001: 500, 1002: 0}, {1001: 9, 1002: 1})},
    ).resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a")
    if nonpositive_price is not None:
        raise AssertionError(
            f"positive-weight non-positive price must fail closed, got {nonpositive_price}"
        )

    zero_weight_missing = _evaluator(
        metrics,
        slots={"day_a": ({1001: 500, 1002: None}, {1001: 2, 1002: 0})},
    ).resolve_total(WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY, "day_a")
    _assert_close(zero_weight_missing, 500.0, "zero-weight SKU exclusion")

    print(
        {
            "status": "ok",
            "weighted": 460.0,
            "legacy_arithmetic": 300.0,
            "zero_denominator": zero_denominator,
            "positive_weight_missing_price": incomplete,
            "positive_weight_invalid_price": invalid,
            "positive_weight_nonpositive_price": nonpositive_price,
            "exact_date_day_b": 280.0,
            "rule": WEIGHTED_SELLER_PRICE_DISCOUNTED_AGGREGATION_RULE,
        }
    )


def _validated_metrics() -> list[MetricV2Item]:
    base = [
        MetricV2Item(
            metric_key=SELLER_PRICE_DISCOUNTED_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru="Цена продавца",
            calc_type="metric",
            calc_ref=SELLER_PRICE_DISCOUNTED_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=10,
            section="Цены",
        ),
        MetricV2Item(
            metric_key=SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY,
            enabled=True,
            scope="SKU",
            label_ru="Заказы",
            calc_type="metric",
            calc_ref=SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY,
            show_in_data=True,
            format="integer",
            display_order=20,
            section="Продажи",
        ),
        MetricV2Item(
            metric_key=LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="Цена продавца средняя",
            calc_type="metric",
            calc_ref=SELLER_PRICE_DISCOUNTED_METRIC_KEY,
            show_in_data=True,
            format="rub",
            display_order=30,
            section="Цены",
        ),
    ]
    once = extend_metrics_with_weighted_seller_price(base)
    twice = extend_metrics_with_weighted_seller_price(once)
    keys = [item.metric_key for item in twice]
    if len(keys) != len(set(keys)) or len(twice) != len(once):
        raise AssertionError("weighted metric registry extension must be unique and idempotent")
    weighted = next(
        item for item in twice
        if item.metric_key == WEIGHTED_SELLER_PRICE_DISCOUNTED_METRIC_KEY
    )
    if (
        weighted.scope != "TOTAL"
        or weighted.label_ru != WEIGHTED_SELLER_PRICE_DISCOUNTED_LABEL_RU
        or "positive_weight_fail_closed" not in weighted.calc_ref
    ):
        raise AssertionError(f"weighted registry contract mismatch: {weighted}")
    legacy = next(
        item for item in twice
        if item.metric_key == LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY
    )
    if legacy != base[-1]:
        raise AssertionError("legacy arithmetic metric registry identity was changed")
    if LEGACY_AVG_SELLER_PRICE_DISCOUNTED_METRIC_KEY not in ARCHIVED_PUBLIC_METRIC_KEYS:
        raise AssertionError("legacy TOTAL identity must be excluded from the active public catalog")
    return twice


def _evaluator(
    metrics: list[MetricV2Item],
    *,
    slots: dict[str, tuple[dict[int, float | None], dict[int, float]]],
) -> _MetricEvaluator:
    config = [
        ConfigV2Item(
            nm_id=1001,
            enabled=True,
            display_name="SKU 1001",
            group="Test",
            display_order=1,
        ),
        ConfigV2Item(
            nm_id=1002,
            enabled=True,
            display_name="SKU 1002",
            group="Test",
            display_order=2,
        ),
    ]
    temporal_slots = []
    lookups = {}
    for index, (slot_key, (prices, weights)) in enumerate(slots.items(), start=1):
        temporal_slots.append(
            SheetVitrinaV1TemporalSlot(
                slot_key=slot_key,
                slot_label=slot_key,
                column_date=f"2026-08-{index:02d}",
            )
        )
        lookups[slot_key] = SlotLookups(
            seller_funnel_lookup={},
            history_lookup={
                nm_id: {SELLER_PRICE_ORDER_WEIGHT_METRIC_KEY: weight}
                for nm_id, weight in weights.items()
            },
            web_lookup={},
            prices_lookup={
                nm_id: SimpleNamespace(price_seller_discounted=price)
                for nm_id, price in prices.items()
            },
            sf_period_lookup={},
            spp_lookup={},
            ads_bids_lookup={},
            stocks_lookup={},
            onec_stocks_lookup={},
            ads_compact_lookup={},
            fin_lookup={},
            fin_storage_fee_total=None,
            cost_price_lookup={},
            promo_lookup={},
        )
    return _MetricEvaluator(
        enabled_config=config,
        metrics_by_key={item.metric_key: item for item in metrics},
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=temporal_slots,
            statuses=[],
            slot_lookups=lookups,
            source_temporal_policies={},
        ),
    )


def _assert_close(value: float | None, expected: float, label: str) -> None:
    if value is None or abs(float(value) - expected) > 0.000001:
        raise AssertionError(f"{label}: got {value}, expected {expected}")


if __name__ == "__main__":
    main()
