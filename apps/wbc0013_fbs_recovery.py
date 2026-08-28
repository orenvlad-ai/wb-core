#!/usr/bin/env python3
"""Exact, inert-by-default WBC0013 dense-A then historical-B adapter."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_dense_fbs import _write_private  # noqa: E402
from apps.registry_upload_http_entrypoint_hosted_runtime import (  # noqa: E402
    ACTIVE_HOSTED_RUNTIME_TARGET_ID,
    load_hosted_runtime_target,
)
from packages.application.business_data_write_barrier import barrier_status  # noqa: E402
from packages.application.ff_pool_dense_fbs import (  # noqa: E402
    DenseFbsService,
    ZERO_REPAIR_MANIFEST_SCHEMA,
    _fingerprint,
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
    MAX_FUNCTIONAL_BALANCE_ROWS,
    REPAIRABLE,
    WarehouseFbsMaterialRematerializer,
    historical_ready_shape_evidence,
)


EXPECTED_ROSTER = 71
EXPECTED_EXISTING = 21
EXPECTED_HISTORICAL = 12
EXPECTED_NO_MATERIAL_HISTORY = 38
EXPECTED_INSERTS = 50


class Wbc0013CliError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details


def _selection_details(
    *,
    predicate: str,
    expected_cardinality: int,
    candidates: list[Mapping[str, Any]],
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one bounded, digest-bound cardinality diagnostic."""

    bounded_candidates = [dict(item) for item in candidates[:20]]
    material = {
        "predicate": str(predicate),
        "expected_cardinality": int(expected_cardinality),
        "observed_cardinality": len(candidates),
        "candidate_digest": _fingerprint(candidates),
        "bounded_candidates": bounded_candidates,
        "bounded_candidate_count": len(bounded_candidates),
        "candidate_details_digest": _fingerprint(bounded_candidates),
        **dict(details or {}),
    }
    material["details_digest"] = _fingerprint(material)
    return material


def _selection_error(
    code: str,
    message: str,
    *,
    predicate: str,
    expected_cardinality: int,
    candidates: list[Mapping[str, Any]],
    details: Mapping[str, Any] | None = None,
) -> Wbc0013CliError:
    return Wbc0013CliError(
        code,
        message,
        details=_selection_details(
            predicate=predicate,
            expected_cardinality=expected_cardinality,
            candidates=candidates,
            details=details,
        ),
    )


def _loads_mapping(value: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}


def _nonmaterial_missing_component(component: sqlite3.Row) -> bool:
    provenance = _loads_mapping(component[6])
    source = str(provenance.get("source") or "")
    semantic = str(provenance.get("semantic") or "")
    return (
        str(component[1]) == "missing"
        and component[2] is None
        and source in {"", "accepted_inventory_history"}
        and semantic in {"", "no_material_value_history"}
    )


def _zero_component_material(component: sqlite3.Row) -> dict[str, Any]:
    return {
        "nm_id": int(component[0]),
        "state": str(component[1]),
        "quantity": component[2],
        "source_revision": str(component[3] or ""),
        "source_digest": str(component[4] or ""),
        "source_watermark": str(component[5] or ""),
        "provenance": _loads_mapping(component[6]),
    }


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _common(args: argparse.Namespace) -> dict[str, Any]:
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    operation_id = str(args.operation_id or "")
    if (
        re.fullmatch(r"production-goal-v1-[0-9a-f]{32}", operation_id) is None
        or evidence_dir.name != operation_id
        or evidence_dir.parent.name != "production-goals"
        or not evidence_dir.is_dir()
        or evidence_dir.stat().st_mode & 0o777 != 0o700
    ):
        raise ValueError("evidence directory must already be private mode 0700")
    target_file = Path(args.target_file).expanduser().resolve()
    target = load_hosted_runtime_target(target_file)
    target_runtime_dir = (
        Path(str(target.runtime_env.get("REGISTRY_UPLOAD_RUNTIME_DIR") or ""))
        .expanduser()
        .resolve()
    )
    deployed_sha = str(args.deployed_sha or "").strip().lower()
    if not (
        target.target_status == "active"
        and target.target_id == ACTIVE_HOSTED_RUNTIME_TARGET_ID
        and target.target_role == "primary_live"
        and target.target_lifecycle == "current_live"
        and target_runtime_dir == runtime_dir
        and re.fullmatch(r"[0-9a-f]{40}", deployed_sha)
    ):
        raise ValueError("exact active hosted runtime or deployed SHA is invalid")
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
        raise ValueError("canonical deployed SHA markers changed")
    registry = StoreRegistry(runtime_dir)
    generation = registry.load(require_files=True)
    if generation.implicit:
        raise ValueError("WBC0013 requires one explicit StoreRegistry generation")
    db_path = registry.resolve("operational", manifest=generation)
    with registry.session(
        "operational",
        mode="ro",
        operation=f"wbc0013_{args.phase}",
        manifest=generation,
    ) as conn:
        query_only = bool(int(conn.execute("PRAGMA query_only").fetchone()[0]))
    canonical_target = {
        "accepted": True,
        "target_id": target.target_id,
        "target_status": target.target_status,
        "target_role": target.target_role,
        "target_lifecycle": target.target_lifecycle,
        "runtime_dir": str(runtime_dir),
        "target_file_sha256": _sha_file(target_file),
        "deployed_sha": deployed_sha,
    }
    storage_generation = {
        "implicit": False,
        "query_only": query_only,
        "manifest_sha256": generation.manifest_sha256,
        "state": generation.state,
        "canonical_source": generation.canonical_source,
        "generation_epoch": generation.generation_epoch,
        "operational_generation_id": generation.operational.generation_id,
        "operational_schema_revision": generation.operational.schema_revision,
        "operational_relative_path": generation.operational.relative_path,
        "manifest_fingerprint": _fingerprint(manifest_payload(generation)),
    }
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir=runtime_dir,
        operational_db_path=db_path,
        store_registry=registry,
    )
    return {
        "runtime_dir": runtime_dir,
        "evidence_dir": evidence_dir,
        "db_path": db_path,
        "deployed_sha": deployed_sha,
        "canonical_target": canonical_target,
        "storage_generation": storage_generation,
        "store_registry": registry,
        "store_registry_generation": generation,
        "runtime": runtime,
        "barrier": barrier_status(runtime_dir),
    }


def _discover_dense_manifest(conn: sqlite3.Connection) -> dict[str, Any]:
    roster = [
        int(row[0])
        for row in conn.execute(
            "SELECT nm_id FROM sheet_vitrina_v1_nomenclature_items "
            "WHERE is_active=1 AND is_hidden=0 AND nm_id IS NOT NULL AND nm_id>0 "
            "ORDER BY nm_id"
        ).fetchall()
    ]
    if len(roster) != EXPECTED_ROSTER or len(roster) != len(set(roster)):
        raise _selection_error(
            "dense_roster_cardinality_invalid",
            "WBC0013 active stock-managed roster is not exact shape 71",
            predicate="dense_a.active_stock_managed_roster_exact_71",
            expected_cardinality=EXPECTED_ROSTER,
            candidates=[{"nm_id": value} for value in roster],
        )
    candidates: list[dict[str, Any]] = []
    extension_diagnostics: list[dict[str, Any]] = []
    extensions = conn.execute(
        "SELECT extension.extension_id,extension.facility_id,"
        "extension.seller_warehouse_id,extension.official_office_id "
        "FROM sheet_vitrina_v1_ff_pool_fbs_mapping_extensions extension "
        "JOIN sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings mapping "
        "ON mapping.mapping_id=extension.warehouse_mapping_id "
        "WHERE mapping.active=1 ORDER BY extension.created_at DESC"
    ).fetchall()
    for extension in extensions:
        facility_id = str(extension[1])
        existing = [
            int(row[0])
            for row in conn.execute(
                "SELECT nm_id FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id=? AND pool='FBS' ORDER BY nm_id",
                (facility_id,),
            ).fetchall()
        ]
        allocations = [
            int(row[0])
            for row in conn.execute(
                "SELECT nm_id FROM sheet_vitrina_v1_ff_pool_fbs_mapping_extension_allocations "
                "WHERE extension_id=? ORDER BY nm_id",
                (str(extension[0]),),
            ).fetchall()
        ]
        targets = sorted(set(roster) - set(existing))
        if (
            len(existing) != EXPECTED_EXISTING
            or existing != allocations
            or len(targets) != EXPECTED_INSERTS
            or sorted((*existing, *targets)) != roster
        ):
            continue
        finalizations = conn.execute(
            "SELECT finalization.finalization_sequence,finalization.finalization_id,"
            "finalization.business_date,finalization.capture_id,"
            "finalization.finalization_identity,finalization.finalization_digest,"
            "finalization.supersedes_finalization_digest,finalization.finalized_at,"
            "capture.source_digest "
            "FROM sheet_vitrina_v1_inventory_history_finalizations finalization "
            "JOIN sheet_vitrina_v1_inventory_history_captures capture "
            "ON capture.capture_id=finalization.capture_id "
            "WHERE capture.business_date=finalization.business_date "
            "ORDER BY finalization.finalization_sequence DESC,"
            "finalization.finalization_id DESC"
        ).fetchall()
        distinct_finalizations: list[sqlite3.Row] = []
        duplicate_finalizations: list[dict[str, Any]] = []
        seen_captures: set[str] = set()
        for finalization in finalizations:
            capture_id = str(finalization[3])
            binding = {
                "finalization_sequence": int(finalization[0]),
                "finalization_id": str(finalization[1]),
                "business_date": str(finalization[2]),
                "capture_id": capture_id,
                "finalization_identity": str(finalization[4]),
                "finalization_digest": str(finalization[5]),
                "supersedes_finalization_digest": str(finalization[6] or ""),
                "finalized_at": str(finalization[7]),
                "capture_source_digest": str(finalization[8]),
            }
            if capture_id in seen_captures:
                duplicate_finalizations.append(binding)
                continue
            seen_captures.add(capture_id)
            distinct_finalizations.append(finalization)

        anchors: list[dict[str, Any]] = []
        partition_conflicts: list[dict[str, Any]] = []
        material_conflicts: list[dict[str, Any]] = []
        for finalization in distinct_finalizations:
            business_date = str(finalization[2])
            components = conn.execute(
                "SELECT nm_id,state,quantity,source_revision,source_digest,"
                "source_watermark,provenance_json "
                "FROM sheet_vitrina_v1_inventory_history_components "
                "WHERE capture_id=? AND scope_kind='SKU' AND nm_id IS NOT NULL "
                "AND component_kind='FBS_FACILITY' AND component_id=? "
                "ORDER BY nm_id",
                (str(finalization[3]), facility_id),
            ).fetchall()
            by_nm: dict[int, sqlite3.Row] = {}
            duplicates: list[int] = []
            for component in components:
                nm_id = int(component[0])
                if nm_id in by_nm:
                    duplicates.append(nm_id)
                    continue
                by_nm[nm_id] = component
            scoped = {nm_id: by_nm[nm_id] for nm_id in targets if nm_id in by_nm}
            if duplicates:
                material_conflicts.append(
                    {
                        "capture_id": str(finalization[3]),
                        "business_date": business_date,
                        "reason": "duplicate_target_components",
                        "nm_ids": sorted(set(duplicates))[:20],
                    }
                )
                continue
            exact_zero = sorted(
                nm_id
                for nm_id, component in scoped.items()
                if str(component[1]) == "exact_zero"
                and component[2] is not None
                and int(component[2]) == 0
                and _loads_mapping(component[6]).get("source")
                == "fbs_mapping_extension_allocation"
            )
            invalid = sorted(
                nm_id
                for nm_id, component in scoped.items()
                if nm_id not in exact_zero
                and not _nonmaterial_missing_component(component)
            )
            if invalid:
                material_conflicts.append(
                    {
                        "capture_id": str(finalization[3]),
                        "business_date": business_date,
                        "reason": "material_or_unaccepted_target_history",
                        "nm_ids": invalid[:20],
                        "component_digest": _fingerprint(
                            [_zero_component_material(scoped[nm_id]) for nm_id in invalid]
                        ),
                    }
                )
                continue
            if exact_zero and len(exact_zero) != EXPECTED_HISTORICAL:
                partition_conflicts.append(
                    {
                        "capture_id": str(finalization[3]),
                        "business_date": business_date,
                        "reason": "historical_zero_partition_cardinality",
                        "nm_ids": exact_zero[:20],
                        "partition_digest": _fingerprint(exact_zero),
                    }
                )
                continue
            if len(exact_zero) == EXPECTED_HISTORICAL:
                zero_rows = [
                    _zero_component_material(scoped[nm_id]) for nm_id in exact_zero
                ]
                anchors.append(
                    {
                        "finalization_sequence": int(finalization[0]),
                        "finalization_id": str(finalization[1]),
                        "business_date": business_date,
                        "capture_id": str(finalization[3]),
                        "finalization_identity": str(finalization[4]),
                        "finalization_digest": str(finalization[5]),
                        "capture_source_digest": str(finalization[8]),
                        "historical_exact_zero": exact_zero,
                        "historical_partition_digest": _fingerprint(exact_zero),
                        "historical_semantic_digest": _fingerprint(zero_rows),
                    }
                )
        anchor_partition_digests = sorted(
            {str(item["historical_partition_digest"]) for item in anchors}
        )
        anchor_semantic_digests = sorted(
            {str(item["historical_semantic_digest"]) for item in anchors}
        )
        if len(anchor_partition_digests) > 1 or len(anchor_semantic_digests) > 1:
            partition_conflicts.extend(
                {
                    "capture_id": str(item["capture_id"]),
                    "business_date": str(item["business_date"]),
                    "reason": "historical_anchor_partition_or_semantic_drift",
                    "partition_digest": str(item["historical_partition_digest"]),
                    "semantic_digest": str(item["historical_semantic_digest"]),
                }
                for item in anchors
            )
        diagnostic = {
            "extension_id": str(extension[0]),
            "facility_id": facility_id,
            "anchor_count": len(anchors),
            "anchor_digest": _fingerprint(anchors),
            "partition_conflict_count": len(partition_conflicts),
            "partition_conflict_digest": _fingerprint(partition_conflicts),
            "material_conflict_count": len(material_conflicts),
            "material_conflict_digest": _fingerprint(material_conflicts),
            "duplicate_finalization_count": len(duplicate_finalizations),
            "duplicate_finalization_digest": _fingerprint(duplicate_finalizations),
        }
        extension_diagnostics.append(diagnostic)
        if not anchors or partition_conflicts or material_conflicts:
            continue
        anchor = anchors[0]
        historical = list(anchor["historical_exact_zero"])
        absent = sorted(set(targets) - set(historical))
        if len(absent) != EXPECTED_NO_MATERIAL_HISTORY:
            continue
        candidates.append(
            {
                "schema": ZERO_REPAIR_MANIFEST_SCHEMA,
                "facility_id": facility_id,
                "seller_warehouse_id": int(extension[2]),
                "official_office_id": int(extension[3]),
                "historical_business_date": str(anchor["business_date"]),
                "partitions": {
                    "historical_exact_zero": historical,
                    "no_material_value_history": absent,
                },
                "expected_roster_nm_ids": roster,
                "expected_existing_nm_ids": existing,
                "discovery_anchor": anchor,
                "discovery_anchor_count": len(anchors),
                "discovery_anchor_digest": _fingerprint(anchors),
            }
        )
    if len(candidates) != 1:
        raise _selection_error(
            "dense_target_missing_or_ambiguous",
            "WBC0013 dense target discovery is missing or ambiguous",
            predicate="dense_a.exact_extension_with_stable_latest_historical_anchor",
            expected_cardinality=1,
            candidates=candidates,
            details={
                "extension_diagnostics": extension_diagnostics[:20],
                "extension_diagnostics_digest": _fingerprint(extension_diagnostics),
            },
        )
    return candidates[0]


def _dense_plan(context: Mapping[str, Any]) -> dict[str, Any]:
    uri = f"file:{Path(context['db_path']).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        manifest = _discover_dense_manifest(conn)
    partitions = manifest["partitions"]
    plan = DenseFbsService(
        db_path=Path(context["db_path"]),
        runtime_dir=Path(context["runtime_dir"]),
    ).build_zero_repair_plan(
        facility_id=str(manifest["facility_id"]),
        historical_exact_zero_nm_ids=partitions["historical_exact_zero"],
        no_material_value_history_nm_ids=partitions[
            "no_material_value_history"
        ],
        seller_warehouse_id=int(manifest["seller_warehouse_id"]),
        official_office_id=int(manifest["official_office_id"]),
        expected_roster_nm_ids=manifest["expected_roster_nm_ids"],
        expected_existing_nm_ids=manifest["expected_existing_nm_ids"],
        historical_business_date=str(manifest["historical_business_date"]),
        canonical_target=context["canonical_target"],
        storage_generation=context["storage_generation"],
    )
    expected_anchor = dict(manifest["discovery_anchor"])
    actual_history = dict(plan.get("historical_zero_evidence") or {})
    actual_finalization = dict(actual_history.get("latest_finalization") or {})
    stable = (
        str(actual_finalization.get("capture_id") or "")
        == str(expected_anchor.get("capture_id") or "")
        and str(actual_finalization.get("finalization_id") or "")
        == str(expected_anchor.get("finalization_id") or "")
        and str(actual_finalization.get("finalization_digest") or "")
        == str(expected_anchor.get("finalization_digest") or "")
        and str(actual_history.get("latest_original_partition_digest") or "")
        == str(expected_anchor.get("historical_semantic_digest") or "")
    )
    if not stable:
        raise _selection_error(
            "dense_discovery_anchor_drift",
            "WBC0013 dense historical anchor changed during qualification",
            predicate="dense_a.discovery_and_plan_anchor_identity_equal",
            expected_cardinality=1,
            candidates=[
                {
                    "capture_id": str(actual_finalization.get("capture_id") or ""),
                    "finalization_id": str(
                        actual_finalization.get("finalization_id") or ""
                    ),
                    "finalization_digest": str(
                        actual_finalization.get("finalization_digest") or ""
                    ),
                    "historical_semantic_digest": str(
                        actual_history.get("latest_original_partition_digest") or ""
                    ),
                }
            ],
            details={"expected_anchor": expected_anchor},
        )
    return plan


def _event_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT event_id,cutover_id,order_id,episode_sequence,event_type,"
        "source_order_observation_sequence,source_status_observation_sequence,"
        "source_revision,status_digest,supplier_status,wb_status,source_observed_at,"
        "facility_id,pool,nm_id,quantity,physical_quantity_delta,capital_delta_rub,"
        "frozen_wac_rub,evidence_digest,occurred_at "
        "FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events WHERE event_id=?",
        (event_id,),
    ).fetchone()


def _discover_historical_manifests(
    conn: sqlite3.Connection,
    *,
    canonical_target: Mapping[str, Any],
    storage_generation: Mapping[str, Any],
) -> dict[str, Any]:
    active = conn.execute(
        "SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1"
    ).fetchone()
    sync = conn.execute(
        "SELECT active_version_id FROM sheet_vitrina_v1_warehouse_wb_sync_status WHERE slot=1"
    ).fetchone()
    if active is None or sync is None or str(active[0]) != str(sync[0]):
        selection = _selection_details(
            predicate="historical_b.current_active_sync_identity",
            expected_cardinality=1,
            candidates=[],
            details={
                "active_version_id": str(active[0]) if active is not None else "",
                "sync_version_id": str(sync[0]) if sync is not None else "",
            },
        )
        return {"mismatches": [], "manifests": [], "selection": selection}
    mismatches: list[dict[str, Any]] = []
    ready_candidates: list[
        tuple[sqlite3.Row, dict[str, Any], dict[str, Any]]
    ] = []
    rows = conn.execute(
        "SELECT version.*,balance.nm_id,balance.quantity,balance.wac_rub,"
        "balance.capital_rub,balance.cost_covered_quantity,balance.provenance_json "
        "FROM sheet_vitrina_v1_warehouse_functional_versions version "
        "JOIN sheet_vitrina_v1_warehouse_functional_balances balance "
        "ON balance.version_id=version.version_id AND balance.warehouse_key='ff' "
        "WHERE version.status='good' AND balance.quantity>0 "
        "AND balance.cost_covered_quantity<>balance.quantity "
        "ORDER BY version.business_effective_date,balance.nm_id "
        "LIMIT ?",
        (MAX_FUNCTIONAL_BALANCE_ROWS + 1,),
    ).fetchall()
    if len(rows) > MAX_FUNCTIONAL_BALANCE_ROWS:
        broad_candidates = [
            {
                "version_id": str(row["version_id"]),
                "business_date": str(row["business_effective_date"]),
                "nm_id": int(row["nm_id"]),
            }
            for row in rows
        ]
        selection = _selection_details(
            predicate="historical_b.broad_mismatch_scope_within_bound",
            expected_cardinality=MAX_FUNCTIONAL_BALANCE_ROWS,
            candidates=broad_candidates,
        )
        return {"mismatches": broad_candidates, "manifests": [], "selection": selection}
    for joined in rows:
        version_id = str(joined["version_id"])
        nm_id = int(joined["nm_id"])
        target_provenance = _loads_mapping(joined["provenance_json"])
        ready = historical_ready_shape_evidence(
            conn,
            business_date=str(joined["business_effective_date"]),
            nm_id=nm_id,
        )
        mismatch = {
            "version_id": version_id,
            "business_date": str(joined["business_effective_date"]),
            "nm_id": nm_id,
            "quantity": str(joined["quantity"]),
            "cost_covered_quantity": str(joined["cost_covered_quantity"]),
            "wac_rub": str(joined["wac_rub"] or ""),
            "ready_shape_eligible": bool(ready["eligible"]),
            "ready_shape_digest": str(ready["fingerprint"]),
            "ready_shape_blockers": list(ready["blockers"]),
            "classification": (
                "ready_shape_candidate"
                if ready["eligible"]
                else "rejected_ready_shape_prerequisites"
            ),
        }
        mismatches.append(mismatch)
        if ready["eligible"]:
            ready_candidates.append((joined, target_provenance, mismatch))

    broad_details = {
        "broad_mismatch_count": len(mismatches),
        "broad_mismatch_digest": _fingerprint(mismatches),
        "bounded_local_rejections": [
            item for item in mismatches if not item["ready_shape_eligible"]
        ][:20],
    }
    ready_summaries = [item[2] for item in ready_candidates]
    if len(ready_candidates) != 1:
        selection = _selection_details(
            predicate="historical_b.exact_ready_shape_candidate",
            expected_cardinality=1,
            candidates=ready_summaries,
            details=broad_details,
        )
        return {"mismatches": mismatches, "manifests": [], "selection": selection}

    joined, target_provenance, selected_mismatch = ready_candidates[0]
    version_id = str(joined["version_id"])
    nm_id = int(joined["nm_id"])
    causal_facilities = sorted(
        {
            str(location.get("facility_id") or "")
            for record in target_provenance.get("source_records") or []
            if isinstance(record, Mapping)
            for location in record.get("locations") or []
            if isinstance(location, Mapping)
            and str(location.get("pool") or "").upper() == "FBS"
            and str(location.get("facility_id") or "")
        }
    )
    published_at = str(joined["published_at"])
    next_version = conn.execute(
        "SELECT published_at FROM sheet_vitrina_v1_warehouse_functional_versions "
        "WHERE status='good' AND published_at>? "
        "ORDER BY published_at,version_id LIMIT 1",
        (published_at,),
    ).fetchone()
    interval_end = str(next_version[0]) if next_version is not None else ""
    selected_mismatch["published_at_exclusive"] = published_at
    selected_mismatch["next_published_at_exclusive"] = interval_end
    selected_mismatch["causal_facilities"] = causal_facilities
    events: list[sqlite3.Row] = []
    if causal_facilities:
        placeholders = ",".join("?" for _ in causal_facilities)
        interval_clause = "AND occurred_at<?" if interval_end else ""
        event_parameters: list[Any] = [nm_id, *causal_facilities, published_at]
        if interval_end:
            event_parameters.append(interval_end)
        events = conn.execute(
            "SELECT event_id FROM sheet_vitrina_v1_ff_pool_fbs_lifecycle_events "
            f"WHERE nm_id=? AND facility_id IN ({placeholders}) AND pool='FBS' "
            "AND event_type='handoff_debit' AND physical_quantity_delta<0 "
            "AND capital_delta_rub<0 AND occurred_at>? "
            f"{interval_clause} ORDER BY occurred_at,event_id",
            tuple(event_parameters),
        ).fetchall()
    event_rows = [
        event
        for event in (_event_row(conn, str(item[0])) for item in events)
        if event is not None
    ]
    event_summaries = [
        {
            "event_id": str(event["event_id"]),
            "facility_id": str(event["facility_id"]),
            "occurred_at": str(event["occurred_at"]),
            "event_row_digest": _fingerprint(dict(event)),
        }
        for event in event_rows
    ]
    selected_mismatch["causal_event_count"] = len(event_rows)
    selected_mismatch["causal_event_ids_digest"] = _fingerprint(
        [item["event_id"] for item in event_summaries]
    )
    selected_mismatch["classification"] = (
        "selected_ready_shape_and_causal_event"
        if len(event_rows) == 1
        else "rejected_causal_event_cardinality"
    )
    if len(event_rows) != 1:
        selection = _selection_details(
            predicate="historical_b.exact_causal_handoff_debit_event",
            expected_cardinality=1,
            candidates=event_summaries,
            details={**broad_details, "ready_shape_candidate": selected_mismatch},
        )
        return {"mismatches": mismatches, "manifests": [], "selection": selection}

    source = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
        (version_id,),
    ).fetchone()
    target = conn.execute(
        "SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances "
        "WHERE version_id=? AND warehouse_key='ff' AND nm_id=?",
        (version_id, nm_id),
    ).fetchone()
    epoch = conn.execute(
        "SELECT epoch FROM sheet_vitrina_v1_ff_pool_feature_epochs ORDER BY epoch DESC LIMIT 1"
    ).fetchone()
    pool_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_pool_balances "
            "WHERE projection_epoch=? AND nm_id=? ORDER BY facility_id,pool,nm_id",
            (int(epoch[0]) if epoch is not None else 0, nm_id),
        ).fetchall()
    ]
    event = event_rows[0]
    manifest = {
        "schema": HISTORICAL_MANIFEST_SCHEMA,
        "business_date": str(source["business_effective_date"]),
        "facility_id": str(event["facility_id"]),
        "pool": "FBS",
        "nm_ids": [nm_id],
        "accepted_version_id": version_id,
        "accepted_version_plan_digest": str(source["plan_fingerprint"]),
        "accepted_version_row_digest": _fingerprint(dict(source)),
        "accepted_target_row_digest": _fingerprint(dict(target)),
        "accepted_provenance_digest": _fingerprint(target_provenance),
        "accepted_effective_at": str(source["effective_at"]),
        "accepted_published_at": str(source["published_at"]),
        "expected_current_active_version_id": str(active[0]),
        "expected_current_sync_version_id": str(sync[0]),
        "expected_current_pool_digest": _fingerprint(pool_rows),
        "event_id": str(event["event_id"]),
        "event_source_digest": _fingerprint(str(event["source_revision"])),
        "event_status_digest": str(event["status_digest"]),
        "event_evidence_digest": str(event["evidence_digest"]),
        "event_row_digest": _fingerprint(dict(event)),
        "event_quantity_delta": str(event["physical_quantity_delta"]),
        "event_capital_delta_rub": str(event["capital_delta_rub"]),
        "event_wac_rub": str(event["frozen_wac_rub"]),
        "event_occurred_at": str(event["occurred_at"]),
        "accepted_quantity": str(target["quantity"]),
        "accepted_cost_covered_quantity": str(target["cost_covered_quantity"]),
        "accepted_capital_rub": str(target["capital_rub"]),
        "canonical_target": dict(canonical_target),
        "storage_generation": dict(storage_generation),
    }
    selection = _selection_details(
        predicate="historical_b.exact_causal_handoff_debit_event",
        expected_cardinality=1,
        candidates=event_summaries,
        details={**broad_details, "ready_shape_candidate": selected_mismatch},
    )
    return {"mismatches": mismatches, "manifests": [manifest], "selection": selection}


def _historical_plan(
    context: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    service = WarehouseFbsMaterialRematerializer(
        runtime=context["runtime"],
        timestamp_factory=lambda: (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
    )
    registry = context["store_registry"]
    generation = context["store_registry_generation"]
    with registry.session(
        "operational",
        mode="ro",
        operation="wbc0013_historical_qualification",
        manifest=generation,
    ) as conn:
        conn.row_factory = sqlite3.Row
        if not bool(int(conn.execute("PRAGMA query_only").fetchone()[0])):
            raise ValueError("WBC0013 historical dependency session is not query-only")
        discovery = _discover_historical_manifests(
            conn,
            canonical_target=context["canonical_target"],
            storage_generation=context["storage_generation"],
        )
        selection = dict(discovery["selection"])
        if len(discovery["manifests"]) != 1:
            raise Wbc0013CliError(
                "historical_target_missing_or_ambiguous",
                "WBC0013 historical target discovery is missing or ambiguous",
                details=selection,
            )
        classified = [
            service.build_historical_plan(manifest, dependency_conn=conn)
            for manifest in discovery["manifests"]
        ]
        repairable = [plan for plan in classified if plan.get("status") == REPAIRABLE]
    qualification = {
        "fresh_mismatch_count": len(discovery["mismatches"]),
        "fresh_mismatch_digest": _fingerprint(discovery["mismatches"]),
        "ready_shape_candidate_count": 1,
        "ready_shape_candidate_digest": str(
            selection.get("ready_shape_candidate", {}).get("ready_shape_digest") or ""
        ),
        "causal_event_count": int(selection["observed_cardinality"]),
        "causal_event_candidate_digest": str(selection["candidate_digest"]),
        "selection_predicate": str(selection["predicate"]),
        "selection_details_digest": str(selection["details_digest"]),
        "candidate_manifest_count": len(discovery["manifests"]),
        "candidate_classifications": [
            {
                "status": str(plan.get("status") or ""),
                "reason": str(plan.get("reason") or ""),
                "source_version_id": str(plan.get("source_version_id") or ""),
                "nm_ids": list(plan.get("nm_ids") or []),
            }
            for plan in classified
        ],
    }
    qualification["fingerprint"] = _fingerprint(qualification)
    if len(repairable) != 1:
        raise Wbc0013CliError(
            "historical_target_missing_or_ambiguous",
            "WBC0013 historical target discovery is missing or ambiguous",
            details=_selection_details(
                predicate="historical_b.repairable_manifest",
                expected_cardinality=1,
                candidates=[
                    item
                    for item in qualification["candidate_classifications"]
                    if item["status"] == REPAIRABLE
                ],
                details={"discovery_qualification": qualification},
            ),
        )
    return repairable[0], qualification


def _latest_plan(evidence_dir: Path, phase: str) -> tuple[Path, dict[str, Any]]:
    paths = sorted(evidence_dir.glob(f"wbc0013-{phase}-plan-*.json"))
    if not paths:
        raise ValueError(f"WBC0013 {phase} reviewed plan is missing")
    path = paths[-1]
    return path, json.loads(path.read_text(encoding="utf-8"))


def _write_plan(
    context: Mapping[str, Any], phase: str, plan: dict[str, Any]
) -> dict[str, Any]:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(context["evidence_dir"]) / f"wbc0013-{phase}-plan-{timestamp}.json"
    written = _write_private(path, plan, owner="production_apply_evidence")
    if not written.get("written"):
        raise RuntimeError("private WBC0013 plan output was not admitted")
    return {"path": path, "sha256": _sha_file(path)}


def run(args: argparse.Namespace) -> int:
    context = _common(args)
    if context["barrier"].get("active") is not False:
        raise ValueError("business-data write barrier is active or invalid")
    phase = str(args.phase)
    if phase == "plan-a":
        plan = _dense_plan(context)
        if not plan.get("apply_allowed"):
            raise Wbc0013CliError(
                "dense_qualification_blocked",
                "WBC0013 dense A qualification is blocked",
                details=_selection_details(
                    predicate="dense_a.apply_allowed_after_exact_qualification",
                    expected_cardinality=1,
                    candidates=[],
                    details={"blockers": list(plan.get("blockers") or [])},
                ),
            )
        output = _write_plan(context, "a", plan)
        payload = {
            "status": "ready",
            "phase": "a",
            "deployed_sha": context["deployed_sha"],
            "query_only": True,
            "database_written": False,
            "manifest_path": str(output["path"]),
            "manifest_sha256": output["sha256"],
            "material_qualification_digest": plan["material_qualification_digest"],
            "file_mode": "0600",
            "barrier_inactive": True,
            "target_generation_bound": True,
            "timer_change_count": 0,
            "roster_count": len(plan["stock_managed_roster"]["nm_ids"]),
            "existing_count": len(
                plan["non_targets"]["target_facility_existing_fbs_nm_ids"]
            ),
            "historical_zero_count": len(plan["partitions"]["historical_exact_zero"]),
            "no_material_value_history_count": len(
                plan["partitions"]["no_material_value_history"]
            ),
            "zero_insert_count": plan["expected_effects"]["balance_insert_count"],
        }
    elif phase == "apply-a":
        plan_path = Path(args.manifest).resolve()
        if (
            plan_path.parent != Path(context["evidence_dir"])
            or not re.fullmatch(
                r"wbc0013-a-plan-[0-9]{8}T[0-9]{6}Z\.json", plan_path.name
            )
            or plan_path.stat().st_mode & 0o777 != 0o600
            or _sha_file(plan_path) != str(args.manifest_sha256)
        ):
            raise ValueError("reviewed dense A plan digest changed")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        result = DenseFbsService(
            db_path=Path(context["db_path"]),
            runtime_dir=Path(context["runtime_dir"]),
        ).apply_zero_repair_plan(
            plan,
            confirm_fingerprint=str(plan["fingerprint"]),
            approval_reference=str(args.approval_reference),
            actor="production_apply_runner:WBC0013:A",
        )
        payload = {"status": "submitted", "phase": "a", "result": result}
    elif phase == "readback-a":
        _path, plan = _latest_plan(Path(context["evidence_dir"]), "a")
        service = DenseFbsService(
            db_path=Path(context["db_path"]),
            runtime_dir=Path(context["runtime_dir"]),
        )
        readback = service.readback_zero_repair(
            operation_id=str(plan["input_manifest"]["operation_id"])
        )
        complete = (
            readback.get("state") == "active"
            and readback.get("query_only") is True
            and readback.get("exact_reconciled") is True
        )
        payload = {
            "status": "reconciled" if complete else "not_reconciled",
            "query_only": True,
            "zero_row_count": int(readback.get("zero_row_count") or 0),
            "document_count": int(readback.get("pool_inventory_document_count") or 0),
            "non_target_preserved": bool(readback.get("non_target_preserved")),
            "readback": readback,
        }
    elif phase == "plan-b":
        plan, qualification = _historical_plan(context)
        output = _write_plan(context, "b", plan)
        preservation = plan["typed_evidence"]["current_preservation"]
        payload = {
            "status": "ready",
            "phase": "b",
            "deployed_sha": context["deployed_sha"],
            "query_only": True,
            "database_written": False,
            "manifest_path": str(output["path"]),
            "manifest_sha256": output["sha256"],
            "material_qualification_digest": _fingerprint(
                {
                    "source_material_digest": plan["source_material_digest"],
                    "roster_digest": plan["roster_digest"],
                    "provenance_digest": plan["provenance_digest"],
                    "ready_before_digest": plan["ready_before_digest"],
                    "ready_after_digest": plan["ready_after_digest"],
                    "historical_manifest": plan["historical_manifest"],
                    "mismatch_classification_digest": qualification["fingerprint"],
                }
            ),
            "file_mode": "0600",
            "barrier_inactive": True,
            "target_generation_bound": True,
            "timer_change_count": 0,
            "historical_repair_count": 1,
            "fresh_mismatch_count": qualification["fresh_mismatch_count"],
            "ready_shape_candidate_count": qualification[
                "ready_shape_candidate_count"
            ],
            "ready_shape_candidate_digest": qualification[
                "ready_shape_candidate_digest"
            ],
            "causal_event_count": qualification["causal_event_count"],
            "causal_event_candidate_digest": qualification[
                "causal_event_candidate_digest"
            ],
            "selection_predicate": qualification["selection_predicate"],
            "selection_details_digest": qualification["selection_details_digest"],
            "mismatch_classification_digest": qualification["fingerprint"],
            "current_active_preserved": bool(preservation["active_version_id"]),
            "current_sync_preserved": bool(preservation["sync_version_id"]),
            "current_pool_preserved": bool(preservation["pool_rows_digest"]),
        }
    elif phase == "apply-b":
        plan_path = Path(args.manifest).resolve()
        if (
            plan_path.parent != Path(context["evidence_dir"])
            or not re.fullmatch(
                r"wbc0013-b-plan-[0-9]{8}T[0-9]{6}Z\.json", plan_path.name
            )
            or plan_path.stat().st_mode & 0o777 != 0o600
            or _sha_file(plan_path) != str(args.manifest_sha256)
        ):
            raise ValueError("reviewed historical B plan digest changed")
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        result = WarehouseFbsMaterialRematerializer(
            runtime=context["runtime"],
            timestamp_factory=lambda: (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
        ).apply_plan(
            plan,
            confirm_fingerprint=str(plan["plan_fingerprint"]),
            approval_reference=str(args.approval_reference),
            actor="production_apply_runner:WBC0013:B",
        )
        payload = {"status": "submitted", "phase": "b", "result": result}
    else:
        _path, plan = _latest_plan(Path(context["evidence_dir"]), "b")
        readback = WarehouseFbsMaterialRematerializer(
            runtime=context["runtime"],
            timestamp_factory=lambda: (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
        ).readback(operation_id=str(plan["operation_id"]))
        expected = plan["typed_evidence"]["readback_identity"]
        actual = readback.get("readback_identity") or {}
        complete = readback.get("status") == "repaired" and actual == expected
        payload = {
            "status": "reconciled" if complete else "not_reconciled",
            "query_only": True,
            "historical_repair_count": 1 if complete else 0,
            "current_active_preserved": actual.get("active_version_id")
            == expected.get("active_version_id"),
            "current_sync_preserved": actual.get("sync_version_id")
            == expected.get("sync_version_id"),
            "current_pool_preserved": actual.get("current_pool_digest")
            == expected.get("current_pool_digest"),
            "ready_target_total_closed": actual.get("ready_snapshot_digest")
            == expected.get("ready_snapshot_digest"),
            "non_target_preserved": actual.get(
                "ready_target_total_non_target_closure_digest"
            )
            == expected.get("ready_target_total_non_target_closure_digest"),
            "readback": readback,
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("status") in {"ready", "submitted", "reconciled"} else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=("plan-a", "apply-a", "readback-a", "plan-b", "apply-b", "readback-b"),
    )
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--target-file", type=Path, required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256", default="")
    parser.add_argument("--approval-reference", default="")
    args = parser.parse_args()
    try:
        return run(args)
    except (OSError, RuntimeError, ValueError, KeyError, sqlite3.Error) as exc:
        raw_phase = str(args.phase)
        phase = "a" if raw_phase.endswith("-a") else "b"
        stage = (
            "qualification"
            if raw_phase.startswith("plan-")
            else "submit"
            if raw_phase.startswith("apply-")
            else "readback"
        )
        code = str(
            getattr(exc, "code", "")
            or f"wbc0013_{phase}_{stage}_{type(exc).__name__.lower()}"
        )[:120]
        message = str(exc).replace("\n", " ")[:500]
        details = getattr(exc, "details", None)
        typed_details = (
            dict(details)
            if isinstance(details, Mapping)
            and {
                "predicate",
                "expected_cardinality",
                "observed_cardinality",
                "candidate_digest",
            }
            <= set(details)
            else _selection_details(
                predicate=f"wbc0013.{phase}.{stage}.exception_free",
                expected_cardinality=1,
                candidates=[],
                details={
                    "exception_type": type(exc).__name__,
                    "message": message,
                    "exception_details": details,
                },
            )
        )
        print(
            json.dumps(
                {
                    "status": "error",
                    "phase": phase,
                    "stage": stage,
                    "code": code,
                    "message": message,
                    "predicate": str(typed_details.get("predicate") or "")[:200],
                    "expected_cardinality": int(
                        typed_details.get("expected_cardinality") or 0
                    ),
                    "observed_cardinality": int(
                        typed_details.get("observed_cardinality") or 0
                    ),
                    "candidate_digest": str(
                        typed_details.get("candidate_digest")
                        or _fingerprint(typed_details.get("bounded_candidates") or [])
                    ),
                    "details_digest": str(
                        typed_details.get("details_digest")
                        or _fingerprint(typed_details)
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
