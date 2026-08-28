"""Deterministic acceptance smoke for the live Change Registry observer."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.change_registry import (  # noqa: E402
    CHECKPOINTS_TABLE,
    CHECKPOINT_SOURCE_MANIFESTS_TABLE,
    FACT_LINKS_TABLE,
    FACTS_TABLE,
    IDENTITY_INCIDENTS_TABLE,
    OBSERVATION_VALUES_TABLE,
    OBSERVER_HEALTH_EVENTS_TABLE,
    OBSERVER_JOB_EVENTS_TABLE,
    canonical_digest,
)
from packages.application.change_registry_observer import (  # noqa: E402
    ChangeRegistryObserver,
    ChangeRegistryObserverBusy,
    ChangeRegistryReadSurface,
)
from packages.application.change_registry_source_acquisition import (  # noqa: E402
    ChangeRegistrySourceAcquirer,
)
from apps.change_registry_source_acquisition_smoke import (  # noqa: E402
    FakeAdsSource,
    FakePricesSource,
    _count_payload,
    _detail,
)


SELLER = "seller-primary"
ACCOUNT = "seller-portal-primary"


def _exact_integer(value: int) -> dict[str, Any]:
    return {
        "status": "exact_zero" if value == 0 else "exact",
        "value": {"kind": "integer", "integer_value": value, "text_value": None},
    }


def _exact_text(value: str) -> dict[str, Any]:
    return {
        "status": "exact",
        "value": {"kind": "text", "integer_value": None, "text_value": value},
    }


def _nonexact(status: str, reason: str) -> dict[str, Any]:
    kind = "null" if status == "null" else "missing"
    return {
        "status": "exact" if status == "null" else status,
        "value": {"kind": kind, "integer_value": None, "text_value": None},
        "reason": reason,
    }


def _snapshot(
    minute: int,
    *,
    price: int | Mapping[str, Any] = 10000,
    complete: bool = True,
    include_good: bool = True,
    mapping: tuple[int, ...] = (101,),
) -> dict[str, Any]:
    started = f"2026-08-29T{minute // 60:02d}:{minute % 60:02d}:00Z"
    completed = f"2026-08-29T{minute // 60:02d}:{minute % 60:02d}:30Z"
    price_observation = dict(price) if isinstance(price, Mapping) else _exact_integer(price)
    good = {
        "nm_id": 101,
        "representation": "sku_uniform",
        "sku_values": {
            "original_price_minor": price_observation,
            "discount_bps": _exact_integer(1000),
            "seller_price_minor": _exact_integer(9000),
        },
        "record_digest": canonical_digest({"price": price_observation}),
    }
    mapping_exact = len(set(mapping)) == 1
    campaign = {
        "advert_id": 201,
        "mapping": {
            "status": "exact" if mapping_exact else "error",
            "candidate_nm_ids": list(mapping),
            "candidate_count": len(set(mapping)),
            "exact_nm_id": mapping[0] if mapping_exact else None,
        },
        "campaign_state": _exact_text("active"),
        "payment_model": _exact_text("cpc"),
        "payment_unit": _exact_text("per_click"),
        "bids": [
            {
                "nm_id": mapping[0] if mapping else 101,
                "advert_id": 201,
                "placement": "search",
                "bid_minor": _exact_integer(0),
                "target_digest": canonical_digest({"bid": 0, "mapping": mapping}),
            }
        ],
        "record_digest": canonical_digest({"campaign": 201, "mapping": mapping}),
    }
    incidents = []
    if not mapping_exact:
        incident = {
            "seller_id": SELLER,
            "account_scope": ACCOUNT,
            "advert_id": 201,
            "candidate_nm_ids": sorted(set(mapping)),
            "source_surface": "wb_promotion_adverts_v2",
            "observed_at": completed,
            "evidence_digest": canonical_digest({"mapping": mapping}),
        }
        incident["incident_id"] = "crii_" + canonical_digest(incident)[7:39]
        incidents.append(incident)
    prices = {
        "seller_id": SELLER,
        "account_scope": ACCOUNT,
        "completeness_status": "complete" if complete else "partial",
        "interval": {"started_at": started, "completed_at": completed},
        "goods": [good] if include_good else [],
        "counts": {"goods": 1 if include_good else 0, "issues": 0 if complete else 1},
    }
    prices["manifest_digest"] = canonical_digest(prices)
    ads = {
        "seller_id": SELLER,
        "account_scope": ACCOUNT,
        "completeness_status": "complete" if complete else "partial",
        "interval": {"started_at": started, "completed_at": completed},
        "count_manifest": {"expected_all": 1},
        "campaigns": [campaign],
        "identity_incidents": incidents,
        "counts": {
            "manifest_campaigns": 1,
            "detail_campaigns": 1,
            "bids": 1,
            "identity_incidents": len(incidents),
            "issues": 0 if complete else 1,
        },
    }
    ads["manifest_digest"] = canonical_digest(ads)
    payload = {
        "contract_name": "wb_change_registry_source_acquisition",
        "contract_version": 1,
        "mapping_version": "wb_change_registry_mapping_v1",
        "seller": {"seller_id": SELLER, "account_scope": ACCOUNT},
        "interval": {"started_at": started, "completed_at": completed},
        "completeness_status": "complete" if complete else "partial",
        "joint_complete": complete,
        "sources": {"prices": prices, "ads": ads},
        "counts": {
            "price_goods": 1 if include_good else 0,
            "ads_manifest_campaigns": 1,
            "ads_detail_campaigns": 1,
            "identity_incidents": len(incidents),
        },
        "persistence": {
            "registry_rows_written": 0,
            "checkpoints_written": 0,
            "facts_written": 0,
            "identity_incidents_written": 0,
        },
    }
    payload["manifest_digest"] = canonical_digest(payload)
    return payload


class SnapshotAcquirer:
    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.snapshot = deepcopy(snapshot)
        self.upload_task_calls = 0
        self.patch_bids_calls = 0
        self.balance_wb_patch_called = False

    def acquire(self) -> dict[str, Any]:
        return deepcopy(self.snapshot)


def _observer(runtime_dir: Path, snapshot: Mapping[str, Any], now: str) -> tuple[ChangeRegistryObserver, SnapshotAcquirer]:
    acquirer = SnapshotAcquirer(snapshot)
    return (
        ChangeRegistryObserver(
            runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: acquirer,
            now_fn=lambda: now,
        ),
        acquirer,
    )


def _counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            "checkpoints": conn.execute(f"SELECT COUNT(*) FROM {CHECKPOINTS_TABLE}").fetchone()[0],
            "facts": conn.execute(f"SELECT COUNT(*) FROM {FACTS_TABLE}").fetchone()[0],
            "incidents": conn.execute(f"SELECT COUNT(*) FROM {IDENTITY_INCIDENTS_TABLE}").fetchone()[0],
        }


def _atomic_result_counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                CHECKPOINTS_TABLE,
                CHECKPOINT_SOURCE_MANIFESTS_TABLE,
                OBSERVATION_VALUES_TABLE,
                IDENTITY_INCIDENTS_TABLE,
                FACTS_TABLE,
                FACT_LINKS_TABLE,
            )
        }


def main() -> None:
    with TemporaryDirectory(prefix="change-registry-observer-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime_dir.mkdir(parents=True)
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        native_jsonl = runtime_dir / "sheet_vitrina_v1_native_audit.jsonl"
        native_jsonl.write_bytes(b'{"native":"unchanged"}\n')
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE sheet_vitrina_v1_sku_action_events(id INTEGER PRIMARY KEY, payload TEXT)")
            conn.execute("INSERT INTO sheet_vitrina_v1_sku_action_events(payload) VALUES('unchanged')")
            conn.commit()
        native_before = native_jsonl.read_bytes()

        observer, adapter = _observer(runtime_dir, _snapshot(0), "2026-08-29T00:00:00Z")
        baseline = observer.run(trigger_kind="activation", requested_by="release-runner", job_id="activation-baseline")
        assert baseline["events"][-1]["state"] == "complete"
        assert _counts(db_path) == {"checkpoints": 1, "facts": 0, "incidents": 0}

        observer, _ = _observer(runtime_dir, _snapshot(10), "2026-08-29T00:10:00Z")
        observer.run(trigger_kind="manual", requested_by="operator", job_id="unchanged")
        assert _counts(db_path)["facts"] == 0

        observer, _ = _observer(runtime_dir, _snapshot(20, price=12000), "2026-08-29T00:20:00Z")
        changed = observer.run(trigger_kind="manual", requested_by="operator", job_id="changed")
        assert changed["events"][-1]["fact_count"] == 1
        assert _counts(db_path)["facts"] == 1
        surface = ChangeRegistryReadSurface(runtime_dir, seller_id=SELLER, account_scope=ACCOUNT)
        fact_id = surface.overview()["facts"][0]["fact_id"]
        annotation = surface.annotate(
            {"subject_kind": "fact", "subject_id": fact_id, "comment": "Проверено"},
            actor="operator",
            now="2026-08-29T00:20:40Z",
        )
        surface.annotate(
            {
                "subject_kind": "fact",
                "subject_id": fact_id,
                "parent_revision_id": annotation["annotation_revision_id"],
                "comment": "Уточнено",
            },
            actor="operator",
            now="2026-08-29T00:20:41Z",
        )
        assert len(surface.overview()["annotations"]) == 2
        assert observer.run(trigger_kind="manual", requested_by="operator", job_id="changed")["events"][-1]["fact_count"] == 1
        assert _counts(db_path)["facts"] == 1
        replay_counts = _counts(db_path)
        replay_observer, _ = _observer(
            runtime_dir,
            _snapshot(20, price=12000),
            "2026-08-29T00:21:00Z",
        )
        replay_other_job = replay_observer.run(
            trigger_kind="manual",
            requested_by="operator",
            job_id="changed-proof-replay",
        )
        assert replay_other_job["events"][-1]["checkpoint_id"] == changed["events"][-1]["checkpoint_id"]
        assert _counts(db_path) == replay_counts

        observer, _ = _observer(runtime_dir, _snapshot(30, price=0), "2026-08-29T00:30:00Z")
        zero = observer.run(trigger_kind="manual", requested_by="operator", job_id="zero")
        assert zero["events"][-1]["fact_count"] == 1

        before_nonexact = _counts(db_path)
        for index, value in enumerate(
            (
                _nonexact("missing", "field_absent"),
                _nonexact("null", "source_null"),
                _nonexact("inapplicable", "size_level"),
            ),
            start=1,
        ):
            observer, _ = _observer(runtime_dir, _snapshot(30 + index, price=value), f"2026-08-29T00:{30 + index:02d}:00Z")
            result = observer.run(trigger_kind="manual", requested_by="operator", job_id=f"nonexact-{index}")
            assert result["events"][-1]["fact_count"] == 0
        assert _counts(db_path)["facts"] == before_nonexact["facts"]

        observer, _ = _observer(runtime_dir, _snapshot(34, price=4444), "2026-08-29T00:34:00Z")
        after_evidence_gap = observer.run(
            trigger_kind="manual",
            requested_by="operator",
            job_id="exact-after-evidence-gap",
        )
        assert after_evidence_gap["events"][-1]["fact_count"] == 1
        gap_fact = ChangeRegistryReadSurface(
            runtime_dir, seller_id=SELLER, account_scope=ACCOUNT
        ).overview()["interval_state"][0]
        assert gap_fact["observation_window"] == {
            "from": "2026-08-29T00:30:30Z",
            "to": "2026-08-29T00:34:30Z",
        }

        observer, _ = _observer(runtime_dir, _snapshot(40, price=7777, complete=False), "2026-08-29T00:40:00Z")
        partial = observer.run(trigger_kind="manual", requested_by="operator", job_id="partial")
        assert partial["events"][-1]["state"] == "partial" and partial["events"][-1]["fact_count"] == 0

        observer, _ = _observer(runtime_dir, _snapshot(41, include_good=False), "2026-08-29T00:41:00Z")
        disappeared = observer.run(trigger_kind="manual", requested_by="operator", job_id="disappeared")
        assert disappeared["events"][-1]["fact_count"] == 0
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {OBSERVATION_VALUES_TABLE} WHERE health_code='target_disappeared'").fetchone()[0] >= 1

        observer, _ = _observer(runtime_dir, _snapshot(42, price=4444, mapping=()), "2026-08-29T00:42:00Z")
        invalid = observer.run(trigger_kind="manual", requested_by="operator", job_id="identity-zero")
        assert invalid["events"][-1]["fact_count"] == 0 and _counts(db_path)["incidents"] == 1
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {OBSERVATION_VALUES_TABLE} "
                "WHERE checkpoint_id=? AND advert_id=201",
                (invalid["events"][-1]["checkpoint_id"],),
            ).fetchone()[0] == 0
        observer, _ = _observer(runtime_dir, _snapshot(43, price=4444, mapping=(101, 102)), "2026-08-29T00:43:00Z")
        invalid_many = observer.run(trigger_kind="manual", requested_by="operator", job_id="identity-many")
        assert _counts(db_path)["incidents"] == 2
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {OBSERVATION_VALUES_TABLE} "
                "WHERE checkpoint_id=? AND advert_id=201",
                (invalid_many["events"][-1]["checkpoint_id"],),
            ).fetchone()[0] == 0

        holder, _ = _observer(runtime_dir, _snapshot(44), "2026-08-29T00:44:00Z")
        holder._admit(job_id="lease-holder", trigger_kind="manual", scheduled_slot_value="", requested_by="operator", requested_at="2026-08-29T00:44:00Z", request_digest=canonical_digest({"lease": 1}))
        contender, _ = _observer(runtime_dir, _snapshot(44), "2026-08-29T00:44:01Z")
        try:
            contender.run(trigger_kind="manual", requested_by="operator", job_id="lease-contender")
        except ChangeRegistryObserverBusy:
            pass
        else:
            raise AssertionError("concurrent observer did not fail closed on the lease")
        holder._fail_job("lease-holder", "manual", "", RuntimeError("smoke release"))

        before_failure = _counts(db_path)
        atomic_before_failure = _atomic_result_counts(db_path)
        observer, _ = _observer(runtime_dir, _snapshot(45, price=3333), "2026-08-29T00:45:00Z")
        try:
            observer.run(trigger_kind="manual", requested_by="operator", job_id="db-rollback", inject_db_failure=True)
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("injected DB failure was not surfaced")
        assert _counts(db_path) == before_failure
        assert _atomic_result_counts(db_path) == atomic_before_failure

        for hour, slot in ((2, "2026-08-29T02:00:00Z"), (4, "2026-08-29T04:00:00Z")):
            observer, _ = _observer(runtime_dir, _snapshot(hour * 60, complete=False), slot)
            observer.run(trigger_kind="scheduled", requested_by="systemd", scheduled_slot_value=slot)
        overview = ChangeRegistryReadSurface(runtime_dir, seller_id=SELLER, account_scope=ACCOUNT).overview()
        assert overview["status"]["health_state"] == "degraded"
        health_count = 2
        observer, _ = _observer(runtime_dir, _snapshot(6 * 60), "2026-08-29T06:00:00Z")
        observer.run(trigger_kind="manual", requested_by="operator", job_id="manual-health-neutral")
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {OBSERVER_HEALTH_EVENTS_TABLE}").fetchone()[0] == health_count
        observer, _ = _observer(runtime_dir, _snapshot(8 * 60), "2026-08-29T08:00:00Z")
        observer.run(trigger_kind="scheduled", requested_by="systemd", scheduled_slot_value="2026-08-29T08:00:00Z")
        overview = ChangeRegistryReadSurface(runtime_dir, seller_id=SELLER, account_scope=ACCOUNT).overview()
        assert overview["status"]["health_state"] == "normal"
        assert overview["interval_semantics"].startswith("Время изменения")

        async_observer, _ = _observer(runtime_dir, _snapshot(9 * 60), "2026-08-29T09:00:00Z")
        async_observer.submit_manual(requested_by="operator", job_id="async-manual")
        for _attempt in range(50):
            async_job = async_observer.read_job("async-manual")
            if async_job["events"][-1]["state"] in {"complete", "partial", "failed"}:
                break
            time.sleep(0.01)
        assert async_job["events"][-1]["state"] == "complete"

        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT payload FROM sheet_vitrina_v1_sku_action_events").fetchone()[0] == "unchanged"
            assert conn.execute(f"SELECT COUNT(*) FROM {OBSERVER_JOB_EVENTS_TABLE} WHERE state='busy'").fetchone()[0] == 0
        assert native_jsonl.read_bytes() == native_before
        assert adapter.upload_task_calls == 0 and adapter.patch_bids_calls == 0
        assert adapter.balance_wb_patch_called is False

        source_runtime = Path(tmp) / "source-runtime"
        prices_source = FakePricesSource(
            {
                0: [
                    {
                        "nmID": 101,
                        "vendorCode": "observer",
                        "discount": 10,
                        "currencyIsoCode4217": "RUB",
                        "editableSizePrice": False,
                        "sizes": [
                            {
                                "sizeID": 1,
                                "techSizeName": "ONE",
                                "price": 100,
                                "discountedPrice": 90,
                            }
                        ],
                    }
                ],
                1000: [],
            }
        )
        ads_source = FakeAdsSource(
            _count_payload([201]),
            {201: _detail(201, [101])},
        )
        source_acquirer = ChangeRegistrySourceAcquirer(
            seller_id=SELLER,
            account_scope=ACCOUNT,
            prices_source=prices_source,
            ads_source=ads_source,
            now_fn=lambda: "2026-08-29T10:00:00Z",
            sleep_fn=lambda _seconds: None,
        )
        ChangeRegistryObserver(
            source_runtime,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: source_acquirer,
            now_fn=lambda: "2026-08-29T10:00:00Z",
        ).run(
            trigger_kind="activation",
            requested_by="smoke",
            job_id="real-readonly-acquirer",
        )
        assert prices_source.write_calls == 0
        assert ads_source.write_calls == 0

    print("change_registry_observer_smoke: OK")


if __name__ == "__main__":
    main()
