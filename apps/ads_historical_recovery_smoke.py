#!/usr/bin/env python3
"""Deterministic mocked smoke for production-safe ads historical recovery."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Sequence
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ads_historical_recovery import (  # noqa: E402
    ALLOWED_CAMPAIGN_STATUSES,
    AdsHistoricalRecovery,
    AdsHistoricalRecoveryError,
    AdsHistoricalNoStatisticsError,
    AdsHistoricalRecoveryScope,
    MAX_IDS_PER_REQUEST,
    MAX_WINDOW_DAYS,
    MIN_REQUEST_INTERVAL_SECONDS,
)
from apps.ads_historical_recovery import (  # noqa: E402
    OfficialAdsHistoricalSource,
    _is_confirmed_no_statistics_http_200_null,
    _is_confirmed_no_statistics_http_400,
    _is_confirmed_no_statistics_payload,
)


class FakeOfficialSource:
    min_request_interval_seconds = MIN_REQUEST_INTERVAL_SECONDS

    def __init__(
        self,
        *,
        empty_date: date,
        omit_target_date: date | None = None,
        fail: bool = False,
    ) -> None:
        self.empty_date = empty_date
        self.omit_target_date = omit_target_date
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def list_campaigns(self) -> dict[str, Any]:
        return {
            "adverts": [
                {
                    "status": status,
                    "advert_list": [
                        {
                            "advertId": campaign_id,
                            "changeTime": "2026-12-31T23:59:59+03:00",
                        }
                        for campaign_id in range(first, last + 1)
                    ],
                }
                for status, first, last in (
                    (7, 1, 55),
                    (9, 56, 105),
                    (11, 106, 120),
                    (4, 999, 999),
                )
            ]
        }

    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("mocked upstream outage")
        self.calls.append(
            {
                "campaign_ids": list(campaign_ids),
                "date_from": date_from,
                "date_to": date_to,
            }
        )
        days: list[dict[str, Any]] = []
        cursor = date_from
        while cursor <= date_to:
            if cursor != self.empty_date:
                nms = [
                    {
                        "nmId": 777000111,
                        "views": 1,
                        "clicks": 1,
                        "atbs": 1,
                        "orders": 1,
                        "sum": "1.25",
                        "sum_price": "2.50",
                    }
                ]
                if cursor != self.omit_target_date:
                    nms.append(
                        {
                            "nmId": 245720334,
                            "views": 10,
                            "clicks": 2,
                            "atbs": 1,
                            "orders": 1,
                            "sum": "12.34",
                            "sum_price": "100.00",
                        }
                    )
                days.append(
                    {
                        "date": cursor.isoformat() + "T00:00:00Z",
                        "apps": [{"nms": nms}],
                    }
                )
            cursor += timedelta(days=1)
        return [
            {
                "advertId": int(campaign_id),
                "days": days if index == 0 else [],
            }
            for index, campaign_id in enumerate(campaign_ids)
        ]


class IncompleteCampaignSource(FakeOfficialSource):
    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> list[dict[str, Any]]:
        payload = super().fetch_fullstats(
            campaign_ids=campaign_ids,
            date_from=date_from,
            date_to=date_to,
        )
        return payload[:-1]


class RecoverableBatchOmissionSource(FakeOfficialSource):
    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> list[dict[str, Any]]:
        payload = super().fetch_fullstats(
            campaign_ids=campaign_ids,
            date_from=date_from,
            date_to=date_to,
        )
        return payload[:-1] if len(campaign_ids) > 1 else payload


class ConfirmedNoStatisticsSource(FakeOfficialSource):
    def list_campaigns(self) -> dict[str, Any]:
        return {
            "adverts": [
                {
                    "status": 7,
                    "advert_list": [
                        {
                            "advertId": 501,
                            "changeTime": "2026-05-01T00:00:00+03:00",
                        }
                    ],
                }
            ]
        }

    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "campaign_ids": list(campaign_ids),
                "date_from": date_from,
                "date_to": date_to,
            }
        )
        if len(campaign_ids) == 1:
            raise AdsHistoricalNoStatisticsError("confirmed no statistics")
        return []


class CompletedBeforeScopeSource(FakeOfficialSource):
    def list_campaigns(self) -> dict[str, Any]:
        return {
            "adverts": [
                {
                    "status": 7,
                    "advert_list": [
                        {
                            "advertId": 401,
                            "changeTime": "2025-01-01T00:00:00+03:00",
                        },
                        {
                            "advertId": 402,
                            "changeTime": "2026-05-01T00:00:00+03:00",
                        },
                    ],
                }
            ]
        }


class UnsupportedOverlapSource(FakeOfficialSource):
    def list_campaigns(self) -> dict[str, Any]:
        return {
            "adverts": [
                {
                    "status": 7,
                    "advert_list": [
                        {
                            "advertId": 601,
                            "changeTime": "2026-05-01T00:00:00+03:00",
                        }
                    ],
                },
                {
                    "status": 8,
                    "advert_list": [
                        {
                            "advertId": 602,
                            "changeTime": "2026-04-15T00:00:00+03:00",
                        }
                    ],
                },
            ]
        }


class MappingSingletonSource(FakeOfficialSource):
    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> Any:
        if len(campaign_ids) == 1:
            return {
                "status": "unexpected",
                "origin": "fixture-origin",
                "detail": "fixture-detail",
                "request_id": "must-not-be-copied",
            }
        payload = super().fetch_fullstats(
            campaign_ids=campaign_ids,
            date_from=date_from,
            date_to=date_to,
        )
        return payload[:-1]


class NoneSingletonSource(FakeOfficialSource):
    def fetch_fullstats(
        self, *, campaign_ids: Sequence[int], date_from: date, date_to: date
    ) -> Any:
        if len(campaign_ids) == 1:
            return None
        payload = super().fetch_fullstats(
            campaign_ids=campaign_ids,
            date_from=date_from,
            date_to=date_to,
        )
        return payload[:-1]


def _seed_database(path: Path, *, existing_date: date, non_target_date: date) -> str:
    sentinel = json.dumps(
        {
            "result": {
                "kind": "success",
                "snapshot_date": existing_date.isoformat(),
                "count": 1,
                "items": [{"nm_id": 245720334, "ads_sum": "99.0000"}],
            },
            "sentinel": "must-not-overwrite",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    non_target = json.dumps(
        {
            "kind": "success",
            "snapshot_date": non_target_date.isoformat(),
            "count": 1,
            "items": [{"nm_id": 1, "ads_sum": "7.0000"}],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE temporal_source_slot_snapshots (
                source_key TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                snapshot_role TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(source_key,snapshot_date,snapshot_role)
            );
            CREATE TABLE temporal_source_closure_state (
                source_key TEXT NOT NULL,
                target_date TEXT NOT NULL,
                slot_kind TEXT NOT NULL,
                state TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                next_retry_at TEXT,
                last_reason TEXT,
                last_attempt_at TEXT,
                last_success_at TEXT,
                accepted_at TEXT,
                PRIMARY KEY(source_key,target_date,slot_kind)
            );
            """
        )
        conn.executemany(
            """INSERT INTO temporal_source_slot_snapshots(
               source_key,snapshot_date,snapshot_role,captured_at,payload_json
               ) VALUES('ads_compact',?,'accepted_closed_day_snapshot',?,?)""",
            (
                (existing_date.isoformat(), "2026-07-01T00:00:00Z", sentinel),
                (non_target_date.isoformat(), "2026-07-01T00:00:00Z", non_target),
            ),
        )
        conn.execute(
            """INSERT INTO temporal_source_closure_state(
               source_key,target_date,slot_kind,state,attempt_count,next_retry_at,
               last_reason,last_attempt_at,last_success_at,accepted_at
               ) VALUES('ads_compact',?,'yesterday_closed','success',1,NULL,
                        'seed',?,?,?)""",
            (
                existing_date.isoformat(),
                "2026-07-01T00:00:00Z",
                "2026-07-01T00:00:00Z",
                "2026-07-01T00:00:00Z",
            ),
        )
        conn.commit()
    return sentinel


def _payload(path: Path, target_date: date) -> dict[str, Any]:
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            """SELECT payload_json FROM temporal_source_slot_snapshots
               WHERE source_key='ads_compact' AND snapshot_date=?
                 AND snapshot_role='accepted_closed_day_snapshot'""",
            (target_date.isoformat(),),
        ).fetchone()[0]
    return json.loads(str(raw))


def _snapshot_count(path: Path) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM temporal_source_slot_snapshots").fetchone()[0])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ads-historical-recovery-") as raw_dir:
        root = Path(raw_dir)
        db_path = root / "registry_upload_runtime.sqlite3"
        backup_dir = root / "backups"
        existing_date = date(2026, 1, 1)
        empty_date = date(2026, 2, 2)
        non_target_date = date(2026, 3, 1)
        target_dates = tuple(
            date(2026, 1, 1) + timedelta(days=offset) for offset in range(33)
        )
        scope = AdsHistoricalRecoveryScope.build(
            nm_ids=[245720334], target_dates=target_dates
        )
        existing_payload = _seed_database(
            db_path,
            existing_date=existing_date,
            non_target_date=non_target_date,
        )
        fixed_now = lambda: datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

        source = FakeOfficialSource(empty_date=empty_date)
        recovery = AdsHistoricalRecovery(
            db_path=db_path, source=source, now_factory=fixed_now
        )
        plan = recovery.plan(scope)
        repeated_plan = recovery.plan(scope)
        assert plan["status"] == "ready", plan["blockers"]
        assert plan["fingerprint"] == repeated_plan["fingerprint"]
        assert plan["write_set"]["insert_snapshot_count"] == 32
        assert plan["target_manifest"][0]["action"] == "skip_existing"
        assert plan["integration_contract"]["allowed_campaign_statuses"] == [7, 9, 11]
        assert plan["integration_contract"]["maximum_window_days_inclusive"] == 31
        assert plan["integration_contract"]["maximum_campaign_ids_per_request"] == 50
        assert plan["integration_contract"]["maximum_requests_per_minute"] == 3
        assert all(len(call["campaign_ids"]) <= MAX_IDS_PER_REQUEST for call in source.calls)
        assert all(
            (call["date_to"] - call["date_from"]).days + 1 <= MAX_WINDOW_DAYS
            for call in source.calls
        )
        assert all(999 not in call["campaign_ids"] for call in source.calls)
        assert {
            campaign["status"] for campaign in plan["source_manifest"]["campaigns"]
        } == ALLOWED_CAMPAIGN_STATUSES
        assert any(
            row.get("payload_kind") == "empty" for row in plan["target_manifest"]
        )

        before_wrong_fingerprint = _snapshot_count(db_path)
        try:
            recovery.apply(
                scope,
                expected_fingerprint="sha256:" + "0" * 64,
                approval_reference="smoke-approval",
                backup_dir=backup_dir,
            )
            raise AssertionError("wrong fingerprint unexpectedly applied")
        except AdsHistoricalRecoveryError:
            pass
        assert _snapshot_count(db_path) == before_wrong_fingerprint

        def fail_after_second_insert(inserted: int) -> None:
            if inserted == 2:
                raise RuntimeError("mocked transactional failure")

        atomic_recovery = AdsHistoricalRecovery(
            db_path=db_path,
            source=FakeOfficialSource(empty_date=empty_date),
            now_factory=fixed_now,
            failure_injector=fail_after_second_insert,
        )
        try:
            atomic_recovery.apply(
                scope,
                expected_fingerprint=plan["fingerprint"],
                approval_reference="smoke-approval",
                backup_dir=backup_dir,
            )
            raise AssertionError("failure injection unexpectedly committed")
        except RuntimeError as exc:
            assert "mocked transactional failure" in str(exc)
        assert _snapshot_count(db_path) == before_wrong_fingerprint

        applied = recovery.apply(
            scope,
            expected_fingerprint=plan["fingerprint"],
            approval_reference="smoke-approval",
            backup_dir=backup_dir,
        )
        assert applied["status"] == "applied"
        assert applied["inserted_snapshot_count"] == 32
        assert applied["readback"]["status"] == "ready"
        backup_path = Path(applied["backup"]["path"])
        assert backup_path.stat().st_mode & 0o777 == 0o600
        assert applied["backup"]["permissions"] == "0600"
        assert applied["backup"]["integrity_check"] == "ok"
        sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        assert applied["backup"]["sha256"] == "sha256:" + sha256

        assert _payload(db_path, existing_date) == json.loads(existing_payload)
        empty_payload = _payload(db_path, empty_date)
        assert empty_payload["kind"] == "empty"
        assert empty_payload["count"] == 0
        assert empty_payload["items"] == []
        assert empty_payload["recovery"]["global_day_response_empty"] is True
        assert empty_payload["recovery"]["synthetic_zero"] is False
        success_payload = _payload(db_path, date(2026, 1, 2))
        assert success_payload["kind"] == "success"
        target = next(
            row for row in success_payload["items"] if row["nm_id"] == 245720334
        )
        assert target["ads_sum"] == 37.02
        assert target["ads_ctr"] == 0.2
        assert target["ads_cr"] == 0.5

        repeated_apply = recovery.apply(
            scope,
            expected_fingerprint=plan["fingerprint"],
            approval_reference="unused-second-reference",
            backup_dir=backup_dir,
        )
        assert repeated_apply["status"] == "no_op_already_applied"
        assert repeated_apply["idempotent"] is True
        assert repeated_apply["backup"] == applied["backup"]
        assert repeated_apply["backup_created_this_attempt"] is False
        with sqlite3.connect(db_path) as conn:
            durable_audit = json.loads(
                conn.execute(
                    "SELECT result_json FROM ads_historical_recovery_audit WHERE fingerprint=?",
                    (plan["fingerprint"],),
                ).fetchone()[0]
            )
        assert durable_audit["backup"]["sha256"] == applied["backup"]["sha256"]
        assert durable_audit["backup"]["integrity_check"] == "ok"
        assert durable_audit["backup"]["permissions"] == "0600"

        failed_source = AdsHistoricalRecovery(
            db_path=db_path,
            source=FakeOfficialSource(
                empty_date=empty_date,
                fail=True,
            ),
            now_factory=fixed_now,
        )
        fresh_scope = AdsHistoricalRecoveryScope.build(
            nm_ids=[245720334], target_dates=[date(2026, 4, 1)]
        )
        failed_plan = failed_source.plan(fresh_scope)
        assert failed_plan["status"] == "blocked"
        assert failed_plan["apply_allowed"] is False
        assert any(
            item["code"] == "ads_upstream_incomplete"
            for item in failed_plan["blockers"]
        )

        unsafe_rate_source = FakeOfficialSource(empty_date=empty_date)
        unsafe_rate_source.min_request_interval_seconds = float("nan")
        unsafe_rate_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=unsafe_rate_source,
            now_factory=fixed_now,
        ).plan(fresh_scope)
        assert unsafe_rate_plan["status"] == "blocked"
        assert any(
            item["code"] == "ads_rate_limit_contract_unsafe"
            for item in unsafe_rate_plan["blockers"]
        )
        assert unsafe_rate_source.calls == []

        incomplete_campaign_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=IncompleteCampaignSource(empty_date=empty_date),
            now_factory=fixed_now,
        ).plan(fresh_scope)
        assert incomplete_campaign_plan["status"] == "blocked"
        assert any(
            item["code"] == "ads_upstream_incomplete"
            and "omitted requested campaigns" in item.get("detail", "")
            for item in incomplete_campaign_plan["blockers"]
        )

        recovered_omission_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=RecoverableBatchOmissionSource(empty_date=empty_date),
            now_factory=fixed_now,
        ).plan(fresh_scope)
        assert recovered_omission_plan["status"] == "ready", recovered_omission_plan[
            "blockers"
        ]
        assert any(
            request["mode"] == "singleton_confirmation"
            and request["outcome"] == "success"
            for request in recovered_omission_plan["source_manifest"]["requests"]
        )

        confirmed_no_stats_source = ConfirmedNoStatisticsSource(empty_date=empty_date)
        confirmed_no_stats_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=confirmed_no_stats_source,
            now_factory=fixed_now,
        ).plan(fresh_scope)
        assert confirmed_no_stats_plan["status"] == "ready", confirmed_no_stats_plan[
            "blockers"
        ]
        assert confirmed_no_stats_plan["target_manifest"][0]["payload_kind"] == "empty"
        assert any(
            request["outcome"] == "confirmed_no_statistics"
            and request["confirmation_signal"] == "official_no_statistics"
            for request in confirmed_no_stats_plan["source_manifest"]["requests"]
        )

        completed_source = CompletedBeforeScopeSource(empty_date=empty_date)
        completed_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=completed_source,
            now_factory=fixed_now,
        ).plan(fresh_scope)
        assert completed_plan["status"] == "ready", completed_plan["blockers"]
        assert all(401 not in call["campaign_ids"] for call in completed_source.calls)
        assert completed_plan["source_manifest"][
            "excluded_completed_before_scope_count"
        ] == 1

        unsupported_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=UnsupportedOverlapSource(empty_date=empty_date),
            now_factory=fixed_now,
        ).plan(fresh_scope)
        assert unsupported_plan["status"] == "blocked"
        assert any(
            item["code"] == "ads_unsupported_campaign_overlaps_scope"
            for item in unsupported_plan["blockers"]
        )

        mapping_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=MappingSingletonSource(empty_date=empty_date),
            now_factory=fixed_now,
        ).plan(fresh_scope)
        mapping_detail = next(
            item["detail"]
            for item in mapping_plan["blockers"]
            if item["code"] == "ads_upstream_incomplete"
        )
        assert '"keys"' in mapping_detail
        assert '"origin":"fixture-origin"' in mapping_detail
        assert '"detail":"fixture-detail"' in mapping_detail
        assert '"digest":"sha256:' in mapping_detail
        assert "must-not-be-copied" not in mapping_detail

        none_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=NoneSingletonSource(empty_date=empty_date),
            now_factory=fixed_now,
        ).plan(fresh_scope)
        none_detail = next(
            item["detail"]
            for item in none_plan["blockers"]
            if item["code"] == "ads_upstream_incomplete"
        )
        assert '"type":"NoneType"' in none_detail

        no_stats_body = json.dumps(
            {
                "detail": "there are no statistics for this advertising period",
                "origin": "camp-api-public-cache",
                "status": 400,
                "title": "invalid payload",
            }
        ).encode("utf-8")
        assert _is_confirmed_no_statistics_http_400(no_stats_body) is True
        no_stats_payload = json.loads(no_stats_body)
        assert _is_confirmed_no_statistics_payload(no_stats_payload) is True
        assert (
            _is_confirmed_no_statistics_http_400(
                no_stats_body.replace(b"no statistics", b"temporary failure")
            )
            is False
        )

        class _SuccessfulNoStatsResponse:
            status = 200
            headers = {"Content-Type": "application/json; charset=utf-8"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return no_stats_body

        with patch(
            "apps.ads_historical_recovery.urllib_request.urlopen",
            return_value=_SuccessfulNoStatsResponse(),
        ):
            try:
                OfficialAdsHistoricalSource(token="fixture-token")._get_json(
                    "https://example.invalid/adv/v3/fullstats"
                )
                raise AssertionError("HTTP-success no-statistics payload was not recognized")
            except AdsHistoricalNoStatisticsError:
                pass

        null_body = b"null"
        assert _is_confirmed_no_statistics_http_200_null(
            null_body,
            status=200,
            content_type="application/json; charset=utf-8",
        ) is True
        for wrong_status, wrong_content_type, wrong_body in (
            (204, "application/json", null_body),
            (200, "text/plain", null_body),
            (200, "application/json", b"{}"),
        ):
            assert _is_confirmed_no_statistics_http_200_null(
                wrong_body,
                status=wrong_status,
                content_type=wrong_content_type,
            ) is False

        class _SuccessfulNullResponse:
            status = 200
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return null_body

        with patch(
            "apps.ads_historical_recovery.urllib_request.urlopen",
            return_value=_SuccessfulNullResponse(),
        ):
            try:
                OfficialAdsHistoricalSource(
                    token="fixture-token",
                    min_request_interval_seconds=MIN_REQUEST_INTERVAL_SECONDS,
                ).fetch_fullstats(
                    campaign_ids=[501],
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                )
                raise AssertionError("HTTP 200 JSON null sentinel was not recognized")
            except AdsHistoricalNoStatisticsError as exc:
                assert exc.signal == "http_200_application_json_null"

        with patch(
            "apps.ads_historical_recovery.urllib_request.urlopen",
            return_value=_SuccessfulNullResponse(),
        ):
            assert OfficialAdsHistoricalSource(token="fixture-token")._get_json(
                "https://example.invalid/adv/v1/promotion/count"
            ) is None

        unclosed_source = FakeOfficialSource(empty_date=empty_date)
        unclosed_scope = AdsHistoricalRecoveryScope.build(
            nm_ids=[245720334], target_dates=[date(2026, 7, 22)]
        )
        unclosed_plan = AdsHistoricalRecovery(
            db_path=db_path,
            source=unclosed_source,
            now_factory=fixed_now,
        ).plan(unclosed_scope)
        assert unclosed_plan["status"] == "blocked"
        assert any(
            item["code"] == "ads_target_date_not_closed"
            for item in unclosed_plan["blockers"]
        )
        assert unclosed_source.calls == []

        absent_target_source = AdsHistoricalRecovery(
            db_path=db_path,
            source=FakeOfficialSource(
                empty_date=empty_date,
                omit_target_date=date(2026, 4, 2),
            ),
            now_factory=fixed_now,
        )
        absent_scope = AdsHistoricalRecoveryScope.build(
            nm_ids=[245720334], target_dates=[date(2026, 4, 2)]
        )
        absent_plan = absent_target_source.plan(absent_scope)
        assert absent_plan["status"] == "blocked"
        assert any(
            item["code"] == "ads_target_nm_absent_in_nonempty_response"
            for item in absent_plan["blockers"]
        )
        assert absent_plan["write_set"]["insert_snapshot_count"] == 0

        with sqlite3.connect(db_path) as conn:
            non_target_after = conn.execute(
                """SELECT payload_json FROM temporal_source_slot_snapshots
                   WHERE source_key='ads_compact' AND snapshot_date=?
                     AND snapshot_role='accepted_closed_day_snapshot'""",
                (non_target_date.isoformat(),),
            ).fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert json.loads(str(non_target_after))["items"][0]["ads_sum"] == "7.0000"
        assert integrity == "ok"

        print(
            json.dumps(
                {
                    "status": "ok",
                    "plan_fingerprint": plan["fingerprint"],
                    "inserted_snapshot_count": applied["inserted_snapshot_count"],
                    "backup_sha256": applied["backup"]["sha256"],
                    "snapshot_digest": applied["snapshot_digest"],
                    "mocked_only": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
