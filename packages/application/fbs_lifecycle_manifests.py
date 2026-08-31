"""Strict canonical manifests for FBS identity, impact and recovery.

The module is intentionally free of database access.  Incident-specific values
live in a checked-in passport or a private reviewed manifest; the application
and Production Apply runner consume only the versioned grammar defined here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


INCIDENT_PASSPORT_CONTRACT = "fbs_lifecycle_incident_passport/v1"
MAPPING_MANIFEST_CONTRACT = "fbs_identity_mapping_manifest/v2"
IMPACT_MANIFEST_CONTRACT = "fbs_lifecycle_impact_manifest/v2"
RECOVERY_MANIFEST_CONTRACT = "fbs_lifecycle_recovery_manifest/v2"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_OPERATION_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}")
_CONTRACT_RE = re.compile(r"[A-Za-z0-9._:/-]{1,200}")
_FORBIDDEN_MAPPING_KEYS = frozenset(
    {
        "orders",
        "order_count",
        "order_digest",
        "statuses",
        "status_count",
        "status_digest",
        "groups",
        "group_count",
        "dates",
        "date_from",
        "date_to",
    }
)
_PRODUCTION_ENVELOPE_KEYS = {
    "schema",
    "target_id",
    "deployed_sha_contract",
    "dry_run_default",
    "explicit_apply",
    "bounded_scope",
    "pre_change_digest",
    "backup_evidence",
    "expected_affected_records",
    "non_target_invariants",
    "idempotency_or_recovery",
    "post_apply_readback",
    "reconciliation",
    "query_only_manifest_readback",
    "pre_change_digest_value",
    "backup_evidence_value",
    "expected_affected_record_count",
    "non_target_invariant_ids",
    "recovery_contract",
    "commands",
}


class FbsManifestError(ValueError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FbsManifestError("invalid_json", f"Cannot read strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise FbsManifestError("invalid_object", "Manifest root must be an object")
    return value


def parse_incident_passport(value: Mapping[str, Any]) -> dict[str, Any]:
    item = _object(value, "passport")
    passport_keys = {
        "contract",
        "operation_id",
        "target",
        "storage",
        "cutover",
        "tuple",
        "evidence",
        "mapping_expectation",
        "rehearsal_snapshot_digest",
    }
    has_production_envelope = bool(set(item) & _PRODUCTION_ENVELOPE_KEYS)
    _keys(
        item,
        passport_keys | (_PRODUCTION_ENVELOPE_KEYS if has_production_envelope else set()),
        "passport",
    )
    if has_production_envelope:
        _literal(item["schema"], "wb-core.production-apply-manifest/v2", "schema")
        _literal(item["target_id"], item["target"]["target_id"], "target_id")
        _literal(item["deployed_sha_contract"], "exact-merge-sha", "deployed_sha_contract")
        for key in (
            "dry_run_default",
            "explicit_apply",
            "bounded_scope",
            "pre_change_digest",
            "backup_evidence",
            "expected_affected_records",
            "non_target_invariants",
            "idempotency_or_recovery",
            "post_apply_readback",
            "reconciliation",
            "query_only_manifest_readback",
        ):
            _literal(item[key], True, key)
        _digest(item["pre_change_digest_value"], "pre_change_digest_value")
        _literal(item["expected_affected_record_count"], 1, "expected_affected_record_count")
        _string_list(item["non_target_invariant_ids"], "non_target_invariant_ids", nonempty=True)
        backup = item["backup_evidence_value"]
        if not isinstance(backup, str) or not backup.strip() or len(backup) > 500:
            raise FbsManifestError("invalid_field", "backup_evidence_value is invalid")
        recovery = _object(item["recovery_contract"], "recovery_contract")
        _keys(recovery, {"mode", "id"}, "recovery_contract")
        _literal(recovery["mode"], "bounded-recovery", "recovery_contract.mode")
        _identity(recovery["id"], "recovery_contract.id")
        commands = _object(item["commands"], "commands")
        _keys(commands, {"dry_run", "apply", "readback", "reconcile"}, "commands")
        for key, command in commands.items():
            _string_list(command, f"commands.{key}", nonempty=True)
    _literal(item["contract"], INCIDENT_PASSPORT_CONTRACT, "contract")
    _identity(item["operation_id"], "operation_id", pattern=_OPERATION_RE)
    target = _object(item["target"], "target")
    _keys(target, {"target_id", "source_runtime_sha", "release_runtime_contract"}, "target")
    _identity(target["target_id"], "target.target_id")
    _sha(target["source_runtime_sha"], "target.source_runtime_sha")
    _literal(
        target["release_runtime_contract"],
        "exact_release_runtime",
        "target.release_runtime_contract",
    )
    storage = _object(item["storage"], "storage")
    _keys(
        storage,
        {
            "manifest_sha256",
            "operational_generation_id",
            "operational_schema_revision",
            "sqlite_schema_version",
        },
        "storage",
    )
    _digest(storage["manifest_sha256"], "storage.manifest_sha256")
    _identity(storage["operational_generation_id"], "storage.operational_generation_id")
    _identity(storage["operational_schema_revision"], "storage.operational_schema_revision")
    _positive_int(storage["sqlite_schema_version"], "storage.sqlite_schema_version")
    cutover = _object(item["cutover"], "cutover")
    _keys(
        cutover,
        {"cutover_id", "forward_generation_id", "source_cursor_max"},
        "cutover",
    )
    _identity(cutover["cutover_id"], "cutover.cutover_id")
    _identity(cutover["forward_generation_id"], "cutover.forward_generation_id")
    _positive_int(cutover["source_cursor_max"], "cutover.source_cursor_max")
    tuple_value = _mapping_tuple(item["tuple"])
    evidence = _object(item["evidence"], "evidence")
    _keys(evidence, {"external_identity_digest"}, "evidence")
    _digest(evidence["external_identity_digest"], "evidence.external_identity_digest")
    expectation = _object(item["mapping_expectation"], "mapping_expectation")
    _keys(
        expectation,
        {"owner_count", "active_mapping_count", "all_mapping_count", "insert_count"},
        "mapping_expectation",
    )
    _literal(expectation["owner_count"], 1, "mapping_expectation.owner_count")
    _literal(
        expectation["active_mapping_count"],
        0,
        "mapping_expectation.active_mapping_count",
    )
    _literal(expectation["all_mapping_count"], 0, "mapping_expectation.all_mapping_count")
    _literal(expectation["insert_count"], 1, "mapping_expectation.insert_count")
    _digest(item["rehearsal_snapshot_digest"], "rehearsal_snapshot_digest")
    if tuple_value["tuple_digest"] != mapping_tuple_digest(tuple_value):
        raise FbsManifestError("tuple_digest_mismatch", "Mapping tuple digest is not canonical")
    return json.loads(canonical_bytes(item))


def parse_mapping_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    item = _object(value, "mapping_manifest")
    _keys(
        item,
        {
            "contract",
            "operation_id",
            "target",
            "storage",
            "cutover",
            "tuple",
            "evidence",
            "expectation",
            "proposed_mapping",
            "material_cas",
            "safety",
            "apply_allowed",
            "blockers",
            "manifest_digest",
        },
        "mapping_manifest",
    )
    _forbid_mapping_scope_keys(item)
    _literal(item["contract"], MAPPING_MANIFEST_CONTRACT, "contract")
    _identity(item["operation_id"], "operation_id", pattern=_OPERATION_RE)
    target = _object(item["target"], "target")
    _keys(target, {"target_id", "runtime_sha", "source_runtime_sha"}, "target")
    _identity(target["target_id"], "target.target_id")
    _sha(target["runtime_sha"], "target.runtime_sha")
    _sha(target["source_runtime_sha"], "target.source_runtime_sha")
    storage = _object(item["storage"], "storage")
    _keys(
        storage,
        {
            "manifest_sha256",
            "operational_generation_id",
            "operational_schema_revision",
            "sqlite_schema_version",
        },
        "storage",
    )
    _digest(storage["manifest_sha256"], "storage.manifest_sha256")
    _identity(storage["operational_generation_id"], "storage.operational_generation_id")
    _identity(storage["operational_schema_revision"], "storage.operational_schema_revision")
    _positive_int(storage["sqlite_schema_version"], "storage.sqlite_schema_version")
    cutover = _object(item["cutover"], "cutover")
    _keys(
        cutover,
        {
            "cutover_id",
            "cutover_manifest_digest",
            "forward_generation_id",
            "forward_generation_manifest_digest",
        },
        "cutover",
    )
    _identity(cutover["cutover_id"], "cutover.cutover_id")
    _digest(cutover["cutover_manifest_digest"], "cutover.cutover_manifest_digest")
    _identity(cutover["forward_generation_id"], "cutover.forward_generation_id")
    _digest(
        cutover["forward_generation_manifest_digest"],
        "cutover.forward_generation_manifest_digest",
    )
    tuple_value = _mapping_tuple(item["tuple"])
    if tuple_value["tuple_digest"] != mapping_tuple_digest(tuple_value):
        raise FbsManifestError("tuple_digest_mismatch", "Mapping tuple digest is not canonical")
    evidence = _object(item["evidence"], "evidence")
    _keys(
        evidence,
        {
            "external_identity_digest",
            "owner_digest",
            "warehouse_evidence_digest",
            "facility_admission_digest",
        },
        "evidence",
    )
    for key, raw in evidence.items():
        _digest(raw, f"evidence.{key}")
    expectation = _object(item["expectation"], "expectation")
    _keys(
        expectation,
        {"owner_count", "active_mapping_count", "all_mapping_count", "insert_count"},
        "expectation",
    )
    _literal(expectation, {"owner_count": 1, "active_mapping_count": 0, "all_mapping_count": 0, "insert_count": 1}, "expectation")
    proposed = _object(item["proposed_mapping"], "proposed_mapping")
    _keys(proposed, {"mapping_id", "mapping_digest"}, "proposed_mapping")
    _identity(proposed["mapping_id"], "proposed_mapping.mapping_id")
    _digest(proposed["mapping_digest"], "proposed_mapping.mapping_digest")
    material_cas = _object(item["material_cas"], "material_cas")
    _digest(material_cas["digest"], "material_cas.digest")
    if material_cas["digest"] != digest(
        {key: raw for key, raw in material_cas.items() if key != "digest"}
    ):
        raise FbsManifestError("material_cas_digest_mismatch", "Material CAS digest differs")
    _bool(item["apply_allowed"], "apply_allowed")
    _string_list(item["blockers"], "blockers")
    _digest(item["manifest_digest"], "manifest_digest")
    material = {key: raw for key, raw in item.items() if key != "manifest_digest"}
    if item["manifest_digest"] != digest(material):
        raise FbsManifestError("manifest_digest_mismatch", "Mapping manifest digest differs")
    return json.loads(canonical_bytes(item))


def parse_impact_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    item = _object(value, "impact_manifest")
    _keys(
        item,
        {
            "contract",
            "operation_id",
            "target",
            "mapping_readback_digest",
            "storage",
            "boundary",
            "unresolved_scan",
            "affected_groups",
            "dependent_surfaces",
            "history_evidence",
            "baselines",
            "blockers",
            "impact_digest",
        },
        "impact_manifest",
    )
    _literal(item["contract"], IMPACT_MANIFEST_CONTRACT, "contract")
    _identity(item["operation_id"], "operation_id", pattern=_OPERATION_RE)
    _digest(item["mapping_readback_digest"], "mapping_readback_digest")
    _string_list(item["dependent_surfaces"], "dependent_surfaces", nonempty=True)
    _string_list(item["blockers"], "blockers")
    _digest(_object(item["baselines"], "baselines")["non_target_digest"], "baselines.non_target_digest")
    _digest(_object(item["baselines"], "baselines")["wb_digest"], "baselines.wb_digest")
    _digest(item["impact_digest"], "impact_digest")
    material = {key: raw for key, raw in item.items() if key != "impact_digest"}
    if item["impact_digest"] != digest(material):
        raise FbsManifestError("impact_digest_mismatch", "Impact manifest digest differs")
    return json.loads(canonical_bytes(item))


def parse_recovery_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    item = _object(value, "recovery_manifest")
    _keys(
        item,
        {
            "contract",
            "operation_id",
            "target",
            "impact_digest",
            "boundary",
            "scope",
            "predicted_effects",
            "history",
            "baselines",
            "safety",
            "apply_allowed",
            "blockers",
            "recovery_digest",
        },
        "recovery_manifest",
    )
    _literal(item["contract"], RECOVERY_MANIFEST_CONTRACT, "contract")
    _identity(item["operation_id"], "operation_id", pattern=_OPERATION_RE)
    _digest(item["impact_digest"], "impact_digest")
    _bool(item["apply_allowed"], "apply_allowed")
    _string_list(item["blockers"], "blockers")
    scope = _object(item["scope"], "scope")
    sequences = _int_list(scope.get("target_sequences"), "scope.target_sequences")
    row_digests = _string_list(
        scope.get("target_row_digests"), "scope.target_row_digests"
    )
    if len(sequences) != len(row_digests) or len(sequences) != len(set(sequences)):
        raise FbsManifestError(
            "target_sequence_coverage_invalid",
            "Recovery target sequence and row-digest coverage differs",
        )
    for index, raw in enumerate(row_digests):
        _digest(raw, f"scope.target_row_digests[{index}]")
    history = _object(item["history"], "history")
    classifications = _object(history.get("classification_counts"), "history.classification_counts")
    _keys(
        classifications,
        {"recoverable_exact", "remain_missing_no_same_date_evidence"},
        "history.classification_counts",
    )
    for key, raw in classifications.items():
        _nonnegative_int(raw, f"history.classification_counts.{key}")
    _digest(item["recovery_digest"], "recovery_digest")
    material = {key: raw for key, raw in item.items() if key != "recovery_digest"}
    if item["recovery_digest"] != digest(material):
        raise FbsManifestError("recovery_digest_mismatch", "Recovery manifest digest differs")
    return json.loads(canonical_bytes(item))


def mapping_tuple_digest(value: Mapping[str, Any]) -> str:
    item = _object(value, "tuple")
    material = {
        "contract": str(item["tuple_contract"]),
        "source_nm_id": int(item["source_nm_id"]),
        "source_chrt_id": int(item["source_chrt_id"]),
        "source_barcode": str(item["source_barcode"]),
        "source_sku": str(item["source_sku"]),
        "target_nm_id": int(item["target_nm_id"]),
    }
    return digest(material)


def attach_digest(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    if field in result:
        raise FbsManifestError("digest_field_present", f"{field} is already present")
    result[field] = digest(result)
    return result


def _mapping_tuple(value: Any) -> dict[str, Any]:
    item = _object(value, "tuple")
    _keys(
        item,
        {
            "tuple_contract",
            "source_nm_id",
            "source_chrt_id",
            "source_barcode",
            "source_sku",
            "target_nm_id",
            "tuple_digest",
        },
        "tuple",
    )
    _identity(item["tuple_contract"], "tuple.tuple_contract", pattern=_CONTRACT_RE)
    _positive_int(item["source_nm_id"], "tuple.source_nm_id")
    _positive_int(item["source_chrt_id"], "tuple.source_chrt_id")
    _bounded_text(item["source_barcode"], "tuple.source_barcode", maximum=200)
    _bounded_text(item["source_sku"], "tuple.source_sku", maximum=500)
    _positive_int(item["target_nm_id"], "tuple.target_nm_id")
    _digest(item["tuple_digest"], "tuple.tuple_digest")
    return item


def _forbid_mapping_scope_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).lower()
            if normalized in _FORBIDDEN_MAPPING_KEYS:
                raise FbsManifestError(
                    "mapping_scope_field_forbidden",
                    f"Mapping manifest cannot contain {path}.{key}",
                )
            _forbid_mapping_scope_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _forbid_mapping_scope_keys(child, path=f"{path}[{index}]")


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise FbsManifestError("invalid_field_type", f"{field} must be an object")
    return dict(value)


def _keys(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    actual = set(value)
    if actual != expected:
        raise FbsManifestError(
            "invalid_fields",
            f"{field} fields differ",
            details={"missing": sorted(expected - actual), "unknown": sorted(actual - expected)},
        )


def _literal(value: Any, expected: Any, field: str) -> None:
    if value != expected or isinstance(value, bool) != isinstance(expected, bool):
        raise FbsManifestError("invalid_literal", f"{field} must equal {expected!r}")


def _identity(value: Any, field: str, *, pattern: re.Pattern[str] = _IDENTITY_RE) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise FbsManifestError("invalid_identity", f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise FbsManifestError("invalid_sha", f"{field} must be exact 40-hex")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise FbsManifestError("invalid_digest", f"{field} must be sha256:<64hex>")
    return value


def _positive_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FbsManifestError("invalid_integer", f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FbsManifestError("invalid_integer", f"{field} must be a non-negative integer")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise FbsManifestError("invalid_boolean", f"{field} must be boolean")
    return value


def _bounded_text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise FbsManifestError("invalid_text", f"{field} must be non-empty and bounded")
    return value


def _string_list(value: Any, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise FbsManifestError("invalid_list", f"{field} must be a string list")
    if nonempty and not value:
        raise FbsManifestError("empty_list", f"{field} must not be empty")
    if len(value) != len(set(value)):
        raise FbsManifestError("duplicate_list_item", f"{field} contains duplicates")
    return list(value)


def _int_list(value: Any, field: str) -> list[int]:
    if not isinstance(value, list):
        raise FbsManifestError("invalid_list", f"{field} must be an integer list")
    return [_positive_int(item, f"{field}[]") for item in value]


def digest_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    return digest([dict(item) for item in rows])
