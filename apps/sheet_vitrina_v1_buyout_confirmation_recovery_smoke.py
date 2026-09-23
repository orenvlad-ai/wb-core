"""Targeted smoke for guarded mature-buyout historical reconcile."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_buyout_confirmation_recovery import (  # noqa: E402
    RECOVERY_DATE_FROM,
    RECOVERY_DATE_TO,
    BuyoutConfirmationRecoveryError,
    run_recovery,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    DB_FILENAME,
    RegistryUploadDbBackedRuntime,
    _SCHEMA_READY_KEYS,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (  # noqa: E402
    BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
    BUYOUT_PERCENT_METRIC_KEY,
)
from packages.contracts.sales_funnel_history_block import (  # noqa: E402
    SalesFunnelHistoryEnvelope,
    SalesFunnelHistoryItem,
    SalesFunnelHistorySuccess,
)


BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
DEPLOYED_SHA = "a" * 40
APPROVAL_REFERENCE = "github-pr:999#issuecomment-123"


class _AuthoritativeHistoryBlock:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple[int, ...]]] = []

    def execute(self, request: object) -> SalesFunnelHistoryEnvelope:
        date_from = str(getattr(request, "date_from"))
        date_to = str(getattr(request, "date_to"))
        nm_ids = tuple(int(value) for value in getattr(request, "nm_ids"))
        self.calls.append((date_from, date_to, nm_ids))
        items: list[SalesFunnelHistoryItem] = []
        current = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
        while current <= end:
            snapshot_date = current.isoformat()
            for nm_id in nm_ids:
                items.extend(
                    [
                        SalesFunnelHistoryItem(
                            date=snapshot_date,
                            nm_id=nm_id,
                            metric=BUYOUT_PERCENT_METRIC_KEY,
                            value=0.96,
                        ),
                        SalesFunnelHistoryItem(
                            date=snapshot_date,
                            nm_id=nm_id,
                            metric="orderCount",
                            value=10,
                        ),
                    ]
                )
            current += timedelta(days=1)
        return SalesFunnelHistoryEnvelope(
            result=SalesFunnelHistorySuccess(
                kind="success",
                date_from=date_from,
                date_to=date_to,
                count=len(items),
                items=items,
            )
        )


class _UnavailableHistoryBlock:
    def execute(self, request: object) -> SalesFunnelHistoryEnvelope:
        raise RuntimeError("official history depth rejected the oldest requested date")


def main() -> None:
    with TemporaryDirectory(prefix="buyout-mature-backfill-") as temp_dir:
        root = Path(temp_dir)
        runtime_dir = root / "runtime"
        evidence_dir = root / "evidence"
        deployed_sha_file = root / "app" / ".wb-core-runtime-sha"
        deployed_sha_file.parent.mkdir(parents=True)
        deployed_sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        deployed_sha_file.with_name(".wb-core-deploy.json").write_text(
            json.dumps({"commit": DEPLOYED_SHA, "deployment_complete": True}),
            encoding="utf-8",
        )
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-09-23T07:00:00Z")
        _assert(accepted.status == "accepted", "fixture registry must be accepted")
        nm_ids = sorted(
            int(item.nm_id)
            for item in runtime.load_current_state().config_v2
            if item.enabled
        )
        _seed_polluted_window(runtime, nm_ids)
        _seed_original_window(runtime, nm_ids)
        original_before = _original_window_digest(runtime)
        runtime.save_temporal_source_snapshot(
            source_key="unrelated_source",
            snapshot_date="2026-08-31",
            captured_at="2026-08-31T07:00:00Z",
            payload={"kind": "success", "value": "preserve-me"},
        )
        before_hash = _file_sha256(runtime_dir / DB_FILENAME)
        history = _AuthoritativeHistoryBlock()
        dry_run = run_recovery(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            date_from=RECOVERY_DATE_FROM,
            date_to=RECOVERY_DATE_TO,
            apply=False,
            history_block=history,  # type: ignore[arg-type]
            now=NOW,
        )
        _assert(dry_run["status"] == "ready", f"dry-run blocked: {dry_run}")
        _assert(not dry_run["database_written"], "dry-run must not report a DB write")
        _assert(_file_sha256(runtime_dir / DB_FILENAME) == before_hash, "dry-run changed DB bytes")
        _assert(
            history.calls == [(RECOVERY_DATE_FROM, RECOVERY_DATE_TO, tuple(nm_ids))],
            "dry-run must make one bounded authoritative request",
        )
        manifest_path = Path(dry_run["manifest_path"])
        _assert(manifest_path.is_file(), "machine-readable manifest missing")
        _assert(os.stat(manifest_path).st_mode & 0o077 == 0, "manifest must be private")

        try:
            run_recovery(
                runtime_dir=runtime_dir,
                evidence_dir=evidence_dir,
                date_from=RECOVERY_DATE_FROM,
                date_to=RECOVERY_DATE_TO,
                apply=True,
                manifest_path=manifest_path,
                expected_manifest_sha256="sha256:" + "0" * 64,
                expected_deployed_sha=DEPLOYED_SHA,
                deployed_sha_file=deployed_sha_file,
                approval_reference=APPROVAL_REFERENCE,
                now=NOW,
            )
        except BuyoutConfirmationRecoveryError as exc:
            _assert("SHA-256 mismatch" in str(exc), "wrong manifest hash must fail closed")
        else:
            raise AssertionError("wrong manifest hash unexpectedly applied")

        deployed_sha_file.with_name(".wb-core-deploy.json").write_text(
            json.dumps({"commit": DEPLOYED_SHA, "deployment_complete": False}),
            encoding="utf-8",
        )
        try:
            run_recovery(
                runtime_dir=runtime_dir, evidence_dir=evidence_dir,
                date_from=RECOVERY_DATE_FROM, date_to=RECOVERY_DATE_TO,
                apply=True, manifest_path=manifest_path,
                expected_manifest_sha256=str(dry_run["manifest_sha256"]),
                expected_deployed_sha=DEPLOYED_SHA, deployed_sha_file=deployed_sha_file,
                approval_reference=APPROVAL_REFERENCE, now=NOW,
            )
        except BuyoutConfirmationRecoveryError as exc:
            _assert("receipt" in str(exc), "incomplete deployment receipt must fail closed")
        else:
            raise AssertionError("incomplete deployment receipt unexpectedly applied")
        deployed_sha_file.with_name(".wb-core-deploy.json").write_text(
            json.dumps({"commit": DEPLOYED_SHA, "deployment_complete": True}),
            encoding="utf-8",
        )

        # Production apply runs in a new process.  Force the same first-call
        # schema path so the smoke catches an implicit schema transaction
        # colliding with the explicit bounded BEGIN IMMEDIATE.
        _SCHEMA_READY_KEYS.clear()
        applied = run_recovery(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            date_from=RECOVERY_DATE_FROM,
            date_to=RECOVERY_DATE_TO,
            apply=True,
            manifest_path=manifest_path,
            expected_manifest_sha256=str(dry_run["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=deployed_sha_file,
            approval_reference=APPROVAL_REFERENCE,
            now=NOW,
        )
        _assert(applied["status"] == "reconciled", f"apply failed: {applied}")
        _assert(applied["database_written"], "apply must report the bounded write")
        _assert(
            applied["deployed_sha"] == DEPLOYED_SHA
            and applied["approval_reference"] == APPROVAL_REFERENCE,
            "apply evidence must bind exact deployment and human gate",
        )
        _assert(applied["non_target_preserved"], "non-target digest changed")
        _assert(applied["rollback_overlay_rows"], "exact mutable overlay rows must be retained")
        _assert(Path(applied["reconciliation_path"]).is_file(), "reconciliation evidence missing")
        payload, captured_at = runtime.load_temporal_source_snapshot(
            source_key=BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
            snapshot_date=RECOVERY_DATE_FROM,
        )
        values = {
            (int(item.nm_id), str(item.metric)): float(item.value)
            for item in payload.items
        }
        _assert(captured_at == "2026-09-23T08:00:00Z", "mature provenance timestamp mismatch")
        _assert(
            values[(nm_ids[0], BUYOUT_PERCENT_METRIC_KEY)] == 0.96,
            "polluted mature value was not replaced by official payload",
        )
        _assert(
            _original_window_digest(runtime) == original_before,
            "recovery must retain the original broad sales-funnel snapshots",
        )
        unrelated, unrelated_at = runtime.load_temporal_source_snapshot(
            source_key="unrelated_source",
            snapshot_date="2026-08-31",
        )
        _assert(
            getattr(unrelated, "value", "") == "preserve-me"
            and unrelated_at == "2026-08-31T07:00:00Z",
            "non-target row changed",
        )
        repeated = run_recovery(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            date_from=RECOVERY_DATE_FROM,
            date_to=RECOVERY_DATE_TO,
            apply=True,
            manifest_path=manifest_path,
            expected_manifest_sha256=str(dry_run["manifest_sha256"]),
            expected_deployed_sha=DEPLOYED_SHA,
            deployed_sha_file=deployed_sha_file,
            approval_reference=APPROVAL_REFERENCE,
            now=NOW,
        )
        _assert(repeated["status"] == "already_applied", "repeat apply must be idempotent")
        _assert(not repeated["database_written"], "idempotent repeat must not write")

        try:
            run_recovery(
                runtime_dir=runtime_dir,
                evidence_dir=evidence_dir,
                date_from="2026-07-21",
                date_to=RECOVERY_DATE_TO,
                apply=False,
                history_block=history,  # type: ignore[arg-type]
                now=NOW,
            )
        except BuyoutConfirmationRecoveryError as exc:
            _assert("bounded to" in str(exc), "out-of-scope window must fail closed")
        else:
            raise AssertionError("out-of-scope backfill window unexpectedly accepted")

        blocked = run_recovery(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir / "blocked",
            date_from=RECOVERY_DATE_FROM,
            date_to=RECOVERY_DATE_TO,
            apply=False,
            history_block=_UnavailableHistoryBlock(),  # type: ignore[arg-type]
            now=NOW,
        )
        _assert(blocked["status"] == "blocked", "upstream depth failure must block dry-run")
        _assert(
            blocked["source_errors"]
            and Path(blocked["manifest_path"]).is_file()
            and not blocked["database_written"],
            "upstream blocker must remain machine-readable without DB mutation",
        )
        _assert_apply_cas_blockers(root)
        _assert_immature_desired_payload_is_refreshed(root)

        print("buyout_confirmation_recovery_dry_run: ok ->", dry_run["manifest_sha256"])
        print("buyout_confirmation_recovery_apply: ok ->", applied["evidence_sha256"])
        print("buyout_confirmation_recovery_retention_idempotency: ok")
        print("buyout_confirmation_recovery_tamper_roster_cas: ok")
        print("buyout_confirmation_recovery_immature_desired_refresh: ok")
        print("buyout_confirmation_recovery_upstream_blocker: ok")


def _assert_immature_desired_payload_is_refreshed(root: Path) -> None:
    runtime_dir = root / "immature-desired" / "runtime"
    evidence_dir = root / "immature-desired" / "evidence"
    sha_file = root / "immature-desired" / "app" / ".wb-core-runtime-sha"
    sha_file.parent.mkdir(parents=True)
    sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
    sha_file.with_name(".wb-core-deploy.json").write_text(
        json.dumps({"commit": DEPLOYED_SHA, "deployment_complete": True}), encoding="utf-8"
    )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    fixture = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    _assert(runtime.ingest_bundle(fixture, activated_at="2026-09-23T07:00:00Z").status == "accepted", "immature fixture")
    nm_ids = [int(item.nm_id) for item in runtime.load_current_state().config_v2 if item.enabled]
    current = date.fromisoformat(RECOVERY_DATE_FROM)
    while current <= date.fromisoformat(RECOVERY_DATE_TO):
        day = current.isoformat()
        runtime.save_temporal_source_snapshot(
            source_key=BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, snapshot_date=day,
            captured_at="2026-09-18T08:00:00Z",
            payload={"kind": "success", "date_from": day, "date_to": day, "count": len(nm_ids) * 2,
                     "items": [item for nm_id in nm_ids for item in (
                         {"date": day, "nm_id": nm_id, "metric": BUYOUT_PERCENT_METRIC_KEY, "value": 0.96},
                         {"date": day, "nm_id": nm_id, "metric": "orderCount", "value": 10},
                     )]},
        )
        current += timedelta(days=1)
    plan = run_recovery(runtime_dir=runtime_dir, evidence_dir=evidence_dir, date_from=RECOVERY_DATE_FROM, date_to=RECOVERY_DATE_TO, apply=False, history_block=_AuthoritativeHistoryBlock(), now=NOW)
    applied = run_recovery(runtime_dir=runtime_dir, evidence_dir=evidence_dir, date_from=RECOVERY_DATE_FROM, date_to=RECOVERY_DATE_TO, apply=True, manifest_path=Path(plan["manifest_path"]), expected_manifest_sha256=str(plan["manifest_sha256"]), expected_deployed_sha=DEPLOYED_SHA, deployed_sha_file=sha_file, approval_reference=APPROVAL_REFERENCE, now=NOW)
    _assert(applied["status"] == "reconciled", "immature desired content must refresh provenance")
    _payload, captured_at = runtime.load_temporal_source_snapshot(source_key=BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, snapshot_date=RECOVERY_DATE_TO)
    _assert(captured_at == "2026-09-23T08:00:00Z", "mature overlay provenance was not refreshed")
    repeated = run_recovery(runtime_dir=runtime_dir, evidence_dir=evidence_dir, date_from=RECOVERY_DATE_FROM, date_to=RECOVERY_DATE_TO, apply=True, manifest_path=Path(plan["manifest_path"]), expected_manifest_sha256=str(plan["manifest_sha256"]), expected_deployed_sha=DEPLOYED_SHA, deployed_sha_file=sha_file, approval_reference=APPROVAL_REFERENCE, now=NOW)
    _assert(repeated["status"] == "already_applied", "mature desired content must then no-op")


def _assert_apply_cas_blockers(root: Path) -> None:
    """Apply must reject an altered overlay or a changed confirmation roster."""

    for name, mutate, expected in (
        ("tampered", _tamper_overlay, "target window changed"),
        ("roster", _drift_roster, "enabled SKU target set changed"),
    ):
        runtime_dir = root / name / "runtime"
        evidence_dir = root / name / "evidence"
        sha_file = root / name / "app" / ".wb-core-runtime-sha"
        sha_file.parent.mkdir(parents=True)
        sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        sha_file.with_name(".wb-core-deploy.json").write_text(
            json.dumps({"commit": DEPLOYED_SHA, "deployment_complete": True}),
            encoding="utf-8",
        )
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        fixture = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        _assert(
            runtime.ingest_bundle(fixture, activated_at="2026-09-23T07:00:00Z").status == "accepted",
            "CAS fixture registry must be accepted",
        )
        nm_ids = [int(item.nm_id) for item in runtime.load_current_state().config_v2 if item.enabled]
        _seed_polluted_window(runtime, nm_ids)
        dry_run = run_recovery(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            date_from=RECOVERY_DATE_FROM,
            date_to=RECOVERY_DATE_TO,
            apply=False,
            history_block=_AuthoritativeHistoryBlock(),  # type: ignore[arg-type]
            now=NOW,
        )
        _assert(dry_run["status"] == "ready", f"{name} preflight must be ready")
        mutate(runtime, fixture)
        try:
            run_recovery(
                runtime_dir=runtime_dir,
                evidence_dir=evidence_dir,
                date_from=RECOVERY_DATE_FROM,
                date_to=RECOVERY_DATE_TO,
                apply=True,
                manifest_path=Path(dry_run["manifest_path"]),
                expected_manifest_sha256=str(dry_run["manifest_sha256"]),
                expected_deployed_sha=DEPLOYED_SHA,
                deployed_sha_file=sha_file,
                approval_reference=APPROVAL_REFERENCE,
                now=NOW,
            )
        except BuyoutConfirmationRecoveryError as exc:
            _assert(expected in str(exc), f"{name} CAS reason mismatch: {exc}")
        else:
            raise AssertionError(f"{name} CAS unexpectedly allowed apply")


def _tamper_overlay(runtime: RegistryUploadDbBackedRuntime, _fixture: object) -> None:
    with __import__("sqlite3").connect(runtime.db_path) as conn:
        raw = conn.execute(
            """SELECT payload_json FROM temporal_source_snapshots
               WHERE source_key=? AND snapshot_date=?""",
            (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, RECOVERY_DATE_FROM),
        ).fetchone()[0]
        conn.execute(
            """UPDATE temporal_source_snapshots SET payload_json=?
               WHERE source_key=? AND snapshot_date=?""",
            (
                json.dumps(json.loads(raw), ensure_ascii=False, indent=2, sort_keys=True),
                BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
                RECOVERY_DATE_FROM,
            ),
        )


def _drift_roster(runtime: RegistryUploadDbBackedRuntime, fixture: object) -> None:
    updated = json.loads(json.dumps(fixture))
    updated["bundle_version"] = "registry_upload_bundle_v1__2026-09-23T08:30:00Z"
    updated["uploaded_at"] = "2026-09-23T08:30:00Z"
    updated["config_v2"][0]["enabled"] = False
    _assert(
        runtime.ingest_bundle(updated, activated_at="2026-09-23T08:30:00Z").status == "accepted",
        "roster-drift fixture must be accepted",
    )


def _seed_polluted_window(
    runtime: RegistryUploadDbBackedRuntime,
    nm_ids: list[int],
) -> None:
    current = date.fromisoformat(RECOVERY_DATE_FROM)
    end = date.fromisoformat(RECOVERY_DATE_TO)
    while current <= end:
        snapshot_date = current.isoformat()
        items = [
            item
            for nm_id in nm_ids
            for item in (
                {
                    "date": snapshot_date,
                    "nm_id": nm_id,
                    "metric": BUYOUT_PERCENT_METRIC_KEY,
                    "value": 0.2,
                },
                {
                    "date": snapshot_date,
                    "nm_id": nm_id,
                    "metric": "orderCount",
                    "value": 10,
                },
            )
        ]
        runtime.save_temporal_source_snapshot(
            source_key=BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
            snapshot_date=snapshot_date,
            captured_at=f"{snapshot_date}T07:00:00Z",
            payload={
                "kind": "success",
                "date_from": snapshot_date,
                "date_to": snapshot_date,
                "count": len(items),
                "items": items,
            },
        )
        current += timedelta(days=1)


def _seed_original_window(
    runtime: RegistryUploadDbBackedRuntime,
    nm_ids: list[int],
) -> None:
    current = date.fromisoformat(RECOVERY_DATE_FROM)
    end = date.fromisoformat(RECOVERY_DATE_TO)
    while current <= end:
        snapshot_date = current.isoformat()
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=snapshot_date,
            captured_at=f"{snapshot_date}T07:00:00Z",
            payload={
                "kind": "success",
                "date_from": snapshot_date,
                "date_to": snapshot_date,
                "count": len(nm_ids) * 3,
                "items": [
                    item
                    for nm_id in nm_ids
                    for item in (
                        {"date": snapshot_date, "nm_id": nm_id, "metric": BUYOUT_PERCENT_METRIC_KEY, "value": 0.2},
                        {"date": snapshot_date, "nm_id": nm_id, "metric": "orderCount", "value": 10},
                        {"date": snapshot_date, "nm_id": nm_id, "metric": "ordersSumRub", "value": 1000},
                    )
                ],
            },
        )
        current += timedelta(days=1)


def _original_window_digest(runtime: RegistryUploadDbBackedRuntime) -> str:
    rows = []
    with __import__("sqlite3").connect(runtime.db_path) as conn:
        for row in conn.execute(
            """SELECT snapshot_date,captured_at,payload_json FROM temporal_source_snapshots
               WHERE source_key='sales_funnel_history' AND snapshot_date>=? AND snapshot_date<=?
               ORDER BY snapshot_date""",
            (RECOVERY_DATE_FROM, RECOVERY_DATE_TO),
        ):
            rows.append(list(row))
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
