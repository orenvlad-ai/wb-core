#!/usr/bin/env python3
"""Deterministic smoke for durable, batched Balance bid application."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.wb_promotion import WbPromotionApiError  # noqa: E402
from packages.application.change_registry import ChangeRegistryRepository  # noqa: E402
from packages.application.change_registry_writer import (  # noqa: E402
    InternalWriterRegistry,
)
from packages.application.sheet_vitrina_v1_ads import (  # noqa: E402
    AdsSafetyConfig,
    SheetVitrinaV1AdsBlock,
    SheetVitrinaV1AdsError,
)
from packages.application.sku_inventory_balance import (  # noqa: E402
    CALCULATION_CONTRACT,
    FORMULA_VERSION,
    LIVE_MODE,
    SkuInventoryBalanceBlock,
    SkuInventoryBalanceError,
)


SELLER_ID = "seller-canonical"
ACCOUNT_SCOPE = "seller-portal-primary"


class FakeRuntime:
    def __init__(self, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir
        self.db_path = runtime_dir / "registry_upload_runtime.sqlite3"


class FakeSkuManagement:
    def __init__(self) -> None:
        self.confirmed_events: list[dict] = []

    def persist_balance_bid_result(self, **payload: object) -> None:
        self.confirmed_events.append(dict(payload))


class FakeLiveAdapter:
    mode = LIVE_MODE
    external_writes_enabled = True

    def __init__(
        self,
        *,
        first_submit: str = "ok",
        mismatch_keys: set[str] | None = None,
        preflight_failure: bool = False,
    ) -> None:
        self.first_submit = first_submit
        self.mismatch_keys = set(mismatch_keys or ())
        self.preflight_failure = preflight_failure
        self.current: dict[str, int] = {}
        self.submit_attempts: list[list[str]] = []
        self.accepted_batches: list[list[str]] = []
        self.preflight_calls: list[list[str]] = []
        self.readback_calls: list[list[str]] = []

    def preflight(self, targets, *, min_bid_interval_seconds, sleep):
        del min_bid_interval_seconds, sleep
        if self.preflight_failure:
            raise RuntimeError("simulated worker interruption before submit")
        keys = [str(item["target_key"]) for item in targets]
        self.preflight_calls.append(keys)
        result = []
        for target in targets:
            key = str(target["target_key"])
            self.current.setdefault(key, int(target["current_bid_minor"]))
            result.append(
                {
                    **dict(target),
                    "ok": True,
                    "minimum_bid_minor": 1,
                    "observed_bid_minor": self.current[key],
                }
            )
        return result

    def submit_batch(self, targets):
        keys = [str(item["target_key"]) for item in targets]
        self.submit_attempts.append(keys)
        if self.first_submit == "local_rejection":
            self.first_submit = "ok"
            raise SheetVitrinaV1AdsError(
                "simulated payload guard", http_status=422
            )
        if self.first_submit == "rate_limit":
            self.first_submit = "ok"
            raise WbPromotionApiError(
                method="PATCH",
                url="https://advert-api.wildberries.ru/api/advert/v1/bids",
                http_status=429,
                headers={"retry-after": "0"},
                retry_after_seconds=0,
            )
        for target in targets:
            key = str(target["target_key"])
            if key not in self.mismatch_keys:
                self.current[key] = int(target["requested_bid_minor"])
        if self.first_submit == "transport_after_apply":
            self.first_submit = "ok"
            raise WbPromotionApiError(
                method="PATCH",
                url="https://advert-api.wildberries.ru/api/advert/v1/bids",
                transport_error="connection reset after request body",
            )
        self.accepted_batches.append(keys)
        return {"status": "accepted"}

    def readback(self, targets):
        keys = [str(item["target_key"]) for item in targets]
        self.readback_calls.append(keys)
        return [
            {
                **dict(target),
                "ok": True,
                "observed_bid_minor": self.current.get(
                    str(target["target_key"]), int(target["current_bid_minor"])
                ),
            }
            for target in targets
        ]


class FakePromotionSource:
    def __init__(self, targets: list[dict]) -> None:
        self.targets = {int(item["advert_id"]): dict(item) for item in targets}
        self.identity_incident_advert = 0
        self.rate_limit_min_advert = 0
        self.patch_payloads: list[dict] = []
        self.min_calls: list[int] = []

    def fetch_adverts(self, advert_ids, *, statuses=None, payment_type=""):
        del statuses, payment_type
        adverts = []
        for advert_id in advert_ids:
            target = self.targets[int(advert_id)]
            settings = [
                {
                    "nm_id": target["nm_id"],
                    "bids_kopecks": {target["placement"]: target["current_bid_minor"]},
                }
            ]
            if int(advert_id) == self.identity_incident_advert:
                settings.append(
                    {"nm_id": int(target["nm_id"]) + 1, "bids_kopecks": {"search": 1}}
                )
            adverts.append(
                {
                    "id": int(advert_id),
                    "status": 9,
                    "bid_type": "manual",
                    "settings": {
                        "name": f"campaign-{advert_id}",
                        "payment_type": target["payment_type"],
                        "placements": {target["placement"]: True},
                    },
                    "nm_settings": settings,
                }
            )
        return {"adverts": adverts}

    def fetch_min_bids(self, *, advert_id, nm_ids, payment_type, placement_types):
        del payment_type
        self.min_calls.append(int(advert_id))
        if int(advert_id) == self.rate_limit_min_advert:
            self.rate_limit_min_advert = 0
            raise WbPromotionApiError(
                method="POST",
                url="https://advert-api.wildberries.ru/api/advert/v1/bids/min",
                http_status=429,
                headers={"retry-after": "0.5"},
                retry_after_seconds=0.5,
            )
        return {
            "bids": [
                {
                    "nm_id": int(nm_ids[0]),
                    "bids": [
                        {"type": placement, "value": 100}
                        for placement in placement_types
                    ],
                }
            ]
        }

    def patch_bids(self, payload):
        self.patch_payloads.append(dict(payload))
        return {"status": "accepted"}


def _target(index: int) -> dict:
    cpc = index % 2 == 0
    current = 5 + index if cpc else 1_500 + index
    requested = current + 1 if cpc else current - 100
    nm_id = 500_000 + index
    advert_id = 900_000 + index
    placement = "recommendations" if cpc else "search"
    return {
        "target_key": f"bid:{advert_id}:{nm_id}:{placement}",
        "nm_id": nm_id,
        "advert_id": advert_id,
        "campaign_name": f"campaign-{index}",
        "campaign_group": "new_cpc" if cpc else "old_cpm",
        "payment_type": "cpc" if cpc else "cpm",
        "placement": placement,
        "identity_valid": True,
        "manual_override_allowed": True,
        "current_bid_rub": current,
        "calculated_target_bid_rub": requested,
    }


def _build_runtime(
    runtime_dir: Path,
    adapter: FakeLiveAdapter,
    *,
    deadline_seconds: float = 0,
) -> tuple[SkuInventoryBalanceBlock, FakeSkuManagement, InternalWriterRegistry]:
    repository = ChangeRegistryRepository(runtime_dir)
    repository.initialize_schema()
    writer = InternalWriterRegistry(
        runtime_dir=runtime_dir,
        seller_id=SELLER_ID,
        account_scope=ACCOUNT_SCOPE,
    )
    sku = FakeSkuManagement()
    block = SkuInventoryBalanceBlock(
        runtime=FakeRuntime(runtime_dir),
        sku_management_block=sku,
        apply_adapter=adapter,
        writer_registry=writer,
        seller_id=SELLER_ID,
        account_scope=ACCOUNT_SCOPE,
        live_batch_size=10,
        min_bid_interval_seconds=0,
        patch_interval_seconds=0,
        readback_initial_delay_seconds=0,
        readback_poll_seconds=0,
        readback_deadline_seconds=deadline_seconds,
        sleep=lambda _seconds: None,
    )
    return block, sku, writer


def _insert_calculation(block: SkuInventoryBalanceBlock, count: int, suffix: str) -> dict:
    calculation_id = f"ibc_live_smoke_{suffix}"
    targets = [_target(index) for index in range(1, count + 1)]
    payload = {
        "contract_name": CALCULATION_CONTRACT,
        "calculation_id": calculation_id,
        "source_generated_at": "2026-08-30T00:00:00+00:00",
        "settings": {},
        "rows": [
            {
                "nm_id": item["nm_id"],
                "name": f"SKU {item['nm_id']}",
                "campaign_recommendations": [item],
            }
            for item in targets
        ],
    }
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(block.runtime.db_path) as conn:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_inventory_balance_calculations(
                   calculation_id,operation_id,previous_calculation_id,contract_name,
                   formula_version,source_digest,settings_json,payload_json,created_at,created_by
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                calculation_id,
                None,
                None,
                CALCULATION_CONTRACT,
                FORMULA_VERSION,
                "a" * 64,
                "{}",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                "operator",
            ),
        )
        conn.commit()
    return block.get_calculation(calculation_id)


def _start(block: SkuInventoryBalanceBlock, calculation: dict) -> dict:
    return block.start_apply(
        {
            "calculation_id": calculation["calculation_id"],
            "nm_ids": [row["nm_id"] for row in calculation["rows"]],
            "mode": LIVE_MODE,
            "confirmed": True,
        },
        actor="operator",
    )


def _wait_terminal(block: SkuInventoryBalanceBlock, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = block.get_apply_job(job_id)
        if job["state"] in {"completed", "completed_with_errors", "stalled"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"live apply did not finish: {block.get_apply_job(job_id)}")


def _table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _regular_batch_case(root: Path) -> None:
    adapter = FakeLiveAdapter()
    block, sku, _writer = _build_runtime(root, adapter)
    calculation = _insert_calculation(block, 33, "regular")
    job = _start(block, calculation)
    terminal = _wait_terminal(block, job["job_id"])
    assert terminal["state"] == "completed", terminal
    assert terminal["progress"]["applied"] == 33
    assert [len(batch) for batch in adapter.accepted_batches] == [1, 10, 10, 10, 2]
    assert all(len(batch) <= 10 for batch in adapter.accepted_batches)
    assert len({key for batch in adapter.accepted_batches for key in batch}) == 33
    assert len(sku.confirmed_events) == 33
    assert _table_count(block.runtime.db_path, "change_registry_operations") == 33
    assert _table_count(block.runtime.db_path, "change_registry_items") == 33
    assert _table_count(block.runtime.db_path, "change_registry_facts") == 33
    again = _start(block, calculation)
    assert again["job_id"] == job["job_id"]
    assert [len(batch) for batch in adapter.accepted_batches] == [1, 10, 10, 10, 2]
    with sqlite3.connect(block.runtime.db_path) as conn:
        linked = conn.execute(
            """SELECT COUNT(*) FROM change_registry_operations
               WHERE calculation_id=? AND apply_operation_id=?""",
            (calculation["calculation_id"], job["job_id"]),
        ).fetchone()[0]
        recommendation_links = conn.execute(
            """SELECT COUNT(*) FROM change_registry_items
               WHERE recommendation_item_id<>''"""
        ).fetchone()[0]
    assert int(linked) == 33
    assert int(recommendation_links) == 33


def _ads_guard_case(root: Path) -> None:
    targets = []
    for index in range(1, 34):
        source = _target(index)
        targets.append(
            {
                **source,
                "current_bid_minor": int(source["current_bid_rub"]) * 100,
                "requested_bid_minor": int(source["calculated_target_bid_rub"]) * 100,
                "recommendation_item_id": f"rec-{index}",
            }
        )
    promotion = FakePromotionSource(targets)
    ads = SheetVitrinaV1AdsBlock(
        runtime=object(),
        runtime_dir=root,
        source=promotion,
        safety_config=AdsSafetyConfig(
            write_enabled=True,
            absolute_max_bid_kopecks=1_000_000,
            max_percent_increase=Decimal("1000"),
            max_absolute_increase_kopecks=1_000_000,
            preview_ttl_seconds=120,
        ),
    )
    sleeps: list[float] = []
    guarded = ads.preflight_bid_targets(
        targets, min_bid_interval_seconds=3, sleep=sleeps.append
    )
    assert all(item["ok"] for item in guarded)
    assert len(promotion.min_calls) == 33
    assert sleeps == [3.0] * 28
    ads.submit_bid_targets(targets)
    assert len(promotion.patch_payloads) == 1
    payload = promotion.patch_payloads[0]
    assert len(payload["bids"]) == 33
    assert sum(len(item["nm_bids"]) for item in payload["bids"]) == 33
    assert {
        "nm_id",
        "bid_kopecks",
        "placement",
    } == set(payload["bids"][0]["nm_bids"][0])

    incident = targets[0]
    promotion.identity_incident_advert = int(incident["advert_id"])
    failed = ads.preflight_bid_targets([incident], sleep=lambda _seconds: None)[0]
    assert failed["ok"] is False
    assert failed["error_code"] == "campaign_identity_incident"
    promotion.identity_incident_advert = 0
    stale = ads.preflight_bid_targets(
        [{**incident, "current_bid_minor": int(incident["current_bid_minor"]) + 1}],
        sleep=lambda _seconds: None,
    )[0]
    assert stale["error_code"] == "stale_current_bid"
    below_min = ads.preflight_bid_targets(
        [{**incident, "requested_bid_minor": 99}], sleep=lambda _seconds: None
    )[0]
    assert below_min["error_code"] == "below_minimum_bid"
    promotion.rate_limit_min_advert = int(incident["advert_id"])
    rate_sleeps: list[float] = []
    recovered = ads.preflight_bid_targets(
        [incident], min_bid_interval_seconds=3, sleep=rate_sleeps.append
    )[0]
    assert recovered["ok"] is True
    assert rate_sleeps == [0.5]


def _rate_limit_case(root: Path) -> None:
    adapter = FakeLiveAdapter(first_submit="rate_limit")
    block, _sku, _writer = _build_runtime(root, adapter)
    terminal = _wait_terminal(block, _start(block, _insert_calculation(block, 1, "rate"))["job_id"])
    assert terminal["state"] == "completed"
    assert len(adapter.submit_attempts) == 2
    assert len(adapter.accepted_batches) == 1


def _transport_unknown_case(root: Path) -> None:
    adapter = FakeLiveAdapter(first_submit="transport_after_apply")
    block, _sku, _writer = _build_runtime(root, adapter)
    terminal = _wait_terminal(block, _start(block, _insert_calculation(block, 1, "transport"))["job_id"])
    assert terminal["state"] == "completed", terminal
    assert len(adapter.submit_attempts) == 1
    assert terminal["progress"]["applied"] == 1
    with sqlite3.connect(block.runtime.db_path) as conn:
        states = [
            row[0]
            for row in conn.execute(
                "SELECT state FROM change_registry_attempt_events ORDER BY sequence_no"
            ).fetchall()
        ]
    assert states == ["created", "ambiguous", "resolved"]


def _local_rejection_case(root: Path) -> None:
    adapter = FakeLiveAdapter(first_submit="local_rejection")
    block, _sku, _writer = _build_runtime(root, adapter)
    terminal = _wait_terminal(
        block,
        _start(block, _insert_calculation(block, 1, "local-rejection"))["job_id"],
    )
    assert terminal["state"] == "completed_with_errors", terminal
    assert terminal["progress"]["failed"] == 1
    assert terminal["progress"]["needs_check"] == 0
    assert terminal["items"][0]["error_code"] == "local_submit_guard"


def _partial_case(root: Path) -> None:
    second_key = _target(2)["target_key"]
    adapter = FakeLiveAdapter(mismatch_keys={second_key})
    block, _sku, _writer = _build_runtime(root, adapter)
    terminal = _wait_terminal(block, _start(block, _insert_calculation(block, 2, "partial"))["job_id"])
    assert terminal["state"] == "completed_with_errors", terminal
    assert terminal["progress"]["applied"] == 1
    assert terminal["progress"]["needs_check"] == 1
    assert len(adapter.submit_attempts) == 2


def _restart_recovery_case(root: Path) -> None:
    broken = FakeLiveAdapter(preflight_failure=True)
    block, _sku, writer = _build_runtime(root, broken, deadline_seconds=30)
    calculation = _insert_calculation(block, 1, "restart")
    stalled = _wait_terminal(block, _start(block, calculation)["job_id"])
    assert stalled["state"] == "stalled"
    item = stalled["items"][0]
    receipt = f"inventory-balance:{stalled['job_id']}:{item['target_key']}"
    prepared = writer.prepare_bid(
        source_surface="sku_inventory_balance",
        actor="operator",
        native_operation_id=f"{stalled['job_id']}:{item['target_key']}",
        nm_id=item["nm_id"],
        advert_id=item["advert_id"],
        placement=item["placement"],
        before_bid_minor=item["current_bid_minor"],
        requested_bid_minor=item["final_target_bid_minor"],
        requested_at=stalled["created_at"],
        correlation_id=stalled["job_id"],
        calculation_id=calculation["calculation_id"],
        apply_operation_id=stalled["job_id"],
        recommendation_item_id=item["recommendation_item_id"],
    )
    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    with sqlite3.connect(block.runtime.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_inventory_balance_apply_jobs
               SET state='running',worker_token='',lease_expires_at='',phase='submit_unknown'
               WHERE job_id=?""",
            (stalled["job_id"],),
        )
        conn.execute(
            """UPDATE sheet_vitrina_v1_inventory_balance_apply_items
               SET state='submitting',registry_operation_id=?,registry_receipt_reference=?,
                   readback_deadline_at=? WHERE job_id=? AND target_key=?""",
            (prepared.operation_id, receipt, future, stalled["job_id"], item["target_key"]),
        )
        conn.commit()
    recovered_adapter = FakeLiveAdapter()
    recovered_adapter.current[item["target_key"]] = item["final_target_bid_minor"]
    recovered, _sku, _writer = _build_runtime(root, recovered_adapter, deadline_seconds=30)
    terminal = _wait_terminal(recovered, stalled["job_id"])
    assert terminal["state"] == "completed", terminal
    assert recovered_adapter.submit_attempts == []
    assert terminal["progress"]["applied"] == 1


def _lease_fencing_case(root: Path) -> None:
    adapter = FakeLiveAdapter()
    block, _sku, _writer = _build_runtime(root, adapter)
    block._apply_worker_stop.set()
    job = _start(block, _insert_calculation(block, 1, "lease"))
    time.sleep(0.05)
    first = block._claim_next_live_job()
    assert first is not None
    job_id, first_token = first
    with sqlite3.connect(block.runtime.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_inventory_balance_apply_jobs
               SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?""",
            (job_id,),
        )
        conn.commit()
    second = block._claim_next_live_job()
    assert second is not None
    assert second[0] == job_id and second[1] != first_token
    try:
        block._renew_job_lease(job_id, first_token, phase="stale")
    except SkuInventoryBalanceError:
        pass
    else:  # pragma: no cover
        raise AssertionError("stale worker renewed a replacement lease")
    block._mark_job_worker_error(job_id, first_token, RuntimeError("stale"))
    with sqlite3.connect(block.runtime.db_path) as conn:
        state, token = conn.execute(
            """SELECT state,worker_token
               FROM sheet_vitrina_v1_inventory_balance_apply_jobs WHERE job_id=?""",
            (job["job_id"],),
        ).fetchone()
    assert state == "running" and token == second[1]
    block._release_job_lease(job_id, first_token)
    assert block._job_row(job_id)["worker_token"] == second[1]
    block._release_job_lease(job_id, second[1])
    assert block._job_row(job_id)["worker_token"] == ""
    with sqlite3.connect(block.runtime.db_path) as conn:
        conn.execute(
            """UPDATE sheet_vitrina_v1_inventory_balance_apply_jobs
               SET state='running',updated_at='2000-01-01T00:00:00+00:00',
                   lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?""",
            (job_id,),
        )
        conn.commit()
    assert block.get_apply_job(job_id)["state"] == "stalled"
    resumed = block.resume_apply(job_id, actor="operator")
    assert resumed["state"] == "running"


def main() -> None:
    with TemporaryDirectory(prefix="inventory-balance-live-") as tmp:
        base = Path(tmp)
        _ads_guard_case(base / "ads-guard")
        _regular_batch_case(base / "regular")
        _rate_limit_case(base / "rate")
        _transport_unknown_case(base / "transport")
        _local_rejection_case(base / "local-rejection")
        _partial_case(base / "partial")
        _restart_recovery_case(base / "restart")
        _lease_fencing_case(base / "lease")
    print("sku_inventory_balance_live_apply_smoke: ok")


if __name__ == "__main__":
    main()
