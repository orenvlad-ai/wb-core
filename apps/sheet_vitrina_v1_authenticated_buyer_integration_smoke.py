"""Focused read-side contract for the authenticated WB Buyer source."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _build_activity_metric_labels_by_source,
    _source_key_for_metric_key,
    _updated_cell_status_for_status_row,
)
from packages.application.sheet_vitrina_v1_authenticated_buyer import (  # noqa: E402
    AUTHENTICATED_SPP_METRIC_KEY,
    AVG_EFFECTIVE_DISCOUNT_METRIC_KEY,
    EFFECTIVE_DISCOUNT_METRIC_KEY,
    METRIC_KEYS,
    SOURCE_KEY,
    extend_metrics_with_authenticated_buyer,
    projection_payload,
    visible_authenticated_buyer_metrics,
)
from packages.application.sheet_vitrina_v1_health import (  # noqa: E402
    _expectation_cell,
    GOOD_EXPECTATION_STATES,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    _MetricEvaluator,
    _capture_live_source,
    _index_items_by_nm_id,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (  # noqa: E402
    _include_authenticated_discount_total_row,
    _normalize_rows,
)
from packages.application.sheet_vitrina_v1_research import _selectable_metric_options  # noqa: E402
from packages.application.sheet_vitrina_v1_temporal_policy import (  # noqa: E402
    reduce_source_temporal_semantics,
)
from packages.contracts.registry_upload_bundle_v1 import MetricV2Item  # noqa: E402
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow  # noqa: E402


def main() -> None:
    metrics = extend_metrics_with_authenticated_buyer([])
    assert {item.metric_key for item in metrics} == set(METRIC_KEYS)
    assert {item.metric_key for item in metrics if item.scope == "TOTAL"} == {AVG_EFFECTIVE_DISCOUNT_METRIC_KEY}
    total_metric = next(item for item in metrics if item.metric_key == AVG_EFFECTIVE_DISCOUNT_METRIC_KEY)
    assert total_metric.calc_ref == EFFECTIVE_DISCOUNT_METRIC_KEY
    assert total_metric.label_ru == "СПП с авторизацией"
    proxy_metrics = extend_metrics_with_authenticated_buyer([
        MetricV2Item(key, True, scope, "SPP-прокси", "metric", ref, True, "percent", 1, "Цены")
        for key, scope, ref in (("spp_proxy", "SKU", "spp_proxy"),
                                ("avg_spp_proxy", "TOTAL", "spp_proxy"))
    ])
    assert [item.label_ru for item in proxy_metrics[:2]] == ["СПП без авторизации"] * 2
    activity_labels = _build_activity_metric_labels_by_source(
        visible_authenticated_buyer_metrics([*proxy_metrics, *metrics])
    )
    assert "СПП покупателя, %" not in activity_labels[SOURCE_KEY]
    assert "СПП с авторизацией" in activity_labels[SOURCE_KEY]
    assert "СПП без авторизации" in activity_labels["spp_proxy"]
    assert _source_key_for_metric_key("buyer_nonwallet_price_rub") == SOURCE_KEY
    assert _source_key_for_metric_key("spp") == "spp"
    assert _source_key_for_metric_key("spp_proxy") == "spp_proxy"
    empty_status = ["wb_buyer_authenticated[today_current]", "empty", "", "", "", "", "", 2, 0, "", "account_context_reset=true"]
    assert _updated_cell_status_for_status_row(empty_status) == "updated"
    assert _updated_cell_status_for_status_row([*empty_status[:10], ""]) == ""
    assert _updated_cell_status_for_status_row(["spp[today_current]", *empty_status[1:]]) == ""
    options = _selectable_metric_options(
        visible_authenticated_buyer_metrics(metrics),
        sku_metric_keys={item.metric_key for item in metrics if item.scope == "SKU"},
    )
    assert {item["metric_key"] for item in options} == set(METRIC_KEYS) - {
        AVG_EFFECTIVE_DISCOUNT_METRIC_KEY, AUTHENTICATED_SPP_METRIC_KEY,
    }
    # A previously published ready sheet has SKU values but no new TOTAL row.
    # The read model must supply a non-zero-only mean without editing that sheet.
    dates = ["2026-09-28", "2026-09-29"]
    def discount_row(nm_id: int, value: object) -> WebVitrinaContractRow:
        return WebVitrinaContractRow(
            row_id=f"SKU:{nm_id}|{EFFECTIVE_DISCOUNT_METRIC_KEY}", row_order=nm_id,
            scope_kind="SKU", scope_key=f"SKU:{nm_id}", scope_label=str(nm_id),
            metric_key=EFFECTIVE_DISCOUNT_METRIC_KEY, metric_label="СПП с авторизацией",
            row_last_updated_at="2026-09-29T10:00:00Z", section="Цены", group=None,
            nm_id=nm_id, format="percent", values_by_date={dates[0]: "", dates[1]: value},
        )
    result = _include_authenticated_discount_total_row(
        [discount_row(1, 0.2), discount_row(2, ""), discount_row(3, 0.4)],
        date_columns=dates, metric=total_metric,
    )
    total = next(row for row in result if row.row_id == f"TOTAL|{AVG_EFFECTIVE_DISCOUNT_METRIC_KEY}")
    assert total.values_by_date[dates[0]] == ""
    assert abs(total.values_by_date[dates[1]] - 0.3) < 1e-12
    historical = _normalize_rows(
        [["old pure", "SKU:1|authenticated_spp", 0.99],
         ["current", "SKU:1|effective_nonwallet_discount", 0.2]],
        date_columns=[dates[1]], config_by_nm_id={},
        metrics_by_key={item.metric_key: item for item in visible_authenticated_buyer_metrics(metrics)},
        row_updated_at_by_id={},
    )
    assert [row.metric_key for row in historical] == [EFFECTIVE_DISCOUNT_METRIC_KEY]

    prior = projection_payload(
        {
            "snapshot_date": "2026-09-27",
            "kind": "empty",
            "requested_count": 1,
            "covered_count": 0,
            "missing_nm_ids": [497416931],
            "items": [],
            "diagnostics": {
                "first_run_business_date": "2026-09-28",
                "first_observed_business_date": "2026-09-28",
            },
        },
        business_date="2026-09-27",
    )
    assert prior.kind == "not_available" and "pre_cutover" in prior.detail
    prior_status, _ = _capture_live_source(
        source_key=SOURCE_KEY,
        temporal_slot="yesterday_closed",
        temporal_policy="dual_day_capable",
        column_date="2026-09-27",
        requested_nm_ids=[497416931],
        loader=lambda: prior,
    )
    prior_cell = _expectation_cell(
        source_key=SOURCE_KEY,
        source_group_id=SOURCE_KEY,
        temporal_policy="dual_day_capable",
        role="yesterday_closed",
        target_date="2026-09-27",
        slot={"kind": prior_status.kind, "note": prior_status.note},
        yesterday_slot=None,
    )
    assert prior_cell["expectation_state"] in GOOD_EXPECTATION_STATES
    assert prior_cell["expectation_state"] == "pre_cutover"
    failed_first_day = projection_payload(
        {
            "snapshot_date": "2026-09-28",
            "kind": "empty",
            "requested_count": 1,
            "covered_count": 0,
            "missing_nm_ids": [497416931],
            "items": [],
            "diagnostics": {
                "first_run_business_date": "2026-09-28",
                "first_observed_business_date": "2026-09-29",
            },
        },
        business_date="2026-09-28",
    )
    assert failed_first_day.kind == "empty" and "pre_cutover" not in failed_first_day.detail
    reduced = reduce_source_temporal_semantics(
        source_key=SOURCE_KEY,
        temporal_policy="dual_day_capable",
        slot_outcomes=[
            {
                "temporal_slot": "yesterday_closed",
                "status": "warning",
                "kind": prior_status.kind,
                "note": prior_status.note,
            },
            {"temporal_slot": "today_current", "status": "success", "kind": "success"},
        ],
    )
    assert reduced["status"] == "success"

    projection = projection_payload(
        {
            "snapshot_date": "2026-09-28",
            "kind": "success",
            "requested_count": 1,
            "covered_count": 1,
            "missing_nm_ids": [],
            "items": [{
                "nm_id": 497416931,
                "buyer_wallet_price_rub": 139.0,
                "buyer_nonwallet_price_rub": 144.0,
                "effective_nonwallet_discount": 0.2,
                "authenticated_spp": 0.99,  # Even a malformed stored scalar cannot become a public SPP.
                "measured_at": "2026-09-28T16:30:00Z",
                "reason": "components_unknown",
            }],
            "diagnostics": {
                "first_run_business_date": "2026-09-28",
                "first_observed_business_date": "2026-09-28",
            },
        },
        business_date="2026-09-28",
    )
    status, admitted = _capture_live_source(
        source_key=SOURCE_KEY,
        temporal_slot="today_current",
        temporal_policy="dual_day_capable",
        column_date="2026-09-28",
        requested_nm_ids=[497416931],
        loader=lambda: projection,
    )
    assert status.kind == "success" and status.covered_count == 1
    assert "spp_evidence=components_unknown" in status.note
    evaluator = _MetricEvaluator.__new__(_MetricEvaluator)
    evaluator.live_sources = SimpleNamespace(slot_lookups={
        "today_current": SimpleNamespace(authenticated_buyer_lookup=_index_items_by_nm_id(admitted))
    })
    assert evaluator._resolve_direct_sku("buyer_wallet_price_rub", 497416931, "today_current") == 139.0
    assert evaluator._resolve_direct_sku("buyer_nonwallet_price_rub", 497416931, "today_current") == 144.0
    assert evaluator._resolve_direct_sku("effective_nonwallet_discount", 497416931, "today_current") == 0.2
    assert evaluator._resolve_direct_sku("authenticated_spp", 497416931, "today_current") is None
    print("sheet_vitrina_v1_authenticated_buyer_integration: ok")


if __name__ == "__main__":
    main()
