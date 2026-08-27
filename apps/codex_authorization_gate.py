#!/usr/bin/env python3
"""Deterministic goal authorization and owner-gate routing.

The module is intentionally repository-only.  It compiles one accepted goal
into an immutable canonical envelope and classifies a proposed action without
performing the action or publishing a question.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


GOAL_SCHEMA = "wb-core.codex-accepted-goal/v1"
ENVELOPE_SCHEMA = "wb-core.codex-authorization-envelope/v1"
MANIFEST_SCHEMA = "wb-core.codex-action-manifest/v1"
GATE_REGISTRY_SCHEMA = "wb-core.codex-owner-gate-registry/v1"
RECEIPT_SCHEMA = "wb-core.codex-authorization-receipt/v1"
IMPLEMENTATION_INTENT = "IMPLEMENT_TO_COMPLETE"

OUTCOMES = {"AUTO_CONTINUE", "EVIDENCE_BLOCKED", "HUMAN_REQUIRED"}
SUBMIT_STATES = {"not_started", "submitted", "ambiguous", "reconciled"}
SUBMIT_INTENTS = {"none", "mutation", "query_only_reconcile"}
SEMANTIC_KINDS = {
    "technical",
    "operational_control_metadata",
    "business_semantic",
    "protected_business_fact",
}
EFFECTS = {
    "business",
    "financial",
    "external",
    "publication",
    "security_access",
    "destination",
    "credential_capability",
    "protected_data",
    "irreversible",
}
WARNING_RELATIONS = {"target", "unrelated", "stale"}
PUBLICATION_ACTIONS = {
    "NONE",
    "PUBLISH_ON_OWNER",
    "ROUTE_TO_OWNER",
    "SUPPRESS_DUPLICATE",
}

AUTO_REASONS = {
    "AUTHORIZED_GOAL_EFFECT",
    "AUTHORIZED_PRE_SUBMIT_CORRECTION",
    "ANSWERED_EXTENSION_COVERS_DELTA",
    "QUERY_ONLY_RECONCILIATION",
    "UNRELATED_WARNING_RECORDED",
    "STALE_WARNING_REFRESHED",
}
EVIDENCE_REASONS = {
    "INVALID_INPUT",
    "MISSING_IDENTITY",
    "MISSING_EVIDENCE",
    "DEPENDENCY_NOT_PROVEN",
    "DEPENDENCY_REMEDIATION_NOT_ALLOWLISTED",
    "SUBMIT_RECONCILIATION_REQUIRED",
    "STALE_OPERATION_ID",
    "TARGET_WARNING_REQUIRES_DIAGNOSIS",
    "UNAUTHORIZED_TECHNICAL_DELTA",
    "ANSWERED_GATE_REJECTED",
    "TERMINAL_DECISION_REUSED",
}
HUMAN_REASONS = {
    "NEW_BUSINESS_SEMANTIC",
    "NEW_FINAL_TARGET",
    "NEW_DESTINATION",
    "NEW_EXTERNAL_EFFECT",
    "NEW_PUBLICATION_EFFECT",
    "NEW_FINANCIAL_EFFECT",
    "NEW_SECURITY_ACCESS_EFFECT",
    "CREDENTIAL_CAPABILITY_REQUIRED",
    "NEW_PROTECTED_DATA_FINAL_DELTA",
    "NEW_IRREVERSIBLE_FINAL_DELTA",
}
ALL_REASONS = AUTO_REASONS | EVIDENCE_REASONS | HUMAN_REASONS

IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,159}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class AuthorizationInputError(ValueError):
    """A closed, machine-readable input validation error."""

    def __init__(self, code: str, pointer: str, message: str):
        super().__init__(message)
        self.code = code
        self.pointer = pointer


def canonical_json_bytes(value: Any) -> bytes:
    """Return the sole canonical JSON encoding used for hashes and artifacts."""

    _reject_non_json_scalars(value, pointer="$.")
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def compile_envelope(goal: Mapping[str, Any]) -> dict[str, Any]:
    """Compile accepted implementation intent into a canonical durable envelope."""

    normalized = _normalize_goal(goal)
    body = {
        "schema": ENVELOPE_SCHEMA,
        "goal_id": normalized["goal_id"],
        "goal_statement_digest": digest(normalized["goal_statement"]),
        "owner_surface_id": normalized["owner_surface_id"],
        "implementation_intent": IMPLEMENTATION_INTENT,
        "included_final_targets": normalized["included_final_targets"],
        "destinations": normalized["destinations"],
        "allowed_final_deltas": normalized["allowed_final_deltas"],
        "allowed_auxiliary_final_deltas": normalized["allowed_auxiliary_final_deltas"],
        "allowed_temporary_dependency_actions": normalized[
            "allowed_temporary_dependency_actions"
        ],
        "forbidden_effects": normalized["forbidden_effects"],
        "answered_decisions": normalized["answered_decisions"],
        "terminal_decision_digests": normalized["terminal_decision_digests"],
        "validity": {
            "until": "COMPLETE_OR_SUPERSEDED",
            "supersedes_goal_id": normalized["supersedes_goal_id"],
        },
    }
    return {**body, "envelope_digest": digest(body)}


def decide(
    envelope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    gate_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return exactly one closed deterministic outcome and a stable receipt."""

    raw_registry: Mapping[str, Any] = gate_registry or {
        "schema": GATE_REGISTRY_SCHEMA,
        "goal_id": envelope.get("goal_id") if isinstance(envelope, Mapping) else "",
        "owner_surface_id": (
            envelope.get("owner_surface_id") if isinstance(envelope, Mapping) else ""
        ),
        "records": [],
    }
    try:
        normalized_envelope = _normalize_envelope(envelope)
        normalized_manifest = _normalize_manifest(manifest)
        normalized_registry = _normalize_gate_registry(raw_registry)
        _validate_bindings(
            normalized_envelope, normalized_manifest, normalized_registry
        )
    except AuthorizationInputError as exc:
        return _invalid_receipt(exc, envelope, manifest, raw_registry)

    goal_id = normalized_envelope["goal_id"]
    owner_surface_id = normalized_envelope["owner_surface_id"]
    proposer_surface_id = normalized_manifest["proposer_surface_id"]
    warning_handling = _warning_handling(normalized_manifest["warnings"])
    evidence: list[dict[str, Any]] = []

    stale_operation = submit_state_terminal = (
        normalized_manifest["submit"]["state"] == "reconciled"
    )
    stale_operation = stale_operation or (
        normalized_manifest["submit"]["operation_id"]
        in normalized_manifest["submit"]["terminal_operation_ids"]
    )
    if stale_operation:
        return _receipt(
            outcome="EVIDENCE_BLOCKED",
            reasons=["STALE_OPERATION_ID"],
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=[
                {
                    "kind": "operation_identity",
                    "operation_id": normalized_manifest["submit"]["operation_id"],
                    "state": "reconciled" if submit_state_terminal else "terminal",
                }
            ],
            warning_handling=warning_handling,
        )

    submit = normalized_manifest["submit"]
    if submit["state"] in {"submitted", "ambiguous"}:
        if not _is_exact_query_only_reconciliation(normalized_manifest):
            return _receipt(
                outcome="EVIDENCE_BLOCKED",
                reasons=["SUBMIT_RECONCILIATION_REQUIRED"],
                goal_id=goal_id,
                owner_surface_id=owner_surface_id,
                proposer_surface_id=proposer_surface_id,
                evidence=[
                    {
                        "kind": "submit_state",
                        "operation_id": submit["operation_id"],
                        "state": submit["state"],
                        "allowed_next_action": "same-operation-query-only-reconcile",
                    }
                ],
                warning_handling=warning_handling,
            )
        return _receipt(
            outcome="AUTO_CONTINUE",
            reasons=["QUERY_ONLY_RECONCILIATION"],
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=[
                {
                    "kind": "submit_state",
                    "operation_id": submit["operation_id"],
                    "state": submit["state"],
                    "allowed_next_action": "same-operation-query-only-reconcile",
                }
            ],
            warning_handling=warning_handling,
        )

    dependency = normalized_manifest["dependency_proof"]
    if dependency["status"] == "missing":
        return _receipt(
            outcome="EVIDENCE_BLOCKED",
            reasons=["DEPENDENCY_NOT_PROVEN"],
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=[
                {
                    "kind": "dependency_proof",
                    "required_dependency_ids": dependency["required_dependency_ids"],
                    "status": "missing",
                }
            ],
            warning_handling=warning_handling,
        )

    allowed_final = {
        _descriptor_digest(item) for item in normalized_envelope["allowed_final_deltas"]
    }
    allowed_auxiliary = {
        _descriptor_digest(item)
        for item in normalized_envelope["allowed_auxiliary_final_deltas"]
    }
    allowed_temporary = {
        _temporary_descriptor_digest(item)
        for item in normalized_envelope["allowed_temporary_dependency_actions"]
    }

    unmatched: list[dict[str, Any]] = []
    for item in normalized_manifest["final_deltas"]:
        if _descriptor_digest(item) not in allowed_final:
            unmatched.append({"kind": "final_delta", "item": item})
    for item in normalized_manifest["auxiliary_final_deltas"]:
        if _descriptor_digest(item) not in allowed_auxiliary:
            unmatched.append({"kind": "auxiliary_final_delta", "item": item})
    unmatched_temporary: list[dict[str, Any]] = []
    for item in normalized_manifest["temporary_dependency_actions"]:
        if _temporary_descriptor_digest(item) not in allowed_temporary:
            wrapped = {"kind": "temporary_dependency_action", "item": item}
            unmatched.append(wrapped)
            unmatched_temporary.append(wrapped)

    if dependency["status"] == "failed":
        has_bounded_remediation = bool(
            normalized_manifest["auxiliary_final_deltas"]
            or normalized_manifest["temporary_dependency_actions"]
        )
        proof_complete = bool(
            dependency["preservation_predicates"] and dependency["readback_predicates"]
        )
        if not has_bounded_remediation or not proof_complete:
            return _receipt(
                outcome="EVIDENCE_BLOCKED",
                reasons=["MISSING_EVIDENCE"],
                goal_id=goal_id,
                owner_surface_id=owner_surface_id,
                proposer_surface_id=proposer_surface_id,
                evidence=[
                    {
                        "kind": "dependency_remediation",
                        "has_bounded_remediation": has_bounded_remediation,
                        "preservation_and_readback_proven": proof_complete,
                    }
                ],
                warning_handling=warning_handling,
            )

    unmatched_digests = sorted(_action_item_digest(item) for item in unmatched)
    accepted_coverage = _answered_coverage(normalized_envelope, normalized_registry)
    if unmatched_digests and set(unmatched_digests) <= accepted_coverage:
        evidence.extend(
            {"kind": item["kind"], "delta_digest": _action_item_digest(item)}
            for item in unmatched
        )
        return _receipt(
            outcome="AUTO_CONTINUE",
            reasons=["ANSWERED_EXTENSION_COVERS_DELTA"],
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=evidence,
            warning_handling=warning_handling,
        )
    if accepted_coverage:
        unmatched = [
            item
            for item in unmatched
            if _action_item_digest(item) not in accepted_coverage
        ]
        unmatched_temporary = [
            item
            for item in unmatched_temporary
            if _action_item_digest(item) not in accepted_coverage
        ]
        unmatched_digests = sorted(_action_item_digest(item) for item in unmatched)
    human_reasons: set[str] = set()
    non_human_unmatched: list[dict[str, Any]] = []
    for unmatched_item in unmatched:
        item = unmatched_item["item"]
        reasons = _human_reasons_for_item(
            unmatched_item["kind"], item, normalized_envelope
        )
        human_reasons.update(reasons)
        if not reasons:
            non_human_unmatched.append(unmatched_item)
        evidence.append(
            {
                "kind": unmatched_item["kind"],
                "delta_digest": _action_item_digest(unmatched_item),
                "target": item.get("target") or item.get("resource"),
                "destination": item.get("destination"),
                "effects": item["effects"],
                "reasons": sorted(reasons),
            }
        )

    if non_human_unmatched:
        reason = (
            "DEPENDENCY_REMEDIATION_NOT_ALLOWLISTED"
            if any(
                item["kind"] == "temporary_dependency_action"
                for item in non_human_unmatched
            )
            else "UNAUTHORIZED_TECHNICAL_DELTA"
        )
        return _receipt(
            outcome="EVIDENCE_BLOCKED",
            reasons=[reason],
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=evidence,
            warning_handling=warning_handling,
        )

    if human_reasons:
        decision_material = {
            "goal_id": goal_id,
            "owner_surface_id": owner_surface_id,
            "reason_codes": sorted(human_reasons),
            "delta_digests": unmatched_digests,
        }
        proposed_decision_digest = digest(decision_material)
        rejected_decisions = {
            record["decision_digest"]
            for record in normalized_registry["records"]
            if record["status"] == "answered" and record["resolution"] == "rejected"
        }
        if proposed_decision_digest in normalized_envelope["terminal_decision_digests"]:
            return _receipt(
                outcome="EVIDENCE_BLOCKED",
                reasons=["TERMINAL_DECISION_REUSED"],
                goal_id=goal_id,
                owner_surface_id=owner_surface_id,
                proposer_surface_id=proposer_surface_id,
                evidence=[
                    {
                        "kind": "terminal_decision",
                        "decision_digest": proposed_decision_digest,
                        "delta_digests": unmatched_digests,
                    }
                ],
                warning_handling=warning_handling,
            )
        if proposed_decision_digest in rejected_decisions:
            return _receipt(
                outcome="EVIDENCE_BLOCKED",
                reasons=["ANSWERED_GATE_REJECTED"],
                goal_id=goal_id,
                owner_surface_id=owner_surface_id,
                proposer_surface_id=proposer_surface_id,
                evidence=[
                    {
                        "kind": "answered_gate",
                        "decision_digest": proposed_decision_digest,
                        "delta_digests": unmatched_digests,
                    }
                ],
                warning_handling=warning_handling,
            )
        pending = {
            record["decision_digest"]
            for record in normalized_registry["records"]
            if record["status"] == "pending"
        }
        if proposed_decision_digest in pending:
            publication = {
                "action": "SUPPRESS_DUPLICATE",
                "surface_id": owner_surface_id,
            }
        elif proposer_surface_id != owner_surface_id:
            publication = {
                "action": "ROUTE_TO_OWNER",
                "surface_id": owner_surface_id,
            }
        else:
            publication = {
                "action": "PUBLISH_ON_OWNER",
                "surface_id": owner_surface_id,
            }
        return _receipt(
            outcome="HUMAN_REQUIRED",
            reasons=sorted(human_reasons),
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=evidence,
            warning_handling=warning_handling,
            publication=publication,
            decision_digest_override=proposed_decision_digest,
        )

    if unmatched_temporary:
        blocked_reason = "DEPENDENCY_REMEDIATION_NOT_ALLOWLISTED"
    elif unmatched:
        blocked_reason = "UNAUTHORIZED_TECHNICAL_DELTA"
    elif any(item["relation"] == "target" for item in normalized_manifest["warnings"]):
        blocked_reason = "TARGET_WARNING_REQUIRES_DIAGNOSIS"
    else:
        blocked_reason = ""
    if blocked_reason:
        return _receipt(
            outcome="EVIDENCE_BLOCKED",
            reasons=[blocked_reason],
            goal_id=goal_id,
            owner_surface_id=owner_surface_id,
            proposer_surface_id=proposer_surface_id,
            evidence=evidence,
            warning_handling=warning_handling,
        )

    auto_reasons = {"AUTHORIZED_GOAL_EFFECT"}
    if submit["state"] == "not_started" and submit["intent"] == "mutation":
        auto_reasons.add("AUTHORIZED_PRE_SUBMIT_CORRECTION")
    if warning_handling["record"]:
        auto_reasons.add("UNRELATED_WARNING_RECORDED")
    if warning_handling["refresh"]:
        auto_reasons.add("STALE_WARNING_REFRESHED")
    return _receipt(
        outcome="AUTO_CONTINUE",
        reasons=sorted(auto_reasons),
        goal_id=goal_id,
        owner_surface_id=owner_surface_id,
        proposer_surface_id=proposer_surface_id,
        evidence=evidence,
        warning_handling=warning_handling,
    )


def validate_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a receipt before any owner-facing publication is attempted."""

    normalized = _expect_mapping(receipt, "receipt")
    _expect_exact_keys(
        normalized,
        {
            "schema",
            "outcome",
            "reason_codes",
            "goal_id",
            "owner_surface_id",
            "proposer_surface_id",
            "evidence",
            "warning_handling",
            "publication",
            "decision_digest",
            "receipt_digest",
        },
        "receipt",
    )
    if normalized["schema"] != RECEIPT_SCHEMA:
        _raise("UNKNOWN_SCHEMA", "receipt.schema", "unknown receipt schema")
    outcome = normalized["outcome"]
    if outcome not in OUTCOMES:
        _raise("UNKNOWN_OUTCOME", "receipt.outcome", "unknown receipt outcome")
    reasons = _string_list(normalized["reason_codes"], "receipt.reason_codes")
    if normalized["reason_codes"] != reasons:
        _raise(
            "NON_CANONICAL_RECEIPT",
            "receipt.reason_codes",
            "receipt reasons are not canonically ordered",
        )
    if not reasons or not set(reasons) <= ALL_REASONS:
        _raise("UNKNOWN_REASON", "receipt.reason_codes", "unknown receipt reason")
    expected_reasons = {
        "AUTO_CONTINUE": AUTO_REASONS,
        "EVIDENCE_BLOCKED": EVIDENCE_REASONS,
        "HUMAN_REQUIRED": HUMAN_REASONS,
    }[outcome]
    if not set(reasons) <= expected_reasons:
        _raise(
            "REASON_OUTCOME_MISMATCH",
            "receipt.reason_codes",
            "receipt reason does not belong to outcome",
        )
    publication = _expect_mapping(normalized["publication"], "receipt.publication")
    _expect_exact_keys(publication, {"action", "surface_id"}, "receipt.publication")
    if publication["action"] not in PUBLICATION_ACTIONS:
        _raise(
            "UNKNOWN_PUBLICATION_ACTION",
            "receipt.publication.action",
            "unknown publication action",
        )
    if outcome != "HUMAN_REQUIRED" and publication["action"] != "NONE":
        _raise(
            "INVALID_PUBLICATION",
            "receipt.publication.action",
            "non-human outcome cannot publish or route a question",
        )
    if outcome == "HUMAN_REQUIRED" and publication["action"] == "NONE":
        _raise(
            "INVALID_PUBLICATION",
            "receipt.publication.action",
            "human outcome requires an explicit owner routing disposition",
        )
    _identity(normalized["goal_id"], "receipt.goal_id")
    _identity(normalized["owner_surface_id"], "receipt.owner_surface_id")
    _identity(normalized["proposer_surface_id"], "receipt.proposer_surface_id")
    _identity(publication["surface_id"], "receipt.publication.surface_id")
    evidence_value = normalized["evidence"]
    if not isinstance(evidence_value, list) or any(
        not isinstance(item, Mapping) for item in evidence_value
    ):
        _raise("INVALID_TYPE", "receipt.evidence", "receipt evidence must be objects")
    if evidence_value != sorted(evidence_value, key=canonical_json_bytes):
        _raise(
            "NON_CANONICAL_RECEIPT",
            "receipt.evidence",
            "receipt evidence is not canonically ordered",
        )
    warning_handling = _expect_mapping(
        normalized["warning_handling"], "receipt.warning_handling"
    )
    _expect_exact_keys(
        warning_handling, {"record", "refresh"}, "receipt.warning_handling"
    )
    for key in ("record", "refresh"):
        canonical_warnings = _identity_list(
            warning_handling[key], f"receipt.warning_handling.{key}"
        )
        if warning_handling[key] != canonical_warnings:
            _raise(
                "NON_CANONICAL_RECEIPT",
                f"receipt.warning_handling.{key}",
                "warning handling is not canonically ordered",
            )
    decision_digest = _digest_string(
        normalized["decision_digest"], "receipt.decision_digest"
    )
    receipt_digest = _digest_string(
        normalized["receipt_digest"], "receipt.receipt_digest"
    )
    body = {key: value for key, value in normalized.items() if key != "receipt_digest"}
    if digest(body) != receipt_digest:
        _raise("DIGEST_MISMATCH", "receipt.receipt_digest", "receipt digest mismatch")
    if outcome == "HUMAN_REQUIRED":
        evidence = normalized["evidence"]
        if not isinstance(evidence, list) or not any(
            isinstance(item, Mapping) and item.get("reasons") for item in evidence
        ):
            _raise(
                "MISSING_HUMAN_DELTA_EVIDENCE",
                "receipt.evidence",
                "human outcome lacks exact delta evidence",
            )
        expected_decision_digest = digest(
            {
                "goal_id": normalized["goal_id"],
                "owner_surface_id": normalized["owner_surface_id"],
                "reason_codes": reasons,
                "delta_digests": sorted(
                    item["delta_digest"]
                    for item in evidence
                    if isinstance(item, Mapping) and "delta_digest" in item
                ),
            }
        )
    else:
        expected_decision_digest = digest(
            {
                "outcome": outcome,
                "reason_codes": reasons,
                "goal_id": normalized["goal_id"],
                "owner_surface_id": normalized["owner_surface_id"],
                "evidence": normalized["evidence"],
            }
        )
    if decision_digest != expected_decision_digest:
        _raise(
            "DIGEST_MISMATCH",
            "receipt.decision_digest",
            "decision digest mismatch",
        )
    if publication["surface_id"] != normalized["owner_surface_id"]:
        _raise(
            "OWNER_BINDING_MISMATCH",
            "receipt.publication.surface_id",
            "publication is not bound to the owner surface",
        )
    if (
        publication["action"] == "PUBLISH_ON_OWNER"
        and normalized["proposer_surface_id"] != normalized["owner_surface_id"]
    ):
        _raise(
            "OWNER_BINDING_MISMATCH",
            "receipt.publication.action",
            "non-owner proposer cannot publish",
        )
    if (
        publication["action"] == "ROUTE_TO_OWNER"
        and normalized["proposer_surface_id"] == normalized["owner_surface_id"]
    ):
        _raise(
            "OWNER_BINDING_MISMATCH",
            "receipt.publication.action",
            "owner proposer does not route to itself",
        )
    return {
        "valid": True,
        "outcome": outcome,
        "decision_digest": decision_digest,
        "receipt_digest": receipt_digest,
    }


def write_canonical_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _normalize_goal(raw: Mapping[str, Any]) -> dict[str, Any]:
    goal = _expect_mapping(raw, "goal")
    keys = {
        "schema",
        "goal_id",
        "owner_surface_id",
        "implementation_intent",
        "goal_statement",
        "included_final_targets",
        "destinations",
        "allowed_final_deltas",
        "allowed_auxiliary_final_deltas",
        "allowed_temporary_dependency_actions",
        "forbidden_effects",
        "answered_decisions",
        "terminal_decision_digests",
        "supersedes_goal_id",
    }
    _expect_exact_keys(goal, keys, "goal")
    if goal["schema"] != GOAL_SCHEMA:
        _raise("UNKNOWN_SCHEMA", "goal.schema", "unknown accepted-goal schema")
    if goal["implementation_intent"] != IMPLEMENTATION_INTENT:
        _raise(
            "MISSING_IMPLEMENTATION_INTENT",
            "goal.implementation_intent",
            "accepted goal does not contain implementation-to-COMPLETE intent",
        )
    goal_id = _identity(goal["goal_id"], "goal.goal_id")
    owner = _identity(goal["owner_surface_id"], "goal.owner_surface_id")
    statement = _nonempty_text(goal["goal_statement"], "goal.goal_statement", 4000)
    targets = _identity_list(
        goal["included_final_targets"], "goal.included_final_targets"
    )
    destinations = _identity_list(goal["destinations"], "goal.destinations")
    if not targets or not destinations:
        _raise(
            "MISSING_IDENTITY",
            "goal",
            "goal requires at least one final target and destination",
        )
    allowed = _delta_descriptors(
        goal["allowed_final_deltas"], "goal.allowed_final_deltas"
    )
    auxiliary = _delta_descriptors(
        goal["allowed_auxiliary_final_deltas"],
        "goal.allowed_auxiliary_final_deltas",
    )
    temporary = _temporary_descriptors(
        goal["allowed_temporary_dependency_actions"],
        "goal.allowed_temporary_dependency_actions",
    )
    for pointer, items in (
        ("goal.allowed_final_deltas", allowed),
        ("goal.allowed_auxiliary_final_deltas", auxiliary),
    ):
        for index, item in enumerate(items):
            if item["target"] not in targets:
                _raise(
                    "UNBOUND_TARGET",
                    f"{pointer}[{index}].target",
                    "allowed delta target is not included",
                )
            if item["destination"] not in destinations:
                _raise(
                    "UNBOUND_DESTINATION",
                    f"{pointer}[{index}].destination",
                    "allowed delta destination is not included",
                )
    for index, item in enumerate(temporary):
        if item["destination"] not in destinations:
            _raise(
                "UNBOUND_DESTINATION",
                f"goal.allowed_temporary_dependency_actions[{index}].destination",
                "allowed temporary action destination is not included",
            )
    forbidden = _effect_list(goal["forbidden_effects"], "goal.forbidden_effects")
    allowed_effects = {
        effect
        for item in [*allowed, *auxiliary, *temporary]
        for effect in item["effects"]
    }
    conflict = sorted(set(forbidden) & allowed_effects)
    if conflict:
        _raise(
            "FORBIDDEN_EFFECT_CONFLICT",
            "goal.forbidden_effects",
            f"forbidden effects are allowlisted: {','.join(conflict)}",
        )
    answers = _answered_decisions(goal["answered_decisions"], "goal.answered_decisions")
    terminal = _digest_list(
        goal["terminal_decision_digests"], "goal.terminal_decision_digests"
    )
    supersedes = goal["supersedes_goal_id"]
    if supersedes is not None:
        supersedes = _identity(supersedes, "goal.supersedes_goal_id")
        if supersedes == goal_id:
            _raise(
                "INVALID_SUPERSESSION",
                "goal.supersedes_goal_id",
                "goal cannot supersede itself",
            )
    return {
        "schema": GOAL_SCHEMA,
        "goal_id": goal_id,
        "owner_surface_id": owner,
        "implementation_intent": IMPLEMENTATION_INTENT,
        "goal_statement": statement,
        "included_final_targets": targets,
        "destinations": destinations,
        "allowed_final_deltas": allowed,
        "allowed_auxiliary_final_deltas": auxiliary,
        "allowed_temporary_dependency_actions": temporary,
        "forbidden_effects": forbidden,
        "answered_decisions": answers,
        "terminal_decision_digests": terminal,
        "supersedes_goal_id": supersedes,
    }


def _normalize_envelope(raw: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _expect_mapping(raw, "envelope")
    keys = {
        "schema",
        "goal_id",
        "goal_statement_digest",
        "owner_surface_id",
        "implementation_intent",
        "included_final_targets",
        "destinations",
        "allowed_final_deltas",
        "allowed_auxiliary_final_deltas",
        "allowed_temporary_dependency_actions",
        "forbidden_effects",
        "answered_decisions",
        "terminal_decision_digests",
        "validity",
        "envelope_digest",
    }
    _expect_exact_keys(envelope, keys, "envelope")
    if envelope["schema"] != ENVELOPE_SCHEMA:
        _raise("UNKNOWN_SCHEMA", "envelope.schema", "unknown envelope schema")
    if envelope["implementation_intent"] != IMPLEMENTATION_INTENT:
        _raise(
            "MISSING_IMPLEMENTATION_INTENT",
            "envelope.implementation_intent",
            "envelope lost implementation-to-COMPLETE intent",
        )
    normalized = {
        "schema": ENVELOPE_SCHEMA,
        "goal_id": _identity(envelope["goal_id"], "envelope.goal_id"),
        "goal_statement_digest": _digest_string(
            envelope["goal_statement_digest"], "envelope.goal_statement_digest"
        ),
        "owner_surface_id": _identity(
            envelope["owner_surface_id"], "envelope.owner_surface_id"
        ),
        "implementation_intent": IMPLEMENTATION_INTENT,
        "included_final_targets": _identity_list(
            envelope["included_final_targets"], "envelope.included_final_targets"
        ),
        "destinations": _identity_list(
            envelope["destinations"], "envelope.destinations"
        ),
        "allowed_final_deltas": _delta_descriptors(
            envelope["allowed_final_deltas"], "envelope.allowed_final_deltas"
        ),
        "allowed_auxiliary_final_deltas": _delta_descriptors(
            envelope["allowed_auxiliary_final_deltas"],
            "envelope.allowed_auxiliary_final_deltas",
        ),
        "allowed_temporary_dependency_actions": _temporary_descriptors(
            envelope["allowed_temporary_dependency_actions"],
            "envelope.allowed_temporary_dependency_actions",
        ),
        "forbidden_effects": _effect_list(
            envelope["forbidden_effects"], "envelope.forbidden_effects"
        ),
        "answered_decisions": _answered_decisions(
            envelope["answered_decisions"], "envelope.answered_decisions"
        ),
        "terminal_decision_digests": _digest_list(
            envelope["terminal_decision_digests"],
            "envelope.terminal_decision_digests",
        ),
    }
    validity = _expect_mapping(envelope["validity"], "envelope.validity")
    _expect_exact_keys(validity, {"until", "supersedes_goal_id"}, "envelope.validity")
    if validity["until"] != "COMPLETE_OR_SUPERSEDED":
        _raise(
            "INVALID_VALIDITY",
            "envelope.validity.until",
            "envelope must remain valid until COMPLETE or superseded",
        )
    supersedes = validity["supersedes_goal_id"]
    if supersedes is not None:
        supersedes = _identity(supersedes, "envelope.validity.supersedes_goal_id")
    normalized["validity"] = {
        "until": "COMPLETE_OR_SUPERSEDED",
        "supersedes_goal_id": supersedes,
    }
    targets = set(normalized["included_final_targets"])
    destinations = set(normalized["destinations"])
    for pointer, items in (
        ("envelope.allowed_final_deltas", normalized["allowed_final_deltas"]),
        (
            "envelope.allowed_auxiliary_final_deltas",
            normalized["allowed_auxiliary_final_deltas"],
        ),
    ):
        for index, item in enumerate(items):
            if item["target"] not in targets:
                _raise(
                    "UNBOUND_TARGET",
                    f"{pointer}[{index}].target",
                    "allowed delta target is not included",
                )
            if item["destination"] not in destinations:
                _raise(
                    "UNBOUND_DESTINATION",
                    f"{pointer}[{index}].destination",
                    "allowed delta destination is not included",
                )
    for index, item in enumerate(normalized["allowed_temporary_dependency_actions"]):
        if item["destination"] not in destinations:
            _raise(
                "UNBOUND_DESTINATION",
                f"envelope.allowed_temporary_dependency_actions[{index}].destination",
                "allowed temporary action destination is not included",
            )
    allowed_effects = {
        effect
        for item in [
            *normalized["allowed_final_deltas"],
            *normalized["allowed_auxiliary_final_deltas"],
            *normalized["allowed_temporary_dependency_actions"],
        ]
        for effect in item["effects"]
    }
    if set(normalized["forbidden_effects"]) & allowed_effects:
        _raise(
            "FORBIDDEN_EFFECT_CONFLICT",
            "envelope.forbidden_effects",
            "forbidden effect is allowlisted",
        )
    supplied_digest = _digest_string(
        envelope["envelope_digest"], "envelope.envelope_digest"
    )
    if canonical_json_bytes(
        {**normalized, "envelope_digest": supplied_digest}
    ) != canonical_json_bytes(envelope):
        _raise(
            "NON_CANONICAL_ENVELOPE",
            "envelope",
            "envelope fields are not canonically normalized",
        )
    if digest(normalized) != supplied_digest:
        _raise(
            "DIGEST_MISMATCH",
            "envelope.envelope_digest",
            "envelope digest mismatch",
        )
    return {**normalized, "envelope_digest": supplied_digest}


def _normalize_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _expect_mapping(raw, "manifest")
    keys = {
        "schema",
        "goal_id",
        "proposer_surface_id",
        "action_id",
        "resources",
        "final_deltas",
        "auxiliary_final_deltas",
        "temporary_dependency_actions",
        "dependency_proof",
        "submit",
        "rollback_predicates",
        "readback_predicates",
        "warnings",
    }
    _expect_exact_keys(manifest, keys, "manifest")
    if manifest["schema"] != MANIFEST_SCHEMA:
        _raise("UNKNOWN_SCHEMA", "manifest.schema", "unknown action-manifest schema")
    resources = _resources(manifest["resources"], "manifest.resources")
    final_deltas = _action_deltas(manifest["final_deltas"], "manifest.final_deltas")
    auxiliary = _action_deltas(
        manifest["auxiliary_final_deltas"], "manifest.auxiliary_final_deltas"
    )
    temporary = _temporary_actions(
        manifest["temporary_dependency_actions"],
        "manifest.temporary_dependency_actions",
    )
    resource_ids = {item["id"] for item in resources}
    for pointer, item_resource in [
        *[("manifest.final_deltas", item["target"]) for item in final_deltas],
        *[("manifest.auxiliary_final_deltas", item["target"]) for item in auxiliary],
        *[
            ("manifest.temporary_dependency_actions", item["resource"])
            for item in temporary
        ],
    ]:
        if item_resource not in resource_ids:
            _raise(
                "UNBOUND_RESOURCE",
                pointer,
                f"action references undeclared resource: {item_resource}",
            )
    dependency = _dependency_proof(manifest["dependency_proof"])
    submit = _submit(manifest["submit"])
    rollback = _predicate_list(
        manifest["rollback_predicates"], "manifest.rollback_predicates"
    )
    readback = _predicate_list(
        manifest["readback_predicates"], "manifest.readback_predicates"
    )
    warnings = _warnings(manifest["warnings"], "manifest.warnings")
    if any(item["reversible"] for item in [*final_deltas, *auxiliary]) and not rollback:
        _raise(
            "MISSING_EVIDENCE",
            "manifest.rollback_predicates",
            "reversible final deltas require rollback predicates",
        )
    if (final_deltas or auxiliary or temporary) and not readback:
        _raise(
            "MISSING_EVIDENCE",
            "manifest.readback_predicates",
            "proposed action requires exact readback predicates",
        )
    return {
        "schema": MANIFEST_SCHEMA,
        "goal_id": _identity(manifest["goal_id"], "manifest.goal_id"),
        "proposer_surface_id": _identity(
            manifest["proposer_surface_id"], "manifest.proposer_surface_id"
        ),
        "action_id": _identity(manifest["action_id"], "manifest.action_id"),
        "resources": resources,
        "final_deltas": final_deltas,
        "auxiliary_final_deltas": auxiliary,
        "temporary_dependency_actions": temporary,
        "dependency_proof": dependency,
        "submit": submit,
        "rollback_predicates": rollback,
        "readback_predicates": readback,
        "warnings": warnings,
    }


def _normalize_gate_registry(raw: Mapping[str, Any]) -> dict[str, Any]:
    registry = _expect_mapping(raw, "gate_registry")
    _expect_exact_keys(
        registry,
        {"schema", "goal_id", "owner_surface_id", "records"},
        "gate_registry",
    )
    if registry["schema"] != GATE_REGISTRY_SCHEMA:
        _raise(
            "UNKNOWN_SCHEMA",
            "gate_registry.schema",
            "unknown owner-gate registry schema",
        )
    records_raw = registry["records"]
    if not isinstance(records_raw, list):
        _raise("INVALID_TYPE", "gate_registry.records", "records must be a list")
    records: list[dict[str, Any]] = []
    for index, raw_record in enumerate(records_raw):
        pointer = f"gate_registry.records[{index}]"
        record = _expect_mapping(raw_record, pointer)
        _expect_exact_keys(
            record,
            {
                "decision_digest",
                "status",
                "resolution",
                "covered_delta_digests",
                "publisher_surface_id",
            },
            pointer,
        )
        status = record["status"]
        resolution = record["resolution"]
        if status not in {"pending", "answered"}:
            _raise("UNKNOWN_GATE_STATUS", f"{pointer}.status", "unknown gate status")
        allowed_resolution = {
            "pending": {"unanswered"},
            "answered": {"accepted_extension", "rejected"},
        }[status]
        if resolution not in allowed_resolution:
            _raise(
                "UNKNOWN_GATE_RESOLUTION",
                f"{pointer}.resolution",
                "gate resolution does not match status",
            )
        covered = _digest_list(
            record["covered_delta_digests"], f"{pointer}.covered_delta_digests"
        )
        if resolution == "accepted_extension" and not covered:
            _raise(
                "MISSING_EVIDENCE",
                f"{pointer}.covered_delta_digests",
                "accepted extension requires covered delta digests",
            )
        records.append(
            {
                "decision_digest": _digest_string(
                    record["decision_digest"], f"{pointer}.decision_digest"
                ),
                "status": status,
                "resolution": resolution,
                "covered_delta_digests": covered,
                "publisher_surface_id": _identity(
                    record["publisher_surface_id"],
                    f"{pointer}.publisher_surface_id",
                ),
            }
        )
    records = _sorted_unique_objects(records, "gate_registry.records")
    return {
        "schema": GATE_REGISTRY_SCHEMA,
        "goal_id": _identity(registry["goal_id"], "gate_registry.goal_id"),
        "owner_surface_id": _identity(
            registry["owner_surface_id"], "gate_registry.owner_surface_id"
        ),
        "records": records,
    }


def _validate_bindings(
    envelope: Mapping[str, Any],
    manifest: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> None:
    if manifest["goal_id"] != envelope["goal_id"]:
        _raise("GOAL_BINDING_MISMATCH", "manifest.goal_id", "manifest goal mismatch")
    if registry["goal_id"] != envelope["goal_id"]:
        _raise(
            "GOAL_BINDING_MISMATCH",
            "gate_registry.goal_id",
            "gate registry goal mismatch",
        )
    if registry["owner_surface_id"] != envelope["owner_surface_id"]:
        _raise(
            "OWNER_BINDING_MISMATCH",
            "gate_registry.owner_surface_id",
            "gate registry owner mismatch",
        )
    if any(
        record["publisher_surface_id"] != envelope["owner_surface_id"]
        for record in registry["records"]
    ):
        _raise(
            "OWNER_BINDING_MISMATCH",
            "gate_registry.records",
            "gate registry contains a non-owner publication",
        )


def _is_exact_query_only_reconciliation(manifest: Mapping[str, Any]) -> bool:
    submit = manifest["submit"]
    return bool(
        submit["intent"] == "query_only_reconcile"
        and submit["submitted_operation_id"] == submit["operation_id"]
        and not manifest["final_deltas"]
        and not manifest["auxiliary_final_deltas"]
        and not manifest["temporary_dependency_actions"]
        and manifest["readback_predicates"]
    )


def _answered_coverage(
    envelope: Mapping[str, Any],
    registry: Mapping[str, Any],
) -> set[str]:
    accepted: set[str] = set()
    for answer in envelope["answered_decisions"]:
        accepted.update(answer["covered_delta_digests"])
    for record in registry["records"]:
        if record["status"] != "answered":
            continue
        covered = set(record["covered_delta_digests"])
        if record["resolution"] == "accepted_extension":
            accepted.update(covered)
    return accepted


def _human_reasons_for_item(
    kind: str, item: Mapping[str, Any], envelope: Mapping[str, Any]
) -> set[str]:
    reasons: set[str] = set()
    if kind != "temporary_dependency_action":
        if item["target"] not in envelope["included_final_targets"]:
            reasons.add("NEW_FINAL_TARGET")
        if item["destination"] not in envelope["destinations"]:
            reasons.add("NEW_DESTINATION")
        if item["semantic_kind"] == "business_semantic":
            reasons.add("NEW_BUSINESS_SEMANTIC")
        if item["semantic_kind"] == "protected_business_fact":
            reasons.add("NEW_PROTECTED_DATA_FINAL_DELTA")
        if item["reversible"] is False:
            reasons.add("NEW_IRREVERSIBLE_FINAL_DELTA")
    effect_map = {
        "business": "NEW_BUSINESS_SEMANTIC",
        "external": "NEW_EXTERNAL_EFFECT",
        "publication": "NEW_PUBLICATION_EFFECT",
        "financial": "NEW_FINANCIAL_EFFECT",
        "security_access": "NEW_SECURITY_ACCESS_EFFECT",
        "credential_capability": "CREDENTIAL_CAPABILITY_REQUIRED",
        "protected_data": "NEW_PROTECTED_DATA_FINAL_DELTA",
        "irreversible": "NEW_IRREVERSIBLE_FINAL_DELTA",
        "destination": "NEW_DESTINATION",
    }
    reasons.update(effect_map[effect] for effect in item["effects"])
    return reasons


def _warning_handling(warnings: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    return {
        "record": sorted(
            item["id"] for item in warnings if item["relation"] == "unrelated"
        ),
        "refresh": sorted(
            item["id"] for item in warnings if item["relation"] == "stale"
        ),
    }


def _receipt(
    *,
    outcome: str,
    reasons: Sequence[str],
    goal_id: str,
    owner_surface_id: str,
    proposer_surface_id: str,
    evidence: Sequence[Mapping[str, Any]],
    warning_handling: Mapping[str, Any],
    publication: Mapping[str, str] | None = None,
    decision_digest_override: str | None = None,
) -> dict[str, Any]:
    reason_codes = sorted(set(reasons))
    if outcome not in OUTCOMES:
        raise AssertionError(f"unknown outcome: {outcome}")
    if not reason_codes or not set(reason_codes) <= ALL_REASONS:
        raise AssertionError(f"unknown reason set: {reason_codes}")
    sorted_evidence = sorted(
        (dict(item) for item in evidence), key=canonical_json_bytes
    )
    publication_value = dict(
        publication or {"action": "NONE", "surface_id": owner_surface_id}
    )
    decision_material = {
        "outcome": outcome,
        "reason_codes": reason_codes,
        "goal_id": goal_id,
        "owner_surface_id": owner_surface_id,
        "evidence": sorted_evidence,
    }
    body = {
        "schema": RECEIPT_SCHEMA,
        "outcome": outcome,
        "reason_codes": reason_codes,
        "goal_id": goal_id,
        "owner_surface_id": owner_surface_id,
        "proposer_surface_id": proposer_surface_id,
        "evidence": sorted_evidence,
        "warning_handling": {
            "record": sorted(warning_handling.get("record", [])),
            "refresh": sorted(warning_handling.get("refresh", [])),
        },
        "publication": publication_value,
        "decision_digest": decision_digest_override or digest(decision_material),
    }
    receipt = {**body, "receipt_digest": digest(body)}
    validate_receipt(receipt)
    return receipt


def _invalid_receipt(
    error: AuthorizationInputError,
    envelope: Any,
    manifest: Any,
    registry: Any,
) -> dict[str, Any]:
    goal_id = (
        _safe_identity_from(envelope, "goal_id")
        or _safe_identity_from(manifest, "goal_id")
        or "unknown-goal"
    )
    owner = _safe_identity_from(envelope, "owner_surface_id") or "unknown-owner"
    proposer = (
        _safe_identity_from(manifest, "proposer_surface_id") or "unknown-proposer"
    )
    evidence = [
        {
            "kind": "input_error",
            "error_code": error.code,
            "pointer": error.pointer,
            "input_digest": digest(
                {
                    "envelope": _safe_json_value(envelope),
                    "manifest": _safe_json_value(manifest),
                    "gate_registry": _safe_json_value(registry),
                }
            ),
        }
    ]
    return _receipt(
        outcome="EVIDENCE_BLOCKED",
        reasons=["INVALID_INPUT"],
        goal_id=goal_id,
        owner_surface_id=owner,
        proposer_surface_id=proposer,
        evidence=evidence,
        warning_handling={"record": [], "refresh": []},
    )


def _resources(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        _raise("MISSING_IDENTITY", pointer, "resources must be a non-empty list")
    resources: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_pointer = f"{pointer}[{index}]"
        item = _expect_mapping(raw, item_pointer)
        _expect_exact_keys(item, {"id", "role", "storage_medium"}, item_pointer)
        role = item["role"]
        if role not in {"target", "dependency", "evidence"}:
            _raise(
                "UNKNOWN_RESOURCE_ROLE", f"{item_pointer}.role", "unknown resource role"
            )
        resources.append(
            {
                "id": _identity(item["id"], f"{item_pointer}.id"),
                "role": role,
                # Deliberately carried for audit only; classification never reads it.
                "storage_medium": _nonempty_text(
                    item["storage_medium"], f"{item_pointer}.storage_medium", 160
                ),
            }
        )
    return _sorted_unique_objects(resources, pointer)


def _delta_descriptors(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", pointer, "delta descriptors must be a list")
    result = [
        _delta_descriptor(item, f"{pointer}[{index}]")
        for index, item in enumerate(value)
    ]
    return _sorted_unique_objects(result, pointer)


def _delta_descriptor(value: Any, pointer: str) -> dict[str, Any]:
    item = _expect_mapping(value, pointer)
    keys = {
        "target",
        "destination",
        "semantic_kind",
        "operation",
        "effects",
        "reversible",
    }
    _expect_exact_keys(item, keys, pointer)
    semantic_kind = item["semantic_kind"]
    if semantic_kind not in SEMANTIC_KINDS:
        _raise(
            "UNKNOWN_SEMANTIC_KIND", f"{pointer}.semantic_kind", "unknown semantic kind"
        )
    effects = _effect_list(item["effects"], f"{pointer}.effects")
    reversible = item["reversible"]
    if not isinstance(reversible, bool):
        _raise("INVALID_TYPE", f"{pointer}.reversible", "reversible must be boolean")
    if (not reversible) != ("irreversible" in effects):
        _raise(
            "IRREVERSIBILITY_MISMATCH",
            pointer,
            "irreversible effect must exactly match reversible=false",
        )
    if semantic_kind == "protected_business_fact" and "protected_data" not in effects:
        _raise(
            "PROTECTED_DATA_MISMATCH",
            pointer,
            "protected business fact requires protected_data effect",
        )
    return {
        "target": _identity(item["target"], f"{pointer}.target"),
        "destination": _identity(item["destination"], f"{pointer}.destination"),
        "semantic_kind": semantic_kind,
        "operation": _identity(item["operation"], f"{pointer}.operation"),
        "effects": effects,
        "reversible": reversible,
    }


def _action_deltas(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", pointer, "action deltas must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_pointer = f"{pointer}[{index}]"
        item = _expect_mapping(raw, item_pointer)
        _expect_exact_keys(
            item,
            {
                "target",
                "destination",
                "semantic_kind",
                "operation",
                "effects",
                "reversible",
                "before_digest",
                "after_digest",
            },
            item_pointer,
        )
        descriptor = _delta_descriptor(
            {
                key: item[key]
                for key in item
                if key not in {"before_digest", "after_digest"}
            },
            item_pointer,
        )
        before = _digest_string(item["before_digest"], f"{item_pointer}.before_digest")
        after = _digest_string(item["after_digest"], f"{item_pointer}.after_digest")
        if before == after:
            _raise(
                "NO_FINAL_DELTA", item_pointer, "before and after digests are identical"
            )
        result.append({**descriptor, "before_digest": before, "after_digest": after})
    return _sorted_unique_objects(result, pointer)


def _temporary_descriptors(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", pointer, "temporary descriptors must be a list")
    result = [
        _temporary_descriptor(item, f"{pointer}[{index}]")
        for index, item in enumerate(value)
    ]
    return _sorted_unique_objects(result, pointer)


def _temporary_descriptor(value: Any, pointer: str) -> dict[str, Any]:
    item = _expect_mapping(value, pointer)
    keys = {
        "resource",
        "destination",
        "dependency_id",
        "operation",
        "effects",
        "bounded",
        "preservation_predicates",
        "readback_predicates",
    }
    _expect_exact_keys(item, keys, pointer)
    if item["bounded"] is not True:
        _raise(
            "UNBOUNDED_TEMPORARY_ACTION",
            f"{pointer}.bounded",
            "temporary action must be bounded",
        )
    preservation = _predicate_list(
        item["preservation_predicates"], f"{pointer}.preservation_predicates"
    )
    readback = _predicate_list(
        item["readback_predicates"], f"{pointer}.readback_predicates"
    )
    if not preservation or not readback:
        _raise(
            "MISSING_EVIDENCE",
            pointer,
            "temporary action requires preservation and readback predicates",
        )
    return {
        "resource": _identity(item["resource"], f"{pointer}.resource"),
        "destination": _identity(item["destination"], f"{pointer}.destination"),
        "dependency_id": _identity(item["dependency_id"], f"{pointer}.dependency_id"),
        "operation": _identity(item["operation"], f"{pointer}.operation"),
        "effects": _effect_list(item["effects"], f"{pointer}.effects"),
        "bounded": True,
        "preservation_predicates": preservation,
        "readback_predicates": readback,
    }


def _temporary_actions(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", pointer, "temporary actions must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_pointer = f"{pointer}[{index}]"
        item = _expect_mapping(raw, item_pointer)
        _expect_exact_keys(
            item,
            {
                "resource",
                "destination",
                "dependency_id",
                "operation",
                "effects",
                "bounded",
                "preservation_predicates",
                "readback_predicates",
                "identity_digest",
            },
            item_pointer,
        )
        descriptor = _temporary_descriptor(
            {key: item[key] for key in item if key != "identity_digest"}, item_pointer
        )
        result.append(
            {
                **descriptor,
                "identity_digest": _digest_string(
                    item["identity_digest"], f"{item_pointer}.identity_digest"
                ),
            }
        )
    return _sorted_unique_objects(result, pointer)


def _dependency_proof(value: Any) -> dict[str, Any]:
    pointer = "manifest.dependency_proof"
    item = _expect_mapping(value, pointer)
    _expect_exact_keys(
        item,
        {
            "status",
            "required_dependency_ids",
            "evidence_digests",
            "preservation_predicates",
            "readback_predicates",
        },
        pointer,
    )
    status = item["status"]
    if status not in {"satisfied", "missing", "failed"}:
        _raise(
            "UNKNOWN_DEPENDENCY_STATUS",
            f"{pointer}.status",
            "unknown dependency status",
        )
    required = _identity_list(
        item["required_dependency_ids"], f"{pointer}.required_dependency_ids"
    )
    evidence = _digest_list(item["evidence_digests"], f"{pointer}.evidence_digests")
    if required and status in {"satisfied", "failed"} and not evidence:
        _raise(
            "MISSING_EVIDENCE",
            f"{pointer}.evidence_digests",
            "dependency evidence is missing",
        )
    return {
        "status": status,
        "required_dependency_ids": required,
        "evidence_digests": evidence,
        "preservation_predicates": _predicate_list(
            item["preservation_predicates"], f"{pointer}.preservation_predicates"
        ),
        "readback_predicates": _predicate_list(
            item["readback_predicates"], f"{pointer}.readback_predicates"
        ),
    }


def _submit(value: Any) -> dict[str, Any]:
    pointer = "manifest.submit"
    item = _expect_mapping(value, pointer)
    _expect_exact_keys(
        item,
        {
            "state",
            "intent",
            "operation_id",
            "submitted_operation_id",
            "terminal_operation_ids",
        },
        pointer,
    )
    state = item["state"]
    intent = item["intent"]
    if state not in SUBMIT_STATES:
        _raise("UNKNOWN_SUBMIT_STATE", f"{pointer}.state", "unknown submit state")
    if intent not in SUBMIT_INTENTS:
        _raise("UNKNOWN_SUBMIT_INTENT", f"{pointer}.intent", "unknown submit intent")
    operation = _identity(item["operation_id"], f"{pointer}.operation_id")
    submitted = item["submitted_operation_id"]
    if submitted is not None:
        submitted = _identity(submitted, f"{pointer}.submitted_operation_id")
    if state == "not_started" and submitted is not None:
        _raise(
            "SUBMIT_STATE_MISMATCH",
            f"{pointer}.submitted_operation_id",
            "not_started operation cannot have submitted identity",
        )
    if state in {"submitted", "ambiguous", "reconciled"} and submitted != operation:
        _raise(
            "SUBMIT_STATE_MISMATCH",
            f"{pointer}.submitted_operation_id",
            "post-submit state must bind the same operation identity",
        )
    return {
        "state": state,
        "intent": intent,
        "operation_id": operation,
        "submitted_operation_id": submitted,
        "terminal_operation_ids": _identity_list(
            item["terminal_operation_ids"], f"{pointer}.terminal_operation_ids"
        ),
    }


def _warnings(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", pointer, "warnings must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_pointer = f"{pointer}[{index}]"
        item = _expect_mapping(raw, item_pointer)
        _expect_exact_keys(item, {"id", "relation", "evidence_digest"}, item_pointer)
        relation = item["relation"]
        if relation not in WARNING_RELATIONS:
            _raise(
                "UNKNOWN_WARNING_RELATION",
                f"{item_pointer}.relation",
                "unknown warning relation",
            )
        result.append(
            {
                "id": _identity(item["id"], f"{item_pointer}.id"),
                "relation": relation,
                "evidence_digest": _digest_string(
                    item["evidence_digest"], f"{item_pointer}.evidence_digest"
                ),
            }
        )
    return _sorted_unique_objects(result, pointer)


def _answered_decisions(value: Any, pointer: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        _raise("INVALID_TYPE", pointer, "answered decisions must be a list")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        item_pointer = f"{pointer}[{index}]"
        item = _expect_mapping(raw, item_pointer)
        _expect_exact_keys(
            item,
            {"decision_digest", "resolution", "covered_delta_digests"},
            item_pointer,
        )
        if item["resolution"] != "accepted_extension":
            _raise(
                "UNKNOWN_GATE_RESOLUTION",
                f"{item_pointer}.resolution",
                "envelope may carry only accepted extensions",
            )
        covered = _digest_list(
            item["covered_delta_digests"], f"{item_pointer}.covered_delta_digests"
        )
        if not covered:
            _raise(
                "MISSING_EVIDENCE",
                f"{item_pointer}.covered_delta_digests",
                "accepted extension requires exact covered deltas",
            )
        result.append(
            {
                "decision_digest": _digest_string(
                    item["decision_digest"], f"{item_pointer}.decision_digest"
                ),
                "resolution": "accepted_extension",
                "covered_delta_digests": covered,
            }
        )
    return _sorted_unique_objects(result, pointer)


def _descriptor_digest(item: Mapping[str, Any]) -> str:
    return digest(
        {
            key: item[key]
            for key in (
                "target",
                "destination",
                "semantic_kind",
                "operation",
                "effects",
                "reversible",
            )
        }
    )


def _temporary_descriptor_digest(item: Mapping[str, Any]) -> str:
    return digest(
        {
            key: item[key]
            for key in (
                "resource",
                "destination",
                "dependency_id",
                "operation",
                "effects",
                "bounded",
                "preservation_predicates",
                "readback_predicates",
            )
        }
    )


def _action_item_digest(item: Mapping[str, Any]) -> str:
    return digest(item)


def _effect_list(value: Any, pointer: str) -> list[str]:
    effects = _string_list(value, pointer)
    unknown = sorted(set(effects) - EFFECTS)
    if unknown:
        _raise("UNKNOWN_EFFECT", pointer, f"unknown effects: {','.join(unknown)}")
    return effects


def _identity(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or IDENTITY_RE.fullmatch(value) is None:
        _raise("MISSING_IDENTITY", pointer, "identity is missing or invalid")
    return value


def _identity_list(value: Any, pointer: str) -> list[str]:
    values = _string_list(value, pointer)
    for index, item in enumerate(values):
        _identity(item, f"{pointer}[{index}]")
    return values


def _digest_string(value: Any, pointer: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        _raise("MISSING_EVIDENCE", pointer, "sha256 digest is missing or invalid")
    return value


def _digest_list(value: Any, pointer: str) -> list[str]:
    values = _string_list(value, pointer)
    for index, item in enumerate(values):
        _digest_string(item, f"{pointer}[{index}]")
    return values


def _predicate_list(value: Any, pointer: str) -> list[str]:
    values = _string_list(value, pointer)
    for index, item in enumerate(values):
        _identity(item, f"{pointer}[{index}]")
    return values


def _string_list(value: Any, pointer: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _raise("INVALID_TYPE", pointer, "value must be a list of strings")
    normalized = sorted(value)
    if len(set(normalized)) != len(normalized):
        _raise("DUPLICATE_VALUE", pointer, "duplicate list value")
    return normalized


def _nonempty_text(value: Any, pointer: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _raise("INVALID_TEXT", pointer, "text is empty or exceeds the bound")
    return value.strip()


def _expect_mapping(value: Any, pointer: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _raise("INVALID_TYPE", pointer, "value must be an object")
    if any(not isinstance(key, str) for key in value):
        _raise("INVALID_KEY", pointer, "object keys must be strings")
    return value


def _expect_exact_keys(
    value: Mapping[str, Any], expected: set[str], pointer: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        _raise(
            "SCHEMA_FIELDS_MISMATCH",
            pointer,
            f"missing={missing}; unknown={unknown}",
        )


def _sorted_unique_objects(
    value: Iterable[dict[str, Any]], pointer: str
) -> list[dict[str, Any]]:
    result = sorted(value, key=canonical_json_bytes)
    encoded = [canonical_json_bytes(item) for item in result]
    if len(set(encoded)) != len(encoded):
        _raise("DUPLICATE_VALUE", pointer, "duplicate object")
    return result


def _safe_identity_from(value: Any, key: str) -> str:
    if isinstance(value, Mapping):
        candidate = value.get(key)
        if isinstance(candidate, str) and IDENTITY_RE.fullmatch(candidate):
            return candidate
    return ""


def _safe_json_value(value: Any) -> Any:
    try:
        canonical_json_bytes(value)
    except (AuthorizationInputError, TypeError, ValueError):
        return {"unserializable_type": type(value).__name__}
    return value


def _reject_non_json_scalars(value: Any, pointer: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        _raise(
            "FLOAT_FORBIDDEN", pointer, "floating-point values are not canonical inputs"
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                _raise("INVALID_KEY", pointer, "object keys must be strings")
            _reject_non_json_scalars(item, f"{pointer}{key}.")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_non_json_scalars(item, f"{pointer}[{index}].")
        return
    _raise(
        "INVALID_TYPE", pointer, f"unsupported canonical type: {type(value).__name__}"
    )


def _raise(code: str, pointer: str, message: str) -> None:
    raise AuthorizationInputError(code, pointer, message)


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _raise("MALFORMED_JSON", str(path), str(exc))
    return _expect_mapping(value, str(path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    compile_parser = subparsers.add_parser("compile-envelope")
    compile_parser.add_argument("--goal", required=True, type=Path)
    compile_parser.add_argument("--output", required=True, type=Path)

    decide_parser = subparsers.add_parser("decide")
    decide_parser.add_argument("--envelope", required=True, type=Path)
    decide_parser.add_argument("--manifest", required=True, type=Path)
    decide_parser.add_argument("--gate-registry", type=Path)
    decide_parser.add_argument("--output", required=True, type=Path)

    validate_parser = subparsers.add_parser("validate-receipt")
    validate_parser.add_argument("--receipt", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "compile-envelope":
        write_canonical_json(args.output, compile_envelope(_load_json(args.goal)))
        return 0
    if args.command == "decide":
        try:
            envelope = _load_json(args.envelope)
            manifest = _load_json(args.manifest)
            registry = _load_json(args.gate_registry) if args.gate_registry else None
            receipt = decide(envelope, manifest, registry)
        except AuthorizationInputError as exc:
            receipt = _invalid_receipt(exc, {}, {}, {})
        write_canonical_json(args.output, receipt)
        return 0
    validation = validate_receipt(_load_json(args.receipt))
    print(canonical_json_bytes(validation).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
