"""Application-слой HTTP entrypoint для registry upload и sheet_vitrina_v1 operator flow."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_daily_report import SheetVitrinaV1DailyReportBlock
from packages.application.sheet_vitrina_v1_load_bridge import load_sheet_vitrina_ready_snapshot_via_clasp
from packages.application.sheet_vitrina_v1_plan_report import SheetVitrinaV1PlanReportBlock
from packages.application.sheet_vitrina_v1_stock_report import SheetVitrinaV1StockReportBlock
from packages.application.sheet_vitrina_v1_stock_report import list_active_sku_options
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.wb_regional_supply import WbRegionalSupplyBlock
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    DAILY_REFRESH_BUSINESS_HOURS,
    DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
    DAILY_REFRESH_SYSTEMD_UTC_TIME,
    current_business_date_iso,
    default_business_as_of_date,
    to_business_datetime,
)
from packages.application.sheet_vitrina_v1_live_plan import (
    BLOCKED_SOURCE_METRIC_KEYS,
    CLOSURE_PENDING_STATES,
    DELIVERY_CONTRACT_VERSION,
    EXECUTION_MODE_AUTO_DAILY,
    EXECUTION_MODE_MANUAL_OPERATOR,
    EXECUTION_MODE_PERSISTED_RETRY,
    HISTORICAL_CLOSED_DAY_SOURCE_KEYS,
    SOURCE_DIAGNOSTIC_SPECS,
    SheetVitrinaV1LivePlanBlock,
    CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS,
    TEMPORAL_SLOT_YESTERDAY_CLOSED,
    TEMPORAL_SLOT_TODAY_CURRENT,
)
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

OperatorLogEmitter = Callable[[str], None]
SheetLoadRunner = Callable[[SheetVitrinaV1Envelope, OperatorLogEmitter], dict[str, Any]]
SHEET_VITRINA_REFRESH_ROUTE = "/v1/sheet-vitrina-v1/refresh"
SHEET_VITRINA_LOAD_ROUTE = "/v1/sheet-vitrina-v1/load"
SHEET_VITRINA_DAILY_TIMER_NAME = "wb-core-sheet-vitrina-refresh.timer"
SHEET_VITRINA_DAILY_AUTO_ACTION = "загрузка данных + отправка данных в таблицу"
SHEET_VITRINA_DAILY_BUSINESS_TIMES = ", ".join(
    f"{hour:02d}:00" for hour in DAILY_REFRESH_BUSINESS_HOURS
)
SHEET_VITRINA_DAILY_AUTO_DESCRIPTION = (
    f"Ежедневно в {SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}: "
    f"{SHEET_VITRINA_DAILY_AUTO_ACTION}"
)
SHEET_VITRINA_DAILY_TRIGGER_DESCRIPTION = (
    f"{SHEET_VITRINA_DAILY_TIMER_NAME} -> POST {SHEET_VITRINA_REFRESH_ROUTE} (auto_load=true) "
    f"в {SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}"
)
SHEET_VITRINA_RETRY_RUNNER_DESCRIPTION = (
    "Persisted retry runner: дожимает due yesterday_closed для historical/date-period families "
    "и same-day today_current только для WB API current-snapshot-only families; manual refresh такие хвосты не создаёт."
)


class RegistryUploadHttpEntrypoint:
    """Тонкий entrypoint: ingest/update current truth, heavy refresh и cheap read готового snapshot."""

    def __init__(
        self,
        runtime_dir: Path,
        runtime: RegistryUploadDbBackedRuntime | None = None,
        activated_at_factory: Callable[[], str] | None = None,
        refreshed_at_factory: Callable[[], str] | None = None,
        now_factory: Callable[[], datetime] | None = None,
        sheet_load_runner: SheetLoadRunner | None = None,
    ) -> None:
        self.runtime = runtime or RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        self.activated_at_factory = activated_at_factory or _default_activated_at_factory
        self.refreshed_at_factory = refreshed_at_factory or _default_activated_at_factory
        self.now_factory = now_factory or _default_now_factory
        self._sheet_cycle_lock = threading.RLock()
        self.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=self.runtime,
            promo_live_source_block=PromoLiveSourceBlock(runtime_dir=self.runtime.runtime_dir),
        )
        self.daily_report_block = SheetVitrinaV1DailyReportBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.stock_report_block = SheetVitrinaV1StockReportBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.plan_report_block = SheetVitrinaV1PlanReportBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.web_vitrina_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.sheet_load_runner = sheet_load_runner or load_sheet_vitrina_ready_snapshot_via_clasp
        self.operator_jobs = SheetVitrinaV1OperatorJobStore(timestamp_factory=self.activated_at_factory)
        self.factory_order_supply_block = FactoryOrderSupplyBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )
        self.wb_regional_supply_block = WbRegionalSupplyBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
            timestamp_factory=self.activated_at_factory,
        )

    def handle_bundle_payload(self, payload: Mapping[str, Any]) -> RegistryUploadResult:
        return self.runtime.ingest_bundle(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_cost_price_payload(self, payload: Mapping[str, Any]) -> CostPriceUploadResult:
        return self.runtime.ingest_cost_price_payload(
            payload,
            activated_at=self.activated_at_factory(),
        )

    def handle_sheet_plan_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return asdict(self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date))

    def handle_sheet_status_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        payload = asdict(self.runtime.load_sheet_vitrina_refresh_status(as_of_date=as_of_date))
        payload["server_context"] = self.build_sheet_server_context()
        payload["manual_context"] = self.build_sheet_manual_context()
        return payload

    def handle_sheet_daily_report_request(self) -> dict[str, Any]:
        return self.daily_report_block.build()

    def handle_sheet_stock_report_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self.stock_report_block.build(as_of_date=as_of_date)

    def handle_sheet_plan_report_request(
        self,
        *,
        period: str,
        q1_buyout_plan_rub: float,
        q2_buyout_plan_rub: float,
        q3_buyout_plan_rub: float,
        q4_buyout_plan_rub: float,
        plan_drr_pct: float,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        return self.plan_report_block.build(
            period=period,
            q1_buyout_plan_rub=q1_buyout_plan_rub,
            q2_buyout_plan_rub=q2_buyout_plan_rub,
            q3_buyout_plan_rub=q3_buyout_plan_rub,
            q4_buyout_plan_rub=q4_buyout_plan_rub,
            plan_drr_pct=plan_drr_pct,
            as_of_date=as_of_date,
        )

    def handle_sheet_web_vitrina_request(
        self,
        *,
        page_route: str,
        read_route: str,
        as_of_date: str | None = None,
    ) -> dict[str, Any]:
        return asdict(
            self.web_vitrina_block.build(
                page_route=page_route,
                read_route=read_route,
                as_of_date=as_of_date,
            )
        )

    def handle_sheet_refresh_request(
        self,
        as_of_date: str | None = None,
        *,
        auto_load: bool = False,
    ) -> dict[str, Any]:
        if auto_load:
            return self._run_sheet_auto_update(as_of_date=as_of_date, log=None)
        return self._run_sheet_refresh(as_of_date=as_of_date, log=None)

    def handle_sheet_load_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self._run_sheet_load(as_of_date=as_of_date, log=None)

    def start_sheet_refresh_job(
        self,
        as_of_date: str | None = None,
        *,
        auto_load: bool = False,
    ) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="auto_update" if auto_load else "refresh",
            runner=(
                (lambda log: self._run_sheet_auto_update(as_of_date=as_of_date, log=log))
                if auto_load
                else (lambda log: self._run_sheet_refresh(as_of_date=as_of_date, log=log))
            ),
        )

    def start_sheet_load_job(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="load",
            runner=lambda log: self._run_sheet_load(as_of_date=as_of_date, log=log),
        )

    def handle_sheet_operator_job_request(self, job_id: str) -> dict[str, Any]:
        return self.operator_jobs.get(job_id)

    def handle_sheet_operator_job_text_request(self, job_id: str) -> tuple[str, str]:
        return self.operator_jobs.get_text(job_id)

    def run_sheet_temporal_closure_retry_cycle(
        self,
        *,
        target_dates: list[str] | None = None,
        auto_load_visible: bool = True,
        log: OperatorLogEmitter | None = None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        requested_dates = sorted({value for value in (target_dates or []) if value})
        default_visible_as_of_date = default_business_as_of_date(self.now_factory())
        current_business_date = current_business_date_iso(self.now_factory())
        due_closed_states = self.sheet_plan_block.list_due_closed_day_retries()
        due_current_states = self.sheet_plan_block.list_due_current_capture_retries(
            current_date=current_business_date
        )
        due_closed_dates = sorted({state.target_date for state in due_closed_states})
        scheduled_dates = sorted(set(requested_dates) | set(due_closed_dates))
        if due_current_states:
            scheduled_dates = sorted(set(scheduled_dates) | {default_visible_as_of_date})

        emit(
            _format_log_event(
                "closure_retry_cycle_start",
                requested_dates=",".join(requested_dates),
                due_closed_dates=",".join(due_closed_dates),
                due_current_capture_sources=",".join(sorted({state.source_key for state in due_current_states})),
                due_current_capture_date=current_business_date if due_current_states else "",
                scheduled_dates=",".join(scheduled_dates),
                historical_sources=",".join(sorted(HISTORICAL_CLOSED_DAY_SOURCE_KEYS)),
                current_capture_sources=",".join(sorted(CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS)),
            )
        )

        refresh_results: list[dict[str, Any]] = []
        with self._sheet_cycle_lock:
            for as_of_date in scheduled_dates:
                emit(
                    _format_log_event(
                        "closure_retry_refresh_start",
                        as_of_date=as_of_date,
                    )
                )
                refresh_payload = self._run_sheet_refresh(
                    as_of_date=as_of_date,
                    log=emit,
                    execution_mode=EXECUTION_MODE_PERSISTED_RETRY,
                )
                refresh_results.append(
                    {
                        "as_of_date": as_of_date,
                        "snapshot_id": refresh_payload["snapshot_id"],
                        "refreshed_at": refresh_payload["refreshed_at"],
                    }
                )

            load_result: dict[str, Any] | None = None
            if auto_load_visible and default_visible_as_of_date in scheduled_dates:
                emit(
                    _format_log_event(
                        "closure_retry_load_start",
                        as_of_date=default_visible_as_of_date,
                    )
                )
                load_result = self._run_sheet_load(
                    as_of_date=default_visible_as_of_date,
                    log=emit,
                    execution_mode=EXECUTION_MODE_PERSISTED_RETRY,
                )

            closure_states = self.runtime.list_temporal_source_closure_states(
                source_keys=sorted(HISTORICAL_CLOSED_DAY_SOURCE_KEYS | CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS),
                states=sorted(CLOSURE_PENDING_STATES),
            )
            payload = {
                "status": "success",
                "operation": "temporal_closure_retry_cycle",
                "requested_dates": requested_dates,
                "due_closed_dates": due_closed_dates,
                "due_current_capture_date": current_business_date if due_current_states else "",
                "scheduled_dates": scheduled_dates,
                "refreshed_dates": refresh_results,
                "visible_load_result": load_result,
                "pending_closure_states": [
                    {
                        "source_key": state.source_key,
                        "target_date": state.target_date,
                        "slot_kind": state.slot_kind,
                        "state": state.state,
                        "attempt_count": state.attempt_count,
                        "next_retry_at": state.next_retry_at,
                        "last_reason": state.last_reason,
                        "accepted_at": state.accepted_at,
                    }
                    for state in closure_states
                    if (
                        state.slot_kind == TEMPORAL_SLOT_YESTERDAY_CLOSED
                        or (
                            state.slot_kind == TEMPORAL_SLOT_TODAY_CURRENT
                            and state.target_date == current_business_date
                        )
                    )
                ],
                "server_context": self.build_sheet_server_context(),
                "manual_context": self.build_sheet_manual_context(),
            }
        emit(
            _format_log_event(
                "closure_retry_cycle_finish",
                scheduled_dates=",".join(scheduled_dates),
                refreshed=len(refresh_results),
                loaded_visible=str(bool(load_result)).lower(),
                pending_states=len(payload["pending_closure_states"]),
            )
        )
        return payload

    def handle_factory_order_status_request(self) -> dict[str, Any]:
        payload = asdict(self.factory_order_supply_block.build_status())
        payload["recommendation_download_path"] = "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
        return payload

    def handle_factory_order_template_request(self, dataset_type: str) -> tuple[bytes, str]:
        return self.factory_order_supply_block.build_template(dataset_type)

    def handle_factory_order_upload_request(
        self,
        dataset_type: str,
        workbook_bytes: bytes,
        *,
        uploaded_filename: str | None = None,
        uploaded_content_type: str | None = None,
    ) -> dict[str, Any]:
        return asdict(
            self.factory_order_supply_block.upload_dataset(
                dataset_type,
                workbook_bytes,
                uploaded_filename=uploaded_filename,
                uploaded_content_type=uploaded_content_type,
            )
        )

    def handle_factory_order_uploaded_file_request(self, dataset_type: str) -> tuple[bytes, str, str]:
        return self.factory_order_supply_block.download_uploaded_dataset(dataset_type)

    def handle_factory_order_delete_request(self, dataset_type: str) -> dict[str, Any]:
        return asdict(self.factory_order_supply_block.delete_dataset(dataset_type))

    def handle_factory_order_calculate_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        result = asdict(self.factory_order_supply_block.calculate(payload))
        result["recommendation_download_path"] = "/v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
        return result

    def handle_factory_order_recommendation_request(self) -> tuple[bytes, str]:
        return self.factory_order_supply_block.download_recommendation()

    def handle_wb_regional_status_request(self) -> dict[str, Any]:
        return asdict(self.wb_regional_supply_block.build_status())

    def handle_wb_regional_calculate_request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return asdict(self.wb_regional_supply_block.calculate(payload))

    def handle_wb_regional_district_recommendation_request(self, district_key: str) -> tuple[bytes, str]:
        return self.wb_regional_supply_block.download_district_recommendation(district_key)

    def _run_sheet_auto_update(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        with self._sheet_cycle_lock:
            started_at = self.activated_at_factory()
            requested_as_of_date = as_of_date or default_business_as_of_date(self.now_factory())
            self.runtime.mark_sheet_vitrina_auto_update_started(
                started_at=started_at,
                as_of_date=requested_as_of_date,
            )
            emit(
                _format_log_event(
                    "cycle_start",
                    cycle="auto_update",
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    requested_as_of_date=requested_as_of_date,
                    action="build_ready_snapshot_and_write_sheet",
                    trigger=SHEET_VITRINA_DAILY_TIMER_NAME,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
            )
            refresh_payload: dict[str, Any] | None = None
            try:
                refresh_payload = self._run_sheet_refresh(
                    as_of_date=as_of_date,
                    log=emit,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
                load_payload = self._run_sheet_load(
                    as_of_date=str(refresh_payload["as_of_date"]),
                    log=emit,
                    execution_mode=EXECUTION_MODE_AUTO_DAILY,
                )
            except Exception as exc:
                finished_at = self.activated_at_factory()
                self.runtime.save_sheet_vitrina_auto_update_result(
                    started_at=started_at,
                    finished_at=finished_at,
                    status="error",
                    as_of_date=(
                        str(refresh_payload["as_of_date"])
                        if refresh_payload is not None
                        else requested_as_of_date
                    ),
                    snapshot_id=(
                        str(refresh_payload["snapshot_id"])
                        if refresh_payload is not None
                        else None
                    ),
                    refreshed_at=(
                        str(refresh_payload["refreshed_at"])
                        if refresh_payload is not None
                        else None
                    ),
                    error=str(exc),
                )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="auto_update",
                        status="error",
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        error=str(exc),
                    )
                )
                raise

            finished_at = self.activated_at_factory()
            self.runtime.save_sheet_vitrina_auto_update_result(
                started_at=started_at,
                finished_at=finished_at,
                status="success",
                as_of_date=str(load_payload["as_of_date"]),
                snapshot_id=str(load_payload["snapshot_id"]),
                refreshed_at=str(load_payload["refreshed_at"]),
                error=None,
            )
            emit(
                _format_log_event(
                    "cycle_finish",
                    cycle="auto_update",
                    status="success",
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    snapshot_id=load_payload["snapshot_id"],
                )
            )
            payload = dict(load_payload)
            payload["operation"] = "auto_update"
            payload["auto_update_started_at"] = started_at
            payload["auto_update_finished_at"] = finished_at
            payload["server_context"] = self.build_sheet_server_context()
            payload["manual_context"] = self.build_sheet_manual_context()
            return payload

    def _run_sheet_refresh(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
        execution_mode: str = EXECUTION_MODE_MANUAL_OPERATOR,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        with self._sheet_cycle_lock:
            current_state = self.runtime.load_current_state()
            emit(
                _format_log_event(
                    "cycle_start",
                    cycle="refresh",
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    requested_as_of_date=as_of_date or "default",
                    action="build_ready_snapshot_only",
                    execution_mode=execution_mode,
                )
            )
            emit(
                _format_log_event(
                    "bundle_selected",
                    cycle="refresh",
                    bundle_version=current_state.bundle_version,
                    activated_at=current_state.activated_at,
                )
            )
            emit(
                _format_log_event(
                    "refresh_build_start",
                    cycle="refresh",
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    step="server_build_plan",
                )
            )
            plan = self.sheet_plan_block.build_plan(
                as_of_date=as_of_date,
                log=emit,
                execution_mode=execution_mode,
            )
            row_counts = _sheet_row_counts(plan)
            emit(
                _format_log_event(
                    "refresh_snapshot_ready",
                    cycle="refresh",
                    snapshot_id=plan.snapshot_id,
                    plan_version=plan.plan_version,
                    as_of_date=plan.as_of_date,
                    date_columns=",".join(plan.date_columns),
                    data_rows=row_counts.get("DATA_VITRINA"),
                    status_rows=row_counts.get("STATUS"),
                )
            )
            emit(
                _format_log_event(
                    "refresh_runtime_save_start",
                    cycle="refresh",
                    runtime_store="sheet_vitrina_ready_snapshot",
                    snapshot_id=plan.snapshot_id,
                )
            )
            refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current_state,
                refreshed_at=self.refreshed_at_factory(),
                plan=plan,
            )
            if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                self.runtime.save_sheet_vitrina_manual_refresh_success(
                    refreshed_at=refresh_result.refreshed_at,
                )
            payload = asdict(refresh_result)
            payload["server_context"] = self.build_sheet_server_context()
            payload["manual_context"] = self.build_sheet_manual_context()
            emit(
                _format_log_event(
                    "refresh_runtime_save_finish",
                    cycle="refresh",
                    snapshot_id=refresh_result.snapshot_id,
                    refreshed_at=refresh_result.refreshed_at,
                    data_rows=refresh_result.sheet_row_counts.get("DATA_VITRINA"),
                    status_rows=refresh_result.sheet_row_counts.get("STATUS"),
                )
            )
            emit(
                _format_log_event(
                    "cycle_finish",
                    cycle="refresh",
                    status="success",
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    snapshot_id=refresh_result.snapshot_id,
                )
            )
            return payload

    def _run_sheet_load(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
        execution_mode: str = EXECUTION_MODE_MANUAL_OPERATOR,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        with self._sheet_cycle_lock:
            current_state = self.runtime.load_current_state()
            emit(
                _format_log_event(
                    "cycle_start",
                    cycle="load",
                    route=SHEET_VITRINA_LOAD_ROUTE,
                    requested_as_of_date=as_of_date or "latest_bundle_snapshot",
                    action="write_prepared_snapshot_only",
                    execution_mode=execution_mode,
                )
            )
            emit(
                _format_log_event(
                    "bundle_selected",
                    cycle="load",
                    bundle_version=current_state.bundle_version,
                    activated_at=current_state.activated_at,
                )
            )
            emit(
                _format_log_event(
                    "snapshot_lookup_start",
                    cycle="load",
                    route=SHEET_VITRINA_LOAD_ROUTE,
                    requested_as_of_date=as_of_date or "latest",
                )
            )
            plan = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
            refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=plan.as_of_date)
            row_counts = _sheet_row_counts(plan)
            emit(
                _format_log_event(
                    "snapshot_lookup_finish",
                    cycle="load",
                    snapshot_id=plan.snapshot_id,
                    plan_version=plan.plan_version,
                    as_of_date=plan.as_of_date,
                    date_columns=",".join(plan.date_columns),
                    refreshed_at=refresh_status.refreshed_at,
                    data_rows=row_counts.get("DATA_VITRINA"),
                    status_rows=row_counts.get("STATUS"),
                )
            )
            _emit_plan_status_sheet_log(plan, emit, cycle="load")
            _emit_plan_metric_sheet_log(plan, emit, cycle="load")
            emit(
                _format_log_event(
                    "bridge_start",
                    cycle="load",
                    snapshot_id=plan.snapshot_id,
                    bridge_runner=getattr(self.sheet_load_runner, "__name__", self.sheet_load_runner.__class__.__name__),
                )
            )
            bridge_result = self.sheet_load_runner(plan, emit)
            finished_at = self.activated_at_factory()
            if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                self.runtime.save_sheet_vitrina_manual_load_success(loaded_at=finished_at)
            _emit_bridge_result_log(bridge_result, emit, cycle="load")
            emit(
                _format_log_event(
                    "cycle_finish",
                    cycle="load",
                    status="success",
                    route=SHEET_VITRINA_LOAD_ROUTE,
                    snapshot_id=plan.snapshot_id,
                    data_rows=row_counts.get("DATA_VITRINA"),
                    status_rows=row_counts.get("STATUS"),
                )
            )
            payload = {
                "status": "success",
                "operation": "load",
                "bundle_version": current_state.bundle_version,
                "activated_at": current_state.activated_at,
                "refreshed_at": refresh_status.refreshed_at,
                "as_of_date": plan.as_of_date,
                "date_columns": plan.date_columns,
                "temporal_slots": [asdict(item) for item in plan.temporal_slots],
                "snapshot_id": plan.snapshot_id,
                "plan_version": plan.plan_version,
                "sheet_row_counts": row_counts,
                "bridge_result": bridge_result,
            }
            payload["server_context"] = self.build_sheet_server_context()
            payload["manual_context"] = self.build_sheet_manual_context()
            return payload

    def build_sheet_server_context(self) -> dict[str, str]:
        now = self.now_factory()
        business_now = to_business_datetime(now).replace(microsecond=0).isoformat()
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        return {
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "business_now": business_now,
            "default_as_of_date": default_business_as_of_date(now),
            "today_current_date": current_business_date_iso(now),
            "daily_refresh_business_time": f"{SHEET_VITRINA_DAILY_BUSINESS_TIMES} {CANONICAL_BUSINESS_TIMEZONE_NAME}",
            "daily_refresh_systemd_time": DAILY_REFRESH_SYSTEMD_UTC_TIME,
            "daily_refresh_systemd_oncalendar": DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
            "daily_auto_action": SHEET_VITRINA_DAILY_AUTO_ACTION,
            "daily_auto_description": SHEET_VITRINA_DAILY_AUTO_DESCRIPTION,
            "daily_auto_trigger_name": SHEET_VITRINA_DAILY_TIMER_NAME,
            "daily_auto_trigger_description": SHEET_VITRINA_DAILY_TRIGGER_DESCRIPTION,
            "retry_runner_description": SHEET_VITRINA_RETRY_RUNNER_DESCRIPTION,
            "last_auto_run_status": auto_update_state.last_run_status or "never",
            "last_auto_run_status_label": _auto_update_status_label(auto_update_state.last_run_status),
            "last_auto_run_time": _format_optional_business_timestamp(auto_update_state.last_run_started_at),
            "last_auto_run_finished_at": _format_optional_business_timestamp(auto_update_state.last_run_finished_at),
            "last_successful_auto_update_at": _format_optional_business_timestamp(
                auto_update_state.last_successful_auto_update_at
            ),
            "last_auto_run_error": auto_update_state.last_run_error or "",
        }

    def build_sheet_manual_context(self) -> dict[str, str]:
        manual_state = self.runtime.load_sheet_vitrina_manual_operator_state()
        return {
            "last_successful_manual_refresh_at": _format_optional_business_timestamp(
                manual_state.last_successful_manual_refresh_at
            ),
            "last_successful_manual_load_at": _format_optional_business_timestamp(
                manual_state.last_successful_manual_load_at
            ),
        }

    def build_sheet_operator_ui_context(self) -> dict[str, Any]:
        try:
            current_state = self.runtime.load_current_state()
        except ValueError:
            active_skus: list[dict[str, Any]] = []
        else:
            active_skus = list_active_sku_options(current_state.config_v2)
        return {
            "stock_report_active_skus": active_skus,
            "stock_report_active_sku_count": len(active_skus),
            "stock_report_active_sku_source": "current_registry_config_v2",
        }


def _default_activated_at_factory() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_now_factory() -> datetime:
    return datetime.now(timezone.utc)


def _format_optional_business_timestamp(value: str | None) -> str:
    if not value:
        return ""
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return to_business_datetime(instant).replace(microsecond=0).isoformat()


def _auto_update_status_label(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "success":
        return "успех"
    if normalized == "error":
        return "ошибка"
    if normalized == "running":
        return "выполняется"
    return "ещё не выполнялся"


@dataclass
class SheetVitrinaV1OperatorJob:
    job_id: str
    operation: str
    status: str
    started_at: str
    log_lines: list[str] = field(default_factory=list)
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_lines": list(self.log_lines),
            "log_line_count": len(self.log_lines),
        }
        if self.result is not None:
            payload["result"] = self.result
        if self.error is not None:
            payload["error"] = self.error
        return payload


class SheetVitrinaV1OperatorJobStore:
    def __init__(self, timestamp_factory: Callable[[], str]) -> None:
        self.timestamp_factory = timestamp_factory
        self._jobs: dict[str, SheetVitrinaV1OperatorJob] = {}
        self._lock = threading.Lock()

    def start(
        self,
        *,
        operation: str,
        runner: Callable[[OperatorLogEmitter], dict[str, Any]],
    ) -> dict[str, Any]:
        job_id = uuid4().hex
        job = SheetVitrinaV1OperatorJob(
            job_id=job_id,
            operation=operation,
            status="running",
            started_at=self.timestamp_factory(),
        )
        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run,
            args=(job_id, runner),
            daemon=True,
        )
        thread.start()
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError(f"sheet_vitrina_v1 operator job not found: {job_id}")
            return job.snapshot()

    def get_text(self, job_id: str) -> tuple[str, str]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise ValueError(f"sheet_vitrina_v1 operator job not found: {job_id}")
            text = "\n".join(job.log_lines).rstrip()
            if text:
                text = f"{text}\n"
            filename = f"sheet-vitrina-v1-{job.operation}-{job.job_id}.txt"
            return text or "Лог пока пуст.\n", filename

    def _run(
        self,
        job_id: str,
        runner: Callable[[OperatorLogEmitter], dict[str, Any]],
    ) -> None:
        try:
            result = runner(lambda message: self._append_log(job_id, message))
        except Exception as exc:
            self._append_log(job_id, f"Ошибка: {exc}")
            with self._lock:
                job = self._jobs[job_id]
                job.status = "error"
                job.finished_at = self.timestamp_factory()
                job.error = str(exc)
            return

        with self._lock:
            job = self._jobs[job_id]
            job.status = "success"
            job.finished_at = self.timestamp_factory()
            job.result = result

    def _append_log(self, job_id: str, message: str) -> None:
        timestamp = self.timestamp_factory()
        with self._lock:
            job = self._jobs[job_id]
            job.log_lines.append(f"{timestamp} {message}")
            if len(job.log_lines) > 4000:
                job.log_lines = job.log_lines[-4000:]


def _sheet_row_counts(plan: SheetVitrinaV1Envelope) -> dict[str, int]:
    return {item.sheet_name: item.row_count for item in plan.sheets}


def _find_sheet(plan: SheetVitrinaV1Envelope, sheet_name: str) -> Any | None:
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return sheet
    return None


def _emit_plan_status_sheet_log(
    plan: SheetVitrinaV1Envelope,
    emit: OperatorLogEmitter,
    *,
    cycle: str,
) -> None:
    status_sheet = _find_sheet(plan, "STATUS")
    if status_sheet is None:
        emit(_format_log_event("status_sheet_missing", cycle=cycle, sheet="STATUS"))
        return
    emit(
        _format_log_event(
            "status_sheet_selected",
            cycle=cycle,
            sheet="STATUS",
            rows=status_sheet.row_count,
            columns=status_sheet.column_count,
        )
    )
    for row in status_sheet.rows:
        if len(row) < 11:
            continue
        source_key = str(row[0] or "")
        kind = str(row[1] or "")
        note = str(row[10] or "")
        if source_key == "registry_upload_current_state":
            emit(
                _format_log_event(
                    "status_registry_state",
                    cycle=cycle,
                    source=source_key,
                    kind=kind,
                    snapshot_date=row[3],
                    requested_count=row[7],
                    covered_count=row[8],
                    note=note,
                )
            )
            continue
        if source_key == DELIVERY_CONTRACT_VERSION:
            emit(
                _format_log_event(
                    "status_delivery_contract",
                    cycle=cycle,
                    source=source_key,
                    kind=kind,
                    snapshot_date=row[3],
                    requested_count=row[7],
                    covered_count=row[8],
                    note=note,
                )
            )
            continue
        source_name, temporal_slot = _split_temporal_source_key(source_key)
        spec = SOURCE_DIAGNOSTIC_SPECS.get(source_name, {})
        emit(
            _format_log_event(
                "snapshot_source_status",
                cycle=cycle,
                source=source_name,
                temporal_slot=temporal_slot,
                module=spec.get("module"),
                block=spec.get("block"),
                adapter=spec.get("adapter"),
                endpoint=spec.get("endpoint"),
                kind=kind,
                freshness=row[2],
                snapshot_date=row[3],
                date=row[4],
                date_from=row[5],
                date_to=row[6],
                requested_count=row[7],
                covered_count=row[8],
                missing_nm_ids=row[9],
                note=note,
            )
        )


def _emit_plan_metric_sheet_log(
    plan: SheetVitrinaV1Envelope,
    emit: OperatorLogEmitter,
    *,
    cycle: str,
) -> None:
    data_sheet = _find_sheet(plan, "DATA_VITRINA")
    if data_sheet is None:
        emit(_format_log_event("metric_sheet_missing", cycle=cycle, sheet="DATA_VITRINA"))
        return
    summaries: dict[str, dict[str, Any]] = {}
    slot_count = max(len(plan.date_columns), 1)
    for row in data_sheet.rows:
        if len(row) < 2:
            continue
        key = str(row[1] or "")
        if "|" not in key:
            continue
        scope_token, metric_key = key.split("|", 1)
        summary = summaries.setdefault(
            metric_key,
            {
                "label_ru": str(row[0] or ""),
                "row_scopes": set(),
                "rows": 0,
                "non_zero": 0,
                "zero": 0,
                "blank": 0,
                "text": 0,
            },
        )
        summary["row_scopes"].add(scope_token.split(":", 1)[0])
        summary["rows"] += 1
        for cell in row[2 : 2 + slot_count]:
            if cell in ("", None):
                summary["blank"] += 1
            elif isinstance(cell, (int, float)):
                if float(cell) == 0.0:
                    summary["zero"] += 1
                else:
                    summary["non_zero"] += 1
            else:
                summary["text"] += 1

    for metric_key in sorted(summaries):
        summary = summaries[metric_key]
        blocked = (
            metric_key in BLOCKED_SOURCE_METRIC_KEYS
            and summary["non_zero"] == 0
            and summary["zero"] == 0
            and summary["blank"] > 0
        )
        emit(
            _format_log_event(
                "metric_batch_result",
                cycle=cycle,
                metric_key=metric_key,
                label_ru=summary["label_ru"],
                row_scopes=",".join(sorted(summary["row_scopes"])),
                rows=summary["rows"],
                slot_cells=summary["rows"] * slot_count,
                non_zero=summary["non_zero"],
                zero=summary["zero"],
                blank=summary["blank"],
                text=summary["text"],
                blocked=blocked,
                blocked_source="promo_by_price" if blocked else "",
            )
        )


def _emit_bridge_result_log(
    bridge_result: dict[str, Any],
    emit: OperatorLogEmitter,
    *,
    cycle: str,
) -> None:
    emit(
        _format_log_event(
            "bridge_finish",
            cycle=cycle,
            bridge=bridge_result.get("bridge"),
            script_id=bridge_result.get("script_id"),
            spreadsheet_id=bridge_result.get("spreadsheet_id"),
        )
    )
    write_result = bridge_result.get("write_result")
    if isinstance(write_result, Mapping):
        for item in write_result.get("sheets", []) or []:
            if not isinstance(item, Mapping):
                continue
            emit(
                _format_log_event(
                    "bridge_write_sheet",
                    cycle=cycle,
                    sheet=item.get("sheet_name"),
                    row_count=item.get("row_count"),
                    write_rect=item.get("write_rect"),
                )
            )
    sheet_state = bridge_result.get("sheet_state")
    if isinstance(sheet_state, Mapping):
        for item in sheet_state.get("sheets", []) or []:
            if not isinstance(item, Mapping):
                continue
            emit(
                _format_log_event(
                    "bridge_sheet_state",
                    cycle=cycle,
                    sheet=item.get("sheet_name"),
                    present=item.get("present"),
                    last_row=item.get("last_row"),
                    last_column=item.get("last_column"),
                )
            )


def _split_temporal_source_key(source_key: str) -> tuple[str, str]:
    if source_key.endswith("]") and "[" in source_key:
        name, slot = source_key[:-1].split("[", 1)
        return name, slot
    return source_key, ""


def _format_log_event(event: str, **fields: Any) -> str:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, bool):
            normalized = str(value).lower()
        else:
            normalized = round(value, 6) if isinstance(value, float) else value
        text = str(normalized)
        if any(char.isspace() or char in {'"', ";", "="} for char in text):
            text = json.dumps(text, ensure_ascii=False)
        parts.append(f"{key}={text}")
    return " ".join(parts)


def _noop_log(_: str) -> None:
    return
