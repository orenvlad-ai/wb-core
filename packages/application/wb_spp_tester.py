"""Bounded application block for manual live WB SPP price checks."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

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
from packages.application.change_registry_writer import (
    InternalWriterRegistry,
    InternalWriterRegistryError,
    price_tuple_from_wb,
)
from packages.application.wb_buyer_session import WbBuyerSessionBlock
from packages.contracts.wb_buyer_session import WbAuthenticatedBuyerPriceSource
from packages.contracts.wb_price_quarantine import (
    WB_QUARANTINE_RATIO,
    evaluate_wb_price_quarantine_transition,
)
from packages.contracts.wb_spp_tester import (
    SPP_TEST_ACTIVE_STATUSES,
    SPP_TEST_CONTRACT_PREFIX,
    SPP_TEST_HISTORY_DEFAULT_LIMIT,
    SPP_TEST_HISTORY_MAX_LIMIT,
    SPP_TEST_LOG_LIMIT,
    SPP_TEST_PRICE_COUNT_MAX,
    SPP_TEST_PRICE_COUNT_MIN,
)


MONEY = Decimal("0.01")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
HISTORY_CONTRACT_NAME = f"{SPP_TEST_CONTRACT_PREFIX}_history"
TECH_LOG_LABELS = {
    "start_preflight": "Проверка бота",
    "start_preflight_failed": "Проверка бота",
    "baseline_failed": "Baseline",
    "sequence_quarantine_blocked": "Карантин",
    "job_start": "Baseline",
    "measurement_started": "Цена",
    "measurement_buyer_session_blocked": "Проверка бота",
    "measurement_prewrite_guard_blocked": "Карантин",
    "wb_upload_task": "Запись цены",
    "wb_upload_task_error": "Запись цены",
    "wb_prices_readback": "WB readback",
    "authenticated_buyer_price_read": "Цена покупателя",
    "measurement_finished": "Результат",
    "measurement_sequence_stopped": "Остановка",
    "restore_preflight_error": "Restore",
    "restore_preflight_blocked": "Restore",
    "restore_already_confirmed": "Restore",
    "restore_final_proof": "Restore",
    "job_finish": "Завершение",
    "orphan_reconcile_restored": "Reconcile",
    "orphan_reconcile_unrestored": "Reconcile",
    "current_job_cleared": "Lock",
}


class WbSppTesterError(ValueError):
    """Expected validation/safety error for the SPP tester block."""

    def __init__(self, message: str, *, http_status: int = 400, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.http_status = int(http_status)
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class WbSppTesterSafetyConfig:
    spp_test_enabled: bool
    prices_write_enabled: bool
    restore_baseline_required: bool = True


@dataclass(frozen=True)
class WbSppTesterCadenceConfig:
    run_async: bool = True
    measurement_upload_cooldown_seconds: int = 600
    first_buyer_poll_delay_seconds: int = 60
    buyer_poll_gap_seconds: int = 90
    upload_status_poll_seconds: int = 20
    upload_status_max_polls: int = 24
    readback_poll_seconds: int = 20
    readback_max_polls: int = 12
    rate_limit_min_cooldown_seconds: int = 900
    active_lock_ttl_seconds: int = 1800


class WbSppTesterBlock:
    """Server-owned live SPP tester with guarded writes and staged restore."""

    def __init__(
        self,
        *,
        runtime: Any,
        runtime_dir: Path,
        prices_source: WbPricesManagementSource | None = None,
        buyer_source: WbAuthenticatedBuyerPriceSource | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
        safety_config: WbSppTesterSafetyConfig | None = None,
        cadence_config: WbSppTesterCadenceConfig | None = None,
        writer_registry: InternalWriterRegistry | None = None,
    ) -> None:
        self.runtime = runtime
        self.runtime_dir = runtime_dir
        self.prices_source = prices_source or HttpBackedWbPricesManagementSource()
        self.buyer_source = buyer_source or WbBuyerSessionBlock()
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or time.sleep
        self.safety = safety_config or _load_safety_config()
        self.cadence = cadence_config or _load_cadence_config()
        self.writer_registry = writer_registry
        self._state_dir = self.runtime_dir / "sheet_vitrina_v1_prices" / "spp_tests"
        self._jobs_dir = self._state_dir / "jobs"
        self._current_job_path = self._state_dir / "current_job.json"
        self._audit_path = self._state_dir / "audit.jsonl"
        self._execution_lock_path = self._state_dir / "execution.lock"
        self._threads: dict[str, threading.Thread] = {}
        self._execution_locks: dict[str, Any] = {}
        self._thread_lock = threading.RLock()
        self._state_lock = threading.RLock()

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

    def start(
        self,
        payload: Mapping[str, Any],
        *,
        actor: str = "",
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
        nm_id, target_prices = self._parse_start_input(payload)
        execution_lock = self._acquire_execution_lock(
            owner=f"manual:{actor or 'unknown'}",
            blocking=False,
        )
        if execution_lock is None:
            raise WbSppTesterError(
                "another SPP test runner holds the execution lock",
                http_status=409,
                payload={"reason": "execution_lock_busy", "active_job": self._current_job_summary(reconcile=False)},
            )
        job_id = uuid4().hex
        lock_transferred = False
        try:
            blocking = self._blocking_current_job(reconcile=True, caller_holds_execution_lock=True)
            if blocking is not None:
                raise WbSppTesterError(
                    "another SPP test job is active or requires restore",
                    http_status=409,
                    payload={"reason": "active_or_unrestored_job", "active_job": blocking},
                )
            self._append_audit(job_id, "start_preflight", {"nmID": nm_id, "price_count": len(target_prices)})
            try:
                buyer_session = self._require_buyer_session()
            except WbSppTesterError as exc:
                self._append_audit(
                    job_id,
                    "start_preflight_failed",
                    {"reason": exc.payload.get("reason") or "buyer_capability_invalid", "error": str(exc)},
                )
                exc.payload["log_events"] = self._load_log_events(job_id=job_id)
                raise
            try:
                baseline = self._capture_baseline(nm_id=nm_id, strict=True)
            except WbSppTesterError as exc:
                self._append_audit(
                    job_id,
                    "baseline_failed",
                    {
                        "reason": exc.payload.get("reason") or "baseline_invalid",
                        "error": str(exc),
                    },
                )
                exc.payload["log_events"] = self._load_log_events(job_id=job_id)
                raise
            measurement_plan = self._build_measurement_plan(
                baseline=baseline,
                target_prices=target_prices,
            )
            risky_transitions = [
                dict(item)
                for item in measurement_plan
                if bool((item.get("quarantine_transition") or {}).get("risky"))
            ]
            if risky_transitions:
                self._append_audit(
                    job_id,
                    "sequence_quarantine_blocked",
                    {"risky_transitions": risky_transitions},
                )
                raise WbSppTesterError(
                    "Последовательность цен может попасть в карантин WB. Ни одна цена не изменена.",
                    http_status=422,
                    payload={
                        "reason": "price_quarantine_risk",
                        "risky_transitions": risky_transitions,
                        "log_events": self._load_log_events(job_id=job_id),
                    },
                )
            now_text = self.timestamp_factory()
            job = {
                "job_id": job_id,
                "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_job",
                "created_at": now_text,
                "updated_at": now_text,
                "finished_at": "",
                "actor": actor,
                "trigger_source": "manual",
                "status": "preflight",
                "result_status": "",
                "nmID": nm_id,
                "input": {
                    "target_prices": [_decimal_to_float(value) for value in target_prices],
                    "price_count": len(target_prices),
                    "restore_baseline": True,
                    "measurement_plan": measurement_plan,
                },
                "baseline": baseline,
                "buyer_session": buyer_session,
                "measurements": [],
                "timeline": [],
                "restore": {"required": True, "restored": False, "proof": None, "steps": []},
                "lifecycle_diagnostics": {
                    "classification": "live",
                    "runner_pid": os.getpid(),
                    "runner_host": socket.gethostname(),
                    "runner_token": uuid4().hex,
                    "heartbeat_at": now_text,
                    "phase": "preflight",
                },
                "manual_restore_required": False,
                "warnings": [],
                "error": "",
            }
            self._append_timeline(job, "preflight", "baseline_and_buyer_capability_confirmed")
            self._save_job(job)
            self._write_current_job(job)
            self._append_audit(
                job_id,
                "job_start",
                {
                    "actor": actor,
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
            "job": self._job_public_payload(current),
            "log_events": self._load_log_events(job_id=job_id),
        }

    def status(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        requested_job_id = str(_single_param(params.get("job_id") or params.get("jobID")) or "").strip()
        active_job = self._current_job_summary()
        if requested_job_id:
            job = self._load_job(requested_job_id)
        else:
            job = self._load_current_job_payload() or self._load_latest_job_payload()
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_status",
            "generated_at": self.timestamp_factory(),
            "active_job": active_job,
            "job": self._job_public_payload(job) if job else None,
            "log_events": self._load_log_events(
                job_id=str(job.get("job_id") or "") if isinstance(job, Mapping) else ""
            ),
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
                job["result_status"] = _restored_result_status(job.get("result_status"))
                job["manual_restore_required"] = False
                job["finished_at"] = self.timestamp_factory()
                self._set_lifecycle(
                    job,
                    classification="terminal",
                    phase="interrupted_restored",
                    reconciled_at=self.timestamp_factory(),
                    restore_proof=(job.get("restore") or {}).get("proof"),
                )
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
            "active_job": self._current_job_summary(),
            "job": self._job_public_payload(job),
            "log_events": self._load_log_events(job_id=str(job.get("job_id") or "")),
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
            result_status = _restored_result_status(job.get("result_status"))
            final_status = "complete" if result_status == "success" else "failed"
            job["result_status"] = result_status
            job["status"] = final_status
            job["updated_at"] = self.timestamp_factory()
            job["finished_at"] = self.timestamp_factory()
            job["manual_restore_required"] = False
            self._set_lifecycle(
                job,
                classification="terminal",
                phase=final_status,
                restore_proof=(job.get("restore") or {}).get("proof"),
            )
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
                job["result_status"] = "inconclusive"
                job["manual_restore_required"] = False
            else:
                job["status"] = "manual_restore_required"
                job["result_status"] = "manual_restore_required"
                job["manual_restore_required"] = True
            job["updated_at"] = self.timestamp_factory()
            job["finished_at"] = self.timestamp_factory()
            self._set_lifecycle(
                job,
                classification="terminal",
                phase=str(job["status"]),
                restore_proof=(job.get("restore") or {}).get("proof"),
            )
            self._save_job(job)
            self._write_current_job(job)
            self._append_audit(
                job_id,
                "job_finish",
                {"status": job["status"], "result_status": job["result_status"], "restored": restored},
            )

    def _execute_measurements(self, job: dict[str, Any]) -> None:
        route = [
            _parse_money(value, f"target_prices[{index}]")
            for index, value in enumerate(job["input"].get("target_prices", []))
        ]
        start_buyer_session = (
            dict(job.get("buyer_session") or {})
            if isinstance(job.get("buyer_session"), Mapping)
            else {}
        )
        for index, target in enumerate(route):
            point = self._measure_point(
                job,
                target,
                buyer_session_preflight=start_buyer_session if index == 0 else None,
            )
            job["measurements"].append(point)
            self._save_job(job)
            if point.get("status") != "ok":
                job["result_status"] = "inconclusive"
                self._append_audit(
                    str(job["job_id"]),
                    "measurement_sequence_stopped",
                    {"index": index + 1, "status": point.get("status"), "remaining": len(route) - index - 1},
                )
                break
            if index < len(route) - 1:
                self._set_job_status(job, "cooldown", "between_measurements")
                self._sleep_with_heartbeat(job, self.cadence.measurement_upload_cooldown_seconds, phase="between_measurements")
                self._set_job_status(job, "measuring", "measurement_resumed")
        else:
            job["result_status"] = "success"
        self._save_job(job)

    def _measure_point(
        self,
        job: dict[str, Any],
        target_discounted: Decimal,
        *,
        buyer_session_preflight: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
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
            "spp_proxy": None,
            "uploadID": None,
            "status": "started",
            "note": "",
            "evidence": {},
        }
        self._append_audit(str(job["job_id"]), "measurement_started", measurement)
        session = (
            {**dict(buyer_session_preflight), "reused_start_preflight": True}
            if isinstance(buyer_session_preflight, Mapping)
            else self._safe_buyer_session_preflight()
        )
        measurement["evidence"]["buyer_session_preflight"] = session
        if session.get("status") != "valid" or session.get("capability_valid") is not True:
            measurement["status"] = "buyer_session_invalid"
            measurement["note"] = "authenticated buyer-price capability is not ready; no seller price write was attempted"
            self._append_audit(str(job["job_id"]), "measurement_buyer_session_blocked", measurement)
            return measurement
        try:
            write_guard = self._fresh_measurement_write_guard(
                job,
                upload_price=upload_price,
                expected_discounted=expected_discounted,
            )
        except Exception:
            write_guard = {
                "status": "seller_guard_unavailable",
                "safe": False,
                "reason": "fresh_seller_readback_or_quarantine_unavailable",
            }
        measurement["evidence"]["prewrite_guard"] = write_guard
        if write_guard.get("safe") is not True:
            measurement["status"] = str(write_guard.get("status") or "seller_guard_unavailable")
            measurement["note"] = "fresh seller/quarantine guard blocked the measurement; no seller price write was attempted"
            self._append_audit(str(job["job_id"]), "measurement_prewrite_guard_blocked", measurement)
            return measurement
        upload = self._upload_price_with_backoff(
            job,
            [{"nmID": nm_id, "price": upload_price}],
            stage=f"measurement:{point_id}",
            before=write_guard["current"],
            requested=write_guard["next"],
        )
        if upload.get("status") == "rate_limited_stop":
            measurement["status"] = "rate_limited_stop"
            measurement["note"] = upload.get("note") or "WB Prices API repeated 429"
            self._append_audit(str(job["job_id"]), "measurement_rate_limited_stop", measurement)
            return measurement
        upload_id = _optional_int(upload.get("uploadID"))
        measurement["uploadID"] = upload_id
        if upload_id is None:
            self._mark_registry_upload_ambiguous(
                upload,
                error_code="wb_upload_missing_id",
                error_message="WB upload response had no uploadID",
            )
            measurement["status"] = "upload_missing_id"
            measurement["note"] = "WB upload task did not return uploadID"
            return measurement
        try:
            upload_status = self._wait_upload_final(job, upload_id)
        except Exception as exc:
            self._mark_registry_upload_ambiguous(
                upload,
                error_code="wb_upload_status_unavailable",
                error_message=str(exc),
            )
            raise
        measurement["evidence"]["upload_status"] = upload_status
        if upload_status.get("status") != "success":
            status = str(upload_status.get("status") or "unknown")
            if status in {"partial_error", "all_error", "canceled"}:
                self._mark_registry_upload_failed(
                    upload,
                    error_code=f"wb_upload_{status}",
                    error_message="WB upload reached a non-success final status",
                )
            else:
                self._mark_registry_upload_ambiguous(
                    upload,
                    error_code=f"wb_upload_{status}",
                    error_message="WB upload final status could not be verified",
                )
            measurement["status"] = "upload_not_success"
            measurement["note"] = str(upload_status.get("status") or "unknown upload status")
            return measurement

        try:
            readback = self._wait_discounted_readback(
                job,
                expected_discounted=expected_discounted,
                expected_price=upload_price,
                expected_discount=discount,
            )
        except Exception as exc:
            self._mark_registry_upload_ambiguous(
                upload,
                error_code="wb_readback_unavailable",
                error_message=str(exc),
            )
            raise
        measurement["evidence"]["readback"] = readback
        actual_discounted = _number_or_none(readback.get("discountedPrice"))
        measurement["actual_wb_discounted_price"] = actual_discounted
        if (
            actual_discounted is None
            or _optional_int(readback.get("price")) != upload_price
            or _optional_int(readback.get("discount")) != discount
            or not _money_exact(actual_discounted, expected_discounted)
        ):
            self._mark_registry_upload_ambiguous(
                upload,
                error_code="wb_readback_mismatch",
                error_message="exact seller price tuple did not match requested values",
            )
            measurement["status"] = "readback_mismatch"
            measurement["note"] = "WB readback did not match expected discounted price"
            return measurement
        self._confirm_registry_upload(job, upload, readback)

        quarantine = self._check_quarantine(job)
        measurement["evidence"]["quarantine"] = quarantine
        if quarantine.get("is_quarantined"):
            measurement["status"] = "quarantine_detected"
            measurement["note"] = "nmID is in WB price quarantine"
            return measurement

        buyer_proof = self._poll_authenticated_buyer_stable(job)
        measurement["evidence"]["buyer_price_proof"] = buyer_proof
        authenticated_price = _number_or_none(buyer_proof.get("authenticated_buyer_price"))
        measurement["authenticated_buyer_price"] = authenticated_price
        if authenticated_price is None or buyer_proof.get("stable") is not True:
            measurement["status"] = str(buyer_proof.get("status") or "authenticated_unstable")
            measurement["note"] = "authenticated buyer price did not reach stable proof"
            return measurement

        authenticated_spp = _spp_proxy(
            _parse_money(actual_discounted, "actual_discounted"),
            _parse_money(authenticated_price, "authenticated_buyer_price"),
        )
        measurement["spp_proxy"] = _decimal_to_float(authenticated_spp)
        measurement["status"] = "ok"
        measurement["note"] = "seller readback, authenticated buyer price and quarantine proof complete"
        self._append_audit(str(job["job_id"]), "measurement_finished", measurement)
        return measurement

    def _upload_price_with_backoff(
        self,
        job: Mapping[str, Any],
        goods: Sequence[Mapping[str, Any]],
        *,
        stage: str,
        before: Mapping[str, Any],
        requested: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Submit exactly once; 429 and transport ambiguity are never retried."""

        prepared = None
        if self.writer_registry is not None:
            try:
                prepared = self.writer_registry.prepare_price(
                    source_surface="spp_tester",
                    actor=str(job.get("actor") or ""),
                    native_operation_id=f"{job['job_id']}:{stage}",
                    nm_id=int(job["nmID"]),
                    before=price_tuple_from_wb(
                        price=before.get("price"),
                        discount=before.get("discount"),
                        seller_price=before.get("discountedPrice"),
                    ),
                    requested=price_tuple_from_wb(
                        price=requested.get("price"),
                        discount=requested.get("discount"),
                        seller_price=requested.get("discountedPrice"),
                    ),
                    explicit_fields=tuple(
                        field
                        for key, field in (
                            ("price", "original_price_minor"),
                            ("discount", "discount_bps"),
                        )
                        if any(key in item for item in goods)
                    ),
                    requested_at=str(job.get("created_at") or self.timestamp_factory()),
                    correlation_id=str(job["job_id"]),
                    apply_operation_id=str(job["job_id"]),
                    native_audit_reference=(
                        "sheet_vitrina_v1_prices/spp_tests/audit.jsonl"
                        f"#job={job['job_id']}&stage={stage}"
                    ),
                    stage=stage,
                )
            except InternalWriterRegistryError as exc:
                raise WbSppTesterError(
                    "change registry preparation failed; WB price upload was not called",
                    http_status=503,
                    payload={"reason": "registry_fail_closed", "detail": str(exc)},
                ) from exc
        receipt_reference = f"wb-spp:{job['job_id']}:{stage}"
        try:
            payload = self.prices_source.upload_task(goods)
        except WbPricesApiError as exc:
            self._append_audit(str(job["job_id"]), "wb_upload_task_error", exc.to_dict())
            if prepared is not None:
                if exc.http_status is None:
                    self.writer_registry.ambiguous(
                        prepared,
                        error_code="wb_submit_transport_unknown",
                        error_message=str(exc),
                        receipt_reference=receipt_reference,
                    )
                else:
                    self.writer_registry.fail_before_submit(
                        prepared,
                        rejected=True,
                        error_code=f"wb_http_{exc.http_status}",
                        error_message=str(exc),
                    )
            if exc.http_status == 429:
                return {
                    "status": "rate_limited_stop",
                    "note": "WB Prices API 429; no blind retry was made",
                    "registry_receipt_reference": receipt_reference,
                }
            raise
        except Exception as exc:
            if prepared is not None:
                self.writer_registry.ambiguous(
                    prepared,
                    error_code="wb_submit_transport_unknown",
                    error_message=str(exc),
                    receipt_reference=receipt_reference,
                )
            raise
        self._append_audit(str(job["job_id"]), "wb_upload_task", {"request": {"data": list(goods)}, "response": payload})
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        upload_id = _optional_int(data.get("id") or data.get("uploadID") or data.get("upload_id"))
        if prepared is not None:
            try:
                self.writer_registry.submitted(
                    prepared,
                    receipt_reference=receipt_reference,
                    receipt_basis={
                        "upload_id": upload_id,
                        "already_exists": bool(data.get("alreadyExists")),
                        "stage": stage,
                    },
                )
            except InternalWriterRegistryError as exc:
                try:
                    self.writer_registry.ambiguous(
                        prepared,
                        error_code="registry_post_submit_failure",
                        error_message=str(exc),
                        receipt_reference=receipt_reference,
                    )
                except InternalWriterRegistryError:
                    pass
                raise WbSppTesterError(
                    "WB price response was received but registry lifecycle is ambiguous",
                    http_status=503,
                    payload={"reason": "registry_post_submit_ambiguous"},
                ) from exc
        return {
            "status": "created",
            "uploadID": upload_id,
            "alreadyExists": bool(data.get("alreadyExists")),
            "wb_response": payload,
            "registry_receipt_reference": receipt_reference if prepared is not None else "",
        }

    def _confirm_registry_upload(
        self,
        job: Mapping[str, Any],
        upload: Mapping[str, Any],
        readback: Mapping[str, Any],
    ) -> None:
        if self.writer_registry is None:
            return
        receipt = str(upload.get("registry_receipt_reference") or "")
        if not receipt:
            return
        prepared = self.writer_registry.find_by_receipt(receipt)
        if prepared is None:
            raise WbSppTesterError(
                "submitted registry operation is missing during exact readback",
                http_status=503,
            )
        exact_tuple = price_tuple_from_wb(
            price=readback.get("price"),
            discount=readback.get("discount"),
            seller_price=readback.get("discountedPrice"),
        )
        self.writer_registry.confirm_price(
            prepared,
            confirmed=exact_tuple,
            readback_basis={
                "job_id": str(job["job_id"]),
                "nm_id": int(job["nmID"]),
                "upload_id": _optional_int(upload.get("uploadID")),
                "confirmed": exact_tuple,
            },
            receipt_reference=receipt,
            native_audit_references=(
                "sheet_vitrina_v1_prices/spp_tests/audit.jsonl"
                f"#native_operation={prepared.native_operation_id}",
            ),
        )

    def _mark_registry_upload_ambiguous(
        self,
        upload: Mapping[str, Any],
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        if self.writer_registry is None:
            return
        receipt = str(upload.get("registry_receipt_reference") or "")
        if not receipt:
            return
        prepared = self.writer_registry.find_by_receipt(receipt)
        if prepared is not None:
            self.writer_registry.ambiguous(
                prepared,
                error_code=error_code,
                error_message=error_message,
                receipt_reference=receipt,
            )

    def _mark_registry_upload_failed(
        self,
        upload: Mapping[str, Any],
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        if self.writer_registry is None:
            return
        receipt = str(upload.get("registry_receipt_reference") or "")
        if not receipt:
            return
        prepared = self.writer_registry.find_by_receipt(receipt)
        if prepared is not None:
            self.writer_registry.failed_after_submit(
                prepared,
                error_code=error_code,
                error_message=error_message,
                receipt_reference=receipt,
            )

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

    def _wait_discounted_readback(
        self,
        job: Mapping[str, Any],
        *,
        expected_discounted: Decimal,
        expected_price: int,
        expected_discount: int,
    ) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        last: dict[str, Any] = {}
        for _attempt in range(self.cadence.readback_max_polls):
            good = self._fetch_current_good(nm_id, job_id=str(job["job_id"]), audit_event="wb_prices_readback")
            last = good
            actual = _number_or_none(good.get("discountedPrice"))
            if (
                actual is not None
                and _optional_int(good.get("price")) == expected_price
                and _optional_int(good.get("discount")) == expected_discount
                and _money_exact(actual, expected_discounted)
            ):
                return good
            self._sleep_with_heartbeat(job, self.cadence.readback_poll_seconds, phase="readback_poll")
        return last

    def _poll_authenticated_buyer_stable(self, job: Mapping[str, Any]) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        stable_reader = getattr(
            self.buyer_source,
            "fetch_stable_authenticated_buyer_price",
            None,
        )
        if callable(stable_reader):
            authenticated = dict(stable_reader(nm_id))
            self._append_audit(
                str(job["job_id"]),
                "authenticated_buyer_price_read",
                authenticated,
            )
            status = str(authenticated.get("status") or "")
            if (
                status == "ok"
                and authenticated.get("stable") is True
                and _number_or_none(authenticated.get("authenticated_buyer_price")) is not None
            ):
                return {
                    "status": "ok",
                    "stable": True,
                    "authenticated_buyer_price": _number_or_none(
                        authenticated.get("authenticated_buyer_price")
                    ),
                    "proof": str(authenticated.get("proof") or "2_identical_authenticated_reads"),
                }
            lost = status.startswith("session_") or status in {
                "login_redirect",
                "security_challenge",
                "wrong_account",
            }
            return {
                "status": "buyer_session_lost" if lost else "authenticated_unstable",
                "stable": False,
                "authenticated_buyer_price": None,
                "reason": str(
                    authenticated.get("reason") or "authenticated_price_read_failed"
                ),
            }
        reads: list[dict[str, Any]] = []
        self._sleep_with_heartbeat(
            job,
            self.cadence.first_buyer_poll_delay_seconds,
            phase="buyer_poll_initial",
        )
        for attempt in range(3):
            authenticated = dict(self.buyer_source.fetch_authenticated_buyer_price(nm_id))
            self._append_audit(str(job["job_id"]), "authenticated_buyer_price_read", authenticated)
            status = str(authenticated.get("status") or "")
            if status != "ok":
                lost = status.startswith("session_") or status in {
                    "login_redirect",
                    "security_challenge",
                    "wrong_account",
                }
                return {
                    "status": "buyer_session_lost" if lost else "authenticated_unstable",
                    "stable": False,
                    "authenticated_buyer_price": None,
                    "reason": str(authenticated.get("reason") or "authenticated_price_read_failed"),
                }
            reads.append(authenticated)
            stable = _stable_authenticated_price(reads)
            if stable is not None:
                return {
                    "status": "ok",
                    "stable": True,
                    "authenticated_buyer_price": stable,
                    "proof": "2_identical_authenticated_reads",
                }
            if attempt < 2:
                self._sleep_with_heartbeat(
                    job,
                    self.cadence.buyer_poll_gap_seconds,
                    phase="buyer_poll_gap",
                )
        return {
            "status": "authenticated_unstable",
            "stable": False,
            "authenticated_buyer_price": None,
        }

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

    def _build_measurement_plan(
        self,
        *,
        baseline: Mapping[str, Any],
        target_prices: Sequence[Decimal],
    ) -> list[dict[str, Any]]:
        discount = int(baseline["discount"])
        previous = _parse_money(baseline["discountedPrice"], "baseline_discountedPrice")
        previous_label = "baseline"
        plan: list[dict[str, Any]] = []
        for index, target in enumerate(target_prices):
            upload_price, expected = _price_for_discounted(target, discount)
            transition = evaluate_wb_price_quarantine_transition(previous, expected).to_dict()
            item = {
                "index": index + 1,
                "from": previous_label,
                "to": f"price_{index + 1}",
                "target_discounted_price": _decimal_to_float(target),
                "upload_price": upload_price,
                "expected_discounted_price": _decimal_to_float(expected),
                "quarantine_transition": transition,
            }
            plan.append(item)
            previous = expected
            previous_label = f"price_{index + 1}"
        return plan

    def _fresh_measurement_write_guard(
        self,
        job: Mapping[str, Any],
        *,
        upload_price: int,
        expected_discounted: Decimal,
    ) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        current = self._fetch_current_good(
            nm_id,
            job_id=str(job["job_id"]),
            audit_event="wb_measurement_prewrite_readback",
        )
        quarantine = self._check_quarantine(job)
        expected_current = self._expected_prewrite_tuple(job)
        tuple_matches = bool(
            _optional_int(current.get("price")) == _optional_int(expected_current.get("price"))
            and _optional_int(current.get("discount")) == _optional_int(expected_current.get("discount"))
            and _money_exact(current.get("discountedPrice"), expected_current.get("discountedPrice"))
        )
        current_discounted = _parse_money(current.get("discountedPrice"), "current_discountedPrice")
        transition = evaluate_wb_price_quarantine_transition(
            current_discounted,
            expected_discounted,
        ).to_dict()
        status = "safe"
        if quarantine.get("is_quarantined"):
            status = "quarantine_detected"
        elif not tuple_matches:
            status = "seller_state_drift"
        elif transition.get("risky"):
            status = "prewrite_quarantine_risk"
        return {
            "status": status,
            "safe": status == "safe",
            "checked_at": self.timestamp_factory(),
            "tuple_matches_expected_current": tuple_matches,
            "current": {
                "price": _optional_int(current.get("price")),
                "discount": _optional_int(current.get("discount")),
                "discountedPrice": _number_or_none(current.get("discountedPrice")),
            },
            "expected_current": {
                "price": _optional_int(expected_current.get("price")),
                "discount": _optional_int(expected_current.get("discount")),
                "discountedPrice": _number_or_none(expected_current.get("discountedPrice")),
            },
            "next": {
                "price": int(upload_price),
                "discount": int(job["baseline"]["discount"]),
                "discountedPrice": _decimal_to_float(expected_discounted),
            },
            "quarantine_absent": not quarantine.get("is_quarantined"),
            "quarantine_transition": transition,
        }

    def _expected_prewrite_tuple(self, job: Mapping[str, Any]) -> Mapping[str, Any]:
        rows = job.get("measurements") if isinstance(job.get("measurements"), list) else []
        for row in reversed(rows):
            if not isinstance(row, Mapping) or row.get("status") != "ok":
                continue
            evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
            readback = evidence.get("readback") if isinstance(evidence.get("readback"), Mapping) else {}
            if readback:
                return readback
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        return baseline

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

        current_discounted = _parse_money(
            (preflight_proof.get("seller_tuple") or {}).get("discountedPrice"),
            "restore_current_discountedPrice",
        )
        steps = self._build_restore_steps(job, current_discounted=current_discounted)
        for step_index, step in enumerate(steps, start=1):
            price = int(step["price"])
            discount = int(step["discount"])
            try:
                fresh_current = self._fetch_current_good(
                    nm_id,
                    job_id=str(job["job_id"]),
                    audit_event="wb_restore_prewrite_readback",
                )
                fresh_quarantine = self._check_quarantine(job)
                fresh_transition = evaluate_wb_price_quarantine_transition(
                    fresh_current.get("discountedPrice"),
                    step["expected_discounted_price"],
                ).to_dict()
            except Exception as exc:
                step["status"] = "prewrite_guard_unavailable"
                step["prewrite_error"] = _safe_text(exc, 160)
                restore_state.setdefault("steps", []).append(step)
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False
            step["prewrite_guard"] = {
                "quarantine_absent": not fresh_quarantine.get("is_quarantined"),
                "quarantine_transition": fresh_transition,
            }
            if fresh_quarantine.get("is_quarantined") or fresh_transition.get("risky"):
                step["status"] = "prewrite_guard_blocked"
                restore_state.setdefault("steps", []).append(step)
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False
            upload = self._upload_price_with_backoff(
                job,
                [{"nmID": nm_id, "price": price, "discount": discount}],
                stage=f"restore:{step.get('kind') or 'step'}:{step_index}",
                before={
                    "price": fresh_current.get("price"),
                    "discount": fresh_current.get("discount"),
                    "discountedPrice": fresh_current.get("discountedPrice"),
                },
                requested={
                    "price": price,
                    "discount": discount,
                    "discountedPrice": step["expected_discounted_price"],
                },
            )
            step["upload"] = upload
            if upload.get("status") == "rate_limited_stop" or upload.get("uploadID") is None:
                self._mark_registry_upload_ambiguous(
                    upload,
                    error_code="wb_upload_unverifiable",
                    error_message="restore upload was not accepted with an uploadID",
                )
                step["status"] = "upload_failed"
                restore_state.setdefault("steps", []).append(step)
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False
            try:
                upload_status = self._wait_upload_final(job, int(upload["uploadID"]))
            except Exception as exc:
                self._mark_registry_upload_ambiguous(
                    upload,
                    error_code="wb_upload_status_unavailable",
                    error_message=str(exc),
                )
                raise
            step["upload_status"] = upload_status
            if upload_status.get("status") != "success":
                upload_outcome = str(upload_status.get("status") or "unknown")
                if upload_outcome in {"partial_error", "all_error", "canceled"}:
                    self._mark_registry_upload_failed(
                        upload,
                        error_code=f"wb_upload_{upload_outcome}",
                        error_message="restore upload reached a non-success final status",
                    )
                else:
                    self._mark_registry_upload_ambiguous(
                        upload,
                        error_code=f"wb_upload_{upload_outcome}",
                        error_message="restore upload final status could not be verified",
                    )
                step["status"] = "upload_not_success"
                restore_state.setdefault("steps", []).append(step)
                job["manual_restore_required"] = True
                job["result_status"] = "manual_restore_required"
                self._save_job(job)
                return False
            expected = _discounted_price(_parse_money(price, "price"), _parse_money(discount, "discount"))
            try:
                readback = self._wait_discounted_readback(
                    job,
                    expected_discounted=expected,
                    expected_price=price,
                    expected_discount=discount,
                )
            except Exception as exc:
                self._mark_registry_upload_ambiguous(
                    upload,
                    error_code="wb_readback_unavailable",
                    error_message=str(exc),
                )
                raise
            step["readback"] = readback
            exact_tuple = bool(
                _optional_int(readback.get("price")) == price
                and _optional_int(readback.get("discount")) == discount
                and _money_exact(readback.get("discountedPrice"), expected)
            )
            if exact_tuple:
                self._confirm_registry_upload(job, upload, readback)
            else:
                self._mark_registry_upload_ambiguous(
                    upload,
                    error_code="wb_readback_mismatch",
                    error_message="restore exact seller price tuple did not match",
                )
            quarantine = self._check_quarantine(job)
            step["quarantine"] = quarantine
            step["status"] = (
                "ok"
                if exact_tuple and not quarantine.get("is_quarantined")
                else "readback_mismatch"
                if not exact_tuple
                else "quarantine_detected"
            )
            restore_state.setdefault("steps", []).append(step)
            self._save_job(job)
            if not exact_tuple or quarantine.get("is_quarantined"):
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
        proof_good = self._fetch_current_good(
            int(job["nmID"]),
            job_id=str(job["job_id"]),
            audit_event=f"wb_{event_prefix}_readback",
        )
        proof_quarantine = self._check_quarantine(job)
        proof = {
            "captured_at": self.timestamp_factory(),
            "price_matches": _optional_int(proof_good.get("price")) == _optional_int(baseline.get("price")),
            "discount_matches": _optional_int(proof_good.get("discount")) == _optional_int(baseline.get("discount")),
            "discountedPrice_matches": _money_exact(
                proof_good.get("discountedPrice"),
                baseline.get("discountedPrice"),
            ),
            "quarantine_absent": not proof_quarantine.get("is_quarantined"),
            "seller_tuple": {
                "price": _optional_int(proof_good.get("price")),
                "discount": _optional_int(proof_good.get("discount")),
                "discountedPrice": _number_or_none(proof_good.get("discountedPrice")),
            },
        }
        proof["proof_status"] = "confirmed" if _restore_proof_ok(proof) else "not_confirmed"
        return proof

    def _build_restore_steps(
        self,
        job: Mapping[str, Any],
        *,
        current_discounted: Decimal | None = None,
    ) -> list[dict[str, Any]]:
        baseline = job["baseline"]
        baseline_discounted = _parse_money(baseline["discountedPrice"], "baseline_discountedPrice")
        baseline_discount = int(baseline["discount"])
        baseline_price = int(baseline["price"])
        current_discounted = current_discounted if current_discounted is not None else self._last_known_discounted(job)
        steps: list[dict[str, Any]] = []
        if current_discounted is not None and current_discounted > baseline_discounted * Decimal("1.25"):
            cursor = current_discounted
            for _attempt in range(32):
                if cursor <= baseline_discounted * Decimal("1.25"):
                    break
                previous = cursor
                target = max(baseline_discounted, (previous * Decimal("0.80")).quantize(MONEY, rounding=ROUND_HALF_UP))
                if target <= baseline_discounted:
                    break
                price, expected = _price_for_discounted(target, baseline_discount)
                if price == baseline_price:
                    break
                if expected >= previous:
                    raise RuntimeError("restore bridge cannot make safe rounded progress")
                steps.append(
                    {
                        "kind": "bridge",
                        "target_discounted_price": _decimal_to_float(target),
                        "price": price,
                        "discount": baseline_discount,
                        "expected_discounted_price": _decimal_to_float(expected),
                        "quarantine_transition": evaluate_wb_price_quarantine_transition(previous, expected).to_dict(),
                    }
                )
                cursor = expected
                if cursor <= baseline_discounted * Decimal("1.25"):
                    break
            else:
                raise RuntimeError("restore bridge exceeded bounded step count")
        final_previous = current_discounted if not steps else _parse_money(
            steps[-1]["expected_discounted_price"],
            "restore_bridge_discountedPrice",
        )
        steps.append(
            {
                "kind": "baseline",
                "target_discounted_price": _decimal_to_float(baseline_discounted),
                "price": baseline_price,
                "discount": baseline_discount,
                "expected_discounted_price": _decimal_to_float(baseline_discounted),
                "quarantine_transition": evaluate_wb_price_quarantine_transition(
                    final_previous or baseline_discounted,
                    baseline_discounted,
                ).to_dict(),
            }
        )
        if any(bool((step.get("quarantine_transition") or {}).get("risky")) for step in steps):
            raise RuntimeError(
                f"restore bridge violates conservative {WB_QUARANTINE_RATIO} quarantine threshold"
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

    def _capture_baseline(self, *, nm_id: int, strict: bool) -> dict[str, Any]:
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
        enrichment = self._load_nomenclature_enrichment().get(nm_id, {})
        baseline = {
            "nmID": nm_id,
            "title": _display_title(enrichment),
            "ourSku": str(enrichment.get("our_sku") or ""),
            "vendorCode": str(good.get("vendorCode") or ""),
            "price": _optional_int(good.get("price")),
            "discount": _optional_int(good.get("discount")),
            "discountedPrice": _number_or_none(good.get("discountedPrice")),
            "quarantine": {"is_quarantined": bool(quarantine_match)},
            "editableSizePrice": bool(good.get("editableSizePrice")),
            "currencyIsoCode4217": str(good.get("currencyIsoCode4217") or "RUB"),
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
            capability_check = getattr(self.buyer_source, "check_spp_capability", None)
            if not callable(capability_check):
                raise RuntimeError("authenticated buyer-price capability check is unavailable")
            payload = dict(capability_check())
        except Exception:
            payload = {
                "status": "probe_error",
                "valid": False,
                "capability": "authenticated_buyer_price",
                "capability_status": "probe_error",
                "capability_valid": False,
                "reason": "buyer_session_probe_failed",
                "diagnostic_category": "application_failure",
                "probe_attempts": 1,
                "probe_retry_attempted": False,
                "checked_at": self.timestamp_factory(),
            }
        payload.setdefault("status", "probe_error")
        payload.setdefault("valid", payload.get("status") == "valid")
        payload.setdefault("capability", "authenticated_buyer_price")
        payload.setdefault("capability_status", "unavailable")
        payload.setdefault("capability_valid", False)
        return payload

    def _require_buyer_session(self) -> dict[str, Any]:
        session = self._safe_buyer_session_preflight()
        if (
            session.get("status") != "valid"
            or session.get("valid") is not True
            or session.get("capability_valid") is not True
        ):
            logged_out = session.get("status") in {"missing", "expired", "invalid", "logged_out"}
            raise WbSppTesterError(
                "Бот покупателя разлогинен. Ни одна цена не изменена. Восстановите сессию в настройках."
                if logged_out
                else "Бот покупателя не готов к чтению авторизованной цены. Ни одна цена не изменена.",
                http_status=422,
                payload={
                    "reason": "buyer_logged_out" if logged_out else "buyer_capability_invalid",
                    "buyer_session": session,
                    "action": "Откройте Настройки → Источники и сессии",
                },
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

    def _parse_start_input(self, payload: Mapping[str, Any]) -> tuple[int, list[Decimal]]:
        nm_id = _as_positive_int(payload.get("nmID") or payload.get("nm_id"), "nmID")
        raw_prices = payload.get("prices") if isinstance(payload.get("prices"), list) else payload.get("target_prices")
        if not isinstance(raw_prices, list):
            raise WbSppTesterError("prices must be an ordered list", http_status=400)
        if len(raw_prices) < SPP_TEST_PRICE_COUNT_MIN or len(raw_prices) > SPP_TEST_PRICE_COUNT_MAX:
            raise WbSppTesterError(
                f"prices must contain between {SPP_TEST_PRICE_COUNT_MIN} and {SPP_TEST_PRICE_COUNT_MAX} values",
                http_status=422,
            )
        declared_count = _optional_int(payload.get("price_count"))
        if declared_count is not None and declared_count != len(raw_prices):
            raise WbSppTesterError("price_count must match prices length", http_status=422)
        prices = [_parse_input_money(value, f"prices[{index}]") for index, value in enumerate(raw_prices)]
        if any(value <= 0 for value in prices):
            raise WbSppTesterError("every price must be greater than zero", http_status=422)
        return nm_id, prices

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
        if reconcile:
            self._reconcile_current_job(caller_holds_execution_lock=caller_holds_execution_lock)
        job = self._load_current_job_payload()
        if not job:
            return None
        status = str(job.get("status") or "")
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        if status in SPP_TEST_ACTIVE_STATUSES or status == "manual_restore_required":
            return self._job_summary(job)
        if status == "failed" and not restore.get("restored"):
            return self._job_summary(job)
        return None

    def _reconcile_current_job(self, *, caller_holds_execution_lock: bool = False) -> dict[str, Any] | None:
        job = self._load_current_job_payload()
        if not job:
            return None
        status = str(job.get("status") or "")
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        if not baseline:
            return job
        if not caller_holds_execution_lock and self._execution_lock_is_held():
            lifecycle = job.get("lifecycle_diagnostics") if isinstance(job.get("lifecycle_diagnostics"), Mapping) else {}
            if status in SPP_TEST_ACTIVE_STATUSES and lifecycle.get("classification") != "live":
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
            was_unfinished = status in SPP_TEST_ACTIVE_STATUSES or status == "manual_restore_required" or (
                status == "failed" and not restore.get("restored")
            )
            if was_unfinished:
                mutable["status"] = "interrupted_restored"
            mutable["result_status"] = _restored_result_status(mutable.get("result_status"))
            mutable["manual_restore_required"] = False
            mutable["finished_at"] = str(mutable.get("finished_at") or self.timestamp_factory())
            self._set_lifecycle(
                mutable,
                classification="stale_orphan_restored_confirmed" if was_unfinished else "terminal_restored_reconciled",
                phase=str(mutable.get("status") or "interrupted_restored"),
                reconciled_at=self.timestamp_factory(),
                restore_proof=proof,
            )
            self._append_timeline(
                mutable,
                str(mutable.get("status") or "interrupted_restored"),
                "fresh_live_baseline_readback_confirmed",
            )
            audit_event = "orphan_reconcile_restored" if was_unfinished else "terminal_restore_reconciled"
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

    def _job_public_payload(self, job: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        proof = restore.get("proof") if isinstance(restore.get("proof"), Mapping) else {}
        return _sanitize_public_payload(
            {
                "job_id": job.get("job_id"),
                "created_at": job.get("created_at"),
                "updated_at": job.get("updated_at"),
                "finished_at": job.get("finished_at"),
                "status": job.get("status"),
                "result_status": job.get("result_status"),
                "nmID": job.get("nmID"),
                "target_prices": _target_prices_from_job(job),
                "measurements": [_measurement_summary(row) for row in _measurement_rows(job)],
                "baseline": {
                    "price": baseline.get("price"),
                    "discount": baseline.get("discount"),
                    "discountedPrice": baseline.get("discountedPrice"),
                    "captured_at": baseline.get("captured_at"),
                },
                "restore": {
                    "restored": bool(restore.get("restored")),
                    "proof_status": proof.get("proof_status"),
                    "price_matches": proof.get("price_matches"),
                    "discount_matches": proof.get("discount_matches"),
                    "discountedPrice_matches": proof.get("discountedPrice_matches"),
                    "quarantine_absent": proof.get("quarantine_absent"),
                    "seller_tuple": proof.get("seller_tuple"),
                },
                "manual_restore_required": bool(job.get("manual_restore_required")),
                "error": _safe_text(job.get("error"), 500),
            },
            details=False,
        )

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

    def _load_latest_job_payload(self) -> dict[str, Any] | None:
        jobs = self._load_history_jobs()
        return max(jobs, key=_history_sort_key) if jobs else None

    def _history_summary(self, job: Mapping[str, Any]) -> dict[str, Any]:
        baseline = job.get("baseline") if isinstance(job.get("baseline"), Mapping) else {}
        created_at = str(job.get("created_at") or "")
        finished_at = str(job.get("finished_at") or job.get("updated_at") or "")
        return {
            "job_id": str(job.get("job_id") or ""),
            "created_at": created_at,
            "finished_at": finished_at,
            "duration_seconds": _duration_seconds(created_at, finished_at),
            "status": str(job.get("status") or ""),
            "result_status": str(job.get("result_status") or ""),
            "nmID": _optional_int(job.get("nmID")),
            "title": _safe_text(baseline.get("title"), 300),
            "ourSku": _safe_text(baseline.get("ourSku"), 160),
            "vendorCode": _safe_text(baseline.get("vendorCode"), 300),
            "results": [_measurement_summary(row) for row in _measurement_rows(job)],
            "manual_restore_required": bool(job.get("manual_restore_required")),
            "restore_restored": bool((job.get("restore") or {}).get("restored")) if isinstance(job.get("restore"), Mapping) else False,
        }

    def _load_log_events(self, *, job_id: str = "") -> list[dict[str, str]]:
        if not self._audit_path.exists():
            return []
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()[-1000:]
        except OSError:
            return []
        parsed_events: list[Mapping[str, Any]] = []
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping):
                continue
            parsed_events.append(event)
        selected_job_id = str(job_id or "").strip()
        if not JOB_ID_RE.fullmatch(selected_job_id):
            selected_job_id = next(
                (
                    str(event.get("job_id") or "")
                    for event in reversed(parsed_events)
                    if JOB_ID_RE.fullmatch(str(event.get("job_id") or ""))
                ),
                "",
            )
        events: list[dict[str, str]] = []
        for event in parsed_events:
            if selected_job_id and str(event.get("job_id") or "") != selected_job_id:
                continue
            event_type = str(event.get("event_type") or "")
            label = TECH_LOG_LABELS.get(event_type)
            if not label:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            events.append(
                {
                    "time": _safe_text(event.get("timestamp"), 80),
                    "stage": label,
                    "message": _technical_log_message(event_type, payload),
                }
            )
        return events[-SPP_TEST_LOG_LIMIT:]

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
            "payload": _sanitize_public_payload(_json_safe(payload), details=False),
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
        status = str(job.get("status") or "")
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        proof = restore.get("proof") if isinstance(restore.get("proof"), Mapping) else {}
        if (
            status not in SPP_TEST_ACTIVE_STATUSES
            and status != "manual_restore_required"
            and bool(restore.get("restored"))
            and _restore_proof_ok(proof)
        ):
            if self._clear_current_job_pointer(str(job.get("job_id") or "")):
                self._append_audit(
                    str(job.get("job_id") or ""),
                    "current_job_cleared",
                    {"status": status, "reason": "fresh_seller_baseline_proof"},
                )
            return
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

    def _clear_current_job_pointer(self, job_id: str) -> bool:
        normalized = str(job_id or "").strip()
        if not JOB_ID_RE.fullmatch(normalized):
            return False
        with self._state_lock:
            if not self._current_job_path.exists():
                return False
            try:
                current = json.loads(self._current_job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(current, Mapping) or str(current.get("job_id") or "") != normalized:
                return False
            try:
                self._current_job_path.unlink()
            except FileNotFoundError:
                return False
        return True

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


def _measurement_rows(job: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = job.get("measurements")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _target_prices_from_job(job: Mapping[str, Any]) -> list[float]:
    input_payload = job.get("input") if isinstance(job.get("input"), Mapping) else {}
    raw = input_payload.get("target_prices")
    if isinstance(raw, list):
        return [value for value in (_number_or_none(item) for item in raw) if value is not None][
            :SPP_TEST_PRICE_COUNT_MAX
        ]
    plan = job.get("plan") if isinstance(job.get("plan"), Mapping) else {}
    initial = plan.get("initial_points") if isinstance(plan.get("initial_points"), list) else []
    legacy = [
        _number_or_none(row.get("target_discounted_price"))
        for row in initial
        if isinstance(row, Mapping)
    ]
    legacy = [value for value in legacy if value is not None]
    if legacy:
        return legacy[:SPP_TEST_PRICE_COUNT_MAX]
    return [
        value
        for value in (
            _number_or_none(row.get("target_discounted_price"))
            for row in _measurement_rows(job)
        )
        if value is not None
    ][:SPP_TEST_PRICE_COUNT_MAX]


def _measurement_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_price": _first_number(row, "target_discounted_price", "target_price"),
        "seller_discounted_price": _first_number(
            row,
            "actual_wb_discounted_price",
            "seller_discounted_price",
        ),
        "buyer_price": _first_number(row, "authenticated_buyer_price", "buyer_price"),
        "spp": _first_number(row, "spp_proxy", "authenticated_spp_proxy"),
        "status": _safe_text(row.get("status"), 80) or "unknown",
        "note": _safe_text(row.get("note"), 300),
    }


def _technical_log_message(event_type: str, payload: Mapping[str, Any]) -> str:
    if event_type == "start_preflight":
        return f"nmID {payload.get('nmID') or '—'}, цен: {payload.get('price_count') or '—'}"
    if event_type == "start_preflight_failed":
        return _safe_text(payload.get("reason"), 160) or "Бот покупателя не готов"
    if event_type == "baseline_failed":
        return _safe_text(payload.get("reason"), 160) or "Исходная seller tuple не подтверждена"
    if event_type == "sequence_quarantine_blocked":
        risks = payload.get("risky_transitions") if isinstance(payload.get("risky_transitions"), list) else []
        transition = risks[0] if risks and isinstance(risks[0], Mapping) else {}
        risk = transition.get("quarantine_transition") if isinstance(transition.get("quarantine_transition"), Mapping) else {}
        return f"Старт заблокирован: снижение {_number_or_none(risk.get('drop_percent')) or '≥33,3'}%"
    if event_type == "job_start":
        return "Исходная seller tuple зафиксирована"
    if event_type == "measurement_started":
        return f"Цель: {_number_or_none(payload.get('target_discounted_price')) or '—'} ₽"
    if event_type == "measurement_buyer_session_blocked":
        return "Запись отменена: capability не готова"
    if event_type == "measurement_prewrite_guard_blocked":
        guard = (payload.get("evidence") or {}).get("prewrite_guard") if isinstance(payload.get("evidence"), Mapping) else {}
        return f"Запись отменена: {_safe_text((guard or {}).get('status'), 80) or 'fresh guard blocked'}"
    if event_type == "wb_upload_task":
        return "Задача изменения цены создана"
    if event_type == "wb_upload_task_error":
        return "WB отклонил изменение цены"
    if event_type == "wb_prices_readback":
        return "Фактическая seller price получена"
    if event_type == "authenticated_buyer_price_read":
        return f"Статус: {_safe_text(payload.get('status'), 80) or 'unknown'}"
    if event_type == "measurement_finished":
        return f"Статус: {_safe_text(payload.get('status'), 80) or 'unknown'}"
    if event_type == "measurement_sequence_stopped":
        return f"Статус: {_safe_text(payload.get('status'), 80) or 'error'}; остальные цены не запущены"
    if event_type.startswith("restore_"):
        return f"Restore: {_safe_text(payload.get('proof_status'), 80) or ('подтверждён' if _restore_proof_ok(payload) else 'требует проверки')}"
    if event_type == "job_finish":
        return f"{_safe_text(payload.get('status'), 80) or 'finished'}; restore={'да' if payload.get('restored') else 'нет'}"
    if event_type.startswith("orphan_reconcile_"):
        return "Свежий seller readback сверён"
    if event_type == "current_job_cleared":
        return "Execution lock освобождён после restore proof"
    return "Событие выполнено"


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


def _restore_proof_ok(proof: Mapping[str, Any]) -> bool:
    return bool(
        proof.get("price_matches")
        and proof.get("discount_matches")
        and proof.get("discountedPrice_matches")
        and proof.get("quarantine_absent")
    )


def _restored_result_status(value: Any) -> str:
    normalized = str(value or "").strip()
    return "inconclusive" if normalized in {"", "manual_restore_required"} else normalized


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
        first_buyer_poll_delay_seconds=_env_int("WB_SPP_TEST_FIRST_BUYER_POLL_DELAY_SECONDS", 60),
        buyer_poll_gap_seconds=_env_int("WB_SPP_TEST_BUYER_POLL_GAP_SECONDS", 90),
        upload_status_poll_seconds=_env_int("WB_SPP_TEST_UPLOAD_STATUS_POLL_SECONDS", 20),
        upload_status_max_polls=_env_int("WB_SPP_TEST_UPLOAD_STATUS_MAX_POLLS", 24),
        readback_poll_seconds=_env_int("WB_SPP_TEST_READBACK_POLL_SECONDS", 20),
        readback_max_polls=_env_int("WB_SPP_TEST_READBACK_MAX_POLLS", 12),
        rate_limit_min_cooldown_seconds=_env_int("WB_SPP_TEST_429_COOLDOWN_SECONDS", 900),
        active_lock_ttl_seconds=_env_int("WB_SPP_TEST_LOCK_TTL_SECONDS", 1800),
    )


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


def _spp_proxy(seller_discounted: Decimal, authenticated_buyer_price: Decimal) -> Decimal:
    if seller_discounted <= 0 or authenticated_buyer_price <= 0:
        raise WbSppTesterError("cannot calculate SPP for non-positive prices", http_status=502)
    return ((seller_discounted - authenticated_buyer_price) / seller_discounted).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )


def _stable_authenticated_price(reads: Sequence[Mapping[str, Any]]) -> float | None:
    if len(reads) < 2:
        return None
    left, right = reads[-2], reads[-1]
    left_price = _number_or_none(left.get("authenticated_buyer_price"))
    right_price = _number_or_none(right.get("authenticated_buyer_price"))
    if left_price is None or right_price is None or not _money_exact(left_price, right_price):
        return None
    left_fingerprint = str(left.get("session_fingerprint") or "")
    right_fingerprint = str(right.get("session_fingerprint") or "")
    if left_fingerprint or right_fingerprint:
        if not left_fingerprint or left_fingerprint != right_fingerprint:
            return None
    elif not (
        bool(left.get("authenticated_session_proof"))
        and bool(right.get("authenticated_session_proof"))
        and bool(left.get("persistent_profile"))
        and bool(right.get("persistent_profile"))
    ):
        return None
    if _destination_signature(left) != _destination_signature(right):
        return None
    if str(left.get("payment_context") or "unknown/mixed") != str(
        right.get("payment_context") or "unknown/mixed"
    ):
        return None
    return right_price


def _destination_signature(value: Mapping[str, Any]) -> tuple[str, str, str, str]:
    context = value.get("destination_context") if isinstance(value.get("destination_context"), Mapping) else {}
    return tuple(str(context.get(key) or "").strip().lower() for key in ("dest", "regions", "currency", "locale"))
def _display_title(enrichment: Mapping[str, Any]) -> str:
    return str(
        enrichment.get("nomenclature_name")
        or enrichment.get("wb_title")
        or enrichment.get("display_name")
        or ""
    )


def _money_close(left: Decimal, right: Decimal, tolerance: Decimal) -> bool:
    return abs(left - right) <= tolerance


def _parse_input_money(value: Any, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise WbSppTesterError(f"{field_name} is required", http_status=422)
    try:
        parsed = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, AttributeError) as exc:
        raise WbSppTesterError(f"{field_name} must be a valid money value", http_status=422) from exc
    if not parsed.is_finite() or parsed.as_tuple().exponent < -2:
        raise WbSppTesterError(f"{field_name} must have at most two decimal places", http_status=422)
    return parsed.quantize(MONEY)


def _parse_money(value: Any, field_name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise WbSppTesterError(f"{field_name} must be numeric", http_status=400)
    try:
        return Decimal(str(value).strip().replace(",", ".")).quantize(MONEY, rounding=ROUND_HALF_UP)
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


def _first_number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in row:
            continue
        number = _number_or_none(row.get(key))
        if number is not None:
            return number
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


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_to_float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
