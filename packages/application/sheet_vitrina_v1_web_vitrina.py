"""Phase-1 web-vitrina read contract built from the existing ready snapshot seam."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import math
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from packages.application.own_product_capital import OwnProductCapitalBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_archived_metrics import (
    ARCHIVED_ONLY_SOURCE_KEYS,
    ARCHIVED_PUBLIC_METRIC_KEYS,
    active_refresh_summary as _active_refresh_summary,
)
from packages.application.sheet_vitrina_v1_onec_stocks import extend_metrics_with_onec_stock_metrics
from packages.application.sheet_vitrina_v1_incident_stocks import (
    extend_metrics_with_incident_stock_metrics,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import extend_metrics_with_our_wb_cost_metrics
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS,
    OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS,
    extend_metrics_with_own_product_capital_metrics,
)
from packages.application.sheet_vitrina_v1_live_plan import (
    _own_product_capital_cell_presentation,
)
from packages.application.sheet_vitrina_v1_sku_actions import (
    extend_metrics_with_sku_action_metrics,
)
from packages.application.wb_incident_policy import get_policy_state, policy_badge
from packages.application.sheet_vitrina_v1_temporal_policy import (
    effective_source_temporal_policies,
)
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    current_business_date_iso,
    default_business_as_of_date,
    to_business_datetime,
)
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item, MetricV2Item
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope, SheetVitrinaWriteTarget
from packages.contracts.web_vitrina_contract import (
    WebVitrinaContractCapabilities,
    WebVitrinaContractMeta,
    WebVitrinaContractRow,
    WebVitrinaContractSchema,
    WebVitrinaContractSchemaColumn,
    WebVitrinaContractSchemaFilter,
    WebVitrinaContractSchemaSort,
    WebVitrinaContractStatusSummary,
    WebVitrinaContractV1,
)

WEB_VITRINA_CONTRACT_NAME = "web_vitrina_contract"
WEB_VITRINA_CONTRACT_VERSION = "v1"
WEB_VITRINA_READ_MODEL = "persisted_ready_snapshot"
WEB_VITRINA_PERIOD_READ_MODEL = "persisted_ready_snapshot_window"
WEB_VITRINA_SOURCE_SHEET_NAME = "DATA_VITRINA"
WEB_VITRINA_PERIOD_PLAN_VERSION = "delivery_contract_v1__web_vitrina_period_window_v1"
WEB_VITRINA_DEFAULT_PERIOD_DAYS = 14
FUNNEL_SECTION_LABEL = "Воронка"
FUNNEL_VIEW_METRIC_KEY = "view_count"
FUNNEL_TOTAL_VIEW_METRIC_KEY = "total_view_count"
FUNNEL_OPEN_CARD_METRIC_KEY = "open_card_count"
FUNNEL_TOTAL_OPEN_CARD_METRIC_KEY = "total_open_card_count"
FUNNEL_CTR_METRIC_KEY = "ctr"
FUNNEL_CTR_LABEL = "CTR в воронке"
FUNNEL_OPEN_CARD_LABEL = "Открытия карточки в воронке"
FUNNEL_DUPLICATE_VIEW_METRIC_KEYS = {"openCount", "total_openCount"}
@dataclass(frozen=True)
class _ScopeDescriptor:
    scope_kind: str
    scope_key: str
    scope_label: str
    group: str | None
    nm_id: int | None


@dataclass(frozen=True)
class _PeriodDateBinding:
    requested_date: str
    snapshot_as_of_date: str
    column_date: str
    missing: bool = False


class SheetVitrinaV1WebVitrinaBlock:
    """Project a stable, grid-library-agnostic contract from the existing ready snapshot."""

    def __init__(
        self,
        *,
        runtime: RegistryUploadDbBackedRuntime,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime = runtime
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def list_readable_dates(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        descending: bool = False,
    ) -> list[str]:
        try:
            exact_ready_dates = self.runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError:
            return []
        default_visible_snapshot = _load_default_visible_snapshot(
            runtime=self.runtime,
            default_as_of_date=default_business_as_of_date(self.now_factory()),
        )
        return _merge_readable_dates(
            exact_ready_dates=exact_ready_dates,
            default_visible_snapshot=default_visible_snapshot,
            business_week_dates=_default_business_period_dates(self.now_factory()),
            date_from=date_from,
            date_to=date_to,
            descending=descending,
        )

    def list_materialized_readable_dates(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        descending: bool = False,
    ) -> list[str]:
        try:
            exact_ready_dates = self.runtime.list_sheet_vitrina_ready_snapshot_dates(
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError:
            return []
        default_visible_snapshot = _load_default_visible_snapshot(
            runtime=self.runtime,
            default_as_of_date=default_business_as_of_date(self.now_factory()),
        )
        return _merge_readable_dates(
            exact_ready_dates=exact_ready_dates,
            default_visible_snapshot=default_visible_snapshot,
            business_week_dates=[],
            date_from=date_from,
            date_to=date_to,
            descending=descending,
        )

    def build(
        self,
        *,
        page_route: str,
        read_route: str,
        as_of_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> WebVitrinaContractV1:
        now = self.now_factory()
        current_state = self.runtime.load_current_state()
        _validate_period_request(as_of_date=as_of_date, date_from=date_from, date_to=date_to)
        read_model = WEB_VITRINA_READ_MODEL
        source_status_snapshot_as_of_date = ""
        if date_from and date_to:
            default_visible_snapshot = _load_default_visible_snapshot(
                runtime=self.runtime,
                default_as_of_date=default_business_as_of_date(now),
            )
            snapshot, period_date_bindings = _build_period_snapshot(
                runtime=self.runtime,
                date_from=date_from,
                date_to=date_to,
                default_visible_snapshot=default_visible_snapshot,
            )
            refreshed_at = _resolve_period_refreshed_at(
                runtime=self.runtime,
                period_date_bindings=period_date_bindings,
            )
            period_refresh_summary = _resolve_period_refresh_summary(
                runtime=self.runtime,
                period_date_bindings=period_date_bindings,
            )
            source_status_snapshot_as_of_date = _last_materialized_snapshot_as_of_date(period_date_bindings)
            data_sheet_row_count = len(snapshot.sheets[0].rows) if snapshot.sheets else 0
            read_model = WEB_VITRINA_PERIOD_READ_MODEL
        else:
            if as_of_date:
                snapshot = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
            else:
                snapshot = _load_default_visible_snapshot(
                    runtime=self.runtime,
                    default_as_of_date=default_business_as_of_date(now),
                )
                if snapshot is None:
                    raise ValueError("sheet_vitrina_v1 ready snapshot missing: no readable snapshots are materialized")
            refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=snapshot.as_of_date)
            refreshed_at = refresh_status.refreshed_at
            period_refresh_summary = _active_refresh_summary(refresh_status)
            source_status_snapshot_as_of_date = snapshot.as_of_date
            data_sheet_row_count = refresh_status.sheet_row_counts.get(WEB_VITRINA_SOURCE_SHEET_NAME, 0)
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        manual_state = self.runtime.load_sheet_vitrina_manual_operator_state()
        load_window_status = _resolve_latest_load_window_status(
            runtime=self.runtime,
            now=now,
        )
        data_sheet = _require_data_sheet(snapshot)

        config_by_nm_id = {
            int(item.nm_id): item
            for item in current_state.config_v2
        }
        effective_metrics = extend_metrics_with_sku_action_metrics(
            extend_metrics_with_incident_stock_metrics(
                extend_metrics_with_own_product_capital_metrics(
                    extend_metrics_with_our_wb_cost_metrics(
                        extend_metrics_with_onec_stock_metrics(current_state.metrics_v2)
                    )
                )
            )
        )
        metrics_by_key = {
            str(item.metric_key): item
            for item in effective_metrics
        }
        server_cell_presentation = _read_time_warehouse_cell_presentation(
            runtime=self.runtime,
            now=now,
            snapshot=snapshot,
            enabled_config=[item for item in current_state.config_v2 if item.enabled],
            displayed_metrics=effective_metrics,
        )
        rows = _normalize_rows(
            data_sheet.rows,
            date_columns=snapshot.date_columns,
            config_by_nm_id=config_by_nm_id,
            metrics_by_key=metrics_by_key,
            row_updated_at_by_id=_resolve_row_updated_at_by_id(
                snapshot,
                fallback_updated_at=refreshed_at,
            ),
            server_cell_presentation=server_cell_presentation,
        )
        rows = _apply_funnel_operator_presentation(rows, date_columns=snapshot.date_columns)
        source_temporal_policies = effective_source_temporal_policies(snapshot.source_temporal_policies)
        current_incident_policy = get_policy_state(
            self.runtime,
            snapshot_date=current_business_date_iso(now),
        )

        return WebVitrinaContractV1(
            contract_name=WEB_VITRINA_CONTRACT_NAME,
            contract_version=WEB_VITRINA_CONTRACT_VERSION,
            page_route=page_route,
            read_route=read_route,
            meta=WebVitrinaContractMeta(
                snapshot_id=snapshot.snapshot_id,
                bundle_version=current_state.bundle_version,
                as_of_date=snapshot.as_of_date,
                business_timezone=CANONICAL_BUSINESS_TIMEZONE_NAME,
                date_columns=list(snapshot.date_columns),
                temporal_slots=list(snapshot.temporal_slots),
                generated_at=_to_utc_timestamp(now),
                refreshed_at=refreshed_at,
                row_count=len(rows),
                incident_policy_badge=policy_badge(current_incident_policy),
            ),
            status_summary=WebVitrinaContractStatusSummary(
                refresh_status=str(period_refresh_summary["status"]),
                refresh_status_label=str(period_refresh_summary["label"]),
                refresh_status_tone=str(period_refresh_summary["tone"]),
                refresh_status_reason=str(period_refresh_summary["reason"]),
                read_model=read_model,
                source_sheet_name=WEB_VITRINA_SOURCE_SHEET_NAME,
                bundle_version=current_state.bundle_version,
                activated_at=current_state.activated_at,
                refreshed_at=refreshed_at,
                business_now=to_business_datetime(now).replace(microsecond=0).isoformat(),
                current_business_date=current_business_date_iso(now),
                default_as_of_date=default_business_as_of_date(now),
                source_status_snapshot_as_of_date=source_status_snapshot_as_of_date,
                last_auto_run_status=auto_update_state.last_run_status or "never",
                last_auto_run_started_at=auto_update_state.last_run_started_at,
                last_auto_run_finished_at=auto_update_state.last_run_finished_at,
                last_successful_auto_update_at=auto_update_state.last_successful_auto_update_at,
                last_successful_manual_refresh_at=manual_state.last_successful_manual_refresh_at,
                last_successful_manual_load_at=manual_state.last_successful_manual_load_at,
                source_policy_counts=_count_values(source_temporal_policies),
                source_count=len(source_temporal_policies),
                data_sheet_row_count=data_sheet_row_count or len(rows),
                refresh_outcome_counts=dict(period_refresh_summary["counts"]),
                load_window_status=load_window_status,
            ),
            schema=_build_schema(snapshot),
            rows=rows,
            capabilities=WebVitrinaContractCapabilities(
                sortable=True,
                filterable=True,
                exportable=False,
                read_only=True,
                grid_library_agnostic=True,
                thin_page_shell=True,
            ),
        )


def _read_time_warehouse_cell_presentation(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    now: datetime,
    snapshot: SheetVitrinaV1Envelope,
    enabled_config: list[ConfigV2Item],
    displayed_metrics: list[MetricV2Item],
) -> dict[str, dict[str, dict[str, str]]]:
    """Revalidate the active warehouse date before serving persisted UI state.

    Ready snapshots remain immutable historical evidence, but their presentation
    metadata must not keep a certified/green interpretation after a mutable
    supplier source changed and targeted replay is queued or failed.  Only the
    current functional business date is revalidated; closed dates continue to
    use their exact persisted version instead of current evidence.
    """

    snapshot_metadata = dict(getattr(snapshot, "metadata", {}) or {})
    raw_presentation = snapshot_metadata.get("server_cell_presentation")
    presentation: dict[str, dict[str, dict[str, str]]] = (
        deepcopy(raw_presentation) if isinstance(raw_presentation, Mapping) else {}
    )
    business_date = current_business_date_iso(now)
    if business_date not in {str(value) for value in snapshot.date_columns}:
        return presentation

    capital = OwnProductCapitalBlock(runtime=runtime)
    cutover_date = capital.functional_warehouse_cutover_date()
    if not cutover_date or business_date < cutover_date:
        return presentation

    frozen_config = _frozen_snapshot_warehouse_config(
        snapshot=snapshot,
        current_enabled_config=enabled_config,
        business_date=business_date,
    )
    exact_state = capital.load_daily_metric_lookup(
        business_date,
        requested_nm_ids=[item.nm_id for item in frozen_config],
        revalidate_current_sources=True,
    )

    # A persisted warning or implicit green state is only valid for the frozen
    # source fingerprint.  Clear the active date for canonical warehouse rows,
    # then rebuild it from the same centralized presentation function used by
    # the heavy publisher.  Unrelated metrics and closed dates are untouched.
    for row_id in list(presentation):
        metric_key = str(row_id).split("|", 1)[1] if "|" in str(row_id) else ""
        by_date = presentation.get(row_id)
        if metric_key not in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS) or not isinstance(by_date, dict):
            continue
        by_date.pop(business_date, None)
        if not by_date:
            presentation.pop(row_id, None)

    coverage_by_date = snapshot_metadata.get("warehouse_history_coverage")
    published_version_id = ""
    if isinstance(coverage_by_date, Mapping):
        published_coverage = coverage_by_date.get(business_date)
        if isinstance(published_coverage, Mapping):
            published_version_id = str(
                published_coverage.get("functional_version_id") or ""
            )
    loaded_version_ids = {
        str(state.get("_warehouse_version_id") or "")
        for state in exact_state.values()
        if str(state.get("_warehouse_version_id") or "")
    }
    loaded_version_is_active = bool(exact_state) and all(
        bool(state.get("_warehouse_version_is_active"))
        for state in exact_state.values()
    )
    if (
        not published_version_id
        or loaded_version_ids != {published_version_id}
        or not loaded_version_is_active
    ):
        reason = (
            "Исторические данные отсутствуют: числовое значение витрины и текущая "
            "функциональная версия склада ещё не опубликованы одним согласованным "
            "снимком. Старое значение скрыто до targeted publication."
        )
        metric_keys = {
            item.metric_key
            for item in displayed_metrics
            if item.metric_key in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
        }
        for item in frozen_config:
            for metric_key in metric_keys & set(OWN_PRODUCT_CAPITAL_SKU_METRIC_KEYS):
                presentation.setdefault(f"SKU:{item.nm_id}|{metric_key}", {})[
                    business_date
                ] = {
                    "state": "unavailable",
                    "tone": "neutral",
                    "reason": reason,
                    "source": "WebCore",
                }
        for metric_key in metric_keys & set(OWN_PRODUCT_CAPITAL_TOTAL_METRIC_KEYS):
            presentation.setdefault(f"TOTAL|{metric_key}", {})[business_date] = {
                "state": "unavailable",
                "tone": "neutral",
                "reason": reason,
                "source": "WebCore",
            }
        return presentation

    revalidated = _own_product_capital_cell_presentation(
        enabled_config=frozen_config,
        displayed_metrics=displayed_metrics,
        temporal_slots=[
            SimpleNamespace(
                slot_key="read_time_current",
                slot_label="read_time_current",
                column_date=business_date,
            )
        ],
        live_sources=SimpleNamespace(
            slot_lookups={
                "read_time_current": SimpleNamespace(
                    own_product_capital_lookup=exact_state,
                    own_product_capital_cutover_date=cutover_date,
                )
            }
        ),
    )
    for row_id, by_date in revalidated.items():
        presentation.setdefault(row_id, {}).update(by_date)
    return presentation


def _frozen_snapshot_warehouse_config(
    *,
    snapshot: SheetVitrinaV1Envelope,
    current_enabled_config: list[ConfigV2Item],
    business_date: str | None = None,
) -> list[ConfigV2Item]:
    """Recover the SKU scope frozen into a ready warehouse snapshot."""

    current_by_nm_id = {int(item.nm_id): item for item in current_enabled_config}
    scope_by_date = dict(getattr(snapshot, "metadata", {}) or {}).get(
        "warehouse_nm_ids_by_date"
    )
    restricted_scope: set[int] | None = None
    if isinstance(scope_by_date, Mapping) and business_date in scope_by_date:
        raw_scope = scope_by_date.get(business_date)
        if isinstance(raw_scope, list):
            restricted_scope = {
                int(value)
                for value in raw_scope
                if str(value).strip().isdigit() and int(value) > 0
            }
    frozen: dict[int, ConfigV2Item] = {}
    for row_order, row in enumerate(_require_data_sheet(snapshot).rows, start=1):
        row_id = str(row[1] or "").strip() if len(row) > 1 else ""
        if "|" not in row_id:
            continue
        scope_token, metric_key = row_id.split("|", 1)
        if (
            not scope_token.startswith("SKU:")
            or metric_key not in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
        ):
            continue
        try:
            nm_id = int(scope_token.split(":", 1)[1])
        except ValueError:
            continue
        if restricted_scope is not None and nm_id not in restricted_scope:
            continue
        frozen.setdefault(
            nm_id,
            current_by_nm_id.get(nm_id)
            or ConfigV2Item(
                nm_id=nm_id,
                enabled=True,
                display_name=_label_prefix(str(row[0] or "")) or str(nm_id),
                group="",
                display_order=row_order,
            ),
        )
    if restricted_scope is not None:
        return list(frozen.values())
    return list(frozen.values()) or list(current_enabled_config)


def _validate_period_request(
    *,
    as_of_date: str | None,
    date_from: str | None,
    date_to: str | None,
) -> None:
    normalized_as_of_date = str(as_of_date or "").strip()
    normalized_date_from = str(date_from or "").strip()
    normalized_date_to = str(date_to or "").strip()
    if normalized_as_of_date and (normalized_date_from or normalized_date_to):
        raise ValueError("as_of_date is mutually exclusive with date_from/date_to")
    if bool(normalized_date_from) != bool(normalized_date_to):
        raise ValueError("date_from and date_to must be provided together")
    if not normalized_date_from:
        return
    start = date.fromisoformat(normalized_date_from)
    end = date.fromisoformat(normalized_date_to)
    if end < start:
        raise ValueError("date_to must be >= date_from")


def _build_period_snapshot(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
    default_visible_snapshot: SheetVitrinaV1Envelope | None,
) -> tuple[SheetVitrinaV1Envelope, list[_PeriodDateBinding]]:
    period_date_bindings = _resolve_period_date_bindings(
        runtime=runtime,
        date_from=date_from,
        date_to=date_to,
        default_visible_snapshot=default_visible_snapshot,
    )
    selected_dates = [binding.requested_date for binding in period_date_bindings]
    snapshots_by_as_of_date: dict[str, SheetVitrinaV1Envelope] = {}
    if default_visible_snapshot is not None:
        snapshots_by_as_of_date[default_visible_snapshot.as_of_date] = default_visible_snapshot
    for binding in period_date_bindings:
        if binding.missing:
            continue
        snapshots_by_as_of_date.setdefault(
            binding.snapshot_as_of_date,
            runtime.load_sheet_vitrina_ready_snapshot_any_bundle(as_of_date=binding.snapshot_as_of_date),
        )
    materialized_bindings = [binding for binding in period_date_bindings if not binding.missing]
    if not materialized_bindings:
        raise ValueError("web_vitrina period window has no materialized row template")
    template_sheets = _period_template_sheets(
        snapshots_by_as_of_date=snapshots_by_as_of_date,
        materialized_bindings=materialized_bindings,
        default_visible_snapshot=default_visible_snapshot,
    )
    template_sheet = template_sheets[0]
    template_rows = _merge_period_template_rows(template_sheets)
    value_maps = {
        binding.requested_date: _extract_snapshot_values_by_row_id(
            _require_data_sheet(snapshots_by_as_of_date[binding.snapshot_as_of_date]),
            expected_date=binding.column_date,
        )
        for binding in period_date_bindings
        if not binding.missing
    }
    combined_presentation = _merge_period_server_cell_presentation(
        period_date_bindings=period_date_bindings,
        snapshots_by_as_of_date=snapshots_by_as_of_date,
        template_rows=template_rows,
    )

    combined_rows: list[list[Any]] = []
    for row in template_rows:
        row_id = str(row[1] or "").strip()
        if not row_id:
            continue
        combined_row = [row[0], row_id]
        for snapshot_date in selected_dates:
            values_by_row_id = value_maps.get(snapshot_date)
            if values_by_row_id is None:
                combined_row.append(None)
                continue
            combined_row.append(values_by_row_id.get(row_id))
        combined_rows.append(combined_row)

    return SheetVitrinaV1Envelope(
        plan_version=WEB_VITRINA_PERIOD_PLAN_VERSION,
        snapshot_id=f"{date_from}__{date_to}__web_vitrina_period_window_v1__ready",
        as_of_date=date_to,
        date_columns=selected_dates,
        temporal_slots=[
            _build_period_temporal_slot(snapshot_date)
            for snapshot_date in selected_dates
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name=WEB_VITRINA_SOURCE_SHEET_NAME,
                write_start_cell=template_sheet.write_start_cell,
                write_rect=template_sheet.write_rect,
                clear_range=template_sheet.clear_range,
                write_mode=template_sheet.write_mode,
                partial_update_allowed=template_sheet.partial_update_allowed,
                header=[template_sheet.header[0], template_sheet.header[1], *selected_dates],
                rows=combined_rows,
                row_count=len(combined_rows),
                column_count=2 + len(selected_dates),
            )
        ],
        metadata={
            "server_cell_presentation": combined_presentation,
            "warehouse_history_coverage": _merge_period_warehouse_history_coverage(
                period_date_bindings=period_date_bindings,
                snapshots_by_as_of_date=snapshots_by_as_of_date,
            ),
            "warehouse_nm_ids_by_date": {
                binding.requested_date: sorted(
                    _warehouse_nm_ids_in_snapshot(
                        snapshots_by_as_of_date[binding.snapshot_as_of_date]
                    )
                )
                for binding in period_date_bindings
                if not binding.missing
            },
        },
    ), period_date_bindings


def _merge_period_warehouse_history_coverage(
    *,
    period_date_bindings: list[_PeriodDateBinding],
    snapshots_by_as_of_date: Mapping[str, SheetVitrinaV1Envelope],
) -> dict[str, dict[str, Any]]:
    """Carry each exact numeric warehouse version into a period envelope."""

    result: dict[str, dict[str, Any]] = {}
    for binding in period_date_bindings:
        if binding.missing:
            continue
        snapshot = snapshots_by_as_of_date[binding.snapshot_as_of_date]
        coverage_by_date = dict(getattr(snapshot, "metadata", {}) or {}).get(
            "warehouse_history_coverage"
        )
        if not isinstance(coverage_by_date, Mapping):
            continue
        coverage = coverage_by_date.get(binding.column_date)
        if isinstance(coverage, Mapping):
            result[binding.requested_date] = deepcopy(dict(coverage))
    return result


def _warehouse_nm_ids_in_snapshot(snapshot: SheetVitrinaV1Envelope) -> set[int]:
    """Return the warehouse SKU scope owned by one immutable source snapshot."""

    result: set[int] = set()
    for row in _require_data_sheet(snapshot).rows:
        row_id = str(row[1] or "").strip() if len(row) > 1 else ""
        if "|" not in row_id:
            continue
        scope_token, metric_key = row_id.split("|", 1)
        if (
            not scope_token.startswith("SKU:")
            or metric_key not in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
        ):
            continue
        try:
            nm_id = int(scope_token.split(":", 1)[1])
        except ValueError:
            continue
        if nm_id > 0:
            result.add(nm_id)
    return result


def _merge_period_server_cell_presentation(
    *,
    period_date_bindings: list[_PeriodDateBinding],
    snapshots_by_as_of_date: Mapping[str, SheetVitrinaV1Envelope],
    template_rows: list[list[Any]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Preserve exact-date warehouse explanations in a multi-day read."""

    result: dict[str, dict[str, dict[str, str]]] = {}
    canonical_row_ids = {
        str(row[1] or "")
        for row in template_rows
        if len(row) > 1
        and "|" in str(row[1] or "")
        and str(row[1] or "").split("|", 1)[1] in set(OWN_PRODUCT_CAPITAL_METRIC_KEYS)
    }
    for binding in period_date_bindings:
        if binding.missing:
            for row_id in canonical_row_ids:
                result.setdefault(row_id, {})[binding.requested_date] = {
                    "state": "unavailable",
                    "tone": "neutral",
                    "reason": (
                        "Исторические данные отсутствуют: exact-date ready snapshot этой "
                        "бизнес-даты не материализован. Нулевое значение не предполагается."
                    ),
                    "source": "WebCore",
                }
            continue
        snapshot = snapshots_by_as_of_date[binding.snapshot_as_of_date]
        raw = dict(getattr(snapshot, "metadata", {}) or {}).get(
            "server_cell_presentation"
        )
        if not isinstance(raw, Mapping):
            continue
        for row_id, by_date in raw.items():
            if not isinstance(by_date, Mapping):
                continue
            presentation = by_date.get(binding.column_date)
            if isinstance(presentation, Mapping):
                result.setdefault(str(row_id), {})[binding.requested_date] = {
                    str(key): str(value) for key, value in presentation.items()
                }
    return result


def _period_template_sheets(
    *,
    snapshots_by_as_of_date: Mapping[str, SheetVitrinaV1Envelope],
    materialized_bindings: list[_PeriodDateBinding],
    default_visible_snapshot: SheetVitrinaV1Envelope | None,
) -> list[SheetVitrinaWriteTarget]:
    preferred_snapshot_keys: list[str] = []
    if default_visible_snapshot is not None:
        preferred_snapshot_keys.append(default_visible_snapshot.as_of_date)
    preferred_snapshot_keys.extend(
        binding.snapshot_as_of_date
        for binding in reversed(materialized_bindings)
        if binding.snapshot_as_of_date
    )
    preferred_snapshot_keys.extend(
        binding.snapshot_as_of_date
        for binding in materialized_bindings
        if binding.snapshot_as_of_date
    )

    seen: set[str] = set()
    sheets: list[SheetVitrinaWriteTarget] = []
    for snapshot_key in preferred_snapshot_keys:
        if not snapshot_key or snapshot_key in seen or snapshot_key not in snapshots_by_as_of_date:
            continue
        seen.add(snapshot_key)
        sheets.append(_require_data_sheet(snapshots_by_as_of_date[snapshot_key]))
    if not sheets:
        raise ValueError("web_vitrina period window has no materialized row template")
    return sheets


def _merge_period_template_rows(template_sheets: list[SheetVitrinaWriteTarget]) -> list[list[Any]]:
    rows_by_id: dict[str, list[Any]] = {}
    for sheet in template_sheets:
        for row in sheet.rows:
            row_id = str(row[1] or "").strip() if len(row) > 1 else ""
            if not row_id or row_id in rows_by_id:
                continue
            rows_by_id[row_id] = list(row[:2])
    return list(rows_by_id.values())


def _resolve_period_date_bindings(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    date_from: str,
    date_to: str,
    default_visible_snapshot: SheetVitrinaV1Envelope | None,
) -> list[_PeriodDateBinding]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    expected_dates = [
        (start + timedelta(days=offset)).isoformat()
        for offset in range((end - start).days + 1)
    ]
    exact_ready_dates = set(
        runtime.list_sheet_vitrina_ready_snapshot_dates_any_bundle(
            date_from=date_from,
            date_to=date_to,
        )
    )
    readable_dates = set(exact_ready_dates)
    if default_visible_snapshot is not None:
        readable_dates.update(
            _merge_readable_dates(
                exact_ready_dates=[],
                default_visible_snapshot=default_visible_snapshot,
                business_week_dates=[],
                date_from=date_from,
                date_to=date_to,
                descending=False,
            )
        )
    period_date_bindings: list[_PeriodDateBinding] = []
    default_visible_date_set = (
        {str(value) for value in default_visible_snapshot.date_columns}
        if default_visible_snapshot is not None
        else set()
    )
    for requested_date in expected_dates:
        if requested_date in exact_ready_dates:
            period_date_bindings.append(
                _PeriodDateBinding(
                    requested_date=requested_date,
                    snapshot_as_of_date=requested_date,
                    column_date=requested_date,
                )
            )
            continue
        if default_visible_snapshot is not None and requested_date in default_visible_date_set:
            period_date_bindings.append(
                _PeriodDateBinding(
                    requested_date=requested_date,
                    snapshot_as_of_date=default_visible_snapshot.as_of_date,
                    column_date=requested_date,
                )
            )
            continue
        period_date_bindings.append(
            _PeriodDateBinding(
                requested_date=requested_date,
                snapshot_as_of_date="",
                column_date=requested_date,
                missing=True,
            )
        )
    return period_date_bindings


def _extract_snapshot_values_by_row_id(
    data_sheet: SheetVitrinaWriteTarget,
    *,
    expected_date: str,
) -> dict[str, Any]:
    try:
        column_index = data_sheet.header.index(expected_date)
    except ValueError as exc:
        raise ValueError(
            f"ready snapshot DATA_VITRINA does not contain expected date column {expected_date}"
        ) from exc
    values_by_row_id: dict[str, Any] = {}
    for row in data_sheet.rows:
        row_id = str(row[1] or "").strip() if len(row) > 1 else ""
        if not row_id:
            continue
        values_by_row_id[row_id] = row[column_index] if column_index < len(row) else None
    return values_by_row_id


def _resolve_period_refreshed_at(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    period_date_bindings: list[_PeriodDateBinding],
) -> str:
    refreshed_values = [
        runtime.load_sheet_vitrina_refresh_status_any_bundle(as_of_date=snapshot_as_of_date).refreshed_at
        for snapshot_as_of_date in sorted(
            {
                binding.snapshot_as_of_date
                for binding in period_date_bindings
                if not binding.missing and binding.snapshot_as_of_date
            }
        )
    ]
    if not refreshed_values:
        return ""
    return max(refreshed_values)


def _resolve_period_refresh_summary(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    period_date_bindings: list[_PeriodDateBinding],
) -> dict[str, Any]:
    statuses = [
        runtime.load_sheet_vitrina_refresh_status_any_bundle(as_of_date=snapshot_as_of_date)
        for snapshot_as_of_date in sorted(
            {
                binding.snapshot_as_of_date
                for binding in period_date_bindings
                if not binding.missing and binding.snapshot_as_of_date
            }
        )
    ]
    active_summaries = [_active_refresh_summary(item) for item in statuses]
    counts = {"success": 0, "warning": 0, "error": 0}
    for item in active_summaries:
        if item["status"] in counts:
            counts[item["status"]] += 1
    missing_count = sum(1 for binding in period_date_bindings if binding.missing)
    if any(item["status"] == "error" for item in active_summaries):
        status = "error"
        reason = (
            f"В выбранном периоде {counts['error']} snapshot с ошибками; "
            f"ещё {counts['warning']} требуют внимания."
        )
    elif any(item["status"] == "warning" for item in active_summaries) or missing_count:
        status = "warning"
        reason = f"В выбранном периоде {counts['warning']} snapshot требуют внимания; {missing_count} дат пока без ready snapshot."
    else:
        status = "success"
        reason = f"Все {len(statuses)} snapshot в выбранном периоде подтверждены без warning/error."
    label = "Успешно" if status == "success" else ("Ошибка" if status == "error" else "Внимание")
    return {
        "status": status,
        "label": label,
        "tone": status,
        "reason": reason,
        "counts": {**counts, "missing": missing_count},
    }


def _resolve_latest_load_window_status(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    now: datetime,
) -> dict[str, Any]:
    yesterday_closed_date = default_business_as_of_date(now)
    today_current_date = current_business_date_iso(now)
    window_label = (
        f"today_current={today_current_date}; "
        f"yesterday_closed={yesterday_closed_date}; "
        f"tz={CANONICAL_BUSINESS_TIMEZONE_NAME}"
    )
    try:
        refresh_status = runtime.load_sheet_vitrina_refresh_status(as_of_date=yesterday_closed_date)
    except ValueError as exc:
        return {
            "status": "warning",
            "label": "Нужно загрузить",
            "tone": "warning",
            "reason": f"Последняя загрузка для окна {window_label} ещё не materialized: {exc}",
            "refreshed_at": "",
            "snapshot_as_of_date": yesterday_closed_date,
            "today_current_date": today_current_date,
            "yesterday_closed_date": yesterday_closed_date,
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "counts": {"success": 0, "warning": 1, "error": 0},
        }
    slot_today = _refresh_status_temporal_slot_date(refresh_status, "today_current") or today_current_date
    slot_yesterday = _refresh_status_temporal_slot_date(refresh_status, "yesterday_closed") or yesterday_closed_date
    active_summary = _active_refresh_summary(refresh_status)
    reason = str(active_summary["reason"] or "").strip()
    return {
        "status": str(active_summary["status"] or "warning"),
        "label": str(active_summary["label"] or ""),
        "tone": str(active_summary["tone"] or active_summary["status"] or "warning"),
        "reason": f"Последняя загрузка для окна {window_label}: {reason}" if reason else f"Последняя загрузка для окна {window_label}.",
        "refreshed_at": str(refresh_status.refreshed_at or ""),
        "snapshot_as_of_date": str(refresh_status.as_of_date or yesterday_closed_date),
        "today_current_date": slot_today,
        "yesterday_closed_date": slot_yesterday,
        "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
        "counts": dict(active_summary["counts"]),
    }


def _refresh_status_temporal_slot_date(refresh_status: Any, slot_key: str) -> str:
    for slot in getattr(refresh_status, "temporal_slots", []) or []:
        if str(getattr(slot, "slot_key", "") or "") == slot_key:
            return str(getattr(slot, "column_date", "") or "")
    return ""


def _load_default_visible_snapshot(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    default_as_of_date: str,
) -> SheetVitrinaV1Envelope | None:
    try:
        return runtime.load_sheet_vitrina_ready_snapshot(as_of_date=default_as_of_date)
    except ValueError:
        try:
            return runtime.load_sheet_vitrina_ready_snapshot()
        except ValueError:
            return None


def _merge_readable_dates(
    *,
    exact_ready_dates: list[str],
    default_visible_snapshot: SheetVitrinaV1Envelope | None,
    business_week_dates: list[str],
    date_from: str | None,
    date_to: str | None,
    descending: bool,
) -> list[str]:
    readable_dates = {str(item) for item in exact_ready_dates if item}
    if default_visible_snapshot is not None:
        readable_dates.update(str(item) for item in default_visible_snapshot.date_columns if item)
    readable_dates.update(str(item) for item in business_week_dates if item)
    filtered_dates = sorted(
        snapshot_date
        for snapshot_date in readable_dates
        if (not date_from or snapshot_date >= date_from)
        and (not date_to or snapshot_date <= date_to)
    )
    if descending:
        filtered_dates.reverse()
    return filtered_dates


def _default_business_period_dates(now: datetime) -> list[str]:
    today = date.fromisoformat(current_business_date_iso(now))
    return [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(WEB_VITRINA_DEFAULT_PERIOD_DAYS - 1, -1, -1)
    ]


def _last_materialized_snapshot_as_of_date(bindings: list[_PeriodDateBinding]) -> str:
    values = [
        binding.snapshot_as_of_date
        for binding in bindings
        if not binding.missing and binding.snapshot_as_of_date
    ]
    return values[-1] if values else ""


def _build_period_temporal_slot(snapshot_date: str):
    from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot

    return SheetVitrinaV1TemporalSlot(
        slot_key=f"period_window:{snapshot_date}",
        slot_label=snapshot_date,
        column_date=snapshot_date,
    )


def _require_data_sheet(snapshot: SheetVitrinaV1Envelope) -> SheetVitrinaWriteTarget:
    data_sheet = next((item for item in snapshot.sheets if item.sheet_name == WEB_VITRINA_SOURCE_SHEET_NAME), None)
    if data_sheet is None:
        raise ValueError(f"ready snapshot {snapshot.as_of_date} does not contain {WEB_VITRINA_SOURCE_SHEET_NAME}")
    return data_sheet


def _normalize_rows(
    rows: list[list[Any]],
    *,
    date_columns: list[str],
    config_by_nm_id: Mapping[int, ConfigV2Item],
    metrics_by_key: Mapping[str, MetricV2Item],
    row_updated_at_by_id: Mapping[str, str],
    server_cell_presentation: Mapping[str, Any] | None = None,
) -> list[WebVitrinaContractRow]:
    normalized: list[WebVitrinaContractRow] = []
    for row_order, row in enumerate(rows, start=1):
        if len(row) < 2:
            continue
        row_id = str(row[1] or "").strip()
        if not row_id or "|" not in row_id:
            continue
        scope_token, metric_key = row_id.split("|", 1)
        if metric_key in ARCHIVED_PUBLIC_METRIC_KEYS:
            continue
        metric = metrics_by_key.get(metric_key)
        scope = _parse_scope(scope_token, row_label=str(row[0] or ""), config_by_nm_id=config_by_nm_id)
        values_by_date = {
            column_date: row[index]
            for index, column_date in enumerate(date_columns, start=2)
        }
        normalized.append(
            WebVitrinaContractRow(
                row_id=row_id,
                row_order=row_order,
                scope_kind=scope.scope_kind,
                scope_key=scope.scope_key,
                scope_label=scope.scope_label,
                metric_key=metric_key,
                metric_label=metric.label_ru if metric is not None else metric_key,
                row_last_updated_at=str(row_updated_at_by_id.get(row_id) or ""),
                section=metric.section if metric is not None else "",
                group=scope.group,
                nm_id=scope.nm_id,
                format=metric.format if metric is not None else None,
                values_by_date=values_by_date,
                presentation_by_date={
                    str(column_date): dict(presentation)
                    for column_date, presentation in (
                        (server_cell_presentation or {}).get(row_id, {})
                    ).items()
                    if isinstance(presentation, Mapping)
                },
            )
        )
    return normalized


def _apply_funnel_operator_presentation(
    rows: list[WebVitrinaContractRow],
    *,
    date_columns: list[str],
) -> list[WebVitrinaContractRow]:
    rows_by_scope_metric: dict[tuple[str, str], WebVitrinaContractRow] = {
        (row.scope_key, row.metric_key): row
        for row in rows
    }
    inserted_ctr_row_ids: set[str] = set()
    presented: list[WebVitrinaContractRow] = []

    for row in rows:
        if _is_funnel_duplicate_view_row(row):
            continue
        if row.row_id in inserted_ctr_row_ids:
            continue

        if _is_funnel_view_row(row):
            presented.append(row)
            ctr_row = _build_funnel_ctr_row(
                row,
                rows_by_scope_metric=rows_by_scope_metric,
                date_columns=date_columns,
            )
            if ctr_row is not None:
                presented.append(ctr_row)
                inserted_ctr_row_ids.add(ctr_row.row_id)
            continue

        if _is_funnel_ctr_row(row):
            ctr_row = _build_funnel_ctr_row(
                row,
                rows_by_scope_metric=rows_by_scope_metric,
                date_columns=date_columns,
            )
            if ctr_row is not None:
                presented.append(ctr_row)
                inserted_ctr_row_ids.add(ctr_row.row_id)
            continue

        if _is_funnel_open_card_row(row):
            presented.append(replace(row, metric_label=FUNNEL_OPEN_CARD_LABEL))
            continue

        presented.append(row)

    return [
        replace(row, row_order=index)
        for index, row in enumerate(presented, start=1)
    ]


def _is_funnel_row(row: WebVitrinaContractRow) -> bool:
    return row.section == FUNNEL_SECTION_LABEL


def _is_funnel_duplicate_view_row(row: WebVitrinaContractRow) -> bool:
    return _is_funnel_row(row) and row.metric_key in FUNNEL_DUPLICATE_VIEW_METRIC_KEYS


def _is_funnel_view_row(row: WebVitrinaContractRow) -> bool:
    if not _is_funnel_row(row):
        return False
    if row.scope_kind == "TOTAL":
        return row.metric_key == FUNNEL_TOTAL_VIEW_METRIC_KEY
    return row.metric_key == FUNNEL_VIEW_METRIC_KEY


def _is_funnel_open_card_row(row: WebVitrinaContractRow) -> bool:
    if not _is_funnel_row(row):
        return False
    if row.scope_kind == "TOTAL":
        return row.metric_key == FUNNEL_TOTAL_OPEN_CARD_METRIC_KEY
    return row.metric_key == FUNNEL_OPEN_CARD_METRIC_KEY


def _is_funnel_ctr_row(row: WebVitrinaContractRow) -> bool:
    if not _is_funnel_row(row):
        return False
    if row.scope_kind == "TOTAL":
        return row.metric_key == FUNNEL_CTR_METRIC_KEY
    return row.metric_key == FUNNEL_CTR_METRIC_KEY


def _build_funnel_ctr_row(
    template_row: WebVitrinaContractRow,
    *,
    rows_by_scope_metric: Mapping[tuple[str, str], WebVitrinaContractRow],
    date_columns: list[str],
) -> WebVitrinaContractRow | None:
    if template_row.scope_kind == "TOTAL":
        numerator_key = FUNNEL_TOTAL_OPEN_CARD_METRIC_KEY
        denominator_key = FUNNEL_TOTAL_VIEW_METRIC_KEY
        metric_key = FUNNEL_CTR_METRIC_KEY
    else:
        numerator_key = FUNNEL_OPEN_CARD_METRIC_KEY
        denominator_key = FUNNEL_VIEW_METRIC_KEY
        metric_key = FUNNEL_CTR_METRIC_KEY

    numerator_row = rows_by_scope_metric.get((template_row.scope_key, numerator_key))
    denominator_row = rows_by_scope_metric.get((template_row.scope_key, denominator_key))
    if numerator_row is None or denominator_row is None:
        return None

    values_by_date = {
        column_date: _funnel_ctr_value(
            numerator=numerator_row.values_by_date.get(column_date),
            denominator=denominator_row.values_by_date.get(column_date),
        )
        for column_date in date_columns
    }

    return WebVitrinaContractRow(
        row_id=f"{template_row.scope_key}|{metric_key}",
        row_order=template_row.row_order,
        scope_kind=template_row.scope_kind,
        scope_key=template_row.scope_key,
        scope_label=template_row.scope_label,
        metric_key=metric_key,
        metric_label=FUNNEL_CTR_LABEL,
        row_last_updated_at=numerator_row.row_last_updated_at or denominator_row.row_last_updated_at,
        section=FUNNEL_SECTION_LABEL,
        group=template_row.group,
        nm_id=template_row.nm_id,
        format="percent",
        values_by_date=values_by_date,
    )


def _funnel_ctr_value(*, numerator: Any, denominator: Any) -> Any:
    numerator_value = _numeric_value(numerator)
    denominator_value = _numeric_value(denominator)
    if numerator_value is None or denominator_value in (None, 0):
        return ""
    return round(float(numerator_value) / float(denominator_value), 6)


def _numeric_value(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, bool):
        return None
    number: float
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    try:
        number = float(str(value).strip().replace(",", "."))
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _parse_scope(
    scope_token: str,
    *,
    row_label: str,
    config_by_nm_id: Mapping[int, ConfigV2Item],
) -> _ScopeDescriptor:
    if scope_token == "TOTAL":
        return _ScopeDescriptor(
            scope_kind="TOTAL",
            scope_key="TOTAL",
            scope_label="ИТОГО",
            group=None,
            nm_id=None,
        )

    if scope_token.startswith("GROUP:"):
        group_name = scope_token.split(":", 1)[1]
        return _ScopeDescriptor(
            scope_kind="GROUP",
            scope_key=scope_token,
            scope_label=group_name,
            group=group_name,
            nm_id=None,
        )

    if scope_token.startswith("SKU:"):
        raw_nm_id = scope_token.split(":", 1)[1]
        nm_id = None
        try:
            nm_id = int(raw_nm_id)
        except ValueError:
            nm_id = None
        config_item = config_by_nm_id.get(nm_id) if nm_id is not None else None
        return _ScopeDescriptor(
            scope_kind="SKU",
            scope_key=scope_token,
            scope_label=(config_item.display_name if config_item is not None else _label_prefix(row_label)),
            group=(config_item.group if config_item is not None else None),
            nm_id=nm_id,
        )

    return _ScopeDescriptor(
        scope_kind="OTHER",
        scope_key=scope_token,
        scope_label=_label_prefix(row_label),
        group=None,
        nm_id=None,
    )


def _resolve_row_updated_at_by_id(
    snapshot: SheetVitrinaV1Envelope,
    *,
    fallback_updated_at: str,
) -> dict[str, str]:
    metadata = dict(getattr(snapshot, "metadata", {}) or {})
    raw_values = metadata.get("row_last_updated_at_by_row_id")
    if isinstance(raw_values, Mapping):
        resolved = {
            str(row_id): str(updated_at)
            for row_id, updated_at in raw_values.items()
            if str(row_id) and str(updated_at)
        }
        for row_id, updated_at in _fallback_row_updated_at_by_id(
            snapshot,
            fallback_updated_at=fallback_updated_at,
        ).items():
            resolved.setdefault(row_id, updated_at)
        return resolved
    return _fallback_row_updated_at_by_id(snapshot, fallback_updated_at=fallback_updated_at)


def _fallback_row_updated_at_by_id(
    snapshot: SheetVitrinaV1Envelope,
    *,
    fallback_updated_at: str,
) -> dict[str, str]:
    row_updated_at: dict[str, str] = {}
    for sheet in snapshot.sheets:
        if sheet.sheet_name != WEB_VITRINA_SOURCE_SHEET_NAME:
            continue
        for row in sheet.rows:
            row_id = str(row[1] or "").strip() if len(row) > 1 else ""
            if row_id:
                row_updated_at[row_id] = fallback_updated_at
    return row_updated_at


def _build_schema(snapshot: SheetVitrinaV1Envelope) -> WebVitrinaContractSchema:
    temporal_slot_by_date = {
        slot.column_date: slot.slot_key
        for slot in snapshot.temporal_slots
    }
    columns = [
        WebVitrinaContractSchemaColumn(
            column_id="row_order",
            label="№",
            kind="identity",
            value_type="integer",
            sortable=True,
            filterable=False,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="scope_kind",
            label="Тип",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="scope_key",
            label="Ключ объекта",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="scope_label",
            label="Объект",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="group",
            label="Группа",
            kind="dimension",
            value_type="string_or_null",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="nm_id",
            label="SKU",
            kind="dimension",
            value_type="integer_or_null",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="metric_key",
            label="Ключ метрики",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="metric_label",
            label="Метрика",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
        WebVitrinaContractSchemaColumn(
            column_id="section",
            label="Раздел",
            kind="dimension",
            value_type="string",
            sortable=True,
            filterable=True,
        ),
    ]
    columns.extend(
        WebVitrinaContractSchemaColumn(
            column_id=f"date:{column_date}",
            label=column_date,
            kind="temporal_value",
            value_type="number_or_blank",
            sortable=True,
            filterable=False,
            column_date=column_date,
            temporal_slot_key=temporal_slot_by_date.get(column_date),
        )
        for column_date in snapshot.date_columns
    )

    filters = [
        WebVitrinaContractSchemaFilter(
            filter_id="scope_kind",
            field="scope_kind",
            label="Тип",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="group",
            field="group",
            label="Группа",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="nm_id",
            field="nm_id",
            label="SKU",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="section",
            field="section",
            label="Раздел",
            operators=["eq", "in"],
        ),
        WebVitrinaContractSchemaFilter(
            filter_id="metric_key",
            field="metric_key",
            label="Ключ метрики",
            operators=["eq", "in"],
        ),
    ]

    sorts = [
        WebVitrinaContractSchemaSort(
            sort_id="row_order",
            field="row_order",
            label="Порядок",
            directions=["asc", "desc"],
            default_direction="asc",
        ),
        WebVitrinaContractSchemaSort(
            sort_id="scope_label",
            field="scope_label",
            label="Объект",
            directions=["asc", "desc"],
        ),
        WebVitrinaContractSchemaSort(
            sort_id="metric_label",
            field="metric_label",
            label="Метрика",
            directions=["asc", "desc"],
        ),
    ]
    sorts.extend(
        WebVitrinaContractSchemaSort(
            sort_id=f"date:{column_date}",
            field=f"date:{column_date}",
            label=column_date,
            directions=["asc", "desc"],
        )
        for column_date in snapshot.date_columns
    )

    return WebVitrinaContractSchema(
        row_identity_fields=["row_id"],
        columns=columns,
        filters=filters,
        sorts=sorts,
    )


def _count_values(items: Mapping[str, str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in items.values():
        counts[value] = counts.get(value, 0) + 1
    return counts


def _label_prefix(value: str) -> str:
    return str(value).split(": ", 1)[0] if ": " in str(value) else str(value)


def _to_utc_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
