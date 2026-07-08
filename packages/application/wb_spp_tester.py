"""Bounded application block for live WB SPP proxy threshold tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
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
from packages.contracts.spp_proxy_block import SppProxyRequest
from packages.contracts.wb_spp_tester import (
    SPP_TEST_ACTIVE_STATUSES,
    SPP_TEST_CONTRACT_PREFIX,
    SPP_TEST_DEFAULT_MAX_MEASUREMENTS,
    SPP_TEST_DEFAULT_PRECISION_RUB,
    SPP_TEST_FINAL_STATUSES,
    SPP_TEST_MAX_MEASUREMENTS_MAX,
    SPP_TEST_MAX_MEASUREMENTS_MIN,
    SPP_TEST_MODE_SAFE_SLOW,
    SppTestPlan,
    SppTestPointPlan,
)


BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
MONEY = Decimal("0.01")


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


class WbSppTesterBlock:
    """Server-owned live SPP tester with guarded writes and staged restore."""

    def __init__(
        self,
        *,
        runtime: Any,
        runtime_dir: Path,
        prices_source: WbPricesManagementSource | None = None,
        public_source: WbSppPublicBuyerPriceSource | None = None,
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
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self.timestamp_factory = timestamp_factory or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or time.sleep
        self.safety = safety_config or _load_safety_config()
        self.cadence = cadence_config or _load_cadence_config()
        self._state_dir = self.runtime_dir / "sheet_vitrina_v1_prices" / "spp_tests"
        self._jobs_dir = self._state_dir / "jobs"
        self._current_job_path = self._state_dir / "current_job.json"
        self._audit_path = self._state_dir / "audit.jsonl"
        self._threads: dict[str, threading.Thread] = {}
        self._thread_lock = threading.RLock()

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
        }

    def start(self, payload: Mapping[str, Any], *, actor: str = "") -> dict[str, Any]:
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
        blocking = self._blocking_current_job()
        if blocking is not None:
            raise WbSppTesterError(
                "another SPP test job is active or requires restore",
                http_status=409,
                payload={"active_job": blocking},
            )
        nm_id, range_min, range_max, precision, max_measurements = self._parse_plan_input(payload)
        baseline = self._capture_baseline(nm_id=nm_id, strict=True)
        plan_payload = self.build_plan(payload)["plan"]
        job_id = uuid4().hex
        job = {
            "job_id": job_id,
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_job",
            "created_at": self.timestamp_factory(),
            "updated_at": self.timestamp_factory(),
            "actor": actor,
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
            "plan": plan_payload,
            "measurements": [],
            "thresholds": [],
            "timeline": [],
            "restore": {"required": True, "restored": False, "proof": None, "steps": []},
            "audit_path": str(self._audit_path),
            "manual_restore_required": False,
            "error": "",
        }
        self._append_timeline(job, "planning", "job_created")
        self._save_job(job)
        self._write_current_job(job)
        self._append_audit(job_id, "job_start", {"actor": actor, "input": job["input"], "baseline": baseline})
        if self.cadence.run_async:
            self._start_background_job(job_id)
        else:
            self._run_job(job_id)
        current = self._load_job(job_id)
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_start",
            "job": self._job_public_payload(current),
        }

    def status(self, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        requested_job_id = str(_single_param(params.get("job_id") or params.get("jobID")) or "").strip()
        job = self._load_job(requested_job_id) if requested_job_id else self._load_current_job_payload()
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_status",
            "generated_at": self.timestamp_factory(),
            "active_job": self._current_job_summary(),
            "job": self._job_public_payload(job) if job else None,
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
        self._append_audit(str(job["job_id"]), "emergency_restore_requested", {"actor": actor})
        restored = self._restore_baseline(job, reason="emergency_restore")
        job = self._load_job(str(job["job_id"]))
        return {
            "contract_name": f"{SPP_TEST_CONTRACT_PREFIX}_restore",
            "status": "restored" if restored else "manual_restore_required",
            "job": self._job_public_payload(job),
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
                return
            result_status = str(job.get("result_status") or "inconclusive")
            final_status = "complete" if result_status not in {"manual_restore_required"} else result_status
            job["status"] = final_status
            job["updated_at"] = self.timestamp_factory()
            self._append_timeline(job, final_status, result_status)
            self._save_job(job)
            self._write_current_job(job)
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
            self._save_job(job)
            self._write_current_job(job)

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
            if point.get("status") == "rate_limited_stop":
                stop_after_restore = True
                break
            if len(job["measurements"]) < max_measurements and route:
                self._set_job_status(job, "cooldown", "between_measurements")
                self.sleep(self.cadence.measurement_upload_cooldown_seconds)
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
            self.sleep(self.cadence.measurement_upload_cooldown_seconds)
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
            "public_buyer_price": None,
            "spp_proxy": None,
            "delta_vs_previous_high_confidence": None,
            "confidence": "low",
            "uploadID": None,
            "status": "started",
            "note": "",
            "evidence": {},
        }
        self._append_audit(str(job["job_id"]), "measurement_started", measurement)
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

        public_proof = self._poll_public_stable(job)
        measurement["evidence"]["public_proof"] = public_proof
        public_price = _number_or_none(public_proof.get("public_buyer_price"))
        measurement["public_buyer_price"] = public_price
        if public_price is None or public_proof.get("stable") is not True:
            measurement["status"] = str(public_proof.get("status") or "public_unstable")
            measurement["note"] = "public buyer price did not reach stable proof"
            return measurement

        spp = _spp_proxy(_parse_money(actual_discounted, "actual_discounted"), _parse_money(public_price, "public_price"))
        measurement["spp_proxy"] = _decimal_to_float(spp)
        previous = _previous_high_confidence_point(job.get("measurements", []))
        if previous is not None:
            prev_spp = _number_or_none(previous.get("spp_proxy"))
            if prev_spp is not None:
                delta = abs(spp - _parse_ratio(prev_spp, "prev_spp"))
                measurement["delta_vs_previous_high_confidence"] = _decimal_to_float(delta)
            if _looks_like_stale_public_price(measurement, previous, precision=_parse_money(job["input"]["precision_rub"], "precision")):
                measurement["status"] = "stale_public_price"
                measurement["confidence"] = "low"
                measurement["note"] = "public buyer price stayed identical after material seller discounted price change"
                self._append_audit(str(job["job_id"]), "measurement_finished", measurement)
                return measurement
        measurement["status"] = "ok"
        measurement["confidence"] = "high"
        measurement["note"] = "upload/readback/public/quarantine proof complete"
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
            self.sleep(self.cadence.upload_status_poll_seconds)
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
            self.sleep(self.cadence.readback_poll_seconds)
        return last

    def _poll_public_stable(self, job: Mapping[str, Any]) -> dict[str, Any]:
        nm_id = int(job["nmID"])
        reads: list[dict[str, Any]] = []
        self.sleep(self.cadence.first_public_poll_delay_seconds)
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
                self.sleep(self.cadence.extended_public_poll_gap_seconds)
            else:
                self.sleep(self.cadence.public_poll_gap_seconds)
        return {"status": "public_unstable", "stable": False, "reads": reads, "public_buyer_price": None}

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
        self.sleep(cooldown)
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
        if (job.get("restore") or {}).get("restored") and (job.get("restore") or {}).get("proof"):
            return True
        nm_id = int(job["nmID"])
        self._set_job_status(job, "restoring", reason)
        steps = self._build_restore_steps(job)
        restore_state = job.setdefault("restore", {"required": True, "restored": False, "proof": None, "steps": []})
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

        proof_good = self._fetch_current_good(nm_id, job_id=str(job["job_id"]), audit_event="wb_restore_final_readback")
        proof_quarantine = self._check_quarantine(job)
        proof_public = dict(self.public_source.fetch_public_buyer_price(nm_id))
        self._append_audit(str(job["job_id"]), "public_restore_final_read", proof_public)
        proof = {
            "price_matches": _optional_int(proof_good.get("price")) == _optional_int(baseline.get("price")),
            "discount_matches": _optional_int(proof_good.get("discount")) == _optional_int(baseline.get("discount")),
            "discountedPrice_matches": _money_close(
                _parse_money(proof_good.get("discountedPrice"), "discountedPrice"),
                _parse_money(baseline.get("discountedPrice"), "baseline_discountedPrice"),
                Decimal("1.00"),
            ),
            "quarantine_absent": not proof_quarantine.get("is_quarantined"),
            "public_buyer_price": proof_public.get("public_buyer_price"),
            "public_status": proof_public.get("status"),
            "wb_readback": proof_good,
            "quarantine": proof_quarantine,
        }
        public_price = _number_or_none(proof_public.get("public_buyer_price"))
        if public_price is not None:
            proof["spp_proxy"] = _decimal_to_float(
                _spp_proxy(
                    _parse_money(baseline.get("discountedPrice"), "baseline_discountedPrice"),
                    _parse_money(public_price, "public_buyer_price"),
                )
            )
        ok = bool(
            proof["price_matches"]
            and proof["discount_matches"]
            and proof["discountedPrice_matches"]
            and proof["quarantine_absent"]
        )
        restore_state["proof"] = proof
        restore_state["restored"] = ok
        job["manual_restore_required"] = not ok
        if not ok:
            job["result_status"] = "manual_restore_required"
        self._append_audit(str(job["job_id"]), "restore_final_proof", proof)
        self._save_job(job)
        return ok

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
        public = dict(self.public_source.fetch_public_buyer_price(nm_id))
        discounted = _number_or_none(good.get("discountedPrice"))
        public_price = _number_or_none(public.get("public_buyer_price"))
        spp_proxy = (
            _decimal_to_float(_spp_proxy(_parse_money(discounted, "discountedPrice"), _parse_money(public_price, "public_buyer_price")))
            if discounted is not None and public_price is not None
            else None
        )
        enrichment = self._load_nomenclature_enrichment().get(nm_id, {})
        baseline = {
            "nmID": nm_id,
            "title": _display_title(enrichment),
            "ourSku": str(enrichment.get("our_sku") or ""),
            "vendorCode": str(good.get("vendorCode") or ""),
            "price": _optional_int(good.get("price")),
            "discount": _optional_int(good.get("discount")),
            "discountedPrice": discounted,
            "publicBuyerPrice": public_price,
            "sppProxy": spp_proxy,
            "sppProxyLabel": _format_percent_label(spp_proxy),
            "quarantine": {
                "is_quarantined": bool(quarantine_match),
                "rows": quarantine_match,
            },
            "editableSizePrice": bool(good.get("editableSizePrice")),
            "editableSizePriceLabel": "размерные цены" if bool(good.get("editableSizePrice")) else "обычная цена",
            "currencyIsoCode4217": str(good.get("currencyIsoCode4217") or "RUB"),
            "public_read": public,
            "captured_at": self.timestamp_factory(),
        }
        blockers: list[str] = []
        if baseline["editableSizePrice"]:
            blockers.append("editableSizePrice=true")
        if baseline["quarantine"]["is_quarantined"]:
            blockers.append("price_quarantine_present")
        if baseline["price"] is None or baseline["discount"] is None or baseline["discountedPrice"] is None:
            blockers.append("wb_price_baseline_incomplete")
        baseline["can_start"] = not blockers
        baseline["blockers"] = blockers
        if strict and blockers:
            raise WbSppTesterError(
                "SPP test baseline is not safe to start",
                http_status=422,
                payload={"baseline": baseline, "blockers": blockers},
            )
        return baseline

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

    def _blocking_current_job(self) -> dict[str, Any] | None:
        job = self._load_current_job_payload()
        if not job:
            return None
        status = str(job.get("status") or "")
        restore = job.get("restore") if isinstance(job.get("restore"), Mapping) else {}
        if status in SPP_TEST_ACTIVE_STATUSES:
            return self._job_summary(job)
        if status in {"manual_restore_required"}:
            return self._job_summary(job)
        if status == "failed" and not restore.get("restored"):
            return self._job_summary(job)
        return None

    def _current_job_summary(self) -> dict[str, Any] | None:
        job = self._load_current_job_payload()
        return self._job_summary(job) if job else None

    def _job_summary(self, job: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "job_id": str(job.get("job_id") or ""),
            "status": str(job.get("status") or ""),
            "result_status": str(job.get("result_status") or ""),
            "nmID": _optional_int(job.get("nmID")),
            "updated_at": str(job.get("updated_at") or ""),
            "manual_restore_required": bool(job.get("manual_restore_required")),
            "restore_restored": bool((job.get("restore") or {}).get("restored")) if isinstance(job.get("restore"), Mapping) else False,
        }

    def _job_public_payload(self, job: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        return {
            key: job.get(key)
            for key in (
                "job_id",
                "created_at",
                "updated_at",
                "actor",
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
                "manual_restore_required",
                "error",
            )
        }

    def _start_background_job(self, job_id: str) -> None:
        with self._thread_lock:
            existing = self._threads.get(job_id)
            if existing and existing.is_alive():
                return
            thread = threading.Thread(target=self._run_job, args=(job_id,), daemon=True)
            self._threads[job_id] = thread
            thread.start()

    def _set_job_status(self, job: dict[str, Any], status: str, note: str) -> None:
        job["status"] = status
        job["updated_at"] = self.timestamp_factory()
        self._append_timeline(job, status, note)
        self._save_job(job)
        self._write_current_job(job)

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
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")

    def _job_path(self, job_id: str) -> Path:
        return self._jobs_dir / f"{job_id}.json"

    def _save_job(self, job: Mapping[str, Any]) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)
        job_id = str(job.get("job_id") or "").strip()
        if not job_id:
            raise WbSppTesterError("job_id is missing", http_status=500)
        self._job_path(job_id).write_text(
            json.dumps(_json_safe(job), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _load_job(self, job_id: str) -> dict[str, Any] | None:
        normalized = str(job_id or "").strip()
        if not normalized or "/" in normalized or "." in normalized:
            return None
        path = self._job_path(normalized)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None

    def _write_current_job(self, job: Mapping[str, Any]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        current = {
            "job_id": str(job.get("job_id") or ""),
            "status": str(job.get("status") or ""),
            "heartbeat_at": self.timestamp_factory(),
            "expires_at_epoch": int(time.time()) + int(self.cadence.active_lock_ttl_seconds),
        }
        self._current_job_path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

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
    current_public = _number_or_none(current.get("public_buyer_price"))
    previous_public = _number_or_none(previous.get("public_buyer_price"))
    current_discounted = _number_or_none(current.get("actual_wb_discounted_price"))
    previous_discounted = _number_or_none(previous.get("actual_wb_discounted_price"))
    current_spp = _number_or_none(current.get("spp_proxy"))
    previous_spp = _number_or_none(previous.get("spp_proxy"))
    if None in {current_public, previous_public, current_discounted, previous_discounted, current_spp, previous_spp}:
        return False
    discounted_delta = abs(_parse_money(current_discounted, "current_discounted") - _parse_money(previous_discounted, "previous_discounted"))
    spp_delta = abs(_parse_ratio(current_spp, "current_spp") - _parse_ratio(previous_spp, "previous_spp"))
    return current_public == previous_public and discounted_delta > precision and spp_delta >= Decimal("0.015")


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
