#!/usr/bin/env python3
"""Deterministic checks for polling, bounded retries, and pause restoration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_auto_refresh_tick import (  # noqa: E402
    HTTPJSONError,
    JobPollDeadlineError,
    _control_canary_tick_exit_success,
    _poll_job,
)
from packages.application.sheet_vitrina_v1_control_refresh_canary import (  # noqa: E402
    ALLOWED_PAUSE_UNITS,
    ControlCanaryError,
    ControlRefreshCanaryRunner,
    SystemdTimerCoordinator,
    arm_control_canary_manifest,
    arm_night_refresh_plan_manifest,
    control_canary_status,
    finalize_night_refresh_plans,
    night_refresh_plan_status,
    rebind_night_refresh_plan_manifest,
)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.monotonic_seconds = 0.0
        self.sleeps: list[float] = []

    def datetime(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def sleep(self, seconds: float) -> None:
        value = max(0.0, float(seconds))
        self.sleeps.append(value)
        self.monotonic_seconds += value
        self.now += timedelta(seconds=value)


class FakeSystemd:
    def __init__(self, units: Sequence[str]) -> None:
        self.timers = {
            unit: {
                "active": "active",
                "sub": "waiting",
                "enabled": "enabled",
                "next": "Sun 2026-08-23 06:30:00 UTC",
                "last": "Sun 2026-08-23 06:00:00 UTC",
            }
            for unit in units
        }
        self.services = {
            ALLOWED_PAUSE_UNITS[unit]: {
                "active": "inactive",
                "sub": "dead",
                "started": "Sun 2026-08-23 05:59:00 UTC",
                "finished": "Sun 2026-08-23 05:59:01 UTC",
                "result": "success",
            }
            for unit in units
        }
        self.actions: list[tuple[str, str]] = []

    def __call__(self, argv: Sequence[str]) -> str:
        action = str(argv[1])
        unit = str(argv[2])
        if action == "cat":
            return f"[Unit]\nDescription={unit}\n"
        if action == "stop":
            self.actions.append((action, unit))
            self.timers[unit]["active"] = "inactive"
            self.timers[unit]["sub"] = "dead"
            self.timers[unit]["next"] = ""
            return ""
        if action == "start":
            self.actions.append((action, unit))
            self.timers[unit]["active"] = "active"
            self.timers[unit]["sub"] = "waiting"
            self.timers[unit]["next"] = "Sun 2026-08-23 06:30:00 UTC"
            return ""
        if action != "show":
            raise AssertionError(f"unexpected systemctl action: {argv}")
        if unit in self.timers:
            row = self.timers[unit]
            return (
                f"Id={unit}\nActiveState={row['active']}\nSubState={row['sub']}\n"
                f"UnitFileState={row['enabled']}\nNextElapseUSecRealtime={row['next']}\n"
                f"LastTriggerUSec={row['last']}\n"
            )
        row = self.services[unit]
        return (
            f"Id={unit}\nActiveState={row['active']}\nSubState={row['sub']}\n"
            f"ExecMainStartTimestamp={row['started']}\nExecMainExitTimestamp={row['finished']}\n"
            f"Result={row['result']}\n"
        )


class FakeContour:
    def __init__(self, clock: FakeClock, *, busy_once: bool = False, deadline_once: bool = False) -> None:
        self.clock = clock
        self.busy_once = busy_once
        self.deadline_once = deadline_once
        self.start_calls: list[str] = []
        self.poll_calls: list[str] = []

    def start(self, manifest: object, attempt_id: str) -> Mapping[str, Any]:
        self.start_calls.append(attempt_id)
        if self.busy_once:
            self.busy_once = False
            return {
                "status": "skipped",
                "already_running_job_id": "ordinary-active-job",
                "retryable": True,
                "reason": "server single-flight observed an active job",
            }
        number = len(self.start_calls)
        return {"status": "running", "operation": "auto_update", "job_id": f"canary-job-{number}"}

    def poll(self, job_id: str, deadline: datetime) -> Mapping[str, Any]:
        del deadline
        self.poll_calls.append(job_id)
        if self.deadline_once:
            self.deadline_once = False
            raise JobPollDeadlineError("bounded retryable observation window exhausted")
        if job_id.endswith("1"):
            return {
                "job_id": job_id,
                "status": "error",
                "finished_at": self.clock.datetime().isoformat(),
                "error": "sqlite_contention_exhausted",
                "result": {"semantic_status": "error", "semantic_reason": "sqlite_contention_exhausted"},
            }
        return {
            "job_id": job_id,
            "status": "success",
            "finished_at": self.clock.datetime().isoformat(),
            "result": {
                "semantic_status": "success",
                "as_of_date": "2026-08-22",
                "sheet_row_counts": {"DATA_VITRINA": 2},
                "source_outcome_counts": {"success": 1},
                "updated_cell_count": 2,
                "latest_confirmed_cell_count": 0,
                "source_outcomes": [
                    {
                        "source_key": "seller_funnel_snapshot",
                        "status": "success",
                        "fallback": False,
                        "preserved": False,
                        "captured_at": self.clock.datetime().isoformat(),
                    }
                ],
            },
        }

    def contract(self, target_date: str) -> Mapping[str, Any]:
        return {
            "contract_name": "web_vitrina_contract",
            "contract_version": "v1",
            "meta": {"as_of_date": target_date},
            "rows": [{"metric": "orders", "value": 7}],
        }

    def source_status(self, target_date: str) -> Mapping[str, Any]:
        return {
            "as_of_date": target_date,
            "status_summary": {"fallback": False, "preserved": False, "latest_confirmed": False},
        }

    def ready(self, target_date: str) -> Mapping[str, Any]:
        return {
            "as_of_date": target_date,
            "snapshot_id": "canary-snapshot",
            "refreshed_at": self.clock.datetime().isoformat(),
        }


class VolatileFingerprintContour(FakeContour):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.contract_reads = 0

    def contract(self, target_date: str) -> Mapping[str, Any]:
        self.contract_reads += 1
        return {
            "contract_name": "web_vitrina_contract",
            "contract_version": "v1",
            "meta": {
                "as_of_date": target_date,
                "generated_at": f"2026-08-24T00:00:{self.contract_reads:02d}Z",
            },
            "status_summary": {
                "business_now": f"2026-08-24T05:00:{self.contract_reads:02d}+05:00",
            },
            "rows": [{"metric": "orders", "value": 7}],
        }


def _arm(runtime_dir: Path, clock: FakeClock, units: Sequence[str], suffix: str = "main") -> tuple[str, datetime]:
    due = clock.datetime() + timedelta(minutes=10)
    experiment_id = f"web-vitrina-closed-day-2026-08-22-canary-{suffix}"
    arm_control_canary_manifest(
        runtime_dir=runtime_dir,
        experiment_id=experiment_id,
        due_at=due.isoformat(),
        deadline=(due + timedelta(minutes=40)).isoformat(),
        expected_deployed_sha="a" * 40,
        pause_units=units,
        now=clock.datetime(),
    )
    return experiment_id, due


def _runner(
    runtime_dir: Path,
    clock: FakeClock,
    contour: FakeContour,
    systemd: FakeSystemd,
    *,
    boot_id: str = "boot-1",
    barrier_state: Mapping[str, Any] | None = None,
    deployed_sha: str = "a" * 40,
) -> ControlRefreshCanaryRunner:
    coordinator = SystemdTimerCoordinator(
        command_runner=systemd,
        pid=12345,
        boot_id=boot_id,
        process_start_ticks="start-1",
    )
    # The runner owns no orphan before its own pause; avoid depending on /proc
    # for the deterministic fake PID.
    coordinator.restore_orphans = lambda **kwargs: []  # type: ignore[method-assign]
    return ControlRefreshCanaryRunner(
        runtime_dir=runtime_dir,
        start_refresh=contour.start,
        poll_job=contour.poll,
        fetch_contract=contour.contract,
        fetch_source_status=contour.source_status,
        fetch_ready_snapshot=contour.ready,
        timer_coordinator=coordinator,
        read_deployed_sha=lambda: deployed_sha,
        read_business_data_barrier=(
            (lambda: dict(barrier_state)) if barrier_state is not None else (lambda: {"status": "inactive", "active": False})
        ),
        now_factory=clock.datetime,
        sleep=clock.sleep,
    )


def _polling_checks() -> None:
    clock = FakeClock(datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc))
    responses: list[Any] = [
        HTTPJSONError(
            status=503,
            payload={
                "contract_name": "wb_core_sqlite_contention_v1",
                "code": "sqlite_write_busy",
                "retryable": True,
                "retry_after_ms": 1500,
            },
        ),
        {"status": "running", "job_id": "job-1"},
        {"status": "success", "job_id": "job-1", "result": {"semantic_status": "success"}},
    ]

    def get_json(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        del args, kwargs
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    result = _poll_job(
        base_url="http://127.0.0.1",
        job_path="/job",
        job_id="job-1",
        cookie="test",
        timeout_seconds=20,
        poll_seconds=2,
        get_json=get_json,
        monotonic_factory=clock.monotonic,
        sleep=clock.sleep,
        jitter_factory=lambda upper: 0.0,
    )
    if result["status"] != "success" or clock.sleeps[:2] != [1.5, 2.0]:
        raise AssertionError(f"typed 503 retry_after_ms was not honored: {result} sleeps={clock.sleeps}")

    auth_error = HTTPJSONError(status=403, payload={"error": "forbidden"})
    try:
        _poll_job(
            base_url="http://127.0.0.1",
            job_path="/job",
            job_id="job-auth",
            cookie="test",
            timeout_seconds=5,
            poll_seconds=1,
            get_json=lambda *args, **kwargs: (_ for _ in ()).throw(auth_error),
            monotonic_factory=clock.monotonic,
            sleep=clock.sleep,
            jitter_factory=lambda upper: 0.0,
        )
    except HTTPJSONError as exc:
        if exc.status != 403:
            raise
    else:
        raise AssertionError("auth errors must fail closed without retry")

    transport_clock = FakeClock(datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc))
    transport_responses: list[Any] = [
        TimeoutError("socket timed out"),
        HTTPJSONError(status=502, payload={"error": "bad gateway"}),
        {"status": "success", "job_id": "job-transport"},
    ]

    def transport_get(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
        del args, kwargs
        item = transport_responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    transport = _poll_job(
        base_url="http://127.0.0.1",
        job_path="/job",
        job_id="job-transport",
        cookie="test",
        timeout_seconds=10,
        poll_seconds=1,
        get_json=transport_get,
        monotonic_factory=transport_clock.monotonic,
        sleep=transport_clock.sleep,
        jitter_factory=lambda upper: 0.0,
    )
    if transport["status"] != "success" or len(transport_clock.sleeps) != 2:
        raise AssertionError("transport timeout and transient 502 must be bounded retryable observations")

    deadline_clock = FakeClock(datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc))
    retryable = HTTPJSONError(status=504, payload={"error": "gateway timeout"}, headers={"Retry-After": "1"})
    try:
        _poll_job(
            base_url="http://127.0.0.1",
            job_path="/job",
            job_id="job-deadline",
            cookie="test",
            timeout_seconds=3,
            poll_seconds=1,
            get_json=lambda *args, **kwargs: (_ for _ in ()).throw(retryable),
            monotonic_factory=deadline_clock.monotonic,
            sleep=deadline_clock.sleep,
            jitter_factory=lambda upper: 0.0,
        )
    except JobPollDeadlineError:
        pass
    else:
        raise AssertionError("repeated transient responses must terminate at the exact bounded deadline")


def _pause_restore_checks() -> None:
    units = list(ALLOWED_PAUSE_UNITS)[:2]
    with TemporaryDirectory(prefix="control-canary-pause-") as tmp:
        root = Path(tmp) / "experiments" / "sheet-vitrina-control-canaries" / "canary"
        root.mkdir(parents=True)
        systemd = FakeSystemd(units)
        coordinator = SystemdTimerCoordinator(
            command_runner=systemd,
            pid=12345,
            boot_id="boot-1",
            process_start_ticks="start-1",
        )
        now = datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc)
        manifest_payload = arm_control_canary_manifest(
            runtime_dir=Path(tmp),
            experiment_id="web-vitrina-closed-day-2026-08-22-canary-pause",
            due_at=(now + timedelta(minutes=10)).isoformat(),
            deadline=(now + timedelta(minutes=50)).isoformat(),
            expected_deployed_sha="b" * 40,
            pause_units=units,
            now=now,
        )["manifest"]
        from packages.application.sheet_vitrina_v1_control_refresh_canary import _load_manifest

        manifest_path = Path(tmp) / "experiments" / "sheet-vitrina-control-canaries" / manifest_payload["experiment_id"] / "manifest.json"
        manifest = _load_manifest(manifest_path)
        experiment_root = manifest_path.parent
        intent = coordinator.pause(experiment_root=experiment_root, manifest=manifest, now=manifest.due_datetime)
        if any(systemd.timers[unit]["active"] != "inactive" for unit in units):
            raise AssertionError("every selected timer must be inactive only after durable before-state")
        intent_path = next((experiment_root / "pauses").glob("*-intent.json"))
        if not intent_path.exists() or not intent.get("intent_sha256"):
            raise AssertionError("pause intent must be immutable and fingerprinted before systemctl stop")
        coordinator.restore(experiment_root=experiment_root, intent=intent, now=manifest.due_datetime + timedelta(minutes=1))
        if any(systemd.timers[unit]["active"] != "active" for unit in units):
            raise AssertionError("finally restore must return exact active before-state")

        # Crash/restart: a different boot identity must restore immediately,
        # even before the hard expiry.
        intent2 = coordinator.pause(experiment_root=experiment_root, manifest=manifest, now=manifest.due_datetime + timedelta(minutes=2))
        restarted = SystemdTimerCoordinator(
            command_runner=systemd,
            pid=22222,
            boot_id="boot-2",
            process_start_ticks="start-2",
        )
        restored = restarted.restore_orphans(
            control_root=experiment_root.parent,
            now=manifest.due_datetime + timedelta(minutes=3),
        )
        if len(restored) != 1 or any(systemd.timers[unit]["active"] != "active" for unit in units):
            raise AssertionError("restart watchdog did not restore the durable pause intent")

        # Same owner is still overridden by the hard maximum pause expiry.
        intent3 = coordinator.pause(experiment_root=experiment_root, manifest=manifest, now=manifest.due_datetime + timedelta(minutes=4))
        coordinator._owner_alive = lambda owner: True  # type: ignore[method-assign]
        expired = coordinator.restore_orphans(
            control_root=experiment_root.parent,
            now=datetime.fromisoformat(str(intent3["hard_restore_at"]).replace("Z", "+00:00")) + timedelta(seconds=1),
        )
        if len(expired) != 1:
            raise AssertionError("hard pause expiry must restore even when the owner appears alive")

        active_service = ALLOWED_PAUSE_UNITS[units[0]]
        systemd.services[active_service]["active"] = "activating"
        actions_before = list(systemd.actions)
        try:
            coordinator.pause(
                experiment_root=experiment_root,
                manifest=manifest,
                now=manifest.due_datetime + timedelta(minutes=5),
            )
        except Exception as exc:
            if "not idle" not in str(exc):
                raise
        else:
            raise AssertionError("an active paired writer must prevent pause without service stop/kill")
        if systemd.actions != actions_before:
            raise AssertionError("active writer evidence must cause zero timer/service mutation")
        systemd.services[active_service]["active"] = "inactive"

        try:
            arm_control_canary_manifest(
                runtime_dir=Path(tmp),
                experiment_id="web-vitrina-closed-day-2026-08-22-canary-unrelated",
                due_at=(now + timedelta(days=1)).isoformat(),
                deadline=(now + timedelta(days=1, minutes=40)).isoformat(),
                expected_deployed_sha="b" * 40,
                pause_units=["unrelated-business.timer"],
                now=now,
            )
        except ValueError as exc:
            if "unsupported pause units" not in str(exc):
                raise
        else:
            raise AssertionError("unrelated timers must never enter a pause manifest")


def _successful_canary_checks() -> None:
    units = list(ALLOWED_PAUSE_UNITS)[:2]
    with TemporaryDirectory(prefix="control-canary-success-") as tmp:
        runtime_dir = Path(tmp)
        clock = FakeClock(datetime(2026, 8, 23, 6, 0, tzinfo=timezone.utc))
        experiment_id, due = _arm(runtime_dir, clock, units)
        clock.now = due
        systemd = FakeSystemd(units)
        contour = FakeContour(clock)
        runner = _runner(runtime_dir, clock, contour, systemd)
        result = runner.tick(now=clock.datetime())
        if result["status"] != "accepted":
            raise AssertionError(f"contention then bounded retry must succeed: {result}")
        root = runtime_dir / "experiments" / "sheet-vitrina-control-canaries" / experiment_id
        artifact_path = root / "artifact.json"
        artifact_bytes = artifact_path.read_bytes()
        artifact = json.loads(artifact_bytes)
        accepted = [row for row in artifact["attempts"] if row.get("event") == "refresh_accepted"]
        terminals = [row for row in artifact["attempts"] if row.get("event") == "attempt_terminal"]
        if len(accepted) != 2 or len(terminals) != 2 or terminals[0]["retryable_contention"] is not True:
            raise AssertionError(f"exact immutable attempt lineage is incomplete: {artifact['attempts']}")
        if artifact["fingerprints"]["payloads_equal"] is not True:
            raise AssertionError("fresh exact-date canonical fingerprint must match the archived payload")
        if not artifact["acceptance_checks"]["all_pauses_restored"]:
            raise AssertionError("canary cannot pass before exact timer restore readback")
        calls = list(contour.start_calls)
        replay = runner.tick(now=clock.datetime() + timedelta(minutes=1))
        if replay["status"] != "no_due_canary" or contour.start_calls != calls or artifact_path.read_bytes() != artifact_bytes:
            raise AssertionError("terminal single canary must expire without replay or overwrite")
        status = control_canary_status(runtime_dir=runtime_dir, now=clock.datetime())
        if status["canaries"][0]["state"] != "terminal" or not (root / "comparison.json").exists():
            raise AssertionError("one-slot comparison/status must become terminal")

    with TemporaryDirectory(prefix="control-canary-busy-") as tmp:
        runtime_dir = Path(tmp)
        clock = FakeClock(datetime(2026, 8, 23, 7, 0, tzinfo=timezone.utc))
        _, due = _arm(runtime_dir, clock, units, suffix="busy")
        clock.now = due
        systemd = FakeSystemd(units)
        contour = FakeContour(clock, busy_once=True)
        runner = _runner(runtime_dir, clock, contour, systemd)
        first = runner.tick(now=clock.datetime())
        if first["status"] != "active_job_retry_pending" or len(contour.start_calls) != 1:
            raise AssertionError("active canonical job must not create a duplicate accepted refresh")
        clock.now += timedelta(minutes=10)
        second = runner.tick(now=clock.datetime())
        if second["status"] != "accepted" or len(contour.start_calls) != 2:
            raise AssertionError("bounded retry after authoritative idle must retain at-most-one active attempt")

    with TemporaryDirectory(prefix="control-canary-deadline-") as tmp:
        runtime_dir = Path(tmp)
        clock = FakeClock(datetime(2026, 8, 23, 8, 0, tzinfo=timezone.utc))
        experiment_id, due = _arm(runtime_dir, clock, units, suffix="deadline")
        clock.now = due
        systemd = FakeSystemd(units)
        contour = FakeContour(clock, deadline_once=True)
        runner = _runner(runtime_dir, clock, contour, systemd)
        pending = runner.tick(now=clock.datetime())
        root = runtime_dir / "experiments" / "sheet-vitrina-control-canaries" / experiment_id
        if pending["status"] != "poll_retry_pending" or (root / "artifact.json").exists():
            raise AssertionError("retryable poll exhaustion must not create an early terminal artifact")
        clock.now = due + timedelta(minutes=41)
        expired = runner.tick(now=clock.datetime())
        if expired["status"] != "failed" or not (root / "artifact.json").exists():
            raise AssertionError("exact canary deadline must create explicit immutable failure")


def _exercise_night_plan(
    *,
    plan_id: str,
    target_date: str,
    started_at: datetime,
    exact_due: list[str],
    exact_deadlines: list[str],
) -> None:
    units = list(ALLOWED_PAUSE_UNITS)
    with TemporaryDirectory(prefix="control-canary-night-plan-") as tmp:
        runtime_dir = Path(tmp)
        clock = FakeClock(started_at)
        armed = arm_night_refresh_plan_manifest(
            runtime_dir=runtime_dir,
            experiment_id=plan_id,
            expected_deployed_sha="a" * 40,
            pause_units=units,
            now=clock.datetime(),
        )
        readback = armed["readback"]
        child_suffix = plan_id.split("-night-", 1)[1]
        exact_child_ids = [
            f"web-vitrina-closed-day-{target_date}-canary-{child_suffix}-{datetime.fromisoformat(value).strftime('%Y%m%dT%H%MEKT')}"
            for value in exact_due
        ]
        if (
            readback["state"] != "armed"
            or readback["target_date"] != target_date
            or readback["timezone"] != "Asia/Yekaterinburg"
            or readback["slot_count"] != 4
            or [row["due_at"] for row in readback["slots"]] != exact_due
            or [row["deadline"] for row in readback["slots"]] != exact_deadlines
            or [row["child_experiment_id"] for row in readback["slots"]] != exact_child_ids
            or [row["state"] for row in readback["slots"]] != ["pending"] * 4
            or any(row["artifact_exists"] for row in readback["slots"])
            or any(row["attempt_receipt_count"] for row in readback["slots"])
            or any(row["pause_intent_count"] for row in readback["slots"])
            or readback["no_early_action"] is not True
            or readback["ordinary_schedule_modified"] is not False
            or readback["automatic_terminal_expiry"] is not True
            or readback["no_next_day_replay"] is not True
        ):
            raise AssertionError(f"four-slot plan did not arm exactly: {readback}")
        control_root = runtime_dir / "experiments" / "sheet-vitrina-control-canaries"
        if any(
            (control_root / row["child_experiment_id"] / name).exists()
            for row in readback["slots"]
            for name in ("attempts", "pauses", "artifact.json", "comparison.json")
        ):
            raise AssertionError("arming must not create an early attempt, pause, or source action")
        parent_path = Path(armed["manifest_path"])
        parent_bytes = parent_path.read_bytes()
        try:
            arm_night_refresh_plan_manifest(
                runtime_dir=runtime_dir,
                experiment_id=plan_id,
                expected_deployed_sha="a" * 40,
                pause_units=units,
                now=clock.datetime() + timedelta(seconds=1),
            )
        except ControlCanaryError:
            pass
        else:
            raise AssertionError("an armed plan must reject a duplicate arm")
        if parent_path.read_bytes() != parent_bytes:
            raise AssertionError("duplicate arm rejection must preserve the immutable parent")

        systemd = FakeSystemd(units)
        contour = VolatileFingerprintContour(clock)
        barrier = {"status": "active", "active": True, "phase": "held", "window_id": "fbs-apply-window"}
        runner = _runner(runtime_dir, clock, contour, systemd, barrier_state=barrier)
        first_due = datetime.fromisoformat(exact_due[0]).astimezone(timezone.utc)
        clock.now = first_due
        waiting = runner.tick(now=clock.datetime())
        if waiting["status"] != "waiting_for_business_data_barrier" or contour.start_calls or systemd.actions:
            raise AssertionError("active FBS/business-data mutation barrier must cause a zero-action bounded wait")

        barrier["status"] = "inactive"
        barrier["active"] = False
        barrier["phase"] = "released"
        slot_results: list[dict[str, Any]] = []
        for due_at in exact_due:
            clock.now = datetime.fromisoformat(due_at).astimezone(timezone.utc)
            result = runner.tick(now=clock.datetime())
            slot_results.append(result)
            finalize_night_refresh_plans(runtime_dir=runtime_dir, now=clock.datetime())
        if any(result["status"] != "failed" for result in slot_results):
            raise AssertionError("known volatile fields must remain visible as formal acceptance failures")
        if any(not _control_canary_tick_exit_success(result) for result in slot_results):
            raise AssertionError("the exact known fingerprint-only false failure must not stop later authorized slots")
        materially_drifted = json.loads(json.dumps(slot_results[0]))
        materially_drifted["artifact"]["fingerprints"]["fresh_readback_diff_paths"].append("rows[0].value")
        materially_drifted["artifact"]["fingerprints"]["known_volatile_only_difference"] = False
        if _control_canary_tick_exit_success(materially_drifted):
            raise AssertionError("a new data-bearing fingerprint difference must remain a true failed tick")

        status = night_refresh_plan_status(runtime_dir=runtime_dir, now=clock.datetime())
        plan = next(row for row in status["plans"] if row.get("experiment_id") == plan_id)
        if (
            plan["state"] != "terminal"
            or [row["state"] for row in plan["slots"]] != ["terminal"] * 4
            or not plan["comparison_exists"]
        ):
            raise AssertionError(f"four-slot plan did not terminalize without replay: {plan}")
        comparison = json.loads((control_root / plan_id / "comparison.json").read_text(encoding="utf-8"))
        if comparison["slot_count"] != 4 or len(comparison["comparisons"]) != 3:
            raise AssertionError("morning comparison must retain all four raw artifacts and three adjacent comparisons")
        for row in plan["slots"]:
            artifact = json.loads(
                (control_root / row["child_experiment_id"] / "artifact.json").read_text(encoding="utf-8")
            )
            if (
                artifact["technical_status"] != "success"
                or artifact["acceptance_checks"]["fresh_exact_date_fingerprint_match"] is not False
                or artifact["fingerprints"]["fresh_readback_diff_paths"]
                != ["meta.generated_at", "status_summary.business_now"]
                or artifact["fingerprints"]["known_volatile_only_difference"] is not True
                or artifact["business_data_barrier_preflight"]["active"] is not False
                or not artifact["pause_intents"]
                or len(artifact["pause_intents"]) != len(artifact["restore_receipts"])
            ):
                raise AssertionError("slot artifact lost technical/raw/barrier/pause-restore evidence")
        calls_before = list(contour.start_calls)
        replay = runner.tick(now=clock.datetime() + timedelta(days=1))
        if replay["status"] != "no_due_canary" or contour.start_calls != calls_before:
            raise AssertionError("terminal four-slot plan must not replay on the next day")


def _night_plan_allowlist_checks() -> None:
    units = list(ALLOWED_PAUSE_UNITS)
    invalid_ids = [
        "web-vitrina-closed-day-2026-08-24-night-v2",
        "web-vitrina-closed-day-2026-08-25-night-v1",
        "web-vitrina-closed-day-2026-08-24-night-v1-extra",
    ]
    for invalid_id in invalid_ids:
        with TemporaryDirectory(prefix="control-canary-night-invalid-id-") as tmp:
            runtime_dir = Path(tmp)
            try:
                arm_night_refresh_plan_manifest(
                    runtime_dir=runtime_dir,
                    experiment_id=invalid_id,
                    expected_deployed_sha="a" * 40,
                    pause_units=units,
                    now=datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc),
                )
            except ValueError:
                pass
            else:
                raise AssertionError(f"unreleased night plan id was accepted: {invalid_id}")
            if any(runtime_dir.rglob("*.json")):
                raise AssertionError("invalid night plan identity must fail before any runtime write")

    mutations = {
        "target_date": lambda payload: payload.__setitem__("target_date", "2026-08-25"),
        "timezone": lambda payload: payload.__setitem__("timezone", "UTC"),
        "slot_count": lambda payload: payload.__setitem__("slots", payload["slots"][:3]),
        "due_at": lambda payload: payload["slots"][0].__setitem__("due_at", "2026-08-25T01:31:00+05:00"),
        "deadline": lambda payload: payload["slots"][0].__setitem__("deadline", "2026-08-25T02:21:00+05:00"),
        "child_id": lambda payload: payload["slots"][0].__setitem__("child_experiment_id", "wrong-child"),
    }
    for label, mutate in mutations.items():
        with TemporaryDirectory(prefix=f"control-canary-night-invalid-{label}-") as tmp:
            runtime_dir = Path(tmp)
            armed = arm_night_refresh_plan_manifest(
                runtime_dir=runtime_dir,
                experiment_id="web-vitrina-closed-day-2026-08-24-night-v1",
                expected_deployed_sha="a" * 40,
                pause_units=units,
                now=datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc),
            )
            path = Path(armed["manifest_path"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            mutate(payload)
            unsigned = {key: value for key, value in payload.items() if key != "manifest_sha256"}
            payload["manifest_sha256"] = hashlib.sha256(
                json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            status = night_refresh_plan_status(
                runtime_dir=runtime_dir,
                now=datetime(2026, 8, 24, 17, 1, tzinfo=timezone.utc),
            )["plans"]
            if len(status) != 1 or status[0].get("state") != "invalid_manifest":
                raise AssertionError(f"night plan {label} drift was not rejected: {status}")


def _night_plan_checks() -> None:
    units = list(ALLOWED_PAUSE_UNITS)
    _night_plan_allowlist_checks()
    _exercise_night_plan(
        plan_id="web-vitrina-closed-day-2026-08-23-night-v1",
        target_date="2026-08-23",
        started_at=datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc),
        exact_due=[
            "2026-08-24T01:30:00+05:00",
            "2026-08-24T03:30:00+05:00",
            "2026-08-24T06:30:00+05:00",
            "2026-08-24T08:30:00+05:00",
        ],
        exact_deadlines=[
            "2026-08-24T02:20:00+05:00",
            "2026-08-24T04:20:00+05:00",
            "2026-08-24T07:20:00+05:00",
            "2026-08-24T09:20:00+05:00",
        ],
    )
    _exercise_night_plan(
        plan_id="web-vitrina-closed-day-2026-08-24-night-v1",
        target_date="2026-08-24",
        started_at=datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc),
        exact_due=[
            "2026-08-25T01:30:00+05:00",
            "2026-08-25T03:30:00+05:00",
            "2026-08-25T06:30:00+05:00",
            "2026-08-25T08:30:00+05:00",
        ],
        exact_deadlines=[
            "2026-08-25T02:20:00+05:00",
            "2026-08-25T04:20:00+05:00",
            "2026-08-25T07:20:00+05:00",
            "2026-08-25T09:20:00+05:00",
        ],
    )

    with TemporaryDirectory(prefix="control-canary-night-rebind-") as tmp:
        runtime_dir = Path(tmp)
        clock = FakeClock(datetime(2026, 8, 23, 10, 0, tzinfo=timezone.utc))
        first_id = "web-vitrina-closed-day-2026-08-23-night-v1"
        replacement_id = "web-vitrina-closed-day-2026-08-23-night-v2"
        arm_night_refresh_plan_manifest(
            runtime_dir=runtime_dir,
            experiment_id=first_id,
            expected_deployed_sha="a" * 40,
            pause_units=units,
            now=clock.datetime(),
        )
        try:
            rebind_night_refresh_plan_manifest(
                runtime_dir=runtime_dir,
                current_experiment_id=first_id,
                replacement_experiment_id=first_id,
                expected_deployed_sha="b" * 40,
                pause_units=units,
                now=clock.datetime() + timedelta(seconds=30),
            )
        except ControlCanaryError:
            pass
        else:
            raise AssertionError("invalid replacement identity must fail before superseding the current plan")
        if (runtime_dir / "experiments" / "sheet-vitrina-control-canaries" / first_id / "superseded.json").exists():
            raise AssertionError("failed rebind validation must leave the current armed plan untouched")
        rebound = rebind_night_refresh_plan_manifest(
            runtime_dir=runtime_dir,
            current_experiment_id=first_id,
            replacement_experiment_id=replacement_id,
            expected_deployed_sha="b" * 40,
            pause_units=units,
            now=clock.datetime() + timedelta(minutes=1),
        )
        if rebound["status"] != "rebound":
            raise AssertionError("pre-due exact-SHA rebind did not produce a replacement plan")
        plans = night_refresh_plan_status(
            runtime_dir=runtime_dir,
            now=clock.datetime() + timedelta(minutes=1),
        )["plans"]
        states = {row["experiment_id"]: row["state"] for row in plans}
        if states != {first_id: "superseded", replacement_id: "armed"}:
            raise AssertionError(f"rebind must leave one superseded and one armed plan: {states}")
        clock.now = datetime.fromisoformat("2026-08-24T01:30:00+05:00").astimezone(timezone.utc)
        systemd = FakeSystemd(units)
        contour = FakeContour(clock)
        runner = _runner(runtime_dir, clock, contour, systemd, deployed_sha="b" * 40)
        result = runner.tick(now=clock.datetime())
        if result["status"] != "accepted" or len(contour.start_calls) != 2:
            raise AssertionError("runner must skip all superseded children and execute only the replacement slot")


def main() -> None:
    _polling_checks()
    _pause_restore_checks()
    _successful_canary_checks()
    _night_plan_checks()
    print("sheet_vitrina_v1_control_refresh_canary: ok")


if __name__ == "__main__":
    main()
