#!/usr/bin/env python3
"""Replay protocol-v2 selector coverage across recent merged main commits."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import test_planner as planner


PR_RE = re.compile(r"\(#([1-9][0-9]*)\)$")
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


def recent_pr_commits(limit: int) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    for sha in git("rev-list", f"--max-count={limit * 3}", "origin/main").splitlines():
        subject = git("show", "-s", "--format=%s", sha)
        match = PR_RE.search(subject)
        if match:
            rows.append((int(match.group(1)), sha, subject))
        if len(rows) >= limit:
            break
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.limit <= 0:
        raise ValueError("limit must be positive")

    head_registry, head_digest = planner.load_registry_at(git("rev-parse", "HEAD"))
    if head_registry is None:
        raise ValueError("current checkout lacks the protocol-v2 registry")
    records: list[dict[str, Any]] = []
    uncovered: set[str] = set()
    full_fallback_count = 0
    for pr_number, commit, subject in recent_pr_commits(args.limit):
        parents = git("show", "-s", "--format=%P", commit).split()
        if not parents:
            continue
        base = parents[0]
        base_registry, base_digest = planner.load_registry_at(base)
        changes = planner.changed_paths(base, commit)
        plan = planner.build_plan(
            pr_number=pr_number,
            base_sha=base,
            head_sha=commit,
            base_registry=base_registry,
            head_registry=head_registry,
            base_registry_blob_sha256=base_digest,
            head_registry_blob_sha256=head_digest,
            changes=changes,
        )
        planner.verify_plan(plan)
        uncovered.update(plan["unknown_paths"])
        if "unknown-path-full-regression" in plan["reason_codes"]:
            full_fallback_count += 1
        records.append(
            {
                "pr": pr_number,
                "commit": commit,
                "subject": subject,
                "changed_path_count": len(changes),
                "selected_suites": plan["selected_suites"],
                "release_kind": plan["release_plan"]["kind"],
                "unknown_paths": plan["unknown_paths"],
                "plan_hash": plan["plan_hash"],
            }
        )

    incident_results = []
    for index, path in enumerate(INCIDENT_PATHS, start=1):
        plan = planner.build_plan(
            pr_number=900000 + index,
            base_sha="a" * 40,
            head_sha="b" * 40,
            base_registry=head_registry,
            head_registry=head_registry,
            changes=[{"status": "M", "path": path}],
        )
        if plan["release_plan"]["kind"] == "repo_only":
            raise AssertionError(f"incident path was classified repo-only: {path}")
        if not ({"finance_storage", "warehouse_recovery", "fulfillment"} & set(plan["selected_suites"])):
            raise AssertionError(f"incident path lacks safety-suite coverage: {path}")
        incident_results.append({"path": path, "selected_suites": plan["selected_suites"], "release_kind": plan["release_plan"]["kind"]})

    result = {
        "schema": "wb-core.test-selection-replay/v2",
        "requested_limit": args.limit,
        "replayed_pr_count": len(records),
        "full_regression_fallback_count": full_fallback_count,
        "uncovered_paths": sorted(uncovered),
        "coverage_policy": "unknown paths are covered by automatic full regression",
        "incident_paths": incident_results,
        "records": records,
    }
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if len(records) < min(args.limit, 100):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
