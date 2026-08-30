"""Deterministic acceptance smoke for Balance manual portal fallback."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.change_registry_observer_smoke import (  # noqa: E402
    ACCOUNT,
    SELLER,
    _observer,
    _snapshot,
)
from packages.application.change_registry import (  # noqa: E402
    ATTEMPT_EVENTS_TABLE,
    FACT_LINKS_TABLE,
    FACTS_TABLE,
    ITEMS_TABLE,
    MANUAL_PENDING_CURRENT_TABLE,
    MANUAL_PENDING_EVENTS_TABLE,
    ChangeRegistryConflict,
    ChangeRegistryError,
    ChangeRegistryRepository,
)
from packages.application.change_registry_observer import (  # noqa: E402
    ChangeRegistryReadSurface,
)
from packages.application.change_registry_writer import (  # noqa: E402
    InternalWriterRegistry,
)


def _recommendation(
    recommendation_id: str,
    *,
    before: int = 0,
    desired: int = 1250,
) -> dict[str, object]:
    return {
        "recommendation_item_id": recommendation_id,
        "action_type": "bid_change",
        "target": {
            "seller_id": SELLER,
            "account_scope": ACCOUNT,
            "target_kind": "bid",
            "nm_id": 101,
            "advert_id": 201,
            "placement": "search",
            "parameter_field": "bid_minor",
        },
        "before_value": before,
        "requested_value": desired,
    }


def _baseline(runtime_dir: Path, *, mapping: tuple[int, ...] = (101,)) -> None:
    observer, _ = _observer(
        runtime_dir, _snapshot(0, bid=0, mapping=mapping), "2026-08-29T00:00:40Z"
    )
    observer.initialize_schema()
    observer.run(trigger_kind="manual", requested_by="smoke", job_id="baseline")


def _register(
    runtime_dir: Path,
    recommendation_id: str,
    *,
    requested_at: str = "2026-08-29T00:01:00Z",
    desired: int = 1250,
) -> dict[str, object]:
    return ChangeRegistryRepository(runtime_dir).register_manual_pending(
        seller_id=SELLER,
        account_scope=ACCOUNT,
        calculation_id="ibcalc_smoke_0001",
        recommendations=[_recommendation(recommendation_id, desired=desired)],
        actor_principal="operator@example.test",
        requested_at=requested_at,
    )


def _counts(runtime_dir: Path) -> dict[str, int]:
    db_path = runtime_dir / "registry_upload_runtime.sqlite3"
    with sqlite3.connect(db_path) as conn:
        return {
            "facts": conn.execute(f"SELECT COUNT(*) FROM {FACTS_TABLE}").fetchone()[0],
            "attempts": conn.execute(
                f"SELECT COUNT(*) FROM {ATTEMPT_EVENTS_TABLE}"
            ).fetchone()[0],
            "items": conn.execute(f"SELECT COUNT(*) FROM {ITEMS_TABLE}").fetchone()[0],
            "events": conn.execute(
                f"SELECT COUNT(*) FROM {MANUAL_PENDING_EVENTS_TABLE}"
            ).fetchone()[0],
        }


def _state(runtime_dir: Path, recommendation_id: str) -> dict[str, object]:
    return ChangeRegistryRepository(runtime_dir).manual_pending_statuses(
        [recommendation_id]
    )[recommendation_id]


def main() -> None:
    with TemporaryDirectory(prefix="change-registry-manual-pending-") as tmp:
        root = Path(tmp)

        manual_first = root / "manual-first"
        _baseline(manual_first)
        receipt = _register(manual_first, "ibr_manual_first")
        assert receipt["external_writes"] is False
        assert receipt["facts_created"] == 0 and receipt["attempts_created"] == 0
        assert _counts(manual_first) == {
            "facts": 0,
            "attempts": 0,
            "items": 1,
            "events": 1,
        }
        replay = _register(
            manual_first,
            "ibr_manual_first",
            requested_at="2026-08-29T00:02:00Z",
        )
        assert replay["operation_id"] == receipt["operation_id"]
        assert _counts(manual_first)["events"] == 1
        try:
            _register(manual_first, "ibr_manual_first", desired=1300)
        except ChangeRegistryConflict:
            pass
        else:
            raise AssertionError("conflicting recommendation replay did not fail closed")
        changed, adapter = _observer(
            manual_first, _snapshot(3, bid=1250), "2026-08-29T00:03:40Z"
        )
        changed.run(trigger_kind="manual", requested_by="operator", job_id="exact")
        assert adapter.upload_task_calls == 0 and adapter.patch_bids_calls == 0
        assert _state(manual_first, "ibr_manual_first")["state"] == "matched"
        counts = _counts(manual_first)
        assert counts["facts"] == 1 and counts["attempts"] == 0
        with sqlite3.connect(manual_first / "registry_upload_runtime.sqlite3") as conn:
            kinds = {
                row[0]
                for row in conn.execute(
                    f"SELECT link_kind FROM {FACT_LINKS_TABLE}"
                ).fetchall()
            }
        assert {"checkpoint", "change_item", "recommendation_item"} <= kinds

        observer_first = root / "observer-first"
        _baseline(observer_first)
        observed, _ = _observer(
            observer_first, _snapshot(2, bid=1250), "2026-08-29T00:02:40Z"
        )
        observed.run(trigger_kind="manual", requested_by="operator", job_id="early")
        _register(observer_first, "ibr_observer_first")
        assert _state(observer_first, "ibr_observer_first")["state"] == "matched"
        assert _counts(observer_first)["facts"] == 1

        deviation = root / "deviation"
        _baseline(deviation)
        _register(deviation, "ibr_deviation")
        deviated, _ = _observer(
            deviation, _snapshot(3, bid=900), "2026-08-29T00:03:40Z"
        )
        deviated.run(trigger_kind="manual", requested_by="operator", job_id="deviated")
        status = _state(deviation, "ibr_deviation")
        assert status["state"] == "deviated" and status["related_fact_id"]
        with sqlite3.connect(deviation / "registry_upload_runtime.sqlite3") as conn:
            forbidden = conn.execute(
                f"SELECT COUNT(*) FROM {FACT_LINKS_TABLE} "
                "WHERE link_kind IN ('change_item','recommendation_item')"
            ).fetchone()[0]
        assert forbidden == 0
        surface = ChangeRegistryReadSurface(
            deviation, seller_id=SELLER, account_scope=ACCOUNT
        )
        overview = surface.overview()
        assert overview["storage"] == {"mode": "ro", "query_only": True}
        assert overview["manual_pending"][0]["state"] == "deviated"
        surface.annotate(
            {
                "subject_kind": "manual_pending",
                "subject_id": status["pending_id"],
                "comment": "Проверено оператором; это не fulfillment рекомендации.",
            },
            actor="operator",
            now="2026-08-29T00:04:00Z",
        )

        supersession = root / "supersession"
        _baseline(supersession)
        _register(supersession, "ibr_old", desired=1100)
        _register(
            supersession,
            "ibr_new",
            requested_at="2026-08-29T00:02:00Z",
            desired=1200,
        )
        assert _state(supersession, "ibr_old")["state"] == "superseded"
        assert _state(supersession, "ibr_new")["state"] == "pending"
        history_states = {
            item["recommendation_item_id"]: item["state"]
            for item in ChangeRegistryReadSurface(
                supersession, seller_id=SELLER, account_scope=ACCOUNT
            ).overview()["manual_pending"]
        }
        assert history_states == {"ibr_old": "superseded", "ibr_new": "pending"}
        with sqlite3.connect(supersession / "registry_upload_runtime.sqlite3") as conn:
            pointer = conn.execute(
                f"SELECT active,revision FROM {MANUAL_PENDING_CURRENT_TABLE}"
            ).fetchone()
        assert pointer == (1, 3)

        expiry = root / "expiry"
        _baseline(expiry)
        _register(expiry, "ibr_expiry")
        repository = ChangeRegistryRepository(expiry)
        with repository.store_registry.session(
            "operational", mode="rw", operation="manual_pending_expiry_smoke"
        ) as conn:
            checkpoint_id = conn.execute(
                "SELECT checkpoint_id FROM change_registry_checkpoints LIMIT 1"
            ).fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            repository.reconcile_manual_pending_in_transaction(
                conn,
                seller_id=SELLER,
                account_scope=ACCOUNT,
                checkpoint_id=checkpoint_id,
                reconciled_at="2026-08-30T00:01:01Z",
            )
            conn.commit()
        assert _state(expiry, "ibr_expiry")["state"] == "expired"
        assert _counts(expiry)["facts"] == 0

        invalid = root / "invalid-identity"
        _baseline(invalid, mapping=())
        try:
            _register(invalid, "ibr_invalid")
        except ChangeRegistryError:
            pass
        else:
            raise AssertionError("zero-cardinality identity created a pending lifecycle")
        assert _counts(invalid)["items"] == 0

        live_writer_only = root / "live-writer-only"
        live_writer_repository = ChangeRegistryRepository(live_writer_only)
        live_writer_repository.initialize_schema()
        live_writer = InternalWriterRegistry(
            runtime_dir=live_writer_only,
            seller_id=SELLER,
            account_scope=ACCOUNT,
        )
        live_writer.prepare_bid(
            source_surface="sku_inventory_balance",
            actor="operator",
            native_operation_id="live-apply-without-manual-pending",
            nm_id=101,
            advert_id=201,
            placement="search",
            before_bid_minor=1000,
            requested_bid_minor=1100,
            requested_at="2026-08-29T00:01:00Z",
            calculation_id="ibcalc_live_writer",
            apply_operation_id="ibapply_live_writer",
            recommendation_item_id="ibr_live_writer_only",
        )
        assert live_writer_repository.manual_pending_statuses(
            ["ibr_live_writer_only"]
        ) == {}

    print("change_registry_manual_pending_smoke: OK")


if __name__ == "__main__":
    main()
