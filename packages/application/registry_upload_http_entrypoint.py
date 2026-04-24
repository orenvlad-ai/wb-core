"""Application-слой HTTP entrypoint для registry upload и sheet_vitrina_v1 operator flow."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import shlex
import threading
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from packages.application.factory_order_supply import FactoryOrderSupplyBlock
from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_daily_report import SheetVitrinaV1DailyReportBlock
from packages.application.sheet_vitrina_v1_load_bridge import load_sheet_vitrina_ready_snapshot_via_clasp
from packages.application.sheet_vitrina_v1_stock_report import SheetVitrinaV1StockReportBlock
from packages.application.sheet_vitrina_v1_stock_report import list_active_sku_options
from packages.application.sheet_vitrina_v1_temporal_policy import reduce_source_temporal_semantics
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.web_vitrina_gravity_table_adapter import (
    build_web_vitrina_gravity_table_adapter,
)
from packages.application.web_vitrina_page_composition import (
    build_web_vitrina_page_composition,
    build_web_vitrina_page_error_composition,
)
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
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
WEB_VITRINA_ACTIVITY_TONE_RANK = {
    "error": 0,
    "warning": 1,
    "success": 2,
    "neutral": 3,
}
WEB_VITRINA_ACTIVITY_ITEM_COPY = {
    "seller_funnel_snapshot": {
        "label_ru": "Воронка продавца",
        "description_ru": "Показы карточки, открытия и базовая конверсия за дату.",
    },
    "sales_funnel_history": {
        "label_ru": "История продаж",
        "description_ru": "Заказы, выручка и конверсия WB за период.",
    },
    "web_source_snapshot": {
        "label_ru": "Поисковая аналитика",
        "description_ru": "Просмотры, CTR, заказы и средняя позиция в поиске.",
    },
    "prices_snapshot": {
        "label_ru": "Цены и скидки",
        "description_ru": "Текущие цены продавца и скидки по SKU.",
    },
    "sf_period": {
        "label_ru": "Периодная аналитика WB",
        "description_ru": "Локализация, рейтинг и другие периодные показатели WB.",
    },
    "spp": {
        "label_ru": "СПП",
        "description_ru": "Скидка постоянного покупателя на выбранную дату.",
    },
    "ads_bids": {
        "label_ru": "Ставки рекламы",
        "description_ru": "Ставки в поиске и рекомендациях по SKU.",
    },
    "stocks": {
        "label_ru": "Остатки по складам",
        "description_ru": "История остатков и суммарный stock по складам.",
    },
    "ads_compact": {
        "label_ru": "Рекламная статистика",
        "description_ru": "Просмотры, клики, заказы и расход по рекламе.",
    },
    "fin_report_daily": {
        "label_ru": "Финансовый отчёт",
        "description_ru": "Выкупы, доставка, комиссии и хранение за дату.",
    },
    "cost_price": {
        "label_ru": "Себестоимость",
        "description_ru": "Себестоимость из текущего загруженного bundle.",
    },
    "promo_by_price": {
        "label_ru": "Промо и акции",
        "description_ru": "Промо-показатели из browser-collected promo source.",
    },
}


class SellerPortalRecoveryController:
    """Thin wrapper around the repo-owned seller relogin tool."""

    def __init__(
        self,
        *,
        config_factory: Callable[[], Any] | None = None,
        start_runner: Callable[[Any, bool], dict[str, Any]] | None = None,
        status_reader: Callable[..., dict[str, Any]] | None = None,
        stop_runner: Callable[[Any], dict[str, Any]] | None = None,
        launcher_builder: Callable[[Any, str, str], tuple[bytes, str]] | None = None,
    ) -> None:
        self._config_factory = config_factory
        self._start_runner = start_runner
        self._status_reader = status_reader
        self._stop_runner = stop_runner
        self._launcher_builder = launcher_builder

    def _tool(self) -> Any:
        return importlib.import_module("apps.seller_portal_relogin_session")

    def _config(self) -> Any:
        if self._config_factory is not None:
            return self._config_factory()
        tool = self._tool()
        return tool.load_relogin_session_config_from_env()

    def read_status(
        self,
        *,
        launcher_download_path: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._status_reader(config, False, requested_run_id=run_id)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=False, requested_run_id=run_id)
        )
        running = bool(raw.get("running"))
        if not running:
            raw = (
                self._status_reader(config, True, requested_run_id=run_id)
                if self._status_reader is not None
                else self._tool().read_session_status(config, with_probe=True, requested_run_id=run_id)
            )
        return _build_seller_portal_recovery_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def start(
        self,
        *,
        replace: bool,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._start_runner(config, replace)
            if self._start_runner is not None
            else self._tool().start_relogin_session(config, replace=replace)
        )
        if not bool(raw.get("running")):
            raw = (
                self._status_reader(config, True)
                if self._status_reader is not None
                else self._tool().read_session_status(config, with_probe=True)
            )
        return _build_seller_portal_recovery_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def stop(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        config = self._config()
        raw = dict(
            (
            self._stop_runner(config)
            if self._stop_runner is not None
            else self._tool().stop_relogin_session(config)
            )
            or {}
        )
        probe_payload = (
            self._status_reader(config, True)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=True)
        )
        if isinstance(probe_payload, Mapping):
            raw["current_storage_probe"] = (
                dict(probe_payload.get("current_storage_probe") or {})
                if isinstance(probe_payload.get("current_storage_probe"), Mapping)
                else probe_payload.get("current_storage_probe")
            )
            if isinstance(probe_payload.get("supplier_context"), Mapping):
                raw["supplier_context"] = dict(probe_payload.get("supplier_context") or {})
        return _build_seller_portal_recovery_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def check_session(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        config = self._config()
        raw = (
            self._status_reader(config, True)
            if self._status_reader is not None
            else self._tool().read_session_status(config, with_probe=True)
        )
        return _build_seller_portal_session_check_payload(
            raw,
            config=config,
            launcher_download_path=launcher_download_path,
        )

    def build_launcher_archive(
        self,
        *,
        public_status_url: str,
        public_operator_url: str,
    ) -> tuple[bytes, str]:
        config = self._config()
        if self._launcher_builder is not None:
            return self._launcher_builder(config, public_status_url, public_operator_url)
        return self._tool().build_macos_launcher_archive(
            config,
            public_status_url=public_status_url,
            public_operator_url=public_operator_url,
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
        seller_portal_recovery_controller: SellerPortalRecoveryController | None = None,
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
        self.web_vitrina_block = SheetVitrinaV1WebVitrinaBlock(
            runtime=self.runtime,
            now_factory=self.now_factory,
        )
        self.sheet_load_runner = sheet_load_runner or load_sheet_vitrina_ready_snapshot_via_clasp
        self.operator_jobs = SheetVitrinaV1OperatorJobStore(timestamp_factory=self.activated_at_factory)
        self.seller_portal_recovery = seller_portal_recovery_controller or SellerPortalRecoveryController()
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
        payload["technical_status"] = payload["status"]
        payload["status"] = payload["semantic_status"]
        payload["status_label"] = payload["semantic_label"]
        payload["status_reason"] = payload["semantic_reason"]
        payload["server_context"] = self.build_sheet_server_context()
        payload["manual_context"] = self.build_sheet_manual_context()
        payload["load_context"] = self.build_sheet_load_context()
        return payload

    def handle_sheet_daily_report_request(self) -> dict[str, Any]:
        return self.daily_report_block.build()

    def handle_sheet_stock_report_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self.stock_report_block.build(as_of_date=as_of_date)

    def handle_sheet_web_vitrina_request(
        self,
        *,
        page_route: str,
        read_route: str,
        as_of_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        return asdict(
            self.web_vitrina_block.build(
                page_route=page_route,
                read_route=read_route,
                as_of_date=as_of_date,
                date_from=date_from,
                date_to=date_to,
            )
        )

    def handle_sheet_web_vitrina_page_composition_request(
        self,
        *,
        page_route: str,
        read_route: str,
        operator_route: str,
        job_path: str = "/v1/sheet-vitrina-v1/job",
        as_of_date: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        effective_as_of_date = as_of_date or default_business_as_of_date(self.now_factory())
        available_snapshot_dates = self.web_vitrina_block.list_readable_dates(descending=True)
        default_as_of_date = default_business_as_of_date(self.now_factory())
        try:
            contract = self.web_vitrina_block.build(
                page_route=page_route,
                read_route=read_route,
                as_of_date=as_of_date,
                date_from=date_from,
                date_to=date_to,
            )
            view_model = build_web_vitrina_view_model(contract)
            adapter = build_web_vitrina_gravity_table_adapter(view_model)
        except Exception as exc:
            return build_web_vitrina_page_error_composition(
                page_route=page_route,
                read_route=read_route,
                operator_route=operator_route,
                as_of_date=effective_as_of_date,
                error_message=str(exc),
                available_snapshot_dates=available_snapshot_dates,
                default_as_of_date=default_as_of_date,
                selected_as_of_date=as_of_date,
                selected_date_from=date_from,
                selected_date_to=date_to,
            )

        activity_surface = _empty_web_vitrina_activity_surface()
        try:
            activity_surface = self._build_web_vitrina_activity_surface(
                snapshot_as_of_date=str(contract.meta.as_of_date),
                snapshot_id=str(contract.meta.snapshot_id),
                refreshed_at=str(contract.meta.refreshed_at),
                read_model=str(contract.status_summary.read_model),
                job_path=job_path,
            )
        except Exception as exc:  # pragma: no cover - bounded fallback
            activity_surface = _empty_web_vitrina_activity_surface(
                log_message=f"activity surface unavailable: {exc}",
                upload_message=f"upload summary unavailable: {exc}",
                update_message=f"update summary unavailable: {exc}",
            )

        return build_web_vitrina_page_composition(
            contract=contract,
            view_model=view_model,
            adapter=adapter,
            page_route=page_route,
            read_route=read_route,
            operator_route=operator_route,
            available_snapshot_dates=available_snapshot_dates,
            selected_as_of_date=as_of_date,
            selected_date_from=date_from,
            selected_date_to=date_to,
            activity_surface=activity_surface,
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

    def handle_seller_portal_recovery_status_request(
        self,
        *,
        launcher_download_path: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.read_status(
            launcher_download_path=launcher_download_path,
            run_id=run_id,
        )

    def handle_seller_portal_recovery_start_request(
        self,
        *,
        launcher_download_path: str,
        replace: bool = True,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.start(
            replace=replace,
            launcher_download_path=launcher_download_path,
        )

    def handle_seller_portal_recovery_stop_request(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.stop(
            launcher_download_path=launcher_download_path,
        )

    def handle_seller_portal_recovery_launcher_request(
        self,
        *,
        public_status_url: str,
        public_operator_url: str,
    ) -> tuple[bytes, str]:
        return self.seller_portal_recovery.build_launcher_archive(
            public_status_url=public_status_url,
            public_operator_url=public_operator_url,
        )

    def handle_seller_portal_session_check_request(
        self,
        *,
        launcher_download_path: str,
    ) -> dict[str, Any]:
        return self.seller_portal_recovery.check_session(
            launcher_download_path=launcher_download_path,
        )

    def _build_web_vitrina_activity_surface(
        self,
        *,
        snapshot_as_of_date: str,
        snapshot_id: str,
        refreshed_at: str,
        read_model: str,
        job_path: str,
    ) -> dict[str, Any]:
        refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=snapshot_as_of_date)
        latest_job = self.operator_jobs.latest_relevant_job(
            operations=("refresh", "auto_update"),
            preferred_as_of_date=snapshot_as_of_date,
            strict_preferred_as_of_date=True,
        )
        upload_records = (
            _extract_upload_source_records_from_job(latest_job)
            if latest_job is not None
            else _extract_source_records_from_outcomes(refresh_status.source_outcomes)
        )
        update_records = _extract_source_records_from_outcomes(refresh_status.source_outcomes)
        shared_source_keys = _collect_activity_source_keys(upload_records, update_records)
        upload_source_keys = _ordered_activity_source_keys(shared_source_keys, upload_records)
        update_source_keys = _ordered_activity_source_keys(shared_source_keys, update_records)
        return {
            "log_block": _build_web_vitrina_log_block(
                latest_job=latest_job,
                job_path=job_path,
                persisted_refresh_status=refresh_status,
            ),
            "upload_summary": _build_web_vitrina_endpoint_summary_block(
                title="Загрузка данных",
                subtitle=(
                    "Что вернули источники в последнем завершённом refresh."
                    if latest_job is not None
                    else "Transient refresh-log недоступен; показываем сохранённый итог по текущему срезу."
                ),
                records=upload_records,
                ordered_source_keys=upload_source_keys,
                empty_message=(
                    "Последний завершённый refresh-run в памяти сервиса пока не найден. "
                    "Показываем только сохранённый итог по текущему срезу."
                ),
                block_updated_at=(
                    str(latest_job.get("finished_at") or latest_job.get("started_at") or "")
                    if latest_job
                    else refreshed_at
                ),
                block_detail=(
                    f"job {latest_job.get('job_id', '')} · {str(latest_job.get('operation', 'refresh'))}"
                    if latest_job
                    else f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}"
                ),
            ),
            "update_summary": _build_web_vitrina_endpoint_summary_block(
                title="Обновление данных",
                subtitle=(
                    "Сохранённый итог для текущего среза. Повторное открытие страницы перечитывает именно "
                    "это состояние и не запускает скрытую загрузку источников."
                ),
                records=update_records,
                ordered_source_keys=update_source_keys,
                empty_message="STATUS-строки для текущего среза пока не материализованы.",
                block_updated_at=refreshed_at,
                block_detail=f"snapshot {snapshot_id} · as_of_date {snapshot_as_of_date} · {read_model}",
            ),
        }

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
            load_payload: dict[str, Any] | None = None
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
                auto_result = _build_auto_update_result_payload(
                    refresh_payload=refresh_payload,
                    load_payload=load_payload,
                    technical_status="error",
                    finished_at=finished_at,
                    error=str(exc),
                )
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
                    result_payload=auto_result,
                )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="auto_update",
                        status="error",
                        semantic_status=auto_result.get("semantic_status"),
                        semantic_reason=auto_result.get("semantic_reason"),
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        error=str(exc),
                    )
                )
                raise

            finished_at = self.activated_at_factory()
            auto_result = _build_auto_update_result_payload(
                refresh_payload=refresh_payload,
                load_payload=load_payload,
                technical_status="success",
                finished_at=finished_at,
                error=None,
            )
            self.runtime.save_sheet_vitrina_auto_update_result(
                started_at=started_at,
                finished_at=finished_at,
                status="success",
                as_of_date=str(load_payload["as_of_date"]),
                snapshot_id=str(load_payload["snapshot_id"]),
                refreshed_at=str(load_payload["refreshed_at"]),
                error=None,
                result_payload=auto_result,
            )
            emit(
                _format_log_event(
                    "cycle_finish",
                    cycle="auto_update",
                    status="success",
                    semantic_status=auto_result.get("semantic_status"),
                    semantic_reason=auto_result.get("semantic_reason"),
                    route=SHEET_VITRINA_REFRESH_ROUTE,
                    snapshot_id=load_payload["snapshot_id"],
                )
            )
            payload = dict(load_payload)
            payload["technical_status"] = str(payload.get("technical_status") or payload.get("status") or "success")
            payload["status"] = str(auto_result.get("semantic_status") or "warning")
            payload["status_label"] = str(auto_result.get("semantic_label") or "")
            payload["status_reason"] = str(auto_result.get("semantic_reason") or "")
            payload["semantic_status"] = str(auto_result.get("semantic_status") or "warning")
            payload["semantic_label"] = str(auto_result.get("semantic_label") or "")
            payload["semantic_tone"] = str(auto_result.get("semantic_tone") or "warning")
            payload["semantic_reason"] = str(auto_result.get("semantic_reason") or "")
            payload["auto_result"] = auto_result
            payload["operation"] = "auto_update"
            payload["auto_update_started_at"] = started_at
            payload["auto_update_finished_at"] = finished_at
            payload["server_context"] = self.build_sheet_server_context()
            payload["manual_context"] = self.build_sheet_manual_context()
            payload["load_context"] = self.build_sheet_load_context()
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
            try:
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
                refresh_outcome = _build_refresh_result_payload(refresh_result)
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_refresh_result(
                        result_payload=refresh_outcome,
                        refreshed_at=refresh_result.refreshed_at,
                    )
                payload = asdict(refresh_result)
                payload["technical_status"] = payload["status"]
                payload["status_label"] = payload["semantic_label"]
                payload["status_reason"] = payload["semantic_reason"]
                payload["server_context"] = self.build_sheet_server_context()
                payload["manual_context"] = self.build_sheet_manual_context()
                payload["load_context"] = self.build_sheet_load_context()
                emit(
                    _format_log_event(
                        "refresh_runtime_save_finish",
                        cycle="refresh",
                        snapshot_id=refresh_result.snapshot_id,
                        refreshed_at=refresh_result.refreshed_at,
                        data_rows=refresh_result.sheet_row_counts.get("DATA_VITRINA"),
                        status_rows=refresh_result.sheet_row_counts.get("STATUS"),
                        semantic_status=refresh_result.semantic_status,
                        semantic_reason=refresh_result.semantic_reason,
                    )
                )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="refresh",
                        status="success",
                        semantic_status=refresh_result.semantic_status,
                        semantic_reason=refresh_result.semantic_reason,
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                        snapshot_id=refresh_result.snapshot_id,
                    )
                )
                return payload
            except Exception as exc:
                finished_at = self.activated_at_factory()
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_refresh_result(
                        result_payload=_build_refresh_error_payload(
                            requested_as_of_date=as_of_date,
                            finished_at=finished_at,
                            error=str(exc),
                        ),
                        refreshed_at=None,
                    )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="refresh",
                        status="error",
                        semantic_status="error",
                        semantic_reason=str(exc),
                        route=SHEET_VITRINA_REFRESH_ROUTE,
                    )
                )
                raise

    def _run_sheet_load(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
        execution_mode: str = EXECUTION_MODE_MANUAL_OPERATOR,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        with self._sheet_cycle_lock:
            previous_load_state = self.runtime.load_sheet_vitrina_load_state()
            plan: SheetVitrinaV1Envelope | None = None
            refresh_status = None
            row_counts: dict[str, int] = {}
            plan_fingerprint: str | None = None
            try:
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
                plan_fingerprint = _plan_fingerprint(plan)
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
                        semantic_status=refresh_status.semantic_status,
                        semantic_reason=refresh_status.semantic_reason,
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
                load_outcome = _build_load_result_payload(
                    plan=plan,
                    refresh_status=refresh_status,
                    bridge_result=bridge_result,
                    previous_load_state=previous_load_state,
                    finished_at=finished_at,
                )
                self.runtime.save_sheet_vitrina_load_state(
                    loaded_at=finished_at,
                    snapshot_id=plan.snapshot_id,
                    as_of_date=plan.as_of_date,
                    refreshed_at=refresh_status.refreshed_at,
                    plan_fingerprint=plan_fingerprint,
                    result_payload=load_outcome,
                )
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_load_result(
                        result_payload=load_outcome,
                        loaded_at=finished_at,
                    )
                _emit_bridge_result_log(bridge_result, emit, cycle="load")
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="load",
                        status="success",
                        semantic_status=load_outcome.get("semantic_status"),
                        semantic_reason=load_outcome.get("semantic_reason"),
                        route=SHEET_VITRINA_LOAD_ROUTE,
                        snapshot_id=plan.snapshot_id,
                        data_rows=row_counts.get("DATA_VITRINA"),
                        status_rows=row_counts.get("STATUS"),
                    )
                )
                payload = {
                    "status": "success",
                    "technical_status": "success",
                    "status_label": str(load_outcome.get("semantic_label") or ""),
                    "status_reason": str(load_outcome.get("semantic_reason") or ""),
                    "semantic_status": str(load_outcome.get("semantic_status") or "warning"),
                    "semantic_label": str(load_outcome.get("semantic_label") or ""),
                    "semantic_tone": str(load_outcome.get("semantic_tone") or "warning"),
                    "semantic_reason": str(load_outcome.get("semantic_reason") or ""),
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
                    "load_result": load_outcome,
                }
                payload["server_context"] = self.build_sheet_server_context()
                payload["manual_context"] = self.build_sheet_manual_context()
                payload["load_context"] = self.build_sheet_load_context()
                return payload
            except Exception as exc:
                finished_at = self.activated_at_factory()
                load_error = _build_load_error_payload(
                    requested_as_of_date=as_of_date,
                    plan=plan,
                    refresh_status=refresh_status,
                    finished_at=finished_at,
                    error=str(exc),
                )
                self.runtime.save_sheet_vitrina_load_state(
                    loaded_at=finished_at,
                    snapshot_id=plan.snapshot_id if plan is not None else None,
                    as_of_date=(plan.as_of_date if plan is not None else as_of_date),
                    refreshed_at=(refresh_status.refreshed_at if refresh_status is not None else None),
                    plan_fingerprint=plan_fingerprint,
                    result_payload=load_error,
                )
                if execution_mode == EXECUTION_MODE_MANUAL_OPERATOR:
                    self.runtime.save_sheet_vitrina_manual_load_result(
                        result_payload=load_error,
                        loaded_at=None,
                    )
                emit(
                    _format_log_event(
                        "cycle_finish",
                        cycle="load",
                        status="error",
                        semantic_status="error",
                        semantic_reason=str(exc),
                        route=SHEET_VITRINA_LOAD_ROUTE,
                        snapshot_id=plan.snapshot_id if plan is not None else None,
                    )
                )
                raise

    def build_sheet_server_context(self) -> dict[str, Any]:
        now = self.now_factory()
        business_now = to_business_datetime(now).replace(microsecond=0).isoformat()
        auto_update_state = self.runtime.load_sheet_vitrina_auto_update_state()
        auto_result = _format_operator_result_payload(auto_update_state.last_run_result)
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
            "last_auto_run_status_label": (
                str(auto_result.get("semantic_label") or "")
                if auto_result
                else _auto_update_status_label(auto_update_state.last_run_status)
            ),
            "last_auto_run_status_reason": (
                str(auto_result.get("semantic_reason") or "")
                if auto_result
                else (auto_update_state.last_run_error or "")
            ),
            "last_auto_run_technical_status_label": _auto_update_status_label(auto_update_state.last_run_status),
            "last_auto_run_time": _format_optional_business_timestamp(auto_update_state.last_run_started_at),
            "last_auto_run_finished_at": _format_optional_business_timestamp(auto_update_state.last_run_finished_at),
            "last_successful_auto_update_at": _format_optional_business_timestamp(
                auto_update_state.last_successful_auto_update_at
            ),
            "last_auto_run_error": auto_update_state.last_run_error or "",
            "last_auto_run_result": auto_result,
        }

    def build_sheet_manual_context(self) -> dict[str, Any]:
        manual_state = self.runtime.load_sheet_vitrina_manual_operator_state()
        return {
            "last_successful_manual_refresh_at": _format_optional_business_timestamp(
                manual_state.last_successful_manual_refresh_at
            ),
            "last_successful_manual_load_at": _format_optional_business_timestamp(
                manual_state.last_successful_manual_load_at
            ),
            "last_manual_refresh_result": _format_operator_result_payload(
                manual_state.last_manual_refresh_result
            ),
            "last_manual_load_result": _format_operator_result_payload(
                manual_state.last_manual_load_result
            ),
        }

    def build_sheet_load_context(self) -> dict[str, Any]:
        load_state = self.runtime.load_sheet_vitrina_load_state()
        return {
            "last_finished_at": _format_optional_business_timestamp(load_state.loaded_at),
            "last_snapshot_id": load_state.snapshot_id or "",
            "last_as_of_date": load_state.as_of_date or "",
            "last_refreshed_at": load_state.refreshed_at or "",
            "last_result": _format_operator_result_payload(load_state.result),
        }

    def build_sheet_operator_ui_context(self) -> dict[str, Any]:
        try:
            current_state = self.runtime.load_current_state()
        except (ValueError, sqlite3.Error):
            active_skus: list[dict[str, Any]] = []
        else:
            active_skus = list_active_sku_options(current_state.config_v2)
        return {
            "stock_report_active_skus": active_skus,
            "stock_report_active_sku_count": len(active_skus),
            "stock_report_active_sku_source": "current_registry_config_v2",
        }


def _build_seller_portal_recovery_payload(
    raw_payload: Mapping[str, Any] | None,
    *,
    config: Any,
    launcher_download_path: str,
) -> dict[str, Any]:
    raw = dict(raw_payload or {})
    current_probe = raw.get("current_storage_probe")
    current_probe_payload = dict(current_probe) if isinstance(current_probe, Mapping) else None
    supplier_context = _seller_portal_recovery_supplier_context(raw)
    expected_supplier_id = str(getattr(config, "canonical_supplier_id", "") or "").strip()
    expected_supplier_label = str(getattr(config, "canonical_supplier_label", "") or "").strip()
    canonical_configured = bool(expected_supplier_id)
    organization_confirmed = _seller_portal_recovery_context_matches_expected(
        supplier_context,
        expected_supplier_id=expected_supplier_id,
    )
    session_status = _seller_portal_session_check_status(
        current_probe=current_probe_payload,
        canonical_configured=canonical_configured,
        organization_confirmed=organization_confirmed,
    )
    run_status = _seller_portal_recovery_run_status(raw)
    summary, instruction = _seller_portal_recovery_copy(
        run_status,
        raw=raw,
        current_probe=current_probe_payload,
        canonical_configured=canonical_configured,
        organization_confirmed=organization_confirmed,
        session_status=session_status,
    )
    run_id = str(raw.get("run_id") or "").strip()
    current_run_id = str(raw.get("current_run_id") or "").strip() or run_id
    requested_run_id = str(raw.get("requested_run_id") or "").strip()
    requested_run_mismatch = bool(requested_run_id and current_run_id and requested_run_id != current_run_id)
    run_is_final = run_status in {"completed", "not_needed", "stopped", "timeout", "error"}
    return {
        "status": run_status,
        "status_label": _seller_portal_recovery_status_label(run_status),
        "status_tone": _seller_portal_recovery_status_tone(run_status),
        "run_status": run_status,
        "run_status_label": _seller_portal_recovery_status_label(run_status),
        "run_status_tone": _seller_portal_recovery_status_tone(run_status),
        "summary": summary,
        "instruction": instruction,
        "technical_line": _seller_portal_recovery_technical_line(
            expected_supplier_id=expected_supplier_id,
            expected_supplier_label=expected_supplier_label,
            supplier_context=supplier_context,
            launcher_ready=run_status == "awaiting_login",
        ),
        "raw_status": str(raw.get("status") or "").strip(),
        "running": bool(raw.get("running")),
        "can_start": (not bool(raw.get("running"))) and canonical_configured,
        "can_stop": bool(raw.get("running")) and run_status in {
            "starting",
            "awaiting_login",
            "saving_session",
            "validating_session",
            "checking_canonical_supplier",
            "triggering_refresh",
        },
        "launcher_enabled": bool(run_id) and run_status == "awaiting_login" and not requested_run_mismatch,
        "launcher_download_path": launcher_download_path,
        "updated_at": _format_optional_business_timestamp(str(raw.get("updated_at") or "") or None),
        "started_at": _format_optional_business_timestamp(str(raw.get("started_at") or "") or None),
        "deadline_at": _format_optional_business_timestamp(str(raw.get("deadline_at") or "") or None),
        "finished_at": _format_optional_business_timestamp(str(raw.get("finished_at") or "") or None),
        "run_id": run_id,
        "current_run_id": current_run_id,
        "requested_run_id": requested_run_id,
        "requested_run_mismatch": requested_run_mismatch,
        "run_is_final": run_is_final,
        "run_final_status": run_status if run_is_final else "",
        "run_final_label": _seller_portal_recovery_final_label(run_status) if run_is_final else "",
        "organization_confirmed": organization_confirmed if canonical_configured else None,
        "organization_switch_applied": bool(raw.get("organization_switch_applied")),
        "expected_supplier_id": expected_supplier_id,
        "expected_supplier_label": expected_supplier_label,
        "current_supplier_id": str(
            supplier_context.get("current_supplier_id")
            or supplier_context.get("analytics_supplier_id")
            or ""
        ),
        "current_supplier_external_id": str(supplier_context.get("current_supplier_external_id") or ""),
        "current_storage_probe": current_probe_payload,
        "session_status": session_status,
        "session_status_label": _seller_portal_session_check_status_label(session_status),
        "session_status_tone": _seller_portal_session_check_status_tone(session_status),
        "message": str(raw.get("message") or "").strip(),
        "run_failure_code": _seller_portal_recovery_failure_code(raw),
    }


def _build_seller_portal_session_check_payload(
    raw_payload: Mapping[str, Any] | None,
    *,
    config: Any,
    launcher_download_path: str,
) -> dict[str, Any]:
    raw = dict(raw_payload or {})
    current_probe = raw.get("current_storage_probe")
    current_probe_payload = dict(current_probe) if isinstance(current_probe, Mapping) else None
    supplier_context = _seller_portal_recovery_supplier_context(raw)
    expected_supplier_id = str(getattr(config, "canonical_supplier_id", "") or "").strip()
    expected_supplier_label = str(getattr(config, "canonical_supplier_label", "") or "").strip()
    canonical_configured = bool(expected_supplier_id)
    organization_confirmed = _seller_portal_recovery_context_matches_expected(
        supplier_context,
        expected_supplier_id=expected_supplier_id,
    )
    status = _seller_portal_session_check_status(
        current_probe=current_probe_payload,
        canonical_configured=canonical_configured,
        organization_confirmed=organization_confirmed,
    )
    summary, instruction = _seller_portal_session_check_copy(
        status,
        canonical_configured=canonical_configured,
    )
    return {
        "status": status,
        "status_label": _seller_portal_session_check_status_label(status),
        "status_tone": _seller_portal_session_check_status_tone(status),
        "summary": summary,
        "instruction": instruction,
        "technical_line": _seller_portal_recovery_technical_line(
            expected_supplier_id=expected_supplier_id,
            expected_supplier_label=expected_supplier_label,
            supplier_context=supplier_context,
            launcher_ready=False,
        ),
        "raw_status": str(raw.get("status") or "").strip(),
        "running": False,
        "can_start": canonical_configured,
        "can_stop": False,
        "launcher_enabled": False,
        "launcher_download_path": launcher_download_path,
        "updated_at": _format_optional_business_timestamp(str(raw.get("updated_at") or "") or None),
        "started_at": "",
        "deadline_at": "",
        "finished_at": "",
        "organization_confirmed": (
            organization_confirmed
            if canonical_configured and current_probe_payload is not None and bool(current_probe_payload.get("ok"))
            else None
        ),
        "organization_switch_applied": False,
        "expected_supplier_id": expected_supplier_id,
        "expected_supplier_label": expected_supplier_label,
        "current_supplier_id": str(
            supplier_context.get("current_supplier_id")
            or supplier_context.get("analytics_supplier_id")
            or ""
        ),
        "current_supplier_external_id": str(supplier_context.get("current_supplier_external_id") or ""),
        "current_storage_probe": current_probe_payload,
        "message": str(raw.get("message") or "").strip(),
    }


def _seller_portal_recovery_supplier_context(raw: Mapping[str, Any]) -> dict[str, Any]:
    for value in (
        raw.get("current_storage_probe"),
        raw.get("last_probe"),
        raw.get("supplier_context"),
    ):
        if isinstance(value, Mapping) and isinstance(value.get("supplier_context"), Mapping):
            return dict(value.get("supplier_context") or {})
        if isinstance(value, Mapping) and any(
            key in value
            for key in ("current_supplier_id", "current_supplier_external_id", "analytics_supplier_id")
        ):
            return dict(value)
    return {}


def _seller_portal_recovery_context_matches_expected(
    supplier_context: Mapping[str, Any],
    *,
    expected_supplier_id: str,
) -> bool:
    expected = str(expected_supplier_id or "").strip()
    if not expected:
        return False
    unique_ids = {
        str(value or "").strip()
        for value in (
            supplier_context.get("current_supplier_id"),
            supplier_context.get("current_supplier_external_id"),
            supplier_context.get("analytics_supplier_id"),
        )
        if str(value or "").strip()
    }
    return bool(unique_ids) and unique_ids == {expected}


def _seller_portal_recovery_run_status(raw: Mapping[str, Any]) -> str:
    raw_status = str(raw.get("status") or "").strip()
    normalized = {
        "starting_visual_session": "starting",
        "auth_confirmed": "triggering_refresh",
        "success": "completed",
        "refresh_failed": "error",
        "wrong_organization": "error",
    }.get(raw_status, raw_status)
    if normalized in {
        "starting",
        "awaiting_login",
        "saving_session",
        "validating_session",
        "checking_canonical_supplier",
        "triggering_refresh",
        "completed",
        "not_needed",
        "stopped",
        "timeout",
        "error",
    }:
        return normalized
    return "idle"


def _seller_portal_recovery_failure_code(raw: Mapping[str, Any]) -> str:
    failure_code = str(raw.get("run_failure_code") or "").strip()
    if failure_code:
        return failure_code
    raw_status = str(raw.get("status") or "").strip()
    if raw_status in {"refresh_failed", "wrong_organization"}:
        return raw_status
    return ""


def _seller_portal_session_check_status(
    *,
    current_probe: Mapping[str, Any] | None,
    canonical_configured: bool,
    organization_confirmed: bool,
) -> str:
    if not canonical_configured:
        return "session_probe_error"
    if not isinstance(current_probe, Mapping):
        return "session_probe_error"
    if not bool(current_probe.get("ok")):
        normalized = str(current_probe.get("status") or "").strip()
        if normalized == "seller_portal_session_missing":
            return "session_missing"
        if normalized == "seller_portal_session_invalid":
            return "session_invalid"
        return "session_probe_error"
    if not organization_confirmed:
        return "session_valid_wrong_org"
    return "session_valid_canonical"


def _seller_portal_recovery_status_label(status: str) -> str:
    labels = {
        "idle": "Не запущено",
        "starting": "Запускаем",
        "awaiting_login": "Нужно войти",
        "saving_session": "Сохраняем сессию",
        "validating_session": "Проверяем сессию",
        "checking_canonical_supplier": "Проверяем кабинет",
        "triggering_refresh": "Обновляем данные",
        "completed": "Завершено",
        "not_needed": "Не потребовалось",
        "timeout": "Таймаут",
        "stopped": "Остановлено",
        "error": "Ошибка",
    }
    return labels.get(str(status or "").strip(), "Внимание")


def _seller_portal_session_check_status_label(status: str) -> str:
    labels = {
        "session_valid_canonical": "Сессия активна",
        "session_valid_wrong_org": "Не тот кабинет",
        "session_invalid": "Нужен вход",
        "session_missing": "Сессии нет",
        "session_probe_error": "Ошибка проверки",
    }
    return labels.get(str(status or "").strip(), "Проверка")


def _seller_portal_recovery_status_tone(status: str) -> str:
    if status in {"completed", "not_needed"}:
        return "success"
    if status in {"idle", "stopped"}:
        return "idle"
    if status in {"starting", "saving_session", "validating_session", "checking_canonical_supplier", "triggering_refresh"}:
        return "loading"
    if status in {"awaiting_login", "timeout"}:
        return "warning"
    return "error"


def _seller_portal_session_check_status_tone(status: str) -> str:
    if status == "session_valid_canonical":
        return "success"
    if status == "session_valid_wrong_org":
        return "warning"
    if status in {"session_invalid", "session_missing", "session_probe_error"}:
        return "error"
    return "idle"


def _seller_portal_recovery_copy(
    status: str,
    *,
    raw: Mapping[str, Any],
    current_probe: Mapping[str, Any] | None,
    canonical_configured: bool,
    organization_confirmed: bool,
    session_status: str,
) -> tuple[str, str]:
    failure_code = _seller_portal_recovery_failure_code(raw)
    if status == "idle" and not canonical_configured:
        return (
            "На хосте не настроен нужный кабинет для seller portal.",
            "Добавьте canonical supplier в runtime env и перезапустите сервис.",
        )
    if status == "idle":
        if session_status == "session_valid_canonical":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия уже активна, нужный кабинет подтверждён.",
                "Если операторский вход снова понадобится, нажмите «Восстановить сессию».",
            )
        if session_status == "session_valid_wrong_org":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сессия жива, но подтверждён не тот кабинет.",
                "Нажмите «Восстановить сессию», чтобы открыть временное окно входа и довести кабинет до canonical supplier.",
            )
        if session_status == "session_invalid":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия больше не действует.",
                "Нажмите «Восстановить сессию», затем скачайте launcher и выполните вход.",
            )
        if session_status == "session_missing":
            return (
                "Новый запуск восстановления сейчас не выполняется. Сохранённая seller-сессия отсутствует.",
                "Нажмите «Восстановить сессию», затем скачайте launcher и выполните вход.",
            )
        return (
            "Новый запуск восстановления сейчас не выполняется.",
            "Сначала проверьте seller-сессию или запустите восстановление повторно.",
        )
    if status == "starting":
        return (
            "Запускаем текущее временное окно входа на host.",
            "Когда статус сменится на «Нужно войти», скачайте launcher и откройте seller portal для этого запуска.",
        )
    if status == "awaiting_login":
        return (
            "Временное окно входа готово. Откройте launcher и войдите в seller portal.",
            "После входа система сама сохранит storage_state.json, проверит seller-сессию, подтвердит нужный кабинет и завершит текущий запуск.",
        )
    if status == "saving_session":
        return (
            "Логин подтверждён. Сохраняем обновлённую seller-сессию для текущего запуска.",
            "Launcher можно не закрывать до финального статуса.",
        )
    if status == "validating_session":
        return (
            "Сохраняемая seller-сессия уже записана. Проверяем обновлённый storage_state.json.",
            "Дождитесь финального статуса текущего запуска.",
        )
    if status == "checking_canonical_supplier":
        return (
            "Seller-сессия валидна. Проверяем, что после входа подтверждён нужный кабинет.",
            "Если кабинет окажется не тем, запуск завершится явной ошибкой.",
        )
    if status == "triggering_refresh":
        return (
            "Seller-сессия сохранена и кабинет подтверждён. Запускаем post-login refresh.",
            "Launcher можно не закрывать до финального статуса.",
        )
    if status == "completed":
        return (
            "Восстановление завершено: seller-сессия сохранена, нужный кабинет подтверждён, refresh завершён.",
            "Текущий запуск завершён. Launcher печатает финальную строку и закрывается сам.",
        )
    if status == "not_needed":
        return (
            "Повторный вход не потребовался: на момент старта seller-сессия уже была активна и нужный кабинет был подтверждён.",
            "Текущий запуск завершён сразу, без noVNC и launcher.",
        )
    if status == "stopped":
        if isinstance(current_probe, Mapping) and bool(current_probe.get("ok")) and organization_confirmed:
            return (
                "Восстановление остановлено: временное окно входа закрыто. Сохранённая seller-сессия и бот не изменены.",
                "Кнопка «Остановить восстановление» закрывает только временное окно входа: storage_state.json сохраняется, бот не разлогинивается.",
            )
        return (
            "Восстановление остановлено: временное окно входа закрыто до завершения сценария.",
            "Если вход всё ещё нужен, снова нажмите «Восстановить сессию».",
        )
    if status == "timeout":
        return (
            "Восстановление завершено по таймауту: вход не был подтверждён до истечения временного окна.",
            "Запустите восстановление снова и войдите в seller portal.",
        )
    if failure_code == "wrong_organization":
        return (
            "Восстановление завершено с ошибкой: вход выполнен, но подтверждён не тот кабинет.",
            "Запустите восстановление снова: система повторно проверит supplier и переключит кабинет перед сохранением state.",
        )
    if failure_code == "refresh_failed":
        return (
            "Восстановление завершено с ошибкой: seller-сессия сохранена, но post-login refresh не завершился.",
            "Повторите запуск. Если ошибка останется, проверьте host-side логи recovery и refresh.",
        )
    if failure_code == "canonical_supplier_not_configured":
        return (
            "Восстановление не запущено: на хосте не настроен canonical supplier.",
            "Добавьте canonical supplier в runtime env и перезапустите сервис.",
        )
    if failure_code == "run_replaced":
        return (
            "Текущий launcher больше не смотрит на свой запуск: этот recovery run уже не является текущим.",
            "Откройте operator page заново и при необходимости скачайте launcher для нового запуска.",
        )
    if failure_code == "unexpected_exit":
        return (
            "Восстановление завершено с ошибкой: runtime завершился раньше финального статуса.",
            "Запустите восстановление снова. Если ошибка повторится, проверьте host-side лог relogin tool.",
        )
    return (
        "Восстановление завершено с ошибкой.",
        "Запустите восстановление снова. Если ошибка повторится, проверьте host-side лог relogin tool.",
    )


def _seller_portal_recovery_final_label(status: str) -> str:
    if status == "completed":
        return "Восстановление завершено"
    if status == "not_needed":
        return "Повторный вход не потребовался"
    if status == "stopped":
        return "Восстановление остановлено"
    if status == "timeout":
        return "Восстановление завершено по таймауту"
    if status == "error":
        return "Восстановление завершено с ошибкой"
    return ""


def _seller_portal_session_check_copy(
    status: str,
    *,
    canonical_configured: bool,
) -> tuple[str, str]:
    if not canonical_configured or status == "session_probe_error":
        return (
            "Не удалось честно проверить seller-сессию.",
            "Проверьте canonical supplier в runtime env и повторите проверку; если ошибка останется, смотрите лог session probe.",
        )
    if status == "session_valid_canonical":
        return (
            "Сохранённая seller-сессия активна, нужный кабинет подтверждён.",
            "Восстановление не требуется.",
        )
    if status == "session_valid_wrong_org":
        return (
            "Сессия активна, но открыт не тот кабинет.",
            "Нажмите «Восстановить сессию»: система откроет временное окно входа и переключит кабинет на нужный supplier.",
        )
    if status == "session_invalid":
        return (
            "Сохранённая seller-сессия больше не действует.",
            "Нажмите «Восстановить сессию» и войдите через launcher для Mac.",
        )
    if status == "session_missing":
        return (
            "Сохранённая seller-сессия не найдена.",
            "Нажмите «Восстановить сессию» и выполните вход заново.",
        )
    return (
        "Проверка seller-сессии завершилась неопределённо.",
        "Повторите проверку или запустите восстановление, если операторский вход нужен прямо сейчас.",
    )


def _seller_portal_recovery_technical_line(
    *,
    expected_supplier_id: str,
    expected_supplier_label: str,
    supplier_context: Mapping[str, Any],
    launcher_ready: bool,
) -> str:
    parts = []
    if expected_supplier_id:
        expected_line = (
            f"Нужный кабинет: {expected_supplier_label} · supplier {expected_supplier_id}"
            if expected_supplier_label
            else f"Нужный supplier: {expected_supplier_id}"
        )
        parts.append(expected_line)
    current_supplier_id = (
        str(supplier_context.get("current_supplier_id") or "").strip()
        or str(supplier_context.get("analytics_supplier_id") or "").strip()
    )
    if current_supplier_id and current_supplier_id != expected_supplier_id:
        parts.append(f"Сейчас выбран supplier {current_supplier_id}")
    if launcher_ready:
        parts.append("Launcher открывает localhost-only noVNC через SSH tunnel; XQuartz не нужен")
    return " · ".join(part for part in parts if part)


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


def _format_operator_result_payload(result_payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result_payload, Mapping):
        return None
    payload = dict(result_payload)
    for field_name in ("finished_at", "loaded_at", "refreshed_at", "last_loaded_at"):
        if field_name in payload:
            payload[field_name] = _format_optional_business_timestamp(str(payload.get(field_name) or "") or None)
    return payload


def _build_refresh_result_payload(refresh_result: Any) -> dict[str, Any]:
    return {
        "technical_status": "success",
        "semantic_status": str(getattr(refresh_result, "semantic_status", "") or "warning"),
        "semantic_label": str(getattr(refresh_result, "semantic_label", "") or "Внимание"),
        "semantic_tone": str(getattr(refresh_result, "semantic_tone", "") or "warning"),
        "semantic_reason": str(getattr(refresh_result, "semantic_reason", "") or ""),
        "snapshot_id": str(getattr(refresh_result, "snapshot_id", "") or ""),
        "as_of_date": str(getattr(refresh_result, "as_of_date", "") or ""),
        "refreshed_at": str(getattr(refresh_result, "refreshed_at", "") or ""),
    }


def _build_refresh_error_payload(
    *,
    requested_as_of_date: str | None,
    finished_at: str,
    error: str,
) -> dict[str, Any]:
    return {
        "technical_status": "error",
        "semantic_status": "error",
        "semantic_label": "Ошибка",
        "semantic_tone": "error",
        "semantic_reason": str(error or "").strip() or "refresh завершился ошибкой",
        "snapshot_id": "",
        "as_of_date": requested_as_of_date or "",
        "finished_at": finished_at,
    }


def _build_load_result_payload(
    *,
    plan: SheetVitrinaV1Envelope,
    refresh_status: Any,
    bridge_result: Mapping[str, Any],
    previous_load_state: Any,
    finished_at: str,
) -> dict[str, Any]:
    previous_fingerprint = str(getattr(previous_load_state, "plan_fingerprint", "") or "").strip()
    current_fingerprint = _plan_fingerprint(plan)
    sheet_verified = _bridge_result_has_sheet_verification(bridge_result)
    if not sheet_verified:
        change_status = "not_verified"
        semantic_status = "warning"
        semantic_reason = "sheet bridge завершился, но не вернул верифицируемое состояние листов"
    elif not previous_fingerprint:
        change_status = "not_verified"
        semantic_status = "warning"
        semantic_reason = "sheet bridge завершился, но предыдущая отправка для сравнения отсутствует"
    elif previous_fingerprint == current_fingerprint:
        change_status = "unchanged"
        semantic_status = "warning"
        semantic_reason = "sheet bridge завершился, но snapshot совпадает с последней отправкой"
    else:
        change_status = "updated"
        semantic_status = "success"
        semantic_reason = "sheet bridge завершился; данные изменились относительно последней отправки"
    return {
        "technical_status": "success",
        "semantic_status": semantic_status,
        "semantic_label": _semantic_status_label(semantic_status),
        "semantic_tone": semantic_status,
        "semantic_reason": semantic_reason,
        "change_status": change_status,
        "change_label": _load_change_label(change_status),
        "change_verified": change_status == "updated",
        "snapshot_id": plan.snapshot_id,
        "as_of_date": plan.as_of_date,
        "refreshed_at": str(getattr(refresh_status, "refreshed_at", "") or ""),
        "finished_at": finished_at,
        "plan_fingerprint": current_fingerprint,
        "last_loaded_at": str(getattr(previous_load_state, "loaded_at", "") or ""),
    }


def _build_load_error_payload(
    *,
    requested_as_of_date: str | None,
    plan: SheetVitrinaV1Envelope | None,
    refresh_status: Any | None,
    finished_at: str,
    error: str,
) -> dict[str, Any]:
    return {
        "technical_status": "error",
        "semantic_status": "error",
        "semantic_label": "Ошибка",
        "semantic_tone": "error",
        "semantic_reason": str(error or "").strip() or "load завершился ошибкой",
        "change_status": "error",
        "change_label": _load_change_label("error"),
        "change_verified": False,
        "snapshot_id": plan.snapshot_id if plan is not None else "",
        "as_of_date": plan.as_of_date if plan is not None else (requested_as_of_date or ""),
        "refreshed_at": str(getattr(refresh_status, "refreshed_at", "") or "") if refresh_status is not None else "",
        "finished_at": finished_at,
        "plan_fingerprint": _plan_fingerprint(plan) if plan is not None else "",
        "last_loaded_at": "",
    }


def _build_auto_update_result_payload(
    *,
    refresh_payload: Mapping[str, Any] | None,
    load_payload: Mapping[str, Any] | None,
    technical_status: str,
    finished_at: str,
    error: str | None,
) -> dict[str, Any]:
    refresh_semantic = str((refresh_payload or {}).get("semantic_status") or "warning")
    load_semantic = str((load_payload or {}).get("semantic_status") or "warning")
    semantic_status = (
        "error"
        if technical_status == "error"
        else _worst_tone([refresh_semantic, load_semantic])
    )
    semantic_reason = (
        str(error or "").strip()
        if technical_status == "error"
        else " | ".join(
            part
            for part in [
                f"refresh: {str((refresh_payload or {}).get('semantic_reason') or '').strip()}",
                f"load: {str((load_payload or {}).get('semantic_reason') or '').strip()}",
            ]
            if not part.endswith(": ")
        )
    )
    return {
        "technical_status": technical_status,
        "semantic_status": semantic_status,
        "semantic_label": _semantic_status_label(semantic_status),
        "semantic_tone": semantic_status,
        "semantic_reason": semantic_reason or ("auto_update завершился" if technical_status == "success" else "auto_update завершился ошибкой"),
        "snapshot_id": str((load_payload or refresh_payload or {}).get("snapshot_id") or ""),
        "as_of_date": str((load_payload or refresh_payload or {}).get("as_of_date") or ""),
        "refreshed_at": str((load_payload or refresh_payload or {}).get("refreshed_at") or ""),
        "finished_at": finished_at,
    }


def _bridge_result_has_sheet_verification(bridge_result: Mapping[str, Any]) -> bool:
    write_result = bridge_result.get("write_result")
    sheet_state = bridge_result.get("sheet_state")
    if not isinstance(write_result, Mapping) or not isinstance(sheet_state, Mapping):
        return False
    written_sheets = write_result.get("sheets")
    state_sheets = sheet_state.get("sheets")
    return isinstance(written_sheets, list) and bool(written_sheets) and isinstance(state_sheets, list) and bool(state_sheets)


def _plan_fingerprint(plan: SheetVitrinaV1Envelope | None) -> str:
    if plan is None:
        return ""
    payload = json.dumps(asdict(plan), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _semantic_status_label(status: str) -> str:
    if status == "success":
        return "Успешно"
    if status == "error":
        return "Ошибка"
    return "Внимание"


def _load_change_label(change_status: str) -> str:
    if change_status == "updated":
        return "Данные изменились"
    if change_status == "unchanged":
        return "Без изменений"
    if change_status == "error":
        return "Ошибка"
    return "Не подтверждено"


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

    def latest_relevant_job(
        self,
        *,
        operations: tuple[str, ...],
        preferred_as_of_date: str | None = None,
        strict_preferred_as_of_date: bool = False,
    ) -> dict[str, Any] | None:
        normalized_operations = {str(value).strip() for value in operations if str(value).strip()}
        normalized_as_of_date = str(preferred_as_of_date or "").strip()
        with self._lock:
            jobs = list(self._jobs.values())
        candidates = [
            job
            for job in jobs
            if job.status in {"success", "error"}
            and (not normalized_operations or job.operation in normalized_operations)
        ]
        if not candidates:
            return None
        if normalized_as_of_date:
            preferred = [
                job
                for job in candidates
                if str(((job.result or {}).get("as_of_date") or "")).strip() == normalized_as_of_date
            ]
            if preferred:
                candidates = preferred
            elif strict_preferred_as_of_date:
                return None
        selected = max(
            enumerate(candidates),
            key=lambda item: (
                str(item[1].finished_at or ""),
                str(item[1].started_at or ""),
                item[0],
            ),
        )[1]
        return selected.snapshot()

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


def _empty_web_vitrina_activity_surface(
    *,
    log_message: str = "Последний релевантный run/log пока недоступен.",
    upload_message: str = "Последний upload-run по источникам пока недоступен.",
    update_message: str = "Сохранённый итог по текущему срезу пока недоступен.",
) -> dict[str, Any]:
    return {
        "log_block": {
            "title": "Лог",
            "subtitle": "Последний релевантный refresh-run",
            "status_label": "Нет данных",
            "tone": "neutral",
            "detail": "",
            "preview_lines": [],
            "line_count": 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": log_message,
        },
        "upload_summary": {
            "title": "Загрузка данных",
            "subtitle": "",
            "detail": "",
            "updated_at": "",
            "items": [],
            "empty_message": upload_message,
        },
        "update_summary": {
            "title": "Обновление данных",
            "subtitle": "",
            "detail": "",
            "updated_at": "",
            "items": [],
            "empty_message": update_message,
        },
    }


def _build_web_vitrina_log_block(
    *,
    latest_job: Mapping[str, Any] | None,
    job_path: str,
    persisted_refresh_status: Any | None = None,
) -> dict[str, Any]:
    if latest_job is None:
        semantic_label = (
            str(getattr(persisted_refresh_status, "semantic_label", "") or "Нет transient-лога")
            if persisted_refresh_status is not None
            else "Нет данных"
        )
        semantic_tone = (
            str(getattr(persisted_refresh_status, "semantic_tone", "") or "neutral")
            if persisted_refresh_status is not None
            else "neutral"
        )
        semantic_reason = (
            str(getattr(persisted_refresh_status, "semantic_reason", "") or "")
            if persisted_refresh_status is not None
            else ""
        )
        return {
            "title": "Лог",
            "subtitle": "Лог последнего refresh для текущего среза недоступен",
            "status_label": semantic_label,
            "tone": semantic_tone,
            "detail": semantic_reason,
            "preview_lines": [],
            "line_count": 0,
            "download_path": "",
            "log_filename": "",
            "empty_message": "Сохранённый итог есть, но transient refresh-log для этого среза недоступен.",
        }
    job_payload = _with_job_urls_from_job_snapshot(latest_job, job_path)
    semantic_status = str(((job_payload.get("result") or {}).get("semantic_status")) or "").strip()
    tone = semantic_status if semantic_status in {"success", "warning", "error"} else (
        "success" if str(job_payload.get("status", "")) == "success" else "error"
    )
    status_label = str(((job_payload.get("result") or {}).get("semantic_label")) or "").strip()
    detail_reason = str(((job_payload.get("result") or {}).get("semantic_reason")) or "").strip()
    preview_lines = [str(line) for line in (job_payload.get("log_lines") or []) if str(line).strip()]
    line_limit = 240
    truncated = len(preview_lines) > line_limit
    if truncated:
        preview_lines = preview_lines[-line_limit:]
    detail_parts = [
        f"job {job_payload.get('job_id', '')}",
        str(job_payload.get("operation", "")),
        str(job_payload.get("finished_at") or job_payload.get("started_at") or ""),
    ]
    if detail_reason:
        detail_parts.append(detail_reason)
    if truncated:
        detail_parts.append(f"показаны последние {line_limit} строк")
    return {
        "title": "Лог",
        "subtitle": "Последний релевантный refresh",
        "status_label": status_label or _semantic_status_label(tone),
        "tone": tone,
        "detail": " · ".join(part for part in detail_parts if part),
        "preview_lines": preview_lines,
        "line_count": int(job_payload.get("log_line_count") or len(preview_lines)),
        "download_path": str(job_payload.get("download_path") or ""),
        "log_filename": str(job_payload.get("log_filename") or ""),
        "empty_message": "Лог пока пуст.",
    }


def _with_job_urls_from_job_snapshot(job_payload: Mapping[str, Any], job_path: str) -> dict[str, Any]:
    normalized = dict(job_payload)
    job_id = str(normalized.get("job_id") or "").strip()
    operation = str(normalized.get("operation") or "refresh").strip() or "refresh"
    if not job_id:
        return normalized
    normalized["job_path"] = f"{job_path}?job_id={job_id}"
    normalized["download_path"] = f"{job_path}?job_id={job_id}&format=text&download=1"
    normalized["log_filename"] = f"sheet-vitrina-v1-{operation}-{job_id}.txt"
    return normalized


def _build_web_vitrina_endpoint_summary_block(
    *,
    title: str,
    subtitle: str,
    records: Mapping[str, Mapping[str, Any]],
    ordered_source_keys: list[str],
    empty_message: str,
    block_updated_at: str,
    block_detail: str,
) -> dict[str, Any]:
    if not ordered_source_keys:
        return {
            "title": title,
            "subtitle": subtitle,
            "detail": block_detail,
            "updated_at": block_updated_at,
            "items": [],
            "empty_message": empty_message,
        }
    items = []
    for source_order, source_key in enumerate(ordered_source_keys):
        items.append(
            _build_endpoint_summary_item(
                source_key=source_key,
                record=records.get(source_key),
                source_order=source_order,
            )
        )
    if not any(item.get("status_label") for item in items):
        items = []
    return {
        "title": title,
        "subtitle": subtitle,
        "detail": block_detail,
        "updated_at": block_updated_at,
        "items": items,
        "empty_message": empty_message,
    }


def _build_endpoint_summary_item(
    *,
    source_key: str,
    record: Mapping[str, Any] | None,
    source_order: int,
) -> dict[str, Any]:
    copy = _web_vitrina_activity_item_copy(source_key)
    tone = str((record or {}).get("tone") or "warning")
    status_label = str((record or {}).get("status_label") or _semantic_status_label(tone))
    severity_rank = _activity_tone_rank(tone)
    if record is None:
        return {
            "endpoint_id": source_key,
            "endpoint_label": copy["endpoint_label"],
            "source_key": source_key,
            "label_ru": copy["label_ru"],
            "description_ru": copy["description_ru"],
            "reason_ru": "обновление не подтверждено",
            "technical_key": copy["technical_key"],
            "technical_text": copy["technical_text"],
            "status_label": "Внимание",
            "tone": "warning",
            "detail": "обновление не подтверждено",
            "severity_rank": _activity_tone_rank("warning"),
            "source_order": source_order,
        }
    reason_ru = _activity_reason_ru(
        tone=tone,
        detail=str(record.get("detail") or ""),
        note=str(record.get("note") or ""),
    )
    detail = _activity_summary_detail(
        description_ru=copy["description_ru"],
        reason_ru=reason_ru,
        fallback_detail=str(record.get("detail") or "").strip(),
    )
    return {
        "endpoint_id": source_key,
        "endpoint_label": copy["endpoint_label"],
        "source_key": source_key,
        "label_ru": copy["label_ru"],
        "description_ru": copy["description_ru"],
        "reason_ru": reason_ru,
        "technical_key": copy["technical_key"],
        "technical_text": copy["technical_text"],
        "status_label": status_label,
        "tone": tone,
        "detail": detail,
        "severity_rank": severity_rank,
        "source_order": source_order,
    }


def _extract_upload_source_records_from_job(
    latest_job: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if latest_job is None:
        return {}
    result_payload = latest_job.get("result") or {}
    source_outcomes = result_payload.get("source_outcomes")
    if isinstance(source_outcomes, list) and source_outcomes:
        return _extract_source_records_from_outcomes(source_outcomes)
    records: dict[str, dict[str, Any]] = {}
    for line in latest_job.get("log_lines") or []:
        parsed = _parse_log_event_line(str(line))
        if parsed is None:
            continue
        event, fields = parsed
        if event != "source_step_finish":
            continue
        source_key = str(fields.get("source") or "").strip()
        if not source_key:
            continue
        _accumulate_source_record(
            records=records,
            source_key=source_key,
            temporal_slot=str(fields.get("temporal_slot") or ""),
            kind=str(fields.get("kind") or ""),
            note=str(fields.get("note") or ""),
            requested_count=_coerce_int(fields.get("requested_count")),
            covered_count=_coerce_int(fields.get("covered_count")),
        )
    return _finalize_source_records(records)


def _extract_source_records_from_outcomes(
    source_outcomes: list[Mapping[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for outcome in source_outcomes or []:
        source_key = str(outcome.get("source_key") or "").strip()
        if not source_key:
            continue
        records[source_key] = {
            "status": str(outcome.get("status") or "warning"),
            "tone": str(outcome.get("tone") or outcome.get("status") or "warning"),
            "status_label": str(outcome.get("label") or _semantic_status_label(str(outcome.get("status") or "warning"))),
            "detail": str(outcome.get("reason") or "").strip(),
            "note": "",
        }
    return records


def _collect_activity_source_keys(
    upload_records: Mapping[str, Mapping[str, Any]],
    update_records: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    seen = set(upload_records) | set(update_records)
    ordered = [source_key for source_key in SOURCE_DIAGNOSTIC_SPECS if source_key in seen]
    extras = sorted(source_key for source_key in seen if source_key not in SOURCE_DIAGNOSTIC_SPECS)
    return ordered + extras


def _ordered_activity_source_keys(
    source_keys: Iterable[str],
    records: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    ordered = list(source_keys)
    canonical_order = {source_key: index for index, source_key in enumerate(ordered)}
    return sorted(
        ordered,
        key=lambda source_key: (
            _activity_tone_rank(str((records.get(source_key) or {}).get("tone") or "warning")),
            canonical_order.get(source_key, len(canonical_order)),
            source_key,
        ),
    )


def _accumulate_source_record(
    *,
    records: dict[str, dict[str, Any]],
    source_key: str,
    temporal_slot: str,
    kind: str,
    note: str,
    requested_count: int = 0,
    covered_count: int = 0,
) -> None:
    bucket = records.setdefault(
        source_key,
        {
            "slot_records": [],
        },
    )
    normalized_slot = temporal_slot or "snapshot"
    status = _semantic_status_from_kind(
        kind=kind,
        note=note,
        requested_count=requested_count,
        covered_count=covered_count,
    )
    bucket["slot_records"].append(
        {
            "temporal_slot": normalized_slot,
            "status": status,
            "kind": str(kind or "").strip().lower(),
            "note": str(note or "").strip(),
            "requested_count": requested_count,
            "covered_count": covered_count,
            "reason": _slot_reason_from_log_record(
                kind=kind,
                note=note,
                requested_count=requested_count,
                covered_count=covered_count,
            ),
        }
    )


def _finalize_source_records(
    records: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for source_key, record in records.items():
        slot_records = list(record.get("slot_records") or [])
        if not slot_records:
            continue
        reduction = reduce_source_temporal_semantics(
            source_key=source_key,
            temporal_policy="",
            slot_outcomes=slot_records,
        )
        tone = str(reduction["status"])
        finalized[source_key] = {
            "status": tone,
            "tone": tone,
            "status_label": _semantic_status_label(tone),
            "detail": str(reduction["reason"]),
            "note": "",
        }
    return finalized


def _semantic_status_from_kind(
    *,
    kind: str,
    note: str,
    requested_count: int,
    covered_count: int,
) -> str:
    normalized_kind = str(kind).strip().lower()
    if normalized_kind in {"error", "closure_exhausted"}:
        return "error"
    if normalized_kind in {
        "missing",
        "incomplete",
        "not_available",
        "blocked",
        "closure_pending",
        "closure_retrying",
        "closure_rate_limited",
        "not_found",
    }:
        return "warning"
    if normalized_kind != "success":
        return "warning"
    if _note_requires_warning(note):
        return "warning"
    if requested_count > 0 and covered_count < requested_count:
        return "warning"
    return "success"


def _slot_reason_from_log_record(
    *,
    kind: str,
    note: str,
    requested_count: int,
    covered_count: int,
) -> str:
    normalized_kind = str(kind).strip().lower()
    human_note = _humanize_note(note)
    if normalized_kind == "success" and requested_count > 0 and covered_count < requested_count:
        return _coverage_reason(requested_count=requested_count, covered_count=covered_count)
    if normalized_kind == "incomplete":
        return _coverage_reason(requested_count=requested_count, covered_count=covered_count)
    if normalized_kind in {"closure_pending", "closure_retrying", "closure_rate_limited"} and human_note:
        return human_note
    if normalized_kind == "closure_exhausted":
        return human_note or "retry исчерпан"
    if human_note:
        return human_note
    if normalized_kind == "success":
        return "обновление подтверждено"
    if normalized_kind == "missing":
        return "данные не получены"
    if normalized_kind == "not_available":
        return "источник не обновлялся"
    if normalized_kind == "not_found":
        return "источник не вернул данные"
    return normalized_kind or "нужна проверка"


def _slot_sort_key(slot: str) -> tuple[int, str]:
    if slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return (0, slot)
    if slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return (1, slot)
    if slot == "snapshot":
        return (2, slot)
    return (3, slot)


def _slot_label(slot: str) -> str:
    if slot == TEMPORAL_SLOT_YESTERDAY_CLOSED:
        return "вчера"
    if slot == TEMPORAL_SLOT_TODAY_CURRENT:
        return "сегодня"
    if slot == "snapshot":
        return "срез"
    return slot


def _coverage_reason(*, requested_count: int, covered_count: int) -> str:
    if requested_count <= 0:
        return "покрытие не подтверждено"
    if covered_count <= 0:
        return f"нет покрытия по {requested_count} позициям"
    return f"покрыто {covered_count} из {requested_count}"


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError:
        return 0


def _humanize_note(note: str) -> str:
    normalized = str(note or "").strip()
    if not normalized:
        return ""
    replacements = (
        (
            "seller_portal_session_invalid",
            "сессия seller portal больше не действует; требуется повторный вход",
        ),
        (
            "seller_portal_session_missing",
            "сессия seller portal отсутствует; требуется повторный вход",
        ),
        (
            "seller_portal_wrong_supplier",
            "после входа выбран не тот кабинет; требуется recovery с переключением supplier",
        ),
        (
            "source is not available for today_current in the bounded live contour; today column stays blank instead of inventing fresh values",
            "текущий день для этого источника не требуется",
        ),
        (
            "source is current-only in the bounded live contour; yesterday_closed is left blank instead of backfilling current values into a closed-day column",
            "закрытый день materialize-ится через current-rollover",
        ),
        (
            "resolution_rule=accepted_closed_preserved_after_invalid_attempt",
            "использована последняя подтверждённая закрытая версия",
        ),
        (
            "resolution_rule=accepted_current_preserved_after_invalid_attempt",
            "использована последняя подтверждённая текущая версия",
        ),
        (
            "resolution_rule=accepted_closed_from_prior_current_snapshot",
            "использована подтверждённая версия предыдущего дня",
        ),
        (
            "resolution_rule=accepted_closed_from_prior_current_cache",
            "использована подтверждённая версия из runtime cache",
        ),
        (
            "resolution_rule=accepted_closed_runtime_snapshot",
            "использована последняя подтверждённая закрытая версия",
        ),
        (
            "resolution_rule=accepted_closed_from_interval_replay",
            "использована сохранённая закрытая версия из interval replay",
        ),
        (
            "resolution_rule=accepted_prior_current_runtime_cache",
            "использована подтверждённая версия из runtime cache",
        ),
        (
            "resolution_rule=exact_date_stocks_history_runtime_cache",
            "использована сохранённая версия на точную дату",
        ),
        (
            "resolution_rule=exact_date_promo_current_runtime_cache",
            "использована сохранённая версия на точную дату",
        ),
        (
            "resolution_rule=exact_date_runtime_cache",
            "использована сохранённая версия на точную дату",
        ),
        (
            "invalid_exact_snapshot=zero_filled_seller_funnel_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=zero_filled_web_source_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=zero_filled_prices_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=zero_filled_ads_bids_snapshot",
            "источник вернул нулевой результат",
        ),
        (
            "invalid_exact_snapshot=promo_live_source_incomplete",
            "получена неполная версия",
        ),
        ("no payload returned", "данные не получены"),
    )
    for marker, message in replacements:
        if marker in normalized:
            return message
    if "closure_state=closure_retrying" in normalized:
        return "источник ещё не закрылся на нужную дату; будет повторная попытка"
    if "closure_state=closure_pending" in normalized:
        return "источник ещё не закрылся на нужную дату"
    if "closure_state=closure_rate_limited" in normalized:
        return "источник ограничил запросы; повторная попытка запланирована"
    if "closure_state=closure_exhausted" in normalized:
        return "повторные попытки исчерпаны"
    if "resolution_rule=latest_effective_from<=slot_date" in normalized:
        return ""
    return normalized


def _note_requires_warning(note: str) -> bool:
    normalized = str(note or "").strip()
    if not normalized:
        return False
    success_markers = {
        "resolution_rule=accepted_closed_current_attempt",
        "resolution_rule=accepted_current_current_attempt",
        "resolution_rule=latest_effective_from<=slot_date",
    }
    if any(marker in normalized for marker in success_markers):
        return False
    warning_markers = {
        "runtime_cache",
        "preserved_after_invalid_attempt",
        "resolution_rule=accepted_closed_from_",
        "resolution_rule=accepted_closed_runtime_snapshot",
        "resolution_rule=accepted_prior_current_runtime_cache",
    }
    return any(marker in normalized for marker in warning_markers)


def _worst_tone(statuses: Iterable[str]) -> str:
    values = [str(item or "").strip() for item in statuses]
    if any(item == "error" for item in values):
        return "error"
    if any(item == "warning" for item in values):
        return "warning"
    return "success"


def _activity_tone_rank(tone: str) -> int:
    return WEB_VITRINA_ACTIVITY_TONE_RANK.get(str(tone or "").strip(), 4)


def _web_vitrina_activity_item_copy(source_key: str) -> dict[str, str]:
    spec = SOURCE_DIAGNOSTIC_SPECS.get(source_key, {})
    item_copy = WEB_VITRINA_ACTIVITY_ITEM_COPY.get(source_key, {})
    endpoint_label = str(spec.get("endpoint") or "").strip()
    technical_parts = [source_key] if source_key else []
    if endpoint_label:
        technical_parts.append(endpoint_label)
    return {
        "label_ru": str(item_copy.get("label_ru") or source_key),
        "description_ru": str(item_copy.get("description_ru") or ""),
        "technical_key": source_key,
        "technical_text": " · ".join(part for part in technical_parts if part),
        "endpoint_label": endpoint_label,
    }


def _activity_reason_ru(*, tone: str, detail: str, note: str) -> str:
    normalized_tone = str(tone or "").strip()
    if normalized_tone == "success":
        return ""
    candidate = _humanize_activity_reason_text(detail, tone=normalized_tone) or _humanize_activity_reason_text(
        note,
        tone=normalized_tone,
    )
    if candidate:
        return candidate
    if normalized_tone == "error":
        return "источник завершился ошибкой, подробности доступны в логе"
    if normalized_tone == "warning":
        return "обновление не подтверждено, подробности доступны в логе"
    return ""


def _humanize_activity_reason_text(text: str, *, tone: str = "") -> str:
    normalized = _normalize_activity_reason_text(text)
    if not normalized:
        return ""
    parts: list[tuple[int, int, str]] = []
    for index, raw_part in enumerate(normalized.split(" · ")):
        prefix, body = _split_activity_reason_part(raw_part)
        humanized = _summarize_activity_reason_part(body, prefix=prefix)
        if not humanized:
            continue
        parts.append((_activity_reason_part_rank(body, humanized), index, humanized))
    deduplicated = list(
        dict.fromkeys(
            summary
            for _rank, _index, summary in sorted(parts, key=lambda item: (item[0], item[1], item[2]))
        )
    )
    if deduplicated:
        if len(deduplicated) == 1:
            single = deduplicated[0]
            for prefix in ("сегодня ", "вчера ", "за вчера ", "срез "):
                if single.startswith(prefix):
                    return _truncate_activity_reason(single[len(prefix) :].strip())
        return _truncate_activity_reason("; ".join(deduplicated[:2]))
    if _looks_like_technical_activity_reason_text(normalized):
        return _activity_reason_fallback(tone)
    humanized_note = _humanize_note(normalized)
    if humanized_note and humanized_note != normalized:
        return _truncate_activity_reason(humanized_note)
    if normalized == "обновление подтверждено":
        return ""
    return _truncate_activity_reason(normalized)


def _normalize_activity_reason_text(text: str) -> str:
    return " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())


def _split_activity_reason_part(part: str) -> tuple[str, str]:
    normalized = str(part or "").strip()
    if ": " not in normalized:
        return "", normalized
    prefix_candidate, body = normalized.split(": ", 1)
    if prefix_candidate not in {"вчера", "сегодня", "snapshot", "срез"}:
        return "", normalized
    prefix = "срез" if prefix_candidate == "snapshot" else prefix_candidate
    return prefix, body.strip()


def _summarize_activity_reason_part(text: str, *, prefix: str = "") -> str:
    normalized = _normalize_activity_reason_text(text)
    if not normalized:
        return ""
    lowered = normalized.lower()
    if _activity_reason_is_success_only(lowered):
        return ""

    rate_limited = _activity_reason_has_any(
        lowered,
        "429",
        "too many requests",
        "retry-after",
        "rate limit",
        "ограничил запросы",
        "closure_rate_limited",
    )
    session_invalid = _activity_reason_has_any(
        lowered,
        "seller_portal_session_invalid",
        "seller_portal_session_missing",
        "seller_portal_wrong_supplier",
        "manual_relogin_required=login_and_save_state",
    )
    sync_failed = _activity_reason_has_any(
        lowered,
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "sync failed",
    )
    timeout = _activity_reason_has_any(
        lowered,
        "timeout",
        "timed out",
        "response not captured",
        "not captured",
        "deadline exceeded",
    )
    no_data = _activity_reason_has_any(
        lowered,
        "no payload returned",
        "payload не materialized",
        "payload not materialized",
        "источник не вернул payload",
        "данные не получены",
    )
    empty = _activity_reason_has_any(
        lowered,
        "no compact ads rows returned",
        "empty result",
        "empty payload",
        "пустой результат",
        "вернул пустой результат",
    )
    zero = _activity_reason_has_any(
        lowered,
        "zero_filled",
        "нулев",
    ) or ("invalid_exact_snapshot" in lowered and "=0" in lowered)
    incomplete = _activity_reason_has_any(
        lowered,
        "promo_live_source_incomplete",
        "получена неполная версия",
        "incomplete",
    )
    requested_date_mismatch = (
        ("requested_date=" in lowered and "latest_available_date=" in lowered)
        or ("requested_window=" in lowered and "latest_available_window=" in lowered)
    )
    blocked = _activity_reason_has_any(
        lowered,
        "collector_status=blocked",
        "источник помечен как blocked",
        "source is blocked",
    )
    not_refreshed = _activity_reason_has_any(
        lowered,
        "persisted status не содержит итог по источнику",
        "слот не обновлялся",
        "not refreshed",
        "неактуаль",
        "stale",
        "invalid_exact_snapshot",
    )
    unchanged = _activity_reason_has_any(
        lowered,
        "unchanged",
        "no-op",
        "not changed",
        "обновление не изменило",
    )

    failure_clause = ""
    if session_invalid:
        failure_clause = (
            "после входа выбран не тот кабинет; требуется повторный recovery"
            if "seller_portal_wrong_supplier" in lowered
            else "сессия seller portal больше не действует; требуется повторный вход"
        )
    elif rate_limited and sync_failed and timeout:
        failure_clause = "источник временно ограничил запросы, а дополнительная синхронизация завершилась по таймауту"
    elif rate_limited and sync_failed:
        failure_clause = "источник временно ограничил запросы, а дополнительная синхронизация завершилась с ошибкой"
    elif rate_limited:
        failure_clause = "источник временно ограничил запросы"
    elif sync_failed and timeout:
        failure_clause = "дополнительная синхронизация завершилась по таймауту"
    elif sync_failed:
        failure_clause = "дополнительная синхронизация завершилась с ошибкой"
    elif timeout:
        failure_clause = "запрос завершился по таймауту"

    data_clause = ""
    if no_data:
        data_clause = "данные не получены"
    elif empty:
        data_clause = "источник вернул пустой результат"
    elif zero:
        data_clause = "источник вернул нулевые данные, обновление не подтверждено"
    elif incomplete:
        data_clause = "получена неполная версия"
    elif requested_date_mismatch:
        data_clause = "получены данные за предыдущую доступную дату"
    elif blocked:
        data_clause = "источник временно недоступен"
    elif unchanged:
        data_clause = "обновление не изменило данные"
    elif not_refreshed:
        data_clause = "обновление не подтверждено"

    state_clause = ""
    if _activity_reason_has_any(lowered, "closure_state=closure_exhausted", "retry для closed-day snapshot исчерпан"):
        state_clause = "повторные попытки исчерпаны"
    elif _activity_reason_has_any(
        lowered,
        "closure_state=closure_retrying",
        "closed-day snapshot ещё не принят; будет retry",
        "closed-day snapshot ещё не готов; ожидается retry",
    ):
        state_clause = "повторная попытка уже запланирована"
    elif "closure_state=closure_pending" in lowered:
        state_clause = "источник ещё не закрылся на нужную дату"
    elif _activity_reason_has_any(
        lowered,
        "resolution_rule=accepted_closed_from_prior_current_cache",
        "resolution_rule=accepted_prior_current_runtime_cache",
        "resolution_rule=exact_date_stocks_history_runtime_cache",
        "resolution_rule=exact_date_promo_current_runtime_cache",
        "resolution_rule=exact_date_runtime_cache",
        "runtime cache",
    ):
        state_clause = "использована подтверждённая версия из runtime cache"
    elif _activity_reason_has_any(
        lowered,
        "resolution_rule=accepted_closed_preserved_after_invalid_attempt",
        "resolution_rule=accepted_current_preserved_after_invalid_attempt",
        "resolution_rule=accepted_closed_from_prior_current_snapshot",
        "resolution_rule=accepted_closed_runtime_snapshot",
        "resolution_rule=accepted_closed_from_interval_replay",
        "interval_replay",
        "interval replay",
        "сохранён ранее принятый closed snapshot после невалидной попытки",
        "использован ранее принятый current snapshot предыдущего дня",
        "использована последняя подтверждённая закрытая версия",
    ):
        state_clause = "использована последняя подтверждённая версия"

    primary_clause = _join_activity_reason_clauses(data_clause, failure_clause)
    clauses = [clause for clause in (primary_clause, state_clause) if clause]
    if not clauses:
        mapped = _mapped_activity_reason_text(normalized)
        if not mapped:
            return ""
        clauses = [mapped]
    summary = "; ".join(dict.fromkeys(clauses[:2]))
    return _apply_activity_reason_prefix(summary, prefix)


def _mapped_activity_reason_text(text: str) -> str:
    replacements = (
        ("seller_portal_session_invalid", "сессия seller portal больше не действует; требуется повторный вход"),
        ("seller_portal_session_missing", "сессия seller portal отсутствует; требуется повторный вход"),
        ("seller_portal_wrong_supplier", "после входа выбран не тот кабинет; требуется recovery с переключением supplier"),
        ("Persisted STATUS не содержит итог по источнику", "итог по источнику не подтверждён"),
        ("payload не materialized", "данные не получены"),
        ("источник не вернул payload", "данные не получены"),
        ("слот не обновлялся в текущем contour", "источник не обновлялся"),
        ("источник помечен как blocked в текущем contour", "источник временно недоступен"),
        ("использован сохранённый current snapshot из runtime cache", "использована подтверждённая версия из runtime cache"),
        ("использован ранее принятый current snapshot из runtime cache", "использована подтверждённая версия из runtime cache"),
        ("использован ранее принятый closed-day snapshot", "использована последняя подтверждённая версия"),
        ("использована сохранённая закрытая версия из interval replay", "использована сохранённая закрытая версия"),
        ("closed-day snapshot ещё не готов; ожидается retry", "источник ещё не закрылся на нужную дату"),
        ("closed-day snapshot ещё не принят; будет retry", "источник ещё не закрылся на нужную дату"),
        ("retry для closed-day snapshot исчерпан", "повторные попытки исчерпаны"),
        ("источник ограничил запросы; retry запланирован", "источник ограничил запросы; повторная попытка запланирована"),
        ("источник не вернул данные на точную дату", "данные на нужную дату не получены"),
        ("обновление подтверждено", ""),
    )
    for marker, replacement in replacements:
        if marker in text:
            return replacement
    humanized_note = _humanize_note(text)
    if humanized_note and humanized_note != text:
        return humanized_note
    if _looks_like_technical_activity_reason_text(text):
        return ""
    return text


def _activity_reason_has_any(text: str, *markers: str) -> bool:
    return any(marker in text for marker in markers)


def _activity_reason_part_rank(raw_text: str, humanized: str) -> int:
    lowered = _normalize_activity_reason_text(raw_text).lower()
    if _activity_reason_has_any(
        lowered,
        "429",
        "too many requests",
        "timeout",
        "timed out",
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "no payload returned",
        "empty result",
        "zero_filled",
        "invalid_exact_snapshot",
        "collector_status=blocked",
    ):
        return 0
    if _activity_reason_has_any(
        humanized.lower(),
        "использована",
        "повторная попытка",
        "источник ещё не закрылся",
    ):
        return 1
    return 2


def _activity_reason_is_success_only(lowered: str) -> bool:
    success_markers = (
        "resolution_rule=accepted_closed_current_attempt",
        "resolution_rule=accepted_current_current_attempt",
        "resolution_rule=latest_effective_from<=slot_date",
        "resolution_rule=explicit_or_latest_date_match",
        "accepted_at=",
    )
    if not any(marker in lowered for marker in success_markers):
        return False
    non_success_markers = (
        "invalid_exact_snapshot",
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "collector_status=blocked",
        "429",
        "too many requests",
        "timeout",
        "timed out",
        "no payload returned",
        "empty result",
        "no compact ads rows returned",
        "runtime cache",
        "preserved_after_invalid_attempt",
        "accepted_closed_from_",
    )
    return not any(marker in lowered for marker in non_success_markers)


def _join_activity_reason_clauses(primary: str, secondary: str) -> str:
    first = str(primary or "").strip()
    second = str(secondary or "").strip()
    if first and second:
        return f"{first}, а {second}"
    return first or second


def _apply_activity_reason_prefix(reason: str, prefix: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        return ""
    if prefix == "сегодня":
        return f"сегодня {normalized}"
    if prefix == "вчера":
        if normalized.startswith("использована"):
            return f"за вчера {normalized}"
        return f"вчера {normalized}"
    return normalized


def _looks_like_technical_activity_reason_text(text: str) -> bool:
    lowered = _normalize_activity_reason_text(text).lower()
    if not lowered:
        return False
    markers = (
        "{",
        "}",
        "traceback",
        "requestid",
        "statustext",
        "origin=",
        "timestamp=",
        "resolution_rule=",
        "accepted_at=",
        "current_day_web_source_sync_failed=",
        "closed_day_sync_error=",
        "collector_mode=",
        "trace_run_dir=",
        "collector_status=",
        "manual_relogin_required=",
        "final_url=",
        "archive_reuse_enabled=",
        "archive_mode=",
        "archive_scanned=",
        "archive_created=",
        "archive_updated=",
        "archive_unchanged=",
        "archive_keys=",
        "covering_campaigns=",
        "usable_campaigns=",
        "playwright._impl._errors",
        "runtimeerror:",
        "http://",
        "https://",
    )
    return any(marker in lowered for marker in markers)


def _activity_reason_fallback(tone: str) -> str:
    if str(tone or "").strip() == "error":
        return "источник завершился ошибкой, подробности доступны в логе"
    return "обновление не подтверждено, подробности доступны в логе"


def _truncate_activity_reason(text: str, *, limit: int = 220) -> str:
    normalized = str(text or "").strip()
    clauses = [clause.strip() for clause in normalized.split("; ") if clause.strip()]
    if len(clauses) > 2:
        normalized = "; ".join(clauses[:2])
    if len(normalized) <= limit:
        return normalized
    head, _, _tail = normalized.partition("; ")
    if head and len(head) <= limit:
        return head
    return normalized[: max(limit - 1, 0)].rstrip(" ,;:") + "…"


def _activity_summary_detail(
    *,
    description_ru: str,
    reason_ru: str,
    fallback_detail: str,
) -> str:
    description = str(description_ru or "").strip()
    reason = str(reason_ru or "").strip()
    if description and reason:
        return f"{description} Причина: {reason}"
    if description:
        return description
    if reason:
        return reason
    return str(fallback_detail or "").strip()


def _first_distinct_note(notes: list[str]) -> str:
    for note in notes:
        normalized = str(note).strip()
        if normalized:
            return normalized
    return ""


def _parse_log_event_line(line: str) -> tuple[str, dict[str, str]] | None:
    text = str(line or "").strip()
    if not text:
        return None
    if " " in text:
        _, candidate = text.split(" ", 1)
    else:
        candidate = text
    try:
        parts = shlex.split(candidate)
    except ValueError:
        return None
    fields: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[str(key)] = str(value)
    event = str(fields.get("event") or "").strip()
    if not event:
        return None
    return event, fields
