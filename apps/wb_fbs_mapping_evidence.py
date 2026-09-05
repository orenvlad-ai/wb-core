#!/usr/bin/env python3
"""Server-side entrypoint for one exact FBS mapping evidence version."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_fbs_warehouse_registry import _load_env_file  # noqa: E402
from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource  # noqa: E402
from packages.application.storage_registry import StoreRegistry  # noqa: E402
from packages.application.wb_fbs_mapping_evidence import (  # noqa: E402
    WbFbsMappingEvidenceError,
    WbFbsMappingEvidenceUpgrade,
)


def execute(
    envelope: Mapping[str, Any], *, runtime_dir: Path, env_file: Path
) -> dict[str, Any]:
    if not isinstance(envelope, Mapping):
        raise WbFbsMappingEvidenceError(
            "envelope_invalid", "Production apply envelope must be an object"
        )
    allowed = {
        "action",
        "operation_id",
        "request",
        "expected_prestate",
        "expected_candidate",
        "expected_runtime_sha",
        "actor",
    }
    if set(envelope) - allowed:
        raise WbFbsMappingEvidenceError(
            "envelope_fields_invalid", "Production apply envelope has unknown fields"
        )
    action = str(envelope.get("action") or "").strip()
    if action not in {"preview", "apply", "readback"}:
        raise WbFbsMappingEvidenceError(
            "action_invalid", "Production apply action is invalid"
        )
    expected_runtime_sha = str(envelope.get("expected_runtime_sha") or "").strip()
    if re.fullmatch(r"[0-9a-f]{40}", expected_runtime_sha) is None:
        raise WbFbsMappingEvidenceError(
            "expected_runtime_sha_invalid", "Expected runtime SHA is invalid"
        )
    marker = ROOT / ".wb-core-runtime-sha"
    deployed_runtime_sha = marker.read_text(encoding="utf-8").strip()
    if deployed_runtime_sha != expected_runtime_sha:
        raise WbFbsMappingEvidenceError(
            "deployed_runtime_sha_mismatch",
            "Deployed runtime SHA differs from the trusted apply checkout",
        )
    request = envelope.get("request")
    if not isinstance(request, Mapping):
        raise WbFbsMappingEvidenceError(
            "request_invalid", "Production apply request must be an object"
        )
    registry = StoreRegistry(Path(runtime_dir).resolve())
    manifest = registry.load(require_files=True)
    generation = registry.generation("operational", manifest=manifest)
    source = None
    if action in {"preview", "apply"}:
        _load_env_file(Path(env_file).resolve())
        source = HttpBackedWbFbsOrdersSource()
    service = WbFbsMappingEvidenceUpgrade(
        db_path=registry.resolve("operational", manifest=manifest),
        storage_identity={
            "generation_id": generation.generation_id,
            "generation_epoch": generation.generation_epoch,
            "manifest_sha256": manifest.manifest_sha256,
        },
        source=source,
        actor=str(envelope.get("actor") or "github-production-apply"),
    )
    operation_id = str(envelope.get("operation_id") or "")
    if action == "preview":
        return service.preview(request, operation_id)
    if action == "readback":
        return service.readback(request, operation_id)
    return service.apply(
        request,
        operation_id,
        expected_prestate=str(envelope.get("expected_prestate") or ""),
        expected_candidate=str(envelope.get("expected_candidate") or ""),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        envelope = json.loads(sys.stdin.read())
        result = execute(
            envelope, runtime_dir=args.runtime_dir, env_file=args.env_file
        )
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, WbFbsMappingEvidenceError)
            else type(exc).__name__
        )
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": {
                        "code": str(code),
                        "message": " ".join(str(exc).split())[:500],
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
