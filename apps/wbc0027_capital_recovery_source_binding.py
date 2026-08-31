#!/usr/bin/env python3
"""Closed source binding for the WBC0027 finalize-only runtime boundary."""

from __future__ import annotations


CONTRACT_NAME = "wbc0027_reconciliation_runtime_source_binding/v1"
WORKFLOW_PATH = ".github/workflows/production-apply.yml"

# These are the executable owners of the trusted GitHub receipt validation and
# the deployed finalize-only readback boundary.  A repo-only workflow bridge may
# differ from the deployed release only while every one of these Git blobs is
# byte-identical.  Changing this list changes this module and therefore selects
# the live-runtime WBC0027 release lane itself.
PATHS = (
    "apps/github_release_runner.py",
    "apps/production_apply_runner.py",
    "apps/release_protocol.py",
    "apps/wbc0027_capital_recovery.py",
    "ci/test_planner.py",
    "packages/application/registry_upload_db_backed_runtime.py",
    "packages/application/root_storage_policy.py",
    "packages/application/sqlite_contention.py",
    "packages/application/storage_registry.py",
    "packages/application/warehouse_business_projection.py",
    "packages/application/warehouse_functional_lock.py",
    "packages/application/warehouse_recovery_policy.py",
    "packages/application/warehouse_sync_lock.py",
)

if len(PATHS) != len(set(PATHS)) or tuple(sorted(PATHS)) != PATHS:
    raise RuntimeError("WBC0027 reconciliation source paths must be unique and sorted")
