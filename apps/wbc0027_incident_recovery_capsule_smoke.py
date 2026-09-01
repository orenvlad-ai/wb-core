#!/usr/bin/env python3
"""End-to-end query-only qualification and single-transaction capsule smoke."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_incident_recovery_capsule as module  # noqa: E402
from apps import wbc0027_incident_capsule_workflow as workflow_module  # noqa: E402
from apps import wbc0027_incident_capsule_target as target_module  # noqa: E402
from apps.fbs_lifecycle_manifests_smoke import (  # noqa: E402
    _Clock,
    _add_later_canonical_identity,
    _insert_custom_order,
    _passport,
    _seed_history_with_four_unsupported_cells,
    _seed_mapping_owner,
    _seed_second_facility_and_balances,
)
from apps.ff_pool_fbs_forward_recovery_smoke import (  # noqa: E402
    SHA,
    _RecoveryClock,
    _insert_backlog,
    _prepared_runtime,
)
from apps.ff_pool_fbs_lifecycle_smoke import _insert_post_t_order  # noqa: E402
from packages.application.fbs_lifecycle_manifests import read_json  # noqa: E402
from packages.application.ff_pool_fbs_forward_recovery import (  # noqa: E402
    FfPoolFbsForwardRecoveryError,
    FfPoolFbsForwardRecoveryMutation,
    _active_manifest,
    _finalize_preview_projection_foreign_keys,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    process_post_t_fbs_lifecycle,
)
from packages.application.wb_fbs_orders import STATUS_OBSERVATIONS_TABLE  # noqa: E402
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    ensure_inventory_history_schema,
)


def _assert_hosted_transport_contract(root: Path) -> None:
    binding = workflow_module.materialize_ssh_transport(
        target_file=target_module.DEFAULT_TARGET_FILE,
        output_directory=root / "capsule-ssh",
        private_key="synthetic-private-key",
        known_hosts="89.191.226.88 ssh-ed25519 c3ludGhldGljLWtleQ==",
    )
    assert binding["target_id"] == module.CANONICAL_TARGET_ID
    assert binding["host_name"] == "89.191.226.88"
    assert binding["user"] == "root"
    assert binding["ssh_host_alias"] != binding["source_ssh_destination"]
    assert {
        Path(binding["ssh_config"]).stat().st_mode & 0o777,
        Path(binding["identity_file"]).stat().st_mode & 0o777,
        Path(binding["known_hosts_file"]).stat().st_mode & 0o777,
    } == {0o600}
    resolved = subprocess.run(
        [
            "ssh",
            "-G",
            "-F",
            str(binding["ssh_config"]),
            str(binding["ssh_host_alias"]),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(root / "empty-home")},
    ).stdout.splitlines()
    assert "hostname 89.191.226.88" in resolved
    assert "user root" in resolved
    assert f"hostkeyalias {binding['host_name']}" in resolved
    assert f"userknownhostsfile {binding['known_hosts_file']}" in resolved

    canonical = json.loads(target_module.DEFAULT_TARGET_FILE.read_text(encoding="utf-8"))
    for label, host in (("missing", ""), ("foreign", "203.0.113.10")):
        invalid = {**canonical, "host_ip": host}
        invalid_path = root / f"target-{label}.json"
        invalid_path.write_text(json.dumps(invalid), encoding="utf-8")
        blocked = False
        try:
            workflow_module.materialize_ssh_transport(
                target_file=invalid_path,
                output_directory=root / f"capsule-ssh-{label}",
                private_key="synthetic-private-key",
                known_hosts="89.191.226.88 ssh-ed25519 c3ludGhldGljLWtleQ==",
            )
        except workflow_module.ApplyError as exc:
            blocked = f"host_ip is {label}" in str(exc)
        assert blocked, label

    workflow = (ROOT / ".github/workflows/wbc0027-incident-capsule.yml").read_text(
        encoding="utf-8"
    )
    assert "materialize-ssh" in workflow
    assert "ssh -F \"$SSH_CONFIG\"" in workflow
    assert "wb-core-eu-root" not in workflow


def _insert_history_component(conn: sqlite3.Connection, *, capture_id: str) -> None:
    conn.execute(
        f"""INSERT INTO {COMPONENTS_TABLE}(
               capture_id,scope_kind,scope_key,nm_id,component_kind,component_id,
               component_label,state,quantity,source_revision,source_digest,
               source_watermark,provenance_json,captured_at
           ) VALUES(?, 'TOTAL', 'TOTAL', NULL, 'WB', 'WB', 'WB', 'missing', NULL,
                    '', '', '', '{{}}', '2026-08-17T00:00:00Z')""",
        (capture_id,),
    )


def _insert_history_capture(conn: sqlite3.Connection, *, capture_id: str) -> None:
    conn.execute(
        f"""INSERT INTO {CAPTURES_TABLE}(
               capture_id,business_date,capture_kind,formula_version,bundle_version,
               ready_snapshot_id,ready_plan_version,generation_identity,
               facility_roster_revision,facility_roster_json,source_manifest_json,
               source_digest,captured_at
           ) VALUES(?, '2026-08-17', 'historical_backfill', 'smoke', '', '', '', '',
                    'smoke', '[]', '{{}}', 'sha256:smoke', '2026-08-17T00:00:00Z')""",
        (capture_id,),
    )


def _assert_deferred_foreign_key_contract(root: Path) -> None:
    success = sqlite3.connect(root / "history-projection-success.sqlite3")
    try:
        success.execute("PRAGMA foreign_keys=OFF")
        ensure_inventory_history_schema(success)
        _insert_history_component(success, capture_id="capture-success")
        _insert_history_capture(success, capture_id="capture-success")
        success.commit()
        _finalize_preview_projection_foreign_keys(success)
        assert int(success.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        assert success.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        success.close()

    failure = sqlite3.connect(root / "history-projection-failure.sqlite3")
    try:
        failure.execute("PRAGMA foreign_keys=OFF")
        ensure_inventory_history_schema(failure)
        _insert_history_component(failure, capture_id="capture-missing")
        failure.commit()
        blocked = False
        try:
            _finalize_preview_projection_foreign_keys(failure)
        except FfPoolFbsForwardRecoveryError as exc:
            blocked = (
                exc.code == "preview_projection_foreign_key_drift"
                and dict(exc.details or {}) == {"violation_count": 1}
            )
        assert blocked
    finally:
        failure.close()


def _seed_superseded_history_dependency(conn: sqlite3.Connection) -> None:
    base = conn.execute(
        f"SELECT * FROM {CAPTURES_TABLE} WHERE business_date='2026-08-17' "
        "ORDER BY capture_sequence DESC LIMIT 1"
    ).fetchone()
    assert base is not None
    components = [
        module.recovery_module._stored_component(row)
        for row in conn.execute(
            f"""SELECT scope_kind,scope_key,nm_id,component_kind,component_id,
                       component_label,state,quantity,source_revision,source_digest,
                       source_watermark,provenance_json
                FROM {COMPONENTS_TABLE} WHERE capture_id=?
                ORDER BY scope_kind,scope_key,component_kind,component_id""",
            (str(base[1]),),
        ).fetchall()
    ]
    later = append_inventory_history_capture(
        conn,
        business_date="2026-08-17",
        capture_kind="historical_backfill",
        formula_version=str(base[4]),
        facility_roster=json.loads(str(base[10])),
        source_manifest={"contract": "synthetic_exact_same_date_v2", "date": "2026-08-17"},
        components=components,
        captured_at="2026-08-17T22:00:00Z",
    )
    append_inventory_history_finalization(
        conn,
        business_date="2026-08-17",
        capture_id=str(later["capture_id"]),
        finalization_identity="synthetic-v2:2026-08-17",
        finalized_at="2026-08-17T23:00:00Z",
        provenance={"source": "capsule-supersession-smoke"},
    )


def main() -> int:
    with TemporaryDirectory(prefix="wbc0027-incident-capsule-") as raw:
        root = Path(raw)
        _assert_hosted_transport_contract(root)
        _assert_deferred_foreign_key_contract(root)
        runtime = _prepared_runtime(root / "runtime")
        _insert_backlog(runtime.db_path)
        forward = FfPoolFbsForwardRecoveryMutation(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            timestamp_factory=_RecoveryClock(),
        )
        forward_plan = forward.build_plan()
        forward.apply(
            forward_plan,
            fingerprint=str(forward_plan["fingerprint"]),
            approval_reference="synthetic-forward-gate",
            actor="smoke",
            evidence_dir=root / "forward-evidence",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_ff_pool_fbs_lifecycle_schema(conn)
            ensure_inventory_history_schema(conn)
            _seed_second_facility_and_balances(conn)
            for order_id, nm_id, chrt_id, sku, barcode, hour in (
                (9701, 998, 1998, "seller-998", "sku-998", 1),
                (9702, 997, 1997, "seller-997", "sku-997", 2),
            ):
                _insert_post_t_order(
                    conn,
                    order_id=order_id,
                    supplier="new",
                    wb="waiting",
                    source_created_at=f"2026-08-17T{hour:02d}:00:00Z",
                    observed_at=f"2026-08-17T{hour:02d}:01:00Z",
                    identity_outcome="unmatched_identity",
                    source_nm_id=nm_id,
                    source_chrt_id=chrt_id,
                    seller_sku=sku,
                    barcode=barcode,
                )
            _insert_custom_order(
                conn,
                order_id=9703,
                warehouse_id=502,
                source_nm_id=996,
                source_chrt_id=1996,
                seller_sku="seller-996",
                barcode="sku-996",
                source_created_at="2026-08-17T03:00:00Z",
                observed_at="2026-08-17T03:01:00Z",
            )
            _insert_post_t_order(
                conn,
                order_id=9704,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T03:10:00Z",
                observed_at="2026-08-17T03:11:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=996,
                source_chrt_id=1996,
                seller_sku="seller-996",
                barcode="sku-996",
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            processed = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-17T04:00:00Z",
                limit=100,
                schema_ready=True,
            )
            conn.commit()
            assert processed["summary"]["identity_pending"] == 4
            _add_later_canonical_identity(conn)
            _seed_mapping_owner(conn)
            _seed_history_with_four_unsupported_cells(conn)
            _seed_superseded_history_dependency(conn)
            source_cursor_max = int(
                conn.execute(
                    "SELECT MAX(source_status_observation_sequence) "
                    f"FROM {IDENTITY_PENDING_TABLE}"
                ).fetchone()[0]
            )
            conn.execute(
                f"""INSERT INTO {STATUS_OBSERVATIONS_TABLE}(
                       observation_id,order_id,order_revision,status_digest,
                       supplier_status,wb_status,positive_quantity,observed_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "status-cursor-witness",
                    9701,
                    "cursor-witness-revision",
                    "sha256:" + "c" * 64,
                    "new",
                    "waiting",
                    1,
                    "2026-08-17T04:01:00Z",
                ),
            )
            assert int(
                conn.execute(
                    f"SELECT MAX(observation_sequence) FROM {STATUS_OBSERVATIONS_TABLE}"
                ).fetchone()[0]
            ) > source_cursor_max
            active = _active_manifest(conn)
            conn.commit()
        passport = _passport(
            runtime=runtime,
            active=active,
            source_cursor_max=source_cursor_max,
        )
        release = {
            "contract": module.RELEASE_BINDING_CONTRACT,
            "repository": "orenvlad-ai/wb-core",
            "pull_request": 9999,
            "release_operation_id": "release-v2-" + "1" * 32,
            "release_kind": "live_runtime",
            "state": "done",
            "base_sha": "2" * 40,
            "head_sha": "3" * 40,
            "merge_sha": SHA,
            "deployed_sha": SHA,
            "plan_hash": "4" * 64,
            "gate_workflow_run_id": 123456,
            "release_receipt_digest": "sha256:" + "5" * 64,
        }
        capsule = module.Wbc0027IncidentRecoveryCapsule(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            incident_passport=passport,
            release_binding=release,
            timestamp_factory=_Clock(),
        )
        evidence = (
            runtime.runtime_dir
            / "backups/private-evidence/production-goals/synthetic-incident-capsule"
        )
        evidence.mkdir(parents=True, mode=0o700)
        operation = "synthetic-incident-capsule"
        qualification = capsule.qualification(
            operation_id=operation,
            evidence_dir=evidence,
        )
        assert qualification["state"] == "HUMAN_REQUIRED"
        assert qualification["production_mutation_submit_count"] == 0
        manifest = read_json(Path(qualification["manifest_path"]))
        qualified = read_json(Path(qualification["qualification_path"]))
        assert manifest["expected_writes"]["logical"]["mapping_insert_count"] == 1
        assert manifest["expected_writes"]["logical"]["target_status_count"] == 5
        assert manifest["expected_writes"]["simulation"] == {
            "contract": "wbc0027_incident_capsule_full_projection_simulation/v1",
            "production_source_open_mode": "ro",
            "production_source_query_only": True,
            "production_source_write_count": 0,
            "scratch_projection": "full_forward_and_history_dependencies",
            "scratch_foreign_keys_during_projection": "disabled",
            "scratch_foreign_keys_after_projection": "enabled",
            "scratch_foreign_key_check": "zero_violations",
            "final_apply_foreign_keys": "enabled",
        }
        assert manifest["history"]["classification_counts"][
            "remain_missing_no_same_date_evidence"
        ] == 4
        assert {path.stat().st_mode & 0o777 for path in evidence.glob("*.json")} == {0o600}
        wrong_gate = False
        try:
            capsule.apply(
                manifest=manifest,
                qualification=qualified,
                operation_id=operation,
                approval_reference="wrong-gate",
                actor="smoke",
                evidence_dir=evidence,
            )
        except module.CapsuleError as exc:
            wrong_gate = exc.code == "human_gate_binding_mismatch"
        assert wrong_gate
        authorization = module._authorization_body(
            release=release,
            operation_id=operation,
            manifest_digest=str(manifest["manifest_digest"]),
            qualification_digest=str(qualified["qualification_digest"]),
        )
        original_client = workflow_module.GitHubClient
        original_token = os.environ.get("GITHUB_TOKEN")
        workflow_module.GitHubClient = lambda _repository, _token: type(
            "Client",
            (),
            {
                "get": lambda _self, _path: {
                    "id": 8181,
                    "author_association": "OWNER",
                    "body": authorization,
                    "issue_url": "https://api.github.com/repos/orenvlad-ai/wb-core/issues/9999",
                }
            },
        )()
        os.environ["GITHUB_TOKEN"] = "synthetic-token"
        try:
            validated = workflow_module.validate_authorization(
                repository="orenvlad-ai/wb-core",
                comment_id=8181,
                release_binding=release,
                manifest=manifest,
                qualification=qualified,
            )
            assert validated["body"] == authorization
            assert validated["operation_id"] == operation
        finally:
            workflow_module.GitHubClient = original_client
            if original_token is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = original_token
        applied = capsule.apply(
            manifest=manifest,
            qualification=qualified,
            operation_id=operation,
            approval_reference=authorization,
            actor="smoke",
            evidence_dir=evidence,
        )
        assert applied["state"] == "done"
        assert applied["apply_count"] == 1
        assert applied["sqlite_transaction_count"] == 1
        readback = capsule.readback(
            manifest=manifest,
            operation_id=operation,
            approval_reference=authorization,
        )
        assert readback["state"] == "done", readback["checks"]
        assert all(readback["checks"].values())
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_wb_supplies_fbs_identity_mappings "
                "WHERE source_nm_id=996 AND source_chrt_id=1996"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"""SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} pending
                    LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} resolution
                      ON resolution.pending_id=pending.pending_id
                    WHERE resolution.pending_id IS NULL"""
            ).fetchone()[0] == 0
        replay = capsule.apply(
            manifest=manifest,
            qualification=qualified,
            operation_id=operation,
            approval_reference=authorization,
            actor="smoke",
            evidence_dir=evidence,
        )
        assert replay["already_terminal"] is True
        assert replay["apply_count"] == 0
    print("wbc0027_incident_recovery_capsule_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
