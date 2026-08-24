"""Pure advisory admission for a frozen repo-only Release Runner snapshot.

This module deliberately has no command-line entrypoint or adapters.  It only
turns an already collected JSON-compatible snapshot into a deterministic
receipt; the existing GitHub Release Train remains authoritative.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any


SNAPSHOT_SCHEMA = "wb-core.release-runner.repo-only-snapshot/v1"
RECEIPT_SCHEMA = "wb-core.release-runner.repo-only-admission-receipt/v1"
CANONICAL_REPOSITORY = "orenvlad-ai/wb-core"
REQUIRED_CHECK_NAME = "baseline"
REQUIRED_CHECK_APP_SLUG = "github-actions"

REASON_CODES = (
    "snapshot-schema-unsupported",
    "snapshot-shape-invalid",
    "repository-not-canonical",
    "pr-number-invalid",
    "pr-not-open",
    "pr-draft",
    "base-repository-mismatch",
    "base-not-main",
    "base-sha-invalid",
    "head-repository-mismatch",
    "head-ref-missing",
    "head-sha-invalid",
    "expected-head-sha-invalid",
    "head-not-expected",
    "task-label-not-standard",
    "scope-label-not-repo-only",
    "release-ready-missing",
    "release-state-conflict",
    "dcp-handoff-unsupported",
    "finance-lease-unsupported",
    "legacy-contour-unsupported",
    "required-check-id-invalid",
    "required-check-name-mismatch",
    "required-check-source-mismatch",
    "required-check-head-mismatch",
    "required-check-not-completed",
    "required-check-not-successful",
    "mergeability-unknown",
    "mergeability-false",
)

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "repository",
        "pr_number",
        "state",
        "draft",
        "base_repository",
        "base_ref",
        "base_sha",
        "head_repository",
        "head_ref",
        "head_sha",
        "expected_head_sha",
        "labels",
        "required_check",
        "mergeable",
    }
)
_CHECK_KEYS = frozenset(
    {"id", "name", "app_slug", "head_sha", "status", "conclusion"}
)
_STRING_FIELDS = frozenset(
    {
        "schema",
        "repository",
        "state",
        "base_repository",
        "base_ref",
        "base_sha",
        "head_repository",
        "head_ref",
        "head_sha",
        "expected_head_sha",
    }
)
_CHECK_STRING_FIELDS = frozenset(
    {"name", "app_slug", "head_sha", "status", "conclusion"}
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DCP_BRANCH_RE = re.compile(r"^ao/wb-core-[1-9][0-9]*/root$")
_LEGACY_RELEASE_LABELS = frozenset(
    {
        "release:staged",
        "release:awaiting-agent",
        "release:awaiting-ui",
        "release:needs-resume",
        "release:lane-owner",
        "release:superseded",
        "release:retired",
    }
)


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON with stable key order and no insignificant whitespace."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _text(value: Any, *, lowercase: bool = False) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized.lower() if lowercase else normalized


def _sha(value: Any) -> str:
    return _text(value, lowercase=True)


def _valid_sha(value: str) -> bool:
    return bool(_SHA_RE.fullmatch(value))


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _labels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {
            normalized
            for item in value
            if (normalized := _text(item, lowercase=True))
        }
    )


def _json_scalar(value: Any) -> str | int | bool | None:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    return None


def _canonical_snapshot_value(value: Any, *, field: str = "") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            result[key] = _canonical_snapshot_value(item, field=key)
        return result
    if isinstance(value, list):
        if field == "labels":
            return _labels(value)
        return [_canonical_snapshot_value(item) for item in value]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        if field.endswith("_sha"):
            return _sha(value)
        return _text(value)
    return {"invalid_json_type": type(value).__name__}


def canonical_snapshot_bytes(snapshot: Any) -> bytes:
    """Return the byte-stable normalized representation used by the digest."""

    return canonical_json_bytes(_canonical_snapshot_value(snapshot))


def _shape_is_valid(snapshot: Any) -> bool:
    if not isinstance(snapshot, Mapping):
        return False
    if any(not isinstance(key, str) for key in snapshot):
        return False
    if frozenset(snapshot) != _TOP_LEVEL_KEYS:
        return False
    if any(not isinstance(snapshot.get(field), str) for field in _STRING_FIELDS):
        return False
    if not isinstance(snapshot.get("pr_number"), int) or isinstance(
        snapshot.get("pr_number"), bool
    ):
        return False
    if not isinstance(snapshot.get("draft"), bool):
        return False
    if not isinstance(snapshot.get("labels"), list) or any(
        not isinstance(label, str) for label in snapshot.get("labels", [])
    ):
        return False
    mergeable = snapshot.get("mergeable")
    if mergeable is not None and not isinstance(mergeable, bool):
        return False
    check = snapshot.get("required_check")
    if not isinstance(check, Mapping):
        return False
    if any(not isinstance(key, str) for key in check):
        return False
    if frozenset(check) != _CHECK_KEYS:
        return False
    if not isinstance(check.get("id"), int) or isinstance(check.get("id"), bool):
        return False
    return all(isinstance(check.get(field), str) for field in _CHECK_STRING_FIELDS)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def admission_receipt(snapshot: Any) -> dict[str, Any]:
    """Compute a deterministic, advisory-only admission receipt."""

    source = _mapping(snapshot)
    check = _mapping(source.get("required_check"))

    repository = _text(source.get("repository"))
    base_repository = _text(source.get("base_repository"))
    base_ref = _text(source.get("base_ref"))
    base_sha = _sha(source.get("base_sha"))
    head_repository = _text(source.get("head_repository"))
    head_ref = _text(source.get("head_ref"))
    head_sha = _sha(source.get("head_sha"))
    expected_head_sha = _sha(source.get("expected_head_sha"))
    labels = _labels(source.get("labels"))

    check_id = check.get("id")
    check_name = _text(check.get("name"))
    check_app_slug = _text(check.get("app_slug"))
    check_head_sha = _sha(check.get("head_sha"))
    check_status = _text(check.get("status"))
    check_conclusion = _text(check.get("conclusion"))

    task_labels = [label for label in labels if label.startswith("task:")]
    scope_labels = [label for label in labels if label.startswith("scope:")]
    release_labels = [label for label in labels if label.startswith("release:")]
    dcp_signal = bool(_DCP_BRANCH_RE.fullmatch(head_ref.lower())) or any(
        label.startswith("dcp:") or "dcp-handoff" in label for label in labels
    )
    finance_signal = any(label.startswith("finance:") for label in labels)
    legacy_signal = any(
        label == "task:loop"
        or label.startswith(
            ("loop:", "orchestration:", "legacy:", "recovery:", "readmission:")
        )
        or "readmission" in label
        or "recovery" in label
        or label in _LEGACY_RELEASE_LABELS
        for label in labels
    )

    conditions = {
        "snapshot-schema-unsupported": _text(source.get("schema"))
        != SNAPSHOT_SCHEMA,
        "snapshot-shape-invalid": not _shape_is_valid(snapshot),
        "repository-not-canonical": repository != CANONICAL_REPOSITORY,
        "pr-number-invalid": not _positive_int(source.get("pr_number")),
        "pr-not-open": _text(source.get("state")) != "open",
        "pr-draft": source.get("draft") is not False,
        "base-repository-mismatch": base_repository != repository,
        "base-not-main": base_ref != "main",
        "base-sha-invalid": not _valid_sha(base_sha),
        "head-repository-mismatch": head_repository != repository,
        "head-ref-missing": not head_ref,
        "head-sha-invalid": not _valid_sha(head_sha),
        "expected-head-sha-invalid": not _valid_sha(expected_head_sha),
        "head-not-expected": _valid_sha(head_sha)
        and _valid_sha(expected_head_sha)
        and head_sha != expected_head_sha,
        "task-label-not-standard": task_labels != ["task:standard"],
        "scope-label-not-repo-only": scope_labels != ["scope:repo-only"],
        "release-ready-missing": "release:ready" not in release_labels,
        "release-state-conflict": any(
            label != "release:ready" for label in release_labels
        ),
        "dcp-handoff-unsupported": dcp_signal,
        "finance-lease-unsupported": finance_signal,
        "legacy-contour-unsupported": legacy_signal,
        "required-check-id-invalid": not _positive_int(check_id),
        "required-check-name-mismatch": check_name != REQUIRED_CHECK_NAME,
        "required-check-source-mismatch": check_app_slug
        != REQUIRED_CHECK_APP_SLUG,
        "required-check-head-mismatch": not _valid_sha(check_head_sha)
        or check_head_sha != head_sha,
        "required-check-not-completed": check_status != "completed",
        "required-check-not-successful": check_conclusion != "success",
        "mergeability-unknown": source.get("mergeable") is not True
        and source.get("mergeable") is not False,
        "mergeability-false": source.get("mergeable") is False,
    }
    reason_codes = [code for code in REASON_CODES if conditions[code]]

    receipt = {
        "schema": RECEIPT_SCHEMA,
        "decision": "eligible" if not reason_codes else "blocked",
        "reason_codes": reason_codes,
        "snapshot_sha256": hashlib.sha256(
            canonical_snapshot_bytes(snapshot)
        ).hexdigest(),
        "bindings": {
            "repository": repository,
            "pr_number": _json_scalar(source.get("pr_number")),
            "base": {
                "repository": base_repository,
                "ref": base_ref,
                "sha": base_sha,
            },
            "head": {
                "repository": head_repository,
                "ref": head_ref,
                "sha": head_sha,
            },
            "expected_head_sha": expected_head_sha,
            "required_check": {
                "id": _json_scalar(check_id),
                "name": check_name,
                "app_slug": check_app_slug,
                "head_sha": check_head_sha,
                "status": check_status,
                "conclusion": check_conclusion,
            },
            "task": task_labels,
            "scope": scope_labels,
        },
    }
    return receipt


def receipt_json_bytes(snapshot: Any) -> bytes:
    """Return the complete admission receipt as byte-stable UTF-8 JSON."""

    return canonical_json_bytes(admission_receipt(snapshot))
