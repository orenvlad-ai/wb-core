"""Fail-closed manual business-data write barrier for short maintenance windows."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


SCHEMA_VERSION = "business_data_write_barrier_v1"
STATE_FILENAME = ".business-data-write-barrier.json"
AUDIT_FILENAME = ".business-data-write-barrier-audit.jsonl"
LOCK_FILENAME = ".business-data-write-barrier.lock"
WINDOW_KINDS = frozenset({"snapshot", "final_cutover", "rollback_drill"})
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,159}")


class BusinessDataWriteBarrierError(RuntimeError):
    """The write barrier cannot be safely changed or read."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _bounded(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _validate_identifier(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if _IDENTIFIER_RE.fullmatch(normalized) is None:
        raise BusinessDataWriteBarrierError(
            f"{label} must be an exact 8..160 character identifier"
        )
    return normalized


def _validate_actor(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 160
        or any(ord(character) < 32 for character in normalized)
    ):
        raise BusinessDataWriteBarrierError(
            "actor must be an exact 1..160 character value"
        )
    return normalized


def _validate_fingerprint(value: str) -> str:
    normalized = str(value or "").strip()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise BusinessDataWriteBarrierError(
            "exact sha256 plan fingerprint is required"
        )
    return normalized


def _state_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir).resolve() / STATE_FILENAME


def _audit_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir).resolve() / AUDIT_FILENAME


def _lock_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir).resolve() / LOCK_FILENAME


def _atomic_write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        data = (_canonical_json(payload) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _append_private_audit(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.chmod(path, 0o600)
        with os.fdopen(descriptor, "ab", closefd=False) as handle:
            handle.write((_canonical_json(payload) + "\n").encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class _BarrierLock:
    def __init__(self, runtime_dir: Path) -> None:
        self.path = _lock_path(runtime_dir)
        self.descriptor = -1

    def __enter__(self) -> "_BarrierLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        os.chmod(self.path, 0o600)
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.descriptor >= 0:
            fcntl.flock(self.descriptor, fcntl.LOCK_UN)
            os.close(self.descriptor)
            self.descriptor = -1


def _load_state(runtime_dir: Path) -> dict[str, Any] | None:
    path = _state_path(runtime_dir)
    if not path.exists():
        return None
    if not path.is_file():
        raise BusinessDataWriteBarrierError(
            "write barrier state path is not a regular file"
        )
    if path.stat().st_mode & 0o077:
        raise BusinessDataWriteBarrierError(
            "write barrier state must be private mode 0600"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessDataWriteBarrierError(
            "write barrier state is unreadable"
        ) from exc
    if not isinstance(payload, dict):
        raise BusinessDataWriteBarrierError(
            "write barrier state is not a JSON object"
        )
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION:
        raise BusinessDataWriteBarrierError(
            "write barrier state schema is unknown"
        )
    return payload


def barrier_status(runtime_dir: Path) -> dict[str, Any]:
    """Return a safe readback; corrupt existing state blocks writes."""

    path = _state_path(runtime_dir)
    try:
        state = _load_state(runtime_dir)
    except BusinessDataWriteBarrierError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "invalid_fail_closed",
            "active": True,
            "phase": "invalid",
            "window_id": "",
            "window_kind": "unknown",
            "plan_fingerprint": "",
            "hold_confirmed": False,
            "message": (
                "Техническое обслуживание: изменения временно заблокированы, "
                "пока состояние защитного барьера не подтверждено."
            ),
            "state_path": str(path),
            "error": str(exc),
        }
    if state is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "inactive",
            "active": False,
            "phase": "inactive",
            "window_id": "",
            "window_kind": "",
            "plan_fingerprint": "",
            "hold_confirmed": False,
            "message": "",
            "state_path": str(path),
        }
    phase = str(state.get("phase") or "")
    active = phase in {"acquiring", "held", "restoring"}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "active" if active else "inactive",
        "active": active,
        "phase": phase,
        "window_id": _bounded(state.get("window_id"), 160),
        "window_kind": _bounded(state.get("window_kind"), 40),
        "plan_fingerprint": _bounded(state.get("plan_fingerprint"), 80),
        "hold_confirmed": bool(state.get("hold_confirmed")),
        "started_at": _bounded(state.get("started_at"), 64),
        "confirmed_at": _bounded(state.get("confirmed_at"), 64),
        "released_at": _bounded(state.get("released_at"), 64),
        "message": (
            "Короткое техническое обслуживание: чтение доступно, "
            "изменения временно заблокированы и будут включены автоматически."
            if active
            else ""
        ),
        "state_path": str(path),
        "state_fingerprint": _fingerprint(state),
    }
    return payload


def acquire_barrier(
    runtime_dir: Path,
    *,
    window_id: str,
    window_kind: str,
    plan_fingerprint: str,
    approval_reference: str,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    """Activate the HTTP/API write barrier before automatic writers drain."""

    runtime_dir = Path(runtime_dir).resolve()
    exact_window_id = _validate_identifier(window_id, label="window_id")
    exact_plan = _validate_fingerprint(plan_fingerprint)
    normalized_kind = str(window_kind or "").strip()
    if normalized_kind not in WINDOW_KINDS:
        raise BusinessDataWriteBarrierError(
            "window_kind must be snapshot, final_cutover, or rollback_drill"
        )
    normalized_actor = _validate_actor(actor)
    normalized_reason = _bounded(reason, 1000)
    normalized_approval = _validate_identifier(
        approval_reference,
        label="approval_reference",
    )
    if not normalized_reason:
        raise BusinessDataWriteBarrierError("audited barrier reason is required")
    with _BarrierLock(runtime_dir):
        existing = _load_state(runtime_dir)
        if existing is not None and str(existing.get("phase") or "") in {
            "acquiring",
            "held",
            "restoring",
        }:
            if (
                str(existing.get("window_id") or "") == exact_window_id
                and str(existing.get("plan_fingerprint") or "") == exact_plan
                and str(existing.get("window_kind") or "") == normalized_kind
            ):
                return {**barrier_status(runtime_dir), "idempotent": True}
            raise BusinessDataWriteBarrierError(
                "a different maintenance write barrier is already active"
            )
        revision = int((existing or {}).get("revision") or 0) + 1
        state: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "revision": revision,
            "phase": "acquiring",
            "active": True,
            "window_id": exact_window_id,
            "window_kind": normalized_kind,
            "plan_fingerprint": exact_plan,
            "approval_reference": normalized_approval,
            "actor": normalized_actor,
            "reason": normalized_reason,
            "started_at": _utc_now(),
            "hold_confirmed": False,
            "maintenance": {},
        }
        state["state_fingerprint"] = _fingerprint(state)
        _atomic_write_private_json(_state_path(runtime_dir), state)
        _append_private_audit(
            _audit_path(runtime_dir),
            {
                "event": "write_barrier_acquired",
                "captured_at": _utc_now(),
                "revision": revision,
                "window_id": exact_window_id,
                "window_kind": normalized_kind,
                "plan_fingerprint": exact_plan,
                "approval_reference": normalized_approval,
                "actor": normalized_actor,
                "reason": normalized_reason,
                "state_fingerprint": state["state_fingerprint"],
            },
        )
        return {**barrier_status(runtime_dir), "idempotent": False}


def confirm_barrier_hold(
    runtime_dir: Path,
    *,
    window_id: str,
    plan_fingerprint: str,
    maintenance_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the active barrier to one exact quiet writer/timer hold."""

    runtime_dir = Path(runtime_dir).resolve()
    exact_window_id = _validate_identifier(window_id, label="window_id")
    exact_plan = _validate_fingerprint(plan_fingerprint)
    if (
        str(maintenance_state.get("schema_version") or "")
        != "business_data_maintenance_v1"
        or str(maintenance_state.get("phase") or "") != "held"
        or not bool((maintenance_state.get("hold_readback") or {}).get("quiet"))
    ):
        raise BusinessDataWriteBarrierError(
            "exact quiet business-data maintenance hold is required"
        )
    with _BarrierLock(runtime_dir):
        state = _load_state(runtime_dir)
        if (
            state is None
            or str(state.get("window_id") or "") != exact_window_id
            or str(state.get("plan_fingerprint") or "") != exact_plan
            or str(state.get("phase") or "") not in {"acquiring", "held"}
        ):
            raise BusinessDataWriteBarrierError(
                "active barrier identity does not match hold confirmation"
            )
        maintenance_evidence = {
            "schema_version": str(maintenance_state.get("schema_version") or ""),
            "phase": str(maintenance_state.get("phase") or ""),
            "held_at": _bounded(maintenance_state.get("held_at"), 64),
            "quiet": True,
            "policy_revision": int(
                ((maintenance_state.get("hold_readback") or {}).get("auto_updates") or {}).get(
                    "revision"
                )
                or 0
            ),
            "state_fingerprint": _fingerprint(maintenance_state),
        }
        state.update(
            {
                "phase": "held",
                "hold_confirmed": True,
                "confirmed_at": _utc_now(),
                "maintenance": maintenance_evidence,
            }
        )
        state["state_fingerprint"] = _fingerprint(
            {key: value for key, value in state.items() if key != "state_fingerprint"}
        )
        _atomic_write_private_json(_state_path(runtime_dir), state)
        _append_private_audit(
            _audit_path(runtime_dir),
            {
                "event": "write_barrier_hold_confirmed",
                "captured_at": _utc_now(),
                "window_id": exact_window_id,
                "plan_fingerprint": exact_plan,
                "maintenance": maintenance_evidence,
                "state_fingerprint": state["state_fingerprint"],
            },
        )
        return {**barrier_status(runtime_dir), "idempotent": False}


def release_barrier(
    runtime_dir: Path,
    *,
    window_id: str,
    plan_fingerprint: str,
    actor: str,
    reason: str,
    restore_readback: Mapping[str, Any],
) -> dict[str, Any]:
    """Release only after exact writer/timer/settings restore readback."""

    runtime_dir = Path(runtime_dir).resolve()
    exact_window_id = _validate_identifier(window_id, label="window_id")
    exact_plan = _validate_fingerprint(plan_fingerprint)
    normalized_actor = _validate_actor(actor)
    normalized_reason = _bounded(reason, 1000)
    if not normalized_reason:
        raise BusinessDataWriteBarrierError("audited release reason is required")
    if (
        str(restore_readback.get("status") or "") != "restored"
        or not bool(restore_readback.get("exact_prior_state_restored"))
    ):
        raise BusinessDataWriteBarrierError(
            "write barrier release requires confirmed exact maintenance restore"
        )
    with _BarrierLock(runtime_dir):
        state = _load_state(runtime_dir)
        if state is None:
            raise BusinessDataWriteBarrierError(
                "write barrier state does not exist"
            )
        if str(state.get("phase") or "") == "released":
            if (
                str(state.get("window_id") or "") == exact_window_id
                and str(state.get("plan_fingerprint") or "") == exact_plan
            ):
                return {**barrier_status(runtime_dir), "idempotent": True}
            raise BusinessDataWriteBarrierError(
                "released barrier identity does not match"
            )
        if (
            str(state.get("window_id") or "") != exact_window_id
            or str(state.get("plan_fingerprint") or "") != exact_plan
            or not bool(state.get("hold_confirmed"))
            or str(state.get("phase") or "") not in {"held", "restoring"}
        ):
            raise BusinessDataWriteBarrierError(
                "active barrier identity/hold proof does not match release"
            )
        restore_evidence = {
            "status": "restored",
            "captured_at": _bounded(restore_readback.get("captured_at"), 64),
            "policy_revision": int(
                (restore_readback.get("auto_updates") or {}).get("revision") or 0
            ),
            "master_desired": bool(
                (restore_readback.get("auto_updates") or {}).get("master_desired")
            ),
            "exact_prior_state_restored": True,
            "control_signature": _bounded(
                restore_readback.get("control_signature"),
                80,
            ),
            "readback_fingerprint": _fingerprint(restore_readback),
        }
        state.update(
            {
                "phase": "released",
                "active": False,
                "released_at": _utc_now(),
                "released_by": normalized_actor,
                "release_reason": normalized_reason,
                "restore": restore_evidence,
            }
        )
        state["state_fingerprint"] = _fingerprint(
            {key: value for key, value in state.items() if key != "state_fingerprint"}
        )
        _atomic_write_private_json(_state_path(runtime_dir), state)
        _append_private_audit(
            _audit_path(runtime_dir),
            {
                "event": "write_barrier_released",
                "captured_at": _utc_now(),
                "window_id": exact_window_id,
                "plan_fingerprint": exact_plan,
                "actor": normalized_actor,
                "reason": normalized_reason,
                "restore": restore_evidence,
                "state_fingerprint": state["state_fingerprint"],
            },
        )
        return {**barrier_status(runtime_dir), "idempotent": False}


def abort_barrier_acquire(
    runtime_dir: Path,
    *,
    window_id: str,
    plan_fingerprint: str,
    actor: str,
    reason: str,
    restore_readback: Mapping[str, Any],
) -> dict[str, Any]:
    """Release an unconfirmed window only after exact pre-hold restore."""

    runtime_dir = Path(runtime_dir).resolve()
    exact_window_id = _validate_identifier(window_id, label="window_id")
    exact_plan = _validate_fingerprint(plan_fingerprint)
    normalized_actor = _validate_actor(actor)
    normalized_reason = _bounded(reason, 1000)
    if not normalized_reason:
        raise BusinessDataWriteBarrierError(
            "audited acquire-abort reason is required"
        )
    if (
        str(restore_readback.get("status") or "") != "restored"
        or not bool(restore_readback.get("exact_prior_state_restored"))
    ):
        raise BusinessDataWriteBarrierError(
            "write barrier acquire abort requires confirmed exact "
            "maintenance restore"
        )
    with _BarrierLock(runtime_dir):
        state = _load_state(runtime_dir)
        if state is None:
            raise BusinessDataWriteBarrierError(
                "write barrier state does not exist"
            )
        if str(state.get("phase") or "") == "released":
            if (
                str(state.get("window_id") or "") == exact_window_id
                and str(state.get("plan_fingerprint") or "") == exact_plan
                and str(state.get("release_kind") or "")
                == "acquire_aborted"
            ):
                return {**barrier_status(runtime_dir), "idempotent": True}
            raise BusinessDataWriteBarrierError(
                "released barrier identity/kind does not match acquire abort"
            )
        if (
            str(state.get("window_id") or "") != exact_window_id
            or str(state.get("plan_fingerprint") or "") != exact_plan
            or bool(state.get("hold_confirmed"))
            or str(state.get("phase") or "") != "acquiring"
        ):
            raise BusinessDataWriteBarrierError(
                "only an exact unconfirmed acquiring barrier may be aborted"
            )
        restore_evidence = {
            "status": "restored",
            "captured_at": _bounded(
                restore_readback.get("captured_at"),
                64,
            ),
            "policy_revision": int(
                (restore_readback.get("auto_updates") or {}).get(
                    "revision"
                )
                or 0
            ),
            "master_desired": bool(
                (restore_readback.get("auto_updates") or {}).get(
                    "master_desired"
                )
            ),
            "exact_prior_state_restored": True,
            "control_signature": _bounded(
                restore_readback.get("control_signature"),
                80,
            ),
            "readback_fingerprint": _fingerprint(restore_readback),
            "restore_boundary_kind": _bounded(
                restore_readback.get("restore_boundary_kind"),
                80,
            ),
            "no_hold_proof_fingerprint": _bounded(
                restore_readback.get("no_hold_proof_fingerprint"),
                80,
            ),
            "no_hold_proof": (
                dict(restore_readback.get("no_hold_proof") or {})
                if isinstance(restore_readback.get("no_hold_proof"), Mapping)
                else {}
            ),
        }
        state.update(
            {
                "phase": "released",
                "active": False,
                "released_at": _utc_now(),
                "released_by": normalized_actor,
                "release_reason": normalized_reason,
                "release_kind": "acquire_aborted",
                "restore": restore_evidence,
            }
        )
        state["state_fingerprint"] = _fingerprint(
            {
                key: value
                for key, value in state.items()
                if key != "state_fingerprint"
            }
        )
        _atomic_write_private_json(_state_path(runtime_dir), state)
        _append_private_audit(
            _audit_path(runtime_dir),
            {
                "event": "write_barrier_acquire_aborted",
                "captured_at": _utc_now(),
                "window_id": exact_window_id,
                "plan_fingerprint": exact_plan,
                "actor": normalized_actor,
                "reason": normalized_reason,
                "restore": restore_evidence,
                "state_fingerprint": state["state_fingerprint"],
            },
        )
        return {**barrier_status(runtime_dir), "idempotent": False}


def mark_barrier_restoring(
    runtime_dir: Path,
    *,
    window_id: str,
    plan_fingerprint: str,
) -> dict[str, Any]:
    """Keep HTTP writes blocked while automatic writer restore runs."""

    runtime_dir = Path(runtime_dir).resolve()
    exact_window_id = _validate_identifier(window_id, label="window_id")
    exact_plan = _validate_fingerprint(plan_fingerprint)
    with _BarrierLock(runtime_dir):
        state = _load_state(runtime_dir)
        if (
            state is None
            or str(state.get("window_id") or "") != exact_window_id
            or str(state.get("plan_fingerprint") or "") != exact_plan
            or str(state.get("phase") or "") not in {"held", "restoring"}
        ):
            raise BusinessDataWriteBarrierError(
                "active barrier identity does not match restore transition"
            )
        state["phase"] = "restoring"
        state["restore_started_at"] = str(
            state.get("restore_started_at") or _utc_now()
        )
        state["state_fingerprint"] = _fingerprint(
            {key: value for key, value in state.items() if key != "state_fingerprint"}
        )
        _atomic_write_private_json(_state_path(runtime_dir), state)
        return barrier_status(runtime_dir)


def audit_blocked_request(
    runtime_dir: Path,
    *,
    method: str,
    path: str,
    actor: str,
    request_id: str,
    remote_address: str,
) -> dict[str, Any]:
    """Audit one blocked attempt without persisting request bodies or secrets."""

    runtime_dir = Path(runtime_dir).resolve()
    status = barrier_status(runtime_dir)
    if not status["active"]:
        return {"audited": False, "reason": "barrier_inactive"}
    event = {
        "event": "manual_business_write_blocked",
        "captured_at": _utc_now(),
        "window_id": _bounded(status.get("window_id"), 160),
        "window_kind": _bounded(status.get("window_kind"), 40),
        "method": _bounded(method, 16).upper(),
        "path": _bounded(path, 500),
        "actor_fingerprint": _fingerprint(_bounded(actor, 160)),
        "request_id": _bounded(request_id, 160),
        "remote_address_fingerprint": _fingerprint(
            _bounded(remote_address, 160)
        ),
    }
    try:
        with _BarrierLock(runtime_dir):
            _append_private_audit(_audit_path(runtime_dir), event)
    except OSError as exc:
        raise BusinessDataWriteBarrierError(
            "blocked request audit could not be persisted"
        ) from exc
    return {"audited": True, "audit_fingerprint": _fingerprint(event)}
