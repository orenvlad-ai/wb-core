#!/usr/bin/env python3
"""Business-safe production canary for the unified warehouse recovery policy."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sqlite_contention import connect_sqlite  # noqa: E402
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryState,
    WarehouseRecoveryRegistry,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm", default="")
    return parser


def plan_fingerprint(deployed_sha: str) -> str:
    material = f"warehouse-recovery-production-canary-v1:{deployed_sha.strip()}"
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def run(
    *,
    runtime_dir: Path,
    deployed_sha: str,
    apply: bool,
    confirm: str,
) -> dict[str, Any]:
    deployed_sha = str(deployed_sha or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_sha):
        raise ValueError("canary requires an exact 40-character deployed SHA")
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir.resolve())
    registry = WarehouseRecoveryRegistry(
        runtime_dir=runtime.runtime_dir,
        db_path=runtime.db_path,
    )
    fingerprint = plan_fingerprint(deployed_sha)
    plan = {
        "contract_name": "warehouse_recovery_policy_production_canary_v1",
        "deployed_sha": deployed_sha,
        "fingerprint": fingerprint,
        "scope": "recovery metadata and domain checkpoint only",
        "business_data_mutation": False,
        "checks": ["T0", "T1", "T2", "non_target_digest", "orphan_scanner"],
    }
    if not apply:
        return {**plan, "status": "dry_run_ready", "would_change": True}
    if confirm != fingerprint:
        raise ValueError("exact canary plan fingerprint is required")

    registry.ensure_schema()
    prior_canary_recovery = registry.release_failed_canary_pre_mutations()
    operation_count_before = _operation_count(runtime.db_path)
    noop = registry.plan_noop(
        mutation_kind="supplier_cost_queue_replay",
        closure_kind="shipment",
        plan_fingerprint=fingerprint + ":noop",
        scope={"canary": True, "deployed_sha": deployed_sha},
    )
    operation_count_after_noop = _operation_count(runtime.db_path)
    if (
        noop["tier"] != "T0"
        or noop["planned_bytes"] != 0
        or noop["actual_bytes"] != 0
        or noop["read_bytes"] != 0
        or operation_count_before != operation_count_after_noop
    ):
        raise RuntimeError("T0 canary created recovery bytes or registry rows")

    marker_id = "bounded-replay-" + deployed_sha[:16]
    marker_after = {
        "marker_id": marker_id,
        "marker_value": "bounded_replay_verified",
        "deployed_sha": deployed_sha,
        "updated_at": deployed_sha,
    }
    bounded_fingerprint = "sha256:" + hashlib.sha256(
        f"{fingerprint}:bounded".encode("utf-8")
    ).hexdigest()
    bounded = registry.prepare_t1(
        mutation_kind="supplier_cost_queue_replay",
        closure_kind="shipment",
        plan_fingerprint=bounded_fingerprint,
        scope={
            "canary": True,
            "deployed_sha": deployed_sha,
            "marker_id": marker_id,
        },
        before_images=[
            {
                "table": "sheet_vitrina_v1_recovery_canary",
                "key": {"marker_id": marker_id},
                "before": None,
                "after": marker_after,
            }
        ],
        expected_after_images=[marker_after],
        source_digest=deployed_sha,
        non_target_digest="canary_metadata_only",
    )
    if bounded["lifecycle"] == RecoveryState.VERIFIED.value:
        bounded = registry.begin_mutation(
            bounded["operation_id"], expected_source_digest=deployed_sha
        )
        with connect_sqlite(runtime.db_path) as conn:
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_recovery_canary(
                    marker_id,marker_value,deployed_sha,updated_at
                ) VALUES(?,?,?,?)
                """,
                tuple(marker_after.values()),
            )
            conn.commit()
        bounded = registry.retain(
            bounded["operation_id"],
            after_digest=_digest(marker_after),
            non_target_digest="canary_metadata_only",
        )
    if bounded["lifecycle"] == RecoveryState.RETAINED.value:
        bounded = registry.rollback_t1(
            bounded["operation_id"],
            reason="production canary proves exact bounded replay",
        )
    if bounded["lifecycle"] != RecoveryState.ROLLED_BACK.value:
        raise RuntimeError("bounded replay canary did not reach rolled_back")
    if _canary_marker(runtime.db_path, marker_id) is not None:
        raise RuntimeError("bounded replay canary left its target marker behind")

    business_before = registry.domain_content_digest()
    business_digest_before = str(business_before["digest"])
    wide_fingerprint = "sha256:" + hashlib.sha256(
        f"{fingerprint}:wide".encode("utf-8")
    ).hexdigest()
    wide = registry.prepare_t2(
        mutation_kind="manual_warehouse_sync",
        plan_fingerprint=wide_fingerprint,
        scope={
            "canary": True,
            "deployed_sha": deployed_sha,
            "publication": "warehouse_domain_checkpoint",
            "business_mutation": False,
        },
        source_digest=business_digest_before,
        non_target_digest=business_digest_before,
        source_watermarks={"deployed_sha": deployed_sha, "canary": True},
        schema_revision="warehouse_recovery_policy_v1",
    )
    if wide["lifecycle"] == RecoveryState.VERIFIED.value:
        wide = registry.begin_mutation(
            wide["operation_id"],
            expected_source_digest=business_digest_before,
            writer_state="canary_verification",
        )
        business_after = registry.domain_content_digest()
        business_digest_after = str(business_after["digest"])
        if business_digest_after != business_digest_before:
            registry.quarantine(wide["operation_id"], "canary_non_target_drift")
            raise RuntimeError("wide canary changed warehouse business tables")
        wide = registry.retain(
            wide["operation_id"],
            after_digest=business_digest_after,
            non_target_digest=business_digest_after,
            timer_state="canary_complete",
        )
    if wide["lifecycle"] != RecoveryState.RETAINED.value:
        raise RuntimeError("wide domain canary did not reach retained")

    orphan = registry.scan_orphans()
    raw_leaks = [
        item["path"]
        for item in orphan["files"]
        if item["kind"] in {"raw", "wal", "shm", "journal", "temp"}
        and not item["managed"]
    ]
    if orphan["status"] != "clean" or raw_leaks:
        raise RuntimeError(
            "production canary left orphan recovery evidence: "
            + json.dumps(
                {
                    "orphan_count": orphan["orphan_count"],
                    "raw_leaks": raw_leaks,
                    "policy_activation_at": orphan["policy_activation_at"],
                    "pre_policy_legacy_count": orphan[
                        "pre_policy_legacy_count"
                    ],
                    "unclassified_paths": orphan["unclassified_paths"],
                },
                sort_keys=True,
            )
        )
    return {
        **plan,
        "status": "complete",
        "would_change": False,
        "prior_canary_recovery": prior_canary_recovery,
        "noop": noop,
        "bounded_replay": bounded,
        "wide_domain_publication": wide,
        "business_digest_before": business_digest_before,
        "business_digest_after": str(registry.domain_content_digest()["digest"]),
        "business_domain_before": business_before,
        "orphan_scanner": {
            "status": orphan["status"],
            "orphan_count": orphan["orphan_count"],
            "raw_leaks": raw_leaks,
            "policy_activation_at": orphan["policy_activation_at"],
            "pre_policy_legacy_count": orphan[
                "pre_policy_legacy_count"
            ],
        },
    }


def _operation_count(db_path: Path) -> int:
    with closing(
        sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro",
            uri=True,
        )
    ) as conn:
        conn.execute("PRAGMA query_only=ON")
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_recovery_operations"
            ).fetchone()[0]
        )


def _canary_marker(db_path: Path, marker_id: str) -> dict[str, Any] | None:
    with closing(
        sqlite3.connect(
            f"file:{db_path.resolve()}?mode=ro",
            uri=True,
        )
    ) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute(
            """
            SELECT * FROM sheet_vitrina_v1_recovery_canary WHERE marker_id=?
            """,
            (marker_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def main() -> int:
    args = build_parser().parse_args()
    result = run(
        runtime_dir=Path(args.runtime_dir),
        deployed_sha=str(args.deployed_sha),
        apply=bool(args.apply),
        confirm=str(args.confirm),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
