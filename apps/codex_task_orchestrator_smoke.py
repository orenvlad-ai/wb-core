"""Deterministic smoke coverage for the local Codex task control plane."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.codex_task_orchestrator import DEFAULT_WATCHER_MAX_RUNS, Registry
from apps.codex_task_orchestrator_spec import (
    AttentionKind,
    IncidentDisposition,
    PROGRESS_PERCENT_BY_STAGE,
    ProgressStage,
    RetryObservation,
    STRICT_HUMAN_REASONS,
    TaskStatus,
    classify_incident,
)


def _fresh_evidence_time() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()


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
        curator_pin_readback_digest="sha256:" + "c" * 64,
        executor_pin_readback_digest="sha256:" + "e" * 64,
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
        curator_pin_readback_digest="sha256:" + "c" * 64,
        executor_pin_readback_digest="sha256:" + "e" * 64,
        acceptance_envelope_id=envelope,
        acceptance_title=envelope_title,
        acceptance_role=role,
    )


def _prepare_watcher(
    registry: Registry,
    *,
    generation: int,
    thread_id: str,
    automation_id: str,
    max_runs: int = DEFAULT_WATCHER_MAX_RUNS,
) -> dict[str, object]:
    return registry.prepare_watcher(
        generation=generation,
        thread_id=thread_id,
        host_id="host-1",
        automation_id=automation_id,
        title_readback_digest="sha256:" + "1" * 64,
        pin_readback_digest="sha256:" + "2" * 64,
        automation_readback_digest="sha256:" + "3" * 64,
        max_runs=max_runs,
    )


def _prove_repo_done(registry: Registry, task_id: str, pr: int) -> None:
    registry.link_pr(
        task_id=task_id,
        pr=pr,
        role="implementation",
        head_sha=f"{pr:040x}"[-40:],
        state="done",
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


def _run_progress_smoke() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "progress-registry")
        registry.initialize()
        identity = "progress-evidence-v1"
        _register(registry, identity, "progress")
        initial = registry.progress_state(task_id=identity)
        assert initial["progress_percent"] == 5
        assert initial["progress_stage"] == "executor-started"
        task = registry.active_tasks()[0]
        assert task["last_delta"] == "Исполнитель запущен и зарегистрирован."
        assert task["current_action"] == "Выполняется первичный технический анализ."

        # Heartbeat count and elapsed time are never evidence by themselves.
        try:
            registry.apply_progress(
                task_id=identity,
                expected_revision=1,
                run_owner="heartbeat-no-evidence",
            )
        except RuntimeError as exc:
            assert "no fresh progress evidence" in str(exc)
        else:
            raise AssertionError("time alone must not increase progress")
        assert registry.progress_state(task_id=identity)["progress_percent"] == 5

        # The executor records a stage, proof and bounded visible checkpoint;
        # only the Watcher-owned mapper materializes its canonical percentage.
        checkpoint = registry.record_progress_checkpoint(
            task_id=identity,
            expected_revision=1,
            stage=ProgressStage.PREFLIGHT_COMPLETE,
            evidence_digest="sha256:" + "1" * 64,
            eta="два–три часа",
            delta="Сверены документы, код и очередь выпуска.",
            current="Реализуется единый контракт этапов.",
        )
        assert checkpoint["stage"] == "preflight-complete"
        assert registry.progress_state(task_id=identity)["progress_percent"] == 5
        applied = registry.apply_progress(
            task_id=identity,
            expected_revision=1,
            run_owner="heartbeat-progress-1",
        )
        assert applied["progress_percent"] == 15
        assert applied["revision"] == 2
        assert registry.apply_progress(
            task_id=identity,
            expected_revision=1,
            run_owner="heartbeat-progress-1",
        )["idempotent"] is True
        assert registry.apply_progress(
            task_id=identity,
            expected_revision=2,
            run_owner="heartbeat-progress-1",
            observed_stage=ProgressStage.PREFLIGHT_COMPLETE,
            observed_evidence_digest="sha256:" + "2" * 64,
            observed_at=_fresh_evidence_time(),
            eta="два–три часа",
            delta="Повторный факт того же запуска не создаёт новый этап.",
            current="Реализуется единый контракт этапов.",
        )["idempotent"] is True

        registry.record_progress_checkpoint(
            task_id=identity,
            expected_revision=2,
            stage=ProgressStage.PREFLIGHT_COMPLETE,
            evidence_digest="sha256:" + "3" * 64,
            eta="около двух часов",
            delta="Уточнена семантика восстановления без смены этапа.",
            current="Добавляются проверки ранних и поздних стадий.",
        )
        same_stage = registry.apply_progress(
            task_id=identity,
            expected_revision=2,
            run_owner="heartbeat-progress-2",
        )
        assert same_stage["progress_percent"] == 15
        assert registry.active_tasks()[0]["last_delta"].startswith("Уточнена")

        # Fresh bounded file/test evidence is a floor, so even a legacy active
        # zero cannot repeat the launch text for another report.
        fallback = registry.apply_progress(
            task_id=identity,
            expected_revision=3,
            run_owner="heartbeat-progress-3",
            observed_stage=ProgressStage.IMPLEMENTATION_STARTED,
            observed_evidence_digest="sha256:" + "4" * 64,
            observed_at=_fresh_evidence_time(),
            eta="около двух часов",
            delta="В рабочем дереве подтверждены целевые изменения.",
            current="Завершается основной diff.",
        )
        assert fallback["progress_percent"] == 25
        legacy_identity = "legacy-zero-progress-v1"
        _register(registry, legacy_identity, "legacy-zero")
        with registry.connect() as connection:
            connection.execute(
                "UPDATE tasks SET progress_percent=0,last_delta='Задача зарегистрирована.',"
                "current_action='Исполнитель начинает работу.' WHERE task_id=?",
                (legacy_identity,),
            )
        recovered_zero = registry.apply_progress(
            task_id=legacy_identity,
            expected_revision=1,
            run_owner="heartbeat-legacy-zero",
            observed_stage=ProgressStage.IMPLEMENTATION_STARTED,
            observed_evidence_digest="sha256:" + "5" * 64,
            observed_at=_fresh_evidence_time(),
            eta="около часа",
            delta="Подтверждены изменения файлов и запуск проверок.",
            current="Продолжаются проверки реализации.",
        )
        assert recovered_zero["progress_percent"] == 25

        # Linked objective GitHub/Release states outrank executor self-report.
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="open",
        )
        pr_created = registry.apply_progress(
            task_id=identity,
            expected_revision=4,
            run_owner="heartbeat-progress-4",
            objective_stage=ProgressStage.PR_CREATED,
            objective_evidence_digest="sha256:" + "6" * 64,
            objective_at=_fresh_evidence_time(),
            eta="до часа",
            delta="Создан PR с проверяемым head.",
            current="Ожидаются проверки и допуск выпуска.",
        )
        assert pr_created["progress_percent"] == 72
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="ready",
        )
        admitted = registry.apply_progress(
            task_id=identity,
            expected_revision=5,
            run_owner="heartbeat-progress-5",
            objective_stage=ProgressStage.RELEASE_ADMITTED,
            objective_evidence_digest="sha256:" + "7" * 64,
            objective_at=_fresh_evidence_time(),
            eta="очередь выпуска",
            delta="Проверки зелёные и допуск выпуска подтверждён.",
            current="Ожидается своя очередь Release Train.",
        )
        assert admitted["progress_percent"] == 80
        assert admitted["status"] == "READY_FOR_RELEASE"
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="deployed",
        )
        try:
            registry.apply_progress(
                task_id=identity,
                expected_revision=6,
                run_owner="heartbeat-invalid-repo-deploy",
                objective_stage=ProgressStage.DEPLOYED_VERIFYING,
                objective_evidence_digest="sha256:" + "8" * 64,
                objective_at=_fresh_evidence_time(),
                eta="финальная проверка",
                delta="Deploy не применим к этой задаче.",
                current="Проверяется контракт.",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("repo-only progress must not invent a deploy stage")
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="running",
        )
        releasing = registry.apply_progress(
            task_id=identity,
            expected_revision=6,
            run_owner="heartbeat-progress-6",
            objective_stage=ProgressStage.RELEASE_RUNNING,
            objective_evidence_digest="sha256:" + "9" * 64,
            objective_at=_fresh_evidence_time(),
            eta="несколько минут",
            delta="Merge и выпуск выполняются.",
            current="Release Train завершает repo-only выпуск.",
        )
        assert releasing["progress_percent"] == 88
        assert releasing["status"] == "RELEASE_OWNED"

        # Ordinary findings refresh the visible work without a reset. Only an
        # objective contradiction may move exactly one milestone backward.
        finding = registry.apply_progress(
            task_id=identity,
            expected_revision=7,
            run_owner="heartbeat-progress-finding",
            observed_stage=ProgressStage.PRIMARY_CHECKS_PASSED,
            observed_evidence_digest="sha256:" + "a" * 64,
            observed_at=_fresh_evidence_time(),
            eta="до часа",
            delta="Semantic review нашёл локальную правку теста.",
            current="Исправляется finding без отмены доказанного выпуска.",
        )
        assert finding["progress_percent"] == 88
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="ready",
        )
        try:
            registry.apply_progress(
                task_id=identity,
                expected_revision=8,
                run_owner="heartbeat-invalid-regression",
                objective_stage=ProgressStage.PR_CREATED,
                objective_evidence_digest="sha256:" + "b" * 64,
                objective_at=_fresh_evidence_time(),
                eta="до часа",
                delta="Недостаточный возврат.",
                current="Проверяется состояние.",
                invalidate=True,
                invalidation_reason="Ранее заявленный этап объективно опровергнут.",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("progress invalidation must be limited to one milestone")
        invalidated = registry.apply_progress(
            task_id=identity,
            expected_revision=8,
            run_owner="heartbeat-progress-7",
            objective_stage=ProgressStage.RELEASE_ADMITTED,
            objective_evidence_digest="sha256:" + "c" * 64,
            objective_at=_fresh_evidence_time(),
            eta="до часа",
            delta="Этот текст заменяется явной причиной.",
            current="Повторно подтверждается запуск выпуска.",
            invalidate=True,
            invalidation_reason="Ранее заявленный запуск выпуска объективно опровергнут.",
        )
        assert invalidated["progress_percent"] == 80
        assert invalidated["status"] == "RECOVERING"
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="running",
        )
        recovered = registry.apply_progress(
            task_id=identity,
            expected_revision=9,
            run_owner="heartbeat-progress-8",
            objective_stage=ProgressStage.RELEASE_RUNNING,
            objective_evidence_digest="sha256:" + "d" * 64,
            objective_at=_fresh_evidence_time(),
            eta="несколько минут",
            delta="Запуск выпуска подтверждён повторно.",
            current="Release Train завершает выпуск.",
        )
        assert recovered["progress_percent"] == 88

        try:
            registry.update_task(
                task_id=identity,
                expected_revision=10,
                status=TaskStatus.RELEASE_OWNED,
                progress=100,
                eta="готово",
                delta="Работа завершена.",
                current="Куратор готовит итог.",
                blocker=None,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("100% must remain terminal-only")
        try:
            registry.enqueue_attention(
                task_id=identity,
                expected_revision=10,
                kind=AttentionKind.TECHNICAL_COMPLETION,
                completion_evidence_class="release:done",
                evidence_summary="Терминальное состояние ещё не доказано.",
                evidence_digest="sha256:" + "e" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("100% must require linked terminal PR evidence")
        registry.link_pr(
            task_id=identity,
            pr=920,
            role="implementation",
            head_sha="a" * 40,
            state="done",
        )
        completion = registry.enqueue_attention(
            task_id=identity,
            expected_revision=10,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:done",
            evidence_summary="Repo-only выпуск и exact origin-main подтверждены.",
            evidence_digest="sha256:" + "e" * 64,
            eta="готово",
            delta="Техническое завершение подтверждено.",
            current="Куратор подтверждает получение результата.",
        )
        assert completion["task_revision"] == 11
        assert registry.progress_state(task_id=identity)["progress_percent"] == 100

        early_invalidation_id = "early-invalidation-v1"
        _register(registry, early_invalidation_id, "early-invalidation")
        registry.link_pr(
            task_id=early_invalidation_id,
            pr=923,
            role="implementation",
            head_sha="d" * 40,
            state="open",
        )
        assert registry.apply_progress(
            task_id=early_invalidation_id,
            expected_revision=1,
            run_owner="heartbeat-early-pr",
            objective_stage=ProgressStage.PR_CREATED,
            objective_evidence_digest="sha256:" + "f" * 64,
            objective_at=_fresh_evidence_time(),
            eta="до часа",
            delta="Создан PR.",
            current="Ожидаются проверки.",
        )["progress_percent"] == 72
        registry.link_pr(
            task_id=early_invalidation_id,
            pr=923,
            role="implementation",
            head_sha="d" * 40,
            state="closed",
        )
        invalidated_pr = registry.apply_progress(
            task_id=early_invalidation_id,
            expected_revision=2,
            run_owner="heartbeat-early-pr-invalidated",
            objective_stage=ProgressStage.FULL_CHECKS_PASSED,
            objective_evidence_digest="sha256:" + "0" * 64,
            objective_at=_fresh_evidence_time(),
            eta="около часа",
            delta="Этот текст заменяется явной причиной.",
            current="Готовится новый PR.",
            invalidate=True,
            invalidation_reason="Ранее заявленный PR больше не существует; проверки сохранены.",
        )
        assert invalidated_pr["progress_percent"] == 65
        assert invalidated_pr["status"] == "RECOVERING"

    # Closure mapping is contour-aware: a LOOP/live task needs production,
    # while a diagnostic task never invents PR/deploy stages.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "contour-registry")
        registry.initialize()
        live_id = "live-progress-v1"
        live_passport = _passport("live-progress", live_id)
        live_passport["scope"]["execution_contour"] = "live-runtime"
        registry.register_task(
            task_id=live_id,
            title="Task live-progress",
            repo="orenvlad-ai/wb-core",
            project_id="wb-core",
            objective="Complete task live-progress",
            passport=live_passport,
            curator_thread_id="curator-live-progress",
            executor_thread_id="executor-live-progress",
            host_id="host-1",
            curator_pin_readback_digest="sha256:" + "c" * 64,
            executor_pin_readback_digest="sha256:" + "e" * 64,
        )
        registry.link_pr(
            task_id=live_id,
            pr=921,
            role="root",
            head_sha="b" * 40,
            state="release:production",
        )
        revision = 1
        for index in range(3):
            applied = registry.apply_progress(
                task_id=live_id,
                expected_revision=revision,
                run_owner=f"heartbeat-live-{index}",
                objective_stage=ProgressStage.DEPLOYED_VERIFYING,
                objective_evidence_digest="sha256:" + str(index + 1) * 64,
                objective_at=_fresh_evidence_time(),
                eta="финальная UI-проверка",
                delta="Deploy подтверждён; выполняется визуальная приёмка.",
                current="Проверяется production UI Flow.",
            )
            revision = int(applied["revision"])
        assert applied["progress_percent"] == 95
        assert applied["status"] == "VERIFYING"
        try:
            registry.enqueue_attention(
                task_id=live_id,
                expected_revision=revision,
                kind=AttentionKind.TECHNICAL_COMPLETION,
                completion_evidence_class="release:done",
                evidence_summary="Неверный класс завершения.",
                evidence_digest="sha256:" + "4" * 64,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("live closure must require release:production")
        live_completion = registry.enqueue_attention(
            task_id=live_id,
            expected_revision=revision,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:production",
            evidence_summary="Deploy и production UI acceptance подтверждены.",
            evidence_digest="sha256:" + "5" * 64,
            eta="готово",
            delta="Production UI Flow принят.",
            current="Куратор подтверждает получение результата.",
        )
        assert registry.progress_state(task_id=live_id)["progress_percent"] == 100
        assert live_completion["task_revision"] == revision + 1

        diagnostic_id = "diagnostic-progress-v1"
        diagnostic_passport = _passport("diagnostic-progress", diagnostic_id)
        diagnostic_passport["scope"]["execution_contour"] = "read-only"
        registry.register_task(
            task_id=diagnostic_id,
            title="Task diagnostic-progress",
            repo="orenvlad-ai/wb-core",
            project_id="wb-core",
            objective="Complete task diagnostic-progress",
            passport=diagnostic_passport,
            curator_thread_id="curator-diagnostic-progress",
            executor_thread_id="executor-diagnostic-progress",
            host_id="host-1",
            curator_pin_readback_digest="sha256:" + "c" * 64,
            executor_pin_readback_digest="sha256:" + "e" * 64,
        )
        registry.link_pr(
            task_id=diagnostic_id,
            pr=922,
            role="implementation",
            head_sha="c" * 40,
            state="ready",
        )
        try:
            registry.apply_progress(
                task_id=diagnostic_id,
                expected_revision=1,
                run_owner="heartbeat-diagnostic-invalid",
                objective_stage=ProgressStage.RELEASE_ADMITTED,
                objective_evidence_digest="sha256:" + "6" * 64,
                objective_at=_fresh_evidence_time(),
                eta="уточняется",
                delta="Недопустимая стадия.",
                current="Проверяется контракт.",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("diagnostic progress must not invent GitHub stages")
        diagnostic_completion = registry.enqueue_attention(
            task_id=diagnostic_id,
            expected_revision=1,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="diagnostic-complete",
            evidence_summary="Диагноз подтверждён доказательствами.",
            evidence_digest="sha256:" + "7" * 64,
            eta="готово",
            delta="Диагностика завершена.",
            current="Куратор подтверждает получение результата.",
        )
        assert diagnostic_completion["task_revision"] == 2
        assert registry.progress_state(task_id=diagnostic_id)["progress_percent"] == 100

    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "quiet-heartbeat-registry")
        registry.initialize()
        _prepare_watcher(
            registry,
            generation=1,
            thread_id="quiet-watcher",
            automation_id="quiet-auto",
        )
        registry.smoke_watcher(
            generation=1,
            evidence_digest="sha256:" + "8" * 64,
        )
        registry.activate_watcher(generation=1)
        before_quiet = registry.snapshot()["watchers"][0]
        quiet = registry.heartbeat_response(automation_id="quiet-auto")
        parsed_quiet = ET.fromstring(quiet)
        assert parsed_quiet.findtext("decision") == "DONT_NOTIFY"
        assert parsed_quiet.findtext("message") == "Нет активных задач."
        assert len(parsed_quiet.findtext("message") or "") < 40
        after_quiet = registry.snapshot()["watchers"][0]
        assert after_quiet["status"] == "ACTIVE"
        assert after_quiet["automation_enabled_readback_digest"] == before_quiet[
            "automation_enabled_readback_digest"
        ]
        assert after_quiet["automation_paused_digest"] == ""


def run_smoke() -> None:
    watcher_contract = json.loads(
        (ROOT / "packages" / "contracts" / "codex_watcher_v1.json").read_text(
            encoding="utf-8"
        )
    )
    assert watcher_contract["schema"] == "wb-core-codex-watcher/v1"
    assert watcher_contract["watcher"]["target_batch_limit"] == 8
    assert watcher_contract["watcher"]["rotate_after_runs"] == 48
    assert DEFAULT_WATCHER_MAX_RUNS == watcher_contract["watcher"]["rotate_after_runs"]
    assert watcher_contract["watcher"]["automation_prompt_mode"] == (
        "bounded-trusted-repo-entrypoint"
    )
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
        "checkpoint-progress",
        "progress-state",
        "apply-progress",
        "completion-evidence-class",
        "heartbeat-response",
        "decision=NOTIFY",
        "DONT_NOTIFY",
        "wait_threads(timeoutMs: 0)",
        "record-failure",
        "close-incident",
        "reserve-attention",
        "ack-attention",
        "accept-curator",
        "pending-executor-archives",
        "prepare-owner-handoff",
        "confirm-watcher-retirement",
        "confirm-watcher-automation-enabled",
        "confirm-watcher-liveness",
        "record-watcher-liveness-failure",
        "record-watcher-context-compaction",
        "contextCompaction",
        "python3 apps/codex_task_orchestrator.py heartbeat-response",
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
    assert watcher_contract["rotation"]["run_boundary"] == {
        "not_due_run_count": 47,
        "due_run_count": 48,
    }
    assert watcher_contract["rotation"]["early_trigger"]["heuristic_detection"] is False
    assert watcher_contract["rotation"]["active_tasks_may_rotate"] is True
    assert watcher_contract["rotation"]["old_automation_pause_gate"] == (
        "successor-first-post-cutover-heartbeat-liveness"
    )
    assert watcher_contract["rotation"]["no_monitoring_gap"] is True
    assert watcher_contract["rotation"][
        "prepared_successor_task_github_attention_mutations"
    ] is False
    assert watcher_contract["role_pinning"]["assignment_time_only"] is True
    assert watcher_contract["role_pinning"]["heartbeat_repin"] is False
    assert watcher_contract["progress"]["initial_registered_percent"] == 5
    assert watcher_contract["progress"]["executor_assigns_percentage"] is False
    assert watcher_contract["progress"]["time_or_heartbeat_based_progress"] is False
    assert watcher_contract["progress"]["terminal_only"] is True
    assert watcher_contract["heartbeat_response"][
        "unchanged_active_envelopes_decision"
    ] == "NOTIFY"
    assert watcher_contract["heartbeat_response"]["single_visible_output"] is True
    assert watcher_contract["heartbeat_response"][
        "dont_notify_requires_no_active_report_blocks"
    ] is True
    assert list(PROGRESS_PERCENT_BY_STAGE.values()) == [
        0,
        5,
        15,
        25,
        40,
        55,
        65,
        72,
        80,
        88,
        95,
        100,
    ]
    assert watcher_contract["acceptance"]["required_member_addition_reopens_envelope"] is True
    assert watcher_contract["acceptance"]["owner_handoff"][
        "repeat_same_digest_on_final_surface"
    ] is True
    assert watcher_contract["rotation"][
        "old_watcher_retirement_evidence_required"
    ] is True
    assert watcher_contract["rotation"]["smoke_visible_report"][
        "forbid_raw_machine_state"
    ] is True
    assert "wb-core-arbiter-brief/v1" in arbiter_prompt
    assert "Do not request or reconstruct the full chat" in arbiter_prompt

    _run_progress_smoke()

    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "registry")
        registry.initialize()
        _register(registry, "t-task-alpha", "alpha")
        _register(registry, "t-task-bravo", "bravo")
        try:
            registry.register_task(
                task_id="missing-pin-v1",
                title="Task missing-pin",
                repo="orenvlad-ai/wb-core",
                project_id="wb-core",
                objective="Complete task missing-pin",
                passport=_passport("missing-pin", "missing-pin-v1"),
                curator_thread_id="curator-missing-pin",
                executor_thread_id="executor-missing-pin",
                host_id="host-1",
                curator_pin_readback_digest="",
                executor_pin_readback_digest="sha256:" + "e" * 64,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("launch registration must fail without curator pin readback")
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
            curator_pin_readback_digest="sha256:" + "c" * 64,
            executor_pin_readback_digest="sha256:" + "e" * 64,
        )
        assert repeated_registration["idempotent"] is True
        assert registry.add_thread(
            task_id="t-task-alpha",
            role="executor",
            generation=1,
            thread_id="executor-alpha",
            host_id="host-1",
            pin_readback_digest="sha256:" + "e" * 64,
        )["idempotent"] is True
        assert registry.confirm_role_pin(
            thread_id="curator-alpha",
            role="curator",
            pin_readback_digest="sha256:" + "c" * 64,
        )["idempotent"] is True
        try:
            registry.confirm_role_pin(
                thread_id="curator-alpha",
                role="curator",
                pin_readback_digest="sha256:" + "d" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("assignment-time pin evidence must not become heartbeat re-pin")

        updated = registry.update_task(
            task_id="t-task-alpha",
            expected_revision=1,
            status=TaskStatus.READY_FOR_RELEASE,
            progress=65,
            eta="30–60 минут",
            delta="Проверки завершены.",
            current="Ожидается допуск в Release Train.",
            blocker=None,
        )
        assert updated["revision"] == 2
        report = registry.report()
        assert "Статус: Выпуск и проверка" in report
        assert "Статус: Выпуск и проверка\nЗадача:" in report
        assert "Прогресс: ≈65% · Осталось: ≈30–60 минут" in report
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

        try:
            registry.prepare_watcher(
                generation=1,
                thread_id="watcher-missing-pin",
                host_id="host-1",
                automation_id="auto-missing-pin",
                title_readback_digest="sha256:" + "1" * 64,
                pin_readback_digest="",
                automation_readback_digest="sha256:" + "3" * 64,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("watcher preparation must fail without pin readback")
        _prepare_watcher(
            registry,
            generation=99,
            thread_id="watcher-legacy-missing-pin",
            automation_id="auto-legacy-missing-pin",
        )
        with registry.connect() as connection:
            connection.execute(
                "UPDATE watchers SET pin_readback_digest='' WHERE generation=99"
            )
        try:
            registry.smoke_watcher(
                generation=99,
                evidence_digest="sha256:" + "9" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("watcher smoke must fail without pin readback")
        with registry.connect() as connection:
            connection.execute(
                "UPDATE watchers SET smoke_digest=?,smoke_at=? WHERE generation=99",
                ("sha256:" + "9" * 64, "2026-08-03T00:00:00+00:00"),
            )
        try:
            registry.activate_watcher(generation=99)
        except RuntimeError:
            pass
        else:
            raise AssertionError("watcher activation must fail without pin readback")
        with registry.connect() as connection:
            connection.execute("DELETE FROM watchers WHERE generation=99")
        _prepare_watcher(
            registry,
            generation=1,
            thread_id="watcher-1",
            automation_id="auto-1",
        )
        assert _prepare_watcher(
            registry,
            generation=1,
            thread_id="watcher-1",
            automation_id="auto-1",
        )["idempotent"] is True
        registry.smoke_watcher(
            generation=1,
            evidence_digest="sha256:" + "1" * 64,
        )
        registry.activate_watcher(generation=1)
        first = None
        for run_number in range(1, 48):
            first = registry.begin_run(
                generation=1, owner=f"run-{run_number}", lease_seconds=60
            )
            assert first["run_count"] == run_number
            assert first["rotation_due"] is False
            assert first["rotation_reasons"] == []
            assert registry.end_run(
                generation=1, owner=f"run-{run_number}"
            )["released"] is True
        first = registry.begin_run(generation=1, owner="run-48", lease_seconds=60)
        assert first["run_count"] == 48
        assert first["rotation_due"] is True
        assert first["rotation_reasons"] == ["run-limit"]
        due_watcher_before_report = registry.snapshot()["watchers"][0]
        registry.heartbeat_response(automation_id="auto-1")
        due_watcher_after_report = registry.snapshot()["watchers"][0]
        assert due_watcher_after_report["status"] == "ACTIVE"
        assert due_watcher_after_report["automation_enabled_readback_digest"] == (
            due_watcher_before_report["automation_enabled_readback_digest"]
        )
        repeated_first = registry.begin_run(
            generation=1, owner="run-48", lease_seconds=60
        )
        assert repeated_first["idempotent"] is True
        assert repeated_first["run_count"] == first["run_count"]
        overlap = registry.begin_run(generation=1, owner="run-b", lease_seconds=60)
        assert first["acquired"] is True
        assert overlap == {"acquired": False, "reason": "overlapping-run", "owner": "run-48"}
        assert registry.end_run(generation=1, owner="run-48")["released"] is True
        assert registry.begin_run(
            generation=1, owner="rotation-old", lease_seconds=60
        )["acquired"] is True
        continuity_report = registry.report()
        _prepare_watcher(
            registry,
            generation=2,
            thread_id="watcher-2",
            automation_id="auto-2",
        )
        prepared_watchers = registry.snapshot()["watchers"]
        assert len(prepared_watchers) == 2
        assert sum(bool(item["automation_enabled_readback_digest"]) for item in prepared_watchers) == 2
        assert sum(item["status"] == "ACTIVE" for item in prepared_watchers) == 1
        try:
            registry.activate_watcher(generation=2)
        except RuntimeError:
            pass
        else:
            raise AssertionError("failed preactivation must not replace the active watcher")
        assert registry.begin_run(generation=2, owner="too-early", lease_seconds=60) == {
            "acquired": False,
            "reason": "stale-watcher-generation",
        }
        assert registry.begin_run(
            generation=1, owner="rotation-old", lease_seconds=60
        )["idempotent"] is True
        registry.smoke_watcher(
            generation=2,
            evidence_digest="sha256:" + "2" * 64,
        )
        registry.activate_watcher(generation=2)
        assert registry.report() == continuity_report
        assert registry.begin_run(generation=1, owner="stale", lease_seconds=60) == {
            "acquired": False,
            "reason": "stale-watcher-generation",
        }
        fresh = registry.begin_run(generation=2, owner="fresh", lease_seconds=60)
        assert fresh["acquired"] is True
        assert fresh["rotation_due"] is False
        liveness_failure = registry.record_watcher_liveness_failure(
            generation=2,
            automation_id="auto-2",
            failure_digest="sha256:" + "f" * 64,
        )
        assert liveness_failure["recovery_action"] == (
            "retry-active-successor-same-automation"
        )
        assert registry.end_run(generation=2, owner="fresh")["released"] is True
        registry.confirm_watcher_automation_enabled(
            generation=1,
            automation_id="auto-1",
            readback_digest="sha256:" + "a" * 64,
        )
        try:
            registry.confirm_watcher_retirement(
                generation=1,
                successor_generation=2,
                automation_paused_digest="sha256:" + "4" * 64,
                archive_readback_digest="sha256:" + "5" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("old automation must remain enabled until successor liveness")
        handover = registry.integrity()
        assert handover["ok"] is True, handover
        assert handover["active_watchers"] == 1
        assert handover["effective_enabled_automations"] >= 1
        assert handover["incomplete_watcher_retirements"] == 1
        retry = registry.begin_run(
            generation=2, owner="fresh-retry", lease_seconds=60
        )
        assert retry["acquired"] is True
        heartbeat_readback = registry.heartbeat_response(automation_id="auto-2")
        try:
            registry.confirm_watcher_liveness(
                generation=2,
                automation_id="auto-2",
                automation_enabled_readback_digest="sha256:" + "b" * 64,
                heartbeat_readback_digest="sha256:" + "c" * 64,
                end_run_readback_digest="sha256:" + "d" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("successor liveness requires a proven end-run")
        assert registry.end_run(
            generation=2, owner="fresh-retry"
        )["released"] is True
        liveness = registry.confirm_watcher_liveness(
            generation=2,
            automation_id="auto-2",
            automation_enabled_readback_digest="sha256:" + "b" * 64,
            heartbeat_readback_digest="sha256:" + "c" * 64,
            end_run_readback_digest="sha256:" + "d" * 64,
        )
        assert heartbeat_readback.startswith("<heartbeat>")
        assert liveness["liveness"] == "PROVEN"
        assert registry.begin_run(
            generation=2, owner="context-run", lease_seconds=60
        )["acquired"] is True
        context_rotation = registry.record_watcher_context_compaction(
            generation=2,
            owner="context-run",
            thread_id="watcher-2",
            item_id="item-context-compaction-1",
            readback_digest="sha256:" + "6" * 64,
        )
        assert context_rotation["recorded"] is True
        assert context_rotation["rotation_due"] is True
        assert context_rotation["rotation_reasons"] == ["context-compaction"]
        repeated_context = registry.record_watcher_context_compaction(
            generation=2,
            owner="context-run",
            thread_id="watcher-2",
            item_id="item-context-compaction-1",
            readback_digest="sha256:" + "6" * 64,
        )
        assert repeated_context["idempotent"] is True
        assert registry.begin_run(
            generation=2, owner="context-run", lease_seconds=60
        )["rotation_due"] is True
        assert registry.end_run(
            generation=2, owner="context-run"
        )["released"] is True
        pending_retirements = registry.pending_watcher_retirements()
        assert [item["generation"] for item in pending_retirements] == [1]
        assert registry.confirm_watcher_retirement(
            generation=1,
            successor_generation=2,
            automation_paused_digest="sha256:" + "4" * 64,
            archive_readback_digest="sha256:" + "5" * 64,
        )["status"] == "ARCHIVED"
        assert registry.confirm_watcher_retirement(
            generation=1,
            successor_generation=2,
            automation_paused_digest="sha256:" + "4" * 64,
            archive_readback_digest="sha256:" + "5" * 64,
        )["idempotent"] is True

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
                "INSERT INTO task_threads(id,task_id,role,generation,thread_id,host_id,active,created_at) "
                "SELECT id,task_id,role,generation,thread_id,host_id,active,created_at "
                "FROM task_threads_v2"
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
            curator_pin_readback_digest="sha256:" + "c" * 64,
            executor_pin_readback_digest="sha256:" + "e" * 64,
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
        _prepare_watcher(
            registry,
            generation=1,
            thread_id="watcher-attention-1",
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
            progress=88,
            eta="готово",
            delta="Основная реализация завершена.",
            current="Проверяется передача результата куратору.",
            blocker=None,
        )
        registry.update_task(
            task_id="routing-child-v1",
            expected_revision=1,
            status=TaskStatus.WORKING,
            progress=40,
            eta="около часа",
            delta="Добавлен надёжный маршрут уведомления.",
            current="Проверяется PR в GitHub; Watcher продолжает наблюдение C3.",
            blocker=None,
        )
        localized = registry.report()
        assert localized.count("Статус:") == 2
        assert localized.count("Задача: Глобальная оркестрация") == 1
        assert "Задача: Независимая задача" in localized
        assert "Прогресс: ≈40%" in localized
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
        assert "Изменений нет; работа продолжается:" not in first_recorded
        heartbeat = registry.heartbeat_response(automation_id="watcher-attention-auto")
        parsed_heartbeat = ET.fromstring(heartbeat)
        assert parsed_heartbeat.findtext("decision") == "NOTIFY"
        heartbeat_message = parsed_heartbeat.findtext("message") or ""
        assert heartbeat_message == registry.report()
        assert heartbeat_message.count("Статус:") == 2
        assert heartbeat_message.count("Изменений нет; работа продолжается:") == 2
        assert "Задача: Глобальная оркестрация" in heartbeat_message
        assert "Задача: Независимая задача" in heartbeat_message
        assert heartbeat.count("<heartbeat>") == 1
        assert heartbeat.count("</heartbeat>") == 1
        repeated_heartbeat = ET.fromstring(
            registry.heartbeat_response(automation_id="watcher-attention-auto")
        )
        assert repeated_heartbeat.findtext("decision") == "NOTIFY"
        assert (repeated_heartbeat.findtext("message") or "").count("Статус:") == 2
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
                progress=40,
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
        with registry.connect() as connection:
            connection.execute(
                "UPDATE task_threads SET pin_readback_digest='',pin_confirmed_at=NULL "
                "WHERE task_id='routing-child-v1' AND role='executor'"
            )
        try:
            registry.register_executor_succession(
                envelope_id=envelope_id,
                predecessor_task_id="routing-root-v1",
                successor_task_id="routing-child-v1",
                reason="successor without pin evidence",
                checkpoint_digest="sha256:" + "1" * 64,
                target_readback_digest="sha256:" + "2" * 64,
                prompt_delivery_digest="sha256:" + "3" * 64,
                registry_link_digest="sha256:" + "4" * 64,
                successor_active_digest="sha256:" + "5" * 64,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("executor succession must fail without successor pin evidence")
        registry.confirm_role_pin(
            thread_id="executor-c3",
            role="executor",
            pin_readback_digest="sha256:" + "e" * 64,
        )
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

        _prove_repo_done(registry, "routing-root-v1", 901)
        root_event = registry.enqueue_attention(
            task_id="routing-root-v1",
            expected_revision=2,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:done",
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
            completion_evidence_class="release:done",
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

        _prove_repo_done(registry, "routing-child-v1", 902)
        child_event = registry.enqueue_attention(
            task_id="routing-child-v1",
            expected_revision=2,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:done",
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
        try:
            registry.prepare_owner_handoff(
                curator_thread_id=curator,
                envelope_id=envelope_id,
                expected_revision=int(child_ack["acceptance_envelope_revision"]),
                done=["routing-child-v1 завершён."],
                verified="Проверки завершены.",
            )
        except ValueError:
            pass
        else:
            raise AssertionError("owner handoff must reject raw task identities")
        handoff = registry.prepare_owner_handoff(
            curator_thread_id=curator,
            envelope_id=envelope_id,
            expected_revision=int(child_ack["acceptance_envelope_revision"]),
            done=[
                "Выпуск завершён, адресная доставка результата работает.",
                "Текущий исполнитель передал проверенный итог куратору.",
            ],
            verified="Тесты, выпуск и текущее состояние подтверждены.",
        )
        assert handoff["handoff_text"] == (
            "Статус: Завершена — требуется приёмка\n"
            "Сделано: Выпуск завершён, адресная доставка результата работает. "
            "Текущий исполнитель передал проверенный итог куратору.\n"
            "Проверено: Тесты, выпуск и текущее состояние подтверждены.\n"
            "Ответьте ровно: «Задача принята»"
        )
        assert registry.prepare_owner_handoff(
            curator_thread_id=curator,
            envelope_id=envelope_id,
            expected_revision=int(child_ack["acceptance_envelope_revision"]),
            done=[
                "Выпуск завершён, адресная доставка результата работает.",
                "Текущий исполнитель передал проверенный итог куратору.",
            ],
            verified="Тесты, выпуск и текущее состояние подтверждены.",
        )["idempotent"] is True
        registry.confirm_owner_notification(
            curator_thread_id=curator,
            envelope_id=envelope_id,
            expected_revision=int(child_ack["acceptance_envelope_revision"]),
            notification_evidence_digest=str(handoff["handoff_digest"]),
        )
        assert "Сейчас: Ожидается приёмка владельца." in registry.report()
        assert registry.confirm_owner_notification(
            curator_thread_id=curator,
            envelope_id=envelope_id,
            expected_revision=int(child_ack["acceptance_envelope_revision"]),
            notification_evidence_digest=str(handoff["handoff_digest"]),
        )["idempotent"] is True
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

    # A new required corrective member reopens an already owner-notified
    # envelope atomically. The previous summary is revision-stale and cannot
    # authorize acceptance; the corrective terminal ack requires a new handoff.
    with tempfile.TemporaryDirectory() as temporary:
        registry = Registry(Path(temporary) / "reopen-registry")
        registry.initialize()
        curator = "curator-reopen"
        envelope = "reopen-envelope-v1"
        _register_with_curator(
            registry,
            identity="reopen-root-v1",
            suffix="reopen-root",
            curator=curator,
            executor="executor-reopen-root",
            envelope=envelope,
            envelope_title="Проверка повторной сдачи",
            role="root",
            title="Проверка повторной сдачи",
        )
        _prove_repo_done(registry, "reopen-root-v1", 903)
        root_event = registry.enqueue_attention(
            task_id="reopen-root-v1",
            expected_revision=1,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:done",
            evidence_summary="Первый этап завершён и проверен.",
            evidence_digest="sha256:" + "1" * 64,
            eta="готово",
            delta="Первый этап завершён.",
            current="Куратор готовит итог владельцу.",
        )
        root_ack = registry.ack_attention(
            event_id=str(root_event["event_id"]),
            event_digest=str(root_event["event_digest"]),
            curator_thread_id=curator,
            expected_task_revision=2,
            ack_evidence_digest="sha256:" + "2" * 64,
        )
        first_revision = int(root_ack["acceptance_envelope_revision"])
        first_handoff = registry.prepare_owner_handoff(
            curator_thread_id=curator,
            envelope_id=envelope,
            expected_revision=first_revision,
            done=["Первый этап завершён."],
            verified="Проверки первого этапа успешны.",
        )
        registry.confirm_owner_notification(
            curator_thread_id=curator,
            envelope_id=envelope,
            expected_revision=first_revision,
            notification_evidence_digest=str(first_handoff["handoff_digest"]),
        )
        _register_with_curator(
            registry,
            identity="reopen-child-v1",
            suffix="reopen-child",
            curator=curator,
            executor="executor-reopen-child",
            envelope=envelope,
            envelope_title="Проверка повторной сдачи",
            role="corrective",
            title="Корректирующий этап",
        )
        snapshot = registry.snapshot()
        reopened = next(
            item
            for item in snapshot["acceptance_envelopes"]
            if item["envelope_id"] == envelope
        )
        assert reopened["status"] == "OPEN"
        assert reopened["owner_notified_at"] is None
        assert reopened["owner_notification_digest"] == ""
        assert reopened["owner_notification_revision"] == 0
        assert int(reopened["revision"]) > first_revision
        assert registry.bind_acceptance_envelope(
            envelope_id=envelope,
            title="Проверка повторной сдачи",
            curator_thread_id=curator,
            root_task_id="reopen-root-v1",
            corrective_task_ids=["reopen-child-v1"],
        )["idempotent"] is True
        assert registry.integrity()["ok"] is True, registry.integrity()
        try:
            registry.accept_curator(
                curator_thread_id=curator,
                expected_envelope_revision=first_revision,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("stale owner notification must not accept a reopened envelope")
        _prove_repo_done(registry, "reopen-child-v1", 904)
        child_event = registry.enqueue_attention(
            task_id="reopen-child-v1",
            expected_revision=1,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:done",
            evidence_summary="Корректирующий этап завершён и проверен.",
            evidence_digest="sha256:" + "3" * 64,
            eta="готово",
            delta="Корректирующий этап завершён.",
            current="Куратор готовит новый итог владельцу.",
        )
        child_ack = registry.ack_attention(
            event_id=str(child_event["event_id"]),
            event_digest=str(child_event["event_digest"]),
            curator_thread_id=curator,
            expected_task_revision=2,
            ack_evidence_digest="sha256:" + "4" * 64,
        )
        assert child_ack["acceptance_envelope_state"] == "AWAITING_ACCEPTANCE"
        assert child_ack["owner_notification_required"] is True
        second_revision = int(child_ack["acceptance_envelope_revision"])
        try:
            registry.confirm_owner_notification(
                curator_thread_id=curator,
                envelope_id=envelope,
                expected_revision=second_revision,
                notification_evidence_digest=str(first_handoff["handoff_digest"]),
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("old handoff digest must not cover a corrective child")
        second_handoff = registry.prepare_owner_handoff(
            curator_thread_id=curator,
            envelope_id=envelope,
            expected_revision=second_revision,
            done=["Первый и корректирующий этапы завершены."],
            verified="Повторные проверки успешны.",
        )
        assert second_handoff["handoff_digest"] != first_handoff["handoff_digest"]
        registry.confirm_owner_notification(
            curator_thread_id=curator,
            envelope_id=envelope,
            expected_revision=second_revision,
            notification_evidence_digest=str(second_handoff["handoff_digest"]),
        )
        accepted = registry.accept_curator(
            curator_thread_id=curator,
            expected_envelope_revision=second_revision,
        )
        assert set(accepted["accepted_task_ids"]) == {
            "reopen-root-v1",
            "reopen-child-v1",
        }
        assert "Проверка повторной сдачи" not in registry.report()

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
        _prepare_watcher(
            registry,
            generation=1,
            thread_id="watcher-ambiguous",
            automation_id="ambiguous-auto",
        )
        registry.smoke_watcher(
            generation=1, evidence_digest="sha256:" + "1" * 64
        )
        registry.activate_watcher(generation=1)
        envelope_revisions = []
        for index in (1, 2):
            _prove_repo_done(registry, f"ambiguity-task-{index}", 904 + index)
            event = registry.enqueue_attention(
                task_id=f"ambiguity-task-{index}",
                expected_revision=1,
                kind=AttentionKind.TECHNICAL_COMPLETION,
                completion_evidence_class="release:done",
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
            handoff = registry.prepare_owner_handoff(
                curator_thread_id="curator-ambiguous",
                envelope_id=f"ambiguity-task-{index}",
                expected_revision=int(ack["acceptance_envelope_revision"]),
                done=[f"Независимая цель {index} завершена."],
                verified="Проверки завершены успешно.",
            )
            registry.confirm_owner_notification(
                curator_thread_id="curator-ambiguous",
                envelope_id=f"ambiguity-task-{index}",
                expected_revision=int(ack["acceptance_envelope_revision"]),
                notification_evidence_digest=str(handoff["handoff_digest"]),
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
        _prepare_watcher(
            registry,
            generation=1,
            thread_id="watcher-old",
            automation_id="old-auto",
        )
        registry.smoke_watcher(
            generation=1, evidence_digest="sha256:" + "1" * 64
        )
        registry.activate_watcher(generation=1)
        before_rotation_heartbeat = ET.fromstring(
            registry.heartbeat_response(automation_id="old-auto")
        )
        assert before_rotation_heartbeat.findtext("decision") == "NOTIFY"
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
            progress=15,
            eta="около часа",
            delta="Техническое восстановление завершено.",
            current="Работа продолжается.",
            blocker=None,
        )
        _prepare_watcher(
            registry,
            generation=2,
            thread_id="watcher-new",
            automation_id="new-auto",
        )
        registry.smoke_watcher(
            generation=2, evidence_digest="sha256:" + "3" * 64
        )
        registry.activate_watcher(generation=2)
        after_rotation_heartbeat = ET.fromstring(
            registry.heartbeat_response(automation_id="new-auto")
        )
        assert after_rotation_heartbeat.findtext("decision") == "NOTIFY"
        assert "Задача: Проверка устаревшего события" in (
            after_rotation_heartbeat.findtext("message") or ""
        )
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
        _prove_repo_done(registry, "legacy-terminal-v1", 907)
        assert registry.integrity()["unproven_terminal_tasks"] == 1
        event = registry.enqueue_attention(
            task_id="legacy-terminal-v1",
            expected_revision=8,
            kind=AttentionKind.TECHNICAL_COMPLETION,
            completion_evidence_class="release:done",
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
            completion_evidence_class="release:done",
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
            completion_evidence_class="release:done",
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
