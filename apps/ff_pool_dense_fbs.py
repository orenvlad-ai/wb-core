#!/usr/bin/env python3
"""Query-only future Orenburg repair plan over the general dense FBS service."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_dense_fbs import DenseFbsService  # noqa: E402
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


ORENBURG_FACILITY_ID = "fff_2579bb2741ed4ab23b11bb4c4183"
ORENBURG_TARGET_NM_IDS = (
    259466031,
    391660889,
    391661710,
    391662410,
    391662965,
    391663632,
    428849827,
    428854502,
    497413772,
    497415593,
    497416559,
    497416931,
)
ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS = 21
ORENBURG_EXPECTED_STOCK_MANAGED_ROSTER = 33
ORENBURG_SELLER_WAREHOUSE_ID = 854205
ORENBURG_OFFICIAL_OFFICE_ID = 12223
ORENBURG_HISTORICAL_ZERO_DATE = "2026-08-24"


def run(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    target_file = Path(args.target_file).expanduser().resolve()
    target = load_hosted_runtime_target(target_file)
    target_runtime_dir = Path(
        str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or "")
    ).expanduser().resolve()
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
        operation="ff_pool_dense_fbs_orenburg_plan",
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
    plan = DenseFbsService(
        db_path=db_path,
        runtime_dir=runtime_dir,
    ).build_zero_repair_plan(
        facility_id=ORENBURG_FACILITY_ID,
        nm_ids=ORENBURG_TARGET_NM_IDS,
        seller_warehouse_id=ORENBURG_SELLER_WAREHOUSE_ID,
        official_office_id=ORENBURG_OFFICIAL_OFFICE_ID,
        expected_roster_count=ORENBURG_EXPECTED_STOCK_MANAGED_ROSTER,
        expected_existing_non_target_count=ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS,
        historical_business_date=ORENBURG_HISTORICAL_ZERO_DATE,
        canonical_target=canonical_target,
        storage_generation=storage_generation,
    )
    if args.output:
        output = _write_private(Path(args.output), plan)
        if not output["written"]:
            print(json.dumps(output, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if plan.get("apply_allowed") else 2


def _write_private(
    path: Path,
    payload: dict[str, object],
    *,
    admission_factory: object = admit_root_write,
) -> dict[str, object]:
    output = path.resolve()
    rendered = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    try:
        admission = admission_factory(
            owner="ff_pool_dense_fbs_plan",
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the exact query-only future Orenburg dense-FBS zero repair plan; "
            "this command exposes no apply path."
        )
    )
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--runtime-dir", required=True)
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
