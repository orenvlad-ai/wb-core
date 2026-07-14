"""Bounded application block for live WB SPP proxy threshold tests."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib import parse as urllib_parse
from uuid import uuid4
from zoneinfo import ZoneInfo

from packages.adapters.spp_proxy_block import HttpBackedPublicWbCardBuyerPriceSource
from packages.adapters.wb_prices_management import (
    HttpBackedWbPricesManagementSource,
    WbPricesApiError,
    WbPricesManagementSource,
)
from packages.application.wb_prices_management import (
    WbPricesManagementError,
    map_upload_status,
    normalize_goods_payload,
    normalize_quarantine_good,
)
from packages.application.wb_buyer_session import WbBuyerSessionBlock
from packages.contracts.spp_proxy_block import SppProxyRequest
from packages.contracts.wb_buyer_session import WbAuthenticatedBuyerPriceSource
from packages.contracts.wb_spp_tester import (
    SPP_TEST_ACTIVE_STATUSES,
    SPP_TEST_CONTRACT_PREFIX,
    SPP_TEST_DEFAULT_MAX_MEASUREMENTS,
    SPP_TEST_DEFAULT_PRECISION_RUB,
    SPP_TEST_HISTORY_DEFAULT_LIMIT,
    SPP_TEST_HISTORY_MAX_LIMIT,
    SPP_TEST_MAX_MEASUREMENTS_MAX,
    SPP_TEST_MAX_MEASUREMENTS_MIN,
    SPP_TEST_MODE_SAFE_SLOW,
    SPP_TEST_SCHEDULE_LATE_WINDOW_MINUTES,
    SPP_TEST_SCHEDULE_TIMEZONE,
    SPP_TEST_SCHEDULE_TIMEZONE_LABEL,
    SppTestPlan,
    SppTestPointPlan,
)


BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
MONEY = Decimal("0.01")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
SCHEDULE_ID = "daily"
SCHEDULE_CADENCE = "daily"
SCHEDULE_CONTRACT_NAME = f"{SPP_TEST_CONTRACT_PREFIX}_schedule"
HISTORY_CONTRACT_NAME = f"{SPP_TEST_CONTRACT_PREFIX}_history"
HISTORY_DETAIL_CONTRACT_NAME = f"{SPP_TEST_CONTRACT_PREFIX}_history_detail"
SCHEDULER_TICK_CONTRACT_NAME = f"{SPP_TEST_CONTRACT_PREFIX}_scheduler_tick"
SAFETY_STOP_POINT_STATUSES = {
    "rate_limited_stop",
    "quarantine_detected",
    "readback_mismatch",
    "upload_not_success",
    "upload_missing_id",
    "public_429",
    "public_unstable",
    "buyer_session_invalid",
    "buyer_session_lost",
    "authenticated_unstable",
    "buyer_destination_mismatch",
}


class WbSppTesterError(ValueError):
    """Expected validation/safety error for the SPP tester block."""

    def __init__(self, message: str, *, http_status: int = 400, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.payload = dict(payload or {})


class WbSppPublicBuyerPriceSource(Protocol):
    """Anonymous public buyer price source used by SPP tester."""

    def fetch_public_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        raise NotImplementedError("adapter skeleton only")


class SppTesterPublicCardSource:
    """Thin wrapper over the existing current-only public WB card source."""

    def __init__(
        self,
        *,
        source: HttpBackedPublicWbCardBuyerPriceSource | None = None,
        business_date_factory: Callable[[], str] | None = None,
    ) -> None:
        self.source = source or HttpBackedPublicWbCardBuyerPriceSource()
        self.business_date_factory = business_date_factory or _current_business_date

    def fetch_public_buyer_price(self, nm_id: int) -> Mapping[str, Any]:
        payload = self.source.fetch(
            SppProxyRequest(
                snapshot_type="spp_proxy",
                snapshot_date=self.business_date_factory(),
                nm_ids=[int(nm_id)],
            )
        )
        items = _payload_items(payload)
        item = next((row for row in items if _optional_int(row.get("nmId") or row.get("nm_id")) == int(nm_id)), None)
        missing = _payload_missing(payload, nm_id=int(nm_id))
        diagnostics = dict(item.get("diagnostics") or {}) if isinstance(item, Mapping) else dict(missing)
        source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
        payload_diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), Mapping) else {}
        destination_context = _destination_context_from_url(str((item or {}).get("api_url") or ""))
        if not destination_context:
            destination_context = _destination_context_from_region_label(
                str(source.get("region_context") or payload_diagnostics.get("region_context") or "")
            )
        if item is None:
            return {
                "status": "missing",
                "public_buyer_price": None,
                "endpoint": "public_wb_card",
                "http_status": diagnostics.get("card_http_status"),
                "headers": {},
                "body_summary": "",
                "diagnostics": diagnostics,
            }
        return {
            "status": "ok",
            "public_buyer_price": _number_or_none(item.get("public_buyer_price")),
            "endpoint": str(item.get("api_url") or item.get("card_url") or "public_wb_card"),
            "http_status": diagnostics.get("card_http_status"),
            "headers": {},
            "body_summary": "",
            "diagnostics": diagnostics,
            "destination_context": destination_context,
        }


@dataclass(frozen=True)
class WbSppTesterSafetyConfig:
    spp_test_enabled: bool
    prices_write_enabled: bool
    restore_baseline_required: bool = True


@dataclass(frozen=True)
class WbSppTesterCadenceConfig:
    run_async: bool = True
    measurement_upload_cooldown_seconds: int = 600
    first_public_poll_delay_seconds: int = 60
    public_poll_gap_seconds: int = 90
    extended_public_poll_gap_seconds: int = 120
    upload_status_poll_seconds: int = 20
    upload_status_max_polls: int = 24
    readback_poll_seconds: int = 20
    readback_max_polls: int = 12
    rate_limit_min_cooldown_seconds: int = 900
    active_lock_ttl_seconds: int = 1800
    schedule_late_window_minutes: int = SPP_TEST_SCHEDULE_LATE_WINDOW_MINUTES


class WbSppTesterBlock:
    """Server-owned live SPP tester with guarded writes and staged restore."""

    def __init__(
        self,
        *,
        runtime: Any,
        runtime_dir: Path,
        prices_source: WbPricesManagementSource | None = None,
        public_source: WbSppPublicBuyerPriceSource | None = None,
        buyer_source: WbAuthenticatedBuyerPriceSource | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        safety_config: WbSppTesterSafetyConfig | None = None,
        cadence_config: WbSppTesterCadenceConfig | None = None,
    ) -> None:
        self.runtime = runtime
        self.runtime_dir = runtime_dir
        self.prices_source = prices_source or HttpBackedWbPricesManagementSource()
        self.public_source = public_source or SppTesterPublicCardSource()
        self.buyer_source = buyer_source or WbBuyerSessionBlock()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or time.sleep
        self.safety = safety_config or _load_safety_config()
        self.cadence = cadence_config or _load_cadence_config()
        self._state_dir = self.runtime_dir / "sheet_vitrina_v1_prices" / "spp_tests"
        self._jobs_dir = self._state_dir / "jobs"
        self._current_job_path = self._state_dir / "current_job.json"
        self._audit_path = self._state_dir / "audit.jsonl"
        self._schedule_path = self._state_dir / "schedule.json"
        self._execution_lock_path = self._state_dir / "execution.lock"
        self._schedule_lock_path = self._state_dir / "schedule.lock"
        self._threads: dict[str, threading.Thread] = {}
        self._execution_locks: dict[str, Any] = {}
        self._thread_lock = threading.RLock()
        self._state_lock = threading.RLock()

    def build_baseline(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        nm_id = _as_positive_int(_single_param((params or {}).get("nmID") or (params or {}).get("nm_id")), "nmID")
        baseline = self._capture_baseline(nm_id=nm_id, strict=False)
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_baseline",
            "generated_at": self.timestamp_factory(),
            "write_enabled": self.safety.prices_write_enabled,
            "spp_test_enabled": self.safety.spp_test_enabled,
            "baseline": baseline,
        }

    def build_plan(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        buyer_session = self._require_buyer_session()
        nm_id, range_min, range_max, precision, max_measurements = self._parse_plan_input(payload)
        initial_values = _dedupe_money([range_min, (range_min + range_max) / Decimal("2"), range_max])
        initial_points = [
            SppTestPointPlan(target_discounted_price=_decimal_to_float(value), kind=kind)
            for value, kind in zip(initial_values, ["min", "midpoint", "max"])
        ]
        estimated_seconds = _estimate_duration_seconds(
            max_measurements=max_measurements,
            cadence=self.cadence,
        )
        plan = SppTestPlan(
            nm_id=nm_id,
            range_min_discounted=_decimal_to_float(range_min),
            range_max_discounted=_decimal_to_float(range_max),
            precision_rub=_decimal_to_float(precision),
            max_measurements=max_measurements,
            mode=SPP_TEST_MODE_SAFE_SLOW,
            initial_points=initial_points,
            refinement_budget=max(0, max_measurements - len(initial_points)),
            estimated_duration_seconds=estimated_seconds,
            request_budget={
                "wb_uploads": max_measurements,
                "wb_upload_status_polls": max_measurements * self.cadence.upload_status_max_polls,
                "wb_readbacks": max_measurements * self.cadence.readback_max_polls,
                "authenticated_reads": max_measurements * 3,
                "anonymous_control_reads": max_measurements * 3,
                "public_reads": max_measurements * 3,
                "quarantine_checks": max_measurements + 4,
            },
            restore_route=[
                {"step": "baseline", "kind": "final_proof_required"},
                {"step": "bridge", "kind": "only_if_downward_move_over_25_percent"},
            ],
            warnings=[
                "test_is_live_and_temporarily_changes_wb_price",
                "restore_baseline_is_required_in_mvp",
                "safe_slow_mode_only",
            ],
        )
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_plan",
            "generated_at": self.timestamp_factory(),
            "plan": plan.to_dict(),
            "active_job": self._current_job_summary(),
            "buyer_session": buyer_session,
        }

    def history(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        limit = _bounded_int(
            _single_param(params.get("limit")),
            minimum=1,
            maximum=SPP_TEST_HISTORY_MAX_LIMIT,
            default=SPP_TEST_HISTORY_DEFAULT_LIMIT,
        )
        cursor = _decode_history_cursor(str(_single_param(params.get("cursor")) or ""))
        keyed_rows = [
            (_history_sort_key(job), self._history_summary(job))
            for job in self._load_history_jobs()
        ]
        keyed_rows.sort(key=lambda item: item[0], reverse=True)
        if cursor is not None:
            keyed_rows = [item for item in keyed_rows if item[0] < cursor]
        page_rows = keyed_rows[:limit]
        page = [item[1] for item in page_rows]
        has_more = len(keyed_rows) > limit
        next_cursor = ""
        if has_more and page_rows:
            next_cursor = _encode_history_cursor(*page_rows[-1][0])
        return {
            "contract_name": HISTORY_CONTRACT_NAME,
            "generated_at": self.timestamp_factory(),
            "items": page,
            "limit": limit,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def history_detail(self, job_id: str) -> dict[str, Any]:
        normalized = str(job_id or "").strip()
        if not JOB_ID_RE.fullmatch(normalized):
            raise WbSppTesterError("invalid SPP test job_id", http_status=400)
        job = self._load_job(normalized)
        if not job:
            raise WbSppTesterError("SPP test job was not found", http_status=404)
        return {
            "contract_name": HISTORY_DETAIL_CONTRACT_NAME,
            "generated_at": self.timestamp_factory(),
            "job": self._job_public_payload(job, details=True),
        }

    def get_schedule(self) -> dict[str, Any]:
        with self._schedule_file_lock():
            schedule = self._load_schedule_unlocked()
        return self._schedule_response(schedule)

    def save_schedule(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        raw = payload.get("schedule") if isinstance(payload.get("schedule"), Mapping) else payload
        if _coerce_bool(raw.get("enabled")):
            self._require_buyer_session()
        now = self._now()
        with self._schedule_file_lock():
            existing = self._load_schedule_unlocked()
            schedule = self._normalize_schedule_for_save(raw, existing=existing, now=now, actor=actor)
            self._write_schedule_unlocked(schedule)
        self._append_audit(
            f"schedule:{SCHEDULE_ID}",
            "schedule_saved",
            {
                "actor": actor,
                "enabled": schedule["enabled"],
                "nmID": schedule.get("nmID"),
                "local_time_hhmm": schedule["local_time_hhmm"],
                "timezone": schedule["timezone"],
                "next_run_at": schedule.get("next_run_at"),
                "future_live_price_changes_confirmed": schedule.get("future_live_price_changes_confirmed"),
            },
        )
        return self._schedule_response(schedule)

    def run_due_schedule_tick(self) -> dict[str, Any]:
        now = self._now()
        with self._schedule_file_lock():
            schedule = self._load_schedule_unlocked()
            if not schedule.get("enabled"):
                return self._scheduler_tick_response("disabled", schedule=schedule)
            due_at = _parse_iso_datetime(schedule.get("next_run_at"))
            if due_at is None:
                schedule["next_run_at"] = _next_daily_run_at(
                    now,
                    local_time_hhmm=str(schedule["local_time_hhmm"]),
                    timezone_name=str(schedule["timezone"]),
                ).isoformat()
                self._write_schedule_unlocked(schedule)
                return self._scheduler_tick_response("not_due", schedule=schedule)
            if now < due_at:
                return self._scheduler_tick_response("not_due", schedule=schedule, due_at=due_at)
            business_date = due_at.astimezone(ZoneInfo(str(schedule["timezone"]))).date().isoformat()
            if str(schedule.get("last_claimed_business_date") or "") == business_date:
                schedule["next_run_at"] = _next_daily_run_at(
                    due_at + timedelta(seconds=1),
                    local_time_hhmm=str(schedule["local_time_hhmm"]),
                    timezone_name=str(schedule["timezone"]),
                ).isoformat()
                schedule["last_scheduler_decision_at"] = now.isoformat()
                schedule["last_automatic_status"] = "already_claimed"
                self._write_schedule_unlocked(schedule)
                return self._scheduler_tick_response("already_claimed", schedule=schedule, due_at=due_at)
            late_seconds = max(0, int((now - due_at).total_seconds()))
            schedule["last_claimed_business_date"] = business_date
            schedule["last_due_at"] = due_at.isoformat()
            schedule["last_scheduler_decision_at"] = now.isoformat()
            schedule["last_automatic_status"] = "claimed"
            schedule["next_run_at"] = _next_daily_run_at(
                due_at + timedelta(seconds=1),
                local_time_hhmm=str(schedule["local_time_hhmm"]),
                timezone_name=str(schedule["timezone"]),
            ).isoformat()
            self._write_schedule_unlocked(schedule)

        self._append_audit(
            f"schedule:{SCHEDULE_ID}",
            "schedule_due_decision",
            {
                "decision": "claimed",
                "due_at": due_at.isoformat(),
                "business_date": business_date,
                "late_seconds": late_seconds,
                "late_window_seconds": int(self.cadence.schedule_late_window_minutes) * 60,
            },
        )
        if late_seconds > int(self.cadence.schedule_late_window_minutes) * 60:
            job = self._record_scheduled_skip(
                schedule,
                reason="missed_late_window",
                due_at=due_at,
                diagnostics={"late_seconds": late_seconds},
            )
            schedule = self._update_schedule_after_automatic_job(schedule, job)
            return self._scheduler_tick_response("skipped_late", schedule=schedule, due_at=due_at, job=job)

        start_payload = {
            "nmID": schedule.get("nmID"),
            "range_min_discounted": schedule.get("range_min_discounted"),
            "range_max_discounted": schedule.get("range_max_discounted"),
            "precision_rub": schedule.get("precision_rub"),
            "max_measurements": schedule.get("max_measurements"),
            "mode": SPP_TEST_MODE_SAFE_SLOW,
            "confirm_live_price_change": True,
            "restore_baseline": True,
        }
        try:
            started = self.start(
                start_payload,
                actor="systemd:spp_schedule",
                trigger_source="schedule",
                schedule_id=SCHEDULE_ID,
                run_async=False,
            )
            job = started.get("job") or {}
            schedule = self._update_schedule_after_automatic_job(schedule, job)
            return self._scheduler_tick_response("finished", schedule=schedule, due_at=due_at, job=job)
        except WbSppTesterError as exc:
            job = self._record_scheduled_skip(
                schedule,
                reason=_scheduled_skip_reason(exc),
                due_at=due_at,
                diagnostics={"error": str(exc), **dict(exc.payload)},
            )
            schedule = self._update_schedule_after_automatic_job(schedule, job)
            return self._scheduler_tick_response("skipped", schedule=schedule, due_at=due_at, job=job)

    def start(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str = "",
        trigger_source: str = "manual",
        schedule_id: str = "",
        run_async: bool | None = None,
    ) -> dict[str, Any]:
        if not self.safety.spp_test_enabled:
            raise WbSppTesterError(
                "WB SPP test writes are disabled; set WB_SPP_TEST_ENABLED=true",
                http_status=403,
            )
        if not self.safety.prices_write_enabled:
            raise WbSppTesterError(
                "WB price writes are disabled; set WB_PRICES_WRITE_ENABLED=true",
                http_status=403,
            )
        if not _coerce_bool(payload.get("confirm_live_price_change")):
            raise WbSppTesterError("confirm_live_price_change=true is required", http_status=400)
        if not _coerce_bool(payload.get("restore_baseline")):
            raise WbSppTesterError("restore_baseline=true is required in MVP", http_status=422)
        normalized_trigger = str(trigger_source or "manual").strip().lower()
        if normalized_trigger not in {"manual", "schedule"}:
            raise WbSppTesterError("trigger_source must be manual or schedule", http_status=400)
        execution_lock = self._acquire_execution_lock(
            owner=f"{normalized_trigger}:{actor or 'unknown'}",
            blocking=False,
        )
        if execution_lock is None:
            raise WbSppTesterError(
                "another SPP test runner holds the execution lock",
                http_status=409,
                payload={"reason": "execution_lock_busy", "active_job": self._current_job_summary(reconcile=False)},
            )
        job_id = ""
        lock_transferred = False
        try:
            blocking = self._blocking_current_job(reconcile=True, caller_holds_execution_lock=True)
            if blocking is not None:
                raise WbSppTesterError(
                    "another SPP test job is active or requires restore",
                    http_status=409,
                    payload={"reason": "active_or_unrestored_job", "active_job": blocking},
                )
            nm_id, range_min, range_max, precision, max_measurements = self._parse_plan_input(payload)
            buyer_session = self._require_buyer_session()
            baseline = self._capture_baseline(nm_id=nm_id, strict=True)
            plan_payload = self.build_plan(payload)["plan"]
            job_id = uuid4().hex
            now_text = self.timestamp_factory()
            job = {
                "job_id": job_id,
                "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_job",
                "created_at": now_text,
                "updated_at": now_text,
                "finished_at": "",
                "actor": actor,
                "trigger_source": normalized_trigger,
                "schedule_id": str(schedule_id or "") if normalized_trigger == "schedule" else "",
                "status": "planning",
                "result_status": "",
                "nmID": nm_id,
                "input": {
                    "range_min_discounted": _decimal_to_float(range_min),
                    "range_max_discounted": _decimal_to_float(range_max),
                    "precision_rub": _decimal_to_float(precision),
                    "max_measurements": max_measurements,
                    "mode": SPP_TEST_MODE_SAFE_SLOW,
                    "restore_baseline": True,
                },
                "baseline": baseline,
                "buyer_session": buyer_session,
                "plan": plan_payload,
                "measurements": [],
                "thresholds": [],
                "timeline": [],
                "restore": {"required": True, "restored": False, "proof": None, "steps": []},
                "lifecycle_diagnostics": {
                    "classification": "live",
                    "runner_pid": os.getpid(),
                    "runner_host": socket.gethostname(),
                    "runner_token": uuid4().hex,
                    "heartbeat_at": now_text,
                    "phase": "planning",
                },
                "manual_restore_required": False,
                "warnings": [],
                "error": "",
            }
            self._append_timeline(job, "planning", "job_created")
            self._save_job(job)
            self._write_current_job(job)
            self._append_audit(
                job_id,
                "job_start",
                {
                    "actor": actor,
                    "trigger_source": normalized_trigger,
                    "schedule_id": job.get("schedule_id"),
                    "input": job["input"],
                    "baseline": baseline,
                },
            )
            should_run_async = self.cadence.run_async if run_async is None else bool(run_async)
            if should_run_async:
                self._start_background_job(job_id, execution_lock)
                lock_transferred = True
            else:
                self._run_job(job_id)
        finally:
            if not lock_transferred:
                self._release_execution_lock(execution_lock, job_id=job_id)
        current = self._load_job(job_id)
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_start",
            "job": self._job_public_payload(current, details=True),
        }

    def status(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        requested_job_id = str(_single_param(params.get("job_id") or params.get("jobID")) or "").strip()
        if requested_job_id:
            job = self._load_job(requested_job_id)
            active_job = self._current_job_summary()
        else:
            active_job = self._current_job_summary()
            job = self._load_current_job_payload()
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_status",
            "generated_at": self.timestamp_factory(),
            "active_job": active_job,
            "job": self._job_public_payload(job, details=True) if job else None,
        }

    def restore(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
        if not self.safety.spp_test_enabled:
            raise WbSppTesterError(
                "WB SPP test restore is disabled; set WB_SPP_TEST_ENABLED=true",
                http_status=403,
            )
        if not self.safety.prices_write_enabled:
            raise WbSppTesterError(
                "WB price writes are disabled; set WB_PRICES_WRITE_ENABLED=true",
                http_status=403,
            )
        if not _coerce_bool(payload.get("confirm_restore")):
            raise WbSppTesterError("confirm_restore=true is required", http_status=400)
        job_id = str(payload.get("job_id") or payload.get("jobID") or "").strip()
        job = self._load_job(job_id) if job_id else self._load_current_job_payload()
        if not job:
            raise WbSppTesterError("SPP test job was not found", http_status=404)
        if not job.get("baseline"):
            raise WbSppTesterError("job has no captured baseline", http_status=409)
        execution_lock = self._acquire_execution_lock(owner=f"manual_restore:{actor or 'unknown'}", blocking=False)
        if execution_lock is None:
            raise WbSppTesterError(
                "SPP execution lock is held by a live runner",
                http_status=409,
                payload={"reason": "execution_lock_busy"},
            )
        try:
            self._append_audit(str(job["job_id"]), "emergency_restore_requested", {"actor": actor})
            restored = self._restore_baseline(job, reason="emergency_restore")
            job = self._load_job(str(job["job_id"])) or job
            if restored:
                job["status"] = "interrupted_restored"
                job["result_status"] = str(job.get("result_status") or "inconclusive")
                job["manual_restore_required"] = False
                job["finished_at"] = self.timestamp_factory()
                self._append_timeline(job, "interrupted_restored", "emergency_restore_confirmed")
            else:
                job["status"] = "manual_restore_required"
                job["result_status"] = "manual_restore_required"
                job["manual_restore_required"] = True
            job["updated_at"] = self.timestamp_factory()
            self._save_job(job)
            self._write_current_job(job)
            self._append_audit(
                str(job["job_id"]),
                "emergency_restore_finished",
                {"restored": restored, "status": job["status"]},
            )
        finally:
            self._release_execution_lock(execution_lock, job_id=str(job.get("job_id") or ""))
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_restore",
            "status": "restored" if restored else "manual_restore_required",
            "job": self._job_public_payload(job, details=True),
        }

    def _run_job(self, job_id: str) -> None:
        job = self._load_job(job_id)
        if not job:
            return
        try:
            self._set_job_status(job, "measuring", "measurement_started")
            self._execute_measurements(job)
            job = self._load_job(job_id) or job
            self._set_job_status(job, "restoring", "restore_started")
            restored = self._restore_baseline(job, reason="planned_restore")
            job = self._load_job(job_id) or job
            if not restored:
                self._set_job_status(job, "manual_restore_required", "restore_failed")
                job = self._load_job(job_id) or job
                job["finished_at"] = self.timestamp_factory()
                job["result_status"] = "manual_restore_required"
                job["manual_restore_required"] = True
                self._save_job(job)
                self._write_current_job(job)
                self._append_audit(job_id, "job_finish", {"status": job["status"], "restored": False})
                return
            result_status = str(job.get("result_status") or "inconclusive")
            final_status = "complete" if result_status not in {"manual_restore_required"} else result_status
            job["status"] = final_status
            job["updated_at"] = self.timestamp_factory()
            job["finished_at"] = self.timestamp_factory()
            job["manual_restore_required"] = False
            self._set_lifecycle(job, classification="terminal", phase=final_status)
            self._append_timeline(job, final_status, result_status)
            self._save_job(job)
            self._write_current_job(job)
            self._append_audit(
                job_id,
                "job_finish",
                {"status": final_status, "result_status": result_status, "restored": True},
            )
        except Exception as exc:
            job = self._load_job(job_id) or job
            job["error"] = str(exc)
            self._append_timeline(job, "failed", str(exc))
            self._save_job(job)
            try:
                restored = self._restore_baseline(job, reason="failure_restore")
            except Exception as restore_exc:  # pragma: no cover - last safety net
                job = self._load_job(job_id) or job
                job["error"] = f"{exc}; restore failed: {restore_exc}"
                restored = False
            job = self._load_job(job_id) or job
            if restored:
                job["status"] = "failed"
                job["result_status"] = job.get("result_status") or "inconclusive"
            else:
                job["status"] = "manual_restore_required"
                job["result_status"] = "manual_restore_required"
                job["manual_restore_required"] = True
            job["updated_at"] = self.timestamp_factory()
            job["finished_at"] = self.timestamp_factory()
            self._set_lifecycle(job, classification="terminal", phase=str(job["status"]))
            self._save_job(job)
            self._write_current_job(job)
            self._append_audit(
                job_id,
                "job_finish",
                {"status": job["status"], "result_status": job["result_status"], "restored": restored},
            )

    def _execute_measurements(self, job: dict[str, Any]) -> None:
        input_payload = job["input"]
        precision = _parse_money(input_payload["precision_rub"], "precision_rub")
        max_measurements = int(input_payload["max_measurements"])
        route = [
            _parse_money(point["target_discounted_price"], "target_discounted_price")
            for point in job["plan"].get("initial_points", [])
            if isinstance(point, Mapping)
        ]
        route = _dedupe_money(route)
        measured_targets: set[str] = set()
        stop_after_restore = False
        while route and len(job["measurements"]) < max_measurements:
            target = route.pop(0)
            signature = str(target.quantize(MONEY, rounding=ROUND_HALF_UP))
            if signature in measured_targets:
                continue
            measured_targets.add(signature)
            point = self._measure_point(job, target)
            job["measurements"].append(point)
            self._save_job(job)
            if point.get("status") in SAFETY_STOP_POINT_STATUSES:
                stop_after_restore = True
                break
            if len(job["measurements"]) < max_measurements and route:
                self._set_job_status(job, "cooldown", "between_measurements")
                self._sleep_with_heartbeat(job, self.cadence.measurement_upload_cooldown_seconds, phase="between_measurements")
                self._set_job_status(job, "measuring", "measurement_resumed")

        while not stop_after_restore and len(job["measurements"]) < max_measurements:
            thresholds, result_status = self._detect_thresholds(job["measurements"], precision=precision)
            job["thresholds"] = thresholds
            job["result_status"] = result_status
            self._save_job(job)
            suspect = _strongest_refinement_interval(thresholds, precision=precision)
            if suspect is None:
                break
            lower = _parse_money(suspect["lower_actual_discounted_price"], "lower_actual_discounted_price")
            upper = _parse_money(suspect["upper_actual_discounted_price"], "upper_actual_discounted_price")
            target = ((lower + upper) / Decimal("2")).quantize(MONEY, rounding=ROUND_HALF_UP)
            signature = str(target)
            if signature in measured_targets:
                break
            self._set_job_status(job, "cooldown", "before_refinement")
            self._sleep_with_heartbeat(job, self.cadence.measurement_upload_cooldown_seconds, phase="before_refinement")
            self._set_job_status(job, "measuring", "refinement_measurement")
            measured_targets.add(signature)
            job["measurements"].append(self._measure_point(job, target))
            self._save_job(job)

        thresholds, result_status = self._detect_thresholds(job["measurements"], precision=precision)
        if stop_after_restore and result_status == "threshold_not_detected":
            result_status = "inconclusive"
        job["thresholds"] = thresholds
        job["result_status"] = result_status
        self._save_job(job)

    def _measure_point(self, job: dict[str, Any], target_discounted: Decimal) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        baseline = job["baseline"]
        discount = int(baseline["discount"])
        upload_price, expected_discounted = _price_for_discounted(target_discounted, discount)
        point_id = uuid4().hex[:12]
        measurement = {
            "point_id": point_id,
            "target_discounted_price": _decimal_to_float(target_discounted),
            "upload_price": upload_price,
            "expected_discounted_price": _decimal_to_float(expected_discounted),
            "actual_wb_discounted_price": None,
            "authenticated_buyer_price": None,
            "anonymous_buyer_price": None,
            "public_buyer_price": None,
            "authenticated_spp_proxy": None,
            "anonymous_spp_proxy": None,
            "spp_proxy": None,
            "additional_account_discount_rub": None,
            "additional_account_discount_pct": None,
            "payment_context": "unknown/mixed",
            "destination_context": {},
            "session_fingerprint": "",
            "delta_vs_previous_high_confidence": None,
            "confidence": "low",
            "uploadID": None,
            "status": "started",
            "note": "",
            "evidence": {},
        }
        self._append_audit(str(job["job_id"]), "measurement_started", measurement)
        session = self._safe_buyer_session_preflight()
        measurement["evidence"]["buyer_session_preflight"] = session
        if session.get("status") != "valid":
            measurement["status"] = "buyer_session_invalid"
            measurement["note"] = "buyer session is invalid; no seller price write was attempted"
            self._append_audit(str(job["job_id"]), "measurement_buyer_session_blocked", measurement)
            return measurement
        upload = self._upload_price_with_backoff(job, [{"nmID": nm_id, "price": upload_price}])
        if upload.get("status") == "rate_limited_stop":
            measurement["status"] = "rate_limited_stop"
            measurement["note"] = upload.get("note") or "WB Prices API repeated 429"
            self._append_audit(str(job["job_id"]), "measurement_rate_limited_stop", measurement)
            return measurement
        upload_id = _optional_int(upload.get("uploadID"))
        measurement["uploadID"] = upload_id
        if upload_id is None:
            measurement["status"] = "upload_missing_id"
            measurement["note"] = "WB upload task did not return uploadID"
            return measurement
        upload_status = self._wait_upload_final(job, upload_id)
        measurement["evidence"]["upload_status"] = upload_status
        if upload_status.get("status") != "success":
            measurement["status"] = "upload_not_success"
            measurement["note"] = str(upload_status.get("status") or "unknown upload status")
            return measurement

        readback = self._wait_discounted_readback(job, expected_discounted=expected_discounted)
        measurement["evidence"]["readback"] = readback
        actual_discounted = _number_or_none(readback.get("discountedPrice"))
        measurement["actual_wb_discounted_price"] = actual_discounted
        if actual_discounted is None or not _money_close(_parse_money(actual_discounted, "discountedPrice"), expected_discounted, Decimal("1.00")):
            measurement["status"] = "readback_mismatch"
            measurement["note"] = "WB readback did not match expected discounted price"
            return measurement

        quarantine = self._check_quarantine(job)
        measurement["evidence"]["quarantine"] = quarantine
        if quarantine.get("is_quarantined"):
            measurement["status"] = "quarantine_detected"
            measurement["note"] = "nmID is in WB price quarantine"
            return measurement

        buyer_proof = self._poll_buyer_pair_stable(job)
        measurement["evidence"]["buyer_price_proof"] = buyer_proof
        authenticated_price = _number_or_none(buyer_proof.get("authenticated_buyer_price"))
        anonymous_price = _number_or_none(buyer_proof.get("anonymous_buyer_price"))
        measurement["authenticated_buyer_price"] = authenticated_price
        measurement["anonymous_buyer_price"] = anonymous_price
        measurement["public_buyer_price"] = anonymous_price
        measurement["payment_context"] = str(buyer_proof.get("payment_context") or "unknown/mixed")
        measurement["destination_context"] = dict(buyer_proof.get("destination_context") or {})
        measurement["session_fingerprint"] = str(buyer_proof.get("session_fingerprint") or "")
        for key in ("normal_price", "wallet_price", "card_price", "club_price"):
            measurement[key] = _number_or_none(buyer_proof.get(key))
        if authenticated_price is None or anonymous_price is None or buyer_proof.get("stable") is not True:
            measurement["status"] = str(buyer_proof.get("status") or "authenticated_unstable")
            measurement["note"] = "authenticated buyer price did not reach stable proof"
            return measurement

        authenticated_spp = _spp_proxy(
            _parse_money(actual_discounted, "actual_discounted"),
            _parse_money(authenticated_price, "authenticated_buyer_price"),
        )
        anonymous_spp = _spp_proxy(
            _parse_money(actual_discounted, "actual_discounted"),
            _parse_money(anonymous_price, "anonymous_buyer_price"),
        )
        measurement["authenticated_spp_proxy"] = _decimal_to_float(authenticated_spp)
        measurement["anonymous_spp_proxy"] = _decimal_to_float(anonymous_spp)
        measurement["spp_proxy"] = _decimal_to_float(authenticated_spp)
        additional_rub, additional_pct = _account_discount(authenticated_price, anonymous_price)
        measurement["additional_account_discount_rub"] = additional_rub
        measurement["additional_account_discount_pct"] = additional_pct
        previous = _previous_high_confidence_point(job.get("measurements", []))
        if previous is not None:
            prev_spp = _number_or_none(previous.get("spp_proxy"))
            if prev_spp is not None:
                delta = abs(authenticated_spp - _parse_ratio(prev_spp, "prev_spp"))
                measurement["delta_vs_previous_high_confidence"] = _decimal_to_float(delta)
            if _looks_like_stale_public_price(measurement, previous, precision=_parse_money(job["input"]["precision_rub"], "precision")):
                measurement["status"] = "stale_public_price"
                measurement["confidence"] = "low"
                measurement["note"] = "public buyer price stayed identical after material seller discounted price change"
                self._append_audit(str(job["job_id"]), "measurement_finished", measurement)
                return measurement
        measurement["status"] = "ok"
        measurement["confidence"] = "high"
        measurement["note"] = "upload/readback/authenticated+anonymous/quarantine proof complete"
        self._append_audit(str(job["job_id"]), "measurement_finished", measurement)
        return measurement

    def _upload_price_with_backoff(self, job: Mapping[str, Any], goods: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        attempts = 0
        while attempts < 3:
            attempts += 1
            try:
                payload = self.prices_source.upload_task(goods)
                self._append_audit(str(job["job_id"]), "wb_upload_task", {"request": {"data": list(goods)}, "response": payload})
                data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
                return {
                    "status": "created",
                    "uploadID": _optional_int(data.get("id") or data.get("uploadID") or data.get("upload_id")),
                    "alreadyExists": bool(data.get("alreadyExists")),
                    "wb_response": payload,
                }
            except WbPricesApiError as exc:
                self._append_audit(str(job["job_id"]), "wb_upload_task_error", exc.to_dict())
                if exc.http_status != 429:
                    raise
                if not self._handle_429_backoff(job, exc, phase="upload_task", attempt=attempts):
                    return {"status": "rate_limited_stop", "note": "WB Prices API repeated 429 during upload"}
        return {"status": "rate_limited_stop", "note": "WB Prices API repeated 429 during upload"}

    def _wait_upload_final(self, job: Mapping[str, Any], upload_id: int) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for _attempt in range(self.cadence.upload_status_max_polls):
            payload = self.prices_source.fetch_upload_status(upload_id)
            self._append_audit(str(job["job_id"]), "wb_upload_status", {"uploadID": upload_id, "response": payload})
            data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
            status_code = _optional_int(data.get("status"))
            status = map_upload_status(status_code)
            last = {"uploadID": upload_id, "status_code": status_code, "status": status, "wb_response": payload}
            if status in {"success", "partial_error", "all_error", "canceled"}:
                return last
            self._sleep_with_heartbeat(job, self.cadence.upload_status_poll_seconds, phase="upload_status_poll")
        last["status"] = last.get("status") or "timeout"
        return last

    def _wait_discounted_readback(self, job: Mapping[str, Any], *, expected_discounted: Decimal) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        last: dict[str, Any] = {}
        for _attempt in range(self.cadence.readback_max_polls):
            good = self._fetch_current_good(nm_id, job_id=str(job["job_id"]), audit_event="wb_prices_readback")
            last = good
            actual = _number_or_none(good.get("discountedPrice"))
            if actual is not None and _money_close(_parse_money(actual, "discountedPrice"), expected_discounted, Decimal("1.00")):
                return good
            self._sleep_with_heartbeat(job, self.cadence.readback_poll_seconds, phase="readback_poll")
        return last

    def _poll_public_stable(self, job: Mapping[str, Any]) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        reads: list[dict[str, Any]] = []
        self._sleep_with_heartbeat(job, self.cadence.first_public_poll_delay_seconds, phase="public_poll_initial")
        for attempt in range(3):
            payload = dict(self.public_source.fetch_public_buyer_price(nm_id))
            reads.append(payload)
            self._append_audit(str(job["job_id"]), "public_buyer_price_read", payload)
            if _public_status_is_429(payload):
                return {
                    "status": "public_429",
                    "stable": False,
                    "reads": reads,
                    "public_buyer_price": None,
                    "note": "public buyer price endpoint returned 429",
                }
            stable_price = _stable_public_price(reads)
            if stable_price is not None:
                return {
                    "status": "ok",
                    "stable": True,
                    "reads": reads,
                    "public_buyer_price": stable_price,
                    "proof": "3_identical_public_reads" if len(reads) >= 3 else "2_identical_public_reads_extended_gap",
                }
            if attempt == 0:
                self._sleep_with_heartbeat(job, self.cadence.extended_public_poll_gap_seconds, phase="public_poll_extended")
            else:
                self._sleep_with_heartbeat(job, self.cadence.public_poll_gap_seconds, phase="public_poll_gap")
        return {"status": "public_unstable", "stable": False, "reads": reads, "public_buyer_price": None}

    def _poll_buyer_pair_stable(self, job: Mapping[str, Any]) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        reads: list[dict[str, Any]] = []
        self._sleep_with_heartbeat(job, self.cadence.first_public_poll_delay_seconds, phase="buyer_poll_initial")
        for attempt in range(3):
            authenticated = dict(self.buyer_source.fetch_authenticated_buyer_price(nm_id))
            self._append_audit(str(job["job_id"]), "authenticated_buyer_price_read", authenticated)
            auth_status = str(authenticated.get("status") or "")
            if auth_status != "ok":
                lost = auth_status.startswith("session_") or auth_status in {
                    "login_redirect",
                    "security_challenge",
                    "wrong_account",
                }
                return {
                    "status": "buyer_session_lost" if lost else "authenticated_unstable",
                    "stable": False,
                    "reads": reads,
                    "authenticated_buyer_price": None,
                    "anonymous_buyer_price": None,
                    "session_status": auth_status,
                    "reason": str(authenticated.get("reason") or "authenticated_price_read_failed"),
                }
            anonymous = dict(self.public_source.fetch_public_buyer_price(nm_id))
            self._append_audit(str(job["job_id"]), "anonymous_buyer_price_read", anonymous)
            if _public_status_is_429(anonymous):
                return {
                    "status": "public_429",
                    "stable": False,
                    "reads": reads,
                    "authenticated_buyer_price": None,
                    "anonymous_buyer_price": None,
                    "reason": "anonymous control endpoint returned 429",
                }
            pair = {"authenticated": authenticated, "anonymous": anonymous}
            reads.append(pair)
            if not _buyer_contexts_compatible(authenticated, anonymous):
                return {
                    "status": "buyer_destination_mismatch",
                    "stable": False,
                    "reads": reads,
                    "authenticated_buyer_price": None,
                    "anonymous_buyer_price": None,
                    "destination_context": {
                        "authenticated": dict(authenticated.get("destination_context") or {}),
                        "anonymous": dict(anonymous.get("destination_context") or {}),
                    },
                }
            stable = _stable_buyer_pair(reads)
            if stable is not None:
                auth_price, anonymous_price = stable
                return {
                    "status": "ok",
                    "stable": True,
                    "reads": reads,
                    "authenticated_buyer_price": auth_price,
                    "anonymous_buyer_price": anonymous_price,
                    "normal_price": _number_or_none(authenticated.get("normal_price")),
                    "wallet_price": _number_or_none(authenticated.get("wallet_price")),
                    "card_price": _number_or_none(authenticated.get("card_price")),
                    "club_price": _number_or_none(authenticated.get("club_price")),
                    "payment_context": str(authenticated.get("payment_context") or "unknown/mixed"),
                    "destination_context": dict(authenticated.get("destination_context") or {}),
                    "session_fingerprint": str(authenticated.get("session_fingerprint") or ""),
                    "source_method": str(authenticated.get("source_method") or ""),
                    "source_endpoint": str(authenticated.get("source_endpoint") or ""),
                    "proof": "2_identical_authenticated_and_anonymous_reads",
                }
            self._sleep_with_heartbeat(
                job,
                self.cadence.extended_public_poll_gap_seconds if attempt == 0 else self.cadence.public_poll_gap_seconds,
                phase="buyer_poll_gap",
            )
        anonymous_missing = any(
            _number_or_none(
                (row.get("anonymous") if isinstance(row.get("anonymous"), Mapping) else {}).get("public_buyer_price")
            )
            is None
            for row in reads
        )
        return {
            "status": "public_unstable" if anonymous_missing else "authenticated_unstable",
            "stable": False,
            "reads": reads,
            "authenticated_buyer_price": None,
            "anonymous_buyer_price": None,
        }

    def _handle_429_backoff(
        self,
        job: Mapping[str, Any],
        exc: WbPricesApiError,
        *,
        phase: str,
        attempt: int,
    ) -> bool:
        cooldown = int(exc.retry_after_seconds or self.cadence.rate_limit_min_cooldown_seconds)
        cooldown = max(cooldown, self.cadence.rate_limit_min_cooldown_seconds)
        if attempt > 1:
            cooldown *= 2
        self._append_audit(
            str(job["job_id"]),
            "wb_prices_429_backoff",
            {"phase": phase, "attempt": attempt, "cooldown_seconds": cooldown, "error": exc.to_dict()},
        )
        current = self._load_job(str(job["job_id"])) or dict(job)
        self._set_job_status(current, "cooldown", f"429_backoff_{phase}")
        self._sleep_with_heartbeat(job, cooldown, phase=f"429_backoff_{phase}")
        try:
            self._fetch_current_good(int(job["nmID"]), job_id=str(job["job_id"]), audit_event="wb_prices_429_probe")
            return True
        except WbPricesApiError as probe_exc:
            self._append_audit(str(job["job_id"]), "wb_prices_429_probe_error", probe_exc.to_dict())
            return probe_exc.http_status != 429

    def _check_quarantine(self, job: Mapping[str, Any]) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        payload = self.prices_source.fetch_quarantine_goods(limit=1000, offset=0)
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        raw_rows = data.get("quarantineGoods") if isinstance(data.get("quarantineGoods"), list) else []
        rows = [normalize_quarantine_good(row) for row in raw_rows if isinstance(row, Mapping)]
        is_quarantined = any(_optional_int(row.get("nmID")) == nm_id for row in rows)
        result = {"is_quarantined": is_quarantined, "rows": rows, "wb_response": payload}
        self._append_audit(str(job["job_id"]), "wb_quarantine_check", result)
        return result

    def _restore_baseline(self, job: dict[str, Any], *, reason: str) -> bool:
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else None
        if not baseline:
            return False
        nm_id = int(job["nmID"])
        self._set_job_status(job, "restoring", reason)
        restore_state = job.setdefault("restore", {"required": True, "restored": False, "proof": None, "steps": []})
        try:
            preflight_proof = self._capture_restore_proof(job, event_prefix="restore_preflight")
        except Exception as exc:
            restore_state["restored"] = False
            restore_state["proof"] = {"error": _safe_text(exc, 1000), "proof_status": "readback_failed"}
            job["manual_restore_required"] = True
            job["result_status"] = "manual_restore_required"
            self._append_audit(str(job["job_id"]), "restore_preflight_error", {"error": str(exc)})
            self._save_job(job)
            return False
        restore_state["proof"] = preflight_proof
        if _restore_proof_ok(preflight_proof):
            restore_state["restored"] = True
            job["manual_restore_required"] = False
            self._append_audit(str(job["job_id"]), "restore_already_confirmed", preflight_proof)
            self._save_job(job)
            return True
        tuple_is_restored = bool(
            preflight_proof.get("price_matches")
            and preflight_proof.get("discount_matches")
            and preflight_proof.get("discountedPrice_matches")
        )
        if tuple_is_restored or not preflight_proof.get("quarantine_absent"):
            restore_state["restored"] = False
            job["manual_restore_required"] = True
            job["result_status"] = "manual_restore_required"
            self._append_audit(
                str(job["job_id"]),
                "restore_preflight_blocked",
                {"tuple_is_restored": tuple_is_restored, "proof": preflight_proof},
            )
            self._save_job(job)
            return False

        steps = self._build_restore_steps(job)
        for step in steps:
            price = int(step["price"])
            discount = int(step["discount"])
            upload = self._upload_price_with_backoff(job, [{"nmID": nm_id, "price": price, "discount": discount}])
            step["upload"] = upload
            if upload.get("status") == "rate_limited_stop" or upload.get("uploadID") is None:
                step["status"] = "upload_failed"
                restore_state.setdefault("steps", []).append(step)
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False
            upload_status = self._wait_upload_final(job, int(upload["uploadID"]))
            step["upload_status"] = upload_status
            if upload_status.get("status") != "success":
                step["status"] = "upload_not_success"
                restore_state.setdefault("steps", []).append(step)
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False
            expected = _discounted_price(_parse_money(price, "price"), _parse_money(discount, "discount"))
            readback = self._wait_discounted_readback(job, expected_discounted=expected)
            step["readback"] = readback
            quarantine = self._check_quarantine(job)
            step["quarantine"] = quarantine
            step["status"] = "ok" if not quarantine.get("is_quarantined") else "quarantine_detected"
            restore_state.setdefault("steps", []).append(step)
            self._save_job(job)
            if quarantine.get("is_quarantined"):
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False

        proof = self._capture_restore_proof(job, event_prefix="restore_final")
        ok = _restore_proof_ok(proof)
        restore_state["proof"] = proof
        restore_state["restored"] = ok
        job["manual_restore_required"] = not ok
        if not ok:
            job["result_status"] = "manual_restore_required"
        self._append_audit(str(job["job_id"]), "restore_final_proof", proof)
        self._save_job(job)
        return ok

    def _capture_restore_proof(self, job: Mapping[str, Any], *, event_prefix: str) -> dict[str, Any]:
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        nm_id = int(job["nmID"])
        proof_good = self._fetch_current_good(
            nm_id,
            job_id=str(job["job_id"]),
            audit_event=f"wb_{event_prefix}_readback",
        )
        proof_quarantine = self._check_quarantine(job)
        buyer_session = self._safe_buyer_session_preflight()
        proof_authenticated = (
            dict(self.buyer_source.fetch_authenticated_buyer_price(nm_id))
            if buyer_session.get("status") == "valid"
            else {
                "status": f"session_{buyer_session.get('status') or 'invalid'}",
                "reason": buyer_session.get("reason"),
            }
        )
        try:
            proof_anonymous = dict(self.public_source.fetch_public_buyer_price(nm_id))
        except Exception:
            proof_anonymous = {"status": "probe_error", "public_buyer_price": None}
        self._append_audit(str(job["job_id"]), f"authenticated_{event_prefix}_read", proof_authenticated)
        self._append_audit(str(job["job_id"]), f"anonymous_{event_prefix}_read", proof_anonymous)
        authenticated_price = _number_or_none(proof_authenticated.get("authenticated_buyer_price"))
        anonymous_price = _number_or_none(proof_anonymous.get("public_buyer_price"))
        authenticated_status = str(proof_authenticated.get("status") or "")
        anonymous_status = str(proof_anonymous.get("status") or "")
        proof: dict[str, Any] = {
            "captured_at": self.timestamp_factory(),
            "price_matches": _optional_int(proof_good.get("price")) == _optional_int(baseline.get("price")),
            "discount_matches": _optional_int(proof_good.get("discount")) == _optional_int(baseline.get("discount")),
            "discountedPrice_matches": _money_exact(
                proof_good.get("discountedPrice"),
                baseline.get("discountedPrice"),
            ),
            "quarantine_absent": not proof_quarantine.get("is_quarantined"),
            "buyer_evidence_captured": authenticated_price is not None and authenticated_status == "ok",
            "anonymous_evidence_captured": anonymous_price is not None and anonymous_status not in {"429", "timeout", "stale", "missing"},
            "authenticated_buyer_price": authenticated_price,
            "anonymous_buyer_price": anonymous_price,
            "public_buyer_price": anonymous_price,
            "authenticated_status": authenticated_status,
            "anonymous_status": anonymous_status,
            "buyer_session": buyer_session,
            "authenticated_spp_proxy": None,
            "anonymous_spp_proxy": None,
            "spp_proxy": None,
            "wb_readback": proof_good,
            "quarantine": proof_quarantine,
        }
        if authenticated_price is not None:
            proof["authenticated_spp_proxy"] = _decimal_to_float(
                _spp_proxy(
                    _parse_money(baseline.get("discountedPrice"), "baseline_discountedPrice"),
                    _parse_money(authenticated_price, "authenticated_buyer_price"),
                )
            )
            proof["spp_proxy"] = proof["authenticated_spp_proxy"]
        if anonymous_price is not None:
            proof["anonymous_spp_proxy"] = _decimal_to_float(
                _spp_proxy(
                    _parse_money(baseline.get("discountedPrice"), "baseline_discountedPrice"),
                    _parse_money(anonymous_price, "anonymous_buyer_price"),
                )
            )
        proof["proof_status"] = "confirmed" if _restore_proof_ok(proof) else "not_confirmed"
        return proof

    def _build_restore_steps(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        baseline = job["baseline"]
        baseline_discounted = _parse_money(baseline["discountedPrice"], "baseline_discountedPrice")
        baseline_discount = int(baseline["discount"])
        baseline_price = int(baseline["price"])
        current_discounted = self._last_known_discounted(job)
        steps: list[dict[str, Any]] = []
        if current_discounted is not None and current_discounted > baseline_discounted * Decimal("1.25"):
            cursor = current_discounted
            while cursor > baseline_discounted * Decimal("1.25"):
                cursor = max(baseline_discounted, (cursor * Decimal("0.80")).quantize(MONEY, rounding=ROUND_HALF_UP))
                if cursor <= baseline_discounted:
                    break
                price, expected = _price_for_discounted(cursor, baseline_discount)
                if price == baseline_price:
                    break
                steps.append(
                    {
                        "kind": "bridge",
                        "target_discounted_price": _decimal_to_float(cursor),
                        "price": price,
                        "discount": baseline_discount,
                        "expected_discounted_price": _decimal_to_float(expected),
                    }
                )
        steps.append(
            {
                "kind": "baseline",
                "target_discounted_price": _decimal_to_float(baseline_discounted),
                "price": baseline_price,
                "discount": baseline_discount,
                "expected_discounted_price": _decimal_to_float(baseline_discounted),
            }
        )
        return steps

    def _last_known_discounted(self, job: Mapping[str, Any]) -> Decimal | None:
        for row in reversed(job.get("measurements", []) if isinstance(job.get("measurements"), list) else []):
            value = _number_or_none(row.get("actual_wb_discounted_price"))
            if value is not None:
                return _parse_money(value, "actual_wb_discounted_price")
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        value = _number_or_none(baseline.get("discountedPrice"))
        return _parse_money(value, "baseline_discountedPrice") if value is not None else None

    def _detect_thresholds(self, measurements: Sequence[Mapping[str, Any]], *, precision: Decimal) -> tuple[list[dict[str, Any]], str]:
        high = [
            row
            for row in measurements
            if row.get("confidence") == "high"
            and _number_or_none(row.get("actual_wb_discounted_price")) is not None
            and _number_or_none(row.get("spp_proxy")) is not None
        ]
        high.sort(key=lambda row: float(row["actual_wb_discounted_price"]))
        thresholds: list[dict[str, Any]] = []
        for left, right in zip(high, high[1:]):
            left_spp = _parse_ratio(left["spp_proxy"], "spp_left")
            right_spp = _parse_ratio(right["spp_proxy"], "spp_right")
            delta = abs(right_spp - left_spp)
            lower_price = _parse_money(left["actual_wb_discounted_price"], "lower_actual_discounted_price")
            upper_price = _parse_money(right["actual_wb_discounted_price"], "upper_actual_discounted_price")
            bracket_width = abs(upper_price - lower_price)
            if delta < Decimal("0.005"):
                confidence = "noise"
            elif delta >= Decimal("0.03"):
                confidence = "strong"
            elif delta >= Decimal("0.015"):
                confidence = "material"
            else:
                confidence = "suspect"
            if confidence in {"material", "strong", "suspect"}:
                thresholds.append(
                    {
                        "lower_actual_discounted_price": _decimal_to_float(lower_price),
                        "upper_actual_discounted_price": _decimal_to_float(upper_price),
                        "spp_left": _decimal_to_float(left_spp),
                        "spp_right": _decimal_to_float(right_spp),
                        "delta": _decimal_to_float(delta),
                        "bracket_width": _decimal_to_float(bracket_width),
                        "confidence": confidence,
                    }
                )
        material = [row for row in thresholds if row["confidence"] in {"material", "strong"}]
        if material:
            return thresholds, "threshold_detected"
        if len(high) >= 3 and len(measurements) >= 3:
            return thresholds, "threshold_not_detected"
        return thresholds, "inconclusive"

    def _capture_baseline(self, *, nm_id: int, strict: bool) -> dict[str, Any]:
        buyer_session = self._safe_buyer_session_preflight()
        good = self._fetch_current_good(nm_id, job_id="", audit_event="baseline_prices_read")
        quarantine_payload = self.prices_source.fetch_quarantine_goods(limit=1000, offset=0)
        quarantine_rows = [
            normalize_quarantine_good(row)
            for row in (
                quarantine_payload.get("data", {}).get("quarantineGoods", [])
                if isinstance(quarantine_payload.get("data"), Mapping)
                else []
            )
            if isinstance(row, Mapping)
        ]
        quarantine_match = [row for row in quarantine_rows if _optional_int(row.get("nmID")) == nm_id]
        authenticated = (
            dict(self.buyer_source.fetch_authenticated_buyer_price(nm_id))
            if buyer_session.get("status") == "valid"
            else {
                "status": f"session_{buyer_session.get('status') or 'invalid'}",
                "authenticated_buyer_price": None,
                "reason": buyer_session.get("reason"),
            }
        )
        public = dict(self.public_source.fetch_public_buyer_price(nm_id))
        discounted = _number_or_none(good.get("discountedPrice"))
        authenticated_price = _number_or_none(authenticated.get("authenticated_buyer_price"))
        anonymous_price = _number_or_none(public.get("public_buyer_price"))
        contexts_compatible = _buyer_contexts_compatible(authenticated, public)
        authenticated_spp = (
            _decimal_to_float(_spp_proxy(_parse_money(discounted, "discountedPrice"), _parse_money(authenticated_price, "authenticated_buyer_price")))
            if discounted is not None and authenticated_price is not None
            else None
        )
        anonymous_spp = (
            _decimal_to_float(_spp_proxy(_parse_money(discounted, "discountedPrice"), _parse_money(anonymous_price, "anonymous_buyer_price")))
            if discounted is not None and anonymous_price is not None
            else None
        )
        additional_rub, additional_pct = _account_discount(authenticated_price, anonymous_price) if contexts_compatible else (None, None)
        enrichment = self._load_nomenclature_enrichment().get(nm_id, {})
        baseline = {
            "nmID": nm_id,
            "title": _display_title(enrichment),
            "ourSku": str(enrichment.get("our_sku") or ""),
            "vendorCode": str(good.get("vendorCode") or ""),
            "price": _optional_int(good.get("price")),
            "discount": _optional_int(good.get("discount")),
            "discountedPrice": discounted,
            "authenticatedBuyerPrice": authenticated_price,
            "anonymousBuyerPrice": anonymous_price,
            "publicBuyerPrice": anonymous_price,
            "authenticatedSppProxy": authenticated_spp,
            "anonymousSppProxy": anonymous_spp,
            "sppProxy": authenticated_spp,
            "sppProxyLabel": _format_percent_label(authenticated_spp),
            "additionalAccountDiscountRub": additional_rub,
            "additionalAccountDiscountPct": additional_pct,
            "normalPrice": _number_or_none(authenticated.get("normal_price")),
            "walletPrice": _number_or_none(authenticated.get("wallet_price")),
            "cardPrice": _number_or_none(authenticated.get("card_price")),
            "clubPrice": _number_or_none(authenticated.get("club_price")),
            "paymentContext": str(authenticated.get("payment_context") or "unknown/mixed"),
            "destinationContext": dict(authenticated.get("destination_context") or {}),
            "destinationCompatible": contexts_compatible,
            "sessionFingerprint": str(authenticated.get("session_fingerprint") or buyer_session.get("session_fingerprint") or ""),
            "buyerSession": buyer_session,
            "quarantine": {
                "is_quarantined": bool(quarantine_match),
                "rows": quarantine_match,
            },
            "editableSizePrice": bool(good.get("editableSizePrice")),
            "editableSizePriceLabel": "размерные цены" if bool(good.get("editableSizePrice")) else "обычная цена",
            "currencyIsoCode4217": str(good.get("currencyIsoCode4217") or "RUB"),
            "public_read": public,
            "authenticated_read": authenticated,
            "captured_at": self.timestamp_factory(),
        }
        blockers: list[str] = []
        if baseline["editableSizePrice"]:
            blockers.append("editableSizePrice=true")
        if baseline["quarantine"]["is_quarantined"]:
            blockers.append("price_quarantine_present")
        if baseline["price"] is None or baseline["discount"] is None or baseline["discountedPrice"] is None:
            blockers.append("wb_price_baseline_incomplete")
        if not enrichment:
            blockers.append("active_nomenclature_missing")
        public_status = str(public.get("status") or "").strip().lower()
        if buyer_session.get("status") != "valid":
            blockers.append("buyer_session_invalid")
        if baseline["authenticatedBuyerPrice"] is None or baseline["authenticatedSppProxy"] is None:
            blockers.append("authenticated_spp_baseline_incomplete")
        if baseline["anonymousBuyerPrice"] is None or baseline["anonymousSppProxy"] is None:
            blockers.append("anonymous_spp_baseline_incomplete")
            blockers.append("public_spp_baseline_incomplete")
        if not contexts_compatible:
            blockers.append("buyer_destination_context_mismatch")
        elif public_status in {"429", "timeout", "stale", "missing"}:
            blockers.append(f"public_spp_baseline_{public_status}")
        baseline["can_start"] = not blockers
        baseline["blockers"] = blockers
        if strict and blockers:
            raise WbSppTesterError(
                "SPP test baseline is not safe to start",
                http_status=422,
                payload={"baseline": baseline, "blockers": blockers},
            )
        return baseline

    def _safe_buyer_session_preflight(self) -> dict[str, Any]:
        try:
            payload = dict(self.buyer_source.check_session())
        except Exception:
            payload = {
                "status": "probe_error",
                "valid": False,
                "reason": "buyer_session_probe_failed",
                "checked_at": self.timestamp_factory(),
            }
        payload.setdefault("status", "probe_error")
        payload.setdefault("valid", payload.get("status") == "valid")
        return payload

    def _require_buyer_session(self) -> dict[str, Any]:
        session = self._safe_buyer_session_preflight()
        if session.get("status") != "valid" or session.get("valid") is not True:
            raise WbSppTesterError(
                "Покупательская сессия недействительна. Установить сессию.",
                http_status=422,
                payload={"reason": "buyer_session_invalid", "buyer_session": session, "action": "Установить сессию"},
            )
        return session

    def _fetch_current_good(self, nm_id: int, *, job_id: str, audit_event: str) -> dict[str, Any]:
        payload = self.prices_source.fetch_goods_by_nm_ids([int(nm_id)])
        if job_id:
            self._append_audit(job_id, audit_event, {"nmID": nm_id, "response": payload})
        goods = normalize_goods_payload(payload)
        good = next((item for item in goods if item.nm_id == int(nm_id)), None)
        if good is None:
            raise WbSppTesterError(f"WB price good not found for nmID={nm_id}", http_status=404)
        return good.to_dict()

    def _parse_plan_input(self, payload: Mapping[str, Any]) -> tuple[int, Decimal, Decimal, Decimal, int]:
        nm_id = _as_positive_int(payload.get("nmID") or payload.get("nm_id"), "nmID")
        range_min = _parse_money(payload.get("range_min_discounted") or payload.get("min_discounted"), "range_min_discounted")
        range_max = _parse_money(payload.get("range_max_discounted") or payload.get("max_discounted"), "range_max_discounted")
        if range_min <= 0 or range_max <= 0:
            raise WbSppTesterError("discounted price range values must be > 0", http_status=422)
        if range_min >= range_max:
            raise WbSppTesterError("range_min_discounted must be lower than range_max_discounted", http_status=422)
        precision = _parse_money(payload.get("precision_rub") or payload.get("precision") or SPP_TEST_DEFAULT_PRECISION_RUB, "precision_rub")
        if precision <= 0:
            raise WbSppTesterError("precision_rub must be > 0", http_status=422)
        max_measurements = _optional_int(payload.get("max_measurements")) or SPP_TEST_DEFAULT_MAX_MEASUREMENTS
        if max_measurements < SPP_TEST_MAX_MEASUREMENTS_MIN or max_measurements > SPP_TEST_MAX_MEASUREMENTS_MAX:
            raise WbSppTesterError(
                f"max_measurements must be between {SPP_TEST_MAX_MEASUREMENTS_MIN} and {SPP_TEST_MAX_MEASUREMENTS_MAX}",
                http_status=422,
            )
        mode = str(payload.get("mode") or SPP_TEST_MODE_SAFE_SLOW)
        if mode != SPP_TEST_MODE_SAFE_SLOW:
            raise WbSppTesterError("only safe_slow mode is supported in MVP", http_status=422)
        return nm_id, range_min, range_max, precision, int(max_measurements)

    def _load_nomenclature_enrichment(self) -> dict[int, Mapping[str, Any]]:
        try:
            items = self.runtime.list_nomenclature_items(active_only=True)
        except Exception:
            return {}
        result: dict[int, Mapping[str, Any]] = {}
        for item in items:
            nm_id = _optional_int(item.get("nm_id"))
            if nm_id is not None:
                result[nm_id] = item
        return result

    def _blocking_current_job(
        self,
        *,
        reconcile: bool = True,
        caller_holds_execution_lock: bool = False,
    ) -> dict[str, Any] | None:
        job = self._reconcile_current_job(caller_holds_execution_lock=caller_holds_execution_lock) if reconcile else self._load_current_job_payload()
        if not job:
            return None
        status = str(job.get("status") or "")
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        if status in SPP_TEST_ACTIVE_STATUSES or status == "manual_restore_required":
            return self._job_summary(job)
        if status == "failed" and not restore.get("restored"):
            return self._job_summary(job)
        return None

    def _current_job_summary(
        self,
        *,
        reconcile: bool = True,
        caller_holds_execution_lock: bool = False,
    ) -> dict[str, Any] | None:
        job = (
            self._reconcile_current_job(caller_holds_execution_lock=caller_holds_execution_lock)
            if reconcile
            else self._load_current_job_payload()
        )
        return self._job_summary(job) if job else None

    def _reconcile_current_job(self, *, caller_holds_execution_lock: bool = False) -> dict[str, Any] | None:
        job = self._load_current_job_payload()
        if not job:
            return None
        status = str(job.get("status") or "")
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        needs_restore_confirmation = status in SPP_TEST_ACTIVE_STATUSES or status == "manual_restore_required" or (
            status == "failed" and not restore.get("restored")
        )
        if not needs_restore_confirmation:
            return job
        if not caller_holds_execution_lock and self._execution_lock_is_held():
            lifecycle = job.get("lifecycle_diagnostics") if isinstance(job.get("lifecycle_diagnostics"), Mapping) else {}
            if lifecycle.get("classification") != "live":
                mutable = dict(job)
                self._set_lifecycle(mutable, classification="live", phase=status)
                self._save_job(mutable)
                return mutable
            return job
        try:
            proof = self._capture_restore_proof(job, event_prefix="orphan_reconcile")
        except Exception as exc:
            mutable = dict(job)
            mutable["status"] = "manual_restore_required"
            mutable["result_status"] = "manual_restore_required"
            mutable["manual_restore_required"] = True
            mutable["updated_at"] = self.timestamp_factory()
            diagnostics = {
                "classification": "stale_orphan_unrestored",
                "phase": status,
                "reconciled_at": self.timestamp_factory(),
                "restore_readback_error": _safe_text(exc, 1000),
            }
            self._set_lifecycle(mutable, **diagnostics)
            self._append_timeline(mutable, "manual_restore_required", "orphan_restore_readback_failed")
            self._save_job(mutable)
            self._write_current_job(mutable)
            self._append_audit(str(mutable["job_id"]), "orphan_reconcile_failed", diagnostics)
            return mutable

        mutable = dict(job)
        restore_state = dict(restore)
        restore_state["proof"] = proof
        restored = _restore_proof_ok(proof)
        restore_state["restored"] = restored
        mutable["restore"] = restore_state
        mutable["updated_at"] = self.timestamp_factory()
        if restored:
            mutable["status"] = "interrupted_restored"
            mutable["result_status"] = str(mutable.get("result_status") or "inconclusive")
            mutable["manual_restore_required"] = False
            mutable["finished_at"] = self.timestamp_factory()
            self._set_lifecycle(
                mutable,
                classification="stale_orphan_restored_confirmed",
                phase="interrupted_restored",
                reconciled_at=self.timestamp_factory(),
            )
            self._append_timeline(mutable, "interrupted_restored", "fresh_live_baseline_readback_confirmed")
            audit_event = "orphan_reconcile_restored"
        else:
            mutable["status"] = "manual_restore_required"
            mutable["result_status"] = "manual_restore_required"
            mutable["manual_restore_required"] = True
            self._set_lifecycle(
                mutable,
                classification="stale_orphan_unrestored",
                phase="manual_restore_required",
                reconciled_at=self.timestamp_factory(),
                restore_proof=proof,
            )
            self._append_timeline(mutable, "manual_restore_required", "fresh_live_baseline_readback_not_confirmed")
            audit_event = "orphan_reconcile_unrestored"
        self._save_job(mutable)
        self._write_current_job(mutable)
        self._append_audit(str(mutable["job_id"]), audit_event, proof)
        return mutable

    def _job_summary(self, job: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(job.get("job_id") or ""),
            "status": str(job.get("status") or ""),
            "result_status": str(job.get("result_status") or ""),
            "nmID": _optional_int(job.get("nmID")),
            "trigger_source": str(job.get("trigger_source") or "") or None,
            "created_at": str(job.get("created_at") or ""),
            "updated_at": str(job.get("updated_at") or ""),
            "manual_restore_required": bool(job.get("manual_restore_required")),
            "restore_restored": bool((job.get("restore") or {}).get("restored")) if isinstance(job.get("restore"), Mapping) else False,
            "lifecycle_classification": str((job.get("lifecycle_diagnostics") or {}).get("classification") or "")
            if isinstance(job.get("lifecycle_diagnostics"), Mapping)
            else "",
        }

    def _job_public_payload(self, job: Mapping[str, Any] | None, *, details: bool = False) -> dict[str, Any] | None:
        if not job:
            return None
        payload = {
            key: job.get(key)
            for key in (
                "job_id",
                "created_at",
                "updated_at",
                "finished_at",
                "actor",
                "trigger_source",
                "schedule_id",
                "status",
                "result_status",
                "nmID",
                "input",
                "baseline",
                "plan",
                "measurements",
                "thresholds",
                "timeline",
                "restore",
                "lifecycle_diagnostics",
                "manual_restore_required",
                "warnings",
                "error",
            )
        }
        return _sanitize_public_payload(payload, details=details)

    def _load_history_jobs(self) -> list[dict[str, Any]]:
        if not self._jobs_dir.exists():
            return []
        jobs: list[dict[str, Any]] = []
        for path in self._jobs_dir.iterdir():
            if not path.is_file() or path.suffix != ".json" or not JOB_ID_RE.fullmatch(path.stem):
                continue
            job = self._load_job(path.stem)
            if job:
                jobs.append(job)
            if len(jobs) >= 10000:
                break
        return jobs

    def _history_summary(self, job: Mapping[str, Any]) -> dict[str, Any]:
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        created_at = str(job.get("created_at") or "")
        finished_at = str(job.get("finished_at") or job.get("updated_at") or "")
        return {
            "job_id": str(job.get("job_id") or ""),
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(created_at, finished_at),
            "trigger_source": str(job.get("trigger_source") or "") or None,
            "status": str(job.get("status") or ""),
            "result_status": str(job.get("result_status") or ""),
            "nmID": _optional_int(job.get("nmID")),
            "title": _safe_text(baseline.get("title"), 300),
            "ourSku": _safe_text(baseline.get("ourSku"), 160),
            "vendorCode": _safe_text(baseline.get("vendorCode"), 300),
            "manual_restore_required": bool(job.get("manual_restore_required")),
            "restore_restored": bool((job.get("restore") or {}).get("restored")) if isinstance(job.get("restore"), Mapping) else False,
        }

    def _default_schedule(self) -> dict[str, Any]:
        return {
            "id": SCHEDULE_ID,
            "enabled": False,
            "cadence": SCHEDULE_CADENCE,
            "nmID": None,
            "range_min_discounted": None,
            "range_max_discounted": None,
            "precision_rub": SPP_TEST_DEFAULT_PRECISION_RUB,
            "max_measurements": SPP_TEST_DEFAULT_MAX_MEASUREMENTS,
            "local_time_hhmm": "12:00",
            "timezone": SPP_TEST_SCHEDULE_TIMEZONE,
            "timezone_label": SPP_TEST_SCHEDULE_TIMEZONE_LABEL,
            "future_live_price_changes_confirmed": False,
            "created_at": "",
            "updated_at": "",
            "enabled_since_at": "",
            "next_run_at": "",
            "last_claimed_business_date": "",
            "last_due_at": "",
            "last_scheduler_decision_at": "",
            "last_automatic_run_at": "",
            "last_automatic_status": "",
            "last_automatic_job_id": "",
            "last_automatic_result_status": "",
        }

    def _load_schedule_unlocked(self) -> dict[str, Any]:
        if not self._schedule_path.exists():
            return self._default_schedule()
        try:
            payload = json.loads(self._schedule_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WbSppTesterError("SPP schedule state is not readable", http_status=500) from exc
        if not isinstance(payload, Mapping):
            raise WbSppTesterError("SPP schedule state has invalid shape", http_status=500)
        return {**self._default_schedule(), **dict(payload), "id": SCHEDULE_ID, "cadence": SCHEDULE_CADENCE}

    def _normalize_schedule_for_save(
        self,
        raw: Mapping[str, Any],
        *,
        existing: Mapping[str, Any],
        now: datetime,
        actor: str,
    ) -> dict[str, Any]:
        enabled = _coerce_bool(raw.get("enabled"))
        timezone_name = str(raw.get("timezone") or existing.get("timezone") or SPP_TEST_SCHEDULE_TIMEZONE)
        if timezone_name != SPP_TEST_SCHEDULE_TIMEZONE:
            raise WbSppTesterError(f"timezone must be {SPP_TEST_SCHEDULE_TIMEZONE}", http_status=422)
        local_time = _normalize_hhmm(raw.get("local_time_hhmm") or raw.get("time") or existing.get("local_time_hhmm") or "12:00")
        consent = _coerce_bool(raw.get("future_live_price_changes_confirmed"))
        if enabled and not consent:
            raise WbSppTesterError(
                "future_live_price_changes_confirmed=true is required for an enabled schedule",
                http_status=422,
            )
        plan_source = {
            "nmID": raw.get("nmID") or raw.get("nm_id") or existing.get("nmID"),
            "range_min_discounted": raw.get("range_min_discounted") or existing.get("range_min_discounted"),
            "range_max_discounted": raw.get("range_max_discounted") or existing.get("range_max_discounted"),
            "precision_rub": raw.get("precision_rub") or existing.get("precision_rub") or SPP_TEST_DEFAULT_PRECISION_RUB,
            "max_measurements": raw.get("max_measurements") or existing.get("max_measurements") or SPP_TEST_DEFAULT_MAX_MEASUREMENTS,
            "mode": SPP_TEST_MODE_SAFE_SLOW,
        }
        parsed: tuple[int, Decimal, Decimal, Decimal, int] | None = None
        if enabled or plan_source["nmID"] not in {None, ""}:
            parsed = self._parse_plan_input(plan_source)
        was_enabled = bool(existing.get("enabled"))
        now_text = now.isoformat()
        schedule = {**self._default_schedule(), **dict(existing)}
        schedule.update(
            {
                "id": SCHEDULE_ID,
                "enabled": enabled,
                "cadence": SCHEDULE_CADENCE,
                "local_time_hhmm": local_time,
                "timezone": timezone_name,
                "timezone_label": SPP_TEST_SCHEDULE_TIMEZONE_LABEL,
                "future_live_price_changes_confirmed": consent if enabled else False,
                "created_at": str(existing.get("created_at") or now_text),
                "updated_at": now_text,
                "updated_by": _safe_text(actor, 160),
                "enabled_since_at": str(existing.get("enabled_since_at") or now_text) if enabled and was_enabled else (now_text if enabled else ""),
                "next_run_at": _next_daily_run_at(
                    now,
                    local_time_hhmm=local_time,
                    timezone_name=timezone_name,
                ).isoformat()
                if enabled
                else "",
            }
        )
        if parsed is not None:
            nm_id, range_min, range_max, precision, max_measurements = parsed
            schedule.update(
                {
                    "nmID": nm_id,
                    "range_min_discounted": _decimal_to_float(range_min),
                    "range_max_discounted": _decimal_to_float(range_max),
                    "precision_rub": _decimal_to_float(precision),
                    "max_measurements": max_measurements,
                }
            )
        return schedule

    def _write_schedule_unlocked(self, schedule: Mapping[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self._schedule_path, schedule)

    def _schedule_response(self, schedule: Mapping[str, Any]) -> dict[str, Any]:
        public = _sanitize_public_payload(dict(schedule), details=False)
        last_job_id = str(schedule.get("last_automatic_job_id") or "")
        last_job = self._load_job(last_job_id) if last_job_id else None
        return {
            "contract_name": SCHEDULE_CONTRACT_NAME,
            "generated_at": self.timestamp_factory(),
            "schedule": public,
            "last_automatic_job": self._job_summary(last_job) if last_job else None,
        }

    def _update_schedule_after_automatic_job(self, schedule: Mapping[str, Any], job: Mapping[str, Any]) -> dict[str, Any]:
        with self._schedule_file_lock():
            current = self._load_schedule_unlocked()
            current.update(
                {
                    "last_automatic_run_at": str(job.get("finished_at") or job.get("updated_at") or self.timestamp_factory()),
                    "last_automatic_status": str(job.get("status") or ""),
                    "last_automatic_job_id": str(job.get("job_id") or ""),
                    "last_automatic_result_status": str(job.get("result_status") or ""),
                    "updated_at": self.timestamp_factory(),
                }
            )
            self._write_schedule_unlocked(current)
            return current

    def _record_scheduled_skip(
        self,
        schedule: Mapping[str, Any],
        *,
        reason: str,
        due_at: datetime,
        diagnostics: Mapping[str, Any],
    ) -> dict[str, Any]:
        job_id = uuid4().hex
        now_text = self.timestamp_factory()
        baseline = diagnostics.get("baseline") if isinstance(diagnostics.get("baseline"), Mapping) else {}
        job = {
            "job_id": job_id,
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_job",
            "created_at": now_text,
            "updated_at": now_text,
            "finished_at": now_text,
            "actor": "systemd:spp_schedule",
            "trigger_source": "schedule",
            "schedule_id": SCHEDULE_ID,
            "status": "skipped",
            "result_status": reason,
            "nmID": _optional_int(schedule.get("nmID")),
            "input": {
                "range_min_discounted": schedule.get("range_min_discounted"),
                "range_max_discounted": schedule.get("range_max_discounted"),
                "precision_rub": schedule.get("precision_rub"),
                "max_measurements": schedule.get("max_measurements"),
                "mode": SPP_TEST_MODE_SAFE_SLOW,
                "restore_baseline": True,
            },
            "baseline": dict(baseline),
            "plan": {},
            "measurements": [],
            "thresholds": [],
            "timeline": [{"timestamp": now_text, "status": "skipped", "note": reason}],
            "restore": {"required": True, "restored": False, "not_started": True, "proof": None, "steps": []},
            "lifecycle_diagnostics": {
                "classification": "scheduled_skip",
                "phase": "skipped",
                "due_at": due_at.isoformat(),
                "reason": reason,
                "diagnostics": _sanitize_public_payload(dict(diagnostics), details=False),
            },
            "manual_restore_required": False,
            "warnings": [reason],
            "error": _safe_text(diagnostics.get("error"), 1000),
        }
        self._save_job(job)
        self._append_audit(job_id, "schedule_skip", {"reason": reason, "due_at": due_at.isoformat(), "diagnostics": diagnostics})
        return self._job_public_payload(job, details=True) or {}

    def _scheduler_tick_response(
        self,
        status: str,
        *,
        schedule: Mapping[str, Any],
        due_at: datetime | None = None,
        job: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "contract_name": SCHEDULER_TICK_CONTRACT_NAME,
            "checked_at": self.timestamp_factory(),
            "status": status,
            "due_at": due_at.isoformat() if due_at else "",
            "schedule": _sanitize_public_payload(dict(schedule), details=False),
            "job": self._job_public_payload(job, details=True) if job else None,
        }

    @contextmanager
    def _schedule_file_lock(self):
        self._state_dir.mkdir(parents=True, exist_ok=True)
        handle = self._schedule_lock_path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _acquire_execution_lock(self, *, owner: str, blocking: bool) -> Any | None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        handle = self._execution_lock_path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            handle.close()
            return None
        metadata = {
            "owner": _safe_text(owner, 200),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": self.timestamp_factory(),
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, ensure_ascii=False, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        return handle

    def _release_execution_lock(self, handle: Any, *, job_id: str) -> None:
        if handle is None:
            return
        with self._thread_lock:
            self._execution_locks.pop(job_id, None)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _execution_lock_is_held(self) -> bool:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        handle = self._execution_lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        return False

    def _start_background_job(self, job_id: str, execution_lock: Any) -> None:
        with self._thread_lock:
            existing = self._threads.get(job_id)
            if existing and existing.is_alive():
                return
            self._execution_locks[job_id] = execution_lock
            thread = threading.Thread(target=self._run_job_background, args=(job_id,), daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _run_job_background(self, job_id: str) -> None:
        try:
            self._run_job(job_id)
        finally:
            with self._thread_lock:
                handle = self._execution_locks.get(job_id)
                self._threads.pop(job_id, None)
            self._release_execution_lock(handle, job_id=job_id)

    def _set_job_status(self, job: dict[str, Any], status: str, note: str) -> None:
        job["status"] = status
        job["updated_at"] = self.timestamp_factory()
        self._set_lifecycle(job, classification="live", phase=status)
        self._append_timeline(job, status, note)
        self._save_job(job)
        self._write_current_job(job)

    def _set_lifecycle(self, job: dict[str, Any], **patch: Any) -> None:
        lifecycle = dict(job.get("lifecycle_diagnostics") or {}) if isinstance(job.get("lifecycle_diagnostics"), Mapping) else {}
        lifecycle.update(_json_safe(patch))
        lifecycle["heartbeat_at"] = self.timestamp_factory()
        lifecycle.setdefault("runner_pid", os.getpid())
        lifecycle.setdefault("runner_host", socket.gethostname())
        job["lifecycle_diagnostics"] = lifecycle

    def _sleep_with_heartbeat(self, job: Mapping[str, Any], seconds: float, *, phase: str) -> None:
        remaining = max(0.0, float(seconds or 0))
        if remaining <= 0:
            return
        job_id = str(job.get("job_id") or "")
        while remaining > 0:
            chunk = min(30.0, remaining)
            self.sleep(chunk)
            remaining -= chunk
            current = self._load_job(job_id) if job_id else None
            if current and str(current.get("status") or "") in SPP_TEST_ACTIVE_STATUSES:
                current["updated_at"] = self.timestamp_factory()
                self._set_lifecycle(current, classification="live", phase=phase)
                self._save_job(current)
                self._write_current_job(current)

    def _append_timeline(self, job: dict[str, Any], status: str, note: str) -> None:
        job.setdefault("timeline", []).append(
            {
                "timestamp": self.timestamp_factory(),
                "status": status,
                "note": note,
            }
        )

    def _append_audit(self, job_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "timestamp": self.timestamp_factory(),
            "job_id": job_id,
            "event_type": event_type,
            "payload": _json_safe(payload),
        }
        with self._audit_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _job_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def _save_job(self, job: Mapping[str, Any]) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        job_id = str(job.get("job_id") or "").strip()
        if not JOB_ID_RE.fullmatch(job_id):
            raise WbSppTesterError("job_id is missing", http_status=500)
        with self._state_lock:
            _atomic_write_json(self._job_path(job_id), job)

    def _load_job(self, job_id: str) -> dict[str, Any] | None:
        normalized = str(job_id or "").strip()
        if not JOB_ID_RE.fullmatch(normalized):
            return None
        path = self._job_path(normalized)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WbSppTesterError("SPP test job state is not readable", http_status=500) from exc
        return payload if isinstance(payload, dict) else None

    def _write_current_job(self, job: Mapping[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        current = {
            "job_id": str(job.get("job_id") or ""),
            "status": str(job.get("status") or ""),
            "heartbeat_at": self.timestamp_factory(),
            "expires_at_epoch": int(self._now().timestamp()) + int(self.cadence.active_lock_ttl_seconds),
            "runner_pid": (job.get("lifecycle_diagnostics") or {}).get("runner_pid")
            if isinstance(job.get("lifecycle_diagnostics"), Mapping)
            else None,
            "runner_host": (job.get("lifecycle_diagnostics") or {}).get("runner_host")
            if isinstance(job.get("lifecycle_diagnostics"), Mapping)
            else None,
            "trigger_source": str(job.get("trigger_source") or "") or None,
        }
        with self._state_lock:
            _atomic_write_json(self._current_job_path, current)

    def _load_current_job_payload(self) -> dict[str, Any] | None:
        if not self._current_job_path.exists():
            return None
        try:
            current = json.loads(self._current_job_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(current, Mapping):
            return None
        return self._load_job(str(current.get("job_id") or ""))

    def _now(self) -> datetime:
        value = self.now_factory()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _sanitize_public_payload(value: Any, *, details: bool, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        blocked_markers = ("token", "cookie", "secret", "password", "authorization")
        for raw_key, raw_value in value.items():
            key = str(raw_key or "")[:160]
            lowered = key.lower()
            if (
                not key
                or lowered == "headers"
                or lowered == "path"
                or lowered.endswith("_path")
                or any(marker in lowered for marker in blocked_markers)
            ):
                continue
            result[key] = _sanitize_public_payload(raw_value, details=details, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        limit = 500 if details else 100
        return [_sanitize_public_payload(item, details=details, depth=depth + 1) for item in value[:limit]]
    if isinstance(value, str):
        return value.replace("\x00", "")[:4000 if details else 1000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _safe_text(value, 1000)


def _encode_history_cursor(created_at: str, job_id: str) -> str:
    raw = json.dumps([created_at, job_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_history_cursor(value: str) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > 1024:
        raise WbSppTesterError("invalid history cursor", http_status=400)
    try:
        padding = "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode((text + padding).encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise WbSppTesterError("invalid history cursor", http_status=400) from exc
    if not isinstance(payload, list) or len(payload) != 2 or not JOB_ID_RE.fullmatch(str(payload[1] or "")):
        raise WbSppTesterError("invalid history cursor", http_status=400)
    return str(payload[0] or "")[:100], str(payload[1])


def _duration_seconds(start_value: Any, end_value: Any) -> int | None:
    start = _parse_iso_datetime(start_value)
    end = _parse_iso_datetime(end_value)
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds())


def _history_sort_key(job: Mapping[str, Any]) -> tuple[str, str]:
    parsed = _parse_iso_datetime(job.get("created_at") or job.get("updated_at"))
    timestamp = parsed.astimezone(timezone.utc).isoformat() if parsed is not None else ""
    return timestamp, str(job.get("job_id") or "")


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _normalize_hhmm(value: Any) -> str:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise WbSppTesterError("local_time_hhmm must use HH:mm", http_status=422)
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise WbSppTesterError("local_time_hhmm must use HH:mm", http_status=422) from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise WbSppTesterError("local_time_hhmm must be a valid 24-hour time", http_status=422)
    return f"{hour:02d}:{minute:02d}"


def _next_daily_run_at(now: datetime, *, local_time_hhmm: str, timezone_name: str) -> datetime:
    timezone_value = ZoneInfo(timezone_name)
    local_now = now.astimezone(timezone_value)
    hour, minute = (int(part) for part in local_time_hhmm.split(":", 1))
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


def _scheduled_skip_reason(exc: WbSppTesterError) -> str:
    reason = str(exc.payload.get("reason") or "").strip()
    if reason:
        return reason
    if exc.http_status == 403:
        return "write_guard_disabled"
    if exc.http_status == 409:
        return "active_or_unrestored_job"
    if exc.http_status == 422:
        return "safety_blocker"
    return "scheduler_start_failed"


def _restore_proof_ok(proof: Mapping[str, Any]) -> bool:
    return bool(
        proof.get("price_matches")
        and proof.get("discount_matches")
        and proof.get("discountedPrice_matches")
        and proof.get("quarantine_absent")
    )


def _money_exact(left: Any, right: Any) -> bool:
    try:
        return _parse_money(left, "left_money") == _parse_money(right, "right_money")
    except WbSppTesterError:
        return False


def _bounded_int(value: Any, *, minimum: int, maximum: int, default: int) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        parsed = default
    return max(minimum, min(maximum, parsed))


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\n", " ").replace("\r", " ").strip()[:limit]


def _load_safety_config() -> WbSppTesterSafetyConfig:
    return WbSppTesterSafetyConfig(
        spp_test_enabled=_env_bool("WB_SPP_TEST_ENABLED"),
        prices_write_enabled=_env_bool("WB_PRICES_WRITE_ENABLED"),
        restore_baseline_required=True,
    )


def _load_cadence_config() -> WbSppTesterCadenceConfig:
    return WbSppTesterCadenceConfig(
        run_async=not _env_bool("WB_SPP_TEST_INLINE_RUNNER"),
        measurement_upload_cooldown_seconds=_env_int("WB_SPP_TEST_UPLOAD_COOLDOWN_SECONDS", 600),
        first_public_poll_delay_seconds=_env_int("WB_SPP_TEST_FIRST_PUBLIC_POLL_DELAY_SECONDS", 60),
        public_poll_gap_seconds=_env_int("WB_SPP_TEST_PUBLIC_POLL_GAP_SECONDS", 90),
        extended_public_poll_gap_seconds=_env_int("WB_SPP_TEST_EXTENDED_PUBLIC_POLL_GAP_SECONDS", 120),
        upload_status_poll_seconds=_env_int("WB_SPP_TEST_UPLOAD_STATUS_POLL_SECONDS", 20),
        upload_status_max_polls=_env_int("WB_SPP_TEST_UPLOAD_STATUS_MAX_POLLS", 24),
        readback_poll_seconds=_env_int("WB_SPP_TEST_READBACK_POLL_SECONDS", 20),
        readback_max_polls=_env_int("WB_SPP_TEST_READBACK_MAX_POLLS", 12),
        rate_limit_min_cooldown_seconds=_env_int("WB_SPP_TEST_429_COOLDOWN_SECONDS", 900),
        active_lock_ttl_seconds=_env_int("WB_SPP_TEST_LOCK_TTL_SECONDS", 1800),
        schedule_late_window_minutes=_env_int(
            "WB_SPP_TEST_SCHEDULE_LATE_WINDOW_MINUTES",
            SPP_TEST_SCHEDULE_LATE_WINDOW_MINUTES,
        ),
    )


def _estimate_duration_seconds(*, max_measurements: int, cadence: WbSppTesterCadenceConfig) -> int:
    per_measurement = (
        cadence.measurement_upload_cooldown_seconds
        + cadence.first_public_poll_delay_seconds
        + cadence.public_poll_gap_seconds * 2
        + cadence.upload_status_poll_seconds * 2
        + cadence.readback_poll_seconds * 2
    )
    return max_measurements * per_measurement


def _price_for_discounted(target_discounted: Decimal, discount: int) -> tuple[int, Decimal]:
    factor = (Decimal("100") - Decimal(int(discount))) / Decimal("100")
    if factor <= 0:
        raise WbSppTesterError("discount must keep positive seller price factor", http_status=422)
    price = (target_discounted / factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if price <= 0:
        price = Decimal("1")
    expected = _discounted_price(price, Decimal(int(discount)))
    return int(price), expected


def _discounted_price(price: Decimal, discount: Decimal) -> Decimal:
    return (price * (Decimal("100") - discount) / Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)


def _spp_proxy(seller_discounted: Decimal, public_buyer_price: Decimal) -> Decimal:
    if seller_discounted <= 0:
        return Decimal("0")
    return ((seller_discounted - public_buyer_price) / seller_discounted).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _looks_like_stale_public_price(current: Mapping[str, Any], previous: Mapping[str, Any], *, precision: Decimal) -> bool:
    current_public = _number_or_none(current.get("authenticated_buyer_price") or current.get("public_buyer_price"))
    previous_public = _number_or_none(previous.get("authenticated_buyer_price") or previous.get("public_buyer_price"))
    current_discounted = _number_or_none(current.get("actual_wb_discounted_price"))
    previous_discounted = _number_or_none(previous.get("actual_wb_discounted_price"))
    current_spp = _number_or_none(current.get("spp_proxy"))
    previous_spp = _number_or_none(previous.get("spp_proxy"))
    if None in {current_public, previous_public, current_discounted, previous_discounted, current_spp, previous_spp}:
        return False
    discounted_delta = abs(_parse_money(current_discounted, "current_discounted") - _parse_money(previous_discounted, "previous_discounted"))
    spp_delta = abs(_parse_ratio(current_spp, "current_spp") - _parse_ratio(previous_spp, "previous_spp"))
    return current_public == previous_public and discounted_delta > precision and spp_delta >= Decimal("0.015")


def _destination_context_from_url(value: str) -> dict[str, str]:
    try:
        parsed = urllib_parse.urlparse(str(value or ""))
        query = urllib_parse.parse_qs(parsed.query)
    except Exception:
        return {}
    result: dict[str, str] = {}
    for target, keys in {
        "dest": ("dest",),
        "regions": ("regions",),
        "currency": ("curr", "currency"),
        "locale": ("locale",),
    }.items():
        for key in keys:
            values = query.get(key)
            if values:
                result[target] = str(values[0])[:160]
                break
    return result


def _destination_context_from_region_label(value: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, raw_value in urllib_parse.parse_qsl(str(value or "").replace(";", "&"), keep_blank_values=False):
        normalized = str(key or "").strip().lower()
        if normalized in {"dest", "regions", "curr", "currency", "locale"}:
            target = "currency" if normalized == "curr" else normalized
            result[target] = str(raw_value)[:160]
    return result


def _buyer_contexts_compatible(authenticated: Mapping[str, Any], anonymous: Mapping[str, Any]) -> bool:
    auth_context = authenticated.get("destination_context") if isinstance(authenticated.get("destination_context"), Mapping) else {}
    anonymous_context = anonymous.get("destination_context") if isinstance(anonymous.get("destination_context"), Mapping) else {}
    if not auth_context and not anonymous_context:
        return True
    if not auth_context or not anonymous_context:
        return False
    compared = False
    for key in ("dest", "regions", "currency", "locale"):
        left = str(auth_context.get(key) or "").strip().lower()
        right = str(anonymous_context.get(key) or "").strip().lower()
        if not left or not right:
            continue
        compared = True
        if left != right:
            return False
    return compared


def _stable_buyer_pair(reads: Sequence[Mapping[str, Any]]) -> tuple[float, float] | None:
    if len(reads) < 2:
        return None
    left = reads[-2]
    right = reads[-1]
    left_auth = left.get("authenticated") if isinstance(left.get("authenticated"), Mapping) else {}
    right_auth = right.get("authenticated") if isinstance(right.get("authenticated"), Mapping) else {}
    left_anon = left.get("anonymous") if isinstance(left.get("anonymous"), Mapping) else {}
    right_anon = right.get("anonymous") if isinstance(right.get("anonymous"), Mapping) else {}
    auth_left = _number_or_none(left_auth.get("authenticated_buyer_price"))
    auth_right = _number_or_none(right_auth.get("authenticated_buyer_price"))
    anon_left = _number_or_none(left_anon.get("public_buyer_price"))
    anon_right = _number_or_none(right_anon.get("public_buyer_price"))
    if None in {auth_left, auth_right, anon_left, anon_right}:
        return None
    if not _money_exact(auth_left, auth_right) or not _money_exact(anon_left, anon_right):
        return None
    left_fingerprint = str(left_auth.get("session_fingerprint") or "")
    right_fingerprint = str(right_auth.get("session_fingerprint") or "")
    if not left_fingerprint or left_fingerprint != right_fingerprint:
        return None
    if _destination_signature(left_auth) != _destination_signature(right_auth):
        return None
    if _destination_signature(left_anon) != _destination_signature(right_anon):
        return None
    if str(left_auth.get("payment_context") or "unknown/mixed") != str(right_auth.get("payment_context") or "unknown/mixed"):
        return None
    return float(auth_right), float(anon_right)


def _destination_signature(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    context = value.get("destination_context") if isinstance(value.get("destination_context"), Mapping) else {}
    return tuple(str(context.get(key) or "").strip().lower() for key in ("dest", "regions", "currency", "locale"))


def _account_discount(authenticated_price: Any, anonymous_price: Any) -> tuple[float | None, float | None]:
    authenticated = _number_or_none(authenticated_price)
    anonymous = _number_or_none(anonymous_price)
    if authenticated is None or anonymous is None or anonymous <= 0:
        return None, None
    difference = (_parse_money(anonymous, "anonymous_price") - _parse_money(authenticated, "authenticated_price")).quantize(MONEY)
    percent = (difference / _parse_money(anonymous, "anonymous_price")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return _decimal_to_float(difference), _decimal_to_float(percent)


def _previous_high_confidence_point(measurements: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for row in reversed(measurements):
        if row.get("confidence") == "high" and row.get("status") == "ok":
            return row
    return None


def _strongest_refinement_interval(thresholds: Sequence[Mapping[str, Any]], *, precision: Decimal) -> Mapping[str, Any] | None:
    candidates = [
        row
        for row in thresholds
        if row.get("confidence") in {"material", "strong"}
        and _parse_money(row.get("bracket_width"), "bracket_width") > precision
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (_parse_ratio(row.get("delta"), "delta"), _parse_money(row.get("bracket_width"), "bracket_width")))


def _stable_public_price(reads: Sequence[Mapping[str, Any]]) -> float | None:
    values = [_number_or_none(row.get("public_buyer_price")) for row in reads]
    values = [value for value in values if value is not None]
    if len(values) >= 3 and values[-1] == values[-2] == values[-3]:
        return values[-1]
    if len(values) >= 2 and values[-1] == values[-2]:
        return values[-1]
    return None


def _public_status_is_429(payload: Mapping[str, Any]) -> bool:
    return _optional_int(payload.get("http_status")) == 429 or str(payload.get("status") or "") == "429"


def _payload_items(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
    items = data.get("items")
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def _payload_missing(payload: Mapping[str, Any], *, nm_id: int) -> Mapping[str, Any]:
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), Mapping) else {}
    missing = diagnostics.get("missing") if isinstance(diagnostics.get("missing"), list) else []
    for item in missing:
        if isinstance(item, Mapping) and _optional_int(item.get("nmId") or item.get("nm_id")) == nm_id:
            return item
    return diagnostics


def _display_title(enrichment: Mapping[str, Any]) -> str:
    return str(
        enrichment.get("nomenclature_name")
        or enrichment.get("wb_title")
        or enrichment.get("display_name")
        or ""
    )


def _format_percent_label(value: Any) -> str:
    number = _number_or_none(value)
    if number is None:
        return "н/д"
    return f"{round(float(number) * 100, 1):g}%"


def _dedupe_money(values: Sequence[Decimal]) -> list[Decimal]:
    result: list[Decimal] = []
    seen: set[str] = set()
    for value in values:
        rounded = value.quantize(MONEY, rounding=ROUND_HALF_UP)
        signature = str(rounded)
        if signature in seen:
            continue
        seen.add(signature)
        result.append(rounded)
    return result


def _money_close(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(left - right) <= tolerance


def _parse_money(value: Any, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise WbSppTesterError(f"{field_name} must be numeric", http_status=400)
    try:
        return Decimal(str(value).strip().replace(",", ".")).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError) as exc:
        raise WbSppTesterError(f"{field_name} must be numeric", http_status=400) from exc


def _parse_ratio(value: Any, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise WbSppTesterError(f"{field_name} must be numeric", http_status=400)
    try:
        return Decimal(str(value).strip().replace(",", ".")).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, AttributeError) as exc:
        raise WbSppTesterError(f"{field_name} must be numeric", http_status=400) from exc


def _decimal_to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _single_param(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def _as_positive_int(value: Any, field_name: str) -> int:
    number = _optional_int(value)
    if number is None or number <= 0:
        raise WbSppTesterError(f"{field_name} must be a positive integer", http_status=400)
    return number


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "да"}


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _current_business_date() -> str:
    return datetime.now(BUSINESS_TIMEZONE).date().isoformat()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_to_float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
