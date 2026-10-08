"""Local race/durability fixtures. No systemd mutation or production calls."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.hosted_runtime_release_window import (
    DenyBoundary, FenceDenied, LaunchReceipt, ReleaseWindow, StageProof, SystemdReadbackBoundary, UnitProof, digest,
)


def request():
    facts = {"W": "W1", "P": "P1", "paused_revision": 3, "target_identity": {"device": 7, "inode": 19},
             "policy_fingerprint": "f"*64, "manifest_fingerprint": "0"*64}
    return {"operation_id": "op1", **facts, "observation": copy.deepcopy(facts),
            "old_sha": "a"*40, "head_sha": "b"*40, "merge_sha": "c"*40,
            "controller_digest": "d"*64, "code_delta_digest": "e"*64, "expires_at": 1000,
            "stages": [{"name": name, "kind": kind, "argv": ["/trusted/worker", name],
                        "timeout_seconds": 30, "reserve_seconds": 100,
                        "pre_observation":copy.deepcopy(facts), "post_observation":copy.deepcopy(facts),
                        "preconditions": {"coherent": "bound"}, "postconditions": {"coherent": "bound"}}
                       for name, kind in (("sync", "sync"), ("metadata", "deploy"), ("restart", "deploy"),
                                          ("finalize", "deploy"), ("rollback", "rollback"), ("restore", "restore"))]}


def unit(intent, role, *, populated=0, invocation="1"*32, result="success"):
    group = "/system.slice/" + intent[role+"_unit"]
    return UnitProof(intent[role+"_unit"], invocation, group, "active", "exited", result,
                     0, 0, 1, 0 if result == "success" else 1, "control-group", 30_000_000,
                     ((group, 91, populated, (123,) if populated else ()),))


class FixtureBoundary(DenyBoundary):
    ready = True

    def __init__(self):
        self.submits = []
        self.stops = []
        self.proofs = {}
        self.accepted = {}
        self.dispatch = lambda intent: None

    def submit_once(self, intent):
        self.submits.append(copy.deepcopy(intent))
        self.accepted[intent["stage"]] = LaunchReceipt(digest(intent),"1"*32,"1"*32)
        self.dispatch(intent)
        return self.accepted[intent["stage"]]

    def stop_once(self, intent):
        self.stops.append(copy.deepcopy(intent))

    def accepted_launch(self, intent):
        return self.accepted[intent["stage"]]

    def inspect(self, intent):
        if intent["stage"] not in self.proofs:
            raise FenceDenied("no retained terminal sender/worker proof")
        return self.proofs[intent["stage"]]

    def finish(self, intent, **worker_kwargs):
        self.proofs[intent["stage"]] = StageProof(digest(intent), unit(intent,"sender"), unit(intent,"worker", **worker_kwargs))


class FenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name).resolve()/"fence"
        self.req = request()
        self.observed = {"binding": copy.deepcopy(self.req["observation"]), "proofs": {"coherent": "bound"}}
        self.boundary = FixtureBoundary()
        self.now = 100
        self.fence = ReleaseWindow(self.path, lambda: copy.deepcopy(self.observed), self.boundary, lambda: self.now)
        self.binding = self.fence.arm(self.req)

    def tearDown(self):
        self.tmp.cleanup()

    def finish(self, name):
        intent = self.fence.status(self.binding)["stages"][name]["intent"]
        self.boundary.finish(intent)
        self.binding = self.fence.reconcile(self.binding)["binding"]

    def test_default_production_denied_without_intent(self):
        fence = ReleaseWindow(self.path, lambda: self.observed, clock=lambda: self.now)
        self.assertFalse(fence.readiness["ready"])
        with self.assertRaises(FenceDenied): fence.submit(self.binding, "sync")
        self.assertEqual(fence.status(self.binding)["stages"], {})

    def test_request_mutation_epoch_window_revision_denied(self):
        for key in ("request_hash", "epoch", "W", "P", "paused_revision"):
            b = dict(self.binding); b[key] = "wrong"
            with self.subTest(key=key), self.assertRaises(FenceDenied): self.fence.submit(b,"sync")
        self.req["W"] = "mutated caller"
        self.assertEqual(self.fence.status(self.binding)["request"]["W"], "W1")
        self.assertEqual(self.boundary.submits,[])

    def test_admission_rereads_after_lock_wait(self):
        entered = threading.Event()
        errors = []
        with self.fence._lock():
            def queued():
                entered.set()
                try: self.fence.submit(self.binding,"sync")
                except FenceDenied as exc: errors.append(str(exc))
            t = threading.Thread(target=queued); t.start(); entered.wait(2)
            self.observed["binding"]["P"] = "drift"
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(errors),1)
        self.assertEqual(self.boundary.submits,[])

    def test_expiry_and_budget_after_queue(self):
        self.now = 900
        with self.assertRaises(FenceDenied): self.fence.submit(self.binding,"sync")
        self.assertEqual(self.boundary.submits,[])

    def test_abort_closes_queued_stages_and_keeps_recovery_binding(self):
        old = self.binding
        self.binding = self.fence.abort(old)["binding"]
        self.assertNotEqual(old["epoch"], self.binding["epoch"])
        self.assertEqual(self.fence.status(old)["binding"],self.binding)
        for name in ("sync","metadata","restart","finalize"):
            for b in (old, self.binding):
                with self.subTest(name=name), self.assertRaises(FenceDenied): self.fence.submit(b,name)
        self.assertEqual(self.boundary.submits,[])

    def test_pending_late_sender_blocks_restore_even_worker_dead(self):
        started, release = threading.Event(), threading.Event()
        self.boundary.dispatch = lambda intent: (started.set(), release.wait(3))
        t = threading.Thread(target=lambda:self.fence.submit(self.binding,"sync")); t.start()
        self.assertTrue(started.wait(2))
        old = self.binding
        self.binding = self.fence.abort(old)["binding"]
        intent = self.boundary.submits[0]
        self.boundary.proofs["sync"] = StageProof(digest(intent),unit(intent,"sender",populated=1),unit(intent,"worker"))
        with self.assertRaises(FenceDenied): self.fence.reconcile(self.binding)
        for name in ("rollback","restore"):
            with self.assertRaises(FenceDenied): self.fence.submit(self.binding,name)
        release.set(); t.join(2)
        self.assertEqual(len(self.boundary.submits),1)
        self.assertEqual(len(self.boundary.stops),1)
        self.boundary.finish(intent)
        self.binding = self.fence.reconcile(self.binding)["binding"]
        self.assertEqual(self.fence.status(self.binding)["phase"],"rollback_required")

    def test_disconnect_no_resubmit_not_found_not_terminal(self):
        self.boundary.dispatch = Mock(side_effect=ConnectionError("SSH lost"))
        self.fence.submit(self.binding,"sync")
        restarted = ReleaseWindow(self.path,lambda:self.observed,self.boundary,lambda:self.now)
        with self.assertRaises(FenceDenied): restarted.submit(self.binding,"sync")
        with self.assertRaises(FenceDenied): restarted.reconcile(self.binding)
        with self.assertRaises(FenceDenied): restarted.submit(self.binding,"restore")
        self.assertEqual(len(self.boundary.submits),1)

    def test_forked_descendant_and_boolean_empty_are_not_proof(self):
        self.fence.submit(self.binding,"sync")
        intent = self.boundary.submits[0]
        for bad in (True, {"empty":True}, StageProof(digest(intent),unit(intent,"sender"),unit(intent,"worker",populated=1))):
            self.boundary.proofs["sync"] = bad
            with self.assertRaises(FenceDenied): self.fence.reconcile(self.binding)
        self.assertFalse(self.fence.status(self.binding)["stages"]["sync"]["terminal"])

    def test_failed_worker_rotates_binding_and_requires_rollback(self):
        self.fence.submit(self.binding,"sync")
        self.boundary.finish(self.boundary.submits[0],result="exit-code")
        self.binding = self.fence.reconcile(self.binding)["binding"]
        self.assertEqual(self.fence.status(self.binding)["phase"],"rollback_required")
        with self.assertRaises(FenceDenied): self.fence.submit(self.binding,"restore")
        self.fence.submit(self.binding,"rollback"); self.finish("rollback")
        self.fence.submit(self.binding,"restore"); self.finish("restore")
        self.assertEqual(self.fence.status(self.binding)["phase"],"closed")
        with self.assertRaises(FenceDenied): self.fence.arm(request())

    def test_semantic_mixed_tree_proof_failure_holds(self):
        self.fence.submit(self.binding,"sync")
        self.observed["proofs"]["coherent"] = "mixed"
        self.finish("sync")
        state = self.fence.status(self.binding)
        self.assertEqual(state["phase"],"rollback_required")
        self.assertTrue(state["stages"]["sync"]["terminal"])
        self.assertFalse(state["stages"]["sync"]["success"])
        with self.assertRaises(FenceDenied): self.fence.submit(self.binding,"rollback")

    def test_restore_lost_response_late_metadata_never_runs(self):
        self.binding = self.fence.abort(self.binding)["binding"]
        self.boundary.dispatch = Mock(side_effect=TimeoutError("lost restore response"))
        self.fence.submit(self.binding,"restore")
        with self.assertRaises(FenceDenied): self.fence.submit(self.binding,"restore")
        self.finish("restore")
        for stage in ("sync","metadata","restart","finalize","restore"):
            with self.assertRaises(FenceDenied): self.fence.submit(self.binding,stage)
        self.assertEqual(len(self.boundary.submits),1)
        self.assertEqual(self.fence.status(self.binding)["phase"],"closed")

    def test_other_operation_blocked_and_tombstone_retained(self):
        other=request(); other["operation_id"]="op2"
        with self.assertRaises(FenceDenied): self.fence.arm(other)
        self.binding=self.fence.abort(self.binding)["binding"]
        self.fence.submit(self.binding,"restore"); self.finish("restore")
        self.fence.arm(other)
        self.assertTrue((self.path/"op1.json").exists())
        with self.assertRaises(FenceDenied): self.fence.arm(request())

    def test_durable_intent_precedes_send_and_lock_not_held(self):
        def check(intent):
            fresh=ReleaseWindow(self.path,lambda:self.observed,self.boundary,lambda:self.now)
            state=fresh.status(self.binding)
            self.assertEqual(state["stages"]["sync"]["intent"],intent)
            self.assertEqual(state["stages"]["sync"]["dispatch"],"pending")
        self.boundary.dispatch=check
        self.fence.submit(self.binding,"sync")
        self.assertIsNone(self.fence.status(self.binding)["stages"]["sync"]["dispatch_error"])

    def test_repeated_abort_stops_once(self):
        self.fence.submit(self.binding,"sync")
        self.binding=self.fence.abort(self.binding)["binding"]
        self.fence.abort(self.binding)
        self.assertEqual(len(self.boundary.stops),1)

    def test_reserved_id_invalid_digest_revision_and_duration(self):
        for key,value in (("operation_id","active"),("old_sha","wrong"),("controller_digest","truthy"),
                          ("paused_revision",True),("expires_at",float("inf"))):
            req=request(); req["operation_id"]="op2"; req[key]=value
            with self.subTest(key=key), self.assertRaises((FenceDenied,ValueError)):
                self.fence.arm(req)

    def test_dangling_active_reservation_fails_closed_after_crash(self):
        self.binding=self.fence.abort(self.binding)["binding"]
        self.fence.submit(self.binding,"restore"); self.finish("restore")
        next_request=request(); next_request["operation_id"]="op2"
        with patch.object(self.fence,"_save",side_effect=OSError("crash before operation record")):
            with self.assertRaises(OSError): self.fence.arm(next_request)
        third=request(); third["operation_id"]="op3"
        with self.assertRaises(FenceDenied): self.fence.arm(third)
        self.assertEqual(json.loads((self.path/"active.json").read_text())["operation_id"],"op2")

    def test_actual_restore_revision_change_has_bound_post_observation(self):
        # Fresh operation with an immutable expected post-restore policy revision.
        temp_path=self.path.parent/"second"
        req=request(); req["stages"][-1]["post_observation"]["paused_revision"]=4
        fence=ReleaseWindow(temp_path,lambda:self.observed,self.boundary,lambda:self.now)
        b=fence.arm(req); b=fence.abort(b)["binding"]
        fence.submit(b,"restore")
        self.observed["binding"]["paused_revision"]=4
        self.boundary.finish(self.boundary.submits[-1])
        result=fence.reconcile(b)
        self.assertEqual(result["phase"],"closed")

    def test_pending_job_and_replaced_invocation_denied(self):
        self.fence.submit(self.binding,"sync")
        intent=self.boundary.submits[0]
        for change in ({"job_id":42},{"invocation_id":"9"*32}):
            worker=unit(intent,"worker")
            worker=UnitProof(**(worker.__dict__|change))
            self.boundary.proofs["sync"]=StageProof(digest(intent),unit(intent,"sender"),worker)
            with self.subTest(change=change), self.assertRaises(FenceDenied): self.fence.reconcile(self.binding)

    def test_lost_sender_receipt_cannot_adopt_visible_invocation(self):
        self.boundary.dispatch=Mock(side_effect=ConnectionError("lost response"))
        self.fence.submit(self.binding,"sync")
        self.boundary.finish(self.boundary.submits[0])
        self.boundary.accepted_launch=Mock(side_effect=FenceDenied("no durable accepted receipt"))
        with self.assertRaises(FenceDenied): self.fence.reconcile(self.binding)


class SystemdReadbackTests(unittest.TestCase):
    def test_missing_unit_denied(self):
        adapter=SystemdReadbackBoundary(run=Mock(return_value=Mock(returncode=0,stdout="LoadState=not-found\n")))
        with self.assertRaises(FenceDenied): adapter._unit("wbc-release-"+"1"*24+"-worker.service")
        self.assertFalse(adapter.ready)

    def test_systemctl_duration_and_pending_job_formats(self):
        self.assertEqual(SystemdReadbackBoundary._duration_usec("1min 30s"),90_000_000)
        self.assertEqual(SystemdReadbackBoundary._duration_usec("30s 250ms"),30_250_000)
        with self.assertRaises(FenceDenied): SystemdReadbackBoundary._duration_usec("infinity")
        self.assertEqual(SystemdReadbackBoundary._job_id("(0, /)"),0)
        self.assertEqual(SystemdReadbackBoundary._job_id("42 /org/freedesktop/systemd1/job/42"),42)
        with self.assertRaises(FenceDenied): SystemdReadbackBoundary._job_id("")

    def test_real_recursive_cgroup_files_not_empty_boolean(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve()
            (root/"cgroup.controllers").write_text("cpu memory\n")
            group=root/"system.slice"/"fixture.service"; group.mkdir(parents=True)
            (group/"cgroup.events").write_text("populated 0\nfrozen 0\n")
            (group/"cgroup.procs").write_text("")
            child=group/"grandchild"; child.mkdir()
            (child/"cgroup.events").write_text("populated 1\nfrozen 0\n")
            (child/"cgroup.procs").write_text("512\n")
            adapter=SystemdReadbackBoundary(root)
            groups=adapter._groups("/system.slice/fixture.service")
            self.assertEqual(len(groups),2)
            proof=UnitProof("fixture", "1"*32,"/system.slice/fixture.service","active","exited","success",0,0,1,0,
                            "control-group",30_000_000,groups)
            with self.assertRaises(FenceDenied): proof.terminal("fixture",30,"1"*32)
            (child/"cgroup.events").write_text("populated 0\n")
            (child/"cgroup.procs").write_text("")
            proof=UnitProof(**(proof.__dict__|{"groups":adapter._groups("/system.slice/fixture.service")}))
            self.assertTrue(proof.terminal("fixture",30,"1"*32))


if __name__ == "__main__":
    unittest.main()
