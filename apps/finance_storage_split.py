#!/usr/bin/env python3
"""Repo-owned Finance raw/operational storage split runner.

Dry-run is the default action.  Candidate creation requires explicit ``apply``,
an exact fresh fingerprint and a separate approval reference.  No action in
this runner switches the global generation manifest or canonical readers.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_raw_storage import shadow_compare_week, storage_health
from packages.application.finance_migration_deploy_lease import (
    validate_finance_migration_deploy_lease,
)
from packages.application.finance_storage_migration import (
    FinanceStorageCandidateBuilder,
    FinanceStorageCoherentSnapshot,
    FinanceStorageCutover,
    FinanceStorageMigrationPlanner,
    FinanceStorageRollback,
    FinanceStorageShadowRunner,
    FinanceStorageShadowVerifier,
)
from packages.application.storage_registry import parse_manifest


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        data = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _emit(payload: dict[str, Any], output: Path | None) -> None:
    if output is not None:
        _write_json(output, payload)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _shadow_read(
    *,
    runtime_dir: Path,
    candidate_manifest_path: Path,
    seller_id: str,
) -> dict[str, Any]:
    planner = FinanceStorageMigrationPlanner(runtime_dir, repo_root=ROOT)
    source_path = planner.registry.resolve("operational")
    manifest = parse_manifest(
        json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
    )
    if manifest.state != "shadow" or manifest.canonical_source != "monolith":
        raise ValueError("shadow-read requires an unselected shadow candidate manifest")
    shadow_path = (runtime_dir / manifest.raw.relative_path).resolve()
    shadow_path.relative_to(runtime_dir.resolve())
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True, timeout=60)
    shadow = sqlite3.connect(f"file:{shadow_path}?mode=ro", uri=True, timeout=60)
    source.row_factory = sqlite3.Row
    shadow.row_factory = sqlite3.Row
    try:
        for conn in (source, shadow):
            conn.execute("PRAGMA query_only=ON")
            if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
                raise ValueError("shadow-read query_only could not be enabled")
        weeks = source.execute(
            """SELECT week_start,week_end FROM wb_finance_weekly_sync
               WHERE seller_id=? ORDER BY week_start""",
            (seller_id,),
        ).fetchall()
        comparisons = [
            shadow_compare_week(
                source_conn=source,
                shadow_conn=shadow,
                seller_id=seller_id,
                week_start=str(row["week_start"]),
                week_end=str(row["week_end"]),
            )
            for row in weeks
        ]
    finally:
        source.close()
        shadow.close()
    payload: dict[str, Any] = {
        "contract_version": "wb_core_finance_storage_shadow_read_v1",
        "mode": "query_only_shadow_read",
        "canonical_source": "monolith",
        "global_manifest_switched": False,
        "comparison_count": len(comparisons),
        "mismatch_count": sum(
            1 for item in comparisons if item["status"] != "match"
        ),
        "comparisons": comparisons,
    }
    from hashlib import sha256

    payload["fingerprint"] = "sha256:" + sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        nargs="?",
        choices=(
            "dry-run",
            "apply",
            "health",
            "shadow-read",
            "snapshot-plan",
            "snapshot-create",
            "snapshot-integrity",
            "shadow-status",
            "shadow-activate",
            "shadow-reconcile",
            "shadow-verify",
            "live-tail-apply",
            "shadow-deactivate",
            "cutover-plan",
            "cutover-apply",
            "rollback-plan",
            "rollback-prepare",
            "rollback-apply",
        ),
        default="dry-run",
    )
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--deployed-sha", default="")
    parser.add_argument("--deployed-sha-file", type=Path)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--chunk-size", type=int, default=10_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--fault-after-chunks", type=int, default=0)
    parser.add_argument("--candidate-manifest", type=Path)
    parser.add_argument("--snapshot-plan-file", type=Path)
    parser.add_argument("--cutover-plan-file", type=Path)
    parser.add_argument("--rollback-plan-file", type=Path)
    parser.add_argument("--rollback-candidate-evidence", type=Path)
    parser.add_argument("--source-snapshot-manifest", type=Path)
    parser.add_argument("--candidate-plan-fingerprint", default="")
    parser.add_argument("--seller-id", default="canonical")
    parser.add_argument("--max-events", type=int, default=100_000)
    parser.add_argument(
        "--minimum-observation-seconds",
        type=int,
        default=3600,
    )
    parser.add_argument("--reason", default="")
    parser.add_argument(
        "--deploy-lease-json",
        default="",
        help=(
            "Fresh GitHub Release Train Finance migration deploy-lease "
            "readback. Canonical hosted execution requires it for every "
            "migration phase except health."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    runtime_dir = args.runtime_dir.expanduser().resolve()
    deployed_sha = str(args.deployed_sha or "").strip()
    if args.deployed_sha_file is not None:
        file_sha = args.deployed_sha_file.expanduser().read_text(encoding="utf-8").strip()
        if deployed_sha and deployed_sha != file_sha:
            raise SystemExit("--deployed-sha and --deployed-sha-file disagree")
        deployed_sha = file_sha
    deploy_lease: dict[str, Any] | None = None
    if args.deploy_lease_json:
        try:
            lease_payload = json.loads(args.deploy_lease_json)
        except json.JSONDecodeError as exc:
            raise SystemExit("--deploy-lease-json must contain valid JSON") from exc
        if not isinstance(lease_payload, dict):
            raise SystemExit("--deploy-lease-json must contain a JSON object")
        deploy_lease = validate_finance_migration_deploy_lease(
            lease_payload,
            deployed_sha=deployed_sha,
        )
    if args.action.startswith("rollback-"):
        rollback = FinanceStorageRollback(
            runtime_dir,
            deployed_sha=deployed_sha,
        )
        if args.action == "rollback-plan":
            payload = rollback.build_plan()
        else:
            if args.rollback_plan_file is None:
                raise SystemExit(
                    "--rollback-plan-file is required for rollback mutations"
                )
            reviewed_plan = json.loads(
                args.rollback_plan_file.expanduser().read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(reviewed_plan, dict):
                raise SystemExit(
                    "--rollback-plan-file must contain a JSON object"
                )
            if args.action == "rollback-prepare":
                payload = rollback.prepare(
                    reviewed_plan=reviewed_plan,
                    expected_fingerprint=args.confirm_fingerprint,
                    approval_reference=args.approval_reference,
                )
            else:
                if args.rollback_candidate_evidence is None:
                    raise SystemExit(
                        "--rollback-candidate-evidence is required for "
                        "rollback-apply"
                    )
                payload = rollback.apply(
                    reviewed_plan=reviewed_plan,
                    expected_fingerprint=args.confirm_fingerprint,
                    approval_reference=args.approval_reference,
                    candidate_evidence_path=(
                        args.rollback_candidate_evidence.expanduser().resolve()
                    ),
                )
    elif args.action.startswith("cutover-"):
        if args.candidate_manifest is None:
            raise SystemExit(
                "--candidate-manifest is required for cutover actions"
            )
        if not str(args.candidate_plan_fingerprint or "").startswith(
            "sha256:"
        ):
            raise SystemExit(
                "--candidate-plan-fingerprint is required for cutover actions"
            )
        cutover = FinanceStorageCutover(
            runtime_dir,
            candidate_manifest_path=(
                args.candidate_manifest.expanduser().resolve()
            ),
            candidate_plan_fingerprint=args.candidate_plan_fingerprint,
            deployed_sha=deployed_sha,
        )
        if args.action == "cutover-plan":
            payload = cutover.build_plan()
        else:
            if args.cutover_plan_file is None:
                raise SystemExit(
                    "--cutover-plan-file is required for cutover-apply"
                )
            reviewed_plan = json.loads(
                args.cutover_plan_file.expanduser().read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(reviewed_plan, dict):
                raise SystemExit(
                    "--cutover-plan-file must contain a JSON object"
                )
            payload = cutover.apply(
                reviewed_plan=reviewed_plan,
                expected_fingerprint=args.confirm_fingerprint,
                approval_reference=args.approval_reference,
            )
    elif args.action.startswith("shadow-") or args.action == "live-tail-apply":
        if args.candidate_manifest is None:
            raise SystemExit(
                "--candidate-manifest is required for shadow actions"
            )
        if args.action == "shadow-verify":
            payload = FinanceStorageShadowVerifier(
                runtime_dir,
                candidate_manifest_path=(
                    args.candidate_manifest.expanduser().resolve()
                ),
                candidate_plan_fingerprint=args.confirm_fingerprint,
                minimum_observation_seconds=(
                    args.minimum_observation_seconds
                ),
            ).verify()
            _emit(payload, args.output)
            return 0
        shadow = FinanceStorageShadowRunner(
            runtime_dir,
            candidate_manifest_path=(
                args.candidate_manifest.expanduser().resolve()
            ),
            plan_fingerprint=args.confirm_fingerprint,
            approval_reference=args.approval_reference,
        )
        if args.action == "shadow-status":
            payload = shadow.status()
        elif args.action == "shadow-activate":
            payload = shadow.activate()
        elif args.action == "shadow-reconcile":
            payload = shadow.reconcile_legacy_current(
                chunk_size=args.chunk_size
            )
        elif args.action == "live-tail-apply":
            payload = shadow.apply_live_tail(
                max_events=args.max_events
            )
        else:
            payload = shadow.deactivate(
                reason=args.reason or "shadow lifecycle completed"
            )
    elif args.action == "snapshot-plan":
        payload = FinanceStorageCoherentSnapshot(
            runtime_dir,
            deployed_sha=deployed_sha,
            repo_root=args.repo_root,
        ).build_plan()
    elif args.action == "snapshot-create":
        if args.snapshot_plan_file is None:
            raise SystemExit(
                "--snapshot-plan-file is required for snapshot-create"
            )
        reviewed_plan = json.loads(
            args.snapshot_plan_file.expanduser().read_text(encoding="utf-8")
        )
        if not isinstance(reviewed_plan, dict):
            raise SystemExit("--snapshot-plan-file must contain a JSON object")
        payload = FinanceStorageCoherentSnapshot(
            runtime_dir,
            deployed_sha=deployed_sha,
            repo_root=args.repo_root,
        ).create(
            reviewed_plan=reviewed_plan,
            expected_fingerprint=args.confirm_fingerprint,
            approval_reference=args.approval_reference,
        )
    elif args.action == "snapshot-integrity":
        if args.source_snapshot_manifest is None:
            raise SystemExit(
                "--source-snapshot-manifest is required for snapshot-integrity"
            )
        payload = FinanceStorageCoherentSnapshot(
            runtime_dir,
            deployed_sha=deployed_sha,
            repo_root=args.repo_root,
        ).verify_integrity(
            args.source_snapshot_manifest.expanduser().resolve()
        )
    elif args.action == "health":
        payload = storage_health(
            FinanceStorageMigrationPlanner(
                runtime_dir,
                chunk_size=args.chunk_size,
                deployed_sha=deployed_sha,
                repo_root=args.repo_root,
            ).registry
        )
    elif args.action == "shadow-read":
        if args.candidate_manifest is None:
            raise SystemExit("--candidate-manifest is required for shadow-read")
        payload = _shadow_read(
            runtime_dir=runtime_dir,
            candidate_manifest_path=args.candidate_manifest.expanduser().resolve(),
            seller_id=str(args.seller_id or "canonical"),
        )
    else:
        planner = FinanceStorageMigrationPlanner(
            runtime_dir,
            chunk_size=args.chunk_size,
            deployed_sha=deployed_sha,
            repo_root=args.repo_root,
            source_snapshot_manifest=args.source_snapshot_manifest,
        )
        if args.action == "apply":
            payload = FinanceStorageCandidateBuilder(
                planner,
                expected_fingerprint=args.confirm_fingerprint,
                approval_reference=args.approval_reference,
                fault_after_chunks=args.fault_after_chunks,
            ).apply()
        else:
            payload = planner.build_plan()
    if deploy_lease is not None:
        payload["deploy_lease"] = {
            "contract_version": deploy_lease["contract_version"],
            "policy": deploy_lease["policy"],
            "lease": deploy_lease["lease"],
            "fingerprint": deploy_lease["fingerprint"],
        }
    _emit(payload, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
