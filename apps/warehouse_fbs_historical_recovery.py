#!/usr/bin/env python3
"""Inert owner-gated adapter for one bounded historical FBS recovery."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_dense_fbs import _write_private  # noqa: E402
from apps.registry_upload_http_entrypoint_hosted_runtime import (  # noqa: E402
    ACTIVE_HOSTED_RUNTIME_TARGET_ID,
    load_hosted_runtime_target,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.storage_registry import (  # noqa: E402
    StoreRegistry,
    manifest_payload,
)
from packages.application.warehouse_fbs_material_rematerialization import (  # noqa: E402
    HISTORICAL_MANIFEST_SCHEMA,
    WarehouseFbsMaterialRematerializer,
)


def run(args: argparse.Namespace) -> int:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    target = load_hosted_runtime_target(Path(args.target_file).expanduser().resolve())
    target_runtime_dir = (
        Path(str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""))
        .expanduser()
        .resolve()
    )
    if not (
        target.target_status == "active"
        and target.target_id == ACTIVE_HOSTED_RUNTIME_TARGET_ID
        and target.target_role == "primary_live"
        and target.target_lifecycle == "current_live"
        and target_runtime_dir == runtime_dir
    ):
        raise ValueError("target file does not pin the active primary hosted runtime")
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
    generation = registry.load(require_files=True)
    if generation.implicit:
        raise ValueError("an explicit StoreRegistry generation is required")
    db_path = registry.resolve("operational", manifest=generation)
    query_only = True
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir=runtime_dir,
        operational_db_path=db_path,
        store_registry=registry,
    )
    service = WarehouseFbsMaterialRematerializer(
        runtime=runtime,
        timestamp_factory=lambda: (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
    )
    runtime_binding = {
        "canonical_target": {
            "accepted": True,
            "target_id": target.target_id,
            "target_status": target.target_status,
            "target_role": target.target_role,
            "target_lifecycle": target.target_lifecycle,
            "runtime_dir": str(runtime_dir),
            "deployed_sha": deployed_sha,
        },
        "storage_generation": {
            "implicit": bool(generation.implicit),
            "query_only": query_only,
            "manifest_sha256": generation.manifest_sha256,
            "state": generation.state,
            "canonical_source": generation.canonical_source,
            "generation_epoch": generation.generation_epoch,
            "operational_generation_id": generation.operational.generation_id,
            "operational_schema_revision": generation.operational.schema_revision,
            "operational_relative_path": generation.operational.relative_path,
            "manifest_fingerprint": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    manifest_payload(generation),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        },
    }
    if args.action == "plan":
        if args.manifest_file is None or args.output is None:
            raise ValueError("historical plan requires --manifest-file and --output")
        manifest = json.loads(Path(args.manifest_file).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("--manifest-file must contain one JSON object")
        _validate_domain_manifest(manifest)
        manifest.update(runtime_binding)
        with registry.session(
            "operational",
            mode="ro",
            operation="warehouse_fbs_historical_recovery_plan",
            manifest=generation,
        ) as dependency_conn:
            if not bool(
                int(dependency_conn.execute("PRAGMA query_only").fetchone()[0])
            ):
                raise ValueError("historical dependency session is not query-only")
            result = service.build_historical_plan(
                manifest,
                dependency_conn=dependency_conn,
            )
        written = _write_private(
            Path(args.output),
            result,
            owner="warehouse_fbs_historical_recovery_plan",
        )
        if not bool(written.get("written")):
            raise RuntimeError("private historical plan output was not admitted")
        public = {
            "contract_name": result.get("contract_name"),
            "status": result.get("status"),
            "reason": result.get("reason", ""),
            "operation_id": result.get("operation_id", ""),
            "plan_fingerprint": result.get("plan_fingerprint", ""),
            "bounds": result.get("bounds", {}),
            "private_output": written,
        }
    elif args.action == "apply":
        if args.plan_file is None:
            raise ValueError("historical apply requires --plan-file")
        plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
        reviewed_binding = dict(plan.get("historical_manifest") or {})
        if any(
            dict(reviewed_binding.get(key) or {}) != value
            for key, value in runtime_binding.items()
        ):
            raise ValueError(
                "reviewed plan target or StoreRegistry generation is no longer current"
            )
        public = service.apply_plan(
            plan,
            confirm_fingerprint=str(args.confirm_fingerprint),
            approval_reference=str(args.approval_reference),
            actor=str(args.actor),
        )
    else:
        if not str(args.operation_id or "").strip():
            raise ValueError("historical readback requires --operation-id")
        public = service.readback(operation_id=str(args.operation_id))
    print(json.dumps(public, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if public.get("status") not in {"unsafe_ambiguous", "not_found"} else 2


def _validate_domain_manifest(manifest: dict[str, object]) -> None:
    required = {
        "schema",
        "business_date",
        "facility_id",
        "pool",
        "nm_ids",
        "accepted_version_id",
        "accepted_version_plan_digest",
        "accepted_version_row_digest",
        "accepted_target_row_digest",
        "accepted_provenance_digest",
        "accepted_effective_at",
        "accepted_published_at",
        "expected_current_active_version_id",
        "expected_current_sync_version_id",
        "expected_current_pool_digest",
        "event_id",
        "event_source_digest",
        "event_status_digest",
        "event_evidence_digest",
        "event_row_digest",
        "event_quantity_delta",
        "event_capital_delta_rub",
        "event_wac_rub",
        "event_occurred_at",
        "accepted_quantity",
        "accepted_cost_covered_quantity",
        "accepted_capital_rub",
    }
    if set(manifest) != required:
        raise ValueError(
            "historical manifest fields must be exact; "
            f"missing={sorted(required - set(manifest))} "
            f"extra={sorted(set(manifest) - required)}"
        )
    if manifest.get("schema") != HISTORICAL_MANIFEST_SCHEMA:
        raise ValueError(
            f"historical manifest schema must be {HISTORICAL_MANIFEST_SCHEMA}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan, apply or query one manifest-bound historical FBS recovery"
    )
    parser.add_argument("action", choices=("plan", "apply", "readback"))
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--manifest-file", type=Path)
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--confirm-fingerprint", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--operation-id", default="")
    try:
        return run(parser.parse_args())
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
