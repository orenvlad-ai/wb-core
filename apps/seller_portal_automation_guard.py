"""Shared Seller Portal automation guardrails.

This module owns process-wide single-flight locking for repo-owned Seller
Portal browser automation. It stores only operational metadata and never reads
or writes cookies/tokens/session payload contents.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any, Iterator, Mapping
from uuid import uuid4


DEFAULT_RUNTIME_DIR = Path(os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR", "/opt/wb-core-runtime/state"))
DEFAULT_LOCK_FILENAME = "seller_portal_automation.lock.json"
DEFAULT_STORAGE_STATE_PATH = Path("/opt/wb-web-bot/storage_state.json")
LOCK_CONTRACT_NAME = "seller_portal_automation_lock"
LOCK_CONTRACT_VERSION = "v1"
BUSY_CODE = "seller_portal_automation_busy"
LOCAL_FALLBACK_DISABLED_CODE = "local_storage_state_fallback_disabled"


class SellerPortalAutomationBusy(RuntimeError):
    def __init__(self, lock_payload: Mapping[str, Any]) -> None:
        self.lock_payload = sanitize_lock_payload(lock_payload)
        owner = str(self.lock_payload.get("owner") or "")
        run_id = str(self.lock_payload.get("run_id") or "")
        purpose = str(self.lock_payload.get("purpose") or "")
        super().__init__(f"{BUSY_CODE}: owner={owner} purpose={purpose} run_id={run_id}")


class SellerPortalStorageStatePolicyError(RuntimeError):
    def __init__(self, message: str, *, code: str = LOCAL_FALLBACK_DISABLED_CODE) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class SellerPortalAutomationLock:
    path: Path
    lock_id: str
    owner: str
    purpose: str
    run_id: str
    acquired: bool
    reentrant: bool
    stale_lock: dict[str, Any] | None = None

    def heartbeat(self) -> None:
        if not self.acquired:
            return
        payload = _read_lock_payload(self.path)
        if not _same_lock(payload, self):
            return
        payload["heartbeat_at"] = _iso_now()
        _write_lock_payload(self.path, payload)

    def release(self) -> None:
        if not self.acquired or self.reentrant:
            return
        payload = _read_lock_payload(self.path)
        if not _same_lock(payload, self):
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            return

    def public_payload(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "reentrant": self.reentrant,
            "path": str(self.path),
            "owner": self.owner,
            "purpose": self.purpose,
            "run_id": self.run_id,
            "lock_id": self.lock_id,
            "stale_lock_handled": bool(self.stale_lock),
            "stale_lock": sanitize_lock_payload(self.stale_lock or {}),
        }


@contextmanager
def seller_portal_automation_lock(
    *,
    runtime_dir: Path | str | None = None,
    owner: str,
    purpose: str,
    run_id: str,
    expected_max_seconds: int = 1800,
    wait_seconds: int = 0,
) -> Iterator[SellerPortalAutomationLock]:
    lock = acquire_seller_portal_automation_lock(
        runtime_dir=runtime_dir,
        owner=owner,
        purpose=purpose,
        run_id=run_id,
        expected_max_seconds=expected_max_seconds,
        wait_seconds=wait_seconds,
    )
    try:
        yield lock
    finally:
        lock.release()


def acquire_seller_portal_automation_lock(
    *,
    runtime_dir: Path | str | None = None,
    owner: str,
    purpose: str,
    run_id: str,
    expected_max_seconds: int = 1800,
    wait_seconds: int = 0,
) -> SellerPortalAutomationLock:
    lock_path = seller_portal_automation_lock_path(runtime_dir)
    deadline = time.monotonic() + max(0, int(wait_seconds))
    stale_payload: dict[str, Any] | None = None
    while True:
        try:
            return _try_acquire_lock(
                lock_path,
                owner=owner,
                purpose=purpose,
                run_id=run_id,
                expected_max_seconds=max(1, int(expected_max_seconds)),
                stale_payload=stale_payload,
            )
        except SellerPortalAutomationBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def seller_portal_automation_lock_path(runtime_dir: Path | str | None = None) -> Path:
    root = Path(runtime_dir).expanduser() if runtime_dir else DEFAULT_RUNTIME_DIR
    return root / DEFAULT_LOCK_FILENAME


def seller_portal_storage_state_path(default: Path = DEFAULT_STORAGE_STATE_PATH) -> Path:
    configured = str(os.environ.get("SELLER_PORTAL_STORAGE_STATE_PATH") or "").strip()
    return Path(configured).expanduser() if configured else default


def validate_storage_state_path_for_runtime(storage_state_path: Path, runtime_dir: Path | str | None) -> None:
    runtime = Path(runtime_dir).expanduser() if runtime_dir else DEFAULT_RUNTIME_DIR
    path = Path(storage_state_path).expanduser()
    if not _is_live_runtime_dir(runtime):
        return
    configured = seller_portal_storage_state_path()
    if path != configured:
        raise SellerPortalStorageStatePolicyError(
            f"EU live Seller Portal jobs must use canonical bot storage_state path {configured}; got {path}"
        )
    local_markers = ("/Users/", "/home/", "/var/folders/")
    path_text = str(path)
    if any(path_text.startswith(marker) for marker in local_markers):
        raise SellerPortalStorageStatePolicyError(
            f"EU live Seller Portal jobs cannot use local storage_state fallback: {path}"
        )


def current_lock_status(runtime_dir: Path | str | None = None) -> dict[str, Any]:
    path = seller_portal_automation_lock_path(runtime_dir)
    payload = _read_lock_payload(path)
    if not payload:
        return {"busy": False, "path": str(path)}
    active = _lock_is_active(payload)
    result = sanitize_lock_payload(payload)
    result.update({"busy": bool(active), "path": str(path), "stale": not bool(active)})
    return result


def busy_response_payload(lock_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "code": BUSY_CODE,
        "message": "Seller Portal automation already running",
        "lock": sanitize_lock_payload(lock_payload),
    }


def sanitize_lock_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    allowed = (
        "contract_name",
        "contract_version",
        "owner",
        "purpose",
        "run_id",
        "started_at",
        "pid",
        "host",
        "command",
        "expected_max_seconds",
        "heartbeat_at",
        "lock_id",
    )
    sanitized: dict[str, Any] = {}
    for key in allowed:
        if key not in payload:
            continue
        value = payload.get(key)
        if key == "command":
            sanitized[key] = _safe_command(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = _safe_text(str(value), 600)
    return sanitized


def _try_acquire_lock(
    path: Path,
    *,
    owner: str,
    purpose: str,
    run_id: str,
    expected_max_seconds: int,
    stale_payload: dict[str, Any] | None = None,
) -> SellerPortalAutomationLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_lock_payload(path)
    if existing:
        if _is_reentrant_lock(existing):
            return SellerPortalAutomationLock(
                path=path,
                lock_id=str(existing.get("lock_id") or ""),
                owner=owner,
                purpose=purpose,
                run_id=run_id,
                acquired=True,
                reentrant=True,
            )
        if _lock_is_active(existing):
            raise SellerPortalAutomationBusy(existing)
        stale_payload = sanitize_lock_payload(existing)
        _archive_stale_lock(path, existing)

    lock_id = uuid4().hex
    payload = {
        "contract_name": LOCK_CONTRACT_NAME,
        "contract_version": LOCK_CONTRACT_VERSION,
        "owner": _safe_text(owner, 160),
        "purpose": _safe_text(purpose, 160),
        "run_id": _safe_text(run_id, 180),
        "started_at": _iso_now(),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "command": _safe_command(sys.argv),
        "expected_max_seconds": expected_max_seconds,
        "heartbeat_at": _iso_now(),
        "lock_id": lock_id,
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError:
        latest = _read_lock_payload(path)
        raise SellerPortalAutomationBusy(latest or {"path": str(path)}) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return SellerPortalAutomationLock(
        path=path,
        lock_id=lock_id,
        owner=owner,
        purpose=purpose,
        run_id=run_id,
        acquired=True,
        reentrant=False,
        stale_lock=stale_payload,
    )


def _lock_is_active(payload: Mapping[str, Any]) -> bool:
    host = str(payload.get("host") or "")
    pid = _safe_int(payload.get("pid"))
    if host == socket.gethostname() and pid > 0:
        return _pid_is_running(pid)
    heartbeat = _parse_iso(str(payload.get("heartbeat_at") or payload.get("started_at") or ""))
    expected = max(60, _safe_int(payload.get("expected_max_seconds")) or 1800)
    if heartbeat is None:
        return False
    return (datetime.now(timezone.utc) - heartbeat).total_seconds() <= expected + 120


def _is_reentrant_lock(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("host") or "") == socket.gethostname() and _safe_int(payload.get("pid")) == os.getpid()


def _archive_stale_lock(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.exists():
        return
    archive = path.with_name(f"{path.name}.stale.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    try:
        path.replace(archive)
    except FileNotFoundError:
        return


def _same_lock(payload: Mapping[str, Any], lock: SellerPortalAutomationLock) -> bool:
    return (
        str(payload.get("lock_id") or "") == lock.lock_id
        and _safe_int(payload.get("pid")) == os.getpid()
        and str(payload.get("host") or "") == socket.gethostname()
    )


def _read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _write_lock_payload(path: Path, payload: Mapping[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp_path.replace(path)


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_live_runtime_dir(runtime_dir: Path) -> bool:
    return str(runtime_dir).rstrip("/") == "/opt/wb-core-runtime/state"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _safe_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


def _safe_command(value: Any) -> str:
    parts = value if isinstance(value, (list, tuple)) else str(value or "").split()
    blocked = ("token", "cookie", "secret", "password", "storage_state", "authorization", "header")
    safe_parts: list[str] = []
    for part in parts:
        text = str(part)
        lowered = text.lower()
        if any(marker in lowered for marker in blocked):
            safe_parts.append("[redacted]")
        else:
            safe_parts.append(_safe_text(text, 180))
    return " ".join(safe_parts)[:1000]


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
