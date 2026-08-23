"""One-night immutable evidence wrapper for the canonical Web Vitrina refresh."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - hosted runtime is POSIX.
    fcntl = None  # type: ignore[assignment]


CONTRACT_NAME = "sheet_vitrina_v1_night_refresh_experiment"
CONTRACT_VERSION = "v1"
EXPERIMENT_ID = "web-vitrina-closed-day-2026-08-22-v1"
TARGET_DATE = "2026-08-22"
TIMEZONE_NAME = "Asia/Yekaterinburg"
TRIGGER_SOURCE = "night_refresh_experiment"
LATE_WINDOW_MINUTES = 50


@dataclass(frozen=True)
class ExperimentSlot:
    slot_id: str
    due_at: str

    @property
    def due_datetime(self) -> datetime:
        return datetime.fromisoformat(self.due_at)

    @property
    def deadline(self) -> datetime:
        return self.due_datetime + timedelta(minutes=LATE_WINDOW_MINUTES)


SLOTS = (
    ExperimentSlot("20260823T0130EKT", "2026-08-23T01:30:00+05:00"),
    ExperimentSlot("20260823T0330EKT", "2026-08-23T03:30:00+05:00"),
    ExperimentSlot("20260823T0630EKT", "2026-08-23T06:30:00+05:00"),
    ExperimentSlot("20260823T0830EKT", "2026-08-23T08:30:00+05:00"),
)


class NightRefreshExperimentRunner:
    """Run the exact one-night manifest without changing ordinary schedules."""

    def __init__(
        self,
        *,
        runtime_dir: Path,
        start_refresh: Callable[[ExperimentSlot, str], Mapping[str, Any]],
        poll_job: Callable[[str], Mapping[str, Any]],
        fetch_contract: Callable[[], Mapping[str, Any]],
        fetch_source_status: Callable[[], Mapping[str, Any]],
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.root = runtime_dir / "experiments" / EXPERIMENT_ID
        self.start_refresh = start_refresh
        self.poll_job = poll_job
        self.fetch_contract = fetch_contract
        self.fetch_source_status = fetch_source_status
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        instant = _aware_utc(now or self.now_factory())
        rows = []
        for slot in SLOTS:
            artifact = self._artifact_path(slot)
            claim = self._claim_path(slot)
            rows.append(
                {
                    "slot_id": slot.slot_id,
                    "due_at": slot.due_at,
                    "deadline": slot.deadline.isoformat(),
                    "state": (
                        "terminal"
                        if artifact.exists()
                        else "claimed"
                        if claim.exists()
                        else "expired"
                        if instant > _aware_utc(slot.deadline)
                        else "pending"
                    ),
                    "artifact_exists": artifact.exists(),
                }
            )
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "experiment_id": EXPERIMENT_ID,
            "target_date": TARGET_DATE,
            "timezone": TIMEZONE_NAME,
            "trigger_source": TRIGGER_SOURCE,
            "late_window_minutes": LATE_WINDOW_MINUTES,
            "state": self._overall_state(instant, rows),
            "slots": rows,
            "ordinary_schedule_modified": False,
            "comparison_exists": self._comparison_path().exists(),
            "experiment_root": str(self.root),
        }

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        instant = _aware_utc(now or self.now_factory())
        with self._experiment_lock():
            self._finalize_expired_slots(instant)
            selected = self._select_open_slot(instant)
            result: dict[str, Any] = {"status": "no_due_slot"}
            if selected is not None:
                result = self._run_or_recover_slot(selected, instant)
            self._write_comparison_if_terminal()
            return {**self.status(now=instant), "tick_result": result}

    def _run_or_recover_slot(
        self,
        slot: ExperimentSlot,
        instant: datetime,
    ) -> dict[str, Any]:
        claim_path = self._claim_path(slot)
        if claim_path.exists():
            claim = _read_json(claim_path)
            run_id = str(claim.get("wrapper_run_id") or "")
            accepted_path = self._accepted_path(slot, run_id)
            if not run_id or not accepted_path.exists():
                self._write_failure_artifact(
                    slot,
                    wrapper_run_id=run_id or "unknown",
                    started_at=str(claim.get("created_at") or ""),
                    reason="ambiguous_claim_without_accepted_job; duplicate launch refused",
                    reason_code="ambiguous_claim_after_restart",
                )
                return {"status": "failed", "slot_id": slot.slot_id, "reason": "ambiguous_claim_after_restart"}
            accepted = _read_json(accepted_path)
            job_id = str(accepted.get("job_id") or "")
            if not job_id:
                self._write_failure_artifact(
                    slot,
                    wrapper_run_id=run_id,
                    started_at=str(claim.get("created_at") or ""),
                    reason="accepted event has no recoverable job id",
                    reason_code="accepted_job_id_missing",
                )
                return {"status": "failed", "slot_id": slot.slot_id, "reason": "accepted_job_id_missing"}
            try:
                terminal = dict(self.poll_job(job_id))
            except Exception as exc:  # noqa: BLE001 - evidence must terminalize conservatively.
                if bool(getattr(exc, "retryable", False)) and _aware_utc(self.now_factory()) < _aware_utc(slot.deadline):
                    return {
                        "status": "poll_retry_pending",
                        "slot_id": slot.slot_id,
                        "job_id": job_id,
                        "retry_deadline": slot.deadline.isoformat(),
                    }
                self._write_failure_artifact(
                    slot,
                    wrapper_run_id=run_id,
                    started_at=str(claim.get("created_at") or ""),
                    reason=f"accepted job recovery failed: {exc}",
                    reason_code="accepted_job_recovery_failed",
                    job_id=job_id,
                )
                return {"status": "failed", "slot_id": slot.slot_id, "job_id": job_id}
            return self._archive_terminal(slot, run_id, terminal, str(claim.get("created_at") or ""))

        wrapper_run_id = uuid4().hex
        started_at = _iso_utc(instant)
        trigger_receipt_path = self._trigger_receipt_path(slot, wrapper_run_id)
        receipt = self._base_record(slot, wrapper_run_id)
        receipt.update({"event": "trigger_receipt", "created_at": started_at, "started_at": started_at})
        _write_json_exclusive(trigger_receipt_path, receipt)
        _write_json_exclusive(
            claim_path,
            {
                **self._base_record(slot, wrapper_run_id),
                "created_at": started_at,
                "trigger_receipt_path": str(trigger_receipt_path),
            },
        )
        try:
            start_payload = dict(self.start_refresh(slot, wrapper_run_id))
        except Exception as exc:  # noqa: BLE001 - transport outcome is ambiguous; never replay.
            self._write_failure_artifact(
                slot,
                wrapper_run_id=wrapper_run_id,
                started_at=started_at,
                reason=f"refresh launch failed or was ambiguous: {exc}",
                reason_code="refresh_launch_ambiguous",
            )
            return {"status": "failed", "slot_id": slot.slot_id, "reason": "refresh_launch_ambiguous"}

        if _is_busy_skip(start_payload):
            _write_json_exclusive(
                self._attempt_outcome_path(slot, wrapper_run_id, "busy"),
                {
                    **self._base_record(slot, wrapper_run_id),
                    "event": "busy_retry",
                    "finished_at": _iso_utc(self.now_factory()),
                    "reason": str(start_payload.get("reason") or start_payload.get("blocker") or "active canonical refresh"),
                    "active_job_id": str(start_payload.get("already_running_job_id") or ""),
                },
            )
            claim_path.unlink()
            return {
                "status": "busy_retry",
                "slot_id": slot.slot_id,
                "retry_deadline": slot.deadline.isoformat(),
            }

        job_id = str(start_payload.get("job_id") or start_payload.get("id") or "")
        _write_json_exclusive(
            self._accepted_path(slot, wrapper_run_id),
            {
                **self._base_record(slot, wrapper_run_id),
                "event": "refresh_accepted",
                "accepted_at": _iso_utc(self.now_factory()),
                "job_id": job_id,
                "operation": str(start_payload.get("operation") or "auto_update"),
            },
        )
        try:
            terminal = dict(self.poll_job(job_id)) if job_id else start_payload
        except Exception as exc:  # noqa: BLE001 - preserve an explicit terminal observation failure.
            if bool(getattr(exc, "retryable", False)) and _aware_utc(self.now_factory()) < _aware_utc(slot.deadline):
                return {
                    "status": "poll_retry_pending",
                    "slot_id": slot.slot_id,
                    "job_id": job_id,
                    "retry_deadline": slot.deadline.isoformat(),
                }
            self._write_failure_artifact(
                slot,
                wrapper_run_id=wrapper_run_id,
                started_at=started_at,
                reason=f"accepted refresh did not reach an observable terminal state: {exc}",
                reason_code="refresh_terminal_observation_failed",
                job_id=job_id,
            )
            return {"status": "failed", "slot_id": slot.slot_id, "job_id": job_id}
        return self._archive_terminal(slot, wrapper_run_id, terminal, started_at)

    def _archive_terminal(
        self,
        slot: ExperimentSlot,
        wrapper_run_id: str,
        terminal: Mapping[str, Any],
        started_at: str,
    ) -> dict[str, Any]:
        job_payload = _without_log_lines(terminal)
        job_id = str(job_payload.get("job_id") or "")
        job_status = str(job_payload.get("status") or "").lower()
        result = job_payload.get("result") if isinstance(job_payload.get("result"), Mapping) else {}
        semantic_status = str(result.get("semantic_status") or result.get("status") or job_status or "unknown").lower()
        contract: dict[str, Any] = {}
        source_status: dict[str, Any] = {}
        capture_errors: list[str] = []
        try:
            contract = dict(self.fetch_contract())
        except Exception as exc:  # noqa: BLE001 - terminal job evidence remains valuable.
            capture_errors.append(f"contract_capture_failed: {exc}")
        try:
            source_status = dict(self.fetch_source_status())
        except Exception as exc:  # noqa: BLE001 - optional current diagnostic surface.
            capture_errors.append(f"source_status_capture_failed: {exc}")
        payload_fingerprint = _fingerprint(contract) if contract else ""
        source_fingerprints = _source_fingerprints(result)
        observation = {
            "job": job_payload,
            "canonical_contract": contract,
            "source_status": source_status,
        }
        valid = job_status == "success" and semantic_status == "success" and bool(contract) and not capture_errors
        observation_status = "valid" if valid else "partial" if (contract or result) else "invalid"
        artifact = {
            **self._base_record(slot, wrapper_run_id),
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "started_at": started_at,
            "finished_at": str(job_payload.get("finished_at") or _iso_utc(self.now_factory())),
            "archived_at": _iso_utc(self.now_factory()),
            "job_id": job_id,
            "technical_status": job_status or "unknown",
            "semantic_status": semantic_status or "unknown",
            "observation_status": observation_status,
            "error_or_partial_reason": "; ".join(capture_errors) or str(job_payload.get("error") or result.get("semantic_reason") or ""),
            "row_counts": _row_counts(contract, result),
            "diagnostic_flags": _diagnostic_flags(observation),
            "fingerprints": {
                "canonical_payload_sha256": payload_fingerprint,
                "observation_sha256": _fingerprint(observation),
                "source_or_group_sha256": source_fingerprints,
            },
            **observation,
        }
        _write_json_exclusive(self._artifact_path(slot), artifact)
        return {
            "status": observation_status,
            "slot_id": slot.slot_id,
            "job_id": job_id,
            "artifact_path": str(self._artifact_path(slot)),
        }

    def _write_failure_artifact(
        self,
        slot: ExperimentSlot,
        *,
        wrapper_run_id: str,
        started_at: str,
        reason: str,
        reason_code: str,
        job_id: str = "",
    ) -> None:
        artifact = {
            **self._base_record(slot, wrapper_run_id),
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "started_at": started_at,
            "finished_at": _iso_utc(self.now_factory()),
            "archived_at": _iso_utc(self.now_factory()),
            "job_id": job_id,
            "technical_status": "failed",
            "semantic_status": "failed",
            "observation_status": "invalid",
            "reason_code": reason_code,
            "error_or_partial_reason": reason,
            "row_counts": {},
            "diagnostic_flags": [],
            "fingerprints": {},
            "job": {},
            "canonical_contract": {},
            "source_status": {},
        }
        _write_json_exclusive(self._artifact_path(slot), artifact)

    def _finalize_expired_slots(self, instant: datetime) -> None:
        for slot in SLOTS:
            if instant <= _aware_utc(slot.deadline) or self._artifact_path(slot).exists():
                continue
            claim = _read_json(self._claim_path(slot)) if self._claim_path(slot).exists() else {}
            self._write_failure_artifact(
                slot,
                wrapper_run_id=str(claim.get("wrapper_run_id") or f"missed-{slot.slot_id}"),
                started_at=str(claim.get("created_at") or ""),
                reason="slot deadline elapsed without an immutable terminal refresh artifact",
                reason_code="slot_deadline_elapsed",
            )

    def _select_open_slot(self, instant: datetime) -> ExperimentSlot | None:
        for slot in SLOTS:
            if self._artifact_path(slot).exists():
                continue
            if _aware_utc(slot.due_datetime) <= instant <= _aware_utc(slot.deadline):
                return slot
        return None

    def _write_comparison_if_terminal(self) -> None:
        if self._comparison_path().exists():
            return
        if not all(self._artifact_path(slot).exists() for slot in SLOTS):
            return
        artifacts = [_read_json(self._artifact_path(slot)) for slot in SLOTS]
        comparisons = []
        for previous, current in zip(artifacts, artifacts[1:]):
            previous_hash = str((previous.get("fingerprints") or {}).get("canonical_payload_sha256") or "")
            current_hash = str((current.get("fingerprints") or {}).get("canonical_payload_sha256") or "")
            comparisons.append(
                {
                    "from_slot": previous.get("slot_id"),
                    "to_slot": current.get("slot_id"),
                    "comparable": bool(previous_hash and current_hash),
                    "payload_fingerprints_equal": bool(previous_hash and current_hash and previous_hash == current_hash),
                    "from_observation_status": previous.get("observation_status"),
                    "to_observation_status": current.get("observation_status"),
                    "source_or_group_changes": _compare_source_fingerprints(previous, current),
                }
            )
        summary = {
            "contract_name": f"{CONTRACT_NAME}_comparison",
            "contract_version": CONTRACT_VERSION,
            "experiment_id": EXPERIMENT_ID,
            "target_date": TARGET_DATE,
            "timezone": TIMEZONE_NAME,
            "created_at": _iso_utc(self.now_factory()),
            "terminal": True,
            "slots": [
                {
                    "slot_id": artifact.get("slot_id"),
                    "observation_status": artifact.get("observation_status"),
                    "technical_status": artifact.get("technical_status"),
                    "semantic_status": artifact.get("semantic_status"),
                    "canonical_payload_sha256": (artifact.get("fingerprints") or {}).get("canonical_payload_sha256"),
                    "diagnostic_flags": artifact.get("diagnostic_flags") or [],
                }
                for artifact in artifacts
            ],
            "comparisons": comparisons,
            "interpretation_guard": (
                "Equal fingerprints mean only that the archived observed payloads were equal; "
                "they do not prove permanent or official source finality."
            ),
        }
        _write_json_exclusive(self._comparison_path(), summary)

    def _overall_state(self, instant: datetime, rows: list[Mapping[str, Any]]) -> str:
        if all(bool(row.get("artifact_exists")) for row in rows):
            return "terminal"
        if instant > _aware_utc(SLOTS[-1].deadline):
            return "expired"
        if instant < _aware_utc(SLOTS[0].due_datetime):
            return "armed"
        return "active"

    def _base_record(self, slot: ExperimentSlot, wrapper_run_id: str) -> dict[str, Any]:
        return {
            "experiment_id": EXPERIMENT_ID,
            "slot_id": slot.slot_id,
            "due_at": slot.due_at,
            "deadline": slot.deadline.isoformat(),
            "target_date": TARGET_DATE,
            "timezone": TIMEZONE_NAME,
            "wrapper_run_id": wrapper_run_id,
            "trigger_source": TRIGGER_SOURCE,
        }

    @contextmanager
    def _experiment_lock(self):
        lock_path = self.runtime_dir / "experiments" / f"{EXPERIMENT_ID}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _artifact_path(self, slot: ExperimentSlot) -> Path:
        return self.root / f"{slot.slot_id}.json"

    def _claim_path(self, slot: ExperimentSlot) -> Path:
        return self.root / "claims" / f"{slot.slot_id}.json"

    def _trigger_receipt_path(self, slot: ExperimentSlot, run_id: str) -> Path:
        return self.root / "attempts" / slot.slot_id / f"{run_id}-trigger.json"

    def _accepted_path(self, slot: ExperimentSlot, run_id: str) -> Path:
        return self.root / "attempts" / slot.slot_id / f"{run_id}-accepted.json"

    def _attempt_outcome_path(self, slot: ExperimentSlot, run_id: str, outcome: str) -> Path:
        return self.root / "attempts" / slot.slot_id / f"{run_id}-{outcome}.json"

    def _comparison_path(self) -> Path:
        return self.root / "comparison.json"


def _is_busy_skip(payload: Mapping[str, Any]) -> bool:
    return (
        str(payload.get("status") or "").lower() == "skipped"
        and bool(payload.get("already_running_job_id"))
        and bool(payload.get("retryable", True))
    )


def _without_log_lines(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"log_lines"}}


def _row_counts(contract: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
    rows = contract.get("rows")
    return {
        "canonical_contract_rows": len(rows) if isinstance(rows, list) else None,
        "sheet_row_counts": dict(result.get("sheet_row_counts") or {}) if isinstance(result.get("sheet_row_counts"), Mapping) else {},
        "source_outcome_counts": dict(result.get("source_outcome_counts") or {}) if isinstance(result.get("source_outcome_counts"), Mapping) else {},
        "updated_cell_count": result.get("updated_cell_count"),
        "latest_confirmed_cell_count": result.get("latest_confirmed_cell_count"),
    }


def _source_fingerprints(result: Mapping[str, Any]) -> dict[str, str]:
    outcomes = result.get("source_outcomes")
    if not isinstance(outcomes, list):
        return {}
    grouped: dict[str, list[Any]] = {}
    for index, item in enumerate(outcomes):
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("source_key") or item.get("source") or item.get("group_id") or f"source_{index}")
        grouped.setdefault(key, []).append(dict(item))
    return {key: _fingerprint(value) for key, value in sorted(grouped.items())}


def _compare_source_fingerprints(previous: Mapping[str, Any], current: Mapping[str, Any]) -> list[dict[str, Any]]:
    previous_map = dict((previous.get("fingerprints") or {}).get("source_or_group_sha256") or {})
    current_map = dict((current.get("fingerprints") or {}).get("source_or_group_sha256") or {})
    return [
        {
            "source_or_group": key,
            "comparable": bool(previous_map.get(key) and current_map.get(key)),
            "equal": bool(previous_map.get(key) and current_map.get(key) and previous_map.get(key) == current_map.get(key)),
        }
        for key in sorted(set(previous_map) | set(current_map))
    ]


def _diagnostic_flags(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    needles = ("fallback", "preserv", "latest_confirmed", "captured_at", "fetched_at", "coverage", "retry", "429")

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > 10 or len(found) >= 500:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if any(needle in str(key).lower() for needle in needles) and not isinstance(child, (Mapping, list)):
                    found.append({"path": child_path, "value": child})
                walk(child, child_path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:1000]):
                walk(child, f"{path}[{index}]", depth + 1)

    walk(payload, "", 0)
    return found


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return dict(raw) if isinstance(raw, Mapping) else {}


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")
