"""Runtime-managed auto-refresh schedules for sheet_vitrina_v1 web vitrina."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Any, Callable, Mapping
from uuid import uuid4
from zoneinfo import ZoneInfo

from packages.business_time import CANONICAL_BUSINESS_TIMEZONE_NAME, DAILY_REFRESH_BUSINESS_HOURS


CONTRACT_NAME = "sheet_vitrina_v1_auto_refresh_schedules"
CONTRACT_VERSION = "v1"
DEFAULT_STATE_FILENAME = "sheet_vitrina_v1_auto_refresh_schedules.json"
DEFAULT_TIMEZONE = CANONICAL_BUSINESS_TIMEZONE_NAME
DEFAULT_TIMER_NAME = "wb-core-sheet-vitrina-refresh.timer"
DEFAULT_TRIGGER_NAME = "runtime_auto_refresh_schedule"
DEFAULT_SCHEDULE_SOURCE = "runtime_json"
DEFAULT_SCHEDULE_MODE = "runtime_managed_json_schedule"
DEFAULT_SYSTEMD_ONCALENDAR = "*-*-* *:00,10,20,30,40,50:00"
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
        schedules = payload.get("schedules") if isinstance(payload.get("schedules"), list) else []
        public_schedules = [_public_schedule(schedule, now=self.now_factory()) for schedule in schedules]
        summary = _summarize(public_schedules, auto_context=auto_context)
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ok",
            "schedule_mode": DEFAULT_SCHEDULE_MODE,
            "schedule_source": DEFAULT_SCHEDULE_SOURCE,
            "timezone": DEFAULT_TIMEZONE,
            "timezone_label": "Asia/Yekaterinburg",
            "can_edit_runtime": True,
            "save_supported": True,
            "run_now_supported": True,
            "operator_approval_required": False,
            "systemd_timer_name": DEFAULT_TIMER_NAME,
            "systemd_oncalendar": DEFAULT_SYSTEMD_ONCALENDAR,
            "message": "Расписание хранится в runtime JSON state; systemd timer только регулярно проверяет due schedules.",
            "schedules": public_schedules,
            **summary,
        }

    def save_schedules(self, payload: Mapping[str, Any], *, auto_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        raw_schedules = payload.get("schedules")
        if not isinstance(raw_schedules, list):
            raise ValueError("schedules must be a JSON array")
        now = _iso_now(self.now_factory)
        with self._lock:
            current = self._read_unlocked()
            existing_by_id = {
                str(item.get("id") or ""): item
                for item in current.get("schedules", [])
                if isinstance(item, Mapping) and str(item.get("id") or "")
            }
            normalized = []
            for index, raw in enumerate(raw_schedules, start=1):
                if not isinstance(raw, Mapping):
                    raise ValueError("each schedule must be an object")
                schedule_id = _safe_text(raw.get("id"), 120) or f"custom_{uuid4().hex[:12]}"
                existing = existing_by_id.get(schedule_id)
                merged = _merge_editable_schedule(existing, raw, schedule_id=schedule_id)
                normalized.append(_normalize_schedule(merged, index=index, now=now, now_factory=self.now_factory))
            _validate_schedule_set(normalized)
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

    def _patch_schedule(self, schedule_id: str, patch: Mapping[str, Any]) -> None:
        normalized_id = str(schedule_id or "").strip()
        now = _iso_now(self.now_factory)
        with self._lock:
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
                "schedules": _default_schedules(_iso_now(self.now_factory), self.now_factory),
            }
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SheetVitrinaV1AutoRefreshSchedulesError("auto refresh schedules state is not readable") from exc
        if not isinstance(raw, Mapping):
            raise SheetVitrinaV1AutoRefreshSchedulesError("auto refresh schedules state has invalid shape")
        now = _iso_now(self.now_factory)
        schedules = raw.get("schedules") if isinstance(raw.get("schedules"), list) else []
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": _safe_text(raw.get("updated_at"), 80),
            "schedules": [
                _normalize_schedule(item, index=index, now=now, now_factory=self.now_factory)
                for index, item in enumerate(schedules, start=1)
                if isinstance(item, Mapping)
            ],
        }

    def _write_unlocked(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = _iso_now(self.now_factory)
        schedules = [
            _normalize_schedule(item, index=index, now=now, now_factory=self.now_factory)
            for index, item in enumerate(payload.get("schedules", []), start=1)
            if isinstance(item, Mapping)
        ]
        normalized = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "updated_at": now,
            "schedules": schedules,
        }
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self.path)


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
    normalized = {
        "id": schedule_id,
        "enabled": enabled,
        "local_time_hhmm": local_time,
        "timezone": timezone_name,
        "timezone_label": timezone_name,
        "trigger_name": DEFAULT_TRIGGER_NAME,
        "trigger_kind": "runtime_json_schedule",
        "action": "canonical_full_refresh",
        "auto_refresh": True,
        "editable": True,
        "status": "active" if enabled else "disabled",
        "description": f"{local_time} {timezone_name}: canonical full refresh with auto_refresh=true",
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
    normalized["next_run_at"] = _next_run_at(normalized, now_factory())
    return normalized


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
    return {**dict(existing or {}), **editable_patch, "id": schedule_id}


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
    last_status = (
        str(latest_run.get("last_status") or "").strip()
        if latest_run
        else str(auto_context.get("last_auto_run_status") or "never")
    )
    return {
        "next_auto_run_at": str((next_run or {}).get("next_run_at") or ""),
        "last_auto_run_at": str((latest_run or {}).get("last_run_at") or auto_context.get("last_auto_run_time") or ""),
        "last_auto_run_time": str((latest_run or {}).get("last_run_at") or auto_context.get("last_auto_run_time") or ""),
        "last_auto_run_finished_at": str((latest_run or {}).get("last_finished_at") or auto_context.get("last_auto_run_finished_at") or ""),
        "last_auto_run_status": last_status,
        "last_auto_run_technical_status": str(
            (latest_run or {}).get("last_technical_status") or auto_context.get("last_auto_run_technical_status") or ""
        ),
        "last_auto_run_status_label": _status_label(last_status),
        "last_auto_run_status_reason": str((latest_run or {}).get("last_result_summary") or auto_context.get("last_auto_run_status_reason") or ""),
        "last_auto_job_id": str((latest_run or {}).get("last_run_id") or auto_context.get("last_auto_job_id") or ""),
        "last_successful_auto_update_at": str((latest_success or {}).get("last_success_at") or auto_context.get("last_successful_auto_update_at") or ""),
        "last_auto_success_at": str((latest_success or {}).get("last_success_at") or auto_context.get("last_successful_auto_update_at") or ""),
        "last_auto_error_at": str((latest_error or {}).get("last_error_at") or ""),
        "last_auto_error_summary": str((latest_error or {}).get("last_error_summary") or ""),
        "last_auto_run_error": str((latest_run or {}).get("last_error") or auto_context.get("last_auto_run_error") or ""),
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
    return max(candidates, key=lambda item: str(item.get(key) or ""))


def _min_by_timestamp(items: list[Mapping[str, Any]], key: str) -> Mapping[str, Any]:
    candidates = [item for item in items if str(item.get(key) or "").strip()]
    if not candidates:
        return {}
    return min(candidates, key=lambda item: str(item.get(key) or ""))


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
    return "Ещё не выполнялось"
