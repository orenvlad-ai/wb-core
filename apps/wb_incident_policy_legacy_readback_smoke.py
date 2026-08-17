#!/usr/bin/env python3
"""Read-model proof for legacy identities and configured/effective consistency."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sku_management import SkuManagementBlock  # noqa: E402
from packages.application.wb_incident_policy import (  # noqa: E402
    get_latest_policy_state,
    get_policy_state,
)


def main() -> int:
    with TemporaryDirectory(prefix="wb-incident-legacy-readback-") as raw:
        runtime_dir = Path(raw)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.load_latest_wb_incident_policy(seller_id="canonical")
        entries = [
            {
                "warehouse_id": 507,
                "warehouse_name": "Коледино",
                "effective_from": "2026-07-01",
                "effective_to_exclusive": "",
                "source": "incident_policy_v2",
            },
            {
                "warehouse_id": 117986,
                "warehouse_name": "Электросталь",
                "effective_from": "2026-07-12",
                "effective_to_exclusive": "",
                "source": "incident_policy_v2",
            },
        ]
        identities = [
            {"warehouse_id": item["warehouse_id"], "warehouse_name": item["warehouse_name"]}
            for item in entries
        ]
        runtime.append_wb_incident_policy_revision(
            seller_id="canonical",
            active=True,
            warehouse_ids=[507],
            warehouse_identities=identities[:1],
            warehouse_entries=entries[:1],
            reason="historical incident",
            effective_from="2026-07-01",
            effective_to="",
            policy_status="active",
            actor="owner",
            created_at="2026-07-01T08:00:00Z",
            source="incident_policy_v2",
        )
        runtime.append_wb_incident_policy_revision(
            seller_id="canonical",
            active=True,
            warehouse_ids=[507, 117986],
            warehouse_identities=identities,
            warehouse_entries=entries,
            reason="current monitoring policy",
            effective_from="2026-07-12",
            effective_to="",
            policy_status="monitoring",
            actor="owner",
            created_at="2026-07-12T08:00:00Z",
            source="incident_policy_v2",
        )

        historical = get_policy_state(
            runtime,
            snapshot_date="2026-07-05",
            seller_id="canonical",
            include_legacy=False,
        )
        if not historical["active"] or historical["revision"] != 1:
            raise AssertionError(f"pre-cutoff historical semantics changed: {historical}")
        current = get_latest_policy_state(
            runtime,
            snapshot_date="2026-08-17",
            seller_id="canonical",
        )
        if (
            not current["configured_active"]
            or not current["active"]
            or not current["currently_effective"]
            or current["effective_revision"] != 2
        ):
            raise AssertionError(f"configured/effective state contradicts the registry: {current}")
        if current["legacy_warehouse_entries"] != entries:
            raise AssertionError("latest legacy policy lost exact historical entries")

        block = SkuManagementBlock(
            runtime=runtime,
            runtime_dir=runtime_dir,
            prices_block=object(),
            ads_block=object(),
            now_factory=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
        )
        payload = block.get_warehouse_exclusion_settings(user_key="ignored")
        if not payload["active"] or not payload["configured_active"]:
            raise AssertionError(f"server settings expose opposite active state: {payload}")
        if payload["legacy_warehouse_entries"] != entries:
            raise AssertionError("settings response lost legacy warehouse identities")
        history = payload["revision_history"]
        if [item["revision"] for item in history] != [2, 1]:
            raise AssertionError(f"append-only revision order changed: {history}")
        if history[0]["warehouse_entries"] != entries or history[1]["warehouse_entries"] != entries[:1]:
            raise AssertionError(f"revision history lost exact names/dates: {history}")
        if payload["effective_excluded_wb_warehouse_ids"] != [507, 117986]:
            raise AssertionError("active retained policy must expose its exact effective IDs")

    print("wb_incident_policy_legacy_readback_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
