"""Targeted smoke-check for the phase-1 web_vitrina_contract v1 builder."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.sheet_vitrina_v1_our_wb_costs import (
    OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
    TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY,
    TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_incident_stocks import (
    incident_stock_metric_key,
)
from packages.application.sheet_vitrina_v1_sku_actions import (
    ADVERTISING_BID_CHANGE_RUB_METRIC_KEY,
    BUYER_PRICE_RUB_METRIC_KEY,
    SELLER_PRICE_CHANGE_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (
    own_stage_metric_key,
    own_stage_total_metric_key,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (
    SheetVitrinaV1WebVitrinaBlock,
    _PeriodDateBinding,
    _merge_period_server_cell_presentation,
    _merge_period_warehouse_history_coverage,
)
from packages.application.wb_incident_policy import save_policy_revision
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 4, 20, 9, 0, tzinfo=timezone.utc)
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-web-vitrina-contract-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if len(enabled) < 2:
            raise AssertionError("fixture must expose at least two enabled SKU rows")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-04-20T09:05:00Z",
            plan=_build_plan(
                current_state=current_state,
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
                first_group=enabled[0].group,
            ),
        )

        payload = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )

        if payload.contract_name != "web_vitrina_contract" or payload.contract_version != "v1":
            raise AssertionError(f"contract identity mismatch, got {payload}")
        if payload.page_route != "/sheet-vitrina-v1/vitrina" or payload.read_route != "/v1/sheet-vitrina-v1/web-vitrina":
            raise AssertionError(f"route fixation mismatch, got {payload}")

        if (
            payload.meta.snapshot_id != "web-vitrina-v1-fixture"
            or payload.meta.row_count != 14 + len(enabled)
        ):
            raise AssertionError(f"meta mismatch, got {payload.meta}")
        if payload.meta.date_columns != ["2026-04-19", "2026-04-20"]:
            raise AssertionError(f"meta date columns mismatch, got {payload.meta}")
        if [slot.slot_key for slot in payload.meta.temporal_slots] != ["yesterday_closed", "today_current"]:
            raise AssertionError(f"meta temporal slots mismatch, got {payload.meta}")
        if payload.meta.incident_policy_badge != {
            "active": False,
            "label": "Политика инцидентов не активна",
            "detail": "",
            "warehouse_names": [],
            "revision": 0,
            "effective_from": "",
        }:
            raise AssertionError(
                f"Vitrina policy badge must come from the read-only contract, got {payload.meta}"
            )
        quality = payload.meta.incident_projection_quality
        if (
            quality.get("state") != "provisional_received_rows"
            or quality.get("dates") != ["2026-04-20"]
            or quality.get("accepted_item_count") != 2
            or quality.get("accepted_warehouse_row_count") != 3
            or "Рассчитано по полученному снимку, полнота WB не подтверждена"
            not in quality.get("detail", "")
        ):
            raise AssertionError(
                "Vitrina contract must expose server-owned provisional quality evidence: "
                f"{quality}"
            )
        save_policy_revision(
            runtime,
            payload={
                "base_revision": 0,
                "active": True,
                "excluded_wb_warehouse_ids": [101],
                "reason": "Vitrina badge fixture",
                "effective_from": "2026-04-20",
                "effective_to": "",
                "status": "active",
            },
            actor="contract-smoke",
            warehouse_options=[
                {"warehouse_id": 101, "warehouse_name": "Fixture warehouse"},
            ],
            timestamp="2026-04-20T09:10:00Z",
        )
        active_badge_payload = SheetVitrinaV1WebVitrinaBlock(
            runtime=runtime,
            now_factory=lambda: NOW,
        ).build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
        )
        if (
            active_badge_payload.meta.incident_policy_badge.get("active") is not True
            or active_badge_payload.meta.incident_policy_badge.get("revision") != 1
            or "Fixture warehouse"
            not in active_badge_payload.meta.incident_policy_badge.get("detail", "")
        ):
            raise AssertionError(
                "Vitrina-only read contract must expose the effective policy badge"
            )
        save_policy_revision(
            runtime,
            payload={
                "base_revision": 1,
                "active": False,
                "excluded_wb_warehouse_ids": [101],
                "reason": "Vitrina badge fixture resolved",
                "effective_from": "2026-04-20",
                "effective_to": "",
                "status": "resolved",
            },
            actor="contract-smoke",
            warehouse_options=[
                {"warehouse_id": 101, "warehouse_name": "Fixture warehouse"},
            ],
            timestamp="2026-04-20T09:11:00Z",
        )

        if payload.status_summary.refresh_status != "success":
            raise AssertionError(f"status_summary.refresh_status mismatch, got {payload.status_summary}")
        if payload.status_summary.refresh_status_label != "Успешно":
            raise AssertionError(f"status_summary.refresh_status_label mismatch, got {payload.status_summary}")
        if payload.status_summary.refresh_status_tone != "success":
            raise AssertionError(f"status_summary.refresh_status_tone mismatch, got {payload.status_summary}")
        if "активных источников" not in payload.status_summary.refresh_status_reason:
            raise AssertionError(f"status_summary.refresh_status_reason mismatch, got {payload.status_summary}")
        if payload.status_summary.read_model != "persisted_ready_snapshot":
            raise AssertionError(f"status_summary.read_model mismatch, got {payload.status_summary}")
        if payload.status_summary.source_policy_counts != {
            "dual_day_capable": 2,
            "accepted_current_rollover": 1,
        }:
            raise AssertionError(f"status_summary.source_policy_counts mismatch, got {payload.status_summary}")
        if payload.status_summary.refresh_outcome_counts != {
            "success": 2,
            "warning": 0,
            "error": 0,
        }:
            raise AssertionError(f"status_summary.refresh_outcome_counts mismatch, got {payload.status_summary}")

        schema_columns = {column.column_id: column for column in payload.schema.columns}
        for required_column in ("scope_kind", "scope_label", "metric_key", "section", "date:2026-04-19", "date:2026-04-20"):
            if required_column not in schema_columns:
                raise AssertionError(f"missing schema column {required_column!r}")
        if schema_columns["date:2026-04-20"].temporal_slot_key != "today_current":
            raise AssertionError(f"temporal slot mapping mismatch, got {schema_columns['date:2026-04-20']}")

        total_row = next((row for row in payload.rows if row.row_id == "TOTAL|total_view_count"), None)
        group_row = next((row for row in payload.rows if row.scope_kind == "GROUP"), None)
        first_sku_row = next((row for row in payload.rows if row.row_id == f"SKU:{enabled[0].nm_id}|view_count"), None)
        second_sku_row = next((row for row in payload.rows if row.row_id == f"SKU:{enabled[1].nm_id}|orderSum"), None)
        if total_row is None or group_row is None or first_sku_row is None or second_sku_row is None:
            raise AssertionError(f"normalized rows missing expected scope variants, got {payload.rows}")
        if total_row.scope_label != "ИТОГО" or total_row.metric_label != "Показы в воронке":
            raise AssertionError(f"TOTAL normalization mismatch, got {total_row}")
        if group_row.group != enabled[0].group or group_row.scope_label != enabled[0].group:
            raise AssertionError(f"GROUP normalization mismatch, got {group_row}")
        if first_sku_row.nm_id != enabled[0].nm_id or first_sku_row.scope_label != enabled[0].display_name:
            raise AssertionError(f"SKU normalization mismatch, got {first_sku_row}")
        if second_sku_row.values_by_date != {"2026-04-19": 5, "2026-04-20": 7}:
            raise AssertionError(f"values_by_date mismatch, got {second_sku_row}")
        rows_by_id = {row.row_id: row for row in payload.rows}
        buyout_total = rows_by_id["TOTAL|buyoutPercent"]
        buyout_sku_rows = [
            rows_by_id[f"SKU:{item.nm_id}|buyoutPercent"]
            for item in enabled
        ]
        if buyout_total.values_by_date != {"2026-04-19": "", "2026-04-20": ""}:
            raise AssertionError("immature TOTAL buyoutPercent must be blank")
        if any(
            row.values_by_date != {"2026-04-19": "", "2026-04-20": ""}
            for row in buyout_sku_rows
        ):
            raise AssertionError("every immature enabled-SKU buyoutPercent row must be blank")
        total_cost_row = rows_by_id[f"TOTAL|{TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY}"]
        total_proxy3_row = rows_by_id[f"TOTAL|{OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY}"]
        total_margin3_row = rows_by_id[f"TOTAL|{OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY}"]
        sku_proxy3_row = rows_by_id[f"SKU:{enabled[0].nm_id}|{OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY}"]
        sku_margin3_row = rows_by_id[f"SKU:{enabled[0].nm_id}|{OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY}"]
        sku_cost_row = rows_by_id[f"SKU:{enabled[0].nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}"]
        if total_cost_row.metric_label != "Себестоимость наша, ₽/шт" or total_cost_row.format != "rub":
            raise AssertionError(f"TOTAL our WB cost metadata mismatch, got {total_cost_row}")
        if sku_proxy3_row.metric_label != "proxy прибыль 3" or sku_proxy3_row.format != "rub":
            raise AssertionError(f"SKU proxy3 metadata mismatch, got {sku_proxy3_row}")
        if (
            total_margin3_row.metric_label != "Прокси маржинальность 3 всего, %"
            or total_margin3_row.format != "percent"
        ):
            raise AssertionError(f"TOTAL proxy margin 3 metadata mismatch, got {total_margin3_row}")
        if sku_margin3_row.metric_label != "Прокси маржинальность 3, %" or sku_margin3_row.format != "percent":
            raise AssertionError(f"SKU proxy margin 3 metadata mismatch, got {sku_margin3_row}")
        row_ids = [row.row_id for row in payload.rows]
        if row_ids.index(total_margin3_row.row_id) != row_ids.index(total_proxy3_row.row_id) + 1:
            raise AssertionError("TOTAL proxy margin 3 row must immediately follow TOTAL proxy profit 3")
        if row_ids.index(sku_margin3_row.row_id) != row_ids.index(sku_proxy3_row.row_id) + 1:
            raise AssertionError("SKU proxy margin 3 row must immediately follow SKU proxy profit 3")
        if sku_cost_row.metric_label != "Себестоимость наша, ₽/шт" or sku_cost_row.format != "rub":
            raise AssertionError(f"SKU our WB cost metadata mismatch, got {sku_cost_row}")
        for archived_row_id in (
            f"TOTAL|{TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY}",
            f"SKU:{enabled[0].nm_id}|{OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY}",
            f"SKU:{enabled[0].nm_id}|{incident_stock_metric_key('fact')}",
            f"SKU:{enabled[0].nm_id}|cost_price_rub",
            f"SKU:{enabled[0].nm_id}|proxy_profit_rub",
            "TOTAL|avg_cost_price_rub",
            "TOTAL|total_proxy_profit_rub",
            "TOTAL|proxy_margin_pct_total",
        ):
            if archived_row_id in rows_by_id:
                raise AssertionError(f"archived metric leaked into active web contract: {archived_row_id}")
        seller_change_row = rows_by_id[f"SKU:{enabled[0].nm_id}|{SELLER_PRICE_CHANGE_RUB_METRIC_KEY}"]
        bid_change_row = rows_by_id[f"SKU:{enabled[0].nm_id}|{ADVERTISING_BID_CHANGE_RUB_METRIC_KEY}"]
        buyer_price_row = rows_by_id[f"SKU:{enabled[0].nm_id}|{BUYER_PRICE_RUB_METRIC_KEY}"]
        if (
            seller_change_row.metric_label != "Изменение нашей цены, ₽"
            or seller_change_row.section != "Цены"
            or seller_change_row.format != "rub"
        ):
            raise AssertionError(f"seller price action metadata mismatch, got {seller_change_row}")
        if (
            bid_change_row.metric_label != "Изменение рекламной ставки, ₽"
            or bid_change_row.section != "Реклама"
            or bid_change_row.format != "rub"
        ):
            raise AssertionError(f"advertising bid action metadata mismatch, got {bid_change_row}")
        if (
            buyer_price_row.metric_label != "Цена для покупателя, ₽"
            or buyer_price_row.section != "Цены"
            or buyer_price_row.format != "rub"
            or buyer_price_row.values_by_date != {"2026-04-19": 777.25, "2026-04-20": 778.0}
        ):
            raise AssertionError(f"buyer price metadata mismatch, got {buyer_price_row}")

        if payload.capabilities.exportable or not payload.capabilities.grid_library_agnostic:
            raise AssertionError(f"capabilities mismatch, got {payload.capabilities}")

        print("web_vitrina_contract_identity: ok ->", payload.contract_name, payload.contract_version)
        print("web_vitrina_routes: ok ->", payload.page_route, payload.read_route)
        print("web_vitrina_meta: ok ->", payload.meta.snapshot_id, payload.meta.row_count)
        print("web_vitrina_schema: ok ->", len(payload.schema.columns), "columns")
        print("web_vitrina_rows: ok ->", total_row.row_id, first_sku_row.row_id, second_sku_row.row_id)
        print("web_vitrina_capabilities: ok -> grid-library-agnostic read-only contract")

    _test_read_time_warehouse_certification_revalidation(bundle)
    _test_period_warehouse_presentation_is_preserved()
    _test_warehouse_incident_ui_contract()


def _test_read_time_warehouse_certification_revalidation(bundle: dict[str, object]) -> None:
    business_date = "2026-07-20"
    stage = "PRODUCTION_TO_FF"
    sku_metric_key = own_stage_metric_key(stage, "unit_cost_rub")
    total_metric_key = own_stage_total_metric_key(stage, "unit_cost_rub")
    with TemporaryDirectory(prefix="web-vitrina-read-time-certification-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-07-20T08:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        nm_id = int(enabled[0].nm_id)
        snapshot_only_nm_id = 999999999
        stale_green = {
            "state": "confirmed",
            "tone": "green",
            "reason": "Все расходы учтены / Подтверждено документами",
            "source": "WebCore",
        }
        plan = SheetVitrinaV1Envelope(
            plan_version="delivery_contract_v1__read_time_revalidation_smoke",
            snapshot_id="web-vitrina-read-time-revalidation",
            as_of_date=business_date,
            date_columns=[business_date],
            temporal_slots=[
                SheetVitrinaV1TemporalSlot(
                    slot_key="today_current",
                    slot_label="Today current",
                    column_date=business_date,
                )
            ],
            source_temporal_policies={},
            sheets=[
                SheetVitrinaWriteTarget(
                    sheet_name="DATA_VITRINA",
                    write_start_cell="A1",
                    write_rect="A1:C2",
                    clear_range="A:Z",
                    write_mode="overwrite",
                    partial_update_allowed=False,
                    header=["label", "key", business_date],
                    rows=[
                        ["SKU: WAC Китай → FF", f"SKU:{nm_id}|{sku_metric_key}", 130.435721],
                        ["Архивная SKU: WAC Китай → FF", f"SKU:{snapshot_only_nm_id}|{sku_metric_key}", 99.5],
                        ["Итого: WAC Китай → FF", f"TOTAL|{total_metric_key}", 113.422195],
                    ],
                    row_count=3,
                    column_count=3,
                ),
                SheetVitrinaWriteTarget(
                    sheet_name="STATUS",
                    write_start_cell="A1",
                    write_rect="A1:K1",
                    clear_range="A:Z",
                    write_mode="overwrite",
                    partial_update_allowed=False,
                    header=STATUS_HEADER,
                    rows=[],
                    row_count=0,
                    column_count=len(STATUS_HEADER),
                ),
            ],
            metadata={
                "server_cell_presentation": {
                    f"SKU:{nm_id}|{sku_metric_key}": {business_date: stale_green},
                    f"SKU:{snapshot_only_nm_id}|{sku_metric_key}": {business_date: stale_green},
                    f"TOTAL|{total_metric_key}": {business_date: stale_green},
                },
                # A period envelope can contain rows from older snapshots.  The
                # active date must revalidate only the SKU scope frozen into its
                # own source snapshot, not the unioned historical template.
                "warehouse_nm_ids_by_date": {business_date: [nm_id]},
                "warehouse_history_coverage": {
                    business_date: {
                        "status": "live",
                        "functional_version_id": "whfv_published",
                    }
                },
            },
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at="2026-07-20T08:05:00Z",
            plan=plan,
        )
        exact_state = {
            int(item.nm_id): {
                "presentation_state": "confirmed",
                "presentation_reason": "",
                "stage_presentation": {stage: {"state": "confirmed", "reason": ""}},
                "_warehouse_version_id": "whfv_published",
                "_warehouse_version_is_active": True,
            }
            for item in enabled
        }
        exact_state[nm_id] = {
            "presentation_state": "unconfirmed",
            "presentation_reason": "source_changed_provisional",
            "stage_presentation": {
                stage: {
                    "state": "unconfirmed",
                    "reason": "source_changed_provisional",
                }
            },
            "_warehouse_version_id": "whfv_published",
            "_warehouse_version_is_active": True,
        }
        exact_state[snapshot_only_nm_id] = exact_state[nm_id]
        with (
            patch.object(
                OwnProductCapitalBlock,
                "functional_warehouse_cutover_date",
                return_value="2026-07-18",
            ),
            patch.object(
                OwnProductCapitalBlock,
                "load_daily_metric_lookup",
                return_value=exact_state,
            ) as load_daily,
        ):
            payload = SheetVitrinaV1WebVitrinaBlock(
                runtime=runtime,
                now_factory=lambda: datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
            ).build(
                page_route="/sheet-vitrina-v1/vitrina",
                read_route="/v1/sheet-vitrina-v1/web-vitrina",
                as_of_date=business_date,
            )
        load_daily.assert_called_once_with(
            business_date,
            requested_nm_ids=[nm_id],
            revalidate_current_sources=True,
        )
        rows = {row.row_id: row for row in payload.rows}
        for row_id in (
            f"SKU:{nm_id}|{sku_metric_key}",
            f"TOTAL|{total_metric_key}",
        ):
            presentation = rows[row_id].presentation_by_date[business_date]
            if (
                presentation.get("state") != "unconfirmed"
                or presentation.get("tone") != "yellow"
                or "источники изменились" not in presentation.get("reason", "")
            ):
                raise AssertionError(
                    f"read-time certification must fail closed for {row_id}: {presentation}"
                )
        historical_only = rows[
            f"SKU:{snapshot_only_nm_id}|{sku_metric_key}"
        ].presentation_by_date.get(business_date)
        if historical_only is not None:
            raise AssertionError(
                "a historical-only SKU must not participate in current-date revalidation: "
                f"{historical_only}"
            )
        mismatched_state = {
            key: {
                **value,
                "_warehouse_version_id": "whfv_newer_active",
                "_warehouse_version_is_active": True,
            }
            for key, value in exact_state.items()
        }
        with (
            patch.object(
                OwnProductCapitalBlock,
                "functional_warehouse_cutover_date",
                return_value="2026-07-18",
            ),
            patch.object(
                OwnProductCapitalBlock,
                "load_daily_metric_lookup",
                return_value=mismatched_state,
            ),
        ):
            mismatched = SheetVitrinaV1WebVitrinaBlock(
                runtime=runtime,
                now_factory=lambda: datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
            ).build(
                page_route="/sheet-vitrina-v1/vitrina",
                read_route="/v1/sheet-vitrina-v1/web-vitrina",
                as_of_date=business_date,
            )
        mismatched_rows = {row.row_id: row for row in mismatched.rows}
        for row_id in (
            f"SKU:{nm_id}|{sku_metric_key}",
            f"TOTAL|{total_metric_key}",
        ):
            presentation = mismatched_rows[row_id].presentation_by_date[business_date]
            if (
                presentation.get("state") != "unavailable"
                or "одним согласованным снимком" not in presentation.get("reason", "")
            ):
                raise AssertionError(
                    "a newer active warehouse version must not certify an older ready value: "
                    f"{row_id}={presentation}"
                )
        print("web_vitrina_read_time_certification_revalidation: ok")


def _test_period_warehouse_presentation_is_preserved() -> None:
    metric_key = own_stage_metric_key("PRODUCTION_TO_FF", "unit_cost_rub")
    row_id = f"SKU:104|{metric_key}"
    source_date = "2026-07-18"
    missing_date = "2026-07-19"
    presentation = {
        "state": "unavailable",
        "tone": "neutral",
        "reason": "Исторические данные отсутствуют: exact-date источник не доказан.",
        "source": "WebCore",
    }
    snapshot = SheetVitrinaV1Envelope(
        plan_version="period-presentation-source",
        snapshot_id="period-presentation-source",
        as_of_date=source_date,
        date_columns=[source_date],
        temporal_slots=[],
        source_temporal_policies={},
        sheets=[],
        metadata={
            "server_cell_presentation": {row_id: {source_date: presentation}},
            "warehouse_history_coverage": {
                source_date: {
                    "status": "closed",
                    "functional_version_id": "whfv_20260718",
                }
            },
        },
    )
    merged = _merge_period_server_cell_presentation(
        period_date_bindings=[
            _PeriodDateBinding(source_date, source_date, source_date),
            _PeriodDateBinding(missing_date, "", "", missing=True),
        ],
        snapshots_by_as_of_date={source_date: snapshot},
        template_rows=[["SKU WAC", row_id]],
    )
    if merged[row_id][source_date] != presentation:
        raise AssertionError(f"period view must preserve exact-date reason, got {merged}")
    missing = merged[row_id][missing_date]
    if missing.get("state") != "unavailable" or "Нулевое значение не предполагается" not in missing.get("reason", ""):
        raise AssertionError(f"missing period date must have an explicit non-zero-assuming reason, got {missing}")
    coverage = _merge_period_warehouse_history_coverage(
        period_date_bindings=[
            _PeriodDateBinding(source_date, source_date, source_date),
            _PeriodDateBinding(missing_date, "", "", missing=True),
        ],
        snapshots_by_as_of_date={source_date: snapshot},
    )
    if coverage != {
        source_date: {
            "status": "closed",
            "functional_version_id": "whfv_20260718",
        }
    }:
        raise AssertionError(f"period envelope must preserve the numeric version binding, got {coverage}")
    print("web_vitrina_period_warehouse_presentation: ok")


def _test_warehouse_incident_ui_contract() -> None:
    template = (
        ROOT / "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
    ).read_text(encoding="utf-8")
    required = (
        "data-wb-incident-legacy",
        "Legacy: инциденты на складах WB",
        "data-wb-incident-drawer",
        "data-wb-incident-history",
        "policy.revision_history",
        "policy.legacy_warehouse_entries",
        "grid-template-columns: repeat(4, minmax(0, 1fr))",
        "grid-template-columns: repeat(3, minmax(0, 1fr))",
        "grid-template-columns: repeat(2, minmax(0, 1fr))",
        "grid-template-columns: minmax(0, 1fr)",
        "data-wb-incident-date-id",
        "draft.retainedDates",
        'warehouse_entries: entries',
        'Number(right.stock_quantity || 0) - Number(left.stock_quantity || 0)',
        'id === "0"',
        "overflow-x: hidden",
        "data-warehouse-documents-drawer",
        "warehouseDocumentsDrawer && warehouseDocumentsDrawer.open",
        "загружается при раскрытии",
    )
    missing = [item for item in required if item not in template]
    if missing:
        raise AssertionError(f"warehouse incident responsive/draft contract is incomplete: {missing}")
    if template.count("data-wb-incident-apply>Применить</button>") != 1:
        raise AssertionError("warehouse incident policy must expose exactly one business Apply button")
    if "data-wb-incident-effective-from" in template:
        raise AssertionError("the removed global incident effective-from field leaked back into the UI")
    if 'data-vitrina-incident-policy-badge hidden>С инцидентами</span>' in template:
        raise AssertionError("ordinary table header still exposes an incident badge")
    if template.count("loadWbIncidentPolicy();") != 1:
        raise AssertionError("incident policy may load only after the explicit legacy disclosure")
    print("web_vitrina_warehouse_incident_responsive_contract: ok")


def _build_plan(
    *,
    current_state: object,
    first_nm_id: int,
    second_nm_id: int,
    first_group: str,
) -> SheetVitrinaV1Envelope:
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="web-vitrina-v1-fixture",
        as_of_date="2026-04-19",
        date_columns=["2026-04-19", "2026-04-20"],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date="2026-04-19",
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date="2026-04-20",
            ),
        ],
        source_temporal_policies={
            "seller_funnel_snapshot": "dual_day_capable",
            "prices_snapshot": "accepted_current_rollover",
            "cost_price": "manual_overlay",
        },
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D21",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", "2026-04-19", "2026-04-20"],
                rows=[
                    ["Итого: Показы в воронке", "TOTAL|total_view_count", 100, 140],
                    [f"Группа {first_group}: Показы в воронке", f"GROUP:{first_group}|view_count", 40, 55],
                    [f"SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 20, 30],
                    [f"SKU B: Заказы, шт.", f"SKU:{second_nm_id}|orderSum", 5, 7],
                    [
                        "Итого: Себестоимость наша, ₽/шт",
                        f"TOTAL|{TOTAL_OUR_WB_UNIT_COST_RUB_METRIC_KEY}",
                        108.5,
                        "",
                    ],
                    [
                        "Итого: Доля подтверждённой себестоимости, %",
                        f"TOTAL|{TOTAL_OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY}",
                        0.727918,
                        "",
                    ],
                    [
                        "Итого: proxy прибыль 3",
                        f"TOTAL|{OUR_WB_TOTAL_PROXY_PROFIT_3_RUB_METRIC_KEY}",
                        456.78,
                        "",
                    ],
                    [
                        "Итого: Прокси маржинальность 3 всего, %",
                        f"TOTAL|{OUR_WB_PROXY_MARGIN_3_PCT_TOTAL_METRIC_KEY}",
                        0.18,
                        "",
                    ],
                    [
                        "SKU A: proxy прибыль 3",
                        f"SKU:{first_nm_id}|{OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY}",
                        123.45,
                        "",
                    ],
                    [
                        "SKU A: Прокси маржинальность 3, %",
                        f"SKU:{first_nm_id}|{OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY}",
                        0.12,
                        "",
                    ],
                    [
                        "SKU A: Себестоимость наша, ₽/шт",
                        f"SKU:{first_nm_id}|{OUR_WB_UNIT_COST_RUB_METRIC_KEY}",
                        96.2,
                        "",
                    ],
                    [
                        "SKU A: Доля подтверждённой себестоимости, %",
                        f"SKU:{first_nm_id}|{OUR_WB_COST_CONFIRMED_SHARE_PCT_METRIC_KEY}",
                        1,
                        "",
                    ],
                    [
                        "SKU A: Изменение нашей цены, ₽",
                        f"SKU:{first_nm_id}|{SELLER_PRICE_CHANGE_RUB_METRIC_KEY}",
                        "",
                        0,
                    ],
                    [
                        "SKU A: Изменение рекламной ставки, ₽",
                        f"SKU:{first_nm_id}|{ADVERTISING_BID_CHANGE_RUB_METRIC_KEY}",
                        "",
                        0,
                    ],
                    [
                        "SKU A: Цена для покупателя, ₽",
                        f"SKU:{first_nm_id}|{BUYER_PRICE_RUB_METRIC_KEY}",
                        777.25,
                        778.0,
                    ],
                    [
                        "SKU A: Остаток WB — факт, шт",
                        f"SKU:{first_nm_id}|{incident_stock_metric_key('fact')}",
                        42,
                        41,
                    ],
                    ["SKU A: Себестоимость", f"SKU:{first_nm_id}|cost_price_rub", 20, 40],
                    ["SKU A: Прибыль Proxy1", f"SKU:{first_nm_id}|proxy_profit_rub", 10, 20],
                    ["Итого: Себестоимость средняя", "TOTAL|avg_cost_price_rub", 30, 40],
                    ["Итого: Прибыль Proxy1", "TOTAL|total_proxy_profit_rub", 50, 60],
                    ["Итого: Маржа Proxy1", "TOTAL|proxy_margin_pct_total", 0.05, 0.06],
                ],
                row_count=21,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K6",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "seller_funnel_snapshot[yesterday_closed]",
                        "success",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "seller_funnel_snapshot[today_current]",
                        "success",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        2,
                        2,
                        "",
                        "",
                    ],
                    [
                        "prices_snapshot[yesterday_closed]",
                        "success",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        "2026-04-19",
                        2,
                        2,
                        "",
                        "resolution_rule=accepted_closed_from_prior_current_snapshot",
                    ],
                    [
                        "prices_snapshot[today_current]",
                        "success",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        "2026-04-20",
                        2,
                        2,
                        "",
                        "resolution_rule=accepted_current_current_attempt",
                    ],
                    [
                        "cost_price[today_current]",
                        "missing",
                        "",
                        "",
                        "2026-04-20",
                        "",
                        "",
                        2,
                        0,
                        "",
                        "authoritative COST_PRICE current state is not materialized",
                    ],
                ],
                row_count=5,
                column_count=len(STATUS_HEADER),
            ),
        ],
        metadata={
            "incident_projection_quality_by_date": {
                "2026-04-20": {
                    "state": "provisional_received_rows",
                    "label_ru": "Полнота WB не подтверждена",
                    "message_ru": (
                        "Рассчитано по полученному снимку, полнота WB не подтверждена"
                    ),
                    "accepted_item_count": 2,
                    "accepted_warehouse_row_count": 3,
                    "policy_revision": 2,
                    "policy_effective_date": "2026-04-20",
                }
            }
        },
    )


if __name__ == "__main__":
    main()
