#!/usr/bin/env python3
"""Deterministic smoke coverage for Codex authorization and gate routing."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import codex_authorization_gate as gate  # noqa: E402


GOAL_ID = "wbc-0001-router-soglasovaniy"
OWNER = "codex-task:wbc-0001"
OTHER_SURFACE = "codex-task:wbc-9999"
REPO_DESTINATION = "github:orenvlad-ai/wb-core"
AUTOANSWERS_DESTINATION = "runtime:wb-core-eu:autoanswers"
REPO_TARGET = "repo:apps/codex_authorization_gate.py"
TASK_TARGET = "autoanswers:task:night-archive-dependency"
LEASE_TARGET = "autoanswers:lease:night-archive-dependency"
AUDIT_TARGET = "autoanswers:audit:night-archive-dependency"


def sha(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode("utf-8")).hexdigest()


def delta_descriptor(
    *,
    target: str,
    destination: str,
    operation: str,
    semantic_kind: str = "technical",
    effects: list[str] | None = None,
    reversible: bool = True,
) -> dict[str, object]:
    return {
        "target": target,
        "destination": destination,
        "semantic_kind": semantic_kind,
        "operation": operation,
        "effects": list(effects or []),
        "reversible": reversible,
    }


def action_delta(
    *,
    target: str,
    destination: str,
    operation: str,
    semantic_kind: str = "technical",
    effects: list[str] | None = None,
    reversible: bool = True,
    evidence_label: str | None = None,
) -> dict[str, object]:
    label = evidence_label or f"{target}:{operation}"
    return {
        **delta_descriptor(
            target=target,
            destination=destination,
            operation=operation,
            semantic_kind=semantic_kind,
            effects=effects,
            reversible=reversible,
        ),
        "before_digest": sha(label + ":before"),
        "after_digest": sha(label + ":after"),
    }


def accepted_goal() -> dict[str, object]:
    return {
        "schema": gate.GOAL_SCHEMA,
        "goal_id": GOAL_ID,
        "owner_surface_id": OWNER,
        "implementation_intent": gate.IMPLEMENTATION_INTENT,
        "goal_statement": "Enforce one autonomous authorization router to COMPLETE.",
        "included_final_targets": [
            REPO_TARGET,
            TASK_TARGET,
            LEASE_TARGET,
            AUDIT_TARGET,
        ],
        "destinations": [REPO_DESTINATION, AUTOANSWERS_DESTINATION],
        "allowed_final_deltas": [
            delta_descriptor(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="same_goal_code_correction",
            )
        ],
        "allowed_auxiliary_final_deltas": [
            delta_descriptor(
                target=TASK_TARGET,
                destination=AUTOANSWERS_DESTINATION,
                operation="processing_to_terminal_error",
                semantic_kind="operational_control_metadata",
            ),
            delta_descriptor(
                target=LEASE_TARGET,
                destination=AUTOANSWERS_DESTINATION,
                operation="clear_exact_lease",
                semantic_kind="operational_control_metadata",
            ),
            delta_descriptor(
                target=AUDIT_TARGET,
                destination=AUTOANSWERS_DESTINATION,
                operation="append_terminal_audit",
                semantic_kind="operational_control_metadata",
            ),
        ],
        "allowed_temporary_dependency_actions": [
            {
                "resource": TASK_TARGET,
                "destination": AUTOANSWERS_DESTINATION,
                "dependency_id": "dependency:autoanswers-night-archive",
                "operation": "fresh_readiness_probe",
                "effects": [],
                "bounded": True,
                "preservation_predicates": ["business_content_unchanged"],
                "readback_predicates": ["same_dependency_identity_read_back"],
            }
        ],
        "forbidden_effects": [
            "credential_capability",
            "destination",
            "external",
            "financial",
            "irreversible",
            "protected_data",
            "publication",
            "security_access",
        ],
        "answered_decisions": [],
        "terminal_decision_digests": [],
        "supersedes_goal_id": None,
    }


def resource(
    resource_id: str, *, role: str = "target", medium: str = "repo"
) -> dict[str, str]:
    return {"id": resource_id, "role": role, "storage_medium": medium}


def manifest(
    *,
    final_deltas: list[dict[str, object]] | None = None,
    auxiliary_final_deltas: list[dict[str, object]] | None = None,
    temporary_dependency_actions: list[dict[str, object]] | None = None,
    resources: list[dict[str, str]] | None = None,
    proposer_surface_id: str = OWNER,
    dependency_status: str = "satisfied",
    dependency_ids: list[str] | None = None,
    dependency_evidence: list[str] | None = None,
    dependency_preservation: list[str] | None = None,
    dependency_readback: list[str] | None = None,
    submit_state: str = "not_started",
    submit_intent: str = "mutation",
    operation_id: str = "operation:router-correction:002",
    submitted_operation_id: str | None = None,
    terminal_operation_ids: list[str] | None = None,
    warnings: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    final_values = (
        final_deltas
        if final_deltas is not None
        else [
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="same_goal_code_correction",
            )
        ]
    )
    auxiliary_values = auxiliary_final_deltas or []
    temporary_values = temporary_dependency_actions or []
    resource_values = resources or [resource(REPO_TARGET)]
    has_reversible_delta = any(
        item["reversible"] is True for item in [*final_values, *auxiliary_values]
    )
    has_action = bool(final_values or auxiliary_values or temporary_values)
    return {
        "schema": gate.MANIFEST_SCHEMA,
        "goal_id": GOAL_ID,
        "proposer_surface_id": proposer_surface_id,
        "action_id": "action:router-correction:002",
        "resources": resource_values,
        "final_deltas": final_values,
        "auxiliary_final_deltas": auxiliary_values,
        "temporary_dependency_actions": temporary_values,
        "dependency_proof": {
            "status": dependency_status,
            "required_dependency_ids": dependency_ids or [],
            "evidence_digests": dependency_evidence or [],
            "preservation_predicates": dependency_preservation or [],
            "readback_predicates": dependency_readback or [],
        },
        "submit": {
            "state": submit_state,
            "intent": submit_intent,
            "operation_id": operation_id,
            "submitted_operation_id": submitted_operation_id,
            "terminal_operation_ids": terminal_operation_ids or [],
        },
        "rollback_predicates": ["restore_exact_before_digest"]
        if has_reversible_delta
        else [],
        "readback_predicates": ["exact_after_digest_matches"] if has_action else [],
        "warnings": warnings or [],
    }


def empty_registry() -> dict[str, object]:
    return {
        "schema": gate.GATE_REGISTRY_SCHEMA,
        "goal_id": GOAL_ID,
        "owner_surface_id": OWNER,
        "records": [],
    }


def assert_outcome(receipt: dict[str, object], expected: str) -> None:
    assert receipt["outcome"] == expected, receipt
    assert gate.validate_receipt(receipt)["valid"] is True


def canonical_and_envelope_contract_smoke() -> None:
    goal = accepted_goal()
    envelope = gate.compile_envelope(goal)
    reordered = deepcopy(goal)
    for key in (
        "included_final_targets",
        "destinations",
        "allowed_auxiliary_final_deltas",
        "forbidden_effects",
    ):
        reordered[key] = list(reversed(reordered[key]))
    assert gate.canonical_json_bytes(envelope) == gate.canonical_json_bytes(
        gate.compile_envelope(reordered)
    )
    assert envelope["implementation_intent"] == gate.IMPLEMENTATION_INTENT
    assert envelope["validity"] == {
        "until": "COMPLETE_OR_SUPERSEDED",
        "supersedes_goal_id": None,
    }
    assert "mode" not in envelope and "trust_tier" not in envelope

    auto = gate.decide(envelope, manifest(), empty_registry())
    assert_outcome(auto, "AUTO_CONTINUE")
    assert "AUTHORIZED_PRE_SUBMIT_CORRECTION" in auto["reason_codes"]

    permuted_manifest = manifest()
    permuted_manifest["resources"] = list(reversed(permuted_manifest["resources"]))
    permuted_manifest["rollback_predicates"] = list(
        reversed(permuted_manifest["rollback_predicates"])
    )
    assert gate.canonical_json_bytes(auto) == gate.canonical_json_bytes(
        gate.decide(envelope, permuted_manifest, empty_registry())
    )


def night_autoanswers_storage_independence_smoke() -> None:
    envelope = gate.compile_envelope(accepted_goal())
    night_deltas = [
        action_delta(
            target=TASK_TARGET,
            destination=AUTOANSWERS_DESTINATION,
            operation="processing_to_terminal_error",
            semantic_kind="operational_control_metadata",
        ),
        action_delta(
            target=LEASE_TARGET,
            destination=AUTOANSWERS_DESTINATION,
            operation="clear_exact_lease",
            semantic_kind="operational_control_metadata",
        ),
        action_delta(
            target=AUDIT_TARGET,
            destination=AUTOANSWERS_DESTINATION,
            operation="append_terminal_audit",
            semantic_kind="operational_control_metadata",
        ),
    ]
    production_manifest = manifest(
        final_deltas=[],
        auxiliary_final_deltas=night_deltas,
        resources=[
            resource(TASK_TARGET, medium="production SQLite"),
            resource(LEASE_TARGET, medium="production SQLite"),
            resource(AUDIT_TARGET, medium="production SQLite"),
            resource(
                "settlement:autoanswers-night-archive",
                role="evidence",
                medium="production SQLite",
            ),
        ],
        dependency_status="failed",
        dependency_ids=["dependency:autoanswers-night-archive"],
        dependency_evidence=[sha("unsafe-owner-policy-terminal")],
        dependency_preservation=[
            "business_content_unchanged",
            "financial_effect_zero",
            "provider_and_wb_calls_zero",
            "publication_effect_zero",
            "settlement_preserved",
        ],
        dependency_readback=[
            "task_terminal_error_read_back",
            "lease_cleared_read_back",
            "audit_appended_read_back",
            "settlement_digest_preserved",
        ],
        operation_id="operation:autoanswers-terminalization:002",
    )
    production_receipt = gate.decide(envelope, production_manifest, empty_registry())
    assert_outcome(production_receipt, "AUTO_CONTINUE")

    local_manifest = deepcopy(production_manifest)
    for item in local_manifest["resources"]:
        item["storage_medium"] = "local fixture file"
    local_receipt = gate.decide(envelope, local_manifest, empty_registry())
    assert gate.canonical_json_bytes(production_receipt) == gate.canonical_json_bytes(
        local_receipt
    )

    temporary_probe = {
        "resource": TASK_TARGET,
        "destination": AUTOANSWERS_DESTINATION,
        "dependency_id": "dependency:autoanswers-night-archive",
        "operation": "fresh_readiness_probe",
        "effects": [],
        "bounded": True,
        "preservation_predicates": ["business_content_unchanged"],
        "readback_predicates": ["same_dependency_identity_read_back"],
        "identity_digest": sha("fresh-readiness-probe-002"),
    }
    temporary_manifest = manifest(
        final_deltas=[],
        temporary_dependency_actions=[temporary_probe],
        resources=[
            resource(TASK_TARGET, role="dependency", medium="production SQLite")
        ],
        dependency_status="failed",
        dependency_ids=["dependency:autoanswers-night-archive"],
        dependency_evidence=[sha("failed-dependency")],
        dependency_preservation=["business_content_unchanged"],
        dependency_readback=["same_dependency_identity_read_back"],
        operation_id="operation:autoanswers-readiness:002",
    )
    assert_outcome(
        gate.decide(envelope, temporary_manifest, empty_registry()),
        "AUTO_CONTINUE",
    )


def warning_and_evidence_smoke() -> None:
    envelope = gate.compile_envelope(accepted_goal())
    warning_only = manifest(
        final_deltas=[],
        resources=[resource(REPO_TARGET, role="evidence")],
        submit_intent="none",
        warnings=[
            {
                "id": "warning:unrelated-disk",
                "relation": "unrelated",
                "evidence_digest": sha("unrelated-disk"),
            },
            {
                "id": "warning:stale-run",
                "relation": "stale",
                "evidence_digest": sha("stale-run"),
            },
        ],
    )
    warning_receipt = gate.decide(envelope, warning_only, empty_registry())
    assert_outcome(warning_receipt, "AUTO_CONTINUE")
    assert warning_receipt["warning_handling"] == {
        "record": ["warning:unrelated-disk"],
        "refresh": ["warning:stale-run"],
    }

    missing_dependency = manifest(
        dependency_status="missing",
        dependency_ids=["dependency:unknown-identity"],
    )
    blocked = gate.decide(envelope, missing_dependency, empty_registry())
    assert_outcome(blocked, "EVIDENCE_BLOCKED")
    assert blocked["reason_codes"] == ["DEPENDENCY_NOT_PROVEN"]
    assert blocked["publication"]["action"] == "NONE"

    missing_identity = manifest()
    missing_identity["submit"]["operation_id"] = ""
    invalid = gate.decide(envelope, missing_identity, empty_registry())
    assert_outcome(invalid, "EVIDENCE_BLOCKED")
    assert invalid["reason_codes"] == ["INVALID_INPUT"]
    assert invalid["evidence"][0]["error_code"] == "MISSING_IDENTITY"

    terminal_identity = manifest(
        operation_id="operation:terminal-old:001",
        terminal_operation_ids=["operation:terminal-old:001"],
    )
    terminal_block = gate.decide(envelope, terminal_identity, empty_registry())
    assert_outcome(terminal_block, "EVIDENCE_BLOCKED")
    assert terminal_block["reason_codes"] == ["STALE_OPERATION_ID"]

    reconciled_identity = manifest(
        final_deltas=[],
        resources=[resource(REPO_TARGET, role="evidence")],
        submit_state="reconciled",
        submit_intent="none",
        operation_id="operation:reconciled-old:001",
        submitted_operation_id="operation:reconciled-old:001",
    )
    reconciled_block = gate.decide(envelope, reconciled_identity, empty_registry())
    assert_outcome(reconciled_block, "EVIDENCE_BLOCKED")
    assert reconciled_block["reason_codes"] == ["STALE_OPERATION_ID"]


def submitted_ambiguity_smoke() -> None:
    envelope = gate.compile_envelope(accepted_goal())
    ambiguous_retry = manifest(
        submit_state="ambiguous",
        submit_intent="mutation",
        operation_id="operation:submitted:001",
        submitted_operation_id="operation:submitted:001",
    )
    retry_block = gate.decide(envelope, ambiguous_retry, empty_registry())
    assert_outcome(retry_block, "EVIDENCE_BLOCKED")
    assert retry_block["reason_codes"] == ["SUBMIT_RECONCILIATION_REQUIRED"]

    reconcile = manifest(
        final_deltas=[],
        resources=[resource(REPO_TARGET, role="evidence")],
        submit_state="ambiguous",
        submit_intent="query_only_reconcile",
        operation_id="operation:submitted:001",
        submitted_operation_id="operation:submitted:001",
    )
    reconcile["readback_predicates"] = ["same_operation_query_only_readback"]
    reconcile_receipt = gate.decide(envelope, reconcile, empty_registry())
    assert_outcome(reconcile_receipt, "AUTO_CONTINUE")
    assert reconcile_receipt["reason_codes"] == ["QUERY_ONLY_RECONCILIATION"]


def human_reason_smoke() -> None:
    envelope = gate.compile_envelope(accepted_goal())

    cases = [
        (
            "new-target",
            action_delta(
                target="repo:new-final-target",
                destination=REPO_DESTINATION,
                operation="change_new_target",
            ),
            "NEW_FINAL_TARGET",
        ),
        (
            "new-destination",
            action_delta(
                target=REPO_TARGET,
                destination="github:another/repository",
                operation="publish_elsewhere",
            ),
            "NEW_DESTINATION",
        ),
        (
            "business-semantic",
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="change_business_rule",
                semantic_kind="business_semantic",
                effects=["business"],
            ),
            "NEW_BUSINESS_SEMANTIC",
        ),
        (
            "payment",
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="create_payment",
                effects=["financial"],
            ),
            "NEW_FINANCIAL_EFFECT",
        ),
        (
            "publication",
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="publish_public_reply",
                effects=["external", "publication"],
            ),
            "NEW_PUBLICATION_EFFECT",
        ),
        (
            "security",
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="change_access_rule",
                effects=["security_access"],
            ),
            "NEW_SECURITY_ACCESS_EFFECT",
        ),
        (
            "protected-data",
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="change_protected_business_fact",
                semantic_kind="protected_business_fact",
                effects=["protected_data"],
            ),
            "NEW_PROTECTED_DATA_FINAL_DELTA",
        ),
        (
            "irreversible",
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="irreversible_final_change",
                effects=["irreversible"],
                reversible=False,
            ),
            "NEW_IRREVERSIBLE_FINAL_DELTA",
        ),
    ]
    for label, proposed_delta, required_reason in cases:
        case_manifest = manifest(
            final_deltas=[proposed_delta],
            resources=[resource(str(proposed_delta["target"]))],
            operation_id=f"operation:human:{label}",
        )
        receipt = gate.decide(envelope, case_manifest, empty_registry())
        assert_outcome(receipt, "HUMAN_REQUIRED")
        assert required_reason in receipt["reason_codes"], receipt
        assert receipt["publication"] == {
            "action": "PUBLISH_ON_OWNER",
            "surface_id": OWNER,
        }

    credential_action = {
        "resource": REPO_TARGET,
        "destination": REPO_DESTINATION,
        "dependency_id": "dependency:credential-capability",
        "operation": "interactive_login",
        "effects": ["credential_capability"],
        "bounded": True,
        "preservation_predicates": ["no_state_change_before_login"],
        "readback_predicates": ["login_capability_observed"],
        "identity_digest": sha("credential-capability"),
    }
    credential_manifest = manifest(
        final_deltas=[],
        temporary_dependency_actions=[credential_action],
        resources=[resource(REPO_TARGET, role="dependency")],
        operation_id="operation:human:credentials",
    )
    credential_receipt = gate.decide(envelope, credential_manifest, empty_registry())
    assert_outcome(credential_receipt, "HUMAN_REQUIRED")
    assert credential_receipt["reason_codes"] == ["CREDENTIAL_CAPABILITY_REQUIRED"]

    unmatched_technical = manifest(
        final_deltas=[
            action_delta(
                target=REPO_TARGET,
                destination=REPO_DESTINATION,
                operation="dominant_technical_recommendation",
            )
        ]
    )
    technical_block = gate.decide(envelope, unmatched_technical, empty_registry())
    assert_outcome(technical_block, "EVIDENCE_BLOCKED")
    assert technical_block["reason_codes"] == ["UNAUTHORIZED_TECHNICAL_DELTA"]
    assert technical_block["publication"]["action"] == "NONE"


def duplicate_and_answered_gate_smoke() -> None:
    base_goal = accepted_goal()
    envelope = gate.compile_envelope(base_goal)
    proposed = action_delta(
        target="repo:new-final-target",
        destination=REPO_DESTINATION,
        operation="change_new_target",
    )
    owner_manifest = manifest(
        final_deltas=[proposed],
        resources=[resource("repo:new-final-target")],
        operation_id="operation:duplicate-gate:001",
    )
    initial = gate.decide(envelope, owner_manifest, empty_registry())
    assert_outcome(initial, "HUMAN_REQUIRED")
    decision_digest = str(initial["decision_digest"])
    delta_digest = str(initial["evidence"][0]["delta_digest"])

    second_surface_manifest = deepcopy(owner_manifest)
    second_surface_manifest["proposer_surface_id"] = OTHER_SURFACE
    routed = gate.decide(envelope, second_surface_manifest, empty_registry())
    assert_outcome(routed, "HUMAN_REQUIRED")
    assert routed["decision_digest"] == decision_digest
    assert routed["publication"] == {
        "action": "ROUTE_TO_OWNER",
        "surface_id": OWNER,
    }

    pending_registry = empty_registry()
    pending_registry["records"] = [
        {
            "decision_digest": decision_digest,
            "status": "pending",
            "resolution": "unanswered",
            "covered_delta_digests": [],
            "publisher_surface_id": OWNER,
        }
    ]
    duplicate = gate.decide(envelope, second_surface_manifest, pending_registry)
    assert_outcome(duplicate, "HUMAN_REQUIRED")
    assert duplicate["publication"] == {
        "action": "SUPPRESS_DUPLICATE",
        "surface_id": OWNER,
    }

    rejected_registry = empty_registry()
    rejected_registry["records"] = [
        {
            "decision_digest": decision_digest,
            "status": "answered",
            "resolution": "rejected",
            "covered_delta_digests": [],
            "publisher_surface_id": OWNER,
        }
    ]
    rejected = gate.decide(envelope, owner_manifest, rejected_registry)
    assert_outcome(rejected, "EVIDENCE_BLOCKED")
    assert rejected["reason_codes"] == ["ANSWERED_GATE_REJECTED"]

    terminal_goal = deepcopy(base_goal)
    terminal_goal["terminal_decision_digests"] = [decision_digest]
    terminal_envelope = gate.compile_envelope(terminal_goal)
    terminal = gate.decide(terminal_envelope, owner_manifest, empty_registry())
    assert_outcome(terminal, "EVIDENCE_BLOCKED")
    assert terminal["reason_codes"] == ["TERMINAL_DECISION_REUSED"]

    extended_goal = deepcopy(base_goal)
    extended_goal["answered_decisions"] = [
        {
            "decision_digest": decision_digest,
            "resolution": "accepted_extension",
            "covered_delta_digests": [delta_digest, sha("larger-accepted-extension")],
        }
    ]
    extended_envelope = gate.compile_envelope(extended_goal)
    covered = gate.decide(extended_envelope, owner_manifest, empty_registry())
    assert_outcome(covered, "AUTO_CONTINUE")
    assert covered["reason_codes"] == ["ANSWERED_EXTENSION_COVERS_DELTA"]

    payment_delta = action_delta(
        target=REPO_TARGET,
        destination=REPO_DESTINATION,
        operation="new_payment_after_extension",
        effects=["financial"],
    )
    mixed_manifest = manifest(
        final_deltas=[proposed, payment_delta],
        resources=[resource("repo:new-final-target"), resource(REPO_TARGET)],
        operation_id="operation:mixed-extension:001",
    )
    mixed = gate.decide(extended_envelope, mixed_manifest, empty_registry())
    assert_outcome(mixed, "HUMAN_REQUIRED")
    assert mixed["reason_codes"] == ["NEW_FINANCIAL_EFFECT"]
    assert len(mixed["evidence"]) == 1
    assert mixed["evidence"][0]["delta_digest"] != delta_digest


def malformed_and_unknown_fail_closed_smoke() -> None:
    envelope = gate.compile_envelope(accepted_goal())
    unknown_schema = manifest()
    unknown_schema["schema"] = "unknown/v999"
    receipt = gate.decide(envelope, unknown_schema, empty_registry())
    assert_outcome(receipt, "EVIDENCE_BLOCKED")
    assert receipt["publication"]["action"] == "NONE"
    assert receipt["evidence"][0]["error_code"] == "UNKNOWN_SCHEMA"

    unknown_effect = manifest()
    unknown_effect["final_deltas"][0]["effects"] = ["risky"]
    receipt = gate.decide(envelope, unknown_effect, empty_registry())
    assert_outcome(receipt, "EVIDENCE_BLOCKED")
    assert receipt["evidence"][0]["error_code"] == "UNKNOWN_EFFECT"

    mode_selector = accepted_goal()
    mode_selector["mode"] = "high-trust"
    try:
        gate.compile_envelope(mode_selector)
    except gate.AuthorizationInputError as exc:
        assert exc.code == "SCHEMA_FIELDS_MISMATCH"
    else:
        raise AssertionError("a selectable trust mode entered the canonical envelope")

    bad_receipt = gate.decide(envelope, manifest(), empty_registry())
    bad_receipt["reason_codes"] = ["RISKY_MATERIAL_SCOPE_EXPANSION"]
    body = {key: value for key, value in bad_receipt.items() if key != "receipt_digest"}
    bad_receipt["receipt_digest"] = gate.digest(body)
    try:
        gate.validate_receipt(bad_receipt)
    except gate.AuthorizationInputError as exc:
        assert exc.code == "UNKNOWN_REASON"
    else:
        raise AssertionError("an unknown subjective reason passed receipt validation")


def cli_canonical_roundtrip_smoke() -> None:
    with tempfile.TemporaryDirectory(prefix="codex-authorization-gate-") as raw:
        root = Path(raw)
        goal_path = root / "goal.json"
        envelope_path = root / "envelope.json"
        manifest_path = root / "manifest.json"
        registry_path = root / "registry.json"
        receipt_path = root / "receipt.json"
        gate.write_canonical_json(goal_path, accepted_goal())
        gate.write_canonical_json(manifest_path, manifest())
        gate.write_canonical_json(registry_path, empty_registry())
        subprocess.run(
            [
                sys.executable,
                "apps/codex_authorization_gate.py",
                "compile-envelope",
                "--goal",
                str(goal_path),
                "--output",
                str(envelope_path),
            ],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "apps/codex_authorization_gate.py",
                "decide",
                "--envelope",
                str(envelope_path),
                "--manifest",
                str(manifest_path),
                "--gate-registry",
                str(registry_path),
                "--output",
                str(receipt_path),
            ],
            cwd=ROOT,
            check=True,
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert_outcome(receipt, "AUTO_CONTINUE")
        assert receipt_path.read_bytes() == gate.canonical_json_bytes(receipt) + b"\n"
        validated = subprocess.run(
            [
                sys.executable,
                "apps/codex_authorization_gate.py",
                "validate-receipt",
                "--receipt",
                str(receipt_path),
            ],
            cwd=ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        assert json.loads(validated.stdout)["valid"] is True


def main() -> int:
    canonical_and_envelope_contract_smoke()
    night_autoanswers_storage_independence_smoke()
    warning_and_evidence_smoke()
    submitted_ambiguity_smoke()
    human_reason_smoke()
    duplicate_and_answered_gate_smoke()
    malformed_and_unknown_fail_closed_smoke()
    cli_canonical_roundtrip_smoke()
    print("codex_authorization_gate_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
