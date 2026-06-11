"""Runtime-managed auto-refresh schedules for sheet_vitrina_v1 web vitrina."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
import os
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from packages.business_time import CANONICAL_BUSINESS_TIMEZONE_NAME, DAILY_REFRESH_BUSINESS_HOURS

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback for local development only.
    fcntl = None  # type: ignore[assignment]


CONTRACT_NAME = "sheet_vitrina_v1_auto_refresh_schedules"
CONTRACT_VERSION = "v1"
DEFAULT_STATE_FILENAME = "sheet_vitrina_v1_auto_refresh_schedules.json"
DEFAULT_TIMEZONE = CANONICAL_BUSINESS_TIMEZONE_NAME
DEFAULT_TIMER_NAME = "wb-core-sheet-vitrina-refresh.timer"
DEFAULT_TRIGGER_NAME = "runtime_auto_refresh_schedule"
DEFAULT_SCHEDULE_SOURCE = "runtime_json"
DEFAULT_SCHEDULE_MODE = "runtime_managed_json_schedule"
DEFAULT_SYSTEMD_ONCALENDAR = "*-*-* *:00,10,20,30,40,50:00"
SCHEDULE_POLICY_MODE_MANUAL = "manual"
SCHEDULE_POLICY_MODE_INTERVAL = "interval"
DEFAULT_INTERVAL_HOURS = 4
ALLOWED_INTERVAL_HOURS = (3, 4, 6)
INTERVAL_WINDOW_START_HHMM = "10:00"
INTERVAL_WINDOW_END_HHMM = "22:00"
MAX_INTERVAL_RUNS_PER_DAY = 6
SUCCESS_STATUSES = {"success"}
WARNING_STATUSES = {"warning", "no_due", "skipped"}
EDITABLE_SCHEDULE_FIELDS = {"enabled", "local_time_hhmm", "time", "timezone"}


class SheetVitrinaV1AutoRefreshSchedulesError(RuntimeError):
    pass


class SheetVitrinaV1AutoRefreshSchedulesBlock:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.path = runtime_dir / DEFAULT_STATE_FILENAME
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def build_payload(self, *, auto_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = self._read()
        schedule_policy = payload.get("schedule_policy") if isinstance(payload.get("schedule_policy"), Mapping) else _default_schedule_policy()
        schedules = payload.get("schedules") if isinstance(payload.get("schedules"), list) else []
        public_schedules = [_public_schedule(schedule, now=self.now_factory()) for schedule in schedules]
        summary = _summarize(public_schedules, auto_context=auto_context)
        interval_hours = int(schedule_policy.get("interval_hours") or DEFAULT_INTERVAL_HOURS)
        interval_preview_slots = _interval_preview_slots(interval_hours)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ok",
            "schedule_mode": DEFAULT_SCHEDULE_MODE,
            "schedule_mode_type": str(schedule_policy.get("mode") or SCHEDULE_POLICY_MODE_MANUAL),
            "schedule_source": DEFAULT_SCHEDULE_SOURCE,
            "schedule_policy": dict(schedule_policy),
            "timezone": DEFAULT_TIMEZONE,
            "timezone_label": "Asia/Yekaterinburg",
            "can_edit_runtime": True,
            "save_supported": True,
            "run_now_supported": True,
            "operator_approval_required": False,
            "systemd_timer_name": DEFAULT_TIMER_NAME,
            "systemd_oncalendar": DEFAULT_SYSTEMD_ONCALENDAR,
            "message": _operator_message(schedule_policy),
            "interval_options": _interval_options(),
            "interval_preview_slots": interval_preview_slots,
            "effective_schedules": public_schedules,
            "schedules": public_schedules,
            **summary,
        }

    def save_schedules(self, payload: Mapping[str, Any], *, auto_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        schedule_policy = _normalize_schedule_policy(payload.get("schedule_policy"), strict=True)
        now = _iso_now(self.now_factory)
        with self._lock:
            with self._file_lock_unlocked():
                current = self._read_unlocked()
                existing_by_id = _schedule_lifecycle_lookup(current.get("schedules", []))
                if schedule_policy["mode"] == SCHEDULE_POLICY_MODE_INTERVAL:
                    normalized = _materialize_interval_schedules(
                        schedule_policy,
                        existing_by_id=existing_by_id,
                        now=now,
                        now_factory=self.now_factory,
                    )
                else:
                    raw_schedules = payload.get("schedules")
                    if not isinstance(raw_schedules, list):
                        raise ValueError("schedules must be a JSON array")
                    normalized = []
                    for index, raw in enumerate(raw_schedules, start=1):
                        if not isinstance(raw, Mapping):
                            raise ValueError("each schedule must be an object")
                        schedule_id = _safe_text(raw.get("id"), 120) or f"custom_{uuid4().hex[:12]}"
                        existing = existing_by_id.get(schedule_id)
                        merged = _merge_editable_schedule(existing, raw, schedule_id=schedule_id)
                        normalized.append(_normalize_schedule(merged, index=index, now=now, now_factory=self.now_factory))
                    _validate_schedule_set(normalized)
                current["schedule_policy"] = schedule_policy
                current["schedules"] = normalized
                self._write_unlocked(current)
        return self.build_payload(auto_context=auto_context)

    def due_schedules(self, *, now: datetime | None = None) -> list[tuple[dict[str, Any], str]]:
        instant = now or self.now_factory()
        due: list[tuple[dict[str, Any], str]] = []
        for schedule in self._read().get("schedules", []):
            if not isinstance(schedule, Mapping) or not bool(schedule.get("enabled", True)):
                continue
            due_at = _last_due_at(schedule, instant)
            if due_at is None:
                continue
            due_iso = _iso_datetime(due_at)
            if due_iso and str(schedule.get("last_due_at") or "") != due_iso:
                due.append((_normalize_schedule(schedule, now=_iso_now(self.now_factory), now_factory=self.now_factory), due_iso))
        return due

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        normalized_id = str(schedule_id or "").strip()
        for schedule in self._read().get("schedules", []):
            if isinstance(schedule, Mapping) and str(schedule.get("id") or "") == normalized_id:
                return _normalize_schedule(schedule, now=_iso_now(self.now_factory), now_factory=self.now_factory)
        raise ValueError(f"auto refresh schedule not found: {normalized_id}")

    def mark_run_started(
        self,
        schedule_id: str,
        *,
        started_at: str,
        due_at: str = "",
        run_id: str = "",
        trigger_source: str = "scheduled",
    ) -> None:
        patch = {
            "last_run_at": started_at,
            "last_status": "running",
            "last_status_label": "Выполняется",
            "last_technical_status": "running",
            "last_error": "",
            "last_error_summary": "",
            "last_result_summary": "",
            "last_run_id": run_id,
            "last_trigger_source": trigger_source,
        }
        if due_at:
            patch["last_due_at"] = due_at
        self._patch_schedule(schedule_id, patch)

    def mark_run_finished(
        self,
        schedule_id: str,
        *,
        finished_at: str,
        result_payload: Mapping[str, Any] | None = None,
        error: str = "",
        http_status: int | None = None,
    ) -> None:
        status, label, reason = _classify_result(result_payload=result_payload, error=error, http_status=http_status)
        technical_status = _technical_status(result_payload=result_payload, error=error, http_status=http_status)
        patch: dict[str, Any] = {
            "last_finished_at": finished_at,
            "last_status": status,
            "last_status_label": label,
            "last_technical_status": technical_status,
            "last_error": error if status == "error" else "",
            "last_error_summary": reason if status in {"error", "warning"} else "",
            "last_result_summary": reason,
        }
        if status in SUCCESS_STATUSES:
            patch["last_success_at"] = finished_at
        elif status == "error":
            patch["last_error_at"] = finished_at
        self._patch_schedule(schedule_id, patch)

    def mark_due_skipped(
        self,
        schedule_id: str,
        *,
        due_at: str,
        reason: str,
        trigger_source: str = "scheduled",
    ) -> None:
        self._patch_schedule(
            schedule_id,
            {
                "last_due_at": due_at,
                "last_status": "skipped",
                "last_status_label": "Пропущено",
                "last_technical_status": "skipped",
                "last_error": "",
                "last_error_summary": reason,
                "last_result_summary": reason,
                "last_trigger_source": trigger_source,
            },
        )

    def _patch_schedule(self, schedule_id: str, patch: Mapping[str, Any]) -> None:
        normalized_id = str(schedule_id or "").strip()
        now = _iso_now(self.now_factory)
        with self._lock:
            with self._file_lock_unlocked():
                payload = self._read_unlocked()
                schedules = payload.setdefault("schedules", [])
                for index, raw in enumerate(schedules):
                    if not isinstance(raw, Mapping) or str(raw.get("id") or "") != normalized_id:
                        continue
                    schedules[index] = _normalize_schedule(
                        {**dict(raw), **dict(patch), "updated_at": now},
                        now=now,
                        now_factory=self.now_factory,
                    )
                    self._write_unlocked(payload)
                    return
                raise ValueError(f"auto refresh schedule not found: {normalized_id}")

    def _read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "schedule_policy": _default_schedule_policy(),
                "schedules": _default_schedules(_iso_now(self.now_factory), self.now_factory),
            }
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1AutoRefreshSchedulesError("auto refresh schedules state is not readable") from exc
        if not isinstance(raw, Mapping):
            raise SheetVitrinaV1AutoRefreshSchedulesError("auto refresh schedules state has invalid shape")
        now = _iso_now(self.now_factory)
        schedule_policy = _normalize_schedule_policy(raw.get("schedule_policy"), strict=False)
        schedules = raw.get("schedules") if isinstance(raw.get("schedules"), list) else []
        normalized_schedules = [
            _normalize_schedule(item, index=index, now=now, now_factory=self.now_factory)
            for index, item in enumerate(schedules, start=1)
            if isinstance(item, Mapping)
        ]
        if schedule_policy["mode"] == SCHEDULE_POLICY_MODE_INTERVAL:
            existing_by_id = _schedule_lifecycle_lookup(normalized_schedules)
            normalized_schedules = _materialize_interval_schedules(
                schedule_policy,
                existing_by_id=existing_by_id,
                now=now,
                now_factory=self.now_factory,
            )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": _safe_text(raw.get("updated_at"), 80),
            "schedule_policy": schedule_policy,
            "schedules": normalized_schedules,
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _iso_now(self.now_factory)
        schedule_policy = _normalize_schedule_policy(payload.get("schedule_policy"), strict=False)
        raw_schedules = [
            item for item in payload.get("schedules", [])
            if isinstance(item, Mapping)
        ]
        if schedule_policy["mode"] == SCHEDULE_POLICY_MODE_INTERVAL:
            existing_by_id = _schedule_lifecycle_lookup(raw_schedules)
            schedules = _materialize_interval_schedules(
                schedule_policy,
                existing_by_id=existing_by_id,
                now=now,
                now_factory=self.now_factory,
            )
        else:
            schedules = [
                _normalize_schedule(item, index=index, now=now, now_factory=self.now_factory)
                for index, item in enumerate(raw_schedules, start=1)
            ]
        normalized = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": now,
            "schedule_policy": schedule_policy,
            "schedules": schedules,
        }
        temp_path = self.path.with_name(
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp_path.replace(self.path)
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    @contextmanager
    def _file_lock_unlocked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _default_schedules(now: str, now_factory: Callable[[], datetime]) -> list[dict[str, Any]]:
    return [
        _normalize_schedule(
            {
                "id": f"daily_{hour:02d}_00_ekt",
                "enabled": True,
                "local_time_hhmm": f"{hour:02d}:00",
                "timezone": DEFAULT_TIMEZONE,
                "created_at": now,
                "updated_at": now,
            },
            index=index,
            now=now,
            now_factory=now_factory,
        )
        for index, hour in enumerate(DAILY_REFRESH_BUSINESS_HOURS, start=1)
    ]


def _default_schedule_policy() -> dict[str, Any]:
    return {
        "mode": SCHEDULE_POLICY_MODE_MANUAL,
        "interval_hours": None,
        "window_start_hhmm": INTERVAL_WINDOW_START_HHMM,
        "window_end_hhmm": INTERVAL_WINDOW_END_HHMM,
        "timezone": DEFAULT_TIMEZONE,
    }


def _normalize_schedule_policy(raw: Any, *, strict: bool) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return _default_schedule_policy()
    mode = str(raw.get("mode") or SCHEDULE_POLICY_MODE_MANUAL).strip().lower()
    if mode not in {SCHEDULE_POLICY_MODE_MANUAL, SCHEDULE_POLICY_MODE_INTERVAL}:
        if strict:
            raise ValueError("schedule_policy.mode must be manual or interval")
        mode = SCHEDULE_POLICY_MODE_MANUAL
    if mode == SCHEDULE_POLICY_MODE_MANUAL:
        return _default_schedule_policy()
    interval_hours = _safe_int(raw.get("interval_hours"))
    if interval_hours is None:
        interval_hours = DEFAULT_INTERVAL_HOURS
    if interval_hours < min(ALLOWED_INTERVAL_HOURS):
        raise ValueError("schedule_policy.interval_hours must be at least 3")
    if interval_hours not in ALLOWED_INTERVAL_HOURS:
        raise ValueError("schedule_policy.interval_hours must be one of 3, 4, 6")
    slots = _interval_preview_slots(interval_hours)
    if len(slots) > MAX_INTERVAL_RUNS_PER_DAY:
        raise ValueError("schedule_policy.interval_hours creates more than 6 runs per day")
    return {
        "mode": SCHEDULE_POLICY_MODE_INTERVAL,
        "interval_hours": interval_hours,
        "window_start_hhmm": INTERVAL_WINDOW_START_HHMM,
        "window_end_hhmm": INTERVAL_WINDOW_END_HHMM,
        "timezone": DEFAULT_TIMEZONE,
    }


def _interval_options() -> list[dict[str, Any]]:
    return [
        {
            "interval_hours": hours,
            "label": _interval_hours_label(hours),
            "preview_slots": _interval_preview_slots(hours),
        }
        for hours in ALLOWED_INTERVAL_HOURS
    ]


def _interval_preview_slots(interval_hours: int) -> list[str]:
    if interval_hours not in ALLOWED_INTERVAL_HOURS:
        raise ValueError("schedule_policy.interval_hours must be one of 3, 4, 6")
    start_hour, start_minute = _parse_hhmm(INTERVAL_WINDOW_START_HHMM)
    end_hour, end_minute = _parse_hhmm(INTERVAL_WINDOW_END_HHMM)
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    if end_minutes < start_minutes:
        raise ValueError("interval schedule window must not cross midnight")
    slots: list[str] = []
    current = start_minutes
    step = interval_hours * 60
    while current <= end_minutes:
        hour = current // 60
        minute = current % 60
        if hour < 0 or hour > 23:
            raise ValueError("interval schedule slot is outside one local day")
        slots.append(f"{hour:02d}:{minute:02d}")
        current += step
    if len(slots) > MAX_INTERVAL_RUNS_PER_DAY:
        raise ValueError("interval schedule creates more than 6 runs per day")
    return slots


def _materialize_interval_schedules(
    policy: Mapping[str, Any],
    *,
    existing_by_id: Mapping[str, Mapping[str, Any]],
    now: str,
    now_factory: Callable[[], datetime],
) -> list[dict[str, Any]]:
    interval_hours = int(policy.get("interval_hours") or DEFAULT_INTERVAL_HOURS)
    slots = _interval_preview_slots(interval_hours)
    schedules: list[dict[str, Any]] = []
    for index, local_time in enumerate(slots, start=1):
        schedule_id = _interval_schedule_id(interval_hours, local_time)
        existing = dict(
            existing_by_id.get(schedule_id)
            or existing_by_id.get(_interval_schedule_identity_key(interval_hours, local_time, DEFAULT_TIMEZONE))
            or existing_by_id.get(_interval_schedule_slot_key(local_time, DEFAULT_TIMEZONE))
            or {}
        )
        schedules.append(
            _normalize_schedule(
                {
                    **existing,
                    "id": schedule_id,
                    "enabled": True,
                    "editable": False,
                    "local_time_hhmm": local_time,
                    "timezone": DEFAULT_TIMEZONE,
                    "schedule_type": SCHEDULE_POLICY_MODE_INTERVAL,
                    "interval_hours": interval_hours,
                    "created_at": existing.get("created_at") or now,
                    "updated_at": now,
                    "enabled_since_at": existing.get("enabled_since_at") or now,
                },
                index=index,
                now=now,
                now_factory=now_factory,
            )
        )
    _validate_schedule_set(schedules)
    return schedules


def _interval_schedule_id(interval_hours: int, local_time_hhmm: str) -> str:
    return f"interval_{interval_hours}h_{str(local_time_hhmm).replace(':', '_')}_ekt"


def _schedule_lifecycle_lookup(raw_schedules: Any) -> dict[str, Mapping[str, Any]]:
    lookup: dict[str, Mapping[str, Any]] = {}
    if not isinstance(raw_schedules, list):
        return lookup
    for raw in raw_schedules:
        if not isinstance(raw, Mapping):
            continue
        schedule_id = str(raw.get("id") or "").strip()
        if schedule_id:
            lookup.setdefault(schedule_id, raw)
        local_time = str(raw.get("local_time_hhmm") or raw.get("time") or "").strip()
        timezone_name = str(raw.get("timezone") or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
        if not _is_hhmm(local_time):
            continue
        interval_hours = _safe_int(raw.get("interval_hours"))
        if interval_hours in ALLOWED_INTERVAL_HOURS:
            lookup.setdefault(_interval_schedule_identity_key(interval_hours, local_time, timezone_name), raw)
        schedule_type = str(raw.get("schedule_type") or "").strip().lower()
        trigger_kind = str(raw.get("trigger_kind") or "").strip().lower()
        if schedule_type == SCHEDULE_POLICY_MODE_INTERVAL or "interval" in trigger_kind or schedule_id.startswith("interval_"):
            lookup.setdefault(_interval_schedule_slot_key(local_time, timezone_name), raw)
    return lookup


def _interval_schedule_identity_key(interval_hours: int, local_time_hhmm: str, timezone_name: str) -> str:
    return f"interval:{int(interval_hours)}:{timezone_name}:{local_time_hhmm}"


def _interval_schedule_slot_key(local_time_hhmm: str, timezone_name: str) -> str:
    return f"interval-slot:{timezone_name}:{local_time_hhmm}"


def _operator_message(policy: Mapping[str, Any]) -> str:
    if str(policy.get("mode") or "") == SCHEDULE_POLICY_MODE_INTERVAL:
        interval_hours = int(policy.get("interval_hours") or DEFAULT_INTERVAL_HOURS)
        slots = "/".join(_interval_preview_slots(interval_hours))
        return f"Интервальный режим: каждые {_interval_hours_label(interval_hours)} в окне 10:00-22:00 Asia/Yekaterinburg ({slots})."
    return "Ручные триггеры хранятся в runtime JSON state; systemd timer только регулярно проверяет due schedules."


def _interval_hours_label(interval_hours: int) -> str:
    return "6 часов" if int(interval_hours) == 6 else f"{int(interval_hours)} часа"


def _normalize_schedule(
    raw: Mapping[str, Any],
    *,
    index: int = 1,
    now: str,
    now_factory: Callable[[], datetime],
) -> dict[str, Any]:
    local_time = _safe_text(raw.get("local_time_hhmm") or raw.get("time"), 5)
    if not _is_hhmm(local_time):
        raise ValueError(f"schedule #{index} local_time_hhmm must use HH:MM")
    timezone_name = _safe_text(raw.get("timezone"), 80) or DEFAULT_TIMEZONE
    _timezone(timezone_name)
    schedule_id = _safe_text(raw.get("id"), 120) or f"custom_{uuid4().hex[:12]}"
    enabled = bool(raw.get("enabled", True))
    schedule_type = _safe_text(raw.get("schedule_type"), 40) or SCHEDULE_POLICY_MODE_MANUAL
    if schedule_type not in {SCHEDULE_POLICY_MODE_MANUAL, SCHEDULE_POLICY_MODE_INTERVAL}:
        schedule_type = SCHEDULE_POLICY_MODE_MANUAL
    trigger_kind = "runtime_interval_schedule" if schedule_type == SCHEDULE_POLICY_MODE_INTERVAL else "runtime_json_schedule"
    editable = bool(raw.get("editable", True))
    normalized = {
        "id": schedule_id,
        "enabled": enabled,
        "local_time_hhmm": local_time,
        "timezone": timezone_name,
        "timezone_label": timezone_name,
        "trigger_name": DEFAULT_TRIGGER_NAME,
        "trigger_kind": trigger_kind,
        "schedule_type": schedule_type,
        "action": "canonical_full_refresh",
        "auto_refresh": True,
        "editable": editable,
        "status": "active" if enabled else "disabled",
        "description": _schedule_description(
            local_time=local_time,
            timezone_name=timezone_name,
            schedule_type=schedule_type,
            interval_hours=_safe_int(raw.get("interval_hours")),
        ),
        "created_at": _safe_text(raw.get("created_at"), 80) or now,
        "updated_at": _safe_text(raw.get("updated_at"), 80) or now,
        "enabled_since_at": _safe_text(raw.get("enabled_since_at"), 80) or (now if enabled else ""),
        "last_run_at": _safe_text(raw.get("last_run_at"), 80),
        "last_finished_at": _safe_text(raw.get("last_finished_at"), 80),
        "last_success_at": _safe_text(raw.get("last_success_at"), 80),
        "last_error_at": _safe_text(raw.get("last_error_at"), 80),
        "last_due_at": _safe_text(raw.get("last_due_at"), 80),
        "last_status": _safe_text(raw.get("last_status"), 80) or ("pending" if enabled else "disabled"),
        "last_status_label": _safe_text(raw.get("last_status_label"), 80),
        "last_technical_status": _safe_text(raw.get("last_technical_status"), 80),
        "last_error": _safe_text(raw.get("last_error"), 1000),
        "last_error_summary": _safe_text(raw.get("last_error_summary"), 1000),
        "last_result_summary": _safe_text(raw.get("last_result_summary"), 1000),
        "last_run_id": _safe_text(raw.get("last_run_id"), 160),
        "last_trigger_source": _safe_text(raw.get("last_trigger_source"), 80),
    }
    if schedule_type == SCHEDULE_POLICY_MODE_INTERVAL:
        normalized["interval_hours"] = _safe_int(raw.get("interval_hours")) or DEFAULT_INTERVAL_HOURS
    normalized["next_run_at"] = _next_run_at(normalized, now_factory())
    return normalized


def _schedule_description(
    *,
    local_time: str,
    timezone_name: str,
    schedule_type: str,
    interval_hours: int | None,
) -> str:
    if schedule_type == SCHEDULE_POLICY_MODE_INTERVAL:
        hours = interval_hours or DEFAULT_INTERVAL_HOURS
        return f"Интервальный слот {local_time} {timezone_name}: canonical full refresh каждые {_interval_hours_label(hours)}."
    return f"{local_time} {timezone_name}: canonical full refresh with auto_refresh=true"


def _merge_editable_schedule(
    existing: Mapping[str, Any] | None,
    raw: Mapping[str, Any],
    *,
    schedule_id: str,
) -> dict[str, Any]:
    # Browser save payloads include rendered rows; lifecycle fields stay server-owned.
    editable_patch = {
        field: raw[field]
        for field in EDITABLE_SCHEDULE_FIELDS
        if field in raw
    }
    return {
        **dict(existing or {}),
        **editable_patch,
        "id": schedule_id,
        "schedule_type": SCHEDULE_POLICY_MODE_MANUAL,
        "editable": True,
        "interval_hours": None,
    }


def _validate_schedule_set(schedules: list[Mapping[str, Any]]) -> None:
    ids = [str(item.get("id") or "") for item in schedules]
    if len(ids) != len(set(ids)):
        raise ValueError("schedule ids must be unique")
    enabled_times = [
        (str(item.get("timezone") or DEFAULT_TIMEZONE), str(item.get("local_time_hhmm") or ""))
        for item in schedules
        if bool(item.get("enabled", True))
    ]
    if len(enabled_times) != len(set(enabled_times)):
        raise ValueError("enabled schedules must not duplicate local_time_hhmm in the same timezone")


def _public_schedule(schedule: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    normalized = dict(schedule)
    normalized["next_run_at"] = str(schedule.get("next_run_at") or _next_run_at(schedule, now) or "")
    normalized["can_run_now"] = True
    return normalized


def _summarize(
    schedules: list[Mapping[str, Any]],
    *,
    auto_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    latest_run = _max_by_timestamp(schedules, "last_run_at")
    latest_success = _max_by_timestamp(schedules, "last_success_at")
    latest_error = _max_by_timestamp(schedules, "last_error_at")
    next_run = _min_by_timestamp([item for item in schedules if bool(item.get("enabled", True))], "next_run_at")
    auto_context = auto_context or {}
    context_last_run_at = str(auto_context.get("last_auto_run_time") or "")
    schedule_last_run_at = str(latest_run.get("last_run_at") or "")
    context_has_newer_run = _timestamp_is_newer(context_last_run_at, schedule_last_run_at)
    last_run = {} if context_has_newer_run else latest_run
    last_status = str(
        (auto_context.get("last_auto_run_status") if context_has_newer_run else last_run.get("last_status"))
        or auto_context.get("last_auto_run_status")
        or "never"
    ).strip()
    last_success_at = _latest_timestamp(
        str((latest_success or {}).get("last_success_at") or ""),
        str(auto_context.get("last_successful_auto_update_at") or ""),
    )
    last_run_at = (
        context_last_run_at
        if context_has_newer_run
        else str((latest_run or {}).get("last_run_at") or auto_context.get("last_auto_run_time") or "")
    )
    last_run_finished_at = (
        str(auto_context.get("last_auto_run_finished_at") or "")
        if context_has_newer_run
        else str((latest_run or {}).get("last_finished_at") or auto_context.get("last_auto_run_finished_at") or "")
    )
    return {
        "next_auto_run_at": str((next_run or {}).get("next_run_at") or ""),
        "last_auto_run_at": last_run_at,
        "last_auto_run_time": last_run_at,
        "last_auto_run_finished_at": last_run_finished_at,
        "last_auto_run_status": last_status,
        "last_auto_run_technical_status": str(
            (last_run or {}).get("last_technical_status") or auto_context.get("last_auto_run_technical_status") or ""
        ),
        "last_auto_run_status_label": _status_label(last_status),
        "last_auto_run_status_reason": str((last_run or {}).get("last_result_summary") or auto_context.get("last_auto_run_status_reason") or ""),
        "last_auto_job_id": str((last_run or {}).get("last_run_id") or auto_context.get("last_auto_job_id") or ""),
        "last_successful_auto_update_at": last_success_at,
        "last_auto_success_at": last_success_at,
        "last_auto_error_at": str((latest_error or {}).get("last_error_at") or ""),
        "last_auto_error_summary": str((latest_error or {}).get("last_error_summary") or ""),
        "last_auto_run_error": str((last_run or {}).get("last_error") or auto_context.get("last_auto_run_error") or ""),
    }


def _classify_result(
    *,
    result_payload: Mapping[str, Any] | None,
    error: str,
    http_status: int | None,
) -> tuple[str, str, str]:
    if error or (http_status is not None and http_status >= 400):
        reason = error or f"HTTP {http_status}"
        return "error", "Ошибка", reason
    payload = result_payload or {}
    nested = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    auto_result = payload.get("auto_result") if isinstance(payload.get("auto_result"), Mapping) else {}
    status = str(
        payload.get("semantic_status")
        or payload.get("status")
        or auto_result.get("semantic_status")
        or nested.get("semantic_status")
        or nested.get("status")
        or "success"
    ).strip().lower()
    if status not in {"success", "warning", "error"}:
        status = "warning"
    label = "Успешно" if status == "success" else ("Ошибка" if status == "error" else "Внимание")
    reason = str(
        payload.get("semantic_reason")
        or payload.get("status_reason")
        or auto_result.get("semantic_reason")
        or nested.get("semantic_reason")
        or nested.get("status_reason")
        or ""
    )
    return status, label, reason


def _technical_status(
    *,
    result_payload: Mapping[str, Any] | None,
    error: str,
    http_status: int | None,
) -> str:
    if error or (http_status is not None and http_status >= 400):
        return "error"
    payload = result_payload or {}
    nested = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    auto_result = payload.get("auto_result") if isinstance(payload.get("auto_result"), Mapping) else {}
    status = str(
        payload.get("technical_status")
        or auto_result.get("technical_status")
        or nested.get("technical_status")
        or "success"
    ).strip().lower()
    return status if status in {"success", "warning", "error", "running"} else "success"


def _max_by_timestamp(items: list[Mapping[str, Any]], key: str) -> Mapping[str, Any]:
    candidates = [item for item in items if str(item.get(key) or "").strip()]
    if not candidates:
        return {}
    return max(candidates, key=lambda item: _timestamp_sort_key(str(item.get(key) or "")))


def _min_by_timestamp(items: list[Mapping[str, Any]], key: str) -> Mapping[str, Any]:
    candidates = [item for item in items if str(item.get(key) or "").strip()]
    if not candidates:
        return {}
    return min(candidates, key=lambda item: _timestamp_sort_key(str(item.get(key) or "")))


def _latest_timestamp(left: str, right: str) -> str:
    left_text = str(left or "").strip()
    right_text = str(right or "").strip()
    if not left_text:
        return right_text
    if not right_text:
        return left_text
    return right_text if _timestamp_is_newer(right_text, left_text) else left_text


def _timestamp_is_newer(candidate: str, current: str) -> bool:
    candidate_text = str(candidate or "").strip()
    current_text = str(current or "").strip()
    if not candidate_text:
        return False
    if not current_text:
        return True
    return _timestamp_sort_key(candidate_text) > _timestamp_sort_key(current_text)


def _timestamp_sort_key(value: str) -> tuple[int, str]:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return (0, str(value or ""))
    return (1, parsed.astimezone(timezone.utc).isoformat())


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware_utc(parsed)


def _last_due_at(schedule: Mapping[str, Any], now: datetime) -> datetime | None:
    timezone_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    zone = _timezone(timezone_name)
    local_now = _aware_utc(now).astimezone(zone)
    hour, minute = _parse_hhmm(str(schedule.get("local_time_hhmm") or ""))
    local_due = datetime.combine(local_now.date(), time(hour=hour, minute=minute), tzinfo=zone)
    if local_now < local_due:
        return None
    return local_due.astimezone(timezone.utc)


def _next_run_at(schedule: Mapping[str, Any], now: datetime) -> str:
    if not bool(schedule.get("enabled", True)):
        return ""
    timezone_name = str(schedule.get("timezone") or DEFAULT_TIMEZONE)
    zone = _timezone(timezone_name)
    local_now = _aware_utc(now).astimezone(zone)
    hour, minute = _parse_hhmm(str(schedule.get("local_time_hhmm") or ""))
    candidate = datetime.combine(local_now.date(), time(hour=hour, minute=minute), tzinfo=zone)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return _iso_datetime(candidate.astimezone(timezone.utc))


def _timezone(value: str) -> ZoneInfo:
    return ZoneInfo(value or DEFAULT_TIMEZONE)


def _parse_hhmm(value: str) -> tuple[int, int]:
    if not _is_hhmm(value):
        raise ValueError("local_time_hhmm must use HH:MM")
    hour, minute = value.split(":", 1)
    return int(hour), int(minute)


def _is_hhmm(value: str) -> bool:
    if len(value) != 5 or value[2] != ":":
        return False
    hour_text, minute_text = value.split(":", 1)
    if not hour_text.isdigit() or not minute_text.isdigit():
        return False
    hour = int(hour_text)
    minute = int(minute_text)
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_now(now_factory: Callable[[], datetime]) -> str:
    return _iso_datetime(now_factory())


def _iso_datetime(value: datetime) -> str:
    return _aware_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_text(value: Any, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) > max_length:
        return text[: max_length - 1] + "…"
    return text


def _status_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "success":
        return "Успешно"
    if normalized == "warning":
        return "Внимание"
    if normalized == "error":
        return "Ошибка"
    if normalized == "running":
        return "Выполняется"
    if normalized == "pending":
        return "Ожидает"
    if normalized == "skipped":
        return "Пропущено"
    return "Ещё не выполнялось"
