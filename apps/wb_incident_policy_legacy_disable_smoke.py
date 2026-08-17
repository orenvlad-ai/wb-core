#!/usr/bin/env python3
"""Contract smoke for the guarded append-only incident-policy retirement."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_incident_policy_legacy_disable import (  # noqa: E402
    EFFECTIVE_FROM,
    INVARIANT_TABLES,
    POLICY_REASON,
    POLICY_SOURCE,
    _file_digest,
    apply_reviewed_plan,
    build_plan,
    readback,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


DEPLOYED_SHA = "a" * 40
CREATED_AT = "2026-08-17T08:00:00Z"


def main() -> int:
    with TemporaryDirectory(prefix="wb-incident-legacy-disable-") as raw:
        root = Path(raw)
        runtime_dir = root / "runtime"
        runtime_dir.mkdir()
        deployed_root = root / "deployed"
        deployed_root.mkdir()
        (deployed_root / ".wb-core-runtime-sha").write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        (deployed_root / ".wb-core-deploy.json").write_text(
            json.dumps(
                {
                    "commit": DEPLOYED_SHA,
                    "deployment_complete": True,
                    "deployed_at": "2026-08-17T07:30:00Z",
                }
            ),
            encoding="utf-8",
        )
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.load_latest_wb_incident_policy(seller_id="canonical")
        source = runtime.append_wb_incident_policy_revision(
            seller_id="canonical",
            active=True,
            warehouse_ids=[507, 117986],
            warehouse_identities=[
                {"warehouse_id": 507, "warehouse_name": "Коледино"},
                {"warehouse_id": 117986, "warehouse_name": "Электросталь"},
            ],
            warehouse_entries=[
                {
                    "warehouse_id": 507,
                    "warehouse_name": "Коледино",
                    "effective_from": "2026-07-01",
                    "effective_to_exclusive": "",
                    "source": "incident_policy_v2",
                },
                {
                    "warehouse_id": 117986,
                    "warehouse_name": "Электросталь",
                    "effective_from": "2026-07-12",
                    "effective_to_exclusive": "",
                    "source": "incident_policy_v2",
                },
            ],
            reason="Historical incident",
            effective_from="2026-07-01",
            effective_to="",
            policy_status="active",
            actor="legacy-owner",
            created_at="2026-07-01T08:00:00Z",
            source="incident_policy_v2",
            legacy_payloads=[{"user_key": "legacy-browser", "revision": 3}],
        )
        if source.get("status") != "ok" or source.get("revision") != 1:
            raise AssertionError(f"source policy setup failed: {source}")
        before_rows = _policy_rows(runtime.db_path)

        plan = build_plan(
            runtime_dir=runtime_dir,
            seller_id="canonical",
            actor="owner-approved-legacy-disable",
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_root=deployed_root,
            now=datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc),
        )
        target = plan["target_revision"]
        if (
            plan["mode"] != "query_only_dry_run"
            or plan["expected_affected_records"] != 1
            or target["active"] != 0
            or target["effective_from"] != EFFECTIVE_FROM
            or target["source"] != POLICY_SOURCE
            or target["reason"] != POLICY_REASON
        ):
            raise AssertionError(f"dry-run target is not exact: {plan}")
        for json_column in (
            "warehouse_ids_json",
            "warehouse_identities_json",
            "warehouse_entries_json",
            "legacy_payloads_json",
        ):
            if target[json_column] != before_rows[0][json_column]:
                raise AssertionError(f"historical payload changed in {json_column}")
        if set(plan["pre_change"]["non_target_tables"]) != set(INVARIANT_TABLES):
            raise AssertionError("dry-run omitted protected tables")

        manifest_path = root / "reviewed-manifest.json"
        manifest_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_digest = _file_digest(manifest_path)
        evidence_dir = root / "evidence"
        result = apply_reviewed_plan(
            runtime_dir=runtime_dir,
            manifest_path=manifest_path,
            manifest_file_digest=manifest_digest,
            expected_deployed_sha=DEPLOYED_SHA,
            approval_reference="github-comment:apply-gate-123",
            approval_digest="sha256:" + "b" * 64,
            evidence_dir=evidence_dir,
            deployed_root=deployed_root,
        )
        if (
            result["status"] != "reconciled"
            or result["appended_revision"] != 2
            or result["policy_active"]
            or not result["prior_history_preserved"]
            or not result["non_target_invariants_preserved"]
            or result["incident_rematerialization_invoked"]
        ):
            raise AssertionError(f"apply reconciliation failed: {result}")
        if not Path(result["backup"]["path"]).is_file():
            raise AssertionError("coherent backup was not retained")

        after_rows = _policy_rows(runtime.db_path)
        if len(after_rows) != 2 or after_rows[0] != before_rows[0]:
            raise AssertionError("the original policy revision was rewritten")
        appended = after_rows[1]
        if (
            appended["revision"] != 2
            or appended["active"] != 0
            or appended["effective_from"] != EFFECTIVE_FROM
            or appended["source"] != POLICY_SOURCE
        ):
            raise AssertionError(f"unexpected appended revision: {appended}")

        terminal = readback(
            runtime_dir=runtime_dir,
            seller_id="canonical",
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_root=deployed_root,
        )
        if terminal["status"] != "readback_ok" or terminal["active"]:
            raise AssertionError(f"terminal readback failed: {terminal}")

        repeated = apply_reviewed_plan(
            runtime_dir=runtime_dir,
            manifest_path=manifest_path,
            manifest_file_digest=manifest_digest,
            expected_deployed_sha=DEPLOYED_SHA,
            approval_reference="github-comment:apply-gate-123",
            approval_digest="sha256:" + "b" * 64,
            evidence_dir=evidence_dir,
            deployed_root=deployed_root,
        )
        if repeated.get("status") != "already_applied" or repeated.get("database_written"):
            raise AssertionError(f"repeat apply must be T0: {repeated}")
        if len(_policy_rows(runtime.db_path)) != 2:
            raise AssertionError("repeat apply appended a duplicate revision")

        source_text = (ROOT / "apps/wb_incident_policy_legacy_disable.py").read_text(encoding="utf-8")
        forbidden = (
            "save_policy_revision(",
            "rematerialize_incident",
            "save_wb_incident_projection_cache(",
            "UPDATE sheet_vitrina_v1_ready_snapshots",
            "DELETE FROM sheet_vitrina_v1_wb_incident_policy_revisions",
        )
        leaked = [item for item in forbidden if item in source_text]
        if leaked:
            raise AssertionError(f"runner crossed the no-rematerialization boundary: {leaked}")

    print("wb_incident_policy_legacy_disable_smoke: OK")
    return 0


def _policy_rows(db_path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_incident_policy_revisions ORDER BY revision"
            ).fetchall()
        ]


if __name__ == "__main__":
    raise SystemExit(main())
