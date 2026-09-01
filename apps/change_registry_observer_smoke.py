"""Deterministic acceptance smoke for the live Change Registry observer."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shlex
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
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
    OBSERVER_LEASES_TABLE,
    canonical_digest,
    ensure_change_registry_schema,
)
from packages.application.change_registry_observer import (  # noqa: E402
    ChangeRegistryObserver,
    ChangeRegistryObserverBusy,
    ChangeRegistryObserverError,
    ChangeRegistryReadSurface,
    PERSISTENCE_STAGE_BINDINGS,
    activation_job_id,
)
from packages.application.change_registry_source_acquisition import (  # noqa: E402
    ChangeRegistrySourceAcquirer,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)
from apps.change_registry_source_acquisition_smoke import (  # noqa: E402
    FakeAdsSource,
    FakePricesSource,
    _count_payload,
    _detail,
)
from apps.change_registry_observer import observer_job_exit_code  # noqa: E402


SELLER = "seller-primary"
ACCOUNT = "seller-portal-primary"


def _unit_environment(path: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("Environment="):
            tokens = shlex.split(line.split("=", 1)[1])
        elif line.startswith("ExecStart="):
            tokens = shlex.split(line.split("=", 1)[1])
        else:
            continue
        for token in tokens:
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key in {"CHANGE_REGISTRY_OBSERVER_ENABLED", "CHANGE_REGISTRY_ACCOUNT_SCOPE"}:
                if key in environment and environment[key] != value:
                    raise AssertionError(f"conflicting {key} values in {path}")
                environment[key] = value
    return environment


def _assert_canonical_hosted_activation_wiring() -> None:
    target_path = (
        ROOT
        / "artifacts"
        / "registry_upload_http_entrypoint"
        / "input"
        / "hosted_runtime_target__europe_api.json"
    )
    target = json.loads(target_path.read_text(encoding="utf-8"))
    runtime_env = target["runtime_env"]
    expected = {
        "CHANGE_REGISTRY_OBSERVER_ENABLED": runtime_env["CHANGE_REGISTRY_OBSERVER_ENABLED"],
        "CHANGE_REGISTRY_ACCOUNT_SCOPE": runtime_env["CHANGE_REGISTRY_ACCOUNT_SCOPE"],
    }
    assert expected == {
        "CHANGE_REGISTRY_OBSERVER_ENABLED": "true",
        "CHANGE_REGISTRY_ACCOUNT_SCOPE": ACCOUNT,
    }

    units_dir = ROOT / target["systemd_units_source_dir"]
    observer_unit = units_dir / "wb-core-change-registry-observer.service"
    activation_unit = units_dir / "wb-core-change-registry-activation@.service"
    http_unit = units_dir / "wb-core-registry-http.service"
    assert _unit_environment(observer_unit) == expected
    assert _unit_environment(activation_unit) == expected
    assert _unit_environment(http_unit) == expected

    environment_file = target["environment_file"]
    observer_text = observer_unit.read_text(encoding="utf-8")
    activation_text = activation_unit.read_text(encoding="utf-8")
    http_text = http_unit.read_text(encoding="utf-8")
    assert f"--env-file {environment_file}" in observer_text
    assert f"--env-file {environment_file}" in activation_text
    assert "--trigger activation --deployed-sha %i" in activation_text
    assert f"EnvironmentFile={environment_file}" in http_text

    managed_units = {item["name"]: item for item in target["managed_systemd_units"]}
    assert managed_units[observer_unit.name] == {
        "name": observer_unit.name,
        "enable": False,
        "restart": False,
    }
    assert managed_units["wb-core-change-registry-observer.timer"] == {
        "name": "wb-core-change-registry-observer.timer",
        "enable": True,
        "restart": True,
    }
    assert managed_units[activation_unit.name] == {
        "name": activation_unit.name,
        "enable": False,
        "restart": False,
    }


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
    bid: int = 0,
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
                "bid_minor": _exact_integer(bid),
                "target_digest": canonical_digest({"bid": bid, "mapping": mapping}),
            }
        ],
        "record_digest": canonical_digest({"campaign": 201, "mapping": mapping, "bid": bid}),
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
        "wb_mutation_calls": {"post": 0, "patch": 0},
    }
    payload["manifest_digest"] = canonical_digest(payload)
    return payload


class SnapshotAcquirer:
    def __init__(self, snapshot: Mapping[str, Any]) -> None:
        self.snapshot = deepcopy(snapshot)
        self.acquire_calls = 0
        self.upload_task_calls = 0
        self.patch_bids_calls = 0
        self.balance_wb_patch_called = False

    def acquire(self) -> dict[str, Any]:
        self.acquire_calls += 1
        return deepcopy(self.snapshot)


class LockProbingAcquirer(SnapshotAcquirer):
    def __init__(self, runtime_dir: Path, snapshot: Mapping[str, Any]) -> None:
        super().__init__(snapshot)
        self.runtime_dir = runtime_dir
        self.lock_evidence: dict[str, Any] | None = None

    def acquire(self) -> dict[str, Any]:
        with warehouse_functional_write_lock(
            self.runtime_dir,
            blocking=False,
        ) as evidence:
            self.lock_evidence = dict(evidence)
        return super().acquire()


def _observer(
    runtime_dir: Path,
    snapshot: Mapping[str, Any],
    now: str,
    *,
    persistence_stage_hook: Any = None,
    fallback_stage_hook: Any = None,
) -> tuple[ChangeRegistryObserver, SnapshotAcquirer]:
    acquirer = SnapshotAcquirer(snapshot)
    return (
        ChangeRegistryObserver(
            runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: acquirer,
            now_fn=lambda: now,
            persistence_stage_hook=persistence_stage_hook,
            fallback_stage_hook=fallback_stage_hook,
        ),
        acquirer,
    )


def _sqlite_integrity_error(*, malicious: bool = False) -> sqlite3.IntegrityError:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE stage_failure(value TEXT UNIQUE)")
        conn.execute("INSERT INTO stage_failure(value) VALUES('owned')")
        try:
            conn.execute("INSERT INTO stage_failure(value) VALUES('owned')")
        except sqlite3.IntegrityError as exc:
            if malicious:
                exc.args = (
                    "UNIQUE constraint failed: stage_failure.value; "
                    "SELECT token FROM /private/runtime/secret " + "x" * 5000,
                )
            return exc
    finally:
        conn.close()
    raise AssertionError("failed to construct SQLite IntegrityError")


class StageFailure:
    def __init__(self, stage: str, *, malicious: bool = False) -> None:
        self.stage = stage
        self.malicious = malicious
        self.calls = 0

    def __call__(self, stage: str, _conn: sqlite3.Connection | None) -> None:
        if stage == self.stage:
            self.calls += 1
            raise _sqlite_integrity_error(malicious=self.malicious)


class FailingSource:
    def __init__(self) -> None:
        self.upload_task_calls = 0
        self.patch_bids_calls = 0
        self.balance_wb_patch_called = False

    def acquire(self) -> dict[str, Any]:
        raise RuntimeError(
            "Authorization=secret-token /private/runtime/source SELECT * "
            + "payload" * 1000
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


def _terminal_event(db_path: Path, job_id: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT * FROM {OBSERVER_JOB_EVENTS_TABLE} "
            "WHERE job_id=? ORDER BY sequence_no DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    if row is None:
        raise AssertionError("terminal observer event is missing")
    return dict(row)


def _assert_released_lease(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        lease = conn.execute(
            f"SELECT * FROM {OBSERVER_LEASES_TABLE} LIMIT 1"
        ).fetchone()
    assert lease is not None
    assert dict(lease)["owner_job_id"] == ""
    assert int(dict(lease)["revision"]) == 2


def _assert_legacy_event_schema_upgrade(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""CREATE TABLE {OBSERVER_JOB_EVENTS_TABLE}(
                job_event_id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                state TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                checkpoint_id TEXT,
                fact_count INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '',
                evidence_digest TEXT NOT NULL,
                UNIQUE(job_id,sequence_no)
            )"""
        )
        ensure_change_registry_schema(conn)
        conn.commit()
        columns = {
            str(row[1])
            for row in conn.execute(
                f"PRAGMA table_info({OBSERVER_JOB_EVENTS_TABLE})"
            ).fetchall()
        }
    assert {
        "source_status",
        "failure_origin",
        "persistence_stage",
        "persistence_table",
        "persistence_operation",
        "sqlite_errorcode",
        "sqlite_errorname",
        "constraint_category",
        "constraint_name",
        "error_digest",
        "fallback_persistence_stage",
        "fallback_error_digest",
    } <= columns


def main() -> None:
    _assert_canonical_hosted_activation_wiring()
    with TemporaryDirectory(prefix="change-registry-observer-") as tmp:
        missing_runtime = Path(tmp) / "missing-runtime"
        missing_runtime.mkdir()
        before_missing = list(missing_runtime.iterdir())
        missing = ChangeRegistryReadSurface(
            missing_runtime, seller_id=SELLER, account_scope=ACCOUNT
        ).overview()
        assert missing["status"]["health_state"] == "schema_missing"
        assert missing["storage"] == {"mode": "ro", "query_only": True}
        assert list(missing_runtime.iterdir()) == before_missing
        incomplete_runtime = Path(tmp) / "incomplete-runtime"
        incomplete_runtime.mkdir()
        incomplete_db = incomplete_runtime / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(incomplete_db) as conn:
            conn.execute("CREATE TABLE unrelated(id INTEGER PRIMARY KEY)")
            conn.commit()
        incomplete_before = incomplete_db.stat()
        incomplete = ChangeRegistryReadSurface(
            incomplete_runtime, seller_id=SELLER, account_scope=ACCOUNT
        ).overview()
        incomplete_after = incomplete_db.stat()
        assert incomplete["status"]["health_state"] == "schema_missing"
        assert incomplete["status"]["missing_tables"]
        assert (incomplete_before.st_size, incomplete_before.st_mtime_ns) == (
            incomplete_after.st_size,
            incomplete_after.st_mtime_ns,
        )
        _assert_legacy_event_schema_upgrade(Path(tmp) / "legacy-observer.sqlite3")
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
        baseline_sha = "a" * 40
        baseline = observer.run(
            trigger_kind="activation",
            requested_by="release-runner",
            deployed_sha=baseline_sha,
        )
        assert baseline["events"][-1]["state"] == "complete"
        assert baseline["job"]["job_id"] == activation_job_id(baseline_sha)
        assert observer_job_exit_code(baseline) == 0
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
        try:
            observer.run(
                trigger_kind="manual",
                requested_by="different-actor",
                job_id="changed",
            )
        except ChangeRegistryObserverError:
            pass
        else:
            raise AssertionError("conflicting job-id actor binding did not fail closed")
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
        assert observer_job_exit_code(partial) == 1

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
        holder_failure = {
            "error_code": "RuntimeError",
            "error_message": "Local persistence failed: RuntimeError; category=local_persistence.",
            "source_status": "invalid",
            "failure_origin": "local_persistence",
            "persistence_stage": "lease_test_cleanup",
            "persistence_table": OBSERVER_LEASES_TABLE,
            "persistence_operation": "release",
            "sqlite_errorcode": None,
            "sqlite_errorname": "",
            "constraint_category": "local_persistence",
            "constraint_name": "",
        }
        holder_failure["error_digest"] = canonical_digest(holder_failure)
        holder._fail_job("lease-holder", "manual", "", holder_failure)

        crash_digest = canonical_digest(
            {
                "seller_id": SELLER,
                "account_scope": ACCOUNT,
                "trigger_kind": "manual",
                "scheduled_slot": "",
                "requested_by": "operator",
                "client_job_id": "accepted-crash",
                "deployed_sha": "",
            }
        )
        crashed, _ = _observer(
            runtime_dir, _snapshot(44), "2026-08-29T00:44:10Z"
        )
        crashed.lease_seconds = 1
        crashed._admit(
            job_id="accepted-crash",
            trigger_kind="manual",
            scheduled_slot_value="",
            requested_by="operator",
            requested_at="2026-08-29T00:44:10Z",
            request_digest=crash_digest,
        )
        assert observer_job_exit_code(crashed.read_job("accepted-crash")) == 1
        recovered, recovered_adapter = _observer(
            runtime_dir, _snapshot(44), "2026-08-29T00:44:12Z"
        )
        recovered_job = recovered.run(
            trigger_kind="manual", requested_by="operator", job_id="accepted-crash"
        )
        assert recovered_job["events"][-1]["state"] == "complete"
        assert [event["state"] for event in recovered_job["events"]].count("running") == 2
        assert recovered_adapter.upload_task_calls == 0

        class FailingAcquirer:
            calls = 0

            def acquire(self):
                self.calls += 1
                raise RuntimeError("expected source failure")

        failing = FailingAcquirer()
        failed_observer = ChangeRegistryObserver(
            runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: failing,
            now_fn=lambda: "2026-08-29T00:44:40Z",
        )
        try:
            failed_observer.run(
                trigger_kind="manual", requested_by="operator", job_id="failed-replay"
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("source failure was not surfaced")
        replay_failed, replay_adapter = _observer(
            runtime_dir, _snapshot(44), "2026-08-29T00:44:41Z"
        )
        failed_job = replay_failed.run(
            trigger_kind="manual", requested_by="operator", job_id="failed-replay"
        )
        assert failed_job["events"][-1]["state"] == "failed"
        assert observer_job_exit_code(failed_job) == 1
        assert replay_adapter.acquire_calls == 0

        before_failure = _counts(db_path)
        atomic_before_failure = _atomic_result_counts(db_path)
        observer, _ = _observer(runtime_dir, _snapshot(45, price=3333), "2026-08-29T00:45:00Z")
        try:
            observer.run(
                trigger_kind="manual",
                requested_by="operator",
                job_id="db-rollback",
                inject_db_failure=True,
            )
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("injected DB failure was not surfaced")
        assert _counts(db_path) == before_failure
        assert _atomic_result_counts(db_path) == atomic_before_failure

        expected_stage_order = (
            "baseline_ingest",
            "baseline_result",
            "source_manifest_prices",
            "source_manifest_ads",
            "terminal_job_event",
            "scheduled_health",
            "lease_release",
            "transaction_commit",
        )
        assert tuple(PERSISTENCE_STAGE_BINDINGS) == expected_stage_order
        for index, stage in enumerate(expected_stage_order):
            stage_runtime = Path(tmp) / f"failure-{index}-{stage}"
            stage_runtime.mkdir(parents=True)
            stage_db = stage_runtime / "registry_upload_runtime.sqlite3"
            failure = StageFailure(stage)
            stage_observer, stage_adapter = _observer(
                stage_runtime,
                _snapshot(45, price=3333),
                "2026-08-29T00:45:00Z",
                persistence_stage_hook=failure,
            )
            job_id = f"stage-failure-{index}"
            try:
                stage_observer.run(
                    trigger_kind="scheduled",
                    requested_by="systemd",
                    scheduled_slot_value="2026-08-29T00:00:00Z",
                    job_id=job_id,
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError(f"{stage} IntegrityError was not surfaced")
            assert failure.calls == 1
            assert _atomic_result_counts(stage_db) == {
                CHECKPOINTS_TABLE: 0,
                CHECKPOINT_SOURCE_MANIFESTS_TABLE: 0,
                OBSERVATION_VALUES_TABLE: 0,
                IDENTITY_INCIDENTS_TABLE: 0,
                FACTS_TABLE: 0,
                FACT_LINKS_TABLE: 0,
            }
            event = _terminal_event(stage_db, job_id)
            expected_table, expected_operation = PERSISTENCE_STAGE_BINDINGS[stage]
            assert event["state"] == "failed"
            assert event["source_status"] == "complete"
            assert event["failure_origin"] == "local_persistence"
            assert event["persistence_stage"] == stage
            assert event["persistence_table"] == expected_table
            assert event["persistence_operation"] == expected_operation
            assert event["sqlite_errorcode"] == sqlite3.SQLITE_CONSTRAINT_UNIQUE
            assert event["sqlite_errorname"] == "SQLITE_CONSTRAINT_UNIQUE"
            assert event["constraint_category"] == "unique"
            assert event["constraint_name"] == "stage_failure.value"
            assert event["error_message"] == (
                "SQLite failure: SQLITE_CONSTRAINT_UNIQUE; category=unique; "
                "constraint=stage_failure.value."
            )
            assert str(event["error_digest"]).startswith("sha256:")
            assert not event["fallback_error_code"]
            _assert_released_lease(stage_db)
            with sqlite3.connect(stage_db) as conn:
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {OBSERVER_HEALTH_EVENTS_TABLE}"
                ).fetchone()[0] == 1
                before_replay_events = conn.execute(
                    f"SELECT COUNT(*) FROM {OBSERVER_JOB_EVENTS_TABLE}"
                ).fetchone()[0]
            replay = stage_observer.run(
                trigger_kind="scheduled",
                requested_by="systemd",
                scheduled_slot_value="2026-08-29T00:00:00Z",
                job_id=job_id,
            )
            assert replay["events"][-1]["persistence_stage"] == stage
            assert failure.calls == 1
            with sqlite3.connect(stage_db) as conn:
                assert conn.execute(
                    f"SELECT COUNT(*) FROM {OBSERVER_JOB_EVENTS_TABLE}"
                ).fetchone()[0] == before_replay_events
            assert stage_adapter.upload_task_calls == 0
            assert stage_adapter.patch_bids_calls == 0
            assert stage_adapter.balance_wb_patch_called is False

        source_runtime = Path(tmp) / "source-failure-runtime"
        source_runtime.mkdir(parents=True)
        source_failure = FailingSource()
        source_observer = ChangeRegistryObserver(
            source_runtime,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: source_failure,
            now_fn=lambda: "2026-08-29T01:00:00Z",
        )
        try:
            source_observer.run(
                trigger_kind="scheduled",
                requested_by="systemd",
                scheduled_slot_value="2026-08-29T00:00:00Z",
                job_id="source-acquisition-failure",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("source acquisition failure was not surfaced")
        source_event = _terminal_event(
            source_runtime / "registry_upload_runtime.sqlite3",
            "source-acquisition-failure",
        )
        assert source_event["source_status"] == "failed"
        assert source_event["failure_origin"] == "source_acquisition"
        assert source_event["persistence_stage"] == ""
        assert source_event["persistence_table"] == ""
        assert source_event["persistence_operation"] == ""
        assert source_event["sqlite_errorcode"] is None
        assert source_event["sqlite_errorname"] == ""
        assert source_event["constraint_category"] == "source_acquisition"
        assert source_event["error_message"] == "Source acquisition failed: RuntimeError."
        assert len(source_event["error_message"]) <= 400
        assert "secret-token" not in source_event["error_message"]
        assert "/private" not in source_event["error_message"]
        assert "SELECT" not in source_event["error_message"]
        _assert_released_lease(
            source_runtime / "registry_upload_runtime.sqlite3"
        )
        assert source_failure.upload_task_calls == 0
        assert source_failure.patch_bids_calls == 0
        assert source_failure.balance_wb_patch_called is False

        sanitized_runtime = Path(tmp) / "sanitized-failure-runtime"
        sanitized_runtime.mkdir(parents=True)
        malicious = StageFailure("terminal_job_event", malicious=True)
        sanitized_observer, _ = _observer(
            sanitized_runtime,
            _snapshot(46, price=3334),
            "2026-08-29T00:46:00Z",
            persistence_stage_hook=malicious,
        )
        try:
            sanitized_observer.run(
                trigger_kind="manual",
                requested_by="operator",
                job_id="sanitized-integrity-failure",
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("malicious IntegrityError was not surfaced")
        sanitized_event = _terminal_event(
            sanitized_runtime / "registry_upload_runtime.sqlite3",
            "sanitized-integrity-failure",
        )
        assert sanitized_event["constraint_name"] == ""
        assert len(sanitized_event["error_message"]) <= 400
        assert "secret-token" not in sanitized_event["error_message"]
        assert "/private" not in sanitized_event["error_message"]
        assert "SELECT" not in sanitized_event["error_message"]

        fallback_runtime = Path(tmp) / "fallback-failure-runtime"
        fallback_runtime.mkdir(parents=True)
        primary_failure = StageFailure("baseline_result")
        fallback_failure = StageFailure(
            "fallback_scheduled_health", malicious=True
        )
        fallback_observer, _ = _observer(
            fallback_runtime,
            _snapshot(47, price=3335),
            "2026-08-29T00:47:00Z",
            persistence_stage_hook=primary_failure,
            fallback_stage_hook=fallback_failure,
        )
        try:
            fallback_observer.run(
                trigger_kind="scheduled",
                requested_by="systemd",
                scheduled_slot_value="2026-08-29T00:00:00Z",
                job_id="fallback-write-failure",
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("primary failure with fallback recovery was not surfaced")
        fallback_event = _terminal_event(
            fallback_runtime / "registry_upload_runtime.sqlite3",
            "fallback-write-failure",
        )
        assert fallback_event["persistence_stage"] == "baseline_result"
        assert fallback_event["error_code"] == "IntegrityError"
        assert fallback_event["fallback_persistence_stage"] == "fallback_scheduled_health"
        assert fallback_event["fallback_persistence_table"] == OBSERVER_HEALTH_EVENTS_TABLE
        assert fallback_event["fallback_persistence_operation"] == "insert_failed_health"
        assert fallback_event["fallback_error_code"] == "IntegrityError"
        assert fallback_event["fallback_sqlite_errorname"] == "SQLITE_CONSTRAINT_UNIQUE"
        assert fallback_event["fallback_constraint_category"] == "unique"
        assert fallback_event["fallback_constraint_name"] == ""
        assert str(fallback_event["fallback_error_digest"]).startswith("sha256:")
        assert "secret-token" not in fallback_event["fallback_error_message"]
        assert "/private" not in fallback_event["fallback_error_message"]
        assert "SELECT" not in fallback_event["fallback_error_message"]
        assert _atomic_result_counts(
            fallback_runtime / "registry_upload_runtime.sqlite3"
        ) == {
            CHECKPOINTS_TABLE: 0,
            CHECKPOINT_SOURCE_MANIFESTS_TABLE: 0,
            OBSERVATION_VALUES_TABLE: 0,
            IDENTITY_INCIDENTS_TABLE: 0,
            FACTS_TABLE: 0,
            FACT_LINKS_TABLE: 0,
        }
        _assert_released_lease(
            fallback_runtime / "registry_upload_runtime.sqlite3"
        )

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
        assert overview["storage"] == {"mode": "ro", "query_only": True}

        scheduled_observer, _ = _observer(
            runtime_dir, _snapshot(12 * 60), "2026-08-29T12:00:00Z"
        )
        scheduled_job = scheduled_observer.run(
            trigger_kind="scheduled",
            requested_by="systemd",
            scheduled_slot_value="2026-08-29T12:00:00Z",
        )
        activation_sha = "b" * 40
        activation_observer, _ = _observer(
            runtime_dir, _snapshot(12 * 60), "2026-08-29T12:00:31+00:00"
        )
        activation_job = activation_observer.run(
            trigger_kind="activation",
            requested_by="trusted-release-runner",
            deployed_sha=activation_sha,
        )
        assert scheduled_job["job"]["job_id"] != activation_job["job"]["job_id"]
        assert activation_job["job"]["job_id"] == activation_job_id(activation_sha)
        activation_event_count = len(activation_job["events"])
        same_sha = activation_observer.run(
            trigger_kind="activation",
            requested_by="trusted-release-runner",
            deployed_sha=activation_sha,
        )
        assert len(same_sha["events"]) == activation_event_count
        new_sha = activation_observer.run(
            trigger_kind="activation",
            requested_by="trusted-release-runner",
            deployed_sha="c" * 40,
        )
        assert new_sha["job"]["job_id"] != same_sha["job"]["job_id"]

        async_observer, _ = _observer(runtime_dir, _snapshot(13 * 60), "2026-08-29T13:00:00Z")
        async_observer.submit_manual(requested_by="operator", job_id="async-manual")
        for _attempt in range(50):
            async_job = async_observer.read_job("async-manual")
            if async_job["events"][-1]["state"] in {"complete", "partial", "failed"}:
                break
            time.sleep(0.01)
        assert async_job["events"][-1]["state"] == "complete"

        scheduled_failure = FailingAcquirer()
        failed_scheduled_observer = ChangeRegistryObserver(
            runtime_dir,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: scheduled_failure,
            now_fn=lambda: "2026-08-29T14:00:00Z",
        )
        try:
            failed_scheduled_observer.run(
                trigger_kind="scheduled",
                requested_by="systemd",
                scheduled_slot_value="2026-08-29T14:00:00Z",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("failed scheduled scan was not surfaced")
        scheduled_replay, scheduled_replay_adapter = _observer(
            runtime_dir, _snapshot(14 * 60), "2026-08-29T14:00:01Z"
        )
        replayed_scheduled_job = scheduled_replay.run(
            trigger_kind="scheduled",
            requested_by="systemd",
            scheduled_slot_value="2026-08-29T14:00:00Z",
        )
        assert replayed_scheduled_job["events"][-1]["state"] == "failed"
        assert observer_job_exit_code(replayed_scheduled_job) == 1
        assert scheduled_replay_adapter.acquire_calls == 0

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
            sleep_fn=lambda _seconds: None,
        )
        ChangeRegistryObserver(
            source_runtime,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: source_acquirer,
        ).run(
            trigger_kind="activation",
            requested_by="smoke",
            deployed_sha="d" * 40,
        )
        assert prices_source.write_calls == 0
        assert ads_source.write_calls == 0

        serialized_runtime = Path(tmp) / "serialized-runtime"
        serialized_acquirer = LockProbingAcquirer(
            serialized_runtime,
            _snapshot(15 * 60),
        )
        serialized_observer = ChangeRegistryObserver(
            serialized_runtime,
            seller_id=SELLER,
            account_scope=ACCOUNT,
            acquirer_factory=lambda: serialized_acquirer,
            now_fn=lambda: "2026-08-29T15:00:00Z",
        )
        serialized_result: dict[str, Any] = {}
        serialized_failure: list[BaseException] = []
        persistence_lock_evidence: list[dict[str, Any]] = []

        def probe_persistence_lock(
            _stage: str,
            _conn: sqlite3.Connection | None,
        ) -> None:
            with warehouse_functional_write_lock(
                serialized_runtime,
                blocking=False,
            ) as evidence:
                persistence_lock_evidence.append(dict(evidence))

        serialized_observer.persistence_stage_hook = probe_persistence_lock

        def run_serialized_activation() -> None:
            try:
                serialized_result.update(
                    serialized_observer.run(
                        trigger_kind="activation",
                        requested_by="trusted-release-runner",
                        deployed_sha="e" * 40,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                serialized_failure.append(exc)

        with warehouse_functional_write_lock(serialized_runtime):
            serialized_thread = threading.Thread(
                target=run_serialized_activation,
                name="change-registry-activation-contention",
            )
            serialized_thread.start()
            time.sleep(0.2)
            assert serialized_thread.is_alive()
            assert serialized_acquirer.acquire_calls == 0
        serialized_thread.join(timeout=10)
        assert not serialized_thread.is_alive()
        assert serialized_failure == []
        assert serialized_result["events"][-1]["state"] == "complete"
        assert serialized_acquirer.acquire_calls == 1
        assert serialized_acquirer.lock_evidence is not None
        assert serialized_acquirer.lock_evidence["reentrant"] == 0.0
        assert persistence_lock_evidence
        assert all(
            evidence["reentrant"] == 1.0
            for evidence in persistence_lock_evidence
        )

        stat_before = db_path.stat()
        readonly_surface = ChangeRegistryReadSurface(
            runtime_dir, seller_id=SELLER, account_scope=ACCOUNT
        )
        readonly = readonly_surface.overview()
        stat_after = db_path.stat()
        assert readonly["storage"] == {"mode": "ro", "query_only": True}
        assert (stat_before.st_size, stat_before.st_mtime_ns) == (
            stat_after.st_size,
            stat_after.st_mtime_ns,
        )
        http_entrypoint = RegistryUploadHttpEntrypoint.__new__(
            RegistryUploadHttpEntrypoint
        )
        http_entrypoint.change_registry_read_surface = readonly_surface
        http_entrypoint.change_registry_enabled = True
        api_payload = http_entrypoint.handle_change_registry_request({"limit": 20})
        assert api_payload["status"]["last_checkpoint"] == readonly["status"][
            "last_checkpoint"
        ]
        assert api_payload["activation"] == {"enabled": True}
        template = (
            ROOT
            / "packages"
            / "adapters"
            / "templates"
            / "sheet_vitrina_v1_web_vitrina.html"
        ).read_text(encoding="utf-8")
        assert "Реестр изменений" in template
        assert "data-change-registry-status" in template

    print("change_registry_observer_smoke: OK")


if __name__ == "__main__":
    main()
