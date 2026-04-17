"""Application-слой HTTP entrypoint для registry upload и sheet_vitrina_v1 operator flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_load_bridge import load_sheet_vitrina_ready_snapshot_via_clasp
from packages.business_time import (
    CANONICAL_BUSINESS_TIMEZONE_NAME,
    DAILY_REFRESH_BUSINESS_HOUR,
    DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
    DAILY_REFRESH_SYSTEMD_UTC_TIME,
    current_business_date_iso,
    default_business_as_of_date,
    to_business_datetime,
)
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.cost_price_upload import CostPriceUploadResult
from packages.contracts.registry_upload_file_backed_service import RegistryUploadResult
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Envelope

OperatorLogEmitter = Callable[[str], None]
SheetLoadRunner = Callable[[SheetVitrinaV1Envelope, OperatorLogEmitter], dict[str, Any]]


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
        self.sheet_plan_block = SheetVitrinaV1LivePlanBlock(runtime=self.runtime)
        self.sheet_load_runner = sheet_load_runner or load_sheet_vitrina_ready_snapshot_via_clasp
        self.operator_jobs = SheetVitrinaV1OperatorJobStore(timestamp_factory=self.activated_at_factory)

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
        return payload

    def handle_sheet_refresh_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self._run_sheet_refresh(as_of_date=as_of_date, log=None)

    def handle_sheet_load_request(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self._run_sheet_load(as_of_date=as_of_date, log=None)

    def start_sheet_refresh_job(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="refresh",
            runner=lambda log: self._run_sheet_refresh(as_of_date=as_of_date, log=log),
        )

    def start_sheet_load_job(self, as_of_date: str | None = None) -> dict[str, Any]:
        return self.operator_jobs.start(
            operation="load",
            runner=lambda log: self._run_sheet_load(as_of_date=as_of_date, log=log),
        )

    def handle_sheet_operator_job_request(self, job_id: str) -> dict[str, Any]:
        return self.operator_jobs.get(job_id)

    def _run_sheet_refresh(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        emit("Старт refresh ready snapshot.")
        current_state = self.runtime.load_current_state()
        emit(f"Активный bundle_version: {current_state.bundle_version}")
        if as_of_date:
            emit(f"Запрошен as_of_date={as_of_date}.")
        else:
            emit("as_of_date не указан, используем server-side default.")
        emit("Собираем ready snapshot на сервере...")
        plan = self.sheet_plan_block.build_plan(as_of_date=as_of_date)
        emit(
            "Ready snapshot собран: "
            f"{plan.snapshot_id} · даты {', '.join(plan.date_columns) or plan.as_of_date}"
        )
        emit("Сохраняем ready snapshot в runtime...")
        refresh_result = self.runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=self.refreshed_at_factory(),
            plan=plan,
        )
        payload = asdict(refresh_result)
        payload["server_context"] = self.build_sheet_server_context()
        emit(f"Refresh завершён: snapshot_id={refresh_result.snapshot_id}")
        return payload

    def _run_sheet_load(
        self,
        *,
        as_of_date: str | None,
        log: OperatorLogEmitter | None,
    ) -> dict[str, Any]:
        emit = log or _noop_log
        emit("Старт load готового snapshot в live sheet.")
        current_state = self.runtime.load_current_state()
        emit(f"Активный bundle_version: {current_state.bundle_version}")
        if as_of_date:
            emit(f"Ищем ready snapshot для as_of_date={as_of_date}.")
        else:
            emit("Ищем последний persisted ready snapshot для current bundle.")
        plan = self.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
        refresh_status = self.runtime.load_sheet_vitrina_refresh_status(as_of_date=plan.as_of_date)
        emit(
            "Ready snapshot найден: "
            f"{plan.snapshot_id} · даты {', '.join(plan.date_columns) or plan.as_of_date}"
        )
        emit("Передаём ready snapshot в bound sheet bridge...")
        bridge_result = self.sheet_load_runner(plan, emit)
        row_counts = {item.sheet_name: item.row_count for item in plan.sheets}
        emit(
            "Load завершён: "
            f"DATA_VITRINA={row_counts.get('DATA_VITRINA', '-')} "
            f"STATUS={row_counts.get('STATUS', '-')}"
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
        return payload

    def build_sheet_server_context(self) -> dict[str, str]:
        now = self.now_factory()
        business_now = to_business_datetime(now).replace(microsecond=0).isoformat()
        return {
            "business_timezone": CANONICAL_BUSINESS_TIMEZONE_NAME,
            "business_now": business_now,
            "default_as_of_date": default_business_as_of_date(now),
            "today_current_date": current_business_date_iso(now),
            "daily_refresh_business_time": f"{DAILY_REFRESH_BUSINESS_HOUR:02d}:00 {CANONICAL_BUSINESS_TIMEZONE_NAME}",
            "daily_refresh_systemd_time": DAILY_REFRESH_SYSTEMD_UTC_TIME,
            "daily_refresh_systemd_oncalendar": DAILY_REFRESH_SYSTEMD_UTC_ONCALENDAR,
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
            if len(job.log_lines) > 200:
                job.log_lines = job.log_lines[-200:]


def _noop_log(_: str) -> None:
    return
