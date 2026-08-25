#!/usr/bin/env python3
"""Replay deterministic before/after selector coverage on merged PR commits."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import test_planner as planner


PR_RE = re.compile(r"\(#([1-9][0-9]*)\)$")
DEFAULT_EXPLICIT_PRS = (1042, 1043, 1044)
CURRENT_TREE_AUDIT_ROOTS = ("apps", "packages", "gas")
INCIDENT_PATHS = (
    "packages/application/finance_storage_split.py",
    "packages/application/warehouse_recovery_policy.py",
    "apps/registry_upload_http_entrypoint_hosted_runtime.py",
    "apps/business_data_maintenance.py",
    "apps/ff_pool_cutover_production.py",
)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def commit_row(sha: str) -> tuple[int, str, str] | None:
    subject = git("show", "-s", "--format=%s", sha)
    match = PR_RE.search(subject)
    if match is None:
        return None
    return int(match.group(1)), sha, subject


def recent_pr_commits(limit: int, explicit_prs: set[int]) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    seen: set[int] = set()
    for sha in git("rev-list", f"--max-count={limit * 5}", "origin/main").splitlines():
        row = commit_row(sha)
        if row is None or row[0] in seen:
            continue
        rows.append(row)
        seen.add(row[0])
        if len(rows) >= limit and explicit_prs <= seen:
            break
    missing = explicit_prs - seen
    if missing:
        raise AssertionError(f"explicit replay PR commits are unavailable on origin/main: {sorted(missing)}")
    return rows[:limit]


def load_registry_bytes(raw: bytes, source: str) -> tuple[dict[str, Any], str]:
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{source} registry is not an object")
    planner.validate_registry(value, source)
    return value, hashlib.sha256(raw).hexdigest()


def registry_at(ref: str) -> tuple[dict[str, Any], str]:
    raw = subprocess.run(
        ["git", "show", f"{ref}:{planner.REGISTRY_PATH}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return load_registry_bytes(raw, f"registry at {ref}")


def candidate_registry() -> tuple[dict[str, Any], str]:
    raw = (ROOT / planner.REGISTRY_PATH).read_bytes()
    return load_registry_bytes(raw, "candidate registry")


def selection_plan(
    *,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    changes: list[dict[str, str]],
    registry: Mapping[str, Any],
    registry_digest: str,
) -> dict[str, Any]:
    plan = planner.build_plan(
        pr_number=pr_number,
        base_sha=base_sha,
        head_sha=head_sha,
        base_registry=registry,
        head_registry=registry,
        base_registry_blob_sha256=registry_digest,
        head_registry_blob_sha256=registry_digest,
        changes=changes,
    )
    planner.verify_plan(plan)
    return plan


def summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "selected_suites": plan["selected_suites"],
        "groups": plan["groups"],
        "release_kind": plan["release_plan"]["kind"],
        "unknown_paths": plan["unknown_paths"],
        "reason_codes": plan["reason_codes"],
        "plan_hash": plan["plan_hash"],
    }


def unexplained_unknowns(
    plan: Mapping[str, Any], registry: Mapping[str, Any]
) -> list[str]:
    if not plan["unknown_paths"]:
        return []
    full = set(registry["protocol"]["full_regression_suites"])
    reasons = set(plan["reason_codes"])
    selected = set(plan["selected_suites"])
    if "unknown-path-full-regression" not in reasons or selected != full:
        return list(plan["unknown_paths"])
    return [
        path
        for path in plan["unknown_paths"]
        if path.startswith(("apps/", "packages/", "gas/"))
        and plan["release_plan"]["kind"] == "repo_only"
    ]


def is_full_regression(
    plan: Mapping[str, Any], registry: Mapping[str, Any]
) -> bool:
    return set(plan["selected_suites"]) == set(
        registry["protocol"]["full_regression_suites"]
    )


def _unmapped_paths(
    paths: list[str], registry: Mapping[str, Any]
) -> list[str]:
    return sorted(
        path
        for path in paths
        if not any(planner._rule_matches(path, rule) for rule in registry["rules"])
    )


def current_tree_mapping_audit(
    registry: Mapping[str, Any], baseline_ref: str
) -> dict[str, Any]:
    current_paths = [
        path
        for path in git("ls-files", *CURRENT_TREE_AUDIT_ROOTS).splitlines()
        if path
    ]
    baseline_paths = [
        path
        for path in git(
            "ls-tree", "-r", "--name-only", baseline_ref, "--", *CURRENT_TREE_AUDIT_ROOTS
        ).splitlines()
        if path
    ]
    current_residual = _unmapped_paths(current_paths, registry)
    baseline_residual = _unmapped_paths(baseline_paths, registry)
    new_residual = sorted(set(current_residual) - set(baseline_residual))
    resolved_or_removed = sorted(set(baseline_residual) - set(current_residual))
    return {
        "scope": list(CURRENT_TREE_AUDIT_ROOTS),
        "baseline_ref": baseline_ref,
        "baseline_tracked_path_count": len(baseline_paths),
        "baseline_specifically_mapped_path_count": (
            len(baseline_paths) - len(baseline_residual)
        ),
        "baseline_unmapped_residual_count": len(baseline_residual),
        "baseline_unmapped_residual_paths": baseline_residual,
        "tracked_path_count": len(current_paths),
        "specifically_mapped_path_count": (
            len(current_paths) - len(current_residual)
        ),
        "unmapped_residual_count": len(current_residual),
        "unmapped_residual_paths": current_residual,
        "new_unmapped_residual_paths": new_residual,
        "resolved_or_removed_unmapped_residual_paths": resolved_or_removed,
        "policy": (
            "candidate registry applied to baseline and current tracked trees; no new "
            "unmapped residual path is allowed, while mapping or removing legacy debt is "
            "allowed; every unmatched change remains fail-closed full regression and "
            "unmatched code remains at least live_runtime"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--baseline-registry-ref", default="origin/main")
    parser.add_argument("--explicit-pr", type=int, action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.limit < 100:
        raise ValueError("limit must be at least 100")

    explicit_prs = set(DEFAULT_EXPLICIT_PRS) | set(args.explicit_pr)
    before_registry, before_digest = registry_at(args.baseline_registry_ref)
    after_registry, after_digest = candidate_registry()
    records: list[dict[str, Any]] = []
    unexplained: set[str] = set()
    before_unknown: set[str] = set()
    after_unknown: set[str] = set()
    replayed_current_unmapped: set[str] = set()
    historical_removed: set[str] = set()
    targeted_to_full: list[dict[str, Any]] = []
    historical_targeted_to_full: list[dict[str, Any]] = []
    before_full_count = 0
    after_full_count = 0
    for pr_number, commit, subject in recent_pr_commits(args.limit, explicit_prs):
        parents = git("show", "-s", "--format=%P", commit).split()
        if not parents:
            continue
        base = parents[0]
        changes = planner.changed_paths(base, commit)
        before = selection_plan(
            pr_number=pr_number,
            base_sha=base,
            head_sha=commit,
            changes=changes,
            registry=before_registry,
            registry_digest=before_digest,
        )
        after = selection_plan(
            pr_number=pr_number,
            base_sha=base,
            head_sha=commit,
            changes=changes,
            registry=after_registry,
            registry_digest=after_digest,
        )
        before_unknown.update(before["unknown_paths"])
        after_unknown.update(after["unknown_paths"])
        unexplained.update(unexplained_unknowns(after, after_registry))
        current_unknown = sorted(
            path for path in after["unknown_paths"] if (ROOT / path).exists()
        )
        historical_unknown = sorted(
            path for path in after["unknown_paths"] if not (ROOT / path).exists()
        )
        replayed_current_unmapped.update(current_unknown)
        historical_removed.update(historical_unknown)
        before_full = is_full_regression(before, before_registry)
        after_full = is_full_regression(after, after_registry)
        if before_full:
            before_full_count += 1
        if after_full:
            after_full_count += 1
        regression = {
            "pr": pr_number,
            "commit": commit,
            "subject": subject,
            "replayed_current_unmapped_paths": current_unknown,
            "historical_removed_paths": historical_unknown,
            "before": summary(before),
            "after": summary(after),
        }
        if not before_full and after_full:
            if current_unknown or not historical_unknown:
                targeted_to_full.append(regression)
            else:
                historical_targeted_to_full.append(regression)
        before_summary = summary(before)
        after_summary = summary(after)
        records.append(
            {
                "pr": pr_number,
                "commit": commit,
                "subject": subject,
                "changed_path_count": len(changes),
                "changed_paths": changes,
                "before": before_summary,
                "after": after_summary,
                "selection_changed": before_summary != after_summary,
            }
        )

    explicit_records = {
        str(pr): next((record for record in records if record["pr"] == pr), None)
        for pr in sorted(explicit_prs)
    }
    missing_explicit = [pr for pr, record in explicit_records.items() if record is None]
    if missing_explicit:
        raise AssertionError(f"explicit PRs missing from bounded replay: {missing_explicit}")

    incident_results = []
    for index, path in enumerate(INCIDENT_PATHS, start=1):
        plan = selection_plan(
            pr_number=900000 + index,
            base_sha="a" * 40,
            head_sha="b" * 40,
            changes=[{"status": "M", "path": path}],
            registry=after_registry,
            registry_digest=after_digest,
        )
        if plan["release_plan"]["kind"] == "repo_only":
            raise AssertionError(f"incident path was classified repo-only: {path}")
        if not (
            {"business_data_safety", "finance_storage", "warehouse_recovery", "fulfillment"}
            & set(plan["selected_suites"])
        ):
            raise AssertionError(f"incident path lacks safety-suite coverage: {path}")
        incident_results.append({"path": path, **summary(plan)})

    current_tree_audit = current_tree_mapping_audit(
        after_registry, args.baseline_registry_ref
    )
    result = {
        "schema": "wb-core.test-selection-replay/v4",
        "requested_limit": args.limit,
        "replayed_pr_count": len(records),
        "baseline_registry_ref": args.baseline_registry_ref,
        "baseline_registry_sha256": before_digest,
        "candidate_registry_sha256": after_digest,
        "before_full_regression_count": before_full_count,
        "after_full_regression_count": after_full_count,
        "before_unknown_full_fallback_paths": sorted(before_unknown),
        "after_unknown_full_fallback_paths": sorted(after_unknown),
        "replayed_current_unmapped_paths": sorted(replayed_current_unmapped),
        "historical_removed_unknown_paths": sorted(historical_removed),
        "new_targeted_to_full_regressions": targeted_to_full,
        "historical_targeted_to_full_regressions": historical_targeted_to_full,
        "replayed_unexplained_uncovered_paths": sorted(
            unexplained | replayed_current_unmapped
        ),
        "current_tree_mapping_audit": current_tree_audit,
        "coverage_policy": "unknown paths select full regression and unknown code remains live_runtime",
        "explicit_prs": explicit_records,
        "incident_paths": incident_results,
        "records": records,
    }
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if (
        len(records) < args.limit
        or unexplained
        or replayed_current_unmapped
        or targeted_to_full
        or current_tree_audit["new_unmapped_residual_paths"]
    ):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
