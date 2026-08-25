#!/usr/bin/env python3
"""Guarded historical inventory materialization for the main Web Vitrina.

Dry-run is the default and performs query-only source reconstruction.  Apply
accepts only the exact reviewed manifest, exact deployed SHA and a separate
human approval reference.  It appends compact component/finalization rows and
never rewrites ready snapshots or source ledgers.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_cutover import (  # noqa: E402
    ALLOCATIONS_TABLE,
    MANIFESTS_TABLE,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    EVENTS_TABLE,
    MAPPING_EXTENSION_ALLOCATIONS_TABLE,
    MAPPING_EXTENSIONS_TABLE,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    APPLIES_TABLE,
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    preview_inventory_history_capture,
)
from packages.application.storage_registry import (  # noqa: E402
    GenerationManifest,
    StoreRegistry,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)
from packages.business_time import current_business_date_iso  # noqa: E402


SCHEMA_VERSION = "sheet_vitrina_v1_inventory_history_backfill_v1"
SOURCE_CAS_CONTRACT = "sheet_vitrina_v1_inventory_history_backfill_source_cas_v3"
READY_EVIDENCE_CONTRACT = "sheet_vitrina_v1_inventory_ready_evidence_v1"
FORMULA_VERSION = "inventory_planning_v1"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Yekaterinburg")
MAX_DAYS = 730
REQUIRED_SOURCE_TABLES = frozenset(
    {
        "sheet_vitrina_v1_ready_snapshots",
        FACILITIES_TABLE,
        MANIFESTS_TABLE,
        ALLOCATIONS_TABLE,
        MAPPING_EXTENSIONS_TABLE,
        MAPPING_EXTENSION_ALLOCATIONS_TABLE,
        OPERATIONS_TABLE,
        LINES_TABLE,
        EVENTS_TABLE,
        OBSERVATIONS_TABLE,
        WAREHOUSE_MAPPINGS_TABLE,
    }
)
REQUIRED_HISTORY_TABLES = frozenset(
    {CAPTURES_TABLE, COMPONENTS_TABLE, FINALIZATIONS_TABLE, APPLIES_TABLE}
)


class InventoryHistoryBackfillError(RuntimeError):
    """A dry-run or apply safety condition failed closed."""


def run_backfill(
    *,
    runtime_dir: Path,
    evidence_dir: Path,
    apply: bool,
    deployed_sha: str,
    date_from: str | None = None,
    date_to: str | None = None,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    approval_reference: str | None = None,
    deployed_sha_file: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    evidence_dir = evidence_dir.expanduser().resolve()
    store_registry = StoreRegistry(runtime_dir)
    storage_manifest = store_registry.load(require_files=True)
    db_path = store_registry.resolve("operational", manifest=storage_manifest)
    if not db_path.is_file():
        raise InventoryHistoryBackfillError("canonical runtime SQLite DB is missing")
    _require_evidence_outside_repo(evidence_dir)
    effective_now = now or datetime.now(timezone.utc)
    last_closed = (
        date.fromisoformat(current_business_date_iso(effective_now)) - timedelta(days=1)
    ).isoformat()
    exact_deployed_sha = _validated_sha(deployed_sha)
    sha_file = (
        deployed_sha_file.expanduser().resolve()
        if deployed_sha_file is not None
        else runtime_dir.parent / "app" / ".wb-core-runtime-sha"
    )
    _validate_exact_deployment(
        expected_deployed_sha=exact_deployed_sha,
        deployed_sha_file=sha_file,
    )
    if not apply:
        return _dry_run(
            db_path=db_path,
            evidence_dir=evidence_dir,
            storage_manifest=storage_manifest,
            deployed_sha=exact_deployed_sha,
            date_from=date_from,
            date_to=min(date_to or last_closed, last_closed),
            created_at=_timestamp(effective_now),
        )
    if manifest_path is None or not expected_manifest_sha256 or not approval_reference:
        raise InventoryHistoryBackfillError(
            "--apply requires exact manifest path/hash and separate human approval reference"
        )
    try:
        with warehouse_sync_lock(runtime_dir, blocking=False):
            return _apply_manifest(
                db_path=db_path,
                store_registry=store_registry,
                evidence_dir=evidence_dir,
                manifest_path=manifest_path.expanduser().resolve(),
                expected_manifest_sha256=str(expected_manifest_sha256),
                deployed_sha=exact_deployed_sha,
                deployed_sha_file=sha_file,
                approval_reference=str(approval_reference).strip(),
                applied_at=_timestamp(effective_now),
            )
    except WarehouseSyncBusyError as exc:
        raise InventoryHistoryBackfillError(
            "canonical warehouse writer is busy; no inventory history mutation was attempted"
        ) from exc


def _dry_run(
    *,
    db_path: Path,
    evidence_dir: Path,
    storage_manifest: GenerationManifest,
    deployed_sha: str,
    date_from: str | None,
    date_to: str,
    created_at: str,
) -> dict[str, Any]:
    before_file = _file_digest(db_path)
    with _query_only_connection(db_path) as conn:
        plan = _build_manifest(
            conn,
            storage_manifest=storage_manifest,
            deployed_sha=deployed_sha,
            date_from=date_from,
            date_to=date_to,
            created_at=created_at,
        )
    after_file = _file_digest(db_path)
    if after_file != before_file:
        raise InventoryHistoryBackfillError("dry-run changed canonical SQLite bytes")
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / (
        "inventory-history-backfill-plan-"
        + created_at.replace(":", "").replace("-", "")
        + ".json"
    )
    _write_private_json(manifest_path, plan)
    manifest_sha256 = _file_digest(manifest_path)
    partitions = dict(plan["partitions"])
    scope_quality = dict(partitions["scope_quality"])
    component_states = dict(partitions["component_states"])
    date_quality = dict(partitions["date_quality"])
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "status": str(plan["status"]),
        "database_written": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "plan_fingerprint": str(plan["plan_fingerprint"]),
        "deployed_sha": deployed_sha,
        "date_from": str(plan["scope"]["date_from"]),
        "date_to": str(plan["scope"]["date_to"]),
        "date_count": int(plan["scope"]["date_count"]),
        "target_capture_count": int(plan["expected_effect"]["capture_count"]),
        "target_component_count": int(plan["expected_effect"]["component_count"]),
        "target_scope_count": int(plan["expected_effect"]["scope_count"]),
        "target_sku_count": int(plan["expected_effect"]["sku_count"]),
        "facility_count": len(plan["facility_roster"]),
        "full": int(scope_quality.get("full", 0)),
        "partial": int(scope_quality.get("partial", 0)),
        "unavailable": int(date_quality.get("unavailable", 0)),
        "inapplicable": int(component_states.get("inapplicable", 0)),
        "partition_units": {
            "full": "scopes",
            "partial": "scopes",
            "unavailable": "dates",
            "inapplicable": "components",
        },
        "partitions": partitions,
        "source_gaps": list(plan["source_gaps"]),
        "blockers": list(plan["blockers"]),
        "non_target_invariants": dict(plan["non_target_invariants"]),
        "recovery": dict(plan["recovery"]),
    }


def _build_manifest(
    conn: sqlite3.Connection,
    *,
    storage_manifest: GenerationManifest,
    deployed_sha: str,
    date_from: str | None,
    date_to: str,
    created_at: str,
) -> dict[str, Any]:
    tables = _tables(conn)
    missing_tables = sorted(REQUIRED_SOURCE_TABLES - tables)
    if missing_tables:
        raise InventoryHistoryBackfillError(
            "required source tables are missing: " + ", ".join(missing_tables)
        )
    missing_history_tables = sorted(REQUIRED_HISTORY_TABLES - tables)
    if missing_history_tables:
        raise InventoryHistoryBackfillError(
            "deployed inventory history schema is missing: "
            + ", ".join(missing_history_tables)
        )
    wb_history, wb_sources, wb_blockers = _ready_wb_history(
        conn,
        date_from=date_from,
        date_to=date_to,
    )
    if not wb_history:
        raise InventoryHistoryBackfillError("no proven ready stock_total history exists")
    earliest = min(wb_history)
    effective_from = max(date_from or earliest, earliest)
    _validate_window(effective_from, date_to)
    target_dates = list(_iter_dates(effective_from, date_to))
    facility_history = _fbs_history(
        conn,
        target_dates=target_dates,
        target_nm_ids=sorted(
            {
                int(scope_key.split(":", 1)[1])
                for scopes in wb_history.values()
                for scope_key in scopes
                if scope_key.startswith("SKU:")
            }
        ),
    )
    blockers = [*wb_blockers, *facility_history["blockers"]]
    source_watermarks = _source_watermarks(
        conn,
        date_from=effective_from,
        date_to=date_to,
    )
    before = _target_history_state(
        conn,
        date_from=effective_from,
        date_to=date_to,
    )
    generation = _schema_generation(
        conn,
        deployed_sha=deployed_sha,
        storage_manifest=storage_manifest,
    )
    roster = facility_history["roster"]
    captures: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    partitions = {
        "date_quality": {"full": 0, "partial": 0, "unavailable": 0},
        "scope_quality": {"full": 0, "partial": 0, "unavailable": 0},
        "component_states": {
            "exact": 0,
            "exact_zero": 0,
            "missing": 0,
            "inapplicable": 0,
        },
    }
    scope_count = 0
    component_count = 0
    for business_date in target_dates:
        scopes = wb_history.get(business_date)
        if scopes is None:
            partitions["date_quality"]["unavailable"] += 1
            gaps.append(
                {
                    "business_date": business_date,
                    "state": "unavailable",
                    "reason": "ready stock_total evidence is missing",
                }
            )
            continue
        components: list[dict[str, Any]] = []
        date_scope_partitions = {"full": 0, "partial": 0, "unavailable": 0}
        for scope_key, wb_value in sorted(scopes.items(), key=lambda item: _scope_sort_key(item[0])):
            scope_kind = "TOTAL" if scope_key == "TOTAL" else "SKU"
            nm_id = None if scope_kind == "TOTAL" else int(scope_key.split(":", 1)[1])
            wb_state = _value_state(wb_value)
            scope_components = [
                _component(
                    scope_kind=scope_kind,
                    scope_key=scope_key,
                    nm_id=nm_id,
                    component_kind="WB",
                    component_id="WB",
                    component_label="WB",
                    state=wb_state,
                    quantity=wb_value,
                    source_revision=str(wb_sources[business_date]["snapshot_id"]),
                    source_digest=str(
                        wb_sources[business_date]["inventory_evidence_digest"]
                    ),
                    source_watermark=str(wb_sources[business_date]["refreshed_at"]),
                    provenance={
                        "source": "sheet_vitrina_v1_ready_snapshots.stock_total",
                        "legacy_fact_semantics": "WB_only",
                        "inventory_evidence_contract": READY_EVIDENCE_CONTRACT,
                        "inventory_evidence_digest": wb_sources[business_date][
                            "inventory_evidence_digest"
                        ],
                    },
                )
            ]
            missing: list[str] = []
            for facility in roster:
                facility_id = str(facility["facility_id"])
                state, value, source = _facility_component_for_date(
                    facility_history,
                    facility_id=facility_id,
                    business_date=business_date,
                    nm_id=nm_id,
                )
                if state == "missing":
                    missing.append(str(facility["name"]))
                scope_components.append(
                    _component(
                        scope_kind=scope_kind,
                        scope_key=scope_key,
                        nm_id=nm_id,
                        component_kind="FBS_FACILITY",
                        component_id=facility_id,
                        component_label=str(facility["name"]),
                        state=state,
                        quantity=value,
                        source_revision=str(source.get("revision") or ""),
                        source_digest=str(source.get("digest") or ""),
                        source_watermark=str(source.get("watermark") or ""),
                        provenance=source,
                    )
                )
            if wb_state == "missing":
                missing.insert(0, "WB")
            for component in scope_components:
                partitions["component_states"][str(component["state"])] += 1
            known_component_exists = any(
                str(component["state"]) in {"exact", "exact_zero"}
                for component in scope_components
            )
            if missing and known_component_exists:
                date_scope_partitions["partial"] += 1
            elif missing:
                date_scope_partitions["unavailable"] += 1
            else:
                date_scope_partitions["full"] += 1
            components.extend(scope_components)
            scope_count += 1
        source_manifest = {
            "contract": SCHEMA_VERSION,
            "business_date": business_date,
            "cutoff_identity": {
                "kind": "latest accepted closed-day ready evidence",
                "date_to": date_to,
            },
            "ready": wb_sources[business_date],
            "fbs_source_watermarks_digest": source_watermarks["fbs_digest"],
            "facility_roster_revision": _digest(roster),
            "formula": {
                "wb": "persisted stock_total preserved as WB-only fact",
                "fbs": "physical minus active reserved",
                "total": "WB plus every applicable FBS facility",
                "excluded": ["FBO", "aggregate_FF", "transit", "seller_stock_readback"],
            },
        }
        preview = preview_inventory_history_capture(
            business_date=business_date,
            capture_kind="historical_backfill",
            formula_version=FORMULA_VERSION,
            facility_roster=roster,
            source_manifest=source_manifest,
            components=components,
            captured_at=created_at,
        )
        finalization_identity = (
            f"backfill:{SCHEMA_VERSION}:{deployed_sha}:"
            f"{business_date}:{preview['capture_id']}"
        )
        before_item = before["by_date"].get(
            business_date,
            {
                "capture_id": "",
                "finalization_digest": "",
                "finalization_identity": "",
                "components": [],
            },
        )
        captures.append(
            {
                "business_date": business_date,
                "capture_id": preview["capture_id"],
                "source_digest": preview["source_digest"],
                "facility_roster_revision": preview["facility_roster_revision"],
                "finalization_identity": finalization_identity,
                "finalization_contract": {
                    "contract": "sheet_vitrina_general_day_closure_v1",
                    "cutoff_kind": "last closed business day",
                    "cutoff_date": date_to,
                    "accepted_ready_snapshot_id": wb_sources[business_date][
                        "snapshot_id"
                    ],
                },
                "source_manifest": preview["source_manifest"],
                "components": preview["components"],
                "before": {
                    **before_item,
                    "values_by_scope": _summarize_components(
                        before_item.get("components") or []
                    ),
                },
                "proposed_values_by_scope": _summarize_components(
                    preview["components"]
                ),
                "quality_counts": date_scope_partitions,
            }
        )
        component_count += len(components)
        partitions["scope_quality"]["full"] += date_scope_partitions["full"]
        partitions["scope_quality"]["partial"] += date_scope_partitions["partial"]
        partitions["scope_quality"]["unavailable"] += date_scope_partitions[
            "unavailable"
        ]
        date_quality = (
            "unavailable"
            if date_scope_partitions["unavailable"]
            else "partial"
            if date_scope_partitions["partial"]
            else "full"
        )
        partitions["date_quality"][date_quality] += 1
    before_capture_ids = set(before["capture_ids"])
    before_finalization_keys = set(before["finalization_keys"])
    new_captures = [
        item for item in captures if str(item["capture_id"]) not in before_capture_ids
    ]
    new_finalizations = [
        item
        for item in captures
        if (
            str(item["business_date"]),
            str(item["capture_id"]),
            str(item["finalization_identity"]),
        )
        not in before_finalization_keys
    ]
    core = {
        "schema_version": SCHEMA_VERSION,
        "status": "blocked" if blockers else "ready",
        "mode": "dry-run",
        "database_written": False,
        "created_at": created_at,
        "deployed_sha": deployed_sha,
        "schema_generation": generation,
        "formula_version": FORMULA_VERSION,
        "scope": {
            "date_from": effective_from,
            "date_to": date_to,
            "date_count": len(target_dates),
            "dates": target_dates,
            "all_dates_before_or_equal_last_closed_day": True,
        },
        "source_watermarks": source_watermarks,
        "facility_roster": roster,
        "captures": captures,
        "partitions": partitions,
        "source_gaps": gaps,
        "blockers": blockers,
        "pre_change": {
            "target_digest": before["digest"],
            "capture_count": before["capture_count"],
            "component_count": before["component_count"],
            "finalization_count": before["finalization_count"],
        },
        "expected_effect": {
            "capture_count": len(captures),
            "component_count": component_count,
            "scope_count": scope_count,
            "sku_count": len(
                {
                    int(component["nm_id"])
                    for item in captures
                    for component in item["components"]
                    if component["scope_kind"] == "SKU"
                }
            ),
            "inserted_capture_count": len(new_captures),
            "inserted_component_count": sum(
                len(item["components"]) for item in new_captures
            ),
            "inserted_finalization_count": len(new_finalizations),
            "write_allowlist": [CAPTURES_TABLE, COMPONENTS_TABLE, FINALIZATIONS_TABLE, APPLIES_TABLE],
            "append_or_supersede_only": True,
            "ready_snapshots_rewritten": False,
        },
        "non_target_invariants": {
            "source_watermarks_digest": source_watermarks["digest"],
            "source_scoped_row_counts": source_watermarks["scoped_row_counts"],
            "post_cutoff_and_unrelated_rows": "excluded_from_target_source_cas",
            "forbidden_pools": ["FBO"],
            "seller_stock_readback_role": "excluded",
            "source_ledgers_written": False,
        },
        "idempotency": {
            "key": "exact manifest SHA-256",
            "capture_identity": "business_date plus normalized source digest",
            "ambiguous_commit": "query applies table and finalization readback; never replay blindly",
        },
        "recovery": {
            "before_image": "verified target-scoped JSON evidence outside repository",
            "restore": "forward finalization restoration under a separately reviewed canonical maintenance/write-barrier action",
            "post_apply": "query-only capture/component/finalization and source-watermark reconciliation",
        },
    }
    core["plan_fingerprint"] = _digest(core)
    return core


def _apply_manifest(
    *,
    db_path: Path,
    store_registry: StoreRegistry,
    evidence_dir: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    deployed_sha: str,
    deployed_sha_file: Path,
    approval_reference: str,
    applied_at: str,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise InventoryHistoryBackfillError("reviewed manifest is missing")
    actual_manifest_sha256 = _file_digest(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise InventoryHistoryBackfillError("reviewed manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "ready":
        raise InventoryHistoryBackfillError("reviewed manifest is not a ready v1 manifest")
    if str(manifest.get("deployed_sha") or "") != deployed_sha:
        raise InventoryHistoryBackfillError("manifest/deployed SHA mismatch")
    unsigned_manifest = dict(manifest)
    supplied_plan_fingerprint = str(unsigned_manifest.pop("plan_fingerprint", ""))
    if _digest(unsigned_manifest) != supplied_plan_fingerprint:
        raise InventoryHistoryBackfillError("manifest plan fingerprint mismatch")
    if not approval_reference or len(approval_reference) > 500:
        raise InventoryHistoryBackfillError("separate human approval reference is invalid")
    _validate_exact_deployment(
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    with _query_only_connection(db_path) as conn:
        already = (
            conn.execute(
                f"SELECT reconciliation_json FROM {APPLIES_TABLE} WHERE manifest_hash=?",
                (actual_manifest_sha256,),
            ).fetchone()
            if APPLIES_TABLE in _tables(conn)
            else None
        )
        if already is not None:
            return {
                **json.loads(str(already[0])),
                "mode": "apply",
                "status": "already_applied",
                "database_written": False,
                "idempotent_noop": True,
            }
    scope = dict(manifest["scope"])
    storage_manifest = store_registry.load(require_files=True)
    if store_registry.resolve("operational", manifest=storage_manifest) != db_path:
        raise InventoryHistoryBackfillError("canonical operational generation changed after dry-run")
    with _query_only_connection(db_path) as conn:
        generation = _schema_generation(
            conn,
            deployed_sha=deployed_sha,
            storage_manifest=storage_manifest,
        )
        if generation != dict(manifest["schema_generation"]):
            raise InventoryHistoryBackfillError("schema/generation changed after dry-run")
        watermarks = _source_watermarks(
            conn,
            date_from=str(scope["date_from"]),
            date_to=str(scope["date_to"]),
        )
        if watermarks["digest"] != str(manifest["source_watermarks"]["digest"]):
            raise InventoryHistoryBackfillError("source watermarks changed after dry-run")
        before = _target_history_state(
            conn,
            date_from=str(scope["date_from"]),
            date_to=str(scope["date_to"]),
        )
    if before["digest"] != str(manifest["pre_change"]["target_digest"]):
        raise InventoryHistoryBackfillError("target history changed after dry-run")
    recovery_path = evidence_dir / "before-images" / (
        "inventory-history-before-"
        + applied_at.replace(":", "").replace("-", "")
        + "-"
        + actual_manifest_sha256.removeprefix("sha256:")[:12]
        + ".json"
    )
    recovery_evidence = {
        "contract": "sheet_vitrina_v1_inventory_history_before_image_v1",
        "manifest_sha256": actual_manifest_sha256,
        "deployed_sha": deployed_sha,
        "scope": scope,
        "target_digest": before["digest"],
        "source_watermarks_digest": watermarks["digest"],
        "target_material": before["evidence"],
        "recovery": {
            "kind": "forward_finalization_restoration",
            "existing_date_action": "append a superseding pointer to the prior capture",
            "previously_empty_date_action": "append an empty unavailable recovery capture",
            "delete_or_rewrite": False,
            "boundary": "separate reviewed canonical maintenance/write-barrier action",
        },
    }
    _write_private_json(recovery_path, recovery_evidence)
    recovery_sha256 = _file_digest(recovery_path)
    if json.loads(recovery_path.read_text(encoding="utf-8")) != recovery_evidence:
        raise InventoryHistoryBackfillError("target before-image readback mismatch")
    capture_count = component_count = finalization_count = 0
    locked_storage_manifest = store_registry.load(require_files=True)
    if store_registry.resolve("operational", manifest=locked_storage_manifest) != db_path:
        raise InventoryHistoryBackfillError("canonical operational generation changed before apply")
    conn = sqlite3.connect(db_path, timeout=60.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN IMMEDIATE")
        transaction_storage_manifest = store_registry.load(require_files=True)
        if (
            transaction_storage_manifest.manifest_sha256
            != locked_storage_manifest.manifest_sha256
            or store_registry.resolve(
                "operational",
                manifest=transaction_storage_manifest,
            )
            != db_path
        ):
            raise InventoryHistoryBackfillError(
                "canonical operational generation changed before the write transaction"
            )
        _validate_exact_deployment(
            expected_deployed_sha=deployed_sha,
            deployed_sha_file=deployed_sha_file,
        )
        locked_generation = _schema_generation(
            conn,
            deployed_sha=deployed_sha,
            storage_manifest=transaction_storage_manifest,
        )
        locked_watermarks = _source_watermarks(
            conn,
            date_from=str(scope["date_from"]),
            date_to=str(scope["date_to"]),
        )
        locked_before = _target_history_state(
            conn,
            date_from=str(scope["date_from"]),
            date_to=str(scope["date_to"]),
        )
        if locked_generation != dict(manifest["schema_generation"]):
            raise InventoryHistoryBackfillError(
                "schema/generation changed before the write transaction"
            )
        if locked_watermarks["digest"] != watermarks["digest"]:
            raise InventoryHistoryBackfillError(
                "source watermarks changed before the write transaction"
            )
        if locked_before["digest"] != before["digest"]:
            raise InventoryHistoryBackfillError(
                "target history changed before the write transaction"
            )
        for item in manifest["captures"]:
            capture = append_inventory_history_capture(
                conn,
                business_date=str(item["business_date"]),
                capture_kind="historical_backfill",
                formula_version=FORMULA_VERSION,
                facility_roster=manifest["facility_roster"],
                source_manifest=item["source_manifest"],
                components=item["components"],
                captured_at=str(manifest["created_at"]),
                generation_identity=_digest(manifest["schema_generation"]),
            )
            if (
                str(capture["capture_id"]) != str(item["capture_id"])
                or str(capture["source_digest"]) != str(item["source_digest"])
            ):
                raise InventoryHistoryBackfillError("normalized capture identity drifted")
            finalization = append_inventory_history_finalization(
                conn,
                business_date=str(item["business_date"]),
                capture_id=str(capture["capture_id"]),
                finalization_identity=str(item["finalization_identity"]),
                finalized_at=applied_at,
                provenance={
                    "manifest_sha256": actual_manifest_sha256,
                    "approval_reference": approval_reference,
                    "deployed_sha": deployed_sha,
                },
            )
            capture_count += int(bool(capture["inserted"]))
            component_count += int(bool(capture["inserted"])) * int(capture["component_count"])
            finalization_count += int(bool(finalization["inserted"]))
        expected_effect = dict(manifest["expected_effect"])
        actual_effect = {
            "inserted_capture_count": capture_count,
            "inserted_component_count": component_count,
            "inserted_finalization_count": finalization_count,
        }
        expected_insertions = {
            key: int(expected_effect[key]) for key in actual_effect
        }
        if actual_effect != expected_insertions:
            raise InventoryHistoryBackfillError(
                "apply row-count reconciliation drifted before commit"
            )
        after = _target_history_state(
            conn,
            date_from=str(scope["date_from"]),
            date_to=str(scope["date_to"]),
        )
        reconciliation = {
            "schema_version": SCHEMA_VERSION,
            "status": "reconciled",
            "manifest_sha256": actual_manifest_sha256,
            "plan_fingerprint": str(manifest["plan_fingerprint"]),
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "applied_at": applied_at,
            "recovery_evidence_path": str(recovery_path),
            "recovery_evidence_sha256": recovery_sha256,
            "target_digest_before": before["digest"],
            "target_digest_after": after["digest"],
            "inserted_capture_count": capture_count,
            "inserted_component_count": component_count,
            "inserted_finalization_count": finalization_count,
            "source_watermarks_digest": watermarks["digest"],
            "non_target_preserved": True,
        }
        conn.execute(
            f"""INSERT INTO {APPLIES_TABLE}(
                    manifest_hash,deployed_sha,schema_generation,
                    source_watermarks_digest,expected_capture_count,
                    expected_component_count,applied_capture_count,
                    applied_component_count,before_digest,after_digest,
                    approval_reference,recovery_evidence_path,applied_at,
                    reconciliation_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                actual_manifest_sha256,
                deployed_sha,
                _digest(manifest["schema_generation"]),
                watermarks["digest"],
                int(manifest["expected_effect"]["inserted_capture_count"]),
                int(manifest["expected_effect"]["inserted_component_count"]),
                capture_count,
                component_count,
                before["digest"],
                after["digest"],
                approval_reference,
                str(recovery_path),
                applied_at,
                _json(reconciliation),
            ),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    readback_storage_manifest = store_registry.load(require_files=True)
    if store_registry.resolve("operational", manifest=readback_storage_manifest) != db_path:
        raise InventoryHistoryBackfillError("canonical operational generation changed after apply")
    with _query_only_connection(db_path) as readback:
        readback_generation = _schema_generation(
            readback,
            deployed_sha=deployed_sha,
            storage_manifest=readback_storage_manifest,
        )
        if readback_generation != dict(manifest["schema_generation"]):
            raise InventoryHistoryBackfillError("schema/generation changed after apply")
        audit = readback.execute(
            f"SELECT reconciliation_json FROM {APPLIES_TABLE} WHERE manifest_hash=?",
            (actual_manifest_sha256,),
        ).fetchone()
        current_watermarks = _source_watermarks(
            readback,
            date_from=str(scope["date_from"]),
            date_to=str(scope["date_to"]),
        )
    if audit is None:
        raise InventoryHistoryBackfillError("post-commit apply audit readback is missing")
    if current_watermarks["digest"] != watermarks["digest"]:
        raise InventoryHistoryBackfillError("post-apply source watermark drifted")
    reconciliation = json.loads(str(audit[0]))
    reconciliation_path = evidence_dir / (
        "inventory-history-backfill-reconciliation-"
        + applied_at.replace(":", "").replace("-", "")
        + ".json"
    )
    _write_private_json(reconciliation_path, reconciliation)
    reconciliation_sha256 = _file_digest(reconciliation_path)
    return {
        **reconciliation,
        "mode": "apply",
        "database_written": True,
        "idempotent_noop": False,
        "reconciliation_path": str(reconciliation_path),
        "reconciliation_sha256": reconciliation_sha256,
        "evidence_sha256": _digest(
            {
                "manifest_sha256": actual_manifest_sha256,
                "recovery_evidence_sha256": recovery_sha256,
                "reconciliation_sha256": reconciliation_sha256,
            }
        ),
    }


def _ready_wb_history(
    conn: sqlite3.Connection,
    *,
    date_from: str | None = None,
    date_to: str,
) -> tuple[dict[str, dict[str, int | None]], dict[str, dict[str, Any]], list[str]]:
    selected: dict[
        str,
        tuple[tuple[str, str, str, str], dict[str, int | None], dict[str, Any]],
    ] = {}
    blockers: list[str] = []
    rows = conn.execute(
        """SELECT bundle_version,activated_at,as_of_date,snapshot_id,plan_version,
                  refreshed_at,plan_json
             FROM sheet_vitrina_v1_ready_snapshots
            WHERE as_of_date<=? AND (? IS NULL OR as_of_date>=?)
            ORDER BY refreshed_at,activated_at,bundle_version,as_of_date""",
        (date_to, date_from, date_from),
    ).fetchall()
    for row in rows:
        try:
            plan = json.loads(str(row["plan_json"]))
        except json.JSONDecodeError:
            blockers.append(f"invalid ready plan JSON: {row['bundle_version']}/{row['as_of_date']}")
            continue
        data_sheets = [
            item
            for item in list(plan.get("sheets") or [])
            if isinstance(item, Mapping)
            and str(item.get("sheet_name") or "") == "DATA_VITRINA"
        ]
        if not data_sheets:
            continue
        if len(data_sheets) != 1:
            blockers.append(f"ambiguous ready DATA_VITRINA sheet: {row['snapshot_id']}")
            continue
        data = data_sheets[0]
        header = list(data.get("header") or [])
        plan_dates = [str(item) for item in list(plan.get("date_columns") or [])]
        if len(header) < 2 or header[:2] != ["label", "key"]:
            blockers.append(f"ready DATA_VITRINA header drift: {row['snapshot_id']}")
            continue
        if len(set(plan_dates)) != len(plan_dates):
            blockers.append(f"duplicate ready date column: {row['snapshot_id']}")
            continue
        embedded_identity = {
            "snapshot_id": str(plan.get("snapshot_id") or ""),
            "plan_version": str(plan.get("plan_version") or ""),
            "as_of_date": str(plan.get("as_of_date") or ""),
        }
        persisted_identity = {
            "snapshot_id": str(row["snapshot_id"]),
            "plan_version": str(row["plan_version"]),
            "as_of_date": str(row["as_of_date"]),
        }
        if embedded_identity != persisted_identity:
            blockers.append(f"ready plan identity drift: {row['snapshot_id']}")
            continue
        rows_by_id: dict[str, list[Any]] = {}
        duplicate_inventory_key = False
        for raw_item in list(data.get("rows") or []):
            if not isinstance(raw_item, list) or len(raw_item) < 2:
                continue
            row_id = str(raw_item[1])
            is_inventory_key = row_id == "TOTAL|total_stock_total" or (
                row_id.startswith("SKU:") and row_id.endswith("|stock_total")
            )
            if not is_inventory_key:
                continue
            if row_id in rows_by_id:
                duplicate_inventory_key = True
                break
            rows_by_id[row_id] = list(raw_item)
        if duplicate_inventory_key:
            blockers.append(f"duplicate ready stock_total key: {row['snapshot_id']}")
            continue
        rank = (
            str(row["refreshed_at"]),
            str(row["activated_at"]),
            str(row["bundle_version"]),
            str(row["snapshot_id"]),
        )
        observed_plan_digest = _digest(plan)
        for date_position, business_date in enumerate(plan_dates):
            if business_date > date_to:
                continue
            header_positions = [
                index for index, value in enumerate(header) if str(value) == business_date
            ]
            if len(header_positions) != 1:
                blockers.append(
                    f"ready date/header mismatch: {row['snapshot_id']}/{business_date}"
                )
                continue
            column = header_positions[0]
            scopes: dict[str, int | None] = {}
            for row_id, item in rows_by_id.items():
                if row_id == "TOTAL|total_stock_total":
                    scopes["TOTAL"] = _optional_integer(item[column] if len(item) > column else None)
                elif row_id.startswith("SKU:") and row_id.endswith("|stock_total"):
                    scope_key = row_id.split("|", 1)[0]
                    try:
                        int(scope_key.split(":", 1)[1])
                    except (IndexError, ValueError):
                        continue
                    scopes[scope_key] = _optional_integer(item[column] if len(item) > column else None)
            if not scopes:
                continue
            typed_scopes = [
                {
                    "scope_kind": "TOTAL" if scope_key == "TOTAL" else "SKU",
                    "scope_key": scope_key,
                    "row_key": (
                        "TOTAL|total_stock_total"
                        if scope_key == "TOTAL"
                        else f"{scope_key}|stock_total"
                    ),
                    "state": _value_state(value),
                    "quantity": value,
                }
                for scope_key, value in sorted(
                    scopes.items(), key=lambda item: _scope_sort_key(item[0])
                )
            ]
            selection_identity = {
                "bundle_version": str(row["bundle_version"]),
                "activated_at": str(row["activated_at"]),
                "snapshot_as_of_date": str(row["as_of_date"]),
                "snapshot_id": str(row["snapshot_id"]),
                "plan_version": str(row["plan_version"]),
                "refreshed_at": str(row["refreshed_at"]),
                "selection_rank": list(rank),
            }
            inventory_evidence = {
                "contract": READY_EVIDENCE_CONTRACT,
                "business_date": business_date,
                "selection_identity": selection_identity,
                "column_schema": {
                    "sheet_name": "DATA_VITRINA",
                    "header": [str(item) for item in header],
                    "date_columns": plan_dates,
                    "date_column_position": date_position,
                    "header_column_index": column,
                    "column_date": business_date,
                    "key_columns": [
                        {"index": 0, "value": "label"},
                        {"index": 1, "value": "key"},
                    ],
                },
                "stock_total_scopes": typed_scopes,
            }
            inventory_evidence_digest = _digest(inventory_evidence)
            source = {
                **selection_identity,
                "column_date": business_date,
                "inventory_evidence": inventory_evidence,
                "inventory_evidence_digest": inventory_evidence_digest,
                "observed_plan_digest": observed_plan_digest,
                "digest_roles": {
                    "inventory_evidence_digest": "capture_source_and_apply_cas",
                    "observed_plan_digest": "immutable_audit_only_not_apply_cas",
                },
            }
            prior = selected.get(business_date)
            if prior is None or rank > prior[0]:
                selected[business_date] = (rank, scopes, source)
            elif rank == prior[0] and prior[2]["inventory_evidence_digest"] != (
                inventory_evidence_digest
            ):
                blockers.append(f"ambiguous ready stock_total revision: {business_date}")
    return (
        {business_date: value[1] for business_date, value in selected.items()},
        {business_date: value[2] for business_date, value in selected.items()},
        blockers,
    )


def _fbs_history(
    conn: sqlite3.Connection,
    *,
    target_dates: Sequence[str],
    target_nm_ids: Sequence[int],
) -> dict[str, Any]:
    date_to = str(target_dates[-1])
    facilities = [
        dict(row)
        for row in conn.execute(
            f"SELECT facility_id,code,name,active,created_at,updated_at FROM {FACILITIES_TABLE} ORDER BY code,facility_id"
        )
    ]
    roster: list[dict[str, Any]] = []
    by_facility: dict[str, Any] = {}
    blockers: list[str] = []
    source_material: dict[str, dict[str, dict[str, Any]]] = {
        table: {}
        for table in (
            FACILITIES_TABLE,
            MANIFESTS_TABLE,
            ALLOCATIONS_TABLE,
            MAPPING_EXTENSIONS_TABLE,
            MAPPING_EXTENSION_ALLOCATIONS_TABLE,
            OPERATIONS_TABLE,
            LINES_TABLE,
            EVENTS_TABLE,
            OBSERVATIONS_TABLE,
            WAREHOUSE_MAPPINGS_TABLE,
        )
    }
    for facility in facilities:
        facility_id = str(facility["facility_id"])
        applicability_dates: list[str] = []
        for raw in conn.execute(
            f"""SELECT observation.observation_sequence,
                       observation.observation_id,observation.order_id,
                       observation.source_revision,observation.source_created_at,
                       observation.warehouse_id,observation.office_id,
                       observation.nm_id,observation.observed_at,
                       mapping.mapping_id,mapping.seller_warehouse_id,
                       mapping.facility_id,mapping.mapping_digest,mapping.active,
                       mapping.created_at,mapping.created_by
                  FROM {OBSERVATIONS_TABLE} observation
                  JOIN {WAREHOUSE_MAPPINGS_TABLE} mapping
                    ON mapping.seller_warehouse_id=observation.warehouse_id
                   AND mapping.facility_id=? AND mapping.active=1
                 WHERE observation.source_created_at<>''
                 ORDER BY observation.observation_sequence,mapping.mapping_id""",
            (facility_id,),
        ).fetchall():
            parsed = _source_business_date(str(raw[4]))
            if parsed:
                if parsed > date_to:
                    continue
                applicability_dates.append(parsed)
                _record_source(
                    source_material,
                    table=OBSERVATIONS_TABLE,
                    key=str(raw[0]),
                    row={
                        "observation_sequence": int(raw[0]),
                        "observation_id": str(raw[1]),
                        "order_id": int(raw[2]),
                        "source_revision": str(raw[3]),
                        "source_created_at": str(raw[4]),
                        "warehouse_id": raw[5],
                        "office_id": raw[6],
                        "nm_id": int(raw[7]),
                        "observed_at": str(raw[8]),
                    },
                )
                _record_source(
                    source_material,
                    table=WAREHOUSE_MAPPINGS_TABLE,
                    key=str(raw[9]),
                    row={
                        "mapping_id": str(raw[9]),
                        "seller_warehouse_id": int(raw[10]),
                        "facility_id": str(raw[11]),
                        "mapping_digest": str(raw[12]),
                        "active": int(raw[13]),
                        "created_at": str(raw[14]),
                        "created_by": str(raw[15]),
                    },
                )
            elif str(raw[4] or ""):
                blockers.append(
                    f"invalid FBS applicability timestamp for facility {facility_id}"
                )
        openings: list[dict[str, Any]] = []
        for manifest in conn.execute(
            f"""SELECT manifest.*
                  FROM {MANIFESTS_TABLE} manifest
                 WHERE EXISTS(
                     SELECT 1 FROM {ALLOCATIONS_TABLE} allocation
                      WHERE allocation.cutover_id=manifest.cutover_id
                        AND allocation.facility_id=? AND allocation.pool='FBS'
                 ) ORDER BY manifest.cutover_at""",
            (facility_id,),
        ).fetchall():
            manifest_row = dict(manifest)
            if str(manifest_row["business_date"]) > date_to:
                continue
            quantities = {
                int(row["nm_id"]): int(row["quantity"])
                for row in conn.execute(
                    f"""SELECT * FROM {ALLOCATIONS_TABLE}
                         WHERE cutover_id=? AND facility_id=? AND pool='FBS' ORDER BY nm_id""",
                    (str(manifest_row["cutover_id"]), facility_id),
                )
            }
            _record_source(
                source_material,
                table=MANIFESTS_TABLE,
                key=str(manifest_row["cutover_id"]),
                row=manifest_row,
            )
            for allocation in conn.execute(
                f"""SELECT * FROM {ALLOCATIONS_TABLE}
                     WHERE cutover_id=? AND facility_id=? AND pool='FBS'
                     ORDER BY line_no""",
                (str(manifest_row["cutover_id"]), facility_id),
            ).fetchall():
                allocation_row = dict(allocation)
                _record_source(
                    source_material,
                    table=ALLOCATIONS_TABLE,
                    key=f"{allocation_row['cutover_id']}:{allocation_row['line_no']}",
                    row=allocation_row,
                )
            openings.append(
                {
                    "exact_from": str(manifest_row["business_date"]),
                    "boundary_at": str(manifest_row["cutover_at"]),
                    "revision": str(manifest_row["cutover_id"]),
                    "digest": str(manifest_row["manifest_digest"]),
                    "watermark": str(manifest_row["observation_watermark_digest"]),
                    "quantities": quantities,
                    "source": "ff_pool_cutover_allocation",
                }
            )
            applicability_dates.append(str(manifest_row["business_date"]))
        for extension in conn.execute(
            f"""SELECT * FROM {MAPPING_EXTENSIONS_TABLE}
                 WHERE facility_id=? ORDER BY created_at""",
            (facility_id,),
        ).fetchall():
            extension_row = dict(extension)
            exact_from = _source_business_date(str(extension_row["created_at"]))
            if exact_from and exact_from > date_to:
                continue
            quantities = {
                int(row["nm_id"]): int(row["opening_quantity"])
                for row in conn.execute(
                    f"""SELECT *
                         FROM {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
                         WHERE extension_id=? ORDER BY nm_id""",
                    (str(extension_row["extension_id"]),),
                )
            }
            if not exact_from:
                blockers.append(
                    f"invalid mapping extension time: {extension_row['extension_id']}"
                )
                continue
            _record_source(
                source_material,
                table=MAPPING_EXTENSIONS_TABLE,
                key=str(extension_row["extension_id"]),
                row=extension_row,
            )
            for allocation in conn.execute(
                f"""SELECT * FROM {MAPPING_EXTENSION_ALLOCATIONS_TABLE}
                     WHERE extension_id=? ORDER BY nm_id""",
                (str(extension_row["extension_id"]),),
            ).fetchall():
                allocation_row = dict(allocation)
                _record_source(
                    source_material,
                    table=MAPPING_EXTENSION_ALLOCATIONS_TABLE,
                    key=f"{allocation_row['extension_id']}:{allocation_row['nm_id']}",
                    row=allocation_row,
                )
            mapping = conn.execute(
                f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE mapping_id=?",
                (str(extension_row["warehouse_mapping_id"]),),
            ).fetchone()
            if mapping is None:
                blockers.append(
                    "mapping extension references a missing warehouse mapping: "
                    + str(extension_row["extension_id"])
                )
            else:
                mapping_row = dict(mapping)
                _record_source(
                    source_material,
                    table=WAREHOUSE_MAPPINGS_TABLE,
                    key=str(mapping_row["mapping_id"]),
                    row=mapping_row,
                )
            boundary = _loads(extension_row["frozen_boundary_json"], {})
            openings.append(
                {
                    "exact_from": exact_from,
                    "boundary_at": str(
                        boundary.get("local_boundary_at")
                        or extension_row["created_at"]
                    ),
                    "revision": str(extension_row["extension_id"]),
                    "digest": str(extension_row["plan_fingerprint"]),
                    "watermark": str(extension_row["frozen_rows_digest"]),
                    "quantities": quantities,
                    "source": "fbs_mapping_extension_allocation",
                }
            )
            applicability_dates.append(exact_from)
        if len(openings) > 1:
            blockers.append(f"multiple FBS opening projections for facility {facility_id}")
        opening = openings[0] if len(openings) == 1 else None
        applicable_from = min(applicability_dates) if applicability_dates else ""
        if not applicable_from and opening is None:
            continue
        _record_source(
            source_material,
            table=FACILITIES_TABLE,
            key=facility_id,
            row=facility,
        )
        roster.append(
            {
                "facility_id": facility_id,
                "code": str(facility["code"]),
                "name": str(facility["name"]),
                "active": bool(facility["active"]),
                "applicable": True,
                "effective_from": applicable_from,
                "display_order": len(roster) + 1,
            }
        )
        by_facility[facility_id] = _fold_facility(
            conn,
            facility_id=facility_id,
            applicable_from=applicable_from,
            opening=opening,
            target_dates=target_dates,
            target_nm_ids=target_nm_ids,
            date_to=date_to,
            blockers=blockers,
            source_material=source_material,
        )
    return {
        "roster": roster,
        "by_facility": by_facility,
        "blockers": blockers,
        "source_material": {
            table: [rows[key] for key in sorted(rows)]
            for table, rows in sorted(source_material.items())
        },
    }


def _fold_facility(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    applicable_from: str,
    opening: Mapping[str, Any] | None,
    target_dates: Sequence[str],
    target_nm_ids: Sequence[int],
    date_to: str,
    blockers: list[str],
    source_material: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "applicable_from": applicable_from,
        "exact_from": str((opening or {}).get("exact_from") or ""),
        "by_date": {},
    }
    if opening is None:
        return result
    boundary_at = str(opening["boundary_at"])
    physical = {int(key): int(value) for key, value in dict(opening["quantities"]).items()}
    movements: list[tuple[str, int, int, str]] = []
    for row in conn.execute(
        f"""SELECT operation.operation_id,operation.operation_type,
                   operation.source_system,operation.source_type,
                   operation.source_id,operation.source_revision,
                   operation.business_date,operation.posted_at,
                   line.line_no,line.facility_id,line.pool,line.nm_id,
                   line.quantity_delta
              FROM {LINES_TABLE} line
              JOIN {OPERATIONS_TABLE} operation USING(operation_id)
             WHERE line.facility_id=? AND line.pool='FBS'
               AND operation.posted_at>?
               AND operation.business_date<=?
               AND operation.source_type<>'fbs_order_lifecycle_event'
             ORDER BY operation.posted_at,operation.operation_id,line.line_no""",
        (facility_id, boundary_at, date_to),
    ).fetchall():
        movement_date = str(row["business_date"])
        try:
            date.fromisoformat(movement_date)
        except ValueError:
            blockers.append(f"invalid FBS movement business date for {facility_id}")
            continue
        movements.append(
            (
                movement_date,
                int(row["nm_id"]),
                int(row["quantity_delta"]),
                str(row["operation_id"]),
            )
        )
        _record_source(
            source_material,
            table=OPERATIONS_TABLE,
            key=str(row["operation_id"]),
            row={
                "operation_id": str(row["operation_id"]),
                "operation_type": str(row["operation_type"]),
                "source_system": str(row["source_system"]),
                "source_type": str(row["source_type"]),
                "source_id": str(row["source_id"]),
                "source_revision": str(row["source_revision"]),
                "business_date": movement_date,
                "posted_at": str(row["posted_at"]),
            },
        )
        _record_source(
            source_material,
            table=LINES_TABLE,
            key=f"{row['operation_id']}:{row['line_no']}",
            row={
                "operation_id": str(row["operation_id"]),
                "line_no": int(row["line_no"]),
                "facility_id": str(row["facility_id"]),
                "pool": str(row["pool"]),
                "nm_id": int(row["nm_id"]),
                "quantity_delta": int(row["quantity_delta"]),
            },
        )
    raw_lifecycle_rows = conn.execute(
        f"""SELECT event_sequence,event_id,order_id,event_type,source_revision,
                   status_digest,facility_id,pool,nm_id,quantity,
                   physical_quantity_delta,evidence_digest,occurred_at
              FROM {EVENTS_TABLE}
             WHERE facility_id=? AND pool='FBS'
             ORDER BY occurred_at,event_sequence""",
        (facility_id,),
    ).fetchall()
    lifecycle_rows: list[tuple[sqlite3.Row, str]] = []
    for row in raw_lifecycle_rows:
        event_date = _source_business_date(str(row["occurred_at"]))
        if not event_date:
            blockers.append(f"invalid FBS lifecycle timestamp for {facility_id}")
            continue
        if event_date > date_to:
            continue
        _record_source(
            source_material,
            table=EVENTS_TABLE,
            key=str(row["event_id"]),
            row={
                "event_sequence": int(row["event_sequence"]),
                "event_id": str(row["event_id"]),
                "order_id": int(row["order_id"]),
                "event_type": str(row["event_type"]),
                "source_revision": str(row["source_revision"]),
                "status_digest": str(row["status_digest"]),
                "facility_id": str(row["facility_id"]),
                "pool": str(row["pool"]),
                "nm_id": int(row["nm_id"]),
                "quantity": int(row["quantity"]),
                "physical_quantity_delta": int(row["physical_quantity_delta"]),
                "evidence_digest": str(row["evidence_digest"]),
                "occurred_at": str(row["occurred_at"]),
            },
        )
        lifecycle_rows.append((row, event_date))
        if (
            int(row["physical_quantity_delta"]) != 0
            and str(row["occurred_at"]) > boundary_at
        ):
            movements.append(
                (
                    event_date,
                    int(row["nm_id"]),
                    int(row["physical_quantity_delta"]),
                    str(row["event_id"]),
                )
            )
    movements.sort(key=lambda item: (item[0], item[3]))
    reservation_state: dict[int, tuple[int, int]] = {}
    movement_index = 0
    lifecycle_index = 0
    for business_date in target_dates:
        if business_date < str(opening["exact_from"]):
            continue
        while movement_index < len(movements) and movements[movement_index][0] <= business_date:
            _, nm_id, delta, _ = movements[movement_index]
            physical[nm_id] = physical.get(nm_id, 0) + delta
            movement_index += 1
        while lifecycle_index < len(lifecycle_rows):
            row, event_date = lifecycle_rows[lifecycle_index]
            if event_date > business_date:
                break
            event_type = str(row["event_type"])
            order_id = int(row["order_id"])
            if event_type in {"opening_reserve", "reserve"}:
                reservation_state[order_id] = (
                    int(row["nm_id"]),
                    int(row["quantity"]),
                )
            elif event_type in {"release", "handoff_debit", "opening_handoff_debit"}:
                reservation_state.pop(order_id, None)
            lifecycle_index += 1
        reserved: dict[int, int] = {}
        for nm_id, quantity in reservation_state.values():
            reserved[nm_id] = reserved.get(nm_id, 0) + quantity
        all_nm_ids = sorted(set(target_nm_ids) | set(physical) | set(reserved))
        available = {
            nm_id: int(physical.get(nm_id, 0)) - int(reserved.get(nm_id, 0))
            for nm_id in all_nm_ids
        }
        result["by_date"][business_date] = {
            "available": available,
            "total": sum(available.values()),
            "source": {
                "source": str(opening["source"]),
                "revision": str(opening["revision"]),
                "digest": str(opening["digest"]),
                "watermark": _digest(
                    {
                        "opening": str(opening["watermark"]),
                        "movement_count": movement_index,
                        "lifecycle_event_count": lifecycle_index,
                    }
                ),
                "formula": "physical minus active reserved",
            },
        }
    if any(item[0] < str(opening["exact_from"]) for item in movements):
        blockers.append(f"post-boundary FBS movement predates exact opening for {facility_id}")
    return result


def _facility_component_for_date(
    history: Mapping[str, Any],
    *,
    facility_id: str,
    business_date: str,
    nm_id: int | None,
) -> tuple[str, int | None, dict[str, Any]]:
    facility = dict(history["by_facility"].get(facility_id) or {})
    applicable_from = str(facility.get("applicable_from") or "")
    exact_from = str(facility.get("exact_from") or "")
    if not applicable_from or business_date < applicable_from:
        return "inapplicable", None, {
            "source": "facility_applicability",
            "effective_from": applicable_from,
        }
    if not exact_from or business_date < exact_from:
        return "missing", None, {
            "source": "facility_applicability_without_exact_projection",
            "effective_from": applicable_from,
            "exact_from": exact_from,
        }
    row = dict(facility.get("by_date", {}).get(business_date) or {})
    if not row:
        return "missing", None, {
            "source": "exact_projection_gap",
            "effective_from": applicable_from,
            "exact_from": exact_from,
        }
    value = int(row["total"] if nm_id is None else row["available"].get(nm_id, 0))
    return _value_state(value), value, dict(row["source"])


def _source_watermarks(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    target_dates = list(_iter_dates(date_from, date_to))
    wb_history, wb_sources, wb_blockers = _ready_wb_history(
        conn,
        date_from=date_from,
        date_to=date_to,
    )
    ready_material = [
        {
            "business_date": business_date,
            "inventory_evidence": wb_sources[business_date]["inventory_evidence"],
            "inventory_evidence_digest": wb_sources[business_date][
                "inventory_evidence_digest"
            ],
        }
        for business_date in target_dates
        if business_date in wb_history
    ]
    target_nm_ids = sorted(
        {
            int(scope_key.split(":", 1)[1])
            for business_date in target_dates
            for scope_key in wb_history.get(business_date, {})
            if scope_key.startswith("SKU:")
        }
    )
    facility_history = _fbs_history(
        conn,
        target_dates=target_dates,
        target_nm_ids=target_nm_ids,
    )
    fbs_material = dict(facility_history["source_material"])
    fbs_projection = {
        "roster": facility_history["roster"],
        "by_facility": facility_history["by_facility"],
    }
    scoped_counts = {
        "selected_ready_dates": len(ready_material),
        **{table: len(rows) for table, rows in sorted(fbs_material.items())},
    }
    result = {
        "contract": SOURCE_CAS_CONTRACT,
        "date_from": date_from,
        "date_to": date_to,
        "ready_digest": _digest(ready_material),
        "fbs_digest": _digest(
            {"source_material": fbs_material, "projection": fbs_projection}
        ),
        "facility_roster_digest": _digest(facility_history["roster"]),
        "source_blockers_digest": _digest(
            {
                "ready": wb_blockers,
                "fbs": facility_history["blockers"],
            }
        ),
        "scoped_row_counts": scoped_counts,
    }
    result["digest"] = _digest(result)
    return result


def _record_source(
    material: dict[str, dict[str, dict[str, Any]]],
    *,
    table: str,
    key: str,
    row: Mapping[str, Any],
) -> None:
    material[table][key] = dict(row)


def _target_history_state(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    tables = _tables(conn)
    if {CAPTURES_TABLE, COMPONENTS_TABLE, FINALIZATIONS_TABLE} - tables:
        empty_evidence = {"captures": [], "components": [], "finalizations": []}
        return {
            "digest": _digest(empty_evidence),
            "capture_count": 0,
            "component_count": 0,
            "finalization_count": 0,
            "by_date": {},
            "capture_ids": [],
            "finalization_keys": [],
            "evidence": empty_evidence,
        }
    captures = [
        dict(row)
        for row in conn.execute(
        f"""SELECT capture_sequence,capture_id,business_date,capture_kind,formula_version,
                       facility_roster_revision,source_digest,captured_at
                  FROM {CAPTURES_TABLE}
                 WHERE business_date BETWEEN ? AND ?
                 ORDER BY capture_sequence""",
            (date_from, date_to),
        )
    ]
    capture_ids = [str(item["capture_id"]) for item in captures]
    components = []
    if capture_ids:
        placeholders = ",".join("?" for _ in capture_ids)
        components = [
            dict(row)
            for row in conn.execute(
                f"""SELECT capture_id,scope_kind,scope_key,nm_id,component_kind,
                           component_id,component_label,state,quantity,
                           source_revision,source_digest,
                           source_watermark
                      FROM {COMPONENTS_TABLE}
                     WHERE capture_id IN ({placeholders})
                     ORDER BY capture_id,scope_kind,scope_key,component_kind,component_id""",
                capture_ids,
            )
        ]
    finalizations = [
        dict(row)
        for row in conn.execute(
            f"""SELECT finalization_sequence,finalization_id,business_date,capture_id,
                       finalization_identity,finalization_digest,
                       supersedes_finalization_digest,finalized_at
                  FROM {FINALIZATIONS_TABLE}
                 WHERE business_date BETWEEN ? AND ?
                 ORDER BY finalization_sequence""",
            (date_from, date_to),
        )
    ]
    by_capture: dict[str, list[dict[str, Any]]] = {}
    for item in components:
        by_capture.setdefault(str(item["capture_id"]), []).append(item)
    by_date: dict[str, Any] = {}
    for item in finalizations:
        by_date[str(item["business_date"])] = {
            "capture_id": str(item["capture_id"]),
            "finalization_digest": str(item["finalization_digest"]),
            "finalization_identity": str(item["finalization_identity"]),
            "components": by_capture.get(str(item["capture_id"]), []),
        }
    material = {"captures": captures, "components": components, "finalizations": finalizations}
    return {
        "digest": _digest(material),
        "capture_count": len(captures),
        "component_count": len(components),
        "finalization_count": len(finalizations),
        "by_date": by_date,
        "capture_ids": [str(item["capture_id"]) for item in captures],
        "finalization_keys": [
            (
                str(item["business_date"]),
                str(item["capture_id"]),
                str(item["finalization_identity"]),
            )
            for item in finalizations
        ],
        "evidence": material,
    }


def _summarize_components(
    components: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_scope: dict[str, list[Mapping[str, Any]]] = {}
    for item in components:
        by_scope.setdefault(str(item.get("scope_key") or ""), []).append(item)
    result: dict[str, Any] = {}
    for scope_key, rows in by_scope.items():
        known = [
            int(item["quantity"])
            for item in rows
            if str(item.get("state") or "") in {"exact", "exact_zero"}
        ]
        missing = [
            str(item.get("component_label") or item.get("component_id") or "")
            for item in rows
            if str(item.get("state") or "") == "missing"
        ]
        result[scope_key] = {
            "value": sum(known) if known else None,
            "quality": (
                "partial" if missing and known else "unavailable" if missing else "full"
            ),
            "missing_components": missing,
            "component_states": {
                str(item.get("component_id") or ""): {
                    "state": str(item.get("state") or ""),
                    "value": item.get("quantity"),
                }
                for item in rows
            },
        }
    return result


def _schema_generation(
    conn: sqlite3.Connection,
    *,
    deployed_sha: str,
    storage_manifest: GenerationManifest,
) -> dict[str, Any]:
    operational = storage_manifest.operational
    if not storage_manifest.implicit and storage_manifest.state != "monolith":
        identity = conn.execute(
            """SELECT schema_revision,logical_store,generation_id,
                      generation_epoch,source_fingerprint
                 FROM finance_operational_schema_meta WHERE singleton=1"""
        ).fetchone()
        expected_identity = (
            operational.schema_revision,
            "operational",
            operational.generation_id,
            operational.generation_epoch,
            storage_manifest.source_fingerprint,
        )
        if identity is None or tuple(identity) != expected_identity:
            raise InventoryHistoryBackfillError(
                "canonical operational file identity does not match the storage generation"
            )
    bundle = conn.execute(
        "SELECT bundle_version,activated_at FROM registry_upload_current_state WHERE slot=1"
    ).fetchone()
    schema_tables = sorted(REQUIRED_SOURCE_TABLES | REQUIRED_HISTORY_TABLES)
    placeholders = ",".join("?" for _ in schema_tables)
    schema_material = [
        [str(value or "") for value in row]
        for row in conn.execute(
            f"""SELECT type,name,tbl_name,sql FROM sqlite_master
                 WHERE name IN ({placeholders}) OR tbl_name IN ({placeholders})
                 ORDER BY type,name""",
            (*schema_tables, *schema_tables),
        ).fetchall()
    ]
    return {
        "deployed_sha": deployed_sha,
        "sqlite_user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "bundle_version": str(bundle[0]) if bundle else "",
        "bundle_activated_at": str(bundle[1]) if bundle else "",
        "storage_generation": {
            "contract_version": storage_manifest.contract_version,
            "state": storage_manifest.state,
            "canonical_source": storage_manifest.canonical_source,
            "generation_epoch": storage_manifest.generation_epoch,
            "manifest_sha256": storage_manifest.manifest_sha256,
            "operational": {
                "generation_id": operational.generation_id,
                "relative_path": operational.relative_path,
                "schema_revision": operational.schema_revision,
                "watermark": operational.watermark,
            },
        },
        "required_schema_digest": _digest(schema_material),
        "history_schema": SCHEMA_VERSION,
    }


def _component(
    *,
    scope_kind: str,
    scope_key: str,
    nm_id: int | None,
    component_kind: str,
    component_id: str,
    component_label: str,
    state: str,
    quantity: int | None,
    source_revision: str,
    source_digest: str,
    source_watermark: str,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "scope_kind": scope_kind,
        "scope_key": scope_key,
        "nm_id": nm_id,
        "component_kind": component_kind,
        "component_id": component_id,
        "component_label": component_label,
        "state": state,
        "quantity": quantity,
        "source_revision": source_revision,
        "source_digest": source_digest,
        "source_watermark": source_watermark,
        "provenance": dict(provenance),
    }


def _value_state(value: int | None) -> str:
    return "missing" if value is None else "exact_zero" if int(value) == 0 else "exact"


def _optional_integer(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else None


def _source_business_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if text.isdigit():
            instant = datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        else:
            instant = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
        return instant.astimezone(BUSINESS_TIMEZONE).date().isoformat()
    except (OverflowError, ValueError):
        return ""


def _scope_sort_key(scope_key: str) -> tuple[int, int]:
    if scope_key == "TOTAL":
        return (0, 0)
    return (1, int(scope_key.split(":", 1)[1]))


def _validate_window(date_from: str, date_to: str) -> None:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    if end < start or (end - start).days + 1 > MAX_DAYS:
        raise InventoryHistoryBackfillError("backfill date window is invalid or too large")


def _iter_dates(date_from: str, date_to: str) -> Iterator[str]:
    current = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _validated_sha(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 40 or any(char not in "0123456789abcdef" for char in normalized):
        raise InventoryHistoryBackfillError("exact deployed SHA is invalid")
    return normalized


def _validate_exact_deployment(*, expected_deployed_sha: str, deployed_sha_file: Path) -> None:
    if not deployed_sha_file.is_file():
        raise InventoryHistoryBackfillError("canonical deployed SHA marker is missing")
    actual = deployed_sha_file.read_text(encoding="utf-8").strip().lower()
    if actual != expected_deployed_sha:
        raise InventoryHistoryBackfillError("canonical deployed SHA marker mismatch")


def _require_evidence_outside_repo(evidence_dir: Path) -> None:
    try:
        evidence_dir.relative_to(ROOT.resolve())
    except ValueError:
        return
    raise InventoryHistoryBackfillError("evidence directory must be outside the repository")


@contextmanager
def _query_only_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=60.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
    finally:
        if os.path.exists(path):
            os.chmod(path, 0o600)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--deployed-sha-file", type=Path)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--approval-reference")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = run_backfill(
            runtime_dir=args.runtime_dir,
            evidence_dir=args.evidence_dir,
            apply=bool(args.apply),
            deployed_sha=args.deployed_sha,
            date_from=args.date_from,
            date_to=args.date_to,
            manifest_path=args.manifest,
            expected_manifest_sha256=args.manifest_sha256,
            approval_reference=args.approval_reference,
            deployed_sha_file=args.deployed_sha_file,
        )
    except (InventoryHistoryBackfillError, ValueError, sqlite3.Error) as exc:
        print(_json({"status": "blocked", "error": str(exc)}))
        return 2
    print(_json(result))
    return 0 if str(result.get("status")) in {"ready", "reconciled", "already_applied"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
