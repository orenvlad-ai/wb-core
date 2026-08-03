"""Deterministic smoke coverage for the local Codex task control plane."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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
    TaskStatus,
    classify_incident,
)


def _passport(name: str) -> dict[str, object]:
    return {
        "schema": "wb-core-task-passport/v1",
        "title": name,
        "objective": f"Complete {name}",
        "acceptance": ["checks pass", "terminal release proof"],
        "autonomy": {"reversible_in_scope_actions": True},
    }


def _register(registry: Registry, identity: str, suffix: str) -> None:
    registry.register_task(
        task_id=identity,
        title=f"Task {suffix}",
        repo="orenvlad-ai/wb-core",
        project_id="wb-core",
        objective=f"Complete task {suffix}",
        passport=_passport(suffix),
        curator_thread_id=f"curator-{suffix}",
        executor_thread_id=f"executor-{suffix}",
        host_id="host-1",
    )


def run_smoke() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "registry")
        registry.initialize()
        _register(registry, "t-task-alpha", "alpha")
        _register(registry, "t-task-bravo", "bravo")

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
            arbiter_thread_id="arbiter-alpha",
        )
        assert claim["status"] == "CLAIMED"

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
            arbiter_thread_id="arbiter-bravo",
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

        registry.prepare_watcher(
            generation=1,
            thread_id="watcher-1",
            host_id="host-1",
            automation_id="auto-1",
        )
        registry.activate_watcher(generation=1)
        first = registry.begin_run(generation=1, owner="run-a", lease_seconds=60)
        overlap = registry.begin_run(generation=1, owner="run-b", lease_seconds=60)
        assert first["acquired"] is True
        assert overlap == {"acquired": False, "reason": "overlapping-run", "owner": "run-a"}
        assert registry.end_run(generation=1, owner="run-a")["released"] is True
        registry.prepare_watcher(
            generation=2,
            thread_id="watcher-2",
            host_id="host-1",
            automation_id="auto-1",
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    run_smoke()
    print("codex task orchestrator smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
