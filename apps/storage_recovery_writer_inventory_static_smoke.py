#!/usr/bin/env python3
"""Fail closed when an unclassified production SQLite backup writer appears."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.root_storage_policy import load_policy  # noqa: E402
from apps.root_storage_policy_smoke import main as root_storage_policy_smoke  # noqa: E402


# Machine-readable ownership/cadence inventory. A full-store writer is allowed
# only behind a one-shot reviewed mutation/migration boundary; scheduled paths
# must be domain/before-image recovery or bounded artifact GC.
WRITERS = [
    {
        "owner": "warehouse_recovery_policy",
        "source": "packages/application/warehouse_recovery_policy.py",
        "cadence": "hourly_and_manual",
        "artifact": "T2 bounded warehouse/cost domain checkpoint",
        "full_monolith": False,
        "guard": "registry CAS + exact fingerprint + writer lock",
        "lifecycle": "automatic age/count/byte retention",
    },
    {
        "owner": "calculation_parameters_and_economics",
        "source": "packages/application/calculation_parameters.py",
        "cadence": "operator_and_dependent_publication",
        "artifact": "T1 target-scoped before images",
        "full_monolith": False,
        "guard": "exact preview/publication fingerprint + writer lock",
        "lifecycle": "registry rollback retention",
    },
    {
        "owner": "promo_collector_artifacts",
        "source": "apps/promo_campaign_archive_gc.py",
        "cadence": "refresh_and_operator_gc",
        "artifact": "workbook/debug artifacts; never runtime SQLite",
        "full_monolith": False,
        "guard": "normalized persistence/hash proof + unknown/run protection",
        "lifecycle": "bounded light GC + exact audited full GC",
    },
    {
        "owner": "registry_sqlite_backup_primitive",
        "source": "packages/application/registry_upload_db_backed_runtime.py",
        "cadence": "primitive_only",
        "artifact": "coherent SQLite copy",
        "full_monolith": True,
        "guard": "callers must be classified; warehouse T3 allowlist only",
        "lifecycle": "caller-owned",
    },
    {
        "owner": "finance_storage_split_coherent_source",
        "source": "packages/application/finance_storage_migration.py",
        "cadence": "human_authorized_one_shot",
        "artifact": "immutable coherent migration-source SQLite copy",
        "full_monolith": True,
        "guard": (
            "exact snapshot plan + HTTP barrier + writer hold + "
            "offline full integrity"
        ),
        "lifecycle": (
            "retained through cutover observation; separate exact "
            "retirement gate"
        ),
    },
    {
        "owner": "finance_rollback_candidate",
        "source": "packages/application/finance_storage_migration.py",
        "cadence": "human_authorized_rollback_prepare",
        "artifact": "temporary legacy monolith rollback candidate",
        "full_monolith": True,
        "guard": "reviewed rollback plan + exact generation filesystem + approval reference",
        "lifecycle": "candidate state machine and explicit rollback terminalization",
    },
    {
        "owner": "finance_post_cutover_backup_rotation",
        "source": "packages/application/finance_storage_backup_rotation.py",
        "cadence": "daily_due_check_weekly_max_full",
        "artifact": "one coherent raw+operational split restore-set",
        "full_monolith": False,
        "guard": (
            "exact manifest/device/inventory CAS + one-current selector + "
            "integrity/FK/logical/restore readback"
        ),
        "lifecycle": (
            "count=1; temporary count=2 only until atomic replacement; "
            "hard byte/age/capacity limits"
        ),
    },
    {
        "owner": "finance_legacy_helper",
        "source": "apps/wb_finance_weekly.py",
        "cadence": "unreferenced_legacy_helper",
        "artifact": "coherent SQLite copy",
        "full_monolith": True,
        "guard": "no production call site",
        "lifecycle": "legacy sanitation",
    },
    {
        "owner": "ads_historical_recovery",
        "source": "packages/application/ads_historical_recovery.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "external exact plan + approval reference",
        "lifecycle": "legacy sanitation after terminal proof",
    },
    {
        "owner": "supplier_26gn390_recovery",
        "source": "apps/supplier_26gn390_recovery.py",
        "cadence": "retired_human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "legacy exact recovery runner; no schedule",
        "lifecycle": "minimal verified compressed evidence",
    },
    {
        "owner": "supplier_cny_payment_10_recovery",
        "source": "apps/supplier_cny_payment_10_recovery.py",
        "cadence": "retired_human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "legacy exact recovery runner; no schedule",
        "lifecycle": "minimal verified compressed evidence",
    },
    {
        "owner": "supplier_26gn527_legacy_apply",
        "source": "apps/supplier_26gn527_bank_statement_recovery.py",
        "cadence": "disabled",
        "artifact": "none",
        "full_monolith": False,
        "guard": "apply raises and points to unified bounded recovery",
        "lifecycle": "legacy raw evidence sanitation only",
    },
    {
        "owner": "ff_transit_cost_recovery",
        "source": "apps/ff_reservations_transit_cost_recovery.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "exact reviewed recovery plan; no schedule",
        "lifecycle": "family sanitation after terminal proof",
    },
    {
        "owner": "ff_pool_overhead_backfill",
        "source": "packages/application/ff_pool_overhead_backfill.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "exact deployed SHA + reviewed five-document manifest + apply gate",
        "lifecycle": "private manifest-bound evidence retained through reconciliation",
    },
    {
        "owner": "supplier_factual_date_correction",
        "source": "packages/application/supplier_shipment_factual_correction.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "exact correction plan + readback",
        "lifecycle": "verified compressed generation",
    },
    {
        "owner": "registry_operational_store",
        "source": "packages/application/supplier_shipment_factual_correction.py",
        "cadence": "human_gated_verified_restore",
        "artifact": "in-place coherent operational-store restore",
        "full_monolith": True,
        "guard": "verified backup identity + inode-preserving restore readback",
        "lifecycle": "essential bounded business recovery; no new retained copy",
    },
    {
        "owner": "autoanswers_first_schema",
        "source": "packages/application/wb_autoanswers_runtime.py",
        "cadence": "once_per_schema_version",
        "artifact": "coherent pre-schema SQLite copy",
        "full_monolith": True,
        "guard": "first schema activation only + rolling recovery",
        "lifecycle": "compressed per-version retention",
    },
    {
        "owner": "autoanswers_activation_candidate",
        "source": "apps/wb_autoanswers_activation.py",
        "cadence": "deploy_candidate_only",
        "artifact": "temporary candidate SQLite",
        "full_monolith": True,
        "guard": "private candidate lifecycle; not retained routine backup",
        "lifecycle": "temporary candidate cleanup",
    },
    {
        "owner": "proxy_margin_historical_backfill",
        "source": "apps/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py",
        "cadence": "retired_human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "exact historical backfill fingerprint; no schedule",
        "lifecycle": "minimal verified compressed evidence",
    },
    {
        "owner": "buyout_mature_backfill",
        "source": "apps/sheet_vitrina_v1_buyout_mature_backfill.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "reviewed manifest + exact deployed SHA + approval reference",
        "lifecycle": "private evidence retained through reconciliation",
    },
    {
        "owner": "proxy_v4_transit_repair",
        "source": "apps/sheet_vitrina_v1_proxy_v4_transit_repair.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "reviewed manifest + exact deployed SHA + approval reference",
        "lifecycle": "private evidence retained through reconciliation",
    },
    {
        "owner": "sheet_vitrina_exact_date_recovery",
        "source": "apps/sheet_vitrina_v1_temporal_closure_retry_live.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "query-only manifest + exact deployed SHA + immutable human gate; no explicit-date schedule",
        "lifecycle": "private evidence retained through reconciliation or reviewed restore",
    },
    {
        "owner": "promo_metric_eligibility_recompute",
        "source": "apps/promo_metric_eligibility_recompute.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "explicit apply; no schedule",
        "lifecycle": "minimal verified compressed evidence",
    },
    {
        "owner": "spp_metric_recompute",
        "source": "apps/spp_metric_recompute.py",
        "cadence": "human_gated_one_shot",
        "artifact": "coherent pre-mutation SQLite copy",
        "full_monolith": True,
        "guard": "explicit apply; no schedule",
        "lifecycle": "family sanitation after terminal proof",
    },
    {
        "owner": "canonical_cost_diagnostic_candidate",
        "source": "apps/canonical_cost_engine_diagnostic.py",
        "cadence": "diagnostic_temporary",
        "artifact": "temporary candidate SQLite",
        "full_monolith": True,
        "guard": "temporary directory only",
        "lifecycle": "temporary cleanup",
    },
    {
        "owner": "canonical_cost_preflight_candidate",
        "source": "apps/canonical_cost_engine_preflight.py",
        "cadence": "preflight_temporary",
        "artifact": "temporary candidate SQLite",
        "full_monolith": True,
        "guard": "temporary directory only",
        "lifecycle": "temporary cleanup",
    },
]


def build_inventory() -> dict[str, object]:
    classified_sources = {str(item["source"]) for item in WRITERS}
    observed = set()
    backup_call_counts: dict[str, int] = {}
    admission_call_counts: dict[str, int] = {}
    backup_entrypoints: list[dict[str, object]] = []
    for base in ("apps", "packages"):
        for path in (ROOT / base).rglob("*.py"):
            relative = str(path.relative_to(ROOT))
            if (
                relative.endswith("_smoke.py")
                or relative.endswith("_test.py")
                or "/tests/" in relative
            ):
                continue
            source = path.read_text(encoding="utf-8")
            backup_count, admission_count = _writer_call_counts(source)
            if backup_count:
                observed.add(relative)
                backup_call_counts[relative] = backup_count
                admission_call_counts[relative] = admission_count
                backup_entrypoints.extend(_writer_entrypoints(source, relative))
    unclassified = sorted(observed - classified_sources)
    missing = sorted(
        source for source in classified_sources if not (ROOT / source).is_file()
    )
    producer_registry = {
        str(item["owner"]): str(item["classification"])
        for item in load_policy()["producers"]
    }
    unregistered_owners = sorted(
        str(item["owner"])
        for item in WRITERS
        if str(item["owner"]) not in producer_registry
    )
    admission_missing = sorted(
        {
            str(item["source"])
            for item in backup_entrypoints
            if not bool(item["admission_covered"])
        }
    )
    registered_owner_ids = set(producer_registry)
    unregistered_entrypoint_owners = sorted(
        {
            owner
            for item in backup_entrypoints
            for owner in item["admission_owners"]
            if not str(owner).startswith("$") and owner not in registered_owner_ids
        }
    )
    allowed_dynamic_owners = {
        (
            "packages/application/registry_upload_db_backed_runtime.py",
            "backup_database",
            "$admission_owner",
        )
    }
    unbounded_dynamic_entrypoint_owners = sorted(
        {
            (str(item["source"]), str(item["function"]), str(owner))
            for item in backup_entrypoints
            for owner in item["admission_owners"]
            if str(owner).startswith("$")
            and (str(item["source"]), str(item["function"]), str(owner))
            not in allowed_dynamic_owners
        }
    )
    scheduled_full = [
        item
        for item in WRITERS
        if bool(item["full_monolith"])
        and str(item["cadence"]) in {"hourly", "daily", "hourly_and_manual"}
    ]
    return {
        "contract_name": "storage_recovery_writer_inventory_v1",
        "writers": WRITERS,
        "observed_backup_call_sources": sorted(observed),
        "unclassified_backup_call_sources": unclassified,
        "catalog_sources_missing": missing,
        "unregistered_root_storage_owners": unregistered_owners,
        "backup_call_counts": backup_call_counts,
        "admission_call_counts": admission_call_counts,
        "backup_entrypoints": backup_entrypoints,
        "large_write_admission_missing": admission_missing,
        "unregistered_entrypoint_admission_owners": unregistered_entrypoint_owners,
        "unbounded_dynamic_entrypoint_admission_owners": unbounded_dynamic_entrypoint_owners,
        "scheduled_full_monolith_writers": scheduled_full,
        "routine_full_monolith_count": len(scheduled_full),
        "status": (
            "ready"
            if not unclassified
            and not missing
            and not scheduled_full
            and not unregistered_owners
            and not unregistered_entrypoint_owners
            and not unbounded_dynamic_entrypoint_owners
            and not admission_missing
            else "failed"
        ),
    }


def _writer_call_counts(source: str) -> tuple[int, int]:
    tree = ast.parse(source)
    backup_count = 0
    admission_count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "backup":
            backup_count += 1
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "backup_database":
            backup_count += 1
            if any(keyword.arg == "admission_owner" for keyword in node.keywords):
                admission_count += 1
        elif isinstance(node.func, ast.Name) and node.func.id == "backup_database":
            backup_count += 1
            if any(keyword.arg == "admission_owner" for keyword in node.keywords):
                admission_count += 1
        elif isinstance(node.func, ast.Name) and node.func.id == "admit_root_write":
            admission_count += 1
    return backup_count, admission_count


def _writer_entrypoints(source: str, relative: str) -> list[dict[str, object]]:
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def containing_function(node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = parents.get(current)
        return None

    def value_name(node: ast.AST | None) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return "$" + node.id
        return "$dynamic"

    admissions: dict[ast.AST | None, list[tuple[int, str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "admit_root_write":
            owner_keyword = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "owner"),
                None,
            )
            admissions.setdefault(containing_function(node), []).append(
                (int(node.lineno), value_name(owner_keyword))
            )

    entries: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        primitive = ""
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "backup",
            "backup_database",
        }:
            primitive = node.func.attr
        elif isinstance(node.func, ast.Name) and node.func.id == "backup_database":
            primitive = node.func.id
        if not primitive:
            continue
        function = containing_function(node)
        inline_owner = next(
            (
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "admission_owner"
            ),
            None,
        )
        admission_owners: list[str] = []
        admission_lines: list[int] = []
        if inline_owner is not None:
            admission_owners.append(value_name(inline_owner))
            admission_lines.append(int(node.lineno))
        else:
            for line, owner in admissions.get(function, []):
                if line < int(node.lineno):
                    admission_lines.append(line)
                    admission_owners.append(owner)
        entries.append(
            {
                "source": relative,
                "function": None if function is None else function.name,
                "backup_line": int(node.lineno),
                "primitive": primitive,
                "admission_lines": sorted(set(admission_lines)),
                "admission_owners": sorted(set(admission_owners)),
                "admission_covered": bool(admission_lines),
            }
        )
    return sorted(entries, key=lambda item: (int(item["backup_line"]), str(item["primitive"])))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not args.json and root_storage_policy_smoke() != 0:
        return 1
    result = build_inventory()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] != "ready":
        if not args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 1
    if not args.json:
        print(
            "storage_recovery_writer_inventory_static_smoke: ok "
            f"({len(WRITERS)} classified writers, routine full monolith 0)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
