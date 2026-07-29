"""Fail-closed recovery for one stale Finance snapshot writer generation.

The Finance snapshot planner deliberately refuses to enter a maintenance
window while any business writer service is already active.  This recovery is
therefore intentionally narrower than the general maintenance boundary: it
can stop only the known closure-retry oneshot, only after its exact systemd
generation has exceeded the repo-owned runtime bound, and only while it has no
runtime-store file descriptors or internet sockets.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, Callable, Mapping, Sequence

from packages.application.business_data_write_barrier import barrier_status


PLAN_CONTRACT = "wb_core_finance_storage_stale_writer_recovery_plan_v1"
RESULT_CONTRACT = "wb_core_finance_storage_stale_writer_recovery_result_v1"
SERVICE_UNIT = "wb-core-sheet-vitrina-closure-retry.service"
TIMER_UNIT = "wb-core-sheet-vitrina-closure-retry.timer"
PROCESS_MARKER = "apps/sheet_vitrina_v1_temporal_closure_retry_live.py"
SERVICE_ARTIFACT = (
    "artifacts/registry_upload_http_entrypoint/systemd/"
    "wb-core-sheet-vitrina-closure-retry.service"
)
POLICY_FILENAME = ".auto-updates-policy.json"
MAINTENANCE_FILENAME = ".business-data-maintenance.json"
AUDIT_FILENAME = ".finance-storage-stale-writer-recovery-audit.jsonl"
LOCK_FILENAME = ".finance-storage-stale-writer-recovery.lock"
MINIMUM_STALE_SECONDS = 3600
EXPECTED_TIMEOUT_USEC = 30 * 60 * 1_000_000
ALLOWED_CHILD_MARKERS = (
    "playwright/driver/",
    "chrome-headless-shell",
)


class FinanceStorageStaleWriterRecoveryError(RuntimeError):
    """The exact stale writer generation is not safely recoverable."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical_json(value).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FinanceStorageStaleWriterRecoveryError(
            f"required private state is unavailable: {path.name}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FinanceStorageStaleWriterRecoveryError(
            f"required private state is not an object: {path.name}"
        )
    return payload


def _file_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    return _digest(path.read_bytes())


def _parse_systemd_properties(stdout: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            properties[key] = value
    return properties


def _parse_systemd_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.strptime(
            str(value or "").strip(),
            "%a %Y-%m-%d %H:%M:%S UTC",
        )
    except ValueError as exc:
        raise FinanceStorageStaleWriterRecoveryError(
            "service start timestamp is not exact systemd UTC evidence"
        ) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _timeout_usec(value: str) -> int:
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    if text == "infinity":
        return -1
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h)", text)
    if match is None:
        return -2
    multiplier = {
        "us": 1,
        "ms": 1_000,
        "s": 1_000_000,
        "min": 60_000_000,
        "h": 3_600_000_000,
    }[match.group(2)]
    return round(float(match.group(1)) * multiplier)


def _proc_cmdline(proc_root: Path, pid: int) -> str:
    try:
        return (
            (proc_root / str(pid) / "cmdline")
            .read_bytes()
            .replace(b"\0", b" ")
            .decode("utf-8", errors="replace")
            .strip()
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ""


def _proc_wchan(proc_root: Path, pid: int) -> str:
    try:
        return (
            (proc_root / str(pid) / "wchan")
            .read_text(encoding="utf-8", errors="replace")
            .strip()
        )
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return ""


def _cgroup_pids(cgroup_root: Path, control_group: str) -> list[int]:
    path = cgroup_root / str(control_group or "").lstrip("/") / "cgroup.procs"
    try:
        return sorted(
            {
                int(line.strip())
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip().isdigit()
            }
        )
    except (FileNotFoundError, PermissionError):
        return []


def _runtime_file_descriptors(
    proc_root: Path,
    *,
    pids: Sequence[int],
    runtime_dir: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    runtime = runtime_dir.resolve()
    for pid in pids:
        fd_dir = proc_root / str(pid) / "fd"
        try:
            entries = sorted(fd_dir.iterdir(), key=lambda item: item.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for entry in entries:
            try:
                raw_target = os.readlink(entry)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            target = raw_target.removesuffix(" (deleted)")
            if not target.startswith("/"):
                continue
            candidate = Path(target)
            try:
                candidate.resolve(strict=False).relative_to(runtime)
            except ValueError:
                continue
            rows.append(
                {
                    "pid": pid,
                    "fd": entry.name,
                    "path": target,
                }
            )
    return rows


def _socket_inodes(proc_root: Path, pids: Sequence[int]) -> set[str]:
    inodes: set[str] = set()
    for pid in pids:
        fd_dir = proc_root / str(pid) / "fd"
        try:
            entries = fd_dir.iterdir()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for entry in entries:
            try:
                target = os.readlink(entry)
            except (FileNotFoundError, PermissionError, OSError):
                continue
            match = re.fullmatch(r"socket:\[([0-9]+)\]", target)
            if match is not None:
                inodes.add(match.group(1))
    return inodes


def _inet_socket_rows(proc_root: Path, inodes: set[str]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not inodes:
        return rows
    for name in ("tcp", "tcp6", "udp", "udp6"):
        path = proc_root / "net" / name
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (FileNotFoundError, PermissionError):
            continue
        for line in lines[1:]:
            parts = line.split()
            if len(parts) <= 9 or parts[9] not in inodes:
                continue
            rows.append(
                {
                    "protocol": name,
                    "local": parts[1],
                    "remote": parts[2],
                    "state": parts[3],
                    "inode": parts[9],
                }
            )
    return sorted(
        rows,
        key=lambda item: (
            item["protocol"],
            item["local"],
            item["remote"],
            item["inode"],
        ),
    )


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    stable = json.loads(_canonical_json(plan))
    stable.pop("fingerprint", None)
    stable.pop("created_at", None)
    stable.pop("deploy_lease", None)
    service = dict(stable.get("service") or {})
    service.pop("age_seconds", None)
    stable["service"] = service
    return _digest(stable)


class _ExclusiveRecoveryLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any | None = None

    def __enter__(self) -> "_ExclusiveRecoveryLock":
        if self.path.is_symlink():
            raise FinanceStorageStaleWriterRecoveryError(
                "stale-writer recovery lock must not be a symlink"
            )
        self.handle = self.path.open("a+b")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(
                self.handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise FinanceStorageStaleWriterRecoveryError(
                "another stale-writer recovery is active"
            ) from exc
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class FinanceStorageStaleWriterRecovery:
    """Plan and stop one exact stale closure-retry systemd generation."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        deployed_sha: str,
        repo_root: Path,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        systemctl: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir.expanduser().resolve()
        self.deployed_sha = str(deployed_sha or "").strip().lower()
        self.repo_root = repo_root.expanduser().resolve()
        self.proc_root = proc_root
        self.cgroup_root = cgroup_root
        self.systemctl = systemctl
        self.now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def _unit_properties(self, unit: str) -> dict[str, str]:
        result = self.systemctl(
            [
                "systemctl",
                "show",
                unit,
                "--property=LoadState,UnitFileState,ActiveState,SubState,"
                "Result,ExecMainStatus,MainPID,ExecMainStartTimestamp,"
                "TimeoutStartUSec,ControlGroup,FragmentPath,NeedDaemonReload",
                "--no-pager",
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise FinanceStorageStaleWriterRecoveryError(
                f"systemctl show failed for {unit}: "
                + (result.stderr.strip() or f"exit {result.returncode}")
            )
        return _parse_systemd_properties(result.stdout)

    def build_plan(self) -> dict[str, Any]:
        blockers: list[dict[str, Any]] = []
        runtime_sha_path = self.repo_root / ".wb-core-runtime-sha"
        runtime_sha = (
            runtime_sha_path.read_text(encoding="utf-8").strip().lower()
            if runtime_sha_path.is_file()
            else ""
        )
        if (
            re.fullmatch(r"[0-9a-f]{40}", self.deployed_sha) is None
            or runtime_sha != self.deployed_sha
        ):
            blockers.append(
                {
                    "code": "deployed_sha_mismatch",
                    "expected": self.deployed_sha,
                    "runtime": runtime_sha,
                }
            )

        service = self._unit_properties(SERVICE_UNIT)
        timer = self._unit_properties(TIMER_UNIT)
        pid = int(service.get("MainPID") or 0)
        started_at = str(service.get("ExecMainStartTimestamp") or "")
        age_seconds = 0
        try:
            age_seconds = max(
                0,
                round(
                    (
                        self.now_factory().astimezone(timezone.utc)
                        - _parse_systemd_timestamp(started_at)
                    ).total_seconds()
                ),
            )
        except FinanceStorageStaleWriterRecoveryError:
            blockers.append(
                {
                    "code": "service_start_identity_invalid",
                    "started_at": started_at,
                }
            )
        if (
            service.get("LoadState") != "loaded"
            or service.get("ActiveState") != "activating"
            or service.get("SubState") != "start"
            or pid <= 0
        ):
            blockers.append(
                {
                    "code": "service_generation_not_exact_stale_start",
                    "active_state": service.get("ActiveState"),
                    "sub_state": service.get("SubState"),
                    "main_pid": pid,
                }
            )
        if age_seconds < MINIMUM_STALE_SECONDS:
            blockers.append(
                {
                    "code": "service_generation_not_stale",
                    "age_seconds": age_seconds,
                    "minimum_stale_seconds": MINIMUM_STALE_SECONDS,
                }
            )
        timeout_usec = _timeout_usec(service.get("TimeoutStartUSec", ""))
        if timeout_usec != EXPECTED_TIMEOUT_USEC:
            blockers.append(
                {
                    "code": "bounded_timeout_not_deployed",
                    "observed_timeout_usec": timeout_usec,
                    "expected_timeout_usec": EXPECTED_TIMEOUT_USEC,
                }
            )
        if (
            timer.get("LoadState") != "loaded"
            or timer.get("UnitFileState") != "enabled"
            or timer.get("ActiveState") != "active"
        ):
            blockers.append(
                {
                    "code": "timer_prior_intent_not_active",
                    "load_state": timer.get("LoadState"),
                    "unit_file_state": timer.get("UnitFileState"),
                    "active_state": timer.get("ActiveState"),
                }
            )

        source_fragment = self.repo_root / SERVICE_ARTIFACT
        installed_fragment = Path(str(service.get("FragmentPath") or ""))
        source_digest = _file_digest(source_fragment)
        installed_digest = _file_digest(installed_fragment)
        if (
            not source_digest
            or not installed_digest
            or source_digest != installed_digest
            or service.get("NeedDaemonReload") != "no"
        ):
            blockers.append(
                {
                    "code": "service_fragment_drift",
                    "source_digest": source_digest,
                    "installed_digest": installed_digest,
                    "need_daemon_reload": service.get("NeedDaemonReload"),
                }
            )

        control_group = str(service.get("ControlGroup") or "")
        cgroup_pids = _cgroup_pids(self.cgroup_root, control_group)
        process_rows = [
            {
                "pid": process_pid,
                "cmdline": _proc_cmdline(self.proc_root, process_pid),
                "wchan": _proc_wchan(self.proc_root, process_pid),
            }
            for process_pid in cgroup_pids
        ]
        main_cmdline = _proc_cmdline(self.proc_root, pid)
        main_wchan = _proc_wchan(self.proc_root, pid)
        if (
            pid not in cgroup_pids
            or PROCESS_MARKER not in main_cmdline
            or not main_cmdline.startswith("/usr/bin/python3 ")
        ):
            blockers.append(
                {
                    "code": "main_process_identity_drift",
                    "main_pid": pid,
                    "main_cmdline": main_cmdline,
                    "cgroup_pids": cgroup_pids,
                }
            )
        if main_wchan != "ep_poll":
            blockers.append(
                {
                    "code": "main_process_not_dormant",
                    "main_pid": pid,
                    "wchan": main_wchan,
                }
            )
        unexpected_children = [
            row
            for row in process_rows
            if row["pid"] != pid
            and not any(marker in row["cmdline"] for marker in ALLOWED_CHILD_MARKERS)
        ]
        if unexpected_children:
            blockers.append(
                {
                    "code": "unexpected_service_child",
                    "processes": unexpected_children,
                }
            )
        runtime_fds = _runtime_file_descriptors(
            self.proc_root,
            pids=cgroup_pids,
            runtime_dir=self.runtime_dir,
        )
        if runtime_fds:
            blockers.append(
                {
                    "code": "runtime_file_descriptor_open",
                    "descriptors": runtime_fds,
                }
            )
        network_sockets = _inet_socket_rows(
            self.proc_root,
            _socket_inodes(self.proc_root, cgroup_pids),
        )
        if network_sockets:
            blockers.append(
                {
                    "code": "internet_socket_open",
                    "sockets": network_sockets,
                }
            )

        barrier = barrier_status(self.runtime_dir)
        if (
            barrier.get("active") is not False
            or str(barrier.get("phase") or "") != "released"
        ):
            blockers.append(
                {
                    "code": "manual_barrier_not_released",
                    "active": barrier.get("active"),
                    "phase": barrier.get("phase"),
                }
            )
        maintenance = _load_json(self.runtime_dir / MAINTENANCE_FILENAME)
        if (
            str(maintenance.get("phase") or "") != "restored"
            or maintenance.get("exact_prior_state_restored") is not True
        ):
            blockers.append(
                {
                    "code": "maintenance_prior_state_not_restored",
                    "phase": maintenance.get("phase"),
                    "exact_prior_state_restored": maintenance.get(
                        "exact_prior_state_restored"
                    ),
                }
            )
        policy = _load_json(self.runtime_dir / POLICY_FILENAME)
        closure_policy = dict(
            (policy.get("processes") or {}).get("vitrina_closure_retry") or {}
        )
        if (
            policy.get("master_desired") is not True
            or closure_policy.get("desired") is not True
        ):
            blockers.append(
                {
                    "code": "closure_retry_prior_intent_not_enabled",
                    "master_desired": policy.get("master_desired"),
                    "process_desired": closure_policy.get("desired"),
                }
            )

        plan: dict[str, Any] = {
            "contract_version": PLAN_CONTRACT,
            "mode": "stale_writer_recovery_dry_run",
            "deployed_sha": self.deployed_sha,
            "service": {
                "unit": SERVICE_UNIT,
                "main_pid": pid,
                "started_at": started_at,
                "age_seconds": age_seconds,
                "minimum_stale_seconds": MINIMUM_STALE_SECONDS,
                "active_state": service.get("ActiveState"),
                "sub_state": service.get("SubState"),
                "result": service.get("Result"),
                "timeout_start_usec": timeout_usec,
                "control_group": control_group,
                "fragment_path": str(installed_fragment),
                "source_fragment_digest": source_digest,
                "installed_fragment_digest": installed_digest,
                "need_daemon_reload": service.get("NeedDaemonReload"),
            },
            "timer": {
                "unit": TIMER_UNIT,
                "unit_file_state": timer.get("UnitFileState"),
                "active_state": timer.get("ActiveState"),
                "sub_state": timer.get("SubState"),
            },
            "processes": process_rows,
            "runtime_file_descriptors": runtime_fds,
            "internet_sockets": network_sockets,
            "manual_barrier": {
                "active": barrier.get("active"),
                "phase": barrier.get("phase"),
                "window_id": barrier.get("window_id"),
            },
            "maintenance": {
                "phase": maintenance.get("phase"),
                "exact_prior_state_restored": maintenance.get(
                    "exact_prior_state_restored"
                ),
                "control_signature": str(
                    (
                        maintenance.get("restore_control_signature")
                        or {}
                    ).get("fingerprint")
                    or ""
                ),
            },
            "owner_policy": {
                "revision": int(policy.get("revision") or 0),
                "policy_fingerprint": str(policy.get("policy_fingerprint") or ""),
                "master_desired": policy.get("master_desired"),
                "closure_retry_desired": closure_policy.get("desired"),
            },
            "action": {
                "systemctl_stop_unit": SERVICE_UNIT,
                "timer_mutation_count": 0,
                "owner_policy_mutation_count": 0,
                "business_data_mutation_count": 0,
                "finance_storage_mutation_count": 0,
            },
            "blockers": blockers,
            "stop_allowed_by_machine_preflight": not blockers,
            "created_at": _utc_now(),
        }
        plan["fingerprint"] = _plan_fingerprint(plan)
        return plan

    def apply(
        self,
        *,
        reviewed_plan: Mapping[str, Any],
        expected_fingerprint: str,
        approval_reference: str,
    ) -> dict[str, Any]:
        if (
            reviewed_plan.get("contract_version") != PLAN_CONTRACT
            or reviewed_plan.get("mode") != "stale_writer_recovery_dry_run"
            or reviewed_plan.get("fingerprint") != expected_fingerprint
            or _plan_fingerprint(reviewed_plan) != expected_fingerprint
            or reviewed_plan.get("stop_allowed_by_machine_preflight") is not True
            or not str(approval_reference or "").strip()
        ):
            raise FinanceStorageStaleWriterRecoveryError(
                "reviewed stale-writer recovery plan is not approved"
            )
        audit_path = self.runtime_dir / AUDIT_FILENAME
        lock_path = self.runtime_dir / LOCK_FILENAME
        with _ExclusiveRecoveryLock(lock_path):
            prior_terminal = None
            prior_attempt = None
            if audit_path.is_file():
                for line in audit_path.read_text(encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        isinstance(row, dict)
                        and row.get("event") == "stale_writer_stop_completed"
                        and row.get("fingerprint") == expected_fingerprint
                    ):
                        prior_terminal = row
                    elif (
                        isinstance(row, dict)
                        and row.get("fingerprint") == expected_fingerprint
                        and row.get("event")
                        in {
                            "stale_writer_stop_started",
                            "stale_writer_stop_failed",
                            "stale_writer_stop_ambiguous",
                        }
                    ):
                        prior_attempt = row
            if prior_terminal is not None:
                return {
                    "contract_version": RESULT_CONTRACT,
                    "status": "already_completed",
                    "fingerprint": expected_fingerprint,
                    "approval_reference": approval_reference,
                    "stop_count": 0,
                    "audit_path": str(audit_path),
                    "terminal_audit": prior_terminal,
                }
            if prior_attempt is not None:
                raise FinanceStorageStaleWriterRecoveryError(
                    "this exact stale-writer stop was already attempted; "
                    "automatic or repeated stop is forbidden"
                )
            current = self.build_plan()
            if current.get("fingerprint") != expected_fingerprint:
                raise FinanceStorageStaleWriterRecoveryError(
                    "stale writer generation drifted after review"
                )
            self._append_audit(
                audit_path,
                {
                    "event": "stale_writer_stop_started",
                    "captured_at": _utc_now(),
                    "fingerprint": expected_fingerprint,
                    "approval_reference": approval_reference,
                    "service": current["service"],
                    "owner_policy": current["owner_policy"],
                    "manual_barrier": current["manual_barrier"],
                },
            )
            result = self.systemctl(
                ["systemctl", "stop", SERVICE_UNIT],
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            if result.returncode != 0:
                self._append_audit(
                    audit_path,
                    {
                        "event": "stale_writer_stop_failed",
                        "captured_at": _utc_now(),
                        "fingerprint": expected_fingerprint,
                        "returncode": result.returncode,
                        "stderr": result.stderr.strip()[-1000:],
                    },
                )
                raise FinanceStorageStaleWriterRecoveryError(
                    "exact stale writer stop failed; no retry was started"
                )
            service = self._unit_properties(SERVICE_UNIT)
            timer = self._unit_properties(TIMER_UNIT)
            post_policy = _load_json(self.runtime_dir / POLICY_FILENAME)
            post_closure_policy = dict(
                (post_policy.get("processes") or {}).get(
                    "vitrina_closure_retry"
                )
                or {}
            )
            post_barrier = barrier_status(self.runtime_dir)
            post_maintenance = _load_json(
                self.runtime_dir / MAINTENANCE_FILENAME
            )
            stopped_pid = int(
                (reviewed_plan.get("service") or {}).get("main_pid") or 0
            )
            expected_policy = dict(
                reviewed_plan.get("owner_policy") or {}
            )
            expected_barrier = dict(
                reviewed_plan.get("manual_barrier") or {}
            )
            expected_maintenance = dict(
                reviewed_plan.get("maintenance") or {}
            )
            policy_unchanged = (
                int(post_policy.get("revision") or 0)
                == int(expected_policy.get("revision") or 0)
                and str(post_policy.get("policy_fingerprint") or "")
                == str(expected_policy.get("policy_fingerprint") or "")
                and post_policy.get("master_desired")
                == expected_policy.get("master_desired")
                and post_closure_policy.get("desired")
                == expected_policy.get("closure_retry_desired")
            )
            barrier_unchanged = (
                post_barrier.get("active") == expected_barrier.get("active")
                and str(post_barrier.get("phase") or "")
                == str(expected_barrier.get("phase") or "")
                and str(post_barrier.get("window_id") or "")
                == str(expected_barrier.get("window_id") or "")
            )
            maintenance_unchanged = (
                str(post_maintenance.get("phase") or "")
                == str(expected_maintenance.get("phase") or "")
                and post_maintenance.get("exact_prior_state_restored")
                == expected_maintenance.get("exact_prior_state_restored")
                and str(
                    (
                        post_maintenance.get("restore_control_signature")
                        or {}
                    ).get("fingerprint")
                    or ""
                )
                == str(expected_maintenance.get("control_signature") or "")
            )
            if (
                service.get("ActiveState") not in {"inactive", "failed"}
                or int(service.get("MainPID") or 0) != 0
                or _proc_cmdline(self.proc_root, stopped_pid)
                or timer.get("UnitFileState") != "enabled"
                or timer.get("ActiveState") != "active"
                or not policy_unchanged
                or not barrier_unchanged
                or not maintenance_unchanged
            ):
                self._append_audit(
                    audit_path,
                    {
                        "event": "stale_writer_stop_ambiguous",
                        "captured_at": _utc_now(),
                        "fingerprint": expected_fingerprint,
                        "service": service,
                        "timer": timer,
                        "policy_unchanged": policy_unchanged,
                        "barrier_unchanged": barrier_unchanged,
                        "maintenance_unchanged": maintenance_unchanged,
                        "stopped_pid_cmdline": _proc_cmdline(
                            self.proc_root, stopped_pid
                        ),
                    },
                )
                raise FinanceStorageStaleWriterRecoveryError(
                    "stale writer stop readback is ambiguous"
                )
            terminal = {
                "event": "stale_writer_stop_completed",
                "captured_at": _utc_now(),
                "fingerprint": expected_fingerprint,
                "approval_reference": approval_reference,
                "stopped_generation": reviewed_plan["service"],
                "service_readback": {
                    "active_state": service.get("ActiveState"),
                    "sub_state": service.get("SubState"),
                    "main_pid": int(service.get("MainPID") or 0),
                    "result": service.get("Result"),
                },
                "timer_readback": {
                    "unit_file_state": timer.get("UnitFileState"),
                    "active_state": timer.get("ActiveState"),
                    "sub_state": timer.get("SubState"),
                },
                "owner_policy_unchanged": policy_unchanged,
                "manual_barrier_unchanged": barrier_unchanged,
                "maintenance_state_unchanged": maintenance_unchanged,
                "business_data_mutation_count": 0,
                "finance_storage_mutation_count": 0,
            }
            self._append_audit(audit_path, terminal)
            return {
                "contract_version": RESULT_CONTRACT,
                "status": "stopped",
                "fingerprint": expected_fingerprint,
                "approval_reference": approval_reference,
                "stop_count": 1,
                "audit_path": str(audit_path),
                "terminal_audit": terminal,
            }

    @staticmethod
    def _append_audit(path: Path, payload: Mapping[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(dict(payload)) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
