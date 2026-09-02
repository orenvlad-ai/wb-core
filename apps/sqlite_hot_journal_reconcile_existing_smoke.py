#!/usr/bin/env python3
"""Production-shaped smoke for marker-only implicit rollback reconciliation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance
import apps.sqlite_hot_journal_reconcile_existing as reconcile
import apps.sqlite_hot_journal_recovery as hot
import apps.storage_recovery_sanitation_job as jobs


OLD_SHA = hot.EXPECTED_SOURCE_EPOCH_SHA
NEW_SHA = "b" * 40
WINDOW = hot.EXPECTED_WINDOW_ID
PLAN_FP = "sha256:" + "c" * 64
STATE_FP = "sha256:" + "d" * 64


def _database(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE change_registry_observer_jobs(
            job_id TEXT PRIMARY KEY, seller_id TEXT, account_scope TEXT,
            trigger_kind TEXT, scheduled_slot TEXT, requested_by TEXT,
            requested_at TEXT, request_digest TEXT
        );
        CREATE TABLE change_registry_observer_job_events(
            job_event_id TEXT PRIMARY KEY, job_id TEXT, sequence_no INTEGER,
            state TEXT, occurred_at TEXT, checkpoint_id TEXT, fact_count INTEGER
        );
        CREATE TABLE change_registry_checkpoints(
            checkpoint_id TEXT PRIMARY KEY, seller_id TEXT, account_scope TEXT,
            source_surface TEXT, scan_kind TEXT, started_at TEXT,
            completed_at TEXT, completeness_status TEXT,
            expected_target_count INTEGER, observed_target_count INTEGER,
            completeness_digest TEXT, evidence_digest TEXT,
            previous_complete_checkpoint_id TEXT, mapping_version TEXT
        );
        CREATE TABLE change_registry_checkpoint_source_manifests(
            source_manifest_id TEXT PRIMARY KEY, checkpoint_id TEXT,
            source_name TEXT, completeness_status TEXT, expected_count INTEGER,
            observed_count INTEGER, summary_json TEXT, evidence_digest TEXT,
            created_at TEXT
        );
        CREATE TABLE sheet_vitrina_v1_source_health_status(
            source_key TEXT PRIMARY KEY, payload_json TEXT, checked_at TEXT
        );
        CREATE TABLE business_projection(
            row_id TEXT PRIMARY KEY, payload BLOB, nullable TEXT
        );
        """
    )
    shas = (reconcile.EXPECTED_FIRST_ACTIVATION_SHA, NEW_SHA)
    for index, sha in enumerate(shas, 1):
        job_id = reconcile.ACTIVATION_JOB_PREFIX + sha
        checkpoint_id = f"checkpoint-{index}"
        connection.execute(
            "INSERT INTO change_registry_observer_jobs VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id, "seller", "scope", "activation", "", "release",
                f"2026-09-02T04:0{index + 4}:00Z", "sha256:" + str(index) * 64,
            ),
        )
        for sequence, state in enumerate(("accepted", "running", "complete"), 1):
            connection.execute(
                "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?)",
                (
                    f"event-{index}-{sequence}", job_id, sequence, state,
                    f"2026-09-02T04:0{index + 4}:0{sequence}Z",
                    checkpoint_id if state == "complete" else None,
                    0,
                ),
            )
        connection.execute(
            "INSERT INTO change_registry_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                checkpoint_id, "seller", "scope", "wb_api", "observer",
                f"2026-09-02T04:0{index + 4}:02Z",
                f"2026-09-02T04:0{index + 4}:03Z", "complete", 0, 0,
                "sha256:" + "b" * 64, "sha256:" + "a" * 64,
                None, "fixture-v1",
            ),
        )
        for source in ("prices", "ads"):
            connection.execute(
                "INSERT INTO change_registry_checkpoint_source_manifests "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"manifest-{index}-{source}", checkpoint_id, source,
                    "complete", 0, 0, "{}", "sha256:" + "e" * 64,
                    f"2026-09-02T04:0{index + 4}:03Z",
                ),
            )
    connection.execute(
        "INSERT INTO sheet_vitrina_v1_source_health_status VALUES(?,?,?)",
        (
            "seller_portal_auth",
            json.dumps({"session_status": "valid"}, sort_keys=True),
            "2026-09-02T04:06:48Z",
        ),
    )
    connection.execute(
        "INSERT INTO business_projection VALUES(?,?,?)",
        ("stable", sqlite3.Binary(b"\x00\xff"), None),
    )
    connection.commit()
    connection.close()
    return path


def _digest_and_allowed_writer_smoke(root: Path) -> dict[str, object]:
    database = _database(root / "operational.sqlite3")
    before_sha = hot.file_sha256(database)
    first = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert first["sqlite_readback"]["integrity_check"] == "ok"
    assert first["sqlite_readback"]["foreign_key_violation_count"] == 0
    assert first["non_operational"]["table_row_counts"]["business_projection"] == 1
    assert first["operational_rows"]["activation_deployed_shas"] == sorted(
        [reconcile.EXPECTED_FIRST_ACTIVATION_SHA, NEW_SHA]
    )

    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE sheet_vitrina_v1_source_health_status "
        "SET payload_json=?,checked_at=? WHERE source_key=?",
        (
            json.dumps({"session_status": "valid", "probe": 2}, sort_keys=True),
            "2026-09-02T04:07:48Z",
            "seller_portal_auth",
        ),
    )
    connection.commit()
    connection.close()
    allowed = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert allowed["non_operational"] == first["non_operational"]
    assert allowed["operational"] != first["operational"]
    assert hot.file_sha256(database) != before_sha

    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO change_registry_observer_job_events VALUES(?,?,?,?,?,?,?)",
        (
            "unexpected-event", "historical-job", 1, "complete",
            "2026-09-02T04:07:59Z", None, 0,
        ),
    )
    connection.commit()
    connection.close()
    try:
        reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    except reconcile.ReconcileExistingError as exc:
        assert "event set" in str(exc)
    else:
        raise AssertionError("unexpected operational row writer must fail closed")
    connection = sqlite3.connect(database)
    connection.execute(
        "DELETE FROM change_registry_observer_job_events "
        "WHERE job_event_id='unexpected-event'"
    )
    connection.execute(
        "INSERT INTO business_projection VALUES(?,?,?)",
        ("unexpected", sqlite3.Binary(b"third-writer"), "material"),
    )
    connection.commit()
    connection.close()
    unexpected = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    assert unexpected["non_operational"] != allowed["non_operational"]
    return {"database": database, "evidence": allowed}


def _marker_only_apply_smoke(root: Path, fixture: dict[str, object]) -> None:
    database = Path(fixture["database"])
    # Restore the one unexpected row before sealing the reviewed plan.
    connection = sqlite3.connect(database)
    connection.execute("DELETE FROM business_projection WHERE row_id='unexpected'")
    connection.commit()
    connection.close()
    evidence = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    runtime = root / "runtime"
    backup = root / "backup"
    runtime.mkdir()
    backup.mkdir()
    operation_id = "1" * 64
    result_dir = backup / "operation"
    plan = {
        "contract_name": reconcile.CONTRACT_NAME,
        "mode": reconcile.MODE,
        "read_only": True,
        "operation_id": operation_id,
        "deployed_sha": NEW_SHA,
        "source_epoch_deployed_sha": OLD_SHA,
        "runtime_dir": str(runtime),
        "barrier": {
            "active": True, "phase": "acquiring", "hold_confirmed": False,
            "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance": {
            "partial_epoch_fingerprint": "sha256:" + "f" * 64,
            "business_operation_counters": {"operations": 0, "submits": 0},
        },
        "business_writer_timeline": {
            "event_count": 0, "digest": "sha256:" + hashlib.sha256(b"").hexdigest(),
        },
        "database": hot._file_identity(database),
        "database_reconciliation": evidence,
        "backup": {
            "directory": str(result_dir),
            "reserve_bytes": 0,
            "evidence_envelope_bytes": 1,
        },
        "created_at": "2026-09-02T05:00:00Z",
    }
    plan["fingerprint"] = reconcile._plan_fingerprint(plan)
    plan_path = root / "reviewed.json"
    plan_path.write_text(reconcile._canonical_json(plan) + "\n", encoding="utf-8")
    plan_sha = "sha256:" + hot.file_sha256(plan_path)

    # An unlisted writer/table after review must fail before evidence or marker writes.
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE unexpected_writer(row_id TEXT PRIMARY KEY)")
    connection.execute("INSERT INTO unexpected_writer VALUES('third-writer')")
    connection.commit()
    connection.close()
    unexpected = json.loads(json.dumps(plan))
    unexpected["database"] = hot._file_identity(database)
    unexpected["database_reconciliation"] = reconcile.database_evidence(
        database, deployed_sha=NEW_SHA
    )
    unexpected["fingerprint"] = reconcile._plan_fingerprint(unexpected)
    with (
        mock.patch.object(reconcile, "DEFAULT_BACKUP_ROOT", backup),
        mock.patch.object(reconcile, "build_plan", return_value=unexpected),
    ):
        try:
            reconcile.apply_plan(
                plan_path=plan_path,
                plan_sha256=plan_sha,
                fingerprint=plan["fingerprint"],
                deployed_sha_file=root / "unused",
            )
        except reconcile.ReconcileExistingError as exc:
            assert "stale" in str(exc)
        else:
            raise AssertionError("unexpected third-table writer must fail closed")
    assert not result_dir.exists()
    assert not (runtime / hot.MARKER_FILENAME).exists()

    connection = sqlite3.connect(database)
    connection.execute("DROP TABLE unexpected_writer")
    connection.commit()
    connection.close()
    evidence = reconcile.database_evidence(database, deployed_sha=NEW_SHA)
    plan["database"] = hot._file_identity(database)
    plan["database_reconciliation"] = evidence
    plan["fingerprint"] = reconcile._plan_fingerprint(plan)
    plan_path.write_text(reconcile._canonical_json(plan) + "\n", encoding="utf-8")
    plan_sha = "sha256:" + hot.file_sha256(plan_path)
    database_before = database.read_bytes()
    with (
        mock.patch.object(reconcile, "DEFAULT_BACKUP_ROOT", backup),
        mock.patch.object(reconcile, "build_plan", return_value=dict(plan)),
    ):
        result = reconcile.apply_plan(
            plan_path=plan_path,
            plan_sha256=plan_sha,
            fingerprint=plan["fingerprint"],
            deployed_sha_file=root / "unused",
        )
    assert result["mode"] == "reconciled_existing"
    assert result["sqlite_write"] is False
    assert result["logical_business_delta"] == 0
    assert database.read_bytes() == database_before
    marker = json.loads((runtime / hot.MARKER_FILENAME).read_text())
    assert marker["non_operational_digest"] == evidence["non_operational"]
    assert marker["operational_digest"] == evidence["operational"]

    stale = json.loads(json.dumps(plan))
    stale["database_reconciliation"]["non_operational"]["digest"] = (
        "sha256:" + "0" * 64
    )
    assert not reconcile._fresh_matches_plan(plan, stale)


def _one_submit_smoke(root: Path) -> None:
    runtime = root / "jobs"
    backups = root / "root-backups"
    runtime.mkdir()
    backups.mkdir()
    sha_file = root / "sha"
    sha_file.write_text(NEW_SHA + "\n", encoding="utf-8")
    plan_path = (
        "/opt/wb-core-runtime/state/private-evidence/production-goals/"
        f"wbc0027-s047-reconcile-existing-plan-{NEW_SHA}.json"
    )
    kwargs = {
        "runtime_dir": runtime,
        "root_backups": backups,
        "deployed_sha_file": sha_file,
        "job_id": "2" * 64,
        "deployed_sha": NEW_SHA,
        "operation": "sqlite-hot-journal-reconcile-existing-apply",
        "root_name": "",
        "family": "",
        "reviewed_plan": plan_path,
        "reviewed_plan_sha256": "sha256:" + "3" * 64,
        "confirm_fingerprint": "sha256:" + "4" * 64,
        "approval_reference": "ROOT exact reconcile callback",
        "starter": lambda job_id: {"name": job_id},
    }
    original = jobs._read_json

    def read(path: Path, *, label: str):
        if label == "hot journal reviewed plan":
            return {
                "contract_name": reconcile.CONTRACT_NAME,
                "operation_id": kwargs["job_id"],
                "deployed_sha": NEW_SHA,
                "fingerprint": kwargs["confirm_fingerprint"],
            }
        return original(path, label=label)

    with (
        mock.patch.object(jobs, "_read_json", side_effect=read),
        mock.patch.object(jobs, "file_sha256", return_value="3" * 64),
    ):
        first = jobs.submit_job(**kwargs)
        second = jobs.submit_job(**kwargs)
        with mock.patch.object(
            reconcile,
            "apply_plan",
            return_value={
                "status": "reconciled_existing",
                "logical_business_delta": 0,
                "sqlite_write": False,
            },
        ) as apply:
            terminal = jobs.run_worker(
                runtime_dir=runtime,
                root_backups=backups,
                deployed_sha_file=sha_file,
                job_id=kwargs["job_id"],
            )
    assert first["unit_start_requested"] is True
    assert second["submit_idempotent"] is True
    assert terminal["status"] == "succeeded"
    apply.assert_called_once_with(
        plan_path=Path(plan_path),
        plan_sha256=kwargs["reviewed_plan_sha256"],
        fingerprint=kwargs["confirm_fingerprint"],
        deployed_sha_file=sha_file,
    )


def _marker_validator_smoke(fixture: dict[str, object]) -> None:
    evidence = reconcile.database_evidence(
        Path(fixture["database"]), deployed_sha=NEW_SHA
    )
    partial = {"deployed_sha": OLD_SHA, "epoch": 2}
    result = {
        "contract_name": maintenance.HOT_JOURNAL_RECOVERY_RESULT_CONTRACT,
        "mode": reconcile.MODE,
        "operation_id": "5" * 64,
        "source_epoch_deployed_sha": OLD_SHA,
        "deployed_sha": NEW_SHA,
        "barrier": {
            "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance_partial_epoch_fingerprint": maintenance._stable_fingerprint(partial),
        "database_after": hot._file_identity(Path(fixture["database"])),
        "journal_absent": True,
        "sqlite_readback": evidence["sqlite_readback"],
        "non_operational_digest": evidence["non_operational"],
        "operational_digest": evidence["operational"],
        "operational_rows": evidence["operational_rows"],
        "business_operation_counters": {"operations": 0},
        "logical_business_delta": 0,
        "sqlite_write": False,
    }
    result["result_fingerprint"] = maintenance._stable_fingerprint(result)
    marker = {
        key: result[key]
        for key in (
            "contract_name", "mode", "operation_id", "source_epoch_deployed_sha",
            "deployed_sha", "barrier", "maintenance_partial_epoch_fingerprint",
            "database_after", "journal_absent", "sqlite_readback",
            "non_operational_digest", "operational_digest",
            "business_operation_counters", "result_fingerprint",
        )
    }
    marker["result_path"] = (
        "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
        "wbc0027-s047-reconcile-existing-bbbbbbbb/" + result["operation_id"]
        + "/result.json"
    )
    marker["marker_fingerprint"] = maintenance._stable_fingerprint(marker)

    def load(path: Path):
        return marker if path.name == hot.MARKER_FILENAME else result

    with (
        mock.patch.object(maintenance, "_load_json_object", side_effect=load),
        mock.patch.object(reconcile.hot, "_file_identity", return_value=result["database_after"]),
        mock.patch.object(reconcile, "database_evidence", return_value=evidence),
        mock.patch.object(
            maintenance, "_prepared_abort_breakglass_counters",
            return_value={"operations": 0},
        ),
        mock.patch.object(Path, "exists", return_value=False),
    ):
        maintenance._validate_hot_journal_recovery_marker(
            Path("/runtime"), partial_epoch=partial, deployed_sha=NEW_SHA,
            barrier=result["barrier"],
        )


def main() -> None:
    assert reconcile.EXPECTED_SEALED_ROLLBACK["nonce_hex"] == "5296552f"
    assert reconcile.EXPECTED_SEALED_ROLLBACK["record_count"] == 169
    assert reconcile.EXPECTED_SEALED_ROLLBACK["checksum_mismatch_count"] == 0
    assert reconcile.EXPECTED_SEALED_ROLLBACK["page_number_list_sha256"] == (
        "8ef27ddebf2d2b12dbf0050bd7719d64a18266f38917aa38a4cf170ec7c8a12e"
    )
    with tempfile.TemporaryDirectory(prefix="wbc0027-reconcile-existing-") as raw:
        root = Path(raw)
        fixture = _digest_and_allowed_writer_smoke(root)
        _marker_only_apply_smoke(root, fixture)
        _one_submit_smoke(root)
        _marker_validator_smoke(fixture)
    print("sqlite_hot_journal_reconcile_existing_smoke: ok")


if __name__ == "__main__":
    main()
