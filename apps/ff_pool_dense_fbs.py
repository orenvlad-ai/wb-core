#!/usr/bin/env python3
"""Owner-gated manifest adapter for bounded dense-FBS zero repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_dense_fbs import (  # noqa: E402
    DenseFbsService,
    ZERO_REPAIR_MANIFEST_SCHEMA,
)
from packages.application.root_storage_policy import (  # noqa: E402
    RootStoragePolicyError,
    admit_root_write,
)
from packages.application.storage_registry import (  # noqa: E402
    StoreRegistry,
    manifest_payload,
)
from apps.registry_upload_http_entrypoint_hosted_runtime import (  # noqa: E402
    ACTIVE_HOSTED_RUNTIME_TARGET_ID,
    load_hosted_runtime_target,
)


def run(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    target_file = Path(args.target_file).expanduser().resolve()
    target = load_hosted_runtime_target(target_file)
    target_runtime_dir = (
        Path(str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""))
        .expanduser()
        .resolve()
    )
    accepted_target = bool(
        target.target_status == "active"
        and target.target_id == ACTIVE_HOSTED_RUNTIME_TARGET_ID
        and target.target_role == "primary_live"
        and target.target_lifecycle == "current_live"
        and target_runtime_dir == runtime_dir
    )
    if not accepted_target:
        raise ValueError(
            "--target-file does not pin the exact active primary hosted runtime"
        )
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", deployed_sha) is None:
        raise ValueError("--deployed-sha must be one exact 40-hex SHA")
    markers = (
        runtime_dir / ".wb-core-runtime-sha",
        runtime_dir.parent / "app" / ".wb-core-runtime-sha",
    )
    actual_shas = {
        marker.read_text(encoding="utf-8").strip().lower()
        for marker in markers
        if marker.is_file()
    }
    if actual_shas != {deployed_sha}:
        raise ValueError("canonical deployed SHA marker differs from --deployed-sha")
    registry = StoreRegistry(runtime_dir)
    manifest = registry.load(require_files=True)
    if manifest.implicit:
        raise ValueError(
            "implicit legacy monolith resolution is forbidden; persist an exact StoreRegistry manifest"
        )
    db_path = registry.resolve("operational", manifest=manifest)
    with registry.session(
        "operational",
        mode="ro",
        operation="ff_pool_dense_fbs_zero_repair_plan",
        manifest=manifest,
    ) as conn:
        query_only = bool(int(conn.execute("PRAGMA query_only").fetchone()[0]))
    canonical_target = {
        "accepted": accepted_target,
        "target_id": target.target_id,
        "target_status": target.target_status,
        "target_role": target.target_role,
        "target_lifecycle": target.target_lifecycle,
        "runtime_dir": str(runtime_dir),
        "target_file_sha256": _file_sha256(target_file),
        "deployed_sha": deployed_sha,
    }
    storage_generation = {
        "implicit": bool(manifest.implicit),
        "query_only": query_only,
        "manifest_sha256": manifest.manifest_sha256,
        "state": manifest.state,
        "canonical_source": manifest.canonical_source,
        "generation_epoch": manifest.generation_epoch,
        "operational_generation_id": manifest.operational.generation_id,
        "operational_schema_revision": manifest.operational.schema_revision,
        "operational_relative_path": manifest.operational.relative_path,
        "manifest_fingerprint": "sha256:"
        + hashlib.sha256(
            json.dumps(
                manifest_payload(manifest),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    service = DenseFbsService(
        db_path=db_path,
        runtime_dir=runtime_dir,
    )
    action = str(getattr(args, "action", "plan") or "plan")
    if action == "readback":
        if not str(getattr(args, "operation_id", "") or "").strip():
            raise ValueError("readback requires --operation-id")
        result = service.readback_zero_repair(operation_id=str(args.operation_id))
    elif action == "apply":
        if getattr(args, "plan_file", None) is None:
            raise ValueError("apply requires --plan-file")
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        if (
            dict(plan.get("canonical_target") or {}) != canonical_target
            or dict(plan.get("storage_generation") or {}) != storage_generation
        ):
            raise ValueError(
                "reviewed plan target or StoreRegistry generation is no longer current"
            )
        result = service.apply_zero_repair_plan(
            plan,
            confirm_fingerprint=str(args.confirm_fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
        )
    else:
        if getattr(args, "manifest_file", None) is None:
            raise ValueError("plan requires --manifest-file")
        domain_manifest = json.loads(
            Path(args.manifest_file).read_text(encoding="utf-8")
        )
        if not isinstance(domain_manifest, dict):
            raise ValueError("--manifest-file must contain one JSON object")
        domain_manifest = _strict_domain_manifest_v3(domain_manifest)
        result = service.build_zero_repair_plan(
            facility_id=str(domain_manifest["facility_id"]),
            seller_warehouse_id=int(domain_manifest["seller_warehouse_id"]),
            official_office_id=int(domain_manifest["official_office_id"]),
            expected_roster_nm_ids=list(domain_manifest["expected_roster_nm_ids"]),
            expected_existing_nm_ids=list(domain_manifest["expected_existing_nm_ids"]),
            owner_approved_missing_nm_ids=list(
                domain_manifest["owner_approved_missing_nm_ids"]
            ),
            canonical_target=canonical_target,
            storage_generation=storage_generation,
        )
    if args.output:
        output = _write_private(Path(args.output), result)
        if not output["written"]:
            print(
                json.dumps(output, ensure_ascii=False, sort_keys=True), file=sys.stderr
            )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if action != "plan" or result.get("apply_allowed") else 2


def _write_private(
    path: Path,
    payload: dict[str, object],
    *,
    admission_factory: object = admit_root_write,
    owner: str = "ff_pool_dense_fbs_plan",
) -> dict[str, object]:
    output = path.resolve()
    rendered = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode(
            "utf-8"
        )
        + b"\n"
    )
    try:
        admission = admission_factory(
            owner=str(owner),
            destination=output,
            predicted_output_bytes=len(rendered),
        )
    except RootStoragePolicyError as exc:
        return {
            "written": False,
            "mode": "stdout_only",
            "reason": "root_storage_admission_unavailable",
            "error": str(exc),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(rendered.decode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return {
        "written": True,
        "mode": "private_file",
        "path": str(output),
        "file_mode": "0600",
        "root_storage_admission": admission,
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _strict_domain_manifest_v3(payload: dict[str, object]) -> dict[str, object]:
    required = {
        "schema",
        "facility_id",
        "seller_warehouse_id",
        "official_office_id",
        "owner_approved_missing_nm_ids",
        "expected_roster_nm_ids",
        "expected_existing_nm_ids",
    }
    actual = set(payload)
    if actual != required:
        raise ValueError(
            "zero-repair manifest fields must be exact; "
            f"missing={sorted(required - actual)} extra={sorted(actual - required)}"
        )
    if payload.get("schema") != ZERO_REPAIR_MANIFEST_SCHEMA:
        raise ValueError(
            f"zero-repair manifest schema must be {ZERO_REPAIR_MANIFEST_SCHEMA}"
        )
    for field in (
        "owner_approved_missing_nm_ids",
        "expected_roster_nm_ids",
        "expected_existing_nm_ids",
    ):
        values = payload[field]
        if not isinstance(values, list):
            raise ValueError(f"{field} must be one JSON array")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError(f"{field} must contain positive integer nmIds")
        if len(values) != len(set(values)):
            raise ValueError(f"{field} must not contain duplicate nmIds")
    targets = set(payload["owner_approved_missing_nm_ids"])
    existing = set(payload["expected_existing_nm_ids"])
    roster = set(payload["expected_roster_nm_ids"])
    if targets & existing:
        raise ValueError(
            "zero-repair target and existing-row identities must be disjoint"
        )
    if targets | existing != roster:
        raise ValueError("zero-repair current identities must exactly cover the roster")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build, apply or query one exact owner-gated dense-FBS zero repair."
        )
    )
    parser.add_argument("action", choices=("plan", "apply", "readback"))
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--confirm-fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--output", default="")
    try:
        return run(parser.parse_args())
    except (OSError, RuntimeError, ValueError) as exc:
        print(
            json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
