"""Deterministic smoke for the change-registry baseline engine."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.change_registry import (  # noqa: E402
    CHECKPOINTS_TABLE,
    FACT_LINKS_TABLE,
    FACTS_TABLE,
    IDENTITY_INCIDENTS_TABLE,
    IMMUTABLE_TABLES,
    OBSERVATION_VALUES_TABLE,
    ChangeRegistryConflict,
    ChangeRegistryRepository,
    canonical_digest,
    target_identity,
)
from packages.application.change_registry_baseline_engine import (  # noqa: E402
    CREATION_ABSENT_STATE,
    ChangeRegistryBaselineEngine,
)


SELLER = "seller-canonical"
ACCOUNT = "wb-seller-account"


def main() -> None:
    with TemporaryDirectory(prefix="change-registry-engine-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        repository = ChangeRegistryRepository(runtime_dir)
        repository.initialize_schema()
        engine = ChangeRegistryBaselineEngine(
            runtime_dir=runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
        )
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"

        noncanonical = _acquisition(
            started_at="2026-08-29T08:00:00Z",
            completed_at="2026-08-29T08:01:00Z",
            status="complete",
            prices=[],
            campaigns=[],
        )
        noncanonical = deepcopy(noncanonical)
        noncanonical["interval"] = {
            "started_at": "2026-08-29T08:00:00+00:00",
            "completed_at": "2026-08-29T13:01:00+05:00",
        }
        for source in noncanonical["sources"].values():
            source["interval"] = dict(noncanonical["interval"])
            source.pop("manifest_digest")
            source["manifest_digest"] = canonical_digest(source)
        noncanonical.pop("manifest_digest")
        noncanonical["manifest_digest"] = canonical_digest(noncanonical)
        try:
            engine.ingest(noncanonical)
        except ChangeRegistryConflict:
            pass
        else:
            raise AssertionError(
                "baseline accepted noncanonical timestamp-bearing digested input"
            )

        partial_before = _acquisition(
            started_at="2026-08-29T09:00:00Z",
            completed_at="2026-08-29T09:01:00Z",
            status="partial",
            prices=[
                _price(
                    2,
                    original=_inapplicable("size_level_representation_required"),
                    discount=_missing("field_absent"),
                    seller=_exact_null(),
                )
            ],
            campaigns=[],
        )
        pre_receipt = engine.ingest(partial_before)
        assert pre_receipt["completeness_status"] == "partial"
        assert pre_receipt["previous_complete_checkpoint_id"] is None
        assert pre_receipt["fact_ids"] == []
        _assert_status_distinctions(db_path, pre_receipt["checkpoint_id"])

        baseline = _acquisition(
            started_at="2026-08-29T10:00:00Z",
            completed_at="2026-08-29T10:01:00Z",
            status="complete",
            prices=[_price(1, original=1_000, discount=1_000, seller=900)],
            campaigns=[
                _campaign(10, 1, state="active", model="cpm", bid=500),
                _identity_campaign(20, []),
                _identity_campaign(30, [1, 2]),
            ],
        )
        baseline_receipt = engine.ingest(baseline)
        assert baseline_receipt["baseline_only"] is True
        assert baseline_receipt["previous_complete_checkpoint_id"] is None
        assert baseline_receipt["fact_ids"] == []
        assert len(baseline_receipt["identity_incident_ids"]) == 2
        _assert_identity_incidents_fail_closed(
            db_path, baseline_receipt["checkpoint_id"]
        )

        counts_before_repeat = _counts(db_path)
        assert engine.ingest(baseline) == baseline_receipt
        assert _counts(db_path) == counts_before_repeat

        partial_after = _acquisition(
            started_at="2026-08-29T10:30:00Z",
            completed_at="2026-08-29T10:31:00Z",
            status="partial",
            prices=[_price(1, original=9_999, discount=9_999, seller=9_999)],
            campaigns=[
                _campaign(10, 1, state="cancelled", model="cpc", bid=9_999)
            ],
        )
        partial_receipt = engine.ingest(partial_after)
        assert partial_receipt["previous_complete_checkpoint_id"] == baseline_receipt[
            "checkpoint_id"
        ]
        assert partial_receipt["fact_ids"] == []

        second = _acquisition(
            started_at="2026-08-29T11:00:00Z",
            completed_at="2026-08-29T11:01:00Z",
            status="complete",
            prices=[_price(1, original=1_000, discount=0, seller=1_000)],
            campaigns=[
                _campaign(10, 1, state="paused", model="cpc", bid=0),
                _identity_campaign(20, []),
                _identity_campaign(30, [1, 2]),
                _campaign(40, 2, state="active", model="cpm", bid=700),
            ],
        )
        second_receipt = engine.ingest(second)
        assert second_receipt["previous_complete_checkpoint_id"] == baseline_receipt[
            "checkpoint_id"
        ]
        assert second_receipt["baseline_only"] is False
        assert len(second_receipt["fact_ids"]) == 7
        _assert_second_diff(db_path, second_receipt["checkpoint_id"])

        counts_before_rollback = _counts(db_path)
        third = _acquisition(
            started_at="2026-08-29T12:00:00Z",
            completed_at="2026-08-29T12:01:00Z",
            status="complete",
            prices=[_price(1, original=1_000, discount=0, seller=1_100)],
            campaigns=[
                _campaign(10, 1, state="paused", model="cpc", bid=0),
                _identity_campaign(20, []),
                _identity_campaign(30, [1, 2]),
                _campaign(40, 2, state="active", model="cpm", bid=700),
            ],
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"""CREATE TRIGGER test_change_registry_fact_abort
                    BEFORE INSERT ON {FACTS_TABLE}
                    BEGIN SELECT RAISE(ABORT,'injected transaction failure'); END"""
            )
            conn.commit()
        try:
            engine.ingest(third)
        except ChangeRegistryConflict:
            pass
        else:
            raise AssertionError("injected transaction failure was not surfaced")
        assert _counts(db_path) == counts_before_rollback
        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TRIGGER test_change_registry_fact_abort")
            conn.commit()
        third_receipt = engine.ingest(third)
        assert len(third_receipt["fact_ids"]) == 1
        assert engine.ingest(third) == third_receipt

        _assert_projection(engine)

        evidence_gap = _acquisition(
            started_at="2026-08-29T12:10:00Z",
            completed_at="2026-08-29T12:11:00Z",
            status="complete",
            prices=[_price(1, original=1_000, discount=0, seller=_exact_null())],
            campaigns=[
                _campaign(10, 1, state="paused", model="cpc", bid=0),
                _identity_campaign(20, []),
                _identity_campaign(30, [1, 2]),
                _campaign(40, 2, state="active", model="cpm", bid=700),
            ],
        )
        gap_receipt = engine.ingest(evidence_gap)
        assert gap_receipt["fact_ids"] == []
        resumed = _acquisition(
            started_at="2026-08-29T12:20:00Z",
            completed_at="2026-08-29T12:21:00Z",
            status="complete",
            prices=[_price(1, original=1_000, discount=0, seller=1_200)],
            campaigns=[
                _campaign(10, 1, state="paused", model="cpc", bid=0),
                _identity_campaign(20, []),
                _identity_campaign(30, [1, 2]),
                _campaign(40, 2, state="active", model="cpm", bid=700),
            ],
        )
        resumed_receipt = engine.ingest(resumed)
        assert len(resumed_receipt["fact_ids"]) == 1
        _assert_evidence_gap_projection(engine, db_path, resumed_receipt["fact_ids"][0])

        failed = _acquisition(
            started_at="2026-08-29T12:30:00Z",
            completed_at="2026-08-29T12:31:00Z",
            status="failed",
            prices=[],
            campaigns=[],
        )
        facts_before_failed = _table_count(db_path, FACTS_TABLE)
        failed_receipt = engine.ingest(failed)
        assert failed_receipt["previous_complete_checkpoint_id"] == resumed_receipt[
            "checkpoint_id"
        ]
        assert failed_receipt["fact_ids"] == []
        assert _table_count(db_path, FACTS_TABLE) == facts_before_failed
        assert _latest_complete_checkpoint(db_path) == resumed_receipt["checkpoint_id"]

        projection_before_unbound = engine.project_intervals(
            target=target_identity("price", nm_id=1),
            parameter_field="seller_price_minor",
        )
        repository.append_fact(
            fact_id="non-checkpoint-proof",
            seller_id=SELLER,
            account_scope=ACCOUNT,
            target=target_identity("price", nm_id=1),
            parameter_field="seller_price_minor",
            before_value=1_200,
            after_value=1_300,
            observed_from="2026-08-29T12:21:00Z",
            observed_to="2026-08-29T13:00:00Z",
            proven_at="2026-08-29T13:00:00Z",
            proof_kind="native_audit",
            evidence_digest=canonical_digest({"proof": "unsupported-projection"}),
        )
        # Writer facts are inert for interval projection until an exact
        # checkpoint reconciliation late-links them.
        assert engine.project_intervals(
            target=target_identity("price", nm_id=1),
            parameter_field="seller_price_minor",
        ) == projection_before_unbound

    print("change_registry_baseline_engine_smoke: OK")


def _assert_status_distinctions(db_path: Path, checkpoint_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""SELECT parameter_field,observation_status,value_kind,value_integer
                FROM {OBSERVATION_VALUES_TABLE}
                WHERE checkpoint_id=? ORDER BY parameter_field""",
            (checkpoint_id,),
        ).fetchall()
    by_field = {row[0]: row[1:] for row in rows}
    assert by_field["original_price_minor"] == ("inapplicable", "missing", None)
    assert by_field["discount_bps"] == ("missing", "missing", None)
    assert by_field["seller_price_minor"] == ("exact", "null", None)


def _assert_identity_incidents_fail_closed(db_path: Path, checkpoint_id: str) -> None:
    surface = f"checkpoint:{checkpoint_id}"
    with sqlite3.connect(db_path) as conn:
        incidents = conn.execute(
            f"""SELECT advert_id,candidate_count,candidate_nm_ids_json
                FROM {IDENTITY_INCIDENTS_TABLE}
                WHERE source_surface=? ORDER BY advert_id""",
            (surface,),
        ).fetchall()
        observations = conn.execute(
            f"""SELECT COUNT(*) FROM {OBSERVATION_VALUES_TABLE}
                WHERE checkpoint_id=? AND advert_id IN (20,30)""",
            (checkpoint_id,),
        ).fetchone()[0]
        facts = conn.execute(
            f"SELECT COUNT(*) FROM {FACTS_TABLE} WHERE advert_id IN (20,30)"
        ).fetchone()[0]
    assert incidents == [(20, 0, "[]"), (30, 2, "[1,2]")]
    assert observations == 0
    assert facts == 0


def _assert_second_diff(db_path: Path, checkpoint_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""SELECT * FROM {FACTS_TABLE}
                WHERE fact_id IN (
                    SELECT fact_id FROM {FACT_LINKS_TABLE}
                    WHERE link_kind='checkpoint' AND checkpoint_id=?
                ) ORDER BY target_kind,advert_id,placement,parameter_field""",
            (checkpoint_id,),
        ).fetchall()
        exact_links = conn.execute(
            f"""SELECT COUNT(*) FROM {FACTS_TABLE} fact
                JOIN {FACT_LINKS_TABLE} link
                  ON link.fact_id=fact.fact_id AND link.link_kind='checkpoint'
                JOIN {OBSERVATION_VALUES_TABLE} observation
                  ON observation.checkpoint_id=link.checkpoint_id
                 AND observation.target_kind=fact.target_kind
                 AND observation.nm_id=fact.nm_id
                 AND observation.advert_id=fact.advert_id
                 AND observation.placement=fact.placement
                 AND observation.parameter_field=fact.parameter_field
                WHERE link.checkpoint_id=?
                  AND observation.observation_status IN ('exact','exact_zero')""",
            (checkpoint_id,),
        ).fetchone()[0]
    assert exact_links == len(rows) == 7
    keys = {
        (row["target_kind"], row["advert_id"], row["placement"], row["parameter_field"])
        for row in rows
    }
    assert keys == {
        ("price", 0, "", "discount_bps"),
        ("price", 0, "", "seller_price_minor"),
        ("bid", 10, "combined", "bid_minor"),
        ("campaign", 10, "", "campaign_state"),
        ("campaign", 10, "", "payment_model"),
        ("campaign", 10, "", "payment_unit"),
        ("campaign", 40, "", "campaign_state"),
    }
    zero_rows = [
        row
        for row in rows
        if row["parameter_field"] in {"discount_bps", "bid_minor"}
    ]
    assert len(zero_rows) == 2
    assert all(row["after_value_kind"] == "integer" for row in zero_rows)
    assert all(row["after_value_integer"] == 0 for row in zero_rows)
    creation = next(row for row in rows if row["advert_id"] == 40)
    assert creation["before_value_kind"] == "text"
    assert creation["before_value_text"] == CREATION_ABSENT_STATE
    assert creation["after_value_text"] == "active"


def _assert_projection(engine: ChangeRegistryBaselineEngine) -> None:
    target = target_identity("price", nm_id=1)
    full = engine.project_intervals(
        target=target,
        parameter_field="seller_price_minor",
        limit=200,
    )
    assert [item["value"]["integer_value"] for item in full["items"]] == [
        900,
        1_000,
        1_100,
    ]
    assert [item["end_at"] for item in full["items"]] == [
        "2026-08-29T11:01:00Z",
        "2026-08-29T12:01:00Z",
        None,
    ]
    page_1 = engine.project_intervals(
        target=target,
        parameter_field="seller_price_minor",
        limit=1,
    )
    assert page_1["next_cursor"]
    page_1_repeat = engine.project_intervals(
        target=target,
        parameter_field="seller_price_minor",
        limit=1,
    )
    assert page_1 == page_1_repeat
    page_2 = engine.project_intervals(
        target=target,
        parameter_field="seller_price_minor",
        limit=1,
        cursor=page_1["next_cursor"],
    )
    page_3 = engine.project_intervals(
        target=target,
        parameter_field="seller_price_minor",
        limit=1,
        cursor=page_2["next_cursor"],
    )
    assert page_3["next_cursor"] == ""
    assert page_1["items"] + page_2["items"] + page_3["items"] == full["items"]
    try:
        engine.project_intervals(
            target=target_identity("price", nm_id=2),
            parameter_field="seller_price_minor",
            cursor=page_1["next_cursor"],
        )
    except Exception:
        pass
    else:
        raise AssertionError("projection cursor was accepted for another target")


def _assert_evidence_gap_projection(
    engine: ChangeRegistryBaselineEngine,
    db_path: Path,
    fact_id: str,
) -> None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"SELECT observed_from,observed_to,before_value_integer,after_value_integer "
            f"FROM {FACTS_TABLE} WHERE fact_id=?",
            (fact_id,),
        ).fetchone()
    assert row == (
        "2026-08-29T12:01:00Z",
        "2026-08-29T12:21:00Z",
        1_100,
        1_200,
    )
    projection = engine.project_intervals(
        target=target_identity("price", nm_id=1),
        parameter_field="seller_price_minor",
        limit=200,
    )
    assert projection["items"][-2]["end_at"] == "2026-08-29T12:11:00Z"
    assert projection["items"][-1]["start_at"] == "2026-08-29T12:21:00Z"
    assert projection["items"][-1]["value"]["integer_value"] == 1_200


def _acquisition(
    *,
    started_at: str,
    completed_at: str,
    status: str,
    prices: Sequence[Mapping[str, Any]],
    campaigns: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    source_status = "complete" if status == "complete" else status
    price_payload: dict[str, Any] = {
        "seller_id": SELLER,
        "account_scope": ACCOUNT,
        "completeness_status": source_status,
        "goods": [dict(item) for item in prices],
        "counts": {"goods": len(prices)},
    }
    price_payload["manifest_digest"] = canonical_digest(price_payload)
    bid_count = sum(len(item.get("bids") or []) for item in campaigns)
    ads_payload: dict[str, Any] = {
        "seller_id": SELLER,
        "account_scope": ACCOUNT,
        "completeness_status": source_status,
        "campaigns": [dict(item) for item in campaigns],
        "identity_incidents": [],
        "counts": {
            "manifest_campaigns": len(campaigns),
            "detail_campaigns": len(campaigns),
            "bids": bid_count,
            "identity_incidents": sum(
                1 for item in campaigns if item["mapping"]["status"] == "error"
            ),
        },
    }
    ads_payload["manifest_digest"] = canonical_digest(ads_payload)
    payload: dict[str, Any] = {
        "contract_name": "wb_change_registry_source_acquisition",
        "contract_version": 1,
        "mapping_version": "wb_change_registry_mapping_v1",
        "seller": {"seller_id": SELLER, "account_scope": ACCOUNT},
        "interval": {"started_at": started_at, "completed_at": completed_at},
        "completeness_status": status,
        "joint_complete": status == "complete",
        "sources": {"prices": price_payload, "ads": ads_payload},
        "counts": {
            "price_goods": len(prices),
            "ads_manifest_campaigns": len(campaigns),
            "ads_detail_campaigns": len(campaigns),
            "identity_incidents": ads_payload["counts"]["identity_incidents"],
        },
        "persistence": {
            "registry_rows_written": 0,
            "checkpoints_written": 0,
            "facts_written": 0,
            "identity_incidents_written": 0,
        },
        "wb_mutation_calls": {"post": 0, "patch": 0},
    }
    payload["manifest_digest"] = canonical_digest(payload)
    return payload


def _price(
    nm_id: int,
    *,
    original: Any,
    discount: Any,
    seller: Any,
) -> dict[str, Any]:
    values = {
        "original_price_minor": _shape(original),
        "discount_bps": _shape(discount),
        "seller_price_minor": _shape(seller),
    }
    payload: dict[str, Any] = {"nm_id": nm_id, "sku_values": values}
    payload["record_digest"] = canonical_digest(payload)
    return payload


def _campaign(
    advert_id: int,
    nm_id: int,
    *,
    state: str,
    model: str,
    bid: int,
) -> dict[str, Any]:
    unit = "per_click" if model == "cpc" else "per_thousand_impressions"
    bids = [
        _bid(advert_id, nm_id, "combined", _exact_integer(bid)),
        _bid(advert_id, nm_id, "recommendations", _inapplicable("placement_disabled_by_source")),
        _bid(advert_id, nm_id, "search", _missing("bid_field_absent")),
    ]
    payload: dict[str, Any] = {
        "advert_id": advert_id,
        "mapping": {
            "status": "exact",
            "candidate_nm_ids": [nm_id],
            "candidate_count": 1,
            "exact_nm_id": nm_id,
        },
        "campaign_state": _exact_text(state),
        "payment_model": _exact_text(model),
        "payment_unit": _exact_text(unit),
        "bids": bids,
    }
    payload["record_digest"] = canonical_digest(payload)
    return payload


def _identity_campaign(advert_id: int, candidates: Sequence[int]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "advert_id": advert_id,
        "mapping": {
            "status": "error",
            "candidate_nm_ids": list(candidates),
            "candidate_count": len(set(candidates)),
            "exact_nm_id": None,
        },
        "campaign_state": _exact_text("active"),
        "payment_model": _exact_text("cpm"),
        "payment_unit": _exact_text("per_thousand_impressions"),
        "bids": [],
    }
    payload["record_digest"] = canonical_digest(payload)
    return payload


def _bid(
    advert_id: int,
    nm_id: int,
    placement: str,
    bid_minor: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "advert_id": advert_id,
        "nm_id": nm_id,
        "placement": placement,
        "bid_minor": dict(bid_minor),
    }
    payload["target_digest"] = canonical_digest(payload)
    return payload


def _shape(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, int):
        return _exact_integer(value)
    raise TypeError("unsupported smoke observation")


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


def _exact_null() -> dict[str, Any]:
    return {
        "status": "exact",
        "value": {"kind": "null", "integer_value": None, "text_value": None},
    }


def _missing(reason: str) -> dict[str, Any]:
    return {
        "status": "missing",
        "value": {"kind": "missing", "integer_value": None, "text_value": None},
        "reason": reason,
    }


def _inapplicable(reason: str) -> dict[str, Any]:
    return {
        "status": "inapplicable",
        "value": {"kind": "missing", "integer_value": None, "text_value": None},
        "reason": reason,
    }


def _counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in IMMUTABLE_TABLES
        }


def _table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _latest_complete_checkpoint(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            f"""SELECT checkpoint_id FROM {CHECKPOINTS_TABLE}
                WHERE completeness_status='complete'
                ORDER BY completed_at DESC,checkpoint_id DESC LIMIT 1"""
        ).fetchone()
    assert row is not None
    return str(row[0])


if __name__ == "__main__":
    main()
