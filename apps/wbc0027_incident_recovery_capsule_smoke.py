#!/usr/bin/env python3
"""End-to-end query-only qualification and single-transaction capsule smoke."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_incident_recovery_capsule as module  # noqa: E402
from apps import wbc0027_incident_capsule_workflow as workflow_module  # noqa: E402
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
    FfPoolFbsForwardRecoveryMutation,
    _active_manifest,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    process_post_t_fbs_lifecycle,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    ensure_inventory_history_schema,
)


def main() -> int:
    with TemporaryDirectory(prefix="wbc0027-incident-capsule-") as raw:
        root = Path(raw)
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
            source_cursor_max = int(
                conn.execute(
                    "SELECT MAX(source_status_observation_sequence) "
                    f"FROM {IDENTITY_PENDING_TABLE}"
                ).fetchone()[0]
            )
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
