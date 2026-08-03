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
    AttentionKind,
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


def _register_with_curator(
    registry: Registry,
    *,
    identity: str,
    suffix: str,
    curator: str,
    executor: str,
    envelope: str = "",
    envelope_title: str = "",
    role: str = "root",
    title: str = "",
) -> None:
    passport = _passport(suffix, identity)
    visible_title = title or f"Task {suffix}"
    passport["title"] = visible_title
    passport["source"] = {
        "curator_thread_id": curator,
        "executor_thread_id": executor,
    }
    registry.register_task(
        task_id=identity,
        title=visible_title,
        repo="orenvlad-ai/wb-core",
        project_id="wb-core",
        objective=f"Complete task {suffix}",
        passport=passport,
        curator_thread_id=curator,
        executor_thread_id=executor,
        host_id="host-1",
        acceptance_envelope_id=envelope,
        acceptance_title=envelope_title,
        acceptance_role=role,
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
        "reserve-attention",
        "ack-attention",
        "accept-curator",
        "pending-executor-archives",
        "python3 apps/codex_task_orchestrator.py report",
        "rotation_due=true",
        "Задача принята",
    ):
        assert required in watcher_prompt
    assert watcher_contract["attention_delivery"]["transport_semantics"] == (
        "at-least-once-with-stable-event-id"
    )
    assert watcher_contract["report"]["unit"] == "acceptance-envelope"
    assert watcher_contract["acceptance"][
        "fail_closed_when_curator_has_multiple_waiting_envelopes"
    ] is True
    assert watcher_contract["executor_succession"][
        "archive_only_after_successor_readback"
    ] is True
    assert watcher_contract["rotation"]["activate_only_after_smoke"] is True
    assert watcher_contract["rotation"]["smoke_visible_report"][
        "forbid_raw_machine_state"
    ] is True
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

    # Attention routing, acceptance envelopes, localized report rendering and
    # executor succession are independent from the incident smoke above.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "attention-registry")
        registry.initialize()
        curator = "curator-shared"
        envelope_id = "global-orchestration-v1"
        _register_with_curator(
            registry,
            identity="routing-root-v1",
            suffix="routing-root",
            curator=curator,
            executor="executor-c2",
            envelope=envelope_id,
            envelope_title="Глобальная оркестрация",
            role="root",
            title="Техническая основа оркестрации",
        )
        _register_with_curator(
            registry,
            identity="routing-child-v1",
            suffix="routing-child",
            curator=curator,
            executor="executor-c3",
            envelope=envelope_id,
            envelope_title="Глобальная оркестрация",
            role="corrective",
            title="Адресная доставка результата",
        )
        _register_with_curator(
            registry,
            identity="independent-v1",
            suffix="independent",
            curator="curator-independent",
            executor="executor-independent",
            title="Независимая задача",
        )
        registry.prepare_watcher(
            generation=1,
            thread_id="watcher-attention-1",
            host_id="host-1",
            automation_id="attention-auto-1",
        )
        registry.smoke_watcher(
            generation=1, evidence_digest="sha256:" + "1" * 64
        )
        registry.activate_watcher(generation=1)

        registry.update_task(
            task_id="routing-root-v1",
            expected_revision=1,
            status=TaskStatus.WORKING,
            progress=100,
            eta="готово",
            delta="Основная реализация завершена.",
            current="Проверяется передача результата куратору.",
            blocker=None,
        )
        registry.update_task(
            task_id="routing-child-v1",
            expected_revision=1,
            status=TaskStatus.WORKING,
            progress=35,
            eta="около часа",
            delta="Добавлен надёжный маршрут уведомления.",
            current="Проверяется PR в GitHub; Watcher продолжает наблюдение C3.",
            blocker=None,
        )
        localized = registry.report()
        assert localized.count("Статус:") == 2
        assert localized.count("Задача: Глобальная оркестрация") == 1
        assert "Задача: Независимая задача" in localized
        assert "Прогресс: ≈35%" in localized
        assert "routing-root-v1" not in localized
        assert "routing-child-v1" not in localized
        assert "DONE_AWAITING_ACCEPTANCE" not in localized
        assert "curator-shared" not in localized
        for hidden in (
            "Registry",
            "integrity",
            "queue",
            "lease",
            "bounded",
            "revision",
            "wait_threads",
        ):
            assert hidden.casefold() not in localized.casefold()
        assert "GitHub" in localized and "Watcher" in localized and "C3" in localized
        first_recorded = registry.report(record=True)
        unchanged = registry.report(record=True)
        assert "Изменений нет; работа продолжается:" not in first_recorded
        assert "Изменений нет; работа продолжается:" in unchanged
        with registry.connect() as connection:
            connection.execute(
                "UPDATE tasks SET revision=revision+1 WHERE task_id='routing-child-v1'"
            )
        diagnostic_only_change = registry.report(record=True)
        assert "Изменений нет; работа продолжается:" in diagnostic_only_change
        with registry.connect() as connection:
            connection.execute(
                "UPDATE tasks SET revision=revision-1 WHERE task_id='routing-child-v1'"
            )
        try:
            registry.update_task(
                task_id="routing-child-v1",
                expected_revision=2,
                status=TaskStatus.WORKING,
                progress=35,
                eta="около часа",
                delta="Registry revision 2 сохранена.",
                current="Работа продолжается.",
                blocker=None,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("visible report fields must reject raw machine diagnostics")

        # TARGET_CREATE_READBACK + prompt delivery + registry link + successor
        # active evidence make C2 an inactive legacy executor. Missing evidence,
        # a wrong envelope and archiving the current successor all fail closed.
        try:
            registry.register_executor_succession(
                envelope_id=envelope_id,
                predecessor_task_id="routing-root-v1",
                successor_task_id="routing-child-v1",
                reason="corrective executor",
                checkpoint_digest="",
                target_readback_digest="sha256:" + "2" * 64,
                prompt_delivery_digest="sha256:" + "3" * 64,
                registry_link_digest="sha256:" + "4" * 64,
                successor_active_digest="sha256:" + "5" * 64,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("missing successor readback must fail closed")
        try:
            registry.register_executor_succession(
                envelope_id="independent-v1",
                predecessor_task_id="routing-root-v1",
                successor_task_id="routing-child-v1",
                reason="wrong envelope",
                checkpoint_digest="sha256:" + "1" * 64,
                target_readback_digest="sha256:" + "2" * 64,
                prompt_delivery_digest="sha256:" + "3" * 64,
                registry_link_digest="sha256:" + "4" * 64,
                successor_active_digest="sha256:" + "5" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("cross-envelope executor succession must fail closed")
        succession = registry.register_executor_succession(
            envelope_id=envelope_id,
            predecessor_task_id="routing-root-v1",
            successor_task_id="routing-child-v1",
            reason="C3 корректирует незавершённую владельцем цель C2.",
            checkpoint_digest="sha256:" + "1" * 64,
            target_readback_digest="sha256:" + "2" * 64,
            prompt_delivery_digest="sha256:" + "3" * 64,
            registry_link_digest="sha256:" + "4" * 64,
            successor_active_digest="sha256:" + "5" * 64,
        )
        assert succession["status"] == "READY_TO_ARCHIVE"
        assert registry.register_executor_succession(
            envelope_id=envelope_id,
            predecessor_task_id="routing-root-v1",
            successor_task_id="routing-child-v1",
            reason="C3 корректирует незавершённую владельцем цель C2.",
            checkpoint_digest="sha256:" + "1" * 64,
            target_readback_digest="sha256:" + "2" * 64,
            prompt_delivery_digest="sha256:" + "3" * 64,
            registry_link_digest="sha256:" + "4" * 64,
            successor_active_digest="sha256:" + "5" * 64,
        )["idempotent"] is True
        try:
            registry.confirm_executor_archive(
                succession_id=str(succession["succession_id"]),
                predecessor_thread_id="executor-c3",
                archive_readback_digest="sha256:" + "6" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("the current successor executor must never be archived")
        archived = registry.confirm_executor_archive(
            succession_id=str(succession["succession_id"]),
            predecessor_thread_id="executor-c2",
            archive_readback_digest="sha256:" + "6" * 64,
        )
        assert archived["status"] == "ARCHIVED"
        assert registry.confirm_executor_archive(
            succession_id=str(succession["succession_id"]),
            predecessor_thread_id="executor-c2",
            archive_readback_digest="sha256:" + "6" * 64,
        )["idempotent"] is True
        assert '"event_type":"archived"' in registry.event_path.read_text(
            encoding="utf-8"
        )

        root_event = registry.enqueue_attention(
            task_id="routing-root-v1",
            expected_revision=2,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            evidence_summary="Основная реализация доказанно завершена.",
            evidence_digest="sha256:" + "a" * 64,
            eta="готово",
            delta="Основная реализация завершена.",
            current="Куратор подтверждает получение результата.",
        )
        duplicate_event = registry.enqueue_attention(
            task_id="routing-root-v1",
            expected_revision=2,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            evidence_summary="Основная реализация доказанно завершена.",
            evidence_digest="sha256:" + "a" * 64,
            eta="готово",
            delta="Основная реализация завершена.",
            current="Куратор подтверждает получение результата.",
        )
        assert duplicate_event["event_id"] == root_event["event_id"]
        assert duplicate_event["idempotent"] is True
        reserved = registry.reserve_attention(
            generation=1, owner="watcher-send-1", lease_seconds=60, limit=8
        )
        assert [item["event_id"] for item in reserved["reserved"]] == [
            root_event["event_id"]
        ]
        assert registry.reserve_attention(
            generation=1, owner="duplicate-heartbeat", lease_seconds=60, limit=8
        )["reserved"] == []
        try:
            registry.ack_attention(
                event_id=str(root_event["event_id"]),
                event_digest=str(root_event["event_digest"]),
                curator_thread_id="wrong-curator",
                expected_task_revision=3,
                ack_evidence_digest="sha256:" + "b" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("wrong curator acknowledgement must fail closed")
        assert registry.retry_attention(
            event_id=str(root_event["event_id"]),
            owner="watcher-send-1",
            error="Desktop send temporarily failed",
            retry_after_seconds=0,
        )["state"] == "RETRY"
        retry_reserved = registry.reserve_attention(
            generation=1, owner="watcher-send-2", lease_seconds=60, limit=8
        )
        assert retry_reserved["reserved"][0]["attempt"] == 2
        registry.mark_attention_sent(
            event_id=str(root_event["event_id"]),
            owner="watcher-send-2",
            transport_receipt_digest="sha256:" + "c" * 64,
            ack_timeout_seconds=600,
        )
        root_ack = registry.ack_attention(
            event_id=str(root_event["event_id"]),
            event_digest=str(root_event["event_digest"]),
            curator_thread_id=curator,
            expected_task_revision=3,
            ack_evidence_digest="sha256:" + "d" * 64,
        )
        assert root_ack["owner_notification_required"] is False
        assert root_ack["acceptance_envelope_state"] == "OPEN"

        child_event = registry.enqueue_attention(
            task_id="routing-child-v1",
            expected_revision=2,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            evidence_summary="Корректирующая доставка доказанно завершена.",
            evidence_digest="sha256:" + "e" * 64,
            eta="готово",
            delta="Адресная доставка результата завершена.",
            current="Куратор готовит единый итог владельцу.",
        )
        child_reserved = registry.reserve_attention(
            generation=1, owner="watcher-crash-window", lease_seconds=60, limit=8
        )
        assert child_reserved["reserved"][0]["event_id"] == child_event["event_id"]
        child_ack = registry.ack_attention(
            event_id=str(child_event["event_id"]),
            event_digest=str(child_event["event_digest"]),
            curator_thread_id=curator,
            expected_task_revision=3,
            ack_evidence_digest="sha256:" + "f" * 64,
        )
        assert child_ack["acceptance_envelope_state"] == "AWAITING_ACCEPTANCE"
        assert child_ack["owner_notification_required"] is True
        # Crash window send -> confirm: curator acknowledgement may win before
        # the Watcher records transport success; the late confirmation is safe.
        assert registry.mark_attention_sent(
            event_id=str(child_event["event_id"]),
            owner="watcher-crash-window",
            transport_receipt_digest="sha256:" + "0" * 64,
            ack_timeout_seconds=600,
        )["idempotent"] is True
        assert registry.attention_event(str(child_event["event_id"]))["event"][
            "transport_receipt_digest"
        ] == "sha256:" + "0" * 64
        registry.confirm_owner_notification(
            curator_thread_id=curator,
            envelope_id=envelope_id,
            expected_revision=int(child_ack["acceptance_envelope_revision"]),
            notification_evidence_digest="sha256:" + "9" * 64,
        )
        accepted = registry.accept_curator(
            curator_thread_id=curator,
            expected_envelope_revision=int(child_ack["acceptance_envelope_revision"]),
        )
        assert set(accepted["accepted_task_ids"]) == {
            "routing-root-v1",
            "routing-child-v1",
        }
        after_accept = registry.report()
        assert "Глобальная оркестрация" not in after_accept
        assert "Независимая задача" in after_accept

        human_event = registry.enqueue_attention(
            task_id="independent-v1",
            expected_revision=1,
            kind=AttentionKind.STRICT_HUMAN_GATE,
            evidence_summary="Требуется интерактивная авторизация владельца.",
            evidence_digest="sha256:" + "8" * 64,
            eta="после входа",
            delta="Все безопасные технические шаги завершены.",
            current="Ожидается вход владельца.",
            blocker="Нужно войти в систему и подтвердить авторизацию.",
            human_reason="interactive-auth",
            repo_owned_remediation_available=False,
            remediation_exhausted=True,
        )
        human_reserved = registry.reserve_attention(
            generation=1, owner="watcher-human", lease_seconds=60, limit=8
        )
        assert human_reserved["reserved"][0]["event_id"] == human_event["event_id"]
        human_ack = registry.ack_attention(
            event_id=str(human_event["event_id"]),
            event_digest=str(human_event["event_digest"]),
            curator_thread_id="curator-independent",
            expected_task_revision=2,
            ack_evidence_digest="sha256:" + "7" * 64,
        )
        assert human_ack["task_revision"] == 3
        assert "Блокер: Нужно войти" in registry.report()
        assert registry.integrity()["ok"] is True, registry.integrity()

    # Multiple independent user-level envelopes in one curator are visible as
    # separate blocks and make the owner phrase ambiguous until one is named by
    # a new explicit user decision. The phrase alone never picks a random task.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "ambiguity-registry")
        registry.initialize()
        for index in (1, 2):
            _register_with_curator(
                registry,
                identity=f"ambiguity-task-{index}",
                suffix=f"ambiguity-{index}",
                curator="curator-ambiguous",
                executor=f"executor-ambiguous-{index}",
                title=f"Независимая цель {index}",
            )
        registry.prepare_watcher(
            generation=1,
            thread_id="watcher-ambiguous",
            host_id="host-1",
            automation_id="ambiguous-auto",
        )
        registry.smoke_watcher(
            generation=1, evidence_digest="sha256:" + "1" * 64
        )
        registry.activate_watcher(generation=1)
        envelope_revisions = []
        for index in (1, 2):
            event = registry.enqueue_attention(
                task_id=f"ambiguity-task-{index}",
                expected_revision=1,
                kind=AttentionKind.TECHNICAL_COMPLETION,
                evidence_summary=f"Независимая цель {index} завершена.",
                evidence_digest="sha256:" + str(index) * 64,
                eta="готово",
                delta="Работа завершена.",
                current="Куратор готовит итог владельцу.",
            )
            ack = registry.ack_attention(
                event_id=str(event["event_id"]),
                event_digest=str(event["event_digest"]),
                curator_thread_id="curator-ambiguous",
                expected_task_revision=2,
                ack_evidence_digest="sha256:" + str(index + 2) * 64,
            )
            registry.confirm_owner_notification(
                curator_thread_id="curator-ambiguous",
                envelope_id=f"ambiguity-task-{index}",
                expected_revision=int(ack["acceptance_envelope_revision"]),
                notification_evidence_digest="sha256:" + str(index + 4) * 64,
            )
            envelope_revisions.append(int(ack["acceptance_envelope_revision"]))
        assert registry.report().count("Статус:") == 2
        try:
            registry.accept_curator(
                curator_thread_id="curator-ambiguous",
                expected_envelope_revision=envelope_revisions[0],
            )
        except RuntimeError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("ambiguous owner acceptance must fail closed")

    # Stale-revision and generation rotation preserve the outbox while old
    # Watchers become no-op. An event whose task advanced is never delivered.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "rotation-registry")
        registry.initialize()
        _register_with_curator(
            registry,
            identity="stale-route-v1",
            suffix="stale-route",
            curator="curator-stale",
            executor="executor-stale",
            title="Проверка устаревшего события",
        )
        registry.prepare_watcher(
            generation=1,
            thread_id="watcher-old",
            host_id="host-1",
            automation_id="old-auto",
        )
        registry.smoke_watcher(
            generation=1, evidence_digest="sha256:" + "1" * 64
        )
        registry.activate_watcher(generation=1)
        stale_event = registry.enqueue_attention(
            task_id="stale-route-v1",
            expected_revision=1,
            kind=AttentionKind.SERIOUS_STALL,
            evidence_summary="Серьёзная остановка зафиксирована.",
            evidence_digest="sha256:" + "2" * 64,
            eta="уточняется",
            delta="Остановка подтверждена.",
            current="Выполняется техническое восстановление.",
        )
        registry.update_task(
            task_id="stale-route-v1",
            expected_revision=2,
            status=TaskStatus.WORKING,
            progress=10,
            eta="около часа",
            delta="Техническое восстановление завершено.",
            current="Работа продолжается.",
            blocker=None,
        )
        registry.prepare_watcher(
            generation=2,
            thread_id="watcher-new",
            host_id="host-1",
            automation_id="new-auto",
        )
        registry.smoke_watcher(
            generation=2, evidence_digest="sha256:" + "3" * 64
        )
        registry.activate_watcher(generation=2)
        assert registry.reserve_attention(
            generation=1, owner="old-run", lease_seconds=60, limit=8
        )["reason"] == "stale-watcher-generation"
        assert registry.reserve_attention(
            generation=2, owner="new-run", lease_seconds=60, limit=8
        )["reserved"] == []
        assert registry.attention_event(str(stale_event["event_id"]))["event"][
            "state"
        ] == "STALE"

    # Ambiguous successor identity is rejected before either executor is made
    # inactive; terminal executors without a proven successor remain current.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "succession-ambiguity")
        registry.initialize()
        envelope = "ambiguous-successor-v1"
        for identity, role in (
            ("successor-root-v1", "root"),
            ("successor-child-a", "corrective"),
            ("successor-child-b", "corrective"),
        ):
            _register_with_curator(
                registry,
                identity=identity,
                suffix=identity,
                curator="curator-successor",
                executor=f"executor-{identity}",
                envelope=envelope,
                envelope_title="Проверка преемника",
                role=role,
                title="Проверка преемника",
            )
        try:
            registry.register_executor_succession(
                envelope_id=envelope,
                predecessor_task_id="successor-root-v1",
                successor_task_id="successor-child-a",
                reason="ambiguous successor",
                checkpoint_digest="sha256:" + "1" * 64,
                target_readback_digest="sha256:" + "2" * 64,
                prompt_delivery_digest="sha256:" + "3" * 64,
                registry_link_digest="sha256:" + "4" * 64,
                successor_active_digest="sha256:" + "5" * 64,
            )
        except RuntimeError as exc:
            assert "ambiguous" in str(exc)
        else:
            raise AssertionError("multiple active successor candidates must fail closed")
        assert registry.pending_executor_archives() == []

    # A task already parked in the legacy terminal state without delivery
    # evidence is moved back to pending handoff exactly once and keeps audit.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "backfill-registry")
        registry.initialize()
        _register_with_curator(
            registry,
            identity="legacy-terminal-v1",
            suffix="legacy-terminal",
            curator="curator-backfill",
            executor="executor-backfill",
            title="Исторический результат",
        )
        with registry.connect() as connection:
            connection.execute(
                "UPDATE tasks SET status='DONE_AWAITING_ACCEPTANCE',revision=8,progress_percent=100,"
                "eta_text='готово',last_delta='Работа завершена.',"
                "current_action='Требуется подтверждение доставки.' WHERE task_id='legacy-terminal-v1'"
            )
        assert registry.integrity()["unproven_terminal_tasks"] == 1
        event = registry.enqueue_attention(
            task_id="legacy-terminal-v1",
            expected_revision=8,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            evidence_summary="Историческое завершение подтверждено и требует адресной доставки.",
            evidence_digest="sha256:" + "a" * 64,
            backfill=True,
            eta="готово",
            delta="Результат подготовлен к адресной передаче.",
            current="Куратор подтверждает получение результата.",
        )
        assert event["task_revision"] == 9
        assert registry.enqueue_attention(
            task_id="legacy-terminal-v1",
            expected_revision=8,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            evidence_summary="Историческое завершение подтверждено и требует адресной доставки.",
            evidence_digest="sha256:" + "a" * 64,
            backfill=True,
            eta="готово",
            delta="Результат подготовлен к адресной передаче.",
            current="Куратор подтверждает получение результата.",
        )["idempotent"] is True
        ack = registry.ack_attention(
            event_id=str(event["event_id"]),
            event_digest=str(event["event_digest"]),
            curator_thread_id="curator-backfill",
            expected_task_revision=9,
            ack_evidence_digest="sha256:" + "b" * 64,
        )
        assert ack["task_revision"] == 10
        assert ack["acceptance_envelope_state"] == "AWAITING_ACCEPTANCE"
        repeated_after_ack = registry.enqueue_attention(
            task_id="legacy-terminal-v1",
            expected_revision=10,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            evidence_summary="Историческое завершение подтверждено и требует адресной доставки.",
            evidence_digest="sha256:" + "a" * 64,
            backfill=True,
            eta="готово",
            delta="Результат подготовлен к адресной передаче.",
            current="Куратор подтверждает получение результата.",
        )
        assert repeated_after_ack["event_id"] == event["event_id"]
        assert repeated_after_ack["idempotent"] is True
        assert registry.integrity()["ok"] is True, registry.integrity()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.parse_args()
    run_smoke()
    print("codex task orchestrator smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
