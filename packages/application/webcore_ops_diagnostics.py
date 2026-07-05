"""Bounded read-only ops diagnostics for the WebCore Data MCP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote


DB_FILENAME = "registry_upload_runtime.sqlite3"
DEFAULT_RUNTIME_DIR = Path(".runtime/registry_upload")
DEFAULT_PUBLIC_BASE_URL = "https://api.selleros.pro"
TARGET_ID = "wb_core_eu_hosted_runtime_active"
TARGET_ROLE = "primary_live"
TARGET_LIFECYCLE = "current_live"
SSH_DESTINATION_LABEL = "wb-core-eu-root"
RUNTIME_DIR_LABEL = "REGISTRY_UPLOAD_RUNTIME_DIR"
DB_LABEL = "registry_upload_runtime.sqlite3"
MAX_SERVICE_LOG_LIMIT = 300
DEFAULT_SERVICE_LOG_LIMIT = 100
MAX_LOG_WINDOW_DAYS = 7
MAX_SNAPSHOT_RANGE_DAYS = 62
MAX_DIAGNOSTIC_ROWS = 80
COMMAND_TIMEOUT_SECONDS = 8.0

OPS_TOOL_NAMES = (
    "get_runtime_health_summary",
    "get_service_logs",
    "get_refresh_diagnostics",
    "get_runtime_snapshot_status",
    "get_deploy_state",
)

ALLOWED_RUNTIME_UNITS = (
    "wb-core-registry-http.service",
    "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-sheet-vitrina-refresh.service",
    "wb-core-sheet-vitrina-closure-retry.timer",
    "wb-core-sheet-vitrina-closure-retry.service",
    "wb-core-data-mcp.service",
)

SERVICE_LOG_PRIORITIES = ("debug", "info", "warning", "error")
_PRIORITY_LABELS = {
    "0": "emergency",
    "1": "alert",
    "2": "critical",
    "3": "error",
    "4": "warning",
    "5": "notice",
    "6": "info",
    "7": "debug",
}

_SYSTEMCTL_SHOW_PROPERTIES = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "NRestarts",
    "ActiveEnterTimestamp",
    "InactiveEnterTimestamp",
    "StateChangeTimestamp",
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)\b("
    r"authorization|cookie|set-cookie|password|passwd|pwd|token|access_token|refresh_token|"
    r"client_secret|secret|api_key|apikey|private_key"
    r")\b"
)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b("
    r"authorization|cookie|set-cookie|password|passwd|pwd|token|access_token|refresh_token|"
    r"client_secret|secret|api_key|apikey|private_key"
    r")\s*[:=]\s*([^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])("
    r"/opt/[^\s,;\"']+|/etc/[^\s,;\"']+|/home/[^\s,;\"']+|/Users/[^\s,;\"']+|"
    r"/var/lib/[^\s,;\"']+|/root/[^\s,;\"']+"
    r")"
)
_DURATION_RE = re.compile(r"^(\d{1,3})(m|h|d)$", re.IGNORECASE)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class WebCoreOpsDiagnosticsError(ValueError):
    """Raised for rejected diagnostics requests."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], float], CommandResult]


class WebCoreOpsDiagnostics:
    """Read-only diagnostics over fixed runtime surfaces."""

    def __init__(
        self,
        *,
        runtime_dir: Path | None = None,
        db_path: Path | None = None,
        command_runner: CommandRunner | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        resolved_runtime_dir = runtime_dir or Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", str(DEFAULT_RUNTIME_DIR)))
        self.runtime_dir = Path(resolved_runtime_dir).expanduser()
        self.db_path = Path(db_path).expanduser() if db_path else self.runtime_dir / DB_FILENAME
        self.command_runner = command_runner or _run_fixed_command
        self.now_factory = now_factory or _utc_now_datetime

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        if name == "get_runtime_health_summary":
            return self.get_runtime_health_summary()
        if name == "get_service_logs":
            return self.get_service_logs(
                unit=_required_unit(args.get("unit")),
                since=_optional_string(args.get("since"), max_length=48) or "1h",
                until=_optional_string(args.get("until"), max_length=48),
                priority=_optional_priority(args.get("priority")),
                limit=_bounded_limit(args.get("limit"), DEFAULT_SERVICE_LOG_LIMIT, MAX_SERVICE_LOG_LIMIT),
            )
        if name == "get_refresh_diagnostics":
            date_from, date_to = _requested_date_range(args, required=True)
            return self.get_refresh_diagnostics(date_from=date_from, date_to=date_to)
        if name == "get_runtime_snapshot_status":
            date_from, date_to = _requested_date_range(args, required=True)
            return self.get_runtime_snapshot_status(date_from=date_from, date_to=date_to)
        if name == "get_deploy_state":
            return self.get_deploy_state()
        raise WebCoreOpsDiagnosticsError(f"tool is not allowlisted: {name}")

    def get_runtime_health_summary(self) -> dict[str, Any]:
        units = [self._read_unit_state(unit) for unit in ALLOWED_RUNTIME_UNITS]
        return _sanitize(
            {
                "status": "ok",
                "generated_at": _utc_now(),
                "boundary": "read_only_ops_diagnostics",
                "allowed_units": list(ALLOWED_RUNTIME_UNITS),
                "units": units,
                "runtime_storage": {
                    "runtime_dir_label": RUNTIME_DIR_LABEL,
                    "disk": self._disk_summary(),
                    "db": self._db_summary(),
                },
                "limits": {
                    "unit_allowlist": "fixed",
                    "arbitrary_shell": False,
                    "arbitrary_filesystem": False,
                    "arbitrary_sql": False,
                },
            }
        )

    def get_service_logs(
        self,
        *,
        unit: str,
        since: str,
        until: str | None,
        priority: str,
        limit: int,
    ) -> dict[str, Any]:
        window = _parse_log_window(
            since=since,
            until=until,
            now=self.now_factory(),
        )
        args = [
            "journalctl",
            "--no-pager",
            "--output=json",
            "--reverse",
            f"--unit={unit}",
            f"--since={_journal_time(window['since'])}",
            f"--priority={priority}",
            f"--lines={limit + 1}",
        ]
        if window.get("until") is not None:
            args.append(f"--until={_journal_time(window['until'])}")
        result = self.command_runner(args, COMMAND_TIMEOUT_SECONDS)
        entries: list[dict[str, Any]] = []
        command_unavailable = result.returncode != 0
        if not command_unavailable:
            for line in result.stdout.splitlines():
                if not line.strip():
                    continue
                parsed = _safe_json_loads(line)
                if not isinstance(parsed, Mapping):
                    continue
                entries.append(_journal_entry(parsed, unit=unit))
                if len(entries) >= limit + 1:
                    break
        truncated = len(entries) > limit
        entries = entries[:limit]
        timestamps = [str(item.get("timestamp") or "") for item in entries if item.get("timestamp")]
        payload: dict[str, Any] = {
            "status": "unavailable" if command_unavailable else "ok",
            "generated_at": _utc_now(),
            "unit": unit,
            "priority": priority,
            "requested_limit": limit,
            "effective_window": {
                "since": window["since"].isoformat().replace("+00:00", "Z"),
                "until": window["until"].isoformat().replace("+00:00", "Z") if window.get("until") else "",
                "max_window_days": MAX_LOG_WINDOW_DAYS,
                "clamped": bool(window.get("clamped")),
            },
            "count": len(entries),
            "truncated": truncated,
            "oldest_timestamp": min(timestamps) if timestamps else "",
            "newest_timestamp": max(timestamps) if timestamps else "",
            "entries": entries,
        }
        if command_unavailable:
            payload["error"] = _redact_text(result.stderr or result.stdout or "journalctl unavailable")
        return _sanitize(payload)

    def get_refresh_diagnostics(self, *, date_from: str, date_to: str) -> dict[str, Any]:
        with self._connect() as conn:
            latest_ready = _latest_ready_snapshot(conn)
            latest_ready_in_range = _latest_ready_snapshot(conn, date_from=date_from, date_to=date_to)
            snapshots = _ready_snapshot_presence(conn, date_from=date_from, date_to=date_to)
            temporal_presence = _temporal_presence(conn, date_from=date_from, date_to=date_to)
            source_outcomes = _source_outcomes_from_latest_ready(latest_ready_in_range or latest_ready)
            auto_update_state = _single_row(conn, "sheet_vitrina_v1_auto_update_state", "slot = 1")
            manual_state = _single_row(conn, "sheet_vitrina_v1_manual_operator_state", "slot = 1")
            load_state = _single_row(conn, "sheet_vitrina_v1_load_state", "slot = 1")
            closure_rows = _closure_diagnostics(conn, date_from=date_from, date_to=date_to)
        likely_failure_area = _likely_failure_area(
            snapshots=snapshots,
            source_outcomes=source_outcomes,
            auto_update_state=auto_update_state,
            load_state=load_state,
            closure_rows=closure_rows,
            date_from=date_from,
            date_to=date_to,
        )
        return _sanitize(
            {
                "status": "ok",
                "generated_at": _utc_now(),
                "date_range": {"date_from": date_from, "date_to": date_to, "max_days": MAX_SNAPSHOT_RANGE_DAYS},
                "latest_refresh": _latest_refresh_summary(latest_ready, latest_ready_in_range, auto_update_state, manual_state),
                "latest_load": _load_summary(load_state, manual_state),
                "latest_successful": {
                    "ready_snapshot_refreshed_at": str((latest_ready or {}).get("refreshed_at") or ""),
                    "manual_refresh_at": str((manual_state or {}).get("last_successful_manual_refresh_at") or ""),
                    "manual_load_at": str((manual_state or {}).get("last_successful_manual_load_at") or ""),
                    "auto_update_at": str((auto_update_state or {}).get("last_successful_auto_update_at") or ""),
                    "load_state_loaded_at": str((load_state or {}).get("loaded_at") or ""),
                },
                "source_statuses": source_outcomes[:MAX_DIAGNOSTIC_ROWS],
                "closure_states": closure_rows[:MAX_DIAGNOSTIC_ROWS],
                "snapshot_presence": _merge_snapshot_presence(snapshots, temporal_presence, date_from=date_from, date_to=date_to),
                "likely_failure_area": likely_failure_area,
                "raw_payloads_returned": False,
                "upstream_calls": False,
                "mutations": False,
            }
        )

    def get_runtime_snapshot_status(self, *, date_from: str, date_to: str) -> dict[str, Any]:
        with self._connect() as conn:
            ready = _ready_snapshot_rows(conn, date_from=date_from, date_to=date_to)
            temporal = _temporal_snapshot_summary(conn, date_from=date_from, date_to=date_to)
            slots = _temporal_slot_snapshot_summary(conn, date_from=date_from, date_to=date_to)
        return _sanitize(
            {
                "status": "ok",
                "generated_at": _utc_now(),
                "date_range": {"date_from": date_from, "date_to": date_to, "max_days": MAX_SNAPSHOT_RANGE_DAYS},
                "ready_snapshots": ready,
                "temporal_source_snapshots": temporal,
                "temporal_source_slot_snapshots": slots,
                "summary": {
                    "ready_snapshot_count": len(ready),
                    "temporal_source_date_count": len(temporal),
                    "temporal_source_slot_bucket_count": len(slots),
                    "bounded_row_limit": MAX_DIAGNOSTIC_ROWS,
                    "raw_payloads_returned": False,
                },
            }
        )

    def get_deploy_state(self) -> dict[str, Any]:
        root = Path(__file__).resolve().parents[2]
        commit = _current_git_commit(root)
        return _sanitize(
            {
                "status": "ok",
                "generated_at": _utc_now(),
                "app": {
                    "commit": commit,
                    "commit_available": bool(commit),
                    "server_name": "webcore-data-mcp",
                    "ops_diagnostics_contract": "webcore_ops_diagnostics_v1",
                },
                "target_identity": {
                    "target_id": TARGET_ID,
                    "target_role": TARGET_ROLE,
                    "target_lifecycle": TARGET_LIFECYCLE,
                    "ssh_destination_label": SSH_DESTINATION_LABEL,
                    "public_base_url": DEFAULT_PUBLIC_BASE_URL,
                    "runtime_dir_label": RUNTIME_DIR_LABEL,
                },
                "source_mtimes": _source_mtimes(root),
                "runtime_storage": {
                    "runtime_dir_label": RUNTIME_DIR_LABEL,
                    "db": self._db_summary(),
                },
                "credential_values_returned": False,
                "raw_env_returned": False,
            }
        )

    def _read_unit_state(self, unit: str) -> dict[str, Any]:
        _required_unit(unit)
        args = [
            "systemctl",
            "show",
            "--no-pager",
            *[f"--property={item}" for item in _SYSTEMCTL_SHOW_PROPERTIES],
            unit,
        ]
        result = self.command_runner(args, COMMAND_TIMEOUT_SECONDS)
        if result.returncode != 0:
            return {
                "unit": unit,
                "status": "unavailable",
                "error": _redact_text(result.stderr or result.stdout or "systemctl unavailable"),
            }
        props = _parse_key_value_lines(result.stdout)
        return {
            "unit": unit,
            "status": "ok",
            "load_state": props.get("LoadState", ""),
            "active_state": props.get("ActiveState", ""),
            "sub_state": props.get("SubState", ""),
            "unit_file_state": props.get("UnitFileState", ""),
            "last_exit": {
                "result": props.get("Result", ""),
                "exec_main_code": props.get("ExecMainCode", ""),
                "exec_main_status": props.get("ExecMainStatus", ""),
            },
            "restart_count": _coerce_int(props.get("NRestarts")),
            "timestamps": {
                "active_enter": props.get("ActiveEnterTimestamp", ""),
                "inactive_enter": props.get("InactiveEnterTimestamp", ""),
                "state_change": props.get("StateChangeTimestamp", ""),
            },
        }

    def _disk_summary(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(self.runtime_dir)
        except Exception as exc:
            return {"status": "unavailable", "error": _redact_text(str(exc))}
        free_pct = round((usage.free / usage.total) * 100, 2) if usage.total else 0.0
        return {
            "status": "ok",
            "total_bytes": int(usage.total),
            "used_bytes": int(usage.used),
            "free_bytes": int(usage.free),
            "free_percent": free_pct,
        }

    def _db_summary(self) -> dict[str, Any]:
        try:
            stat = self.db_path.stat()
        except FileNotFoundError:
            return {"status": "missing", "db_label": DB_LABEL}
        except Exception as exc:
            return {"status": "unavailable", "db_label": DB_LABEL, "error": _redact_text(str(exc))}
        return {
            "status": "ok",
            "db_label": DB_LABEL,
            "size_bytes": int(stat.st_size),
            "mtime": _timestamp_from_epoch(stat.st_mtime),
        }

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(_sqlite_ro_uri(self.db_path), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn


def _run_fixed_command(args: Sequence[str], timeout_seconds: float) -> CommandResult:
    if not args:
        raise WebCoreOpsDiagnosticsError("empty command vector rejected")
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", "diagnostic command timed out")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _required_unit(value: Any) -> str:
    unit = str(value or "").strip()
    if unit not in ALLOWED_RUNTIME_UNITS:
        raise WebCoreOpsDiagnosticsError(f"unsupported unit: {unit}")
    return unit


def _optional_priority(value: Any) -> str:
    priority = str(value or "info").strip().lower()
    if priority not in SERVICE_LOG_PRIORITIES:
        raise WebCoreOpsDiagnosticsError(f"unsupported priority: {priority}")
    return priority


def _optional_string(value: Any, *, max_length: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) > max_length:
        raise WebCoreOpsDiagnosticsError("argument is too long")
    return text or None


def _bounded_limit(value: Any, default: int, maximum: int) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WebCoreOpsDiagnosticsError("limit must be an integer") from exc
    return max(1, min(parsed, maximum))


def _requested_date_range(args: Mapping[str, Any], *, required: bool) -> tuple[str, str]:
    single = _optional_string(args.get("date"), max_length=10)
    raw_from = _optional_string(args.get("date_from"), max_length=10)
    raw_to = _optional_string(args.get("date_to"), max_length=10)
    if single:
        if raw_from or raw_to:
            raise WebCoreOpsDiagnosticsError("use either date or date_from/date_to, not both")
        date_from = date_to = _validate_date(single, "date")
    else:
        if required and (not raw_from or not raw_to):
            raise WebCoreOpsDiagnosticsError("date or date_from/date_to is required")
        date_from = _validate_date(raw_from or raw_to or _utc_now_date(), "date_from")
        date_to = _validate_date(raw_to or raw_from or date_from, "date_to")
    from_date = date.fromisoformat(date_from)
    to_date = date.fromisoformat(date_to)
    if to_date < from_date:
        raise WebCoreOpsDiagnosticsError("date_to must be greater than or equal to date_from")
    if (to_date - from_date).days > MAX_SNAPSHOT_RANGE_DAYS:
        raise WebCoreOpsDiagnosticsError(f"date range must be <= {MAX_SNAPSHOT_RANGE_DAYS} days")
    return date_from, date_to


def _validate_date(value: str, field_name: str) -> str:
    text = str(value or "").strip()
    if not _DATE_RE.fullmatch(text):
        raise WebCoreOpsDiagnosticsError(f"{field_name} must be YYYY-MM-DD")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise WebCoreOpsDiagnosticsError(f"{field_name} must be a valid date") from exc
    return text


def _parse_log_window(*, since: str, until: str | None, now: datetime) -> dict[str, Any]:
    now = _as_utc(now)
    since_dt, clamped = _parse_since(since, now=now)
    until_dt = _parse_until(until, now=now) if until else now
    if until_dt < since_dt:
        raise WebCoreOpsDiagnosticsError("until must be greater than or equal to since")
    max_since = until_dt - timedelta(days=MAX_LOG_WINDOW_DAYS)
    if since_dt < max_since:
        since_dt = max_since
        clamped = True
    return {"since": since_dt, "until": until_dt, "clamped": clamped}


def _parse_since(value: str, *, now: datetime) -> tuple[datetime, bool]:
    text = str(value or "").strip()
    duration = _DURATION_RE.fullmatch(text)
    if duration:
        amount = int(duration.group(1))
        unit = duration.group(2).lower()
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        clamped = delta > timedelta(days=MAX_LOG_WINDOW_DAYS)
        return now - min(delta, timedelta(days=MAX_LOG_WINDOW_DAYS)), clamped
    return _parse_iso_datetime(text, field_name="since"), False


def _parse_until(value: str | None, *, now: datetime) -> datetime:
    parsed = _parse_iso_datetime(str(value or "").strip(), field_name="until")
    if parsed > now + timedelta(minutes=5):
        return now
    return parsed


def _parse_iso_datetime(value: str, *, field_name: str) -> datetime:
    if _DATE_RE.fullmatch(value):
        parsed = datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
        return parsed
    safe = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(safe)
    except ValueError as exc:
        raise WebCoreOpsDiagnosticsError(f"{field_name} must be a bounded duration or ISO timestamp") from exc
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _journal_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def _journal_entry(payload: Mapping[str, Any], *, unit: str) -> dict[str, Any]:
    timestamp = _journal_timestamp(payload)
    message = _redact_text(str(payload.get("MESSAGE") or ""))
    priority_raw = str(payload.get("PRIORITY") or "")
    return {
        "timestamp": timestamp,
        "unit": unit,
        "priority": _PRIORITY_LABELS.get(priority_raw, priority_raw),
        "identifier": _redact_text(str(payload.get("SYSLOG_IDENTIFIER") or ""))[:120],
        "pid": _safe_pid(payload.get("_PID") or payload.get("SYSLOG_PID")),
        "message": message[:1200],
    }


def _journal_timestamp(payload: Mapping[str, Any]) -> str:
    raw = str(payload.get("__REALTIME_TIMESTAMP") or "")
    if raw.isdigit():
        return _timestamp_from_epoch(int(raw) / 1_000_000)
    raw = str(payload.get("_SOURCE_REALTIME_TIMESTAMP") or payload.get("SYSLOG_TIMESTAMP") or "")
    return _redact_text(raw)[:80]


def _safe_pid(value: Any) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _parse_key_value_lines(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _redact_text(value.strip())
    return values


def _sqlite_ro_uri(path: Path) -> str:
    return "file:" + quote(str(path.expanduser().resolve()), safe="/:") + "?mode=ro"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _single_row(conn: sqlite3.Connection, table: str, where_sql: str) -> dict[str, Any] | None:
    if table not in {
        "sheet_vitrina_v1_auto_update_state",
        "sheet_vitrina_v1_manual_operator_state",
        "sheet_vitrina_v1_load_state",
    }:
        raise WebCoreOpsDiagnosticsError("internal table is not allowlisted")
    if where_sql != "slot = 1":
        raise WebCoreOpsDiagnosticsError("internal predicate is not allowlisted")
    if not _table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT * FROM {table} WHERE {where_sql} LIMIT 1").fetchone()
    return _row_dict(row) if row is not None else None


def _latest_ready_snapshot(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any] | None:
    if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
        return None
    clauses: list[str] = []
    params: list[Any] = []
    if date_from:
        clauses.append("as_of_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("as_of_date <= ?")
        params.append(date_to)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        f"""
        SELECT bundle_version, activated_at, as_of_date, snapshot_id, plan_version, refreshed_at, plan_json
        FROM sheet_vitrina_v1_ready_snapshots
        {where}
        ORDER BY refreshed_at DESC, as_of_date DESC, activated_at DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return _row_dict(row) if row is not None else None


def _ready_snapshot_rows(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "sheet_vitrina_v1_ready_snapshots"):
        return []
    rows = conn.execute(
        """
        SELECT bundle_version, activated_at, as_of_date, snapshot_id, plan_version, refreshed_at,
               length(plan_json) AS plan_json_size_bytes
        FROM sheet_vitrina_v1_ready_snapshots
        WHERE as_of_date >= ? AND as_of_date <= ?
        ORDER BY as_of_date DESC, refreshed_at DESC
        LIMIT ?
        """,
        (date_from, date_to, MAX_DIAGNOSTIC_ROWS),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _ready_snapshot_presence(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> dict[str, dict[str, Any]]:
    presence = {item: {"date": item, "ready_snapshot": False} for item in _date_span(date_from, date_to)}
    for row in _ready_snapshot_rows(conn, date_from=date_from, date_to=date_to):
        day = str(row.get("as_of_date") or "")
        if day in presence:
            presence[day].update(
                {
                    "ready_snapshot": True,
                    "snapshot_id": row.get("snapshot_id") or "",
                    "refreshed_at": row.get("refreshed_at") or "",
                    "plan_version": row.get("plan_version") or "",
                }
            )
    return presence


def _temporal_presence(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> dict[str, dict[str, Any]]:
    presence = {
        item: {"temporal_source_count": 0, "temporal_slot_count": 0, "latest_captured_at": ""}
        for item in _date_span(date_from, date_to)
    }
    if _table_exists(conn, "temporal_source_snapshots"):
        rows = conn.execute(
            """
            SELECT snapshot_date, COUNT(*) AS cnt, MAX(captured_at) AS latest_captured_at
            FROM temporal_source_snapshots
            WHERE snapshot_date >= ? AND snapshot_date <= ?
            GROUP BY snapshot_date
            """,
            (date_from, date_to),
        ).fetchall()
        for row in rows:
            day = str(row["snapshot_date"])
            if day in presence:
                presence[day]["temporal_source_count"] = int(row["cnt"] or 0)
                presence[day]["latest_captured_at"] = str(row["latest_captured_at"] or "")
    if _table_exists(conn, "temporal_source_slot_snapshots"):
        rows = conn.execute(
            """
            SELECT snapshot_date, COUNT(*) AS cnt, MAX(captured_at) AS latest_captured_at
            FROM temporal_source_slot_snapshots
            WHERE snapshot_date >= ? AND snapshot_date <= ?
            GROUP BY snapshot_date
            """,
            (date_from, date_to),
        ).fetchall()
        for row in rows:
            day = str(row["snapshot_date"])
            if day in presence:
                presence[day]["temporal_slot_count"] = int(row["cnt"] or 0)
                if str(row["latest_captured_at"] or "") > str(presence[day].get("latest_captured_at") or ""):
                    presence[day]["latest_captured_at"] = str(row["latest_captured_at"] or "")
    return presence


def _temporal_snapshot_summary(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "temporal_source_snapshots"):
        return []
    rows = conn.execute(
        """
        SELECT snapshot_date, COUNT(*) AS snapshot_count, MAX(captured_at) AS latest_captured_at,
               GROUP_CONCAT(source_key) AS source_keys
        FROM temporal_source_snapshots
        WHERE snapshot_date >= ? AND snapshot_date <= ?
        GROUP BY snapshot_date
        ORDER BY snapshot_date DESC
        LIMIT ?
        """,
        (date_from, date_to, MAX_DIAGNOSTIC_ROWS),
    ).fetchall()
    return [_compact_temporal_row(row) for row in rows]


def _temporal_slot_snapshot_summary(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "temporal_source_slot_snapshots"):
        return []
    rows = conn.execute(
        """
        SELECT snapshot_date, snapshot_role, COUNT(*) AS snapshot_count, MAX(captured_at) AS latest_captured_at,
               GROUP_CONCAT(source_key) AS source_keys
        FROM temporal_source_slot_snapshots
        WHERE snapshot_date >= ? AND snapshot_date <= ?
        GROUP BY snapshot_date, snapshot_role
        ORDER BY snapshot_date DESC, snapshot_role
        LIMIT ?
        """,
        (date_from, date_to, MAX_DIAGNOSTIC_ROWS),
    ).fetchall()
    return [_compact_temporal_row(row, include_role=True) for row in rows]


def _compact_temporal_row(row: sqlite3.Row, *, include_role: bool = False) -> dict[str, Any]:
    source_keys = sorted({item for item in str(row["source_keys"] or "").split(",") if item})
    payload = {
        "snapshot_date": row["snapshot_date"],
        "snapshot_count": int(row["snapshot_count"] or 0),
        "latest_captured_at": row["latest_captured_at"] or "",
        "source_count": len(source_keys),
        "source_keys_sample": source_keys[:20],
    }
    if include_role:
        payload["snapshot_role"] = row["snapshot_role"] or ""
    return payload


def _closure_diagnostics(conn: sqlite3.Connection, *, date_from: str, date_to: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, "temporal_source_closure_state"):
        return []
    rows = conn.execute(
        """
        SELECT source_key, target_date, slot_kind, state, attempt_count, next_retry_at, last_reason,
               last_attempt_at, last_success_at, accepted_at
        FROM temporal_source_closure_state
        WHERE target_date >= ? AND target_date <= ?
        ORDER BY target_date DESC, source_key, slot_kind
        LIMIT ?
        """,
        (date_from, date_to, MAX_DIAGNOSTIC_ROWS),
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _source_outcomes_from_latest_ready(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not row:
        return []
    payload = _safe_json_loads(str(row.get("plan_json") or ""))
    if not isinstance(payload, Mapping):
        return []
    status_sheet = None
    for sheet in payload.get("sheets") or []:
        if isinstance(sheet, Mapping) and str(sheet.get("sheet_name") or "") == "STATUS":
            status_sheet = sheet
            break
    if not isinstance(status_sheet, Mapping):
        return []
    rows = status_sheet.get("rows") if isinstance(status_sheet.get("rows"), list) else []
    outcomes: list[dict[str, Any]] = []
    for row_value in rows[:MAX_DIAGNOSTIC_ROWS]:
        if not isinstance(row_value, list) or len(row_value) < 11:
            continue
        source_key, temporal_slot = _split_temporal_key(str(row_value[0] or ""))
        if source_key in {"registry_upload_current_state", "sheet_vitrina_v1_temporal_live_v1"}:
            continue
        kind = str(row_value[1] or "").strip().lower()
        note = _redact_text(str(row_value[10] or "").strip())
        requested_count = _coerce_int(row_value[7])
        covered_count = _coerce_int(row_value[8])
        status = _source_status(kind=kind, requested_count=requested_count, covered_count=covered_count, note=note)
        outcomes.append(
            {
                "source_key": source_key,
                "temporal_slot": temporal_slot or "snapshot",
                "status": status,
                "kind": kind,
                "freshness": str(row_value[2] or "").strip(),
                "snapshot_date": str(row_value[3] or "").strip(),
                "date": str(row_value[4] or "").strip(),
                "date_from": str(row_value[5] or "").strip(),
                "date_to": str(row_value[6] or "").strip(),
                "requested_count": requested_count,
                "covered_count": covered_count,
                "note": note[:300],
            }
        )
    return outcomes


def _source_status(*, kind: str, requested_count: int, covered_count: int, note: str) -> str:
    if kind in {"error", "closure_exhausted"}:
        return "error"
    if kind in {
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
    if kind != "success":
        return "warning"
    if requested_count > 0 and covered_count < requested_count:
        return "warning"
    if note and any(marker in note.lower() for marker in ("runtime_cache", "fallback", "preserved_after_invalid_attempt")):
        return "warning"
    return "success"


def _latest_refresh_summary(
    latest_ready: Mapping[str, Any] | None,
    latest_ready_in_range: Mapping[str, Any] | None,
    auto_update_state: Mapping[str, Any] | None,
    manual_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "latest_ready_snapshot": _ready_snapshot_compact(latest_ready),
        "latest_ready_snapshot_in_requested_range": _ready_snapshot_compact(latest_ready_in_range),
        "auto_update_state": _state_subset(
            auto_update_state,
            (
                "last_run_started_at",
                "last_run_finished_at",
                "last_run_status",
                "last_run_error",
                "last_run_snapshot_id",
                "last_run_as_of_date",
                "last_run_refreshed_at",
                "last_successful_auto_update_at",
            ),
        ),
        "manual_refresh_state": _state_subset(
            manual_state,
            ("last_successful_manual_refresh_at", "last_manual_refresh_result_json"),
            summarize_json_keys={"last_manual_refresh_result_json": "last_manual_refresh_result"},
        ),
    }


def _load_summary(
    load_state: Mapping[str, Any] | None,
    manual_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "load_state": _state_subset(
            load_state,
            ("loaded_at", "snapshot_id", "as_of_date", "refreshed_at", "plan_fingerprint", "result_json"),
            summarize_json_keys={"result_json": "result"},
        ),
        "manual_load_state": _state_subset(
            manual_state,
            ("last_successful_manual_load_at", "last_manual_load_result_json"),
            summarize_json_keys={"last_manual_load_result_json": "last_manual_load_result"},
        ),
    }


def _state_subset(
    state: Mapping[str, Any] | None,
    keys: Sequence[str],
    *,
    summarize_json_keys: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not state:
        return {"status": "missing"}
    out: dict[str, Any] = {"status": "ok"}
    summaries = dict(summarize_json_keys or {})
    for key in keys:
        if key in summaries:
            out[summaries[key]] = _summarize_state_payload(str(state.get(key) or ""))
        else:
            out[key] = state.get(key)
    return out


def _summarize_state_payload(raw_json: str) -> dict[str, Any]:
    payload = _safe_json_loads(raw_json)
    if not isinstance(payload, Mapping):
        return {"present": bool(raw_json), "summary_available": False}
    allowed = {
        "status",
        "semantic_status",
        "semantic_reason",
        "snapshot_id",
        "as_of_date",
        "refreshed_at",
        "loaded_at",
        "error",
        "source_outcome_counts",
    }
    summary: dict[str, Any] = {key: payload.get(key) for key in allowed if key in payload}
    source_outcomes = payload.get("source_outcomes")
    if isinstance(source_outcomes, list):
        summary["source_outcome_count"] = len(source_outcomes)
        summary["source_outcomes_sample"] = [
            _source_outcome_subset(item) for item in source_outcomes[:20] if isinstance(item, Mapping)
        ]
    summary["present"] = True
    summary["raw_payload_returned"] = False
    return summary


def _source_outcome_subset(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source_key": item.get("source_key") or "",
        "status": item.get("status") or "",
        "reason": _redact_text(str(item.get("reason") or ""))[:300],
    }


def _ready_snapshot_compact(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"status": "missing"}
    return {
        "status": "ok",
        "bundle_version": row.get("bundle_version") or "",
        "activated_at": row.get("activated_at") or "",
        "as_of_date": row.get("as_of_date") or "",
        "snapshot_id": row.get("snapshot_id") or "",
        "plan_version": row.get("plan_version") or "",
        "refreshed_at": row.get("refreshed_at") or "",
    }


def _likely_failure_area(
    *,
    snapshots: Mapping[str, Mapping[str, Any]],
    source_outcomes: Sequence[Mapping[str, Any]],
    auto_update_state: Mapping[str, Any] | None,
    load_state: Mapping[str, Any] | None,
    closure_rows: Sequence[Mapping[str, Any]],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    missing_ready = [day for day, item in snapshots.items() if not item.get("ready_snapshot")]
    error_sources = [item for item in source_outcomes if item.get("status") == "error"]
    warning_sources = [item for item in source_outcomes if item.get("status") == "warning"]
    reasons: list[str] = []
    area = "no_obvious_persisted_failure"
    if missing_ready:
        area = "ready_snapshot_missing"
        reasons.append(f"ready snapshots missing for {len(missing_ready)} requested date(s)")
    if error_sources:
        area = "source_error"
        reasons.append(f"{len(error_sources)} source outcome(s) have error status")
    elif warning_sources and area == "no_obvious_persisted_failure":
        area = "source_warning"
        reasons.append(f"{len(warning_sources)} source outcome(s) have warning status")
    if auto_update_state and str(auto_update_state.get("last_run_status") or "").lower() == "error":
        area = "auto_refresh_error"
        reasons.append("latest auto update state is error")
    if closure_rows and any(str(item.get("state") or "") not in {"accepted", "success"} for item in closure_rows):
        if area == "no_obvious_persisted_failure":
            area = "temporal_closure_pending"
        reasons.append("temporal closure state has pending/retry/error rows")
    if load_state and not load_state.get("loaded_at") and area == "no_obvious_persisted_failure":
        area = "load_state_missing"
        reasons.append("load state has no loaded_at timestamp")
    return {
        "area": area,
        "reason": "; ".join(reasons) if reasons else f"no persisted failure marker in {date_from}..{date_to}",
        "source_error_count": len(error_sources),
        "source_warning_count": len(warning_sources),
        "missing_ready_dates": missing_ready[:MAX_DIAGNOSTIC_ROWS],
    }


def _merge_snapshot_presence(
    snapshots: Mapping[str, Mapping[str, Any]],
    temporal_presence: Mapping[str, Mapping[str, Any]],
    *,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for day in _date_span(date_from, date_to):
        item = dict(snapshots.get(day) or {"date": day, "ready_snapshot": False})
        item.update(temporal_presence.get(day) or {})
        merged.append(item)
    return merged


def _date_span(date_from: str, date_to: str) -> list[str]:
    current = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    days: list[str] = []
    while current <= end and len(days) <= MAX_SNAPSHOT_RANGE_DAYS + 1:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {str(key): row[key] for key in row.keys()}


def _safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def _split_temporal_key(value: str) -> tuple[str, str]:
    normalized = str(value or "").strip()
    if normalized.endswith("]") and "[" in normalized:
        name, slot = normalized[:-1].split("[", 1)
        return name, slot
    return normalized, ""


def _coerce_int(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _current_git_commit(root: Path) -> str:
    head = root / ".git" / "HEAD"
    try:
        head_text = head.read_text(encoding="utf-8").strip()
        if head_text.startswith("ref:"):
            ref = head_text.split(" ", 1)[1].strip()
            ref_path = root / ".git" / ref
            if ref_path.exists():
                return ref_path.read_text(encoding="utf-8").strip()[:40]
            packed = root / ".git" / "packed-refs"
            if packed.exists():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.endswith(f" {ref}"):
                        return line.split(" ", 1)[0][:40]
            return ""
        return head_text[:40]
    except Exception:
        return ""


def _source_mtimes(root: Path) -> list[dict[str, Any]]:
    labels = {
        "mcp_server": root / "apps" / "webcore_data_mcp_server.py",
        "data_gateway": root / "packages" / "application" / "webcore_data_mcp.py",
        "ops_diagnostics": root / "packages" / "application" / "webcore_ops_diagnostics.py",
        "systemd_artifact": root
        / "artifacts"
        / "registry_upload_http_entrypoint"
        / "systemd"
        / "wb-core-data-mcp.service",
        "systemd_installed_unit": Path("/etc/systemd/system/wb-core-data-mcp.service"),
    }
    rows: list[dict[str, Any]] = []
    for label, path in labels.items():
        try:
            stat = path.stat()
        except FileNotFoundError:
            rows.append({"label": label, "status": "missing"})
        except Exception as exc:
            rows.append({"label": label, "status": "unavailable", "error": _redact_text(str(exc))})
        else:
            rows.append({"label": label, "status": "ok", "mtime": _timestamp_from_epoch(stat.st_mtime), "size_bytes": stat.st_size})
    return rows


def _sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text):
                sanitized[key_text] = "[redacted]"
            else:
                sanitized[key_text] = _sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:MAX_SERVICE_LOG_LIMIT]]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_text(value: str) -> str:
    text = str(value or "")
    text = _PRIVATE_KEY_RE.sub("[redacted-private-key]", text)
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _KEY_VALUE_SECRET_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _PRIVATE_PATH_RE.sub("[redacted-path]", text)
    text = text.replace("\x00", "")
    if len(text) > 4000:
        return text[:4000] + "...[truncated]"
    return text


def _timestamp_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now() -> str:
    return _utc_now_datetime().isoformat().replace("+00:00", "Z")


def _utc_now_date() -> str:
    return _utc_now_datetime().date().isoformat()
