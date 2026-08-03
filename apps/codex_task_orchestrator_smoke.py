"""Deterministic smoke coverage for the local Codex task control plane."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.codex_task_orchestrator import Registry
from apps.codex_task_orchestrator_spec import (
    IncidentDisposition,
    RetryObservation,
    STRICT_HUMAN_REASONS,
    TaskStatus,
    classify_incident,
)


def _passport(name: str, identity: str) -> dict[str, object]:
    return {
        "schema": "wb-core-task-passport/v1",
        "title": f"Task {name}",
        "objective": f"Complete task {name}",
        "expected_result": f"{name} is complete and independently verified",
        "scope": {
            "execution_contour": "repo-only",
            "included": ["orchestration registry"],
            "excluded": ["production data mutation"],
        },
        "constraints": ["preserve unrelated worktrees"],
        "acceptance": ["checks pass", "terminal release proof"],
        "closure": ["origin/main contains the reviewed change"],
        "autonomy": {
            "reversible_technical_actions": "authorized",
            "production_data_mutation": "forbidden",
            "human_only_reasons": sorted(STRICT_HUMAN_REASONS),
        },
        "initial_resources": [f"task:{identity}", "wb-core:release"],
        "source": {
            "curator_thread_id": f"curator-{name}",
            "executor_thread_id": f"executor-{name}",
        },
    }


def _register(registry: Registry, identity: str, suffix: str) -> None:
    registry.register_task(
        task_id=identity,
        title=f"Task {suffix}",
        repo="orenvlad-ai/wb-core",
        project_id="wb-core",
        objective=f"Complete task {suffix}",
        passport=_passport(suffix, identity),
        curator_thread_id=f"curator-{suffix}",
        executor_thread_id=f"executor-{suffix}",
        host_id="host-1",
    )


def _decision(
    *,
    task_id: str,
    revision: int,
    incident_key: str,
    action: str,
    scope: list[str],
    transition: str,
    digest: str,
) -> dict[str, object]:
    return {
        "schema": "wb-core-arbiter-decision/v1",
        "task_id": task_id,
        "task_revision": revision,
        "incident_key": incident_key,
        "action": action,
        "scope": scope,
        "expected_transition": transition,
        "evidence_digest": digest,
        "reason": "Bounded pilot evidence supports this exact action.",
        "human_reason": "",
    }


def run_smoke() -> None:
    watcher_contract = json.loads(
        (ROOT / "packages" / "contracts" / "codex_watcher_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert watcher_contract["schema"] == "wb-core-codex-watcher/v1"
    assert watcher_contract["watcher"]["target_batch_limit"] == 8
    assert watcher_contract["retry_policy"]["third_identical_fingerprint"] == "open-sol-arbiter"
    assert watcher_contract["fallback_discovery"]["require_pinned"] is True
    assert watcher_contract["feature_flag"]["default"] is False
    passport_schema = json.loads(
        (ROOT / "packages" / "contracts" / "codex_task_passport_v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert passport_schema["properties"]["schema"]["const"] == "wb-core-task-passport/v1"
    watcher_prompt = (
        ROOT / "docs" / "policies" / "codex_watcher_prompt_v1.md"
    ).read_text(encoding="utf-8")
    arbiter_prompt = (
        ROOT / "docs" / "policies" / "codex_arbiter_prompt_v1.md"
    ).read_text(encoding="utf-8")
    for required in (
        "begin-run",
        "wait_threads(timeoutMs: 0)",
        "record-failure",
        "close-incident",
        "rotation_due=true",
        "Задача принята",
    ):
        assert required in watcher_prompt
    assert "wb-core-arbiter-brief/v1" in arbiter_prompt
    assert "Do not request or reconstruct the full chat" in arbiter_prompt

    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "registry")
        registry.initialize()
        _register(registry, "t-task-alpha", "alpha")
        _register(registry, "t-task-bravo", "bravo")
        repeated_registration = registry.register_task(
            task_id="t-task-alpha",
            title="Task alpha",
            repo="orenvlad-ai/wb-core",
            project_id="wb-core",
            objective="Complete task alpha",
            passport=_passport("alpha", "t-task-alpha"),
            curator_thread_id="curator-alpha",
            executor_thread_id="executor-alpha",
            host_id="host-1",
        )
        assert repeated_registration["idempotent"] is True
        assert registry.add_thread(
            task_id="t-task-alpha",
            role="executor",
            generation=1,
            thread_id="executor-alpha",
            host_id="host-1",
        )["idempotent"] is True

        updated = registry.update_task(
            task_id="t-task-alpha",
            expected_revision=1,
            status=TaskStatus.READY_FOR_RELEASE,
            progress=70,
            eta="30–60 минут",
            delta="Проверки завершены.",
            current="Ожидается допуск в Release Train.",
            blocker=None,
        )
        assert updated["revision"] == 2
        report = registry.report()
        assert "Статус: Выпуск и проверка" in report
        assert "Статус: Выпуск и проверка\nЗадача:" in report
        assert "Прогресс: ≈70% · Осталось: ≈30–60 минут" in report
        assert "Блокер:" not in report

        def open_same() -> dict[str, object]:
            return registry.open_incident(
                task_id="t-task-alpha",
                task_revision=2,
                phase="release",
                error_class="transport-indeterminate",
                evidence_fingerprint="same-runtime-readback",
                resources=("task:t-task-alpha", "wb-core:release"),
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            cases = list(executor.map(lambda _: open_same(), range(4)))
        assert len({item["case_id"] for item in cases}) == 1
        case_alpha = str(cases[0]["case_id"])
        claim = registry.claim_incident(
            case_id=case_alpha,
            expected_task_revision=2,
            reservation_owner="watcher-run-a",
        )
        assert claim["status"] == "CLAIMED"
        overlapping_case = registry.open_incident(
            task_id="t-task-alpha",
            task_revision=2,
            phase="release",
            error_class="different-observation",
            evidence_fingerprint="different-fingerprint",
            resources=("wb-core:release",),
        )
        assert overlapping_case["case_id"] == case_alpha
        assert overlapping_case["incident_key"] == cases[0]["incident_key"]
        assert overlapping_case["requested_incident_key"] != overlapping_case["incident_key"]
        attached = registry.attach_arbiter(
            case_id=case_alpha,
            expected_task_revision=2,
            thread_id="arbiter-alpha",
            host_id="host-1",
            generation=1,
            reservation_owner="watcher-run-a",
        )
        assert attached["arbiter_thread_id"] == "arbiter-alpha"

        case_bravo = registry.open_incident(
            task_id="t-task-bravo",
            task_revision=1,
            phase="release",
            error_class="queue-order",
            evidence_fingerprint="shared-lane",
            resources=("task:t-task-bravo", "wb-core:release"),
        )
        waiting = registry.claim_incident(
            case_id=str(case_bravo["case_id"]),
            expected_task_revision=1,
            reservation_owner="watcher-run-b",
        )
        assert waiting["status"] == "WAITING_RESOURCE"
        assert waiting["conflicts"][0]["resource"] == "wb-core:release"

        registry.update_task(
            task_id="t-task-alpha",
            expected_revision=2,
            status=TaskStatus.RECOVERING,
            progress=None,
            eta=None,
            delta="Открыт recovery.",
            current="Проверяется новое состояние.",
            blocker=None,
        )
        stale = registry.decide(
            case_id=case_alpha,
            expected_task_revision=2,
            decision={"action": "retry"},
            expected_transition="runtime-healthy",
            evidence_digest="sha256:" + "a" * 64,
        )
        assert stale["status"] == "STALE"
        assert '"event_type":"stale"' in registry.event_path.read_text(
            encoding="utf-8"
        )

        claimed_bravo = registry.claim_incident(
            case_id=str(case_bravo["case_id"]),
            expected_task_revision=1,
            reservation_owner="watcher-run-b",
        )
        assert claimed_bravo["status"] == "CLAIMED"
        registry.attach_arbiter(
            case_id=str(case_bravo["case_id"]),
            expected_task_revision=1,
            thread_id="arbiter-bravo",
            host_id="host-1",
            generation=1,
            reservation_owner="watcher-run-b",
        )
        registry.decide(
            case_id=str(case_bravo["case_id"]),
            expected_task_revision=1,
            decision=_decision(
                task_id="t-task-bravo",
                revision=1,
                incident_key=str(case_bravo["incident_key"]),
                action="continue-waiting",
                scope=["task:t-task-bravo", "wb-core:release"],
                transition="queue-observed",
                digest="sha256:" + "b" * 64,
            ),
            expected_transition="queue-observed",
            evidence_digest="sha256:" + "b" * 64,
        )
        assert registry.decide(
            case_id=str(case_bravo["case_id"]),
            expected_task_revision=1,
            decision=_decision(
                task_id="t-task-bravo",
                revision=1,
                incident_key=str(case_bravo["incident_key"]),
                action="continue-waiting",
                scope=["task:t-task-bravo", "wb-core:release"],
                transition="queue-observed",
                digest="sha256:" + "b" * 64,
            ),
            expected_transition="queue-observed",
            evidence_digest="sha256:" + "b" * 64,
        )["idempotent"] is True
        registry.deliver(case_id=str(case_bravo["case_id"]))
        verified = registry.verify(
            case_id=str(case_bravo["case_id"]),
            observed_transition="queue-observed",
            verification_evidence_digest="sha256:" + "d" * 64,
        )
        assert verified["status"] == "VERIFIED"
        assert registry.verify(
            case_id=str(case_bravo["case_id"]),
            observed_transition="queue-observed",
            verification_evidence_digest="sha256:" + "d" * 64,
        )["idempotent"] is True
        assert registry.close_incident(
            case_id=str(case_bravo["case_id"]),
            archive_evidence_digest="sha256:" + "c" * 64,
        )["status"] == "CLOSED"

        failures = [
            registry.record_failure(
                task_id="t-task-bravo",
                task_revision=1,
                phase="executor",
                error_class="empty-system-error",
                evidence_fingerprint="same-empty-turn",
                transient=True,
                empty_system_error=True,
                repo_owned_remediation_available=True,
                remediation_exhausted=False,
                human_reason="",
            )
            for _ in range(3)
        ]
        assert [item["disposition"] for item in failures] == [
            "RETRY",
            "REPLACE_EXECUTOR",
            "OPEN_ARBITER",
        ]
        assert [item["incident_required"] for item in failures] == [
            False,
            True,
            True,
        ]
        replacement_case = registry.open_incident(
            task_id="t-task-bravo",
            task_revision=1,
            phase="executor",
            error_class="empty-system-error",
            evidence_fingerprint="same-empty-turn",
            resources=("task:t-task-bravo",),
        )
        resolved_failure = registry.resolve_failure(
            task_id="t-task-bravo",
            phase="executor",
            evidence_fingerprint="same-empty-turn",
        )
        assert resolved_failure["resolved"] is True
        assert resolved_failure["incidents_staled"] == 1
        with registry.connect() as connection:
            resolved_case = connection.execute(
                "SELECT status FROM incidents WHERE case_id=?",
                (replacement_case["case_id"],),
            ).fetchone()
        assert resolved_case["status"] == "STALE"

        registry.prepare_watcher(
            generation=1,
            thread_id="watcher-1",
            host_id="host-1",
            automation_id="auto-1",
            max_runs=1,
        )
        assert registry.prepare_watcher(
            generation=1,
            thread_id="watcher-1",
            host_id="host-1",
            automation_id="auto-1",
            max_runs=1,
        )["idempotent"] is True
        registry.smoke_watcher(
            generation=1,
            evidence_digest="sha256:" + "1" * 64,
        )
        registry.activate_watcher(generation=1)
        first = registry.begin_run(generation=1, owner="run-a", lease_seconds=60)
        assert first["rotation_due"] is True
        repeated_first = registry.begin_run(
            generation=1, owner="run-a", lease_seconds=60
        )
        assert repeated_first["idempotent"] is True
        assert repeated_first["run_count"] == first["run_count"]
        overlap = registry.begin_run(generation=1, owner="run-b", lease_seconds=60)
        assert first["acquired"] is True
        assert overlap == {"acquired": False, "reason": "overlapping-run", "owner": "run-a"}
        assert registry.end_run(generation=1, owner="run-a")["released"] is True
        assert registry.begin_run(
            generation=1, owner="rotation-old", lease_seconds=60
        )["acquired"] is True
        registry.prepare_watcher(
            generation=2,
            thread_id="watcher-2",
            host_id="host-1",
            automation_id="auto-1",
        )
        registry.smoke_watcher(
            generation=2,
            evidence_digest="sha256:" + "2" * 64,
        )
        registry.activate_watcher(generation=2)
        assert registry.begin_run(generation=1, owner="stale", lease_seconds=60) == {
            "acquired": False,
            "reason": "stale-watcher-generation",
        }
        assert registry.begin_run(generation=2, owner="fresh", lease_seconds=60)["acquired"] is True

        assert classify_incident(
            RetryObservation("transport", 2, transient=True)
        ) == IncidentDisposition.RETRY
        assert classify_incident(
            RetryObservation("transport", 3, transient=True)
        ) == IncidentDisposition.OPEN_ARBITER
        assert classify_incident(
            RetryObservation("system", 2, empty_system_error=True)
        ) == IncidentDisposition.REPLACE_EXECUTOR
        assert classify_incident(
            RetryObservation("system", 3, empty_system_error=True)
        ) == IncidentDisposition.OPEN_ARBITER
        assert classify_incident(
            RetryObservation(
                "auth",
                3,
                repo_owned_remediation_available=False,
                remediation_exhausted=True,
                human_reason="interactive-auth",
            )
        ) == IncidentDisposition.AWAIT_HUMAN
        assert classify_incident(
            RetryObservation(
                "formal-gate",
                3,
                repo_owned_remediation_available=False,
                remediation_exhausted=True,
                human_reason="generic-risk",
            )
        ) == IncidentDisposition.OPEN_ARBITER

        integrity = registry.integrity()
        assert integrity["ok"] is True, integrity
        assert registry.flush_events() == 0
        assert registry.event_path.stat().st_size > 0

        # Upgrade the v1 global thread uniqueness without losing mappings. A
        # curator may own several wb-core tasks, while active executors/arbiters
        # remain globally unique through the partial index.
        with registry.connect() as connection:
            connection.execute("DROP INDEX IF EXISTS one_active_execution_thread")
            connection.execute("ALTER TABLE task_threads RENAME TO task_threads_v2")
            connection.execute(
                "CREATE TABLE task_threads ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "task_id TEXT NOT NULL REFERENCES tasks(task_id),"
                "role TEXT NOT NULL CHECK(role IN ('curator','executor','arbiter')) ,"
                "generation INTEGER NOT NULL CHECK(generation > 0),"
                "thread_id TEXT NOT NULL UNIQUE,"
                "host_id TEXT NOT NULL DEFAULT '',"
                "active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),"
                "created_at TEXT NOT NULL,"
                "UNIQUE(task_id,role,generation))"
            )
            connection.execute(
                "INSERT INTO task_threads SELECT * FROM task_threads_v2"
            )
            connection.execute("DROP TABLE task_threads_v2")
        registry.initialize()
        with registry.connect() as connection:
            task_threads_sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_threads'"
                ).fetchone()[0]
            )
        assert "UNIQUE(task_id,thread_id)" in "".join(task_threads_sql.split())
        shared_curator_passport = _passport("charlie", "t-task-charlie")
        shared_curator_passport["source"]["curator_thread_id"] = "curator-alpha"
        registry.register_task(
            task_id="t-task-charlie",
            title="Task charlie",
            repo="orenvlad-ai/wb-core",
            project_id="wb-core",
            objective="Complete task charlie",
            passport=shared_curator_passport,
            curator_thread_id="curator-alpha",
            executor_thread_id="executor-charlie",
            host_id="host-1",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    run_smoke()
    print("codex task orchestrator smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
