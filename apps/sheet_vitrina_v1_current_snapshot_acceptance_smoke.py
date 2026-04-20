"""Targeted smoke-check for current-snapshot acceptance, same-day retry, and manual non-destructive behavior."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import (
    EXECUTION_MODE_AUTO_DAILY,
    EXECUTION_MODE_MANUAL_OPERATOR,
    TEMPORAL_ROLE_ACCEPTED_CURRENT,
    TEMPORAL_SLOT_TODAY_CURRENT,
)


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-18"
CURRENT_DATE = "2026-04-19"
ROLLOVER_AS_OF_DATE = CURRENT_DATE
NEXT_CURRENT_DATE = "2026-04-20"
ACTIVATED_AT = "2026-04-19T00:00:00Z"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    _assert_auto_invalid_schedules_same_day_retry(bundle, requested_nm_ids)
    preserved_note = _assert_preserved_current_snapshot_across_auto_and_manual(bundle, requested_nm_ids, probe_nm_id)
    rollover_note = _assert_rollover_preserves_yesterday_closed_across_auto_and_manual(
        bundle,
        requested_nm_ids,
        probe_nm_id,
    )
    _assert_manual_invalid_without_acceptance_does_not_schedule_retry(bundle, requested_nm_ids)

    print("auto_retry_current_only: ok -> closure_retrying created for prices_snapshot[today_current] and ads_bids[today_current]")
    print(f"preserved_current_snapshot: ok -> {preserved_note}")
    print(f"rollover_preserved_yesterday_closed: ok -> {rollover_note}")
    print("manual_no_retry_leak: ok -> invalid manual current-only run stayed non-destructive and unscheduled")
    print("smoke-check passed")


def _assert_auto_invalid_schedules_same_day_retry(bundle: dict[str, object], requested_nm_ids: list[int]) -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-current-only-auto-retry-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        scenario = _CurrentOnlyScenario(requested_nm_ids=requested_nm_ids, mode="invalid")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            refreshed_at_factory=_SequenceTimestampFactory(["2026-04-19T06:05:00Z"]),
            now_factory=_MutableNowFactory("2026-04-19T06:00:00+00:00"),
            prices_block=_SyntheticPricesBlock(scenario),
            ads_bids_block=_SyntheticAdsBidsBlock(scenario),
            requested_nm_ids=requested_nm_ids,
        )

        refresh_payload = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError("auto refresh with invalid current-only candidate must still persist a ready snapshot")

        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        status_rows = _status_rows(plan)
        prices_status = status_rows["prices_snapshot[today_current]"]
        ads_status = status_rows["ads_bids[today_current]"]
        if prices_status[1] != "closure_retrying":
            raise AssertionError(f"auto invalid current-only candidate must create same-day retry state, got {prices_status}")
        if ads_status[1] != "closure_retrying":
            raise AssertionError(f"auto invalid ads candidate must create same-day retry state, got {ads_status}")
        closure_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is None or closure_state.state != "closure_retrying":
            raise AssertionError(f"same-day retry state missing after auto invalid current-only run: {closure_state}")
        ads_closure_state = runtime.load_temporal_source_closure_state(
            source_key="ads_bids",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if ads_closure_state is None or ads_closure_state.state != "closure_retrying":
            raise AssertionError(f"same-day retry state missing after auto invalid ads run: {ads_closure_state}")


def _assert_preserved_current_snapshot_across_auto_and_manual(
    bundle: dict[str, object],
    requested_nm_ids: list[int],
    probe_nm_id: int,
) -> str:
    with TemporaryDirectory(prefix="sheet-vitrina-current-only-preserve-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        scenario = _CurrentOnlyScenario(requested_nm_ids=requested_nm_ids, mode="valid")
        now_factory = _MutableNowFactory("2026-04-19T06:00:00+00:00")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            refreshed_at_factory=_SequenceTimestampFactory(
                [
                    "2026-04-19T06:05:00Z",
                    "2026-04-19T14:05:00Z",
                    "2026-04-19T15:05:00Z",
                ]
            ),
            now_factory=now_factory,
            prices_block=_SyntheticPricesBlock(scenario),
            ads_bids_block=_SyntheticAdsBidsBlock(scenario),
            requested_nm_ids=requested_nm_ids,
        )

        morning_refresh = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if morning_refresh["status"] != "success":
            raise AssertionError("morning auto refresh must succeed")
        morning_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        morning_rows = _data_rows(morning_plan)
        morning_price = _today_value(morning_rows[f"SKU:{probe_nm_id}|price_seller_discounted"])
        morning_bid = _today_value(morning_rows[f"SKU:{probe_nm_id}|ads_bid_search"])
        if morning_price != 199.0:
            raise AssertionError(f"morning valid current-only snapshot must materialize the accepted value, got {morning_price}")
        if morning_bid != 12.0:
            raise AssertionError(f"morning valid ads snapshot must materialize the accepted value, got {morning_bid}")

        scenario.mode = "invalid"
        now_factory.value = "2026-04-19T14:00:00+00:00"
        evening_auto = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if evening_auto["status"] != "success":
            raise AssertionError("evening auto refresh must still persist a ready snapshot")
        evening_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        evening_status = _status_rows(evening_plan)["prices_snapshot[today_current]"]
        evening_ads_status = _status_rows(evening_plan)["ads_bids[today_current]"]
        if evening_status[1] != "success":
            raise AssertionError(f"later invalid auto attempt must keep accepted current snapshot visible, got {evening_status}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(evening_status[10]):
            raise AssertionError(f"later invalid auto attempt must explain preserved accepted snapshot, got {evening_status}")
        if evening_ads_status[1] != "success":
            raise AssertionError(f"later invalid auto ads attempt must keep accepted current snapshot visible, got {evening_ads_status}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(evening_ads_status[10]):
            raise AssertionError(
                f"later invalid auto ads attempt must explain preserved accepted snapshot, got {evening_ads_status}"
            )
        evening_rows = _data_rows(evening_plan)
        if _today_value(evening_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != morning_price:
            raise AssertionError("later invalid auto attempt must not overwrite the accepted morning current snapshot")
        if _today_value(evening_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != morning_bid:
            raise AssertionError("later invalid auto ads attempt must not overwrite the accepted morning current snapshot")

        manual_refresh = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        )
        if manual_refresh["status"] != "success":
            raise AssertionError("manual refresh must still persist a ready snapshot")
        manual_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        manual_status = _status_rows(manual_plan)["prices_snapshot[today_current]"]
        manual_ads_status = _status_rows(manual_plan)["ads_bids[today_current]"]
        if manual_status[1] != "success":
            raise AssertionError(f"invalid manual current-only attempt must keep accepted snapshot visible, got {manual_status}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(manual_status[10]):
            raise AssertionError(f"manual invalid current-only attempt must explain preserved accepted snapshot, got {manual_status}")
        if manual_ads_status[1] != "success":
            raise AssertionError(f"invalid manual ads attempt must keep accepted snapshot visible, got {manual_ads_status}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(manual_ads_status[10]):
            raise AssertionError(f"manual invalid ads attempt must explain preserved accepted snapshot, got {manual_ads_status}")
        manual_rows = _data_rows(manual_plan)
        if _today_value(manual_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != morning_price:
            raise AssertionError("invalid manual current-only attempt must not overwrite the accepted auto snapshot")
        if _today_value(manual_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != morning_bid:
            raise AssertionError("invalid manual ads attempt must not overwrite the accepted auto snapshot")

        closure_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is None or closure_state.state != "success":
            raise AssertionError(f"preserved accepted current snapshot must keep closure state successful, got {closure_state}")
        ads_closure_state = runtime.load_temporal_source_closure_state(
            source_key="ads_bids",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if ads_closure_state is None or ads_closure_state.state != "success":
            raise AssertionError(f"preserved accepted ads snapshot must keep closure state successful, got {ads_closure_state}")
        return f"{manual_status[10]} | {manual_ads_status[10]}"


def _assert_rollover_preserves_yesterday_closed_across_auto_and_manual(
    bundle: dict[str, object],
    requested_nm_ids: list[int],
    probe_nm_id: int,
) -> str:
    with TemporaryDirectory(prefix="sheet-vitrina-current-only-rollover-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        scenario = _CurrentOnlyScenario(requested_nm_ids=requested_nm_ids, mode="valid")
        now_factory = _MutableNowFactory("2026-04-19T06:00:00+00:00")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            refreshed_at_factory=_SequenceTimestampFactory(
                [
                    "2026-04-19T06:05:00Z",
                    "2026-04-20T06:05:00Z",
                    "2026-04-20T14:05:00Z",
                    "2026-04-20T15:05:00Z",
                ]
            ),
            now_factory=now_factory,
            prices_block=_SyntheticPricesBlock(scenario),
            ads_bids_block=_SyntheticAdsBidsBlock(scenario),
            requested_nm_ids=requested_nm_ids,
        )

        day_d_refresh = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if day_d_refresh["status"] != "success":
            raise AssertionError("day D refresh must succeed")

        accepted_prices, accepted_prices_at = runtime.load_temporal_source_slot_snapshot(
            source_key="prices_snapshot",
            snapshot_date=CURRENT_DATE,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
        )
        accepted_ads, accepted_ads_at = runtime.load_temporal_source_slot_snapshot(
            source_key="ads_bids",
            snapshot_date=CURRENT_DATE,
            snapshot_role=TEMPORAL_ROLE_ACCEPTED_CURRENT,
        )
        if accepted_prices is None or accepted_ads is None:
            raise AssertionError("day D valid current snapshots must persist accepted current truth for both current-only sources")
        if (
            getattr(accepted_prices.items[0], "price_seller", None) != 219.0
            or getattr(accepted_prices.items[0], "price_seller_discounted", None) != 199.0
        ):
            raise AssertionError(f"accepted price payload must keep both price metrics, got {accepted_prices}")
        if (
            getattr(accepted_ads.items[0], "ads_bid_search", None) != 12.0
            or getattr(accepted_ads.items[0], "ads_bid_recommendations", None) != 9.0
        ):
            raise AssertionError(f"accepted ads payload must keep both bid metrics, got {accepted_ads}")
        if accepted_prices_at != "2026-04-19T06:00:00Z" or accepted_ads_at != "2026-04-19T06:00:00Z":
            raise AssertionError("accepted current snapshots must keep the original day-D capture timestamp")

        now_factory.value = "2026-04-20T06:00:00+00:00"
        rollover_refresh = entrypoint._run_sheet_refresh(
            as_of_date=ROLLOVER_AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if rollover_refresh["status"] != "success":
            raise AssertionError("day D+1 refresh with valid current snapshot must succeed")
        rollover_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=ROLLOVER_AS_OF_DATE)
        rollover_status = _status_rows(rollover_plan)
        rollover_rows = _data_rows(rollover_plan)
        _assert_rollover_success_state(rollover_status)
        if _yesterday_value(rollover_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != 199.0:
            raise AssertionError("day D accepted price must materialize into D+1 yesterday_closed")
        if _today_value(rollover_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != 209.0:
            raise AssertionError("day D+1 valid price must materialize into today_current only")
        if _yesterday_value(rollover_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != 12.0:
            raise AssertionError("day D accepted ads bid must materialize into D+1 yesterday_closed")
        if _today_value(rollover_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != 22.0:
            raise AssertionError("day D+1 valid ads bid must materialize into today_current only")

        scenario.mode = "invalid"
        now_factory.value = "2026-04-20T14:00:00+00:00"
        invalid_auto_refresh = entrypoint._run_sheet_refresh(
            as_of_date=ROLLOVER_AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if invalid_auto_refresh["status"] != "success":
            raise AssertionError("later invalid auto refresh must still persist a ready snapshot")
        invalid_auto_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=ROLLOVER_AS_OF_DATE)
        invalid_auto_status = _status_rows(invalid_auto_plan)
        invalid_auto_rows = _data_rows(invalid_auto_plan)
        _assert_rollover_success_state(invalid_auto_status)
        if _yesterday_value(invalid_auto_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != 199.0:
            raise AssertionError("invalid D+1 auto attempt must preserve day-D yesterday price")
        if _today_value(invalid_auto_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != 209.0:
            raise AssertionError("invalid D+1 auto attempt must preserve already accepted D+1 current price")
        if _yesterday_value(invalid_auto_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != 12.0:
            raise AssertionError("invalid D+1 auto attempt must preserve day-D yesterday ads bid")
        if _today_value(invalid_auto_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != 22.0:
            raise AssertionError("invalid D+1 auto attempt must preserve already accepted D+1 current ads bid")

        now_factory.value = "2026-04-20T15:00:00+00:00"
        invalid_manual_refresh = entrypoint._run_sheet_refresh(
            as_of_date=ROLLOVER_AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        )
        if invalid_manual_refresh["status"] != "success":
            raise AssertionError("later invalid manual refresh must still persist a ready snapshot")
        invalid_manual_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=ROLLOVER_AS_OF_DATE)
        invalid_manual_status = _status_rows(invalid_manual_plan)
        invalid_manual_rows = _data_rows(invalid_manual_plan)
        _assert_rollover_success_state(invalid_manual_status)
        if _yesterday_value(invalid_manual_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != 199.0:
            raise AssertionError("invalid D+1 manual attempt must preserve day-D yesterday price")
        if _today_value(invalid_manual_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != 209.0:
            raise AssertionError("invalid D+1 manual attempt must preserve already accepted D+1 current price")
        if _yesterday_value(invalid_manual_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != 12.0:
            raise AssertionError("invalid D+1 manual attempt must preserve day-D yesterday ads bid")
        if _today_value(invalid_manual_rows[f"SKU:{probe_nm_id}|ads_bid_search"]) != 22.0:
            raise AssertionError("invalid D+1 manual attempt must preserve already accepted D+1 current ads bid")

        prices_next_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=NEXT_CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        ads_next_state = runtime.load_temporal_source_closure_state(
            source_key="ads_bids",
            target_date=NEXT_CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if prices_next_state is None or prices_next_state.state != "success":
            raise AssertionError(f"day D+1 accepted current price must remain successful after invalid attempts, got {prices_next_state}")
        if ads_next_state is None or ads_next_state.state != "success":
            raise AssertionError(f"day D+1 accepted current ads must remain successful after invalid attempts, got {ads_next_state}")
        return (
            f"{invalid_manual_status['prices_snapshot[yesterday_closed]'][10]} | "
            f"{invalid_manual_status['ads_bids[yesterday_closed]'][10]}"
        )


def _assert_manual_invalid_without_acceptance_does_not_schedule_retry(
    bundle: dict[str, object],
    requested_nm_ids: list[int],
) -> None:
    with TemporaryDirectory(prefix="sheet-vitrina-current-only-manual-no-retry-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        scenario = _CurrentOnlyScenario(requested_nm_ids=requested_nm_ids, mode="invalid")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            refreshed_at_factory=_SequenceTimestampFactory(["2026-04-19T15:35:00Z"]),
            now_factory=_MutableNowFactory("2026-04-19T15:30:00+00:00"),
            prices_block=_SyntheticPricesBlock(scenario),
            ads_bids_block=_SyntheticAdsBidsBlock(scenario),
            requested_nm_ids=requested_nm_ids,
        )

        refresh_payload = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError("manual invalid current-only refresh must still persist a ready snapshot")
        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        prices_status = _status_rows(plan)["prices_snapshot[today_current]"]
        ads_status = _status_rows(plan)["ads_bids[today_current]"]
        if prices_status[1] != "error":
            raise AssertionError(f"manual invalid current-only candidate without acceptance must stay error, got {prices_status}")
        if "zero_filled_prices_snapshot" not in str(prices_status[10]):
            raise AssertionError(f"manual invalid current-only candidate must expose invalid note, got {prices_status}")
        if ads_status[1] != "error":
            raise AssertionError(f"manual invalid ads candidate without acceptance must stay error, got {ads_status}")
        if "zero_filled_ads_bids_snapshot" not in str(ads_status[10]):
            raise AssertionError(f"manual invalid ads candidate must expose invalid note, got {ads_status}")
        closure_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is not None:
            raise AssertionError(f"manual invalid current-only refresh must not leak persisted retry state, got {closure_state}")
        ads_closure_state = runtime.load_temporal_source_closure_state(
            source_key="ads_bids",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if ads_closure_state is not None:
            raise AssertionError(f"manual invalid ads refresh must not leak persisted retry state, got {ads_closure_state}")


def _build_entrypoint(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    refreshed_at_factory: "_SequenceTimestampFactory",
    now_factory: "_MutableNowFactory",
    prices_block: object,
    ads_bids_block: object,
    requested_nm_ids: list[int],
) -> RegistryUploadHttpEntrypoint:
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime.runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: ACTIVATED_AT,
        refreshed_at_factory=refreshed_at_factory,
        now_factory=now_factory,
    )
    entrypoint.sheet_plan_block = _build_live_plan(
        runtime=runtime,
        now_factory=now_factory,
        prices_block=prices_block,
        ads_bids_block=ads_bids_block,
        requested_nm_ids=requested_nm_ids,
    )
    return entrypoint


def _build_live_plan(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    now_factory: "_MutableNowFactory",
    prices_block: object,
    ads_bids_block: object,
    requested_nm_ids: list[int],
):
    from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock

    return SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        now_factory=now_factory,
        current_web_source_sync=_NoopCurrentWebSourceSync(),
        seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot", requested_nm_ids),
        web_source_block=_SyntheticSuccessBlock("web_source_snapshot", requested_nm_ids),
        sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history", requested_nm_ids),
        prices_snapshot_block=prices_block,
        sf_period_block=_SyntheticSuccessBlock("sf_period", requested_nm_ids),
        spp_block=_SyntheticSuccessBlock("spp", requested_nm_ids),
        ads_bids_block=ads_bids_block,
        stocks_block=_SyntheticSuccessBlock("stocks", requested_nm_ids),
        ads_compact_block=_SyntheticSuccessBlock("ads_compact", requested_nm_ids),
        fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily", requested_nm_ids),
    )


class _NoopCurrentWebSourceSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return


class _CurrentOnlyScenario:
    def __init__(self, *, requested_nm_ids: list[int], mode: str) -> None:
        self.requested_nm_ids = requested_nm_ids
        self.mode = mode


class _SyntheticPricesBlock:
    def __init__(self, scenario: _CurrentOnlyScenario) -> None:
        self.scenario = scenario

    def execute(self, request: object) -> SimpleNamespace:
        snapshot_date = str(getattr(request, "snapshot_date"))
        if snapshot_date != CURRENT_DATE:
            if snapshot_date != NEXT_CURRENT_DATE:
                raise AssertionError(f"prices current-only path must only request today_current date, got {snapshot_date}")
        zero_filled = self.scenario.mode == "invalid"
        price_seller_base = 219.0 if snapshot_date == CURRENT_DATE else 229.0
        price_discounted_base = 199.0 if snapshot_date == CURRENT_DATE else 209.0
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=snapshot_date,
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        price_seller=0.0 if zero_filled else float(price_seller_base + index),
                        price_seller_discounted=0.0 if zero_filled else float(price_discounted_base + index),
                    )
                    for index, nm_id in enumerate(self.scenario.requested_nm_ids)
                ],
            )
        )


class _SyntheticAdsBidsBlock:
    def __init__(self, scenario: _CurrentOnlyScenario) -> None:
        self.scenario = scenario

    def execute(self, request: object) -> SimpleNamespace:
        snapshot_date = str(getattr(request, "snapshot_date"))
        if snapshot_date != CURRENT_DATE:
            if snapshot_date != NEXT_CURRENT_DATE:
                raise AssertionError(f"ads current-only path must only request today_current date, got {snapshot_date}")
        zero_filled = self.scenario.mode == "invalid"
        search_base = 12.0 if snapshot_date == CURRENT_DATE else 22.0
        recommendations_base = 9.0 if snapshot_date == CURRENT_DATE else 19.0
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=snapshot_date,
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        ads_bid_search=0.0 if zero_filled else float(search_base + index),
                        ads_bid_recommendations=0.0 if zero_filled else float(recommendations_base + index),
                    )
                    for index, nm_id in enumerate(self.scenario.requested_nm_ids)
                ],
            )
        )


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str, requested_nm_ids: list[int]) -> None:
        self.source_key = source_key
        self.requested_nm_ids = requested_nm_ids

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[SimpleNamespace(nm_id=nm_id) for nm_id in self.requested_nm_ids],
                snapshot_date=request_date,
                date=request_date,
                date_from=request_date,
                date_to=request_date,
                detail=f"{self.source_key} synthetic success for {request_date}",
                storage_total=None,
            )
        )


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)


class _SequenceTimestampFactory:
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> str:
        if self._index >= len(self._values):
            return self._values[-1]
        value = self._values[self._index]
        self._index += 1
        return value


def _status_rows(plan) -> dict[str, list[object]]:
    status_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {row[0]: row for row in status_sheet.rows}


def _data_rows(plan) -> dict[str, list[object]]:
    data_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
    return {row[1]: row for row in data_sheet.rows}


def _today_value(row: list[object]) -> object:
    return row[3]


def _yesterday_value(row: list[object]) -> object:
    return row[2]


def _assert_rollover_success_state(status_rows: dict[str, list[object]]) -> None:
    for source_key in ("prices_snapshot", "ads_bids"):
        yesterday_status = status_rows[f"{source_key}[yesterday_closed]"]
        if yesterday_status[1] != "success":
            raise AssertionError(f"accepted prior current snapshot must materialize yesterday_closed, got {yesterday_status}")
        if "accepted_closed_from_prior_current_snapshot" not in str(yesterday_status[10]):
            raise AssertionError(
                f"yesterday_closed must explain accepted-current rollover semantics for {source_key}, got {yesterday_status}"
            )
        today_status = status_rows[f"{source_key}[today_current]"]
        if today_status[1] != "success":
            raise AssertionError(f"today_current must stay successful for {source_key}, got {today_status}")


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    return CURRENT_DATE


if __name__ == "__main__":
    main()
