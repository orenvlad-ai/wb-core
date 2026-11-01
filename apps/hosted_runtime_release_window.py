"""Durable task-scoped release admission core; not a deploy engine.

No production mutation adapter is provided. A trusted controller must supply a
boundary which durably encloses BOTH sender and worker in bounded systemd units.
A missing unit, exited SSH shell, timeout, or empty PID list is never a receipt.
State lives outside the synchronized application; callers must fence every stage.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid


class FenceDenied(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise FenceDenied(reason)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class UnitProof:
    unit: str
    invocation_id: str
    control_group: str
    active_state: str
    sub_state: str
    result: str
    main_pid: int
    job_id: int
    exec_main_code: int
    exec_main_status: int
    kill_mode: str
    runtime_max_usec: int
    # Every recursively observed cgroup, including its inode, events and PIDs.
    groups: tuple[tuple[str, int, int, tuple[int, ...]], ...]

    def terminal(self, expected_unit, timeout_seconds, expected_invocation):
        require(self.unit == expected_unit, "unit identity changed")
        require(bool(re.fullmatch(r"[a-f0-9]{32}", self.invocation_id)) and
                self.invocation_id == expected_invocation, "unaccepted/replaced invocation")
        require(self.job_id == 0, "pending systemd job")
        require(self.control_group.startswith("/") and self.control_group != "/", "unknown cgroup")
        require(self.kill_mode == "control-group", "unbounded KillMode")
        require(0 < self.runtime_max_usec <= timeout_seconds * 1_000_000, "unbounded unit runtime")
        require(self.main_pid == 0, "unit main process still present")
        require((self.active_state, self.sub_state) in {("inactive", "dead"), ("failed", "failed"),
                                                      ("active", "exited")}, "unit not terminal")
        require(self.exec_main_code in {1, 2, 3} and bool(self.result), "missing execution result")
        require(bool(self.groups) and self.groups[0][0] == self.control_group, "missing recursive cgroup proof")
        require(all(inode > 0 and populated == 0 and not pids
                    for _, inode, populated, pids in self.groups), "cgroup still populated")
        return True


@dataclass(frozen=True)
class LaunchReceipt:
    """Durable accepted identities from trusted launcher, independent of inspection.

    A mutation adapter must persist this before losing its control connection.
    Lookup after ambiguity reads that receipt; it must not adopt a currently
    visible unit's InvocationID or issue another systemd start.
    """
    intent_hash: str
    sender_invocation: str
    worker_invocation: str

    def validate(self, intent):
        require(self.intent_hash == digest(intent), "accepted launch binding changed")
        require(all(re.fullmatch(r"[a-f0-9]{32}", value) for value in
                    (self.sender_invocation, self.worker_invocation)), "accepted invocation missing")
        return self


@dataclass(frozen=True)
class StageProof:
    intent_hash: str
    sender: UnitProof
    worker: UnitProof

    def validate(self, intent, launch):
        require(isinstance(launch, LaunchReceipt), "independent accepted launch receipt required")
        launch.validate(intent)
        require(self.intent_hash == digest(intent), "stage receipt binding changed")
        self.sender.terminal(intent["sender_unit"], intent["timeout_seconds"], launch.sender_invocation)
        self.worker.terminal(intent["worker_unit"], intent["timeout_seconds"], launch.worker_invocation)
        return self

    @property
    def succeeded(self):
        return all(p.result == "success" and p.exec_main_code == 1 and p.exec_main_status == 0
                   for p in (self.sender, self.worker))


class DenyBoundary:
    """Fail-closed default. No subprocess or production mutation is attempted."""
    ready = False

    def submit_once(self, intent):
        raise FenceDenied("trusted bounded sender/worker adapter not installed")

    def stop_once(self, intent):
        raise FenceDenied("trusted bounded sender/worker stop adapter not installed")

    def inspect(self, intent):
        raise FenceDenied("terminal sender and worker evidence unavailable")

    def accepted_launch(self, intent):
        raise FenceDenied("durable accepted launch identities unavailable")


class SystemdReadbackBoundary(DenyBoundary):
    """Read-only Linux adapter; cannot launch or stop stages, readiness stays false.

    Requires retained exact unit metadata AND still-addressable cgroup v2 trees.
    Garbage-collected/not-found units or cgroups fail closed. Delivery must arrange
    retained evidence; this class never infers terminal state from disappearance.
    """
    def __init__(self, cgroup_root=Path("/sys/fs/cgroup"), run=subprocess.run):
        self.cgroup_root = Path(cgroup_root)
        self.run = run

    def _properties(self, unit):
        require(bool(re.fullmatch(r"wbc-release-[a-f0-9]{24}-(sender|worker)\.service", unit)), "unsafe unit")
        response = self.run(["systemctl", "show", unit, "--no-pager"],
                            capture_output=True, text=True, timeout=10)
        require(response.returncode == 0, "systemd readback unavailable")
        props = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
        require(props.get("LoadState") == "loaded" and props.get("Id") == unit, "unit not found or replaced")
        return props

    def _groups(self, group):
        require(group.startswith("/") and group != "/" and ".." not in Path(group).parts, "unsafe cgroup")
        root = self.cgroup_root / group.lstrip("/")
        require(root.resolve(strict=True) == root and self.cgroup_root.resolve(strict=True) == self.cgroup_root,
                "cgroup path alias")
        require((self.cgroup_root / "cgroup.controllers").is_file(), "cgroup v2 required")
        result = []
        def fail_walk(error):
            raise FenceDenied("cgroup inventory unreadable") from error
        for current, directories, _ in os.walk(root, followlinks=False, onerror=fail_walk):
            path = Path(current)
            require(not path.is_symlink() and all(not (path / name).is_symlink() for name in directories), "cgroup symlink")
            before = path.stat()
            events = dict(line.split() for line in (path / "cgroup.events").read_text().splitlines())
            pids = tuple(int(pid) for pid in (path / "cgroup.procs").read_text().split())
            require(before.st_ino == path.stat().st_ino, "cgroup replaced")
            relative = "/" + str(path.relative_to(self.cgroup_root))
            result.append((relative, before.st_ino, int(events["populated"]), pids))
            require(len(result) <= 4096, "unbounded cgroup topology")
        require(bool(result), "missing cgroup is not terminal proof")
        return tuple(result)

    @staticmethod
    def _duration_usec(value):
        if value.isdigit():
            return int(value)
        units = {"us": 1, "ms": 1000, "s": 1_000_000, "min": 60_000_000,
                 "h": 3_600_000_000, "d": 86_400_000_000}
        parts = value.split()
        require(bool(parts), "unknown RuntimeMaxUSec")
        total = 0
        for part in parts:
            match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(us|ms|s|min|h|d)", part)
            require(match is not None, "unbounded/unknown RuntimeMaxUSec")
            total += float(match[1]) * units[match[2]]
        require(math.isfinite(total) and total > 0, "invalid RuntimeMaxUSec")
        return math.ceil(total)

    @staticmethod
    def _job_id(value):
        # systemctl's printable Job forms, never infer absence from missing data.
        match = re.fullmatch(r"\(?([0-9]+)(?:,? /(?:org/freedesktop/systemd1/job/[0-9]+)?)?\)?", value)
        require(match is not None, "unknown systemd Job")
        return int(match[1])

    def _unit(self, name):
        first = self._properties(name)
        groups = self._groups(first.get("ControlGroup", ""))
        again = self._properties(name)
        keys = ("Id", "InvocationID", "ControlGroup", "ActiveState", "SubState", "Result", "MainPID",
                "ExecMainCode", "ExecMainStatus", "KillMode", "RuntimeMaxUSec", "Job")
        require(all(first.get(key) == again.get(key) for key in keys), "unit changed during cgroup readback")
        require(groups == self._groups(first["ControlGroup"]), "recursive cgroup changed during readback")
        return UnitProof(first["Id"], first["InvocationID"], first["ControlGroup"], first["ActiveState"],
                         first["SubState"], first["Result"], int(first["MainPID"]), self._job_id(first["Job"]), int(first["ExecMainCode"]),
                         int(first["ExecMainStatus"]), first["KillMode"], self._duration_usec(first["RuntimeMaxUSec"]), groups)

    def inspect(self, intent):
        return StageProof(digest(intent), self._unit(intent["sender_unit"]), self._unit(intent["worker_unit"]))


class ReleaseWindow:
    def __init__(self, state_dir, observer, boundary=None, clock=time.time):
        self.directory = Path(state_dir)
        require(self.directory.is_absolute(), "absolute external state directory required")
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        require(self.directory.resolve(strict=True) == self.directory, "state directory alias")
        self.observer, self.boundary, self.clock = observer, boundary or DenyBoundary(), clock

    @property
    def readiness(self):
        return {"ready": bool(self.boundary.ready), "production_release_ready": False,
                "reason": "core requires reviewed controller integration and bounded mutation adapter"}

    @contextmanager
    def _lock(self):
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        lock = None
        try:
            info = os.fstat(directory)
            require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, "unsafe state directory permissions")
            lock = os.open("admission.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            require(stat.S_ISREG(os.fstat(lock).st_mode) and os.fstat(lock).st_nlink == 1, "unsafe fence lock")
            os.fsync(lock)
            os.fsync(directory)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read(self, directory, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 2_000_000,
                    "unsafe fence record")
            return json.load(stream)

    def _write(self, directory, name, value):
        data = canonical(value)
        require(len(data) <= 2_000_000, "fence record budget exceeded")
        temporary = ".pending-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)

    @staticmethod
    def _name(operation):
        require(isinstance(operation, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation))
                and operation not in {"active", "admission"}, "unsafe/reserved operation ID")
        return operation + ".json"

    @staticmethod
    def _binding(state):
        request = state["request"]
        return {key: request[key] for key in ("operation_id", "W", "P", "paused_revision")} | {
            "request_hash": state["request_hash"], "epoch": state["epoch"]}

    def _load(self, directory, binding, *, read_only=False):
        state = self._read(directory, self._name(binding["operation_id"]))
        require(state is not None, "unknown operation")
        require(digest(state["request"]) == state["request_hash"], "immutable request corrupted")
        expected = self._binding(state)
        compared = dict(binding)
        if read_only:
            compared["epoch"] = expected["epoch"]
        require(compared == expected, "stale epoch/request/W/P/revision")
        return state

    def _save(self, directory, state):
        state["revision"] += 1
        self._write(directory, self._name(state["request"]["operation_id"]), state)

    def _observe(self, request, proofs=None, expected_observation=None):
        observed = self.observer()
        require(observed["binding"] == (expected_observation if expected_observation is not None else request["observation"]), "fresh maintenance/target/policy identity drift")
        require(all(observed.get("proofs", {}).get(key) == value for key, value in (proofs or {}).items()),
                "required stage/restore proof missing or changed")
        return observed

    def arm(self, request):
        request = json.loads(canonical(request))
        name = self._name(request["operation_id"])
        for key in ("W", "P", "paused_revision", "old_sha", "head_sha", "merge_sha", "target_identity",
                    "policy_fingerprint", "manifest_fingerprint", "code_delta_digest", "controller_digest"):
            require(bool(request.get(key)), "missing exact request field: " + key)
        for key in ("old_sha", "head_sha", "merge_sha"):
            require(isinstance(request[key], str) and bool(re.fullmatch(r"[a-f0-9]{40}",request[key])), "invalid exact Git SHA")
        for key in ("policy_fingerprint", "manifest_fingerprint", "code_delta_digest", "controller_digest"):
            require(isinstance(request[key], str) and bool(re.fullmatch(r"[a-f0-9]{64}",request[key])), "invalid exact digest")
        require(type(request["paused_revision"]) is int and request["paused_revision"] >= 0, "invalid paused revision")
        require(all(isinstance(request[k], str) and bool(request[k].strip()) for k in ("W","P")), "invalid W/P")
        require(isinstance(request["target_identity"], dict) and bool(request["target_identity"]), "invalid target identity")
        require(type(request["expires_at"]) in (int,float), "invalid expiry")
        require(all(request["observation"].get(key) == request[key] for key in
                    ("W", "P", "paused_revision", "target_identity", "policy_fingerprint", "manifest_fingerprint")),
                "observation binding incomplete")
        require(math.isfinite(request["expires_at"]) and request["expires_at"] > self.clock(), "expired request")
        stages = request["stages"]
        require(0 < len(stages) <= 64 and len({s["name"] for s in stages}) == len(stages), "invalid stage set")
        require(sum(s["kind"] == "restore" for s in stages) == 1 and sum(s["kind"] == "rollback" for s in stages) == 1,
                "exact recovery stages required")
        forward = [stage for stage in stages if stage["kind"] in {"sync","deploy"}]
        require(bool(forward) and forward[0]["kind"] == "sync" and
                sum(stage["kind"] == "sync" for stage in stages) == 1, "exact initial sync required")
        for stage in stages:
            require(isinstance(stage["name"], str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,100}",stage["name"])), "unsafe stage name")
            require(stage["kind"] in {"sync", "deploy", "rollback", "restore"}, "unknown stage kind")
            require(isinstance(stage["argv"], list) and bool(stage["argv"]) and
                    all(isinstance(a, str) and "\0" not in a for a in stage["argv"]), "exact argv required")
            require(type(stage["timeout_seconds"]) is int and type(stage["reserve_seconds"]) is int and
                    0 < stage["timeout_seconds"] <= 900 and 0 < stage["reserve_seconds"] <= 3600, "missing bounded recovery reserve")
            require(bool(stage.get("pre_observation")) and bool(stage.get("post_observation")),
                    "immutable pre/post stage observations required")
            require(bool(stage.get("preconditions")) and bool(stage.get("postconditions")), "semantic stage proofs required")
        with self._lock() as directory:
            require(self._read(directory, name) is None, "operation tombstone/request already exists")
            active = self._read(directory, "active.json")
            if active:
                prior = self._read(directory, self._name(active["operation_id"]))
                require(prior is not None and prior["phase"] == "closed", "another release owns the fence")
            self._observe(request)
            require(request["expires_at"] > self.clock(), "expired while waiting for admission lock")
            state = {"request": request, "request_hash": digest(request), "epoch": uuid.uuid4().hex,
                     "revision": 0, "phase": "armed", "stages": {}, "abort": False}
            # A crash between these writes leaves a blocking dangling owner, never a free fence.
            self._write(directory, "active.json", {"operation_id": request["operation_id"]})
            self._save(directory, state)
            return self._binding(state)

    def status(self, binding):
        with self._lock() as directory:
            # Lost abort response: old epoch can read the durable recovery binding,
            # but cannot admit a stage or reconcile with its stale epoch.
            state = self._load(directory, binding, read_only=True)
            return json.loads(canonical(state)) | {"binding": self._binding(state), "readiness": self.readiness}

    def submit(self, binding, stage_name):
        with self._lock() as directory:
            state = self._load(directory, binding)
            request = state["request"]
            require(state["phase"] != "closed", "terminal tombstone")
            require(stage_name not in state["stages"], "intent exists: only reconcile, never resubmit")
            stages = {stage["name"]: stage for stage in request["stages"]}
            require(stage_name in stages, "stage not permitted")
            stage = stages[stage_name]
            require(all(s["terminal"] for s in state["stages"].values()), "sender/worker terminal proof missing")
            kind = stage["kind"]
            if kind == "rollback":
                require(state["abort"] and state["phase"] == "rollback_required", "rollback not admitted")
            elif kind == "restore":
                require(state["phase"] in {"deployed", "rolled_back", "abort_clean"}, "coherent terminal deploy/rollback required")
            else:
                require(not state["abort"], "admission closed by abort")
                forward = [s["name"] for s in request["stages"] if s["kind"] in {"sync", "deploy"}]
                done = [name for name in forward if name in state["stages"] and state["stages"][name].get("success")]
                require(forward[len(done)] == stage_name, "out-of-order or failed stage")
            self._observe(request, stage["preconditions"], stage["pre_observation"])
            # Recovery is allowed after lease expiry, under the rotated recovery epoch;
            # forward admission never is. Expiry does not reopen writers.
            if kind in {"sync", "deploy"}:
                require(self.clock() + stage["timeout_seconds"] + stage["reserve_seconds"] <= request["expires_at"],
                        "expired/insufficient rollback and restore reserve")
            require(self.boundary.ready, "production boundary unavailable")
            token = digest({"binding": binding, "stage": stage_name})[:24]
            intent = {"binding": binding, "stage": stage_name, "argv": stage["argv"],
                      "timeout_seconds": stage["timeout_seconds"], "sender_unit": f"wbc-release-{token}-sender.service",
                      "worker_unit": f"wbc-release-{token}-worker.service"}
            state["stages"][stage_name] = {"intent": intent, "terminal": False, "stop_intent": False,
                                                  "dispatch": "pending", "proof": None, "launch": None}
            state["phase"] = {"sync": "syncing", "deploy": "deploying", "rollback": "rolling_back", "restore": "restoring"}[kind]
            self._save(directory, state)  # Durable before sending; lock is not held while worker runs.
        error, launch = None, None
        try:
            launch = self.boundary.submit_once(intent)
            require(isinstance(launch, LaunchReceipt), "submit returned no accepted launch receipt")
            launch.validate(intent)
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
        with self._lock() as directory:
            # Abort may have rotated epoch while sending; recording outcome cannot reopen admission.
            state = self._read(directory, self._name(binding["operation_id"]))
            record = state["stages"][stage_name]
            require(record["intent"] == intent, "intent changed during submit")
            record["dispatch"] = "ambiguous" if error else "returned"
            record["dispatch_error"] = error
            if not error:
                record["launch"] = asdict(launch)
            self._save(directory, state)
            return {"binding": self._binding(state), "phase": state["phase"], "requires_reconcile": True}

    def reconcile(self, binding):
        # Inspect outside the lock; validate epoch and exact intent again before persisting.
        before = self.status(binding)
        proofs = {}
        for name, record in before["stages"].items():
            if not record["terminal"]:
                launch = LaunchReceipt(**record["launch"]) if record.get("launch") else self.boundary.accepted_launch(record["intent"])
                require(isinstance(launch, LaunchReceipt), "accepted launch receipt unavailable")
                launch.validate(record["intent"])
                proof = self.boundary.inspect(record["intent"])
                require(isinstance(proof, StageProof), "typed sender/worker receipt required")
                proofs[name] = (proof.validate(record["intent"], launch), launch)
        with self._lock() as directory:
            state = self._load(directory, binding)
            require(state["revision"] == before["revision"], "state changed during terminal readback")
            stages = {s["name"]: s for s in state["request"]["stages"]}
            for name, (proof, launch) in proofs.items():
                record = state["stages"][name]
                record.update(terminal=True, proof=asdict(proof), launch=asdict(launch), success=False)
                if proof.succeeded:
                    try:
                        self._observe(state["request"], stages[name]["postconditions"], stages[name]["post_observation"])
                        record["success"] = True
                    except Exception as exc:
                        record["semantic_error"] = str(exc)
                if not record["success"]:
                    state["abort"] = True
                    state["epoch"] = uuid.uuid4().hex
            if all(r["terminal"] for r in state["stages"].values()):
                records = state["stages"]
                completed = {stages[n]["kind"] for n, r in records.items() if r.get("success")}
                if "restore" in completed:
                    state["phase"] = "closed"
                elif "rollback" in completed:
                    state["phase"] = "rolled_back"
                elif state["abort"]:
                    state["phase"] = "rollback_required" if records else "abort_clean"
                else:
                    forward = [s["name"] for s in stages.values() if s["kind"] in {"sync", "deploy"}]
                    state["phase"] = "deployed" if all(n in records and records[n].get("success") for n in forward) else "synced"
            self._save(directory, state)
            return {"binding": self._binding(state), "phase": state["phase"]}

    def abort(self, binding):
        with self._lock() as directory:
            state = self._load(directory, binding)
            require(state["phase"] != "closed", "terminal tombstone")
            if not state["abort"]:
                state["abort"] = True
                state["epoch"] = uuid.uuid4().hex
            pending = [r for r in state["stages"].values() if not r["terminal"]]
            if state["phase"] != "rolled_back":
                state["phase"] = "abort_requested" if pending else ("rollback_required" if state["stages"] else "abort_clean")
            stops = []
            for record in pending:
                if not record["stop_intent"]:
                    record["stop_intent"] = True
                    stops.append(record["intent"])
            self._save(directory, state)
            recovery = self._binding(state)
        # The adapter must seal/stop the sender as well as its worker. If sender is
        # still in flight, its terminal proof remains mandatory; stop is not proof.
        errors = []
        for intent in stops:
            try:
                self.boundary.stop_once(intent)
            except Exception as exc:
                errors.append(type(exc).__name__ + ": " + str(exc))
        return {"binding": recovery, "phase": state["phase"], "stop_errors": errors,
                "requires_reconcile": bool(pending)}
