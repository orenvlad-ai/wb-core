#!/usr/bin/env python3
"""Smoke the owner-paused Web Vitrina FBS last-good read path."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import playwright.sync_api  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name not in {"playwright", "playwright.sync_api"}:
        raise
    playwright_module = ModuleType("playwright")
    sync_api_module = ModuleType("playwright.sync_api")
    for name in ("Browser", "BrowserContext", "Download", "Page", "Playwright"):
        setattr(sync_api_module, name, Any)

    def _playwright_not_used() -> Any:
        raise RuntimeError("playwright is not used by this contract smoke")

    sync_api_module.sync_playwright = _playwright_not_used
    playwright_module.sync_api = sync_api_module
    sys.modules["playwright"] = playwright_module
    sys.modules["playwright.sync_api"] = sync_api_module

from apps.sheet_vitrina_v1_inventory_planning_smoke import (  # noqa: E402
    CURRENT_DATE,
    NOW,
    _seed_inventory_planning,
)
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.application.sheet_vitrina_v1_inventory_planning import (  # noqa: E402
    COMBINED_TOTAL_ALIAS_KEY,
    INVENTORY_WB_TOTAL_KEY,
    inventory_planning_facility_metric_key,
)
from packages.application.web_vitrina_fbs_lifecycle_last_good import (  # noqa: E402
    CACHE_FILENAME,
    FbsLifecycleQualityCacheAdmissionError,
    OWNER_POLICY_FILENAME,
    OWNER_POLICY_SCHEMA,
    QUALITY_CONTRACT,
    _fingerprint,
    build_and_publish_cache,
)


def _forbidden_live_scan(*_: object, **__: object) -> dict[str, object]:
    raise AssertionError("interactive Web Vitrina must not scan live FBS lifecycle backlog")


def _build(fixture: LocalWebVitrinaFixtureServer):
    with (
        patch(
            "packages.application.inventory_planning_read_model.fbs_lifecycle_quality_coverage",
            side_effect=_forbidden_live_scan,
        ),
        patch(
            "packages.application.sheet_vitrina_v1_inventory_history.fbs_lifecycle_quality_coverage",
            side_effect=_forbidden_live_scan,
        ),
        patch(
            "packages.application.ff_pool_fbs_lifecycle.fbs_lifecycle_quality_coverage",
            side_effect=_forbidden_live_scan,
        ),
    ):
        return fixture.entrypoint.web_vitrina_block.build(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-08",
            date_to=CURRENT_DATE,
        )


def main() -> int:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    fixture.__enter__()
    try:
        runtime = fixture.entrypoint.runtime
        enabled = [
            item for item in runtime.load_current_state().config_v2 if item.enabled
        ]
        nm_ids = (int(enabled[0].nm_id), int(enabled[1].nm_id))
        _seed_inventory_planning(runtime.db_path, nm_ids=nm_ids)
        (runtime.runtime_dir / OWNER_POLICY_FILENAME).write_text(
            json.dumps(
                {
                    "schema_version": OWNER_POLICY_SCHEMA,
                    "revision": 61,
                    "master_desired": True,
                    "processes": {"fbs_shadow": {"desired": False}},
                }
            ),
            encoding="utf-8",
        )
        exact_material = {
            "contract": QUALITY_CONTRACT,
            "as_of_date": CURRENT_DATE,
            "status": "exact",
            "groups": [],
        }
        exact_coverage = {
            **exact_material,
            "digest": _fingerprint(exact_material),
        }
        exact_source_state = {
            "cutover_id": "planning-v1-cutover",
            "lifecycle_cursor": 0,
            "max_status_observation_sequence": 0,
            "unmaterialized_status_count": 0,
            "unresolved_pending_count": 0,
        }
        with patch(
            "packages.application.web_vitrina_fbs_lifecycle_last_good.fbs_lifecycle_quality_coverage",
            return_value=exact_coverage,
        ):
            published = build_and_publish_cache(
                runtime.runtime_dir,
                db_path=runtime.db_path,
                generated_at=NOW,
                source_as_of_date=CURRENT_DATE,
            )
        assert published["status"] == "published"
        assert published["source_as_of_date"] == CURRENT_DATE

        contract = _build(fixture)
        rows = {row.row_id: row for row in contract.rows}
        facility_key = inventory_planning_facility_metric_key("moscow")
        facility = rows[f"SKU:{nm_ids[0]}|{facility_key}"]
        assert facility.values_by_date[CURRENT_DATE] == -3
        warning = facility.presentation_by_date[CURRENT_DATE]
        assert warning["quality_state"] == "fbs_last_good_owner_paused"
        assert warning["quality_label"] == "Последние подтверждённые данные"
        assert warning["last_good_at"] == NOW
        assert warning["last_good_source_as_of_date"] == CURRENT_DATE
        combined = rows[f"SKU:{nm_ids[0]}|{COMBINED_TOTAL_ALIAS_KEY}"]
        assert combined.values_by_date[CURRENT_DATE] == 7
        assert combined.presentation_by_date[CURRENT_DATE]["tone"] == "warning"
        wb = rows[f"SKU:{nm_ids[0]}|{INVENTORY_WB_TOTAL_KEY}"]
        assert wb.values_by_date[CURRENT_DATE] == 10
        assert wb.presentation_by_date[CURRENT_DATE]["tone"] != "warning"

        cache_path = runtime.runtime_dir / CACHE_FILENAME
        valid_cache = cache_path.read_bytes()
        corrupt = json.loads(valid_cache)
        corrupt["generated_at"] = "2026-04-21T12:00:01Z"
        cache_path.write_text(json.dumps(corrupt), encoding="utf-8")
        corrupt_contract = _build(fixture)
        corrupt_rows = {row.row_id: row for row in corrupt_contract.rows}
        assert corrupt_rows[
            f"SKU:{nm_ids[0]}|{facility_key}"
        ].values_by_date[CURRENT_DATE] == ""
        cache_path.write_bytes(valid_cache)

        source_mismatch = json.loads(valid_cache)
        source_mismatch["source_state"]["lifecycle_cursor"] = 1
        source_mismatch["source_state"]["max_status_observation_sequence"] = 1
        source_mismatch["admission"]["source_state_digest"] = _fingerprint(
            source_mismatch["source_state"]
        )
        source_mismatch.pop("cache_digest")
        source_mismatch["cache_digest"] = _fingerprint(source_mismatch)
        cache_path.write_text(json.dumps(source_mismatch), encoding="utf-8")
        mismatched_contract = _build(fixture)
        mismatched_rows = {row.row_id: row for row in mismatched_contract.rows}
        assert mismatched_rows[
            f"SKU:{nm_ids[0]}|{facility_key}"
        ].values_by_date[CURRENT_DATE] == ""
        cache_path.write_bytes(valid_cache)

        partial_material = {
            "contract": QUALITY_CONTRACT,
            "as_of_date": CURRENT_DATE,
            "status": "partial",
            "groups": [
                {
                    "facility_id": "moscow",
                    "nm_id": nm_ids[0],
                    "earliest_business_date": CURRENT_DATE,
                    "reason_codes": ["lifecycle_status_not_materialized"],
                    "status_sequence_count": 1,
                    "status_sequence_digest": _fingerprint([11]),
                }
            ],
        }
        partial_coverage = {
            **partial_material,
            "digest": _fingerprint(partial_material),
        }
        with patch(
            "packages.application.web_vitrina_fbs_lifecycle_last_good.fbs_lifecycle_quality_coverage",
            return_value=partial_coverage,
        ):
            try:
                build_and_publish_cache(
                    runtime.runtime_dir,
                    db_path=runtime.db_path,
                    generated_at="2026-04-21T12:00:02Z",
                    source_as_of_date=CURRENT_DATE,
                )
            except FbsLifecycleQualityCacheAdmissionError as exc:
                assert "partial or incomplete" in str(exc)
            else:
                raise AssertionError("partial lifecycle candidate must not publish")
        assert cache_path.read_bytes() == valid_cache

        with (
            patch(
                "packages.application.web_vitrina_fbs_lifecycle_last_good.fbs_lifecycle_quality_coverage",
                return_value=exact_coverage,
            ),
            patch(
                "packages.application.web_vitrina_fbs_lifecycle_last_good.os.replace",
                side_effect=OSError("simulated atomic publication failure"),
            ),
        ):
            try:
                build_and_publish_cache(
                    runtime.runtime_dir,
                    db_path=runtime.db_path,
                    generated_at="2026-04-21T12:00:04Z",
                    source_as_of_date=CURRENT_DATE,
                )
            except OSError as exc:
                assert "simulated atomic publication failure" in str(exc)
            else:
                raise AssertionError("failed atomic replace must not report publication")
        assert cache_path.read_bytes() == valid_cache
        assert not list(runtime.runtime_dir.glob(f".{CACHE_FILENAME}.*.tmp"))

        with (
            patch(
                "packages.application.web_vitrina_fbs_lifecycle_last_good.fbs_lifecycle_quality_coverage",
                side_effect=_forbidden_live_scan,
            ),
            patch(
                "packages.application.web_vitrina_fbs_lifecycle_last_good._source_state",
                return_value={
                    **exact_source_state,
                    "max_status_observation_sequence": 11,
                    "unmaterialized_status_count": 1,
                },
            ),
        ):
            try:
                build_and_publish_cache(
                    runtime.runtime_dir,
                    db_path=runtime.db_path,
                    generated_at="2026-04-21T12:00:03Z",
                    source_as_of_date=CURRENT_DATE,
                )
            except FbsLifecycleQualityCacheAdmissionError as exc:
                assert "not fully materialized" in str(exc)
            else:
                raise AssertionError("incomplete lifecycle source must not be scanned")
        assert cache_path.read_bytes() == valid_cache

        cache_path.unlink()

        def _simulated_legacy_overlay(rows, **_):
            result = []
            legacy_ids = {
                f"SKU:{nm_ids[0]}|{facility_key}",
                f"SKU:{nm_ids[0]}|{COMBINED_TOTAL_ALIAS_KEY}",
            }
            for row in rows:
                if row.row_id in legacy_ids:
                    values = dict(row.values_by_date)
                    values[CURRENT_DATE] = 999
                    result.append(replace(row, values_by_date=values))
                else:
                    result.append(row)
            return result

        with patch(
            "packages.application.sheet_vitrina_v1_web_vitrina.apply_breakglass_last_good_overlay",
            side_effect=_simulated_legacy_overlay,
        ):
            unavailable = _build(fixture)
        unavailable_rows = {row.row_id: row for row in unavailable.rows}
        assert unavailable_rows[
            f"SKU:{nm_ids[0]}|{facility_key}"
        ].values_by_date[CURRENT_DATE] == ""
        assert unavailable_rows[
            f"SKU:{nm_ids[0]}|{facility_key}"
        ].presentation_by_date[CURRENT_DATE]["quality_label"] == "Нет данных"
        assert unavailable_rows[
            f"SKU:{nm_ids[0]}|{INVENTORY_WB_TOTAL_KEY}"
        ].values_by_date[CURRENT_DATE] == 10
        assert unavailable_rows[
            f"SKU:{nm_ids[0]}|{COMBINED_TOTAL_ALIAS_KEY}"
        ].values_by_date[CURRENT_DATE] == ""
    finally:
        fixture.__exit__(None, None, None)
    print("web_vitrina_fbs_lifecycle_last_good_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
