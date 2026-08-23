#!/usr/bin/env python3
"""Fail closed when an unclassified production SQLite backup writer appears."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
BACKUP_CALL = re.compile(r"(?:\.backup\(|backup_database\()")

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
        "owner": "ff_pool_fbs_forward_recovery_preview",
        "source": "packages/application/ff_pool_fbs_forward_recovery.py",
        "cadence": "human_gated_one_shot",
        "artifact": "temporary in-memory query-only planning copy",
        "full_monolith": True,
        "guard": (
            "mode=ro/query_only source + disposable memory after-image + "
            "exact C target manifest"
        ),
        "lifecycle": "process-local only; never persisted as a runtime backup",
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
            if BACKUP_CALL.search(source):
                observed.add(relative)
    unclassified = sorted(observed - classified_sources)
    missing = sorted(
        source for source in classified_sources if not (ROOT / source).is_file()
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
        "scheduled_full_monolith_writers": scheduled_full,
        "routine_full_monolith_count": len(scheduled_full),
        "status": (
            "ready"
            if not unclassified and not missing and not scheduled_full
            else "failed"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
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
