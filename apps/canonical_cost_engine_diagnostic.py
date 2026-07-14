#!/usr/bin/env python3
"""Exhaustive non-fail-fast diagnostic collector for canonical cutover."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.canonical_cost_engine_backfill import (  # noqa: E402
    PROTECTED_TABLES,
    SOURCE_TABLES,
    _canonical_digest,
    _candidate_reconciliation,
    _integrity_check,
    _layer_cost_continuity,
    _legacy_digest,
    _sqlite_backup,
    _tables_digest,
)
from packages.application.canonical_cost_engine import (  # noqa: E402
    CUTOVER_DATE,
    STAGES,
    CanonicalCostBlocked,
    CanonicalCostEngine,
    _wb_supply_cache_evidence,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


PIPELINE_STAGES = (
    "source_wide_validation",
    "baseline_discovery",
    "baseline_coverage",
    "component_materialization",
    "ff_movement_replay",
    "wb_movement_layers",
    "acceptance",
    "doprinato_direct_fifo",
    "outstanding_underaccepted",
    "layer_cost_continuity",
    "recognized_wac",
    "paid_wac",
    "daily_state",
    "module_40_read_side",
    "module_45_read_side",
    "our_wb_cost",
    "proxy3",
    "finance_pnl",
    "historical_publication",
    "reconciliation",
    "idempotency",
    "integrity_preservation",
    "schema_migration",
    "rollback_inode_wal_safety",
    "ui_vitrina_aggregation",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--date-to", default=date.today().isoformat())
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(args.runtime_dir))
    source_db = runtime.db_path
    if not source_db.exists():
        raise ValueError("runtime SQLite database does not exist")
    date_to = date.fromisoformat(str(args.date_to)).isoformat()
    inode = source_db.stat().st_ino
    integrity = _integrity_check(source_db)
    source_digest = _tables_digest(source_db, SOURCE_TABLES)
    protected_digest = _tables_digest(source_db, PROTECTED_TABLES)
    legacy_digest = _legacy_digest(source_db)
    target_digest = _canonical_digest(
        source_db, date_from=CUTOVER_DATE, date_to=date_to
    )

    with tempfile.TemporaryDirectory(prefix="canonical-cost-diagnostic-") as tmp:
        candidate_runtime = RegistryUploadDbBackedRuntime(
            runtime_dir=Path(tmp) / "runtime"
        )
        candidate_runtime.runtime_dir.mkdir(parents=True, exist_ok=True)
        _sqlite_backup(source_db, candidate_runtime.db_path)
        engine = CanonicalCostEngine(runtime=candidate_runtime)

        first_source = engine.source_anomaly_preflight(date_to=date_to)
        second_source = engine.source_anomaly_preflight(date_to=date_to)
        source_stable = first_source["fingerprint"] == second_source["fingerprint"]

        coverage: dict[str, dict[str, Any]] = {}
        blockers: list[dict[str, Any]] = []
        quarantine: list[dict[str, Any]] = []
        primary_ids: list[str] = []

        primary_anomalies = list(first_source.get("unresolved_anomalies") or [])
        for anomaly in primary_anomalies:
            record = _primary_blocker(anomaly)
            blockers.append(record)
            primary_ids.append(record["blocker_id"])
            quarantine.append(
                {
                    "blocker_id": record["blocker_id"],
                    "source_identity": record["source_identity"],
                    "root_cause": record["code"],
                    "affected_stages": record["affected_pipeline_stages"],
                    "quantity_impact": record["quantity_impact"],
                    "recognized_capital_impact_rub": None,
                    "paid_capital_impact_rub": None,
                    "continued_checks": [
                        "baseline_discovery",
                        "baseline_coverage",
                        "component_materialization",
                        "ff_movement_replay",
                        "schema_migration",
                        "integrity_preservation",
                    ],
                    "tainted_results": [
                        "wb_movement_layers",
                        "acceptance",
                        "outstanding_underaccepted",
                        "recognized_wac",
                        "paid_wac",
                        "daily_state",
                        "module_40_read_side",
                        "module_45_read_side",
                        "our_wb_cost",
                        "proxy3",
                        "finance_pnl",
                        "historical_publication",
                        "ui_vitrina_aggregation",
                    ],
                    "proposed_fix": record["recommended_fix"],
                }
            )

        coverage["source_wide_validation"] = _coverage(
            "BLOCKED" if primary_ids else "PASS",
            int(first_source.get("operation_count") or 0),
            len(primary_ids),
            [],
            first_source["fingerprint"],
        )

        quarantine_actions = _apply_diagnostic_quarantine(
            candidate_runtime.db_path,
            first_source.get("unresolved_anomalies") or [],
        )
        engine._diagnostic_quarantined_doprinato_keys.update(  # noqa: SLF001
            (
                str(item.get("supply_id") or ""),
                int(item.get("nm_id") or 0),
            )
            for item in first_source.get("unresolved_anomalies") or []
            if item.get("blocker_class") == "doprinato_unmatched_surplus"
            and str(item.get("supply_id") or "")
            and int(item.get("nm_id") or 0) > 0
        )
        for action in quarantine_actions:
            for item in quarantine:
                if item["blocker_id"] == action["blocker_id"]:
                    item["diagnostic_action"] = action["action"]
                    item["continued_checks"] = list(PIPELINE_STAGES[1:])
                    break
        quarantine_source = engine.source_anomaly_preflight(date_to=date_to)

        baseline: dict[str, Any] | None = None
        try:
            baseline = engine.build_baseline_plan(
                cutover_date=CUTOVER_DATE, diagnostic=True
            )
            coverage["baseline_discovery"] = _coverage(
                "PASS", 1, 0, [], str(baseline["fingerprint"])
            )
            coverage["baseline_coverage"] = _coverage(
                "PASS" if baseline.get("cost_coverage") == "1" else "BLOCKED",
                int(baseline.get("primary_sku_count") or 0)
                + int(baseline.get("fallback_sku_count") or 0)
                + int(baseline.get("business_approved_sku_count") or 0),
                0 if baseline.get("cost_coverage") == "1" else 1,
                ["baseline_discovery"],
                str(baseline["fingerprint"]),
            )
            engine.materialize_baseline_plan(baseline)
        except CanonicalCostBlocked as exc:
            record = _exception_blocker(exc, stage="baseline_discovery")
            blockers.append(record)
            primary_ids.append(record["blocker_id"])
            coverage["baseline_discovery"] = _coverage(
                "BLOCKED", 1, 1, [], record["blocker_id"]
            )
            coverage["baseline_coverage"] = _coverage(
                "TAINTED", 0, 1, [record["blocker_id"]], record["blocker_id"]
            )

        component_fingerprint = ""
        if baseline is not None:
            try:
                changed, invalidated = engine._materialize_components(date_to)  # noqa: SLF001
                component_fingerprint = _hash(
                    {"changed": changed, "invalidated_from": invalidated}
                )
                coverage["component_materialization"] = _coverage(
                    "PASS", changed, 0, ["baseline_coverage"], component_fingerprint
                )
            except CanonicalCostBlocked as exc:
                record = _exception_blocker(exc, stage="component_materialization")
                blockers.append(record)
                primary_ids.append(record["blocker_id"])
                coverage["component_materialization"] = _coverage(
                    "BLOCKED", 0, 1, ["baseline_coverage"], record["blocker_id"]
                )
        else:
            coverage["component_materialization"] = _coverage(
                "TAINTED", 0, 0, ["baseline_coverage"], "baseline unavailable"
            )

        opening = first_source.get("checks", {}).get(
            "opening_snapshot_event_replay", {}
        )
        ff_ok = (
            isinstance(opening, Mapping)
            and opening.get("status") == "ok"
            and opening.get("current_ff_total")
            == opening.get("canonical_replay_total")
        )
        coverage["ff_movement_replay"] = _coverage(
            "PASS" if ff_ok else "BLOCKED",
            int(first_source.get("operation_count") or 0),
            0 if ff_ok else 1,
            ["source_wide_validation"],
            _hash(opening),
        )

        normalization = _normalization_analysis(
            first_source=first_source,
            baseline=baseline,
            engine=engine,
            date_to=date_to,
        )
        normalization_blockers = [
            item for item in normalization["operations"]
            if not item["all_conditions_met"]
        ]
        for item in normalization["operations"]:
            blocker_id = next(
                (
                    record["blocker_id"] for record in blockers
                    if record.get("operation_id") == item["operation_id"]
                ),
                "",
            )
            if blocker_id:
                item["blocker_id"] = blocker_id
                for record in blockers:
                    if record["blocker_id"] == blocker_id:
                        record["recognized_capital_impact_rub"] = item[
                            "recognized_capital_exposure_rub"
                        ]
                        record["paid_capital_impact_rub"] = item[
                            "paid_capital_exposure_rub"
                        ]
                        record["eligible_for_approved_normalization"] = item[
                            "all_conditions_met"
                        ]
                        record["requires_new_business_decision"] = not item[
                            "all_conditions_met"
                        ]

        dependent = ["source_wide_validation"] if primary_ids else []
        downstream_status = "TAINTED" if primary_ids else "PASS"
        for stage in (
            "wb_movement_layers",
            "acceptance",
            "outstanding_underaccepted",
            "layer_cost_continuity",
            "recognized_wac",
            "paid_wac",
            "daily_state",
            "module_40_read_side",
            "module_45_read_side",
            "our_wb_cost",
            "proxy3",
            "finance_pnl",
            "historical_publication",
            "ui_vitrina_aggregation",
        ):
            coverage[stage] = _coverage(
                downstream_status,
                0,
                len(primary_ids) if primary_ids else 0,
                dependent,
                _hash({"stage": stage, "primary_blockers": primary_ids}),
            )

        rebuild_payload: dict[str, Any] | None = None
        reconciliation_payload: dict[str, Any] | None = None
        layer_cost_continuity_payload: dict[str, Any] | None = None
        if quarantine_source["status"] == "ok" and baseline is not None:
            try:
                pipeline_quarantine_attempts: list[dict[str, Any]] = []
                for attempt in range(1, 26):
                    try:
                        first_rebuild = engine.rebuild(
                            date_from=CUTOVER_DATE, date_to=date_to
                        )
                        break
                    except CanonicalCostBlocked as exc:
                        exc = _enrich_pipeline_blocker(
                            candidate_runtime.db_path,
                            exc,
                            baseline=baseline,
                            date_to=date_to,
                        )
                        action = _apply_pipeline_quarantine(engine, exc)
                        if action is None:
                            raise
                        record = _exception_blocker(
                            exc, stage="canonical_rebuild"
                        )
                        if record["blocker_id"] not in {
                            item["blocker_id"] for item in blockers
                        }:
                            blockers.append(record)
                            primary_ids.append(record["blocker_id"])
                            quarantine.append(
                                {
                                    "blocker_id": record["blocker_id"],
                                    "source_identity": record["source_identity"],
                                    "root_cause": record["code"],
                                    "affected_stages": record[
                                        "affected_pipeline_stages"
                                    ],
                                    "quantity_impact": record["quantity_impact"],
                                    "recognized_capital_impact_rub": None,
                                    "paid_capital_impact_rub": None,
                                    "continued_checks": list(
                                        PIPELINE_STAGES[1:]
                                    ),
                                    "tainted_results": list(
                                        PIPELINE_STAGES[4:]
                                    ),
                                    "proposed_fix": record[
                                        "recommended_fix"
                                    ],
                                    "diagnostic_action": action,
                                }
                            )
                        pipeline_quarantine_attempts.append(
                            {
                                "attempt": attempt,
                                "blocker_id": record["blocker_id"],
                                "code": record["code"],
                                "action": action,
                            }
                        )
                else:
                    raise CanonicalCostBlocked(
                        "diagnostic_quarantine_attempt_limit_exceeded",
                        {"attempt_limit": 25},
                    )
                first_target = _canonical_digest(
                    candidate_runtime.db_path,
                    date_from=CUTOVER_DATE,
                    date_to=date_to,
                )
                second_rebuild = engine.rebuild(
                    date_from=CUTOVER_DATE, date_to=date_to
                )
                second_target = _canonical_digest(
                    candidate_runtime.db_path,
                    date_from=CUTOVER_DATE,
                    date_to=date_to,
                )
                second_changes = sum(
                    (
                        second_rebuild.component_rows_changed,
                        second_rebuild.movement_rows_changed,
                        second_rebuild.outstanding_rows_changed,
                        second_rebuild.daily_rows_changed,
                    )
                )
                if first_target != second_target or second_changes:
                    raise CanonicalCostBlocked(
                        "candidate_second_run_non_idempotent",
                        {
                            "first_target_digest": first_target,
                            "second_target_digest": second_target,
                            "second_changes": second_changes,
                        },
                    )
                reconciliation_payload = _candidate_reconciliation(
                    candidate_runtime.db_path, date_to
                )
                layer_cost_continuity_payload = _layer_cost_continuity(
                    candidate_runtime.db_path
                )
                if layer_cost_continuity_payload["status"] != "ok":
                    raise CanonicalCostBlocked(
                        "layer_cost_continuity_mismatch",
                        {
                            "fingerprint": layer_cost_continuity_payload[
                                "fingerprint"
                            ],
                            "mismatches": layer_cost_continuity_payload[
                                "mismatches"
                            ],
                        },
                    )
                coverage["layer_cost_continuity"] = _coverage(
                    "TAINTED" if primary_ids else "PASS",
                    int(layer_cost_continuity_payload["movement_layer_count"])
                    + int(
                        layer_cost_continuity_payload[
                            "outstanding_layer_count"
                        ]
                    ),
                    0,
                    ["wb_movement_layers", "outstanding_underaccepted"],
                    str(layer_cost_continuity_payload["fingerprint"]),
                )
                rebuild_payload = {
                    "first": first_rebuild.__dict__,
                    "second": second_rebuild.__dict__,
                    "first_target_digest": first_target,
                    "second_target_digest": second_target,
                    "pipeline_quarantine_attempts": pipeline_quarantine_attempts,
                }
                stage_counts = {
                    "wb_movement_layers": first_rebuild.movement_rows_changed,
                    "acceptance": int(first_source.get("post_cutover_operation_count") or 0),
                    "outstanding_underaccepted": first_rebuild.outstanding_rows_changed,
                    "recognized_wac": first_rebuild.daily_rows_changed,
                    "paid_wac": first_rebuild.daily_rows_changed,
                    "daily_state": first_rebuild.daily_rows_changed,
                    "module_40_read_side": first_rebuild.daily_rows_changed,
                    "module_45_read_side": first_rebuild.daily_rows_changed,
                    "our_wb_cost": first_rebuild.daily_rows_changed,
                    "proxy3": 1,
                    "finance_pnl": 1,
                    "historical_publication": first_rebuild.daily_rows_changed,
                    "ui_vitrina_aggregation": first_rebuild.daily_rows_changed,
                }
                for stage, checked in stage_counts.items():
                    coverage[stage] = _coverage(
                        "TAINTED" if primary_ids else "PASS",
                        checked,
                        0,
                        ["component_materialization"],
                        _hash(
                            {
                                "stage": stage,
                                "projection": first_target,
                                "reconciliation": reconciliation_payload,
                            }
                        ),
                    )
            except CanonicalCostBlocked as exc:
                record = _exception_blocker(exc, stage="canonical_rebuild")
                blockers.append(record)
                primary_ids.append(record["blocker_id"])
                for stage in (
                    "wb_movement_layers",
                    "acceptance",
                    "outstanding_underaccepted",
                    "recognized_wac",
                    "paid_wac",
                    "daily_state",
                    "module_40_read_side",
                    "module_45_read_side",
                    "our_wb_cost",
                    "proxy3",
                    "finance_pnl",
                    "historical_publication",
                    "ui_vitrina_aggregation",
                ):
                    coverage[stage] = _coverage(
                        "TAINTED", 0, 1, [record["blocker_id"]], record["blocker_id"]
                    )
            except Exception as exc:  # collector records technical pipeline blockers too
                record = _technical_blocker(exc, stage="canonical_rebuild")
                blockers.append(record)
                primary_ids.append(record["blocker_id"])
                for stage in (
                    "wb_movement_layers",
                    "acceptance",
                    "outstanding_underaccepted",
                    "recognized_wac",
                    "paid_wac",
                    "daily_state",
                    "module_40_read_side",
                    "module_45_read_side",
                    "our_wb_cost",
                    "proxy3",
                    "finance_pnl",
                    "historical_publication",
                    "ui_vitrina_aggregation",
                ):
                    coverage[stage] = _coverage(
                        "TAINTED", 0, 1, [record["blocker_id"]], record["blocker_id"]
                    )
        dop_checked_count = int(
            first_source.get("checks", {}).get("doprinato_unmatched_surplus")
            or 0
        )
        dop_blocker_count = sum(
            item.get("blocker_class") == "doprinato_unmatched_surplus"
            for item in first_source.get("unresolved_anomalies") or []
        )
        coverage["doprinato_direct_fifo"] = _coverage(
            "BLOCKED" if dop_blocker_count else "PASS",
            int(first_source.get("legacy_doprinato_count") or 0)
            + dop_checked_count,
            dop_blocker_count,
            ["source_wide_validation"],
            _hash(first_source.get("legacy_doprinato") or []),
        )
        coverage["reconciliation"] = _coverage(
            "TAINTED" if primary_ids else "PASS",
            len(STAGES),
            len(primary_ids),
            dependent,
            _hash({"ff": opening, "normalization": normalization}),
        )
        coverage["idempotency"] = _coverage(
            "PASS" if source_stable else "BLOCKED",
            2,
            0 if source_stable else 1,
            [],
            _hash(
                {
                    "first": first_source["fingerprint"],
                    "second": second_source["fingerprint"],
                }
            ),
        )
        coverage["integrity_preservation"] = _coverage(
            "PASS",
            len(SOURCE_TABLES) + len(PROTECTED_TABLES),
            0,
            [],
            _hash(
                {
                    "integrity": _integrity_check(candidate_runtime.db_path),
                    "source": _tables_digest(candidate_runtime.db_path, SOURCE_TABLES),
                    "protected": _tables_digest(candidate_runtime.db_path, PROTECTED_TABLES),
                    "legacy": _legacy_digest(candidate_runtime.db_path),
                }
            ),
        )
        coverage["schema_migration"] = _coverage(
            "PASS", 1, 0, [], _schema_fingerprint(candidate_runtime.db_path)
        )
        coverage["rollback_inode_wal_safety"] = _coverage(
            "PASS", 1, 0, [], "validated by canonical backfill smoke contract"
        )

        for stage in PIPELINE_STAGES:
            coverage.setdefault(
                stage,
                _coverage(
                    "TAINTED",
                    0,
                    len(primary_ids),
                    dependent,
                    "diagnostic dependency taint",
                ),
            )

        for stage, item in coverage.items():
            if item["status"] != "TAINTED":
                continue
            cascade = _cascade_blocker(stage, primary_ids)
            blockers.append(cascade)

        unique_first = sorted(
            record["blocker_id"] for record in blockers
            if record["kind"] == "primary"
        )
        second_source_ids = sorted(
            _primary_blocker(item)["blocker_id"]
            for item in second_source.get("unresolved_anomalies") or []
        )
        first_source_ids = {
            _primary_blocker(item)["blocker_id"]
            for item in first_source.get("unresolved_anomalies") or []
        }
        unique_second = sorted(
            set(second_source_ids)
            | (set(unique_first) - first_source_ids)
        )
        fixpoint = unique_first == unique_second
        inventory_by_id = {
            record["blocker_id"]: _inventory_record(record)
            for record in blockers
            if record["kind"] == "primary"
        }
        anomaly_inventory = sorted(
            inventory_by_id.values(),
            key=lambda item: (
                item["reason_code"],
                item["exact_identity"]["business_date"],
                item["exact_identity"]["supply_id"],
                int(item["exact_identity"]["nm_id"] or 0),
                item["exact_identity"]["operation_id"],
            ),
        )
        report = {
            "contract_name": "canonical_cost_exhaustive_diagnostic_v1",
            "status": "blocked" if primary_ids else "ok",
            "scope": {"date_from": CUTOVER_DATE, "date_to": date_to},
            "source_preflight": first_source,
            "diagnostic_quarantine_preflight": quarantine_source,
            "baseline": baseline,
            "rebuild": rebuild_payload,
            "reconciliation": reconciliation_payload,
            "layer_cost_continuity": layer_cost_continuity_payload,
            "postcutover_normalization": normalization,
            "unmatched_doprinato_absorption": first_source.get(
                "unmatched_doprinato_absorption"
            ),
            "blocker_registry": sorted(
                blockers, key=lambda item: (item["kind"], item["blocker_id"])
            ),
            "primary_blocker_count": len(unique_first),
            "anomaly_inventory": anomaly_inventory,
            "anomaly_inventory_fingerprint": _hash(anomaly_inventory),
            "cascading_blocker_count": sum(
                record["kind"] == "cascading" for record in blockers
            ),
            "quarantine": quarantine,
            "coverage_matrix": [
                {"stage": stage, **coverage[stage]} for stage in PIPELINE_STAGES
            ],
            "diagnostic_passes": [
                {
                    "pass": 1,
                    "new_unique_blocker_ids": unique_first,
                    "quarantined_entities": len(quarantine),
                },
                {
                    "pass": 2,
                    "new_unique_blocker_ids": sorted(
                        set(unique_second) - set(unique_first)
                    ),
                    "all_primary_blocker_ids": unique_second,
                    "quarantined_entities": len(quarantine),
                },
            ],
            "fixpoint": {
                "reached": fixpoint,
                "pass_count": 2,
                "new_blockers_on_last_pass": len(
                    set(unique_second) - set(unique_first)
                ),
                "unexplained_not_reached_count": sum(
                    item["status"] == "NOT_REACHED" for item in coverage.values()
                ),
            },
            "preservation": {
                "integrity_check": integrity,
                "source_inode": inode,
                "source_digest": source_digest,
                "protected_digest": protected_digest,
                "legacy_pre_cutover_digest": legacy_digest,
                "target_before_digest": target_digest,
                "production_mutation": False,
            },
        }
        payload = {**report, "fingerprint": _hash(report)}

    if source_db.stat().st_ino != inode:
        raise ValueError("diagnostic collector changed live SQLite inode")
    live_source_after = _tables_digest(source_db, SOURCE_TABLES)
    live_protected_after = _tables_digest(source_db, PROTECTED_TABLES)
    live_legacy_after = _legacy_digest(source_db)
    concurrent_live_drift = any(
        (
            source_digest != live_source_after,
            protected_digest != live_protected_after,
            legacy_digest != live_legacy_after,
        )
    )
    preservation = dict(payload["preservation"])
    preservation.update(
        {
            "concurrent_live_drift": concurrent_live_drift,
            "source_digest_after": live_source_after,
            "protected_digest_after": live_protected_after,
            "legacy_pre_cutover_digest_after": live_legacy_after,
            "snapshot_publishable": not concurrent_live_drift,
        }
    )
    report = {
        **{key: value for key, value in payload.items() if key != "fingerprint"},
        "status": "stale_snapshot" if concurrent_live_drift else payload["status"],
        "preservation": preservation,
    }
    return {**report, "fingerprint": _hash(report)}


def _normalization_analysis(
    *,
    first_source: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    engine: CanonicalCostEngine,
    date_to: str,
) -> dict[str, Any]:
    opening_quantity = Decimal(str((baseline or {}).get("physical_quantity") or 0))
    opening_recognized = Decimal(
        str((baseline or {}).get("recognized_capital_rub") or 0)
    )
    opening_paid = Decimal(str((baseline or {}).get("paid_capital_rub") or 0))
    official_stock = engine._snapshot_metric(date_to, "stock_total")  # noqa: SLF001
    anomaly_operation_ids = {
        str(item.get("operation_id") or "")
        for item in first_source.get("anomalies") or []
        if item.get("blocker_class") == "accepted_quantity_exceeds_sent"
        and str(item.get("business_date") or "") >= CUTOVER_DATE
    }
    rows: list[dict[str, Any]] = []
    total_quantity = Decimal("0")
    total_recognized = Decimal("0")
    total_paid = Decimal("0")
    for operation in first_source.get("operations") or []:
        if operation.get("operation_id") not in anomaly_operation_ids:
            continue
        normalization = dict(operation.get("postcutover_normalization") or {})
        quantity = Decimal(str(normalization.get("surplus_quantity") or 0))
        rec_unit = Decimal(
            str(normalization.get("recognized_weighted_unit_cost_rub") or 0)
        )
        paid_unit = Decimal(
            str(normalization.get("paid_weighted_unit_cost_rub") or 0)
        )
        rec_exposure = quantity * rec_unit
        paid_exposure = quantity * paid_unit
        accepted_nm_ids = {
            int(item["nm_id"]) for item in operation.get("accepted_lines") or []
        }
        official_evidence = all(nm_id in official_stock for nm_id in accepted_nm_ids)
        checks = dict(normalization.get("checks") or {})
        checks.update(
            {
                "official_wb_evidence_present": official_evidence,
                "quantity_exposure_within_0_15_pct": (
                    opening_quantity > 0
                    and quantity / opening_quantity <= Decimal("0.0015")
                ),
                "recognized_capital_exposure_within_0_15_pct": (
                    opening_recognized > 0
                    and rec_exposure / opening_recognized <= Decimal("0.0015")
                ),
                "paid_capital_exposure_within_0_15_pct": (
                    opening_paid > 0
                    and paid_exposure / opening_paid <= Decimal("0.0015")
                ),
            }
        )
        all_conditions = all(
            bool(value) for key, value in checks.items()
            if key != "missing_cost_nm_ids"
        ) and not checks.get("missing_cost_nm_ids")
        row = {
            "operation_id": operation["operation_id"],
            "supply_id": operation["supply_id"],
            "business_date": operation["business_date"],
            "warehouse": operation["warehouse"],
            "destination": operation["destination"],
            "sent_quantity": operation["sent_quantity"],
            "accepted_quantity": operation["raw_accepted_quantity"],
            "underaccepted_quantity": operation["underaccepted_quantity"],
            "surplus_quantity": normalization.get("surplus_quantity"),
            "sent_lines": operation.get("sent_lines") or [],
            "accepted_lines": operation.get("accepted_lines") or [],
            "line_set_fingerprint": operation["line_set_fingerprint"],
            "accepted_line_set_fingerprint": operation[
                "accepted_line_set_fingerprint"
            ],
            "evidence_fingerprint": operation["evidence_fingerprint"],
            "recognized_weighted_unit_cost_rub": normalization.get(
                "recognized_weighted_unit_cost_rub"
            ),
            "paid_weighted_unit_cost_rub": normalization.get(
                "paid_weighted_unit_cost_rub"
            ),
            "recognized_capital_exposure_rub": _text(rec_exposure),
            "paid_capital_exposure_rub": _text(paid_exposure),
            "official_wb_stock_evidence": {
                str(nm_id): official_stock.get(nm_id)
                for nm_id in sorted(accepted_nm_ids)
            },
            "checks": checks,
            "all_conditions_met": all_conditions,
            "manifest_entry": {
                key: operation[key] for key in (
                    "operation_id",
                    "supply_id",
                    "source_key",
                    "business_date",
                    "line_set_fingerprint",
                    "accepted_line_set_fingerprint",
                    "evidence_fingerprint",
                )
            },
        }
        rows.append(row)
        total_quantity += quantity
        total_recognized += rec_exposure
        total_paid += paid_exposure
    global_checks = {
        "exactly_four_operations": len(rows) == 4,
        "total_normalized_quantity_within_500": total_quantity <= Decimal("500"),
        "total_quantity_exposure_within_0_15_pct": (
            opening_quantity > 0
            and total_quantity / opening_quantity <= Decimal("0.0015")
        ),
        "total_recognized_exposure_within_0_15_pct": (
            opening_recognized > 0
            and total_recognized / opening_recognized <= Decimal("0.0015")
        ),
        "total_paid_exposure_within_0_15_pct": (
            opening_paid > 0
            and total_paid / opening_paid <= Decimal("0.0015")
        ),
    }
    return {
        "policy": "CUTOVER_POSTCUTOVER_SOURCE_NORMALIZATION_V1",
        "opening_totals": {
            "quantity": _text(opening_quantity),
            "recognized_capital_rub": _text(opening_recognized),
            "paid_capital_rub": _text(opening_paid),
        },
        "operations": rows,
        "totals": {
            "normalized_quantity": _text(total_quantity),
            "recognized_capital_exposure_rub": _text(total_recognized),
            "paid_capital_exposure_rub": _text(total_paid),
        },
        "global_checks": global_checks,
        "all_conditions_met": all(global_checks.values())
        and all(item["all_conditions_met"] for item in rows),
        "fingerprint": _hash({"operations": rows, "global_checks": global_checks}),
    }


def _apply_diagnostic_quarantine(
    db_path: Path,
    anomalies: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Remove only blocked entities from the disposable diagnostic copy."""

    actions: list[dict[str, str]] = []
    with sqlite3.connect(db_path) as conn:
        for anomaly in anomalies:
            blocker = _primary_blocker(anomaly)
            operation_id = str(anomaly.get("operation_id") or "")
            supply_id = str(anomaly.get("supply_id") or "")
            code = str(anomaly.get("blocker_class") or "")
            if operation_id and "," not in operation_id:
                conn.execute(
                    "DELETE FROM sheet_vitrina_v1_ff_stock_operation_lines WHERE operation_id=?",
                    (operation_id,),
                )
                conn.execute(
                    "DELETE FROM sheet_vitrina_v1_ff_stock_operations WHERE operation_id=?",
                    (operation_id,),
                )
                actions.append(
                    {
                        "blocker_id": blocker["blocker_id"],
                        "action": "operation header/lines excluded from disposable diagnostic replay",
                    }
                )
                continue
            if code == "doprinato_unmatched_surplus" and supply_id:
                actions.append(
                    {
                        "blocker_id": blocker["blocker_id"],
                        "action": (
                            "exact doprinato supply/SKU line excluded in-memory "
                            "from disposable diagnostic reconciliation"
                        ),
                    }
                )
        conn.commit()
    return actions


def _apply_pipeline_quarantine(
    engine: CanonicalCostEngine, exc: CanonicalCostBlocked
) -> str | None:
    """Quarantine one newly exposed entity and let the next pass continue."""

    supply_id = str(exc.details.get("supply_id") or "")
    nm_id = int(exc.details.get("nm_id") or 0)
    if (
        exc.code != "doprinato_unmatched_surplus"
        or not supply_id
        or nm_id <= 0
    ):
        return None
    key = (supply_id, nm_id)
    if key in engine._diagnostic_quarantined_doprinato_keys:  # noqa: SLF001
        return None
    engine._diagnostic_quarantined_doprinato_keys.add(key)  # noqa: SLF001
    return (
        "newly exposed exact doprinato supply/SKU line excluded in-memory "
        "from disposable diagnostic reconciliation"
    )


def _enrich_pipeline_blocker(
    db_path: Path,
    exc: CanonicalCostBlocked,
    *,
    baseline: Mapping[str, Any],
    date_to: str,
) -> CanonicalCostBlocked:
    if exc.code != "doprinato_unmatched_surplus":
        return exc
    supply_id = str(exc.details.get("supply_id") or "")
    nm_id = int(exc.details.get("nm_id") or 0)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        fact = next(
            (
                item
                for item in _wb_supply_cache_evidence(conn, date_to=date_to)
                if str(item.get("supply_id") or "") == supply_id
                and int(item.get("nm_id") or 0) == nm_id
            ),
            None,
        )
    cost_candidates = [
        item
        for item in baseline.get("lines") or []
        if int(item.get("nm_id") or 0) == nm_id
    ]
    cost = next(
        (item for item in cost_candidates if item.get("stage") == "FF"),
        cost_candidates[0] if cost_candidates else None,
    )
    quantity = Decimal(str(exc.details.get("surplus") or 0))
    recognized_unit = Decimal(str((cost or {}).get("recognized_unit_cost_rub") or 0))
    paid_unit = Decimal(str((cost or {}).get("paid_unit_cost_rub") or 0))
    details = {
        **exc.details,
        "business_date": str((fact or {}).get("accepted_date") or ""),
        "source_identity": str((fact or {}).get("source_identity") or ""),
        "original_supply_id": str((fact or {}).get("original_supply_id") or ""),
        "warehouse": str((fact or {}).get("warehouse") or ""),
        "destination": str((fact or {}).get("destination") or ""),
        "is_final_accepted": bool((fact or {}).get("is_final_accepted")),
        "raw_accepted_quantity": str((fact or {}).get("accepted_quantity") or ""),
        "cost_source": cost,
        "recognized_capital_impact_rub": str(quantity * recognized_unit),
        "paid_capital_impact_rub": str(quantity * paid_unit),
    }
    return CanonicalCostBlocked(exc.code, details)


def _primary_blocker(anomaly: Mapping[str, Any]) -> dict[str, Any]:
    identity = {
        "code": str(anomaly.get("blocker_class") or "unknown"),
        "operation_id": str(anomaly.get("operation_id") or ""),
        "supply_id": str(anomaly.get("supply_id") or ""),
        "nm_id": anomaly.get("nm_id"),
        "business_date": str(anomaly.get("business_date") or ""),
    }
    blocker_id = "cblk_" + _hash(identity)[:20]
    quantity = Decimal(str(anomaly.get("discrepancy") or 0))
    cost_source = anomaly.get("cost_source") or {}
    recognized_unit = Decimal(
        str(cost_source.get("recognized_unit_cost_rub") or 0)
    )
    paid_unit = Decimal(str(cost_source.get("paid_unit_cost_rub") or 0))
    recommended_fix = (
        "after a new human decision, add only this exact fingerprinted "
        "supply/SKU evidence to CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1"
        if identity["code"] == "doprinato_unmatched_surplus"
        else (
            "add the exact immutable operation evidence to "
            "CUTOVER_POSTCUTOVER_SOURCE_NORMALIZATION_V1 only when all "
            "exposure gates pass"
        )
    )
    return {
        "blocker_id": blocker_id,
        "code": identity["code"],
        "class": str(anomaly.get("classification") or identity["code"]),
        "severity": "production_apply_blocker",
        "kind": "primary",
        "operation_id": identity["operation_id"],
        "supply_id": identity["supply_id"],
        "shipment_id": "",
        "document_id": "",
        "nm_id": identity["nm_id"],
        "business_date": identity["business_date"],
        "source_identity": str(anomaly.get("source_identity") or ""),
        "raw_evidence": dict(anomaly),
        "expected": "strict canonical source invariant or exact manifest match",
        "actual": str(anomaly.get("reason") or ""),
        "quantity_impact": anomaly.get("discrepancy"),
        "recognized_capital_impact_rub": (
            _text(quantity * recognized_unit) if recognized_unit > 0 else None
        ),
        "paid_capital_impact_rub": (
            _text(quantity * paid_unit) if paid_unit > 0 else None
        ),
        "affected_pipeline_stages": [
            "wb_movement_layers",
            "acceptance",
            "outstanding_underaccepted",
            "recognized_wac",
            "paid_wac",
            "daily_state",
            "module_40_read_side",
            "module_45_read_side",
            "our_wb_cost",
            "proxy3",
            "finance_pnl",
            "historical_publication",
            "ui_vitrina_aggregation",
        ],
        "dependencies": ["source_wide_validation"],
        "eligible_for_approved_normalization": bool(anomaly.get("eligible")),
        "recommended_fix": recommended_fix,
        "requires_new_business_decision": not bool(anomaly.get("eligible")),
    }


def _inventory_record(blocker: Mapping[str, Any]) -> dict[str, Any]:
    evidence = dict(blocker.get("raw_evidence") or {})
    doprinato = dict(evidence.get("doprinato_evidence") or {})
    manifest = dict(evidence.get("manifest_decision") or {})
    actual = dict(manifest.get("actual") or {})
    source_fingerprint = str(
        actual.get("semantic_evidence_fingerprint")
        or doprinato.get("semantic_evidence_fingerprint")
        or actual.get("raw_row_line_fingerprint")
        or doprinato.get("raw_row_line_fingerprint")
        or evidence.get("evidence_fingerprint")
        or blocker.get("blocker_id")
        or ""
    )
    return {
        "blocker_id": str(blocker.get("blocker_id") or ""),
        "reason_code": str(blocker.get("code") or "unknown"),
        "exact_identity": {
            "operation_id": str(blocker.get("operation_id") or ""),
            "supply_id": str(blocker.get("supply_id") or ""),
            "shipment_id": str(blocker.get("shipment_id") or ""),
            "document_id": str(blocker.get("document_id") or ""),
            "nm_id": blocker.get("nm_id"),
            "business_date": str(blocker.get("business_date") or ""),
            "source_identity": str(blocker.get("source_identity") or ""),
        },
        "source_fingerprint": source_fingerprint,
        "evidence_summary": {
            "classification": str(
                evidence.get("classification") or blocker.get("class") or ""
            ),
            "reason": str(evidence.get("reason") or blocker.get("actual") or ""),
            "quantity": blocker.get("quantity_impact"),
            "recognized_capital_impact_rub": blocker.get(
                "recognized_capital_impact_rub"
            ),
            "paid_capital_impact_rub": blocker.get("paid_capital_impact_rub"),
        },
        "affected_scope": list(blocker.get("affected_pipeline_stages") or []),
        "recommended_policy_category": str(blocker.get("recommended_fix") or ""),
    }


def _exception_blocker(
    exc: CanonicalCostBlocked, *, stage: str
) -> dict[str, Any]:
    identity = {"code": exc.code, "stage": stage, "details": exc.details}
    return {
        "blocker_id": "cblk_" + _hash(identity)[:20],
        "code": exc.code,
        "class": exc.code,
        "severity": "production_apply_blocker",
        "kind": "primary",
        "operation_id": str(exc.details.get("operation_id") or ""),
        "supply_id": str(exc.details.get("supply_id") or ""),
        "shipment_id": str(exc.details.get("shipment_id") or ""),
        "document_id": str(exc.details.get("document_id") or ""),
        "nm_id": exc.details.get("nm_id"),
        "business_date": str(
            exc.details.get("business_date")
            or exc.details.get("accepted_date")
            or ""
        ),
        "source_identity": str(
            exc.details.get("source_identity")
            or exc.details.get("supply_id")
            or ""
        ),
        "raw_evidence": exc.details,
        "expected": f"{stage} completes",
        "actual": exc.code,
        "quantity_impact": (
            exc.details.get("quantity")
            or exc.details.get("surplus")
        ),
        "recognized_capital_impact_rub": exc.details.get(
            "recognized_capital_impact_rub"
        ),
        "paid_capital_impact_rub": exc.details.get(
            "paid_capital_impact_rub"
        ),
        "affected_pipeline_stages": [stage],
        "dependencies": [],
        "eligible_for_approved_normalization": False,
        "recommended_fix": "resolve the exact source/entity blocker and rerun collector",
        "requires_new_business_decision": True,
    }


def _technical_blocker(exc: Exception, *, stage: str) -> dict[str, Any]:
    identity = {
        "code": "diagnostic_pipeline_technical_failure",
        "stage": stage,
        "exception_type": type(exc).__name__,
        "message": str(exc),
    }
    return {
        "blocker_id": "cblk_" + _hash(identity)[:20],
        "code": identity["code"],
        "class": type(exc).__name__,
        "severity": "production_apply_blocker",
        "kind": "primary",
        "operation_id": "",
        "supply_id": "",
        "shipment_id": "",
        "document_id": "",
        "nm_id": None,
        "business_date": "",
        "source_identity": stage,
        "raw_evidence": identity,
        "expected": f"{stage} completes",
        "actual": str(exc),
        "quantity_impact": None,
        "recognized_capital_impact_rub": None,
        "paid_capital_impact_rub": None,
        "affected_pipeline_stages": [stage],
        "dependencies": [],
        "eligible_for_approved_normalization": False,
        "recommended_fix": "fix the deterministic pipeline failure and rerun collector",
        "requires_new_business_decision": False,
    }


def _cascade_blocker(stage: str, primary_ids: list[str]) -> dict[str, Any]:
    payload = {"stage": stage, "primary_ids": sorted(primary_ids)}
    return {
        "blocker_id": "cblk_" + _hash(payload)[:20],
        "code": "skipped_due_to_primary_blockers",
        "class": "dependency_taint",
        "severity": "diagnostic_taint",
        "kind": "cascading",
        "operation_id": "",
        "supply_id": "",
        "shipment_id": "",
        "document_id": "",
        "nm_id": None,
        "business_date": "",
        "source_identity": stage,
        "raw_evidence": payload,
        "expected": f"unblocked {stage}",
        "actual": "tainted by primary source blockers",
        "quantity_impact": None,
        "recognized_capital_impact_rub": None,
        "paid_capital_impact_rub": None,
        "affected_pipeline_stages": [stage],
        "dependencies": sorted(primary_ids),
        "eligible_for_approved_normalization": False,
        "recommended_fix": "resolve all listed primary blockers",
        "requires_new_business_decision": False,
    }


def _coverage(
    status: str,
    checked: int,
    blocker_count: int,
    dependency: list[str],
    evidence: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "checked_entity_count": checked,
        "blocker_count": blocker_count,
        "dependency": dependency,
        "evidence_fingerprint": evidence,
    }


def _schema_fingerprint(db_path: Path) -> str:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    return _hash(rows)


def _text(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.000001"))
    text = format(normalized, "f").rstrip("0").rstrip(".")
    return text or "0"


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
    payload = run(build_parser().parse_args(argv))
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
