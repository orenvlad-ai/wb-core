#!/usr/bin/env python3
"""Failure-injected recovery checks for the Stage E bootstrap entrypoint.

The test uses only temporary SQLite/runtime files.  It deliberately calls the
same ``stage_e.execute`` entrypoint that the production adapter invokes and
never creates a WB source.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps import search_cluster_cleaner_stage_e as stage_e
from apps.search_cluster_cleaner_write_fixture import PROFILE
from packages.application.search_cluster_cleaner import KeywordCleaner
from packages.application.search_cluster_cleaner_store import CleanerStore
from packages.application.storage_registry import _implicit_manifest, manifest_payload
from packages.contracts.search_cluster_cleaner import Account, CleanerError, digest


RUNTIME_SHA = "b" * 40
ACCOUNT = Account("seller", "scope")
GENERATION = "monolith"
ORIGINAL = "stage-e-bootstrap-original-0001"


@contextlib.contextmanager
def replaced(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


class Sandbox:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="stage-e-recovery-")
        self.root = Path(self._temporary.name)
        self.app = self.root / "app"
        self.runtime = self.root / "runtime"
        self.admission = self.root / "admission"
        self.env = self.root / "server.env"
        self.package_path = self.admission / "approved-baseline-v1.json"
        self._old_root = stage_e.ROOT
        self._old_env = {key: os.environ.get(key) for key in self.env_keys}

    env_keys = (
        "SELLER_PORTAL_CANONICAL_SUPPLIER_ID",
        "CHANGE_REGISTRY_ACCOUNT_SCOPE",
        "CLEANER_BOOTSTRAP_PACKAGE_PATH",
    )

    def __enter__(self):
        self.app.mkdir()
        self.runtime.mkdir()
        self.admission.mkdir(mode=0o700)
        (self.app / ".wb-core-runtime-sha").write_text(RUNTIME_SHA)
        (self.app / ".wb-core-deploy.json").write_text(
            json.dumps({"commit": RUNTIME_SHA, "deployment_complete": True})
        )
        (self.runtime / "registry_upload_runtime.sqlite3").touch()
        (self.runtime / "storage_generation_manifest.json").write_text(
            json.dumps(manifest_payload(_implicit_manifest()))
        )
        evidence = {
            "cards": [
                {
                    "nm_id": 101,
                    "state": "verified",
                    "current_card_sha256": "sha256:" + "1" * 64,
                    "verified_at": "2026-09-23T00:00:00Z",
                }
            ]
        }
        evidence_bytes = json.dumps(evidence, sort_keys=True).encode()
        evidence_path = self.admission / "current-card-evidence.json"
        evidence_path.write_bytes(evidence_bytes)
        os.chmod(evidence_path, 0o600)
        rows = [
            {
                "advert_id": 1,
                "nm_id": 101,
                "query": "baseline",
                "decision": "allow",
                "observed_state": "active",
                "provenance": {"synthetic": True},
            }
        ]
        self.package = {
            "schema": "search_cluster_cleaner_approved_baseline/v1",
            "seller_id": "seller",
            "account_scope": "scope",
            "generation": GENERATION,
            "owner_username": "owner",
            "rows": rows,
            "profiles": [PROFILE],
            "provenance": {"synthetic": True},
            "source_sha256": "sha256:" + "0" * 64,
            "rows_digest": "sha256:" + digest(rows),
            "card_evidence_sha256": "sha256:" + hashlib.sha256(evidence_bytes).hexdigest(),
            "manual_admission": [
                {
                    "advert_id": 11,
                    "nm_id": 101,
                    "card_digest": "sha256:" + "1" * 64,
                    "verified_at": "2026-09-23T00:00:00Z",
                    "state": "verified",
                }
            ],
        }
        self.write_package()
        self.env.write_text(
            "SELLER_PORTAL_CANONICAL_SUPPLIER_ID=seller\n"
            "CHANGE_REGISTRY_ACCOUNT_SCOPE=scope\n"
            f"CLEANER_BOOTSTRAP_PACKAGE_PATH={self.package_path}\n"
        )
        stage_e.ROOT = self.app
        return self

    def __exit__(self, *_):
        stage_e.ROOT = self._old_root
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._temporary.cleanup()

    def write_package(self) -> None:
        self.package_path.write_text(json.dumps(self.package, sort_keys=True))
        os.chmod(self.package_path, 0o600)

    def envelope(self, action, operation_id=ORIGINAL, request=None, **extra):
        return {
            "action": action,
            "operation_id": operation_id,
            "request": request or {"mode": "bootstrap"},
            "expected_runtime_sha": RUNTIME_SHA,
            **extra,
        }

    def execute(self, action, operation_id=ORIGINAL, request=None, **extra):
        return stage_e.execute(
            self.envelope(action, operation_id, request, **extra),
            runtime_dir=self.runtime,
            env_file=self.env,
            admission_dir=self.admission,
        )

    def service(self):
        return KeywordCleaner(CleanerStore(stage_e.StoreRegistry(self.runtime)), ACCOUNT, owner_username="owner")

    def schema(self):
        return stage_e._schema_state(self.service())

    def journal(self):
        return stage_e._bootstrap_journal(self.admission)


def expect_error(code, call):
    try:
        call()
    except CleanerError as error:
        assert error.code == code, error.code
    else:
        raise AssertionError(f"expected {code}")


def assert_no_cleaner_change(box, foreign_before):
    schema = box.schema()
    assert schema["cleaner"] == []
    assert schema["foreign_schema_sha256"] == foreign_before


def bootstrap_preview(box):
    return box.execute("preview")


def bootstrap_apply(box, preview):
    return box.execute(
        "apply",
        expected_prestate=preview["prestate_sha256"],
        expected_candidate=preview["candidate_sha256"],
    )


def recover_preview(box, operation_id):
    request = {"mode": "bootstrap_recover", "original_operation_id": ORIGINAL}
    result = box.execute("preview", operation_id, request)
    assert result["operation_id"] == operation_id
    assert result["recovery"] == {
        "kind": "scoped_bootstrap_recovery",
        "original_operation_id": ORIGINAL,
        "journal_phase": box.journal()["phase"],
    }
    return result


def recover_apply(box, operation_id, preview):
    request = {"mode": "bootstrap_recover", "original_operation_id": ORIGINAL}
    result = box.execute(
        "apply",
        operation_id,
        request,
        expected_prestate=preview["prestate_sha256"],
        expected_candidate=preview["candidate_sha256"],
    )
    assert result == {"operation_id": operation_id, "disposition": "submitted"}
    readback = box.execute("readback", operation_id, request)
    assert readback == {"operation_id": operation_id, "state": "applied"}
    journal = box.journal()
    assert journal["operation_id"] == ORIGINAL
    assert journal["recovery_operation_id"] == operation_id
    assert journal["phase"] == "applied"
    guard = stage_e.AdmissionGuard(box.admission, box.service().store)
    with guard._lock():
        state = guard._load()
    assert state["hold"] and not state["seals"] and not state.get("owner")


def test_cas_and_package_rejection_before_ddl():
    with Sandbox() as box:
        foreign_before = box.schema()["foreign_schema_sha256"]
        preview = bootstrap_preview(box)
        expect_error(
            "bootstrap_preview_drift",
            lambda: box.execute(
                "apply",
                expected_prestate="sha256:" + "f" * 64,
                expected_candidate=preview["candidate_sha256"],
            ),
        )
        assert_no_cleaner_change(box, foreign_before)
        assert box.journal() == {}
    print("recovery case CAS drift and invalid package before DDL: ok")
    for field, value in (("query", None), ("duplicate", True)):
        with Sandbox() as box:
            foreign_before = box.schema()["foreign_schema_sha256"]
            if field == "query":
                box.package["rows"][0]["query"] = value
            else:
                box.package["rows"].append(dict(box.package["rows"][0]))
            box.package["rows_digest"] = "sha256:" + digest(box.package["rows"])
            box.write_package()
            expect_error("approved_package_invalid", lambda: bootstrap_preview(box))
            assert_no_cleaner_change(box, foreign_before)
            assert box.journal() == {}


def test_claim_before_recovery_mutation():
    with Sandbox() as box:
        foreign_before = box.schema()["foreign_schema_sha256"]
        original_initialize = stage_e.AdmissionGuard.initialize_held

        def crash_after_journal(self, **kwargs):
            raise CleanerError("injected_after_journal", "synthetic", 409)

        with replaced(stage_e.AdmissionGuard, "initialize_held", crash_after_journal):
            expect_error("injected_after_journal", lambda: bootstrap_apply(box, bootstrap_preview(box)))
        assert_no_cleaner_change(box, foreign_before)
        assert box.journal()["phase"] == "started"

        recovery_id = "stage-e-recovery-claim-0001"
        preview = recover_preview(box, recovery_id)
        saw_claim = {"value": False}

        def claim_then_crash(self, **kwargs):
            journal = box.journal()
            assert journal["operation_id"] == ORIGINAL
            assert journal["recovery_operation_id"] == recovery_id
            saw_claim["value"] = True
            raise CleanerError("injected_after_claim", "synthetic", 409)

        with replaced(stage_e.AdmissionGuard, "initialize_held", claim_then_crash):
            expect_error(
                "injected_after_claim",
                lambda: box.execute(
                    "apply",
                    recovery_id,
                    {"mode": "bootstrap_recover", "original_operation_id": ORIGINAL},
                    expected_prestate=preview["prestate_sha256"],
                    expected_candidate=preview["candidate_sha256"],
                ),
            )
        assert saw_claim["value"]
        assert_no_cleaner_change(box, foreign_before)
        assert box.journal()["phase"] == "started"
        assert box.journal()["recovery_operation_id"] == recovery_id
        # An incomplete service-bootstrap claim is not a WB submit.  Its
        # readback must establish the incomplete state before a fresh governed
        # recovery operation may take the same original journal forward.
        request = {"mode": "bootstrap_recover", "original_operation_id": ORIGINAL}
        assert box.execute("readback", recovery_id, request) == {
            "operation_id": recovery_id,
            "state": "not_submitted",
        }
        assert_no_cleaner_change(box, foreign_before)
        other_id = "stage-e-recovery-other-0001"
        other_preview = recover_preview(box, other_id)
        recover_apply(box, other_id, other_preview)
    print("recovery case claim then R1 readback and R2 recovery: ok")


def test_foreign_schema_change_rejected():
    with Sandbox() as box:
        foreign_before = box.schema()["foreign_schema_sha256"]

        def crash_after_journal(self, **kwargs):
            raise CleanerError("injected_after_journal", "synthetic", 409)

        with replaced(stage_e.AdmissionGuard, "initialize_held", crash_after_journal):
            expect_error("injected_after_journal", lambda: bootstrap_apply(box, bootstrap_preview(box)))
        with box.service().store.registry.session("operational", mode="rw", operation="recovery_test") as conn:
            conn.execute("CREATE TABLE foreign_recovery_test(value INTEGER)")
            conn.commit()
        foreign_after_fault = box.schema()["foreign_schema_sha256"]
        assert foreign_after_fault != foreign_before
        recovery_id = "stage-e-recovery-foreign-0001"
        preview = recover_preview(box, recovery_id)
        expect_error(
            "bootstrap_journal_conflict",
            lambda: box.execute(
                "apply",
                recovery_id,
                {"mode": "bootstrap_recover", "original_operation_id": ORIGINAL},
                expected_prestate=preview["prestate_sha256"],
                expected_candidate=preview["candidate_sha256"],
            ),
        )
        assert_no_cleaner_change(box, foreign_after_fault)
        assert box.journal()["phase"] == "started"
    print("recovery case foreign schema change rejected: ok")


def test_live_bootstrap_lock_blocks_recovery():
    with Sandbox() as box:
        foreign_before = box.schema()["foreign_schema_sha256"]

        def crash_after_journal(self, **kwargs):
            raise CleanerError("injected_after_journal", "synthetic", 409)

        with replaced(stage_e.AdmissionGuard, "initialize_held", crash_after_journal):
            expect_error("injected_after_journal", lambda: bootstrap_apply(box, bootstrap_preview(box)))
        recovery_id = "stage-e-recovery-lock-0001"
        preview = recover_preview(box, recovery_id)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl,sys,time; f=open(sys.argv[1],'a+b'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(20)",
                str(box.admission / "bootstrap.lock"),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout and holder.stdout.readline().strip() == "locked"
            expect_error(
                "bootstrap_recovery_busy",
                lambda: box.execute(
                    "apply",
                    recovery_id,
                    {"mode": "bootstrap_recover", "original_operation_id": ORIGINAL},
                    expected_prestate=preview["prestate_sha256"],
                    expected_candidate=preview["candidate_sha256"],
                ),
            )
        finally:
            holder.terminate()
            holder.wait(timeout=5)
        assert_no_cleaner_change(box, foreign_before)
        assert box.journal().get("recovery_operation_id") is None
    print("recovery case active bootstrap flock blocked: ok")


def test_crash_windows_recover_with_original_journal():
    cases = []

    def mid_schema(box):
        def crash(self, *args, **kwargs):
            # A process can die after the first DDL statement.  This is
            # intentionally a proper subset of the canonical namespace,
            # rather than a completed initialize path.
            with self.store.registry.session("operational", mode="rw", operation="recovery_test") as conn:
                conn.execute("CREATE TABLE cleaner_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)")
                conn.execute("INSERT INTO cleaner_schema VALUES(1,1)")
                conn.commit()
            raise CleanerError("injected_mid_schema", "synthetic", 409)

        return replaced(stage_e.KeywordCleaner, "initialize", crash), "injected_mid_schema", "started"

    def after_initialize(box):
        def crash(self, *args, **kwargs):
            raise CleanerError("injected_after_initialize", "synthetic", 409)

        return replaced(stage_e.KeywordCleaner, "import_baseline", crash), "injected_after_initialize", "initialized"

    def after_import(box):
        original = stage_e.KeywordCleaner.import_baseline

        def crash(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise CleanerError("injected_after_import", "synthetic", 409)

        return replaced(stage_e.KeywordCleaner, "import_baseline", crash), "injected_after_import", "initialized"

    def config_before_receipt(box):
        original = stage_e._write_stage_config

        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise CleanerError("injected_before_receipt", "synthetic", 409)

        return replaced(stage_e, "_write_stage_config", crash), "injected_before_receipt", "imported"

    cases.extend(
        (
            ("mid-schema", mid_schema),
            ("after-initialize", after_initialize),
            ("after-import", after_import),
            ("config-before-receipt", config_before_receipt),
        )
    )
    for index, (name, injected) in enumerate(cases, 1):
        with Sandbox() as box:
            foreign_before = box.schema()["foreign_schema_sha256"]
            patch, code, phase = injected(box)
            with patch:
                expect_error(code, lambda: bootstrap_apply(box, bootstrap_preview(box)))
            journal = box.journal()
            assert journal["operation_id"] == ORIGINAL
            assert journal["generation"] == GENERATION
            assert journal["package_digest"] == "sha256:" + digest(box.package)
            assert journal["phase"] == phase
            assert box.schema()["foreign_schema_sha256"] == foreign_before, name
            recovery_id = f"stage-e-recovery-window-{index:04d}"
            recover_apply(box, recovery_id, recover_preview(box, recovery_id))
            print(f"recovery case {name}: ok")


def main():
    test_cas_and_package_rejection_before_ddl()
    test_claim_before_recovery_mutation()
    test_foreign_schema_change_rejected()
    test_live_bootstrap_lock_blocks_recovery()
    test_crash_windows_recover_with_original_journal()
    print("search cluster cleaner Stage E recovery smoke: ok")


if __name__ == "__main__":
    main()
