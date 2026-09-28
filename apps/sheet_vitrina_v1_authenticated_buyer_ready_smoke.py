"""Persisted buyer observations publish into the default ready Web Vitrina snapshot."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

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
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.sheet_vitrina_v1_live_plan import EXECUTION_MODE_AUTO_DAILY  # noqa: E402
from packages.application.wb_buyer_authenticated_observations import (  # noqa: E402
    append_observation,
    begin_run,
    classify_observation,
    finish_run,
)


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
        entrypoint = _build_entrypoint(
            runtime=runtime,
            now_factory=_MutableNowFactory("2026-05-07T08:00:00+00:00"),
            scenario=_SppProxyScenario(mode="valid"),
        )
        initial = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE, log=None, execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        assert initial["status"] == "success"
        prior_rows = _data_rows(runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE))
        prior_spp = _today_value(prior_rows[f"SKU:{REQUESTED_NM_IDS[0]}|spp"])

        _record_run(runtime, run_id="buyer-old", auth_reference="auth-old",
                    timestamp="2026-05-07T08:01:00Z", observed_nm_ids=set(REQUESTED_NM_IDS))
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
        with sqlite3.connect(f"file:{runtime.db_path}?mode=ro", uri=True) as connection:
            connection.execute("PRAGMA query_only=ON")
            assert connection.execute(
                "SELECT COUNT(*) FROM wb_buyer_authenticated_observations"
            ).fetchone()[0] == 6
        print("sheet_vitrina_v1_authenticated_buyer_ready: ok")


if __name__ == "__main__":
    main()
