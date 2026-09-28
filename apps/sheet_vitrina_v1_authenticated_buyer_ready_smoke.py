"""Persisted buyer observations publish into the default ready Web Vitrina snapshot."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_spp_proxy_integration_smoke import (  # noqa: E402
    ACTIVATED_AT,
    AS_OF_DATE,
    CURRENT_DATE,
    REQUESTED_NM_IDS,
    _MutableNowFactory,
    _SppProxyScenario,
    _build_entrypoint,
    _bundle,
    _data_rows,
    _today_value,
)
from apps.wb_buyer_authenticated_collect import _requested_ids  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import EXECUTION_MODE_AUTO_DAILY  # noqa: E402
from packages.application.sheet_vitrina_v1_authenticated_buyer import projection_payload  # noqa: E402
from packages.application.vitrina_catalog import reporting_config  # noqa: E402
from packages.contracts.registry_upload_bundle_v1 import ConfigV2Item  # noqa: E402
from packages.application.wb_buyer_authenticated_observations import (  # noqa: E402
    append_observation,
    begin_run,
    classify_observation,
    finish_run,
    load_active_requested_nm_ids,
    load_daily_projection,
    load_source_requested_nm_ids,
)


EXTRA_REPORTING_NM_ID = 999999999


def _reporting_config_with_extra(db_path: Path, configured: object):
    items, scope = reporting_config(db_path, configured)
    return items + [ConfigV2Item(EXTRA_REPORTING_NM_ID, True, "Extra", "fixture", 999)], scope


def _record_run(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    run_id: str,
    auth_reference: str,
    timestamp: str,
    observed_nm_ids: set[int],
    wallet_offset: float = 0.0,
    status: str = "completed",
) -> None:
    assert begin_run(
        runtime,
        run_id=run_id,
        started_at=timestamp,
        requested_nm_ids=REQUESTED_NM_IDS,
    ) == CURRENT_DATE
    for nm_id, baseline, nonwallet, wallet in (
        (REQUESTED_NM_IDS[0], 180.0, 144.0, 139.0),
        (REQUESTED_NM_IDS[1], 200.0, 160.0, 155.0),
    ):
        observed = nm_id in observed_nm_ids
        observation = classify_observation(
            nm_id=nm_id,
            buyer={
                "status": "observed" if observed else "unavailable",
                "reason": "authenticated_price_unavailable" if not observed else "",
                "measured_at": timestamp,
                "wallet_price": wallet + wallet_offset if observed else None,
                "normal_price": nonwallet + wallet_offset if observed else None,
                "authenticated_session_proof": True,
                "session_status": "authenticated_surface",
                "auth_run_reference": auth_reference,
            },
            seller={
                "measured_at": timestamp,
                "sizes": [{"sizeID": 1, "price": baseline, "discountedPrice": baseline}],
            } if observed else None,
        )
        assert observation["authenticated_spp"] is None
        append_observation(runtime, run_id=run_id, observation=observation)
    finish_run(runtime, run_id=run_id, finished_at=timestamp, status=status)


def _publish(entrypoint: object) -> dict:
    with patch("packages.application.sheet_vitrina_v1_live_plan.reporting_config",
               side_effect=_reporting_config_with_extra):
        return entrypoint._run_sheet_source_group_refresh(
            source_group_id="wb_buyer_authenticated",
            selected_as_of_date=CURRENT_DATE,
            target_snapshot_as_of_date=AS_OF_DATE,
            log=None,
        )


def main() -> None:
    with TemporaryDirectory(prefix="authenticated-buyer-ready-") as directory:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "runtime")
        accepted = runtime.ingest_bundle(_bundle(), activated_at=ACTIVATED_AT)
        assert accepted.status == "accepted"
        assert load_active_requested_nm_ids(runtime) == REQUESTED_NM_IDS
        assert load_source_requested_nm_ids(runtime, CURRENT_DATE) == (
            REQUESTED_NM_IDS, "versioned_day_bundle",
        )
        # The normal timer roster read must remain read-only while another
        # SQLite writer holds the operational store's write reservation.
        with sqlite3.connect(runtime.db_path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            assert _requested_ids(runtime) == REQUESTED_NM_IDS
            writer.rollback()
        entrypoint = _build_entrypoint(
            runtime=runtime,
            now_factory=_MutableNowFactory("2026-05-07T08:00:00+00:00"),
            scenario=_SppProxyScenario(mode="valid"),
        )
        with patch("packages.application.sheet_vitrina_v1_live_plan.reporting_config",
                   side_effect=_reporting_config_with_extra):
            initial = entrypoint._run_sheet_refresh(
                as_of_date=AS_OF_DATE, log=None, execution_mode=EXECUTION_MODE_AUTO_DAILY,
            )
        assert initial["status"] == "success"
        prior_rows = _data_rows(runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE))
        prior_spp = _today_value(prior_rows[f"SKU:{REQUESTED_NM_IDS[0]}|spp"])

        # One imported diagnostic SKU must not redefine the entire day's
        # operational denominator as 1/1. Its versioned bundle has two
        # enabled SKUs despite a three-SKU reporting catalog.
        assert begin_run(runtime, run_id="buyer-auth-evidence-fixture",
                         started_at="2026-05-07T08:00:30Z",
                         requested_nm_ids=[REQUESTED_NM_IDS[0]]) == CURRENT_DATE
        append_observation(runtime, run_id="buyer-auth-evidence-fixture", observation=classify_observation(
            nm_id=REQUESTED_NM_IDS[0],
            buyer={"status": "observed", "session_status": "authenticated_surface",
                   "authenticated_session_proof": True, "measured_at": "2026-05-07T08:00:30Z",
                   "wallet_price": 139.0, "normal_price": 144.0},
            seller={"measured_at": "2026-05-07T08:00:30Z",
                    "sizes": [{"sizeID": 1, "price": 180.0, "discountedPrice": 180.0}]},
        ))
        finish_run(runtime, run_id="buyer-auth-evidence-fixture",
                   finished_at="2026-05-07T08:00:30Z", status="completed")
        assert load_source_requested_nm_ids(runtime, CURRENT_DATE) == (
            REQUESTED_NM_IDS, "versioned_at_observation",
        )
        imported = _publish(entrypoint)
        assert imported["status"] == "success", imported
        imported_ready = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        imported_status_sheet = next(sheet for sheet in imported_ready.sheets if sheet.sheet_name == "STATUS")
        imported_buyer_status = next(row for row in imported_status_sheet.rows if row[0] == "wb_buyer_authenticated[today_current]")
        assert imported_buyer_status[7:9] == [2, 1] and imported_buyer_status[1] == "incomplete"

        _record_run(runtime, run_id="buyer-old", auth_reference="auth-old",
                    timestamp="2026-05-07T08:01:00Z", observed_nm_ids=set(REQUESTED_NM_IDS))
        assert load_source_requested_nm_ids(runtime, CURRENT_DATE) == (
            REQUESTED_NM_IDS, "frozen_collection_run",
        )
        future_scope, future_scope_source = load_source_requested_nm_ids(runtime, "2026-05-08")
        assert future_scope == REQUESTED_NM_IDS and future_scope_source == "versioned_day_bundle"
        future_projection = load_daily_projection(runtime, "2026-05-08", future_scope)
        assert future_projection["kind"] == "empty" and future_projection["requested_count"] == 2
        published = _publish(entrypoint)
        assert published["status"] == "success", published
        ready = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        rows = _data_rows(ready)
        sku = REQUESTED_NM_IDS[0]
        assert _today_value(rows[f"SKU:{sku}|buyer_wallet_price_rub"]) == 139.0
        assert _today_value(rows[f"SKU:{sku}|buyer_nonwallet_price_rub"]) == 144.0
        assert _today_value(rows[f"SKU:{sku}|effective_nonwallet_discount"]) == 0.2
        assert _today_value(rows[f"SKU:{sku}|authenticated_spp"]) == ""
        assert _today_value(rows[f"SKU:{sku}|spp"]) == prior_spp

        _record_run(runtime, run_id="buyer-new-partial", auth_reference="auth-new",
                    timestamp="2026-05-07T08:10:00Z", observed_nm_ids={sku},
                    wallet_offset=10.0, status="partial")
        partial = _publish(entrypoint)
        assert partial["status"] == "success", partial
        partial_rows = _data_rows(runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE))
        assert _today_value(partial_rows[f"SKU:{sku}|buyer_wallet_price_rub"]) == 149.0
        assert _today_value(partial_rows[f"SKU:{REQUESTED_NM_IDS[1]}|buyer_wallet_price_rub"]) == ""
        assert _today_value(partial_rows[f"SKU:{sku}|spp"]) == prior_spp
        partial_ready = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        partial_status_sheet = next(sheet for sheet in partial_ready.sheets if sheet.sheet_name == "STATUS")
        partial_buyer_status = next(row for row in partial_status_sheet.rows if row[0] == "wb_buyer_authenticated[today_current]")
        assert partial_buyer_status[7:9] == [2, 1]

        _record_run(runtime, run_id="buyer-new-zero", auth_reference="auth-zero",
                    timestamp="2026-05-07T08:20:00Z", observed_nm_ids=set(),
                    status="auth_unavailable")
        reset = _publish(entrypoint)
        assert reset["status"] == "success", reset
        assert reset["semantic_status"] == "error", reset
        final_ready = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        final_rows = _data_rows(final_ready)
        for nm_id in REQUESTED_NM_IDS:
            for metric_key in (
                "buyer_wallet_price_rub",
                "buyer_nonwallet_price_rub",
                "effective_nonwallet_discount",
                "authenticated_spp",
            ):
                assert _today_value(final_rows[f"SKU:{nm_id}|{metric_key}"]) == ""
            assert _today_value(final_rows[f"SKU:{nm_id}|spp"]) == _today_value(
                prior_rows[f"SKU:{nm_id}|spp"]
            )
        status_sheet = next(sheet for sheet in final_ready.sheets if sheet.sheet_name == "STATUS")
        buyer_status = next(row for row in status_sheet.rows if row[0] == "wb_buyer_authenticated[today_current]")
        assert buyer_status[1] == "empty" and "account_context_reset=true" in buyer_status[10]
        assert buyer_status[7:9] == [2, 0]
        with sqlite3.connect(f"file:{runtime.db_path}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            assert connection.execute(
                "SELECT COUNT(*) FROM wb_buyer_authenticated_observations"
            ).fetchone()[0] == 7

        # A bundle activated after the imported observation must not redefine
        # that historical denominator. With no earlier version, retain the
        # DB fact but make ready coverage unavailable instead of 1/catalog.
        later_bundle = _bundle()
        later_bundle["bundle_version"] = "later_reporting_bundle"
        later_bundle["uploaded_at"] = "2026-05-07T12:00:00Z"
        later_bundle["config_v2"].append({
            "nm_id": EXTRA_REPORTING_NM_ID, "enabled": True,
            "display_name": "Extra", "group": "fixture", "display_order": 999,
        })
        late_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "late-runtime")
        assert late_runtime.ingest_bundle(later_bundle, activated_at="2026-05-07T12:00:00Z").status == "accepted"
        assert begin_run(late_runtime, run_id="buyer-auth-evidence-before-bundle",
                         started_at="2026-05-07T08:00:30Z",
                         requested_nm_ids=[REQUESTED_NM_IDS[0]]) == CURRENT_DATE
        append_observation(late_runtime, run_id="buyer-auth-evidence-before-bundle", observation=classify_observation(
            nm_id=REQUESTED_NM_IDS[0],
            buyer={"status": "observed", "session_status": "authenticated_surface",
                   "authenticated_session_proof": True, "measured_at": "2026-05-07T08:00:30Z",
                   "wallet_price": 139.0, "normal_price": 144.0},
            seller={"measured_at": "2026-05-07T08:00:30Z",
                    "sizes": [{"sizeID": 1, "price": 180.0, "discountedPrice": 180.0}]},
        ))
        finish_run(late_runtime, run_id="buyer-auth-evidence-before-bundle",
                   finished_at="2026-05-07T08:00:30Z", status="completed")
        unknown_scope, unknown_source = load_source_requested_nm_ids(late_runtime, CURRENT_DATE)
        assert unknown_scope == [] and unknown_source == "unknown_historical_roster"
        unknown_projection = load_daily_projection(late_runtime, CURRENT_DATE, unknown_scope)
        unknown_projection["diagnostics"]["eligible_scope_source"] = unknown_source
        unknown_payload = projection_payload(unknown_projection, business_date=CURRENT_DATE)
        assert unknown_payload.kind == "not_available" and not unknown_payload.items
        assert unknown_payload.requested_count == unknown_payload.covered_count == 0
        late_entrypoint = _build_entrypoint(
            runtime=late_runtime,
            now_factory=_MutableNowFactory("2026-05-07T13:00:00+00:00"),
            scenario=_SppProxyScenario(mode="valid"),
        )
        late_refresh = late_entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE, log=None, execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        assert late_refresh["status"] == "success", late_refresh
        late_ready = late_runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        late_status_sheet = next(sheet for sheet in late_ready.sheets if sheet.sheet_name == "STATUS")
        late_buyer_status = next(row for row in late_status_sheet.rows if row[0] == "wb_buyer_authenticated[today_current]")
        assert late_buyer_status[1] == "not_available" and late_buyer_status[7:9] == [0, 0]
        assert "eligible_roster_unknown" in late_buyer_status[10]
        late_rows = _data_rows(late_ready)
        assert _today_value(late_rows[f"SKU:{REQUESTED_NM_IDS[0]}|buyer_wallet_price_rub"]) == ""

        timed_runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(directory) / "timed-runtime")
        assert timed_runtime.ingest_bundle(_bundle(), activated_at=ACTIVATED_AT).status == "accepted"
        assert timed_runtime.ingest_bundle(later_bundle, activated_at="2026-05-07T12:00:00Z").status == "accepted"
        assert begin_run(timed_runtime, run_id="buyer-auth-evidence-timed",
                         started_at="2026-05-07T08:00:30Z",
                         requested_nm_ids=[REQUESTED_NM_IDS[0]]) == CURRENT_DATE
        assert load_source_requested_nm_ids(timed_runtime, CURRENT_DATE) == (
            REQUESTED_NM_IDS, "versioned_at_observation",
        )
        print("sheet_vitrina_v1_authenticated_buyer_ready: ok")


if __name__ == "__main__":
    main()
