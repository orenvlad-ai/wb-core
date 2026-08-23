"""One-slot control canary for the canonical Web Vitrina full refresh.

The canary is armed explicitly after an exact-SHA deploy.  The existing
``wb-core-sheet-vitrina-refresh.timer`` discovers the immutable manifest and
owns the trigger.  Potential SQLite-writing timers may be stopped only after
their paired services are idle; an immutable before-state is written first and
the exact active/enabled state is restored in ``finally`` or by the independent
watchdog after a crash, host restart, or hard pause expiry.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - hosted runtime is POSIX.
    fcntl = None  # type: ignore[assignment]


CONTRACT_NAME = "sheet_vitrina_v1_control_refresh_canary"
CONTRACT_VERSION = "v2"
TRIGGER_SOURCE = "night_refresh_experiment"
CONTROL_ROOT_NAME = "sheet-vitrina-control-canaries"
TARGET_DATE = "2026-08-22"
NIGHT_PLAN_CONTRACT_NAME = "sheet_vitrina_v1_night_refresh_plan"
NIGHT_PLAN_CONTRACT_VERSION = "v1"
NIGHT_PLAN_TARGET_DATE = "2026-08-23"
NIGHT_PLAN_TIMEZONE = "Asia/Yekaterinburg"
MIN_LEAD_MINUTES = 10
MAX_SLOT_WINDOW_MINUTES = 50
HARD_MAX_PAUSE_MINUTES = 25
DEFAULT_MAX_ATTEMPTS = 3

# Every entry is a checked-in repo-owned timer whose service can write the
# canonical operational SQLite.  The refresh timer is deliberately absent: it
# remains alive to own the canary and to run restart recovery.
ALLOWED_PAUSE_UNITS: dict[str, str] = {
    "wb-core-sheet-vitrina-closure-retry.timer": "wb-core-sheet-vitrina-closure-retry.service",
    "wb-core-wb-finance-weekly.timer": "wb-core-wb-finance-weekly.service",
    "wb-core-warehouse-functional-sync.timer": "wb-core-warehouse-functional-sync.service",
    "wb-core-fbs-shadow-collector.timer": "wb-core-fbs-shadow-collector.service",
}

NIGHT_PLAN_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("20260824T0130EKT", "2026-08-24T01:30:00+05:00", "2026-08-24T02:20:00+05:00"),
    ("20260824T0330EKT", "2026-08-24T03:30:00+05:00", "2026-08-24T04:20:00+05:00"),
    ("20260824T0630EKT", "2026-08-24T06:30:00+05:00", "2026-08-24T07:20:00+05:00"),
    ("20260824T0830EKT", "2026-08-24T08:30:00+05:00", "2026-08-24T09:20:00+05:00"),
)


class ControlCanaryError(RuntimeError):
    """Base error for fail-closed canary coordination."""


class ActiveWriterError(ControlCanaryError):
    def __init__(self, active_services: Sequence[Mapping[str, Any]]) -> None:
        self.active_services = [dict(row) for row in active_services]
        names = ", ".join(str(row.get("unit") or "") for row in self.active_services)
        super().__init__(f"paired writer services are not idle: {names}")


class RestoreIncompleteError(ControlCanaryError):
    pass


@dataclass(frozen=True)
class ControlCanaryManifest:
    experiment_id: str
    target_date: str
    slot_id: str
    due_at: str
    deadline: str
    expected_deployed_sha: str
    pause_units: tuple[str, ...]
    max_attempts: int
    created_at: str
    manifest_sha256: str
    parent_plan_id: str = ""
    parent_plan_manifest_sha256: str = ""

    @property
    def due_datetime(self) -> datetime:
        return _parse_datetime(self.due_at)

    @property
    def deadline_datetime(self) -> datetime:
        return _parse_datetime(self.deadline)

    def payload(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "experiment_id": self.experiment_id,
            "target_date": self.target_date,
            "slot_id": self.slot_id,
            "due_at": self.due_at,
            "deadline": self.deadline,
            "expected_deployed_sha": self.expected_deployed_sha,
            "trigger_source": TRIGGER_SOURCE,
            "pause_units": list(self.pause_units),
            "max_attempts": self.max_attempts,
            "ordinary_schedule_policy": "suppress_due_launch_while_canary_tick_is_active",
            "created_at": self.created_at,
        }
        if include_digest:
            payload["manifest_sha256"] = self.manifest_sha256
        if self.parent_plan_id:
            payload["parent_plan_id"] = self.parent_plan_id
            payload["parent_plan_manifest_sha256"] = self.parent_plan_manifest_sha256
        return payload


@dataclass(frozen=True)
class NightRefreshPlanSlot:
    slot_id: str
    due_at: str
    deadline: str
    child_experiment_id: str

    @property
    def due_datetime(self) -> datetime:
        return _parse_datetime(self.due_at)

    @property
    def deadline_datetime(self) -> datetime:
        return _parse_datetime(self.deadline)

    def payload(self) -> dict[str, str]:
        return {
            "slot_id": self.slot_id,
            "due_at": self.due_at,
            "deadline": self.deadline,
            "child_experiment_id": self.child_experiment_id,
        }


@dataclass(frozen=True)
class NightRefreshPlanManifest:
    experiment_id: str
    target_date: str
    timezone: str
    expected_deployed_sha: str
    pause_units: tuple[str, ...]
    max_attempts_per_slot: int
    slots: tuple[NightRefreshPlanSlot, ...]
    created_at: str
    manifest_sha256: str

    def payload(self, *, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_name": NIGHT_PLAN_CONTRACT_NAME,
            "contract_version": NIGHT_PLAN_CONTRACT_VERSION,
            "experiment_id": self.experiment_id,
            "target_date": self.target_date,
            "timezone": self.timezone,
            "expected_deployed_sha": self.expected_deployed_sha,
            "trigger_source": TRIGGER_SOURCE,
            "pause_units": list(self.pause_units),
            "max_attempts_per_slot": self.max_attempts_per_slot,
            "slots": [slot.payload() for slot in self.slots],
            "ordinary_schedule_modified": False,
            "automatic_terminal_expiry": True,
            "no_next_day_replay": True,
            "manual_comparison_volatile_exclusions": [
                "meta.generated_at",
                "status_summary.business_now",
            ],
            "created_at": self.created_at,
        }
        if include_digest:
            payload["manifest_sha256"] = self.manifest_sha256
        return payload


def arm_control_canary_manifest(
    *,
    runtime_dir: Path,
    experiment_id: str,
    due_at: str,
    deadline: str,
    expected_deployed_sha: str,
    pause_units: Sequence[str],
    now: datetime,
    target_date: str = TARGET_DATE,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    instant = _aware_utc(now)
    normalized_id = str(experiment_id or "").strip()
    _validate_target_date(target_date)
    if target_date not in {TARGET_DATE, NIGHT_PLAN_TARGET_DATE}:
        raise ValueError("control canary target_date is outside the two released bounded dates")
    if not _valid_control_experiment_id(normalized_id, target_date):
        raise ValueError("experiment_id must be a new date-bound control-canary id")
    normalized_sha = str(expected_deployed_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", normalized_sha) is None:
        raise ValueError("expected_deployed_sha must be an exact 40-character SHA")
    due = _parse_datetime(due_at)
    finish = _parse_datetime(deadline)
    if due < instant + timedelta(minutes=MIN_LEAD_MINUTES):
        raise ValueError(f"canary due_at must be at least {MIN_LEAD_MINUTES} minutes in the future")
    if finish <= due or finish > due + timedelta(minutes=MAX_SLOT_WINDOW_MINUTES):
        raise ValueError(f"canary deadline must be within {MAX_SLOT_WINDOW_MINUTES} minutes after due_at")
    if max_attempts < 1 or max_attempts > DEFAULT_MAX_ATTEMPTS:
        raise ValueError(f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}")
    normalized_units = tuple(dict.fromkeys(str(value or "").strip() for value in pause_units if str(value or "").strip()))
    unexpected = sorted(set(normalized_units) - set(ALLOWED_PAUSE_UNITS))
    if unexpected:
        raise ValueError(f"unrelated or unsupported pause units: {', '.join(unexpected)}")

    control_root = _control_root(runtime_dir)
    for existing_path in sorted(control_root.glob("*/manifest.json")):
        existing = _load_manifest(existing_path)
        if (
            not _control_manifest_superseded(existing_path, existing)
            and not _artifact_path(control_root / existing.experiment_id).exists()
            and instant <= existing.deadline_datetime
        ):
            raise ControlCanaryError(f"another control canary is still non-terminal: {existing.experiment_id}")

    created_at = _iso_utc(instant)
    slot_id = due.astimezone(timezone(timedelta(hours=5))).strftime("%Y%m%dT%H%MEKT")
    draft = ControlCanaryManifest(
        experiment_id=normalized_id,
        target_date=target_date,
        slot_id=slot_id,
        due_at=due_at,
        deadline=deadline,
        expected_deployed_sha=normalized_sha,
        pause_units=normalized_units,
        max_attempts=max_attempts,
        created_at=created_at,
        manifest_sha256="",
    )
    digest = _fingerprint(draft.payload(include_digest=False))
    manifest = ControlCanaryManifest(**{**draft.__dict__, "manifest_sha256": digest})
    root = control_root / normalized_id
    _write_json_exclusive(root / "manifest.json", manifest.payload())
    return {
        "status": "armed",
        "manifest": manifest.payload(),
        "manifest_path": str(root / "manifest.json"),
        "artifact_path": str(_artifact_path(root)),
    }


def arm_night_refresh_plan_manifest(
    *,
    runtime_dir: Path,
    experiment_id: str,
    expected_deployed_sha: str,
    pause_units: Sequence[str],
    now: datetime,
    max_attempts_per_slot: int = DEFAULT_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """Arm the exact owner-authorized four-slot night plan.

    One immutable parent manifest owns all four exact slots.  Date-bound child
    manifests reuse the released per-slot pause/restore runner and are
    cryptographically bound back to that parent.
    """

    instant = _aware_utc(now)
    normalized_id = str(experiment_id or "").strip()
    if re.fullmatch(r"web-vitrina-closed-day-2026-08-23-night-[A-Za-z0-9_.-]+", normalized_id) is None:
        raise ValueError("experiment_id must be a new bounded 2026-08-23 night-plan id")
    normalized_sha = str(expected_deployed_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", normalized_sha) is None:
        raise ValueError("expected_deployed_sha must be an exact 40-character SHA")
    if max_attempts_per_slot < 1 or max_attempts_per_slot > DEFAULT_MAX_ATTEMPTS:
        raise ValueError(f"max_attempts_per_slot must be between 1 and {DEFAULT_MAX_ATTEMPTS}")
    normalized_units = tuple(dict.fromkeys(str(value or "").strip() for value in pause_units if str(value or "").strip()))
    if set(normalized_units) != set(ALLOWED_PAUSE_UNITS) or len(normalized_units) != len(ALLOWED_PAUSE_UNITS):
        raise ValueError("night plan must contain the exact fresh-read SQLite conflict timer allowlist")

    slots = tuple(
        NightRefreshPlanSlot(
            slot_id=slot_id,
            due_at=due_at,
            deadline=deadline,
            child_experiment_id=(
                f"web-vitrina-closed-day-{NIGHT_PLAN_TARGET_DATE}-canary-"
                f"{normalized_id.removeprefix('web-vitrina-closed-day-2026-08-23-night-')}-{slot_id}"
            ),
        )
        for slot_id, due_at, deadline in NIGHT_PLAN_SLOTS
    )
    if slots[0].due_datetime < instant + timedelta(minutes=MIN_LEAD_MINUTES):
        raise ValueError(f"first night-plan slot must be at least {MIN_LEAD_MINUTES} minutes in the future")

    control_root = _control_root(runtime_dir)
    for existing_path in sorted(control_root.glob("*/manifest.json")):
        existing = _load_manifest(existing_path)
        if (
            not _control_manifest_superseded(existing_path, existing)
            and not _artifact_path(existing_path.parent).exists()
            and instant <= existing.deadline_datetime
        ):
            raise ControlCanaryError(f"another control canary is still non-terminal: {existing.experiment_id}")
    for existing_path in sorted(control_root.glob("*/plan.json")):
        existing = _load_night_refresh_plan(existing_path)
        existing_status = _night_plan_row(control_root, existing, instant)
        if existing_status["state"] not in {"terminal", "expired", "superseded"}:
            raise ControlCanaryError(f"another night plan is still non-terminal: {existing.experiment_id}")

    created_at = _iso_utc(instant)
    draft = NightRefreshPlanManifest(
        experiment_id=normalized_id,
        target_date=NIGHT_PLAN_TARGET_DATE,
        timezone=NIGHT_PLAN_TIMEZONE,
        expected_deployed_sha=normalized_sha,
        pause_units=normalized_units,
        max_attempts_per_slot=max_attempts_per_slot,
        slots=slots,
        created_at=created_at,
        manifest_sha256="",
    )
    digest = _fingerprint(draft.payload(include_digest=False))
    plan = NightRefreshPlanManifest(**{**draft.__dict__, "manifest_sha256": digest})
    plan_root = control_root / normalized_id
    _write_json_idempotent_exclusive(plan_root / "plan.json", plan.payload())

    child_paths: list[str] = []
    for slot in plan.slots:
        child_draft = ControlCanaryManifest(
            experiment_id=slot.child_experiment_id,
            target_date=plan.target_date,
            slot_id=slot.slot_id,
            due_at=slot.due_at,
            deadline=slot.deadline,
            expected_deployed_sha=plan.expected_deployed_sha,
            pause_units=plan.pause_units,
            max_attempts=plan.max_attempts_per_slot,
            created_at=plan.created_at,
            manifest_sha256="",
            parent_plan_id=plan.experiment_id,
            parent_plan_manifest_sha256=plan.manifest_sha256,
        )
        child_digest = _fingerprint(child_draft.payload(include_digest=False))
        child = ControlCanaryManifest(**{**child_draft.__dict__, "manifest_sha256": child_digest})
        child_path = control_root / child.experiment_id / "manifest.json"
        _write_json_idempotent_exclusive(child_path, child.payload())
        child_paths.append(str(child_path))

    status = night_refresh_plan_status(runtime_dir=runtime_dir, now=instant)
    return {
        "status": "armed",
        "manifest": plan.payload(),
        "manifest_path": str(plan_root / "plan.json"),
        "child_manifest_paths": child_paths,
        "readback": next(
            row for row in status["plans"] if row.get("experiment_id") == plan.experiment_id
        ),
    }


def rebind_night_refresh_plan_manifest(
    *,
    runtime_dir: Path,
    current_experiment_id: str,
    replacement_experiment_id: str,
    expected_deployed_sha: str,
    pause_units: Sequence[str],
    now: datetime,
) -> dict[str, Any]:
    """Supersede an untouched pre-due plan, then arm the same slots to a new SHA."""

    instant = _aware_utc(now)
    control_root = _control_root(runtime_dir)
    current_path = control_root / str(current_experiment_id or "").strip() / "plan.json"
    current = _load_night_refresh_plan(current_path)
    if instant >= current.slots[0].due_datetime:
        raise ControlCanaryError("night plan cannot be rebound at or after the first due slot")
    current_status = _night_plan_row(control_root, current, instant)
    if current_status.get("state") != "armed" or current_status.get("no_early_action") is not True:
        raise ControlCanaryError("night plan rebind requires an untouched pre-due armed plan")
    if any(
        row.get("artifact_exists")
        or int(row.get("attempt_receipt_count") or 0) > 0
        or int(row.get("pause_intent_count") or 0) > 0
        for row in current_status["slots"]
    ):
        raise ControlCanaryError("night plan rebind requires zero artifact/attempt/pause state")
    normalized_replacement = str(replacement_experiment_id or "").strip()
    normalized_sha = str(expected_deployed_sha or "").strip().lower()
    normalized_units = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in pause_units
            if str(value or "").strip()
        )
    )
    if normalized_replacement == current.experiment_id:
        raise ControlCanaryError("night plan rebind requires a new replacement experiment id")
    if re.fullmatch(
        r"web-vitrina-closed-day-2026-08-23-night-[A-Za-z0-9_.-]+",
        normalized_replacement,
    ) is None:
        raise ValueError("replacement experiment_id must be a new bounded 2026-08-23 night-plan id")
    if re.fullmatch(r"[0-9a-f]{40}", normalized_sha) is None:
        raise ValueError("replacement expected_deployed_sha must be an exact 40-character SHA")
    if normalized_sha == current.expected_deployed_sha:
        raise ControlCanaryError("night plan rebind requires actual deployed SHA drift")
    if normalized_units != current.pause_units:
        raise ControlCanaryError("night plan rebind must preserve the exact timer order and set")
    if (control_root / normalized_replacement).exists():
        raise ControlCanaryError("night plan replacement path already exists")
    superseded = {
        "contract_name": f"{NIGHT_PLAN_CONTRACT_NAME}_superseded",
        "contract_version": NIGHT_PLAN_CONTRACT_VERSION,
        "experiment_id": current.experiment_id,
        "manifest_sha256": current.manifest_sha256,
        "previous_expected_deployed_sha": current.expected_deployed_sha,
        "replacement_experiment_id": normalized_replacement,
        "replacement_expected_deployed_sha": normalized_sha,
        "reason": "pre-due exact deployed SHA drift; same four slots rebound without replay",
        "superseded_at": _iso_utc(instant),
    }
    superseded["superseded_sha256"] = _fingerprint(superseded)
    _write_json_idempotent_exclusive(current_path.parent / "superseded.json", superseded)
    replacement = arm_night_refresh_plan_manifest(
        runtime_dir=runtime_dir,
        experiment_id=normalized_replacement,
        expected_deployed_sha=normalized_sha,
        pause_units=normalized_units,
        now=instant,
        max_attempts_per_slot=current.max_attempts_per_slot,
    )
    return {
        "status": "rebound",
        "superseded": superseded,
        "replacement": replacement,
    }


def night_refresh_plan_status(*, runtime_dir: Path, now: datetime) -> dict[str, Any]:
    instant = _aware_utc(now)
    control_root = _control_root(runtime_dir)
    rows: list[dict[str, Any]] = []
    for path in sorted(control_root.glob("*/plan.json")):
        try:
            plan = _load_night_refresh_plan(path)
            rows.append(_night_plan_row(control_root, plan, instant))
        except Exception as exc:  # noqa: BLE001 - status must expose corrupt state.
            rows.append({"manifest_path": str(path), "state": "invalid_manifest", "error": str(exc)})
    return {
        "contract_name": f"{NIGHT_PLAN_CONTRACT_NAME}_status",
        "contract_version": NIGHT_PLAN_CONTRACT_VERSION,
        "observed_at": _iso_utc(instant),
        "plans": rows,
    }


def finalize_night_refresh_plans(*, runtime_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """Write the immutable four-slot comparison after every child is terminal."""

    instant = _aware_utc(now)
    control_root = _control_root(runtime_dir)
    finalized: list[dict[str, Any]] = []
    for path in sorted(control_root.glob("*/plan.json")):
        plan = _load_night_refresh_plan(path)
        if _night_plan_superseded(path.parent):
            continue
        comparison_path = path.parent / "comparison.json"
        if comparison_path.exists():
            continue
        artifacts: list[dict[str, Any]] = []
        artifact_paths: list[Path] = []
        for slot in plan.slots:
            artifact_path = _artifact_path(control_root / slot.child_experiment_id)
            if not artifact_path.exists():
                break
            artifact_paths.append(artifact_path)
            artifacts.append(_read_json(artifact_path))
        if len(artifacts) != len(plan.slots):
            continue
        slot_rows: list[dict[str, Any]] = []
        for slot, artifact, artifact_path in zip(plan.slots, artifacts, artifact_paths):
            slot_rows.append(
                {
                    **slot.payload(),
                    "artifact_path": str(artifact_path),
                    "artifact_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                    "canary_status": artifact.get("canary_status"),
                    "acceptance_passed": artifact.get("acceptance_passed"),
                    "technical_status": artifact.get("technical_status"),
                    "semantic_status": artifact.get("semantic_status"),
                    "observation_status": artifact.get("observation_status"),
                    "job_id": artifact.get("job_id"),
                    "canonical_payload_sha256": (artifact.get("fingerprints") or {}).get("canonical_payload_sha256"),
                    "source_or_group_sha256": (artifact.get("fingerprints") or {}).get("source_or_group_sha256") or {},
                    "ready_snapshot": artifact.get("ready_snapshot") or {},
                    "row_counts": artifact.get("row_counts") or {},
                }
            )
        comparisons = []
        for previous, current in zip(slot_rows, slot_rows[1:]):
            previous_hash = str(previous.get("canonical_payload_sha256") or "")
            current_hash = str(current.get("canonical_payload_sha256") or "")
            comparisons.append(
                {
                    "from_slot": previous["slot_id"],
                    "to_slot": current["slot_id"],
                    "comparable": bool(previous_hash and current_hash),
                    "raw_payload_fingerprints_equal": bool(
                        previous_hash and current_hash and previous_hash == current_hash
                    ),
                    "manual_volatile_exclusions": [
                        "meta.generated_at",
                        "status_summary.business_now",
                    ],
                }
            )
        comparison = {
            "contract_name": f"{NIGHT_PLAN_CONTRACT_NAME}_comparison",
            "contract_version": NIGHT_PLAN_CONTRACT_VERSION,
            "experiment_id": plan.experiment_id,
            "target_date": plan.target_date,
            "timezone": plan.timezone,
            "expected_deployed_sha": plan.expected_deployed_sha,
            "manifest_sha256": plan.manifest_sha256,
            "created_at": _iso_utc(instant),
            "terminal": True,
            "slot_count": len(slot_rows),
            "slots": slot_rows,
            "comparisons": comparisons,
            "manual_interpretation_guard": (
                "Raw equality is not finality. Exclude only meta.generated_at and "
                "status_summary.business_now, then verify source freshness/fallback evidence "
                "before comparing closed-day financial metrics; state metrics remain timestamped observations."
            ),
        }
        _write_json_exclusive(comparison_path, comparison)
        finalized.append({"experiment_id": plan.experiment_id, "comparison_path": str(comparison_path)})
    return finalized


def control_canary_status(*, runtime_dir: Path, now: datetime) -> dict[str, Any]:
    instant = _aware_utc(now)
    rows: list[dict[str, Any]] = []
    for path in sorted(_control_root(runtime_dir).glob("*/manifest.json")):
        try:
            manifest = _load_manifest(path)
        except Exception as exc:  # noqa: BLE001 - status must expose corrupt state.
            rows.append({"manifest_path": str(path), "state": "invalid_manifest", "error": str(exc)})
            continue
        root = path.parent
        artifact_exists = _artifact_path(root).exists()
        if _control_manifest_superseded(path, manifest):
            state = "superseded"
        elif artifact_exists:
            state = "terminal"
        elif instant > manifest.deadline_datetime:
            state = "expired_pending_terminalization"
        elif instant < manifest.due_datetime:
            state = "armed"
        else:
            state = "active"
        rows.append(
            {
                "experiment_id": manifest.experiment_id,
                "target_date": manifest.target_date,
                "slot_id": manifest.slot_id,
                "due_at": manifest.due_at,
                "deadline": manifest.deadline,
                "state": state,
                "artifact_exists": artifact_exists,
                "comparison_exists": _comparison_path(root).exists(),
                "manifest_sha256": manifest.manifest_sha256,
                "pause_units": list(manifest.pause_units),
            }
        )
    return {
        "contract_name": f"{CONTRACT_NAME}_status",
        "contract_version": CONTRACT_VERSION,
        "observed_at": _iso_utc(instant),
        "canaries": rows,
    }


class SystemdTimerCoordinator:
    """Exact timer before-state capture plus crash-safe restore."""

    def __init__(
        self,
        *,
        command_runner: Callable[[Sequence[str]], str] | None = None,
        pid: int | None = None,
        boot_id: str | None = None,
        process_start_ticks: str | None = None,
    ) -> None:
        self.command_runner = command_runner or _run_command
        self.pid = int(pid or os.getpid())
        self.boot_id = boot_id if boot_id is not None else _read_text(Path("/proc/sys/kernel/random/boot_id"))
        self.process_start_ticks = (
            process_start_ticks if process_start_ticks is not None else _process_start_ticks(self.pid)
        )

    def pause(
        self,
        *,
        experiment_root: Path,
        manifest: ControlCanaryManifest,
        now: datetime,
    ) -> dict[str, Any]:
        before = [self._timer_state(unit) for unit in manifest.pause_units]
        services = [self._service_state(ALLOWED_PAUSE_UNITS[unit]) for unit in manifest.pause_units]
        active_services = [row for row in services if str(row.get("active_state") or "") not in {"inactive", "failed"}]
        if active_services:
            raise ActiveWriterError(active_services)
        pause_id = uuid4().hex
        hard_restore_at = min(
            manifest.deadline_datetime,
            _aware_utc(now) + timedelta(minutes=HARD_MAX_PAUSE_MINUTES),
        )
        intent_payload = {
            "contract_name": f"{CONTRACT_NAME}_pause_intent",
            "contract_version": CONTRACT_VERSION,
            "experiment_id": manifest.experiment_id,
            "pause_id": pause_id,
            "created_at": _iso_utc(now),
            "reason": "bounded Web Vitrina control refresh SQLite quiet window",
            "hard_restore_at": _iso_utc(hard_restore_at),
            "owner": {
                "unit": "wb-core-sheet-vitrina-refresh.service",
                "pid": self.pid,
                "boot_id": self.boot_id,
                "process_start_ticks": self.process_start_ticks,
            },
            "before_state": before,
            "paired_services": services,
            "manifest_sha256": manifest.manifest_sha256,
        }
        intent_payload["intent_sha256"] = _fingerprint(intent_payload)
        intent_path = _pause_intent_path(experiment_root, pause_id)
        _write_json_exclusive(intent_path, intent_payload)
        try:
            for row in before:
                if str(row.get("active_state") or "") != "inactive":
                    self.command_runner(("systemctl", "stop", str(row["unit"])))
            after = [self._timer_state(unit) for unit in manifest.pause_units]
            services_after_pause = [
                self._service_state(ALLOWED_PAUSE_UNITS[unit])
                for unit in manifest.pause_units
            ]
            raced_active_services = [
                row
                for row in services_after_pause
                if str(row.get("active_state") or "") not in {"inactive", "failed"}
            ]
            if raced_active_services:
                raise ActiveWriterError(raced_active_services)
            if any(str(row.get("active_state") or "") != "inactive" for row in after):
                raise ControlCanaryError("one or more conflict timers did not become inactive")
            if any(
                str(after_row.get("unit_file_state") or "") != str(before_row.get("unit_file_state") or "")
                for before_row, after_row in zip(before, after)
            ):
                raise ControlCanaryError("pause changed desired timer enablement")
            applied = {
                "contract_name": f"{CONTRACT_NAME}_pause_applied",
                "contract_version": CONTRACT_VERSION,
                "experiment_id": manifest.experiment_id,
                "pause_id": pause_id,
                "applied_at": _iso_utc(datetime.now(timezone.utc)),
                "after_pause_state": after,
                "paired_services_after_pause": services_after_pause,
                "intent_sha256": intent_payload["intent_sha256"],
            }
            applied["applied_sha256"] = _fingerprint(applied)
            _write_json_exclusive(_pause_applied_path(experiment_root, pause_id), applied)
            return intent_payload
        except Exception:
            self.restore(experiment_root=experiment_root, intent=intent_payload, now=datetime.now(timezone.utc))
            raise

    def restore(
        self,
        *,
        experiment_root: Path,
        intent: Mapping[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        pause_id = str(intent.get("pause_id") or "")
        restored_path = _pause_restored_path(experiment_root, pause_id)
        with _file_lock(experiment_root.parent / "restore-watchdog.lock"):
            if restored_path.exists():
                return _read_json(restored_path)
            failures: list[str] = []
            before = [dict(row) for row in (intent.get("before_state") or []) if isinstance(row, Mapping)]
            for row in before:
                unit = str(row.get("unit") or "")
                try:
                    current = self._timer_state(unit)
                    if str(current.get("unit_file_state") or "") != str(row.get("unit_file_state") or ""):
                        failures.append(f"{unit}: desired enablement drifted")
                        continue
                    wanted_active = str(row.get("active_state") or "") in {"active", "activating", "reloading"}
                    current_active = str(current.get("active_state") or "") in {"active", "activating", "reloading"}
                    if wanted_active and not current_active:
                        self.command_runner(("systemctl", "start", unit))
                    elif not wanted_active and current_active:
                        self.command_runner(("systemctl", "stop", unit))
                except Exception as exc:  # noqa: BLE001 - attempt every timer before failing.
                    failures.append(f"{unit}: {exc}")
            after: list[dict[str, Any]] = []
            paired_services_after_restore: list[dict[str, Any]] = []
            for row in before:
                unit = str(row.get("unit") or "")
                try:
                    current = self._timer_state(unit)
                    after.append(current)
                    wanted_active = str(row.get("active_state") or "") in {"active", "activating", "reloading"}
                    actual_active = str(current.get("active_state") or "") in {"active", "activating", "reloading"}
                    if wanted_active != actual_active:
                        failures.append(f"{unit}: active-state restore mismatch")
                    if str(current.get("unit_file_state") or "") != str(row.get("unit_file_state") or ""):
                        failures.append(f"{unit}: unit-file-state restore mismatch")
                    had_next = bool(
                        str(row.get("next_elapse") or "")
                        or str(row.get("next_elapse_monotonic") or "")
                    )
                    has_next = bool(
                        str(current.get("next_elapse") or "")
                        or str(current.get("next_elapse_monotonic") or "")
                    )
                    if wanted_active and had_next and not has_next:
                        failures.append(f"{unit}: restored timer has no next run")
                    if str(current.get("definition_sha256") or "") != str(row.get("definition_sha256") or ""):
                        failures.append(f"{unit}: unit definition drifted")
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{unit}: restore readback failed: {exc}")
                try:
                    paired_services_after_restore.append(
                        self._service_state(ALLOWED_PAUSE_UNITS[unit])
                    )
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{unit}: paired service restore readback failed: {exc}")
            if failures:
                failure = {
                    "contract_name": f"{CONTRACT_NAME}_restore_failure",
                    "contract_version": CONTRACT_VERSION,
                    "experiment_id": str(intent.get("experiment_id") or ""),
                    "pause_id": pause_id,
                    "observed_at": _iso_utc(now),
                    "failures": failures,
                    "after_restore_state": after,
                }
                _write_json_exclusive(
                    experiment_root / "pauses" / f"{pause_id}-restore-failure-{uuid4().hex}.json",
                    failure,
                )
                raise RestoreIncompleteError("; ".join(failures))
            restored = {
                "contract_name": f"{CONTRACT_NAME}_restored",
                "contract_version": CONTRACT_VERSION,
                "experiment_id": str(intent.get("experiment_id") or ""),
                "pause_id": pause_id,
                "restored_at": _iso_utc(now),
                "restore_reason": "finally_or_watchdog",
                "before_state": before,
                "after_restore_state": after,
                "paired_services_after_restore": paired_services_after_restore,
                "intent_sha256": str(intent.get("intent_sha256") or ""),
                "restore_complete": True,
            }
            restored["restore_sha256"] = _fingerprint(restored)
            _write_json_exclusive(restored_path, restored)
            return restored

    def restore_orphans(self, *, control_root: Path, now: datetime) -> list[dict[str, Any]]:
        restored: list[dict[str, Any]] = []
        for intent_path in sorted(control_root.glob("*/pauses/*-intent.json")):
            intent = _read_json(intent_path)
            root = intent_path.parent.parent
            pause_id = str(intent.get("pause_id") or "")
            if _pause_restored_path(root, pause_id).exists():
                continue
            expired = _aware_utc(now) >= _parse_datetime(str(intent.get("hard_restore_at") or ""))
            if not expired and self._owner_alive(intent.get("owner") or {}):
                continue
            restored.append(self.restore(experiment_root=root, intent=intent, now=now))
        return restored

    def _owner_alive(self, owner: Mapping[str, Any]) -> bool:
        if str(owner.get("boot_id") or "") != self.boot_id:
            return False
        try:
            pid = int(owner.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        return _process_start_ticks(pid) == str(owner.get("process_start_ticks") or "")

    def _timer_state(self, unit: str) -> dict[str, Any]:
        if unit not in ALLOWED_PAUSE_UNITS:
            raise ValueError(f"unsupported timer: {unit}")
        values = _parse_systemctl_show(
            self.command_runner(
                (
                    "systemctl",
                    "show",
                    unit,
                    "--no-pager",
                    "-p",
                    "Id",
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "UnitFileState",
                    "-p",
                    "NextElapseUSecRealtime",
                    "-p",
                    "NextElapseUSecMonotonic",
                    "-p",
                    "LastTriggerUSec",
                )
            )
        )
        definition = self.command_runner(("systemctl", "cat", unit, "--no-pager"))
        return {
            "unit": unit,
            "active_state": values.get("ActiveState", ""),
            "sub_state": values.get("SubState", ""),
            "unit_file_state": values.get("UnitFileState", ""),
            "next_elapse": values.get("NextElapseUSecRealtime", ""),
            "next_elapse_monotonic": values.get("NextElapseUSecMonotonic", ""),
            "last_trigger": values.get("LastTriggerUSec", ""),
            "definition_sha256": hashlib.sha256(definition.encode("utf-8")).hexdigest(),
        }

    def _service_state(self, unit: str) -> dict[str, Any]:
        values = _parse_systemctl_show(
            self.command_runner(
                (
                    "systemctl",
                    "show",
                    unit,
                    "--no-pager",
                    "-p",
                    "Id",
                    "-p",
                    "ActiveState",
                    "-p",
                    "SubState",
                    "-p",
                    "ExecMainStartTimestamp",
                    "-p",
                    "ExecMainExitTimestamp",
                    "-p",
                    "Result",
                )
            )
        )
        return {
            "unit": unit,
            "active_state": values.get("ActiveState", ""),
            "sub_state": values.get("SubState", ""),
            "started_at": values.get("ExecMainStartTimestamp", ""),
            "finished_at": values.get("ExecMainExitTimestamp", ""),
            "result": values.get("Result", ""),
        }


class ControlRefreshCanaryRunner:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        start_refresh: Callable[[ControlCanaryManifest, str], Mapping[str, Any]],
        poll_job: Callable[[str, datetime], Mapping[str, Any]],
        fetch_contract: Callable[[str], Mapping[str, Any]],
        fetch_source_status: Callable[[str], Mapping[str, Any]],
        fetch_ready_snapshot: Callable[[str], Mapping[str, Any]],
        timer_coordinator: SystemdTimerCoordinator,
        read_deployed_sha: Callable[[], str],
        read_business_data_barrier: Callable[[], Mapping[str, Any]] | None = None,
        now_factory: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.control_root = _control_root(runtime_dir)
        self.start_refresh = start_refresh
        self.poll_job = poll_job
        self.fetch_contract = fetch_contract
        self.fetch_source_status = fetch_source_status
        self.fetch_ready_snapshot = fetch_ready_snapshot
        self.timer_coordinator = timer_coordinator
        self.read_deployed_sha = read_deployed_sha
        self.read_business_data_barrier = read_business_data_barrier
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        if sleep is None:
            import time

            self.sleep = time.sleep
        else:
            self.sleep = sleep

    def tick(self, *, now: datetime | None = None) -> dict[str, Any]:
        instant = _aware_utc(now or self.now_factory())
        watchdog_restore = self.timer_coordinator.restore_orphans(control_root=self.control_root, now=instant)
        with _file_lock(self.control_root / "runner.lock"):
            manifest = self._selected_manifest(instant)
            if manifest is None:
                return {"status": "no_due_canary", "watchdog_restore": watchdog_restore}
            root = self.control_root / manifest.experiment_id
            if _artifact_path(root).exists():
                return {"status": "terminal", "experiment_id": manifest.experiment_id}
            if instant > manifest.deadline_datetime:
                artifact = self._archive(
                    manifest=manifest,
                    outcome={"reason_code": "canary_deadline_elapsed", "error": "canary deadline elapsed"},
                    restore_error="",
                )
                return {"status": "failed", "artifact": artifact}
            if instant < manifest.due_datetime:
                return {"status": "armed", "experiment_id": manifest.experiment_id}
            actual_deployed_sha = str(self.read_deployed_sha() or "").strip().lower()
            if actual_deployed_sha != manifest.expected_deployed_sha:
                artifact = self._archive(
                    manifest=manifest,
                    outcome={
                        "terminal": True,
                        "reason_code": "deployed_sha_drift",
                        "error": (
                            "control canary deployed SHA drift: "
                            f"expected={manifest.expected_deployed_sha} actual={actual_deployed_sha}"
                        ),
                    },
                    restore_error="",
                )
                return {"status": "failed", "artifact": artifact}

            barrier_readback: dict[str, Any] = {
                "status": "not_configured",
                "active": False,
            }
            if self.read_business_data_barrier is not None:
                try:
                    barrier_readback = dict(self.read_business_data_barrier())
                except Exception as exc:  # noqa: BLE001 - ambiguous barrier state is fail closed.
                    return {
                        "status": "waiting_for_business_data_barrier",
                        "experiment_id": manifest.experiment_id,
                        "barrier": {
                            "status": "readback_failed",
                            "active": True,
                            "error": str(exc),
                        },
                    }
                if barrier_readback.get("active") is not False:
                    return {
                        "status": "waiting_for_business_data_barrier",
                        "experiment_id": manifest.experiment_id,
                        "barrier": barrier_readback,
                    }

            try:
                pause_intent = self.timer_coordinator.pause(
                    experiment_root=root,
                    manifest=manifest,
                    now=instant,
                )
            except ActiveWriterError as exc:
                return {
                    "status": "waiting_for_idle_writers",
                    "experiment_id": manifest.experiment_id,
                    "active_services": exc.active_services,
                }

            outcome: dict[str, Any] = {}
            restore_error = ""
            try:
                outcome = self._run_attempts(manifest)
            finally:
                try:
                    self.timer_coordinator.restore(
                        experiment_root=root,
                        intent=pause_intent,
                        now=_aware_utc(self.now_factory()),
                    )
                except Exception as exc:  # noqa: BLE001 - artifact must expose material failure.
                    restore_error = str(exc)
            outcome["business_data_barrier_preflight"] = barrier_readback
            if not bool(outcome.get("terminal")) and not restore_error:
                return {
                    "status": str(outcome.get("status") or "retry_pending"),
                    "experiment_id": manifest.experiment_id,
                    "attempt_count": len(self._attempt_lineage(root)),
                }
            artifact = self._archive(manifest=manifest, outcome=outcome, restore_error=restore_error)
            return {"status": str(artifact.get("canary_status") or "failed"), "artifact": artifact}

    def _selected_manifest(self, instant: datetime) -> ControlCanaryManifest | None:
        candidates = [
            (path, _load_manifest(path))
            for path in sorted(self.control_root.glob("*/manifest.json"))
        ]
        candidates = [
            item
            for path, item in candidates
            if not _control_manifest_superseded(path, item)
            and not _artifact_path(self.control_root / item.experiment_id).exists()
            and instant >= item.due_datetime
        ]
        return min(candidates, key=lambda item: item.due_datetime) if candidates else None

    def _run_attempts(self, manifest: ControlCanaryManifest) -> dict[str, Any]:
        root = self.control_root / manifest.experiment_id
        ambiguous = self._ambiguous_trigger(root)
        if ambiguous:
            return {
                "terminal": True,
                "reason_code": "refresh_launch_ambiguous",
                "error": f"trigger receipt has no accepted/busy/terminal outcome: {ambiguous}",
            }
        while _aware_utc(self.now_factory()) <= manifest.deadline_datetime:
            outstanding = self._outstanding_accepted(root)
            if outstanding:
                attempt_id = str(outstanding.get("attempt_id") or "")
                job_id = str(outstanding.get("job_id") or "")
                try:
                    terminal = dict(self.poll_job(job_id, manifest.deadline_datetime))
                except Exception as exc:  # typed retry exhaustion stays non-terminal before exact deadline.
                    if bool(getattr(exc, "retryable", False)) and _aware_utc(self.now_factory()) < manifest.deadline_datetime:
                        return {"terminal": False, "status": "poll_retry_pending", "job_id": job_id}
                    return {
                        "terminal": True,
                        "reason_code": "refresh_terminal_observation_failed",
                        "error": str(exc),
                        "job_id": job_id,
                    }
                event = self._write_attempt_terminal(root, manifest, attempt_id, terminal)
                if _technical_success(terminal):
                    return {
                        "terminal": True,
                        "terminal_job": terminal,
                        "terminal_event": event,
                        "capture": self._capture_success(manifest),
                    }
                if _retryable_terminal_contention(terminal):
                    if self._accepted_count(root) >= manifest.max_attempts:
                        if _aware_utc(self.now_factory()) < manifest.deadline_datetime:
                            return {"terminal": False, "status": "attempts_exhausted_waiting_deadline"}
                        break
                    delay = min(30.0, 5.0 * (2 ** max(0, self._accepted_count(root) - 1)))
                    remaining = (manifest.deadline_datetime - _aware_utc(self.now_factory())).total_seconds()
                    if remaining <= 0:
                        break
                    self.sleep(min(delay, remaining))
                    continue
                return {"terminal": True, "terminal_job": terminal, "terminal_event": event}

            if self._accepted_count(root) >= manifest.max_attempts:
                return {"terminal": False, "status": "attempts_exhausted_waiting_deadline"}
            attempt_id = uuid4().hex
            attempt_number = self._trigger_count(root) + 1
            trigger = {
                **manifest.payload(),
                "event": "trigger_receipt",
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "triggered_at": _iso_utc(self.now_factory()),
            }
            _write_json_exclusive(_attempt_path(root, attempt_number, attempt_id, "trigger"), trigger)
            try:
                started = dict(self.start_refresh(manifest, attempt_id))
            except Exception as exc:  # post transport is ambiguous; never replay.
                return {
                    "terminal": True,
                    "reason_code": "refresh_launch_ambiguous",
                    "error": str(exc),
                }
            if _is_busy_skip(started):
                busy = {
                    **manifest.payload(),
                    "event": "busy_retry",
                    "attempt_id": attempt_id,
                    "attempt_number": attempt_number,
                    "observed_at": _iso_utc(self.now_factory()),
                    "active_job_id": str(started.get("already_running_job_id") or ""),
                    "active_job_stale": bool(started.get("active_job_stale")),
                    "reason": str(started.get("reason") or started.get("blocker") or "active canonical job"),
                }
                _write_json_exclusive(_attempt_path(root, attempt_number, attempt_id, "busy"), busy)
                return {"terminal": False, "status": "active_job_retry_pending"}
            job_id = str(started.get("job_id") or started.get("id") or "")
            if not job_id:
                terminal = {
                    **manifest.payload(),
                    "event": "attempt_terminal",
                    "attempt_id": attempt_id,
                    "attempt_number": attempt_number,
                    "technical_status": "failed",
                    "reason_code": "accepted_job_id_missing",
                    "finished_at": _iso_utc(self.now_factory()),
                }
                _write_json_exclusive(_attempt_path(root, attempt_number, attempt_id, "terminal"), terminal)
                return {"terminal": True, "reason_code": "accepted_job_id_missing", "error": "accepted response has no job id"}
            accepted = {
                **manifest.payload(),
                "event": "refresh_accepted",
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "accepted_at": _iso_utc(self.now_factory()),
                "job_id": job_id,
                "operation": str(started.get("operation") or "auto_update"),
                "server_single_flight_no_active_job_proof": True,
            }
            _write_json_exclusive(_attempt_path(root, attempt_number, attempt_id, "accepted"), accepted)

        return {
            "terminal": True,
            "reason_code": "canary_deadline_elapsed",
            "error": "canary deadline elapsed before one accepted technical success",
        }

    def _write_attempt_terminal(
        self,
        root: Path,
        manifest: ControlCanaryManifest,
        attempt_id: str,
        terminal: Mapping[str, Any],
    ) -> dict[str, Any]:
        accepted = next(
            row
            for row in self._attempt_lineage(root)
            if row.get("event") == "refresh_accepted" and row.get("attempt_id") == attempt_id
        )
        event = {
            **manifest.payload(),
            "event": "attempt_terminal",
            "attempt_id": attempt_id,
            "attempt_number": int(accepted.get("attempt_number") or 0),
            "job_id": str(accepted.get("job_id") or terminal.get("job_id") or ""),
            "finished_at": str(terminal.get("finished_at") or _iso_utc(self.now_factory())),
            "technical_status": str(terminal.get("status") or "unknown").lower(),
            "semantic_status": _semantic_status(terminal),
            "retryable_contention": _retryable_terminal_contention(terminal),
            "job": _without_log_lines(terminal),
        }
        _write_json_exclusive(
            _attempt_path(root, int(accepted.get("attempt_number") or 0), attempt_id, "terminal"),
            event,
        )
        return event

    def _capture_success(self, manifest: ControlCanaryManifest) -> dict[str, Any]:
        captured: dict[str, Any] = {
            "canonical_contract": {},
            "fresh_canonical_contract": {},
            "source_status": {},
            "ready_snapshot": {},
            "capture_errors": [],
        }
        for label, callback in (
            ("canonical_contract", lambda: self.fetch_contract(manifest.target_date)),
            ("source_status", lambda: self.fetch_source_status(manifest.target_date)),
            ("ready_snapshot", lambda: self.fetch_ready_snapshot(manifest.target_date)),
            ("fresh_canonical_contract", lambda: self.fetch_contract(manifest.target_date)),
        ):
            try:
                captured[label] = dict(callback())
            except Exception as exc:  # noqa: BLE001 - retain observed terminal job evidence.
                captured["capture_errors"].append(f"{label}_capture_failed: {exc}")
        return captured

    def _archive(
        self,
        *,
        manifest: ControlCanaryManifest,
        outcome: Mapping[str, Any],
        restore_error: str,
    ) -> dict[str, Any]:
        root = self.control_root / manifest.experiment_id
        terminal_job = dict(outcome.get("terminal_job") or {}) if isinstance(outcome.get("terminal_job"), Mapping) else {}
        technical_status = str(terminal_job.get("status") or ("failed" if outcome.get("reason_code") else "unknown")).lower()
        semantic_status = _semantic_status(terminal_job) if terminal_job else "failed"
        capture = dict(outcome.get("capture") or {}) if isinstance(outcome.get("capture"), Mapping) else {}
        contract = dict(capture.get("canonical_contract") or {})
        fresh_contract = dict(capture.get("fresh_canonical_contract") or {})
        source_status = dict(capture.get("source_status") or {})
        ready_snapshot = dict(capture.get("ready_snapshot") or {})
        capture_errors = [str(value) for value in (capture.get("capture_errors") or [])]
        archived_at = _aware_utc(self.now_factory())
        fingerprint = _fingerprint(contract) if contract else ""
        fresh_fingerprint = _fingerprint(fresh_contract) if fresh_contract else ""
        fresh_diff_paths = _recursive_diff_paths(contract, fresh_contract)
        known_volatile_difference = set(fresh_diff_paths) == {
            "meta.generated_at",
            "status_summary.business_now",
        }
        attempts = self._attempt_lineage(root)
        pauses, restores = self._pause_lineage(root)
        accepted = [row for row in attempts if row.get("event") == "refresh_accepted"]
        successful = [row for row in attempts if row.get("event") == "attempt_terminal" and row.get("technical_status") == "success"]
        refreshed_at = _parse_optional_datetime(str(ready_snapshot.get("refreshed_at") or ""))
        checks = {
            "trigger_and_job_receipts": bool(accepted and str(accepted[-1].get("job_id") or "")),
            "terminal_technical_success": technical_status == "success",
            "semantic_status_allowed": semantic_status in {"success", "warning"},
            "ready_snapshot_exact_date": str(ready_snapshot.get("as_of_date") or "") == manifest.target_date,
            "ready_snapshot_refreshed_in_canary_window": bool(
                refreshed_at and manifest.due_datetime <= refreshed_at <= archived_at
            ),
            "canonical_contract_nonempty": bool(contract),
            "source_status_nonempty": bool(source_status),
            "row_counts_nonempty": bool(_row_counts(contract, terminal_job)),
            "fingerprints_nonempty": bool(fingerprint and fresh_fingerprint),
            "fresh_exact_date_fingerprint_match": bool(fingerprint and fingerprint == fresh_fingerprint),
            "fresh_fingerprint_difference_classified": bool(
                fingerprint
                and fresh_fingerprint
                and (fingerprint == fresh_fingerprint or known_volatile_difference)
            ),
            "bounded_attempts": 0 < len(accepted) <= manifest.max_attempts,
            "exactly_one_accepted_success": len(successful) == 1,
            "all_pauses_restored": bool(pauses) and len(restores) == len(pauses) and all(bool(row.get("restore_complete")) for row in restores),
            "restore_error_absent": not restore_error,
        }
        acceptance_passed = all(checks.values())
        if acceptance_passed and semantic_status == "warning":
            canary_status = "accepted_with_warning"
            observation_status = "partial"
        elif acceptance_passed:
            canary_status = "accepted"
            observation_status = "valid"
        else:
            canary_status = "failed"
            observation_status = "invalid" if not contract else "partial"
        result = terminal_job.get("result") if isinstance(terminal_job.get("result"), Mapping) else {}
        artifact = {
            **manifest.payload(),
            "archived_at": _iso_utc(archived_at),
            "canary_status": canary_status,
            "acceptance_passed": acceptance_passed,
            "acceptance_checks": checks,
            "technical_status": technical_status,
            "semantic_status": semantic_status,
            "observation_status": observation_status,
            "reason_code": str(outcome.get("reason_code") or ""),
            "error_or_partial_reason": "; ".join(capture_errors)
            or restore_error
            or str(outcome.get("error") or terminal_job.get("error") or result.get("semantic_reason") or ""),
            "job_id": str(terminal_job.get("job_id") or (accepted[-1].get("job_id") if accepted else "")),
            "attempts": attempts,
            "pause_intents": pauses,
            "restore_receipts": restores,
            "row_counts": _row_counts(contract, terminal_job),
            "diagnostic_flags": _diagnostic_flags({"job": terminal_job, "contract": contract, "source_status": source_status}),
            "fingerprints": {
                "canonical_payload_sha256": fingerprint,
                "fresh_readback_payload_sha256": fresh_fingerprint,
                "payloads_equal": bool(fingerprint and fingerprint == fresh_fingerprint),
                "fresh_readback_diff_paths": fresh_diff_paths,
                "known_volatile_only_difference": known_volatile_difference,
                "source_or_group_sha256": _source_fingerprints(result),
            },
            "ready_snapshot": ready_snapshot,
            "canonical_contract": contract,
            "fresh_canonical_contract": fresh_contract,
            "source_status": source_status,
            "job": _without_log_lines(terminal_job),
            "business_data_barrier_preflight": dict(
                outcome.get("business_data_barrier_preflight") or {}
            ),
        }
        _write_json_exclusive(_artifact_path(root), artifact)
        comparison = {
            "contract_name": f"{CONTRACT_NAME}_comparison",
            "contract_version": CONTRACT_VERSION,
            "experiment_id": manifest.experiment_id,
            "target_date": manifest.target_date,
            "created_at": _iso_utc(self.now_factory()),
            "terminal": True,
            "slot_count": 1,
            "observation_status": observation_status,
            "canary_status": canary_status,
            "canonical_payload_sha256": fingerprint,
            "fresh_readback_payload_sha256": fresh_fingerprint,
            "payload_fingerprints_equal": bool(fingerprint and fingerprint == fresh_fingerprint),
            "interpretation_guard": "One control canary proves only this exact technical refresh and archived payload, not permanent upstream finality.",
        }
        _write_json_exclusive(_comparison_path(root), comparison)
        return artifact

    def _attempt_lineage(self, root: Path) -> list[dict[str, Any]]:
        rows = [
            _read_json(path)
            for path in sorted((root / "attempts").glob("*.json"))
            if path.is_file()
        ]
        event_order = {"trigger_receipt": 0, "busy_retry": 1, "refresh_accepted": 1, "attempt_terminal": 2}
        return sorted(
            rows,
            key=lambda row: (
                int(row.get("attempt_number") or 0),
                event_order.get(str(row.get("event") or ""), 99),
                str(row.get("attempt_id") or ""),
            ),
        )

    def _pause_lineage(self, root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        intents = [_read_json(path) for path in sorted((root / "pauses").glob("*-intent.json"))]
        restores = [_read_json(path) for path in sorted((root / "pauses").glob("*-restored.json"))]
        return intents, restores

    def _trigger_count(self, root: Path) -> int:
        return sum(1 for row in self._attempt_lineage(root) if row.get("event") == "trigger_receipt")

    def _accepted_count(self, root: Path) -> int:
        return sum(1 for row in self._attempt_lineage(root) if row.get("event") == "refresh_accepted")

    def _outstanding_accepted(self, root: Path) -> dict[str, Any] | None:
        rows = self._attempt_lineage(root)
        terminals = {str(row.get("attempt_id") or "") for row in rows if row.get("event") == "attempt_terminal"}
        accepted = [row for row in rows if row.get("event") == "refresh_accepted" and str(row.get("attempt_id") or "") not in terminals]
        return accepted[-1] if accepted else None

    def _ambiguous_trigger(self, root: Path) -> str:
        rows = self._attempt_lineage(root)
        completed = {
            str(row.get("attempt_id") or "")
            for row in rows
            if row.get("event") in {"refresh_accepted", "busy_retry", "attempt_terminal"}
        }
        for row in rows:
            attempt_id = str(row.get("attempt_id") or "")
            if row.get("event") == "trigger_receipt" and attempt_id not in completed:
                return attempt_id
        return ""


def _load_manifest(path: Path) -> ControlCanaryManifest:
    payload = _read_json(path)
    if payload.get("contract_name") != CONTRACT_NAME or payload.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"invalid control canary manifest contract: {path}")
    expected = str(payload.get("manifest_sha256") or "")
    actual = _fingerprint({key: value for key, value in payload.items() if key != "manifest_sha256"})
    if not expected or expected != actual:
        raise ValueError(f"control canary manifest digest mismatch: {path}")
    units = tuple(str(value or "") for value in (payload.get("pause_units") or []))
    if set(units) - set(ALLOWED_PAUSE_UNITS):
        raise ValueError(f"control canary manifest contains unrelated pause units: {path}")
    if len(units) != len(set(units)):
        raise ValueError(f"control canary manifest contains duplicate pause units: {path}")
    manifest = ControlCanaryManifest(
        experiment_id=str(payload.get("experiment_id") or ""),
        target_date=str(payload.get("target_date") or ""),
        slot_id=str(payload.get("slot_id") or ""),
        due_at=str(payload.get("due_at") or ""),
        deadline=str(payload.get("deadline") or ""),
        expected_deployed_sha=str(payload.get("expected_deployed_sha") or ""),
        pause_units=units,
        max_attempts=int(payload.get("max_attempts") or 0),
        created_at=str(payload.get("created_at") or ""),
        manifest_sha256=expected,
        parent_plan_id=str(payload.get("parent_plan_id") or ""),
        parent_plan_manifest_sha256=str(payload.get("parent_plan_manifest_sha256") or ""),
    )
    _validate_target_date(manifest.target_date)
    if manifest.target_date not in {TARGET_DATE, NIGHT_PLAN_TARGET_DATE}:
        raise ValueError(f"control canary target date is outside the released bounded dates: {path}")
    if not _valid_control_experiment_id(manifest.experiment_id, manifest.target_date):
        raise ValueError(f"invalid control canary experiment id: {path}")
    if re.fullmatch(r"[0-9a-f]{40}", manifest.expected_deployed_sha) is None:
        raise ValueError(f"invalid control canary deployed SHA: {path}")
    if manifest.max_attempts < 1 or manifest.max_attempts > DEFAULT_MAX_ATTEMPTS:
        raise ValueError(f"invalid control canary max attempts: {path}")
    if (
        manifest.deadline_datetime <= manifest.due_datetime
        or manifest.deadline_datetime > manifest.due_datetime + timedelta(minutes=MAX_SLOT_WINDOW_MINUTES)
    ):
        raise ValueError(f"invalid control canary time window: {path}")
    if bool(manifest.parent_plan_id) != bool(manifest.parent_plan_manifest_sha256):
        raise ValueError(f"incomplete parent night-plan binding: {path}")
    if manifest.parent_plan_id:
        plan_path = path.parent.parent / manifest.parent_plan_id / "plan.json"
        plan = _load_night_refresh_plan(plan_path)
        if plan.manifest_sha256 != manifest.parent_plan_manifest_sha256:
            raise ValueError(f"parent night-plan digest mismatch: {path}")
        slot = next(
            (item for item in plan.slots if item.child_experiment_id == manifest.experiment_id),
            None,
        )
        if slot is None:
            raise ValueError(f"child manifest is absent from parent night plan: {path}")
        if (
            manifest.target_date != plan.target_date
            or manifest.expected_deployed_sha != plan.expected_deployed_sha
            or manifest.pause_units != plan.pause_units
            or manifest.max_attempts != plan.max_attempts_per_slot
            or manifest.slot_id != slot.slot_id
            or manifest.due_at != slot.due_at
            or manifest.deadline != slot.deadline
        ):
            raise ValueError(f"child manifest drifted from parent night plan: {path}")
    return manifest


def _load_night_refresh_plan(path: Path) -> NightRefreshPlanManifest:
    payload = _read_json(path)
    if (
        payload.get("contract_name") != NIGHT_PLAN_CONTRACT_NAME
        or payload.get("contract_version") != NIGHT_PLAN_CONTRACT_VERSION
    ):
        raise ValueError(f"invalid night refresh plan contract: {path}")
    expected = str(payload.get("manifest_sha256") or "")
    actual = _fingerprint({key: value for key, value in payload.items() if key != "manifest_sha256"})
    if not expected or expected != actual:
        raise ValueError(f"night refresh plan digest mismatch: {path}")
    experiment_id = str(payload.get("experiment_id") or "")
    if re.fullmatch(r"web-vitrina-closed-day-2026-08-23-night-[A-Za-z0-9_.-]+", experiment_id) is None:
        raise ValueError(f"invalid night refresh plan id: {path}")
    slots = tuple(
        NightRefreshPlanSlot(
            slot_id=str(item.get("slot_id") or ""),
            due_at=str(item.get("due_at") or ""),
            deadline=str(item.get("deadline") or ""),
            child_experiment_id=str(item.get("child_experiment_id") or ""),
        )
        for item in (payload.get("slots") or [])
        if isinstance(item, Mapping)
    )
    expected_slots = tuple((slot_id, due_at, deadline) for slot_id, due_at, deadline in NIGHT_PLAN_SLOTS)
    actual_slots = tuple((slot.slot_id, slot.due_at, slot.deadline) for slot in slots)
    if len(slots) != 4 or actual_slots != expected_slots:
        raise ValueError(f"night refresh plan must retain the four exact owner-authorized slots: {path}")
    units = tuple(str(value or "") for value in (payload.get("pause_units") or []))
    if set(units) != set(ALLOWED_PAUSE_UNITS) or len(units) != len(ALLOWED_PAUSE_UNITS):
        raise ValueError(f"night refresh plan conflict timer set drifted: {path}")
    plan = NightRefreshPlanManifest(
        experiment_id=experiment_id,
        target_date=str(payload.get("target_date") or ""),
        timezone=str(payload.get("timezone") or ""),
        expected_deployed_sha=str(payload.get("expected_deployed_sha") or ""),
        pause_units=units,
        max_attempts_per_slot=int(payload.get("max_attempts_per_slot") or 0),
        slots=slots,
        created_at=str(payload.get("created_at") or ""),
        manifest_sha256=expected,
    )
    if plan.target_date != NIGHT_PLAN_TARGET_DATE or plan.timezone != NIGHT_PLAN_TIMEZONE:
        raise ValueError(f"night refresh plan date/timezone drifted: {path}")
    if re.fullmatch(r"[0-9a-f]{40}", plan.expected_deployed_sha) is None:
        raise ValueError(f"invalid night refresh plan deployed SHA: {path}")
    if plan.max_attempts_per_slot < 1 or plan.max_attempts_per_slot > DEFAULT_MAX_ATTEMPTS:
        raise ValueError(f"invalid night refresh plan max attempts: {path}")
    if payload.get("ordinary_schedule_modified") is not False:
        raise ValueError(f"night refresh plan must not modify ordinary schedules: {path}")
    if payload.get("automatic_terminal_expiry") is not True or payload.get("no_next_day_replay") is not True:
        raise ValueError(f"night refresh plan expiry contract is invalid: {path}")
    if payload.get("manual_comparison_volatile_exclusions") != [
        "meta.generated_at",
        "status_summary.business_now",
    ]:
        raise ValueError(f"night refresh plan manual comparison exclusions drifted: {path}")
    return plan


def _night_plan_row(
    control_root: Path,
    plan: NightRefreshPlanManifest,
    instant: datetime,
) -> dict[str, Any]:
    superseded = _night_plan_superseded(control_root / plan.experiment_id)
    slot_rows: list[dict[str, Any]] = []
    for slot in plan.slots:
        child_root = control_root / slot.child_experiment_id
        child_path = child_root / "manifest.json"
        child = _load_manifest(child_path)
        artifact_exists = _artifact_path(child_root).exists()
        attempt_count = len(list((child_root / "attempts").glob("*.json")))
        pause_count = len(list((child_root / "pauses").glob("*-intent.json")))
        if superseded:
            state = "superseded"
        elif artifact_exists:
            state = "terminal"
        elif instant > slot.deadline_datetime:
            state = "expired_pending_terminalization"
        elif instant < slot.due_datetime:
            state = "pending"
        else:
            state = "active"
        slot_rows.append(
            {
                **slot.payload(),
                "state": state,
                "artifact_exists": artifact_exists,
                "attempt_receipt_count": attempt_count,
                "pause_intent_count": pause_count,
                "child_manifest_sha256": child.manifest_sha256,
            }
        )
    if superseded:
        state = "superseded"
    elif all(row["artifact_exists"] for row in slot_rows):
        state = "terminal"
    elif instant > plan.slots[-1].deadline_datetime:
        state = "expired"
    elif instant < plan.slots[0].due_datetime:
        state = "armed"
    else:
        state = "active"
    return {
        "experiment_id": plan.experiment_id,
        "target_date": plan.target_date,
        "timezone": plan.timezone,
        "state": state,
        "expected_deployed_sha": plan.expected_deployed_sha,
        "manifest_sha256": plan.manifest_sha256,
        "pause_units": list(plan.pause_units),
        "slot_count": len(slot_rows),
        "slots": slot_rows,
        "ordinary_schedule_modified": False,
        "automatic_terminal_expiry": True,
        "no_next_day_replay": True,
        "superseded": superseded,
        "comparison_exists": (control_root / plan.experiment_id / "comparison.json").exists(),
        "no_early_action": bool(
            instant < plan.slots[0].due_datetime
            and all(row["attempt_receipt_count"] == 0 and row["pause_intent_count"] == 0 for row in slot_rows)
        ),
        "manifest_path": str(control_root / plan.experiment_id / "plan.json"),
    }


def _retryable_terminal_contention(payload: Mapping[str, Any]) -> bool:
    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    text = " ".join(
        str(value or "")
        for value in (
            payload.get("error"),
            payload.get("reason"),
            result.get("semantic_reason"),
            result.get("reason_code"),
            result.get("error"),
        )
    ).lower()
    return str(payload.get("status") or "").lower() == "error" and "sqlite_contention_exhausted" in text


def _technical_success(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("status") or "").lower() == "success"


def _semantic_status(payload: Mapping[str, Any]) -> str:
    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    return str(result.get("semantic_status") or result.get("status") or payload.get("status") or "unknown").lower()


def _is_busy_skip(payload: Mapping[str, Any]) -> bool:
    return (
        str(payload.get("status") or "").lower() == "skipped"
        and bool(payload.get("already_running_job_id"))
        and bool(payload.get("retryable", True))
    )


def _row_counts(contract: Mapping[str, Any], terminal_job: Mapping[str, Any]) -> dict[str, Any]:
    result = terminal_job.get("result") if isinstance(terminal_job.get("result"), Mapping) else {}
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
    if not found:
        found.append({"path": "diagnostic_summary", "value": "no fallback/preservation/retry flags observed"})
    return found


def _recursive_diff_paths(previous: Any, current: Any) -> list[str]:
    paths: list[str] = []
    missing = object()

    def walk(left: Any, right: Any, path: str, depth: int) -> None:
        if depth > 16 or len(paths) >= 500:
            return
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            for key in sorted(set(left) | set(right), key=str):
                child = f"{path}.{key}" if path else str(key)
                walk(left.get(key, missing), right.get(key, missing), child, depth + 1)
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                paths.append(f"{path}.__length__")
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                walk(left_item, right_item, f"{path}[{index}]", depth + 1)
            return
        if left is missing or right is missing or left != right:
            paths.append(path or "$")

    walk(previous, current, "", 0)
    return paths


def _without_log_lines(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"log_lines"}}


def _control_root(runtime_dir: Path) -> Path:
    return runtime_dir / "experiments" / CONTROL_ROOT_NAME


def _night_plan_superseded(plan_root: Path) -> bool:
    path = plan_root / "superseded.json"
    if not path.exists():
        return False
    payload = _read_json(path)
    expected = str(payload.get("superseded_sha256") or "")
    actual = _fingerprint({key: value for key, value in payload.items() if key != "superseded_sha256"})
    if not expected or expected != actual:
        raise ValueError(f"night refresh plan superseded receipt digest mismatch: {path}")
    if payload.get("contract_name") != f"{NIGHT_PLAN_CONTRACT_NAME}_superseded":
        raise ValueError(f"invalid night refresh plan superseded receipt: {path}")
    return True


def _control_manifest_superseded(path: Path, manifest: ControlCanaryManifest) -> bool:
    if not manifest.parent_plan_id:
        return False
    return _night_plan_superseded(path.parent.parent / manifest.parent_plan_id)


def _artifact_path(root: Path) -> Path:
    return root / "artifact.json"


def _comparison_path(root: Path) -> Path:
    return root / "comparison.json"


def _attempt_path(root: Path, attempt_number: int, attempt_id: str, event: str) -> Path:
    return root / "attempts" / f"{attempt_number:02d}-{attempt_id}-{event}.json"


def _pause_intent_path(root: Path, pause_id: str) -> Path:
    return root / "pauses" / f"{pause_id}-intent.json"


def _pause_applied_path(root: Path, pause_id: str) -> Path:
    return root / "pauses" / f"{pause_id}-applied.json"


def _pause_restored_path(root: Path, pause_id: str) -> Path:
    return root / "pauses" / f"{pause_id}-restored.json"


def _parse_systemctl_show(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _run_command(argv: Sequence[str]) -> str:
    completed = subprocess.run(list(argv), check=True, capture_output=True, text=True)
    return completed.stdout


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _process_start_ticks(pid: int) -> str:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
    except OSError:
        return ""
    return fields[21] if len(fields) > 21 else ""


def _read_json(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return dict(raw) if isinstance(raw, Mapping) else {}


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_json_idempotent_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists():
        if _read_json(path) != dict(payload):
            raise ControlCanaryError(f"immutable manifest already exists with different bytes: {path}")
        return
    _write_json_exclusive(path, payload)


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_target_date(value: str) -> None:
    normalized = str(value or "")
    try:
        parsed = datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("control canary target_date must be YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != normalized:
        raise ValueError("control canary target_date must be canonical YYYY-MM-DD")


def _valid_control_experiment_id(experiment_id: str, target_date: str) -> bool:
    return re.fullmatch(
        rf"web-vitrina-closed-day-{re.escape(target_date)}-canary-[A-Za-z0-9_.-]+",
        str(experiment_id or ""),
    ) is not None


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timezone-aware datetime required: {value}")
    return parsed.astimezone(timezone.utc)


def _parse_optional_datetime(value: str) -> datetime | None:
    try:
        return _parse_datetime(value)
    except (TypeError, ValueError):
        return None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return _aware_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")
