"""Fixed supervised history derivation under the actual caller's ownership.

One frozen source target may continue only with exact immutable dated progress.
No source acquisition or cycle replay occurs; the caller retains SH/heavy.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import fcntl
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import threading
import time
from uuid import uuid4

from packages.application.owned_history_worker_capability import (
    CONTRACT, HistoryDelegationError, file_digest, fingerprint, linux_required,
    lock_proof, process_generation, read_json, receive_message, send_message, history_result_error,
)

ROOT = Path(__file__).resolve().parents[2]
PARENT_GRACE_SECONDS = 5.0
HISTORY_TOTAL_SECONDS = 3600.0
HISTORY_MAX_PORTIONS = 20
_POPEN = subprocess.Popen


def _thread_children():
    path = Path(f"/proc/self/task/{threading.get_native_id()}/children")
    data = path.read_text()
    if len(data) > 65536:
        raise HistoryDelegationError("history_spawn_children_proof_oversized")
    return {int(value) for value in data.split()}


def _recover_spawn_exception(process, before):
    known_pid = getattr(process, "pid", None)
    if isinstance(known_pid, int) and getattr(process, "_child_created", False):
        _kill_reap(process)
        return
    # Supported Linux CPython boundary: one fixed spawn by this native thread.
    # Other HTTP threads' children do not occur in this per-thread kernel list.
    # Missing/ambiguous proof after possible spawn is nonterminal: retain all
    # locks rather than unwind while an unidentified worker may exist.
    while True:
        try:
            added = _thread_children() - before
        except (OSError, ValueError, HistoryDelegationError):
            time.sleep(.05)
            continue
        if len(added) > 1:
            time.sleep(.05)
            continue
        if not added:
            process._owned_history_reaped = True
            return  # No unreaped child of this spawning thread exists.
        pid = added.pop()
        if isinstance(known_pid, int) and pid != known_pid:
            time.sleep(.05)
            continue  # A recorded PID alone cannot identify another child.
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            # May be between fork and setsid/exec, but is our actual new child.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        _, status = os.waitpid(pid, 0)
        process.returncode = os.waitstatus_to_exitcode(status)
        process._owned_history_reaped = True
        return


def source_binding(runtime_dir):
    from packages.application.storage_registry import StoreRegistry
    registry = StoreRegistry(runtime_dir)
    path = registry.resolve("operational")
    value = path.stat()
    return {"path": str(path), "device": value.st_dev, "inode": value.st_ino,
            "manifest": file_digest(registry.manifest_path) if registry.manifest_path.exists() else "implicit"}


def _kill_reap(process):
    if getattr(process, "_owned_history_reaped", False):
        return  # Explicit terminal proof from native-thread kernel recovery.
    if not getattr(process, "_child_created", False):
        if isinstance(getattr(process, "pid", None), int):
            # CPython may assign PID before setting _child_created. Never let
            # that internal flag discard an actual child lifetime obligation.
            _recover_spawn_exception(process, set())
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    # Never release leases while a possibly living worker remains. A delayed
    # kernel termination is not permission to report success or launch again.
    process.wait()


def terminal_readback(root, invocation, anchor):
    """Derived-only observation; cannot authorize retry or claim source validity."""
    from packages.application.web_vitrina_history_store import HistoryStore
    from packages.application.web_vitrina_history_compiler import digest
    store = HistoryStore(Path(root) / "history")
    current_path, pending_path = store.root / "CURRENT.json", store.root / "PENDING.json"
    current = read_json(current_path, limit=4096) if current_path.exists() else None
    pending = read_json(pending_path) if pending_path.exists() else None
    intent_path = Path(root) / "proofs" / ("owned-portion-" + invocation + ".json")
    intent = read_json(intent_path) if intent_path.exists() else None
    observed = {"current": current, "pending_digest": fingerprint(pending) if pending else None,
                "intent": intent, "completed_days": 0, "publication_observed": False}
    if (not anchor or not intent or intent.get("contract") != CONTRACT or intent.get("invocation") != invocation
            or intent.get("anchor_digest") != fingerprint(anchor) or intent.get("base") != anchor["base"]):
        return observed
    vector = anchor["vector"]
    def present(ref):
        if not isinstance(ref, str) or re.fullmatch(r"[0-9a-f]{64}", ref) is None:
            return False
        path = store.root / "objects" / (ref + ".sqlite3")
        return path.is_file() and not path.is_symlink()
    if (pending and pending.get("target") == intent.get("target") and pending.get("base") == anchor["base"]
            and pending.get("vector") == vector and pending.get("catalog") == intent["merged_catalog"]):
        observed["completed_days"] = sum(pending.get("day_proofs", {}).get(day) ==
            {"epoch": vector["epoch"], "token": token} and
            pending.get("day_catalogs", {}).get(day) == intent["fresh_catalog"] and present(pending.get("refs", {}).get(day))
            for day, token in vector["dates"].items() if day in anchor.get("scope_dates", vector["dates"]))
    if current and current.get("current") != anchor["base"]:
        edition_id = current["current"]
        if not isinstance(edition_id, str) or re.fullmatch(r"[0-9a-f]{64}", edition_id) is None:
            raise HistoryDelegationError("history_readback_current_unsafe")
        edition = read_json(store.root / "editions" / (edition_id + ".json"))
        if digest(edition) != edition_id:
            raise HistoryDelegationError("history_readback_edition_corrupt")
        proofs = store.day_proofs(edition)
        observed["publication_observed"] = (current.get("previous") == anchor["base"]
            and edition["catalog"] == intent["merged_catalog"] and
            all(proofs.get(day) == {"epoch": vector["epoch"], "token": token}
                and present(edition["days"].get(day)) for day, token in vector["dates"].items() if day in anchor.get("scope_dates", vector["dates"])))
    return observed


class _OwnedHistoryWorker:
    """One anchor and fixed target. Each actual child has a fresh FD capability."""
    def __init__(self, *, runtime, config, cycle_owner, lock_fds, operation="cycle"):
        self.runtime, self.config = runtime, config
        self.cycle_owner = dict(cycle_owner) if cycle_owner is not None else None
        self.operation = operation
        self.lock_fds = lock_fds
        self.pid, self.thread = os.getpid(), threading.current_thread()
        self._process = None
        self._closed = False
        self._anchor = None
        self._capture_used = False
        self._portion_used = False
        self._completion_used = False
        self._completion_deadline = None
        self._source_dates = self._backfill_dates = None
        self._target = self._closed_binding = self._closed_proof = None
        self._historical_binding = self._historical_proof = self._historical_receipt = None
        self._policy_binding = self._policy_proof = self._policy_receipt = None
        self._supplier_binding = self._supplier_proof = self._supplier_receipt = None
        self._source_binding = source_binding(runtime.runtime_dir)
        self._contract_digest = file_digest(config.runtime_contract)

    def _check(self):
        from packages.application.business_data_heavy_admission import require_heavy_owner
        from packages.application.web_vitrina_snapshot_admission import api_jobs_admission
        if (self.pid != os.getpid() or self.thread is not threading.current_thread() or self._closed
                or require_heavy_owner(self.runtime.runtime_dir).operation != self.operation
                or source_binding(self.runtime.runtime_dir) != self._source_binding
                or file_digest(self.config.runtime_contract) != self._contract_digest
                or api_jobs_admission(self.runtime.runtime_dir, cycle_owner=self.cycle_owner) != "idle"):
            raise HistoryDelegationError("history_parent_owner_changed")

    def capture(self, now: datetime):
        self._check()
        if self._capture_used or self._portion_used:
            raise HistoryDelegationError("history_capture_already_consumed")
        if now.tzinfo is None:
            raise HistoryDelegationError("history_clock_requires_timezone")
        self._capture_used = True
        result = self._invoke("capture", now.isoformat(), None)
        if result["status"] == "complete" and result["result"].get("status") == "captured":
            self._anchor = deepcopy(result["result"]["anchor"])
        else:
            # Unknown is one submitted operation, never an invitation to resend.
            self._portion_used = True
        return result

    def portion(self, anchor):
        self._check()
        if self._portion_used or self._anchor is None or anchor != self._anchor:
            raise HistoryDelegationError("history_portion_anchor_or_replay_refused")
        self._portion_used = True  # Before Popen; even ambiguous startup is consumed.
        return self._invoke("portion", anchor["now"], anchor)

    def complete(self, now, *, backfill_dates=(), closed_receipt=None, historical_receipt=None, policy_receipt=None, supplier_receipt=None, source_range=None,
                 total_seconds=HISTORY_TOTAL_SECONDS, max_portions=HISTORY_MAX_PORTIONS):
        """One frozen target; only verified strict dated progress permits more work."""
        from packages.business_time import current_business_date_iso
        from packages.application.web_vitrina_history_compiler import dates_between
        self._check()
        if self._completion_used or self._capture_used or self._portion_used:
            raise HistoryDelegationError("history_completion_already_consumed")
        if (not 0 < total_seconds <= HISTORY_TOTAL_SECONDS or type(max_portions) is not int or not 1 <= max_portions <= HISTORY_MAX_PORTIONS
                or type(backfill_dates) is not tuple or tuple(sorted(set(backfill_dates))) != backfill_dates):
            raise HistoryDelegationError("history_completion_limits_invalid")
        if sum(receipt is not None for receipt in (historical_receipt, policy_receipt, supplier_receipt)) > 1:
            raise HistoryDelegationError("history_multiple_source_authorities")
        today = current_business_date_iso(now)
        first = (datetime.fromisoformat(today) - timedelta(days=13)).date().isoformat()
        if self.operation == "cycle":
            if source_range is not None or (historical_receipt is None and policy_receipt is None and supplier_receipt is None and len(backfill_dates) > 2) or any(day >= (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat() for day in backfill_dates):
                raise HistoryDelegationError("history_cycle_backfill_scope_invalid")
            for day in backfill_dates:
                datetime.strptime(day, "%Y-%m-%d")
            dates = tuple(dates_between(min((first, *backfill_dates)), today))
        else:
            if closed_receipt is not None or source_range is None or max_portions != 1 or total_seconds > 240:
                raise HistoryDelegationError("history_standalone_limits_invalid")
            dates = tuple(dates_between(*source_range))
            if not set(backfill_dates) <= set(dates):
                raise HistoryDelegationError("history_standalone_backfill_outside_range")
        if closed_receipt is not None:
            from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
            if (type(closed_receipt) is not ClosedBacklog or closed_receipt.runtime is not self.runtime
                    or len(closed_receipt.publication_dates()) > 2
                    or not set(closed_receipt.publication_dates()) <= set(backfill_dates)
                    or (historical_receipt is None and policy_receipt is None and supplier_receipt is None
                        and closed_receipt.publication_dates() != backfill_dates)):
                raise HistoryDelegationError("history_closed_receipt_context_changed")
            from packages.application.owned_history_native_ack import closed_receipt_digest
            self._closed_binding = {"digest": closed_receipt_digest(closed_receipt.status()), "dates": list(closed_receipt.publication_dates())}
        if historical_receipt is not None:
            from packages.application.fbs_accounting_historical_history import HistoricalReceipt
            if type(historical_receipt) is not HistoricalReceipt or historical_receipt.runtime is not self.runtime or self.operation!='cycle':
                raise HistoryDelegationError('historical_history_receipt_context_changed')
            historical_dates=historical_receipt.publication_dates()
            old_dates=tuple(day for day in historical_dates if day < (datetime.fromisoformat(today)-timedelta(days=1)).date().isoformat())
            closed_dates=tuple(self._closed_binding['dates']) if self._closed_binding else ()
            if backfill_dates != tuple(sorted(set(old_dates)|set(closed_dates))):
                raise HistoryDelegationError('historical_history_exact_scope_changed')
            historical_receipt.validate_sources_readonly(now=now)
            self._historical_binding=historical_receipt.binding()
            self._historical_receipt=historical_receipt
        elif self.operation == "cycle" and backfill_dates and closed_receipt is None and policy_receipt is None and supplier_receipt is None:
            raise HistoryDelegationError("history_closed_receipt_required")
        if policy_receipt is not None:
            from packages.application.operator_policy_history import PolicyHistory
            if type(policy_receipt) is not PolicyHistory or policy_receipt.runtime is not self.runtime or self.operation!='cycle':
                raise HistoryDelegationError('policy_history_context_changed')
            selected=tuple(d for d in policy_receipt.publication_dates() if d < (datetime.fromisoformat(today)-timedelta(days=1)).date().isoformat())
            expected=tuple(sorted(set(selected)|set((self._closed_binding or {}).get('dates',[]))))
            if backfill_dates!=expected:
                raise HistoryDelegationError('policy_history_scope_changed')
            policy_receipt.validate_sources_readonly(now=now)
            self._policy_binding=policy_receipt.binding();self._policy_receipt=policy_receipt
        if supplier_receipt is not None:
            from packages.application.operator_supplier_history import SupplierHistory
            if type(supplier_receipt) is not SupplierHistory or supplier_receipt.runtime is not self.runtime or self.operation!='cycle':
                raise HistoryDelegationError('supplier_history_context_changed')
            selected=tuple(d for d in supplier_receipt.publication_dates() if d < (datetime.fromisoformat(today)-timedelta(days=1)).date().isoformat())
            expected=tuple(sorted(set(selected)|set((self._closed_binding or {}).get('dates',[]))))
            if backfill_dates!=expected:
                raise HistoryDelegationError('supplier_history_scope_changed')
            supplier_receipt.validate_sources_readonly(now=now)
            self._supplier_binding=supplier_receipt.binding();self._supplier_receipt=supplier_receipt
        self._completion_used = True
        self._source_dates, self._backfill_dates = dates, backfill_dates
        self._completion_deadline = time.monotonic() + total_seconds
        captured = self.capture(now)
        if captured.get("status") != "complete" or captured["result"].get("status") != "captured":
            raise history_result_error("history_completion_capture_unproven", captured, mode="capture")
        anchor = self._anchor
        # Source warming is not dated progress. No new capture/adoption occurs.
        previous = self._verified_state(anchor)
        progress = previous["completed"]
        if previous["terminal"]:
            return self._finish_completion(previous, closed_receipt, portions=0)
        for number in range(1, max_portions + 1):
            if time.monotonic() + PARENT_GRACE_SECONDS >= self._completion_deadline:
                raise HistoryDelegationError("history_completion_total_deadline")
            # Internal continuation only. Each prior child was actually reaped,
            # and this supervisor alone checked exact progress/anchor below.
            self._portion_used = True
            result = self._invoke("portion", anchor["now"], anchor)
            intent = result.get("readback", {}).get("intent")
            if intent:
                if (intent.get("contract") != CONTRACT or intent.get("invocation") != result["invocation"]
                        or intent.get("anchor_digest") != fingerprint(anchor) or intent.get("base") != anchor["base"]):
                    raise HistoryDelegationError("history_completion_intent_unbound")
                target = {key: intent[key] for key in ("fresh_catalog", "merged_catalog", "target")}
                if self._target is not None and target != self._target:
                    raise HistoryDelegationError("history_completion_target_changed")
                self._target = target
            # Always read the same exact CURRENT before another compile, even
            # when the transport outcome is unknown after a possible publish.
            observed = self._verified_state(anchor)
            if number == 1 and intent:
                progress = intent["baseline_completed"]
            if observed["terminal"]:
                return self._finish_completion(observed, closed_receipt, portions=number)
            if result.get("status") == "complete" and result["result"].get("status") not in {"pending"}:
                raise history_result_error("history_completion_portion_failed", result, mode="portion")
            current = observed["completed"]
            if not (set(progress) < set(current) and all(current[day] == ref for day, ref in progress.items())):
                raise history_result_error("history_completion_no_dated_progress", result, mode="portion")
            progress = current
        raise HistoryDelegationError("history_completion_portion_limit")

    def _verified_state(self, anchor):
        result = self._invoke("verify", anchor["now"], anchor)
        if (result.get("status") != "complete" or result["result"].get("status") != "verified"
                or result["result"].get("invocation") != result.get("invocation")):
            raise history_result_error("history_completion_readback_unproven", result, mode="verify")
        return result["result"]

    def _finish_completion(self, observed, closed_receipt, *, portions):
        from packages.application.owned_history_native_ack import source_stamp
        if source_stamp(self.runtime.runtime_dir, self.runtime.db_path) != observed["source_stamp"]:
            raise HistoryDelegationError("history_source_changed_after_readback")
        if closed_receipt is not None:
            from packages.application.owned_history_native_ack import _VerifiedClosedHistory
            closed = observed.get("closed")
            if not closed or closed["receipt_digest"] != self._closed_binding["digest"]:
                raise HistoryDelegationError("history_closed_terminal_unproven")
            self._closed_proof = _VerifiedClosedHistory(self, observed["invocation"], closed["receipt_digest"],
                tuple(self._closed_binding["dates"]), observed["current"], closed["native"], observed["source_stamp"])
            closed_receipt._acknowledge_verified_native(self._closed_proof, backfill_dates=tuple(self._closed_binding["dates"]))
        if self._historical_receipt is not None:
            from packages.application.owned_history_native_ack import _VerifiedHistoricalHistory
            historical=observed.get('historical')
            if not historical or historical['receipt_digest']!=self._historical_binding['digest']:
                raise HistoryDelegationError('historical_history_terminal_unproven')
            self._historical_proof=_VerifiedHistoricalHistory(self,observed['invocation'],historical['receipt_digest'],
                tuple(self._historical_binding['dates']),observed['current'],historical['native'],observed['source_stamp'])
            self._historical_receipt._acknowledge_verified_native(self._historical_proof)
        if self._policy_receipt is not None:
            from packages.application.owned_history_native_ack import _VerifiedPolicyHistory
            policy=observed.get('policy')
            if not policy or policy['receipt_digest']!=self._policy_binding['digest']:
                raise HistoryDelegationError('policy_history_terminal_unproven')
            self._policy_proof=_VerifiedPolicyHistory(self,observed['invocation'],policy['receipt_digest'],
                tuple(self._policy_binding['dates']),observed['current'],policy['native'],observed['source_stamp'])
            self._policy_receipt._acknowledge_verified_native(self._policy_proof)
        if self._supplier_receipt is not None:
            from packages.application.owned_history_native_ack import _VerifiedSupplierHistory
            supplier=observed.get('supplier')
            if not supplier or supplier['receipt_digest']!=self._supplier_binding['digest']:
                raise HistoryDelegationError('supplier_history_terminal_unproven')
            self._supplier_proof=_VerifiedSupplierHistory(self,observed['invocation'],supplier['receipt_digest'],
                tuple(self._supplier_binding['dates']),observed['current'],supplier['native'],observed['source_stamp'])
            self._supplier_receipt._acknowledge_verified_native(self._supplier_proof)
        return {"status": "unchanged" if observed["current"]["current"] == self._anchor["base"] else "published",
                "edition_id": observed["current"]["current"], "vector_digest": fingerprint(self._anchor["vector"]),
                "window_from": self._anchor["window_from"], "window_to": self._anchor["window_to"],
                "portions": portions, "completed": len(observed["completed"]),
                "backfill_count": len(self._backfill_dates), "closed_ack": closed_receipt is not None,
                "historical_ack": self._historical_receipt is not None, "policy_ack": self._policy_receipt is not None, "supplier_ack": self._supplier_receipt is not None}

    def _invoke(self, mode, now, anchor):
        try:
            return self._invoke_owned(mode, now, anchor)
        except HistoryDelegationError as exc:
            diagnostic = exc.diagnostic(mode=mode)
            raise HistoryDelegationError(diagnostic["error_code"], mode=diagnostic["mode"],
                reason_code=diagnostic["reason_code"], outcome=diagnostic["outcome"]) from None

    def _invoke_owned(self, mode, now, anchor):
        self._check()
        if self._process is not None:
            raise HistoryDelegationError("history_child_already_live")
        config, runtime = self.config, self.runtime
        invocation = uuid4().hex
        seconds = min(240.0, float(config.budget_seconds) + PARENT_GRACE_SECONDS)
        if self._completion_deadline is not None:
            seconds = min(seconds, self._completion_deadline - time.monotonic())
        if self._completion_deadline is not None and seconds <= PARENT_GRACE_SECONDS:
            raise HistoryDelegationError("history_completion_total_deadline")
        deadline = time.monotonic() + seconds
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        process = None
        paths = [Path(runtime.runtime_dir) / ".business-data-procedure-admission.lock",
                 Path(runtime.runtime_dir) / ".business-data-heavy-admission.lock",
                 Path(runtime.runtime_dir) / ".web-vitrina-finished-builder.lock",
                 Path(config.candidate_root) / "candidate.lock"]
        try:
            locks = [{"fd": fd, "proof": lock_proof(fd, path, mode=kind, parent_pid=self.pid)}
                for fd, path, kind in zip(self.lock_fds, paths, ["READ", "WRITE", "WRITE", "WRITE"], strict=True)]
            binding = source_binding(runtime.runtime_dir)
            if binding["path"] != str(Path(runtime.db_path).resolve()):
                raise HistoryDelegationError("history_noncanonical_source")
            cap = {"contract": CONTRACT, "mode": mode, "invocation": invocation,
                "parent_pid": self.pid, "parent_generation": process_generation(self.pid),
                "runtime_dir": str(Path(runtime.runtime_dir).resolve()), "candidate_root": str(Path(config.candidate_root)),
                "runtime_contract": str(Path(config.runtime_contract).resolve()),
                "runtime_contract_digest": file_digest(config.runtime_contract), "source": binding,
                "cycle_owner": self.cycle_owner, "owner_kind": self.operation, "locks": locks, "now": now,
                "source_dates": self._source_dates, "backfill_dates": self._backfill_dates,
                "target": self._target, "closed_receipt": self._closed_binding, "historical_receipt": self._historical_binding, "policy_receipt": self._policy_binding, "supplier_receipt": self._supplier_binding,
                "formula_epoch": config.formula_epoch,
                "inner_seconds": min(float(config.budget_seconds), seconds - PARENT_GRACE_SECONDS),
                "max_recomputes": config.max_recomputes, "anchor": anchor}
            if time.monotonic() >= deadline:
                raise HistoryDelegationError("history_budget_exhausted_before_spawn")
            # Retain the object BEFORE initialization: an exception after fork
            # must not lose the PID/handle. Signal delivery to this thread is
            # masked across the native spawn->PID assignment boundary. No
            # caller-provided Popen/factory/command is accepted.
            process = _POPEN.__new__(_POPEN)
            self._process = process
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM, signal.SIGHUP})
            try:
                before_children = _thread_children()  # Refuse before launch if unavailable.
                try:
                    process.__init__([sys_executable(), "-m", "apps.web_vitrina_owned_history_worker", str(child.fileno())],
                        cwd=ROOT, pass_fds=(child.fileno(), *self.lock_fds), close_fds=True,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                except BaseException:
                    _recover_spawn_exception(process, before_children)
                    raise
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            child.close()
            self._check()
            if time.monotonic() >= deadline:
                _kill_reap(process)
                return {"status": "outcome_unknown", "invocation": invocation,
                        "readback": terminal_readback(config.candidate_root, invocation, anchor)}
            cap.update(child_pid=process.pid, child_generation=process_generation(process.pid))
            parent.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                # A failed send can follow delivery and actual publication.
                # Resolve that ambiguity with the same post-reap readback.
                send_message(parent, cap)
                response = receive_message(parent)
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                if (process.returncode != 0 or not isinstance(response, dict)
                        or response.get("invocation") != invocation or response.get("contract") != CONTRACT):
                    raise HistoryDelegationError("history_child_terminal_unproven")
            except (OSError, ValueError, subprocess.TimeoutExpired, HistoryDelegationError):
                _kill_reap(process)
                response = None
            self._check()
            observed = terminal_readback(config.candidate_root, invocation, anchor)
            if response is None:
                return {"status": "outcome_unknown", "invocation": invocation, "readback": observed}
            return {"status": "complete", "invocation": invocation, "result": response["result"], "readback": observed}
        finally:
            if process is not None:
                _kill_reap(process)
            self._process = None
            parent.close()
            child.close()

    def close(self):
        # Forked/copied supervisor is not authority and must not signal its parent.
        if os.getpid() != self.pid or threading.current_thread() is not self.thread:
            raise HistoryDelegationError("history_supervisor_owner_mismatch")
        if self._process is not None:
            _kill_reap(self._process)
        self._closed = True


def sys_executable():
    import sys
    return sys.executable


@contextmanager
def owned_history_worker(*, runtime, config, cycle_owner):
    """Fixed actual cycle owner; never a held-window exception."""
    with _history_worker(runtime=runtime, config=config, cycle_owner=cycle_owner, operation="cycle") as worker:
        yield worker


@contextmanager
def standalone_history_worker(*, runtime, config):
    """Ordinary canonical parent must already own history SH/heavy."""
    with _history_worker(runtime=runtime, config=config, cycle_owner=None, operation="history") as worker:
        yield worker


@contextmanager
def _history_worker(*, runtime, config, cycle_owner, operation):
    linux_required()  # Fail before any provisioning on Mac/unknown platforms.
    from apps.web_vitrina_history_candidate_build import runtime_storage_admission
    from apps.web_vitrina_finished_snapshot_build import systemd_admission, lock_admission
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.web_vitrina_snapshot_admission import api_jobs_admission
    from packages.application.warehouse_functional_lock import warehouse_functional_job_is_busy
    source, root = Path(runtime.runtime_dir).resolve(), Path(config.candidate_root)
    owner = require_heavy_owner(source)
    if (owner.operation != operation or owner.fd is None or owner._maintenance is None
            or owner._maintenance.fd is None or root != root.resolve() or root.is_relative_to(source)
            or not 0 < config.budget_seconds <= 240 or not 1 <= config.max_recomputes <= 31):
        raise HistoryDelegationError("history_parent_contract_invalid")
    if (api_jobs_admission(source, cycle_owner=cycle_owner) != "idle" or systemd_admission() != "idle"
            or lock_admission(source / ".wb-finance-daily-worker.lock") != "idle"
            or warehouse_functional_job_is_busy(source)):
        raise HistoryDelegationError("history_parent_admission_busy_or_unknown")
    runtime_storage_admission(root, config.runtime_contract, config.formula_epoch)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptors = []
    supervisor = None
    try:
        for path, create in [(source / ".web-vitrina-finished-builder.lock", False), (root / "candidate.lock", True)]:
            flags = os.O_RDONLY if not create else os.O_RDWR | os.O_CREAT
            fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            descriptors.append(fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_proof(fd, path, mode="WRITE", parent_pid=os.getpid())
        supervisor = _OwnedHistoryWorker(runtime=runtime, config=config, cycle_owner=cycle_owner, operation=operation,
            lock_fds=(owner._maintenance.fd, owner.fd, *descriptors))
        yield supervisor
    finally:
        if supervisor is not None:
            supervisor.close()  # Actual child exit before any parent lock FD closes.
        for fd in reversed(descriptors):
            os.close(fd)  # No LOCK_UN, including if a parent is abruptly killed.
