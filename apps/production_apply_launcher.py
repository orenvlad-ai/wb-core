#!/usr/bin/env python3
"""Small one-submit launcher for registered production-data adapters."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping

from apps.production_apply_adapters import ADAPTERS


RECEIPT_SCHEMA = "wb-core.production-apply-receipt/v1"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}")


class ApplyError(RuntimeError):
    pass


class AmbiguousSubmit(RuntimeError):
    """The adapter cannot tell whether its single submit reached the target."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def validate_operation(value: str) -> str:
    normalized = value.strip().lower()
    if OPERATION_RE.fullmatch(normalized) is None:
        raise ApplyError("invalid-operation-id")
    return normalized


def validate_preview(value: Mapping[str, Any], operation_id: str) -> dict[str, Any]:
    required = {"operation_id", "target", "scope", "prestate_sha256", "candidate_sha256", "recovery"}
    if not required.issubset(value):
        raise ApplyError("preview-incomplete")
    if value.get("operation_id") != operation_id:
        raise ApplyError("preview-operation-mismatch")
    if not isinstance(value.get("target"), str) or not value["target"]:
        raise ApplyError("preview-target-invalid")
    if not isinstance(value.get("scope"), Mapping) or not value["scope"]:
        raise ApplyError("preview-scope-invalid")
    for field in ("prestate_sha256", "candidate_sha256"):
        if DIGEST_RE.fullmatch(str(value.get(field) or "")) is None:
            raise ApplyError(f"preview-{field}-invalid")
    if not isinstance(value.get("recovery"), Mapping) or not value["recovery"]:
        raise ApplyError("preview-recovery-invalid")
    return dict(value)


def validate_readback(value: Mapping[str, Any], operation_id: str) -> dict[str, Any]:
    if value.get("operation_id") != operation_id:
        raise ApplyError("readback-operation-mismatch")
    if value.get("state") not in {"not_submitted", "applied", "no_change", "ambiguous", "failed"}:
        raise ApplyError("readback-state-invalid")
    return dict(value)


def make_receipt(*, action: str, adapter: str, operation_id: str, state: str, preview: Mapping[str, Any] | None = None, submit: Mapping[str, Any] | None = None, readback: Mapping[str, Any] | None = None, reason: str | None = None) -> dict[str, Any]:
    return {
        "schema": RECEIPT_SCHEMA,
        "action": action,
        "adapter": adapter,
        "operation_id": operation_id,
        "state": state,
        "target": preview.get("target") if preview else None,
        "scope": preview.get("scope") if preview else None,
        "prestate_sha256": preview.get("prestate_sha256") if preview else None,
        "candidate_sha256": preview.get("candidate_sha256") if preview else None,
        "recovery": preview.get("recovery") if preview else None,
        "submit": dict(submit) if submit else None,
        "readback": dict(readback) if readback else None,
        "reason": reason,
    }


def execute(*, action: str, adapter_name: str, operation_id: str, request: dict[str, Any], expected_prestate: str = "", expected_candidate: str = "", adapters: Mapping[str, Any] = ADAPTERS) -> dict[str, Any]:
    operation_id = validate_operation(operation_id)
    adapter = adapters.get(adapter_name)
    if adapter is None:
        raise ApplyError("adapter-not-registered")
    if action == "readback":
        readback = validate_readback(adapter.readback(request, operation_id), operation_id)
        return make_receipt(action=action, adapter=adapter_name, operation_id=operation_id, state=readback["state"], readback=readback)

    preview = validate_preview(adapter.preview(request, operation_id), operation_id)
    if action == "preview":
        return make_receipt(action=action, adapter=adapter_name, operation_id=operation_id, state="preview", preview=preview)
    if action != "apply":
        raise ApplyError("unsupported-action")
    if preview["prestate_sha256"] != expected_prestate or preview["candidate_sha256"] != expected_candidate:
        raise ApplyError("candidate-or-prestate-drift")

    before = validate_readback(adapter.readback(request, operation_id), operation_id)
    if before["state"] in {"applied", "no_change"}:
        return make_receipt(action=action, adapter=adapter_name, operation_id=operation_id, state=before["state"], preview=preview, readback=before)
    if before["state"] != "not_submitted":
        raise ApplyError("operation-not-ready")

    submit: Mapping[str, Any] | None = None
    try:
        submit = adapter.apply(request, operation_id, preview)
    except AmbiguousSubmit:
        readback = validate_readback(adapter.readback(request, operation_id), operation_id)
        return make_receipt(action=action, adapter=adapter_name, operation_id=operation_id, state=readback["state"], preview=preview, readback=readback, reason="submit-ambiguous-readback-only")
    if not isinstance(submit, Mapping) or submit.get("operation_id") != operation_id:
        raise ApplyError("submit-receipt-invalid")
    if submit.get("disposition") not in {"submitted", "already_applied", "no_change"}:
        raise ApplyError("submit-disposition-invalid")
    readback = validate_readback(adapter.readback(request, operation_id), operation_id)
    if readback["state"] not in {"applied", "no_change"}:
        raise ApplyError("post-submit-readback-failed")
    return make_receipt(action=action, adapter=adapter_name, operation_id=operation_id, state=readback["state"], preview=preview, submit=submit, readback=readback)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preview", "apply", "readback"))
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--request-json", default="{}")
    parser.add_argument("--expected-prestate", default="")
    parser.add_argument("--expected-candidate", default="")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request_json)
        if not isinstance(request, dict):
            raise ValueError
        receipt = execute(
            action=args.action,
            adapter_name=args.adapter,
            operation_id=args.operation_id,
            request=request,
            expected_prestate=args.expected_prestate,
            expected_candidate=args.expected_candidate,
        )
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ApplyError) else type(exc).__name__
        receipt = make_receipt(action=args.action, adapter=args.adapter, operation_id=args.operation_id, state="blocked", reason=reason)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(receipt) + b"\n")
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["state"] in {"preview", "applied", "no_change", "not_submitted"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
