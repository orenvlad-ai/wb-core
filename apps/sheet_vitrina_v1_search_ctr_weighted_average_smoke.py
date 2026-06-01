"""Regression smoke for TOTAL search CTR aggregation.

`CTR в поиске средний` is a TOTAL row over SKU search CTR. A low-view SKU can
carry an extreme upstream CTR value, so the TOTAL row must be weighted by
`views_current` instead of taking an arithmetic mean of SKU ratios.
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    SEARCH_CTR_AVG_TOTAL_METRIC_KEY,
    SlotLookups,
    TemporalLiveSources,
    _MetricEvaluator,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item  # noqa: E402
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot  # noqa: E402
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotItem  # noqa: E402


def main() -> None:
    evaluator = _build_evaluator(
        [
            WebSourceSnapshotItem(nm_id=1001, views_current=1000, ctr_current=20, orders_current=0, position_avg=1),
            WebSourceSnapshotItem(nm_id=1002, views_current=2, ctr_current=2500, orders_current=0, position_avg=1),
        ]
    )
    value = evaluator.resolve_total(SEARCH_CTR_AVG_TOTAL_METRIC_KEY, "yesterday_closed")
    expected = (1000 * 0.20 + 2 * 25.0) / 1002
    if value is None or abs(value - expected) > 0.000001:
        raise AssertionError(f"weighted CTR mismatch: got {value}, expected {expected}")
    arithmetic = (0.20 + 25.0) / 2
    if abs(float(value) - arithmetic) < 0.01:
        raise AssertionError("TOTAL search CTR must not use arithmetic mean of SKU ratios")

    zero_evaluator = _build_evaluator(
        [
            WebSourceSnapshotItem(nm_id=1001, views_current=10, ctr_current=0, orders_current=0, position_avg=1),
            WebSourceSnapshotItem(nm_id=1002, views_current=5, ctr_current=0, orders_current=0, position_avg=1),
        ]
    )
    zero_value = zero_evaluator.resolve_total(SEARCH_CTR_AVG_TOTAL_METRIC_KEY, "yesterday_closed")
    if zero_value != 0.0:
        raise AssertionError(f"valid zero CTR must stay zero, got {zero_value}")

    print(
        {
            "status": "ok",
            "weighted_ctr": round(value, 6),
            "arithmetic_ctr": round(arithmetic, 6),
            "valid_zero": zero_value,
        }
    )


def _build_evaluator(items: list[WebSourceSnapshotItem]) -> _MetricEvaluator:
    config = [
        ConfigV2Item(nm_id=item.nm_id, enabled=True, display_name=f"SKU {item.nm_id}", group="Test", display_order=index)
        for index, item in enumerate(items, start=1)
    ]
    metrics = [
        MetricV2Item(
            metric_key="views_current",
            enabled=True,
            scope="SKU",
            label_ru="Просмотры в поиске",
            calc_type="metric",
            calc_ref="views_current",
            show_in_data=True,
            format="integer",
            display_order=1,
            section="Поиск",
        ),
        MetricV2Item(
            metric_key="ctr_current",
            enabled=True,
            scope="SKU",
            label_ru="CTR в поиске",
            calc_type="metric",
            calc_ref="ctr_current",
            show_in_data=True,
            format="percent",
            display_order=2,
            section="Поиск",
        ),
        MetricV2Item(
            metric_key=SEARCH_CTR_AVG_TOTAL_METRIC_KEY,
            enabled=True,
            scope="TOTAL",
            label_ru="CTR в поиске средний",
            calc_type="metric",
            calc_ref="ctr_current",
            show_in_data=True,
            format="percent",
            display_order=3,
            section="Поиск",
        ),
    ]
    lookups = SlotLookups(
        seller_funnel_lookup={},
        history_lookup={},
        web_lookup={item.nm_id: item for item in items},
        prices_lookup={},
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
        metrics_by_key={metric.metric_key: metric for metric in metrics},
        formulas_by_id={},
        live_sources=TemporalLiveSources(
            temporal_slots=[
                SheetVitrinaV1TemporalSlot(
                    slot_key="yesterday_closed",
                    slot_label="Вчера закрыто",
                    column_date="2026-05-27",
                )
            ],
            statuses=[],
            slot_lookups={"yesterday_closed": lookups},
            source_temporal_policies={},
        ),
    )


if __name__ == "__main__":
    main()
