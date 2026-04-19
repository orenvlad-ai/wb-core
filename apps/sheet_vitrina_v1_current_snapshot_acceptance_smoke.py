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
    TEMPORAL_SLOT_TODAY_CURRENT,
)


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-18"
CURRENT_DATE = "2026-04-19"
ACTIVATED_AT = "2026-04-19T00:00:00Z"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]]
    probe_nm_id = requested_nm_ids[0]

    _assert_auto_invalid_schedules_same_day_retry(bundle, requested_nm_ids)
    preserved_note = _assert_preserved_current_snapshot_across_auto_and_manual(bundle, requested_nm_ids, probe_nm_id)
    _assert_manual_invalid_without_acceptance_does_not_schedule_retry(bundle, requested_nm_ids)

    print("auto_retry_current_only: ok -> closure_retrying created for prices_snapshot[today_current]")
    print(f"preserved_current_snapshot: ok -> {preserved_note}")
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
        if prices_status[1] != "closure_retrying":
            raise AssertionError(f"auto invalid current-only candidate must create same-day retry state, got {prices_status}")
        closure_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is None or closure_state.state != "closure_retrying":
            raise AssertionError(f"same-day retry state missing after auto invalid current-only run: {closure_state}")


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
        if morning_price != 199.0:
            raise AssertionError(f"morning valid current-only snapshot must materialize the accepted value, got {morning_price}")

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
        if evening_status[1] != "success":
            raise AssertionError(f"later invalid auto attempt must keep accepted current snapshot visible, got {evening_status}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(evening_status[10]):
            raise AssertionError(f"later invalid auto attempt must explain preserved accepted snapshot, got {evening_status}")
        evening_rows = _data_rows(evening_plan)
        if _today_value(evening_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != morning_price:
            raise AssertionError("later invalid auto attempt must not overwrite the accepted morning current snapshot")

        manual_refresh = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
        )
        if manual_refresh["status"] != "success":
            raise AssertionError("manual refresh must still persist a ready snapshot")
        manual_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        manual_status = _status_rows(manual_plan)["prices_snapshot[today_current]"]
        if manual_status[1] != "success":
            raise AssertionError(f"invalid manual current-only attempt must keep accepted snapshot visible, got {manual_status}")
        if "accepted_current_preserved_after_invalid_attempt" not in str(manual_status[10]):
            raise AssertionError(f"manual invalid current-only attempt must explain preserved accepted snapshot, got {manual_status}")
        manual_rows = _data_rows(manual_plan)
        if _today_value(manual_rows[f"SKU:{probe_nm_id}|price_seller_discounted"]) != morning_price:
            raise AssertionError("invalid manual current-only attempt must not overwrite the accepted auto snapshot")

        closure_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is None or closure_state.state != "success":
            raise AssertionError(f"preserved accepted current snapshot must keep closure state successful, got {closure_state}")
        return str(manual_status[10])


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
        if prices_status[1] != "error":
            raise AssertionError(f"manual invalid current-only candidate without acceptance must stay error, got {prices_status}")
        if "zero_filled_prices_snapshot" not in str(prices_status[10]):
            raise AssertionError(f"manual invalid current-only candidate must expose invalid note, got {prices_status}")
        closure_state = runtime.load_temporal_source_closure_state(
            source_key="prices_snapshot",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is not None:
            raise AssertionError(f"manual invalid current-only refresh must not leak persisted retry state, got {closure_state}")


def _build_entrypoint(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    refreshed_at_factory: "_SequenceTimestampFactory",
    now_factory: "_MutableNowFactory",
    prices_block: object,
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
        requested_nm_ids=requested_nm_ids,
    )
    return entrypoint


def _build_live_plan(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    now_factory: "_MutableNowFactory",
    prices_block: object,
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
        ads_bids_block=_SyntheticSuccessBlock("ads_bids", requested_nm_ids),
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
            raise AssertionError(f"prices current-only path must only request today_current date, got {snapshot_date}")
        zero_filled = self.scenario.mode == "invalid"
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                snapshot_date=snapshot_date,
                items=[
                    SimpleNamespace(
                        nm_id=nm_id,
                        price_seller=0.0 if zero_filled else float(219 + index),
                        price_seller_discounted=0.0 if zero_filled else float(199 + index),
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


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    return CURRENT_DATE


if __name__ == "__main__":
    main()
